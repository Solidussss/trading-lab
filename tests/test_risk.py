import unittest
from datetime import date
from decimal import Decimal

from tradinglab.broker.base import OrderRequest, OrderType, Side, make_client_order_id
from tradinglab.engine.risk import RiskParams, evaluate_halts, size_long_position


class SizingTests(unittest.TestCase):
    P = RiskParams()

    def size(self, **kw):
        base = dict(equity=10_000, cash=10_000, current_exposure_usd=0, open_positions=0,
                    params=self.P)
        base.update(kw)
        return size_long_position(**base)

    def test_risk_budget_binding(self):
        # $50 budget; risk/share = (100-95) + 1% gap buffer = $6 -> 8 shares
        r = self.size(entry_price=100, stop_price=95)
        self.assertEqual((r.shares, r.binding_constraint), (8, "risk_budget"))
        self.assertLessEqual(r.intended_risk_usd, 50)

    def test_exposure_cap_binding(self):
        r = self.size(entry_price=500, stop_price=499)
        self.assertEqual((r.shares, r.binding_constraint), (6, "max_exposure"))

    def test_existing_exposure_and_no_margin(self):
        self.assertEqual(self.size(entry_price=500, stop_price=499, current_exposure_usd=2_800,
                                   open_positions=1).shares, 0)
        r = self.size(cash=200, entry_price=100, stop_price=99)
        self.assertEqual((r.shares, r.binding_constraint), (2, "cash"))

    def test_max_positions_and_bad_stop(self):
        self.assertEqual(self.size(entry_price=100, stop_price=95, open_positions=3)
                         .binding_constraint, "max_open_positions")
        self.assertEqual(self.size(entry_price=100, stop_price=101).binding_constraint,
                         "invalid_stop")


class HaltTests(unittest.TestCase):
    P = RiskParams()

    def test_halts(self):
        self.assertTrue(evaluate_halts(equity=9_690, prior_close_equity=10_000,
                                       peak_equity=10_000, params=self.P).intraday_halt)
        h = evaluate_halts(equity=9_850, prior_close_equity=9_900, peak_equity=11_000,
                           params=self.P)
        self.assertTrue(h.peak_drawdown_halt and not h.intraday_halt)
        self.assertFalse(evaluate_halts(equity=10_100, prior_close_equity=10_000,
                                        peak_equity=10_200, params=self.P).entries_blocked)


class OrderRequestTests(unittest.TestCase):
    def test_validation(self):
        ok = OrderRequest("SPY", Side.BUY, Decimal(1), "tl-a")
        self.assertEqual(ok.qty, 1)
        bad = [dict(qty=Decimal(0)), dict(qty=Decimal("1.5")), dict(client_order_id="bad id"),
               dict(order_type=OrderType.LIMIT),
               dict(order_type=OrderType.STOP, stop_price=Decimal("1.00")),  # buy stop
               dict(order_type=OrderType.LIMIT, limit_price=Decimal("1.005"))]
        for kw in bad:
            args = dict(symbol="SPY", side=Side.BUY, qty=Decimal(1), client_order_id="tl-a")
            args.update(kw)
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                OrderRequest(**args)

    def test_client_order_id_deterministic(self):
        a = make_client_order_id("trend-v1", "SPY", date(2026, 10, 9), "entry")
        self.assertEqual(a, make_client_order_id("trend-v1", "SPY", date(2026, 10, 9), "entry"))
        self.assertNotEqual(a, make_client_order_id("trend-v1", "SPY", date(2026, 10, 9), "exit"))
        self.assertLessEqual(len(a), 48)


if __name__ == "__main__":
    unittest.main()
