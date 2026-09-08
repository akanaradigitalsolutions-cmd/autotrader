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
        self.closed: list[str] = []
        self.closes: list[float] = []  # candle closes for the trend filter

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

    async def close_position(self, position_id: str) -> bool:
        self.closed.append(position_id)
        return True

    async def get_current_price(self, symbol: str) -> tuple[float, float]:
        return self.bid, self.ask

    async def get_closes(self, symbol, timeframe="H1", count=100):
        return list(self.closes)

    async def get_pending_orders(self, symbol):
        return []

    async def cancel_order(self, order_id):
        return True

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
# A realistic BUY: stop below the entry zone, take-profits above it.
BUY_SIGNAL = "gold buy 4310-15\nsl 4300\ntp 4325\ntp 4335\ntp 4400"


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
async def test_cancellation_closes_the_positions_opened_for_that_signal():
    broker = FakeBroker(bid=4057, ask=4057)  # in the 4055-59 sell zone
    engine = TradingEngine(make_settings(), SignalParser(), broker)

    await engine.handle_message("gold sell 4055-59\nsl 4067\ntp 4050\ntp 4034\ntp 3955")
    assert len(broker.placed_orders) >= 1

    # The channel then quotes the same signal and says don't trade it.
    await engine.handle_message(
        "gold sell 4055-59 sl 4067 tp 4050 tp 4034 tp 3955\ndon't trade it"
    )

    assert len(broker.closed) == len(broker.placed_orders)  # every position closed


@pytest.mark.asyncio
async def test_cancellation_arriving_first_blocks_the_signal():
    broker = FakeBroker(bid=4057, ask=4057)
    engine = TradingEngine(make_settings(), SignalParser(), broker)

    # "don't trade it" seen before the signal is ever executed.
    await engine.handle_message(
        "gold sell 4055-59 sl 4067 tp 4050 tp 4034 tp 3955\ndon't trade it"
    )
    await engine.handle_message("gold sell 4055-59\nsl 4067\ntp 4050\ntp 4034\ntp 3955")

    assert broker.placed_orders == []  # never opened


@pytest.mark.asyncio
async def test_cancellation_of_a_different_signal_does_not_close_positions():
    broker = FakeBroker(bid=4057, ask=4057)
    engine = TradingEngine(make_settings(), SignalParser(), broker)

    await engine.handle_message("gold sell 4055-59\nsl 4067\ntp 4050\ntp 4034\ntp 3955")
    # Cancellation quotes a DIFFERENT signal (different levels).
    await engine.handle_message(
        "gold sell 4061-65 sl 4075 tp 4054 tp 4041 tp 3955\ndon't trade it"
    )

    assert broker.closed == []  # the open position is left alone


@pytest.mark.asyncio
async def test_close_at_entry_with_no_levels_closes_the_last_signal():
    broker = FakeBroker(bid=4057, ask=4057)  # in the 4055-59 sell zone -> filled
    engine = TradingEngine(make_settings(), SignalParser(), broker)

    await engine.handle_message("gold sell 4055-59\nsl 4067\ntp 4050\ntp 4034\ntp 3955")
    assert len(broker.placed_orders) >= 1

    # A follow-up message with NO price levels, just the instruction.
    await engine.handle_message("close at entry")

    assert len(broker.closed) == len(broker.placed_orders)  # the last trade flattened


@pytest.mark.asyncio
async def test_close_at_entry_prefers_the_same_channels_last_signal():
    broker = FakeBroker(bid=4057, ask=4057)
    engine = TradingEngine(make_settings(), SignalParser(), broker)

    # Channel A opens a trade, then channel B opens a different one.
    await engine.handle_message(
        "gold sell 4055-59\nsl 4067\ntp 4050\ntp 4034\ntp 3955", source=-100
    )
    a_orders = len(broker.placed_orders)
    await engine.handle_message(
        "gold sell 4056-60\nsl 4068\ntp 4051\ntp 4035\ntp 3956", source=-200
    )

    # Channel A says "close at entry" - only A's positions must close.
    await engine.handle_message("close at entry", source=-100)

    assert len(broker.closed) == a_orders
    assert broker.closed == [str(i) for i in range(1, a_orders + 1)]


