# Research protocol

Backtests and the live paper simulation share one engine, `sim/core.py`. A test runs the
live engine one evening at a time over two years and requires identical trades and equity
to a single backtest over the same dates.

## Fill model

For each trading day D:

0. **Corporate actions:** splits rescale positions and pending orders; cash dividends going
   ex on D are credited for shares held at the previous close.

1. **Open:** orders decided at close(D−1) fill at open(D) plus half-spread and slippage.
   Exits fill before entries. Entries are on-open limit orders at close(D−1) × 1.01: an open
   above the limit cancels the order; otherwise the fill is min(open + costs, limit). An entry
   that opens at or below its stop is stopped out at the open immediately.
2. **Intraday stops:** if open(D) ≤ stop, fill at the open (gap); else if low(D) ≤ stop, fill
   at the stop. Stop fills pay extra slippage.
3. **Close:** mark to market; update peak equity and halts.
4. **Decisions:** the strategy sees bars through D only. Entries are sized from close(D).

Default costs (deliberately pessimistic for SPY/QQQ/IWM): 2 bp half-spread + 3 bp slippage
per fill, +5 bp on stop fills, SEC Section 31 fee and FINRA TAF on sells. Commission per
order is configurable. Currency conversion (CAD to USD) is not modelled; results are in USD.

## Risk rules (shared with the live engine: `engine/risk.py`)

- Shares = floor(equity × 0.5% ÷ (limit − stop + 1% gap buffer)), then capped by remaining
  30% exposure and by cash (never margin), all at the limit price. Max 3 open positions.
- New entries halt at a 3% daily loss or 10% drawdown from peak. Exits always run.
- Live, a drawdown halt stays on until you resume it from the dashboard. Backtests model that
  resume as a peak reset after 20 halted sessions (logged as a `halt_reset` decision), so a
  single breach cannot silently freeze the rest of a run.

## No-leakage protocol

| Step | Window | Allowed |
|---|---|---|
| Parameter search | Train (first 60%) | Yes |
| Choose / compare | Validation (next 20%) | Evaluate only, no search |
| Walk-forward | Train + validation | Re-optimise on past 2y, trade next 6m |
| Final check | Test (last 20%) | Once per strategy spec, logged to `research/test_ledger.jsonl` |

Enforced in code: `grid_search` and `walk_forward` raise `LeakageError` on any overlap with
the test window; `TestVault` refuses a second evaluation of the same spec on any overlapping
window, even with revised data. Spec hashes canonicalise parameters (`50` and `50.0` are the
same spec). The ledger path is absolute and each entry records a data checksum. Calling the
backtester directly on test dates is not technically prevented; reports from the CLI never do.

After the test set is consumed, the only fresh evidence is **forward paper trading**.

## Versioning

Every result carries `name@version#spec_hash`, where the hash covers all parameters. Data
files carry SHA-256 checksums. A report can be regenerated exactly from those two things.

## Look-ahead tests

`tests/test_backtest.py::LookAheadTests` scrambles every bar after a cutoff and asserts all
decisions and equity values up to the cutoff are byte-identical.

## Known simplifications

- Intraday-loss halt evaluated on closing equity (no intraday path in daily bars).
- Missing bar for a held symbol: marked at last close; pending order for it cancelled.
- Positions open at the end of a window are closed at the final close with costs.
- Walk-forward segments start flat (positions are closed at each segment boundary).
