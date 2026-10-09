import asyncio
import logging
import time
from dataclasses import dataclass

from bot.aio import externally_cancelled
from bot.models import Direction

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 30


@dataclass
class SimTrade:
    sim_id: str
    source: object
    symbol: str
    direction: Direction
    entry: float
    stop_loss: float
    take_profit: float
    opened_at: float


class SignalSimulator:
    """Paper-trades signals the bot did NOT execute.

    For each skipped signal it follows the live price and records whether
    price would have reached TP1 (win) or the stop (loss) first, writing the
    hypothetical outcome to a separate journal. This measures the quality of
    filtered channels without risking money - the honest form of "learn from
    history". Resolution is polled, so a fast spike-and-reverse between checks
    can be missed (same limitation as any polling monitor).
    """

    def __init__(self, broker, journal, pip_size, timeout_hours=24, interval=CHECK_INTERVAL_SECONDS):
        self._broker = broker
        self._journal = journal
        self._pip = pip_size
        self._timeout = timeout_hours * 3600
        self._interval = interval
        self._open: list[SimTrade] = []
        self._counter = 0

    def add(self, source, symbol, direction, entry, stop_loss, take_profit, reason) -> None:
        if entry is None or stop_loss is None or take_profit is None:
            return
        self._counter += 1
        sid = f"sim{self._counter}"
        self._open.append(
            SimTrade(sid, source, symbol, direction, entry, stop_loss, take_profit, time.time())
        )
        if self._journal is not None:
            risk = abs(entry - stop_loss)
            rr = abs(take_profit - entry) / risk if risk else None
            self._journal.record_open(
                sid, source, direction, symbol, entry, stop_loss, take_profit, rr, 0.0
            )
        logger.info(
            "SIM open %s: %s %s entry=%.2f sl=%.2f tp1=%.2f (skipped: %s)",
            sid, direction, symbol, entry, stop_loss, take_profit, reason,
        )

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self.check_once()
            except asyncio.CancelledError:
                if externally_cancelled():
                    raise
                logger.warning("Simulator cycle cancelled by a reconnect")
            except Exception:
                logger.exception("Simulator cycle failed")

    async def check_once(self) -> None:
        if not self._open:
            return
        prices: dict[str, tuple] = {}
        for symbol in {t.symbol for t in self._open}:
            try:
                prices[symbol] = await self._broker.get_current_price(symbol)
            except Exception:
                logger.exception("Simulator could not read price for %s", symbol)

        now = time.time()
        still_open: list[SimTrade] = []
        for t in self._open:
            price = prices.get(t.symbol)
            if price is None:
                still_open.append(t)
                continue
            bid, ask = price
            exit_price = bid if t.direction == Direction.SELL else ask
            outcome = self._outcome(t, exit_price)
            if outcome is None and now - t.opened_at >= self._timeout:
                outcome = ("EXPIRED", None)
            if outcome is None:
                still_open.append(t)
                continue
            label, profit = outcome
            logger.info(
                "SIM %s %s: %s %s would-be P/L $%s",
                t.sim_id, label, t.direction, t.symbol,
                "n/a" if profit is None else f"{profit:.2f}",
            )
            if self._journal is not None:
                self._journal.record_close(t.sim_id, profit)
        self._open = still_open

    @staticmethod
    def _outcome(t: SimTrade, exit_price: float):
        """(label, profit$) if resolved, else None. 0.01 lot gold ~= $1/$1."""
        if t.direction == Direction.SELL:
            if exit_price <= t.take_profit:
                return "WIN", round(t.entry - t.take_profit, 2)
            if exit_price >= t.stop_loss:
                return "LOSS", round(t.entry - t.stop_loss, 2)
        else:
            if exit_price >= t.take_profit:
                return "WIN", round(t.take_profit - t.entry, 2)
            if exit_price <= t.stop_loss:
                return "LOSS", round(t.stop_loss - t.entry, 2)
        return None
