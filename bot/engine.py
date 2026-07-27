import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional

from bot.aio import externally_cancelled
from bot.broker.base import ExecutionClient
from bot.config import Settings
from bot.models import Direction, TradeSignal
from bot.position_monitor import PositionMonitor
from bot.risk import resolve_lot_size, reward_risk_ratio, take_profits_for_trades
from bot.risk_guards import DailyLossGuard
from bot.signal_parser import SignalParser

logger = logging.getLogger(__name__)

# Bounds how long a single signal can spend waiting on broker RPCs (price
# lookup, position count, order placement). Without this, an unrecognized
# broker symbol or a stuck MetaApi connection hangs the handler forever with
# no error logged, silently dropping the trade.
BROKER_CALL_TIMEOUT_SECONDS = 30
# A MetaApi reconnect can kill one attempt (TimeoutError or a cancelled
# in-flight call); signals are time-sensitive, so retry a couple of times
# before giving up - but never retry once an order may have been sent, to
# avoid duplicated trades.
EXECUTE_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 5
RECONNECT_TIMEOUT_SECONDS = 60
# How long an opened signal stays cancellable, and how long a cancellation
# blocks the same signal from being (re-)traded.
CANCEL_WINDOW = timedelta(hours=6)

# Phrases a channel uses to call off a signal. Kept specific to avoid
# mistaking normal signal wording for a cancellation.
CANCEL_PATTERN = re.compile(
    r"do\s*n[’'`]?t\s*trade|do\s+not\s+trade|no\s+trade|\bcancel"
    r"|skip\s+(this|it)|ignore\s+(this|it)|invalid\s+signal|do\s*n[’'`]?t\s+take",
    re.IGNORECASE,
)


