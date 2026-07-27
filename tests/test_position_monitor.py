import pytest

from bot.models import Direction, OpenPosition
from bot.position_monitor import PositionMonitor


class FakeBroker:
    def __init__(self):
        self.positions: list[OpenPosition] = []
        self.sl_mods: list[tuple[str, float]] = []
        self.closed_profit: dict[str, float] = {}
        self.price: tuple[float, float] = (0.0, 0.0)

    async def get_positions(self, symbol):
        return [p for p in self.positions if p.symbol == symbol]

    async def get_current_price(self, symbol):
        return self.price

    async def modify_stop_loss(self, position_id, stop_loss):
        self.sl_mods.append((position_id, stop_loss))
        return True

    async def get_closed_profit(self, position_id):
        return self.closed_profit.get(position_id)


class FakeJournal:
    def __init__(self):
        self.closes: list[tuple[str, float]] = []

    def record_close(self, position_id, profit):
        self.closes.append((position_id, profit))


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
async def test_journal_records_close_with_profit_when_position_disappears():
    broker = FakeBroker()
    broker.positions = [pos("1")]
    broker.closed_profit = {"1": -11.0}
    journal = FakeJournal()
    monitor = PositionMonitor(broker, journal=journal)
    monitor.track_for_journal("1", "XAUUSD")

    await monitor.check_once()  # still open - no close recorded
    assert journal.closes == []

    broker.positions = []  # position closed
    await monitor.check_once()

    assert journal.closes == [("1", -11.0)]
    # Not recorded twice on the next cycle.
    await monitor.check_once()
    assert journal.closes == [("1", -11.0)]


@pytest.mark.asyncio
async def test_trailing_does_not_move_stop_before_activation():
    broker = FakeBroker()
    # SELL opened at 4120; price only 30 pips ($3) in profit, activation is 50.
    broker.positions = [
        OpenPosition(id="1", symbol="XAUUSD", direction=Direction.SELL,
                     volume=0.01, open_price=4120.0, stop_loss=4128.0)
    ]
    broker.price = (4117.0, 4117.0)  # ask 4117 -> only $3 favourable
    monitor = PositionMonitor(broker, trailing_activate_pips=50, trailing_distance_pips=15)
    monitor.track_for_journal("1", "XAUUSD")

    await monitor.check_once()

    assert broker.sl_mods == []


@pytest.mark.asyncio
async def test_trailing_locks_profit_once_activated_for_a_sell():
    broker = FakeBroker()
    broker.positions = [
        OpenPosition(id="1", symbol="XAUUSD", direction=Direction.SELL,
                     volume=0.01, open_price=4120.0, stop_loss=4128.0)
    ]
    broker.price = (4113.0, 4113.0)  # $7 favourable (>= $5 activation)
    monitor = PositionMonitor(broker, trailing_activate_pips=50, trailing_distance_pips=15)
    monitor.track_for_journal("1", "XAUUSD")

    await monitor.check_once()

    # Stop trailed to ask + 15 pips = 4113 + 1.5 = 4114.5 (locks ~$5.5 profit)
    assert broker.sl_mods == [("1", 4114.5)]


@pytest.mark.asyncio
async def test_trailing_only_tightens_never_loosens():
    broker = FakeBroker()
    broker.positions = [
        OpenPosition(id="1", symbol="XAUUSD", direction=Direction.SELL,
                     volume=0.01, open_price=4120.0, stop_loss=4128.0)
    ]
    monitor = PositionMonitor(broker, trailing_activate_pips=50, trailing_distance_pips=15)
    monitor.track_for_journal("1", "XAUUSD")

    broker.price = (4110.0, 4110.0)  # deep in profit -> SL to 4111.5
    await monitor.check_once()
    # Price pulls back (less favourable); SL must NOT move back up.
    broker.positions[0].stop_loss = 4111.5
    broker.price = (4116.0, 4116.0)
    await monitor.check_once()

    assert broker.sl_mods == [("1", 4111.5)]  # only the first, tighter move


@pytest.mark.asyncio
async def test_single_trade_is_not_registered():
    broker = FakeBroker()
    monitor = PositionMonitor(broker)
    monitor.register("XAUUSD", Direction.SELL, "1", [])  # no runners

    assert monitor._managed == []
