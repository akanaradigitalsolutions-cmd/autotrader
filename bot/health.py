import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from bot.aio import externally_cancelled
from bot.broker.base import ExecutionClient

logger = logging.getLogger(__name__)

# The MetaApi connection can die while the process still looks healthy:
# Telegram keeps replying but every broker call times out (seen in
# production as "/status -> timed out checking broker" plus missed trades).
# Probe the broker on an interval; if it stays dead, exit non-zero so
# systemd restarts the bot and it reconnects from scratch.
BROKER_CHECK_INTERVAL_SECONDS = 120
BROKER_PROBE_TIMEOUT_SECONDS = 30
BROKER_MAX_FAILURES = 3
# Emit a proof-of-life log line roughly every 30 minutes at the 120s interval.
HEARTBEAT_EVERY_CHECKS = 15


def _exit_process() -> None:
    # os._exit instead of sys.exit: this runs inside a background task,
    # where SystemExit would only kill the task, not the process.
    os._exit(1)


async def _notify_and_die(
    notify: Optional[Callable[[str], Awaitable[None]]],
    on_dead: Callable[[], None],
    failures: int,
) -> None:
    logger.error(
        "Broker connection is dead (%d failed health checks in a row) - "
        "restarting the bot to reconnect to MetaApi",
        failures,
    )
    if notify is not None:
        # Telegram is a separate connection, so this usually still works
        # when the broker side is what died.
        try:
            await notify(
                "⚠️ Autotrader: broker connection is dead - restarting to "
                "reconnect to MetaApi. If this keeps repeating, check your "
                "account at https://app.metaapi.cloud"
            )
        except Exception:
            logger.exception("Failed to send broker-dead alert")
    on_dead()


def market_is_closed(now: Optional[datetime] = None) -> bool:
    """True during the weekend window when gold/forex trading is closed.

    Brokers routinely idle or disconnect their MT5 servers over the
    weekend, so failed broker probes then are normal and must not
    restart-loop the bot. The window used here (Fri 23:00 UTC - Sun
    20:00 UTC) sits inside the true market closure in both US DST
    regimes (close Fri 21:00-22:00 UTC, reopen Sun 21:00-22:00 UTC),
    so the watchdog is never suspended while trading is actually open.
    """
    now = now or datetime.now(timezone.utc)
    if now.weekday() == 5:  # Saturday
        return True
    if now.weekday() == 4 and now.hour >= 23:  # late Friday
        return True
    if now.weekday() == 6 and now.hour < 20:  # Sunday before reopen
        return True
    return False


async def broker_watchdog(
    broker: ExecutionClient,
    symbol: str,
    on_dead: Optional[Callable[[], None]] = None,
    notify: Optional[Callable[[str], Awaitable[None]]] = None,
) -> None:
    on_dead = on_dead or _exit_process
    failures = 0
    checks = 0
    while True:
        await asyncio.sleep(BROKER_CHECK_INTERVAL_SECONDS)
        checks += 1
        try:
            await asyncio.wait_for(
                broker.get_current_price(symbol), timeout=BROKER_PROBE_TIMEOUT_SECONDS
            )
        except asyncio.CancelledError as exc:
            if externally_cancelled():
                raise
            # The SDK cancelled its own call (socket reconnect). Seen in
            # production: this silently killed the watchdog task, leaving
            # the bot unguarded for 14+ hours. Count it as a failed probe.
            failures += 1
            logger.warning(
                "Broker health check cancelled by MetaApi client (%d/%d): %r",
                failures, BROKER_MAX_FAILURES, exc,
            )
            if failures >= BROKER_MAX_FAILURES:
                await _notify_and_die(notify, on_dead, failures)
                return
            continue
        except Exception as exc:
            if market_is_closed():
                failures = 0
                logger.info(
                    "Broker probe failed but the market is closed for the "
                    "weekend - not counting it as fatal: %r", exc,
                )
                continue
            failures += 1
            logger.warning(
                "Broker health check failed (%d/%d): %r",
                failures, BROKER_MAX_FAILURES, exc,
            )
            if failures >= BROKER_MAX_FAILURES:
                await _notify_and_die(notify, on_dead, failures)
                return
            continue
        failures = 0
        if checks % HEARTBEAT_EVERY_CHECKS == 0:
            logger.info("Heartbeat: broker connection healthy")
