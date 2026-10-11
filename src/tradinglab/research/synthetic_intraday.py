"""Seeded synthetic 5-minute bars for tests and offline previews. NOT market data.

A random walk per session with an overnight gap, a U-shaped volume profile and occasional
intraday trends, on the real NYSE calendar (early closes included). It exists so the engine,
strategies and dashboard can be exercised deterministically without network access.
"""

from __future__ import annotations

import math
import random
from datetime import date

from tradinglab.marketdata.calendar import session, trading_days
from tradinglab.marketdata.intraday import BAR, IBar


def synthetic_intraday(symbol: str, start: date, end: date, *, seed: int = 7,
                       start_price: float = 400.0, daily_vol: float = 0.011
                       ) -> tuple[dict[date, list[IBar]], dict[date, float]]:
    """Returns ({day: bars}, {day: official close})."""
    rng = random.Random(f"{symbol}:{seed}:intraday")
    days: dict[date, list[IBar]] = {}
    closes: dict[date, float] = {}
    price = start_price
    for d in trading_days(start, end):
        s = session(d)
        n = int((s.close_utc - s.open_utc) / BAR)
        sd = daily_vol / math.sqrt(78)
        drift = rng.choice((0.0, 0.0, 0.0, 1.0, -1.0)) * sd * 0.35  # some trend days
        price *= math.exp(rng.gauss(0, daily_vol * 0.45))  # overnight gap
        bars = []
        for i in range(n):
            o = price
            path = [o]
            for _ in range(5):
                path.append(path[-1] * math.exp(rng.gauss(drift / 5, sd / math.sqrt(5))))
            c = path[-1]
            hi = max(path) * (1 + abs(rng.gauss(0, sd * 0.15)))
            lo = min(path) * (1 - abs(rng.gauss(0, sd * 0.15)))
            u = abs(i - n / 2) / (n / 2)  # U-shaped volume
            vol = float(int(20_000 * (0.4 + u * u) * rng.uniform(0.6, 1.4)))
            bars.append(IBar(s.open_utc + i * BAR, round(o, 4), round(hi, 4), round(lo, 4),
                             round(c, 4), vol))
            price = c
        days[d] = bars
        closes[d] = round(price, 4)
    return days, closes
