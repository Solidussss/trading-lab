"""Following good traders: reading a wallet's trades from the chain (read-only), the background
watch, and what the scout does with a followed trader's buy."""

from __future__ import annotations

import json
import unittest
from datetime import timedelta
from unittest import mock

from tests.test_hype import T0, Clock, hot
from tests.test_scout import RUGGER, Market
from tradinglab.hype import traders
from tradinglab.hype.bot import MemoryHypeStore
from tradinglab.hype.chain import (
    READ_METHODS,
    USDC,
    WRAPPED_SOL,
    ChainError,
    Move,
    SolanaReadClient,
    is_address,
    parse_move,
)
from tradinglab.hype.known_traders import KNOWN, Trader
from tradinglab.hype.notify import alert_text, worth_sending
from tradinglab.hype.scout import Scout
from tradinglab.hype.traders import TX_PER_WALLET, TraderFeed, followed
from tradinglab.safety.endpoints import CHAIN_READ_URLS, EgressDeniedError
from tradinglab.web.views_hype import traders_section

ANSEM, BIGDOG, NOBODY = "Trader1" + "A" * 30, "Trader2" + "B" * 30, "Nobody3" + "C" * 30
COIN, OTHER = "CoinXXX" + "D" * 30, "CoinYYY" + "E" * 30
SIG = "5" * 88
TWO = (Trader("Ansem", ANSEM), Trader("Big Dog", BIGDOG))


def sig(n):
    return (str(n) * 88)[:88]


def tx(wallet=ANSEM, coin=COIN, coins=(0, 1000), sol=(5.0, 3.0), err=None, at=T0, more=()):
    """A transaction as a node reports it: the wallet's coin and SOL before and after. `more`
    adds (mint, before, after) rows for the same wallet."""
    def side(i):
        rows = [{"owner": "SomePool", "mint": coin, "uiTokenAmount": {"uiAmountString": "900000"}}]
        for mint, *amounts in [(coin, *coins), *more]:
            if amounts[i]:
                rows.append({"owner": wallet, "mint": mint,
                             "uiTokenAmount": {"uiAmountString": str(amounts[i])}})
        return rows

    return {"blockTime": int(at.timestamp()),
            "meta": {"err": err, "preBalances": [int(sol[0] * 1e9), 7], "postBalances": [int(sol[1] * 1e9), 7],
                     "preTokenBalances": side(0), "postTokenBalances": side(1)},
            "transaction": {"message": {"accountKeys": [{"pubkey": wallet}, {"pubkey": "SomePool"}]}}}


class ReadingATransactionTests(unittest.TestCase):
    def test_more_coin_and_less_sol_is_a_buy_and_the_reverse_is_a_sell(self):
        buy = parse_move(tx(), ANSEM, SIG)
        self.assertEqual((buy.side, buy.token, buy.tokens, round(buy.sol, 6), buy.usd, buy.at, buy.sig),
                         ("buy", COIN, 1000.0, 2.0, 0.0, T0, SIG))
        sell = parse_move(tx(coins=(1000, 250), sol=(3.0, 4.5)), ANSEM)
        self.assertEqual((sell.side, sell.tokens, round(sell.sol, 6)), ("sell", 750.0, 1.5))

    def test_paying_with_wrapped_sol_or_dollars_counts_too(self):
        wrapped = parse_move(tx(sol=(5.0, 4.99999), more=[(WRAPPED_SOL, 3, 1)]), ANSEM)
        self.assertEqual((wrapped.side, round(wrapped.sol, 3)), ("buy", 2.0))
        dollars = parse_move(tx(sol=(5.0, 4.99999), more=[(USDC, 500, 300)]), ANSEM)
        self.assertEqual((dollars.side, dollars.sol, dollars.usd), ("buy", 0.0, 200.0))

    def test_anything_that_is_not_coin_for_money_is_ignored(self):
        self.assertIsNone(parse_move(tx(err={"InstructionError": [0, "Custom"]}), ANSEM))  # it failed
        self.assertIsNone(parse_move(tx(sol=(5.0, 4.99999)), ANSEM))  # coins arrived, nothing paid
        self.assertIsNone(parse_move(tx(coins=(1000, 0), sol=(5.0, 4.99999)), ANSEM))  # sent away
        self.assertIsNone(parse_move(tx(more=[(OTHER, 50, 0)]), ANSEM))  # one coin for another
        self.assertIsNone(parse_move(tx(sol=(5.0, 3.0), coins=(1000, 1000)), ANSEM))  # just SOL moving
        self.assertIsNone(parse_move(tx(), BIGDOG))  # somebody else's trade
        for junk in (None, [], "x", {}, {"meta": None}, {"meta": {}, "transaction": {"message": {}}},
                     {**tx(), "blockTime": None}):
            self.assertIsNone(parse_move(junk, ANSEM))


