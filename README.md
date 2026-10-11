# Trading Lab

Private, autonomous, **simulation-only** quantitative trading research system for SPY, QQQ
and IWM. It runs its own virtual portfolio against daily market data from Tiingo. There is
no brokerage connection and no real-money trading code; see `docs/SAFETY.md`.

## Status

| Phase | Scope | State |
|---|---|---|
| 1 | Project structure, fail-closed config, egress allowlist, network guard, self-check | ✅ done, tested |
| 2 | NYSE calendar, data layer + checksummed cache, trend following v1, backtester, costs, splits, walk-forward, test vault, metrics, reports | ✅ done, tested on synthetic data |
| 3 | Simulation core shared by backtests and live engine; live engine with catch-up, idempotency, evidence-based data gaps, atomic commits, pause, manual drawdown resume; Tiingo client | ✅ done, tested (incl. exact live/backtest parity); independently audited |
| 4 | PostgreSQL store + migrations, advisory-lock run lock | ✅ done, tested against real PostgreSQL |
| 5 | Railway service: in-app scheduler (15-min idempotent checks), nightly CLI, restart/catch-up | ✅ done |
| 6 | Authenticated mobile dashboard | 🟡 Overview + controls live; Activity, Strategies, Analytics, Research pages next |
| 7 | Momentum and mean-reversion strategies | |
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

- **Virtual portfolio:** `$10,000` starting capital, long-only, no leverage, whole shares.
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
trading day after it settles (8:30 pm ET or later). The first run starts a fresh $10,000
portfolio at the latest settled session. `python main.py nightly` runs one check and exits,
if you prefer a separate Railway cron service.

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
  broker/base.py      future brokerage interface (documentation only)
tests/                unittest-style; run with pytest or `python -m unittest`
docs/                 SAFETY.md, DATA.md, RESEARCH.md
```

## Notes

- Python 3.12. The engine, simulator, safety layer and data client use only the standard
  library, so the whole core runs and tests without third-party packages.
- Results are in USD. A Canadian account trading these ETFs would also pay currency
  conversion costs, which are not modelled.
