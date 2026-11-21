"""Stocks in play: the morning scan, the odds replay, the odds-checked strategy and the runner."""

import unittest
from datetime import UTC, date, datetime, timedelta

from tests.helpers import settings
from tests.test_intraday import at, day_bars
from tradinglab.engine.risk import RiskParams
from tradinglab.engine.store import InMemoryStore
from tradinglab.intraday.core import INTRADAY_COSTS, BotState, IntradayCore
from tradinglab.intraday.inplay import IN_PLAY_BOTS, UNIVERSE_ID, InPlayRunner, pick
from tradinglab.intraday.live import ALL_FUND_BOTS
from tradinglab.intraday.replay import replay
from tradinglab.intraday.strategies import ALL_STRATEGIES, REGISTRY, OrbOdds
from tradinglab.intraday.universe import IN_PLAY
from tradinglab.marketdata.calendar import previous_trading_day, session, trading_days
from tradinglab.marketdata.intraday import BAR, IBar
from tradinglab.research.synthetic_intraday import synthetic_intraday

MON = date(2024, 3, 4)
TUE = date(2024, 3, 5)
WED = date(2024, 3, 6)
UNI = ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG")


def quote(sym, prev, opened, last, day=MON, **extra):
    stamp = at(day, "09:40").isoformat()
    return {"ticker": sym.lower(), "prevClose": prev, "open": opened, "tngoLast": last,
            "timestamp": stamp, **extra}


def trending(day, start=100.0, step=0.0, dip=0.0):
    """A session whose every bar moves `step` (as a fraction) and dips `dip` below its open."""
    s = session(day)
    n = int((s.close_utc - s.open_utc) / BAR)
    out, price = [], start
    for i in range(n):
        nxt = price * (1 + step)
        out.append(IBar(s.open_utc + i * BAR, price, max(price, nxt) + 0.01,
                        min(price, nxt) * (1 - dip) - 0.01, nxt, 1000.0))
        price = nxt
    return out


def sessions_before(day, n, **kw):
    days = []
    d = day
    for _ in range(n):
        d = previous_trading_day(d)
        days.append(d)
    return [trending(d, **kw) for d in reversed(days)]


class PickTests(unittest.TestCase):
    def test_keeps_the_biggest_gaps_that_are_still_rising(self):
        quotes = [
            quote("AAA", 100, 103, 104),  # +3%, rising
            quote("BBB", 100, 105, 104),  # +5% but below its open: fading
            quote("CCC", 100, 100.5, 101),  # +0.5%: not enough
            quote("DDD", 50, 51, 51.5),  # +2%
            quote("EEE", 4, 4.4, 4.5),  # +10% but under $5
            quote("ZZZ", 10, 12, 13),  # not in the universe
            quote("FFF", 100, 102, 103, day=date(2024, 3, 1)),  # last traded on Friday
            {"ticker": "GGG", "prevClose": None, "open": "x", "tngoLast": 5},  # bad numbers
        ]
        got = pick(quotes, UNI, MON)
        self.assertEqual([p.symbol for p in got], ["AAA", "DDD"])
        self.assertAlmostEqual(got[0].gap, 0.03)
        self.assertEqual((got[1].last, got[1].prev_close), (51.5, 50.0))

    def test_a_split_sized_gap_is_not_a_stock_in_play(self):
        quotes = [quote("AAA", 10, 100, 101), quote("BBB", 100, 103, 104)]  # AAA: reverse split
        self.assertEqual([p.symbol for p in pick(quotes, UNI, MON)], ["BBB"])

    def test_at_most_five_ordered_by_gap_then_symbol(self):
        quotes = [quote(s, 100, 100 + g, 200) for s, g in zip(UNI, (2, 3, 3, 4, 5, 6, 7), strict=True)]
        self.assertEqual([p.symbol for p in pick(quotes, UNI, MON)],
                         ["GGG", "FFF", "EEE", "DDD", "BBB"])

    def test_the_universe_is_a_clean_list_of_stocks(self):
        self.assertEqual(len(IN_PLAY), len(set(IN_PLAY)))
        self.assertGreaterEqual(len(IN_PLAY), 90)
        self.assertFalse({"SPY", "QQQ", "IWM"} & set(IN_PLAY))
        self.assertTrue(all(s.isalpha() and s.isupper() for s in IN_PLAY))


