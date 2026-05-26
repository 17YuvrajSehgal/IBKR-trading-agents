"""
Post-session analyzer for the trading-agents JSONL event streams.

Usage:
    python tools/analyze_session.py logs/multi-*.jsonl logs/trail-*.jsonl logs/news-*.jsonl

What it does
------------
* Reconstructs every position lifecycle by matching `open` with the
  corresponding `close` / `external_close` / `close_done`.
* Cross-references the multi-agent's `close_*` events with the
  trailing-stop's `close_done` events (one position can be closed by
  either side; we attribute realized PnL to whichever fired).
* For each trade, computes:
    - realized PnL
    - hold duration (from open time to close time)
    - max favorable excursion (MFE) — best paper-PnL while position was open
    - max adverse excursion (MAE) — worst paper-PnL while position was open
    - "profit_turned_red" — flag when MFE > 0 but final PnL < 0
* Aggregates by:
    - symbol
    - close reason
    - regime / bias at entry
    - phase at trail close (initial / breakeven / trailing)
* Prints a concise markdown-style report to stdout.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Optional


def parse_ts(s: str) -> datetime:
    """Robust parse of the recorder's ISO timestamps."""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def iter_events(paths: list[Path]) -> Iterator[dict]:
    """Yield every event from every file in chronological order."""
    events: list[tuple[datetime, dict]] = []
    for p in paths:
        if not p.exists():
            print(f"WARN: file not found: {p}", file=sys.stderr)
            continue
        with p.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = rec.get("t")
                if not t:
                    continue
                rec["_t"] = parse_ts(t)
                rec["_src"] = p.name
                events.append((rec["_t"], rec))
    events.sort(key=lambda x: x[0])
    for _, e in events:
        yield e


# ---------------------------------------------------------------------------
# Domain model
# ---------------------------------------------------------------------------

@dataclass
class TrackedTrade:
    """A single position lifecycle reconstructed from events."""

    symbol: str
    side: str                  # ENTER_LONG/ENTER_SHORT or LONG/SHORT
    shares: int
    entry_time: datetime
    entry_price: float
    stop_price: Optional[float] = None
    target_price: Optional[float] = None
    regime: Optional[str] = None
    bias: Optional[str] = None
    adx: Optional[float] = None
    atr: Optional[float] = None
    rsi: Optional[float] = None
    reason_open: Optional[str] = None

    close_time: Optional[datetime] = None
    close_price: Optional[float] = None
    close_reason: Optional[str] = None
    close_source: Optional[str] = None   # multi / trail / external / unknown
    close_category: Optional[str] = None  # for trail: profit/breakeven/loss
    pnl: Optional[float] = None

    # Trail stop trajectory (from `stop_update` events)
    peak_price: Optional[float] = None
    trough_price: Optional[float] = None
    final_stop: Optional[float] = None
    phase_path: list[str] = field(default_factory=list)
    stop_updates: int = 0

    @property
    def long(self) -> bool:
        s = (self.side or "").upper()
        return "LONG" in s or s == "BUY"

    @property
    def held(self) -> Optional[float]:
        if self.close_time is None:
            return None
        return (self.close_time - self.entry_time).total_seconds()

    @property
    def mfe(self) -> Optional[float]:
        """Max favorable excursion in $ (per-trade, signed positive)."""
        if self.peak_price is None or self.trough_price is None:
            return None
        if self.long:
            return max(0.0, (self.peak_price - self.entry_price) * self.shares)
        return max(0.0, (self.entry_price - self.trough_price) * self.shares)

    @property
    def mae(self) -> Optional[float]:
        """Max adverse excursion in $ (per-trade, signed positive)."""
        if self.peak_price is None or self.trough_price is None:
            return None
        if self.long:
            return max(0.0, (self.entry_price - self.trough_price) * self.shares)
        return max(0.0, (self.peak_price - self.entry_price) * self.shares)

    @property
    def turned_red(self) -> bool:
        """True if we had unrealized profit at some point but exited at a loss."""
        if self.pnl is None or self.mfe is None:
            return False
        return self.pnl < 0 and self.mfe > 0

    @property
    def gave_back_pct(self) -> Optional[float]:
        """
        Fraction of peak unrealized profit given back.

        Examples:
          MFE=+$100, PnL=+$100  -> 0.0   (kept all profit)
          MFE=+$100, PnL=+$50   -> 0.5   (kept half)
          MFE=+$100, PnL=  $0   -> 1.0   (gave it all back to flat)
          MFE=+$100, PnL=-$50   -> 1.5   (gave back peak AND $50 more)
          MFE=+$100, PnL=-$200  -> 3.0   (large loss after small peak)

        Values > 1.0 mean the trade became a loss after being in profit.
        """
        if self.mfe is None or self.mfe <= 0 or self.pnl is None:
            return None
        return max(0.0, (self.mfe - self.pnl) / self.mfe)

    @property
    def gave_back_dollars(self) -> Optional[float]:
        """How many $ of peak unrealized profit were lost. Always positive."""
        if self.mfe is None or self.mfe <= 0 or self.pnl is None:
            return None
        return max(0.0, self.mfe - self.pnl)


