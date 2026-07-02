from bot.config import Settings
from bot.risk import resolve_lot_size


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
