import pytest

from bot.models import Direction
from bot.signal_parser import SignalParser


REAL_CHANNEL_SIGNALS = [
    (
        "gold sell 4090-95\nsl 4108\ntp 4084\ntp 4070\ntp 4000",
        Direction.SELL, 4092.5, 4108, [4084, 4070, 4000],
    ),
    (
        "gold sell 3977-81\nsl 3988\ntp 3970\ntp 3960\ntp 3877",
        Direction.SELL, 3979, 3988, [3970, 3960, 3877],
    ),
    (
        "gold sell 3996-4000\nsl 4009\ntp 3990\ntp 3980\ntp 3890",
        Direction.SELL, 3998, 4009, [3990, 3980, 3890],
    ),
    (
        "gold sell 4153-58\nsl 4165\ntp 4149\ntp 4144\ntp 4060",
        Direction.SELL, 4155.5, 4165, [4149, 4144, 4060],
    ),
    (
        "gold sell 4314-18\nsl 4325.9\ntp 4309\ntp 4301\ntp 4200",
        Direction.SELL, 4316, 4325.9, [4309, 4301, 4200],
    ),
    (
        "Gold buy 4310-15\nsl 4325\ntp 4305\ntp 4295\ntp 4210",
        Direction.BUY, 4312.5, 4325, [4305, 4295, 4210],
    ),
]


@pytest.mark.parametrize("text,direction,entry,sl,tps", REAL_CHANNEL_SIGNALS)
def test_parses_real_channel_signal_samples(text, direction, entry, sl, tps):
    signal = SignalParser(allowed_symbol="XAUUSD").parse(text)

    assert signal is not None
    assert signal.direction == direction
    assert signal.entry == entry
    assert signal.stop_loss == sl
    assert signal.take_profits == tps


def make_parser():
    return SignalParser(allowed_symbol="XAUUSD")


def test_parses_keyword_format():
    text = """
    GOLD BUY
    Entry: 2350
    SL: 2340
    TP1: 2360
    TP2: 2370
    TP3: 2380
    """
    signal = make_parser().parse(text)

    assert signal is not None
    assert signal.symbol == "XAUUSD"
    assert signal.direction == Direction.BUY
    assert signal.entry == 2350
    assert signal.stop_loss == 2340
    assert signal.take_profits == [2360, 2370, 2380]
    assert signal.primary_take_profit == 2360


def test_parses_compact_single_line_format():
    text = "SELL XAUUSD @2355 SL 2365 TP 2345"
    signal = make_parser().parse(text)

    assert signal is not None
    assert signal.direction == Direction.SELL
    assert signal.entry == 2355
    assert signal.stop_loss == 2365
    assert signal.take_profits == [2345]


def test_parses_entry_range_as_midpoint():
    text = "BUY GOLD Entry 2350-2352 SL: 2340 TP: 2360"
    signal = make_parser().parse(text)

    assert signal is not None
    assert signal.entry == 2351


def test_ignores_other_symbols():
    text = "BUY EURUSD Entry 1.0800 SL 1.0750 TP 1.0900"
    signal = make_parser().parse(text)

    assert signal is None


def test_ignores_messages_without_a_direction():
    text = "XAUUSD is looking bullish today, watch the 2350 level"
    signal = make_parser().parse(text)

    assert signal is None


def test_handles_missing_entry_as_market_order():
    text = "BUY XAUUSD NOW SL 2340 TP 2360"
    signal = make_parser().parse(text)

    assert signal is not None
    assert signal.entry is None


def test_parses_real_world_lowercase_shorthand_signal():
    text = """gold sell 4455-60
sl 4468
tp 4451
tp 4444
tp 4390"""
    signal = make_parser().parse(text)

    assert signal is not None
    assert signal.symbol == "XAUUSD"
    assert signal.direction == Direction.SELL
    assert signal.entry == 4457.5
    assert signal.stop_loss == 4468
    assert signal.take_profits == [4451, 4444, 4390]
