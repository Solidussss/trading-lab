"""Strategy rules, splits/leakage guards, walk-forward, metrics, data cache, data client."""

import math
import tempfile
import unittest
from datetime import date
from pathlib import Path

from tests.helpers import FAKE_TIINGO_TOKEN
from tradinglab.marketdata.tiingo import RateLimiter
from tradinglab.marketdata.bars import Bar, BarSeries, DatasetMeta, load_series, save_series
from tradinglab.marketdata.calendar import trading_days
from tradinglab.research.backtest import BacktestConfig, EquityPoint
from tradinglab.research.metrics import (
    attribution,
    buy_and_hold,
    compute_metrics,
    drawdown_episodes,
    max_drawdown,
)
from tradinglab.research.splits import (
    LeakageError,
    TestVault,
    Window,
    grid_search,
    time_splits,
    walk_forward,
)
from tradinglab.research.synthetic import synthetic_series
from tradinglab.strategies.base import Action, PositionState, StrategySpec
from tradinglab.strategies.trend_following import TrendFollowing


def ramp(days, start, step):
    return [Bar(d, start + i * step, start + i * step + 0.5, start + i * step - 0.5,
                start + i * step, 1e6) for i, d in enumerate(days)]


class TrendFollowingTests(unittest.TestCase):
    days = trading_days(date(2020, 1, 1), date(2021, 6, 30))
    strat = TrendFollowing.create(fast=10, slow=30, atr_period=5, atr_mult=2.0)

    def test_warmup(self):
        s = self.strat.evaluate(ramp(self.days[:20], 100, 1), PositionState(False))
        self.assertEqual((s.action, s.reason), (Action.NO_TRADE, "warmup"))

    def test_enters_in_uptrend_with_stop_below_close(self):
        hist = ramp(self.days[:60], 100, 1)
        s = self.strat.evaluate(hist, PositionState(False))
        self.assertEqual(s.action, Action.ENTER_LONG)
        self.assertLess(s.stop_price, hist[-1].close)

    def test_no_entry_in_downtrend(self):
        s = self.strat.evaluate(ramp(self.days[:60], 200, -1), PositionState(False))
        self.assertEqual(s.action, Action.NO_TRADE)

    def test_exit_when_trend_breaks(self):
        s = self.strat.evaluate(ramp(self.days[:60], 200, -1), PositionState(True, 150, 140, 5, 160))
        self.assertEqual(s.action, Action.EXIT)

    def test_trailing_stop_only_ratchets_up(self):
        hist = ramp(self.days[:60], 100, 1)
        high_stop = 1e6 - 1  # absurdly high existing stop must not be lowered
        s = self.strat.evaluate(hist, PositionState(True, 120, high_stop, 10, hist[-1].close))
        self.assertEqual(s.action, Action.HOLD)
        self.assertEqual(s.stop_price, high_stop)
        s2 = self.strat.evaluate(hist, PositionState(True, 120, 1.0, 10, hist[-1].close))
        self.assertGreater(s2.stop_price, 1.0)

    def test_invalid_params(self):
        with self.assertRaises(ValueError):
            TrendFollowing.create(fast=50, slow=20)

    def test_equivalent_params_share_a_hash(self):
        # Auditor probe: fast=50 vs fast=50.0 produced different hashes -> test re-use loophole.
        self.assertEqual(TrendFollowing.create(fast=50).spec.spec_hash,
                         TrendFollowing.create(fast=50.0).spec.spec_hash)
        self.assertNotEqual(TrendFollowing.create(atr_mult=3.0).spec.spec_hash,
                            TrendFollowing.create(atr_mult=3.5).spec.spec_hash)

    def test_spec_hash_tracks_params(self):
        a = TrendFollowing.create().spec
        b = TrendFollowing.create(fast=40).spec
        self.assertNotEqual(a.spec_hash, b.spec_hash)
        self.assertEqual(a.spec_hash, TrendFollowing.create().spec.spec_hash)
        self.assertEqual(StrategySpec("x", "1", {"a": 1, "b": 2}).spec_hash,
                         StrategySpec("x", "1", {"b": 2, "a": 1}).spec_hash)


class SplitAndLeakageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.syms = ["SPY", "QQQ"]
        cls.data = {s: synthetic_series(s, date(2016, 1, 1), date(2025, 12, 31), seed=11)
                    for s in cls.syms}
        cls.splits = time_splits(date(2017, 1, 3), date(2025, 12, 31))
        cls.base = BacktestConfig(start=date(2017, 1, 3), end=date(2025, 12, 31))
        cls.grid = {"fast": [20, 50], "slow": [100, 200], "atr_period": [20], "atr_mult": [3.0]}

    def test_splits_ordered_contiguous(self):
        sp = self.splits
        self.assertLess(sp.train.end, sp.validation.start)
        self.assertLess(sp.validation.end, sp.test.start)
        self.assertFalse(sp.train.overlaps(sp.test))
        all_days = trading_days(date(2017, 1, 3), date(2025, 12, 31))
        n = lambda w: len(trading_days(w.start, w.end))  # noqa: E731
        self.assertEqual(n(sp.train) + n(sp.validation) + n(sp.test), len(all_days))

    def test_grid_search_refuses_test_window(self):
        with self.assertRaises(LeakageError):
            grid_search(TrendFollowing.create, self.grid, self.data, self.splits.test, self.base,
                        forbidden=self.splits.test, symbols=self.syms)
        overlapping = Window("x", self.splits.validation.start, self.splits.test.start)
        with self.assertRaises(LeakageError):
            grid_search(TrendFollowing.create, self.grid, self.data, overlapping, self.base,
                        forbidden=self.splits.test, symbols=self.syms)

    def test_grid_search_ranks_deterministically(self):
        r1 = grid_search(TrendFollowing.create, self.grid, self.data, self.splits.train,
                         self.base, forbidden=self.splits.test, symbols=self.syms)
        r2 = grid_search(TrendFollowing.create, self.grid, self.data, self.splits.train,
                         self.base, forbidden=self.splits.test, symbols=self.syms)
        self.assertEqual([r.label for r in r1], [r.label for r in r2])
        self.assertEqual(len(r1), 4)
        self.assertGreaterEqual(r1[0].score, r1[-1].score)

    def test_walk_forward_never_touches_test_and_is_sequential(self):
        wf = walk_forward(TrendFollowing.create, self.grid, self.data, self.splits.train.start,
                          self.splits.validation.end, self.base, train_days=504, oos_days=126,
                          forbidden=self.splits.test, symbols=self.syms)
        self.assertGreater(len(wf.segments), 2)
        for a, b in zip(wf.segments, wf.segments[1:], strict=False):
            self.assertLess(a.train.end, a.oos.start)  # choose on past, trade on future
            self.assertLess(a.oos.end, b.oos.start)
        self.assertTrue(all(s.oos.end < self.splits.test.start for s in wf.segments))
        days = [p.day for p in wf.oos_curve]
        self.assertEqual(days, sorted(set(days)))
        self.assertTrue(0 < wf.param_stability <= 1)

    def test_walk_forward_refuses_test_range(self):
        with self.assertRaises(LeakageError):
            walk_forward(TrendFollowing.create, self.grid, self.data, self.splits.train.start,
                         self.splits.test.end, self.base, train_days=504, oos_days=126,
                         forbidden=self.splits.test, symbols=self.syms)

    def test_test_vault_allows_one_evaluation_per_spec(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = TestVault(Path(tmp) / "ledger.jsonl", self.splits.test)
            s = TrendFollowing.create()
            with self.assertRaises(ValueError):
                vault.evaluate(s, self.data, self.base, reason=" ", symbols=self.syms)
            vault.evaluate(s, self.data, self.base, reason="final check v1", symbols=self.syms)
            with self.assertRaises(LeakageError):
                vault.evaluate(TrendFollowing.create(), self.data, self.base, reason="again",
                               symbols=self.syms)
            vault.evaluate(TrendFollowing.create(fast=40), self.data, self.base,
                           reason="new version", symbols=self.syms)
            lines = (Path(tmp) / "ledger.jsonl").read_text().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertIn("data_sha256", lines[0])

    def test_vault_blocks_float_alias_and_revised_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = TestVault(Path(tmp) / "ledger.jsonl", self.splits.test)
            vault.evaluate(TrendFollowing.create(fast=50), self.data, self.base, reason="v1",
                           symbols=self.syms)
            with self.assertRaises(LeakageError):  # same behaviour, float spelling
                vault.evaluate(TrendFollowing.create(fast=50.0), self.data, self.base,
                               reason="alias", symbols=self.syms)
            revised = {s: synthetic_series(s, date(2016, 1, 1), date(2025, 12, 31), seed=99)
                       for s in self.syms}
            with self.assertRaises(LeakageError):  # same spec, "revised" data
                vault.evaluate(TrendFollowing.create(fast=50), revised, self.base,
                               reason="new data", symbols=self.syms)
            # a vault opened from a different working directory sees the same ledger
            other = TestVault(Path(tmp) / "." / "ledger.jsonl", self.splits.test)
            self.assertTrue(other.already_evaluated(TrendFollowing.create().spec.spec_hash))


class MetricsTests(unittest.TestCase):
    def test_max_drawdown(self):
        dd, length = max_drawdown([100, 120, 90, 95, 130, 117])
        self.assertAlmostEqual(dd, 0.25)
        self.assertEqual(length, 2)

    def test_drawdown_episodes(self):
        days = trading_days(date(2024, 1, 2), date(2024, 1, 12))
        eq = [100, 110, 99, 105, 111, 100, 101, 112, 112]
        curve = [EquityPoint(d, e, e, 0, 0, False) for d, e in zip(days, eq, strict=False)]
        eps = drawdown_episodes(curve, min_depth=0.05)
        self.assertEqual(len(eps), 2)
        self.assertAlmostEqual(eps[0].depth, 0.1)

    def test_metrics_and_benchmarks_on_synthetic(self):
        from tradinglab.research.backtest import Backtester

        data = {"SPY": synthetic_series("SPY", date(2016, 1, 1), date(2020, 12, 31), seed=5)}
        cfg = BacktestConfig(start=date(2017, 1, 3), end=date(2020, 12, 31))
        res = Backtester(TrendFollowing.create(), data, cfg).run()
        m = compute_metrics(res)
        self.assertEqual(m.trades, len(res.trades))
        self.assertTrue(0 <= m.win_rate <= 1)
        self.assertTrue(0 <= m.avg_exposure <= 0.35)
        self.assertTrue(math.isfinite(m.sharpe))
        bh = buy_and_hold(data["SPY"], cfg.start, cfg.end, 10_000)
        bh30 = buy_and_hold(data["SPY"], cfg.start, cfg.end, 10_000, fraction=0.3)
        self.assertEqual(len(bh), len(res.equity_curve))
        self.assertAlmostEqual(bh30[0].exposure, 0.3, delta=0.02)
        attr = attribution(res.trades)
        self.assertAlmostEqual(sum(attr["by_symbol"].values()), sum(t.pnl for t in res.trades))


class DataCacheTests(unittest.TestCase):
    def test_validation_flags_bad_rows(self):
        days = trading_days(date(2026, 3, 2), date(2026, 3, 6))
        bars = [Bar(days[0], 10, 11, 9, 10, 1), Bar(days[0], 10, 11, 9, 10, 1),  # duplicate
                Bar(days[1], 10, 9, 8, 10, 1),  # high < open
                Bar(days[2], -1, 11, 9, 10, 1),  # non-positive
                Bar(days[4], 10, 11, 9, float("nan"), 1)]  # non-finite
        s = BarSeries("SPY", bars)
        kinds = sorted(i.kind for i in s.issues)
        self.assertEqual(len(s), 1)
        for k in ("duplicate", "bad_ohlc", "non_positive", "non_finite"):
            self.assertIn(k, kinds)

    def test_history_through_excludes_future(self):
        days = trading_days(date(2026, 3, 2), date(2026, 3, 13))
        s = BarSeries("SPY", ramp(days, 100, 1))
        self.assertEqual(s.history_through(days[3])[-1].day, days[3])
        self.assertEqual(len(s.history_through(date(2026, 3, 7))), 5)  # Saturday -> Friday

    def test_csv_round_trip_and_tamper_detection(self):
        s = synthetic_series("SPY", date(2024, 1, 1), date(2024, 3, 31), seed=1)
        meta = DatasetMeta("SPY", "synthetic", "sip", "split", "2024-01-01", "2024-03-31",
                           "2026-10-10T00:00:00+00:00", 0)
        with tempfile.TemporaryDirectory() as tmp:
            path = save_series(s, Path(tmp), meta)
            loaded, m2 = load_series(path)
            self.assertEqual(loaded.bars, s.bars)
            self.assertEqual(m2.rows, len(s))
            path.write_text(path.read_text().replace(",", ";", 1))
            with self.assertRaises(ValueError):
                load_series(path)


class TiingoClientTests(unittest.TestCase):
    ROW = {"date": "2026-10-09T00:00:00.000Z", "open": 670.1, "high": 672.5, "low": 668.0,
           "close": 671.2, "volume": 55000000, "adjClose": 671.2, "divCash": 0.0,
           "splitFactor": 1.0}

    def client(self, responses, calls=None):
        from tradinglab.config import Secret
        from tradinglab.marketdata.tiingo import TiingoClient

        calls = calls if calls is not None else []
        it = iter(responses)

        def http_get(url, headers, timeout):
            calls.append((url, headers))
            r = next(it)
            if isinstance(r, Exception):
                raise r
            return r

        return TiingoClient(Secret(FAKE_TIINGO_TOKEN), http_get=http_get,
                            limiter=RateLimiter(1000, 1), sleep=lambda s: None), calls

    def test_parses_rows_with_session_date_and_corporate_actions(self):
        import json as _json

        rows = [self.ROW, {**self.ROW, "date": "2026-09-19T00:00:00.000Z", "divCash": 1.85}]
        rows.sort(key=lambda r: r["date"])
        c, calls = self.client([(200, _json.dumps(rows).encode())])
        s = c.daily("SPY", date(2026, 9, 1), date(2026, 10, 9))
        self.assertEqual([b.day for b in s.bars], [date(2026, 9, 19), date(2026, 10, 9)])
        self.assertEqual(s.bars[0].dividend, 1.85)
        url, headers = calls[0]
        self.assertTrue(url.startswith("https://api.tiingo.com/tiingo/daily/spy/prices?"))
        self.assertIn("startDate=2026-09-01", url)
        self.assertNotIn(FAKE_TIINGO_TOKEN, url)  # token only in the header
        self.assertEqual(headers["Authorization"], f"Token {FAKE_TIINGO_TOKEN}")

    def test_history_floor_2016(self):
        c, calls = self.client([(200, b"[]")])
        c.daily("QQQ", date(2010, 1, 1), date(2016, 6, 1))
        self.assertIn("startDate=2016-01-01", calls[0][0])

    def test_retries_transient_then_succeeds(self):
        import json as _json

        c, calls = self.client([TimeoutError(), (503, b""), (429, b""),
                                (200, _json.dumps([self.ROW]).encode())])
        self.assertEqual(len(c.daily("IWM", date(2026, 10, 1), date(2026, 10, 9))), 1)
        self.assertEqual(len(calls), 4)

    def test_auth_failure_is_not_retried_and_hides_token(self):
        from tradinglab.marketdata.tiingo import DataProviderError

        c, calls = self.client([(401, b'{"detail":"Invalid token"}')])
        with self.assertRaises(DataProviderError) as ctx:
            c.daily("SPY", date(2026, 10, 1), date(2026, 10, 9))
        self.assertEqual(len(calls), 1)
        self.assertNotIn(FAKE_TIINGO_TOKEN, str(ctx.exception))

    def test_gives_up_after_max_attempts(self):
        from tradinglab.marketdata.tiingo import DataProviderError

        c, _ = self.client([(500, b"")] * 4)
        with self.assertRaises(DataProviderError):
            c.daily("SPY", date(2026, 10, 1), date(2026, 10, 9))

    def test_malformed_payloads(self):
        from tradinglab.marketdata.tiingo import DataProviderError

        for body in (b"not json", b'{"detail":"x"}', b'[{"date":"2026-10-09"}]'):
            c, _ = self.client([(200, body)])
            with self.subTest(body=body), self.assertRaises(DataProviderError):
                c.daily("SPY", date(2026, 10, 1), date(2026, 10, 9))

    def test_bad_rows_are_flagged_not_used(self):
        import json as _json

        bad = {**self.ROW, "date": "2026-10-08T00:00:00.000Z", "high": 1.0}  # high < open
        c, _ = self.client([(200, _json.dumps([bad, self.ROW]).encode())])
        s = c.daily("SPY", date(2026, 10, 1), date(2026, 10, 9))
        self.assertEqual(len(s), 1)
        self.assertIn("bad_ohlc", {i.kind for i in s.issues})

    def test_redirects_are_refused(self):
        from tradinglab.marketdata.tiingo import DataProviderError

        c, calls = self.client([(302, b"", {"Location": "http://evil.example/steal"})])
        with self.assertRaises(DataProviderError) as ctx:
            c.daily("SPY", date(2026, 10, 1), date(2026, 10, 9))
        self.assertIn("redirect", str(ctx.exception))
        self.assertEqual(len(calls), 1)

    def test_real_opener_does_not_follow_redirects(self):
        import urllib.request

        from tradinglab.marketdata.tiingo import _NoRedirect

        req = urllib.request.Request("https://api.tiingo.com/x",
                                     headers={"Authorization": "Token secret"})
        self.assertIsNone(_NoRedirect().redirect_request(req, None, 302, "Found", {},
                                                         "http://evil.example/"))

    def test_retry_after_is_honoured_or_ends_the_run(self):
        import json as _json

        from tradinglab.marketdata.tiingo import DataProviderError

        slept = []
        c, _ = self.client([(429, b"", {"Retry-After": "30"}),
                            (200, _json.dumps([self.ROW]).encode())])
        c._sleep = slept.append
        c.daily("SPY", date(2026, 10, 1), date(2026, 10, 9))
        self.assertEqual(slept, [30.0])
        c, calls = self.client([(429, b"", {"Retry-After": "3600"})])
        with self.assertRaises(DataProviderError):
            c.daily("SPY", date(2026, 10, 1), date(2026, 10, 9))
        self.assertEqual(len(calls), 1)  # does not burn the hourly quota retrying

    def test_oversized_response_refused(self):
        from tradinglab.marketdata.tiingo import MAX_RESPONSE_BYTES, DataProviderError

        c, _ = self.client([(200, b"[" + b" " * MAX_RESPONSE_BYTES + b"]")])
        with self.assertRaises(DataProviderError):
            c.daily("SPY", date(2026, 10, 1), date(2026, 10, 9))

    def test_rate_limiter(self):
        t = [0.0]
        slept = []
        lim = RateLimiter(max_calls=3, per_seconds=60, clock=lambda: t[0],
                          sleep=lambda s: (slept.append(s), t.__setitem__(0, t[0] + s)))
        for _ in range(4):
            lim.acquire()
        self.assertEqual(len(slept), 1)
        self.assertGreaterEqual(slept[0], 60)


if __name__ == "__main__":
    unittest.main()
