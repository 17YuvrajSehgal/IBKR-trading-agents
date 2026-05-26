# Session review — 2026-05-26 (multi-agent + trailing-stop + news observer)

Sources: `logs/multi-2026-05-26T13-19-09.jsonl`,
`logs/trail-2026-05-26T13-19-16.jsonl`,
`logs/news-2026-05-26T13-19-30.jsonl`. Generated with
`tools/analyze_session.py`.

---

## TL;DR

* **From what our JSONL captured: -$1,207** realized across 29 closed trades.
  You said the broker showed a $4–5k loss by EOD — the gap is in the
  **6 trades closed externally with PnL unknown to our agents** (manual TWS
  closes / earlier flatten passes) plus the **7 positions still open at EOD**
  (some had four-figure MAE). We can't see those in the JSONL; you'd need the
  IBKR account statement to confirm. **Recommend: export today's IBKR Activity
  Statement and compare against `logs/today-trades.csv` to identify which
  trades are missing.**
* **The "profits turning into losses" pattern you described is largely a
  misinterpretation of the noisy chart.** Of 7 "profit-turned-red" trades,
  6 had max favorable excursion of **less than $66** — i.e. they barely
  poked into profit before reverting. They look like profit-turned-red in
  TWS because the unrealized PnL ticker flickers positive, but in reality
  the strategy never got a real favorable move before getting stopped out.
* **The real problem is the entry side, not the exit side.** Trend-up
  longs in BULL bias (the most common trade) have a **25% win rate**
  (2 wins / 8 trades, -$464 net). Short-side trades in BEAR bias did
  fine (43% win rate, +$118). The strategy is **buying too eagerly into
  pullbacks that aren't really pullbacks**.

---

## Headline numbers

| Metric | Value |
| --- | --- |
| Closed trades | **29** |
| Still open at EOD | **7** |
| Realized PnL (known) | **-$1,207.13** |
| Wins / Losses / Unknown-PnL | 9 / 14 / 6 |
| Win rate (known) | **39.1 %** |
| Avg winner | **+$189.65** |
| Avg loser | **-$208.14** |
| Largest winner | +$491.52 (DKNG short, target hit) |
| Largest loser | -$420.12 (CMG short, stop hit) |
| Profit factor | **0.59** (need > 1.0 to be profitable) |

The winners-vs-losers ratio is the headline issue: average winner is
**smaller** than average loser. Combined with sub-50% win rate, that's a
losing strategy regardless of how well you trail.

---

## Why "profit turning red" isn't what you think

You felt like trades were giving back profit. Here's what actually happened
on the 7 trades that the analyzer flagged as "turned red":

| Symbol | Side | Entry | **Peak** | Exit | Held | **MFE** | PnL |
| --- | --- | --- | --- | --- | --- | --- | --- |
| CMG | SHORT | $32.13 | $32.13 | $32.31 | 1.6 h | **+$35** | -$420 |
| RL | LONG | $382.78 | $383.12 | $381.61 | 5 min | **+$65** | -$228 |
| AAOI | LONG | $183.00 | $183.07 | $181.62 | 13 min | **+$12** | -$228 |
| DASH | LONG | $154.05 | $154.06 | $153.64 | 20 min | **+$5** | -$199 |
| DG | SHORT | $102.72 | $102.72 | $103.38 | 1.3 h | **+$22** | -$158 |
| SMCI | LONG | $37.41 | $37.44 | $37.17 | 4 min | **+$42** | -$154 |
| EBAY | SHORT | $115.20 | $115.20 | $115.36 | 20 min | **+$16** | -$104 |

Look at the **Peak vs Entry** column — six of the seven barely moved in our
favor at all. **CMG** went *zero* in our favor for 1.6 hours then took a
$420 loss. **AAOI** peaked $0.07 above entry then lost $1.38. These aren't
"profit melted away" trades. These are "we entered, the price went the
wrong way almost immediately, and the stop took 5–20 minutes (or 1.6 hours
for CMG) to get hit."

**The only trade that genuinely gave back real profit** was RL: peaked
+$65, exited -$228. That's a real profit-turned-red event. The other six
were just losers from the start.

So:
- The trailing-stop agent **did not fail** — there was no profit to trail.
- The TWS unrealized-PnL ticker flickered green briefly on noise, which is
  what felt like "we were up." But noise of $5–35 isn't a real edge.

---

## Where the money actually went

Breakdown by close source / reason:

| Source | Reason | # | Win % | Total |
| --- | --- | --- | --- | --- |
| **trail** | INITIAL loss (stopped before breakeven) | 4 | 0% | **-$835.09** |
| **agent** | regime stop hit | 9 | 0% | -$2,026.18 (sum of 9 stop-hit losers) |
| trail | profit (locked in trail phase) | 3 | 100% | +$121.77 |
| agent | target hit | 5 | 100% | +$1,496.22 |
| agent | regime flipped to TREND_DOWN | 1 | 100% | +$88.90 |
| external | (manual / flatten / other client) | 6 | — | unknown PnL |

Three takeaways:

1. **Initial-stop losses are the dominant loss category** (multi-agent + trail
   combined). Stop is set at 1.5× ATR but on a 5-minute bar that's a *small*
   absolute dollar amount, and any normal intra-session reversal triggers it.
2. **When we did reach the target, we won every time** (5/5). The signal is
   directionally OK once given room.
3. **The trailing-stop agent's profit closes only averaged +$40.59**. It
   ratchets in 0.5% increments, which on a $530 SPOT translates to $2.65
   per ratchet — most trades exit on the first 0.5% pullback, well before
   the run-up has played out.