class ReplayTests(unittest.TestCase):
    def test_same_seed_same_odds_and_too_little_history_is_none(self):
        hist = sessions_before(TUE, 12, step=0.0004, dip=0.001)
        a = replay(hist, 30, 100.0, 99.0, paths=500, seed=7)
        self.assertEqual(a, replay(hist, 30, 100.0, 99.0, paths=500, seed=7))
        self.assertEqual((a.sessions, a.paths), (12, 500))
        self.assertIsNone(replay(hist[:9], 30, 100.0, 99.0, paths=500, seed=7))
        self.assertIsNone(replay(hist, 30, 100.0, 100.0, paths=500, seed=7))  # no risk to measure
        self.assertIsNone(replay(hist, 385, 100.0, 99.0, paths=500, seed=7))  # no time left

    def test_a_half_day_is_not_replayed_to_four_pm(self):
        hist = sessions_before(TUE, 12, step=-0.0002)
        full = replay(hist, 30, 100.0, 99.0, paths=300, seed=1)
        half = replay(hist, 30, 100.0, 99.0, paths=300, seed=1, session_minutes=210)
        self.assertEqual(full.p_stop, 1.0)  # six hours of drifting down reaches the stop
        self.assertEqual(half.p_stop, 0.0)  # three hours does not
        self.assertIsNone(replay(hist, 205, 100.0, 99.0, paths=300, seed=1, session_minutes=210))

    def test_rising_days_give_good_odds_and_falling_days_hit_the_stop(self):
        up = replay(sessions_before(TUE, 12, step=0.0005), 30, 100.0, 99.0, paths=300, seed=1)
        self.assertEqual(up.p_stop, 0.0)
        self.assertEqual(up.p_up, 1.0)
        self.assertGreater(up.exp_r, 1.0)
        down = replay(sessions_before(TUE, 12, step=-0.0005), 30, 100.0, 99.0, paths=300, seed=1)
        self.assertEqual(down.p_stop, 1.0)
        self.assertEqual(down.exp_r, -1.0)


class OrbOddsTests(unittest.TestCase):
    BREAKOUT = {"09:45": (100.0, 101.0, 99.9, 100.9)}  # first bar after the 15-minute range

    def run_day(self, history):
        core = IntradayCore(OrbOdds(), ["AAA"], RiskParams(), INTRADAY_COSTS)
        st = BotState.initial(1_000_000.0, TUE)
        bars = day_bars(TUE, overrides=self.BREAKOUT)
        return core.run_day(st, TUE, {"AAA": bars}, {"AAA": 100.0}, history={"AAA": history})

    def test_enters_with_the_odds_recorded_when_the_replay_is_good(self):
        res = self.run_day(sessions_before(TUE, 12, step=0.0005))
        ordered = [g for g in res.signals if g.outcome == "ordered"]
        self.assertEqual(len(ordered), 1)
        self.assertGreater(ordered[0].diagnostics["replay_exp_r"], 0.1)
        self.assertEqual(ordered[0].diagnostics["replay_sessions"], 12)
        self.assertEqual([f.side for f in res.fills], ["buy", "sell"])

    def test_passes_with_a_reason_when_the_replay_is_bad(self):
        res = self.run_day(sessions_before(TUE, 12, step=-0.0005))
        self.assertEqual(res.fills, [])
        skips = [g for g in res.signals if g.action == "skip"]
        self.assertEqual([(g.reason, g.outcome) for g in skips], [("replay_odds_too_low", "rejected")])
        self.assertLess(skips[0].diagnostics["replay_exp_r"], 0.1)

    def test_passes_on_a_stock_with_under_ten_days_of_history(self):
        res = self.run_day(sessions_before(TUE, 9, step=0.0005))
        self.assertEqual(res.fills, [])
        self.assertEqual([g.reason for g in res.signals if g.action == "skip"], ["not_enough_history"])

    def test_it_never_joins_the_three_fund_bots(self):
        self.assertNotIn("orb_odds", REGISTRY)
        self.assertIn("orb_odds", ALL_STRATEGIES)
        self.assertFalse({b.id for b in ALL_FUND_BOTS} & {b.id for b in IN_PLAY_BOTS})
        self.assertEqual([b.id for b in IN_PLAY_BOTS], ["sip", "sip_odds", "sip_trend", "sip_noise"])
        self.assertTrue(all(b.universe == "in_play" for b in IN_PLAY_BOTS))


