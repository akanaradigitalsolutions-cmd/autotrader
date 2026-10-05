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
    settings_overrides.setdefault("pause_state_file", str(tmp_path / "pause.json"))
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
async def test_set_daily_loss_applies_and_refreshes_the_guard(tmp_path):
    handler = make_handler(tmp_path, daily_loss_limit_percent=5.0)

    proposal = await handler.handle("/set daily_loss 3")
    assert "5.0 to 3" in proposal
    assert handler.settings.daily_loss_limit_percent == 5.0  # not yet applied

    confirmation = await handler.handle("yes")
    assert handler.settings.daily_loss_limit_percent == 3.0
    # the live guard must reflect the new limit, not just settings
    assert handler.engine._daily_loss.limit_percent == 3.0
    assert "DAILY_LOSS_LIMIT_PERCENT=3.0" in handler.env_path.read_text()


@pytest.mark.asyncio
async def test_set_daily_loss_rejects_out_of_range(tmp_path):
    handler = make_handler(tmp_path)

    reply = await handler.handle("/set daily_loss 150")

    assert "between 0 and 100" in reply


@pytest.mark.asyncio
async def test_pause_and_resume(tmp_path):
    handler = make_handler(tmp_path)

    reply = await handler.handle("/pause")
    assert "PAUSED" in reply
    assert handler.engine.pause.is_paused() is True

    reply = await handler.handle("/resume")
    assert "RESUMED" in reply
    assert handler.engine.pause.is_paused() is False


@pytest.mark.asyncio
async def test_pause_with_duration(tmp_path):
    handler = make_handler(tmp_path)

    reply = await handler.handle("/pause 1d")
    assert "PAUSED" in reply
    assert "left" in reply
    assert handler.engine.pause.is_paused() is True


@pytest.mark.asyncio
async def test_pause_with_bad_duration_is_rejected(tmp_path):
    handler = make_handler(tmp_path)

    reply = await handler.handle("/pause whenever")
    assert "Couldn't read" in reply
    assert handler.engine.pause.is_paused() is False  # not paused on a bad arg


@pytest.mark.asyncio
async def test_status_shows_pause_state(tmp_path):
    handler = make_handler(tmp_path)
    await handler.handle("/pause 4h")

    reply = await handler.handle("/status")

    assert "PAUSED" in reply


@pytest.mark.asyncio
async def test_unrelated_message_does_not_consume_pending_confirmation_silently(tmp_path):
    handler = make_handler(tmp_path)

    await handler.handle("/set lot 0.02")
    reply = await handler.handle("/status")

    assert "Lot size: 0.01" in reply  # unchanged - confirmation was abandoned, not applied
