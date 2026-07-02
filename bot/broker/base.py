from abc import ABC, abstractmethod
from typing import Optional

from bot.models import ExecutionResult, TradeSignal


class ExecutionClient(ABC):
    """Abstract trade execution backend, so the engine isn't tied to one broker API."""

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    async def count_open_positions(self, symbol: str) -> int: ...

    @abstractmethod
    async def get_current_price(self, symbol: str) -> tuple[float, float]:
        """Returns (bid, ask) for the symbol."""
        ...

    @abstractmethod
    async def place_order(
        self, signal: TradeSignal, volume: float, entry_price: Optional[float] = None
    ) -> ExecutionResult:
        """Places a trade for the signal.

        entry_price=None executes immediately at market. Otherwise places a
        pending limit order that fills when price reaches entry_price -
        used when the signal's entry zone hasn't been reached yet.
        """
        ...
