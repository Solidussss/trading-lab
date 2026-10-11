"""Markdown + JSON research reports (what the dashboard's Research page will render)."""

from __future__ import annotations

import json
import math
from dataclasses import asdict
from pathlib import Path
from typing import Any

from tradinglab.research.metrics import BenchmarkStats, Metrics


def _pct(x: float) -> str:
    return "n/a" if not math.isfinite(x) else f"{x:+.2%}"


def _num(x: float) -> str:
    return "inf" if math.isinf(x) else f"{x:.2f}"


def metrics_table(rows: list[tuple[str, Metrics]]) -> str:
    head = ("| Window | Return | CAGR | Sharpe | Max DD | Trades | Win % | PF | Avg R "
            "| Avg exposure | Costs |\n|---|---|---|---|---|---|---|---|---|---|---|")
    lines = [head]
    for name, m in rows:
        lines.append(
            f"| {name} ({m.start}→{m.end}) | {_pct(m.total_return)} | {_pct(m.cagr)} "
            f"| {_num(m.sharpe)} | {m.max_drawdown:.2%} | {m.trades} | {m.win_rate:.0%} "
            f"| {_num(m.profit_factor)} | {_num(m.avg_r)} | {m.avg_exposure:.0%} "
            f"| ${m.total_costs:,.0f} |"
        )
    return "\n".join(lines)


def benchmark_table(rows: list[BenchmarkStats]) -> str:
    lines = ["| Benchmark | Return | CAGR | Sharpe | Max DD |", "|---|---|---|---|---|"]
    for b in rows:
        lines.append(f"| {b.name} | {_pct(b.total_return)} | {_pct(b.cagr)} | "
                     f"{_num(b.sharpe)} | {b.max_drawdown:.2%} |")
    return "\n".join(lines)


def write_report(path: Path, markdown: str, payload: dict[str, Any]) -> tuple[Path, Path]:
    path.parent.mkdir(parents=True, exist_ok=True)
    md = path.with_suffix(".md")
    js = path.with_suffix(".json")
    md.write_text(markdown)
    js.write_text(json.dumps(payload, indent=2, default=_json_default))
    return md, js


def _json_default(o: Any) -> Any:
    if hasattr(o, "isoformat"):
        return o.isoformat()
    if hasattr(o, "__dataclass_fields__"):
        return asdict(o)
    if isinstance(o, float) and not math.isfinite(o):
        return str(o)
    return str(o)
