"""Phone alerts: what is sent to Discord, how it is made safe, rationing, and the daily recap."""

from __future__ import annotations

import io
import json
import logging
import unittest
from datetime import timedelta, timezone

from tests.helpers import settings
from tests.test_hype import NOW_MS, Clock, Tick
from tests.test_scout import CLEAN, MINT, Market, curve_pair
from tradinglab.config import Secret
from tradinglab.hype.bot import MemoryHypeStore
from tradinglab.hype.dexscreener import parse_pair
from tradinglab.hype.notify import (
    PUSH_PER_HOUR,
    DiscordNotifier,
    alert_text,
    clean,
    digest_text,
    worth_sending,
)
from tradinglab.hype.rugcheck import Verdict
from tradinglab.hype.scout import Scout

HOOK = "https://discord.com/api/webhooks/123456789012345678/" + "s3cr3t" * 10
PASSED = {"checked": True, "score": 7, "dangers": [], "warnings": []}


def alert(kind="whales", score=80.0, safety=PASSED, title="PENNY", text="4 big wallets bought $6,300", **kw):
    return {"kind": kind, "score": score, "safety": safety, "title": title, "text": text,
            "links": {"DexScreener": "https://dexscreener.com/solana/abc"}, **kw}


class Outbox:
    """A stand-in for Discord: records what would be posted."""

    def __init__(self, status=204, reply=b""):
        self.status, self.reply, self.posts = status, reply, []

    def __call__(self, url, body, headers, timeout):
        self.posts.append((url, json.loads(body)))
        return self.status, self.reply


class MessageTests(unittest.TestCase):
    def test_only_checked_coins_that_score_well_are_worth_a_buzz(self):
        self.assertTrue(worth_sending(alert()))
        self.assertFalse(worth_sending(alert(score=40.0)))
        self.assertFalse(worth_sending(alert(safety={"checked": False})))
        self.assertFalse(worth_sending(alert(safety={**PASSED, "dangers": ["rugged before"]})))
        self.assertTrue(worth_sending(alert(safety={**PASSED, "warnings": ["top holders own a lot"]})))
        self.assertTrue(worth_sending(alert(kind="theme", safety={"checked": False}, score=60.0)))

    def test_outside_text_cannot_format_link_or_ping(self):
        nasty = "@everyone **FREE** [click](https://evil.example) `x` <@123> discord.gg/scam\nnew line"
        got = clean(nasty)
        for bad in ("@", "*", "`", "[", "(", "<", "http", "discord.gg", "\n"):
            self.assertNotIn(bad, got)
        self.assertIn("FREE", got)
        text = alert_text(alert(title=nasty, text=nasty))
        self.assertEqual(text.count("@"), 0)
        self.assertNotIn("evil.example", text)

    def test_the_message_says_what_it_is_and_links_only_to_known_sites(self):
        text = alert_text(alert())
        self.assertIn("**WHALES BUYING · PENNY** · score 80 · passed scam check", text)
        self.assertIn("4 big wallets bought $6,300", text)
        self.assertTrue(text.endswith("<https://dexscreener.com/solana/abc>"))
        odd = alert_text(alert(links={"x": "https://evil.example/a", "y": "javascript:alert(1)"}))
        self.assertNotIn("<", odd)
        warned = alert_text(alert(safety={**PASSED, "warnings": ["top 10 holders high ownership"]}))
        self.assertIn("caution: top 10 holders high ownership", warned)
        self.assertNotIn("scam check", alert_text(alert(kind="theme", title="GTA")))

    def test_the_daily_recap_reports_how_alerts_did(self):
        stats = {"early": {"n": 6, "ran": 2, "peak": 0.4, "now": -0.3},
                 "whales": {"n": 2, "ran": 1, "peak": 0.9, "now": 0.2}}
        alerts = [alert(title="LOOSY", kind="smart", price=1.0, peak=2.6), alert(title="MUR", price=1.0, peak=1.1)]
        text = digest_text(stats, alerts, 5, "Sat Nov 21")
        self.assertIn("8 coin alerts in the last 24 hours, 3 went up 20% or more", text)
        self.assertIn("5 coins kept out by the scam check", text)
        self.assertIn("before migration: 6 alerts, 2 ran, now -30% on average", text)
        self.assertIn("Best: LOOSY, up 160% at its high (proven wallet).", text)
        self.assertIn("not tips", text)


