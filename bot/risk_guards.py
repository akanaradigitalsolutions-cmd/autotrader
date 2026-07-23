import logging
from datetime import datetime, timezone
from typing import Callable, Optional

from bot.broker.base import ExecutionClient

logger = logging.getLogger(__name__)


class DailyLossGuard:
    """Blocks new trades once the day's realized loss crosses a threshold.

    The day-start balance is captured the first time a new UTC day is seen,
    so the limit is measured against where the account started that day.
    A restart resets the reference to the current balance (the day's earlier
    losses before the restart are not remembered) - acceptable for a simple
    circuit breaker.
    """

    def __init__(
        self,
        broker: ExecutionClient,
        limit_percent: float,
        now: Optional[Callable[[], datetime]] = None,
    ):
        self._broker = broker
        self._limit_percent = limit_percent
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._day = None
        self._day_start_balance: Optional[float] = None

    async def should_block(self) -> bool:
        if self._limit_percent <= 0:
            return False
        try:
            balance = await self._broker.get_account_balance()
        except Exception:
            logger.exception("Daily loss guard could not read the account balance")
            return False  # can't tell - don't block trading on a read error
        if balance is None:
            return False

        today = self._now().date()
        if self._day != today or self._day_start_balance is None:
            self._day = today
            self._day_start_balance = balance
            return False
        if self._day_start_balance <= 0:
            return False

        drawdown_pct = (self._day_start_balance - balance) / self._day_start_balance * 100
        return drawdown_pct >= self._limit_percent
