# Market data: Alpaca free (Basic) plan and its consequences

Verified against Alpaca's documentation on 2026-10-10. Re-verify before relying on it.

| Item | Basic (free) | Effect on this lab |
|---|---|---|
| Real-time equities | IEX exchange only | IEX is roughly 2% of consolidated volume (Alpaca's own AAPL example: 0.92M vs 51.9M shares on one day). IEX-only bars are **not** used for signals. |
| Historical SIP (all exchanges) | Allowed if the request's `end` is at least 15 minutes old | Daily bars evaluated after the close can use full consolidated data for free. The fetcher clamps `end` to now − 16 min. |
| Latest-quote / snapshot SIP endpoints | Require a subscription | Not used. Live sizing uses the last completed daily close, not a real-time quote. |
| History depth | Since 2016 | Backtests start 2016; 2016 is reserved for indicator warm-up, research begins 2017-01-03. Roughly nine years: one major crash (2020), one bear market (2022). |
| Historical API rate limit | 200 calls / minute | Three symbols of daily bars is a handful of calls. Client-side limiter caps at 150/min for headroom. |
| WebSocket | 30 symbols, IEX | Not needed for a daily strategy. |

## Why daily bars

With IEX-only real-time data, intraday signals would be built from ~2% of the tape and could
diverge from the consolidated price. Daily SIP bars, read 20 minutes after the close, are
complete and free. V1 therefore evaluates once per day after the close.

## Execution timing (live paper engine, Phase 3)

1. **After close (D + 20 min):** fetch final SIP daily bars, evaluate strategies, size entries
   from close(D), write decisions and pending order intents to the database.
2. **Next morning (D+1, ~09:00 ET):** re-check pause, halts, calendar (rules vs Alpaca
   `/v2/calendar` must agree), and existing orders; submit **on-open (OPG) limit** orders at
   close(D) × 1.01. Positions are sized as if filled at that limit, so a gap up can never push
   risk or exposure past the limits; an open above the limit simply does not fill. This is
   exactly what the backtest models.
3. **After the entry fills:** place a resting GTC **stop** order at the strategy's stop.
   Trailing-stop updates replace that order after each close. This matches the backtest's
   intraday stop model.

## How data limits affect results

- **Backtests vs paper fills:** paper fills match against the NBBO, and Alpaca randomly
  partially fills ~10% of eligible orders. The paper broker does not model market impact,
  queue position, latency slippage, or dividends. Backtests assume worse-than-real costs.
- **Dividends:** the paper account does not credit dividends, so research uses
  split-adjusted (not dividend-adjusted) prices for both the strategy and its benchmarks.
- **Missing data:** every series is checked against the NYSE calendar. Missing sessions,
  duplicates and impossible OHLC rows are reported and never silently filled. On a live
  session with a missing or stale bar, the engine records a `stale_data` no-trade decision.
- **Revisions:** downloaded data is cached with a SHA-256 checksum and fetch metadata, and
  each report records the checksums it used, so results are reproducible even if Alpaca later
  revises history.
- **Paper account size:** new Alpaca paper accounts default to $100,000. Create a paper
  account with a $10,000 balance (the balance is chosen at creation). The engine also sizes
  against `CAPITAL_BASE_USD` regardless of the broker balance.

Sources: [About Market Data API](https://docs.alpaca.markets/docs/about-market-data-api),
[Market Data FAQ](https://docs.alpaca.markets/us/docs/market-data-faq),
[Paper Trading](https://docs.alpaca.markets/docs/paper-trading),
[TradingClient reference](https://alpaca.markets/sdks/python/api_reference/trading/trading-client.html).