def ALLOWED(url):  # stands in for an egress policy that had the webhook host added at startup
    return None


class DiscordTests(unittest.TestCase):
    def test_nothing_is_posted_unless_the_host_was_allowed_at_startup(self):
        out = Outbox()
        n = DiscordNotifier(Secret(HOOK), http_post=out, clock=Tick())  # the real network guard
        self.assertFalse(n.send("hello"))
        self.assertEqual((out.posts, n.status()["last_error"]), ([], "EgressDeniedError"))

    def test_posts_quietly_pings_nobody_and_keeps_count(self):
        out, tick = Outbox(), Tick()
        n = DiscordNotifier(Secret(HOOK), http_post=out, clock=tick, check_url=ALLOWED)
        self.assertTrue(n.send("hello " + "x" * 3000))
        url, body = out.posts[0]
        self.assertEqual(url, HOOK)
        self.assertEqual((body["allowed_mentions"], body["flags"]), ({"parse": []}, 4))
        self.assertLessEqual(len(body["content"]), 1900)
        self.assertEqual(n.status()["last_error"], "")
        self.assertFalse(n.send("   "))
        for _ in range(PUSH_PER_HOUR + 3):
            n.send("more")
        self.assertEqual(len(out.posts), PUSH_PER_HOUR)  # rationed: the rest were not posted
        tick.t += 3601
        self.assertTrue(n.send("an hour later"))

    def test_slows_down_when_told_and_never_reveals_the_address(self):
        out, tick = Outbox(429, b'{"retry_after": 30}'), Tick()
        n = DiscordNotifier(Secret(HOOK), http_post=out, clock=tick, check_url=ALLOWED)
        logs = io.StringIO()
        handler = logging.StreamHandler(logs)
        logging.getLogger("tradinglab.hype.notify").addHandler(handler)
        self.addCleanup(logging.getLogger("tradinglab.hype.notify").removeHandler, handler)
        self.assertFalse(n.send("hi"))
        self.assertEqual((n.room(), n.status()["last_error"]), (0, "HTTP 429"))
        self.assertFalse(n.send("again"))
        self.assertEqual(len(out.posts), 1)  # it waited instead of asking again
        tick.t += 31
        out.status = 204
        self.assertTrue(n.send("now"))

        def boom(url, body, headers, timeout):
            raise ConnectionError(f"could not reach {url}")

        broken = DiscordNotifier(Secret(HOOK), http_post=boom, clock=tick, check_url=ALLOWED)
        self.assertFalse(broken.send("hi"))
        self.assertEqual(broken.status()["last_error"], "ConnectionError")
        for text in (logs.getvalue(), repr(broken.status()), repr(Secret(HOOK))):
            self.assertNotIn("s3cr3t", text)


class SettingTests(unittest.TestCase):
    def test_the_host_is_allowed_only_when_a_real_webhook_is_set(self):
        self.assertIsNone(settings().discord_webhook)
        self.assertNotIn("discord.com", settings().egress_hosts)
        s = settings(DISCORD_WEBHOOK_URL=HOOK)
        self.assertEqual(s.discord_webhook.get(), HOOK)
        self.assertIn("discord.com", s.egress_hosts)
        self.assertNotIn("s3cr3t", repr(s))
        for bad in (HOOK.replace("https", "http"), HOOK.replace("discord.com", "discord.com.evil.io"),
                    "https://discord.com/api/webhooks/123456789012345678/short", HOOK + "/extra?x=1",
                    "https://example.com/hook"):
            s = settings(DISCORD_WEBHOOK_URL=bad)  # a typo turns alerts off; it never stops the app
            self.assertIsNone(s.discord_webhook, bad[:40])
            self.assertIn("doesn't look like a Discord webhook", s.discord_problem)
            self.assertNotIn("discord.com", s.egress_hosts)
            self.assertNotIn("discord.com.evil.io", s.egress_hosts)
        self.assertEqual(settings(DISCORD_WEBHOOK_URL="  ").discord_problem, "")
        self.assertEqual(settings(DISCORD_WEBHOOK_URL=f"  {HOOK}\n").discord_webhook.get(), HOOK)


class FakeNotifier:
    def __init__(self):
        self.sent, self.ok = [], True

    def room(self):
        return 6

    def send(self, text):
        if self.ok:
            self.sent.append(text)
        return self.ok


