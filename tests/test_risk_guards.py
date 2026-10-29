from datetime import datetime, timezone

import pytest

from bot.risk_guards import DailyLossGuard


class FakeBroker:
    def __init__(self, balance):
        self.balance = balance

    async def get_account_balance(self):
        return self.balance


@pytest.mark.asyncio
async def test_first_call_sets_baseline_and_does_not_block():
    broker = FakeBroker(1000.0)
    clock = {"t": datetime(2026, 7, 22, 8, 0, tzinfo=timezone.utc)}
    guard = DailyLossGuard(broker, 5.0, now=lambda: clock["t"])

    assert await guard.should_block() is False


@pytest.mark.asyncio
async def test_blocks_only_once_loss_crosses_threshold():
    broker = FakeBroker(1000.0)
    clock = {"t": datetime(2026, 7, 22, 8, 0, tzinfo=timezone.utc)}
    guard = DailyLossGuard(broker, 5.0, now=lambda: clock["t"])

    await guard.should_block()  # baseline 1000
    broker.balance = 960.0  # -4%
    assert await guard.should_block() is False
    broker.balance = 940.0  # -6%
    assert await guard.should_block() is True


@pytest.mark.asyncio
async def test_resets_on_a_new_utc_day():
    broker = FakeBroker(1000.0)
    clock = {"t": datetime(2026, 7, 22, 8, 0, tzinfo=timezone.utc)}
    guard = DailyLossGuard(broker, 5.0, now=lambda: clock["t"])

    await guard.should_block()
    broker.balance = 900.0
    assert await guard.should_block() is True

    clock["t"] = datetime(2026, 7, 23, 8, 0, tzinfo=timezone.utc)  # next day
    assert await guard.should_block() is False  # new baseline = 900


@pytest.mark.asyncio
async def test_zero_limit_disables_the_guard():
    broker = FakeBroker(1000.0)
    guard = DailyLossGuard(broker, 0.0)

    broker.balance = 1.0  # -99.9%
    assert await guard.should_block() is False


@pytest.mark.asyncio
async def test_unavailable_balance_does_not_block():
    broker = FakeBroker(None)
    guard = DailyLossGuard(broker, 5.0)

    assert await guard.should_block() is False
