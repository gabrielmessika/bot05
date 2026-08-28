"""Deterministic, gap-explicit candle aggregation without synthetic prices."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from bot05.data.contracts import TimeRange
from bot05.models import Candle, DatasetProvenance, Trade

SUPPORTED_INTERVALS = frozenset({60, 300})


class CandleAggregationError(ValueError):
    """Raised when candle inputs or requested bounds are ambiguous."""


class CandleGapReason(StrEnum):
    PARTIAL_COVERAGE = "partial_coverage"
    NO_TRADES = "no_trades"
    MISSING_COMPONENT_CANDLE = "missing_component_candle"


class CandleParityIssueKind(StrEnum):
    MISSING_DERIVED = "missing_derived"
    MISSING_REFERENCE = "missing_reference"
    FIELD_MISMATCH = "field_mismatch"


@dataclass(frozen=True, slots=True)
class CandleGap:
    market: str
    interval_seconds: int
    start: datetime
    end: datetime
    reason: CandleGapReason


@dataclass(frozen=True, slots=True)
class CandleBuildResult:
    candles: tuple[Candle, ...]
    gaps: tuple[CandleGap, ...]

    @property
    def complete(self) -> bool:
        return not self.gaps


@dataclass(frozen=True, slots=True)
class CandleParityIssue:
    open_time: datetime
    kind: CandleParityIssueKind
    fields: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CandleParityResult:
    compared_count: int
    issues: tuple[CandleParityIssue, ...]

    @property
    def matches(self) -> bool:
        return not self.issues


def _utc_ms(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise CandleAggregationError("timestamps must be timezone-aware UTC")
    delta = value - datetime(1970, 1, 1, tzinfo=UTC)
    return ((delta.days * 86_400) + delta.seconds) * 1000 + delta.microseconds // 1000


def _datetime_from_ms(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def _validate_request(requested: TimeRange, interval_seconds: int) -> int:
    if interval_seconds not in SUPPORTED_INTERVALS:
        raise CandleAggregationError("only 1m and 5m candles are supported")
    interval_ms = interval_seconds * 1000
    if requested.start_ms % interval_ms or requested.end_ms % interval_ms:
        raise CandleAggregationError("requested bounds must align to the interval")
    return interval_ms


def _normalized_coverage(coverage: tuple[TimeRange, ...]) -> tuple[TimeRange, ...]:
    if not coverage:
        raise CandleAggregationError("qualified coverage is required")
    ordered = tuple(sorted(coverage))
    if any(
        previous.end_ms > current.start_ms
        for previous, current in zip(ordered, ordered[1:], strict=False)
    ):
        raise CandleAggregationError("qualified coverage must not overlap")
    merged: list[TimeRange] = []
    for span in ordered:
        if merged and merged[-1].end_ms == span.start_ms:
            merged[-1] = TimeRange(merged[-1].start_ms, span.end_ms)
        else:
            merged.append(span)
    return tuple(merged)


def _is_covered(bucket: TimeRange, coverage: tuple[TimeRange, ...]) -> bool:
    return any(
        span.start_ms <= bucket.start_ms and span.end_ms >= bucket.end_ms
        for span in coverage
    )


def _gap(
    market: str,
    interval_seconds: int,
    start_ms: int,
    end_ms: int,
    reason: CandleGapReason,
) -> CandleGap:
    return CandleGap(
        market=market,
        interval_seconds=interval_seconds,
        start=_datetime_from_ms(start_ms),
        end=_datetime_from_ms(end_ms),
        reason=reason,
    )


def aggregate_trades(
    trades: tuple[Trade, ...],
    *,
    market: str,
    interval_seconds: int,
    requested: TimeRange,
    qualified_coverage: tuple[TimeRange, ...],
    provenance: DatasetProvenance,
) -> CandleBuildResult:
    """Aggregate exchange-time trades and expose every unbuildable bucket."""

    if not market.strip():
        raise CandleAggregationError("market must not be blank")
    interval_ms = _validate_request(requested, interval_seconds)
    coverage = _normalized_coverage(qualified_coverage)
    selected: list[tuple[int, Trade]] = []
    seen_trade_ids: set[str] = set()
    for source_position, trade in enumerate(trades):
        if trade.market != market:
            raise CandleAggregationError("all trades must match the requested market")
        exchange_ms = _utc_ms(trade.exchange_time)
        if requested.start_ms <= exchange_ms < requested.end_ms:
            if trade.trade_id in seen_trade_ids:
                raise CandleAggregationError("duplicate trade_id in requested window")
            seen_trade_ids.add(trade.trade_id)
            selected.append((source_position, trade))
    selected.sort(
        key=lambda item: (item[1].exchange_time, item[1].received_at, item[0])
    )

    by_bucket: dict[int, list[Trade]] = {}
    for _, trade in selected:
        exchange_ms = _utc_ms(trade.exchange_time)
        bucket_start = exchange_ms - exchange_ms % interval_ms
        by_bucket.setdefault(bucket_start, []).append(trade)

    candles: list[Candle] = []
    gaps: list[CandleGap] = []
    for start_ms in range(requested.start_ms, requested.end_ms, interval_ms):
        end_ms = start_ms + interval_ms
        bucket = TimeRange(start_ms, end_ms)
        if not _is_covered(bucket, coverage):
            gaps.append(
                _gap(
                    market,
                    interval_seconds,
                    start_ms,
                    end_ms,
                    CandleGapReason.PARTIAL_COVERAGE,
                )
            )
            continue
        bucket_trades = by_bucket.get(start_ms, [])
        if not bucket_trades:
            gaps.append(
                _gap(
                    market,
                    interval_seconds,
                    start_ms,
                    end_ms,
                    CandleGapReason.NO_TRADES,
                )
            )
            continue
        prices = [item.price for item in bucket_trades]
        close_time = _datetime_from_ms(end_ms)
        latest_received = max(item.received_at for item in bucket_trades)
        candles.append(
            Candle(
                market=market,
                interval_seconds=interval_seconds,
                open_time=_datetime_from_ms(start_ms),
                close_time=close_time,
                closed_at=max(close_time, latest_received),
                open=prices[0],
                high=max(prices),
                low=min(prices),
                close=prices[-1],
                volume=sum((item.size for item in bucket_trades), Decimal(0)),
                trade_count=len(bucket_trades),
                provenance=provenance,
            )
        )
    return CandleBuildResult(tuple(candles), tuple(gaps))


def aggregate_candles(
    source: tuple[Candle, ...],
    *,
    market: str,
    interval_seconds: int,
    requested: TimeRange,
    provenance: DatasetProvenance,
) -> CandleBuildResult:
    """Roll complete lower-interval candles into aligned 1m or 5m candles."""

    interval_ms = _validate_request(requested, interval_seconds)
    if not source:
        return CandleBuildResult(
            (),
            tuple(
                _gap(
                    market,
                    interval_seconds,
                    start_ms,
                    start_ms + interval_ms,
                    CandleGapReason.MISSING_COMPONENT_CANDLE,
                )
                for start_ms in range(requested.start_ms, requested.end_ms, interval_ms)
            ),
        )
    base_interval = source[0].interval_seconds
    if base_interval <= 0 or interval_seconds % base_interval:
        raise CandleAggregationError("source interval must divide target interval")
    base_ms = base_interval * 1000
    if any(
        item.market != market or item.interval_seconds != base_interval
        for item in source
    ):
        raise CandleAggregationError("source candles must share market and interval")
    by_start: dict[int, Candle] = {}
    for item in source:
        start_ms = _utc_ms(item.open_time)
        if start_ms in by_start:
            raise CandleAggregationError("duplicate source candle open_time")
        by_start[start_ms] = item

    candles: list[Candle] = []
    gaps: list[CandleGap] = []
    for start_ms in range(requested.start_ms, requested.end_ms, interval_ms):
        end_ms = start_ms + interval_ms
        expected_starts = range(start_ms, end_ms, base_ms)
        components = [by_start.get(value) for value in expected_starts]
        if any(item is None for item in components):
            gaps.append(
                _gap(
                    market,
                    interval_seconds,
                    start_ms,
                    end_ms,
                    CandleGapReason.MISSING_COMPONENT_CANDLE,
                )
            )
            continue
        complete = [item for item in components if item is not None]
        if any(
            _utc_ms(item.close_time) != _utc_ms(item.open_time) + base_ms
            for item in complete
        ):
            raise CandleAggregationError("source candle bounds are inconsistent")
        trade_count = (
            sum(item.trade_count for item in complete if item.trade_count is not None)
            if all(item.trade_count is not None for item in complete)
            else None
        )
        close_time = _datetime_from_ms(end_ms)
        candles.append(
            Candle(
                market=market,
                interval_seconds=interval_seconds,
                open_time=_datetime_from_ms(start_ms),
                close_time=close_time,
                closed_at=max(close_time, *(item.closed_at for item in complete)),
                open=complete[0].open,
                high=max(item.high for item in complete),
                low=min(item.low for item in complete),
                close=complete[-1].close,
                volume=sum((item.volume for item in complete), Decimal(0)),
                trade_count=trade_count,
                provenance=provenance,
            )
        )
    return CandleBuildResult(tuple(candles), tuple(gaps))


def compare_candle_series(
    derived: tuple[Candle, ...],
    reference: tuple[Candle, ...],
    *,
    market: str,
    interval_seconds: int,
    price_tolerance: Decimal = Decimal(0),
    volume_tolerance: Decimal = Decimal(0),
) -> CandleParityResult:
    """Compare derived OHLCV to official candles without hiding missing buckets."""

    if price_tolerance < 0 or volume_tolerance < 0:
        raise CandleAggregationError("parity tolerances must be non-negative")
    if any(
        item.market != market or item.interval_seconds != interval_seconds
        for item in (*derived, *reference)
    ):
        raise CandleAggregationError("parity candles must match market and interval")

    def indexed(candles: tuple[Candle, ...]) -> dict[datetime, Candle]:
        result: dict[datetime, Candle] = {}
        for candle in candles:
            if candle.open_time in result:
                raise CandleAggregationError("duplicate parity candle open_time")
            result[candle.open_time] = candle
        return result

    derived_by_time = indexed(derived)
    reference_by_time = indexed(reference)
    issues: list[CandleParityIssue] = []
    compared_count = 0
    for open_time in sorted(derived_by_time.keys() | reference_by_time.keys()):
        candidate = derived_by_time.get(open_time)
        official = reference_by_time.get(open_time)
        if candidate is None:
            issues.append(
                CandleParityIssue(open_time, CandleParityIssueKind.MISSING_DERIVED)
            )
            continue
        if official is None:
            issues.append(
                CandleParityIssue(open_time, CandleParityIssueKind.MISSING_REFERENCE)
            )
            continue
        compared_count += 1
        mismatches: list[str] = []
        for field in ("open", "high", "low", "close"):
            if (
                abs(getattr(candidate, field) - getattr(official, field))
                > price_tolerance
            ):
                mismatches.append(field)
        if abs(candidate.volume - official.volume) > volume_tolerance:
            mismatches.append("volume")
        if (
            candidate.close_time != official.close_time
            or candidate.open_time != official.open_time
        ):
            mismatches.append("bounds")
        if mismatches:
            issues.append(
                CandleParityIssue(
                    open_time,
                    CandleParityIssueKind.FIELD_MISMATCH,
                    tuple(mismatches),
                )
            )
    return CandleParityResult(compared_count, tuple(issues))
