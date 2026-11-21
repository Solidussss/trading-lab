"""Whale flow, the pump.fun curve, the whale and early rule sets, and the early-warning scout."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import timedelta

from tests.test_hype import NOW_MS, T0, Clock, FakeSource, Tick, hot, next_scan, pair, why_nots
from tradinglab.hype.bot import HypeBot, MemoryHypeStore
from tradinglab.hype.dexscreener import MarketDataError, parse_pair
from tradinglab.hype.feeds import TRADES_TTL, SharedFeed
from tradinglab.hype.geckoterminal import GeckoTerminalClient
from tradinglab.hype.model import VARIANTS, Rules, curve_liquidity, curve_progress
from tradinglab.hype.scout import Scout, is_early, is_migrated, links, summary, themes
from tradinglab.hype.whales import big_size, flow

MINT = "MintAAA111pump"


def trade(kind, usd, wallet, minutes_ago=1.0, **extra):
    at = (T0 - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")
    return {"kind": kind, "volume_in_usd": str(usd), "tx_from_address": wallet, "block_timestamp": at, **extra}


BUYING = [trade("buy", 1500, "w1"), trade("buy", 2500, "w2"), trade("buy", 1200, "w3"),
          trade("sell", 1100, "w9")]


def curve_pair(addr=MINT, mcap=20_000, buys=90, sells=40, age_min=20, sol=170.0, **kw):
    """A coin still on pump.fun's curve, the way DexScreener reports it (no pool size)."""
    p = pair(addr=addr, mcap=mcap, buys=buys, sells=sells, age_min=age_min, **kw)
    p.update({"dexId": "pumpfun", "priceNative": str(float(p["priceUsd"]) / sol),
              "quoteToken": {"symbol": "SOL"}})
    del p["liquidity"]
    return p


class Market(FakeSource):
    """A fake feed with everything the bot and the scout ask for."""

    def __init__(self):
        super().__init__()
        self.early_coins: list = []
        self.trades: dict[str, list | None] = {}
        self.trade_calls: list = []

    def early(self):
        return list(self.early_coins)

    def big_trades(self, pair_addr, min_usd):
        self.trade_calls.append((pair_addr, min_usd))
        return self.trades.get(pair_addr)


class FlowTests(unittest.TestCase):
    def test_counts_wallets_and_dollars_inside_the_window(self):
        rows = BUYING + [trade("buy", 5000, "old", minutes_ago=30), trade("buy", 300, "small"),
                         trade("buy", 9000, "bad", is_anomalous=True), trade("buy", "x", "w7"),
                         trade("buy", 2000, "w1"), {"kind": "buy"}, "junk"]
        f = flow(rows, T0, 10, 1000)
        self.assertEqual((f.buyers, f.buy_usd, f.sellers, f.sell_usd), (3, 7200.0, 1, 1100.0))
        self.assertTrue(f.buying(3, 2.0))
        self.assertFalse(f.buying(4, 2.0))
        self.assertFalse(f.selling(2.0))
        self.assertIn("3 big wallets bought $7,200", f.words())

    def test_big_is_measured_against_the_pool(self):
        self.assertEqual([big_size(x) for x in (5_000, 18_317, 40_000, 100_000, 2_000_000)],
                         [250.0, 250.0, 400.0, 1000.0, 2000.0])
        self.assertEqual(big_size(40_020), big_size(39_990))  # steady while the pool wobbles

    def test_selling_needs_two_whales_and_twice_the_dollars(self):
        f = flow([trade("sell", 3000, "a"), trade("sell", 2000, "b"), trade("buy", 2000, "c")], T0, 10, 1000)
        self.assertTrue(f.selling(2.0))
        self.assertFalse(flow([trade("sell", 9000, "a")], T0, 10, 1000).selling(2.0))  # one wallet
        self.assertFalse(flow([], T0, 10, 1000).buying(1, 2.0) and True)


