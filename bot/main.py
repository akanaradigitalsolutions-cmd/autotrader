import asyncio
import logging
import os
import sys
import time

from bot.broker.metaapi_client import MetaApiExecutionClient
from bot.commands import CommandHandler
from bot.config import load_settings
from bot.engine import TradingEngine
from bot.health import broker_watchdog
from bot.logging_config import setup_logging
from bot.notifier import Alerter
from bot.position_monitor import PositionMonitor
from bot.signal_parser import SignalParser
from bot.telegram_listener import TelegramListener

logger = logging.getLogger(__name__)

# MetaApi's connect/synchronize steps have no upper bound of their own; if
# their service is degraded the bot could sit here forever, never reaching
# the Telegram listener. Bound it and let systemd retry instead.
BROKER_CONNECT_TIMEOUT_SECONDS = 180


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

    # Telegram comes up FIRST, so any failure after this point (especially
    # the broker) can be pushed to Saved Messages instead of dying silently
    # in a journal nobody is watching.
    listener = TelegramListener(
        api_id=settings.telegram_api_id,
        api_hash=settings.telegram_api_hash,
        session_name=settings.telegram_session_name,
        channel=settings.telegram_channel,
    )
    await listener.connect()
    alerter = Alerter(listener.send_to_me)

    # Connected even in dry-run: price lookups are read-only and needed to
    # simulate whether a signal would market-fill or wait as a pending order.
    if settings.broker_backend.lower() == "mt5local":
        # Imported lazily: the MetaTrader5 package only installs on Windows.
        from bot.broker.mt5_local_client import Mt5LocalExecutionClient

        broker = Mt5LocalExecutionClient(
            login=settings.mt5_login,
            password=settings.mt5_password,
            server=settings.mt5_server,
            terminal_path=settings.mt5_terminal_path,
        )
    else:
        if not settings.metaapi_token or not settings.metaapi_account_id:
            logger.error(
                "BROKER_BACKEND=metaapi needs METAAPI_TOKEN and METAAPI_ACCOUNT_ID in .env"
            )
            sys.exit(1)
        broker = MetaApiExecutionClient(settings.metaapi_token, settings.metaapi_account_id)
    try:
        await asyncio.wait_for(broker.connect(), timeout=BROKER_CONNECT_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 - this is the top-level startup boundary
        logger.error(
            "Could not connect to the broker (%s) within %ds: %r",
            settings.broker_backend, BROKER_CONNECT_TIMEOUT_SECONDS, exc,
        )
        logger.error(
            "metaapi: check METAAPI_TOKEN/METAAPI_ACCOUNT_ID in .env and the "
            "account at https://app.metaapi.cloud. mt5local: check that the "
            "MT5 terminal is running and logged in on this machine."
        )
        await alerter.alert(
            "broker-connect",
            f"🚨 Autotrader is DOWN: cannot connect to the broker "
            f"({settings.broker_backend}): {exc!r}\n"
            "It keeps retrying automatically every ~10s.\n"
            "metaapi: check https://app.metaapi.cloud | mt5local: check the "
            "MT5 terminal is running and logged in on the VPS.",
        )
        await listener.stop()
        # Exit non-zero: a clean exit here would leave systemd thinking the
        # bot stopped on purpose, and it would never be restarted.
        sys.exit(1)

    parser = SignalParser(allowed_symbol=settings.symbol)
    monitor = PositionMonitor(
        broker, notify=lambda text: alerter.alert("breakeven", text)
    )
    engine = TradingEngine(
        settings, parser, broker,
        notify=lambda text: alerter.alert("trade-failed", text),
        position_monitor=monitor,
        risk_notify=lambda text: alerter.alert("risk-warning", text),
    )

    start_time = time.monotonic()
    command_handler = CommandHandler(settings, broker, engine, start_time)

    # Moves runner positions to breakeven after their TP1 hits.
    monitor_task = asyncio.create_task(monitor.run())

    # Watches the MetaApi side the same way the listener watches Telegram:
    # if broker calls keep failing, the process exits and systemd restarts
    # it, instead of running on with a dead broker and missing trades.
    health_task = asyncio.create_task(
        broker_watchdog(
            broker,
            settings.broker_symbol,
            notify=lambda text: alerter.alert("broker-dead", text),
        )
    )
    # Backstop: a watchdog that dies of an unexpected exception leaves the
    # bot unguarded (seen in production: it ran blind for 14+ hours).
    # Restart rather than run without it.
    def _die_if_health_watchdog_crashed(task: "asyncio.Task") -> None:
        if task.cancelled() or task.exception() is None:
            return
        logger.critical(
            "Broker watchdog died unexpectedly - forcing a restart",
            exc_info=task.exception(),
        )
        os._exit(1)

    health_task.add_done_callback(_die_if_health_watchdog_crashed)

    await alerter.alert(
        "online",
        f"✅ Autotrader online - {mode}. Listening on {settings.telegram_channel}.",
    )

    try:
        await listener.start(engine.handle_message, command_handler.handle)
        # start() returning means Telegram disconnected (or the watchdog gave
        # up on a dead connection). The bot is no longer trading either way,
        # so exit non-zero to make systemd bring it back up.
        logger.error("Telegram listener stopped - exiting so systemd restarts the bot")
        sys.exit(1)
    finally:
        health_task.cancel()
        monitor_task.cancel()
        try:
            await asyncio.wait_for(broker.disconnect(), timeout=10)
        except Exception:
            logger.warning("Broker disconnect failed or timed out during shutdown")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
