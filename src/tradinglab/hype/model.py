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

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime

# pump fun starts every coin on a fixed price curve ("bonding curve") and moves it to a normal
# trading pool ("migrates") once buyers have put in about 85 SOL. On that curve
# (virtual SOL)^2 = market cap in SOL x PUMP_K, starting from 30 virtual SOL. These are the
# long-standing public parameters; treat anything computed from them as approximate.
PUMP_CURVE = "pumpfun"
PUMP_K = 30.0 * 1_073_000_000 / 1_000_000_000
PUMP_START_SOL, PUMP_RAISE_SOL = 30.0, 85.0


def curve_progress(market_cap_usd: float, sol_usd: float) -> float:
    """0 to 1: how much of the 85 SOL a pump fun coin has raised, from its market cap."""
    if market_cap_usd <= 0 or sol_usd <= 0:
        return 0.0
    virtual_sol = math.sqrt(market_cap_usd / sol_usd * PUMP_K)
    return min(max((virtual_sol - PUMP_START_SOL) / PUMP_RAISE_SOL, 0.0), 1.0)


def curve_liquidity(market_cap_usd: float, sol_usd: float) -> float:
    """Both sides of the curve's (virtual) pool in dollars: what price impact is worked out
    from, the same way as for a normal pool."""
    if market_cap_usd <= 0 or sol_usd <= 0:
        return 0.0
    return 2.0 * math.sqrt(market_cap_usd * PUMP_K * sol_usd)


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
    promo_known: bool = True  # False when the feed has no boost/social data (GeckoTerminal)
    sol_usd: float = 0.0  # price of SOL when the coin is quoted in SOL (0 = unknown)

    @property
    def pre_migration(self) -> bool:
        """Still on pump fun's starting curve: it has not moved to a normal pool yet."""
        return self.dex == PUMP_CURVE

    @property
    def curve_done(self) -> float | None:
        """How far along pump fun's curve the coin is, 0 to 1 (1 = it migrates). None when
        it is not on the curve or SOL's price is unknown."""
        if not self.pre_migration or self.sol_usd <= 0:
            return None
        return curve_progress(self.market_cap, self.sol_usd)

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
    min_score: float = 50.0  # ranking cut-off; the hard rules above already define hype
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
    # guard rails
    no_repeat_losers: bool = False  # never buy a coin again after closing it at a loss
    auto_pause_drawdown: float = 0.30  # stop buying once the account is this far below its start
    # whale flow (hype/whales.py); whale_min_usd = 0 means the rule set ignores it
    whale_min_usd: float = 0.0  # above 0: use whale flow; the least a "big" trade can be (see big_size)
    whale_window_min: float = 10.0  # look at the last 10 minutes
    whale_min_buyers: int = 3  # at least 3 different whales buying...
    whale_ratio: float = 2.0  # ...and twice as many dollars bought as sold (and the reverse to exit)
    whale_checks: int = 3  # coins per scan whose whale flow is looked up (the data is rate-limited)
    stake_pct: float = 0.0  # above 0: bet this share of the account instead of the fixed stake
    stage: str = ""  # "pre": only coins still on pump fun's curve (found through feed.early())
    min_curve: float = 0.0  # with stage "pre": how far along the curve a coin must be (0 to 1)
    scam_check: bool = False  # buy only coins that pass RugCheck's report (hype/rugcheck.py)


@dataclass(frozen=True)
class Variant:
    """One rule set, run side by side with the others on its own pretend bankroll."""

    id: str
    name: str
    blurb: str
    rules: Rules
    buys_enabled: bool = True  # False = paused for new buys; open positions still exit by the rules