class CurveTests(unittest.TestCase):
    def test_progress_runs_from_zero_at_the_start_to_one_at_migration(self):
        self.assertEqual(curve_progress(0, 170), 0.0)
        self.assertAlmostEqual(curve_progress(4_750, 170), 0.0, places=1)  # a brand new coin
        self.assertAlmostEqual(curve_progress(70_000, 170), 1.0, places=1)  # about to migrate
        self.assertTrue(0.1 < curve_progress(10_000, 170) < curve_progress(40_000, 170) < 1.0)
        self.assertEqual(curve_progress(10_000, 0), 0.0)

    def test_a_curve_coin_gets_a_worked_out_pool_size_and_stage(self):
        s = parse_pair(curve_pair(mcap=20_000), NOW_MS)
        self.assertTrue(s.pre_migration)
        self.assertAlmostEqual(s.sol_usd, 170.0, places=3)
        self.assertAlmostEqual(s.liquidity, curve_liquidity(20_000, 170.0), places=2)
        self.assertGreater(s.liquidity, 15_000)
        self.assertTrue(0.3 < s.curve_done < 0.5)
        normal = hot()
        self.assertFalse(normal.pre_migration)
        self.assertIsNone(normal.curve_done)
        self.assertEqual(normal.liquidity, 40000)  # a real pool's number is never replaced


class WhaleRuleSetTests(unittest.TestCase):
    RULES = next(v for v in VARIANTS if v.id == "whales").rules

    def bot(self, rules=None):
        m, clock = Market(), Clock()
        m.trending, m.data = ["TokA1"], {"TokA1": hot(c5=1.0)}  # holding up, not jumping
        return HypeBot(m, MemoryHypeStore(), rules or self.RULES, clock=clock), m, clock

    def test_buys_only_when_the_whales_are_buying(self):
        bot, m, clock = self.bot()
        acct = bot.step()
        self.assertEqual(acct.positions, [])
        self.assertIn("no whale data right now", why_nots(acct))
        m.trades["PTokA1"] = [trade("buy", 5000, "w1"), trade("sell", 4000, "w2")]
        acct = next_scan(bot, clock)
        self.assertEqual(acct.positions, [])
        self.assertTrue(any(r.startswith("whales aren't buying") for r in why_nots(acct)))
        m.trades["PTokA1"] = [replace_time(t, clock.t + timedelta(minutes=1)) for t in BUYING]
        acct = next_scan(bot, clock)
        self.assertEqual([p.symbol for p in acct.positions], ["TokA"])
        self.assertIn("3 big wallets bought", acct.positions[0].why)
        self.assertEqual(m.trade_calls[0], ("PTokA1", 400.0))  # 1% of its $40,000 pool

    def test_an_ordinary_rule_set_never_asks_for_whale_data(self):
        bot, m, _ = self.bot(Rules())
        m.data["TokA1"] = hot()
        self.assertEqual(len(bot.step().positions), 1)
        self.assertEqual(m.trade_calls, [])

    def test_sells_when_the_whales_turn(self):
        bot, m, clock = self.bot()
        m.trades["PTokA1"] = BUYING
        self.assertEqual(len(bot.step().positions), 1)
        for _ in range(40):  # about seven minutes of 10-second cycles
            clock.t += timedelta(seconds=10)
            m.trades["PTokA1"] = [replace_time(trade("sell", 4000, "w1"), clock.t),
                                  replace_time(trade("sell", 3000, "w2"), clock.t)]
            acct = bot.step()
            if not acct.positions:
                break
        self.assertEqual(acct.positions, [])
        self.assertEqual(bot.store.trades[-1].reason, "whales selling")

    def test_only_the_best_few_coins_cost_a_lookup(self):
        bot, m, _ = self.bot()
        m.trending = [f"Tok{i}x" for i in range(6)]
        m.data = {t: hot(addr=t, c5=1.0) for t in m.trending}
        acct = bot.step()
        self.assertEqual(len(m.trade_calls), self.RULES.whale_checks)
        self.assertIn("whales not checked this scan", why_nots(acct))

    def test_account_sized_bets_and_the_quick_stop(self):
        quick = next(v for v in VARIANTS if v.id == "whales_quick").rules
        self.assertEqual((quick.stop_loss, quick.stake_pct), (-0.08, 0.07))
        bot, m, clock = self.bot(quick)
        m.trades["PTokA1"] = BUYING
        acct = bot.step()
        self.assertEqual(acct.positions[0].stake, 70.0)  # 7% of the $1,000 it started with
        m.data["TokA1"] = hot(price="0.00088", c5=1.0)  # down 12% on screen
        clock.t += timedelta(seconds=10)
        self.assertEqual(bot.step().positions, [])
        self.assertEqual(bot.store.trades[-1].reason, "stop loss")


