"""Hype paper bot: parsing, scoring, the cost model, entries, exits, rugs, and safety."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from tradinglab.hype.bot import HypeBot, MemoryHypeStore, equity
from tradinglab.hype.dexscreener import DexScreenerClient, MarketDataError, parse_pair
from tradinglab.hype.model import Rules, Snapshot, buy_fill, entry_check, score, sell_fill
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

    def trending_tokens(self):
        return list(self.trending)

    def snapshots(self, tokens):
        return {t: self.data[t] for t in tokens if t in self.data}


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


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
        a = self.move(2, price=0.001)
        self.assertEqual(a.positions, [])
        self.assertTrue(any("cooldown" in r for w in a.watch for r in w["why_not"]))

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
            def snapshots(self, tokens):
                raise MarketDataError("down")

        self.bot.step()
        self.bot.source = Broken()
        self.bot.source.trending = ["TokA1"]
        self.clock.t += timedelta(minutes=1)
        a = self.bot.step()
        self.assertEqual(len(a.positions), 1)  # nothing sold on missing data
        self.assertIn("down", a.last_error)


class SafetyTest(unittest.TestCase):
    def test_dexscreener_allowed_and_trading_venues_denied(self):
        self.assertEqual(policy().check(CRYPTO_DATA_HOST), CRYPTO_DATA_HOST)
        for host in ("pump.fun", "frontend-api.pump.fun", "axiom.trade", "api.axiom.trade",
                     "quote-api.jup.ag", "api.raydium.io", "gmgn.ai", "phantom.app"):
            self.assertTrue(is_brokerage_host(host), host)


if __name__ == "__main__":
    unittest.main()
