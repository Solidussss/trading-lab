"""History tests for the day-trading bots, and the background loader that collects the history.

Backfill: loads August 2017 onwards, one ~15-session chunk per request, oldest first (so the
training years are complete first), only while the shared request budget has room - the live
bots always come first. About 470 requests cover all three funds: roughly a day on the free plan.
It also loads each fund's official daily closes once (for the gap strategy and the benchmark).

History test, per bot, using exactly the same core as live trading:
  * The history is split by fixed dates: training 2017-08 to 2022, validation 2023-2024, and a
    final test from 2025 on. Fixed dates mean a day never moves between windows as data loads.
    The final test stays sealed until you choose to open it, so nobody can adjust a strategy
    to fit it (docs/RESEARCH.md). Sealed means not even run.
  * Every bot starts with the same $1,000,000, pays the same costs, and obeys the same limits;
    a peak-drawdown halt is modelled as resumed after 20 trading days.
  * Benchmark: simply buying SPY at the start of each window and holding it.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from tradinglab.engine.risk import RiskParams
from tradinglab.intraday.core import INTRADAY_COSTS, BotState, IntradayCore, ITrade
from tradinglab.intraday.live import BotSpec
from tradinglab.marketdata.calendar import previous_trading_day, trading_days
from tradinglab.marketdata.intraday import (
    INTRADAY_HISTORY_START,
    detect_end_labelled,
    chunks,
    parse_iex_rows,
    split_by_day,
)
from tradinglab.research.splits import Window

TRADING_DAYS = 252
AUTO_RESUME_DAYS = 20
MIN_HISTORY_DAYS = 60
TRAIN = Window("train", date(2017, 8, 1), date(2022, 12, 31))
VALIDATION = Window("validation", date(2023, 1, 1), date(2024, 12, 31))
TEST_START = date(2025, 1, 1)


# -- backfill --------------------------------------------------------------------------------


@dataclass
class BackfillStatus:
    days_total: int
    days_loaded: int
    requests: int = 0

    @property
    def done(self) -> bool:
        return self.days_loaded >= self.days_total

    @property
    def fraction(self) -> float:
        return self.days_loaded / self.days_total if self.days_total else 1.0


def backfill_status(store, symbols: Sequence[str], end: date,
                    start: date = INTRADAY_HISTORY_START) -> BackfillStatus:
    total = len(trading_days(start, end)) * len(symbols)
    loaded = sum(sum(1 for f in store.fetches(s, start, end).values() if f.final)
                 for s in symbols)
    return BackfillStatus(total, loaded)


def backfill_step(store, fetch_rows: Callable[[str, date, date], list], symbols: Sequence[str],
                  end: date, budget: int, now: datetime | None = None,
                  start: date = INTRADAY_HISTORY_START,
                  fetch_daily: Callable[[str, date, date], list] | None = None) -> BackfillStatus:
    """Fetch up to `budget` missing chunks, oldest first. Safe to interrupt at any point."""
    now = now or datetime.now(UTC)
    used = 0
    if fetch_daily is not None:
        for sym in symbols:
            if used < budget and not store.get_flag(f"daily_history:{sym}"):
                # from two weeks earlier, so the benchmark has a close before day one
                store.save_prices(sym, fetch_daily(sym, start - timedelta(days=14), end))
                store.set_flag(f"daily_history:{sym}", True, now)
                used += 1
    for a, b in chunks(start, end):
        if used >= budget:
            break
        for sym in symbols:
            if used >= budget:
                break
            have = store.fetches(sym, a, b)
            days = trading_days(a, b)
            if all(d in have and have[d].final for d in days):
                continue
            rows = fetch_rows(sym, a, b)
            used += 1
            labels = (store.get_flag("iex_end_labelled") if store.get_flag("iex_labels_known")
                      else None)
            if labels is None:
                learned = detect_end_labelled(rows)
                if learned is not None:
                    store.set_flag("iex_end_labelled", learned, now)
                    store.set_flag("iex_labels_known", True, now)
                    labels = learned
            bars, _ = parse_iex_rows(rows, labels)
            if len(rows) >= 1990 and a < b:  # near the provider's point cap: refetch halves
                mid = len(days) // 2
                for x, y in ((days[0], days[mid - 1]), (days[mid], days[-1])):
                    more, _ = parse_iex_rows(fetch_rows(sym, x, y), labels)
                    used += 1
                    bars = [z for z in bars if not (x <= z.day <= y)] + more
            store.replace_ibars(sym, a, b, bars)  # never replaces a day with fewer bars
            per_day = split_by_day(store.ibars(sym, a, b))
            for d in days:
                if not (d in have and have[d].final):
                    store.mark_fetched(sym, d, len(per_day.get(d, [])), True, now)
    status = backfill_status(store, symbols, end, start)
    status.requests = used
    return status


# -- metrics ---------------------------------------------------------------------------------


def _max_drawdown(equity: Sequence[float]) -> float:
    peak, worst = -math.inf, 0.0
    for e in equity:
        peak = max(peak, e)
        worst = min(worst, e / peak - 1)
    return worst


def summarize(days: Sequence[tuple[date, float, float]], trades: Sequence[ITrade],
              benchmark_return: float | None) -> dict:
    """days: (day, start_equity, end_equity)."""
    if not days:
        return {"days": 0}
    start_eq, end_eq = days[0][1], days[-1][2]
    rets = [e / s - 1 for _, s, e in days if s > 0]
    years = len(days) / TRADING_DAYS
    total = end_eq / start_eq - 1
    sd = statistics.pstdev(rets) if len(rets) > 1 else 0.0
    wins = [t.pnl for t in trades if t.pnl > 0]
    losses = [t.pnl for t in trades if t.pnl <= 0]
    traded_days = sum(1 for d in {t.day for t in trades})
    return {
        "start": days[0][0].isoformat(), "end": days[-1][0].isoformat(), "days": len(days),
        "start_equity": start_eq, "end_equity": end_eq, "total_return": total,
        "annual_return": (1 + total) ** (1 / years) - 1 if years > 0 and total > -1 else -1.0,
        "max_drawdown": _max_drawdown([days[0][1]] + [e for _, _, e in days]),
        "sharpe": (statistics.fmean(rets) / sd * math.sqrt(TRADING_DAYS)) if sd > 0 else 0.0,
        "trades": len(trades), "win_rate": len(wins) / len(trades) if trades else 0.0,
        "avg_win": statistics.fmean(wins) if wins else 0.0,
        "avg_loss": statistics.fmean(losses) if losses else 0.0,
        "profit_factor": (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else None,
        "avg_trade": statistics.fmean([t.pnl for t in trades]) if trades else 0.0,
        "costs": sum(t.costs for t in trades),
        "days_traded": traded_days,
        "best_day": max(e - s for _, s, e in days), "worst_day": min(e - s for _, s, e in days),
        "benchmark_return": benchmark_return,
    }


def _spy_return(closes: Mapping[date, float], a: date, b: date) -> float | None:
    """Buy SPY at the close before `a`, hold to the close of `b` (price only)."""
    before = [k for k in closes if k < a]
    upto = [k for k in closes if a <= k <= b]  # windows end on calendar dates, not sessions
    if not before or not upto:
        return None
    return closes[max(upto)] / closes[max(before)] - 1


# -- history test ----------------------------------------------------------------------------


def run_history_test(store, bots: Sequence[BotSpec], symbols: Sequence[str], risk: RiskParams,
                     capital: float, *, end: date, unlocked: Callable[[str], bool],
                     start: date = INTRADAY_HISTORY_START, now: datetime | None = None) -> dict:
    """Runs every bot over the loaded history and saves one result payload per bot."""
    now = now or datetime.now(UTC)
    loaded = sorted(d for d, f in store.fetches(symbols[0], start, end).items() if f.final)
    if len(loaded) < MIN_HISTORY_DAYS:
        return {}
    first = loaded[0]
    test = Window("test", TEST_START, end)
    bars = {s: split_by_day(store.ibars(s, first, end)) for s in symbols}
    closes = {s: {b.day: b.close for b in store.price_history(s, 4000)} for s in symbols}
    out = {}
    for bot in bots:
        core = IntradayCore(bot.strategy(), symbols, risk, INTRADAY_COSTS,
                            auto_resume_days=AUTO_RESUME_DAYS)
        sealed = not unlocked(bot.id)
        stop_at = VALIDATION.end if sealed else test.end
        st = BotState.initial(capital, first)
        days, trades = [], []
        for d in trading_days(first, stop_at):
            day_bars = {s: bars[s].get(d, []) for s in symbols}
            if not any(day_bars.values()):
                continue
            pd = previous_trading_day(d)
            prev = {s: bars[s][pd][-1].close if bars[s].get(pd) else closes[s].get(pd)
                    for s in symbols}  # same rule as live (IntradayRunner.prev_closes)
            r = core.run_day(st, d, day_bars, prev, final=True)
            days.append((d, r.start_equity, r.end_equity))
            trades.extend(r.trades)
        spy = closes.get("SPY", {})
        result = {"bot": bot.id, "strategy": bot.strategy().label, "generated": now.isoformat(),
                  "history_start": first.isoformat(), "history_end": end.isoformat(),
                  "test_sealed": sealed, "windows": {}}
        for w in (TRAIN, VALIDATION, test):
            if sealed and w is test:
                result["windows"][w.name] = {"sealed": True, "start": w.start.isoformat(),
                                             "end": w.end.isoformat()}
                continue
            wd = [x for x in days if w.start <= x[0] <= w.end]
            if not wd:
                result["windows"][w.name] = {"days": 0, "start": w.start.isoformat(),
                                             "end": w.end.isoformat()}
                continue
            wt = [t for t in trades if w.start <= t.day <= w.end]
            result["windows"][w.name] = summarize(wd, wt, _spy_return(spy, w.start, w.end))
        step = max(1, len(days) // 150)
        result["curve"] = [(d.isoformat(), round(e, 2)) for d, _, e in days[::step]]
        store.save_backtest(bot.id, result)
        out[bot.id] = result
    return out
