# Market data: provider choice and its consequences

Checked 2026-10-10. Re-verify pricing and terms before relying on them.

## Choice: Tiingo (free plan)

| Need | Tiingo free plan |
|---|---|
| SPY, QQQ, IWM daily bars from 2016 | Yes: 30+ years of EOD history, ETFs included |
| Raw prices + corporate actions | Raw OHLCV plus `divCash` and `splitFactor` per day (also CRSP-adjusted fields) |
| Daily update | US EOD published ~5:30 pm ET; corrections until ~8 pm ET |
| Limits | 50 requests/hour, 1,000/day, 500 unique symbols/month. The engine uses 3 requests/day |
| Licence | Individual plans are "internal use only": personal use, not displayed or shared with others. A private single-user dashboard fits |
| Canada | No country restriction stated |
| Cost | $0 (Power plan $30/month if limits ever matter) |

**Open item:** the full Tiingo Terms of Use page could not be loaded during research
(pricing and documentation pages were). Read it when creating the account. If it conflicts
with storing data in your own database, switch to Twelve Data's Grow plan ($29/month),
whose terms explicitly allow storing data for internal use; only `marketdata/` changes.

### Alternatives considered

| Provider | Why not chosen |
|---|---|
| Twelve Data | Free tier is "internal non-display" only; Grow plan $29/month for dashboard use. Good fallback. |
| Massive (formerly Polygon) | Consolidated-tape quality, but 2016 history needs $79/month+, and its market-data terms restrict non-display use. |
| EODHD | $19.99/month; prices are aggregated from non-exchange sources (its own disclaimer). |
| Alpha Vantage | Adjusted full history is premium ($49.99/month). |
| FMP | Terms forbid downloading/copying content without written approval. |
| Marketstack | Free tier 100 requests/month and 1 year of history. |
| Yahoo Finance / yfinance | Terms prohibit automated access; frequent rate-limit bans. |
| Stooq | Unofficial; API-key change and outages in 2026; terms unreadable. |
| Alpaca data | Market-data subscriptions currently unavailable to Canadian residents. |

## How the engine uses the data

- **Once per evening** (after the corrections window): fetch ~500 days of daily bars for
  each symbol (3 requests), then process every completed session since the last run.
- **Fills use raw prices.** Orders decided at close(D) fill at open(D+1), using the real
  open/high/low of D+1 — so they are settled in the evening run after D+1.
- **Corporate actions are explicit.** Cash dividends are credited on ex-dates to positions
  held at the prior close; splits rescale positions. Strategies see a split-adjusted view.
- **Missing data:** if a session's bar is missing, the engine waits; after two sessions it
  proceeds and records a data gap (e.g. an unscheduled closure). Bars on days the NYSE
  calendar marks closed are flagged and ignored. Every session stores the exact bars used.
- **Revisions:** fills are final once processed (as in real trading). Research datasets are
  cached with SHA-256 checksums so backtests are reproducible.
- **Intraday prices:** not used by the V1 strategy. Tiingo's free IEX endpoint returns a
  derived reference price without an IEX agreement; it may be added for dashboard display.
