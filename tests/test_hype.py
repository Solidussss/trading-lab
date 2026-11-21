"""Hype paper bot: parsing, scoring, the cost model, entries, exits, rugs, and safety."""

from __future__ import annotations

import contextlib
import json
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest import mock

from tests import test_pg_store
from tradinglab.hype.bot import (
    CYCLE_SECONDS,
    MISSING_LIMIT,
    SCAN_EVERY,
    HypeBot,
    HypeRunner,
    MemoryHypeStore,
    equity,
)
from tradinglab.hype.dexscreener import DexScreenerClient, MarketDataError, parse_pair
from tradinglab.hype.feeds import DISCOVER_TTL, PRICE_TTL, RATE_LIMIT_PAUSE, SharedFeed
from tradinglab.hype.model import (
    VARIANTS,
    Account,
    Rules,
    Snapshot,
    Trade,
    buy_fill,
    entry_check,
    score,
    sell_fill,
)
from tradinglab.hype.store import SCHEMA, PgHypeStore
from tradinglab.safety.endpoints import CRYPTO_DATA_HOST, is_brokerage_host
from tradinglab.safety.network_guard import policy

T0 = datetime(2026, 11, 5, 15, 0, tzinfo=UTC)
NOW_MS = int(T0.timestamp() * 1000)


def pair(addr="TokA1", price="0.001", liq=40000, buys=120, sells=50, v5=12000, v1h=36000,
         c5=12.0, c1h=30.0, age_min=90, mcap=400000, chain="solana"):
    return {"chainId": chain, "dexId": "raydium", "url": f"https://dexscreener.com/solana/{addr}",
            "pairAddress": f"P{addr}", "baseToken": {"address": addr, "name": addr, "symbol": addr[:4]},
            "priceUsd": price, "txns": {"m5": {"buys": buys, "sells": sells}, "h1": {"buys": 900, "sells": 600}},
            "volume": {"m5": v5, "h1": v1h}, "priceChange": {"m5": c5, "h1": c1h},
            "liquidity": {"usd": liq}, "marketCap": mcap, "pairCreatedAt": NOW_MS - age_min * 60000,
            "info": {"socials": [{"type": "x"}], "websites": [{}]}, "boosts": {"active": 5}}


def hot(**kw) -> Snapshot:
    return parse_pair(pair(**kw), NOW_MS)


class FakeSource:
    def __init__(self):
        self.trending: list[str] = []
        self.data: dict[str, Snapshot] = {}

    def discover(self):
        return [self.data[t] for t in self.trending if t in self.data]

    def snapshots(self, tokens, pairs=None):
        return {t: self.data[t] for t in tokens if t in self.data}


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


def next_scan(bot, clock):
    """Run 10-second cycles until the bot has scanned for new coins again."""
    for _ in range(SCAN_EVERY):
        clock.t += timedelta(seconds=CYCLE_SECONDS)
        acct = bot.step()
    return acct


def why_nots(acct):
    return [r for w in acct.watch for r in w["why_not"]]


class ParsingTest(unittest.TestCase):
    def test_parse_pair(self):
        s = hot()
        self.assertEqual(s.price, 0.001)
        self.assertAlmostEqual(s.buy_ratio, 120 / 170)
        self.assertAlmostEqual(s.vol_accel, 12000 / 3000)
        self.assertAlmostEqual(s.age_min, 90)

    def test_rejects_other_chains_and_bad_prices(self):
        self.assertIsNone(parse_pair(pair(chain="ethereum"), NOW_MS))
        self.assertIsNone(parse_pair(pair(price="NaN"), NOW_MS))
        self.assertIsNone(parse_pair(pair(price="0"), NOW_MS))
        self.assertIsNone(parse_pair("junk", NOW_MS))  # type: ignore[arg-type]

    def test_client_picks_most_liquid_pair_and_batches(self):
        calls = []

        def http_get(url, headers, timeout):
            calls.append(url)
            if "/tokens/v1/" in url:
                return 200, json.dumps([pair("TokA1", liq=5000), pair("TokA1", liq=90000, price="0.002")]).encode()
            return 200, json.dumps([{"chainId": "solana", "tokenAddress": "TokA1"},
                                    {"chainId": "base", "tokenAddress": "0xabc"}]).encode()

        c = DexScreenerClient(http_get=http_get, sleep=lambda s: None, clock_ms=lambda: NOW_MS)
        self.assertEqual(c.trending_tokens(), ["TokA1"])
        snaps = c.snapshots(["TokA1"])
        self.assertEqual(snaps["TokA1"].liquidity, 90000)
        self.assertTrue(all(u.startswith("https://api.dexscreener.com/") for u in calls))

    def test_client_refuses_redirects_and_gives_up(self):
        c = DexScreenerClient(http_get=lambda u, h, t: (302, b""), sleep=lambda s: None)
        with self.assertRaises(MarketDataError):
            c.snapshots(["TokA1"])
        c = DexScreenerClient(http_get=lambda u, h, t: (503, b""), sleep=lambda s: None)
        with self.assertRaises(MarketDataError):
            c.snapshots(["TokA1"])

    def test_client_drops_weird_addresses(self):
        seen = []
        c = DexScreenerClient(http_get=lambda u, h, t: (seen.append(u), (200, b"[]"))[1], sleep=lambda s: None)
        c.snapshots(["../../evil", "ok123"])
        self.assertEqual(len(seen), 1)
        self.assertNotIn("evil", seen[0])