class Feed:
    """Synthetic 5-minute rows for made-up stocks, and a scriptable quote reply."""

    def __init__(self):
        self.data = {s: synthetic_intraday(s, date(2024, 1, 2), date(2024, 3, 28),
                                           start_price=50.0 + 10 * i)[0]
                     for i, s in enumerate(UNI)}
        self.row_calls = self.quote_calls = 0
        self.quotes = self.day_quotes(MON)
        self.fail_quotes = False

    @staticmethod
    def day_quotes(day, **gaps):
        """A full reply for `day`: AAA +3% and BBB +2% unless overridden; the rest flat."""
        gaps = {"AAA": 3, "BBB": 2, **gaps}
        return [quote(s, 100, 100 + gaps.get(s, 0.2), 100 + gaps.get(s, 0.2) + 1, day=day) for s in UNI]

    def rows(self, sym, a, b):
        self.row_calls += 1
        return [{"date": bar.ts.isoformat(), "open": bar.open, "high": bar.high, "low": bar.low,
                 "close": bar.close, "volume": bar.volume}
                for d in trading_days(a, b) for bar in self.data[sym].get(d, [])]

    def get_quotes(self, tickers):
        self.quote_calls += 1
        if self.fail_quotes:
            raise ConnectionError("provider down")
        return list(self.quotes)


def make(feed=None, **kw):
    feed = feed or Feed()
    store = InMemoryStore()
    return InPlayRunner(settings(), store, feed.rows, feed.get_quotes, universe=UNI, **kw), store, feed


