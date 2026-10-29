import logging
import re
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Optional, Union

from bot.broker.base import ExecutionClient
from bot.config import Settings
from bot.engine import TradingEngine
from bot.status import build_status_report

logger = logging.getLogger(__name__)

SET_PATTERN = re.compile(r"^/set\s+(\w+)\s+(.+?)\s*$", re.IGNORECASE)
PAUSE_PATTERN = re.compile(r"^/pause(?:\s+(.+))?$", re.IGNORECASE)
# Duration tokens like "1d", "4h", "30m" (any mix, e.g. "1d6h").
DURATION_TOKEN = re.compile(r"(\d+)\s*([dhm])", re.IGNORECASE)
CONFIRM_WORDS = {"yes", "y", "/confirm"}
CANCEL_WORDS = {"no", "n", "/cancel"}

SET_USAGE = (
    "/set <setting> <value> — asks YES/NO, saves to .env:\n"
    "Risk:   lot 0.02 | trades 2 | max_positions 6 | daily_loss 10\n"
    "Filter: skip_rr 1.0 | min_rr 1.0 | trend off|shadow|on | dup_window 120\n"
    "Entry:  immediate_entry on|off | now_guard 10 | fallback_sl 120 | pending_expiry 60\n"
    "Trail:  trail 60 10  (or trail off)\n"
    "Guards: conflict on|off | breakeven on|off | reentry on|off | require_sl on|off | cancellations on|off\n"
    "Sim:    sim_skip_rr 1.0 | sim_trail 60 10  (or sim_trail off)"
)

USAGE = (
    "Commands:\n"
    "/status   /report   /simreport\n"
    "/pause [1d|4h|30m]   /resume\n" + SET_USAGE
)

TRUE_WORDS = {"on", "true", "1", "yes", "y", "enable", "enabled"}
FALSE_WORDS = {"off", "false", "0", "no", "n", "disable", "disabled"}


def parse_bool(s: str) -> Optional[bool]:
    s = s.strip().lower()
    if s in TRUE_WORDS:
        return True
    if s in FALSE_WORDS:
        return False
    return None


@dataclass
class _Spec:
    attr: str  # Settings attribute
    env_key: str  # .env key
    kind: str  # 'float' | 'int' | 'percent' | 'bool' | 'enum'
    minv: Optional[float] = None
    maxv: Optional[float] = None
    choices: tuple = ()


# Single-value settings tunable from Telegram (lot/trail/sim_trail are handled
# specially below). Credentials, channels, symbol, paths and the backend are
# deliberately NOT here - they need a restart or would be unsafe to change live.
SET_SPECS = {
    "trades": _Spec("trades_per_signal", "TRADES_PER_SIGNAL", "int", 1, 10),
    "max_positions": _Spec("max_open_positions", "MAX_OPEN_POSITIONS", "int", 1, 50),
    "max": _Spec("max_open_positions", "MAX_OPEN_POSITIONS", "int", 1, 50),
    "daily_loss": _Spec("daily_loss_limit_percent", "DAILY_LOSS_LIMIT_PERCENT", "percent", 0, 100),
    "skip_rr": _Spec("skip_reward_risk_below", "SKIP_REWARD_RISK_BELOW", "float", 0, 100),
    "min_rr": _Spec("min_reward_risk", "MIN_REWARD_RISK", "float", 0, 100),
    "fallback_sl": _Spec("fallback_stop_loss_pips", "FALLBACK_STOP_LOSS_PIPS", "float", 0, 100000),
    "now_guard": _Spec("market_entry_min_stop_pips", "MARKET_ENTRY_MIN_STOP_PIPS", "float", 0, 100000),
    "pending_expiry": _Spec("pending_order_expiry_minutes", "PENDING_ORDER_EXPIRY_MINUTES", "int", 0, 100000),
    "dup_window": _Spec("duplicate_signal_window_minutes", "DUPLICATE_SIGNAL_WINDOW_MINUTES", "int", 0, 100000),
    "sim_skip_rr": _Spec("sim_skip_reward_risk_below", "SIM_SKIP_REWARD_RISK_BELOW", "float", 0, 100),
    "conflict": _Spec("prevent_opposite_positions", "PREVENT_OPPOSITE_POSITIONS", "bool"),
    "breakeven": _Spec("breakeven_after_tp1", "BREAKEVEN_AFTER_TP1", "bool"),
    "reentry": _Spec("reenter_after_close", "REENTER_AFTER_CLOSE", "bool"),
    "require_sl": _Spec("require_stop_loss", "REQUIRE_STOP_LOSS", "bool"),
    "immediate_entry": _Spec("immediate_entry", "IMMEDIATE_ENTRY", "bool"),
    "cancellations": _Spec("honor_cancellations", "HONOR_CANCELLATIONS", "bool"),
    "trend": _Spec("trend_filter", "TREND_FILTER", "enum", choices=("off", "shadow", "on")),
}