class TradingEngine:
    """Wires a parsed Telegram message to a broker order, with basic guardrails."""

    def __init__(
        self,
        settings: Settings,
        parser: SignalParser,
        broker: ExecutionClient,
        notify: Optional[Callable[[str], Awaitable[None]]] = None,
        position_monitor: Optional[PositionMonitor] = None,
        risk_notify: Optional[Callable[[str], Awaitable[None]]] = None,
    ):
        self.settings = settings
        self.parser = parser
        self.broker = broker
        self.notify = notify
        self.risk_notify = risk_notify
        self.position_monitor = position_monitor
        self.last_signal_summary: Optional[str] = None
        self.last_signal_at: Optional[datetime] = None
        self._recent_signals: dict[tuple, datetime] = {}
        # signal fingerprint -> (opened_at, [position ids]) for cancellation.
        self._opened: dict[tuple, tuple[datetime, list[str]]] = {}
        # signal fingerprint -> cancelled_at, so a cancelled signal isn't
        # (re-)traded if it arrives again within the window.
        self._cancelled: dict[tuple, datetime] = {}
        self._daily_loss = DailyLossGuard(broker, settings.daily_loss_limit_percent)

    async def handle_message(self, text: str) -> None:
        if self.settings.honor_cancellations and CANCEL_PATTERN.search(text):
            await self._handle_cancellation(text)
            return

        signal = self.parser.parse(text)
        if signal is None:
            logger.debug("Message did not match a tradable signal, ignoring")
            return

        rr = reward_risk_ratio(signal)
        rr_text = f"{rr:.2f}" if rr is not None else "n/a"
        logger.info(
            "Parsed signal: %s %s entry=%s-%s sl=%s tp=%s R:R(TP1)=%s",
            signal.direction,
            signal.symbol,
            signal.entry_low,
            signal.entry_high,
            signal.stop_loss,
            signal.take_profits,
            rr_text,
        )
        self.last_signal_at = datetime.now(timezone.utc)
        self.last_signal_summary = (
            f"{signal.direction} {signal.symbol} entry={signal.entry_low}-{signal.entry_high} "
            f"sl={signal.stop_loss} tp={signal.take_profits} R:R={rr_text}"
        )

        if self._is_duplicate(signal):
            return

        if self._is_cancelled(signal):
            logger.warning(
                "Skipping signal the channel already cancelled: %s",
                self.last_signal_summary,
            )
            return

        if signal.stop_loss is None and self.settings.require_stop_loss:
            logger.error(
                "Refusing to trade a signal without a stop loss: %s",
                self.last_signal_summary,
            )
            if self.notify is not None:
                try:
                    await self.notify(
                        "⚠️ Autotrader ignored a signal because no stop loss "
                        f"could be read from it:\n{self.last_signal_summary}"
                    )
                except Exception:
                    logger.exception("Failed to send no-stop-loss alert")
            return

        inconsistency = self._consistency_error(signal)
        if inconsistency is not None:
            logger.error(
                "Refusing inconsistent signal (%s) - likely a parse error: %s",
                inconsistency, self.last_signal_summary,
            )
            if self.notify is not None:
                try:
                    await self.notify(
                        f"⚠️ Autotrader ignored a signal that looks misparsed "
                        f"({inconsistency}):\n{self.last_signal_summary}"
                    )
                except Exception:
                    logger.exception("Failed to send inconsistent-signal alert")
            return

        if rr is not None and rr < self.settings.min_reward_risk:
            logger.warning(
                "LOW reward:risk (%.2f < %.2f) - TP1 is nearer than the stop: %s",
                rr, self.settings.min_reward_risk, self.last_signal_summary,
            )
            if self.risk_notify is not None:
                try:
                    await self.risk_notify(
                        f"⚠️ Low reward:risk ({rr_text}) - TP1 is closer than the stop, "
                        f"so this setup risks more than TP1 pays:\n{self.last_signal_summary}"
                    )
                except Exception:
                    logger.exception("Failed to send risk-warning alert")

        state = {"order_attempted": False}
        for attempt in range(1, EXECUTE_ATTEMPTS + 1):
            try:
                await asyncio.wait_for(
                    self._execute_signal(signal, state), timeout=BROKER_CALL_TIMEOUT_SECONDS
                )
                if state.get("order_failed"):
                    await self._report_failed_signal()
                return
            except asyncio.TimeoutError:
                logger.error(
                    "Attempt %d/%d timed out after %ds waiting on broker (check "
                    "BROKER_SYMBOL=%s is a valid symbol on your MT5 account)",
                    attempt, EXECUTE_ATTEMPTS, BROKER_CALL_TIMEOUT_SECONDS,
                    self.settings.broker_symbol,
                )
            except asyncio.CancelledError:
                if externally_cancelled():
                    raise
                # The MetaApi SDK cancelled its own in-flight call (its socket
                # reconnected mid-request). Seen in production as a signal
                # that parsed and then vanished with no trade and no error.
                logger.error(
                    "Attempt %d/%d: broker call was cancelled by the MetaApi "
                    "client (connection reset mid-call)",
                    attempt, EXECUTE_ATTEMPTS,
                )
            except Exception:  # noqa: BLE001 - surface any unexpected error instead of dropping it silently
                logger.exception(
                    "Attempt %d/%d: unexpected error executing signal",
                    attempt, EXECUTE_ATTEMPTS,
                )

            if state["order_attempted"]:
                logger.error(
                    "Not retrying: an order may already have reached the broker - "
                    "check the MT5 account manually"
                )
                break
            if attempt < EXECUTE_ATTEMPTS:
                # Retrying on the same dead connection just fails again -
                # rebuild it first so the retry has a fresh one.
                await self._rebuild_broker_connection()
                await asyncio.sleep(RETRY_DELAY_SECONDS)

        await self._report_failed_signal()

    @staticmethod
    def _fingerprint(signal: TradeSignal) -> tuple:
        return (
            signal.direction, signal.symbol, signal.entry_low, signal.entry_high,
            signal.stop_loss, tuple(signal.take_profits),
        )

    def _is_cancelled(self, signal: TradeSignal) -> bool:
        cancelled_at = self._cancelled.get(self._fingerprint(signal))
        return (
            cancelled_at is not None
            and datetime.now(timezone.utc) - cancelled_at < CANCEL_WINDOW
        )

    async def _handle_cancellation(self, text: str) -> None:
        signal = self.parser.parse(text)
        now = datetime.now(timezone.utc)
        closed: list[str] = []

        if signal is not None:
            fp = self._fingerprint(signal)
            self._cancelled[fp] = now  # block (re-)trading this signal too
            record = self._opened.pop(fp, None)
            if record is not None:
                _, position_ids = record
                for pid in position_ids:
                    if await self._close_position(pid):
                        closed.append(pid)
            logger.warning(
                "Cancellation received (%s %s) - closed %d open position(s)",
                signal.direction, signal.symbol, len(closed),
            )
        else:
            logger.warning(
                "Cancellation message with no matchable signal levels: %.60s", text
            )

        if self.notify is not None:
            if closed:
                msg = (
                    f"🛑 Channel said DON'T TRADE - closed {len(closed)} open "
                    "position(s) for that signal."
                )
            elif signal is not None:
                msg = (
                    "🛑 Channel said DON'T TRADE - no matching open position "
                    "(already closed, or it was never opened). It won't be traded."
                )
            else:
                msg = (
                    "🛑 Channel sent a cancellation I couldn't match to a signal - "
                    "please check MT5 manually."
                )
            try:
                await self.notify(msg)
            except Exception:
                logger.exception("Failed to send cancellation alert")

    async def _close_position(self, position_id: str) -> bool:
        try:
            return await asyncio.wait_for(
                self.broker.close_position(position_id), timeout=BROKER_CALL_TIMEOUT_SECONDS
            )
        except asyncio.CancelledError:
            if externally_cancelled():
                raise
            logger.warning("Close of %s cancelled by a broker reconnect", position_id)
            return False
        except Exception:
            logger.exception("Failed to close position %s", position_id)
            return False

    def _record_opened(self, signal: TradeSignal, placed_ids: list[str]) -> None:
        ids = [i for i in placed_ids if i]
        if not ids:
            return
        now = datetime.now(timezone.utc)
        self._opened[self._fingerprint(signal)] = (now, ids)
        self._opened = {
            fp: rec for fp, rec in self._opened.items() if now - rec[0] < CANCEL_WINDOW
        }
        self._cancelled = {
            fp: ts for fp, ts in self._cancelled.items() if now - ts < CANCEL_WINDOW
        }

    def _is_duplicate(self, signal: TradeSignal) -> bool:
        """True when a signal with identical levels was traded recently.

        Channels repost the same signal as a reminder (seen in production:
        the same levels 30-70 minutes apart); each repost is a new message
        and would open the same trades again without this guard.
        """
        window = timedelta(minutes=self.settings.duplicate_signal_window_minutes)
        if window <= timedelta(0):
            return False

        fingerprint = self._fingerprint(signal)
        now = datetime.now(timezone.utc)
        last_seen = self._recent_signals.get(fingerprint)
        if last_seen is not None and now - last_seen < window:
            logger.info(
                "Ignoring duplicate signal (same levels seen %.0f minutes ago): %s",
                (now - last_seen).total_seconds() / 60, self.last_signal_summary,
            )
            return True

        self._recent_signals[fingerprint] = now
        # Prune expired fingerprints so the map can't grow unbounded.
        self._recent_signals = {
            fp: ts for fp, ts in self._recent_signals.items() if now - ts < window
        }
        return False

    async def _rebuild_broker_connection(self) -> None:
        reconnect = getattr(self.broker, "reconnect", None)
        if reconnect is None:
            return
        try:
            await asyncio.wait_for(reconnect(), timeout=RECONNECT_TIMEOUT_SECONDS)
            logger.info("Broker connection rebuilt before retry")
        except asyncio.CancelledError:
            if externally_cancelled():
                raise
            logger.warning("Broker reconnect was cancelled by the MetaApi client")
        except Exception:
            logger.exception("Broker reconnect failed - retrying on the old connection")

    async def _report_failed_signal(self) -> None:
        logger.error("Signal was NOT fully executed: %s", self.last_signal_summary)
        if self.notify is None:
            return
        try:
            await self.notify(
                f"🚨 Autotrader could NOT execute this signal:\n"
                f"{self.last_signal_summary}\n"
                "The broker connection kept failing. Check the MT5 account and "
                "https://app.metaapi.cloud - and check MT5 in case a duplicate "
                "or partial order went through."
            )
        except Exception:
            logger.exception("Failed to send trade-failure alert")

    async def _execute_signal(self, signal: TradeSignal, state: Optional[dict] = None) -> None:
        if state is None:
            state = {"order_attempted": False}
        volume = resolve_lot_size(self.settings)
        trade_tps = take_profits_for_trades(signal.take_profits, self.settings.trades_per_signal)
        entry_price = await self._resolve_entry_price(signal)
        order_kind = "MARKET (in zone)" if entry_price is None else f"PENDING LIMIT @ {entry_price}"

        if self.settings.dry_run:
            for i, tp in enumerate(trade_tps, start=1):
                logger.info(
                    "[DRY RUN] Trade %d/%d: %s %s %s lots=%.2f sl=%s tp=%s",
                    i,
                    len(trade_tps),
                    signal.direction,
                    signal.symbol,
                    order_kind,
                    volume,
                    signal.stop_loss,
                    tp,
                )
            return

        if await self._daily_loss.should_block():
            logger.warning(
                "Daily loss limit (%.1f%%) reached - not opening new trades today",
                self.settings.daily_loss_limit_percent,
            )
            if self.notify is not None:
                try:
                    await self.notify(
                        f"🛑 Daily loss limit ({self.settings.daily_loss_limit_percent:.0f}%) "
                        "reached - no new trades will open until tomorrow (UTC)."
                    )
                except Exception:
                    logger.exception("Failed to send daily-loss alert")
            return

        if self.settings.prevent_opposite_positions and await self._has_opposite_position(
            signal.direction
        ):
            logger.warning(
                "Skipping %s %s: an opposite-direction position is already open "
                "(conflict guard)",
                signal.direction, signal.symbol,
            )
            return

        open_positions = await self.broker.count_open_positions(self.settings.broker_symbol)
        available_slots = self.settings.max_open_positions - open_positions
        if available_slots <= 0:
            logger.warning(
                "Skipping signal: already %d open position(s) on %s (limit %d)",
                open_positions,
                signal.symbol,
                self.settings.max_open_positions,
            )
            return

        trades_to_place = trade_tps[:available_slots]
        if len(trades_to_place) < len(trade_tps):
            logger.warning(
                "Only placing %d/%d trades: max open positions limit (%d) reached",
                len(trades_to_place),
                len(trade_tps),
                self.settings.max_open_positions,
            )

        placed_ids: list[str] = []
        for i, tp in enumerate(trades_to_place, start=1):
            trade_signal = signal.model_copy(
                update={
                    "take_profits": [tp] if tp is not None else [],
                    "symbol": self.settings.broker_symbol,
                }
            )
            state["order_attempted"] = True
            result = await self.broker.place_order(trade_signal, volume, entry_price)
            if result.success:
                placed_ids.append(result.order_id or "")
                logger.info(
                    "Trade %d/%d executed (%s): id=%s tp=%s",
                    i,
                    len(trades_to_place),
                    order_kind,
                    result.order_id,
                    tp,
                )
            else:
                logger.error("Trade %d/%d failed: %s", i, len(trades_to_place), result.message)
                state["order_failed"] = True

        # Remember which positions this signal opened, so a later "don't
        # trade it" from the channel can close exactly these.
        self._record_opened(signal, placed_ids)

        # Only market fills (entry_price is None) are breakeven-managed; a
        # pending order isn't a position yet, so there's no ticket to move.
        if entry_price is None:
            self._register_breakeven(signal, placed_ids)

    def _register_breakeven(self, signal: TradeSignal, placed_ids: list[str]) -> None:
        if not self.settings.breakeven_after_tp1 or self.position_monitor is None:
            return
        ids = [i for i in placed_ids if i]
        if len(ids) < 2:
            return  # single trade - no runner to protect
        self.position_monitor.register(
            symbol=self.settings.broker_symbol,
            direction=signal.direction,
            tp1_id=ids[0],
            runner_ids=ids[1:],
        )

    @staticmethod
    def _consistency_error(signal: TradeSignal) -> Optional[str]:
        """Sanity-check the price levels against the trade direction.

        A misparse (e.g. a mangled entry of 420 while the real levels are
        ~4120) shows up as levels on the wrong side: a SELL whose stop is
        not above entry, or whose take-profits are not below it. Catching
        that here refuses the trade instead of sending a wrongly-priced
        order. Also rejects genuinely nonsensical signals (stop on the
        wrong side).
        """
        entry, sl, tps = signal.entry, signal.stop_loss, signal.take_profits
        if signal.direction == Direction.BUY:
            if entry is not None and sl is not None and sl >= entry:
                return f"BUY stop {sl} not below entry {entry}"
            if entry is not None and any(tp <= entry for tp in tps):
                return f"BUY take-profit not above entry {entry}: {tps}"
            if entry is None and sl is not None and any(tp <= sl for tp in tps):
                return f"BUY take-profit not above stop {sl}: {tps}"
        else:
            if entry is not None and sl is not None and sl <= entry:
                return f"SELL stop {sl} not above entry {entry}"
            if entry is not None and any(tp >= entry for tp in tps):
                return f"SELL take-profit not below entry {entry}: {tps}"
            if entry is None and sl is not None and any(tp >= sl for tp in tps):
                return f"SELL take-profit not below stop {sl}: {tps}"
        return None

    async def _has_opposite_position(self, direction: Direction) -> bool:
        positions = await self.broker.get_positions(self.settings.broker_symbol)
        opposite = Direction.SELL if direction == Direction.BUY else Direction.BUY
        return any(p.direction == opposite for p in positions)

    async def _resolve_entry_price(self, signal: TradeSignal) -> Optional[float]:
        """Decides whether to fire at market now or place a pending order.

        Returns None (market) if there's no entry zone, price is already
        inside it, or price is already better than the zone offers.
        Otherwise returns the near edge of the zone as a pending limit price,
        so the trade only opens once price actually reaches it.
        """
        if signal.entry_low is None or signal.entry_high is None:
            return None

        bid, ask = await self.broker.get_current_price(self.settings.broker_symbol)
        current_price = bid if signal.direction == Direction.SELL else ask

        if signal.entry_low <= current_price <= signal.entry_high:
            return None
        if signal.direction == Direction.SELL and current_price > signal.entry_high:
            return None  # already higher than the zone - even better for a sell
        if signal.direction == Direction.BUY and current_price < signal.entry_low:
            return None  # already lower than the zone - even better for a buy

        return signal.entry_low if signal.direction == Direction.SELL else signal.entry_high
