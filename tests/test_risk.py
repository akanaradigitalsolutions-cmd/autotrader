from bot.config import Settings
from bot.risk import resolve_lot_size, take_profits_for_trades


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
