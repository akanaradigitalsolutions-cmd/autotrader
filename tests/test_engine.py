import asyncio
from typing import Optional

import pytest

import bot.engine as engine_mod
from bot.config import Settings
from bot.engine import TradingEngine
from bot.models import Direction, ExecutionResult, OpenPosition, TradeSignal
from bot.position_monitor import PositionMonitor
from bot.signal_parser import SignalParser


class FakeBroker:
    def __init__(self, bid: float, ask: float):
        self.bid = bid
        self.ask = ask
        self.placed_orders: list[tuple[TradeSignal, float, Optional[float]]] = []
        self.positions: list[OpenPosition] = []
        self.balance: float = 1000.0
        self.sl_modifications: list[tuple[str, float]] = []

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def count_open_positions(self, symbol: str) -> int:
        return len(self.positions)

    async def get_positions(self, symbol: str) -> list[OpenPosition]:
        return list(self.positions)

    async def get_account_balance(self) -> float:
        return self.balance

    async def modify_stop_loss(self, position_id: str, stop_loss: float) -> bool:
        self.sl_modifications.append((position_id, stop_loss))
        return True

    async def get_current_price(self, symbol: str) -> tuple[float, float]:
        return self.bid, self.ask

    async def place_order(
        self, signal: TradeSignal, volume: float, entry_price: Optional[float] = None
    ) -> ExecutionResult:
        self.placed_orders.append((signal, volume, entry_price))
        order_id = str(len(self.placed_orders))
        return ExecutionResult(success=True, message="ok", order_id=order_id, signal=signal, dry_run=False)


def make_settings(**overrides) -> Settings:
    defaults = dict(
        telegram_api_id=1,
        telegram_api_hash="hash",
        telegram_channel="@channel",
        metaapi_token="token",
        metaapi_account_id="account",
        dry_run=False,
        trades_per_signal=3,
    )
    defaults.update(overrides)
    return Settings(**defaults)


SELL_SIGNAL = "gold sell 4314-18\nsl 4325.9\ntp 4309\ntp 4301\ntp 4200"
BUY_SIGNAL = "gold buy 4310-15\nsl 4325\ntp 4305\ntp 4295\ntp 4210"


@pytest.mark.asyncio
async def test_sell_signal_market_fills_when_price_already_inside_zone():
    broker = FakeBroker(bid=4316, ask=4316.2)  # inside 4314-4318
    engine = TradingEngine(make_settings(), SignalParser(), broker)

    await engine.handle_message(SELL_SIGNAL)

    assert len(broker.placed_orders) == 3
    assert all(entry_price is None for _, _, entry_price in broker.placed_orders)


@pytest.mark.asyncio
async def test_sell_signal_market_fills_when_price_better_than_zone():
    broker = FakeBroker(bid=4320, ask=4320.2)  # above 4318 - a better sell price
    engine = TradingEngine(make_settings(), SignalParser(), broker)

    await engine.handle_message(SELL_SIGNAL)

    assert all(entry_price is None for _, _, entry_price in broker.placed_orders)


@pytest.mark.asyncio
async def test_sell_signal_waits_as_pending_order_when_price_below_zone():
    broker = FakeBroker(bid=4300, ask=4300.2)  # below 4314 - hasn't reached the zone
    engine = TradingEngine(make_settings(), SignalParser(), broker)

    await engine.handle_message(SELL_SIGNAL)

    assert len(broker.placed_orders) == 3
    assert all(entry_price == 4314 for _, _, entry_price in broker.placed_orders)


@pytest.mark.asyncio
async def test_buy_signal_market_fills_when_price_already_inside_zone():
    broker = FakeBroker(bid=4311.8, ask=4312)  # inside 4310-4315
    engine = TradingEngine(make_settings(), SignalParser(), broker)

    await engine.handle_message(BUY_SIGNAL)

    assert all(entry_price is None for _, _, entry_price in broker.placed_orders)


@pytest.mark.asyncio
async def test_buy_signal_waits_as_pending_order_when_price_above_zone():
    broker = FakeBroker(bid=4319.8, ask=4320)  # above 4315 - hasn't dropped into the zone
    engine = TradingEngine(make_settings(), SignalParser(), broker)

    await engine.handle_message(BUY_SIGNAL)

    assert len(broker.placed_orders) == 3
    assert all(entry_price == 4315 for _, _, entry_price in broker.placed_orders)


