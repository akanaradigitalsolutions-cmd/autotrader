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

        entry = None
        entry_match = ENTRY_PATTERN.search(text)
        if entry_match:
            low = float(entry_match.group(1))
            high = float(entry_match.group(2)) if entry_match.group(2) else low
            entry = round((low + high) / 2, 2)

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
