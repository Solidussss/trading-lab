"""Transaction cost and fill model (conservative by default).

Defaults are deliberately pessimistic for SPY/QQQ/IWM, whose real quoted spreads are
usually well under 1 bp on the open auction:

  half_spread_bps     2.0   paid on every fill
  slippage_bps        3.0   paid on every fill (market-on-open / market orders)
  stop_extra_bps      5.0   added on stop fills (a triggered stop becomes a market order)
  commission          0.0   per share, plus commission_per_order (set it to your broker's fee)
  sec_fee_rate        Section 31 fee on sell notional (rate changes; keep conservative)
  finra_taf           per-share trading activity fee on sells, capped per trade

Not modelled: currency conversion. A Canadian account buying US ETFs pays FX spreads
(often 1-2% each way at retail brokers) unless it already holds USD; results here are in USD.

A round trip therefore costs about 10 bp before fees - several times the real cost of these
ETFs - so a strategy that only works with cheaper fills is rejected early.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CostModel:
    half_spread_bps: float = 2.0
    slippage_bps: float = 3.0
    stop_extra_bps: float = 5.0
    commission_per_share: float = 0.0
    commission_per_order: float = 0.0
    sec_fee_rate: float = 0.0000278
    finra_taf_per_share: float = 0.000166
    finra_taf_max: float = 8.30

    def buy_price(self, ref: float) -> float:
        return ref * (1 + (self.half_spread_bps + self.slippage_bps) / 1e4)

    def sell_price(self, ref: float, *, stop: bool = False) -> float:
        bps = self.half_spread_bps + self.slippage_bps + (self.stop_extra_bps if stop else 0.0)
        return ref * (1 - bps / 1e4)

    def fees(self, *, sell: bool, shares: int, price: float) -> float:
        f = self.commission_per_share * shares + self.commission_per_order
        if sell:
            f += self.sec_fee_rate * shares * price
            f += min(self.finra_taf_per_share * shares, self.finra_taf_max)
        return f


ZERO_COST = CostModel(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
