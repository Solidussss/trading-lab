"""Day-trading bots: core fills/limits, strategies, data parsing, live runner, history tests."""

import copy
import unittest
from datetime import UTC, date, datetime, timedelta

from tests.helpers import settings
from tradinglab.engine.risk import RiskParams
from tradinglab.engine.store import InMemoryStore
from tradinglab.intraday.core import INTRADAY_COSTS, BotState, IntradayCore
from tradinglab.intraday.live import DEFAULT_BOTS, BotSpec, IntradayRunner, first_session_after
from tradinglab.intraday.research import backfill_status, backfill_step, run_history_test
from tradinglab.intraday.strategies import (
    REGISTRY,
    Enter,
    Exit,
    GapFill,
    IntradayStrategy,
    OpeningRangeBreakout,
    StrategyContext,
    VwapDip,
    VwapTrend,
)
from tradinglab.marketdata.calendar import NY, session, trading_days
from tradinglab.marketdata.intraday import BAR, POINT_CAP, IBar, fetch_range, parse_iex_rows
from tradinglab.research.synthetic_intraday import synthetic_intraday

DAY = date(2024, 3, 5)  # a Tuesday
SYMS = ["SPY", "QQQ", "IWM"]


def at(day, hhmm):
    h, m = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=NY).astimezone(UTC)


def day_bars(day=DAY, price=100.0, overrides=None, n=None):
    """Flat bars at `price` for the whole session; overrides: {"HH:MM": (o, h, l, c)}."""
    s = session(day)
    n = n or int((s.close_utc - s.open_utc) / BAR)
    out = []
    for i in range(n):
        ts = s.open_utc + i * BAR
        o, h, lo, c = (overrides or {}).get(ts.astimezone(NY).strftime("%H:%M"),
                                            (price, price + 0.05, price - 0.05, price))
        out.append(IBar(ts, o, h, lo, c, 1000.0))
    return out


class Scripted(IntradayStrategy):
    id = "scripted"
    max_entries_per_symbol = 5

    def __init__(self, plan):
        super().__init__()
        self.plan = plan

    def on_bar(self, ctx):
        return self.plan.get((ctx.symbol, ctx.bar.ny_time))


def run(plan, bars, risk=None, state=None, **kw):
    core = IntradayCore(Scripted(plan), ["SPY"], risk or RiskParams(), INTRADAY_COSTS)
    st = state or BotState.initial(100_000.0, DAY)
    return core.run_day(st, DAY, {"SPY": bars}, {"SPY": 100.0}, **kw), st


