# Rebuild: new look, stock-picking bots, hype bot variants

Approved by the owner on 2026-11-20 ("Yes, build it") from a mockup of the home screen.

## Why

The owner's words: the system is "failing bad" and "looks terrible". Measured: the hype bot lost
$433 on 77 trades in seven days (13 wins). The stock pages are a pale text-only scroll and the
hype page is a different design altogether.

## Goals

1. One dark, chart-first design on every page, usable on a phone and on a laptop.
2. Better candidate bots: trade the day's busiest stocks, not only SPY/QQQ/IWM, using the ideas
   in the day-trader skill pack (morning scan, odds replay before entry).
3. Stop the hype bot's mechanical losses and let rule sets compete side by side.

## Non-goals

- No rewrite of the simulation core, safety layer, data cache or database layer.
- No history tests for the stock-picking bots (the data plan cannot load 100 stocks of history).
  They are judged on forward paper trading only, and the page says so.

## Anti-goals

- No brokerage connection of any kind. The skill pack's order sender is not ported.
- No new data host. Everything stays on Tiingo, GeckoTerminal and DexScreener.
- No tuned parameters. Round numbers fixed up front, as in docs/RESEARCH.md.

## Constraints

- Tiingo free plan: 50 requests/hour (limiter set to 45), 1,000/day, 500 symbols/month.
  The three ETFs already use 36/hour while the market is open.
- Committed results must come from final 5-minute bars after the close, as today.
- Standard library only in the engine. Python 3.12. `ruff check` and the test suite stay green.

## Approaches considered

- **Full rewrite** on a web framework with a JS front end. Rejected: weeks of work, throws away
  247 passing tests, fixes nothing the owner is unhappy with.
- **Install the skill pack in Claude only.** Rejected by the owner: it leaves the bots unchanged.
- **Keep the engine; rebuild the look, add bots, fix hype (chosen).** Smallest change that
  addresses both complaints.

## Design

### 1. Look (web/)

- One set of design tokens (dark, IBM Plex Sans + Mono, green/red only for profit and loss,
  amber for the "SIM" badge) in a shared stylesheet used by every page.
- Shared shell: header (name, SIM badge, market status, last update) and tabs
  (Overview, Stocks, Crypto, History tests, Controls).
- Overview (`/`): today's total across bots, one chart with every bot's curve, a ranked table
  (30-day sparkline, today, total, win rate, state), latest trades with the reason for each, and
  a system-health panel (stock feed, coin feed, database, last nightly run).
- Bot page (`/bot/<id>`): headline numbers, equity curve, today's trades on the price chart,
  trade-by-trade list, daily history. Plain-English wording is kept.
- Hype page (`/hype`): the same shell and tokens; a comparison strip of the rule sets, then the
  selected one's detail (`?a=<id>`).
- Wide screens use a two-column grid; under 820px it collapses to one column.

### 2. Stocks in play (intraday/, marketdata/, engine/)

- Universe: a fixed list of about 100 liquid US stocks (`intraday/universe.py`).
- Scan: once per session, between 9:33 and 9:45 am ET, one Tiingo multi-ticker quote request.
  A stock qualifies when it gapped up 1% to 50% from yesterday's close, is trading above its
  open, and costs at least $5. The top 5 by gap are the day's picks. Picks are stored with the
  snapshot values; a missed scan means the bot sits that day out (logged, shown on the page).
  The window ends at 9:45 because the bots' first possible signal is the bar closing at 9:50:
  a pick is always made before any price it could trade on. A reply with usable prices for
  under half the list counts as a failed scan (retried inside the window), not a quiet market.
- Bars: after the close, final 5-minute bars for the picks only (5 requests). During the day
  the picks' bars refresh inside an allowance of 6 requests an hour (what the three funds leave
  of the 45), so about once an hour per stock. This runner never waits on the limiter.
- Bot `sip`: opening breakout (15-minute range) on the day's picks. Same risk rules and costs.
- Bot `sip_odds`: the same, but before each entry it replays the rest of the session 2,000
  times from the stock's own bars at the same time of day over up to 15 earlier sessions
  (seeded, so the replay is repeatable). It enters only when the replay's average result is at
  least +0.1 times the amount risked. The odds are stored with the trade and shown beside it.
  History for a pick is loaded on the day it is picked (1 request per stock); with fewer than
  10 sessions of history the bot does not trade that stock.
- `BotSpec` gains `universe` ("etf" or "in_play"). The core is unchanged: it already takes the
  symbol list per run.

### 3. Hype bot (hype/)

- Several paper accounts run side by side, $1,000 each, one scan shared between them:
  - `current`: today's rules, paused for new buys (open positions still exit by the rules).
  - `no_repeat`: today's rules, but never re-buys a coin it lost on, 24-hour cooldown otherwise.
  - `deep_pools`: only pools of $100k+, market cap $500k+, 80+ trades in 5 minutes.
- Open positions are re-priced every 10 seconds from DexScreener first (its limits are far
  roomier), GeckoTerminal second. Discovery backs off for 5 minutes after a rate-limit reply.
- An account that falls 30% below its start is paused automatically.
- Fill and cost model unchanged (it is deliberately pessimistic).

## Order of work

Look first, then stocks in play, then hype (hype is built in parallel since it shares no files).

## Left to the builder

Exact layout spacing, chart rendering details, test structure, universe membership.

## Changes after independent review (2026-11-20)

- Replay history cut from 30 to 15 sessions so every pick's history fits in the morning's
  request allowance and the daytime preview matches the committed day.
- Scan window moved from 9:35-10:00 to 9:33-9:45 (removes a look-ahead when a scan ran late).
- Request budget made non-blocking with its own hourly allowance; gap ceiling of 50%; failed
  quote replies detected; half-day sessions replayed to their real close; in-play bots resume
  under their own lock.

## Open risks

- Whether Tiingo counts a multi-ticker quote request as one request is unverified. If it does
  not, the scan fails loudly (system health shows it) and the universe is cut to fit.
- The odds replay assumes bars at the same time of day are exchangeable across days. It is a
  reality check, not a forecast.
