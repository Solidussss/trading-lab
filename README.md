# Trading Lab

Private, autonomous, **simulation-only** trading research system. It runs pretend-money bots
side by side, each with its own simulated account: seven day-trading bots on SPY, QQQ and IWM
(5-minute prices, flat every night), one slow daily trend follower, two "stocks in play" bots
that trade the day's busiest stocks, and three competing rule sets of a Solana memecoin hype
bot. Stock prices come from Tiingo. There is no brokerage connection and no real-money trading
code; see `docs/SAFETY.md`.

## Status

| Phase | Scope | State |
|---|---|---|
| 1 | Project structure, fail-closed config, egress allowlist, network guard, self-check | ✅ done, tested |
| 2 | NYSE calendar, data layer + checksummed cache, trend following v1, backtester, costs, splits, walk-forward, test vault, metrics, reports | ✅ done, tested on synthetic data |
| 3 | Simulation core shared by backtests and live engine; live engine with catch-up, idempotency, evidence-based data gaps, atomic commits, pause, manual drawdown resume; Tiingo client | ✅ done, tested (incl. exact live/backtest parity); independently audited |
| 4 | PostgreSQL store + migrations, advisory-lock run lock | ✅ done, tested against real PostgreSQL |
| 5 | Railway service: in-app scheduler (15-min idempotent checks), nightly CLI, restart/catch-up | ✅ done |
| 6 | Authenticated mobile dashboard | ✅ leaderboard, one page per bot (every trade explained), history-test results, controls |
| 7 | More strategies | ✅ seven day-trading bots (opening breakout, gap-down bounce, VWAP dip, VWAP trend, fair value gap, plus two from published research: noise-area breakout (Zarattini, Aziz & Barbon 2024) and last-half-hour momentum (Gao, Han, Li & Zhou 2018)) on a shared intraday core with market and resting limit orders; independently reviewed |
| 7b | Intraday history tests | ✅ background loader (Aug 2017 on) + fixed train/validation windows, sealed final test |
| 9 | One dashboard look (`web/theme.py`): Overview, Stocks, Crypto, History tests, Controls | ✅ |
| 10 | Stocks in play: morning scan of ~100 stocks, opening breakouts in the top five gappers, and an odds-checked twin that replays the rest of the day before each entry | ✅ forward paper trading only (no history test) |
| 11 | Hype bot rule sets side by side, positions priced from DexScreener first, back-off when rate-limited | ✅ |
| 8 | Claude research reports (scheduled / on demand only) | |

## Architecture

```
Tiingo EOD API ──► marketdata/tiingo.py ──► BarSource
                                              │
                    engine/live.py  ◄─────────┘   nightly job: process each new session
                         │
                         ▼
                    sim/core.py  ◄── research/backtest.py (same code path for research)
              (orders, fills, stops, dividends, splits, risk, halts, decisions)
                         │
                         ▼
                    engine/store.py  (in-memory now; PostgreSQL in Phase 4)
                         │
                         ▼
                 dashboard / Claude research (Phases 6, 8)
```

- **Virtual portfolios:** `$1,000,000` per bot (`CAPITAL_BASE_USD`), long-only, no leverage,
  whole shares. Same limits for every bot: 0.5% risk per trade, 30% invested at most, 3
  positions, -3% daily and -10% peak halts.
- **Day-trading bots** (`intraday/`): one shared 5-minute IEX feed; each 5-minute step is
  processed for all funds together; orders fill at the next bar's open with costs and
  commission; everything sold at the close. The same core runs live, in the dashboard's
  "today so far" view and in history tests. Add a strategy by subclassing
  `IntradayStrategy` and registering it in `intraday/strategies.py`.
- **Execution model:** signal after close(D); on-open limit order at close × 1.01; resting
  protective stop. Fills use D+1's real open/low with conservative costs.
- **Modular:** `broker/base.py` documents the interface a brokerage adapter would need, but
  none exists and the safety layer forbids one in V1.

## Deploy on Railway

1. New Project → Deploy from GitHub repo → `trading-lab`. Railway reads `railway.json`
   (start command `python main.py`, health check `/healthz`).
2. Service → Variables:
   - `APP_ENV` = `production`
   - `TRADING_MODE` = `simulated`
   - `DASHBOARD_PASSWORD` = a password of at least 12 characters (you log in with any
     username and this password)
   - `TIINGO_API_TOKEN` = your Tiingo token
   - `DATABASE_URL` = `${{Postgres.DATABASE_URL}}` (after adding PostgreSQL: **+ New →
     Database → PostgreSQL**). Add only this one; the app refuses libpq `PG*` variables.
3. Service → Settings → Networking → Generate Domain. Open it on your phone.

Once the database is attached, the web service checks every 15 minutes and processes each
trading day after it settles (8:30 pm ET or later); during market hours it checks every 5
minutes for the day-trading bots and commits each bot's day 20 minutes after the close. Each
new bot starts on the next session with $1,000,000. In the background, the remaining Tiingo
request budget loads intraday history back to August 2017 and re-runs the history tests. `python main.py nightly` runs one check and exits,
if you prefer a separate Railway cron service.

A few minutes after each open the stocks-in-play scan makes one Tiingo quote request for the
whole list in `intraday/universe.py` and stores that day's picks; the picks' 5-minute prices are
then loaded inside an allowance of 6 requests an hour, after the three funds (so their
daytime view updates about once an hour; results come from complete prices after the close).

Do not add brokerage or proxy variables: the app refuses to start its trading functions
if it sees them.

## Quick start (local)

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env          # add TIINGO_API_TOKEN (never commit .env)

pytest                                           # full suite, no network or keys needed
pytest tests/test_no_real_trading.py -v

python -m tradinglab.research.cli baseline --synthetic   # offline pipeline demo
python -m tradinglab.research.cli fetch                  # real daily bars from Tiingo
python -m tradinglab.research.cli baseline --data data/cache
```

## Layout

```
src/tradinglab/
  config.py           fail-closed settings, Secret wrapper, hard risk ceilings
  safety/             egress allowlist, network guard, startup self-check
  marketdata/         NYSE calendar, bars (+dividends/splits), cache, Tiingo client
  strategies/         strategy interface, indicators, trend following v1
  engine/risk.py      position sizing and halts
  sim/core.py         the simulation core (one session at a time, serialisable state)
  engine/live.py      live paper simulation driver; engine/store.py persistence interface
  research/           backtester, costs, metrics, splits/walk-forward/test vault, CLI
  intraday/           day-trading core, strategies, live runner, history tests;
                      inplay.py (morning scan + runner), replay.py (odds replay), universe.py
  hype/               memecoin hype paper bot: rule sets, feeds, cost model
  web/                dashboard (theme.py: the shared look; views_bots.py: overview and bot
                      pages; views_hype.py: the crypto page)
  broker/base.py      future brokerage interface (documentation only)
tests/                unittest-style; run with pytest or `python -m unittest`
docs/                 SAFETY.md, DATA.md, RESEARCH.md
```

## Notes

- Python 3.12. The engine, simulator, safety layer and data client use only the standard
  library, so the whole core runs and tests without third-party packages.
- Results are in USD. A Canadian account trading these ETFs would also pay currency
  conversion costs, which are not modelled.
