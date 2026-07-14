import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

SEND_TIMEOUT_SECONDS = 15
# Alerts repeat at most this often per key, so a crash-restart loop turns
# into one Telegram message every half hour instead of hundreds.
MIN_INTERVAL_SECONDS = 1800


class Alerter:
    """Sends rate-limited alert messages (e.g. to Telegram Saved Messages).

    State survives restarts via a small JSON file, because the common case
    for an alert - the bot dying and systemd restarting it - is exactly the
    case where in-memory state is lost.
    """

    def __init__(
        self,
        send: Callable[[str], Awaitable[None]],
        state_file: str = ".alert_state.json",
        min_interval: int = MIN_INTERVAL_SECONDS,
    ):
        self._send = send
        self._state_file = Path(state_file)
        self._min_interval = min_interval

    async def alert(self, key: str, text: str) -> None:
        if not self._due(key):
            logger.info("Alert %r suppressed (rate limited): %s", key, text)
            return
        try:
            await asyncio.wait_for(self._send(text), timeout=SEND_TIMEOUT_SECONDS)
        except Exception:
            logger.exception("Failed to send alert %r", key)
            return
        self._mark_sent(key)

    def _load(self) -> dict:
        try:
            return json.loads(self._state_file.read_text())
        except Exception:
            return {}

    def _due(self, key: str) -> bool:
        last = self._load().get(key, 0)
        return time.time() - last >= self._min_interval

    def _mark_sent(self, key: str) -> None:
        state = self._load()
        state[key] = time.time()
        try:
            self._state_file.write_text(json.dumps(state))
        except Exception:
            logger.exception("Could not persist alert state")
