import unittest
from datetime import date
from decimal import Decimal

from tests.helpers import settings
from tradinglab.broker.base import OrderRequest, OrderType, Side, make_client_order_id
from tradinglab.broker.fake import FakePaperBroker
from tradinglab.engine.risk import RiskParams, evaluate_halts, size_long_position
from tradinglab.safety.order_gate import GateOutcome, InMemoryPause, OrderGate


def req(side=Side.BUY, qty=10, symbol="SPY", cid="tl-x-1"):
    return OrderRequest(symbol=symbol, side=side, qty=Decimal(qty), client_order_id=cid)


class OrderGateTests(unittest.TestCase):
    def setUp(self):
        self.broker = FakePaperBroker(prices={"SPY": Decimal(500), "QQQ": Decimal(400)})
        self.pause = InMemoryPause()

    def gate(self, enabled=True):
        return OrderGate(
            settings(ORDER_SUBMISSION_ENABLED="true" if enabled else "false"),
            self.broker, self.pause,
        )

    def test_default_is_dry_run(self):
        d = self.gate(enabled=False).check(req(), held_qty=Decimal(0))
        self.assertEqual(d.outcome, GateOutcome.DRY_RUN)
        self.assertFalse(d.may_submit)

    def test_enabled_passes(self):
        self.assertTrue(self.gate().check(req(), held_qty=Decimal(0)).may_submit)

    def test_pause_blocks_entries_but_not_exits(self):
        self.pause.pause()
        d = self.gate().check(req(), held_qty=Decimal(0))
        self.assertEqual(d.outcome, GateOutcome.REJECT)
        self.assertIn("emergency_pause_active", d.reasons)
        exit_ = self.gate().check(req(side=Side.SELL, qty=5, cid="tl-x-2"), held_qty=Decimal(5))
        self.assertTrue(exit_.may_submit)

    def test_pause_can_block_exits_too(self):
        self.pause.pause()
        g = OrderGate(settings(ORDER_SUBMISSION_ENABLED="true"), self.broker, self.pause,
                      allow_exits_when_paused=False)
        d = g.check(req(side=Side.SELL, qty=5), held_qty=Decimal(5))
        self.assertEqual(d.outcome, GateOutcome.REJECT)

    def test_long_only(self):
        d = self.gate().check(req(side=Side.SELL, qty=1), held_qty=Decimal(0))
        self.assertIn("no_position_to_sell(long_only)", d.reasons)
        d = self.gate().check(req(side=Side.SELL, qty=11), held_qty=Decimal(10))
        self.assertEqual(d.outcome, GateOutcome.REJECT)

    def test_symbol_allowlist(self):
        d = self.gate().check(req(symbol="TSLA"), held_qty=Decimal(0))
        self.assertIn("symbol_not_allowed:TSLA", d.reasons)

    def test_duplicate_client_order_id(self):
        self.broker.submit_order(req(cid="tl-dup-1"))
        d = self.gate().check(req(cid="tl-dup-1"), held_qty=Decimal(0))
        self.assertIn("duplicate_client_order_id", d.reasons)

    def test_halts_block_entries_not_exits(self):
        g = self.gate()
        d = g.check(req(), held_qty=Decimal(0), entries_halted=True,
                    halt_reasons=("peak_drawdown_halt",))
        self.assertIn("peak_drawdown_halt", d.reasons)
        d = g.check(req(side=Side.SELL, qty=3, cid="tl-x-9"), held_qty=Decimal(3),
                    entries_halted=True)
        self.assertTrue(d.may_submit)


class OrderRequestTests(unittest.TestCase):
    def test_validation(self):
        with self.assertRaises(ValueError):
            req(qty=0)
        with self.assertRaises(ValueError):
            OrderRequest("SPY", Side.BUY, Decimal("1.5"), "tl-a")  # whole shares only
        with self.assertRaises(ValueError):
            OrderRequest("SPY", Side.BUY, Decimal(1), "bad id with spaces")
        with self.assertRaises(ValueError):
            OrderRequest("SPY", Side.BUY, Decimal(1), "tl-a", order_type=OrderType.LIMIT)

    def test_client_order_id_deterministic(self):
        a = make_client_order_id("trend-v1", "SPY", date(2026, 10, 9), "entry")
        b = make_client_order_id("trend-v1", "SPY", date(2026, 10, 9), "entry")
        c = make_client_order_id("trend-v1", "SPY", date(2026, 10, 9), "exit")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertLessEqual(len(a), 48)


class SizingTests(unittest.TestCase):
    P = RiskParams()

    def test_risk_budget_binding(self):
        # $10k * 0.5% = $50 budget; risk/share = (100-95) + 1% gap buffer = $6 -> 8 shares
        r = size_long_position(equity=10_000, cash=10_000, entry_price=100, stop_price=95,
                               current_exposure_usd=0, open_positions=0, params=self.P)
        self.assertEqual(r.shares, 8)
        self.assertEqual(r.binding_constraint, "risk_budget")
        self.assertLessEqual(r.intended_risk_usd, 50)

    def test_exposure_cap_binding(self):
        # Tight stop allows many shares by risk, but 30% exposure caps at $3,000 -> 6 shares
        r = size_long_position(equity=10_000, cash=10_000, entry_price=500, stop_price=499,
                               current_exposure_usd=0, open_positions=0, params=self.P)
        self.assertEqual(r.shares, 6)
        self.assertEqual(r.binding_constraint, "max_exposure")

    def test_existing_exposure_reduces_room(self):
        r = size_long_position(equity=10_000, cash=10_000, entry_price=500, stop_price=499,
                               current_exposure_usd=2_800, open_positions=1, params=self.P)
        self.assertEqual(r.shares, 0)

    def test_no_margin(self):
        r = size_long_position(equity=10_000, cash=200, entry_price=100, stop_price=99,
                               current_exposure_usd=0, open_positions=0, params=self.P)
        self.assertEqual(r.shares, 2)
        self.assertEqual(r.binding_constraint, "cash")

    def test_max_positions_and_bad_stop(self):
        r = size_long_position(equity=10_000, cash=10_000, entry_price=100, stop_price=95,
                               current_exposure_usd=0, open_positions=3, params=self.P)
        self.assertEqual((r.shares, r.binding_constraint), (0, "max_open_positions"))
        r = size_long_position(equity=10_000, cash=10_000, entry_price=100, stop_price=101,
                               current_exposure_usd=0, open_positions=0, params=self.P)
        self.assertEqual((r.shares, r.binding_constraint), (0, "invalid_stop"))


class HaltTests(unittest.TestCase):
    P = RiskParams()

    def test_intraday_halt(self):
        h = evaluate_halts(equity=9_690, prior_close_equity=10_000, peak_equity=10_000, params=self.P)
        self.assertTrue(h.intraday_halt)
        self.assertTrue(h.entries_blocked)

    def test_peak_drawdown_halt(self):
        h = evaluate_halts(equity=9_850, prior_close_equity=9_900, peak_equity=11_000, params=self.P)
        self.assertFalse(h.intraday_halt)
        self.assertTrue(h.peak_drawdown_halt)

    def test_no_halt(self):
        h = evaluate_halts(equity=10_100, prior_close_equity=10_000, peak_equity=10_200, params=self.P)
        self.assertFalse(h.entries_blocked)
        self.assertEqual(h.reasons(), [])


if __name__ == "__main__":
    unittest.main()
