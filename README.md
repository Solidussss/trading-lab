# Trading Lab

Private, autonomous, **paper-only** quantitative trading research system for SPY, QQQ and
IWM, built on the Alpaca paper trading API. No real-money trading code exists in this repo.

## Status

| Phase | Scope | State |
|---|---|---|
| 1 | Project structure, fail-closed config, paper-only safety layers, broker adapter, order gate, risk rules | ✅ done, tested |
| 2 | NYSE calendar, data layer + cache, trend following v1, backtester, costs, splits, walk-forward, test vault, metrics, reports | ✅ done, tested on synthetic data; real-data run needs paper keys |
| 3 | Deterministic paper engine (evaluate after close, OPG orders, resting stops, reconciliation) | next |
| 4 | PostgreSQL schema + decision/order/fill logging | |
| 5 | Railway scheduled jobs + restart/recovery | |
| 6 | Authenticated mobile dashboard | |
| 7 | Momentum and mean-reversion strategies | |
| 8 | Claude research reports (scheduled / on demand only) | |

Order submission is **off** (`ORDER_SUBMISSION_ENABLED=false`) and stays off until the
activation checklist in `docs/SAFETY.md` passes and you approve.

## Quick start (local)

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env        # add your Alpaca PAPER keys (never commit .env)

pytest                                        # full suite, no network or keys needed
pytest tests/test_live_trading_impossible.py -v

python -m tradinglab.research.cli baseline --synthetic   # offline pipeline demo
python -m tradinglab.research.cli fetch                  # real SIP daily bars (paper keys)
python -m tradinglab.research.cli baseline --data data/cache
```

Reports are written to `reports/` as Markdown and JSON.

## Layout

```
src/tradinglab/
  config.py               fail-closed settings, Secret wrapper, hard risk ceilings
  safety/                 endpoint allowlist, network guard, order gate, startup self-check
  broker/                 Broker protocol, Alpaca paper adapter, deterministic fake broker
  marketdata/             NYSE calendar, bar validation + checksummed cache, Alpaca fetcher
  strategies/             strategy interface, indicators, trend following v1
  engine/risk.py          position sizing and halts (shared by backtest and live engine)
  research/               backtester, costs, metrics, splits/walk-forward/test vault, CLI
tests/                    unittest-style tests, runnable with pytest or `python -m unittest`
docs/                     SAFETY.md, DATA.md, RESEARCH.md
```

## Key documents

- `docs/SAFETY.md` — the seven layers that make live trading impossible, and their limits
- `docs/DATA.md` — Alpaca free-plan coverage, delays, limits, and how they shape the design
- `docs/RESEARCH.md` — fill model, costs, risk rules, no-leakage protocol

## Development notes

- Target runtime: Python 3.12. Core engine and tests use only the standard library, so the
  safety and research suites run without third-party packages.
- Secrets live only in environment variables (Railway service variables in production).
  `Secret` values never appear in `repr`, logs or error messages.