class CoreTests(unittest.TestCase):
    def test_signal_fills_at_next_bar_open_with_costs(self):
        bars = day_bars(overrides={"10:05": (101.0, 101.1, 100.9, 101.0)})
        res, st = run({("SPY", "10:00"): Enter(99.0, "test")}, bars)
        buy = res.fills[0]
        self.assertEqual(buy.ts, at(DAY, "10:05"))
        self.assertEqual(buy.ref_price, 101.0)  # the next bar's open, not the signal bar's close
        self.assertAlmostEqual(buy.fill_price, 101.0 * (1 + 2.0 / 1e4))
        self.assertEqual(res.trades[0].exit_reason, "end_of_day")
        self.assertEqual(res.fills[-1].ts, at(DAY, "15:55"))
        self.assertAlmostEqual(st.cash, res.end_equity)
        self.assertEqual((st.days, st.last_day), (1, DAY))

    def test_stop_hit_inside_bar_and_gap_through_stop(self):
        bars = day_bars(overrides={"10:30": (100.0, 100.0, 98.5, 99.0)})
        res, _ = run({("SPY", "10:00"): Enter(99.0, "t")}, bars)
        t = res.trades[0]
        self.assertEqual(t.exit_reason, "stop_hit")
        self.assertEqual(res.fills[-1].ref_price, 99.0)
        bars = day_bars(overrides={"10:30": (97.0, 97.2, 96.8, 97.0)})
        res, _ = run({("SPY", "10:00"): Enter(99.0, "t")}, bars)
        self.assertEqual(res.trades[0].exit_reason, "stop_gap")
        self.assertEqual(res.fills[-1].ref_price, 97.0)  # the open, worse than the stop

    def test_stop_assumed_before_target_in_the_same_bar(self):
        bars = day_bars(overrides={"10:30": (100.0, 102.0, 98.0, 100.0)})
        res, _ = run({("SPY", "10:00"): Enter(99.0, "t", target=101.0)}, bars)
        self.assertEqual(res.trades[0].exit_reason, "stop_hit")

    def test_target_and_exit_signal(self):
        bars = day_bars(overrides={"10:30": (100.0, 101.5, 99.9, 101.2)})
        res, _ = run({("SPY", "10:00"): Enter(99.0, "t", target=101.0)}, bars)
        self.assertEqual((res.trades[0].exit_reason, res.fills[-1].ref_price), ("target_hit", 101.0))
        res, _ = run({("SPY", "10:00"): Enter(99.0, "t"), ("SPY", "11:00"): Exit("done")},
                     day_bars())
        self.assertEqual((res.trades[0].exit_reason, res.trades[0].exit_ts),
                         ("done", at(DAY, "11:05")))

    def test_no_entries_in_the_last_30_minutes(self):
        res, _ = run({("SPY", "15:30"): Enter(99.0, "late")}, day_bars())
        self.assertFalse(res.fills)
        self.assertIn("too_close_to_the_close", res.signals[-1].reason)

    def test_sizing_obeys_risk_budget_and_exposure_cap(self):
        res, _ = run({("SPY", "10:00"): Enter(90.0, "wide")}, day_bars())
        px = res.fills[0].fill_price
        shares = res.fills[0].shares
        self.assertLessEqual(shares * (px - 90.0), 500.0 + 1e-6)  # 0.5% of $100k
        res, _ = run({("SPY", "10:00"): Enter(99.9, "tight")}, day_bars())
        self.assertLessEqual(res.fills[0].shares * res.fills[0].fill_price, 30_000.0 + 1e-6)
        self.assertGreater(res.fills[0].shares * res.fills[0].fill_price, 29_000.0)

    def test_daily_loss_halt_blocks_new_entries_but_manages_positions(self):
        risk = RiskParams(intraday_loss_halt_pct=0.001)
        bars = day_bars(overrides={"10:30": (100.0, 100.0, 98.0, 98.5)})
        plan = {("SPY", "10:00"): Enter(98.6, "a"), ("SPY", "12:00"): Enter(99.0, "b")}
        res, _ = run(plan, bars, risk=risk)
        self.assertEqual(len(res.trades), 1)
        self.assertTrue(any("daily_loss_halt" in g.reason for g in res.signals))
        self.assertTrue(res.halted)

    def test_pause_applies_from_the_moment_it_is_set(self):
        plan = {("SPY", "10:00"): Enter(99.0, "a"), ("SPY", "10:30"): Exit("x"),
                ("SPY", "13:00"): Enter(99.0, "b")}
        res, _ = run(plan, day_bars(), paused=lambda ts: ts >= at(DAY, "12:00"))
        self.assertEqual(len(res.trades), 1)
        self.assertIn("paused_by_owner", res.signals[-1].reason)

    def test_today_so_far_does_not_mutate_and_matches_the_final_run(self):
        plan = {("SPY", "10:00"): Enter(99.0, "a"), ("SPY", "14:00"): Exit("x")}
        bars = day_bars(overrides={"10:05": (100.0, 100.5, 99.9, 100.4)})
        st = BotState.initial(100_000.0, DAY)
        before = copy.deepcopy(st)
        partial, _ = run(plan, bars[:30], state=st, final=False)
        self.assertEqual(st, before)
        self.assertIn("SPY", partial.open_positions)
        final, _ = run(plan, bars, state=st)
        self.assertEqual(final.fills[: len(partial.fills)], partial.fills)
        self.assertEqual(final.signals[: len(partial.signals)], partial.signals)

    def test_patchy_data_still_trades_and_only_a_missing_symbol_sits_out(self):
        bars = day_bars()[::2]  # no peeking at how complete the whole day will be
        res, _ = run({("SPY", "10:00"): Enter(99.0, "a")}, bars)
        self.assertNotIn("SPY", res.excluded)
        res, _ = run({("SPY", "10:00"): Enter(99.0, "a")}, [])
        self.assertIn("SPY", res.excluded)
        self.assertFalse(res.fills)

    def test_symbol_order_cannot_change_results(self):
        """Reviewer's case: one fund's crash must stop another fund's same-bar entry."""
        risk = RiskParams(intraday_loss_halt_pct=0.002)
        held = Scripted({("SPY", "09:45"): Enter(90.0, "hold"), ("QQQ", "10:00"): Enter(95.0, "q")})
        spy = day_bars(overrides={"10:00": (100.0, 100.0, 92.0, 92.5)})
        qqq = day_bars()
        out = []
        for order in (["QQQ", "SPY"], ["SPY", "QQQ"]):
            core = IntradayCore(held, order, risk)
            res = core.run_day(BotState.initial(100_000.0, DAY), DAY, {"SPY": spy, "QQQ": qqq},
                               {"SPY": 100.0, "QQQ": 100.0})
            out.append((res.fills, res.signals, res.end_equity))
            self.assertFalse([f for f in res.fills if f.symbol == "QQQ"])
        self.assertEqual(out[0][2], out[1][2])
        self.assertEqual(sorted(map(repr, out[0][0])), sorted(map(repr, out[1][0])))

    def test_same_moment_fills_do_not_depend_on_symbol_order(self):
        """Reviewer's second case: a stop later in the bar must not free room for a fill at
        that bar's open, and same-open entries are decided by a fixed rule."""
        plan = {("SPY", "09:45"): Enter(99.5, "s"), ("QQQ", "10:25"): Enter(95.0, "q"),
                ("IWM", "10:25"): Enter(95.0, "i")}
        spy = day_bars(overrides={"10:30": (100.0, 100.0, 99.0, 99.2)})
        data = {"SPY": spy, "QQQ": day_bars(), "IWM": day_bars()}
        ends = []
        for order in (["SPY", "QQQ", "IWM"], ["IWM", "QQQ", "SPY"], ["QQQ", "SPY", "IWM"]):
            core = IntradayCore(Scripted(plan), order, RiskParams())
            res = core.run_day(BotState.initial(100_000.0, DAY), DAY, data,
                               dict.fromkeys(order, 100.0))
            ends.append((res.end_equity, sorted(map(repr, res.fills))))
        self.assertEqual(ends[0], ends[1])
        self.assertEqual(ends[0], ends[2])

    def test_short_response_never_wipes_better_stored_bars(self):
        store = InMemoryStore()
        full = day_bars()
        store.replace_ibars("SPY", DAY, DAY, full)
        store.replace_ibars("SPY", DAY, DAY, full[:10])
        store.replace_ibars("SPY", DAY, DAY, [])
        self.assertEqual(len(store.ibars("SPY", DAY)), 78)

    def test_halt_is_rechecked_when_a_pending_order_would_fill(self):
        calls = []

        def paused(ts):  # not paused when the order is placed, paused by the time it fills
            calls.append(ts)
            return len(calls) > 1

        res, _ = run({("SPY", "10:00"): Enter(99.0, "a")}, day_bars(), paused=paused)
        self.assertFalse(res.fills)
        self.assertEqual((res.signals[-1].outcome, res.signals[-1].reason), ("cancelled", "a; paused_by_owner"))

    def test_order_cancelled_when_the_next_bar_is_missing(self):
        bars = [b for b in day_bars() if b.ny_time not in ("10:05", "10:10")]
        res, _ = run({("SPY", "10:00"): Enter(99.0, "a")}, bars)
        self.assertFalse(res.fills)
        self.assertEqual(res.signals[-1].outcome, "cancelled")

    def test_early_close_day_is_flat_by_its_close(self):
        d = date(2024, 11, 29)  # day after Thanksgiving: closes 1 pm
        core = IntradayCore(Scripted({("SPY", "10:00"): Enter(99.0, "a")}), ["SPY"], RiskParams())
        st = BotState.initial(100_000.0, d)
        res = core.run_day(st, d, {"SPY": day_bars(d)}, {"SPY": 100.0})
        self.assertEqual(res.fills[-1].ts.astimezone(NY).strftime("%H:%M"), "12:55")

    def test_peak_halt_latches_live_and_auto_resumes_in_tests(self):
        st = BotState.initial(100_000.0, DAY)
        st.peak_equity = 120_000.0  # already 16% below the best day
        res, _ = run({("SPY", "10:00"): Enter(99.0, "a")}, day_bars(), state=st)
        self.assertFalse(res.fills)
        self.assertTrue(st.halt_latched)
        core = IntradayCore(Scripted({}), ["SPY"], RiskParams(), auto_resume_days=2)
        for d in trading_days(date(2024, 3, 6), date(2024, 3, 8)):
            core.run_day(st, d, {"SPY": day_bars(d)}, {"SPY": 100.0})
        self.assertFalse(st.halt_latched)
        self.assertEqual(st.peak_equity, st.cash)

    def test_state_round_trips(self):
        st = BotState.initial(100_000.0, DAY)
        st.last_day = DAY
        self.assertEqual(BotState.from_dict(st.to_dict() | {"future_field": 1}), st)


