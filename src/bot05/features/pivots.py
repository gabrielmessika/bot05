"""Causally confirmed fractal pivots and prior-session levels."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from bot05.models import Candle


class PivotError(ValueError):
    """Raised when pivot inputs cannot be interpreted causally."""


class PivotSide(StrEnum):
    HIGH = "high"
    LOW = "low"


@dataclass(frozen=True, slots=True)
class ConfirmedPivot:
    market: str
    interval_seconds: int
    side: PivotSide
    pivot_time: datetime
    confirmed_at: datetime
    price: Decimal


@dataclass(frozen=True, slots=True)
class PreviousSessionLevels:
    market: str
    session_start: datetime
    session_end: datetime
    observed_at: datetime
    high: Decimal
    low: Decimal


def _require_utc(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise PivotError(f"{name} must be timezone-aware UTC")


def _ordered_candles(candles: tuple[Candle, ...], market: str) -> tuple[Candle, ...]:
    if not market.strip():
        raise PivotError("market must not be blank")
    if any(item.market != market for item in candles):
        raise PivotError("all candles must match the requested market")
    ordered = tuple(sorted(candles, key=lambda item: item.open_time))
    if len({item.open_time for item in ordered}) != len(ordered):
        raise PivotError("duplicate candle open_time")
    if ordered and any(
        item.interval_seconds != ordered[0].interval_seconds for item in ordered
    ):
        raise PivotError("all candles must share one interval")
    return ordered


def confirmed_pivots(
    candles: tuple[Candle, ...],
    *,
    market: str,
    observed_at: datetime,
    left: int = 2,
    right: int = 2,
) -> tuple[ConfirmedPivot, ...]:
    """Return strict fractals whose right-hand bars were closed by observed_at."""

    _require_utc(observed_at, "observed_at")
    if left <= 0 or right <= 0:
        raise PivotError("pivot wings must be positive")
    observable_list: list[Candle] = []
    for item in _ordered_candles(candles, market):
        if item.closed_at > observed_at:
            break
        observable_list.append(item)
    observable = tuple(observable_list)
    if not observable:
        return ()
    pivots: list[ConfirmedPivot] = []
    for index in range(left, len(observable) - right):
        candidate = observable[index]
        neighbours = (
            observable[index - left : index] + observable[index + 1 : index + right + 1]
        )
        confirmed_at = max(
            item.closed_at for item in observable[index : index + right + 1]
        )
        if candidate.high > max(item.high for item in neighbours):
            pivots.append(
                ConfirmedPivot(
                    market=market,
                    interval_seconds=candidate.interval_seconds,
                    side=PivotSide.HIGH,
                    pivot_time=candidate.open_time,
                    confirmed_at=confirmed_at,
                    price=candidate.high,
                )
            )
        if candidate.low < min(item.low for item in neighbours):
            pivots.append(
                ConfirmedPivot(
                    market=market,
                    interval_seconds=candidate.interval_seconds,
                    side=PivotSide.LOW,
                    pivot_time=candidate.open_time,
                    confirmed_at=confirmed_at,
                    price=candidate.low,
                )
            )
    return tuple(
        sorted(pivots, key=lambda item: (item.confirmed_at, item.pivot_time, item.side))
    )


def previous_session_levels(
    candles: tuple[Candle, ...],
    *,
    market: str,
    session_start: datetime,
    session_end: datetime,
    observed_at: datetime,
) -> PreviousSessionLevels | None:
    """Compute high/low over calendar-resolved prior bounds known by observed_at."""

    for name, value in (
        ("session_start", session_start),
        ("session_end", session_end),
        ("observed_at", observed_at),
    ):
        _require_utc(value, name)
    if session_start >= session_end or session_end > observed_at:
        raise PivotError("previous-session bounds must end by observed_at")
    selected = tuple(
        item
        for item in _ordered_candles(candles, market)
        if item.open_time >= session_start and item.close_time <= session_end
    )
    if not selected:
        return None
    if any(item.closed_at > observed_at for item in selected):
        raise PivotError("previous-session candle was not observable at observed_at")
    interval_seconds = selected[0].interval_seconds
    interval = timedelta(seconds=interval_seconds)
    expected_count, remainder = divmod(
        int((session_end - session_start).total_seconds()), interval_seconds
    )
    expected_opens = tuple(
        session_start + index * interval for index in range(expected_count)
    )
    if (
        remainder
        or any(item.interval_seconds != interval_seconds for item in selected)
        or tuple(item.open_time for item in selected) != expected_opens
        or any(item.close_time != item.open_time + interval for item in selected)
    ):
        raise PivotError("previous-session candles do not provide complete coverage")
    return PreviousSessionLevels(
        market=market,
        session_start=session_start,
        session_end=session_end,
        observed_at=observed_at,
        high=max(item.high for item in selected),
        low=min(item.low for item in selected),
    )
