from datetime import timedelta

from bot.commands import parse_duration
from bot.trade_pause import TradePause


def make_pause(tmp_path):
    return TradePause(str(tmp_path / "pause.json"))


def test_starts_active(tmp_path):
    p = make_pause(tmp_path)
    assert p.is_paused() is False
    assert "ACTIVE" in p.status_text()


def test_indefinite_pause_and_resume(tmp_path):
    p = make_pause(tmp_path)
    p.pause(None)
    assert p.is_paused() is True
    assert "indefinite" in p.status_text().lower()
    p.resume()
    assert p.is_paused() is False


def test_timed_pause_auto_expires(tmp_path):
    p = make_pause(tmp_path)
    p.pause(timedelta(seconds=-1))  # already in the past -> expired immediately
    assert p.is_paused() is False  # auto-resumed


def test_timed_pause_still_active_within_window(tmp_path):
    p = make_pause(tmp_path)
    p.pause(timedelta(hours=5))
    assert p.is_paused() is True
    assert "left" in p.status_text()


def test_state_persists_across_instances(tmp_path):
    path = str(tmp_path / "pause.json")
    TradePause(path).pause(timedelta(hours=8))
    # A brand-new instance (as after a bot restart) must still be paused.
    revived = TradePause(path)
    assert revived.is_paused() is True


def test_corrupt_state_file_starts_active(tmp_path):
    path = tmp_path / "pause.json"
    path.write_text("not json{{{")
    p = TradePause(str(path))
    assert p.is_paused() is False


def test_parse_duration_variants():
    assert parse_duration("1d") == timedelta(days=1)
    assert parse_duration("4h") == timedelta(hours=4)
    assert parse_duration("30m") == timedelta(minutes=30)
    assert parse_duration("1d6h") == timedelta(days=1, hours=6)
    assert parse_duration("2") == timedelta(hours=2)  # bare number = hours
    assert parse_duration("0") is None
    assert parse_duration("abc") is None
    assert parse_duration("1dxyz") is None
