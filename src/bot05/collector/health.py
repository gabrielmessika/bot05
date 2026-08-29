"""Immutable public-feed health reducer with explicit recovery evidence."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum

_SHA256 = re.compile(r"^[a-f0-9]{64}$")


class CollectorHealthError(ValueError):
    """Raised when collector health transitions are ambiguous or unsafe."""


class FeedHealthCode(StrEnum):
    DISCONNECTED = "disconnected"
    NO_HEARTBEAT = "no_heartbeat"
    STALE_FEED = "stale_feed"
    CLOCK_DRIFT = "clock_drift"
    SEQUENCE_GAP = "sequence_gap"


def _require_utc(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise CollectorHealthError(f"{name} must be timezone-aware UTC")


@dataclass(frozen=True, slots=True)
class FeedHealthPolicy:
    max_staleness_ms: int
    max_transport_delay_ms: int
    max_clock_offset_ms: int

    def __post_init__(self) -> None:
        if (
            min(
                self.max_staleness_ms,
                self.max_transport_delay_ms,
                self.max_clock_offset_ms,
            )
            <= 0
        ):
            raise CollectorHealthError("feed health limits must be positive")


@dataclass(frozen=True, slots=True)
class FeedHealthState:
    market: str
    connected: bool
    connected_at: datetime | None = None
    last_disconnect_at: datetime | None = None
    last_exchange_time: datetime | None = None
    last_received_at: datetime | None = None
    last_sequence: int | None = None
    reconnect_count: int = 0
    sequence_gap_count: int = 0
    gap_expected_sequence: int | None = None
    gap_observed_sequence: int | None = None
    max_transport_delay_ms: int = 0
    clock_offset_ms: int | None = None
    clock_sampled_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.market.strip():
            raise CollectorHealthError("feed market is required")
        for name, value in (
            ("connected_at", self.connected_at),
            ("last_disconnect_at", self.last_disconnect_at),
            ("last_exchange_time", self.last_exchange_time),
            ("last_received_at", self.last_received_at),
            ("clock_sampled_at", self.clock_sampled_at),
        ):
            if value is not None:
                _require_utc(value, name)
        if (
            min(
                self.reconnect_count,
                self.sequence_gap_count,
                self.max_transport_delay_ms,
            )
            < 0
        ):
            raise CollectorHealthError("feed health counters must be non-negative")
        if (self.gap_expected_sequence is None) != (self.gap_observed_sequence is None):
            raise CollectorHealthError("sequence gap bounds must be paired")

    @property
    def unresolved_sequence_gap(self) -> bool:
        return self.gap_expected_sequence is not None


@dataclass(frozen=True, slots=True)
class GapRepairEvidence:
    first_sequence: int
    last_sequence: int
    records_sha256: str
    verified_at: datetime

    def __post_init__(self) -> None:
        if self.first_sequence < 0 or self.last_sequence < self.first_sequence:
            raise CollectorHealthError("gap repair sequence bounds are invalid")
        if _SHA256.fullmatch(self.records_sha256) is None:
            raise CollectorHealthError("gap repair checksum must be a SHA-256")
        _require_utc(self.verified_at, "gap repair verified_at")


@dataclass(frozen=True, slots=True)
class FeedHealthDecision:
    market: str
    checked_at: datetime
    healthy: bool
    codes: tuple[FeedHealthCode, ...]
    kill_required: bool


def initial_feed_health(market: str) -> FeedHealthState:
    return FeedHealthState(market=market, connected=False)


def connect_feed(state: FeedHealthState, connected_at: datetime) -> FeedHealthState:
    _require_utc(connected_at, "connected_at")
    if state.connected:
        raise CollectorHealthError("feed is already connected")
    if state.last_disconnect_at is not None and connected_at < state.last_disconnect_at:
        raise CollectorHealthError("reconnect predates disconnection")
    return replace(
        state,
        connected=True,
        connected_at=connected_at,
        reconnect_count=state.reconnect_count
        + (1 if state.last_disconnect_at is not None else 0),
    )


def disconnect_feed(
    state: FeedHealthState, disconnected_at: datetime
) -> FeedHealthState:
    _require_utc(disconnected_at, "disconnected_at")
    if not state.connected:
        raise CollectorHealthError("feed is already disconnected")
    if state.connected_at is not None and disconnected_at < state.connected_at:
        raise CollectorHealthError("disconnection predates connection")
    return replace(state, connected=False, last_disconnect_at=disconnected_at)


def observe_public_record(
    state: FeedHealthState,
    *,
    sequence: int,
    exchange_time: datetime,
    received_at: datetime,
) -> FeedHealthState:
    """Consume one public event and preserve every sequence anomaly."""

    _require_utc(exchange_time, "exchange_time")
    _require_utc(received_at, "received_at")
    if sequence < 0 or received_at < exchange_time:
        raise CollectorHealthError("public record sequence or timing is invalid")
    if not state.connected:
        raise CollectorHealthError("cannot observe a record while disconnected")
    if state.last_received_at is not None and received_at < state.last_received_at:
        raise CollectorHealthError("received timestamps must be monotonic")
    gap_expected = state.gap_expected_sequence
    gap_observed = state.gap_observed_sequence
    gap_count = state.sequence_gap_count
    if state.last_sequence is not None and sequence != state.last_sequence + 1:
        gap_expected = state.last_sequence + 1
        gap_observed = sequence
        gap_count += 1
    delay_ms = int((received_at - exchange_time).total_seconds() * 1000)
    return replace(
        state,
        last_sequence=sequence,
        last_exchange_time=exchange_time,
        last_received_at=received_at,
        sequence_gap_count=gap_count,
        gap_expected_sequence=gap_expected,
        gap_observed_sequence=gap_observed,
        max_transport_delay_ms=max(state.max_transport_delay_ms, delay_ms),
    )


def observe_clock_offset(
    state: FeedHealthState, *, offset_ms: int, sampled_at: datetime
) -> FeedHealthState:
    _require_utc(sampled_at, "clock sampled_at")
    if state.clock_sampled_at is not None and sampled_at < state.clock_sampled_at:
        raise CollectorHealthError("clock samples must be chronological")
    return replace(state, clock_offset_ms=offset_ms, clock_sampled_at=sampled_at)


def repair_sequence_gap(
    state: FeedHealthState, evidence: GapRepairEvidence
) -> FeedHealthState:
    """Clear one gap only when the exact missing sequence interval was replayed."""

    if state.gap_expected_sequence is None or state.gap_observed_sequence is None:
        raise CollectorHealthError("feed has no unresolved sequence gap")
    expected_last = state.gap_observed_sequence - 1
    if (
        evidence.first_sequence != state.gap_expected_sequence
        or evidence.last_sequence != expected_last
    ):
        raise CollectorHealthError("gap repair does not cover the missing interval")
    return replace(state, gap_expected_sequence=None, gap_observed_sequence=None)


def evaluate_feed_health(
    state: FeedHealthState,
    policy: FeedHealthPolicy,
    *,
    checked_at: datetime,
    theoretical_position_open: bool,
) -> FeedHealthDecision:
    _require_utc(checked_at, "health checked_at")
    codes: list[FeedHealthCode] = []
    if not state.connected:
        codes.append(FeedHealthCode.DISCONNECTED)
    if state.last_received_at is None:
        codes.append(FeedHealthCode.NO_HEARTBEAT)
    else:
        age_ms = int((checked_at - state.last_received_at).total_seconds() * 1000)
        if age_ms < 0:
            codes.append(FeedHealthCode.CLOCK_DRIFT)
        elif age_ms > policy.max_staleness_ms:
            codes.append(FeedHealthCode.STALE_FEED)
    if state.max_transport_delay_ms > policy.max_transport_delay_ms:
        codes.append(FeedHealthCode.STALE_FEED)
    if (
        state.clock_offset_ms is None
        or abs(state.clock_offset_ms) > policy.max_clock_offset_ms
    ):
        codes.append(FeedHealthCode.CLOCK_DRIFT)
    if state.unresolved_sequence_gap:
        codes.append(FeedHealthCode.SEQUENCE_GAP)
    ordered = tuple(dict.fromkeys(codes))
    return FeedHealthDecision(
        market=state.market,
        checked_at=checked_at,
        healthy=not ordered,
        codes=ordered,
        kill_required=theoretical_position_open and bool(ordered),
    )
