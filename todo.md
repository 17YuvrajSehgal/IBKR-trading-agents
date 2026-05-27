# TODO — open work and things to test

Living document. As you run sessions and find new issues, add them here so we can pick them up later.

> **Reminder:** the entire codebase is paper-only by design — runners refuse
> live ports, RiskManager has hard daily-loss caps. Don't change those without
> a separate, deliberate discussion.

---

## Verified working

These have been smoke-tested on the live paper TWS and confirmed end-to-end:

- **`ibkr/` package** — connection, account summary, positions (via `reqPositionsAsync`), L1 market data, order place/cancel, risk gating, error classification, session recording (JSONL).
- **TQQQ/SQQQ pairs agent** — full lifecycle.
- **Regime-adaptive single-symbol agent (`run_regime_agent.py`)** — TSLA warmup + decision pipeline.
- **Multi-symbol regime runner (`run_multi_agent.py`)** — 248-symbol watchlist warmed up in ~11 min; 11 simultaneous entries on a 5-min bar; risk-manager race fix verified blocks subsequent entries.
- **Flatten utility (`flatten_positions.py`)** — cancels working orders first, then closes all positions with fill waiting.
- **News-driven agent (`run_news_agent.py`)** — subscription + headline classification pipeline confirmed; observe mode confirmed.
- **Trailing-stop agent (`run_trailing_stop_agent.py`)** — picked up 9 pre-existing positions, immediately shifted SPOT and LMND into BREAKEVEN/TRAILING phase with stops above entry. Ratchet logic unit-tested across LONG profit, LONG loss, and SHORT profit scenarios.
- **External-close detection in regime agent** — `positionEvent` handler clears internal state when another agent (trailing-stop, manual TWS, flatten utility) closes a tracked position, so the regime agent can re-enter on the next signal.
- **Cross-client close coordination** — every close path (regime, news, trailing) now does two checks before submitting: (1) a local `closing` flag prevents intra-agent double-fires from queued evaluations; (2) `OrderManager.has_working_order()` uses `reqAllOpenOrdersAsync` to see if any OTHER client in the same account has a working order on the same symbol/side and skips if so. This prevents the regime agent and trailing-stop agent (running in separate processes) from both closing the same position and ending up with an opposite-side short/long over-shoot.
- **Trail positionEvent race fix** — synchronous `_tracking_in_flight` set added in `TrailingStopAgent._on_position_event` so a cascade of partial-fill positionEvents for the same symbol can't schedule multiple `_begin_tracking` coroutines. Discovered after the 2026-05-27 DBX cascade (-$3,640 from 8 phantom tracking entries).
- **Per-symbol circuit breaker** — `RiskLimits.max_consecutive_losses_per_symbol` and `max_loss_per_symbol_per_session` blacklist any symbol after N consecutive losses OR cumulative session loss exceeds the threshold. New `RiskManager.record_close(symbol, pnl)` (called by all three agents on every close) updates per-symbol PnL + consecutive-loss counts and trips the blacklist. Blacklisted symbols are denied with `SYMBOL_BLACKLISTED` for the rest of the session. Tested standalone; verified that 2 consecutive losses on a symbol trip the breaker while other symbols still trade normally.
- **External-close feeds circuit breaker** — when the trailing-stop (or any other client) flattens a tracked position, both the regime agent's and the news agent's `_on_position_event` now estimate realized PnL from the latest bar/quote vs. entry, then call `risk.record_close(symbol, est_pnl)`. Without this, the multi-agent's circuit breaker would never see losses from externally-closed positions and would keep re-entering losers — exactly what happened with W on 2026-05-27 (4 consecutive INITIAL-phase trail closes for -$664.83 with no blacklist trip). Integration-tested: 2 external closes trip the breaker; new orders on the same symbol get rejected as `SYMBOL_BLACKLISTED`.
- **Passive-until-profit trailing stop** — `TrailConfig.passive_until_profit` (CLI: `--passive-until-profit`) makes the trail leave the INITIAL phase un-armed, only kicking in once the position has moved `breakeven_trigger_pct` favorable. Lets the strategy agent's own ATR-based stop manage initial risk; the trail's job becomes pure profit protection. Discovered after 2026-05-27: the trail's tight 0.7% INITIAL stop cut 5 regime-agent trades (W ×4, TTD ×1) before the regime's wider ATR stop could fire. Unit-tested: passive mode survives -0.7% drops, arms after +0.5% favorable, closes on pullback.

---