class RulesTest(unittest.TestCase):
    def test_hot_coin_passes_and_scores_high(self):
        self.assertEqual(entry_check(hot(), Rules()), [])
        self.assertGreaterEqual(score(hot()), 60)

    def test_feed_without_promo_data_is_not_penalised(self):
        plain = replace(hot(), boosts=0, socials=0)
        blind = replace(plain, promo_known=False)
        self.assertGreater(score(blind), score(plain))
        self.assertLessEqual(score(replace(blind, buys_5m=500, sells_5m=10, vol_5m=1e6, chg_5m=30,
                                           liquidity=1e6)), 100)

    def test_filters(self):
        r = Rules()
        self.assertTrue(any("thin pool" in x for x in entry_check(hot(liq=5000), r)))
        self.assertTrue(any("too new" in x for x in entry_check(hot(age_min=3), r)))
        self.assertTrue(any("sellers" in x for x in entry_check(hot(buys=60, sells=60), r)))
        self.assertTrue(any("vertical" in x for x in entry_check(hot(c5=80), r)))
        self.assertTrue(any("no volume surge" in x for x in entry_check(hot(v5=3000, v1h=36000), r)))

    def test_costs_are_charged_both_ways(self):
        r = Rules()
        eff, qty = buy_fill(1.0, 50, 40000, r)
        self.assertGreater(eff, 1.02)  # latency + impact
        back = sell_fill(1.0, qty, 40000, r)
        self.assertLess(back, 50 * 0.96)  # a flat round trip loses >4%
        self.assertGreater(back, 50 * 0.90)


class BotTest(unittest.TestCase):
    def setUp(self):
        self.src, self.store, self.clock = FakeSource(), MemoryHypeStore(), Clock()
        self.bot = HypeBot(self.src, self.store, Rules(), clock=self.clock)
        self.src.trending = ["TokA1"]
        self.src.data["TokA1"] = hot()

    def move(self, minutes, **kw):
        self.clock.t += timedelta(minutes=minutes)
        base = self.src.data["TokA1"]
        self.src.data["TokA1"] = replace(base, **kw)
        return self.bot.step()

    def test_buys_a_hyped_coin_once(self):
        a = self.bot.step()
        self.assertEqual(len(a.positions), 1)
        self.assertEqual(a.cash, 950)
        a = self.move(1)
        self.assertEqual(len(a.positions), 1)  # no double buy

    def test_moon_takes_half_then_trails(self):
        self.bot.step()
        a = self.move(5, price=0.0025)  # 2.5x seen price
        p = a.positions[0]
        self.assertTrue(p.half_taken)
        self.assertGreater(a.cash, 950 + 40)
        a = self.move(5, price=0.004)
        a = self.move(5, price=0.0026)  # 35% off the 0.004 top
        self.assertEqual(a.positions, [])
        t = self.store.trades[-1]
        self.assertIn("trailing", t.reason)
        self.assertGreater(t.pnl, 50)

    def test_dump_hits_stop(self):
        self.bot.step()
        a = self.move(2, price=0.0007)
        self.assertEqual(a.positions, [])
        t = self.store.trades[-1]
        self.assertEqual(t.reason, "stop loss")
        self.assertLess(t.pnl, -15)

    def test_rug_books_a_near_total_loss(self):
        self.bot.step()
        a = self.move(2, liquidity=2000)
        self.assertEqual(a.positions, [])
        t = self.store.trades[-1]
        self.assertIn("rug", t.reason)
        self.assertAlmostEqual(t.proceeds, 2.5, places=2)

    def test_vanished_pair_is_a_rug(self):
        self.bot.step()
        del self.src.data["TokA1"]
        for _ in range(MISSING_LIMIT - 1):  # a few missing reads is a data hiccup, not a rug
            self.clock.t += timedelta(minutes=1)
            self.assertEqual(len(self.bot.step().positions), 1)
        self.clock.t += timedelta(minutes=1)
        a = self.bot.step()
        self.assertEqual(a.positions, [])
        self.assertIn("rug", self.store.trades[-1].reason)

    def test_flat_coin_is_dropped_after_45_minutes(self):
        self.bot.step()
        self.move(46, price=0.00101)
        self.assertIn("faded", self.store.trades[-1].reason)

    def test_cooldown_blocks_rebuy(self):
        self.bot.step()
        self.move(2, price=0.0007)  # stopped out
        self.move(2, price=0.001)
        a = next_scan(self.bot, self.clock)
        self.assertEqual(a.positions, [])
        self.assertTrue(any("cooldown" in r for r in why_nots(a)))

    def test_respects_max_open_and_cash(self):
        rules = Rules(max_open=2, max_new_per_cycle=5)
        bot = HypeBot(self.src, MemoryHypeStore(), rules, clock=self.clock)
        self.src.trending = [f"Tok{i}" for i in range(6)]
        for t in self.src.trending:
            self.src.data[t] = hot(addr=t)
        a = bot.step()
        self.assertEqual(len(a.positions), 2)
        self.assertAlmostEqual(equity(a, rules), 1000, delta=10)

    def test_data_outage_keeps_books_and_reports(self):
        class Broken(FakeSource):
            def snapshots(self, tokens, pairs=None):
                raise MarketDataError("down")

        self.bot.step()
        self.bot.source = Broken()
        self.bot.source.trending = ["TokA1"]
        self.clock.t += timedelta(minutes=1)
        a = self.bot.step()
        self.assertEqual(len(a.positions), 1)  # nothing sold on missing data
        self.assertIn("down", a.last_error)


GECKO = {
    "data": [{"id": "solana_PoolA", "type": "pool", "attributes": {
        "name": "WIF / SOL", "address": "PoolA", "base_token_price_usd": "0.0021",
        "reserve_in_usd": "52000", "fdv_usd": "900000", "market_cap_usd": None,
        "pool_created_at": "2026-11-05T13:00:00Z",
        "price_change_percentage": {"m5": "9.5", "h1": "40"},
        "transactions": {"m5": {"buys": 140, "sells": 60}, "h1": {"buys": 1200, "sells": 800}},
        "volume_usd": {"m5": "15000", "h1": "60000"}},
        "relationships": {"base_token": {"data": {"id": "solana_MintA"}}, "dex": {"data": {"id": "raydium"}}}},
        {"id": "solana_bad", "type": "pool", "attributes": {"address": "Bad", "base_token_price_usd": None}}],
    "included": [{"id": "solana_MintA", "type": "token",
                  "attributes": {"address": "MintA", "symbol": "WIF", "name": "dogwifhat"}}],
}


