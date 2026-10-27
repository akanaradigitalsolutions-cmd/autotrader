import pytest

from bot.models import Direction
from bot.simulator import SignalSimulator


class FakeBroker:
    def __init__(self, price):
        self.price = price

    async def get_current_price(self, symbol):
        return self.price


class FakeJournal:
    def __init__(self):
        self.opens = []
        self.closes = []

    def record_open(self, position_id, source, direction, symbol, entry, sl, tp1, rr, volume):
        self.opens.append((position_id, source, rr))

    def record_close(self, position_id, profit):
        self.closes.append((position_id, profit))


@pytest.mark.asyncio
async def test_sim_records_a_win_when_price_reaches_tp1():
    broker = FakeBroker((4444.0, 4444.0))  # a SELL exits at the bid
    journal = FakeJournal()
    sim = SignalSimulator(broker, journal, pip_size=0.1)
    # SELL entry 4450, TP1 4445, SL 4462. Price 4444 <= TP1 -> win.
    sim.add(-100, "XAUUSD", Direction.SELL, 4450.0, 4462.0, 4445.0, "below R:R filter")

    await sim.check_once()

    assert journal.closes == [("sim1", 5.0)]  # +5 (entry 4450 - tp 4445)


@pytest.mark.asyncio
async def test_sim_records_a_loss_when_price_reaches_stop():
    broker = FakeBroker((4462.0, 4462.0))  # price hit the sell stop
    journal = FakeJournal()
    sim = SignalSimulator(broker, journal, pip_size=0.1)
    sim.add(-100, "XAUUSD", Direction.SELL, 4450.0, 4462.0, 4445.0, "below R:R filter")

    await sim.check_once()

    assert journal.closes == [("sim1", -12.0)]  # -12 (entry 4450 - sl 4462)


@pytest.mark.asyncio
async def test_sim_stays_open_while_price_is_between_tp_and_stop():
    broker = FakeBroker((4452.0, 4452.0))  # between TP1 (4445) and SL (4462)
    journal = FakeJournal()
    sim = SignalSimulator(broker, journal, pip_size=0.1)
    sim.add(-100, "XAUUSD", Direction.SELL, 4450.0, 4462.0, 4445.0, "test")

    await sim.check_once()

    assert journal.closes == []  # unresolved


@pytest.mark.asyncio
async def test_sim_buy_win():
    broker = FakeBroker((4610.0, 4610.0))  # a BUY exits at the ask
    journal = FakeJournal()
    sim = SignalSimulator(broker, journal, pip_size=0.1)
    # BUY entry 4600, TP1 4607, SL 4588. Price 4610 >= TP1 -> win +7.
    sim.add(-200, "XAUUSD", Direction.BUY, 4600.0, 4588.0, 4607.0, "no stop loss")

    await sim.check_once()

    assert journal.closes == [("sim1", 7.0)]


class SettableBroker:
    def __init__(self, price):
        self.price = price

    async def get_current_price(self, symbol):
        return self.price


@pytest.mark.asyncio
async def test_sim_trailing_locks_profit_on_reversal():
    # SELL entry 4450, SL 4462, TP1 4445. Trailing: arm after 20 pips ($2),
    # trail by 10 pips ($1).
    broker = SettableBroker((4448.0, 4448.0))
    journal = FakeJournal()
    sim = SignalSimulator(
        broker, journal, pip_size=0.1,
        trailing_activate_pips=20, trailing_distance_pips=10,
    )
    sim.add(-100, "XAUUSD", Direction.SELL, 4450.0, 4462.0, 4445.0, "test")

    await sim.check_once()                      # 4448: arms trail at 4449, open
    assert journal.closes == []
    broker.price = (4446.0, 4446.0)
    await sim.check_once()                      # trail advances to 4447, open
    assert journal.closes == []
    broker.price = (4447.5, 4447.5)             # reverses up into the trail
    await sim.check_once()

    # Exits at the trailed stop 4447 -> locked +3 (entry 4450 - 4447).
    assert journal.closes == [("sim1", 3.0)]


@pytest.mark.asyncio
async def test_sim_trailing_disabled_does_not_exit_early():
    # Same reversal, but no trailing configured -> must stay open (never hit
    # TP1 4445 or SL 4462).
    broker = SettableBroker((4446.0, 4446.0))
    journal = FakeJournal()
    sim = SignalSimulator(broker, journal, pip_size=0.1)  # trailing off
    sim.add(-100, "XAUUSD", Direction.SELL, 4450.0, 4462.0, 4445.0, "test")

    await sim.check_once()
    broker.price = (4448.0, 4448.0)             # reversal that a trail would catch
    await sim.check_once()

    assert journal.closes == []                 # still open, no trailing


@pytest.mark.asyncio
async def test_sim_trailing_not_armed_until_activation_distance():
    # Price never moves the 20-pip activation distance, then hits the stop.
    broker = SettableBroker((4451.0, 4451.0))   # only 10 pips of profit
    journal = FakeJournal()
    sim = SignalSimulator(
        broker, journal, pip_size=0.1,
        trailing_activate_pips=20, trailing_distance_pips=10,
    )
    sim.add(-100, "XAUUSD", Direction.BUY, 4450.0, 4438.0, 4465.0, "test")

    await sim.check_once()                      # +10 pips, not armed, open
    assert journal.closes == []
    broker.price = (4438.0, 4438.0)             # drops to the original stop
    await sim.check_once()

    assert journal.closes == [("sim1", -12.0)]  # full stop loss, trail never armed