def ctx_for(bars, sym="SPY", prev=None, pos=None, entries=0):
    s = session(bars[0].day)
    return StrategyContext(sym, bars[0].day, bars, prev, pos, entries, s.open_utc, s.close_utc)


class StrategyTests(unittest.TestCase):
    def test_registry_has_four_plain_english_strategies(self):
        self.assertEqual(set(REGISTRY), {"orb", "gap", "vwap_dip", "vwap_trend"})
        for cls in REGISTRY.values():
            self.assertTrue(cls.name and cls.summary and cls.rules)
        with self.assertRaises(ValueError):
            OpeningRangeBreakout(nonsense=1)

    def test_opening_range_breakout(self):
        o = {"09:45": (100.0, 101.0, 99.5, 100.5), "10:05": (100.6, 101.4, 100.5, 101.3)}
        bars = day_bars(overrides=o)
        s = OpeningRangeBreakout()
        signals = [s.on_bar(ctx_for(bars[: i + 1])) for i in range(len(bars))]
        hits = [(bars[i].ny_time, x) for i, x in enumerate(signals) if x]
        self.assertEqual(hits[0][0], "10:05")
        self.assertEqual(hits[0][1].stop, 99.5)
        self.assertEqual(len(hits), 1)  # fresh breakouts only
        self.assertIsNone(s.on_bar(ctx_for(bars[:25], entries=1)))
        # a missing opening-range bar must not hide a breakout on the first bar after it
        gappy = [b for b in bars if b.ny_time != "09:40"]
        o2 = {"10:00": (100.6, 101.4, 100.5, 101.3)}
        gappy = [b for b in day_bars(overrides={**o, **o2}) if b.ny_time != "09:40"]
        hit = next(b.ny_time for i, b in enumerate(gappy)
                   if s.on_bar(ctx_for(gappy[: i + 1])))
        self.assertEqual(hit, "10:00")

    def test_gap_fill(self):
        bars = day_bars(price=99.5)
        sig = GapFill().on_bar(ctx_for(bars[:1], prev=100.0))
        self.assertEqual((sig.target, sig.stop), (100.0, 99.0))
        self.assertIsNone(GapFill().on_bar(ctx_for(bars[:1], prev=99.6)))  # gap too small
        self.assertIsNone(GapFill().on_bar(ctx_for(bars[:1], prev=105.0)))  # too big
        self.assertIsNone(GapFill().on_bar(ctx_for(bars[1:2], prev=100.0)))  # 9:30 bar missing
        late = [b for b in bars if b.ny_time <= "13:00"]
        self.assertIsInstance(GapFill().on_bar(ctx_for(late, prev=100.0, pos=99.5)), Exit)

    def test_vwap_dip_and_trend(self):
        o = {f"{h:02d}:{m:02d}": (100.0, 100.3, 99.7, 100.0 + (0.2 if m % 10 else -0.2))
             for h in range(9, 11) for m in range(0, 60, 5)}
        o["10:35"] = (99.4, 99.45, 98.6, 99.2)  # flush well below the day's average, closes off low
        bars = [b for b in day_bars(overrides=o) if b.ny_time <= "10:35"]
        sig = VwapDip().on_bar(ctx_for(bars))
        self.assertIsInstance(sig, Enter)
        self.assertLess(sig.stop, 99.2)
        self.assertIsInstance(VwapDip().on_bar(ctx_for(day_bars()[:20], pos=99.0)), Exit)
        up = {f"{9 + (i + 6) // 12:02d}:{((i + 6) % 12) * 5:02d}": (100 + i * 0.1, 100 + i * 0.1 + 0.15,
              100 + i * 0.1 - 0.05, 100 + i * 0.1 + 0.1) for i in range(14)}
        bars = [b for b in day_bars(overrides=up) if b.ny_time <= "10:35"]
        ctx = ctx_for(bars)
        vw = ctx.vwap_series()[-1]
        last = bars[-1]
        bars[-1] = IBar(last.ts, vw + 0.02, vw + 0.2, vw - 0.01, vw + 0.1, 1000.0)
        self.assertIsInstance(VwapTrend().on_bar(ctx_for(bars)), Enter)


