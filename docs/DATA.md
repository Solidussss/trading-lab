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
- **Missing or suspect data — never invented, never silently discarded:**
  - a symbol's missing bar is accepted as a gap only once the provider has delivered later
    sessions for it; until then the engine waits (and flags a stall after 3 sessions);
  - if every symbol is missing a day but later data exists, the day is treated as an
    unscheduled market closure and skipped;
  - a row the validator rejects, or an overnight move beyond ±35% without a recorded split,
    blocks processing until the provider corrects it or you acknowledge it on the dashboard;
  - bars on days the NYSE calendar marks closed are flagged and ignored;
  - every session stores the exact bars used.
- **Settling:** a session is processed 4.5 h after its close and never before 8:30 pm ET
  (Tiingo's corrections run until ~8 pm ET, including on early-close days).
- **Revisions:** fills are final once processed (as in real trading). Research datasets are
  cached with SHA-256 checksums so backtests are reproducible.
## Intraday prices (day-trading bots)

- **Source:** Tiingo IEX endpoint, 5-minute bars, regular session only
  (`/iex/<ticker>/prices?resampleFreq=5min&afterHours=false&columns=open,high,low,close,volume`).
  History from August 2017; same free plan and "internal use only" licence.
- **What it is:** trades on the IEX exchange only. For SPY/QQQ/IWM this tracks the market
  closely; volume is IEX-only, so "the day's average price" (VWAP) is IEX VWAP.
- **Budget:** one limiter for the whole process (45/hour, 900/day). Live bots: 3 requests per
  5-minute poll, shared by every bot. The history loader only uses what's left and pauses
  during market hours.
- **Completeness and labels:** only bars that have fully closed are used. A day's data is
  final once 90% of bars are in or 3 hours after the close; until then the day isn't
  committed. The provider's time-stamp convention (start or end of each 5 minutes) is learned
  once from a complete day. A shorter response never replaces more data already stored.
- **Yesterday's close** (gap strategy) is the last 5-minute price of the previous session,
  the same rule live and in history tests.
