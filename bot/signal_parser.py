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
ENTRY_PATTERN = re.compile(
    r"(?:ENTRY|PRICE|@)\s*[:=\-]?\s*(\d{3,5}(?:\.\d+)?)(?:\s*[-/]\s*(\d{3,5}(?:\.\d+)?))?",
    re.IGNORECASE,
)
# Fallback for signals with no "Entry:" keyword, e.g. "gold sell 4455-60"
INLINE_RANGE_PATTERN = re.compile(r"(\d{3,5}(?:\.\d+)?)(?:\s*-\s*(\d{1,5}(?:\.\d+)?))?")
SL_PATTERN = re.compile(
    r"(?:S\s*/?\s*L|STOP\s*LOSS)\s*[:=\-]?\s*(\d{3,5}(?:\.\d+)?)", re.IGNORECASE
)
TP_PATTERN = re.compile(
    r"(?:T\s*/?\s*P\d?|TAKE\s*PROFIT\d?)\s*[:=\-]?\s*(\d{3,5}(?:\.\d+)?)", re.IGNORECASE
)


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

        entry = self._parse_entry(text, direction_match)

        stop_loss = None
        sl_match = SL_PATTERN.search(text)
        if sl_match:
            stop_loss = float(sl_match.group(1))

        take_profits = [float(v) for v in TP_PATTERN.findall(text)]
        # de-duplicate while preserving order
        seen = set()
        take_profits = [tp for tp in take_profits if not (tp in seen or seen.add(tp))]

        return TradeSignal(
            symbol=symbol,
            direction=direction,
            entry=entry,
            stop_loss=stop_loss,
            take_profits=take_profits,
            raw_text=text,
        )

    @staticmethod
    def _parse_entry(text: str, direction_match: re.Match) -> Optional[float]:
        entry_match = ENTRY_PATTERN.search(text)
        if entry_match:
            low = float(entry_match.group(1))
            high = float(entry_match.group(2)) if entry_match.group(2) else low
            return round((low + high) / 2, 2)

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
            return None

        low = float(inline_match.group(1))
        high_raw = inline_match.group(2)
        if not high_raw:
            return low
        if "." in high_raw or len(high_raw) >= len(str(int(low))):
            high = float(high_raw)
        else:
            low_str = str(int(low))
            high = float(low_str[: -len(high_raw)] + high_raw)
        return round((low + high) / 2, 2)
