import logging

from bot.broker.base import ExecutionClient
from bot.config import Settings
from bot.risk import resolve_lot_size, take_profits_for_trades
from bot.signal_parser import SignalParser

logger = logging.getLogger(__name__)


class TradingEngine:
    """Wires a parsed Telegram message to a broker order, with basic guardrails."""

    def __init__(self, settings: Settings, parser: SignalParser, broker: ExecutionClient):
        self.settings = settings
        self.parser = parser
        self.broker = broker

    async def handle_message(self, text: str) -> None:
        signal = self.parser.parse(text)
        if signal is None:
            logger.debug("Message did not match a tradable signal, ignoring")
            return

        logger.info(
            "Parsed signal: %s %s entry=%s sl=%s tp=%s",
            signal.direction,
            signal.symbol,
            signal.entry,
            signal.stop_loss,
            signal.take_profits,
        )

        volume = resolve_lot_size(self.settings)
        trade_tps = take_profits_for_trades(signal.take_profits, self.settings.trades_per_signal)

        if self.settings.dry_run:
            for i, tp in enumerate(trade_tps, start=1):
                logger.info(
                    "[DRY RUN] Trade %d/%d: %s %s lots=%.2f sl=%s tp=%s",
                    i,
                    len(trade_tps),
                    signal.direction,
                    signal.symbol,
                    volume,
                    signal.stop_loss,
                    tp,
                )
            return

        open_positions = await self.broker.count_open_positions(signal.symbol)
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
                update={"take_profits": [tp] if tp is not None else []}
            )
            result = await self.broker.place_order(trade_signal, volume)
            if result.success:
                logger.info("Trade %d/%d executed: id=%s tp=%s", i, len(trades_to_place), result.order_id, tp)
            else:
                logger.error("Trade %d/%d failed: %s", i, len(trades_to_place), result.message)