@pytest.mark.asyncio
async def test_close_at_entry_cancels_a_still_pending_order():
    # Price below the zone -> pending limit orders, nothing filled yet.
    broker = FakeBroker(bid=4300, ask=4300.2)
    broker.pending = []

    async def get_pending_orders(symbol):
        return list(broker.pending)

    async def cancel_order(order_id):
        broker.cancelled.append(order_id)
        return True

    broker.cancelled = []
    broker.get_pending_orders = get_pending_orders
    broker.cancel_order = cancel_order

    engine = TradingEngine(make_settings(), SignalParser(), broker)
    await engine.handle_message("gold sell 4314-18\nsl 4325.9\ntp 4309\ntp 4301\ntp 4200")
    # Every placed order is still pending (unfilled).
    broker.pending = [str(i) for i in range(1, len(broker.placed_orders) + 1)]

    await engine.handle_message("close at entry")

    # Pending orders were CANCELLED, not market-closed.
    assert broker.cancelled == broker.pending
    assert broker.closed == []


UPTREND_CLOSES = [float(x) for x in range(4000, 4100)]  # rising -> price > EMA


@pytest.mark.asyncio
async def test_trend_filter_shadow_logs_but_still_trades():
    broker = FakeBroker(bid=4099, ask=4099)
    broker.closes = UPTREND_CLOSES  # uptrend
    engine = TradingEngine(make_settings(trend_filter="shadow"), SignalParser(), broker)

    # A SELL in an uptrend is counter-trend, but shadow mode still trades it.
    await engine.handle_message("gold sell 4098-4100\nsl 4110\ntp 4090\ntp 4080")

    assert len(broker.placed_orders) >= 1


@pytest.mark.asyncio
async def test_trend_filter_on_skips_counter_trend_signal():
    broker = FakeBroker(bid=4099, ask=4099)
    broker.closes = UPTREND_CLOSES
    engine = TradingEngine(make_settings(trend_filter="on"), SignalParser(), broker)

    await engine.handle_message("gold sell 4098-4100\nsl 4110\ntp 4090\ntp 4080")

    assert broker.placed_orders == []  # counter-trend sell blocked


@pytest.mark.asyncio
async def test_trend_filter_on_allows_with_trend_signal():
    broker = FakeBroker(bid=4099, ask=4099)
    broker.closes = UPTREND_CLOSES
    engine = TradingEngine(make_settings(trend_filter="on"), SignalParser(), broker)

    # A BUY in an uptrend is with the trend - allowed.
    await engine.handle_message("gold buy 4098-4100\nsl 4088\ntp 4110\ntp 4120")

    assert len(broker.placed_orders) >= 1


@pytest.mark.asyncio
async def test_no_sl_signal_refused_by_default():
    broker = FakeBroker(bid=4450, ask=4450)
    engine = TradingEngine(make_settings(), SignalParser(), broker)

    # "SL: VIP" style - no numeric stop.
    await engine.handle_message("#XAUUSD SELL 4450\ntp 4445\ntp 4440\ntp 4435")

    assert broker.placed_orders == []


@pytest.mark.asyncio
async def test_fallback_stop_loss_lets_a_no_sl_signal_trade():
    broker = FakeBroker(bid=4450, ask=4450)
    engine = TradingEngine(
        make_settings(fallback_stop_loss_pips=120, skip_reward_risk_below=0.0),
        SignalParser(), broker,
    )

    await engine.handle_message("#XAUUSD SELL 4450\ntp 4445\ntp 4440\ntp 4435")

    assert len(broker.placed_orders) >= 1
    # synthetic stop is 120 pips = $12 ABOVE entry for a sell
    placed_signal = broker.placed_orders[0][0]
    assert placed_signal.stop_loss == 4462.0


@pytest.mark.asyncio
async def test_immediate_entry_market_fills_instead_of_pending():
    # Price below a SELL zone would normally place a pending limit; with
    # immediate_entry it markets in now instead.
    broker = FakeBroker(bid=4300, ask=4300)
    engine = TradingEngine(
        make_settings(immediate_entry=True), SignalParser(), broker
    )

    await engine.handle_message(SELL_SIGNAL)  # price below zone

    assert len(broker.placed_orders) == 3
    assert all(entry_price is None for _, _, entry_price in broker.placed_orders)


