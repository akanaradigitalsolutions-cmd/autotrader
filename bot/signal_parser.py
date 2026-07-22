import re
from typing import Optional

from bot.models import Direction, TradeSignal

# Common aliases signal providers use for gold
SYMBOL_ALIASES = {
    "XAUUSD": "XAUUSD",
    "XAU/USD": "XAUUSD",
    "XAU-USD": "XAUUSD",
    "GOLD": "XAUUSD",
}

DIRECTION_PATTERN = re.compile(r"\b(BUY|SELL|LONG|SHORT)\b", re.IGNORECASE)
SYMBOL_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in SYMBOL_ALIASES) + r")\b", re.IGNORECASE
)
# [^\d\n]{0,20} tolerates filler between a keyword and its number - real
# channels put words/emoji/arrows there ("ENTRY POINT 👉 4007_4004",
# "STOP LOSS 🛑 3997", "TAKE PROFIT 🛡 4012"). It cannot cross a newline,
# so a keyword never steals a number from the next line.
ENTRY_PATTERN = re.compile(
    r"(?:\b(?:ENTRY|PRICE)[^\d\n]{0,20}|@\s*)(\d{3,5}(?:\.\d+)?)"
    r"(?:\s*[-/_]\s*(\d{1,5}(?:\.\d+)?))?",
    re.IGNORECASE,
)
# Fallback for signals with no "Entry:" keyword, e.g. "gold sell 4455-60"
# or "Gold sell 4118/21" (shorthand: "21" means "...21").
INLINE_RANGE_PATTERN = re.compile(
    r"(\d{3,5}(?:\.\d+)?)(?:\s*[-/_]\s*(\d{1,5}(?:\.\d+)?))?"
)
SL_PATTERN = re.compile(
    r"\b(?:S\s*/?\s*L|STOP\s*LOSS)[^\d\n]{0,20}(\d{3,5}(?:\.\d+)?)", re.IGNORECASE
)
# The value part accepts slash-separated shorthand lists ("4115/10/5"
# meaning 4115, 4110, 4105) as used by some channels.
TP_PATTERN = re.compile(
    r"\b(?:T\s*/?\s*P\d?|TAKE\s*PROFIT\d?)[^\d\n]{0,20}"
    r"(\d{3,5}(?:\.\d+)?(?:\s*/\s*\d{1,5}(?:\.\d+)?)*)",
    re.IGNORECASE,
)


def _expand_shorthand(base: float, part: str) -> float:
    """Expands "33" against 4128 -> 4133 (replace trailing digits)."""
    if "." in part or len(part) >= len(str(int(base))):
        return float(part)
    base_str = str(int(base))
    return float(base_str[: -len(part)] + part)


def _expand_range(low: float, high_raw: Optional[str]) -> tuple[float, float]:
    if not high_raw:
        return low, low
    high = _expand_shorthand(low, high_raw)
    return (low, high) if low <= high else (high, low)


def _expand_tp_values(token: str, direction: Direction) -> list[float]:
    """Expands a TP token like "4115/10/5" into [4115, 4110, 4105].

    Shorthand parts splice into the previous value's digits; when that
    produces a value on the wrong side (TPs must move away from entry:
    down for SELL, up for BUY), shift it one digit-magnitude to keep the
    sequence monotonic - e.g. SELL "4115/10/5": "5" splices to 4115,
    which is not below 4110, so it becomes 4105.
    """
    parts = [p.strip() for p in token.split("/")]
    values = [float(parts[0])]
    for raw in parts[1:]:
        prev = values[-1]
        if "." in raw or len(raw) >= len(str(int(prev))):
            value = float(raw)
        else:
            value = _expand_shorthand(prev, raw)
            if direction == Direction.SELL and value >= prev:
                value -= 10 ** len(raw)
            elif direction == Direction.BUY and value <= prev:
                value += 10 ** len(raw)
        values.append(value)
    return values


class SignalParser:
    """Extracts a structured TradeSignal from a raw Telegram message.

    Signal message formats vary a lot between providers, so this only
    understands the common keyword-based layout (SYMBOL, BUY/SELL, SL, TP...).
    Tune the regexes above to match your specific channel's format.
    """

    def __init__(self, allowed_symbol: str = "XAUUSD"):
        self.allowed_symbol = allowed_symbol.upper()

    def parse(self, text: str) -> Optional[TradeSignal]:
        if not text:
            return None

        symbol_match = SYMBOL_PATTERN.search(text)
        direction_match = DIRECTION_PATTERN.search(text)
        if not symbol_match or not direction_match:
            return None

        symbol = SYMBOL_ALIASES[symbol_match.group(1).upper()]
        if symbol != self.allowed_symbol:
            return None

        direction_raw = direction_match.group(1).upper()
        direction = Direction.BUY if direction_raw in ("BUY", "LONG") else Direction.SELL

        entry_low, entry_high = self._parse_entry_zone(text, direction_match)
        entry = round((entry_low + entry_high) / 2, 2) if entry_low is not None else None

        stop_loss = None
        sl_match = SL_PATTERN.search(text)
        if sl_match:
            stop_loss = float(sl_match.group(1))

        take_profits: list[float] = []
        for token in TP_PATTERN.findall(text):
            take_profits.extend(_expand_tp_values(token, direction))
        # de-duplicate while preserving order
        seen = set()
        take_profits = [tp for tp in take_profits if not (tp in seen or seen.add(tp))]

        return TradeSignal(
            symbol=symbol,
            direction=direction,
            entry=entry,
            entry_low=entry_low,
            entry_high=entry_high,
            stop_loss=stop_loss,
            take_profits=take_profits,
            raw_text=text,
        )

    @staticmethod
    def _parse_entry_zone(
        text: str, direction_match: re.Match
    ) -> tuple[Optional[float], Optional[float]]:
        entry_match = ENTRY_PATTERN.search(text)
        if entry_match:
            return _expand_range(
                float(entry_match.group(1)), entry_match.group(2)
            )

        # No "Entry:" keyword - look for a bare number/range on the direction's
        # own line, e.g. "gold sell 4455-60" (shorthand: "60" means "...60").
        # Stop before any SL/TP on the same line so those aren't mistaken for entry.
        line = text[direction_match.end():].splitlines()[0]
        cutoff = len(line)
        for keyword_pattern in (SL_PATTERN, TP_PATTERN):
            keyword_match = keyword_pattern.search(line)
            if keyword_match:
                cutoff = min(cutoff, keyword_match.start())
        inline_match = INLINE_RANGE_PATTERN.search(line[:cutoff])
        if not inline_match:
            return None, None

        return _expand_range(float(inline_match.group(1)), inline_match.group(2))
