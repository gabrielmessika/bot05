from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

from bot05.calendars.holidays import load_holiday_calendar
from bot05.calendars.sessions import (
    EUROPE_OPEN,
    MarketHours,
    OutsideExternalState,
    resolve_session,
)
from bot05.models import Candle, DatasetProvenance, encode_domain_record


def test_resolved_drive_accepts_exactly_three_closed_causal_candles() -> None:
    loaded = load_holiday_calendar(Path("tests/fixtures/calendars/europe_2026.toml"))
    hours = MarketHours(
        market="xyz:GOLD",
        timezone_name="Europe/Paris",
        regular_open=time(8, 0),
        regular_close=time(17, 30),
        calendar=loaded.calendar,
        outside_external_state=OutsideExternalState.INTERNAL_ORACLE,
    )
    resolution = resolve_session(date(2026, 8, 17), EUROPE_OPEN, hours)
    assert resolution.window is not None
    window = resolution.window
    provenance = DatasetProvenance(
        dataset_id="causal-drive-fixture",
        evidence_tier="H1",
        source="fixture",
        source_path_or_url="tests/fixtures/drive.json",
        raw_sha256="a" * 64,
        manifest_sha256="b" * 64,
        adapter_version="fixture-v1",
        calendar_version=window.calendar_version,
        code_version="test",
        config_sha256="c" * 64,
        source_timezone="UTC",
        period_start=window.t0,
        period_end=window.drive_end,
    )
    candles = tuple(
        Candle(
            market="xyz:GOLD",
            interval_seconds=300,
            open_time=window.t0 + timedelta(minutes=5 * index),
            close_time=window.t0 + timedelta(minutes=5 * (index + 1)),
            closed_at=window.t0 + timedelta(minutes=5 * (index + 1)),
            open=Decimal("2500") + index,
            high=Decimal("2502") + index,
            low=Decimal("2499") + index,
            close=Decimal("2501") + index,
            volume=Decimal("10"),
            trade_count=10,
            provenance=provenance,
        )
        for index in range(3)
    )

    assert candles[-1].close_time == window.drive_end
    first = b"".join(encode_domain_record(candle) for candle in candles)
    second = b"".join(encode_domain_record(candle) for candle in candles)
    assert first == second
    assert datetime.fromisoformat("2026-08-17T07:00:00+00:00") == window.t0
