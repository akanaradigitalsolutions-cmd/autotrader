import asyncio
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

# bot.telegram_listener imports telethon at module level. Telethon needs a
# native build step that isn't available in every environment the tests run
# in, so fall back to a minimal stub when it isn't installed - every test
# below replaces the client with a fake anyway.
try:
    import telethon  # noqa: F401
except ImportError:
    telethon_stub = types.ModuleType("telethon")
    events_stub = types.ModuleType("telethon.events")

    class _NewMessage:
        def __init__(self, chats=None, pattern=None):
            self.chats = chats
            self.pattern = pattern

    events_stub.NewMessage = _NewMessage
    telethon_stub.events = events_stub
    telethon_stub.TelegramClient = object
    sys.modules["telethon"] = telethon_stub
    sys.modules["telethon.events"] = events_stub

import bot.telegram_listener as tl


class FakeTelegramClient:
    def __init__(self, session, api_id, api_hash, **kwargs):
        self.handlers = []
        self.disconnect_calls = 0
        self.get_me_error: Exception | None = None
        self.catch_up_error: Exception | None = None
        self.catch_up_calls = 0

    def on(self, event_builder):
        def decorator(fn):
            self.handlers.append((event_builder, fn))
            return fn

        return decorator

    async def start(self):
        pass

    async def run_until_disconnected(self):
        pass

    async def disconnect(self):
        self.disconnect_calls += 1

    async def get_me(self):
        if self.get_me_error is not None:
            raise self.get_me_error

    async def catch_up(self):
        self.catch_up_calls += 1
        if self.catch_up_error is not None:
            raise self.catch_up_error


class FakeEvent:
    def __init__(self, text, date=None, chat_id=-1001):
        self.raw_text = text
        self.chat_id = chat_id
        self.message = types.SimpleNamespace(date=date or datetime.now(timezone.utc))
        self.replies = []

    async def respond(self, reply):
        self.replies.append(reply)


def make_listener(monkeypatch, channel="-1001234"):
    monkeypatch.setattr(tl, "TelegramClient", FakeTelegramClient)
    return tl.TelegramListener(api_id=1, api_hash="h", session_name="s", channel=channel)


def test_resolve_channel_types():
    assert tl.TelegramListener._resolve_channel("-1001234567890") == -1001234567890
    assert tl.TelegramListener._resolve_channel(" @signals ") == "@signals"


def test_message_age_handles_missing_date():
    assert tl.message_age_seconds(None) == 0.0


@pytest.mark.asyncio
async def test_fresh_message_is_handled_but_stale_is_dropped(monkeypatch):
    listener = make_listener(monkeypatch)
    received = []

    async def on_message(text, source=None):
        received.append(text)

    await listener.start(on_message)
    message_handler = listener._client.handlers[0][1]

    await message_handler(FakeEvent("GOLD BUY 4000"))
    assert received == ["GOLD BUY 4000"]

    stale_date = datetime.now(timezone.utc) - timedelta(
        seconds=tl.MAX_MESSAGE_AGE_SECONDS + 60
    )
    await message_handler(FakeEvent("GOLD SELL 4100", date=stale_date))
    assert received == ["GOLD BUY 4000"]  # stale signal must never be traded


@pytest.mark.asyncio
async def test_command_failure_still_sends_a_reply(monkeypatch):
    listener = make_listener(monkeypatch)

    async def on_message(text, source=None):
        pass

    async def on_command(text):
        raise RuntimeError("boom")

    await listener.start(on_message, on_command)
    command_handler = listener._client.handlers[1][1]

    event = FakeEvent("/status")
    await command_handler(event)

    assert len(event.replies) == 1
    assert "went wrong" in event.replies[0]


@pytest.mark.asyncio
async def test_watchdog_disconnects_after_repeated_failures(monkeypatch):
    monkeypatch.setattr(tl, "WATCHDOG_INTERVAL_SECONDS", 0)
    listener = make_listener(monkeypatch)
    listener._client.get_me_error = ConnectionError("dead link")

    await asyncio.wait_for(listener._watchdog(), timeout=5)

    assert listener._client.disconnect_calls == 1


@pytest.mark.asyncio
async def test_watchdog_treats_internal_cancellation_as_failure(monkeypatch):
    # A CancelledError raised inside the probe (library reconnect) must be
    # counted as a failed check, not silently kill the watchdog task.
    monkeypatch.setattr(tl, "WATCHDOG_INTERVAL_SECONDS", 0)
    listener = make_listener(monkeypatch)
    listener._client.get_me_error = asyncio.CancelledError()

    await asyncio.wait_for(listener._watchdog(), timeout=5)

    assert listener._client.disconnect_calls == 1


@pytest.mark.asyncio
async def test_watchdog_restarts_when_catch_up_keeps_failing(monkeypatch):
    monkeypatch.setattr(tl, "WATCHDOG_INTERVAL_SECONDS", 0)
    listener = make_listener(monkeypatch)
    # get_me stays healthy - this is the silent update-stream death mode,
    # where round-trips work but new messages never arrive.
    listener._client.catch_up_error = ConnectionError("update stream dead")

    await asyncio.wait_for(listener._watchdog(), timeout=5)

    assert listener._client.catch_up_calls == tl.WATCHDOG_MAX_FAILURES
    assert listener._client.disconnect_calls == 1


@pytest.mark.asyncio
async def test_watchdog_tolerates_transient_failures(monkeypatch):
    monkeypatch.setattr(tl, "WATCHDOG_INTERVAL_SECONDS", 0)
    listener = make_listener(monkeypatch)
    client = listener._client
    probes = {"count": 0}
    enough_probes = tl.WATCHDOG_MAX_FAILURES + 3

    async def flaky_get_me():
        probes["count"] += 1
        if probes["count"] <= tl.WATCHDOG_MAX_FAILURES - 1:
            raise ConnectionError("blip")
        if probes["count"] >= enough_probes:
            # Park forever so the watchdog is suspended mid-probe when the
            # test cancels it (cancelling a hot loop is racy on 3.11).
            await asyncio.get_running_loop().create_future()

    client.get_me = flaky_get_me

    task = asyncio.create_task(listener._watchdog())
    for _ in range(1000):
        if probes["count"] >= enough_probes:
            break
        await asyncio.sleep(0)
    assert probes["count"] >= enough_probes
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Two failures then recovery: the counter reset, so no disconnect.
    assert client.disconnect_calls == 0