# ---------------------------------------------------------------------------
# Reconstruction
# ---------------------------------------------------------------------------

def reconstruct(events: Iterator[dict]) -> list[TrackedTrade]:
    """Pair opens with closes and attach the stop-update trajectory."""
    open_trades: dict[str, TrackedTrade] = {}   # symbol -> open trade
    completed: list[TrackedTrade] = []

    for e in events:
        kind = e["kind"]

        # --- OPENS (from regime/multi or news) ---
        if kind == "open":
            sym = e.get("symbol")
            if not sym:
                continue
            t = TrackedTrade(
                symbol=sym,
                side=e.get("side", ""),
                shares=int(e.get("shares") or 0),
                entry_time=e["_t"],
                entry_price=float(e.get("entry") or 0.0),
                stop_price=e.get("stop"),
                target_price=e.get("target"),
                regime=e.get("regime"),
                bias=e.get("bias"),
                adx=e.get("adx"),
                atr=e.get("atr"),
                rsi=e.get("rsi"),
                reason_open=e.get("reason"),
            )
            t.peak_price = t.entry_price
            t.trough_price = t.entry_price
            open_trades[sym] = t

        # --- Trail tracker picked up a position (potentially pre-existing) ---
        elif kind == "track_start":
            sym = e.get("symbol")
            if not sym:
                continue
            # If we already track this from an `open` event, just seed the peak/trough.
            if sym in open_trades:
                t = open_trades[sym]
                t.peak_price = max(t.peak_price or t.entry_price, float(e.get("peak") or t.entry_price))
                t.trough_price = min(t.trough_price or t.entry_price, float(e.get("peak") or t.entry_price))
            else:
                # Pre-existing position picked up by trail at startup
                t = TrackedTrade(
                    symbol=sym,
                    side=str(e.get("side") or ""),
                    shares=int(e.get("shares") or 0),
                    entry_time=e["_t"],
                    entry_price=float(e.get("entry") or 0.0),
                )
                t.peak_price = float(e.get("peak") or t.entry_price)
                t.trough_price = float(e.get("peak") or t.entry_price)
                open_trades[sym] = t

        # --- Trail ratchet updates: track peak/trough ---
        elif kind == "stop_update":
            sym = e.get("symbol")
            if sym not in open_trades:
                continue
            t = open_trades[sym]
            t.stop_updates += 1
            price = float(e.get("price") or 0)
            peak = float(e.get("peak") or price)
            if t.long:
                if peak > (t.peak_price or 0):
                    t.peak_price = peak
                if price and price < (t.trough_price or price):
                    t.trough_price = price
            else:
                if peak and peak < (t.trough_price or peak):
                    t.trough_price = peak
                if price and price > (t.peak_price or price):
                    t.peak_price = price
            t.final_stop = float(e.get("new_stop") or t.final_stop or 0.0)
            phase = e.get("phase")
            if phase and (not t.phase_path or t.phase_path[-1] != phase):
                t.phase_path.append(phase)

        # --- Trail-triggered close ---
        elif kind == "close_done":
            sym = e.get("symbol")
            if sym not in open_trades:
                continue
            t = open_trades.pop(sym)
            t.close_time = e["_t"]
            t.close_price = float(e.get("exit") or 0.0)
            t.close_reason = "trail"
            t.close_source = "trail"
            t.close_category = e.get("category")
            t.pnl = float(e.get("pnl") or 0.0)
            completed.append(t)

        # --- Regime/news agent close ---
        elif kind == "close":
            sym = e.get("symbol")
            if sym not in open_trades:
                continue
            t = open_trades.pop(sym)
            t.close_time = e["_t"]
            t.close_price = float(e.get("exit") or 0.0)
            t.close_reason = e.get("reason")
            t.close_source = "agent"
            t.pnl = float(e.get("pnl") or 0.0)
            completed.append(t)

        # --- External close detected (some other client / manual / flatten) ---
        elif kind in ("external_close", "external_close_detected"):
            sym = e.get("symbol")
            if sym not in open_trades:
                continue
            t = open_trades.pop(sym)
            t.close_time = e["_t"]
            # No PnL in this event — estimate from peak / trough if we have one
            if t.long and t.peak_price is not None:
                t.close_price = t.peak_price
            elif not t.long and t.trough_price is not None:
                t.close_price = t.trough_price
            else:
                t.close_price = t.entry_price
            t.close_reason = "external"
            t.close_source = "external"
            t.pnl = None  # unknown until we look at the broker statement
            completed.append(t)

    # Whatever is still in `open_trades` is open at end of session
    return completed, list(open_trades.values())


