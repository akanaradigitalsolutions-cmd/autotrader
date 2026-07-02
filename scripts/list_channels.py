"""One-off helper: logs into Telegram (first-time login happens here) and
lists every channel/group you're a member of, with its chat id and
username (if it has a public one). Use this to find the exact
TELEGRAM_CHANNEL value for a channel whose display name uses stylized
characters or has no visible @username.

Usage:
    python scripts/list_channels.py

Only needs TELEGRAM_API_ID / TELEGRAM_API_HASH / TELEGRAM_SESSION_NAME
from .env - doesn't require the rest of the app config to be filled in yet.
"""

import asyncio
import os

from dotenv import load_dotenv
from telethon import TelegramClient

load_dotenv()


async def main() -> None:
    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]
    session_name = os.environ.get("TELEGRAM_SESSION_NAME", "autotrader")

    client = TelegramClient(session_name, api_id, api_hash)
    await client.start()  # prompts for phone number + login code on first run

    me = await client.get_me()
    print(f"Logged in as {me.first_name} ({me.phone})\n")
    print(f"{'chat id':<16}{'username':<28}title")
    print("-" * 70)

    async for dialog in client.iter_dialogs():
        if dialog.is_channel or dialog.is_group:
            username = getattr(dialog.entity, "username", None)
            username_display = f"@{username}" if username else "(none - use chat id)"
            print(f"{dialog.id:<16}{username_display:<28}{dialog.name}")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