class ParsingTests(unittest.TestCase):
    def test_start_labelled_rows(self):
        rows = [{"date": "2024-03-05T14:30:00.000Z", "open": 1, "high": 2, "low": 0.5, "close": 1.5,
                 "volume": 10},
                {"date": "2024-03-05T13:00:00.000Z", "open": 1, "high": 2, "low": 0.5, "close": 1.5},
                {"date": "2024-03-05T16:35:00-05:00", "open": 1, "high": 2, "low": 0.5, "close": 1.5},
                {"date": "2024-03-05T14:40:00", "open": 1, "high": 2, "low": 0.5, "close": 1.5},
                {"date": "2024-03-05T14:45:00Z", "open": 1, "high": 0.9, "low": 0.5, "close": 1.5}]
        bars, rejected = parse_iex_rows(rows)
        self.assertEqual([b.ny_time for b in bars], ["09:30"])  # pre-market and after-hours dropped
        self.assertEqual(rejected, 2)  # naive timestamp + high below open

    def test_end_labelled_rows_are_shifted(self):
        s = session(DAY)
        rows = [{"date": (s.open_utc + (i + 1) * BAR).isoformat(), "open": 1, "high": 1, "low": 1,
                 "close": 1} for i in range(78)]
        bars, _ = parse_iex_rows(rows)
        self.assertEqual((bars[0].ny_time, bars[-1].ny_time, len(bars)), ("09:30", "15:55", 78))

    def test_convention_detected_once_from_a_complete_day(self):
        from tradinglab.marketdata.intraday import detect_end_labelled

        s = session(DAY)
        end = [{"date": (s.open_utc + (i + 1) * BAR).isoformat()} for i in range(78)]
        start = [{"date": (s.open_utc + i * BAR).isoformat()} for i in range(78)]
        self.assertTrue(detect_end_labelled(end))
        self.assertFalse(detect_end_labelled(start))
        self.assertIsNone(detect_end_labelled(start[5:40]))  # a partial day can't tell
        partial = [{"date": r["date"], "open": 1, "high": 1, "low": 1, "close": 1} for r in end[:20]]
        bars, _ = parse_iex_rows(partial, end_labelled=True)
        self.assertEqual(bars[0].ny_time, "09:30")

    def test_fetch_range_splits_a_capped_response(self):
        calls = []

        def fetch(sym, a, b):
            calls.append((a, b))
            n = len(trading_days(a, b)) * 78
            if n > 1000:
                return [{"date": "2024-01-02T14:30:00Z", "open": 1, "high": 1, "low": 1,
                         "close": 1}] * POINT_CAP
            return [{"date": (session(d).open_utc + i * BAR).isoformat(), "open": 1, "high": 1,
                     "low": 1, "close": 1} for d in trading_days(a, b) for i in range(78)]

        bars, _, requests = fetch_range(fetch, "SPY", date(2024, 1, 2), date(2024, 1, 31))
        self.assertEqual(len(bars), len(trading_days(date(2024, 1, 2), date(2024, 1, 31))) * 78)
        self.assertGreater(requests, 2)


