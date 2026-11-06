import json

import pytest

from bot.config import Settings
from bot.market_recorder import MarketRecorder
from bot.models import Direction, TradeSignal


class FakeBroker:
    def __init__(self):
        self.calls = []

    async def get_candles(self, symbol, timeframe, count):
        self.calls.append((symbol, timeframe, count))
        # one deterministic candle so the output is checkable
        return [{"t": 1700000000, "o": 4000.0, "h": 4005.0, "l": 3998.0, "c": 4002.0, "v": 10}]


def make_settings(tmp_path, **over):
    base = dict(
        telegram_api_id=1, telegram_api_hash="h", telegram_channel="@c",
        metaapi_token="t", metaapi_account_id="a",
        market_data_file=str(tmp_path / "snap.jsonl"),
        market_data_timeframes="M1,M5,H1", market_data_candles=1,
        record_market_data=True,
    )
    base.update(over)
    return Settings(**base)


def a_signal():
    return TradeSignal(
        symbol="XAUUSD", direction=Direction.SELL, entry=4030.0,
        entry_low=4028.0, entry_high=4032.0, stop_loss=4040.0,
        take_profits=[4020.0, 4010.0], market_now=True, raw_text="gold sell now 4028-4032",
    )


@pytest.mark.asyncio
async def test_records_a_snapshot_line(tmp_path):
    settings = make_settings(tmp_path)
    broker = FakeBroker()
    rec = MarketRecorder(broker, settings)

    await rec.record(a_signal(), "EXECUTED", source=-100, position_ids=["111", "222"], rr=1.0)

    lines = (tmp_path / "snap.jsonl").read_text().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["action"] == "EXECUTED"
    assert row["source"] == -100
    assert row["direction"] == "SELL"
    assert row["position_ids"] == ["111", "222"]
    assert row["stop_loss"] == 4040.0
    assert set(row["candles"].keys()) == {"M1", "M5", "H1"}
    assert row["candles"]["M1"][0]["c"] == 4002.0


@pytest.mark.asyncio
async def test_disabled_recorder_writes_nothing(tmp_path):
    settings = make_settings(tmp_path, record_market_data=False)
    rec = MarketRecorder(FakeBroker(), settings)

    await rec.record(a_signal(), "EXECUTED")

    assert not (tmp_path / "snap.jsonl").exists()


@pytest.mark.asyncio
async def test_broker_without_candles_still_records(tmp_path):
    class NoCandles:
        async def get_candles(self, *a, **k):
            raise RuntimeError("no candle feed")

    rec = MarketRecorder(NoCandles(), make_settings(tmp_path))
    await rec.record(a_signal(), "SKIPPED", reason="low_rr")

    row = json.loads((tmp_path / "snap.jsonl").read_text().splitlines()[0])
    assert row["action"] == "SKIPPED"
    assert row["reason"] == "low_rr"
    assert row["candles"]["M1"] == []  # fetch failed, recorded empty, no crash


@pytest.mark.asyncio
async def test_recording_never_raises_on_bad_signal(tmp_path):
    rec = MarketRecorder(FakeBroker(), make_settings(tmp_path))
    await rec.record(object(), "EXECUTED")  # junk signal must not raise
