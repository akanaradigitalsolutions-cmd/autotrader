import logging

from bot.broker.base import ExecutionClient
from bot.config import Settings
from bot.risk import resolve_lot_size
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

        if self.settings.dry_run:
            logger.info(
                "[DRY RUN] Would place %s %s lots=%.2f sl=%s tp=%s",
                signal.direction,
                signal.symbol,
                volume,
                signal.stop_loss,
                signal.primary_take_profit,
            )
            return

        open_positions = await self.broker.count_open_positions(signal.symbol)
        if open_positions >= self.settings.max_open_positions:
            logger.warning(
                "Skipping signal: already %d open position(s) on %s (limit %d)",
                open_positions,
                signal.symbol,
                self.settings.max_open_positions,
            )
            return

        result = await self.broker.place_order(signal, volume)
        if result.success:
            logger.info("Order executed: id=%s", result.order_id)
        else:
            logger.error("Order failed: %s", result.message)
