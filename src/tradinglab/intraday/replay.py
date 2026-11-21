"""The odds replay: before a trade, replay the rest of the session many times from the stock's
own recent bars and see how the trade would have ended.

Method (after the day-trader skill pack's "time machine"): for every 5-minute slot left in the
session, a move is drawn at random from what the stock did in that same slot on earlier days.
Chaining the draws gives one possible rest-of-day; repeating it thousands of times gives the
share of paths that hit the stop and the average result. The random numbers are seeded, so the
same inputs always give the same odds (a replay can be re-run and checked).

What it is not: a forecast. It assumes moves at the same time of day are interchangeable across
days and independent of each other, and it ignores trading costs. It is a reality check on the
stop distance against the stock's own recent behaviour.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass

from tradinglab.marketdata.calendar import session
from tradinglab.marketdata.intraday import IBar


@dataclass(frozen=True)
class Odds:
    p_stop: float  # share of replays that hit the stop
    p_up: float  # share that finished above the entry price without hitting the stop
    exp_r: float  # average result, in multiples of the amount risked (stop = -1)
    sessions: int  # earlier sessions the moves were drawn from
    paths: int


def slot_moves(history: Sequence[Sequence[IBar]]) -> tuple[dict[int, list[tuple[float, float]]], int]:
    """For each slot (minutes after the open at the bar's start): one (close move, low move) per
    earlier session, both relative to the previous bar's close (the open for the first bar).
    Also returns how many sessions had data."""
    moves: dict[int, list[tuple[float, float]]] = {}
    sessions = 0
    for bars in history:
        bars = sorted(bars, key=lambda b: b.ts)
        if not bars:
            continue
        sessions += 1
        opened = session(bars[0].day).open_utc
        base = bars[0].open
        for b in bars:
            if base > 0:
                k = round((b.ts - opened).total_seconds() / 60)
                moves.setdefault(k, []).append((b.close / base - 1, b.low / base - 1))
            base = b.close
    return moves, sessions


def replay(history: Sequence[Sequence[IBar]], after_minute: int, entry: float, stop: float, *,
           paths: int = 2000, seed: int = 0, min_sessions: int = 10,
           session_minutes: int | None = None) -> Odds | None:
    """Odds for buying at `entry` with a stop at `stop`, holding to the close, on a signal from
    the bar that started `after_minute` minutes after the open. None when there is too little
    history, no time left in the session, or the stop is not below the entry.
    `session_minutes`: today's session length, so a half day is not replayed to 4 pm."""
    moves, sessions = slot_moves(history)
    slots = [moves[k] for k in sorted(moves)
             if k > after_minute and (session_minutes is None or k < session_minutes)]
    risk = entry - stop
    if sessions < min_sessions or not slots or risk <= 0 or paths <= 0:
        return None
    rng = random.Random(seed)  # noqa: S311 - a repeatable simulation, not cryptography
    stopped = ups = 0
    total = 0.0
    for _ in range(paths):
        price = entry
        for options in slots:
            close_move, low_move = rng.choice(options)
            if price * (1 + low_move) <= stop:
                stopped += 1
                total -= 1.0
                break
            price *= 1 + close_move
        else:
            total += (price - entry) / risk
            ups += price > entry
    return Odds(stopped / paths, ups / paths, total / paths, sessions, paths)