def replace_time(row, when):
    return {**row, "block_timestamp": when.isoformat().replace("+00:00", "Z")}


class EarlyRuleSetTests(unittest.TestCase):
    RULES = next(v for v in VARIANTS if v.id == "early").rules

    def test_it_trades_curve_coins_from_the_early_list_only(self):
        m, clock = Market(), Clock()
        m.trending, m.data = ["TokA1"], {"TokA1": hot()}  # trending and migrated: not its business
        bot = HypeBot(m, MemoryHypeStore(), self.RULES, clock=clock)
        self.assertEqual(bot.step().positions, [])
        m.early_coins = [parse_pair(curve_pair(), NOW_MS), hot(addr="TokB2")]
        acct = next_scan(bot, clock)
        self.assertEqual([p.token for p in acct.positions], [MINT])
        self.assertEqual(acct.watch, [])  # the migrated coin on that list isn't even considered
        barely = replace(self.RULES, min_curve=0.9)
        acct = HypeBot(m, MemoryHypeStore(), barely, clock=clock).step()
        self.assertEqual(acct.positions, [])
        self.assertTrue(any(r.startswith("barely started") for r in why_nots(acct)))

    def test_ordinary_rule_sets_never_see_the_early_list(self):
        m = Market()
        m.early_coins = [parse_pair(curve_pair(), NOW_MS)]
        self.assertEqual(HypeBot(m, MemoryHypeStore(), Rules(), clock=Clock()).step().positions, [])


