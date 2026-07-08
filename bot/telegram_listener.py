import logging
from typing import Awaitable, Callable, Optional

from telethon import TelegramClient, events

logger = logging.getLogger(__name__)

MessageHandler = Callable[[str], Awaitable[None]]
CommandHandler = Callable[[str], Awaitable[str]]


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
        self._channel = self._resolve_channel(channel)

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
        logger.info("Telegram client started, listening on %s", self._channel)

        @self._client.on(events.NewMessage(chats=self._channel))
        async def _handler(event) -> None:
            text = event.raw_text or ""
            logger.debug("Received message: %s", text)
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
                logger.info("Received command: %s", text)
                reply = await on_command(text)
                await event.respond(reply)

        await self._client.run_until_disconnected()

    async def stop(self) -> None:
        await self._client.disconnect()