@pytest.mark.asyncio
async def test_reposted_signal_is_not_traded_twice():
    broker = FakeBroker(bid=4316, ask=4316.2)
    engine = TradingEngine(make_settings(), SignalParser(), broker)

    await engine.handle_message(SELL_SIGNAL)
    first_count = len(broker.placed_orders)
    await engine.handle_message(SELL_SIGNAL)  # channel reposts the signal

    assert first_count == 3
    assert len(broker.placed_orders) == first_count  # no duplicate trades


@pytest.mark.asyncio
async def test_duplicate_guard_can_be_disabled():
    broker = FakeBroker(bid=4316, ask=4316.2)
    engine = TradingEngine(
        make_settings(duplicate_signal_window_minutes=0), SignalParser(), broker
    )

    await engine.handle_message(SELL_SIGNAL)
    await engine.handle_message(SELL_SIGNAL)

    assert len(broker.placed_orders) == 6  # guard off - both messages trade


@pytest.mark.asyncio
async def test_different_signals_are_not_treated_as_duplicates():
    broker = FakeBroker(bid=4316, ask=4316.2)
    engine = TradingEngine(make_settings(), SignalParser(), broker)

    await engine.handle_message(SELL_SIGNAL)
    await engine.handle_message(
        "gold sell 4330-35\nsl 4345\ntp 4325\ntp 4315\ntp 4200"
    )

    assert len(broker.placed_orders) == 6  # both traded - levels differ


def _open(direction, open_price=4300.0):
    return OpenPosition(
        id="99", symbol="XAUUSD", direction=direction, volume=0.01, open_price=open_price
    )


@pytest.mark.asyncio
async def test_conflict_guard_blocks_opposite_direction():
    broker = FakeBroker(bid=4316, ask=4316.2)
    broker.positions = [_open(Direction.BUY)]  # a BUY is already open
    engine = TradingEngine(make_settings(), SignalParser(), broker)

    await engine.handle_message(SELL_SIGNAL)  # opposite direction

    assert broker.placed_orders == []


@pytest.mark.asyncio
async def test_conflict_guard_allows_same_direction():
    broker = FakeBroker(bid=4316, ask=4316.2)
    broker.positions = [_open(Direction.SELL, open_price=4320.0)]
    # max_open_positions high so the slot cap doesn't mask the guard's decision.
    engine = TradingEngine(make_settings(max_open_positions=6), SignalParser(), broker)

    await engine.handle_message(SELL_SIGNAL)

    assert len(broker.placed_orders) == 3  # same direction - allowed


@pytest.mark.asyncio
async def test_conflict_guard_can_be_disabled():
    broker = FakeBroker(bid=4316, ask=4316.2)
    broker.positions = [_open(Direction.BUY)]
    engine = TradingEngine(
        make_settings(prevent_opposite_positions=False, max_open_positions=6),
        SignalParser(),
        broker,
    )

    await engine.handle_message(SELL_SIGNAL)

    assert len(broker.placed_orders) == 3


@pytest.mark.asyncio
async def test_daily_loss_limit_blocks_further_trades():
    broker = FakeBroker(bid=4316, ask=4316.2)
    engine = TradingEngine(
        make_settings(daily_loss_limit_percent=5.0), SignalParser(), broker
    )

    await engine.handle_message(SELL_SIGNAL)  # sets day-start balance 1000, trades
    assert len(broker.placed_orders) == 3

    broker.balance = 940.0  # -6% realized on the day
    await engine.handle_message("gold sell 4330-35\nsl 4345\ntp 4325\ntp 4315\ntp 4200")

    assert len(broker.placed_orders) == 3  # blocked - no new trades today


@pytest.mark.asyncio
async def test_breakeven_registered_for_multi_trade_market_fill():
    broker = FakeBroker(bid=4316, ask=4316.2)  # price in zone -> market fill
    monitor = PositionMonitor(broker, interval=0)
    engine = TradingEngine(
        make_settings(), SignalParser(), broker, position_monitor=monitor
    )

    await engine.handle_message(SELL_SIGNAL)  # 3 trades

    assert len(monitor._managed) == 1
    managed = monitor._managed[0]
    assert managed.tp1_id == "1"
    assert managed.runner_ids == ["2", "3"]


