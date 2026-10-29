import pytest

import bot.notifier as notifier
from bot.notifier import Alerter


class SendRecorder:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    async def __call__(self, text):
        if self.fail:
            raise ConnectionError("telegram down")
        self.sent.append(text)


@pytest.mark.asyncio
async def test_alert_sends_then_rate_limits(tmp_path):
    send = SendRecorder()
    alerter = Alerter(send, state_file=str(tmp_path / "state.json"))

    await alerter.alert("broker-dead", "first")
    await alerter.alert("broker-dead", "second (should be suppressed)")

    assert send.sent == ["first"]


@pytest.mark.asyncio
async def test_alert_rate_limit_is_per_key(tmp_path):
    send = SendRecorder()
    alerter = Alerter(send, state_file=str(tmp_path / "state.json"))

    await alerter.alert("broker-dead", "broker msg")
    await alerter.alert("online", "online msg")

    assert send.sent == ["broker msg", "online msg"]


@pytest.mark.asyncio
async def test_alert_sends_again_after_interval(tmp_path, monkeypatch):
    send = SendRecorder()
    alerter = Alerter(send, state_file=str(tmp_path / "state.json"))

    now = {"t": 1_000_000.0}
    monkeypatch.setattr(notifier.time, "time", lambda: now["t"])

    await alerter.alert("k", "first")
    now["t"] += notifier.MIN_INTERVAL_SECONDS + 1
    await alerter.alert("k", "second")

    assert send.sent == ["first", "second"]


@pytest.mark.asyncio
async def test_failed_send_does_not_consume_the_rate_limit(tmp_path):
    send = SendRecorder(fail=True)
    alerter = Alerter(send, state_file=str(tmp_path / "state.json"))

    await alerter.alert("k", "will fail")  # must not raise

    send.fail = False
    await alerter.alert("k", "retry")
    assert send.sent == ["retry"]


@pytest.mark.asyncio
async def test_rate_limit_state_survives_restart(tmp_path):
    state_file = str(tmp_path / "state.json")
    send = SendRecorder()

    first_process = Alerter(send, state_file=state_file)
    await first_process.alert("crash-loop", "first")

    second_process = Alerter(send, state_file=state_file)  # fresh instance
    await second_process.alert("crash-loop", "suppressed across restart")

    assert send.sent == ["first"]
