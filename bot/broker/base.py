from abc import ABC, abstractmethod

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
    async def place_order(self, signal: TradeSignal, volume: float) -> ExecutionResult: ...
