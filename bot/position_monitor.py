import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from bot.aio import externally_cancelled
from bot.broker.base import ExecutionClient
from bot.models import Direction
from bot.signal_parser import GOLD_PIP_SIZE

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 20


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
        trailing_activate_pips: float = 0.0,
        trailing_distance_pips: float = 0.0,
        pip_size: float = GOLD_PIP_SIZE,
    ):
        self._broker = broker
        self._notify = notify
        self._interval = interval
        self._journal = journal
        self._activate = trailing_activate_pips * pip_size
        self._distance = trailing_distance_pips * pip_size
        self._managed: list[ManagedTrade] = []
        # position id -> symbol, for every bot position (close detection +
        # trailing). Independent of the journal so trailing works either way.
        self._tracked: dict[str, str] = {}
        # position id -> best (most favourable) price seen, for trailing.
        self._peak: dict[str, float] = {}

    def track_for_journal(self, position_id: str, symbol: str) -> None:
        if position_id:
            self._tracked[position_id] = symbol

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
        if not self._managed and not self._tracked:
            return

        symbols = {m.symbol for m in self._managed} | set(self._tracked.values())
        open_by_symbol: dict[str, dict[str, object]] = {}
        for symbol in symbols:
            positions = await self._broker.get_positions(symbol)
            open_by_symbol[symbol] = {p.id: p for p in positions}

        await self._reconcile_closed(open_by_symbol)
        await self._apply_trailing(open_by_symbol)

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

    async def _reconcile_closed(self, open_by_symbol) -> None:
        for pid, symbol in list(self._tracked.items()):
            if pid in open_by_symbol.get(symbol, {}):
                continue  # still open
            # Closed since last check - record its realized P/L if journaling.
            if self._journal is not None:
                try:
                    profit = await self._broker.get_closed_profit(pid)
                except Exception:
                    logger.exception("Could not read closed profit for %s", pid)
                    profit = None
                self._journal.record_close(pid, profit)
            del self._tracked[pid]

    async def _apply_trailing(self, open_by_symbol) -> None:
        if self._activate <= 0:
            return
        open_ids = set()
        for symbol, positions in open_by_symbol.items():
            if not positions:
                continue
            try:
                bid, ask = await self._broker.get_current_price(symbol)
            except Exception:
                logger.exception("Trailing: could not read price for %s", symbol)
                continue
            for pid, p in positions.items():
                open_ids.add(pid)
                await self._trail_one(pid, p, bid, ask)
        # Forget peaks of positions that have closed.
        self._peak = {k: v for k, v in self._peak.items() if k in open_ids}

    async def _trail_one(self, pid, position, bid, ask) -> None:
        if position.direction == Direction.BUY:
            exit_price = bid  # a buy is closed at the bid
            if exit_price - position.open_price < self._activate:
                return
            peak = max(self._peak.get(pid, exit_price), exit_price)
            self._peak[pid] = peak
            new_sl = round(peak - self._distance, 2)
            if position.stop_loss is None or new_sl > position.stop_loss + 1e-9:
                await self._broker.modify_stop_loss(pid, new_sl)
        else:
            exit_price = ask  # a sell is closed at the ask
            if position.open_price - exit_price < self._activate:
                return
            peak = min(self._peak.get(pid, exit_price), exit_price)
            self._peak[pid] = peak
            new_sl = round(peak + self._distance, 2)
            if position.stop_loss is None or new_sl < position.stop_loss - 1e-9:
                await self._broker.modify_stop_loss(pid, new_sl)

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