class GeckoTest(unittest.TestCase):
    def client(self, body, status=200):
        from tradinglab.hype.geckoterminal import GeckoTerminalClient
        self.urls = []

        def http_get(url, headers, timeout):
            self.urls.append(url)
            return status, json.dumps(body).encode()
        return GeckoTerminalClient(http_get=http_get, sleep=lambda s: None, clock_ms=lambda: NOW_MS)

    def test_discover_parses_pools(self):
        snaps = self.client(GECKO).discover()
        self.assertEqual(len(snaps), 1)
        s = snaps[0]
        self.assertEqual((s.token, s.pair, s.symbol), ("MintA", "PoolA", "WIF"))
        self.assertEqual(s.market_cap, 900000)  # falls back to FDV
        self.assertAlmostEqual(s.age_min, 120)
        self.assertAlmostEqual(s.vol_accel, 15000 / 5000)
        self.assertTrue(all(u.startswith("https://api.geckoterminal.com/api/v2/") for u in self.urls))

    def test_snapshots_by_pool(self):
        c = self.client(GECKO)
        self.assertEqual(set(c.snapshots(["MintA"], {"MintA": "PoolA"})), {"MintA"})
        self.assertIn("/pools/multi/PoolA", self.urls[-1])
        self.assertEqual(c.snapshots(["MintA"], {"MintA": "../x"}), {})

    def test_fallback_source(self):
        from tradinglab.hype.bot import FallbackSource

        class Down:
            def discover(self):
                raise MarketDataError("429")

        good = FakeSource()
        good.trending, good.data = ["TokA1"], {"TokA1": hot()}
        self.assertEqual(len(FallbackSource(Down(), good).discover()), 1)
        with self.assertRaises(RuntimeError):
            FallbackSource(Down()).discover()


class VariantTest(unittest.TestCase):
    def test_three_variants_in_order(self):
        self.assertEqual([v.id for v in VARIANTS],
                         ["current", "no_repeat", "deep_pools", "whales", "early", "whales_quick"])
        self.assertEqual([(v.id, v.name, v.blurb, v.buys_enabled) for v in VARIANTS[:3]], [
            ("current", "Current rules", "Chases 5-minute jumps", False),
            ("no_repeat", "No repeat losers", "Never re-buys a coin it lost on", True),
            ("deep_pools", "Bigger pools only", "Skips anything under $100k in the pool", True)])
        current, no_repeat, deep = VARIANTS[:3]
        self.assertEqual(current.rules, Rules())
        self.assertEqual(no_repeat.rules, replace(Rules(), no_repeat_losers=True, cooldown_hours=24.0))
        self.assertEqual(deep.rules, replace(Rules(), min_liquidity=100_000.0, min_mcap=500_000.0,
                                             min_txns_5m=80))
        self.assertEqual((Rules().no_repeat_losers, Rules().auto_pause_drawdown), (False, 0.30))

    def test_old_account_payload_still_loads(self):
        src = FakeSource()
        src.trending, src.data = ["TokA1"], {"TokA1": hot()}
        old = HypeBot(src, MemoryHypeStore(), Rules(), clock=Clock()).step().to_json()
        del old["banned"], old["paused"]  # what last week's rows look like
        acct = Account.from_json(json.loads(json.dumps(old)))
        self.assertEqual((acct.banned, acct.paused, len(acct.positions)), ([], "", 1))
        self.assertEqual(Account.from_json(acct.to_json()), acct)


class NoRepeatTest(unittest.TestCase):
    def setUp(self):
        self.src, self.store, self.clock = FakeSource(), MemoryHypeStore(), Clock()
        self.rules = replace(Rules(), no_repeat_losers=True, cooldown_hours=24.0)
        self.bot = HypeBot(self.src, self.store, self.rules, clock=self.clock)
        self.src.trending = ["TokA1"]
        self.src.data["TokA1"] = hot()

    def move(self, minutes, bot=None, **kw):
        self.clock.t += timedelta(minutes=minutes)
        self.src.data["TokA1"] = replace(self.src.data["TokA1"], **kw)
        return (bot or self.bot).step()

    def test_never_rebuys_a_coin_it_lost_on(self):
        self.bot.step()
        self.move(2, price=0.0007)  # stopped out at a loss
        self.assertEqual(self.store.account.banned, ["TokA1"])
        self.move(25 * 60, price=0.001)  # the 24h cooldown is over and the coin is hot again
        a = next_scan(self.bot, self.clock)
        self.assertEqual(a.positions, [])
        self.assertEqual(a.cooldown, {})
        self.assertIn("lost on it before", why_nots(a))
        self.assertEqual(len(self.store.trades), 1)

    def test_default_rules_do_rebuy_after_the_cooldown(self):
        bot = HypeBot(self.src, self.store, Rules(), clock=self.clock)
        bot.step()
        self.move(2, bot, price=0.0007)
        self.assertEqual(self.store.account.banned, [])
        self.move(7 * 60, bot, price=0.001)
        self.assertEqual(len(next_scan(bot, self.clock).positions), 1)

    def test_a_winner_can_be_bought_again(self):
        self.bot.step()
        self.move(5, price=0.0025)
        self.move(5, price=0.004)
        self.move(5, price=0.0026)  # trailing stop, in profit
        self.assertGreater(self.store.trades[-1].pnl, 0)
        self.assertEqual(self.store.account.banned, [])

    def test_ban_list_keeps_the_newest_500(self):
        acct = self.bot.step()
        acct.banned = [f"Old{i}" for i in range(500)]
        self.store.save_hype(acct, [])
        self.move(2, price=0.0007)
        banned = self.store.account.banned
        self.assertEqual((len(banned), banned[0], banned[-1]), (500, "Old1", "TokA1"))