class ScoutTests(unittest.TestCase):
    def setUp(self):
        self.m, self.clock, self.store = Market(), Clock(), MemoryHypeStore()
        self.scout = Scout(self.m, self.store, clock=self.clock)

    def kinds(self, doc):
        return [(a["kind"], a["title"]) for a in doc["alerts"]]

    def test_what_counts_as_early_and_as_migrated(self):
        busy = parse_pair(curve_pair(), NOW_MS)
        self.assertTrue(is_early(busy))
        self.assertFalse(is_early(parse_pair(curve_pair(buys=20, sells=10), NOW_MS)))  # quiet
        self.assertFalse(is_early(parse_pair(curve_pair(buys=50, sells=70), NOW_MS)))  # being sold
        self.assertFalse(is_early(parse_pair(curve_pair(age_min=2), NOW_MS)))  # too new to judge
        self.assertFalse(is_early(parse_pair(curve_pair(mcap=5_500), NOW_MS)))  # barely started
        self.assertFalse(is_early(hot()))
        moved = hot(addr=MINT, age_min=12, buys=300, sells=150)
        self.assertTrue(is_migrated(moved))
        self.assertFalse(is_migrated(hot(addr=MINT, age_min=300, buys=300, sells=150)))
        self.assertFalse(is_migrated(hot(addr="NotAPumpCoin", age_min=12, buys=300, sells=150)))

    def test_raises_each_alert_once_and_keeps_its_price(self):
        self.m.early_coins = [parse_pair(curve_pair(), NOW_MS)]
        self.m.trending, self.m.data = ["MovedBBBpump"], {
            "MovedBBBpump": hot(addr="MovedBBBpump", age_min=10, buys=400, sells=100)}
        doc = self.scout.step()
        self.assertEqual(sorted(k for k, _ in self.kinds(doc)), ["early", "migrated"])
        early = next(a for a in doc["alerts"] if a["kind"] == "early")
        self.assertIn("of the way to migrating", early["text"])
        self.assertEqual((early["price"], early["token"]), (0.001, MINT))
        self.assertEqual(sorted(early["links"]), ["DexScreener", "Solscan"])
        self.clock.t += timedelta(minutes=1)
        self.assertEqual(len(self.scout.step()["alerts"]), 2)  # not raised again
        self.clock.t += timedelta(hours=7)
        self.assertEqual(len(self.scout.step()["alerts"]), 4)  # six hours on, it may be

    def test_a_shared_subject_is_one_theme_alert(self):
        coins = [hot(addr=a, age_min=60) for a in ("GtaSix111", "GtaVice222", "GtaCity333")]
        coins = [replace(c, name=n, symbol=s) for c, n, s in zip(
            coins, ("GTA Six", "Vice GTA coin", "the gta city"), ("GTA6", "VGTA", "CITY"), strict=True)]
        self.assertEqual(sorted(themes(coins)), ["gta"])
        self.assertEqual(themes(coins[:2]), {})  # two coins is not a trend
        copies = [replace(hot(addr=f"Copy{i}", age_min=30), name="Last Penny", symbol="PENNY") for i in range(3)]
        self.assertEqual(list(themes(copies)), ["penny"])  # one subject, not one per word
        self.m.trending, self.m.data = [c.token for c in coins], {c.token: c for c in coins}
        doc = self.scout.step()
        self.assertEqual([a["title"] for a in doc["alerts"] if a["kind"] == "theme"], ["GTA"])

    def test_whale_buying_is_an_alert_and_wallets_are_scored_an_hour_later(self):
        coin = hot()
        self.m.trending, self.m.data = ["TokA1"], {"TokA1": coin}
        self.m.trades["PTokA1"] = BUYING
        doc = self.scout.step()
        self.assertIn(("whales", "TokA"), self.kinds(doc))
        self.assertEqual(sorted(p["wallet"] for p in doc["pending"]), ["w1", "w2", "w3"])
        self.m.trades["PTokA1"] = []
        self.m.data["TokA1"] = hot(price="0.0015")  # up 50% an hour later
        self.clock.t += timedelta(minutes=61)
        doc = self.scout.step()
        self.assertEqual(doc["pending"], [])
        self.assertEqual(doc["wallets"]["w1"][:2], [1, 1])
        self.assertEqual(Scout.smart_wallets(doc), set())  # one good call is not a record

    def test_a_wallet_with_a_record_raises_a_smart_alert_and_a_vanished_coin_is_a_loss(self):
        self.store.save_doc("scout", {"wallets": {"w1": [6, 5, 3.0, "", "OLD"], "w2": [6, 1, -2.0, "", "OLD"]},
                                      "pending": [{"wallet": "w2", "token": "Gone", "symbol": "GONE",
                                                   "price": 1.0, "at": (T0 - timedelta(hours=2)).isoformat()}]})
        self.m.trending, self.m.data = ["TokA1"], {"TokA1": hot()}
        self.m.trades["PTokA1"] = [trade("buy", 2000, "w1")]
        doc = self.scout.step()
        smart = [a for a in doc["alerts"] if a["kind"] == "smart"]
        self.assertEqual(len(smart), 1)
        self.assertIn("right on 5 of its last 6", smart[0]["text"])
        self.assertEqual(doc["wallets"]["w2"][:2], [7, 1])  # the coin it bought no longer exists

    def test_follows_each_alert_so_the_page_can_say_how_it_did(self):
        self.m.early_coins = [parse_pair(curve_pair(), NOW_MS)]
        self.scout.step()
        self.m.data[MINT] = hot(addr=MINT, price="0.003")
        for _ in range(5):
            self.clock.t += timedelta(minutes=1)
            doc = self.scout.step()
        self.m.data[MINT] = hot(addr=MINT, price="0.0015")
        for _ in range(5):
            self.clock.t += timedelta(minutes=1)
            doc = self.scout.step()
        a = doc["alerts"][-1]
        self.assertEqual((a["price"], a["peak"], a["last"]), (0.001, 0.003, 0.0015))
        s = summary(doc, self.clock.t)["early"]
        self.assertEqual((s["n"], s["ran"]), (1, 1))
        self.assertAlmostEqual(s["peak"], 2.0)
        self.assertAlmostEqual(s["now"], 0.5)

    def test_a_dead_feed_is_recorded_not_raised(self):
        def boom():
            raise ConnectionError("down")

        self.m.early = boom
        self.m.discover = boom
        doc = self.scout.step()
        self.assertEqual((doc["alerts"], doc["error"]), ([], ""))
        self.m.big_trades = lambda *_a: boom()
        self.m.early = lambda: [parse_pair(curve_pair(), NOW_MS)]
        doc = self.scout.step()
        self.assertIn("ConnectionError", doc["error"])
        self.assertEqual(len(doc["alerts"]), 1)  # what it had found before the failure is kept

    def test_links_go_to_a_chart_site_and_an_explorer_never_a_trading_site(self):
        from tradinglab.safety.endpoints import is_brokerage_host

        got = links(parse_pair(curve_pair(), NOW_MS))
        self.assertEqual(sorted(got), ["DexScreener", "Solscan"])
        for url in got.values():
            self.assertTrue(url.startswith("https://"))
            self.assertFalse(is_brokerage_host(url.split("/")[2]))


