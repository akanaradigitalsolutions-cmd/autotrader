import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Candle fetches for the snapshot are bounded so data collection can never
# hang a signal decision.
FETCH_TIMEOUT_SECONDS = 15


class MarketRecorder:
    """Appends a market snapshot to a JSONL dataset for every signal decision.

    Each line records WHAT the signal was (direction, entry, SL, TPs, R:R,
    channel), HOW the bot acted on it (EXECUTED / SIMULATED / SKIPPED + reason),
    the opened position ids, and WHAT the market looked like at that moment -
    the last N candles on each configured timeframe (M1..H1). Later you join a
    line to its outcome by position id (via the trade journal's P/L), giving
    labelled examples to learn from or train a model on.

    Writing never raises into the caller - collecting data must never disturb
    trading. Off unless RECORD_MARKET_DATA is set.
    """

    def __init__(self, broker, settings, path: Optional[str] = None):
        self._broker = broker
        self._settings = settings
        self._path = Path(path or settings.market_data_file)
        self._timeframes = [
            tf.strip().upper()
            for tf in settings.market_data_timeframes.split(",")
            if tf.strip()
        ]
        self._count = settings.market_data_candles

    @property
    def enabled(self) -> bool:
        return bool(self._settings.record_market_data)

    async def record(
        self, signal, action: str, source=None, reason: Optional[str] = None,
        position_ids=None, rr: Optional[float] = None,
    ) -> None:
        if not self.enabled:
            return
        try:
            candles = {}
            for tf in self._timeframes:
                try:
                    candles[tf] = await asyncio.wait_for(
                        self._broker.get_candles(
                            self._settings.broker_symbol, tf, self._count
                        ),
                        timeout=FETCH_TIMEOUT_SECONDS,
                    )
                except Exception:
                    logger.exception("Market snapshot: could not fetch %s candles", tf)
                    candles[tf] = []

            record = {
                "time": datetime.now(timezone.utc).isoformat(),
                "action": action,
                "reason": reason,
                "source": source,
                "symbol": self._settings.broker_symbol,
                "direction": getattr(signal.direction, "value", str(signal.direction)),
                "entry_low": signal.entry_low,
                "entry_high": signal.entry_high,
                "stop_loss": signal.stop_loss,
                "take_profits": list(signal.take_profits),
                "rr": round(rr, 3) if rr is not None else None,
                "market_now": getattr(signal, "market_now", False),
                "position_ids": [str(i) for i in (position_ids or []) if i],
                "raw_text": (getattr(signal, "raw_text", "") or "")[:500],
                "candles": candles,
            }
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
            logger.debug("Recorded %s market snapshot (%s)", action, reason or "")
        except Exception:
            logger.exception("Failed to record market snapshot")