## Needs real-world validation

These work in code but haven't been observed under the conditions they're designed to handle.

### 1. Daily reset / auto-reconnect

- `connection.py` now fires `on_reconnect` callbacks after a successful reconnect, but **no agent currently registers a handler**. After the nightly server reset (≈ 23:30–00:30 ET) the historical bar subscriptions in `RegimeAdaptiveAgent` will be stale.
- **Test plan:** leave the multi-agent runner active across midnight ET on a paper session; verify the agents either resubscribe automatically or log a clean error rather than hanging.
- **Likely fix:** have `RegimeAdaptiveAgent` register `conn.on_reconnect(self._resubscribe_bars)` in `start()`; that method should cancel the dead subscriptions and call `reqHistoricalDataAsync` again with `keepUpToDate=True`.

### 2. News agent against actually-actionable headlines

- Smoke test only saw generic Dow Jones topic headlines (CFA newsletters, SpaceX/WSJ explainers). None matched the lexicon.
- **Test plan:** run news agent during a US earnings session (e.g., post-close 16:00–17:30 ET when AMD/NVDA/CRM etc. report), or during macro events (Fed days, CPI release). Use `--observe` so nothing trades. Inspect the JSONL for `score` events that exceeded the threshold and would-be trades.
- **Likely follow-ups depending on data:**
  - New phrases to add to `classifier.py` if real headlines use templates the lexicon doesn't cover.
  - Adjust `--threshold` if the bar is too high/low.
  - Adjust provider weights — DJNL might deserve more, BZ might deserve less.

### 3. Order partial-fill behavior on close

- During the first 248-symbol session, ROKU got a partial 124/194 close fill; the remaining 70 only filled on a later flatten pass, leading to an over-shoot that ended SHORT 70.
- `flatten_positions.py` now cancels working orders first to prevent recurrence, but the **agent's own `_close_position` doesn't cancel its prior close order** if a re-shutdown happens. Edge case but worth fixing.
- **Test plan:** simulate by calling stop() twice rapidly or by Ctrl-C'ing during a forced flatten.

### 4. Run with `--passive-until-profit` end-to-end (2026-05-28+)

- Today's pattern was: regime opens W LONG → trail kills at -0.7% → regime re-opens W LONG (with new size) → trail kills again. 4 W rounds for -$664.83. The new flag should stop that cycle (regime stop fires before trail does), but unit-test only — no live session yet.
- **Test plan:** next paper session, run
  `run_trailing_stop_agent.py --passive-until-profit`
  in parallel with `run_multi_agent.py`. Inspect JSONL for INITIAL-phase exits — there should be zero now. Profit-trailing exits should look identical to before.

### 5. External-close PnL estimate accuracy

- The estimate uses the latest bar/quote close, not the actual fill. If the trail fills 0.1-0.3% away from the bar close, our PnL estimate diverges from broker reality. The circuit breaker treats it as ground truth.
- **Effect:** worst case, breaker trips one trade too early or one trade too late. Not catastrophic, but worth instrumenting.
- **Better fix later:** subscribe to `ib.execDetailsEvent` (which fires for ALL account fills, including other clients on the same account) and read the actual fill price from the `Execution` object.

### 6. BRK B and XYZ resolution

- We replaced `BRK.B → BRK B` and `SQ → XYZ` in the watchlist but haven't actually run a session that includes them and confirms they qualify.
- **Test plan:** `flatten_positions.py --dry-run` after `run_multi_agent.py --watchlist watchlist-1.json --category "Payment Processors"` and another with `Banks & Financials`. Watch for error 200 on either.

---

## Known limitations

These are intentional design choices for v1. Document, don't necessarily fix.

### Race vs. limit consistency

The atomic check-and-reserve in `RiskManager.check_order` prevents N parallel agents from busting the cap, but **prices used for reservation are check-time estimates** (mid quote or last bar close), not fill prices. If a fill drifts materially from the reservation price, the running `_notional` total will be slightly off until the position is released. Acceptable for paper; for live trading you'd want a fill-time reconciliation step.

### Topic-stream headlines lose symbol tagging

When IBKR sends headlines via topic feeds (`DJ-RTG`, `DJ-RTA`, etc.) without a symbol tag, `NewsManager`'s fallback only attributes if there's exactly one subscribed symbol. With multiple subscribed symbols those headlines are recorded with `symbol=None` and the news agent can't act on them.

