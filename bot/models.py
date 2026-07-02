from enum import Enum
from typing import Optional

from pydantic import BaseModel


class Direction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class TradeSignal(BaseModel):
    symbol: str
    direction: Direction
    entry: Optional[float] = None  # None means "execute at market"
    stop_loss: Optional[float] = None
    take_profits: list[float] = []
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