class FakeFeed:
    """Synthetic provider for the runner: rows for any symbol/day range; can fail."""

    def __init__(self, end=date(2024, 6, 28)):
        self.data = {}
        self.closes = {}
        for s, p in zip(SYMS, (470.0, 400.0, 200.0), strict=True):
            self.data[s], self.closes[s] = synthetic_intraday(s, date(2024, 1, 2), end,
                                                              start_price=p)
        self.calls = 0
        self.fail = False
        self.until: datetime | None = None  # bars after this moment are not published yet
        self.forming = False  # also publish the bar that is still forming at `until`
        self.end_labelled = False
        self.drop: set = set()  # (symbol, day) with no data

    def rows(self, sym, a, b):
        self.calls += 1
        if self.fail:
            raise ConnectionError("provider down")
        out = []
        for d in trading_days(a, b):
            if (sym, d) in self.drop:
                continue
            for bar in self.data[sym].get(d, []):
                cut = bar.ts if self.forming else bar.end
                if self.until is None or cut <= self.until:
                    ts = bar.end if self.end_labelled else bar.ts
                    out.append({"date": ts.isoformat(), "open": bar.open, "high": bar.high,
                                "low": bar.low, "close": bar.close, "volume": bar.volume})
        return out


def runner_with(feed, store=None, bots=DEFAULT_BOTS):
    store = store or InMemoryStore()
    from tradinglab.marketdata.bars import Bar

    for s in SYMS:
        store.save_prices(s, [Bar(d, c, c, c, c, 0.0) for d, c in feed.closes[s].items()])
    return IntradayRunner(settings(), store, feed.rows, bots), store


