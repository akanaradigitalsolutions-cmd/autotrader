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


# Formats seen in the "PAID VIP SIGNALS" channel (from user screenshots):
# emoji/words between keyword and number, "_" and "/" entry ranges, and
# slash-shorthand TP lists.
PAID_VIP_SIGNALS = [
    (
        "🔽GOLD BUY NOW 🔽\n\nENTRY POINT 👉 4007_4004\n"
        "TAKE PROFIT 🛡 4012\nTAKE PROFIT 🛡 4016\nTAKE PROFIT 🛡 4020\n"
        "STOP LOSS 🛑 3997",
        Direction.BUY, (4004.0, 4007.0), 3997, [4012, 4016, 4020],
    ),
    (
        "Gold sell 4118/21\nSL 4133\n\nTp 4115\nTp 4110\nTp 4110\nTp 4050",
        Direction.SELL, (4118.0, 4121.0), 4133, [4115, 4110, 4050],
    ),
    (
        "gold buy 4175-72\nsl 4162\ntp 4180\ntp 4190\ntp 4250",
        Direction.BUY, (4172.0, 4175.0), 4162, [4180, 4190, 4250],
    ),
    (
        "GOlD SEll : 4128/33\nSL : 4140\n💱\nTP : 4115/10/5\nTP : Open\n\nPlZZ ❕❗️AWARE",
        Direction.SELL, (4128.0, 4133.0), 4140, [4115, 4110, 4105],
    ),
    (
        "GOlD SEll : 4152/56\nSL : 4165\n💱\nTP : 4140/20/03\nTP : Open",
        Direction.SELL, (4152.0, 4156.0), 4165, [4140, 4120, 4103],
    ),
    (
        "GOlD BUY : 4169/65\nSL : 4159\n💱\nTP : 4190/4220/50\nTP : Open",
        Direction.BUY, (4165.0, 4169.0), 4159, [4190, 4220, 4250],
    ),
]


@pytest.mark.parametrize("text,direction,zone,sl,tps", PAID_VIP_SIGNALS)
def test_parses_paid_vip_channel_formats(text, direction, zone, sl, tps):
    signal = make_parser().parse(text)

    assert signal is not None
    assert signal.direction == direction
    assert (signal.entry_low, signal.entry_high) == zone
    assert signal.stop_loss == sl
    assert signal.take_profits == tps


# Formats from the "Gold Sell/Buy Zone @ ..." channel (user screenshots).
ZONE_CHANNEL_SIGNALS = [
    (
        "Gold Sell Zone @ 4058 - 4063 🔴\n\nStoploss: 4068\n"
        "Take profits: 4053 / 4051 / 4049",
        Direction.SELL, (4058.0, 4063.0), 4068, [4053, 4051, 4049],
    ),
    (
        "Gold Buy Now @ 4102-4107 🟢\n\nStoploss: 4097\n"
        "Take profits: 4112 / 4114 / 4116",
        Direction.BUY, (4102.0, 4107.0), 4097, [4112, 4114, 4116],
    ),
    (
        "Gold Sell Zone @ 4103 - 4108 🔴\n\nStoploss: 4112\n"
        "Take profits: 4098 / 4096 / 4094",
        Direction.SELL, (4103.0, 4108.0), 4112, [4098, 4096, 4094],
    ),
]


@pytest.mark.parametrize("text,direction,zone,sl,tps", ZONE_CHANNEL_SIGNALS)
def test_parses_zone_channel_formats(text, direction, zone, sl, tps):
    signal = make_parser().parse(text)

    assert signal is not None
    assert signal.direction == direction
    assert (signal.entry_low, signal.entry_high) == zone
    assert signal.stop_loss == sl
    assert signal.take_profits == tps


