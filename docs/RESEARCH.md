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

## Day-trading bots: history tests

- Fixed windows: training 2017-08-01 to 2022-12-31, validation 2023 to 2024, final test from
  2025-01-01. Fixed dates mean no day ever moves between windows as history loads.
- The final test is sealed per bot: it is not even run until you open it from the bot's page
  (flag `unlock_test:<bot>`). After that, judge any change to the strategy on new live days.
- Every bot uses the live core with $1,000,000, the same costs (2 bp per side, +3 bp on stops,
  $0.005/share commission with a $1 minimum, SEC/FINRA fees) and limits. A 10% peak halt is
  modelled as resumed after 20 trading days (live, it waits for you).
- Benchmark: buy SPY at the last close before the window and hold it (price only).
- Strategy parameters are round numbers fixed up front; nothing has been tuned.

## November 2026 review of the seven fund bots

All seven day-trading bots were re-run offline on the stored 5-minute history with the live
core, costs and limits (it reproduces the app's own history test to the dollar). The sealed
2025+ window was not loaded. Dollars are on a $1,000,000 account.

| Bot | 2017-2022 after costs | before costs | 2023-2024 after costs | before costs |
|---|---|---|---|---|
| Opening breakout | -163,561 (-3.2%/yr) | +26,678 | -32,776 (-2.0%/yr) | +27,396 |
| Gap-down bounce | -109,258 (-2.1%/yr) | -37,169 | -36,775 (-2.1%/yr) | -7,500 |
| Dip below the day's average | -147,512 (-2.9%/yr) | -9,147 | -41,006 (-2.4%/yr) | +497 |
| Ride the day's trend | -243,921 (-5.0%/yr) | +102,265 | -94,402 (-6.5%/yr) | +2,889 |
| Fair value gap | -144,917 (-2.9%/yr) | -33,805 | -22,172 (-1.3%/yr) | -270 |
| Breakout past normal noise | -41,803 (-0.8%/yr) | +34,470 | -17,005 (-0.9%/yr) | +12,205 |
| Last half-hour momentum | -131,780 (-2.6%/yr) | -37,916 | -37,496 (-2.2%/yr) | -4,228 |

Simply holding over the same windows (price only): SPY +54.6% and +52.5%, QQQ +85.6% and
+90.3%, IWM +22.7% and +25.6%.

What the training window (2017-2022) showed, and what was done about it:

- **No bot makes money after costs, in either window.** Three have no edge even with free
  trading (gap, fair value gap, last half-hour) and one only breaks even before costs (dip).
  The breakout and trend bots make a little before costs, but pay three to seven times that in
  costs: their edge is about 1 to 3 hundredths of a percent per trade against a cost of about 5.
  This is not a parameter problem, so nothing was tuned.
- **IWM loses before costs for six of the seven.** One change was pre-declared from the
  training window, trade SPY and QQQ only, and then checked once on validation:

  | Bot (SPY + QQQ only) | 2017-2022 after costs | 2023-2024 after costs |
  |---|---|---|
  | Breakout past normal noise | +2,541 (+0.05%/yr) | +2,947 (+0.15%/yr) |
  | Opening breakout | -142,177 | -21,777 |
  | Ride the day's trend | -169,502 | -67,892 |
  | all seven together | -643,095 (was -982,752) | -216,594 (was -281,633) |

  Only the noise breakout stops losing, and it only breaks even.
- **Decision.** `noise` keeps trading, on SPY and QQQ. The other six are retired
  (`intraday/live.py: RETIRED`): their accounts and trades are kept and shown, they no longer
  trade. The two ideas with an edge before costs that are not already there (trend, noise)
  were added to the stocks-in-play runner (`sip_trend`, `sip_noise`), where a day's move is
  several times the cost of trading it. Those have no history test; only new days can judge them.
- **Not done on purpose.** No search over parameters: with before-cost edges this close to
  zero, a setting that looks good on 2017-2022 would almost certainly be noise, and every extra
  look at 2023-2024 spends it as a check. The cost model was not loosened either. It is
  deliberately cautious (2 hundredths of a percent a side); real costs on SPY and QQQ are lower,
  which would move these results up a little, not turn them around.

