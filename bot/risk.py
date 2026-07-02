from bot.config import Settings


def resolve_lot_size(settings: Settings) -> float:
    """Lot size is fixed per the account config, capped as a safety limit."""
    return min(settings.lot_size, settings.max_lot_size)