class FeedAndClientTests(unittest.TestCase):
    def test_big_trades_and_the_early_list_are_shared_for_a_minute_and_fail_quietly(self):
        class Src:
            calls = 0

            def trades(self, pool, min_usd):
                Src.calls += 1
                if pool == "bad":
                    raise ConnectionError("HTTP 429")
                return [{"kind": "buy"}]

            def discover(self):
                Src.calls += 1
                return [hot()]

        tick, src = Tick(), Src()
        feed = SharedFeed(discover=FakeSource(), price=FakeSource(), clock=tick, trades=src, early=src)
        self.assertEqual(feed.big_trades("pool", 1000), [{"kind": "buy"}])
        self.assertEqual(feed.big_trades("pool", 1000), [{"kind": "buy"}])
        self.assertEqual(len(feed.early()), 1)
        self.assertEqual(len(feed.early()), 1)
        self.assertEqual(Src.calls, 2)
        self.assertIsNone(feed.big_trades("bad", 1000))
        self.assertEqual(feed.status()["rate_limited_1h"], 1)
        tick.t += TRADES_TTL + 1
        self.assertIsNone(feed.big_trades("pool", 1000))  # the provider said slow down: left alone
        self.assertEqual(feed.early(), [])
        self.assertEqual(Src.calls, 3)
        plain = SharedFeed(discover=FakeSource(), price=FakeSource(), clock=tick)
        self.assertEqual((plain.big_trades("pool", 1000), plain.early()), (None, []))

    def test_the_client_reads_the_trade_list_and_refuses_odd_addresses(self):
        import json

        seen = []

        def http_get(url, headers, timeout):
            seen.append(url)
            body = {"data": [{"attributes": {"kind": "buy", "volume_in_usd": "1500.5"}}, {"oops": 1}, "x"]}
            return 200, json.dumps(body).encode(), {}

        client = GeckoTerminalClient(http_get=http_get)
        self.assertEqual(client.trades("Pool123", 1000.0), [{"kind": "buy", "volume_in_usd": "1500.5"}])
        self.assertTrue(seen[0].endswith("/networks/solana/pools/Pool123/trades"
                                         "?trade_volume_in_usd_greater_than=1000"))
        with self.assertRaises(MarketDataError):
            client.trades("../etc", 1000.0)
        self.assertEqual(len(seen), 1)

    def test_documents_round_trip_in_the_memory_store(self):
        store = MemoryHypeStore()
        self.assertIsNone(store.load_doc("scout"))
        store.save_doc("scout", {"alerts": [1]})
        got = store.load_doc("scout")
        got["alerts"].append(2)
        self.assertEqual(store.load_doc("scout"), {"alerts": [1]})


if __name__ == "__main__":
    unittest.main()
