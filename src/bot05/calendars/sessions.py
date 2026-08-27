"""Timezone-aware BOT05 session resolution with fail-closed market hours."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bot05.calendars.holidays import (
    CalendarCoverageError,
    HolidayCalendar,
    TradingDayKind,
)


class SessionCalendarError(ValueError):
    """Raised when a session or external-hours definition is invalid."""


class LocalTimeResolutionError(SessionCalendarError):
    """Raised for ambiguous or nonexistent local wall-clock times."""


class OutsideExternalState(StrEnum):
    INTERNAL_ORACLE = "internal_oracle"
    CLOSED = "closed"


class ExternalMarketState(StrEnum):
    EXTERNAL_OPEN = "external_open"
    INTERNAL_ORACLE = "internal_oracle"
    CLOSED = "closed"
    UNKNOWN = "unknown"


class SessionRejectionReason(StrEnum):
    CALENDAR_OUT_OF_RANGE = "calendar_out_of_range"
    WEEKEND = "weekend"
    HOLIDAY = "holiday"
    AMBIGUOUS_LOCAL_TIME = "ambiguous_local_time"
    NONEXISTENT_LOCAL_TIME = "nonexistent_local_time"
    OUTSIDE_EXTERNAL_SESSION = "outside_external_session"
    HORIZON_AFTER_EXTERNAL_CLOSE = "horizon_after_external_close"


@dataclass(frozen=True, slots=True)
class SessionDefinition:
    name: str
    timezone_name: str
    local_open: time
    drive_minutes: int = 15
    pullback_expiry_minutes: int = 60
    maximum_position_minutes: int = 120

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise SessionCalendarError("session name must not be blank")
        try:
            ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise SessionCalendarError("session timezone must be IANA") from exc
        if self.local_open.tzinfo is not None:
            raise SessionCalendarError("local_open must be a naive wall-clock time")
        if (
            min(
                self.drive_minutes,
                self.pullback_expiry_minutes,
                self.maximum_position_minutes,
            )
            <= 0
        ):
            raise SessionCalendarError("session horizons must be positive")
        if self.drive_minutes >= self.pullback_expiry_minutes:
            raise SessionCalendarError("drive must end before pullback expiry")


EUROPE_OPEN = SessionDefinition(
    name="europe_open",
    timezone_name="Europe/Paris",
    local_open=time(9, 0),
)
US_CASH_OPEN = SessionDefinition(
    name="us_cash_open",
    timezone_name="America/New_York",
    local_open=time(9, 30),
)
VIDEO_US_1500 = SessionDefinition(
    name="video_us_1500",
    timezone_name="Europe/Paris",
    local_open=time(15, 0),
)


@dataclass(frozen=True, slots=True)
class MarketHours:
    market: str
    timezone_name: str
    regular_open: time
    regular_close: time
    calendar: HolidayCalendar
    outside_external_state: OutsideExternalState

    def __post_init__(self) -> None:
        if not self.market.strip():
            raise SessionCalendarError("market must not be blank")
        try:
            ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise SessionCalendarError("market timezone must be IANA") from exc
        if self.timezone_name != self.calendar.timezone_name:
            raise SessionCalendarError("market and holiday timezones must match")
        if (
            self.regular_open.tzinfo is not None
            or self.regular_close.tzinfo is not None
        ):
            raise SessionCalendarError("market hours must be naive local times")
        if self.regular_open == self.regular_close:
            raise SessionCalendarError("24-hour external windows must be explicit")


@dataclass(frozen=True, slots=True)
class SessionWindow:
    market: str
    session_name: str
    session_date: date
    t0: datetime
    drive_end: datetime
    pullback_expiry: datetime
    latest_exit: datetime
    external_open: datetime
    external_close: datetime
    calendar_version: str
    day_kind: TradingDayKind

    def __post_init__(self) -> None:
        values = (
            self.t0,
            self.drive_end,
            self.pullback_expiry,
            self.latest_exit,
            self.external_open,
            self.external_close,
        )
        if any(
            value.tzinfo is None or value.utcoffset() != timedelta(0)
            for value in values
        ):
            raise SessionCalendarError("session window timestamps must be UTC")
        if not (
            self.external_open
            <= self.t0
            < self.drive_end
            < self.pullback_expiry
            < self.latest_exit
            <= self.external_close
        ):
            raise SessionCalendarError("session window ordering is invalid")


@dataclass(frozen=True, slots=True)
class SessionResolution:
    window: SessionWindow | None
    rejection_reason: SessionRejectionReason | None

    def __post_init__(self) -> None:
        if (self.window is None) == (self.rejection_reason is None):
            raise SessionCalendarError(
                "resolution must contain a window or a rejection"
            )

    @property
    def eligible(self) -> bool:
        return self.window is not None


def _strict_localize(local_date: date, local_time: time, zone: ZoneInfo) -> datetime:
    naive = datetime.combine(local_date, local_time)
    candidates: list[datetime] = []
    for fold in (0, 1):
        candidate = naive.replace(tzinfo=zone, fold=fold)
        roundtrip = candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
        if roundtrip == naive:
            candidates.append(candidate)
    if not candidates:
        raise LocalTimeResolutionError("nonexistent local time")
    if len(candidates) == 2 and candidates[0].utcoffset() != candidates[1].utcoffset():
        raise LocalTimeResolutionError("ambiguous local time")
    return candidates[0]


def _market_bounds(
    reference_utc: datetime, market_hours: MarketHours
) -> tuple[datetime, datetime, date]:
    if reference_utc.tzinfo is None or reference_utc.utcoffset() != timedelta(0):
        raise SessionCalendarError("reference_utc must be timezone-aware UTC")
    zone = ZoneInfo(market_hours.timezone_name)
    local = reference_utc.astimezone(zone)
    local_date = local.date()
    if market_hours.regular_open < market_hours.regular_close:
        trading_date = local_date
        open_date = local_date
        close_date = local_date
    elif local.timetz().replace(tzinfo=None) >= market_hours.regular_open:
        trading_date = local_date
        open_date = local_date
        close_date = local_date + timedelta(days=1)
    else:
        trading_date = local_date - timedelta(days=1)
        open_date = trading_date
        close_date = local_date
    external_open = _strict_localize(open_date, market_hours.regular_open, zone)
    early_close = market_hours.calendar.early_close_for(trading_date)
    close_time = early_close if early_close is not None else market_hours.regular_close
    if market_hours.regular_open > market_hours.regular_close:
        close_date = trading_date + timedelta(days=1)
    external_close = _strict_localize(close_date, close_time, zone)
    return external_open.astimezone(UTC), external_close.astimezone(UTC), trading_date


def _outside_state(market_hours: MarketHours) -> ExternalMarketState:
    if market_hours.outside_external_state is OutsideExternalState.INTERNAL_ORACLE:
        return ExternalMarketState.INTERNAL_ORACLE
    return ExternalMarketState.CLOSED


def external_market_state_at(
    instant_utc: datetime, market_hours: MarketHours
) -> ExternalMarketState:
    """Return UNKNOWN outside calendar coverage and never infer a valid session."""

    try:
        external_open, external_close, trading_date = _market_bounds(
            instant_utc, market_hours
        )
        day_kind = market_hours.calendar.day_kind(trading_date)
    except CalendarCoverageError:
        return ExternalMarketState.UNKNOWN
    if day_kind in {TradingDayKind.WEEKEND, TradingDayKind.HOLIDAY}:
        return _outside_state(market_hours)
    if external_open <= instant_utc < external_close:
        return ExternalMarketState.EXTERNAL_OPEN
    return _outside_state(market_hours)


def resolve_session(
    session_date: date,
    definition: SessionDefinition,
    market_hours: MarketHours,
) -> SessionResolution:
    """Resolve a causal session or return one explicit fail-closed reason."""

    try:
        session_zone = ZoneInfo(definition.timezone_name)
        t0_local = _strict_localize(session_date, definition.local_open, session_zone)
    except LocalTimeResolutionError as exc:
        reason = (
            SessionRejectionReason.AMBIGUOUS_LOCAL_TIME
            if "ambiguous" in str(exc)
            else SessionRejectionReason.NONEXISTENT_LOCAL_TIME
        )
        return SessionResolution(None, reason)
    t0 = t0_local.astimezone(UTC)
    try:
        external_open, external_close, trading_date = _market_bounds(t0, market_hours)
        day_kind = market_hours.calendar.day_kind(trading_date)
    except CalendarCoverageError:
        return SessionResolution(None, SessionRejectionReason.CALENDAR_OUT_OF_RANGE)
    if day_kind is TradingDayKind.WEEKEND:
        return SessionResolution(None, SessionRejectionReason.WEEKEND)
    if day_kind is TradingDayKind.HOLIDAY:
        return SessionResolution(None, SessionRejectionReason.HOLIDAY)
    if not external_open <= t0 < external_close:
        return SessionResolution(None, SessionRejectionReason.OUTSIDE_EXTERNAL_SESSION)

    drive_end = t0 + timedelta(minutes=definition.drive_minutes)
    pullback_expiry = t0 + timedelta(minutes=definition.pullback_expiry_minutes)
    latest_exit = pullback_expiry + timedelta(
        minutes=definition.maximum_position_minutes
    )
    if latest_exit > external_close:
        return SessionResolution(
            None, SessionRejectionReason.HORIZON_AFTER_EXTERNAL_CLOSE
        )
    return SessionResolution(
        SessionWindow(
            market=market_hours.market,
            session_name=definition.name,
            session_date=session_date,
            t0=t0,
            drive_end=drive_end,
            pullback_expiry=pullback_expiry,
            latest_exit=latest_exit,
            external_open=external_open,
            external_close=external_close,
            calendar_version=market_hours.calendar.version_id,
            day_kind=day_kind,
        ),
        None,
    )