VARIANTS: tuple[Variant, ...] = (
    Variant("current", "Current rules", "Chases 5-minute jumps", Rules(), buys_enabled=False),
    Variant("no_repeat", "No repeat losers", "Never re-buys a coin it lost on",
            Rules(no_repeat_losers=True, cooldown_hours=24.0)),
    Variant("deep_pools", "Bigger pools only", "Skips anything under $100k in the pool",
            Rules(min_liquidity=100_000.0, min_mcap=500_000.0, min_txns_5m=80)),
    # Buy what the big wallets are buying, instead of what has already jumped: the price only
    # has to be holding up (not falling), and three whales must have bought in the last ten
    # minutes with little whale selling against them. Sells when the whales turn.
    Variant("whales", "Follow the whales", "Buys only when big wallets are buying",
            Rules(scam_check=True, whale_min_usd=250.0, min_chg_5m=0.0, min_vol_accel=1.0, min_score=0.0,
                  no_repeat_losers=True, cooldown_hours=24.0)),
    # The same whale rule with the quick exit and account-sized bets from the "agent bot"
    # videos: out at -8%, 7% of the account per coin.
    # Before they migrate: coins still on pump fun's curve that are drawing a crowd. Found
    # through DexScreener's fresh profiles and boosts; small, young, busy, more buying than selling.
    Variant("early", "Before they migrate", "Buys buzzing pump fun coins before they migrate",
            Rules(scam_check=True,
                  stage="pre", min_curve=0.15, min_liquidity=3_000.0, min_mcap=0.0, max_mcap=1e12,
                  min_age_min=5.0, max_age_hours=3.0, min_txns_5m=60, min_buy_ratio=0.55,
                  min_vol_accel=1.0, min_chg_5m=0.0, min_score=0.0, no_repeat_losers=True,
                  cooldown_hours=24.0)),
    Variant("whales_quick", "Whales, quick exit", "Whale buys, out at -8%, bets 7% of the account",
            Rules(scam_check=True, whale_min_usd=250.0, min_chg_5m=0.0, min_vol_accel=1.0, min_score=0.0,
                  no_repeat_losers=True, cooldown_hours=24.0, stop_loss=-0.08, stake_pct=0.07)),
)

BANNED_MAX = 500  # how many lost-on coins an account remembers


def score(s: Snapshot) -> float:
    """0-100. Buy pressure, volume surge, momentum, promotion and how 'real' it looks."""
    pressure = min(max((s.buy_ratio - 0.5) / 0.25, 0.0), 1.0) * 25
    activity = min((s.buys_5m + s.sells_5m) / 150.0, 1.0) * 15
    surge = min(max((s.vol_accel - 1.0) / 3.0, 0.0), 1.0) * 25
    momentum = min(max(s.chg_5m / 20.0, 0.0), 1.0) * 10 + (5 if s.chg_1h > 0 else 0)
    promo = min(s.boosts / 10.0, 1.0) * 10
    socials = min(s.socials, 3) / 3 * 5
    depth = min(s.liquidity / 100_000.0, 1.0) * 5
    raw = pressure + activity + surge + momentum + promo + depth + socials
    if not s.promo_known:  # don't punish a coin because the feed can't see its promotion
        raw = raw / 85 * 100
    return round(raw, 1)


def entry_check(s: Snapshot, rules: Rules) -> list[str]:
    """Reasons NOT to buy (empty list = passes every rule)."""
    no = []
    if rules.stage == "pre":
        done = s.curve_done
        if not s.pre_migration:
            no.append("already migrated")
        elif done is None or done < rules.min_curve:
            no.append(f"barely started ({(done or 0):.0%} of the way to migrating)")
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
    pair: str = ""  # the pool it's priced from (survives restarts)
    missing: int = 0  # consecutive checks with no data for this pool
    trail: list[float] = field(default_factory=list)  # recent prices, for the card's mini chart

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
    curve: list[list] = field(default_factory=list)  # [[iso time, portfolio value], ...]
    banned: list[str] = field(default_factory=list)  # tokens it closed at a loss; newest last
    paused: str = ""  # non-empty = the reason it stopped buying (exits carry on)

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> Account:
        """Rows saved before `banned` and `paused` existed load with those fields empty."""
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
