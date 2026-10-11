"""Research command line.

    python -m tradinglab.research.cli fetch    --out data/cache     # needs TIINGO_API_TOKEN
    python -m tradinglab.research.cli baseline --data data/cache
    python -m tradinglab.research.cli baseline --synthetic          # offline demo only

`baseline` runs the full no-leakage protocol for trend following v1:
  1. time-ordered train / validation / test split (60/20/20)
  2. grid search on TRAIN only
  3. best parameters checked once on VALIDATION
  4. walk-forward over train+validation (choose on past, trade the next window)
  5. benchmarks: SPY buy-and-hold at 100% and at the 30% exposure cap
  6. the TEST window is NOT touched unless --final-test --reason "..." is given, and then
     only once per strategy spec (recorded in research/test_ledger.jsonl)
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from tradinglab.marketdata.bars import BarSeries, file_sha256, load_series, save_series
from tradinglab.research.backtest import BacktestConfig, Backtester
from tradinglab.research.metrics import (
    attribution,
    benchmark_stats,
    buy_and_hold,
    compute_metrics,
    drawdown_episodes,
    worst_trades,
)
from tradinglab.research.report import benchmark_table, metrics_table, write_report
from tradinglab.research.splits import TestVault, grid_search, time_splits, walk_forward
from tradinglab.research.synthetic import synthetic_series
from tradinglab.strategies.trend_following import TrendFollowing

DEFAULT_SYMBOLS = ("SPY", "QQQ", "IWM")
GRID = {"fast": [20, 50, 100], "slow": [150, 200, 250], "atr_period": [20], "atr_mult": [2.5, 3.0, 4.0]}
WARMUP_START = date(2016, 1, 4)
RESEARCH_START = date(2017, 1, 3)  # first year of data reserved for indicator warm-up


def ledger_path() -> Path:
    """Absolute, working-directory-independent location of the test-window ledger."""
    override = os.environ.get("TRADINGLAB_TEST_LEDGER")
    if override:
        return Path(override).resolve()
    return (Path(__file__).resolve().parents[3] / "research" / "test_ledger.jsonl")


def load_dotenv_if_present(path: Path = Path(".env")) -> None:
    """Minimal .env reader for local runs. Never overrides real environment variables."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def cmd_fetch(args: argparse.Namespace) -> int:
    load_dotenv_if_present()
    from tradinglab.config import load_settings
    from tradinglab.marketdata.calendar import last_completed_session
    from tradinglab.marketdata.tiingo import TiingoClient
    from tradinglab.safety.network_guard import install_network_guard

    settings = load_settings(os.environ)
    install_network_guard(settings.egress_hosts)
    client = TiingoClient(settings.require_data_token())
    latest = last_completed_session(datetime.now(UTC), settle_minutes=270)
    end = min(date.fromisoformat(args.end), latest) if args.end else latest
    series, metas = client.history(list(settings.symbols), date.fromisoformat(args.start), end)
    out = Path(args.out)
    for sym, s in series.items():
        p = save_series(s, out, metas[sym])
        missing = s.missing_sessions()
        print(f"{sym}: {len(s)} bars {s.bars[0].day}→{s.bars[-1].day}  issues={len(s.issues)} "
              f"missing_sessions={len(missing)}  -> {p}")
    return 0


def _load_data(args: argparse.Namespace) -> tuple[dict[str, BarSeries], dict, bool]:
    if args.synthetic:
        data = {s: synthetic_series(s, WARMUP_START, date(2025, 12, 31), seed=42)
                for s in DEFAULT_SYMBOLS}
        return data, {"source": "SYNTHETIC (not market data)"}, True
    data, fingerprints = {}, {}
    for sym in DEFAULT_SYMBOLS:
        path = Path(args.data) / f"{sym}_eod_raw.csv"
        if not path.exists():
            raise SystemExit(f"missing {path}; run the fetch command first")
        s, meta = load_series(path)
        data[sym] = s
        fingerprints[sym] = {"file": str(path), "sha256": file_sha256(path), "meta": meta.__dict__}
    return data, fingerprints, False