class RunnerTests(unittest.TestCase):
    def test_bots_start_on_the_next_session_and_commit_after_the_close(self):
        feed = FakeFeed()
        r, store = runner_with(feed)
        sat = datetime(2024, 3, 2, 18, tzinfo=UTC)
        self.assertEqual(r.run(sat).status, "idle")
        self.assertEqual(first_session_after(sat), date(2024, 3, 4))
        self.assertEqual(store.load_bot("orb").start_day, date(2024, 3, 4))
        self.assertEqual(store.load_bot("orb").cash, 1_000_000.0)
        mid = at(date(2024, 3, 4), "11:02")
        feed.until = mid
        rep = r.run(mid)
        self.assertEqual((rep.status, rep.requests), ("refreshed", 6))  # Friday once + today
        self.assertEqual(r.run(mid + timedelta(minutes=1)).requests, 0)  # not stale yet
        projections = {b.id: r.today(b, mid) for b in DEFAULT_BOTS}
        self.assertTrue(all(p is not None and not p.final for p in projections.values()))
        after = at(date(2024, 3, 4), "16:25")
        feed.until = None
        rep = r.run(after)
        self.assertEqual(rep.status, "committed")
        self.assertEqual(sorted(rep.committed), sorted((b.id, date(2024, 3, 4)) for b in DEFAULT_BOTS))
        self.assertEqual(r.run(after + timedelta(minutes=5)).committed, [])  # no double commit
        for b in DEFAULT_BOTS:
            days = store.bot_days(b.id)
            self.assertEqual(len(days), 1)
            self.assertAlmostEqual(store.load_bot(b.id).cash, days[0].end_equity)
            # what the dashboard showed mid-day is a prefix of what was committed
            committed = store.bot_fills(b.id, date(2024, 3, 4))
            self.assertEqual(committed[: len(projections[b.id].fills)], projections[b.id].fills)

    def test_missed_days_are_caught_up_in_order_and_errors_commit_nothing(self):
        feed = FakeFeed()
        r, store = runner_with(feed)
        r.run(datetime(2024, 3, 2, 18, tzinfo=UTC))
        feed.fail = True
        rep = r.run(at(date(2024, 3, 6), "18:00"))
        self.assertEqual(rep.status, "data_error")
        self.assertEqual(store.bot_days("gap"), [])
        feed.fail = False
        rep = r.run(at(date(2024, 3, 7), "18:00"))
        self.assertEqual([d.day for d in store.bot_days("gap")],
                         trading_days(date(2024, 3, 4), date(2024, 3, 7)))

    def test_live_runner_matches_a_plain_replay(self):
        feed = FakeFeed()
        r, store = runner_with(feed, bots=[BotSpec("orb", "orb")])
        r.run(datetime(2024, 3, 2, 18, tzinfo=UTC))
        for d in trading_days(date(2024, 3, 4), date(2024, 3, 15)):
            r.run(at(d, "16:30"))
        core = IntradayCore(OpeningRangeBreakout(), SYMS, r.risk, INTRADAY_COSTS)
        st = BotState.initial(r.capital, date(2024, 3, 4))
        for d in trading_days(date(2024, 3, 4), date(2024, 3, 15)):
            core.run_day(st, d, {s: feed.data[s][d] for s in SYMS}, r.prev_closes(d))
        self.assertAlmostEqual(store.load_bot("orb").cash, st.cash)

    def test_forming_bars_are_ignored_and_end_labelled_feed_is_handled(self):
        feed = FakeFeed()
        feed.end_labelled = True
        feed.forming = True
        r, store = runner_with(feed)
        r.run(datetime(2024, 3, 2, 18, tzinfo=UTC))
        mid = at(date(2024, 3, 4), "10:02")
        feed.until = mid
        r.run(mid)
        self.assertTrue(store.get_flag("iex_end_labelled"))  # learned from Friday's full day
        bars = store.ibars("SPY", date(2024, 3, 4))
        self.assertEqual((bars[0].ny_time, bars[-1].ny_time), ("09:30", "09:55"))
        feed.until = None
        r.run(at(date(2024, 3, 4), "16:30"))
        self.assertEqual(len(store.ibars("SPY", date(2024, 3, 4))), 78)  # restamped, no duplicates
        replay = IntradayCore(OpeningRangeBreakout(), SYMS, r.risk, INTRADAY_COSTS)
        st = BotState.initial(r.capital, date(2024, 3, 4))
        replay.run_day(st, date(2024, 3, 4), {s: feed.data[s][date(2024, 3, 4)] for s in SYMS},
                       r.prev_closes(date(2024, 3, 4)))
        self.assertAlmostEqual(store.load_bot("orb").cash, st.cash)

    def test_incomplete_data_after_the_close_waits_then_gives_up_gracefully(self):
        feed = FakeFeed()
        r, store = runner_with(feed)
        r.run(datetime(2024, 3, 2, 18, tzinfo=UTC))
        feed.drop = {("IWM", date(2024, 3, 4))}
        rep = r.run(at(date(2024, 3, 4), "16:30"))
        self.assertEqual(rep.status, "waiting")
        self.assertEqual(store.bot_days("orb"), [])
        rep = r.run(at(date(2024, 3, 4), "19:30"))  # 3+ hours later: take what exists
        self.assertEqual(rep.status, "committed")
        self.assertIn("IWM", store.bot_days("orb")[0].excluded)

    def test_today_hidden_until_earlier_days_are_committed(self):
        feed = FakeFeed()
        r, store = runner_with(feed)
        r.run(datetime(2024, 3, 2, 18, tzinfo=UTC))
        feed.until = at(date(2024, 3, 5), "11:00")
        self.assertIsNone(r.today(DEFAULT_BOTS[0], at(date(2024, 3, 5), "11:00")))

    def test_settings_change_before_any_trading_restarts_the_bot(self):
        feed = FakeFeed()
        r, store = runner_with(feed, bots=[BotSpec("gap", "gap")])
        r.run(datetime(2024, 3, 2, 18, tzinfo=UTC))
        r2 = IntradayRunner(settings(), store, feed.rows, [BotSpec("gap", "gap", {"min_gap": 0.004})])
        r2.run(datetime(2024, 3, 2, 19, tzinfo=UTC))
        self.assertEqual(store.load_bot("gap").config_fingerprint, r2.fingerprint(r2.bots[0]))
        self.assertTrue(any(e.kind == "bot_started" for e in store.events()))

    def test_pause_flag_history_is_respected_at_commit(self):
        feed = FakeFeed()
        r, store = runner_with(feed, bots=[BotSpec("vwap_trend", "vwap_trend")])
        r.run(datetime(2024, 3, 2, 18, tzinfo=UTC))
        store.set_flag("paused", True, at(date(2024, 3, 4), "09:00"))
        for d in trading_days(date(2024, 3, 4), date(2024, 3, 8)):
            r.run(at(d, "16:30"))
        self.assertEqual(store.bot_trades("vwap_trend"), [])