class PausedTest(unittest.TestCase):
    def setUp(self):
        self.src, self.store, self.clock = FakeSource(), MemoryHypeStore(), Clock()
        self.src.trending = ["TokA1"]
        self.src.data["TokA1"] = hot()

    def test_paused_variant_manages_exits_but_never_buys(self):
        HypeBot(self.src, self.store, Rules(), clock=self.clock).step()  # holds TokA1 from before
        paused = HypeBot(self.src, self.store, Rules(), clock=self.clock, buys_enabled=False)
        self.src.trending.append("TokB2")
        self.src.data["TokB2"] = hot(addr="TokB2")
        self.clock.t += timedelta(minutes=2)
        self.src.data["TokA1"] = replace(self.src.data["TokA1"], price=0.0007)
        a = paused.step()
        self.assertEqual(a.positions, [])  # the stop still works
        self.assertEqual(self.store.trades[-1].reason, "stop loss")
        a = next_scan(paused, self.clock)
        self.assertEqual(a.positions, [])
        self.assertEqual([w["symbol"] for w in a.watch], ["TokA", "TokB"])  # radar still filled
        self.assertTrue(all(w["why_not"][0] == "paused" and not w["bought"] for w in a.watch))
        self.assertEqual(a.paused, "")  # switched off by its variant, not by a drawdown

    def test_fresh_paused_variant_buys_nothing(self):
        a = HypeBot(self.src, self.store, Rules(), clock=self.clock, buys_enabled=False).step()
        self.assertEqual((a.positions, a.cash), ([], 1000))
        self.assertEqual(a.watch[0]["why_not"], ["paused"])

    def test_a_pause_reason_on_the_account_blocks_buys(self):
        self.store.save_hype(Account(cash=1000.0, started=T0.isoformat(), paused="down 30% from its start"), [])
        a = HypeBot(self.src, self.store, Rules(), clock=self.clock).step()
        self.assertEqual(a.positions, [])
        self.assertEqual(a.watch[0]["why_not"], ["paused"])


class AutoPauseTest(unittest.TestCase):
    def setUp(self):
        self.src, self.store, self.clock = FakeSource(), MemoryHypeStore(), Clock()
        self.src.trending = ["TokA1", "TokB2"]
        self.src.data = {t: hot(addr=t) for t in self.src.trending}

    def crash(self, rules):
        """Buy two big positions, then both lose 80%: about $550 is left of the $1,200 start."""
        bot = HypeBot(self.src, self.store, rules, clock=self.clock)
        a = bot.step()
        self.assertEqual((len(a.positions), a.paused), (2, ""))
        self.clock.t += timedelta(minutes=2)
        self.src.data = {t: replace(s, price=0.0002) for t, s in self.src.data.items()}
        return bot, bot.step()

    def test_pauses_once_when_down_30_percent(self):
        rules = Rules(bankroll=1200.0, stake=400.0)
        with self.assertLogs("tradinglab.hype.bot", level="INFO") as logs:
            bot, a = self.crash(rules)
            self.assertLess(equity(a, rules), 1200 * 0.7)
            self.assertGreater(a.cash, rules.stake)  # it could afford another coin
            self.assertEqual(a.paused, "down 30% from its start")
            self.src.trending.append("TokC3")
            self.src.data["TokC3"] = hot(addr="TokC3")
            a = next_scan(bot, self.clock)
        self.assertEqual(a.positions, [])  # a fresh hot coin, cash in hand, and still no buy
        self.assertIn("paused", why_nots(a))
        self.assertEqual(len([m for m in logs.output if "down 30% from its start" in m]), 1)

    def test_the_percent_comes_from_the_rule(self):
        with self.assertLogs("tradinglab.hype.bot", level="WARNING"):
            _, a = self.crash(Rules(bankroll=1200.0, stake=400.0, auto_pause_drawdown=0.5))
        self.assertEqual(a.paused, "down 50% from its start")

    def test_no_pause_above_the_line(self):
        _, a = self.crash(Rules(bankroll=1200.0, stake=400.0, auto_pause_drawdown=0.9))
        self.assertEqual(a.paused, "")


class Tick:
    """A clock in epoch seconds that only moves when a test says so."""

    def __init__(self):
        self.t = T0.timestamp()

    def __call__(self):
        return self.t


class CountingSource(FakeSource):
    def __init__(self):
        super().__init__()
        self.discovers, self.price_calls = 0, []
        self.fail_discover = self.fail_price = ""

    def discover(self):
        self.discovers += 1
        if self.fail_discover:
            raise MarketDataError(self.fail_discover)
        return super().discover()

    def snapshots(self, tokens, pairs=None):
        self.price_calls.append(list(tokens))
        if self.fail_price:
            raise MarketDataError(self.fail_price)
        return super().snapshots(tokens, pairs)


def counting(*tokens):
    src = CountingSource()
    src.trending = list(tokens)
    src.data = {t: hot(addr=t) for t in tokens}
    return src


