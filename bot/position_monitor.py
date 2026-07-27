import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from bot.aio import externally_cancelled
from bot.broker.base import ExecutionClient
from bot.models import Direction

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 30


@dataclass
class ManagedTrade:
    """A signal's set of trades that should be moved to breakeven after TP1.

    tp1_id is the nearest-take-profit position (placed first); runner_ids are
    the further-out positions sharing the same original stop loss.
    """

    symbol: str
    direction: Direction
    tp1_id: str
    runner_ids: list[str]
    breakeven_done: bool = False


class PositionMonitor:
    """Moves runner positions to breakeven once their TP1 sibling closes.

    Because every trade in a signal shares the same stop loss, if the TP1
    position has closed while runners are still open, price must have reached
    TP1 (a stop-out would have closed the runners too). That is the moment to
    lift each runner's stop to its own entry, so a banked winner can't reverse
    into a loss - the exact "TP2 never hit, came back to SL" failure.
    """

    def __init__(
        self,
        broker: ExecutionClient,
        notify: Optional[Callable[[str], Awaitable[None]]] = None,
        interval: int = CHECK_INTERVAL_SECONDS,
        journal=None,
    ):
        self._broker = broker
        self._notify = notify
        self._interval = interval
        self._journal = journal
        self._managed: list[ManagedTrade] = []
        # position id -> symbol, for detecting closes to journal.
        self._journal_tracked: dict[str, str] = {}

    def track_for_journal(self, position_id: str, symbol: str) -> None:
        if self._journal is not None and position_id:
            self._journal_tracked[position_id] = symbol

    def register(
        self, symbol: str, direction: Direction, tp1_id: str, runner_ids: list[str]
    ) -> None:
        runner_ids = [rid for rid in runner_ids if rid]
        if not tp1_id or not runner_ids:
            return  # nothing to manage (single trade, or missing ids)
        self._managed.append(
            ManagedTrade(symbol=symbol, direction=direction, tp1_id=tp1_id, runner_ids=runner_ids)
        )
        logger.info(
            "Breakeven-managing %d runner(s) on %s %s after TP1 (%s)",
            len(runner_ids), symbol, direction, tp1_id,
        )

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self.check_once()
            except asyncio.CancelledError:
                if externally_cancelled():
                    raise
                logger.warning("Position monitor cycle cancelled by a reconnect")
            except Exception:
                logger.exception("Position monitor cycle failed")

    async def check_once(self) -> None:
        if not self._managed and not self._journal_tracked:
            return

        symbols = {m.symbol for m in self._managed} | set(self._journal_tracked.values())
        open_by_symbol: dict[str, dict[str, object]] = {}
        for symbol in symbols:
            positions = await self._broker.get_positions(symbol)
            open_by_symbol[symbol] = {p.id: p for p in positions}

        await self._journal_closed_positions(open_by_symbol)

        still_managed: list[ManagedTrade] = []
        for m in self._managed:
            open_map = open_by_symbol.get(m.symbol, {})
            open_runners = [rid for rid in m.runner_ids if rid in open_map]

            if not open_runners:
                continue  # all runners closed - done managing this group

            tp1_open = m.tp1_id in open_map
            if not tp1_open and not m.breakeven_done:
                await self._move_runners_to_breakeven(m, open_map, open_runners)
                m.breakeven_done = True
            still_managed.append(m)

        self._managed = still_managed

    async def _journal_closed_positions(self, open_by_symbol) -> None:
        if self._journal is None or not self._journal_tracked:
            return
        for pid, symbol in list(self._journal_tracked.items()):
            if pid in open_by_symbol.get(symbol, {}):
                continue  # still open
            # Closed since last check - record its realized P/L.
            try:
                profit = await self._broker.get_closed_profit(pid)
            except Exception:
                logger.exception("Could not read closed profit for %s", pid)
                profit = None
            self._journal.record_close(pid, profit)
            del self._journal_tracked[pid]

    async def _move_runners_to_breakeven(self, m, open_map, open_runners) -> None:
        moved = 0
        for rid in open_runners:
            position = open_map[rid]
            breakeven_price = position.open_price
            try:
                if await self._broker.modify_stop_loss(rid, breakeven_price):
                    moved += 1
            except Exception:
                logger.exception("Failed to move position %s to breakeven", rid)

        logger.info(
            "TP1 hit on %s %s - moved %d/%d runner(s) to breakeven",
            m.symbol, m.direction, moved, len(open_runners),
        )
        if moved and self._notify is not None:
            try:
                await self._notify(
                    f"🟢 TP1 hit on {m.symbol} {m.direction.value} - stop moved to "
                    f"breakeven on {moved} runner(s). They can no longer lose."
                )
            except Exception:
                logger.exception("Failed to send breakeven alert")
