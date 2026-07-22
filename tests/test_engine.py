import asyncio
from typing import Optional

import pytest

import bot.engine as engine_mod
from bot.config import Settings
from bot.engine import TradingEngine
from bot.models import ExecutionResult, TradeSignal
from bot.signal_parser import SignalParser


class FakeBroker:
    def __init__(self, bid: float, ask: float):
        self.bid = bid
        self.ask = ask
        self.placed_orders: list[tuple[TradeSignal, float, Optional[float]]] = []

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def count_open_positions(self, symbol: str) -> int:
        return 0

    async def get_current_price(self, symbol: str) -> tuple[float, float]:
        return self.bid, self.ask

    async def place_order(
        self, signal: TradeSignal, volume: float, entry_price: Optional[float] = None
    ) -> ExecutionResult:
        self.placed_orders.append((signal, volume, entry_price))
        return ExecutionResult(success=True, message="ok", order_id="1", signal=signal, dry_run=False)


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
