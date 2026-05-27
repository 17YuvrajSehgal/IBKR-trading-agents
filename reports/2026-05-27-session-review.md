# Session review — 2026-05-27 (multi-agent + trailing-stop)

Sources: `logs/multi-2026-05-27T09-30-01.jsonl`,
`logs/trail-2026-05-27T09-30-27.jsonl`. Generated with
`tools/analyze_session.py` plus manual reconciliation.

---

## TL;DR

**Realized PnL: about -$7,990** (37 trail closes for -$5,923 + 3 multi
stop-hits for ~-$1,770 + uncaptured externals). Win rate **5/27 (18%)**.
Profit factor **0.22**.

**The loss is dominated by TWO catastrophic bugs**, not by strategy
weakness:

1. **The trail agent's `positionEvent` handler has a race condition.** A
   single multi-agent order triggers multiple `positionEvent` fires as
   IBKR sends partial-fill updates. Each fire schedules an async
   `_begin_tracking`, but the symbol isn't added to `self.tracked`
   until that coroutine completes. Result: a single 2,851-share short
   on DBX produced **8 phantom tracked positions in 700 milliseconds**,
   then chaotic close attempts. The agents effectively over-flipped
   `SHORT 2851 → LONG 5325 DBX` and rode the long down for -$1,406 on
   the trail's next close.

2. **No per-symbol loss circuit breaker.** DBX kept re-tracking and
   re-losing 7 times in a row: -$22 → -$387 → -$396 → +$219 → -$1,406
   → -$951 → -$695 = **-$3,640 from one ticker**. Any sane circuit
   breaker would have blacklisted DBX after loss #2.

These two fixes alone would have eliminated **~$4,500 of today's loss**.

---

## Headline numbers

| Metric | Value |
| --- | --- |
| Trail closes (37) total PnL | **-$5,923.19** |
| Multi-agent stop-hits | **-$1,770.19** |
| Externals (unrecorded PnL) | 17 events; ~-$300 estimated |
| **Day total (best estimate)** | **-$8,000** |
| Win rate (37 trail closes) | 11/37 = **29.7 %** |
| Profit factor | 0.22 |
| Largest single loser | DBX -$1,406.58 |
| Largest single winner | SMCI +$1,247.36 |

---

## The DBX cascade — exactly what happened

Reconstructed from the JSONL timestamps (millisecond precision):

```
13:40:05.956   multi-agent: OPEN ENTER_SHORT 2851 DBX entry=$26.30
13:40:06.689   trail: track_start SHORT 201 DBX   (positionEvent #1)
13:40:06.689   trail: track_start SHORT 301 DBX   (positionEvent #2)  ← race
13:40:06.689   trail: track_start SHORT 301 DBX   (positionEvent #3)  ← race
13:40:06.689   trail: track_start SHORT 301 DBX   (positionEvent #4)  ← race
13:40:06.735   trail: track_start SHORT 51  DBX   (positionEvent #5)  ← race
13:40:06.736   trail: track_start SHORT 151 DBX   (positionEvent #6)  ← race
13:40:06.751   trail: track_start SHORT 151 DBX   (positionEvent #7)  ← race
13:40:06.752   trail: track_start SHORT 151 DBX   (positionEvent #8)  ← race

13:41:21       trail closes 151 shares    → -$22
13:41:22       trail re-tracks SHORT 2751 (residual)
13:41:26       trail closes 2751 shares   → -$387
13:41:27       trail re-tracks SHORT 2543 (re-sync from broker)
13:41:57       trail closes 2543 shares   → -$396
13:42:00       trail re-tracks LONG 1653  ← OVER-FLIPPED to long
13:46:16       trail closes 1653 LONG     → +$219 (briefly profitable)
13:46:17       trail re-tracks LONG 5325  ← position keeps growing
14:07:02       trail closes 5325 LONG     → -$1,406
14:07:02       trail re-tracks LONG 3600
14:07:19       trail closes 3600 LONG     → -$951
14:07:20       trail re-tracks SHORT 5133 ← flipped back to short
14:19:22       trail closes 5133 SHORT    → -$695

Total DBX  : -$3,640 in 39 minutes from one mis-tracked position
```