class RunnerTests(unittest.TestCase):
    def test_scans_once_trades_the_picks_and_commits_after_the_close(self):
        r, store, feed = make()
        self.assertEqual(r.run(datetime(2024, 3, 2, 18, tzinfo=UTC)).status, "idle")
        self.assertEqual(store.load_bot("sip").start_day, MON)
        self.assertEqual(r.run(at(MON, "09:32")).requests, 0)  # too early: opening prints settle
        self.assertIsNone(store.picks(UNIVERSE_ID, MON))
        rep = r.run(at(MON, "09:40"))
        self.assertEqual(feed.quote_calls, 1)
        payload = store.picks(UNIVERSE_ID, MON)
        self.assertEqual([p["symbol"] for p in payload["picks"]], ["AAA", "BBB"])
        self.assertEqual(payload["scanned"], 7)
        self.assertEqual(rep.requests, 1 + 2 + 2)  # scan + each pick's history + today's bars
        self.assertEqual(len(store.fetches("AAA", date(2024, 1, 2), previous_trading_day(MON))), 15)

        feed.quotes = feed.day_quotes(MON, DDD=10)  # the picks are never re-chosen
        r.run(at(MON, "09:50"))
        self.assertEqual(feed.quote_calls, 1)
        self.assertEqual(r.symbols_on(MON), ["AAA", "BBB"])
        self.assertIsNotNone(r.today(IN_PLAY_BOTS[0], at(MON, "11:00")))

        rep = r.run(at(MON, "16:30"))
        self.assertEqual(sorted(rep.committed), sorted((b.id, MON) for b in IN_PLAY_BOTS))
        for bot in ("sip", "sip_odds"):
            days = store.bot_days(bot)
            self.assertEqual([d.day for d in days], [MON])
            self.assertTrue({t.symbol for t in store.bot_trades(bot)} <= {"AAA", "BBB"})
        self.assertEqual(r.run(at(MON, "16:45")).committed, [])  # idempotent

    def test_a_day_without_a_scan_is_a_flat_day_not_a_guess(self):
        r, store, feed = make()
        r.run(datetime(2024, 3, 2, 18, tzinfo=UTC))
        rep = r.run(at(MON, "16:30"))  # the service was down all morning
        self.assertEqual(sorted(rep.committed), sorted((b.id, MON) for b in IN_PLAY_BOTS))
        self.assertEqual(feed.quote_calls, 0)
        self.assertEqual(store.picks(UNIVERSE_ID, MON)["picks"], [])
        self.assertIn("did not run", store.picks(UNIVERSE_ID, MON)["note"])
        day = store.bot_days("sip")[0]
        self.assertEqual((day.trades, day.pnl), (0, 0.0))

    def test_a_late_start_misses_the_window_and_a_failed_scan_retries_inside_it(self):
        r, store, feed = make()
        r.run(datetime(2024, 3, 2, 18, tzinfo=UTC))
        r.run(at(MON, "11:00"))
        self.assertEqual(feed.quote_calls, 0)
        self.assertIn("missed its window", store.picks(UNIVERSE_ID, MON)["note"])
        r.run(at(MON, "16:30"))
        feed.fail_quotes = True
        rep = r.run(at(TUE, "09:36"))
        self.assertIn("scan failed", rep.messages[0])
        self.assertIsNone(store.picks(UNIVERSE_ID, TUE))
        feed.fail_quotes = False
        feed.quotes = feed.day_quotes(TUE)
        r.run(at(TUE, "09:41"))
        self.assertEqual(r.symbols_on(TUE), ["AAA", "BBB"])
        self.assertEqual(feed.quote_calls, 2)

    def test_nothing_gapped_up_means_no_picks_and_no_trades(self):
        r, store, feed = make()
        feed.quotes = feed.day_quotes(MON, AAA=0.1, BBB=0.1)
        r.run(datetime(2024, 3, 2, 18, tzinfo=UTC))
        r.run(at(MON, "09:40"))
        self.assertEqual(store.picks(UNIVERSE_ID, MON)["picks"], [])
        self.assertIn("nothing gapped up", store.picks(UNIVERSE_ID, MON)["note"])
        r.run(at(MON, "16:30"))
        self.assertEqual(store.bot_trades("sip"), [])

    def test_display_work_waits_when_the_request_budget_is_spent(self):
        free = [3]  # exactly what the three funds need next: nothing to spare for display
        r, store, feed = make(budget=lambda: free[0])
        r.run(datetime(2024, 3, 2, 18, tzinfo=UTC))
        rep = r.run(at(MON, "09:40"))
        self.assertEqual(rep.requests, 1)  # the scan needs one free request, and gets it
        self.assertEqual(feed.row_calls, 0)
        free[0] = 0
        rep = r.run(at(MON, "16:30"))  # after the close: wait, never sleep on the limiter
        self.assertEqual((rep.status, rep.committed, feed.row_calls), ("waiting", [], 0))
        free[0] = 45
        rep = r.run(at(MON, "16:45"))
        self.assertEqual(sorted(rep.committed), sorted((b.id, MON) for b in IN_PLAY_BOTS))
        self.assertGreater(feed.row_calls, 0)

    def test_daytime_requests_stay_inside_the_hourly_allowance(self):
        from tradinglab.intraday.inplay import OWN_PER_HOUR

        r, store, feed = make()
        feed.quotes = feed.day_quotes(MON, CCC=4, DDD=5, EEE=6)  # five picks
        r.run(datetime(2024, 3, 2, 18, tzinfo=UTC))
        spent = 0
        for minute in range(35, 95, 5):  # 9:35 to 10:30, a check every five minutes
            spent += r.run(at(MON, "09:00") + timedelta(minutes=minute)).requests
        self.assertEqual(len(r.symbols_on(MON)), 5)
        self.assertLessEqual(spent, OWN_PER_HOUR)
        self.assertEqual(spent, OWN_PER_HOUR)  # and it does use what it is allowed

    def test_the_scan_always_comes_before_the_first_possible_signal(self):
        from tradinglab.intraday.inplay import SCAN_UNTIL

        for bot in IN_PLAY_BOTS:  # each strategy's first decision is no earlier than this
            params = bot.strategy().params
            first = params.get("range_minutes", params.get("start_minutes"))
            self.assertLessEqual(SCAN_UNTIL, timedelta(minutes=first), bot.id)
        r, store, feed = make()
        r.run(datetime(2024, 3, 2, 18, tzinfo=UTC))
        r.run(at(MON, "09:46"))  # one minute late: the day is sat out, not picked with hindsight
        self.assertEqual(feed.quote_calls, 0)
        self.assertEqual(store.picks(UNIVERSE_ID, MON)["picks"], [])

    def test_a_useless_quote_reply_is_a_failed_scan_not_a_quiet_market(self):
        r, store, feed = make()
        r.run(datetime(2024, 3, 2, 18, tzinfo=UTC))
        for bad in ([], feed.day_quotes(date(2024, 3, 1)), [{"ticker": s, "open": None} for s in UNI]):
            feed.quotes = bad
            rep = r.run(at(MON, "09:36"))
            self.assertIn("scan failed", rep.messages[0])
            self.assertIsNone(store.picks(UNIVERSE_ID, MON))  # still open for a retry
        feed.quotes = feed.day_quotes(MON)
        r.run(at(MON, "09:41"))
        self.assertEqual(r.symbols_on(MON), ["AAA", "BBB"])
        feed.quotes = []
        r.run(at(MON, "16:30"))
        r.run(at(TUE, "09:36"))
        r.run(at(TUE, "09:50"))
        self.assertIn("usable prices for only 0 of 7", store.picks(UNIVERSE_ID, TUE)["note"])

    def test_no_scan_on_a_day_before_any_bot_is_live(self):
        r, store, feed = make()
        r.run(at(MON, "09:40"))  # first ever run: the bots start on Tuesday
        self.assertEqual((feed.quote_calls, feed.row_calls), (0, 0))
        self.assertEqual(store.load_bot("sip").start_day, TUE)

    def test_a_half_loaded_earlier_day_is_fetched_again(self):
        r, store, feed = make()
        fri = previous_trading_day(MON)
        store.replace_ibars("AAA", fri, fri, feed.data["AAA"][fri][:2])
        store.mark_fetched("AAA", fri, 2, False, at(fri, "09:40"))  # a display fetch, never finished
        r.run(datetime(2024, 3, 2, 18, tzinfo=UTC))
        r.run(at(MON, "09:40"))
        self.assertTrue(store.fetches("AAA", fri, fri)[fri].final)
        self.assertEqual(len(store.ibars("AAA", fri)), len(feed.data["AAA"][fri]))

    def test_a_stock_whose_data_keeps_failing_sits_out_instead_of_blocking_the_day(self):
        r, store, feed = make()
        r.run(datetime(2024, 3, 2, 18, tzinfo=UTC))
        r.run(at(MON, "09:40"))
        good = feed.rows

        def rows(sym, a, b):
            if sym == "BBB":
                raise ConnectionError("provider down")
            return good(sym, a, b)

        r.fetch_rows = rows
        rep = r.run(at(MON, "16:30"))
        self.assertEqual((rep.status, rep.committed), ("data_error", []))  # retried for a while
        rep = r.run(at(MON, "16:30") + timedelta(hours=3))
        self.assertEqual(sorted(rep.committed), sorted((b.id, MON) for b in IN_PLAY_BOTS))
        self.assertIn("BBB", store.bot_days("sip")[0].excluded)

    def test_resuming_a_bot_takes_the_same_lock_as_its_runs(self):
        from tradinglab.engine.store import EngineBusyError

        r, store, feed = make()
        self.assertEqual(r.lock_name, "inplay")
        r.run(datetime(2024, 3, 2, 18, tzinfo=UTC))
        with store.run_lock("inplay"), self.assertRaises(EngineBusyError):
            r.resume_bot("sip")
        with store.run_lock("intraday"):
            r.resume_bot("sip")  # the three-fund runner's lock is a different one

    def test_a_pick_with_no_bars_sits_out_with_a_reason(self):
        r, store, feed = make()
        feed.data["BBB"] = {}
        r.run(datetime(2024, 3, 2, 18, tzinfo=UTC))
        r.run(at(MON, "09:40"))
        r.run(at(MON, "16:30") + timedelta(hours=3))  # past the grace period for missing data
        days = store.bot_days("sip")
        self.assertEqual([d.day for d in days], [MON])
        self.assertIn("BBB", days[0].excluded)


