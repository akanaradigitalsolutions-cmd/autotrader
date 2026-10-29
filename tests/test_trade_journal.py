from bot.models import Direction
from bot.trade_journal import TradeJournal


def make_journal(tmp_path):
    return TradeJournal(str(tmp_path / "trades.csv"))


def test_summary_empty_when_no_trades(tmp_path):
    assert "No trades" in make_journal(tmp_path).summary()


def test_records_and_summarizes_per_channel(tmp_path):
    j = make_journal(tmp_path)
    # Channel A: one win, one loss.
    j.record_open("1", -100, Direction.SELL, "XAUUSD", 4120, 4128, 4099, 2.6, 0.01)
    j.record_open("2", -100, Direction.BUY, "XAUUSD", 4139, 4129, 4146, 0.7, 0.01)
    # Channel B: one loss.
    j.record_open("3", -200, Direction.SELL, "XAUUSD", 4056, 4067, 4050, 0.5, 0.01)
    j.record_close("1", 21.0)   # win
    j.record_close("2", -11.0)  # loss
    j.record_close("3", -11.0)  # loss

    summary = j.summary()

    assert "OVERALL: 3 trades (3 closed)" in summary
    assert "1W/2L" in summary
    assert "channel -100:" in summary
    assert "channel -200:" in summary
    # Overall net P/L = 21 - 11 - 11 = -1.00
    assert "-1.00" in summary


def test_open_without_close_is_counted_but_not_won_or_lost(tmp_path):
    j = make_journal(tmp_path)
    j.record_open("9", -100, Direction.SELL, "XAUUSD", 4120, 4128, 4099, 2.6, 0.01)

    summary = j.summary()

    assert "1 trades (0 closed)" in summary
    assert "0W/0L" in summary


def test_open_without_close_is_listed_for_backfill(tmp_path):
    j = make_journal(tmp_path)
    j.record_open("1", -100, Direction.SELL, "XAUUSDm", 4120, 4128, 4099, 2.6, 0.01)
    j.record_open("2", -100, Direction.BUY, "XAUUSDm", 4139, 4129, 4146, 0.7, 0.01)
    j.record_close("1", 5.0)  # 1 is closed, 2 is not

    pending = j.open_position_ids_without_close()

    assert pending == {"2": "XAUUSDm"}


def test_blank_reward_risk_does_not_poison_the_average(tmp_path):
    j = make_journal(tmp_path)
    j.record_open("1", -100, Direction.SELL, "XAUUSD", 4120, 4128, 4099, 2.0, 0.01)
    j.record_open("2", -100, Direction.SELL, "XAUUSD", 4120, 4128, 4099, None, 0.01)  # blank rr
    j.record_close("1", 5.0)
    j.record_close("2", 5.0)

    summary = j.summary()

    # avg R:R must be the one valid value (2.00), not NaN
    assert "avg R:R 2.00" in summary
    assert "nan" not in summary.lower()


def test_writing_never_raises_on_bad_path(tmp_path):
    # A directory where a file is expected - _append must swallow the error.
    bad = tmp_path / "dir"
    bad.mkdir()
    j = TradeJournal(str(bad))
    j.record_open("1", None, Direction.BUY, "XAUUSD", 1, 1, 1, None, 0.01)  # must not raise