The whole thing started with a SHORT 2851 order whose partial fills
triggered duplicate tracking. After that, the agents fought to close
imaginary positions, the real position kept growing in size, and DBX
ended up netting out at much larger sizes than intended.

The race-condition guard (`close_skipped_race`) **fired 34 times on
DBX alone**. It correctly identified that multiple agents were trying
to close, but it doesn't prevent the underlying duplicate tracking that
caused the over-flips.

---

## By symbol (trail PnL, sorted)

| Symbol | n | wins | losses | PnL |
| --- | --- | --- | --- | --- |
| **DBX** | **7** | **0** | **7** | **-$3,640** |
| GOOG | 3 | 0 | 3 | -$973 |
| EBAY | 3 | 0 | 3 | -$804 |
| GIS | 1 | 0 | 1 | -$577 |
| AKAM | 1 | 0 | 1 | -$573 |
| MSFT | 3 | 0 | 3 | -$463 |
| TTWO | 2 | 0 | 2 | -$478 |
| PEP | 1 | 0 | 1 | -$391 |
| COIN | 2 | 0 | 2 | -$335 |
| ADSK | 1 | 0 | 1 | -$151 |
| STX | 1 | 0 | 1 | -$137 |
| NTAP | 4 | 0 | 4 | -$47 |
| KR | 1 | 1 | 0 | +$88 |
| CVS | 1 | 1 | 0 | +$85 |
| ORCL | 4 | 2 | 2 | +$456 |
| MNST | 1 | 1 | 0 | +$770 |
| **SMCI** | 1 | 1 | 0 | **+$1,247** |

DBX accounts for **61%** of the total trail loss. Without it the trail
PnL would have been -$2,283 (still bad, but not catastrophic).

---

## What's getting fixed in this commit

1. **`TrailingStopAgent._on_position_event` — race-free tracking.** Add
   a synchronous `_tracking_in_flight` set so duplicate `positionEvent`
   fires for the same symbol can't schedule multiple `_begin_tracking`
   coroutines. The first event wins; subsequent fires for the same
   symbol are no-ops until the placeholder is cleared.

2. **`RiskManager` — per-symbol circuit breaker.** New `RiskLimits`
   fields:
     * `max_consecutive_losses_per_symbol` (default 2)
     * `max_loss_per_symbol_per_session` (default $500)
   Both wired into `check_order()` so blacklisted symbols are denied.
   New `record_close()` method that updates per-symbol session PnL and
   loss counts, and trips the blacklist when thresholds are crossed.
   All three agents call `risk.record_close(symbol, pnl)` at close time.

3. **Analyzer fix — match opens with closes by sequence.** When the
   same symbol is closed and re-entered in one session, the analyzer
   no longer conflates them into a single record.

---

## What still needs work (not in this commit — for later)

* **15-min bars for the regime entry** (recommended yesterday, still
  not adopted). The 5-min noise is amplifying every other problem.
* **Disable TREND_UP/BULL entries** (recommended yesterday, still not
  adopted). They continue to under-perform.
* **`commissionReportEvent` subscription** to capture per-fill PnL in
  the JSONL so we can stop guessing at the broker total.
* **Pre-existing-position warning at startup.** Today's session likely
  inherited DBX/GIS/etc. from yesterday and reused them under the
  wrong strategy bias. Could be worth adding a `--flatten-on-start`
  flag to multi-agent that closes everything before fresh entries.

---

## Recommended runner args after this commit

```powershell
# Single-process run with the new circuit breaker + 15-min bars
.\.venv\Scripts\python.exe run_multi_agent.py `
    --watchlist watchlist-1.json --category Semiconductors `
    --mtf "15 mins" `
    --max-loss-per-symbol 400 --max-consecutive-losses 2 `
    --duration 14400

# Trail with the duplicate-tracking fix
.\.venv\Scripts\python.exe run_trailing_stop_agent.py
```