class Node:
    """Stands in for the nodes: answers from a script and records every request."""

    def __init__(self, *replies):
        self.replies, self.asked = list(replies), []

    def __call__(self, url, body, headers, timeout):
        self.asked.append((url, json.loads(body)))
        reply = self.replies.pop(0) if self.replies else (200, {"result": []})
        if isinstance(reply, Exception):
            raise reply
        status, payload = reply
        return status, payload if isinstance(payload, bytes) else json.dumps(payload).encode()


def client(node, **kw):
    return SolanaReadClient(http_post=node, check_url=lambda url: None, **kw)


class ReadOnlyClientTests(unittest.TestCase):
    def test_only_the_two_reads_ever_leave_the_program(self):
        self.assertEqual(READ_METHODS, {"getSignaturesForAddress", "getTransaction"})
        node = Node()
        c = client(node)
        for method in ("sendTransaction", "sendRawTransaction", "simulateTransaction", "requestAirdrop",
                       "getBalance", ""):
            with self.subTest(method=method), self.assertRaises(ChainError):
                c._call(method, [])
        self.assertEqual(node.asked, [])

    def test_asks_for_a_wallets_latest_transactions_and_reads_one(self):
        node = Node((200, {"result": [{"signature": SIG, "err": None}, "junk", {"err": None}]}),
                    (200, {"result": tx()}))
        c = client(node)
        self.assertEqual(c.signatures(ANSEM, until=sig(4), limit=5000), [{"signature": SIG, "err": None}])
        self.assertEqual(c.move(SIG, ANSEM).side, "buy")
        first, second = (body for _, body in node.asked)
        self.assertEqual((first["method"], first["params"]),
                         ("getSignaturesForAddress", [ANSEM, {"limit": 1000, "until": sig(4)}]))
        self.assertEqual((second["method"], second["params"][0]), ("getTransaction", SIG))
        self.assertTrue(all(url == CHAIN_READ_URLS[0] for url, _ in node.asked))

    def test_refuses_things_that_are_not_addresses_without_asking(self):
        node = Node()
        c = client(node)
        for call in (lambda: c.signatures("not a wallet"), lambda: c.signatures(ANSEM + "/../x"),
                     lambda: c.signatures(ANSEM, until="short"), lambda: c.move("nope", ANSEM),
                     lambda: c.move(SIG, "0OIl")):
            with self.assertRaises(ChainError):
                call()
        self.assertEqual(node.asked, [])
        self.assertTrue(is_address(ANSEM))
        self.assertFalse(is_address("0" * 40))  # 0, O, I and l are not in the alphabet

    def test_a_node_that_refuses_is_passed_over_and_the_working_one_is_asked_first_next_time(self):
        node = Node((429, b"slow down"), (200, {"result": []}), (200, {"result": []}))
        c = client(node)
        self.assertEqual(c.signatures(ANSEM), [])
        self.assertEqual([url for url, _ in node.asked], list(CHAIN_READ_URLS))
        c.signatures(ANSEM)
        self.assertEqual(node.asked[-1][0], CHAIN_READ_URLS[1])
        self.assertEqual(c.node(), "solana-rpc.publicnode.com")

    def test_when_no_node_answers_the_error_says_why(self):
        node = Node(TimeoutError(), (200, b"<html>"), (500, b""), (200, {"error": {"code": -32005}}))
        c = client(node)
        with self.assertRaises(ChainError) as first:
            c.signatures(ANSEM)
        self.assertIn("TimeoutError", str(first.exception))
        self.assertIn("malformed reply", str(first.exception))
        with self.assertRaises(ChainError) as second:
            c.signatures(ANSEM)
        self.assertIn("HTTP 500", str(second.exception))
        self.assertIn("error -32005", str(second.exception))

    def test_a_transaction_too_new_to_read_is_skipped_not_an_error(self):
        c = client(Node((200, {"error": {"code": -32015, "message": "version not supported"}})))
        self.assertIsNone(c.move(SIG, ANSEM))

    def test_every_request_is_checked_against_the_egress_policy(self):
        node = Node()

        def denied(url):
            raise EgressDeniedError("not on the list")

        with self.assertRaises(EgressDeniedError):
            SolanaReadClient(http_post=node, check_url=denied).signatures(ANSEM)
        self.assertEqual(node.asked, [])