class ResearchTests(unittest.TestCase):
    def test_backfill_oldest_first_within_budget_then_history_test_with_sealed_final(self):
        feed = FakeFeed(end=date(2025, 2, 28))
        r, store = runner_with(feed)
        start, end = date(2024, 1, 2), date(2025, 2, 28)
        st = backfill_step(store, feed.rows, SYMS, end, budget=5, start=start)
        self.assertEqual(st.requests, 5)
        first = min(store.fetches("SPY", start, end))
        self.assertEqual(first, start)
        while not st.done:
            st = backfill_step(store, feed.rows, SYMS, end, budget=50, start=start)
        self.assertEqual(backfill_status(store, SYMS, end, start).fraction, 1.0)
        out = run_history_test(store, [BotSpec("orb", "orb")], SYMS, r.risk, 100_000.0, end=end,
                               start=start, unlocked=lambda _b: False)
        w = out["orb"]["windows"]
        self.assertTrue(w["test"]["sealed"])
        self.assertGreater(w["validation"]["days"], 200)
        self.assertNotIn("total_return", w["test"])
        self.assertEqual(store.backtest("orb")["bot"], "orb")
        opened = run_history_test(store, [BotSpec("orb", "orb")], SYMS, r.risk, 100_000.0, end=end,
                                  start=start, unlocked=lambda _b: True)
        self.assertGreater(opened["orb"]["windows"]["test"]["days"], 30)
        # the validation result does not depend on whether the test was opened
        self.assertEqual(w["validation"], opened["orb"]["windows"]["validation"])


if __name__ == "__main__":
    unittest.main()
