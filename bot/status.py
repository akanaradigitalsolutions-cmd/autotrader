import asyncio
import time
from datetime import timedelta

from bot.aio import externally_cancelled
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
    except asyncio.CancelledError:
        if externally_cancelled():
            raise
        # MetaApi SDK cancelled its own call (socket reconnect); without
        # this, /status dies silently and never replies.
        open_positions = "broker call cancelled (MetaApi reconnecting)"
    except Exception as exc:  # noqa: BLE001 - report the error instead of crashing the status reply
        open_positions = f"error: {exc}"

    daily_loss = (
        f"{settings.daily_loss_limit_percent:.0f}%"
        if settings.daily_loss_limit_percent > 0
        else "off"
    )
    trailing = (
        f"{settings.trailing_activate_pips:.0f}/{settings.trailing_distance_pips:.0f}p"
        if settings.trailing_activate_pips > 0
        else "off"
    )
    guards = (
        f"conflict={'on' if settings.prevent_opposite_positions else 'off'} "
        f"breakeven={'on' if settings.breakeven_after_tp1 else 'off'} "
        f"daily_loss={daily_loss} trail={trailing}"
    )
    rr_filter = (
        f"skip R:R<{settings.skip_reward_risk_below:.1f}"
        if settings.skip_reward_risk_below > 0
        else "R:R filter off"
    )
    trend = f"trend={settings.trend_filter}"
    if settings.trend_filter.lower() != "off":
        trend += f"(EMA{settings.trend_ema_period} {settings.trend_timeframe})"
    pending_expiry = (
        f"pending_expiry={settings.pending_order_expiry_minutes}m"
        if settings.pending_order_expiry_minutes > 0
        else "pending_expiry=off"
    )
    entry_mode = "entry=market" if settings.immediate_entry else "entry=zone"
    fallback_sl = (
        f"fallback_sl={settings.fallback_stop_loss_pips:.0f}p"
        if settings.fallback_stop_loss_pips > 0
        else "fallback_sl=off"
    )

    if engine.last_signal_at is None:
        last_signal = "none yet"
    else:
        last_signal = (
            f"{engine.last_signal_at.strftime('%Y-%m-%d %H:%M:%S UTC')} - "
            f"{engine.last_signal_summary}"
        )

    if settings.broker_backend.lower() == "mt5local":
        account = f"local MT5 ({settings.mt5_login or 'terminal login'})"
    else:
        account = settings.metaapi_account_id

    return (
        f"Mode: {mode}\n"
        f"Account: {account}\n"
        f"Symbol: {settings.symbol} (broker: {settings.broker_symbol})\n"
        f"Lot size: {settings.lot_size} (max {settings.max_lot_size})\n"
        f"Trades/signal: {settings.trades_per_signal}\n"
        f"Max open positions: {settings.max_open_positions}\n"
        f"Guards: {guards}\n"
        f"Filter: {rr_filter} {trend} {pending_expiry}\n"
        f"Entry: {entry_mode} {fallback_sl}\n"
        f"Open positions now: {open_positions}\n"
        f"Uptime: {uptime}\n"
        f"Last signal: {last_signal}"
    )