class Chain:
    """A fake read client: each wallet's transactions, newest first."""

    def __init__(self):
        self.history: dict[str, list[dict]] = {}
        self.txs: dict[str, dict] = {}
        self.fail = ""
        self.calls: list = []

    def add(self, wallet, n, transaction, err=None):
        self.history.setdefault(wallet, []).insert(0, {"signature": sig(n), "err": err})
        self.txs[sig(n)] = transaction

    def signatures(self, wallet, until="", limit=15):
        self.calls.append(("signatures", wallet, until, limit))
        if self.fail:
            raise ChainError(self.fail)
        rows = self.history.get(wallet, [])
        if until:
            rows = rows[:[r["signature"] for r in rows].index(until)]
        return rows[:limit]

    def move(self, signature, wallet):
        self.calls.append(("move", signature))
        return parse_move(self.txs[signature], wallet, signature)

    def node(self):
        return "a.node"


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.chain, self.naps = Chain(), []
        self.feed = TraderFeed(self.chain, clock=lambda: 1000.0, sleep=self.naps.append, pass_seconds=2.0)

    def test_the_first_look_only_notes_where_history_stands(self):
        self.chain.add(ANSEM, 1, tx())
        self.assertEqual(self.feed.poll(ANSEM), [])
        self.assertEqual(self.chain.calls, [("signatures", ANSEM, "", 1)])  # no transaction was read
        self.assertEqual(self.feed.poll(ANSEM), [])  # and nothing new since

    def test_new_trades_come_back_oldest_first_without_the_failed_ones(self):
        self.chain.add(ANSEM, 1, tx())
        self.feed.poll(ANSEM)
        self.chain.add(ANSEM, 2, tx(coin=COIN))
        self.chain.add(ANSEM, 3, tx(), err={"x": 1})
        self.chain.add(ANSEM, 4, tx(coin=OTHER, coins=(500, 0), sol=(3.0, 4.0)))
        self.chain.add(ANSEM, 5, tx(sol=(5.0, 4.99999)))  # a transfer: read, not a trade
        moves = self.feed.poll(ANSEM)
        self.assertEqual([(m.side, m.token, m.sig) for m in moves],
                         [("buy", COIN, sig(2)), ("sell", OTHER, sig(4))])
        self.assertNotIn(("move", sig(3)), self.chain.calls)
        self.assertEqual(self.feed.poll(ANSEM), [])

    def flood(self, wallet, first, count, err=None):
        for n in range(first, first + count):
            self.chain.history.setdefault(wallet, []).insert(0, {"signature": f"{n:0>88}", "err": err})
            self.chain.txs[f"{n:0>88}"] = tx(wallet=wallet, sol=(5.0, 4.99999))  # somebody else's doing

    def test_a_flooded_wallet_is_sampled_and_then_looked_at_less_often(self):
        self.feed.follow([ANSEM, BIGDOG])
        for w in (ANSEM, BIGDOG):
            self.chain.add(w, 1, tx(wallet=w))
        self.feed.run_once()  # both wallets noted
        self.flood(ANSEM, 100, TX_PER_WALLET + 4)
        self.chain.calls.clear()
        self.feed.run_once()
        self.assertEqual(sum(c[0] == "move" for c in self.chain.calls), TX_PER_WALLET)  # a sample
        self.assertEqual(self.feed.status()["flooded"], [ANSEM])
        self.chain.calls.clear()
        for _ in range(traders.FLOOD_REST):
            self.feed.run_once()
        self.assertEqual({c[1] for c in self.chain.calls}, {BIGDOG})  # it sat those passes out
        self.feed.run_once()
        self.assertIn(ANSEM, {c[1] for c in self.chain.calls})  # and is looked at again
        self.assertEqual(self.feed.status()["flooded"], [])  # the flood has stopped

    def test_a_flood_of_failed_look_alikes_costs_no_reads_and_widens_the_next_look(self):
        self.chain.add(ANSEM, 1, tx())
        self.feed.poll(ANSEM)
        self.flood(ANSEM, 100, traders.NARROW + 50, err={"x": 1})
        self.chain.calls.clear()
        self.assertEqual(self.feed.poll(ANSEM), [])
        self.assertEqual(self.chain.calls, [("signatures", ANSEM, sig(1), traders.NARROW)])
        self.flood(ANSEM, 900, 30, err={"x": 1})
        self.chain.add(ANSEM, 2, tx())  # the wallet's own buy, in among them
        self.chain.calls.clear()
        self.assertEqual([m.side for m in self.feed.poll(ANSEM)], ["buy"])
        self.assertEqual(self.chain.calls[0][3], traders.WIDE)
        self.assertEqual(self.feed.status()["flooded"], [])  # failed look-alikes are free to skip
        self.feed.poll(ANSEM)
        self.assertEqual(self.chain.calls[-1][3], traders.NARROW)  # quiet again

    def test_one_pass_looks_at_every_wallet_at_a_steady_pace(self):
        self.feed.follow([ANSEM, BIGDOG, ANSEM])
        for w in (ANSEM, BIGDOG):
            self.chain.add(w, 1, tx(wallet=w))
        self.assertTrue(self.feed.run_once())
        self.chain.add(BIGDOG, 2, tx(wallet=BIGDOG))
        self.assertTrue(self.feed.run_once())
        self.assertEqual(self.naps, [1.0] * 4)  # the pass is spread evenly over the wallets
        for n in range(3, 3 + TX_PER_WALLET):
            self.chain.add(ANSEM, n, tx())
        self.feed.run_once()
        self.assertEqual(self.naps[-2:], [0.35 * (1 + TX_PER_WALLET), 1.0])  # many reads: a longer pause
        self.feed.drain()
        self.chain.add(BIGDOG, 9, tx(wallet=BIGDOG))
        self.feed.run_once()
        self.assertEqual([m.wallet for m in self.feed.drain()], [BIGDOG])
        self.assertEqual(self.feed.drain(), [])
        self.assertEqual(self.feed.status(), {"on": True, "wallets": 2, "ok": 1000.0, "error": "",
                                              "node": "a.node", "flooded": []})

    def test_a_failing_node_ends_the_pass_and_is_reported(self):
        self.feed.follow([ANSEM, BIGDOG])
        self.chain.fail = "a.node: HTTP 429"
        self.assertFalse(self.feed.run_once())
        self.assertEqual(len(self.chain.calls), 1)  # the second wallet was not asked about
        self.assertEqual(self.feed.status()["error"], "a.node: HTTP 429")

    def test_a_wallet_no_longer_followed_is_forgotten(self):
        self.feed.follow([ANSEM])
        self.chain.add(ANSEM, 1, tx())
        self.feed.run_once()
        self.feed.follow([BIGDOG])
        self.feed.run_once()
        self.feed.follow([ANSEM])
        self.chain.add(ANSEM, 2, tx())
        self.feed.run_once()
        self.assertEqual(self.feed.drain(), [])  # followed afresh: history is not replayed