class FeedTest(unittest.TestCase):
    def setUp(self):
        self.tick = Tick()
        self.gecko, self.dex = counting("TokA1", "TokB2"), counting("TokA1", "TokB2")
        self.feed = SharedFeed(discover=(self.gecko, self.dex), price=(self.dex, self.gecko), clock=self.tick)

    def test_positions_are_priced_from_dexscreener_first(self):
        self.assertEqual(set(self.feed.snapshots(["TokA1"], {"TokA1": "PTokA1"})), {"TokA1"})
        self.assertEqual((self.dex.price_calls, self.gecko.price_calls), ([["TokA1"]], []))

    def test_discovery_asks_geckoterminal_first(self):
        self.assertEqual(len(self.feed.discover()), 2)
        self.assertEqual((self.gecko.discovers, self.dex.discovers), (1, 0))

    def test_price_falls_back_to_geckoterminal(self):
        self.dex.fail_price = "DexScreener unavailable: HTTP 503"
        self.assertEqual(set(self.feed.snapshots(["TokA1"])), {"TokA1"})
        self.assertEqual(self.gecko.price_calls, [["TokA1"]])
        self.assertIn("503", self.feed.status()["last_error"])
        self.dex.fail_price = ""
        self.tick.t += PRICE_TTL
        self.feed.snapshots(["TokA1"])
        self.assertEqual(self.feed.status()["last_error"], "")

    def test_a_coin_the_first_price_source_lacks_is_asked_from_the_next(self):
        del self.dex.data["TokB2"]
        self.dex.data["TokA1"] = replace(self.dex.data["TokA1"], price=0.005)
        snaps = self.feed.snapshots(["TokA1", "TokB2"])
        self.assertEqual(self.gecko.price_calls, [["TokB2"]])
        self.assertEqual((snaps["TokA1"].price, snaps["TokB2"].price), (0.005, 0.001))

    def test_a_price_without_pool_depth_is_not_trusted(self):
        self.dex.data["TokA1"] = replace(self.dex.data["TokA1"], liquidity=0.0)
        self.assertEqual(self.feed.snapshots(["TokA1"])["TokA1"].liquidity, 40000)
        del self.gecko.data["TokA1"]
        self.tick.t += PRICE_TTL
        self.assertEqual(self.feed.snapshots(["TokA1"]), {})  # counts as missing, not as a rug

    def test_raises_when_no_price_source_answers(self):
        self.dex.fail_price = self.gecko.fail_price = "HTTP 500"
        with self.assertRaises(RuntimeError):
            self.feed.snapshots(["TokA1"])
        self.assertEqual(self.feed.snapshots([]), {})

    def test_variants_holding_the_same_coin_cost_one_price_request(self):
        for _ in range(3):
            self.feed.snapshots(["TokA1"])
        self.assertEqual(len(self.dex.price_calls), 1)
        self.tick.t += PRICE_TTL - 1
        self.feed.snapshots(["TokA1", "TokB2"])
        self.assertEqual(self.dex.price_calls, [["TokA1"], ["TokB2"]])
        self.tick.t += PRICE_TTL
        self.feed.snapshots(["TokA1"])
        self.assertEqual(len(self.dex.price_calls), 3)

    def test_a_missing_coin_is_remembered_for_the_cycle_too(self):
        for _ in range(3):
            self.assertEqual(self.feed.snapshots(["Gone9"]), {})
        self.assertEqual((len(self.dex.price_calls), len(self.gecko.price_calls)), (1, 1))

    def test_discover_result_is_reused_for_a_minute(self):
        first = self.feed.discover()
        self.tick.t += DISCOVER_TTL - 1
        self.assertEqual(self.feed.discover(), first)
        self.assertEqual(self.gecko.discovers, 1)
        self.tick.t += 1
        self.feed.discover()
        self.assertEqual(self.gecko.discovers, 2)

    def test_429_backs_off_for_five_minutes_and_reuses_the_cache(self):
        self.assertEqual((DISCOVER_TTL, RATE_LIMIT_PAUSE, PRICE_TTL), (60, 300, 8))
        src = counting("TokA1")
        feed = SharedFeed(discover=src, price=src, clock=self.tick)
        first = feed.discover()
        src.fail_discover = "GeckoTerminal unavailable: HTTP 429"
        self.tick.t += 61
        self.assertEqual(feed.discover(), first)  # the failed call falls back to the last good scan
        self.assertEqual(src.discovers, 2)
        src.fail_discover = ""  # it would answer now, but we leave it alone for 300 seconds
        self.tick.t += 60
        self.assertEqual(feed.discover(), first)
        self.assertEqual(src.discovers, 2)
        self.tick.t = T0.timestamp() + 301  # the last good scan is now too old to use
        with self.assertRaises(RuntimeError) as err:
            feed.discover()
        self.assertIn("429", str(err.exception))
        self.assertEqual(src.discovers, 2)
        self.tick.t = T0.timestamp() + 61 + 300
        self.assertEqual(len(feed.discover()), 1)
        self.assertEqual(src.discovers, 3)

    def test_a_rate_limited_provider_is_skipped_while_the_other_still_scans(self):
        self.gecko.fail_discover = "GeckoTerminal unavailable: HTTP 429"
        for _ in range(4):
            self.assertEqual(len(self.feed.discover()), 2)
            self.tick.t += DISCOVER_TTL
        self.assertEqual((self.gecko.discovers, self.dex.discovers), (1, 4))
        self.tick.t += RATE_LIMIT_PAUSE
        self.gecko.fail_discover = ""
        self.feed.discover()
        self.assertEqual(self.gecko.discovers, 2)

    def test_a_plain_outage_is_not_retried_by_every_variant_every_cycle(self):
        self.gecko.fail_discover = self.dex.fail_discover = "timed out"
        for _ in range(3):
            with self.assertRaises(RuntimeError):
                self.feed.discover()
        self.assertEqual((self.gecko.discovers, self.dex.discovers), (1, 1))
        self.tick.t += DISCOVER_TTL
        with self.assertRaises(RuntimeError):
            self.feed.discover()
        self.assertEqual((self.gecko.discovers, self.dex.discovers), (2, 2))

    def test_a_rate_limited_price_source_is_still_asked_for_prices(self):
        self.gecko.fail_price = "GeckoTerminal unavailable: HTTP 429"
        self.dex.fail_price = "HTTP 500"
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                self.feed.snapshots(["TokA1"])
        self.assertEqual(len(self.gecko.price_calls), 2)  # stops matter more than politeness
        self.feed.discover()
        self.assertEqual((self.gecko.discovers, self.dex.discovers), (0, 1))  # but scans leave it alone

    def test_status(self):
        self.assertEqual(self.feed.status(), {"rate_limited_1h": 0, "last_ok": "", "last_error": ""})
        self.gecko.fail_discover = "GeckoTerminal unavailable: HTTP 429"
        self.feed.discover()
        st = self.feed.status()
        self.assertEqual((st["rate_limited_1h"], st["last_ok"]), (1, "2026-11-05T15:00:00+00:00"))
        self.assertIn("429", st["last_error"])
        self.tick.t += 3601
        self.feed.snapshots(["TokA1"])
        st = self.feed.status()
        self.assertEqual((st["rate_limited_1h"], st["last_ok"]), (0, "2026-11-05T16:00:01+00:00"))

    def test_variants_share_one_discover_call_per_minute(self):
        clock = Clock()
        bots = [HypeBot(self.feed, MemoryHypeStore(), v.rules, clock=clock, buys_enabled=v.buys_enabled,
                        name=v.id) for v in VARIANTS]
        twin = HypeBot(self.feed, MemoryHypeStore(), VARIANTS[1].rules, clock=clock, name="twin")
        for cycle in range(1, SCAN_EVERY + 2):
            accounts = [b.step() for b in [*bots, twin]]
            if cycle in (1, SCAN_EVERY):
                self.assertEqual(self.gecko.discovers, 1, cycle)
            if cycle == 2:  # two variants hold the same two coins: one price request between them
                self.assertEqual(self.dex.price_calls, [["TokA1", "TokB2"]])
            clock.t += timedelta(seconds=CYCLE_SECONDS)
            self.tick.t += CYCLE_SECONDS
        self.assertEqual((self.gecko.discovers, self.dex.discovers), (2, 0))
        # paused / buys / too thin / no whale data / no early list / no whale data / the twin buys
        self.assertEqual([len(a.positions) for a in accounts], [0, 2, 0, 0, 0, 0, 2])


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.src = counting("TokA1")
        self.feed = SharedFeed(discover=self.src, price=self.src, clock=Tick())
        self.stores = {v.id: MemoryHypeStore() for v in VARIANTS}
        self.built = 0

    def bots(self):
        self.built += 1
        return [HypeBot(self.feed, self.stores[v.id], v.rules, clock=Clock(), buys_enabled=v.buys_enabled,
                        name=v.id) for v in VARIANTS]

    def runner(self):
        return HypeRunner(self.bots, variants=[(v, self.stores[v.id]) for v in VARIANTS], feed=self.feed)

    def test_runs_every_variant_and_exposes_them_to_the_dashboard(self):
        r = self.runner()
        r.run_once()
        self.assertEqual([v.id for v, _ in r.variants], [v.id for v in VARIANTS])
        self.assertEqual([len(s.load_hype().positions) for _, s in r.variants], [0, 1, 0, 0, 0, 0])
        for _, store in r.variants:
            self.assertEqual(store.hype_trades(5), [])
            self.assertEqual(store.hype_stats()[0], 0)
        self.assertIs(r.store, self.stores["current"])
        self.assertEqual(r.rules, VARIANTS[0].rules)
        self.assertEqual(set(r.feed_status()), {"rate_limited_1h", "last_ok", "last_error"})
        self.assertEqual((r.error, CYCLE_SECONDS, SCAN_EVERY), ("", 10, 6))

    def test_one_failing_variant_does_not_stop_the_others(self):
        def broken(account, trades):
            raise OSError("connection lost")

        self.stores["current"].save_hype = broken
        r = self.runner()
        with self.assertLogs("tradinglab.hype.bot", level="ERROR"):
            r.run_once()
        self.assertIsNone(self.stores["current"].account)
        self.assertEqual(len(self.stores["no_repeat"].load_hype().positions), 1)
        self.assertEqual(self.stores["deep_pools"].load_hype().cycles, 1)
        self.assertIn("current: OSError: connection lost", r.error)
        del self.stores["current"].save_hype
        r.run_once()  # rebuilt with fresh connections, and healthy again
        self.assertEqual((self.built, r.error), (2, ""))
        self.assertEqual(self.stores["current"].load_hype().cycles, 1)

    def test_a_factory_failure_is_reported_and_retried(self):
        def no_database():
            raise OSError("no database")

        r = HypeRunner(no_database)
        with self.assertLogs("tradinglab.hype.bot", level="ERROR"):
            r.run_once()
        self.assertIn("no database", r.error)
        self.assertEqual((r.variants, r.store, r.rules), ([], None, Rules()))
        self.assertEqual(r.feed_status(), {"rate_limited_1h": 0, "last_ok": "", "last_error": ""})

    def test_log_lines_carry_the_variant_id(self):
        with self.assertLogs("tradinglab.hype.bot", level="INFO") as logs:
            self.runner().run_once()
        said = [m.split(":", 2)[2] for m in logs.output]
        self.assertTrue(any(m.startswith("hype[no_repeat]: bought TokA") for m in said), said)
        self.assertTrue(any(m.startswith("hype[deep_pools] scan: 1 coins, 0 bought") for m in said), said)


