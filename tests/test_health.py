import asyncio
from datetime import datetime, timezone

import pytest

import bot.health as health


class FlakyBroker:
    def __init__(self, failures_before_recovery=None):
        # None = fail forever; N = fail the first N probes, then succeed.
        self.failures_before_recovery = failures_before_recovery
        self.probes = 0
        self.park_after = None

    async def get_current_price(self, symbol):
        self.probes += 1
        if self.park_after is not None and self.probes >= self.park_after:
            # Suspend forever so the test can cancel the watchdog cleanly.
            await asyncio.get_running_loop().create_future()
        if (
            self.failures_before_recovery is None
            or self.probes <= self.failures_before_recovery
        ):
            raise ConnectionError("broker down")
        return 1.0, 1.0


def test_market_is_closed_on_weekend():
    saturday = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)
    friday_night = datetime(2026, 7, 10, 23, 30, tzinfo=timezone.utc)
    sunday_afternoon = datetime(2026, 7, 12, 15, 0, tzinfo=timezone.utc)
    assert health.market_is_closed(saturday)
    assert health.market_is_closed(friday_night)
    assert health.market_is_closed(sunday_afternoon)


def test_market_is_open_during_trading_hours():
    monday_morning = datetime(2026, 7, 13, 8, 50, tzinfo=timezone.utc)
    friday_afternoon = datetime(2026, 7, 10, 15, 0, tzinfo=timezone.utc)
    sunday_late = datetime(2026, 7, 12, 22, 30, tzinfo=timezone.utc)
    assert not health.market_is_closed(monday_morning)
    assert not health.market_is_closed(friday_afternoon)
    assert not health.market_is_closed(sunday_late)


@pytest.mark.asyncio
async def test_broker_watchdog_exits_after_repeated_failures(monkeypatch):
    monkeypatch.setattr(health, "BROKER_CHECK_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(health, "market_is_closed", lambda now=None: False)
    broker = FlakyBroker(failures_before_recovery=None)
    deaths = []
    alerts = []

    async def notify(text):
        alerts.append(text)

    await asyncio.wait_for(
        health.broker_watchdog(
            broker, "XAUUSDm", on_dead=lambda: deaths.append(1), notify=notify
        ),
        timeout=5,
    )

    assert deaths == [1]
    assert broker.probes == health.BROKER_MAX_FAILURES
    assert len(alerts) == 1  # user is told on Telegram before the restart


@pytest.mark.asyncio
async def test_broker_watchdog_survives_metaapi_cancellations(monkeypatch):
    # The SDK cancelling its own call must count as a failed probe, not
    # silently kill the watchdog (this left the bot unguarded for 14h).
    monkeypatch.setattr(health, "BROKER_CHECK_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(health, "market_is_closed", lambda now=None: False)

    class CancellingBroker:
        def __init__(self):
            self.probes = 0

        async def get_current_price(self, symbol):
            self.probes += 1
            raise asyncio.CancelledError()

    broker = CancellingBroker()
    deaths = []

    await asyncio.wait_for(
        health.broker_watchdog(broker, "XAUUSDm", on_dead=lambda: deaths.append(1)),
        timeout=5,
    )

    assert deaths == [1]
    assert broker.probes == health.BROKER_MAX_FAILURES


@pytest.mark.asyncio
async def test_broker_watchdog_ignores_failures_while_market_closed(monkeypatch):
    monkeypatch.setattr(health, "BROKER_CHECK_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(health, "market_is_closed", lambda now=None: True)
    broker = FlakyBroker(failures_before_recovery=None)
    enough_probes = health.BROKER_MAX_FAILURES * 3
    broker.park_after = enough_probes
    deaths = []

    task = asyncio.create_task(
        health.broker_watchdog(broker, "XAUUSDm", on_dead=lambda: deaths.append(1))
    )
    for _ in range(1000):
        if broker.probes >= enough_probes:
            break
        await asyncio.sleep(0)
    assert broker.probes >= enough_probes
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Weekend failures are expected - the bot must NOT restart-loop.
    assert deaths == []


@pytest.mark.asyncio
async def test_broker_watchdog_tolerates_transient_failures(monkeypatch):
    monkeypatch.setattr(health, "BROKER_CHECK_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(health, "market_is_closed", lambda now=None: False)
    broker = FlakyBroker(failures_before_recovery=health.BROKER_MAX_FAILURES - 1)
    enough_probes = health.BROKER_MAX_FAILURES + 3
    broker.park_after = enough_probes
    deaths = []

    task = asyncio.create_task(
        health.broker_watchdog(broker, "XAUUSDm", on_dead=lambda: deaths.append(1))
    )
    for _ in range(1000):
        if broker.probes >= enough_probes:
            break
        await asyncio.sleep(0)
    assert broker.probes >= enough_probes
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Failures then recovery: the counter reset, so the bot must NOT restart.
    assert deaths == []
