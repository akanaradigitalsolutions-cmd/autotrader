import asyncio
import time
from datetime import timedelta

from bot.broker.base import ExecutionClient
from bot.config import Settings
from bot.engine import TradingEngine

STATUS_BROKER_TIMEOUT_SECONDS = 10


async def build_status_report(
    settings: Settings, broker: ExecutionClient, engine: TradingEngine, start_time: float
) -> str:
    mode = "DRY RUN (no real orders)" if settings.dry_run else "LIVE - REAL MONEY"
    uptime = timedelta(seconds=int(time.monotonic() - start_time))

    try:
        open_positions = str(
            await asyncio.wait_for(
                broker.count_open_positions(settings.broker_symbol),
                timeout=STATUS_BROKER_TIMEOUT_SECONDS,
            )
        )
    except asyncio.TimeoutError:
        open_positions = "timed out checking broker"
    except Exception as exc:  # noqa: BLE001 - report the error instead of crashing the status reply
        open_positions = f"error: {exc}"

    if engine.last_signal_at is None:
        last_signal = "none yet"
    else:
        last_signal = (
            f"{engine.last_signal_at.strftime('%Y-%m-%d %H:%M:%S UTC')} - "
            f"{engine.last_signal_summary}"
        )

    return (
        f"Mode: {mode}\n"
        f"Account: {settings.metaapi_account_id}\n"
        f"Symbol: {settings.symbol} (broker: {settings.broker_symbol})\n"
        f"Lot size: {settings.lot_size} (max {settings.max_lot_size})\n"
        f"Trades/signal: {settings.trades_per_signal}\n"
        f"Max open positions: {settings.max_open_positions}\n"
        f"Open positions now: {open_positions}\n"
        f"Uptime: {uptime}\n"
        f"Last signal: {last_signal}"
    )
