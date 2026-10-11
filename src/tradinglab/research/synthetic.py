"""Seeded synthetic daily bars for tests and offline demos. NOT market data.

Regime-switching geometric Brownian motion on the real NYSE calendar, so tests exercise
holidays, gaps, trends, and drawdowns deterministically without network access.
"""

from __future__ import annotations

import math
import random
from datetime import date

from tradinglab.marketdata.bars import Bar, BarSeries
from tradinglab.marketdata.calendar import trading_days


def synthetic_series(symbol: str, start: date, end: date, *, seed: int, start_price: float = 100.0,
                     regimes: tuple[tuple[float, float], ...] = ((0.18, 0.12), (-0.20, 0.25),
                                                                   (0.10, 0.15)),
                     regime_days: int = 180) -> BarSeries:
    """regimes: (annual drift, annual vol) pairs cycled every ~regime_days sessions."""
    rng = random.Random(f"{symbol}:{seed}")
    bars = []
    price = start_price
    for i, d in enumerate(trading_days(start, end)):
        mu, sigma = regimes[(i // regime_days) % len(regimes)]
        mu_d, sd_d = mu / 252, sigma / math.sqrt(252)
        gap = rng.gauss(0, sd_d * 0.3)
        o = price * math.exp(gap)
        c = o * math.exp(rng.gauss(mu_d - 0.5 * sd_d * sd_d, sd_d * 0.95))
        hi = max(o, c) * math.exp(abs(rng.gauss(0, sd_d * 0.5)))
        lo = min(o, c) * math.exp(-abs(rng.gauss(0, sd_d * 0.5)))
        bars.append(Bar(d, round(o, 4), round(hi, 4), round(lo, 4), round(c, 4),
                        float(rng.randint(1_000_000, 5_000_000))))
        price = c
    return BarSeries(symbol, bars)
