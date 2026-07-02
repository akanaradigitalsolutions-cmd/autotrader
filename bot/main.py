import asyncio
import logging

from bot.broker.metaapi_client import MetaApiExecutionClient
from bot.config import load_settings
from bot.engine import TradingEngine
from bot.logging_config import setup_logging
from bot.signal_parser import SignalParser
from bot.telegram_listener import TelegramListener

logger = logging.getLogger(__name__)


async def run() -> None:
    settings = load_settings()
    setup_logging(settings)

    if settings.dry_run:
        logger.warning("Running in DRY RUN mode - no real orders will be placed")

    broker = MetaApiExecutionClient(settings.metaapi_token, settings.metaapi_account_id)
    if not settings.dry_run:
        await broker.connect()

    parser = SignalParser(allowed_symbol=settings.symbol)
    engine = TradingEngine(settings, parser, broker)

    listener = TelegramListener(
        api_id=settings.telegram_api_id,
        api_hash=settings.telegram_api_hash,
        session_name=settings.telegram_session_name,
        channel=settings.telegram_channel,
    )

    try:
        await listener.start(engine.handle_message)
    finally:
        if not settings.dry_run:
            await broker.disconnect()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