class ScoutPushTests(unittest.TestCase):
    TZ = timezone(timedelta(hours=-7))  # T0 is 15:00 UTC = 8:00 am there

    def setUp(self):
        self.m, self.clock, self.note = Market(), Clock(), FakeNotifier()
        self.scout = Scout(self.m, MemoryHypeStore(), clock=self.clock, notifier=self.note, tz=self.TZ)
        self.m.verdicts[MINT] = Verdict(3, (), ())

    def busy(self, addr=MINT):
        return parse_pair(curve_pair(addr=addr, buys=400, sells=100, v5=60000), NOW_MS)

    def test_a_good_checked_alert_is_sent_once(self):
        self.m.early_coins = [self.busy()]
        doc = self.scout.step()
        self.assertEqual(len(self.note.sent), 1)
        self.assertIn("BEFORE MIGRATION", self.note.sent[0])
        self.assertIn("passed scam check", self.note.sent[0])
        self.assertTrue(doc["alerts"][0]["pushed"])
        self.clock.t += timedelta(minutes=1)
        self.scout.step()
        self.assertEqual(len(self.note.sent), 1)

    def test_an_unchecked_or_weak_alert_stays_on_the_page_only(self):
        self.m.verdicts[MINT] = None  # the scam check is down
        self.m.early_coins = [self.busy()]
        doc = self.scout.step()
        self.assertEqual((len(doc["alerts"]), self.note.sent), (1, []))
        quiet = Scout(self.m, MemoryHypeStore(), clock=self.clock, notifier=self.note, tz=self.TZ)
        self.m.verdicts[MINT] = CLEAN
        # qualifies as an alert, but with no volume surge and no move it scores too low to buzz
        self.m.early_coins = [parse_pair(curve_pair(buys=40, sells=25, v5=1000, c5=0.0, c1h=-5.0), NOW_MS)]
        doc = quiet.step()
        self.assertEqual((len(doc["alerts"]), self.note.sent), (1, []))

    def test_a_message_that_fails_is_tried_again_until_it_is_old_news(self):
        self.note.ok = False
        self.m.early_coins = [self.busy()]
        self.scout.step()
        self.note.ok = True
        self.clock.t += timedelta(minutes=1)
        self.scout.step()
        self.assertEqual(len(self.note.sent), 1)
        late = Scout(self.m, MemoryHypeStore(), clock=self.clock, notifier=self.note, tz=self.TZ)
        self.note.ok = False
        self.m.early_coins = [self.busy("LateBBBpump")]
        self.m.verdicts["LateBBBpump"] = CLEAN
        late.step()
        self.note.ok = True
        self.clock.t += timedelta(minutes=45)
        late.step()
        self.assertEqual(len(self.note.sent), 1)  # 45 minutes on, it is not sent

    def test_the_daily_recap_goes_out_once_after_nine_in_the_morning(self):
        self.m.early_coins = [self.busy()]
        self.scout.step()  # 8:00 am: an alert, no recap yet
        self.assertEqual(len(self.note.sent), 1)
        self.m.early_coins = []  # the coin drops off the list
        self.clock.t += timedelta(minutes=61)  # 9:01 am
        self.scout.step()
        self.assertEqual(len(self.note.sent), 2)
        self.assertIn("Daily recap", self.note.sent[1])
        self.assertIn("1 coin alerts in the last 24 hours", self.note.sent[1])
        self.clock.t += timedelta(hours=3)
        self.scout.step()
        self.assertEqual(len(self.note.sent), 2)  # once a day
        self.clock.t += timedelta(hours=24)
        self.scout.step()
        self.assertEqual(len([t for t in self.note.sent if "Daily recap" in t]), 1)  # nothing new to recap

    def test_a_broken_notifier_never_stops_the_scout(self):
        def boom(text):
            raise RuntimeError("discord is down")

        self.note.send = boom
        self.m.early_coins = [self.busy()]
        doc = self.scout.step()
        self.assertEqual((len(doc["alerts"]), doc["error"]), (1, ""))

    def test_without_a_notifier_nothing_is_sent_and_nothing_breaks(self):
        plain = Scout(self.m, MemoryHypeStore(), clock=self.clock)
        self.m.early_coins = [self.busy()]
        self.assertEqual(len(plain.step()["alerts"]), 1)


if __name__ == "__main__":
    unittest.main()