def parse_duration(arg: str) -> Optional[timedelta]:
    """Parse '1d' / '4h' / '30m' / '1d6h' (or a bare number = hours).

    Returns None if the string can't be read as a duration.
    """
    arg = arg.strip().lower()
    tokens = DURATION_TOKEN.findall(arg)
    if tokens:
        # Reject stray characters so "1dxyz" isn't silently accepted as 1d.
        if DURATION_TOKEN.sub("", arg).strip():
            return None
        total = timedelta()
        for num, unit in tokens:
            n = int(num)
            total += (
                timedelta(days=n) if unit == "d"
                else timedelta(hours=n) if unit == "h"
                else timedelta(minutes=n)
            )
        return total if total > timedelta() else None
    try:
        hours = float(arg)
    except ValueError:
        return None
    return timedelta(hours=hours) if hours > 0 else None


@dataclass
class _Edit:
    attr: str  # Settings attribute, e.g. "lot_size"
    env_key: str  # .env key, e.g. "LOT_SIZE"
    old_value: Union[float, int]
    new_value: Union[float, int]


@dataclass
class PendingChange:
    edits: list  # list[_Edit] - usually one, two for /set trail
    label: str  # human label for the messages, e.g. "LOT_SIZE" or "TRAIL"
    old_desc: str
    new_desc: str


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
        journal=None,
        sim_journal=None,
    ):
        self.settings = settings
        self.broker = broker
        self.engine = engine
        self.start_time = start_time
        self.env_path = Path(env_path)
        self.journal = journal
        self.sim_journal = sim_journal
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
                return f"Cancelled. {pending.label} stays at {pending.old_desc}."
            # A new command overrides an unanswered confirmation instead of
            # getting stuck waiting for a yes/no forever.
            self._pending = None

        if lower == "/status":
            return await build_status_report(
                self.settings, self.broker, self.engine, self.start_time
            )

        if lower == "/report":
            if self.journal is None:
                return "Trade journal is not enabled."
            return self.journal.summary()

        if lower == "/simreport":
            if self.sim_journal is None:
                return "Simulation is not enabled."
            return "SIMULATED (skipped signals, paper-traded):\n" + self.sim_journal.summary()

        if lower == "/resume":
            self.engine.pause.resume()
            return "▶️ Trading RESUMED - new signals will be traded again."

        if lower.startswith("/pause"):
            return self._handle_pause(stripped)

        if lower.startswith("/set"):
            match = SET_PATTERN.match(stripped)
            if not match:
                return SET_USAGE
            return self._propose_change(match.group(1), match.group(2))

        return USAGE

    def _handle_pause(self, stripped: str) -> str:
        match = PAUSE_PATTERN.match(stripped)
        arg = (match.group(1) or "").strip() if match else ""
        if not arg:
            self.engine.pause.pause(None)
            return (
                "⏸️ Trading PAUSED until you send /resume.\n"
                "Open trades keep running and cancellations still work - only "
                "NEW signals are skipped."
            )
        duration = parse_duration(arg)
        if duration is None:
            return (
                "Couldn't read that duration. Try: /pause 1d, /pause 4h, "
                "/pause 30m, /pause 1d6h, or /pause with no time for indefinite."
            )
        self.engine.pause.pause(duration)
        return (
            f"⏸️ {self.engine.pause.status_text()}.\n"
            "Open trades keep running; new signals are skipped. "
            "Send /resume to restart early."
        )

    def _propose_change(self, field: str, args: str) -> str:
        field = field.lower()
        if field == "lot":
            return self._propose_lot(args)
        if field == "trail":
            return self._propose_trail(args, sim=False)
        if field == "sim_trail":
            return self._propose_trail(args, sim=True)

        spec = SET_SPECS.get(field)
        if spec is None:
            return "Unknown setting.\n" + SET_USAGE

        old = getattr(self.settings, spec.attr)
        if spec.kind == "bool":
            new = parse_bool(args)
            if new is None:
                return f"Use on/off: /set {field} on   (or off)."
            old_desc, new_desc = ("on" if old else "off"), ("on" if new else "off")
        elif spec.kind == "enum":
            new = args.strip().lower()
            if new not in spec.choices:
                return f"{spec.env_key} must be one of: {', '.join(spec.choices)}."
            old_desc, new_desc = str(old), new
        else:  # float / int / percent
            try:
                value = float(args.strip())
            except ValueError:
                return f"Invalid number: {args}"
            if spec.kind == "int" and not value.is_integer():
                return f"{spec.env_key} must be a whole number between {int(spec.minv)} and {int(spec.maxv)}."
            if spec.kind == "percent" and (value < 0 or value > 100):
                return f"{spec.env_key} must be between 0 and 100 (0 disables)."
            if spec.minv is not None and value < spec.minv:
                return f"{spec.env_key} must be >= {spec.minv}."
            if spec.maxv is not None and value > spec.maxv:
                return f"{spec.env_key} must be <= {spec.maxv}."
            new = int(value) if spec.kind == "int" else value
            old_desc, new_desc = str(old), str(new)

        self._pending = PendingChange(
            [_Edit(spec.attr, spec.env_key, old, new)], spec.env_key, old_desc, new_desc
        )
        return (
            f"Change {spec.env_key} from {old_desc} to {new_desc}?\n"
            "Reply YES to confirm, NO to cancel."
        )

    def _propose_lot(self, args: str) -> str:
        try:
            value = float(args.strip())
        except ValueError:
            return f"Invalid number: {args}"
        if value <= 0 or value > self.settings.max_lot_size:
            return f"Lot size must be > 0 and <= MAX_LOT_SIZE ({self.settings.max_lot_size})."
        old = self.settings.lot_size
        self._pending = PendingChange(
            [_Edit("lot_size", "LOT_SIZE", old, value)], "LOT_SIZE", str(old), str(value)
        )
        return f"Change LOT_SIZE from {old} to {value}?\nReply YES to confirm, NO to cancel."

    def _propose_trail(self, args: str, sim: bool = False) -> str:
        name = "sim_trail" if sim else "trail"
        label = "SIM_TRAIL" if sim else "TRAIL"
        act_attr = "sim_trailing_activate_pips" if sim else "trailing_activate_pips"
        dist_attr = "sim_trailing_distance_pips" if sim else "trailing_distance_pips"
        act_key = ("SIM_" if sim else "") + "TRAILING_ACTIVATE_PIPS"
        dist_key = ("SIM_" if sim else "") + "TRAILING_DISTANCE_PIPS"

        parts = args.split()
        if len(parts) == 1 and parts[0].lower() in ("off", "0"):
            activate = distance = 0.0
        elif len(parts) == 2:
            try:
                activate, distance = float(parts[0]), float(parts[1])
            except ValueError:
                return f"{name} needs two numbers: /set {name} <activate_pips> <distance_pips>  (or /set {name} off)."
            if activate <= 0 or distance <= 0:
                return f"Both values must be > 0, or use /set {name} off to disable."
        else:
            return f"Usage: /set {name} <activate_pips> <distance_pips>  (e.g. /set {name} 60 10, or /set {name} off)."

        old_act = getattr(self.settings, act_attr)
        old_dist = getattr(self.settings, dist_attr)
        old_desc = f"{old_act:.0f}/{old_dist:.0f}p" if old_act > 0 and old_dist > 0 else "off"
        new_desc = f"{activate:.0f}/{distance:.0f}p" if activate > 0 and distance > 0 else "off"
        self._pending = PendingChange(
            [
                _Edit(act_attr, act_key, old_act, activate),
                _Edit(dist_attr, dist_key, old_dist, distance),
            ],
            label, old_desc, new_desc,
        )
        return (
            f"Change {label} (activate/distance) from {old_desc} to {new_desc}?\n"
            "Reply YES to confirm, NO to cancel."
        )

    def _apply_pending(self) -> str:
        pending = self._pending
        self._pending = None
        # Route through the engine so dependent guards (daily-loss circuit
        # breaker, trailing monitor) pick up the change immediately.
        for edit in pending.edits:
            self.engine.apply_setting(edit.attr, edit.new_value)
            self._persist_to_env(edit.env_key, edit.new_value)
            logger.warning(
                "Config changed via Telegram: %s %s -> %s",
                edit.attr, edit.old_value, edit.new_value,
            )
        return f"Done. {pending.label} is now {pending.new_desc} (saved to .env)."

    def _persist_to_env(self, key: str, value) -> None:
        # Write booleans as lowercase true/false so pydantic re-reads them.
        if isinstance(value, bool):
            value = "true" if value else "false"
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
