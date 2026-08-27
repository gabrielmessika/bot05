"""Strict loader for versioned holiday and early-close calendars."""

from __future__ import annotations

import hashlib
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, time
from enum import StrEnum
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_SHA256 = re.compile(r"^[a-f0-9]{64}$")


class HolidayCalendarError(ValueError):
    """Raised when a calendar definition is malformed or inconsistent."""


class CalendarCoverageError(HolidayCalendarError):
    """Raised when a date falls outside a calendar's declared validity."""


class TradingDayKind(StrEnum):
    REGULAR = "regular"
    EARLY_CLOSE = "early_close"
    WEEKEND = "weekend"
    HOLIDAY = "holiday"


@dataclass(frozen=True, slots=True, order=True)
class EarlyClose:
    session_date: date
    local_close: time

    def __post_init__(self) -> None:
        if self.local_close.tzinfo is not None:
            raise HolidayCalendarError("early-close time must be local and naive")


@dataclass(frozen=True, slots=True)
class HolidayCalendar:
    calendar_id: str
    version: str
    timezone_name: str
    valid_from: date
    valid_to: date
    closed_dates: frozenset[date]
    early_closes: tuple[EarlyClose, ...]
    source_sha256: str

    def __post_init__(self) -> None:
        if not self.calendar_id.strip() or not self.version.strip():
            raise HolidayCalendarError("calendar_id and version must not be blank")
        try:
            ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise HolidayCalendarError(
                "timezone_name must be an IANA timezone"
            ) from exc
        if self.valid_from > self.valid_to:
            raise HolidayCalendarError("calendar validity range is inverted")
        if _SHA256.fullmatch(self.source_sha256) is None:
            raise HolidayCalendarError("source_sha256 must be a lowercase SHA-256")
        early_dates = [item.session_date for item in self.early_closes]
        if early_dates != sorted(early_dates) or len(early_dates) != len(
            set(early_dates)
        ):
            raise HolidayCalendarError("early closes must be unique and sorted")
        for item in (*self.closed_dates, *early_dates):
            if not self.valid_from <= item <= self.valid_to:
                raise HolidayCalendarError("calendar exception falls outside validity")
        if self.closed_dates.intersection(early_dates):
            raise HolidayCalendarError("a date cannot be closed and an early close")

    @property
    def version_id(self) -> str:
        return f"{self.calendar_id}:{self.version}:{self.source_sha256[:16]}"

    def day_kind(self, session_date: date) -> TradingDayKind:
        if not self.valid_from <= session_date <= self.valid_to:
            raise CalendarCoverageError(
                f"{session_date} outside {self.valid_from}..{self.valid_to}"
            )
        if session_date.weekday() >= 5:
            return TradingDayKind.WEEKEND
        if session_date in self.closed_dates:
            return TradingDayKind.HOLIDAY
        if any(item.session_date == session_date for item in self.early_closes):
            return TradingDayKind.EARLY_CLOSE
        return TradingDayKind.REGULAR

    def early_close_for(self, session_date: date) -> time | None:
        self.day_kind(session_date)
        for item in self.early_closes:
            if item.session_date == session_date:
                return item.local_close
        return None


@dataclass(frozen=True, slots=True)
class LoadedHolidayCalendar:
    calendar: HolidayCalendar
    source_path: Path
    sha256: str


def _exact(payload: Mapping[str, object], name: str, expected: set[str]) -> None:
    actual = set(payload)
    if actual != expected:
        raise HolidayCalendarError(
            f"[{name}] keys mismatch; missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}"
        )


def _section(document: Mapping[str, object], name: str) -> Mapping[str, object]:
    value = document.get(name)
    if not isinstance(value, dict):
        raise HolidayCalendarError(f"missing or invalid [{name}] section")
    return cast(Mapping[str, object], value)


def _string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise HolidayCalendarError(f"{key} must be a non-empty string")
    return value


def _date(payload: Mapping[str, object], key: str) -> date:
    value = payload.get(key)
    if not isinstance(value, date):
        raise HolidayCalendarError(f"{key} must be an unquoted TOML local date")
    return value


def _dates(payload: Mapping[str, object], key: str) -> frozenset[date]:
    value = payload.get(key)
    if not isinstance(value, list) or not all(isinstance(item, date) for item in value):
        raise HolidayCalendarError(f"{key} must be an array of TOML local dates")
    dates = cast(list[date], value)
    if len(dates) != len(set(dates)):
        raise HolidayCalendarError(f"{key} must not contain duplicates")
    return frozenset(dates)


def _early_closes(document: Mapping[str, object]) -> tuple[EarlyClose, ...]:
    raw = document.get("early_close")
    if not isinstance(raw, list):
        raise HolidayCalendarError("[[early_close]] entries are required, possibly []")
    result: list[EarlyClose] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise HolidayCalendarError(f"early_close[{index}] must be a table")
        payload = cast(Mapping[str, object], item)
        _exact(payload, f"early_close[{index}]", {"date", "local_time"})
        local_time = payload.get("local_time")
        if not isinstance(local_time, time):
            raise HolidayCalendarError("local_time must be an unquoted TOML local time")
        result.append(EarlyClose(_date(payload, "date"), local_time))
    return tuple(sorted(result))


def load_holiday_calendar(path: str | Path) -> LoadedHolidayCalendar:
    """Load a strict TOML calendar and bind its exact bytes to the version."""

    source_path = Path(path).resolve()
    raw = source_path.read_bytes()
    sha256 = hashlib.sha256(raw).hexdigest()
    try:
        parsed = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise HolidayCalendarError(f"cannot parse {source_path}") from exc
    document = cast(Mapping[str, object], parsed)
    _exact(document, "root", {"calendar", "early_close"})
    calendar_section = _section(document, "calendar")
    _exact(
        calendar_section,
        "calendar",
        {
            "calendar_id",
            "version",
            "timezone",
            "valid_from",
            "valid_to",
            "closed_dates",
        },
    )
    calendar = HolidayCalendar(
        calendar_id=_string(calendar_section, "calendar_id"),
        version=_string(calendar_section, "version"),
        timezone_name=_string(calendar_section, "timezone"),
        valid_from=_date(calendar_section, "valid_from"),
        valid_to=_date(calendar_section, "valid_to"),
        closed_dates=_dates(calendar_section, "closed_dates"),
        early_closes=_early_closes(document),
        source_sha256=sha256,
    )
    return LoadedHolidayCalendar(calendar, source_path, sha256)
