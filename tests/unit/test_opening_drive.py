from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from bot05.features.opening_drive import (
    DriveDirection,
    DriveFilter,
    DriveObservation,
    OpeningDriveError,
    build_opening_drive,
    causal_drive_threshold,
    passes_drive_filter,
)
from bot05.models import Candle, DatasetProvenance


def _provenance(t0: datetime) -> DatasetProvenance:
    return DatasetProvenance(
        dataset_id="btc-5m",
        evidence_tier="H1",
        source="bot05",
        source_path_or_url="/tmp/candles.jsonl",
        raw_sha256="a" * 64,
        manifest_sha256="b" * 64,
        adapter_version="bot05-trade-candles-v1",
        calendar_version="test",
        code_version="test",
        config_sha256="c" * 64,
        source_timezone="UTC",
        period_start=t0,
        period_end=t0 + timedelta(minutes=15),
        transformations=("qualified_trades_to_5m_v1",),
    )


def _candles(
    t0: datetime, rows: tuple[tuple[str, str, str, str], ...]
) -> tuple[Candle, ...]:
    provenance = _provenance(t0)
    return tuple(
        Candle(
            market="BTC",
            interval_seconds=300,
            open_time=t0 + timedelta(minutes=index * 5),
            close_time=t0 + timedelta(minutes=(index + 1) * 5),
            closed_at=t0 + timedelta(minutes=(index + 1) * 5),
            open=Decimal(row[0]),
            high=Decimal(row[1]),
            low=Decimal(row[2]),
            close=Decimal(row[3]),
            volume=Decimal("1"),
            trade_count=1,
            provenance=provenance,
        )
        for index, row in enumerate(rows)
    )


@pytest.mark.parametrize(
    ("rows", "direction", "midpoint"),
    [
        (
            (
                ("100", "102", "99", "101"),
                ("101", "104", "100", "103"),
                ("103", "106", "102", "105.5"),
            ),
            DriveDirection.LONG,
            Decimal("102.5"),
        ),
        (
            (
                ("100", "101", "98", "99"),
                ("99", "100", "96", "97"),
                ("97", "98", "94", "94.5"),
            ),
            DriveDirection.SHORT,
            Decimal("97.5"),
        ),
    ],
)
def test_opening_drive_accepts_symmetric_outer_quartile_setups(
    rows: tuple[tuple[str, str, str, str], ...],
    direction: DriveDirection,
    midpoint: Decimal,
) -> None:
    t0 = datetime(2026, 8, 21, 13, 30, tzinfo=UTC)

    result = build_opening_drive(
        _candles(t0, rows),
        market="BTC",
        session_id="us_cash_open",
        t0=t0,
        observed_at=t0 + timedelta(minutes=15),
    )

    assert result.rejection_reason is None
    assert result.drive is not None
    assert result.drive.direction is direction
    assert result.drive.midpoint == midpoint


def test_opening_drive_rejects_inner_close_and_unobservable_bar() -> None:
    t0 = datetime(2026, 8, 21, 13, 30, tzinfo=UTC)
    candles = _candles(
        t0,
        (
            ("100", "102", "99", "101"),
            ("101", "104", "100", "103"),
            ("103", "106", "98", "102"),
        ),
    )

    result = build_opening_drive(
        candles,
        market="BTC",
        session_id="us_cash_open",
        t0=t0,
        observed_at=t0 + timedelta(minutes=15),
    )
    assert result.drive is None
    assert result.rejection_reason == "close_outside_outer_quartile"

    with pytest.raises(OpeningDriveError, match="not observable"):
        build_opening_drive(
            candles,
            market="BTC",
            session_id="us_cash_open",
            t0=t0,
            observed_at=t0 + timedelta(minutes=14, seconds=59),
        )


def test_rolling_percentile_excludes_current_and_future_sessions() -> None:
    t0 = datetime(2026, 8, 21, 13, 30, tzinfo=UTC)
    history = tuple(
        DriveObservation(
            market="BTC",
            session_id="us_cash_open",
            t0=t0 - timedelta(days=21 - index),
            absolute_body_bps=Decimal(index),
        )
        for index in range(1, 21)
    ) + (
        DriveObservation("BTC", "us_cash_open", t0, Decimal("9999")),
        DriveObservation(
            "BTC", "us_cash_open", t0 + timedelta(days=1), Decimal("9999")
        ),
        DriveObservation(
            "ETH", "us_cash_open", t0 - timedelta(days=1), Decimal("9999")
        ),
    )

    q50 = causal_drive_threshold(
        history,
        market="BTC",
        session_id="us_cash_open",
        as_of=t0,
        filter=DriveFilter.Q50,
    )
    q75 = causal_drive_threshold(
        history,
        market="BTC",
        session_id="us_cash_open",
        as_of=t0,
        filter=DriveFilter.Q75,
    )

    assert q50.sample_count == 20 and q50.value == Decimal("10.5")
    assert q75.sample_count == 20 and q75.value == Decimal("15.25")

    drive = build_opening_drive(
        _candles(
            t0,
            (
                ("100", "102", "99", "101"),
                ("101", "104", "100", "103"),
                ("103", "106", "102", "105.5"),
            ),
        ),
        market="BTC",
        session_id="us_cash_open",
        t0=t0,
        observed_at=t0 + timedelta(minutes=15),
    ).drive
    assert drive is not None
    assert passes_drive_filter(drive, q75)


def test_rolling_filter_fails_closed_before_twenty_prior_sessions() -> None:
    t0 = datetime(2026, 8, 21, 13, 30, tzinfo=UTC)
    history = tuple(
        DriveObservation(
            "BTC", "us_cash_open", t0 - timedelta(days=index), Decimal(index)
        )
        for index in range(1, 20)
    )

    threshold = causal_drive_threshold(
        history,
        market="BTC",
        session_id="us_cash_open",
        as_of=t0,
        filter=DriveFilter.Q50,
    )

    assert not threshold.eligible
    assert threshold.value is None


def test_threshold_cannot_cross_market_or_session_scope() -> None:
    t0 = datetime(2026, 8, 21, 13, 30, tzinfo=UTC)
    drive = build_opening_drive(
        _candles(
            t0,
            (
                ("100", "102", "99", "101"),
                ("101", "104", "100", "103"),
                ("103", "106", "102", "105.5"),
            ),
        ),
        market="BTC",
        session_id="us_cash_open",
        t0=t0,
        observed_at=t0 + timedelta(minutes=15),
    ).drive
    assert drive is not None
    threshold = causal_drive_threshold(
        (),
        market="ETH",
        session_id="us_cash_open",
        as_of=t0,
        filter=DriveFilter.NONE,
    )

    with pytest.raises(OpeningDriveError, match="scope"):
        passes_drive_filter(drive, threshold)
