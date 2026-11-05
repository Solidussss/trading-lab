"""The hype bot's brain: a market snapshot, a hype score, entry and exit rules, and a
deliberately unforgiving cost model. Pure functions and plain data, no I/O.

What it models, and why it is pessimistic on purpose:
  * You never buy at the price you saw. Memecoins move in the seconds between seeing and
    buying, so entries pay a latency penalty on top of fees and price impact.
  * Price impact is from pool depth: a $50 buy into a $20k pool moves the price ~0.5%.
  * Every swap pays a platform fee (terminals like Axiom charge ~1%), the pool's LP fee,
    and a network + priority fee.
  * The bot only sees prices when it polls, so stops fill at whatever price it sees next,
    which can be far through the stop. A pulled pool (rug) is booked as a -95% exit.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class Snapshot:
    token: str
    symbol: str
    name: str
    pair: str
    dex: str
    url: str
    price: float
    liquidity: float
    market_cap: float
    age_min: float | None
    buys_5m: int
    sells_5m: int
    buys_1h: int
    sells_1h: int
    vol_5m: float
    vol_1h: float
    chg_5m: float
    chg_1h: float
    boosts: int
    socials: int

    @property
    def buy_ratio(self) -> float:
        n = self.buys_5m + self.sells_5m
        return self.buys_5m / n if n else 0.0

    @property
    def vol_accel(self) -> float:
        """Last 5 minutes' volume vs the hour's average 5 minutes (1.0 = normal)."""
        return self.vol_5m / max(self.vol_1h / 12.0, 1.0)


@dataclass(frozen=True)
class Rules:
    bankroll: float = 1000.0
    stake: float = 50.0
    max_open: int = 8
    max_new_per_cycle: int = 2
    # what counts as hype
    min_liquidity: float = 15_000.0
    min_mcap: float = 40_000.0
    max_mcap: float = 8_000_000.0
    min_age_min: float = 10.0
    max_age_hours: float = 72.0
    min_txns_5m: int = 40
    min_buy_ratio: float = 0.58
    min_vol_accel: float = 1.8
    min_chg_5m: float = 3.0
    max_chg_5m: float = 45.0  # don't buy a candle that already went vertical
    min_score: float = 60.0
    cooldown_hours: float = 6.0
    # exits
    stop_loss: float = -0.25
    trail_after: float = 0.30  # once up 30%, trail...
    trail_drop: float = 0.30  # ...30% below the peak
    take_half_at: float = 1.00  # sell half at +100% (2x), then the stop moves to breakeven
    stale_minutes: float = 45.0  # flat after 45 min = the hype didn't come
    stale_min_gain: float = 0.05
    max_hold_hours: float = 4.0
    rug_liquidity_drop: float = 0.60
    rug_loss: float = -0.95
    # costs
    platform_fee: float = 0.01
    lp_fee: float = 0.0025
    latency_penalty: float = 0.02
    network_fee_usd: float = 0.10


def score(s: Snapshot) -> float:
    """0-100. Buy pressure, volume surge, momentum, promotion and how 'real' it looks."""
    pressure = min(max((s.buy_ratio - 0.5) / 0.25, 0.0), 1.0) * 25
    activity = min((s.buys_5m + s.sells_5m) / 150.0, 1.0) * 15
    surge = min(max((s.vol_accel - 1.0) / 3.0, 0.0), 1.0) * 25
    momentum = min(max(s.chg_5m / 20.0, 0.0), 1.0) * 10 + (5 if s.chg_1h > 0 else 0)
    promo = min(s.boosts / 10.0, 1.0) * 10
    depth = min(s.liquidity / 100_000.0, 1.0) * 5 + min(s.socials, 3) / 3 * 5
    return round(pressure + activity + surge + momentum + promo + depth, 1)


def entry_check(s: Snapshot, rules: Rules) -> list[str]:
    """Reasons NOT to buy (empty list = passes every rule)."""
    no = []
    if s.liquidity < rules.min_liquidity:
        no.append(f"thin pool (${s.liquidity:,.0f})")
    if not rules.min_mcap <= s.market_cap <= rules.max_mcap:
        no.append(f"market cap ${s.market_cap:,.0f} out of range")
    if s.age_min is None or s.age_min < rules.min_age_min:
        no.append("too new to trust")
    elif s.age_min > rules.max_age_hours * 60:
        no.append("old news")
    if s.buys_5m + s.sells_5m < rules.min_txns_5m:
        no.append("not enough trading")
    if s.buy_ratio < rules.min_buy_ratio:
        no.append(f"sellers keeping up ({s.buy_ratio:.0%} buys)")
    if s.vol_accel < rules.min_vol_accel:
        no.append(f"no volume surge ({s.vol_accel:.1f}x)")
    if s.chg_5m < rules.min_chg_5m:
        no.append("not moving up")
    elif s.chg_5m > rules.max_chg_5m:
        no.append(f"already vertical (+{s.chg_5m:.0f}% in 5 min)")
    if score(s) < rules.min_score:
        no.append(f"hype score {score(s):.0f} < {rules.min_score:.0f}")
    return no


def impact(usd: float, liquidity: float) -> float:
    """Price impact of a swap of `usd` into a constant-product pool holding `liquidity` (both sides)."""
    side = max(liquidity / 2.0, 1.0)
    return usd / (side + usd)


def buy_fill(price: float, usd: float, liquidity: float, rules: Rules) -> tuple[float, float]:
    """(effective price paid per token, tokens received) for spending `usd` incl. all costs."""
    eff = price * (1 + rules.latency_penalty) * (1 + impact(usd, liquidity))
    spend = usd - rules.network_fee_usd
    spend *= (1 - rules.platform_fee - rules.lp_fee)
    return eff, spend / eff


def sell_fill(price: float, qty: float, liquidity: float, rules: Rules) -> float:
    """USD received for selling `qty` tokens at an observed `price`, after all costs."""
    gross = price * qty
    gross *= (1 - impact(gross, liquidity))
    return max(gross * (1 - rules.platform_fee - rules.lp_fee) - rules.network_fee_usd, 0.0)


@dataclass
class Position:
    token: str
    symbol: str
    url: str
    opened: str  # ISO UTC
    stake: float  # USD spent, incl. costs
    qty: float
    entry_price: float  # effective, incl. latency and impact
    seen_price: float  # price on screen when it decided to buy
    entry_liquidity: float
    entry_score: float
    why: str
    peak: float
    last: float
    last_liquidity: float
    realized: float = 0.0  # USD already taken off the table
    half_taken: bool = False

    def gain(self, price: float | None = None) -> float:
        p = self.last if price is None else price
        return p / self.entry_price - 1


@dataclass
class Trade:
    token: str
    symbol: str
    url: str
    opened: str
    closed: str
    stake: float
    proceeds: float
    reason: str
    why: str
    peak_gain: float

    @property
    def pnl(self) -> float:
        return self.proceeds - self.stake


@dataclass
class Account:
    cash: float
    started: str
    positions: list[Position] = field(default_factory=list)
    cooldown: dict[str, str] = field(default_factory=dict)  # token -> ISO time it may be bought again
    watch: list[dict] = field(default_factory=list)  # latest hype radar for the page
    last_cycle: str = ""
    last_error: str = ""
    cycles: int = 0

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> Account:
        d = dict(d)
        d["positions"] = [Position(**p) for p in d.get("positions", [])]
        return cls(**d)


def exit_check(p: Position, s: Snapshot | None, now: datetime, rules: Rules) -> str | None:
    """Why to sell everything now, or None. `s` is None when the pair vanished."""
    if s is None:
        return "pair disappeared (rug)"
    if p.entry_liquidity and s.liquidity < p.entry_liquidity * (1 - rules.rug_liquidity_drop):
        return "liquidity pulled (rug)"
    g = p.gain(s.price)
    peak = p.peak / p.entry_price - 1
    floor = 0.0 if p.half_taken else rules.stop_loss
    if g <= floor:
        return "stop loss" if not p.half_taken else "back to breakeven after taking half"
    if peak >= rules.trail_after and s.price <= p.peak * (1 - rules.trail_drop):
        return f"trailing stop ({rules.trail_drop:.0%} off the top)"
    held = (now - datetime.fromisoformat(p.opened)).total_seconds() / 60
    if held >= rules.max_hold_hours * 60:
        return "max hold time"
    if held >= rules.stale_minutes and g < rules.stale_min_gain and not p.half_taken:
        return "hype faded (flat after 45 min)"
    return None
