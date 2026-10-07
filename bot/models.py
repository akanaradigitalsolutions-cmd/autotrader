from enum import Enum
from typing import Optional

from pydantic import BaseModel


class Direction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class TradeSignal(BaseModel):
    symbol: str
    direction: Direction
    entry: Optional[float] = None  # midpoint of the entry zone, for display/logging
    entry_low: Optional[float] = None
    entry_high: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profits: list[float] = []
    # True when the signal says to enter immediately ("sell now", "at market"),
    # so the engine fires at market instead of waiting for a zone/limit price.
    market_now: bool = False
    raw_text: str

    @property
    def primary_take_profit(self) -> Optional[float]:
        return self.take_profits[0] if self.take_profits else None


class ExecutionResult(BaseModel):
    success: bool
    message: str
    order_id: Optional[str] = None
    signal: TradeSignal
    dry_run: bool


class OpenPosition(BaseModel):
    """A currently-open broker position (used by the conflict guard and the
    breakeven monitor)."""

    id: str
    symbol: str
    direction: Direction
    volume: float
    open_price: float
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    profit: float = 0.0
