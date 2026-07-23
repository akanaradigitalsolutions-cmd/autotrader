import pytest

from bot.models import Direction, OpenPosition
from bot.position_monitor import PositionMonitor


class FakeBroker:
    def __init__(self):
        self.positions: list[OpenPosition] = []
        self.sl_mods: list[tuple[str, float]] = []

    async def get_positions(self, symbol):
        return [p for p in self.positions if p.symbol == symbol]

    async def modify_stop_loss(self, position_id, stop_loss):
        self.sl_mods.append((position_id, stop_loss))
        return True


def pos(pid, direction=Direction.SELL, open_price=4120.0, symbol="XAUUSD"):
    return OpenPosition(
        id=pid, symbol=symbol, direction=direction, volume=0.01, open_price=open_price
    )


@pytest.mark.asyncio
async def test_no_breakeven_while_tp1_still_open():
    broker = FakeBroker()
    broker.positions = [pos("1"), pos("2")]  # tp1 and runner both open
    monitor = PositionMonitor(broker)
    monitor.register("XAUUSD", Direction.SELL, "1", ["2"])

    await monitor.check_once()

    assert broker.sl_mods == []


@pytest.mark.asyncio
async def test_runner_moved_to_breakeven_when_tp1_closes():
    broker = FakeBroker()
    broker.positions = [pos("2", open_price=4118.0)]  # tp1 "1" gone, runner "2" open
    alerts = []

    async def record(text):
        alerts.append(text)

    monitor = PositionMonitor(broker, notify=record)
    monitor.register("XAUUSD", Direction.SELL, "1", ["2"])

    await monitor.check_once()

    assert broker.sl_mods == [("2", 4118.0)]  # SL moved to the runner's entry
    assert len(alerts) == 1


@pytest.mark.asyncio
async def test_breakeven_applied_only_once():
    broker = FakeBroker()
    broker.positions = [pos("2", open_price=4118.0)]
    monitor = PositionMonitor(broker)
    monitor.register("XAUUSD", Direction.SELL, "1", ["2"])

    await monitor.check_once()
    await monitor.check_once()

    assert broker.sl_mods == [("2", 4118.0)]  # not moved a second time


@pytest.mark.asyncio
async def test_group_dropped_when_all_positions_closed():
    broker = FakeBroker()
    broker.positions = []  # everything closed
    monitor = PositionMonitor(broker)
    monitor.register("XAUUSD", Direction.SELL, "1", ["2"])

    await monitor.check_once()

    assert monitor._managed == []


@pytest.mark.asyncio
async def test_single_trade_is_not_registered():
    broker = FakeBroker()
    monitor = PositionMonitor(broker)
    monitor.register("XAUUSD", Direction.SELL, "1", [])  # no runners

    assert monitor._managed == []
