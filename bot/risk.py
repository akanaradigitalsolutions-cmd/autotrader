from typing import Optional

from bot.config import Settings


def resolve_lot_size(settings: Settings) -> float:
    """Lot size is fixed per the account config, capped as a safety limit."""
    return min(settings.lot_size, settings.max_lot_size)


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