**Possible v2:** scan headline text for ticker mentions (`$NVDA`, `(NASDAQ:NVDA)`, `Nvidia Corp`) and re-tag. Requires a symbol-to-name dictionary.

### IBKR pacing on large watchlists

A full 248-symbol warmup hits IBKR's "60 historical requests per 10 min" limit area; we sequence with `--warmup-delay 1.5` and saw ~11-minute warmups without errors. If pacing violations appear (error code 162):
- Raise `--warmup-delay` to 2.5–3.0
- Use `--category` to narrow the universe
- Use `--max-symbols N` to truncate

### Duplicate `reqMktData` cleanup warnings in news agent

`NewsManager` and `MarketDataManager` both call `reqMktData` for the same contract via different code paths. On shutdown, the first cancel removes the subscription record and the second logs `cancelMktData: No reqId found`. Cosmetic only — cleanup continues fine.

**Fix idea:** consolidate to a single subscription per contract that yields both market data and news. Requires refactoring `NewsManager` to not pass `mdoff` and instead share the ticker with `MarketDataManager`.

---

## Open improvements (nice-to-have)

### A. LLM-augmented news sentiment

- Plumb in an optional Claude API call when the keyword lexicon scores above some "maybe interesting" threshold. Use the API call to:
  1. Confirm sentiment direction
  2. Extract structured fields (event type, magnitude in %, surprise vs expectation)
  3. Reject false positives (e.g., "Apple recall" referring to a 2008 incident in a retrospective)
- Cost: ~$0.001 per call, ~500ms latency.
- File to add: `strategies/news_driven/llm_confirm.py` plus an `--llm-confirm` flag on the runner.

### B. Topic / macro news subscription

The IBKR API announcement noted that you can request topic news by using the provider code as the exchange and `*` as the symbol. Build a "macro news" agent that watches general Dow Jones / Reuters feeds for Fed/CPI/geopolitical headlines and trades broad-market ETFs (SPY/QQQ/IWM/VIX-related).

### C. Backtest framework

Currently we can only validate strategies live. Add an offline replay runner that:
- Reads historical bars from a stored Parquet or CSV file
- Feeds them through `RegimeDetector` + `SignalGenerator` synchronously
- Computes hypothetical PnL with the same stop/target/time-stop logic
- Outputs the same JSONL events for comparison with live runs

Without this, parameter tuning is observation-only.

### D. Per-symbol parameter overrides

`AgentConfig` currently applies the same `stop_atr`, `target_atr`, `risk_pct` etc. to every symbol. NVDA's volatility profile is very different from MDLZ's. A simple JSON of overrides — keyed by symbol — would let you say "NVDA gets 2.0×ATR stop, MDLZ gets 1.0×ATR stop".

### E. Reconnect-aware bar subscription

Already discussed in "Needs real-world validation #1". The hook is in place; the implementation isn't.

### F. Configurable news provider weights

Provider weights in `classifier.py` are hard-coded. Move them to a JSON config so you can tune without editing code (e.g., bump DJNL to 1.2 after observing it leads BRFG by 30+ seconds).

---

## Open design questions

When you have a chance, decide and we'll implement:

1. **News + regime combo strategy?** Run regime-adaptive on a watchlist, but accept overriding signals from the news agent. E.g., regime says HOLD but news says "FDA approved" — should we trade?
2. **Should observe-mode also disable subscriptions for symbols we don't trade?** Currently it subscribes to news + market data regardless. For pure observation we could subscribe to news only (saves market-data lines).
3. **Risk pool sharing across runners?** Today each runner has its own RiskManager. If you run regime AND news in parallel, each has its own $10k daily-loss cap. Total exposure could be $20k. Probably fine for paper; for live we'd want a shared persistent state.
4. **Switch the news agent's exits to ATR-based?** Currently it uses fixed % (0.7%/1.4%). Most volatile names need wider stops than low-vol names. Easy to switch to per-symbol ATR via the existing indicators module.

---

## Quick reference — how to share data with me for analysis

When something looks wrong, or you want me to dig into a session:

```powershell
# Find the session you want to share
Get-ChildItem logs\

# Send me both files — text log for context, JSONL for structured analysis
# logs\multi-2026-05-26T11-08-00.log
# logs\multi-2026-05-26T11-08-00.jsonl
```

The JSONL is the more useful one. Events have well-defined `kind` discriminators (`headline`, `score`, `open`, `close`, `risk_reject`, `session_summary`, etc.) so I can replay your session and analyze specific trades without needing to re-derive context from the text log.
