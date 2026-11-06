import pytest

from bot.config import Settings
from bot.models import Direction, TradeSignal
from bot.risk import resolve_lot_size, reward_risk_ratio, take_profits_for_trades


def make_signal(**overrides) -> TradeSignal:
    defaults = dict(
        symbol="XAUUSD",
        direction=Direction.BUY,
        entry=4139.0,
        stop_loss=4129.0,
        take_profits=[4146.0],
        raw_text="x",
    )
    defaults.update(overrides)
    return TradeSignal(**defaults)


def make_settings(**overrides) -> Settings:
    defaults = dict(
        telegram_api_id=1,
        telegram_api_hash="hash",
        telegram_channel="@channel",
        metaapi_token="token",
        metaapi_account_id="account",
    )
    defaults.update(overrides)
    return Settings(**defaults)


def test_uses_configured_lot_size():
    settings = make_settings(lot_size=0.05, max_lot_size=1.0)
    assert resolve_lot_size(settings) == 0.05


def test_caps_lot_size_at_max():
    settings = make_settings(lot_size=5.0, max_lot_size=1.0)
    assert resolve_lot_size(settings) == 1.0


def test_splits_take_profits_across_configured_trade_count():
    assert take_profits_for_trades([4451, 4444, 4390], trades_per_signal=2) == [4451, 4444]


def test_caps_trade_count_at_available_take_profits():
    assert take_profits_for_trades([4451], trades_per_signal=3) == [4451]


def test_falls_back_to_single_trade_with_no_take_profit():
    assert take_profits_for_trades([], trades_per_signal=3) == [None]


def test_reward_risk_below_one_when_tp1_nearer_than_stop():
    # BUY 4139, SL 4129 (risk 10), TP1 4146 (reward 7) -> 0.7
    assert reward_risk_ratio(make_signal()) == pytest.approx(0.7)


def test_reward_risk_above_one_for_a_good_setup():
    # SELL 4120, SL 4128 (risk 8), TP1 4099 (reward 21) -> 2.625
    signal = make_signal(
        direction=Direction.SELL, entry=4120.0, stop_loss=4128.0, take_profits=[4099.0]
    )
    assert reward_risk_ratio(signal) == pytest.approx(2.625)


def test_reward_risk_is_none_without_entry_or_stop_or_tp():
    assert reward_risk_ratio(make_signal(entry=None)) is None
    assert reward_risk_ratio(make_signal(stop_loss=None)) is None
    assert reward_risk_ratio(make_signal(take_profits=[])) is None
    assert reward_risk_ratio(make_signal(entry=4129.0, stop_loss=4129.0)) is None