class FakePg:
    """Records the SQL a store sends; stands in for PostgresStore's _tx/_query."""

    def __init__(self):
        self.sent, self.rows = [], []

    @contextlib.contextmanager
    def _tx(self):
        yield self

    def execute(self, sql, params=()):
        self.sent.append((" ".join(sql.split()), tuple(params)))

    def _query(self, sql, params=()):
        self.execute(sql, params)
        return self.rows


class StoreTest(unittest.TestCase):
    def acct(self):
        return Account(cash=1000.0, started=T0.isoformat())

    def trade(self):
        return Trade("TokA1", "TokA", "u", "o", "c", 50.0, 40.0, "stop loss", "why", 0.1)

    def test_schema_adds_the_accounts_table_and_the_trade_account_column(self):
        pg = FakePg()
        PgHypeStore(pg)
        ddl = [s for s, _ in pg.sent]
        self.assertIn("CREATE TABLE IF NOT EXISTS hype_accounts ( id text PRIMARY KEY, payload text NOT NULL, "
                      "updated_at timestamptz NOT NULL DEFAULT now())", ddl)
        alter = "ALTER TABLE hype_trades ADD COLUMN IF NOT EXISTS account text NOT NULL DEFAULT 'current'"
        self.assertIn(alter, ddl)
        created = next(i for i, s in enumerate(ddl) if s.startswith("CREATE TABLE IF NOT EXISTS hype_trades"))
        self.assertLess(created, ddl.index(alter))  # the column is added after the table surely exists

    def test_current_account_keeps_its_old_row(self):
        pg = FakePg()
        store = PgHypeStore(pg)
        self.assertEqual(store.account, "current")
        pg.sent.clear()
        store.save_hype(self.acct(), [self.trade()])
        self.assertIsNone(store.load_hype())
        (save, save_params), (insert, insert_params), (load, load_params) = pg.sent
        self.assertTrue(save.startswith("INSERT INTO hype_state (id, payload, updated_at) VALUES (1, %s, now())"))
        self.assertEqual(len(save_params), 1)
        self.assertIn("account", insert)
        self.assertEqual(insert_params[-1], "current")
        self.assertEqual((load, load_params), ("SELECT payload FROM hype_state WHERE id = 1", ()))

    def test_other_accounts_use_the_accounts_table(self):
        pg = FakePg()
        store = PgHypeStore(pg, "no_repeat")
        pg.sent.clear()
        store.save_hype(self.acct(), [self.trade()])
        pg.rows = [(json.dumps(self.acct().to_json()),)]
        self.assertEqual(store.load_hype(), self.acct())
        (save, save_params), (_, insert_params), (load, load_params) = pg.sent
        self.assertTrue(save.startswith("INSERT INTO hype_accounts (id, payload, updated_at) VALUES (%s, %s, now())"))
        self.assertEqual(save_params[0], "no_repeat")
        self.assertEqual(insert_params[-1], "no_repeat")
        self.assertEqual((load, load_params), ("SELECT payload FROM hype_accounts WHERE id = %s", ("no_repeat",)))
        self.assertNotIn("hype_state", save + load)

    def test_trades_and_stats_are_filtered_by_account(self):
        pg = FakePg()
        store = PgHypeStore(pg, "deep_pools")
        pg.sent.clear()
        pg.rows = [("TokA1", "TokA", "u", "o", "c", 50.0, 40.0, "stop loss", "why", 0.1)]
        self.assertEqual(store.hype_trades(7), [self.trade()])
        pg.rows = [(3, 1, -12.5, 4.0, -9.0)]
        self.assertEqual(store.hype_stats(), (3, 1, -12.5, 4.0, -9.0))
        (trades, trades_params), (stats, stats_params) = pg.sent
        self.assertIn("WHERE account = %s ORDER BY id DESC LIMIT %s", trades)
        self.assertEqual(trades_params, ("deep_pools", 7))
        self.assertTrue(stats.endswith("FROM hype_trades WHERE account = %s"), stats)
        self.assertEqual(stats_params, ("deep_pools",))

    def test_every_statement_has_a_value_for_every_placeholder(self):
        for account in ("current", "no_repeat"):
            pg = FakePg()
            store = PgHypeStore(pg, account)
            store.save_hype(self.acct(), [self.trade()])
            store.load_hype()
            store.hype_trades()
            pg.rows = [(0, 0, 0, 0, 0)]
            store.hype_stats()
            for sql, params in pg.sent:
                self.assertEqual(sql.count("%s"), len(params), sql)