@pytest.mark.asyncio
async def test_breakeven_not_registered_for_pending_orders():
    broker = FakeBroker(bid=4300, ask=4300.2)  # below zone -> pending orders
    monitor = PositionMonitor(broker, interval=0)
    engine = TradingEngine(
        make_settings(), SignalParser(), broker, position_monitor=monitor
    )

    await engine.handle_message(SELL_SIGNAL)

    assert monitor._managed == []  # pending orders aren't positions yet


@pytest.mark.asyncio
async def test_signal_without_stop_loss_is_refused():
    broker = FakeBroker(bid=4316, ask=4316.2)
    alerts = []

    async def notify(text):
        alerts.append(text)

    engine = TradingEngine(make_settings(), SignalParser(), broker, notify=notify)

    await engine.handle_message("gold sell 4314-18\ntp 4309\ntp 4301")

    assert broker.placed_orders == []  # naked position must never be opened
    assert len(alerts) == 1
    assert "stop loss" in alerts[0]


@pytest.mark.asyncio
async def test_signal_retries_after_metaapi_cancels_a_call(monkeypatch):
    # The MetaApi SDK cancels its own in-flight calls when its socket
    # reconnects; the first attempts fail that way, then it recovers.
    monkeypatch.setattr(engine_mod, "RETRY_DELAY_SECONDS", 0)
    broker = FakeBroker(bid=4316, ask=4316.2)
    original_get_price = broker.get_current_price
    remaining_failures = {"n": engine_mod.EXECUTE_ATTEMPTS - 1}
    reconnects = []

    async def flaky_get_price(symbol):
        if remaining_failures["n"] > 0:
            remaining_failures["n"] -= 1
            raise asyncio.CancelledError()
        return await original_get_price(symbol)

    async def fake_reconnect():
        reconnects.append(1)

    broker.get_current_price = flaky_get_price
    broker.reconnect = fake_reconnect
    engine = TradingEngine(make_settings(), SignalParser(), broker)

    await engine.handle_message(SELL_SIGNAL)

    assert len(broker.placed_orders) == 3  # retried and eventually traded
    # A fresh connection is built before each retry instead of hammering
    # the dead one.
    assert len(reconnects) == engine_mod.EXECUTE_ATTEMPTS - 1


@pytest.mark.asyncio
async def test_failed_signal_sends_alert_and_does_not_crash(monkeypatch):
    monkeypatch.setattr(engine_mod, "RETRY_DELAY_SECONDS", 0)
    broker = FakeBroker(bid=4316, ask=4316.2)

    async def always_cancelled(symbol):
        raise asyncio.CancelledError()

    broker.get_current_price = always_cancelled
    alerts = []

    async def notify(text):
        alerts.append(text)

    engine = TradingEngine(make_settings(), SignalParser(), broker, notify=notify)

    await engine.handle_message(SELL_SIGNAL)  # must not raise

    assert broker.placed_orders == []
    assert len(alerts) == 1
    assert "could NOT execute" in alerts[0]


@pytest.mark.asyncio
async def test_no_retry_after_an_order_was_attempted(monkeypatch):
    # If the failure happens after an order may have reached the broker,
    # retrying could duplicate trades - the engine must stop and alert.
    monkeypatch.setattr(engine_mod, "RETRY_DELAY_SECONDS", 0)
    broker = FakeBroker(bid=4316, ask=4316.2)
    original_place = broker.place_order

    async def place_then_die(signal, volume, entry_price=None):
        result = await original_place(signal, volume, entry_price)
        raise asyncio.CancelledError()  # dies after the first order went out
        return result

    broker.place_order = place_then_die
    alerts = []

    async def notify(text):
        alerts.append(text)

    engine = TradingEngine(make_settings(), SignalParser(), broker, notify=notify)

    await engine.handle_message(SELL_SIGNAL)

    assert len(broker.placed_orders) == 1  # NOT retried into duplicates
    assert len(alerts) == 1
