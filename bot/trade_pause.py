import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class TradePause:
    """Pause / resume execution of NEW trades, controllable from Telegram.

    Built for high-impact events (NFP, FOMC) where signals are dangerous.
    While paused the bot still runs - it monitors open trades, honours
    cancellations ("close at entry"), and answers commands - it simply does
    not OPEN new positions.

    State is persisted to a small JSON file so a pause set the night before
    an event survives a bot restart. A timed pause auto-expires and resumes
    on its own; an indefinite pause stays until /resume.
    """

    def __init__(self, path: str = ".pause_state.json"):
        self._path = Path(path)
        self._paused = False
        self._until: Optional[datetime] = None  # None while paused = indefinite
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
        except Exception:
            logger.exception("Could not read pause state - starting un-paused")
            return
        self._paused = bool(data.get("paused", False))
        raw_until = data.get("until")
        if raw_until:
            try:
                self._until = datetime.fromisoformat(raw_until)
            except ValueError:
                self._until = None

    def _save(self) -> None:
        try:
            self._path.write_text(
                json.dumps(
                    {
                        "paused": self._paused,
                        "until": self._until.isoformat() if self._until else None,
                    }
                )
            )
        except Exception:
            logger.exception("Could not persist pause state")

    def pause(self, duration: Optional[timedelta] = None) -> None:
        """Pause new trades. duration=None pauses indefinitely."""
        self._paused = True
        self._until = (datetime.now(timezone.utc) + duration) if duration else None
        self._save()
        logger.warning("Trading PAUSED (%s)", self.status_text())

    def resume(self) -> None:
        was_paused = self._paused
        self._paused = False
        self._until = None
        self._save()
        if was_paused:
            logger.warning("Trading RESUMED")

    def is_paused(self) -> bool:
        if not self._paused:
            return False
        if self._until is not None and datetime.now(timezone.utc) >= self._until:
            # A timed pause has elapsed - auto-resume so trading restarts
            # without needing a manual /resume.
            self.resume()
            return False
        return True

    def status_text(self) -> str:
        if not self.is_paused():
            return "trading ACTIVE"
        if self._until is None:
            return "trading PAUSED (indefinite - send /resume to restart)"
        remaining = self._until - datetime.now(timezone.utc)
        return (
            f"trading PAUSED until {self._until:%Y-%m-%d %H:%M} UTC "
            f"({self._human(remaining)} left)"
        )

    @staticmethod
    def _human(delta: timedelta) -> str:
        total = max(int(delta.total_seconds()), 0)
        days, rem = divmod(total, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, _ = divmod(rem, 60)
        parts = []
        if days:
            parts.append(f"{days}d")
        if hours:
            parts.append(f"{hours}h")
        if minutes and not days:  # minutes matter less once we're counting days
            parts.append(f"{minutes}m")
        return " ".join(parts) or "under 1m"
