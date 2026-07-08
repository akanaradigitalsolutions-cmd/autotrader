import pytest

from bot.commands import CommandHandler
from bot.config import Settings
from bot.engine import TradingEngine
from bot.signal_parser import SignalParser


class FakeBroker:
    async def count_open_positions(self, symbol: str) -> int:
        return 0

    async def get_current_price(self, symbol: str) -> tuple[float, float]:
        return 1.0, 1.0

    async def place_order(self, signal, volume, entry_price=None):
        raise AssertionError("not used in these tests")


def make_settings(**overrides) -> Settings:
    defaults = dict(
        telegram_api_id=1,
        telegram_api_hash="hash",
        telegram_channel="@channel",
        metaapi_token="token",
        metaapi_account_id="account",
        lot_size=0.01,
        max_lot_size=1.0,
        trades_per_signal=2,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def make_handler(tmp_path, **settings_overrides) -> CommandHandler:
    settings = make_settings(**settings_overrides)
    engine = TradingEngine(settings, SignalParser(), FakeBroker())
    env_path = tmp_path / ".env"
    env_path.write_text("LOT_SIZE=0.01\nTRADES_PER_SIGNAL=2\n")
    return CommandHandler(settings, FakeBroker(), engine, start_time=0.0, env_path=str(env_path))


@pytest.mark.asyncio
async def test_status_reports_configured_values(tmp_path):
    handler = make_handler(tmp_path)
    reply = await handler.handle("/status")
    assert "Lot size: 0.01" in reply
    assert "Trades/signal: 2" in reply


@pytest.mark.asyncio
async def test_set_lot_requires_confirmation_before_applying(tmp_path):
    handler = make_handler(tmp_path)

    proposal = await handler.handle("/set lot 0.02")
    assert "0.01 to 0.02" in proposal
    assert handler.settings.lot_size == 0.01  # not yet applied

    confirmation = await handler.handle("yes")
    assert "0.02" in confirmation
    assert handler.settings.lot_size == 0.02
    assert "LOT_SIZE=0.02" in handler.env_path.read_text()


@pytest.mark.asyncio
async def test_set_change_can_be_cancelled(tmp_path):
    handler = make_handler(tmp_path)

    await handler.handle("/set trades 5")
    reply = await handler.handle("no")

    assert "Cancelled" in reply
    assert handler.settings.trades_per_signal == 2


@pytest.mark.asyncio
async def test_set_lot_rejects_value_above_max(tmp_path):
    handler = make_handler(tmp_path, max_lot_size=1.0)

    reply = await handler.handle("/set lot 5")

    assert "MAX_LOT_SIZE" in reply
    assert handler.settings.lot_size == 0.01


@pytest.mark.asyncio
async def test_unrelated_message_does_not_consume_pending_confirmation_silently(tmp_path):
    handler = make_handler(tmp_path)

    await handler.handle("/set lot 0.02")
    reply = await handler.handle("/status")

    assert "Lot size: 0.01" in reply  # unchanged - confirmation was abandoned, not applied
