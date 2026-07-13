import asyncio

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


@pytest.mark.asyncio
async def test_broker_watchdog_exits_after_repeated_failures(monkeypatch):
    monkeypatch.setattr(health, "BROKER_CHECK_INTERVAL_SECONDS", 0)
    broker = FlakyBroker(failures_before_recovery=None)
    deaths = []

    await asyncio.wait_for(
        health.broker_watchdog(broker, "XAUUSDm", on_dead=lambda: deaths.append(1)),
        timeout=5,
    )

    assert deaths == [1]
    assert broker.probes == health.BROKER_MAX_FAILURES


@pytest.mark.asyncio
async def test_broker_watchdog_tolerates_transient_failures(monkeypatch):
    monkeypatch.setattr(health, "BROKER_CHECK_INTERVAL_SECONDS", 0)
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
