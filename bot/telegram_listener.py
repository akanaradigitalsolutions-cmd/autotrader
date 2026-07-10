import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from telethon import TelegramClient, events

logger = logging.getLogger(__name__)

MessageHandler = Callable[[str], Awaitable[None]]
CommandHandler = Callable[[str], Awaitable[str]]

# The watchdog makes a real Telegram API call on a fixed interval. Telethon
# can end up "connected" on a dead TCP link and silently stop receiving
# updates - no exception is ever raised, so the only way to notice is to
# probe. After enough consecutive probe failures the client is shut down,
# which ends run_until_disconnected() and lets the process exit non-zero so
# systemd restarts it with a fresh connection.
WATCHDOG_INTERVAL_SECONDS = 60
WATCHDOG_PROBE_TIMEOUT_SECONDS = 20
WATCHDOG_MAX_FAILURES = 3
# Emit a proof-of-life log line roughly every 30 minutes at the 60s interval.
HEARTBEAT_EVERY_CHECKS = 30

# Signals delivered late (reconnect backlog, stalled connection) are
# dangerous to trade: the market has moved since the price levels were
# written. Anything older than this is logged and dropped, not handled.
MAX_MESSAGE_AGE_SECONDS = 600


def message_age_seconds(message_date: Optional[datetime]) -> float:
    if message_date is None:
        return 0.0
    return (datetime.now(timezone.utc) - message_date).total_seconds()


class TelegramListener:
    """Reads messages from one or more Telegram channels via a user session.

    A user session (not the Bot API) is required because the target
    channels are ones you subscribe to, not ones you administer - bots can
    only read channels/groups they've been added to as admin.

    First run will prompt for your phone number + login code interactively
    to create the local session file; subsequent runs reuse it.
    """

    def __init__(self, api_id: int, api_hash: str, session_name: str, channel: str):
        self._client = TelegramClient(session_name, api_id, api_hash)
        # Comma-separated list of channels/chat ids, so multiple signal
        # sources can be monitored at once (e.g. "-1001422815541,@othersignals").
        self._channels = [
            self._resolve_channel(part) for part in channel.split(",") if part.strip()
        ]

    @staticmethod
    def _resolve_channel(channel: str) -> str | int:
        # A numeric chat id (e.g. "-1001234567890") must be passed as an int,
        # not a string, or Telethon will treat it as a username lookup.
        stripped = channel.strip()
        return int(stripped) if stripped.lstrip("-").isdigit() else stripped

    async def start(
        self, on_message: MessageHandler, on_command: Optional[CommandHandler] = None
    ) -> None:
        await self._client.start()
        logger.info("Telegram client started, listening on %s", self._channels)

        @self._client.on(events.NewMessage(chats=self._channels))
        async def _handler(event) -> None:
            text = event.raw_text or ""
            age = message_age_seconds(event.message.date)
            if age > MAX_MESSAGE_AGE_SECONDS:
                logger.warning(
                    "Dropping message from %s delivered %.0fs late (limit %ds): %.60s",
                    event.chat_id, age, MAX_MESSAGE_AGE_SECONDS, text,
                )
                return
            logger.debug("Received message from %s: %s", event.chat_id, text)
            await on_message(text)

        if on_command is not None:
            # Commands are only accepted in Saved Messages ("me") - the chat
            # with yourself - so no one else can control the bot remotely.
            # Matches slash commands (with any arguments) as well as the bare
            # yes/no replies used to confirm a pending /set change.
            @self._client.on(
                events.NewMessage(chats="me", pattern=r"(?i)^(/\w+(?:\s.*)?|yes|no|y|n)$")
            )
            async def _command_handler(event) -> None:
                text = event.raw_text or ""
                if message_age_seconds(event.message.date) > MAX_MESSAGE_AGE_SECONDS:
                    return
                logger.info("Received command: %s", text)
                try:
                    reply = await on_command(text)
                except Exception:
                    logger.exception("Command %r failed", text)
                    reply = "Something went wrong handling that command - check the bot logs."
                await event.respond(reply)

        watchdog = asyncio.create_task(self._watchdog())
        try:
            await self._client.run_until_disconnected()
        finally:
            watchdog.cancel()
            # Bounded wait: wait_for() can swallow a cancellation if the
            # probe completes at the same instant, and shutdown must never
            # be able to hang on that race.
            await asyncio.wait({watchdog}, timeout=5)

    async def _watchdog(self) -> None:
        failures = 0
        checks = 0
        while True:
            await asyncio.sleep(WATCHDOG_INTERVAL_SECONDS)
            checks += 1
            try:
                await asyncio.wait_for(
                    self._client.get_me(), timeout=WATCHDOG_PROBE_TIMEOUT_SECONDS
                )
            except Exception as exc:
                failures += 1
                logger.warning(
                    "Telegram health check failed (%d/%d): %r",
                    failures, WATCHDOG_MAX_FAILURES, exc,
                )
                if failures >= WATCHDOG_MAX_FAILURES:
                    await self._shutdown_dead_connection()
                    return
                continue
            failures = 0
            if checks % HEARTBEAT_EVERY_CHECKS == 0:
                logger.info("Heartbeat: Telegram connection healthy")

    async def _shutdown_dead_connection(self) -> None:
        logger.error(
            "Telegram connection is dead (%d failed health checks in a row) - "
            "shutting down so systemd restarts the bot with a fresh connection",
            WATCHDOG_MAX_FAILURES,
        )
        try:
            await asyncio.wait_for(self._client.disconnect(), timeout=15)
        except Exception:
            # disconnect() itself can hang on a dead connection; at that point
            # the only reliable recovery is killing the process so systemd
            # brings up a clean one.
            logger.exception("Disconnect hung as well - force-exiting")
            os._exit(1)

    async def stop(self) -> None:
        await self._client.disconnect()
