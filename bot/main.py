import asyncio
import logging
import time

from bot.broker.metaapi_client import MetaApiExecutionClient
from bot.config import load_settings
from bot.engine import TradingEngine
from bot.logging_config import setup_logging
from bot.signal_parser import SignalParser
from bot.status import build_status_report
from bot.telegram_listener import TelegramListener

logger = logging.getLogger(__name__)


async def run() -> None:
    settings = load_settings()
    setup_logging(settings)

    mode = "DRY RUN (no real orders)" if settings.dry_run else "LIVE - REAL MONEY"
    logger.warning("=" * 60)
    logger.warning("MODE: %s", mode)
    logger.warning("Account: %s | Symbol: %s | Lot size: %s | Trades/signal: %s",
                    settings.metaapi_account_id, settings.symbol, settings.lot_size,
                    settings.trades_per_signal)
    logger.warning("=" * 60)

    # Connected even in dry-run: price lookups are read-only and needed to
    # simulate whether a signal would market-fill or wait as a pending order.
    broker = MetaApiExecutionClient(settings.metaapi_token, settings.metaapi_account_id)
    try:
        await broker.connect()
    except Exception as exc:  # noqa: BLE001 - this is the top-level startup boundary
        logger.error("Could not connect to MetaApi: %s", exc)
        logger.error(
            "Check METAAPI_TOKEN/METAAPI_ACCOUNT_ID in .env, and that your "
            "MetaApi.cloud account has billing set up (Billing tab at "
            "https://app.metaapi.cloud) - deploying an account requires it."
        )
        return

    parser = SignalParser(allowed_symbol=settings.symbol)
    engine = TradingEngine(settings, parser, broker)

    listener = TelegramListener(
        api_id=settings.telegram_api_id,
        api_hash=settings.telegram_api_hash,
        session_name=settings.telegram_session_name,
        channel=settings.telegram_channel,
    )

    start_time = time.monotonic()

    async def on_command(text: str) -> str:
        command = text.strip().split()[0].lower()
        if command == "/status":
            return await build_status_report(settings, broker, engine, start_time)
        return "Unknown command. Available: /status"

    try:
        await listener.start(engine.handle_message, on_command)
    finally:
        await broker.disconnect()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