@pytest.mark.asyncio
async def test_misparsed_signal_with_levels_on_wrong_side_is_refused():
    # A mangled SELL: entry ~420 but stop/TPs are ~4100 (real levels), so
    # the take-profits sit ABOVE the entry - impossible for a sell.
    broker = FakeBroker(bid=4120, ask=4120)
    alerts = []

    async def notify(text):
        alerts.append(text)

    engine = TradingEngine(make_settings(), SignalParser(), broker, notify=notify)

    # Build the broken signal directly (the parser mangles some channels'
    # formatting into exactly this shape).
    from bot.models import TradeSignal

    bad = TradeSignal(
        symbol="XAUUSD", direction=Direction.SELL, entry=420.0,
        entry_low=418.0, entry_high=422.0, stop_loss=4129.0,
        take_profits=[4113.0, 4105.0, 4010.0], raw_text="x",
    )
    engine.parser = _StubParser(bad)

    await engine.handle_message("anything")

    assert broker.placed_orders == []
    assert len(alerts) == 1
    assert "misparsed" in alerts[0].lower()


class _StubParser:
    def __init__(self, signal):
        self._signal = signal

    def parse(self, text):
        return self._signal


@pytest.mark.asyncio
async def test_low_reward_risk_signal_warns_but_still_trades():
    # BUY 4139, SL 4129 (risk 10), TP1 4146 (reward 7) -> R:R 0.7, below 1.0
    broker = FakeBroker(bid=4139, ask=4139)
    warnings = []

    async def risk_notify(text):
        warnings.append(text)

    engine = TradingEngine(
        make_settings(skip_reward_risk_below=0.0), SignalParser(), broker,
        risk_notify=risk_notify,
    )

    await engine.handle_message(
        "GOLD BUY NOW : 4139\nSL : 4129\nTP 1st: 70PIPS\nTP 2nd: 150PIPS"
    )

    assert len(warnings) == 1
    assert "reward:risk" in warnings[0].lower()
    assert len(broker.placed_orders) >= 1  # warned, not blocked (filter off)


@pytest.mark.asyncio
async def test_rr_filter_skips_low_reward_risk_signals():
    # BUY 4139/SL 4129/TP1 4146 -> R:R 0.70, below the 1.0 filter.
    broker = FakeBroker(bid=4139, ask=4139)
    skipped = []

    async def risk_notify(text):
        skipped.append(text)

    engine = TradingEngine(
        make_settings(skip_reward_risk_below=1.0), SignalParser(), broker,
        risk_notify=risk_notify,
    )

    await engine.handle_message(
        "GOLD BUY NOW : 4139\nSL : 4129\nTP 1st: 70PIPS\nTP 2nd: 150PIPS"
    )

    assert broker.placed_orders == []  # skipped, not traded
    assert len(skipped) == 1
    assert "Skipped" in skipped[0]


@pytest.mark.asyncio
async def test_rr_filter_allows_good_reward_risk_signals():
    broker = FakeBroker(bid=4120, ask=4120)
    engine = TradingEngine(
        make_settings(skip_reward_risk_below=1.0), SignalParser(), broker
    )

    # SELL 4118-22/SL 4128/TP1 4099 -> R:R ~2.6, above the filter.
    await engine.handle_message("gold sell 4118-22\nsl 4128\ntp 4099\ntp 4090")

    assert len(broker.placed_orders) >= 1


@pytest.mark.asyncio
async def test_rr_filter_disabled_trades_everything():
    broker = FakeBroker(bid=4139, ask=4139)
    engine = TradingEngine(
        make_settings(skip_reward_risk_below=0.0), SignalParser(), broker
    )

    await engine.handle_message(
        "GOLD BUY NOW : 4139\nSL : 4129\nTP 1st: 70PIPS\nTP 2nd: 150PIPS"
    )

    assert len(broker.placed_orders) >= 1  # filter off - low R:R still trades


@pytest.mark.asyncio
async def test_good_reward_risk_signal_does_not_warn():
    broker = FakeBroker(bid=4120, ask=4120)
    warnings = []

    async def risk_notify(text):
        warnings.append(text)

    engine = TradingEngine(
        make_settings(), SignalParser(), broker, risk_notify=risk_notify
    )

    # SELL 4120, SL 4128 (risk 8), TP1 4099 (reward 21) -> R:R 2.6
    await engine.handle_message("gold sell 4118-22\nsl 4128\ntp 4099\ntp 4090")

    assert warnings == []


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
