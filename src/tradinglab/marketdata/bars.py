"""Daily bar types, validation, and a reproducible CSV cache.

Every backtest records which cached dataset it used (path + SHA-256 + metadata), so a
result can be reproduced exactly even after the provider revises history.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from tradinglab.marketdata.calendar import trading_days


@dataclass(frozen=True, slots=True)
class Bar:
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class DataIssue:
    symbol: str
    day: date | None
    kind: str  # missing_session | duplicate | bad_ohlc | non_positive | non_finite | unsorted
    detail: str = ""


@dataclass
class BarSeries:
    """Validated, strictly date-ordered daily bars for one symbol."""

    symbol: str
    bars: list[Bar]
    issues: list[DataIssue] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.bars, self.issues = _clean(self.symbol, self.bars)
        self._index = {b.day: i for i, b in enumerate(self.bars)}

    def __len__(self) -> int:
        return len(self.bars)

    @property
    def days(self) -> list[date]:
        return [b.day for b in self.bars]

    def index_of(self, d: date) -> int | None:
        return self._index.get(d)

    def bar_on(self, d: date) -> Bar | None:
        i = self._index.get(d)
        return self.bars[i] if i is not None else None

    def history_through(self, d: date) -> Sequence[Bar]:
        """Bars up to and including `d` - the ONLY view a strategy ever receives."""
        i = self._index.get(d)
        if i is None:
            # last bar strictly before d
            lo, hi = 0, len(self.bars)
            while lo < hi:
                mid = (lo + hi) // 2
                if self.bars[mid].day <= d:
                    lo = mid + 1
                else:
                    hi = mid
            return self.bars[:lo]
        return self.bars[: i + 1]

    def missing_sessions(self, start: date | None = None, end: date | None = None) -> list[date]:
        if not self.bars:
            return []
        start = start or self.bars[0].day
        end = end or self.bars[-1].day
        have = set(self._index)
        return [d for d in trading_days(start, end) if d not in have]


def _clean(symbol: str, bars: Iterable[Bar]) -> tuple[list[Bar], list[DataIssue]]:
    issues: list[DataIssue] = []
    by_day: dict[date, Bar] = {}
    prev: date | None = None
    for b in bars:
        if prev is not None and b.day < prev:
            issues.append(DataIssue(symbol, b.day, "unsorted"))
        prev = b.day
        vals = (b.open, b.high, b.low, b.close)
        if not all(math.isfinite(v) for v in (*vals, b.volume)):
            issues.append(DataIssue(symbol, b.day, "non_finite"))
            continue
        if min(vals) <= 0:
            issues.append(DataIssue(symbol, b.day, "non_positive"))
            continue
        if b.high < max(b.open, b.close) - 1e-9 or b.low > min(b.open, b.close) + 1e-9:
            issues.append(DataIssue(symbol, b.day, "bad_ohlc", f"{vals}"))
            continue
        if b.day in by_day:
            issues.append(DataIssue(symbol, b.day, "duplicate"))
            continue  # keep the first occurrence; duplicates are reported, never merged
        by_day[b.day] = b
    ordered = [by_day[d] for d in sorted(by_day)]
    if ordered:
        have = set(by_day)
        for d in trading_days(ordered[0].day, ordered[-1].day):
            if d not in have:
                issues.append(DataIssue(symbol, d, "missing_session"))
    return ordered, issues


# --- reproducible cache -------------------------------------------------------------------

CSV_FIELDS = ("day", "open", "high", "low", "close", "volume")


@dataclass(frozen=True)
class DatasetMeta:
    symbol: str
    source: str  # e.g. "alpaca", "synthetic"
    feed: str  # e.g. "sip", "iex"
    adjustment: str  # e.g. "split", "all", "raw"
    start: str
    end: str
    fetched_at: str
    rows: int
    sha256: str = ""


def save_series(series: BarSeries, directory: Path, meta: DatasetMeta) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{series.symbol}_{meta.feed}_{meta.adjustment}.csv"
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_FIELDS)
        for b in series.bars:
            w.writerow([b.day.isoformat(), repr(b.open), repr(b.high), repr(b.low),
                        repr(b.close), repr(b.volume)])
    digest = file_sha256(path)
    meta_out = DatasetMeta(**{**asdict(meta), "rows": len(series), "sha256": digest})
    path.with_suffix(".meta.json").write_text(json.dumps(asdict(meta_out), indent=2))
    return path


def load_series(path: Path) -> tuple[BarSeries, DatasetMeta]:
    meta_path = path.with_suffix(".meta.json")
    meta = DatasetMeta(**json.loads(meta_path.read_text()))
    digest = file_sha256(path)
    if meta.sha256 and meta.sha256 != digest:
        raise ValueError(f"{path} does not match its recorded checksum; cache was modified.")
    bars = []
    with path.open(newline="") as fh:
        for row in csv.DictReader(fh):
            bars.append(Bar(date.fromisoformat(row["day"]), float(row["open"]),
                            float(row["high"]), float(row["low"]), float(row["close"]),
                            float(row["volume"])))
    return BarSeries(meta.symbol, bars), meta


def data_fingerprint(data: dict[str, BarSeries]) -> str:
    """Order-independent SHA-256 over every bar of every symbol (for result provenance)."""
    h = hashlib.sha256()
    for sym in sorted(data):
        h.update(sym.encode())
        for b in data[sym].bars:
            h.update(f"{b.day}|{b.open!r}|{b.high!r}|{b.low!r}|{b.close!r}|{b.volume!r}".encode())
    return h.hexdigest()


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()
