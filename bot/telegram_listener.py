import logging
from typing import Awaitable, Callable

from telethon import TelegramClient, events

logger = logging.getLogger(__name__)

MessageHandler = Callable[[str], Awaitable[None]]


class TelegramListener:
    """Reads messages from a single Telegram channel via a user session.

    A user session (not the Bot API) is required because the target
    channel is one you subscribe to, not one you administer - bots can
    only read channels/groups they've been added to as admin.

    First run will prompt for your phone number + login code interactively
    to create the local session file; subsequent runs reuse it.
    """

    def __init__(self, api_id: int, api_hash: str, session_name: str, channel: str):
        self._client = TelegramClient(session_name, api_id, api_hash)
        self._channel = channel

    async def start(self, on_message: MessageHandler) -> None:
        await self._client.start()
        logger.info("Telegram client started, listening on %s", self._channel)

        @self._client.on(events.NewMessage(chats=self._channel))
        async def _handler(event) -> None:
            text = event.raw_text or ""
            logger.debug("Received message: %s", text)
            await on_message(text)

        await self._client.run_until_disconnected()

    async def stop(self) -> None:
        await self._client.disconnect()
