from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from bot05.features.pivots import (
    PivotError,
    PivotSide,
    confirmed_pivots,
    previous_session_levels,
)
from bot05.models import Candle, DatasetProvenance


def _provenance(start: datetime, end: datetime) -> DatasetProvenance:
    return DatasetProvenance(
        dataset_id="pivot-fixture",
        evidence_tier="H1",
        source="bot05",
        source_path_or_url="/tmp/candles.jsonl",
        raw_sha256="a" * 64,
        manifest_sha256="b" * 64,
        adapter_version="fixture",
        calendar_version="test",
        code_version="test",
        config_sha256="c" * 64,
        source_timezone="UTC",
        period_start=start,
        period_end=end,
    )


def _candles(
    t0: datetime,
    highs: tuple[int, ...],
    lows: tuple[int, ...],
) -> tuple[Candle, ...]:
    provenance = _provenance(t0, t0 + timedelta(minutes=len(highs)))
    return tuple(
        Candle(
            market="BTC",
            interval_seconds=60,
            open_time=t0 + timedelta(minutes=index),
            close_time=t0 + timedelta(minutes=index + 1),
            closed_at=t0 + timedelta(minutes=index + 1),
            open=Decimal("5"),
            high=Decimal(high),
            low=Decimal(low),
            close=Decimal("5"),
            volume=Decimal("1"),
            trade_count=1,
            provenance=provenance,
        )
        for index, (high, low) in enumerate(zip(highs, lows, strict=True))
    )


def test_pivot_appears_only_after_two_right_bars_close() -> None:
    t0 = datetime(2026, 8, 21, 13, 0, tzinfo=UTC)
    candles = _candles(t0, (6, 7, 10, 8, 7, 100), (4, 4, 4, 4, 4, 1))

    before_confirmation = confirmed_pivots(
        candles,
        market="BTC",
        observed_at=t0 + timedelta(minutes=4, seconds=59),
    )
    after_confirmation = confirmed_pivots(
        candles,
        market="BTC",
        observed_at=t0 + timedelta(minutes=5),
    )

    assert before_confirmation == ()
    assert len(after_confirmation) == 1
    pivot = after_confirmation[0]
    assert pivot.side is PivotSide.HIGH
    assert pivot.pivot_time == t0 + timedelta(minutes=2)
    assert pivot.confirmed_at == t0 + timedelta(minutes=5)
    assert pivot.price == Decimal("10")


def test_previous_session_levels_exclude_current_session() -> None:
    previous_start = datetime(2026, 8, 20, 13, 30, tzinfo=UTC)
    previous_end = previous_start + timedelta(minutes=3)
    current_t0 = datetime(2026, 8, 21, 13, 30, tzinfo=UTC)
    previous = _candles(previous_start, (6, 9, 7), (4, 3, 2))
    current = _candles(current_t0, (100,), (1,))

    levels = previous_session_levels(
        previous + current,
        market="BTC",
        session_start=previous_start,
        session_end=previous_end,
        observed_at=current_t0,
    )

    assert levels is not None
    assert levels.high == Decimal("9")
    assert levels.low == Decimal("2")


def test_previous_session_levels_fail_closed_if_not_yet_observable() -> None:
    start = datetime(2026, 8, 20, 13, 30, tzinfo=UTC)
    candles = _candles(start, (6, 7), (4, 3))

    with pytest.raises(PivotError, match="end by observed_at"):
        previous_session_levels(
            candles,
            market="BTC",
            session_start=start,
            session_end=start + timedelta(minutes=2),
            observed_at=start + timedelta(minutes=1),
        )


def test_previous_session_levels_reject_incomplete_candles() -> None:
    start = datetime(2026, 8, 20, 13, 30, tzinfo=UTC)
    candles = _candles(start, (6, 7, 8), (4, 3, 2))

    with pytest.raises(PivotError, match="complete coverage"):
        previous_session_levels(
            (candles[0], candles[2]),
            market="BTC",
            session_start=start,
            session_end=start + timedelta(minutes=3),
            observed_at=start + timedelta(minutes=3),
        )