# ---------------------------------------------------------------------------
# Aggregations
# ---------------------------------------------------------------------------

def summarize_per_symbol(trades: list[TrackedTrade]) -> list[dict]:
    bysym: dict[str, list[TrackedTrade]] = defaultdict(list)
    for t in trades:
        bysym[t.symbol].append(t)
    out = []
    for sym, ts in bysym.items():
        wins = [t for t in ts if (t.pnl or 0) > 0]
        losses = [t for t in ts if (t.pnl or 0) < 0]
        red = [t for t in ts if t.turned_red]
        total_pnl = sum((t.pnl or 0) for t in ts)
        gross_win = sum(t.pnl for t in wins if t.pnl is not None)
        gross_loss = sum(t.pnl for t in losses if t.pnl is not None)
        out.append({
            "symbol": sym,
            "n": len(ts),
            "wins": len(wins),
            "losses": len(losses),
            "turned_red": len(red),
            "pnl": total_pnl,
            "gross_win": gross_win,
            "gross_loss": gross_loss,
            "avg_win": (gross_win / len(wins)) if wins else 0.0,
            "avg_loss": (gross_loss / len(losses)) if losses else 0.0,
        })
    out.sort(key=lambda r: r["pnl"])
    return out


def summarize_by_close_source(trades: list[TrackedTrade]) -> list[dict]:
    by: dict[tuple[str, str], list[TrackedTrade]] = defaultdict(list)
    for t in trades:
        by[(t.close_source or "?", t.close_category or t.close_reason or "?")].append(t)
    out = []
    for (src, reason), ts in by.items():
        wins = [t for t in ts if (t.pnl or 0) > 0]
        total = sum((t.pnl or 0) for t in ts)
        out.append({
            "close_source": src,
            "reason": reason,
            "n": len(ts),
            "wins": len(wins),
            "win_rate": (len(wins) / len(ts)) if ts else 0.0,
            "total_pnl": total,
            "avg_pnl": total / len(ts) if ts else 0.0,
        })
    out.sort(key=lambda r: r["total_pnl"])
    return out


def summarize_by_regime(trades: list[TrackedTrade]) -> list[dict]:
    by: dict[str, list[TrackedTrade]] = defaultdict(list)
    for t in trades:
        key = f"{t.regime or '?'} / {t.bias or '?'}"
        by[key].append(t)
    out = []
    for key, ts in by.items():
        wins = [t for t in ts if (t.pnl or 0) > 0]
        total = sum((t.pnl or 0) for t in ts)
        out.append({
            "regime_bias": key,
            "n": len(ts),
            "wins": len(wins),
            "win_rate": (len(wins) / len(ts)) if ts else 0.0,
            "total_pnl": total,
        })
    out.sort(key=lambda r: r["total_pnl"])
    return out


