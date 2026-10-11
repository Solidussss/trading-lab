"""Risk rules shared by the backtester and the live paper engine.

Keeping one implementation means a backtest and the live engine size and halt identically.
All functions are pure and work in floats; the live engine converts at the boundary.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from tradinglab.config import RiskLimits


@dataclass(frozen=True)
class RiskParams:
    risk_per_trade_pct: float = 0.005
    max_exposure_pct: float = 0.30
    max_open_positions: int = 3
    intraday_loss_halt_pct: float = 0.03
    peak_drawdown_halt_pct: float = 0.10
    # Extra adverse move assumed beyond the stop when sizing (gaps, slippage). A stop at
    # $95 on a $100 entry with 1% gap buffer is sized as if the loss could be $6/share.
    stop_gap_buffer_pct: float = 0.01
    # Entries are market-on-open LIMIT orders at close * (1 + entry_limit_pct). An open that
    # gaps above the limit does not fill, so exposure and risk can never exceed what was sized.
    entry_limit_pct: float = 0.01
    # A 10% drawdown halt is lifted by you (manual resume) in the live engine. Backtests
    # model that resume as a reset of the peak after this many halted sessions.
    drawdown_halt_cooloff_days: int = 20

    @classmethod
    def from_limits(cls, limits: RiskLimits, stop_gap_buffer_pct: float = 0.01) -> RiskParams:
        return cls(
            risk_per_trade_pct=float(limits.risk_per_trade_pct),
            max_exposure_pct=float(limits.max_exposure_pct),
            max_open_positions=limits.max_open_positions,
            intraday_loss_halt_pct=float(limits.intraday_loss_halt_pct),
            peak_drawdown_halt_pct=float(limits.peak_drawdown_halt_pct),
            stop_gap_buffer_pct=stop_gap_buffer_pct,
        )


@dataclass(frozen=True)
class SizingResult:
    shares: int
    risk_per_share: float
    intended_risk_usd: float
    binding_constraint: str
    notes: tuple[str, ...] = field(default_factory=tuple)


def size_long_position(
    *,
    equity: float,
    cash: float,
    entry_price: float,
    stop_price: float,
    current_exposure_usd: float,
    open_positions: int,
    params: RiskParams,
) -> SizingResult:
    """Whole-share size for a new long entry, or 0 with the reason it is blocked."""
    if equity <= 0 or entry_price <= 0 or not math.isfinite(entry_price):
        return SizingResult(0, 0.0, 0.0, "invalid_inputs")
    if stop_price <= 0 or stop_price >= entry_price:
        return SizingResult(0, 0.0, 0.0, "invalid_stop")
    if open_positions >= params.max_open_positions:
        return SizingResult(0, 0.0, 0.0, "max_open_positions")

    risk_per_share = (entry_price - stop_price) + entry_price * params.stop_gap_buffer_pct
    risk_budget = equity * params.risk_per_trade_pct
    by_risk = math.floor(risk_budget / risk_per_share)

    exposure_room = equity * params.max_exposure_pct - current_exposure_usd
    by_exposure = math.floor(max(exposure_room, 0.0) / entry_price)
    by_cash = math.floor(max(cash, 0.0) / entry_price)  # no margin, ever

    candidates = {"risk_budget": by_risk, "max_exposure": by_exposure, "cash": by_cash}
    binding = min(candidates, key=lambda k: candidates[k])
    shares = max(candidates[binding], 0)
    return SizingResult(
        shares=shares,
        risk_per_share=risk_per_share,
        intended_risk_usd=shares * risk_per_share,
        binding_constraint=binding if shares > 0 else f"{binding}_zero",
    )


@dataclass(frozen=True)
class HaltStatus:
    intraday_halt: bool
    peak_drawdown_halt: bool
    intraday_return: float
    drawdown_from_peak: float

    @property
    def entries_blocked(self) -> bool:
        return self.intraday_halt or self.peak_drawdown_halt

    def reasons(self) -> list[str]:
        out = []
        if self.intraday_halt:
            out.append(f"intraday_loss_halt ({self.intraday_return:.2%})")
        if self.peak_drawdown_halt:
            out.append(f"peak_drawdown_halt ({self.drawdown_from_peak:.2%})")
        return out


def evaluate_halts(
    *, equity: float, prior_close_equity: float, peak_equity: float, params: RiskParams
) -> HaltStatus:
    """New entries halt on a 3% intraday loss or 10% drawdown from peak. Exits never halt."""
    intraday = equity / prior_close_equity - 1.0 if prior_close_equity > 0 else 0.0
    peak = max(peak_equity, equity)
    dd = 1.0 - equity / peak if peak > 0 else 0.0
    return HaltStatus(
        intraday_halt=intraday <= -params.intraday_loss_halt_pct,
        peak_drawdown_halt=dd >= params.peak_drawdown_halt_pct,
        intraday_return=intraday,
        drawdown_from_peak=dd,
    )
