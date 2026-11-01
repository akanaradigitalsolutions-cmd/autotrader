"""Build an analysis-ready, labelled dataset from the market snapshots.

Joins each market snapshot (logs/market_snapshots.jsonl) to its real outcome
(logs/trades.csv, by position id) and flattens the multi-timeframe candles into
a few derived features per timeframe (last close, EMA, trend direction, range).
Output is a plain CSV you can open in Excel or load into pandas.

Read-only. Run it any time, on the VPS or after copying the logs to your Mac:

    python scripts/build_dataset.py
    # or custom paths:
    python scripts/build_dataset.py --snapshots logs/market_snapshots.jsonl \
        --trades logs/trades.csv --out logs/dataset.csv

Outcome per row:
  win/loss/breakeven - an EXECUTED trade whose position(s) have all closed
  open               - EXECUTED, some position still open (no final P/L yet)
  no_fill            - EXECUTED but nothing opened (order failed)
  unknown            - SIMULATED/SKIPPED (real P/L not in trades.csv)
"""

import argparse
import csv
import json
from pathlib import Path

EMA_SPAN = 14  # candles, for the per-timeframe EMA/trend feature


def ema(values, span=EMA_SPAN):
    if not values:
        return None
    k = 2.0 / (span + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def load_outcomes(trades_path: Path):
    """position_id -> realised profit (from CLOSE rows); and set of all opens."""
    closed, opened = {}, set()
    if not trades_path.exists():
        return closed, opened
    with trades_path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            pid = row.get("position_id", "")
            if row.get("type") == "OPEN":
                opened.add(pid)
            elif row.get("type") == "CLOSE" and row.get("profit", "") not in ("", None):
                try:
                    closed[pid] = float(row["profit"])
                except ValueError:
                    pass
    return closed, opened


def tf_features(candles):
    """last close, EMA, trend (+1/-1/0 vs EMA), and range, for one timeframe."""
    if not candles:
        return {"close": "", "ema": "", "trend": "", "range": ""}
    closes = [c["c"] for c in candles]
    highs = [c["h"] for c in candles]
    lows = [c["l"] for c in candles]
    e = ema(closes)
    close = closes[-1]
    trend = 0 if e is None else (1 if close > e else (-1 if close < e else 0))
    return {
        "close": round(close, 3),
        "ema": round(e, 3) if e is not None else "",
        "trend": trend,
        "range": round(max(highs) - min(lows), 3),
    }


def outcome_for(row, closed, opened):
    pids = row.get("position_ids") or []
    if not pids:
        return ("no_fill" if row.get("action") == "EXECUTED" else "unknown"), ""
    profits = [closed[p] for p in pids if p in closed]
    if len(profits) == len(pids):
        total = sum(profits)
        label = "win" if total > 0 else ("loss" if total < 0 else "breakeven")
        return label, round(total, 2)
    # at least one not closed yet
    return "open", (round(sum(profits), 2) if profits else "")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshots", default="logs/market_snapshots.jsonl")
    ap.add_argument("--trades", default="logs/trades.csv")
    ap.add_argument("--out", default="logs/dataset.csv")
    args = ap.parse_args()

    snaps_path = Path(args.snapshots)
    if not snaps_path.exists():
        print(f"No snapshots file at {snaps_path} - enable RECORD_MARKET_DATA and let it run first.")
        return

    closed, opened = load_outcomes(Path(args.trades))

    rows = []
    timeframes = []
    with snaps_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                snap = json.loads(line)
            except json.JSONDecodeError:
                continue
            outcome, profit = outcome_for(snap, closed, opened)
            base = {
                "time": snap.get("time", ""),
                "action": snap.get("action", ""),
                "reason": snap.get("reason", "") or "",
                "source": snap.get("source", ""),
                "direction": snap.get("direction", ""),
                "entry_low": snap.get("entry_low", ""),
                "entry_high": snap.get("entry_high", ""),
                "stop_loss": snap.get("stop_loss", ""),
                "tp1": (snap.get("take_profits") or [""])[0],
                "rr": snap.get("rr", ""),
                "market_now": snap.get("market_now", ""),
                "num_positions": len(snap.get("position_ids") or []),
                "outcome": outcome,
                "profit": profit,
            }
            for tf, candles in (snap.get("candles") or {}).items():
                if tf not in timeframes:
                    timeframes.append(tf)
                for name, val in tf_features(candles).items():
                    base[f"{tf}_{name}"] = val
            rows.append(base)

    if not rows:
        print("No snapshot rows found.")
        return

    # Stable column order: fixed fields, then per-timeframe feature blocks.
    cols = [
        "time", "action", "reason", "source", "direction", "entry_low",
        "entry_high", "stop_loss", "tp1", "rr", "market_now", "num_positions",
        "outcome", "profit",
    ]
    for tf in timeframes:
        cols += [f"{tf}_close", f"{tf}_ema", f"{tf}_trend", f"{tf}_range"]

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=cols)
        writer.writeheader()
        for r in rows:
            writer.writerow({c: r.get(c, "") for c in cols})

    executed = sum(1 for r in rows if r["action"] == "EXECUTED")
    labelled = sum(1 for r in rows if r["outcome"] in ("win", "loss", "breakeven"))
    print(f"Wrote {len(rows)} rows to {out_path} "
          f"({executed} executed, {labelled} with a final win/loss outcome).")


if __name__ == "__main__":
    main()
