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
    best_price: float = 0.0          # best price seen so far (for trailing)
    trailing_stop: float = None      # active trailing stop, or None until armed


class SignalSimulator:
    """Paper-trades signals the bot did NOT execute.

    For each skipped signal it follows the live price and records whether
    price would have reached TP1 (win) or the stop (loss) first, writing the
    hypothetical outcome to a separate journal. This measures the quality of
    filtered channels without risking money - the honest form of "learn from
    history". Resolution is polled, so a fast spike-and-reverse between checks
    can be missed (same limitation as any polling monitor).

    An optional trailing stop can be modelled (trailing_activate_pips /
    trailing_distance_pips) so the paper-trade reflects a trailing strategy -
    independent of the live TRAILING_* settings. It trails off polled prices,
    so it is approximate, not tick-exact.
    """

    def __init__(
        self, broker, journal, pip_size, timeout_hours=24,
        interval=CHECK_INTERVAL_SECONDS,
        trailing_activate_pips=0.0, trailing_distance_pips=0.0,
    ):
        self._broker = broker
        self._journal = journal
        self._pip = pip_size
        self._timeout = timeout_hours * 3600
        self._interval = interval
        self._trail_activate = trailing_activate_pips
        self._trail_distance = trailing_distance_pips
        self._open: list[SimTrade] = []
        self._counter = 0

    def add(self, source, symbol, direction, entry, stop_loss, take_profit, reason) -> None:
        if entry is None or stop_loss is None or take_profit is None:
            return
        self._counter += 1
        sid = f"sim{self._counter}"
        self._open.append(
            SimTrade(
                sid, source, symbol, direction, entry, stop_loss, take_profit,
                time.time(), best_price=entry,
            )
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
            self._update_trailing(t, exit_price)
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

    def _update_trailing(self, t: SimTrade, price: float) -> None:
        """Arm/advance the trailing stop off the best price seen so far.

        No-op unless both trailing settings are positive. The stop only ever
        moves in the favourable direction (never loosens)."""
        if self._trail_activate <= 0 or self._trail_distance <= 0:
            return
        activate = self._trail_activate * self._pip
        distance = self._trail_distance * self._pip
        if t.direction == Direction.SELL:
            if price < t.best_price:
                t.best_price = price
            if t.entry - t.best_price >= activate:  # moved enough into profit
                new_stop = t.best_price + distance
                if t.trailing_stop is None or new_stop < t.trailing_stop:
                    t.trailing_stop = new_stop
        else:
            if price > t.best_price:
                t.best_price = price
            if t.best_price - t.entry >= activate:
                new_stop = t.best_price - distance
                if t.trailing_stop is None or new_stop > t.trailing_stop:
                    t.trailing_stop = new_stop

    @staticmethod
    def _outcome(t: SimTrade, exit_price: float):
        """(label, profit$) if resolved, else None. 0.01 lot gold ~= $1/$1.

        TP1 is checked first, then a trailing stop (if armed), then the
        original stop - so a trade that ran into profit and reversed exits at
        the trailed level, not the full stop."""
        if t.direction == Direction.SELL:
            if exit_price <= t.take_profit:
                return "WIN", round(t.entry - t.take_profit, 2)
            if t.trailing_stop is not None and exit_price >= t.trailing_stop:
                return "TRAIL", round(t.entry - t.trailing_stop, 2)
            if exit_price >= t.stop_loss:
                return "LOSS", round(t.entry - t.stop_loss, 2)
        else:
            if exit_price >= t.take_profit:
                return "WIN", round(t.take_profit - t.entry, 2)
            if t.trailing_stop is not None and exit_price <= t.trailing_stop:
                return "TRAIL", round(t.trailing_stop - t.entry, 2)
            if exit_price <= t.stop_loss:
                return "LOSS", round(t.stop_loss - t.entry, 2)
        return None