class PgHypeStoreTest(test_pg_store.PgTestCase):
    """The same SQL against a real PostgreSQL server; skipped where the test server isn't running."""

    def test_accounts_are_kept_apart_and_old_trades_stay_with_current(self):
        with self.store._tx() as cur:  # last week's tables, with a trade from before `account` existed
            cur.execute(SCHEMA[0])
            cur.execute(SCHEMA[1])
            cur.execute("INSERT INTO hype_trades (token, symbol, url, opened, closed, stake, proceeds, "
                        "reason, why, peak_gain) VALUES ('Old1', 'OLD', 'u', 'o', 'c', 50, 20, 'stop loss', "
                        "'why', 0)")
        current, other = PgHypeStore(self.store), PgHypeStore(self.store, "no_repeat")
        PgHypeStore(self.store, "no_repeat")  # setting up twice is harmless
        self.assertIsNone(current.load_hype())
        self.assertIsNone(other.load_hype())
        new = Trade("TokA1", "TokA", "u", "o", "c", 50.0, 40.0, "stop loss", "why", 0.1)
        current.save_hype(Account(cash=1.0, started=T0.isoformat()), [])
        other.save_hype(Account(cash=2.0, started=T0.isoformat()), [new])
        other.save_hype(Account(cash=3.0, started=T0.isoformat(), banned=["TokA1"]), [])
        self.assertEqual(current.load_hype().cash, 1.0)
        self.assertEqual((other.load_hype().cash, other.load_hype().banned), (3.0, ["TokA1"]))
        self.assertEqual([t.token for t in current.hype_trades()], ["Old1"])
        self.assertEqual(other.hype_trades(5), [new])
        self.assertEqual(current.hype_stats(), (1, 0, -30.0, -30.0, -30.0))
        self.assertEqual(other.hype_stats(), (1, 0, -10.0, -10.0, -10.0))
        self.assertEqual(PgHypeStore(self.store, "deep_pools").hype_stats(), (0, 0, 0.0, 0.0, 0.0))

    def test_the_scouts_document_lives_beside_the_accounts_without_becoming_one(self):
        scout = PgHypeStore(self.store, "scout")
        self.assertIsNone(scout.load_doc("scout"))
        doc = {"alerts": [{"kind": "early", "title": "MARS", "price": 0.001}], "wallets": {"w1": [1, 1, 0.5, "", "MARS"]}}
        scout.save_doc("scout", doc)
        scout.save_doc("scout", {**doc, "updated": "now"})
        self.assertEqual(scout.load_doc("scout"), {**doc, "updated": "now"})
        self.assertIsNone(scout.load_hype())  # the document is not mistaken for the scout "account"
        self.assertIsNone(PgHypeStore(self.store, "_scout").load_hype() and None)


