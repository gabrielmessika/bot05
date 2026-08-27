from __future__ import annotations

from datetime import UTC, date, datetime, time
from pathlib import Path

import pytest

from bot05.calendars.holidays import (
    CalendarCoverageError,
    HolidayCalendarError,
    TradingDayKind,
    load_holiday_calendar,
)
from bot05.calendars.sessions import (
    EUROPE_OPEN,
    US_CASH_OPEN,
    ExternalMarketState,
    MarketHours,
    OutsideExternalState,
    SessionDefinition,
    SessionRejectionReason,
    external_market_state_at,
    resolve_session,
)

FIXTURES = Path("tests/fixtures/calendars")


def _us_hours(
    *, outside: OutsideExternalState = OutsideExternalState.CLOSED
) -> MarketHours:
    calendar = load_holiday_calendar(FIXTURES / "us_cash_2026.toml").calendar
    return MarketHours(
        market="xyz:SP500",
        timezone_name="America/New_York",
        regular_open=time(9, 30),
        regular_close=time(16, 0),
        calendar=calendar,
        outside_external_state=outside,
    )


def _europe_hours() -> MarketHours:
    calendar = load_holiday_calendar(FIXTURES / "europe_2026.toml").calendar
    return MarketHours(
        market="xyz:GOLD",
        timezone_name="Europe/Paris",
        regular_open=time(8, 0),
        regular_close=time(17, 30),
        calendar=calendar,
        outside_external_state=OutsideExternalState.INTERNAL_ORACLE,
    )


def test_calendar_loader_binds_exact_bytes_and_strict_schema(tmp_path: Path) -> None:
    source = FIXTURES / "us_cash_2026.toml"
    first = load_holiday_calendar(source)
    copied = tmp_path / "calendar.toml"
    copied.write_bytes(source.read_bytes() + b"\n")
    second = load_holiday_calendar(copied)

    assert first.sha256 != second.sha256
    assert first.calendar.version_id != second.calendar.version_id
    invalid = tmp_path / "invalid.toml"
    invalid.write_text(source.read_text() + "unknown = true\n")
    with pytest.raises(HolidayCalendarError, match="keys mismatch"):
        load_holiday_calendar(invalid)


def test_calendar_is_fail_closed_outside_versioned_range() -> None:
    calendar = load_holiday_calendar(FIXTURES / "us_cash_2026.toml").calendar

    with pytest.raises(CalendarCoverageError):
        calendar.day_kind(date(2025, 12, 31))


def test_weekend_holiday_and_early_close_are_distinct() -> None:
    calendar = load_holiday_calendar(FIXTURES / "us_cash_2026.toml").calendar

    assert calendar.day_kind(date(2026, 8, 15)) is TradingDayKind.WEEKEND
    assert calendar.day_kind(date(2026, 7, 3)) is TradingDayKind.HOLIDAY
    assert calendar.day_kind(date(2026, 11, 27)) is TradingDayKind.EARLY_CLOSE
    assert calendar.early_close_for(date(2026, 11, 27)) == time(13, 0)


def test_us_and_europe_sessions_follow_their_own_dst_transitions() -> None:
    us_before = resolve_session(date(2026, 3, 6), US_CASH_OPEN, _us_hours()).window
    us_after = resolve_session(date(2026, 3, 9), US_CASH_OPEN, _us_hours()).window
    eu_before = resolve_session(date(2026, 3, 27), EUROPE_OPEN, _europe_hours()).window
    eu_after = resolve_session(date(2026, 3, 30), EUROPE_OPEN, _europe_hours()).window

    assert us_before is not None and us_before.t0.hour == 14
    assert us_after is not None and us_after.t0.hour == 13
    assert eu_before is not None and eu_before.t0.hour == 8
    assert eu_after is not None and eu_after.t0.hour == 7


@pytest.mark.parametrize(
    ("session_date", "reason"),
    [
        (date(2026, 8, 15), SessionRejectionReason.WEEKEND),
        (date(2026, 7, 3), SessionRejectionReason.HOLIDAY),
        (date(2027, 1, 4), SessionRejectionReason.CALENDAR_OUT_OF_RANGE),
    ],
)
def test_ineligible_days_return_explicit_fail_closed_reason(
    session_date: date, reason: SessionRejectionReason
) -> None:
    resolution = resolve_session(session_date, US_CASH_OPEN, _us_hours())

    assert resolution.eligible is False
    assert resolution.rejection_reason is reason


def test_half_day_accepts_early_setup_but_rejects_excess_horizon() -> None:
    baseline = resolve_session(date(2026, 11, 27), US_CASH_OPEN, _us_hours())
    late = SessionDefinition(
        name="late_test",
        timezone_name="America/New_York",
        local_open=time(11, 0),
    )
    rejected = resolve_session(date(2026, 11, 27), late, _us_hours())

    assert baseline.eligible is True
    assert baseline.window is not None
    assert baseline.window.day_kind is TradingDayKind.EARLY_CLOSE
    assert (
        rejected.rejection_reason is SessionRejectionReason.HORIZON_AFTER_EXTERNAL_CLOSE
    )


def test_xyz_external_to_internal_state_is_explicit() -> None:
    hours = _us_hours(outside=OutsideExternalState.INTERNAL_ORACLE)

    assert (
        external_market_state_at(datetime(2026, 8, 17, 14, tzinfo=UTC), hours)
        is ExternalMarketState.EXTERNAL_OPEN
    )
    assert (
        external_market_state_at(datetime(2026, 8, 17, 23, tzinfo=UTC), hours)
        is ExternalMarketState.INTERNAL_ORACLE
    )
    assert (
        external_market_state_at(datetime(2027, 1, 4, 15, tzinfo=UTC), hours)
        is ExternalMarketState.UNKNOWN
    )


def test_session_is_rejected_when_named_open_is_outside_market_hours() -> None:
    resolution = resolve_session(date(2026, 3, 16), EUROPE_OPEN, _us_hours())

    assert (
        resolution.rejection_reason is SessionRejectionReason.OUTSIDE_EXTERNAL_SESSION
    )


@pytest.mark.parametrize(
    ("session_date", "local_open", "reason"),
    [
        (
            date(2026, 3, 8),
            time(2, 30),
            SessionRejectionReason.NONEXISTENT_LOCAL_TIME,
        ),
        (
            date(2026, 11, 1),
            time(1, 30),
            SessionRejectionReason.AMBIGUOUS_LOCAL_TIME,
        ),
    ],
)
def test_nonexistent_and_ambiguous_wall_times_fail_closed(
    session_date: date, local_open: time, reason: SessionRejectionReason
) -> None:
    definition = SessionDefinition(
        name="dst_edge",
        timezone_name="America/New_York",
        local_open=local_open,
    )

    resolution = resolve_session(session_date, definition, _us_hours())

    assert resolution.rejection_reason is reason