class PagesTests(unittest.TestCase):
    """The dashboard with stocks-in-play bots: leaderboard rows, picks table, odds wording."""

    @classmethod
    def setUpClass(cls):
        from tests.helpers import valid_env
        from tradinglab.web.server import AppState

        cls.runner, cls.store, cls.feed = make()
        cls.runner.run(datetime(2024, 3, 2, 18, tzinfo=UTC))
        for d in (MON, TUE):
            cls.feed.quotes = cls.feed.day_quotes(d)
            cls.runner.run(at(d, "09:40"))
            cls.runner.run(at(d, "16:30"))
        cls.state = AppState(valid_env(DASHBOARD_PASSWORD="a-long-test-password"), freeze=False,
                             system_proxies=dict, store=cls.store, controls=object())
        cls.state.inplay = cls.runner

    def test_bot_page_shows_the_picks_and_every_decision_in_plain_words(self):
        from tradinglab.web.views_bots import bot_page

        for bot in ("sip", "sip_odds"):
            body = bot_page(self.state, bot, at(TUE, "17:00"))
            self.assertIn("stocks in play", body)
            self.assertIn("Opened up", body)
            self.assertIn("AAA", body)
            self.assertIn("How this bot trades", body)
            self.assertNotIn("On past prices", body)  # no history test for these bots
            for jargon in ("replay_exp_r", "replay_odds_too_low", "not_enough_history", "orb_odds"):
                self.assertNotIn(jargon, body)
        self.assertIsNone(bot_page(self.state, "nope", at(TUE, "17:00")))

    def test_a_day_without_picks_still_renders(self):
        from tradinglab.web.views_bots import bot_page, picks_section

        self.assertIn("No scan yet", picks_section(self.state, self.runner, WED))
        self.assertIn("Stocks in play", bot_page(self.state, "sip", at(WED, "09:31")))

    def test_skips_and_odds_read_as_sentences(self):
        from tradinglab.intraday.core import ISignal
        from tradinglab.web.views_bots import _skip_line

        diag = {"replay_exp_r": -0.34, "replay_p_stop": 0.73, "replay_p_up": 0.22,
                "replay_sessions": 30.0, "replay_needed": 0.1}
        line = _skip_line(ISignal(at(TUE, "09:50"), "AAA", "skip", "rejected", "replay_odds_too_low", diag))
        self.assertIn("Saw AAA break out but passed", line)
        self.assertIn("hit the stop 73% of the time", line)
        self.assertIn("fewer than 10 earlier days", _skip_line(
            ISignal(at(TUE, "09:50"), "AAA", "skip", "rejected", "not_enough_history", {})))

    def test_a_bot_wide_halt_still_reads_as_a_halt(self):
        from tradinglab.intraday.core import DayResult, ISignal
        from tradinglab.web.views_bots import timeline

        r = DayResult(TUE, 1.0, 1.0, True, signals=[
            ISignal(at(TUE, "10:00"), "*", "skip", "rejected", "daily_loss_halt")])
        text = timeline(r, self.state.tz)
        self.assertIn("down 3% on the day", text)
        self.assertNotIn("Saw a setup", text)


if __name__ == "__main__":
    unittest.main()