class RateLimitedClientTest(unittest.TestCase):
    def http(self, status):
        self.urls = []

        def http_get(url, headers, timeout):
            self.urls.append(url)
            return status, b"[]"
        return http_get

    def gecko(self, status):
        from tradinglab.hype.geckoterminal import GeckoTerminalClient
        return GeckoTerminalClient(http_get=self.http(status), sleep=lambda s: None)

    def test_dexscreener_does_not_retry_a_429(self):
        c = DexScreenerClient(http_get=self.http(429), sleep=lambda s: None)
        with self.assertRaises(MarketDataError) as err:
            c.snapshots(["TokA1"])
        self.assertIn("429", str(err.exception))
        self.assertEqual(len(self.urls), 1)

    def test_dexscreener_discover_reports_a_rate_limit_instead_of_an_empty_list(self):
        c = DexScreenerClient(http_get=self.http(429), sleep=lambda s: None)
        with self.assertLogs("tradinglab.hype", level="WARNING"), self.assertRaises(MarketDataError) as err:
            c.discover()
        self.assertIn("429", str(err.exception))
        self.assertEqual(len(self.urls), 1)  # the other lists are left alone once one says "slow down"

    def test_dexscreener_discover_still_works_when_one_list_is_down(self):
        def http_get(url, headers, timeout):
            if "/token-boosts/latest/" in url:
                return 500, b""
            if "/tokens/v1/" in url:
                return 200, json.dumps([pair("TokA1")]).encode()
            return 200, json.dumps([{"chainId": "solana", "tokenAddress": "TokA1"}]).encode()

        c = DexScreenerClient(http_get=http_get, sleep=lambda s: None, clock_ms=lambda: NOW_MS)
        with self.assertLogs("tradinglab.hype", level="WARNING"):
            self.assertEqual([s.token for s in c.discover()], ["TokA1"])

    def test_geckoterminal_discover_stops_at_the_first_429(self):
        with self.assertLogs("tradinglab.hype", level="WARNING"), self.assertRaises(MarketDataError) as err:
            self.gecko(429).discover()
        self.assertIn("429", str(err.exception))
        self.assertEqual(len(self.urls), 1)

    def test_geckoterminal_discover_raises_when_every_list_is_down(self):
        with self.assertLogs("tradinglab.hype", level="WARNING"), self.assertRaises(MarketDataError):
            self.gecko(503).discover()
        self.assertEqual(len(self.urls), 9)  # three lists, three tries each: outages are still retried


class StartHypeTest(unittest.TestCase):
    def test_wires_three_variants_one_feed_and_the_env_overrides(self):
        from tradinglab.web import server

        gecko, dex = counting("TokA1"), counting("TokA1")
        stores: dict[str, MemoryHypeStore] = {}
        state = SimpleNamespace(store=object(), hype=None)
        with mock.patch.object(HypeRunner, "start"), \
                mock.patch("tradinglab.engine.runtime.postgres_store", return_value=object()), \
                mock.patch("tradinglab.hype.store.PgHypeStore",
                           lambda pg, account="current": stores.setdefault(account, MemoryHypeStore())), \
                mock.patch("tradinglab.hype.geckoterminal.GeckoTerminalClient", lambda: gecko), \
                mock.patch("tradinglab.hype.dexscreener.DexScreenerClient", lambda: dex):
            server.start_hype(state, None, {"HYPE_BANKROLL": "500", "HYPE_STAKE": "20"})
            runner = state.hype
            runner.run_once()
            runner.run_once()
        self.assertEqual(runner.error, "")
        self.assertEqual([v.id for v, _ in runner.variants], [v.id for v in VARIANTS])
        for (v, store), base in zip(runner.variants, VARIANTS, strict=True):
            self.assertEqual(v.rules, replace(base.rules, bankroll=500.0, stake=20.0))
            self.assertEqual(v.buys_enabled, base.buys_enabled)
            self.assertIs(store, stores[v.id])
        self.assertIs(runner.store, stores["current"])
        self.assertEqual((runner.rules.bankroll, runner.rules.stake), (500.0, 20.0))
        self.assertEqual([stores[k].account.cash for k in ("current", "no_repeat", "deep_pools")],
                         [500.0, 480.0, 500.0])
        # scans: GeckoTerminal first; DexScreener's list is asked once, for the early coins
        self.assertEqual((gecko.discovers, dex.discovers), (1, 1))
        self.assertEqual((dex.price_calls, gecko.price_calls), ([["TokA1"]], []))  # prices: DexScreener first
        self.assertEqual(runner.feed_status()["rate_limited_1h"], 0)

    def test_hype_bot_can_be_switched_off(self):
        from tradinglab.web import server

        state = SimpleNamespace(store=object(), hype=None)
        server.start_hype(state, None, {"HYPE_BOT": "off"})
        self.assertIsNone(state.hype)


class SafetyTest(unittest.TestCase):
    def test_dexscreener_allowed_and_trading_venues_denied(self):
        self.assertEqual(policy().check(CRYPTO_DATA_HOST), CRYPTO_DATA_HOST)
        for host in ("pump.fun", "frontend-api.pump.fun", "axiom.trade", "api.axiom.trade",
                     "quote-api.jup.ag", "api.raydium.io", "gmgn.ai", "phantom.app"):
            self.assertTrue(is_brokerage_host(host), host)



class QuietCycleTests(unittest.TestCase):
    """An idle account is not rewritten every 10 seconds, and the scan cadence is unaffected."""

    def test_quiet_account_writes_about_once_a_minute_and_keeps_counting(self):
        from tradinglab.hype.bot import SCAN_EVERY

        class One:
            scans = 0

            def discover(self):
                One.scans += 1
                return [hot(buys=10, sells=50)]  # on the radar, never bought (sellers win)

            def snapshots(self, tokens, pairs=None):
                return {}

        class Counting(MemoryHypeStore):
            saves = 0

            def save_hype(self, account, trades):
                Counting.saves += 1
                super().save_hype(account, trades)

        t = [datetime(2026, 11, 20, 12, 0, tzinfo=UTC)]
        store = Counting()
        bot = HypeBot(One(), store, clock=lambda: t[0])
        cycles = SCAN_EVERY * 4
        for _ in range(cycles):
            bot.step()
            t[0] += timedelta(seconds=10)
        self.assertEqual(store.account.positions, [])
        self.assertEqual(One.scans, 4)  # the scan cadence is exactly what it was
        self.assertLessEqual(Counting.saves, 4 + 1)  # the scans, plus at most one chart point
        self.assertEqual(store.account.cycles + bot._quiet, cycles)


if __name__ == "__main__":
    unittest.main()
