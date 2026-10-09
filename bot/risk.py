from typing import Optional

from bot.config import Settings
from bot.models import TradeSignal


def resolve_lot_size(settings: Settings) -> float:
    """Lot size is fixed per the account config, capped as a safety limit."""
    return min(settings.lot_size, settings.max_lot_size)


def reward_risk_ratio(signal: TradeSignal) -> Optional[float]:
    """Distance to TP1 divided by distance to the stop loss.

    Below 1.0 means TP1 is nearer than the stop - the trade risks more than
    its first target pays, the low-quality setup that quietly bleeds an
    account. None when the signal lacks an entry, stop, or take profit.
    """
    if signal.entry is None or signal.stop_loss is None or not signal.take_profits:
        return None
    risk = abs(signal.entry - signal.stop_loss)
    if risk == 0:
        return None
    reward = abs(signal.take_profits[0] - signal.entry)
    return reward / risk


def take_profits_for_trades(
    take_profits: list[float], trades_per_signal: int
) -> list[Optional[float]]:
    """Splits a signal's TP levels across separate trades, one TP each.

    e.g. take_profits=[4451, 4444, 4390], trades_per_signal=2 -> [4451, 4444]
    (each becomes its own trade with the same entry/SL but a single TP).
    """
    if not take_profits:
        return [None]
    count = max(1, min(trades_per_signal, len(take_profits)))
    return take_profits[:count]
