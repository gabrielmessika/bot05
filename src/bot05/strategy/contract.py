"""Immutable facts and snapshots for the BOT05 strategy state machine."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from bot05.features.opening_drive import (
    DriveDirection,
    DriveFilter,
    OpeningDrive,
)
from bot05.models import Candle

_SHA256 = re.compile(r"^[a-f0-9]{64}$")


class StrategyContractError(ValueError):
    """Raised when a strategy fact violates a causal or price invariant."""


class ConfirmationKind(StrEnum):
    BREAKOUT = "breakout_confirm"
    ENGULF = "engulf_confirm"
    MIDPOINT_RECLAIM = "midpoint_reclaim"


class TargetKind(StrEnum):
    FIXED_1R = "fixed_1r"
    FIXED_2R = "fixed_2r"
    CAUSAL_LIQUIDITY = "causal_liquidity"


class SetupState(StrEnum):
    WAITING_OPEN = "waiting_open"
    DRIVE_COMPLETE = "drive_complete"
    WAITING_PULLBACK = "waiting_pullback"
    WAITING_CONFIRMATION = "waiting_confirmation"
    INTENT = "intent"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"

    @property
    def terminal(self) -> bool:
        return self in {
            SetupState.INTENT,
            SetupState.EXPIRED,
            SetupState.INVALIDATED,
        }


def _require_utc(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise StrategyContractError(f"{name} must be timezone-aware UTC")


def _positive(value: Decimal, name: str, *, allow_zero: bool = False) -> None:
    if not value.is_finite() or value < 0 or (value == 0 and not allow_zero):
        qualifier = "non-negative" if allow_zero else "positive"
        raise StrategyContractError(f"{name} must be finite and {qualifier}")


@dataclass(frozen=True, slots=True)
class StrategySpec:
    market: str
    session_id: str
    drive_filter: DriveFilter
    confirmation: ConfirmationKind
    target: TargetKind
    config_sha256: str
    calendar_version: str
    code_version: str
    pullback_expiry_minutes: int = 60
    max_entry_delay_seconds: int = 300

    def __post_init__(self) -> None:
        if not self.market.strip() or not self.session_id.strip():
            raise StrategyContractError("market and session_id are required")
        if _SHA256.fullmatch(self.config_sha256) is None:
            raise StrategyContractError("config_sha256 must be a SHA-256 digest")
        if not self.calendar_version.strip() or not self.code_version.strip():
            raise StrategyContractError("calendar and code versions are required")
        if self.pullback_expiry_minutes != 60:
            raise StrategyContractError(
                "strategy v0 pullback expiry must be 60 minutes"
            )
        if not 0 < self.max_entry_delay_seconds <= 300:
            raise StrategyContractError("entry delay must be within five minutes")

    @property
    def spec_id(self) -> str:
        payload = {
            "confirmation": self.confirmation.value,
            "calendar_version": self.calendar_version,
            "code_version": self.code_version,
            "config_sha256": self.config_sha256,
            "drive_filter": self.drive_filter.value,
            "market": self.market,
            "max_entry_delay_seconds": self.max_entry_delay_seconds,
            "pullback_expiry_minutes": self.pullback_expiry_minutes,
            "session_id": self.session_id,
            "target": self.target.value,
        }
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class LiquidityLevel:
    label: str
    price: Decimal
    known_at: datetime

    def __post_init__(self) -> None:
        if not self.label.strip():
            raise StrategyContractError("liquidity level label is required")
        _positive(self.price, "liquidity level price")
        _require_utc(self.known_at, "liquidity level known_at")


@dataclass(frozen=True, slots=True)
class PullbackTouch:
    touched_at: datetime
    candle_open_time: datetime
    midpoint: Decimal

    def __post_init__(self) -> None:
        _require_utc(self.touched_at, "touch timestamp")
        _require_utc(self.candle_open_time, "touch candle open_time")
        _positive(self.midpoint, "midpoint")


@dataclass(frozen=True, slots=True)
class Confirmation:
    kind: ConfirmationKind
    confirmed_at: datetime
    candle_open_time: datetime
    same_candle_as_touch: bool

    def __post_init__(self) -> None:
        _require_utc(self.confirmed_at, "confirmation timestamp")
        _require_utc(self.candle_open_time, "confirmation candle open_time")


@dataclass(frozen=True, slots=True)
class EntryPriceObservation:
    market: str
    observed_at: datetime
    price: Decimal
    source: str

    def __post_init__(self) -> None:
        if not self.market.strip() or not self.source.strip():
            raise StrategyContractError("entry market and source are required")
        _require_utc(self.observed_at, "entry observed_at")
        _positive(self.price, "entry price")


@dataclass(frozen=True, slots=True)
class TradeIntent:
    intent_id: str
    spec_id: str
    market: str
    session_id: str
    t0: datetime
    decided_at: datetime
    direction: DriveDirection
    confirmation: ConfirmationKind
    touch_time: datetime
    confirmation_time: datetime
    same_candle_touch_confirmation: bool
    entry_source: str
    entry_price: Decimal
    stop_price: Decimal
    target_kind: TargetKind
    target_price: Decimal
    target_label: str
    midpoint: Decimal
    source_data_sha256: str
    config_sha256: str
    calendar_version: str
    strategy_code_version: str

    def __post_init__(self) -> None:
        if not self.intent_id or not self.spec_id:
            raise StrategyContractError("intent and spec identities are required")
        if not self.market.strip() or not self.session_id.strip():
            raise StrategyContractError("intent market and session are required")
        if not self.entry_source.strip() or not self.target_label.strip():
            raise StrategyContractError("entry source and target label are required")
        for digest_name, digest in (
            ("source_data_sha256", self.source_data_sha256),
            ("config_sha256", self.config_sha256),
        ):
            if _SHA256.fullmatch(digest) is None:
                raise StrategyContractError(f"{digest_name} must be a SHA-256 digest")
        if not self.calendar_version.strip() or not self.strategy_code_version.strip():
            raise StrategyContractError(
                "intent calendar and code versions are required"
            )
        for name, timestamp in (
            ("t0", self.t0),
            ("decided_at", self.decided_at),
            ("touch_time", self.touch_time),
            ("confirmation_time", self.confirmation_time),
        ):
            _require_utc(timestamp, name)
        if not self.t0 < self.touch_time <= self.confirmation_time <= self.decided_at:
            raise StrategyContractError("intent timestamps are not causal")
        for price_name, price in (
            ("entry_price", self.entry_price),
            ("stop_price", self.stop_price),
            ("target_price", self.target_price),
            ("midpoint", self.midpoint),
        ):
            _positive(price, price_name)
        if self.direction is DriveDirection.LONG and not (
            self.stop_price < self.entry_price < self.target_price
        ):
            raise StrategyContractError("long intent price ordering is invalid")
        if self.direction is DriveDirection.SHORT and not (
            self.target_price < self.entry_price < self.stop_price
        ):
            raise StrategyContractError("short intent price ordering is invalid")


@dataclass(frozen=True, slots=True)
class StrategySnapshot:
    spec: StrategySpec
    t0: datetime
    source_data_sha256: str
    state: SetupState
    drive: OpeningDrive | None
    touch: PullbackTouch | None
    confirmation: Confirmation | None
    intent: TradeIntent | None
    last_candle: Candle | None
    processed_candles: tuple[tuple[datetime, str], ...]
    intact_liquidity_levels: tuple[LiquidityLevel, ...]
    reason: str | None = None

    def __post_init__(self) -> None:
        _require_utc(self.t0, "snapshot t0")
        if _SHA256.fullmatch(self.source_data_sha256) is None:
            raise StrategyContractError("source_data_sha256 must be a SHA-256 digest")
        if self.state is SetupState.INTENT and self.intent is None:
            raise StrategyContractError("intent state requires a trade intent")
        if self.intent is not None and self.state is not SetupState.INTENT:
            raise StrategyContractError("trade intent requires intent state")
        if (
            self.state in {SetupState.EXPIRED, SetupState.INVALIDATED}
            and not self.reason
        ):
            raise StrategyContractError("terminal refusal state requires a reason")
        if len({item[0] for item in self.processed_candles}) != len(
            self.processed_candles
        ):
            raise StrategyContractError("processed candle timestamps must be unique")
        if any(item.known_at >= self.t0 for item in self.intact_liquidity_levels):
            raise StrategyContractError(
                "liquidity levels must be known before session t0"
            )