def cmd_baseline(args: argparse.Namespace) -> int:
    data, fingerprints, synthetic = _load_data(args)
    syms = list(DEFAULT_SYMBOLS)
    end = min(s.bars[-1].day for s in data.values())
    splits = time_splits(RESEARCH_START, end)
    base = BacktestConfig(start=RESEARCH_START, end=end)

    ranked = grid_search(TrendFollowing.create, GRID, data, splits.train, base,
                         forbidden=splits.test, symbols=syms)
    best = ranked[0]
    default = TrendFollowing.create()
    strat = TrendFollowing.create(**best.params)

    def run(window, s):
        return Backtester(s, data, BacktestConfig(start=window.start, end=window.end), syms).run()

    train_res, val_res = run(splits.train, strat), run(splits.validation, strat)
    val_default = run(splits.validation, default)
    wf = walk_forward(TrendFollowing.create, GRID, data, splits.train.start, splits.validation.end,
                      base, train_days=504, oos_days=126, forbidden=splits.test, symbols=syms)

    bench_rows = []
    for w in (splits.train, splits.validation):
        bench_rows.append(benchmark_stats(f"SPY 100% B&H ({w.name})",
                                          buy_and_hold(data["SPY"], w.start, w.end, 10_000)))
        bench_rows.append(benchmark_stats(f"SPY 30% B&H ({w.name})",
                                          buy_and_hold(data["SPY"], w.start, w.end, 10_000, 0.30)))

    rows = [("Train (optimised)", compute_metrics(train_res)),
            ("Validation (best params)", compute_metrics(val_res)),
            ("Validation (default params)", compute_metrics(val_default))]

    final = None
    if args.final_test:
        vault = TestVault(ledger_path(), splits.test)
        final = vault.evaluate(strat, data, base, reason=args.reason or "", symbols=syms)
        rows.append(("TEST (one-time)", compute_metrics(final)))

    wf_lines = ["| Train | OOS | Chosen params | OOS return | OOS Sharpe | OOS max DD | Trades |",
                "|---|---|---|---|---|---|---|"]
    for seg in wf.segments:
        p = seg.chosen_params
        wf_lines.append(f"| {seg.train.start}→{seg.train.end} | {seg.oos.start}→{seg.oos.end} "
                        f"| {p['fast']}/{p['slow']}/×{p['atr_mult']} "
                        f"| {seg.oos_metrics.total_return:+.2%} | {seg.oos_metrics.sharpe:.2f} "
                        f"| {seg.oos_metrics.max_drawdown:.2%} | {seg.oos_metrics.trades} |")
    wf_total = wf.oos_curve[-1].equity / base.initial_capital - 1 if wf.oos_curve else 0.0

    attr = attribution(val_res.trades)
    dd = drawdown_episodes(val_res.equity_curve)[:3]
    worst = worst_trades(train_res.trades + val_res.trades, 5)
    banner = ("> **SYNTHETIC DATA - pipeline demonstration only. These numbers say nothing "
              "about real markets.**\n\n" if synthetic else "")
    md = f"""# Baseline study: trend following v1

{banner}Generated {datetime.now(UTC):%Y-%m-%d %H:%M UTC}. Capital $10,000, risk 0.5%/trade,
30% max exposure, 3 max positions, conservative costs (~10 bp round trip + fees).

**Splits** train {splits.train.start}→{splits.train.end} · validation
{splits.validation.start}→{splits.validation.end} · test {splits.test.start}→{splits.test.end}
({'evaluated once, see ledger' if final else '**untouched**'}).

**Selected on train:** `{strat.spec.label}` params {dict(strat.spec.params)}
(best of {len(ranked)} combinations; runner-up score {ranked[1].score:.2f} vs best {best.score:.2f}).

## Results

{metrics_table(rows)}

## Benchmarks

{benchmark_table(bench_rows)}

## Walk-forward (train 2y → trade next 6m, re-optimised each step)

Stitched out-of-sample return: **{wf_total:+.2%}** over {len(wf.segments)} segments.
Parameter stability: {wf.param_stability:.0%} of segments chose the same parameter set.

{chr(10).join(wf_lines)}

## Attribution (validation)

- By symbol: {', '.join(f'{k} ${v:,.0f}' for k, v in attr['by_symbol'].items()) or 'no trades'}
- By exit reason: {', '.join(f'{k} ${v:,.0f}' for k, v in attr['by_exit_reason'].items()) or 'n/a'}

## Largest drawdowns (validation)

{chr(10).join(f'- {e.depth:.2%}: peak {e.peak_day}, trough {e.trough_day}, recovered {e.recovery_day or "not yet"}' for e in dd) or '- none ≥ 2%'}

## Worst trades (train + validation)

{chr(10).join(f'- {t.symbol} {t.entry_day}→{t.exit_day}: ${t.pnl:,.2f} ({t.r_multiple:.2f}R, {t.exit_reason})' for t in worst) or '- none'}

## Caveats

- Fills use raw traded prices; cash dividends are credited on ex-dates (not reinvested) and
  splits rescale positions. Benchmarks are price-only buy-and-hold plus the same dividends.
- The 3% intraday halt is checked on closing equity in backtests (no intraday path in daily bars).
- Exposure cap applies when sizing; positions may drift slightly above 30% as prices move.
- A good backtest is not evidence of future profitability. Forward paper results decide.
"""
    payload = {"strategy": strat.spec.label, "params": dict(strat.spec.params),
               "synthetic": synthetic, "data": fingerprints,
               "splits": {k: [getattr(splits, k).start, getattr(splits, k).end]
                          for k in ("train", "validation", "test")},
               "metrics": {name: m for name, m in rows},
               "benchmarks": bench_rows,
               "walk_forward": [{"train": [s.train.start, s.train.end],
                                 "oos": [s.oos.start, s.oos.end], "params": s.chosen_params,
                                 "oos_metrics": s.oos_metrics} for s in wf.segments],
               "grid": [{"params": r.params, "score": r.score} for r in ranked]}
    stem = "baseline_synthetic" if synthetic else f"baseline_{strat.spec.spec_hash}"
    md_path, js_path = write_report(Path(args.reports) / stem, md, payload)
    print(f"wrote {md_path} and {js_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="tradinglab.research")
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="download daily bars from Tiingo (needs TIINGO_API_TOKEN)")
    f.add_argument("--start", default="2016-01-01")
    f.add_argument("--end")
    f.add_argument("--out", default="data/cache")
    f.set_defaults(func=cmd_fetch)

    b = sub.add_parser("baseline", help="run the no-leakage baseline study")
    g = b.add_mutually_exclusive_group(required=True)
    g.add_argument("--data")
    g.add_argument("--synthetic", action="store_true")
    b.add_argument("--reports", default="reports")
    b.add_argument("--final-test", action="store_true")
    b.add_argument("--reason")
    b.set_defaults(func=cmd_baseline)

    args = p.parse_args(argv)
    if getattr(args, "final_test", False) and not args.reason:
        p.error("--final-test requires --reason")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
