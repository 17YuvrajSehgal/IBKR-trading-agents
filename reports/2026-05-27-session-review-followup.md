# 2026-05-27 follow-up session review (afternoon run)

The afternoon session (13:22 → 15:49) is a smaller, cleaner dataset than the morning run that informed the first report. The earlier circuit-breaker and trail-race fixes (commit `647ece4`) are in place. Two new defects surfaced.

## Numbers

| | Multi (regime) | Trail |
|---|---:|---:|
| Closed | 7 | 14 |
| Realized | +$1,035.78 | +$17.59 |
| Wins / Losses | 1 / 0 | 7 / 5 |

The multi-agent's 6 "$0 PnL" closes are externally-closed positions that the trail flattened — they look win-less only because the multi-agent never recorded the realized PnL.

## Defect 1: W cascade, take two — circuit breaker never tripped

The trail closed W LONG **four times** in INITIAL phase (100, 448, 100, 534 shares), for a cumulative **-$664.83**. The per-symbol circuit breaker should have blacklisted W after the 2nd loss. It didn't.

**Root cause:** the regime agent's `_on_position_event` external-close path cleared local state and released the reservation, but never called `risk.record_close(symbol, pnl)`. So from the multi-agent's RiskManager's perspective, no loss ever happened on W. The trail's RiskManager learned about its own losses but they're in a different process.

**Fix in this commit:**
- Added `_estimate_external_pnl()` to `RegimeAdaptiveAgent` — uses the latest MTF close as exit-price proxy.
- External-close handler now also calls `risk.record_close(symbol, est_pnl)`.
- Same handler added to `NewsDrivenAgent` (which had no positionEvent handler at all before).

Integration-tested: 2 simulated external closes on W trip the breaker, the 3rd entry attempt is rejected with `SYMBOL_BLACKLISTED`.

## Defect 2: Trail's INITIAL stop is too tight for the regime agent's risk model

**100% of trail losses today (5/5) were INITIAL phase**, never reached BREAKEVEN. Trail's `--initial-stop-pct` default is 0.7%. The regime agent's stop is `1.0 × ATR / entry`, which for most large-cap stocks is **~1.0-1.5%**. So the trail's stop is **tighter than the regime's**, meaning the trail is cutting trades before the regime's own stop logic gets a chance to run.

This isn't what the user described as the trail's purpose — they said "a lot of time trades are in big profits but they start sliding into losses." The trail was supposed to **protect profits**, not act as a tighter initial stop.

**Fix in this commit:**
- New `TrailConfig.passive_until_profit` flag (CLI: `--passive-until-profit`).
- When true: the trail doesn't arm any stop in the INITIAL phase. The position's owning strategy manages initial risk. The trail only kicks in once the position reaches `breakeven_trigger_pct` favorable (default 0.5%), at which point it arms BREAKEVEN/TRAILING stops as before.
- Unit-tested across passive LONG and SHORT lifecycles.

## How much these would have saved today

- **W cascade alone**: -$664.83. With breaker tripping after loss #2, you'd save ~$450 of that.
- **All INITIAL-phase trail closes**: -$740.57. Most would have either hit the regime's wider ATR stop OR recovered (impossible to know without re-running). Conservative estimate: ~$300-500 saved by passive mode.
- **Combined**: another ~$750-950 of the daily loss recovered.

## Recommended invocation for next session

```powershell
# Terminal 1: multi-agent with per-symbol breaker
.\.venv\Scripts\python.exe run_multi_agent.py `
    --watchlist watchlist-1.json --category Semiconductors `
    --max-loss-per-symbol 400 --max-consecutive-losses 2

# Terminal 2: trail in passive mode
.\.venv\Scripts\python.exe run_trailing_stop_agent.py --passive-until-profit
```

The combination gives the regime agent room to manage its own risk while the trail handles only the profit-protection job it was designed for.

## Still pending

- Switch regime entries from 5-min to 15-min bars (recommended in the first 2026-05-27 report, not adopted yet).
- Cross-agent fill-based PnL: `ib.execDetailsEvent` fires for all account fills regardless of which client placed the order. Subscribing to that would give us actual fill prices for external closes instead of estimates.
- 8 stuck positions at end of multi-agent session and 7 in trail — neither runner was passed `--flatten-on-stop`, so positions remained for next-session pickup. Confirm next morning that the trail (or a flatten run) cleans them up.