def gave_back_stats(trades: list[TrackedTrade]) -> dict:
    gave_backs = [t.gave_back_pct for t in trades if t.gave_back_pct is not None]
    return {
        "trades_with_profit_at_some_point": len(gave_backs),
        "trades_that_turned_red": sum(1 for t in trades if t.turned_red),
        "avg_pct_of_peak_given_back": sum(gave_backs) / len(gave_backs) if gave_backs else 0.0,
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def fmt_money(x: Optional[float]) -> str:
    if x is None:
        return "—"
    sign = "+" if x >= 0 else ""
    return f"{sign}${x:,.2f}"


def fmt_pct(x: Optional[float]) -> str:
    if x is None:
        return "—"
    return f"{x*100:.1f}%"


def fmt_secs(x: Optional[float]) -> str:
    if x is None:
        return "—"
    if x < 60:
        return f"{x:.0f}s"
    if x < 3600:
        return f"{x/60:.1f}m"
    return f"{x/3600:.1f}h"


def print_report(completed: list[TrackedTrade], still_open: list[TrackedTrade]) -> None:
    print()
    print("# Session analysis")
    print()
    print(f"Closed trades : **{len(completed)}**")
    print(f"Still open    : **{len(still_open)}**")
    print()

    # Headline numbers
    pnl_known = [t.pnl for t in completed if t.pnl is not None]
    total_pnl = sum(pnl_known)
    wins = [p for p in pnl_known if p > 0]
    losses = [p for p in pnl_known if p < 0]
    print("## Realized PnL summary")
    print()
    print(f"Total realized PnL (known)   : {fmt_money(total_pnl)}")
    print(f"Wins / Losses / Unknown      : {len(wins)} / {len(losses)} / {len(completed)-len(pnl_known)}")
    if wins:
        print(f"Avg winner                   : {fmt_money(sum(wins)/len(wins))}")
        print(f"Largest winner               : {fmt_money(max(wins))}")
    if losses:
        print(f"Avg loser                    : {fmt_money(sum(losses)/len(losses))}")
        print(f"Largest loser                : {fmt_money(min(losses))}")
    if wins and losses:
        pf = sum(wins) / abs(sum(losses)) if losses else float("inf")
        print(f"Profit factor                : {pf:.2f}")
    print()

    # Profit-turned-red analysis
    gb = gave_back_stats(completed)
    print("## Profit-turned-red analysis (the user's main concern)")
    print()
    print(f"Trades that hit profit at some point   : {gb['trades_with_profit_at_some_point']}")
    print(f"Trades that ENDED in loss after profit : {gb['trades_that_turned_red']}")
    print(f"Average % of peak-profit given back    : {fmt_pct(gb['avg_pct_of_peak_given_back'])}")
    print()

    red_trades = [t for t in completed if t.turned_red]
    if red_trades:
        red_trades.sort(key=lambda t: t.pnl or 0)
        print("Top profit->red individual trades (sorted by realized loss):")
        print()
        print(f"{'Symbol':<6} {'Side':<5} {'Entry':>9} {'Peak':>9} {'Exit':>9} {'Held':>7} {'MFE':>10} {'PnL':>10} {'Gave back':>10}")
        for t in red_trades[:15]:
            print(
                f"{t.symbol:<6} {('L' if t.long else 'S'):<5} "
                f"{(t.entry_price or 0):>9.2f} "
                f"{(t.peak_price or 0):>9.2f} "
                f"{(t.close_price or 0):>9.2f} "
                f"{fmt_secs(t.held):>7} "
                f"{fmt_money(t.mfe):>10} "
                f"{fmt_money(t.pnl):>10} "
                f"{fmt_pct(t.gave_back_pct):>10}"
            )
        print()

    # Per-symbol breakdown
    sym_rows = summarize_per_symbol(completed)
    if sym_rows:
        print("## Per-symbol breakdown")
        print()
        print(f"{'Symbol':<6} {'#':>3} {'W':>3} {'L':>3} {'Red':>4} {'PnL':>11} {'GrossW':>10} {'GrossL':>10}")
        for r in sym_rows:
            print(
                f"{r['symbol']:<6} {r['n']:>3} {r['wins']:>3} {r['losses']:>3} "
                f"{r['turned_red']:>4} "
                f"{fmt_money(r['pnl']):>11} "
                f"{fmt_money(r['gross_win']):>10} "
                f"{fmt_money(r['gross_loss']):>10}"
            )
        print()

    # By close source / reason
    cr_rows = summarize_by_close_source(completed)
    if cr_rows:
        print("## By close source / reason")
        print()
        print(f"{'Source':<10} {'Reason':<28} {'#':>3} {'W':>3} {'Win%':>6} {'Total':>11} {'Avg':>10}")
        for r in cr_rows:
            print(
                f"{r['close_source']:<10} {r['reason'][:28]:<28} {r['n']:>3} "
                f"{r['wins']:>3} {fmt_pct(r['win_rate']):>6} "
                f"{fmt_money(r['total_pnl']):>11} "
                f"{fmt_money(r['avg_pnl']):>10}"
            )
        print()

    # By regime/bias at entry
    reg_rows = summarize_by_regime(completed)
    if reg_rows:
        print("## By regime / bias at entry")
        print()
        print(f"{'Regime/Bias':<28} {'#':>3} {'W':>3} {'Win%':>6} {'Total':>11}")
        for r in reg_rows:
            print(
                f"{r['regime_bias']:<28} {r['n']:>3} {r['wins']:>3} "
                f"{fmt_pct(r['win_rate']):>6} {fmt_money(r['total_pnl']):>11}"
            )
        print()

    # Still-open positions
    if still_open:
        print("## Still open at end of session")
        print()
        for t in still_open:
            mfe = fmt_money(t.mfe) if t.mfe is not None else "—"
            mae = fmt_money(t.mae) if t.mae is not None else "—"
            print(
                f"  {t.symbol:<6} {('L' if t.long else 'S')} {t.shares:>5}  "
                f"entry=${t.entry_price:.2f}  peak=${(t.peak_price or 0):.2f}  "
                f"trough=${(t.trough_price or 0):.2f}  "
                f"MFE={mfe}  MAE={mae}  stop_updates={t.stop_updates}"
            )
        print()

    # Hold duration distribution
    held = [t.held for t in completed if t.held is not None]
    if held:
        held.sort()
        print("## Hold duration (closed trades)")
        print()
        print(f"min / median / p90 / max : "
              f"{fmt_secs(held[0])} / {fmt_secs(held[len(held)//2])} / "
              f"{fmt_secs(held[int(len(held)*0.9)])} / {fmt_secs(held[-1])}")
        print()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("paths", nargs="+", help="One or more JSONL session files")
    p.add_argument("--dump-csv", default=None,
                   help="Optional: write per-trade CSV to this path")
    args = p.parse_args()

    paths = [Path(p) for p in args.paths]
    events = iter_events(paths)
    completed, still_open = reconstruct(events)

    if args.dump_csv:
        import csv
        with open(args.dump_csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow([
                "symbol", "side", "shares", "entry_time", "entry_price",
                "close_time", "close_price", "close_source", "close_reason",
                "close_category", "pnl", "peak", "trough", "mfe", "mae",
                "turned_red", "gave_back_pct", "regime", "bias", "adx", "rsi",
                "held_seconds", "stop_updates",
            ])
            for t in completed:
                w.writerow([
                    t.symbol, t.side, t.shares,
                    t.entry_time.isoformat(), t.entry_price,
                    t.close_time.isoformat() if t.close_time else "",
                    t.close_price or "", t.close_source or "",
                    t.close_reason or "", t.close_category or "",
                    t.pnl if t.pnl is not None else "",
                    t.peak_price or "", t.trough_price or "",
                    t.mfe if t.mfe is not None else "",
                    t.mae if t.mae is not None else "",
                    t.turned_red,
                    t.gave_back_pct if t.gave_back_pct is not None else "",
                    t.regime or "", t.bias or "", t.adx or "", t.rsi or "",
                    t.held or "", t.stop_updates,
                ])
        print(f"Wrote per-trade CSV: {args.dump_csv}", file=sys.stderr)

    print_report(completed, still_open)
    return 0


if __name__ == "__main__":
    sys.exit(main())
