import csv
import logging
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

COLUMNS = [
    "type", "time", "position_id", "source", "direction", "symbol",
    "entry", "sl", "tp1", "rr", "volume", "profit",
]


class TradeJournal:
    """Append-only CSV of trades, so per-channel performance can be measured.

    Writing never raises into the caller - analytics must never interfere
    with trade execution. An OPEN row is written when a position opens; a
    CLOSE row (with realized profit) when it closes. `summary()` pairs them.
    """

    def __init__(self, path: str = "logs/trades.csv"):
        self._path = Path(path)

    def record_open(
        self, position_id, source, direction, symbol, entry, sl, tp1, rr, volume
    ) -> None:
        self._append({
            "type": "OPEN", "time": self._now(), "position_id": str(position_id),
            "source": source if source is not None else "",
            "direction": getattr(direction, "value", direction),
            "symbol": symbol, "entry": entry, "sl": sl, "tp1": tp1,
            "rr": "" if rr is None else round(rr, 2), "volume": volume,
        })

    def record_close(self, position_id, profit: Optional[float]) -> None:
        self._append({
            "type": "CLOSE", "time": self._now(), "position_id": str(position_id),
            "profit": "" if profit is None else round(profit, 2),
        })

    def _append(self, row: dict) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            write_header = not self._path.exists()
            with self._path.open("a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                if write_header:
                    writer.writerow(COLUMNS)
                writer.writerow([row.get(col, "") for col in COLUMNS])
        except Exception:
            logger.exception("Failed to write trade journal row")

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    def summary(self) -> str:
        if not self._path.exists():
            return "No trades recorded yet."

        opens: dict[str, dict] = {}
        closes: dict[str, dict] = {}
        try:
            with self._path.open(newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    if row["type"] == "OPEN":
                        opens[row["position_id"]] = row
                    elif row["type"] == "CLOSE":
                        closes[row["position_id"]] = row
        except Exception:
            logger.exception("Failed to read trade journal")
            return "Could not read the trade journal."

        stats: dict[str, dict] = defaultdict(
            lambda: {"n": 0, "wins": 0, "losses": 0, "pnl": 0.0, "rr_sum": 0.0, "rr_n": 0}
        )

        def record(key: str, open_row: dict, profit: Optional[float]) -> None:
            s = stats[key]
            s["n"] += 1
            if profit is not None:
                s["pnl"] += profit
                if profit > 0:
                    s["wins"] += 1
                elif profit < 0:
                    s["losses"] += 1
            try:
                s["rr_sum"] += float(open_row.get("rr", "") or "nan")
                s["rr_n"] += 1
            except ValueError:
                pass

        for pid, open_row in opens.items():
            close_row = closes.get(pid)
            profit = None
            if close_row and close_row.get("profit", "") != "":
                try:
                    profit = float(close_row["profit"])
                except ValueError:
                    profit = None
            record("ALL", open_row, profit)
            record(open_row.get("source") or "unknown", open_row, profit)

        if not stats:
            return "No trades recorded yet."

        lines = []
        ordered = ["ALL"] + sorted(k for k in stats if k != "ALL")
        for key in ordered:
            s = stats[key]
            closed = s["wins"] + s["losses"]
            win_rate = (s["wins"] / closed * 100) if closed else 0.0
            avg_rr = (s["rr_sum"] / s["rr_n"]) if s["rr_n"] else 0.0
            label = "OVERALL" if key == "ALL" else f"channel {key}"
            lines.append(
                f"{label}: {s['n']} trades ({closed} closed) | "
                f"win {win_rate:.0f}% {s['wins']}W/{s['losses']}L | "
                f"P/L {s['pnl']:+.2f} | avg R:R {avg_rr:.2f}"
            )
        return "\n".join(lines)
