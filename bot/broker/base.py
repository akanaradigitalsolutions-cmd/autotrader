from abc import ABC, abstractmethod
from typing import Optional

from bot.models import ExecutionResult, OpenPosition, TradeSignal


class ExecutionClient(ABC):
    """Abstract trade execution backend, so the engine isn't tied to one broker API."""

    @abstractmethod
    async def connect(self) -> None: ...

    async def reconnect(self) -> None:
        """Rebuild a broken connection so a retry doesn't reuse a dead one.

        Default is a no-op; backends with stateful connections override it.
        """

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    async def count_open_positions(self, symbol: str) -> int: ...

    async def get_positions(self, symbol: str) -> list[OpenPosition]:
        """Open positions for the symbol. Default empty; real backends override.

        Used by the conflict guard (don't hold BUY and SELL at once) and the
        breakeven monitor (move a runner's stop after TP1 hits).
        """
        return []

    async def modify_stop_loss(self, position_id: str, stop_loss: float) -> bool:
        """Move an open position's stop loss. Default: unsupported (False)."""
        return False

    async def close_position(self, position_id: str) -> bool:
        """Close an open position at market. Default: unsupported (False)."""
        return False

    async def get_closed_profit(self, position_id: str) -> Optional[float]:
        """Realized net profit of a closed position, or None if unknown.

        Used only for the trade journal; a None result just leaves that
        trade's outcome blank in the report.
        """
        return None

    async def get_closes(
        self, symbol: str, timeframe: str = "H1", count: int = 100
    ) -> list[float]:
        """Recent candle close prices (oldest first). Default empty; used by
        the trend filter. Real backends override."""
        return []

    async def get_pending_orders(self, symbol: str) -> list[str]:
        """Ids of not-yet-filled pending orders for the symbol. Default empty."""
        return []

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a pending order. Default: unsupported (False)."""
        return False

    async def get_account_balance(self) -> Optional[float]:
        """Realized account balance, or None if the backend can't report it."""
        return None

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
