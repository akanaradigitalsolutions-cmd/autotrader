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
from bot.trade_pause import TradePause
from bot.signal_parser import GOLD_PIP_SIZE, SignalParser

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

def _ema(values: list[float], period: int) -> float:
    k = 2.0 / (period + 1)
    ema = values[0]
    for v in values[1:]:
        ema = v * k + ema * (1 - k)
    return ema


# Phrases a channel uses to call off a signal. Kept specific to avoid
# mistaking normal signal wording for a cancellation.
CANCEL_PATTERN = re.compile(
    r"do\s*n[’'`]?t\s*trade|do\s+not\s+trade|no\s+trade|\bcancel"
    r"|skip\s+(this|it)|ignore\s+(this|it)|invalid\s+signal|do\s*n[’'`]?t\s+take"
    # "close at entry" and friends: the channel wants the trade flattened -
    # cancel it if it hasn't filled, close it if it has. Anchored to the verb
    # "close"/"exit" + an object so ordinary prose ("market will close soon")
    # or a breakeven instruction ("move SL to entry") never matches.
    r"|closed?\s+(at|on)\s+entry"
    r"|closed?\s+(at|on)\s+(be\b|break\s*even)"
    r"|close\s+(the\s+|this\s+)?(trade|position|order|deal|all|everything)"
    r"|exit\s+(the\s+)?(trade|position|now|at\s+entry)",
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
        journal=None,
        simulator=None,
        pause: Optional[TradePause] = None,
    ):
        self.settings = settings
        self.parser = parser
        self.broker = broker
        self.notify = notify
        self.risk_notify = risk_notify
        self.position_monitor = position_monitor
        self.journal = journal
        self.simulator = simulator
        # Controls whether NEW trades open; cancellations and monitoring run
        # regardless. Defaults to a file-backed pause so state survives restart.
        self.pause = pause or TradePause(settings.pause_state_file)
        self.last_signal_summary: Optional[str] = None
        self.last_signal_at: Optional[datetime] = None
        self._recent_signals: dict[tuple, datetime] = {}
        # signal fingerprint -> (opened_at, [position ids], source) for
        # cancellation. `source` lets a contextless "close at entry" close the
        # right channel's most-recent trade.
        self._opened: dict[tuple, tuple[datetime, list[str], Optional[int]]] = {}
        # signal fingerprint -> cancelled_at, so a cancelled signal isn't
        # (re-)traded if it arrives again within the window.
        self._cancelled: dict[tuple, datetime] = {}
        self._daily_loss = DailyLossGuard(broker, settings.daily_loss_limit_percent)

    def _simulate(self, signal: TradeSignal, source, reason: str) -> None:
        """Paper-trade a signal the bot chose not to execute."""
        if not self.settings.simulate_skipped_signals or self.simulator is None:
            return
        stop = signal.stop_loss
        if stop is None and signal.entry is not None:
            dist = self.settings.simulate_stop_loss_pips * GOLD_PIP_SIZE
            stop = round(
                signal.entry + dist if signal.direction == Direction.SELL
                else signal.entry - dist, 2,
            )
        self.simulator.add(
            source, self.settings.broker_symbol, signal.direction,
            signal.entry, stop, signal.primary_take_profit, reason,
        )

    def _apply_fallback_stop_loss(self, signal: TradeSignal) -> TradeSignal:
        """Give a no-stop signal a synthetic stop so it can be traded safely.

        Only when FALLBACK_STOP_LOSS_PIPS is set and the signal has an entry
        but no stop. The stop goes on the correct side (above entry for a
        SELL, below for a BUY). Off by default - no-SL signals stay refused.
        """
        if (
            signal.stop_loss is not None
            or self.settings.fallback_stop_loss_pips <= 0
            or signal.entry is None
        ):
            return signal
        distance = self.settings.fallback_stop_loss_pips * GOLD_PIP_SIZE
        if signal.direction == Direction.SELL:
            stop = round(signal.entry + distance, 2)
        else:
            stop = round(signal.entry - distance, 2)
        logger.info(
            "Signal had no stop loss - applied fallback stop %.2f (%.0f pips from %.2f)",
            stop, self.settings.fallback_stop_loss_pips, signal.entry,
        )
        return signal.model_copy(update={"stop_loss": stop})

    def apply_setting(self, attr: str, value) -> None:
        """Apply a runtime setting change (from a Telegram command) so it
        takes effect immediately, including refreshing dependent guards."""
        setattr(self.settings, attr, value)
        if attr == "daily_loss_limit_percent":
            self._daily_loss.limit_percent = value

    async def handle_message(self, text: str, source: Optional[int] = None) -> None:
        if self.settings.honor_cancellations and CANCEL_PATTERN.search(text):
            await self._handle_cancellation(text, source)
            return

        signal = self.parser.parse(text)
        if signal is None:
            logger.debug("Message did not match a tradable signal, ignoring")
            return

        # Trading paused (e.g. NFP/FOMC): a real signal arrived but we don't
        # open new trades. Cancellations were already handled above, and the
        # position monitor keeps managing anything already open.
        if self.pause.is_paused():
            logger.warning(
                "Trading PAUSED (%s) - skipping signal: %.80s",
                self.pause.status_text(), text.replace("\n", " "),
            )
            if self.notify is not None:
                try:
                    await self.notify(
                        "⏸️ A signal arrived but trading is PAUSED - it was skipped.\n"
                        f"{self.pause.status_text()}\nSend /resume to trade again."
                    )
                except Exception:
                    logger.exception("Failed to send pause-skip alert")
            return

        signal = self._apply_fallback_stop_loss(signal)

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
            self._simulate(signal, source, "no stop loss")
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

        if (
            rr is not None
            and self.settings.skip_reward_risk_below > 0
            and rr < self.settings.skip_reward_risk_below
        ):
            logger.warning(
                "SKIPPING signal below R:R filter (%.2f < %.2f): %s",
                rr, self.settings.skip_reward_risk_below, self.last_signal_summary,
            )
            if self.risk_notify is not None:
                try:
                    await self.risk_notify(
                        f"⛔ Skipped a low reward:risk signal ({rr_text} < "
                        f"{self.settings.skip_reward_risk_below:.2f}) - TP1 too close to "
                        f"the stop to be worth it:\n{self.last_signal_summary}"
                    )
                except Exception:
                    logger.exception("Failed to send R:R-filter alert")
            self._simulate(signal, source, "below R:R filter")
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

        trend_reason = await self._trend_conflict(signal)
        if trend_reason is not None:
            if self.settings.trend_filter.lower() == "on":
                logger.warning(
                    "TREND FILTER: skipping %s %s - %s",
                    signal.direction, signal.symbol, trend_reason,
                )
                if self.risk_notify is not None:
                    try:
                        await self.risk_notify(
                            f"⛔ Skipped {signal.direction.value} against the trend "
                            f"({trend_reason}):\n{self.last_signal_summary}"
                        )
                    except Exception:
                        logger.exception("Failed to send trend-filter alert")
                self._simulate(signal, source, "against trend")
                return
            # shadow mode: log what it *would* skip, but still trade.
            logger.warning(
                "TREND FILTER (shadow) would skip %s %s - %s",
                signal.direction, signal.symbol, trend_reason,
            )

        state = {"order_attempted": False}
        for attempt in range(1, EXECUTE_ATTEMPTS + 1):
            try:
                await asyncio.wait_for(
                    self._execute_signal(signal, state, source, rr),
                    timeout=BROKER_CALL_TIMEOUT_SECONDS,
                )
                if state.get("order_failed"):
                    await self._report_failed_signal(state.get("last_error"))
                return
            except asyncio.TimeoutError:
                state["last_error"] = f"timed out after {BROKER_CALL_TIMEOUT_SECONDS}s"
                logger.error(
                    "Attempt %d/%d timed out after %ds waiting on broker (check "
                    "BROKER_SYMBOL=%s is a valid symbol on your MT5 account)",
                    attempt, EXECUTE_ATTEMPTS, BROKER_CALL_TIMEOUT_SECONDS,
                    self.settings.broker_symbol,
                )
            except asyncio.CancelledError:
                if externally_cancelled():
                    raise
                # The broker client cancelled its own in-flight call (socket
                # reconnected mid-request).
                state["last_error"] = "broker call cancelled (connection reset mid-call)"
                logger.error(
                    "Attempt %d/%d: broker call was cancelled (connection reset mid-call)",
                    attempt, EXECUTE_ATTEMPTS,
                )
            except Exception as exc:  # noqa: BLE001 - surface any unexpected error instead of dropping it silently
                state["last_error"] = repr(exc)
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

        await self._report_failed_signal(state.get("last_error"))

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

    async def _handle_cancellation(self, text: str, source: Optional[int] = None) -> None:
        signal = self.parser.parse(text)
        now = datetime.now(timezone.utc)
        closed: list[str] = []
        matched = False

        if signal is not None:
            # The cancellation quotes the signal's levels - match it exactly.
            fp = self._fingerprint(signal)
            self._cancelled[fp] = now  # block (re-)trading this signal too
            record = self._opened.pop(fp, None)
            if record is not None:
                matched = True
                closed = await self._flatten(record[1])
            logger.warning(
                "Cancellation received (%s %s) - closed/cancelled %d position(s)",
                signal.direction, signal.symbol, len(closed),
            )
        else:
            # A contextless "close at entry" carries no levels. It refers to
            # the trade the channel just gave, so fall back to the most recent
            # signal still open - preferring the same channel - and flatten it.
            fp = self._most_recent_opened_fingerprint(source, now)
            if fp is not None:
                matched = True
                self._cancelled[fp] = now
                record = self._opened.pop(fp)
                closed = await self._flatten(record[1])
                logger.warning(
                    "Contextless close (%.30s) - closed/cancelled %d position(s) "
                    "from the most recent signal", text.strip(), len(closed),
                )
            else:
                logger.warning(
                    "Close/cancel message with no matchable signal and nothing "
                    "recently opened to act on: %.60s", text,
                )

        if self.notify is not None:
            if closed:
                msg = (
                    f"🛑 Channel said CLOSE / DON'T TRADE - closed or cancelled "
                    f"{len(closed)} position(s) for that signal."
                )
            elif signal is not None:
                msg = (
                    "🛑 Channel said CLOSE / DON'T TRADE - no matching open position "
                    "(already closed, or it was never opened). It won't be traded."
                )
            elif matched:
                msg = (
                    "🛑 Channel said close the last trade - the most recent signal "
                    "had nothing left to close (already gone)."
                )
            else:
                msg = (
                    "🛑 Channel sent a close/cancel I couldn't match to a signal - "
                    "please check MT5 manually."
                )
            try:
                await self.notify(msg)
            except Exception:
                logger.exception("Failed to send cancellation alert")

    def _most_recent_opened_fingerprint(
        self, source: Optional[int], now: datetime
    ) -> Optional[tuple]:
        recent = [
            (ts, fp, src)
            for fp, (ts, _ids, src) in self._opened.items()
            if now - ts < CANCEL_WINDOW
        ]
        if not recent:
            return None
        # Prefer a trade from the same channel; only if none, use any recent.
        if source is not None:
            same_source = [(ts, fp) for ts, fp, src in recent if src == source]
            if same_source:
                return max(same_source)[1]
        return max((ts, fp) for ts, fp, _src in recent)[1]

    async def _flatten(self, position_ids: list[str]) -> list[str]:
        """Close filled positions and cancel still-pending orders for a signal.

        A tracked id may be a filled position OR a pending limit order that
        never reached its entry ("close at entry" often arrives before the
        zone is touched). Pending ones are cancelled; filled ones are closed.
        """
        pending: set[str] = set()
        try:
            pending = set(
                await asyncio.wait_for(
                    self.broker.get_pending_orders(self.settings.broker_symbol),
                    timeout=BROKER_CALL_TIMEOUT_SECONDS,
                )
            )
        except Exception:
            logger.exception("Could not list pending orders while cancelling")
        done: list[str] = []
        for pid in position_ids:
            if pid in pending:
                ok = await self._cancel_order(pid)
            else:
                ok = await self._close_position(pid)
            if ok:
                done.append(pid)
        return done

    async def _cancel_order(self, order_id: str) -> bool:
        try:
            return await asyncio.wait_for(
                self.broker.cancel_order(order_id), timeout=BROKER_CALL_TIMEOUT_SECONDS
            )
        except asyncio.CancelledError:
            if externally_cancelled():
                raise
            logger.warning("Cancel of pending order %s cancelled by a reconnect", order_id)
            return False
        except Exception:
            logger.exception("Failed to cancel pending order %s", order_id)
            return False

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

    def _journal_opened(self, signal, placed_ids, source, rr, volume) -> None:
        ids = [i for i in placed_ids if i]
        if not ids:
            return
        if self.journal is not None:
            for pid in ids:
                self.journal.record_open(
                    position_id=pid, source=source, direction=signal.direction,
                    symbol=self.settings.broker_symbol, entry=signal.entry,
                    sl=signal.stop_loss, tp1=signal.primary_take_profit, rr=rr,
                    volume=volume,
                )
        if self.position_monitor is not None:
            for pid in ids:
                self.position_monitor.track_for_journal(pid, self.settings.broker_symbol)

    def _record_opened(
        self, signal: TradeSignal, placed_ids: list[str], source: Optional[int] = None
    ) -> None:
        ids = [i for i in placed_ids if i]
        if not ids:
            return
        now = datetime.now(timezone.utc)
        self._opened[self._fingerprint(signal)] = (now, ids, source)
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

    async def _report_failed_signal(self, reason: Optional[str] = None) -> None:
        logger.error(
            "Signal was NOT executed: %s (reason: %s)", self.last_signal_summary, reason
        )
        if self.notify is None:
            return
        detail = f"\nBroker said: {reason}" if reason else ""
        try:
            await self.notify(
                f"🚨 Autotrader could NOT execute this signal:\n"
                f"{self.last_signal_summary}{detail}\n"
                "Check: MT5 terminal running with AutoTrading enabled, the symbol "
                "name matches the account, stops aren't too close, and there's "
                "enough margin. Also check MT5 for a duplicate/partial order."
            )
        except Exception:
            logger.exception("Failed to send trade-failure alert")

    async def _execute_signal(
        self,
        signal: TradeSignal,
        state: Optional[dict] = None,
        source: Optional[int] = None,
        rr: Optional[float] = None,
    ) -> None:
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
                state["last_error"] = result.message

        # Remember which positions this signal opened, so a later "don't
        # trade it" from the channel can close exactly these.
        self._record_opened(signal, placed_ids, source)
        self._journal_opened(signal, placed_ids, source, rr, volume)

        # Only market fills (entry_price is None) are breakeven-managed; a
        # pending order isn't a position yet, so there's no ticket to move.
        if entry_price is None:
            self._register_breakeven(signal, placed_ids)
        elif self.position_monitor is not None:
            # Pending limit orders: track them so a stale one that never fills
            # gets cancelled instead of filling late into a reversed market.
            for oid in placed_ids:
                if oid:
                    self.position_monitor.track_pending(oid, self.settings.broker_symbol)

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

    async def _trend_conflict(self, signal: TradeSignal) -> Optional[str]:
        """Reason string if the signal trades against the trend, else None.

        Trend = last close vs an EMA on the configured timeframe. A BUY below
        the EMA (downtrend) or a SELL above it (uptrend) is counter-trend.
        """
        if self.settings.trend_filter.lower() == "off":
            return None
        period = self.settings.trend_ema_period
        try:
            closes = await self.broker.get_closes(
                self.settings.broker_symbol, self.settings.trend_timeframe, period + 5
            )
        except Exception:
            logger.exception("Trend filter: could not read candles")
            return None
        if len(closes) < period:
            return None  # not enough history - don't filter
        ema = _ema(closes, period)
        price = closes[-1]
        if signal.direction == Direction.BUY and price < ema:
            return f"price {price:.1f} below EMA{period} {ema:.1f} (downtrend)"
        if signal.direction == Direction.SELL and price > ema:
            return f"price {price:.1f} above EMA{period} {ema:.1f} (uptrend)"
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
        if self.settings.immediate_entry:
            return None  # always take the market now, never a pending limit
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