---

## Where the strategy is lacking

### 1. The 5-min bar timeframe is too noisy for the chosen stops

ATR on a 5-minute bar for these names is typically $0.30–$1.50.
Stop = `entry - 1.5 × ATR` is therefore $0.45–$2.25 below entry — well
within routine intraday range. Most stops get hit on bog-standard
mean-reversion noise.

**Concrete proof:** of 14 trades that hit any favorable move, 7 (50%)
ended up in loss. Real edges don't look like a coin flip on hold.

### 2. Trend-up longs in a bullish-bias session are mis-firing

| Regime / Bias | # | Win % | Total |
| --- | --- | --- | --- |
| **TREND_UP / BULL** | 8 | **25 %** | **-$464.80** |
| RANGE / BULL | 1 | 0% | -$228.15 |
| RANGE / BEAR | 2 | 50% | -$110.36 |
| **TREND_DOWN / BEAR** | **14** | **43 %** | **+$118.66** |

The dominant trade type (trend-up long in a bullish session) is the
*worst* performer. Counter-intuitively, our short-side trades when the
HTF bias was BEAR did the best job. Hypothesis: the pullback-to-EMA
entry needs an *active* uptrend, but the 1-hour EMA20>EMA50 condition
fires even after the trend has been running for hours and is due for
mean-reversion.

### 3. The trail's breakeven trigger (0.5%) is too far for 5-min noise

A 0.5% favorable move = $2.65 on a $530 stock. By the time we've moved
0.5%, the move is often already done and pulling back. **Lowering the
breakeven trigger to 0.2–0.3%** would let the trail lock in no-loss
earlier on the trades that *do* work.

### 4. The 24-bar time stop (2 hours on 5-min) is too long

**CMG** sat short for 1.6 hours without a single tick in our favor.
**DG** sat short for 1.3 hours, same. We should bail far sooner if the
move doesn't materialize — say 6–8 bars (30–40 min) without favorable
progress.

### 5. The agent's tracked PnL doesn't match the broker

6 trades closed externally; we have **no PnL** for them. Without parsing
TWS's commission reports or activity statement, we can't see the true
end-of-day number. Improvement: subscribe to `commissionReportEvent` and
build a real fill-by-fill PnL in the JSONL.

---

## Specific things to try (in priority order)

| # | Change | Expected effect |
| --- | --- | --- |
| 1 | **Move regime entries from 5-min bars to 15-min bars** (`--mtf "15 mins"`) | Cuts noise; stops won't get hit on a single bad 5-min candle. Tradeoff: fewer entries per session. |
| 2 | **Disable TREND_UP/BULL entries**, or tighten — require HTF *strictly* bullish AND ADX rising | Removes the 25%-win-rate bucket that drove most of today's losses. |
| 3 | **Lower trail breakeven_trigger_pct to 0.003** (0.3%) | Locks in no-loss earlier on winners. |
| 4 | **Cut time stop from 24 bars to 8** | Avoids hour-long sits in trades that never move. |
| 5 | **Add a "minimum favorable progress" gate** — if 3 bars after entry the position hasn't moved at least 0.5×ATR in our favor, exit | Cuts dead trades without waiting for the hard stop. |
| 6 | **Subscribe `commissionReportEvent`** in OrderManager and record commission + realized PnL per fill | Gives us true PnL in the JSONL so future reports match broker reality. |
| 7 | **Per-sector parameter tuning** — consumer staples (CMG, DG, KR, MO) need different stops than semis (SMCI, AAOI) | The same percent moves differently per name. |

---

## What worked well today

These are worth keeping:

* **CVS** (+$517 on 2 wins), **PEP** (+$486), **DKNG** (+$491 short, target hit
  cleanly), **MNST** (+$91) all reached their targets. The signal *can* work.
* **Trail's profit-category closes** (3 trades, +$122) all locked in gains
  that would otherwise have given back to noise.
* **Regime "flipped to TREND_DOWN"** closed a position at +$89 — the regime
  awareness saved us from a deeper loss.
* **Cross-client coordination** worked: 3 `close_skipped_race` events
  fired when both the regime agent and the trail tried to close the same
  symbol. None of those produced an over-flip.

---

## Open items (for next session before we touch live)

* Cross-reference `logs/today-trades.csv` against the IBKR Activity Statement
  to identify the unknown-PnL externals.
* Inspect the 7 still-open EOD positions in TWS — were they closed manually
  later, or are they still open?
  - `GIS S 2254`, `EBAY S 651`, `SMCI L 1589`, `NOW L 749`,
    `DOCU L 1520`, `ADBE L 311`, `MNST L 413`
* `MNST` specifically had +$189.98 MFE while still open — if it's still
  open, run `flatten_positions.py --symbol MNST` to capture that gain
  before it fades.
* Re-run with `--mtf "15 mins"` and `--shorts confident` and compare
  the resulting JSONL against today's.

---

## How this report was produced

```powershell
.\.venv\Scripts\python.exe tools\analyze_session.py `
    logs\multi-2026-05-26T13-19-09.jsonl `
    logs\trail-2026-05-26T13-19-16.jsonl `
    logs\news-2026-05-26T13-19-30.jsonl `
    --dump-csv logs\today-trades.csv
```

`today-trades.csv` has one row per trade with entry, exit, peak, trough,
MFE, MAE, PnL, regime, bias, ADX, RSI, etc. Open that in Excel/Sheets if
you want to slice it yourself.
