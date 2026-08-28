"""Causal opening-drive construction and exclusive rolling thresholds."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from bot05.models import Candle


class OpeningDriveError(ValueError):
    """Raised when opening-drive inputs are structurally ambiguous."""


class DriveDirection(StrEnum):
    LONG = "long"
    SHORT = "short"


class DriveFilter(StrEnum):
    NONE = "drive_none"
    Q50 = "drive_q50"
    Q75 = "drive_q75"


@dataclass(frozen=True, slots=True)
class OpeningDrive:
    market: str
    session_id: str
    t0: datetime
    observed_at: datetime
    direction: DriveDirection
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    body_bps: Decimal
    range_bps: Decimal
    close_location: Decimal
    midpoint: Decimal


@dataclass(frozen=True, slots=True)
class OpeningDriveResult:
    drive: OpeningDrive | None
    rejection_reason: str | None

    def __post_init__(self) -> None:
        if (self.drive is None) == (self.rejection_reason is None):
            raise OpeningDriveError("result must contain exactly one outcome")


@dataclass(frozen=True, slots=True)
class DriveObservation:
    market: str
    session_id: str
    t0: datetime
    absolute_body_bps: Decimal

    def __post_init__(self) -> None:
        if not self.market.strip() or not self.session_id.strip():
            raise OpeningDriveError("observation market and session are required")
        if self.t0.tzinfo is None or self.t0.utcoffset() != timedelta(0):
            raise OpeningDriveError("observation t0 must be UTC")
        if not self.absolute_body_bps.is_finite() or self.absolute_body_bps < 0:
            raise OpeningDriveError("absolute body must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class DriveThreshold:
    market: str
    session_id: str
    filter: DriveFilter
    as_of: datetime
    sample_count: int
    value: Decimal | None
    eligible: bool


def _require_utc(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise OpeningDriveError(f"{name} must be timezone-aware UTC")


def build_opening_drive(
    candles: tuple[Candle, ...],
    *,
    market: str,
    session_id: str,
    t0: datetime,
    observed_at: datetime,
) -> OpeningDriveResult:
    """Build the three-candle drive only after all bars are observable."""

    _require_utc(t0, "t0")
    _require_utc(observed_at, "observed_at")
    if len(candles) != 3:
        raise OpeningDriveError("opening drive requires exactly three candles")
    if not market.strip() or not session_id.strip():
        raise OpeningDriveError("market and session_id are required")
    ordered = tuple(sorted(candles, key=lambda item: item.open_time))
    for index, candle in enumerate(ordered):
        expected_open = t0 + timedelta(minutes=5 * index)
        if (
            candle.market != market
            or candle.interval_seconds != 300
            or candle.open_time != expected_open
            or candle.close_time != expected_open + timedelta(minutes=5)
        ):
            raise OpeningDriveError("drive candles must be contiguous aligned 5m bars")
        if candle.closed_at > observed_at:
            raise OpeningDriveError("drive uses a candle not observable at observed_at")

    drive_open = ordered[0].open
    drive_close = ordered[-1].close
    drive_high = max(item.high for item in ordered)
    drive_low = min(item.low for item in ordered)
    body_bps = Decimal(10_000) * (drive_close - drive_open) / drive_open
    range_bps = Decimal(10_000) * (drive_high - drive_low) / drive_open
    if drive_high == drive_low:
        return OpeningDriveResult(None, "zero_range")
    if body_bps == 0:
        return OpeningDriveResult(None, "zero_body")
    close_location = (drive_close - drive_low) / (drive_high - drive_low)
    direction = DriveDirection.LONG if body_bps > 0 else DriveDirection.SHORT
    if direction is DriveDirection.LONG and close_location < Decimal("0.75"):
        return OpeningDriveResult(None, "close_outside_outer_quartile")
    if direction is DriveDirection.SHORT and close_location > Decimal("0.25"):
        return OpeningDriveResult(None, "close_outside_outer_quartile")
    return OpeningDriveResult(
        OpeningDrive(
            market=market,
            session_id=session_id,
            t0=t0,
            observed_at=max(item.closed_at for item in ordered),
            direction=direction,
            open=drive_open,
            high=drive_high,
            low=drive_low,
            close=drive_close,
            body_bps=body_bps,
            range_bps=range_bps,
            close_location=close_location,
            midpoint=(drive_high + drive_low) / Decimal(2),
        ),
        None,
    )


def _quantile(values: tuple[Decimal, ...], probability: Decimal) -> Decimal:
    ordered = tuple(sorted(values))
    if len(ordered) == 1:
        return ordered[0]
    position = Decimal(len(ordered) - 1) * probability
    lower = int(position)
    fraction = position - Decimal(lower)
    return ordered[lower] + (ordered[lower + 1] - ordered[lower]) * fraction


def causal_drive_threshold(
    history: tuple[DriveObservation, ...],
    *,
    market: str,
    session_id: str,
    as_of: datetime,
    filter: DriveFilter,
    lookback: int = 60,
    min_samples: int = 20,
) -> DriveThreshold:
    """Compute a type-7 percentile from prior comparable sessions only."""

    _require_utc(as_of, "as_of")
    if lookback <= 0 or min_samples <= 0 or min_samples > lookback:
        raise OpeningDriveError("rolling window and minimum sample count are invalid")
    comparable = [
        item
        for item in history
        if item.market == market and item.session_id == session_id and item.t0 < as_of
    ]
    comparable.sort(key=lambda item: item.t0)
    if len({item.t0 for item in comparable}) != len(comparable):
        raise OpeningDriveError("duplicate comparable session in drive history")
    window = tuple(comparable[-lookback:])
    if filter is DriveFilter.NONE:
        return DriveThreshold(
            market, session_id, filter, as_of, len(window), Decimal(0), True
        )
    if len(window) < min_samples:
        return DriveThreshold(
            market, session_id, filter, as_of, len(window), None, False
        )
    probability = Decimal("0.5") if filter is DriveFilter.Q50 else Decimal("0.75")
    value = _quantile(tuple(item.absolute_body_bps for item in window), probability)
    return DriveThreshold(market, session_id, filter, as_of, len(window), value, True)


def passes_drive_filter(drive: OpeningDrive, threshold: DriveThreshold) -> bool:
    """Apply a precomputed threshold without mutating its historical sample."""

    if (
        threshold.market != drive.market
        or threshold.session_id != drive.session_id
        or threshold.as_of != drive.t0
    ):
        raise OpeningDriveError("threshold scope must match the drive")
    if not threshold.eligible or threshold.value is None:
        return False
    return abs(drive.body_bps) >= threshold.value
