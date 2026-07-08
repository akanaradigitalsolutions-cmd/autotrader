import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Union

from bot.broker.base import ExecutionClient
from bot.config import Settings
from bot.engine import TradingEngine
from bot.status import build_status_report

logger = logging.getLogger(__name__)

SET_PATTERN = re.compile(r"^/set\s+(lot|trades)\s+(\S+)\s*$", re.IGNORECASE)
CONFIRM_WORDS = {"yes", "y", "/confirm"}
CANCEL_WORDS = {"no", "n", "/cancel"}

USAGE = (
    "Unknown command. Available:\n"
    "/status\n"
    "/set lot <value>\n"
    "/set trades <value>"
)


@dataclass
class PendingChange:
    attr: str  # Settings attribute name, e.g. "lot_size"
    env_key: str  # .env key, e.g. "LOT_SIZE"
    old_value: Union[float, int]
    new_value: Union[float, int]


class CommandHandler:
    """Handles /status and /set commands sent to your own Saved Messages.

    /set changes require a YES/NO confirmation reply before taking effect,
    since they directly change how much money is risked per trade. Confirmed
    changes apply immediately in-memory and are written to .env so they
    survive a restart.
    """

    def __init__(
        self,
        settings: Settings,
        broker: ExecutionClient,
        engine: TradingEngine,
        start_time: float,
        env_path: str = ".env",
    ):
        self.settings = settings
        self.broker = broker
        self.engine = engine
        self.start_time = start_time
        self.env_path = Path(env_path)
        self._pending: PendingChange | None = None

    async def handle(self, text: str) -> str:
        stripped = text.strip()
        lower = stripped.lower()

        if self._pending is not None:
            if lower in CONFIRM_WORDS:
                return self._apply_pending()
            if lower in CANCEL_WORDS:
                pending = self._pending
                self._pending = None
                return f"Cancelled. {pending.env_key} stays at {pending.old_value}."
            # A new command overrides an unanswered confirmation instead of
            # getting stuck waiting for a yes/no forever.
            self._pending = None

        if lower == "/status":
            return await build_status_report(
                self.settings, self.broker, self.engine, self.start_time
            )

        if lower.startswith("/set"):
            match = SET_PATTERN.match(stripped)
            if not match:
                return "Usage: /set lot <value>  or  /set trades <value>"
            return self._propose_change(match.group(1).lower(), match.group(2))

        return USAGE

    def _propose_change(self, field: str, raw_value: str) -> str:
        try:
            value = float(raw_value)
        except ValueError:
            return f"Invalid number: {raw_value}"

        if field == "lot":
            if value <= 0 or value > self.settings.max_lot_size:
                return f"Lot size must be > 0 and <= MAX_LOT_SIZE ({self.settings.max_lot_size})."
            old_value = self.settings.lot_size
            self._pending = PendingChange("lot_size", "LOT_SIZE", old_value, value)
            return f"Change LOT_SIZE from {old_value} to {value}?\nReply YES to confirm, NO to cancel."

        if value <= 0 or not value.is_integer() or value > 10:
            return "Trades/signal must be a whole number between 1 and 10."
        new_value = int(value)
        old_value = self.settings.trades_per_signal
        self._pending = PendingChange("trades_per_signal", "TRADES_PER_SIGNAL", old_value, new_value)
        return (
            f"Change TRADES_PER_SIGNAL from {old_value} to {new_value}?\n"
            "Reply YES to confirm, NO to cancel."
        )

    def _apply_pending(self) -> str:
        pending = self._pending
        self._pending = None
        setattr(self.settings, pending.attr, pending.new_value)
        self._persist_to_env(pending.env_key, pending.new_value)
        logger.warning(
            "Config changed via Telegram: %s %s -> %s",
            pending.attr,
            pending.old_value,
            pending.new_value,
        )
        return f"Done. {pending.env_key} is now {pending.new_value} (saved to .env)."

    def _persist_to_env(self, key: str, value: Union[float, int]) -> None:
        lines = self.env_path.read_text().splitlines() if self.env_path.exists() else []
        pattern = re.compile(rf"^{re.escape(key)}\s*=")
        new_line = f"{key}={value}"
        for i, line in enumerate(lines):
            if pattern.match(line):
                lines[i] = new_line
                break
        else:
            lines.append(new_line)
        self.env_path.write_text("\n".join(lines) + "\n")
