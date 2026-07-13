import asyncio
import logging
import os
from typing import Callable, Optional

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


async def broker_watchdog(
    broker: ExecutionClient,
    symbol: str,
    on_dead: Optional[Callable[[], None]] = None,
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
        except Exception as exc:
            failures += 1
            logger.warning(
                "Broker health check failed (%d/%d): %r",
                failures, BROKER_MAX_FAILURES, exc,
            )
            if failures >= BROKER_MAX_FAILURES:
                logger.error(
                    "Broker connection is dead (%d failed health checks in a row) - "
                    "restarting the bot to reconnect to MetaApi",
                    failures,
                )
                on_dead()
                return
            continue
        failures = 0
        if checks % HEARTBEAT_EVERY_CHECKS == 0:
            logger.info("Heartbeat: broker connection healthy")