class Seen:
    """Stands in for the watch: hands the scout whatever a test says was seen."""

    def __init__(self):
        self.moves: list[Move] = []
        self.following: dict = {}
        self.error = ""
        self.flooded: list = []

    def follow(self, wallets):
        self.following = dict(wallets)

    def drain(self):
        out, self.moves = self.moves, []
        return out

    def status(self):
        return {"on": True, "wallets": len(self.following), "ok": T0.timestamp(), "error": self.error,
                "node": "a.node", "flooded": self.flooded}


def bought(wallet, token="TokA1", tokens=500_000.0, sol=0.0, usd=0.0, minutes_ago=1.0, side="buy"):
    return Move(wallet, token, side, tokens, sol, usd, T0 - timedelta(minutes=minutes_ago), SIG)


@mock.patch.object(traders, "KNOWN", TWO)
class FollowingTests(unittest.TestCase):
    def setUp(self):
        self.m, self.clock, self.store, self.seen = Market(), Clock(), MemoryHypeStore(), Seen()
        self.scout = Scout(self.m, self.store, clock=self.clock, traders=self.seen)
        self.m.data = {"TokA1": hot()}  # $0.001 a coin: 500,000 coins is a $500 buy

    def kinds(self, doc):
        return [a["kind"] for a in doc["alerts"]]

    def test_a_buy_goes_on_the_page_and_is_scored_an_hour_later(self):
        self.seen.moves = [bought(ANSEM)]
        doc = self.scout.step()
        self.assertEqual(self.seen.following, {ANSEM: "Ansem", BIGDOG: "Big Dog"})
        entry = doc["traders"]["feed"][0]
        self.assertEqual((entry["name"], entry["side"], entry["symbol"], entry["usd"]),
                         ("Ansem", "buy", "TokA", 500))
        self.assertEqual(doc["traders"]["last"][ANSEM]["symbol"], "TokA")
        self.assertEqual(doc["alerts"], [])  # one unproven trader buying is not an alert
        self.assertEqual([(p["wallet"], p["token"]) for p in doc["pending"]], [(ANSEM, "TokA1")])
        self.m.data["TokA1"] = hot(price="0.0015")
        self.clock.t += timedelta(minutes=61)
        doc = self.scout.step()
        self.assertEqual(doc["wallets"][ANSEM][:2], [1, 1])
        self.assertEqual(doc["traders"]["watch"], {"wallets": 2, "node": "a.node", "problem": "",
                                                   "flooded": [], "ok": "2026-11-05T15:00:00+00:00"})

    def test_a_proven_trader_buying_is_an_alert_that_goes_through_the_scam_check(self):
        self.store.save_doc("scout", {"wallets": {ANSEM: [6, 5, 3.0, "", "OLD"], BIGDOG: [6, 1, -2.0, "", "OLD"]}})
        self.m.data["Rug1"] = hot(addr="Rug1")
        self.m.verdicts["Rug1"] = RUGGER
        self.seen.moves = [bought(ANSEM), bought(BIGDOG, token="Other9"), bought(ANSEM, token="Rug1")]
        self.m.data["Other9"] = hot(addr="Other9")
        doc = self.scout.step()
        (alert,) = doc["alerts"]
        self.assertEqual((alert["kind"], alert["title"]), ("trader", "TokA"))
        self.assertIn("Ansem bought about $500", alert["text"])
        self.assertIn("5 times out of the last 6", alert["text"])
        self.assertTrue(alert["safety"]["checked"])
        self.assertEqual(doc["blocked"][0]["symbol"], "Rug1")
        self.assertTrue(worth_sending(alert))
        self.assertTrue(alert_text(alert).startswith("**PROVEN TRADER BOUGHT · TokA**"))
        self.seen.moves = [bought(ANSEM)]  # the same trader adding to the same coin: not news again
        self.assertEqual(len(self.scout.step()["alerts"]), 1)

    def test_two_followed_traders_in_one_coin_is_a_crowd_alert_raised_once(self):
        self.seen.moves = [bought(ANSEM, minutes_ago=5)]
        self.assertEqual(self.scout.step()["alerts"], [])
        self.clock.t += timedelta(minutes=10)
        self.seen.moves = [Move(BIGDOG, "TokA1", "buy", 300_000.0, 0.0, 0.0, self.clock.t, SIG)]
        doc = self.scout.step()
        (alert,) = doc["alerts"]
        self.assertEqual(alert["kind"], "crowd")
        self.assertIn("2 traders we follow bought within 30 min: Big Dog, Ansem", alert["text"])
        self.clock.t += timedelta(minutes=1)
        self.seen.moves = [Move(ANSEM, "TokA1", "buy", 300_000.0, 0.0, 0.0, self.clock.t, SIG)]
        self.assertEqual(len(self.scout.step()["alerts"]), 1)

    def test_buys_far_apart_are_not_a_crowd(self):
        self.seen.moves = [bought(ANSEM)]
        self.scout.step()
        self.clock.t += timedelta(minutes=45)
        self.seen.moves = [Move(BIGDOG, "TokA1", "buy", 300_000.0, 0.0, 0.0, self.clock.t, SIG)]
        self.assertEqual(self.scout.step()["alerts"], [])

    def test_what_is_left_out(self):
        self.seen.moves = [bought(ANSEM, tokens=20_000.0),  # a $20 test buy
                           bought(NOBODY),  # not someone we follow
                           bought(ANSEM, minutes_ago=30),  # old news
                           bought(BIGDOG, side="sell")]
        doc = self.scout.step()
        self.assertEqual([(e["name"], e["side"]) for e in doc["traders"]["feed"]], [("Big Dog", "sell")])
        self.assertEqual(doc["pending"], [])  # a sale is shown but never scored or alerted
        self.assertEqual(doc["alerts"], [])

    def test_the_size_of_a_buy_comes_from_what_was_paid_when_that_is_known(self):
        self.m.data["TokA1"] = hot()
        coin = self.m.data["TokA1"]
        self.m.data["TokA1"] = type(coin)(**{**coin.__dict__, "sol_usd": 70.0})
        self.seen.moves = [bought(ANSEM, tokens=1.0, sol=2.0), bought(BIGDOG, tokens=1.0, usd=333.0)]
        doc = self.scout.step()
        self.assertEqual(sorted(e["usd"] for e in doc["traders"]["feed"]), [140, 333])

    def test_a_coin_too_new_to_have_a_price_waits_a_few_minutes_for_one(self):
        self.seen.moves = [bought(ANSEM, token="Fresh1")]
        doc = self.scout.step()
        self.assertEqual((doc["traders"]["feed"], len(doc["traders"]["waiting"])), ([], 1))
        self.m.data["Fresh1"] = hot(addr="Fresh1")
        self.clock.t += timedelta(minutes=1)
        doc = self.scout.step()
        self.assertEqual(([e["symbol"] for e in doc["traders"]["feed"]], doc["traders"]["waiting"]),
                         (["Fres"], []))
        self.seen.moves = [bought(ANSEM, token="Never1", minutes_ago=0)]
        self.scout.step()
        self.clock.t += timedelta(minutes=11)
        self.assertEqual(self.scout.step()["traders"]["waiting"], [])  # given up on

    def test_a_wallet_the_scout_proved_itself_is_followed_too(self):
        self.store.save_doc("scout", {"wallets": {NOBODY: [8, 6, 4.0, "", "OLD"]}})
        self.seen.moves = []
        self.scout.step()
        self.assertEqual(self.seen.following[NOBODY], "wallet Nobo…CCCC")
        self.seen.moves = [bought(NOBODY)]
        (alert,) = self.scout.step()["alerts"]
        self.assertIn("wallet Nobo…CCCC bought about $500", alert["text"])

    def test_only_so_many_found_wallets_are_followed_best_first(self):
        proven = [(f"w{i}", [10, 10 - i % 4, 0.0, "", ""]) for i in range(30)]
        names = followed(sorted(proven, key=lambda kv: kv[1][1], reverse=True))
        self.assertEqual(len(names), len(TWO) + traders.FOUND_MAX)
        self.assertEqual(list(names)[:2], [ANSEM, BIGDOG])
        self.assertEqual(followed([(ANSEM, [9, 9, 0.0, "", ""])]), {ANSEM: "Ansem", BIGDOG: "Big Dog"})

    def test_trouble_with_prices_or_the_watch_never_stops_the_scout(self):
        self.seen.moves = [bought(ANSEM)]
        with mock.patch.object(self.m, "snapshots", side_effect=RuntimeError("no data sources")):
            doc = self.scout.step()
        self.assertEqual((doc["error"], len(doc["traders"]["waiting"])), ("", 1))  # asked again next minute
        self.seen.error = "a.node: HTTP 429"
        with mock.patch.object(self.seen, "drain", side_effect=ValueError("boom")):
            doc = self.scout.step()
        self.assertEqual(doc["error"], "")
        self.assertEqual(doc["traders"]["error"], "ValueError: boom")
        self.assertEqual(doc["traders"]["watch"]["problem"], "a.node: HTTP 429")

    def test_the_page_lists_who_is_followed_their_record_and_their_latest_moves(self):
        self.store.save_doc("scout", {"wallets": {ANSEM: [6, 5, 3.0, "", "OLD"]}})
        self.m.data["Evil1"] = type(hot())(**{**hot(addr="Evil1").__dict__, "symbol": "<b>x"})
        self.seen.moves = [bought(ANSEM), bought(BIGDOG, token="Evil1", side="sell")]
        doc = self.scout.step()
        page = traders_section(doc, self.clock.t)
        for text in ("Traders we follow", "2 wallets", "Ansem", "PROVEN", "5 of 6", "Big Dog",
                     "not scored yet", "sold &lt;b&gt;x", "bought TokA", "Their latest moves",
                     f"https://solscan.io/account/{ANSEM}"):
            self.assertIn(text, page)
        self.assertNotIn("<b>x", page)
        self.assertLess(page.index("Ansem"), page.index("Big Dog"))  # the proven one first
        self.assertEqual(traders_section(None, self.clock.t), "")
        self.assertEqual(traders_section({"alerts": []}, self.clock.t), "")  # the watch is off
        doc["traders"]["watch"]["problem"] = "a.node: HTTP 429"
        self.assertIn("Trouble reading the blockchain", traders_section(doc, self.clock.t))
        self.assertNotIn("FLOODED", page)
        doc["traders"]["watch"]["flooded"] = [BIGDOG]
        page = traders_section(doc, self.clock.t)
        self.assertIn("FLOODED", page)
        self.assertIn("1 of the best-known wallets are flooded right now", page)


class KnownListTests(unittest.TestCase):
    def test_every_known_trader_has_a_real_looking_address_and_appears_once(self):
        self.assertTrue(all(is_address(t.wallet) for t in KNOWN))
        self.assertEqual(len({t.wallet for t in KNOWN}), len(KNOWN))
        self.assertEqual(len({t.name.lower() for t in KNOWN}), len(KNOWN))
        self.assertTrue(all(t.name and len(t.name) <= 24 for t in KNOWN))
        self.assertLessEqual(len(KNOWN), 45)  # every one costs requests to a free, shared node


if __name__ == "__main__":
    unittest.main()