# Formats from the pips-based channel: "GOLD BUY NOW : 4139" with TPs as
# pip distances from entry (1 gold pip = $0.10).
PIPS_CHANNEL_SIGNALS = [
    (
        "GOLD BUY NOW : 4139 ✅✅\n\nSL : 4129\nTP 1st: 70PIPS\nTP 2nd: 150PIPS\n\n"
        "Use Proper Entry & Money Management ‼️",
        Direction.BUY, (4139.0, 4139.0), 4129, [4146.0, 4154.0],
    ),
    (
        "GOLD BUY NOW : 4160 ✅✅\n\nSL : 4150\nTP 1st: 70PIPS\nTP 2nd: 150PIPS\n\n"
        "Use Proper Entry & Money Management ‼️",
        Direction.BUY, (4160.0, 4160.0), 4150, [4167.0, 4175.0],
    ),
    (
        "GOLD BUY NOW : 4065 ✅✅\n\nSL : 4055\nTP 1st: 70PIPS\nTP 2nd: 150PIPS",
        Direction.BUY, (4065.0, 4065.0), 4055, [4072.0, 4080.0],
    ),
    (
        # Synthetic sell variant: pips TPs must go BELOW entry for a sell.
        "GOLD SELL NOW : 4139\nSL : 4149\nTP 1st: 70PIPS\nTP 2nd: 150PIPS",
        Direction.SELL, (4139.0, 4139.0), 4149, [4132.0, 4124.0],
    ),
]


@pytest.mark.parametrize("text,direction,zone,sl,tps", PIPS_CHANNEL_SIGNALS)
def test_parses_pips_based_tp_formats(text, direction, zone, sl, tps):
    signal = make_parser().parse(text)

    assert signal is not None
    assert signal.direction == direction
    assert (signal.entry_low, signal.entry_high) == zone
    assert signal.stop_loss == sl
    assert signal.take_profits == tps


def test_pips_value_is_never_mistaken_for_an_absolute_tp_price():
    # "150PIPS" must not be read as an absolute TP of 150.
    text = "GOLD BUY NOW : 4139\nSL : 4129\nTP 2nd: 150PIPS"
    signal = make_parser().parse(text)

    assert signal is not None
    assert 150.0 not in signal.take_profits
    assert signal.take_profits == [4154.0]


# Single-TP "Now : <price>" channel (user screenshots).
SINGLE_TP_CHANNEL_SIGNALS = [
    (
        "Gold Sell Now : 4077\nSL : 4085\nTP : 4062",
        Direction.SELL, (4077.0, 4077.0), 4085, [4062],
    ),
    (
        "Gold Buy Now : 4067\nSL : 4060\nTP : 4101",
        Direction.BUY, (4067.0, 4067.0), 4060, [4101],
    ),
    (
        "Gold Buy Now : 4008\nSL : 4000\nTP : 4031",
        Direction.BUY, (4008.0, 4008.0), 4000, [4031],
    ),
]


@pytest.mark.parametrize("text,direction,zone,sl,tps", SINGLE_TP_CHANNEL_SIGNALS)
def test_parses_single_tp_now_format(text, direction, zone, sl, tps):
    signal = make_parser().parse(text)

    assert signal is not None
    assert signal.direction == direction
    assert (signal.entry_low, signal.entry_high) == zone
    assert signal.stop_loss == sl
    assert signal.take_profits == tps


def test_thousands_separator_commas_are_stripped():
    signal = make_parser().parse(
        "Gold Sell Zone @ 4,118 - 4,122\nStoploss: 4,129\nTake profits: 4,113 / 4,105"
    )
    assert signal is not None
    assert (signal.entry_low, signal.entry_high) == (4118.0, 4122.0)
    assert signal.stop_loss == 4129.0
    assert signal.take_profits == [4113.0, 4105.0]


def test_teaser_posts_without_any_price_level_are_not_signals():
    # Channels post hype messages before the real signal - these must not
    # be treated as tradable signals.
    assert make_parser().parse("Gold Buy Now") is None
    assert make_parser().parse("Gold Sell Now") is None


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
