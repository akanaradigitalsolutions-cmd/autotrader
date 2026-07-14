import asyncio
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from bot.aio import externally_cancelled
from bot.broker.base import ExecutionClient
from bot.config import Settings
from bot.models import Direction, TradeSignal
from bot.risk import resolve_lot_size, take_profits_for_trades
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


class TradingEngine:
    """Wires a parsed Telegram message to a broker order, with basic guardrails."""

    def __init__(
        self,
        settings: Settings,
        parser: SignalParser,
        broker: ExecutionClient,
        notify: Optional[Callable[[str], Awaitable[None]]] = None,
    ):
        self.settings = settings
        self.parser = parser
        self.broker = broker
        self.notify = notify
        self.last_signal_summary: Optional[str] = None
        self.last_signal_at: Optional[datetime] = None

    async def handle_message(self, text: str) -> None:
        signal = self.parser.parse(text)
        if signal is None:
            logger.debug("Message did not match a tradable signal, ignoring")
            return

        logger.info(
            "Parsed signal: %s %s entry=%s-%s sl=%s tp=%s",
            signal.direction,
            signal.symbol,
            signal.entry_low,
            signal.entry_high,
            signal.stop_loss,
            signal.take_profits,
        )
        self.last_signal_at = datetime.now(timezone.utc)
        self.last_signal_summary = (
            f"{signal.direction} {signal.symbol} entry={signal.entry_low}-{signal.entry_high} "
            f"sl={signal.stop_loss} tp={signal.take_profits}"
        )

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
                await asyncio.sleep(RETRY_DELAY_SECONDS)

        await self._report_failed_signal()

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
