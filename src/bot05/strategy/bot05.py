"""Functional, exact-once BOT05 strategy state transitions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

from bot05.features.opening_drive import (
    DriveDirection,
    DriveThreshold,
    OpeningDrive,
    passes_drive_filter,
)
from bot05.models import Candle, domain_record_sha256
from bot05.strategy.contract import (
    Confirmation,
    ConfirmationKind,
    EntryPriceObservation,
    LiquidityLevel,
    PullbackTouch,
    SetupState,
    StrategyContractError,
    StrategySnapshot,
    StrategySpec,
    TargetKind,
    TradeIntent,
)


def _require_utc(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise StrategyContractError(f"{name} must be timezone-aware UTC")


def _terminal(
    snapshot: StrategySnapshot, state: SetupState, reason: str
) -> StrategySnapshot:
    return replace(snapshot, state=state, reason=reason)


def initialize_strategy(
    spec: StrategySpec,
    *,
    t0: datetime,
    source_data_sha256: str,
    liquidity_levels: tuple[LiquidityLevel, ...] = (),
) -> StrategySnapshot:
    """Create an empty session snapshot with levels known no later than t0."""

    _require_utc(t0, "t0")
    if any(item.known_at >= t0 for item in liquidity_levels):
        raise StrategyContractError("liquidity level contains future information")
    if len({item.label for item in liquidity_levels}) != len(liquidity_levels):
        raise StrategyContractError("liquidity level labels must be unique")
    ordered_levels = tuple(
        sorted(liquidity_levels, key=lambda item: (item.price, item.label))
    )
    return StrategySnapshot(
        spec=spec,
        t0=t0,
        source_data_sha256=source_data_sha256,
        state=SetupState.WAITING_OPEN,
        drive=None,
        touch=None,
        confirmation=None,
        intent=None,
        last_candle=None,
        processed_candles=(),
        intact_liquidity_levels=ordered_levels,
    )


def register_opening_drive(
    snapshot: StrategySnapshot,
    drive: OpeningDrive,
    threshold: DriveThreshold,
) -> StrategySnapshot:
    """Register one eligible drive and causally remove levels already crossed."""

    if snapshot.state is not SetupState.WAITING_OPEN:
        if snapshot.drive == drive:
            return snapshot
        raise StrategyContractError("opening drive can only be registered once")
    if (
        drive.market != snapshot.spec.market
        or drive.session_id != snapshot.spec.session_id
        or drive.t0 != snapshot.t0
        or threshold.filter is not snapshot.spec.drive_filter
    ):
        raise StrategyContractError("opening drive scope disagrees with strategy")
    if not passes_drive_filter(drive, threshold):
        return replace(
            snapshot,
            state=SetupState.INVALIDATED,
            drive=drive,
            reason="drive_filter_failed",
        )
    intact = tuple(
        item
        for item in snapshot.intact_liquidity_levels
        if not drive.low <= item.price <= drive.high
    )
    return replace(
        snapshot,
        state=SetupState.DRIVE_COMPLETE,
        drive=drive,
        intact_liquidity_levels=intact,
    )


def _is_touch(drive: OpeningDrive, candle: Candle) -> bool:
    if drive.direction is DriveDirection.LONG:
        return candle.low <= drive.midpoint
    return candle.high >= drive.midpoint


def _is_invalidated(drive: OpeningDrive, candle: Candle) -> bool:
    if drive.direction is DriveDirection.LONG:
        return candle.low <= drive.low
    return candle.high >= drive.high


def _is_confirmation(
    kind: ConfirmationKind,
    drive: OpeningDrive,
    previous: Candle | None,
    candle: Candle,
) -> bool:
    bullish = candle.close > candle.open
    bearish = candle.close < candle.open
    if kind is ConfirmationKind.MIDPOINT_RECLAIM:
        if drive.direction is DriveDirection.LONG:
            return bullish and candle.close > drive.midpoint
        return bearish and candle.close < drive.midpoint
    if previous is None:
        return False
    if kind is ConfirmationKind.BREAKOUT:
        if drive.direction is DriveDirection.LONG:
            return bullish and candle.close > previous.high
        return bearish and candle.close < previous.low
    previous_body_low = min(previous.open, previous.close)
    previous_body_high = max(previous.open, previous.close)
    if drive.direction is DriveDirection.LONG:
        return (
            bullish
            and candle.open <= previous_body_low
            and candle.close >= previous_body_high
        )
    return (
        bearish
        and candle.open >= previous_body_high
        and candle.close <= previous_body_low
    )


def _remove_traversed_levels(
    levels: tuple[LiquidityLevel, ...], candle: Candle
) -> tuple[LiquidityLevel, ...]:
    return tuple(item for item in levels if not candle.low <= item.price <= candle.high)


def advance_candle(
    snapshot: StrategySnapshot,
    candle: Candle,
    *,
    observed_at: datetime,
) -> StrategySnapshot:
    """Consume one closed post-drive candle with idempotent retry semantics."""

    _require_utc(observed_at, "observed_at")
    if snapshot.state.terminal:
        return snapshot
    if candle.market != snapshot.spec.market or candle.interval_seconds not in {
        60,
        300,
    }:
        return _terminal(snapshot, SetupState.INVALIDATED, "candle_scope_mismatch")
    if candle.closed_at > observed_at:
        return _terminal(snapshot, SetupState.INVALIDATED, "candle_not_observable")
    candle_hash = domain_record_sha256(candle)
    processed = dict(snapshot.processed_candles)
    if candle.open_time in processed:
        if processed[candle.open_time] == candle_hash:
            return snapshot
        return _terminal(snapshot, SetupState.INVALIDATED, "candle_revision_detected")
    if (
        snapshot.last_candle is not None
        and candle.open_time < snapshot.last_candle.open_time
    ):
        return _terminal(snapshot, SetupState.INVALIDATED, "out_of_order_candle")
    if snapshot.drive is None:
        return _terminal(snapshot, SetupState.INVALIDATED, "opening_drive_missing")
    drive_end = snapshot.t0 + timedelta(minutes=15)
    if candle.open_time < drive_end:
        return _terminal(snapshot, SetupState.INVALIDATED, "candle_overlaps_drive")
    expiry = snapshot.t0 + timedelta(minutes=snapshot.spec.pullback_expiry_minutes)
    if candle.close_time > expiry:
        return _terminal(snapshot, SetupState.EXPIRED, "pullback_window_expired")
    expected_open = (
        drive_end if snapshot.last_candle is None else snapshot.last_candle.close_time
    )
    if candle.open_time != expected_open:
        return _terminal(snapshot, SetupState.INVALIDATED, "candle_gap")
    if (
        snapshot.last_candle is not None
        and candle.interval_seconds != snapshot.last_candle.interval_seconds
    ):
        return _terminal(snapshot, SetupState.INVALIDATED, "candle_interval_changed")
    if snapshot.confirmation is not None:
        return _terminal(snapshot, SetupState.INVALIDATED, "entry_price_missing")

    processed_candles = snapshot.processed_candles + ((candle.open_time, candle_hash),)
    intact_levels = _remove_traversed_levels(snapshot.intact_liquidity_levels, candle)
    base = replace(
        snapshot,
        state=(
            SetupState.WAITING_PULLBACK
            if snapshot.touch is None
            else SetupState.WAITING_CONFIRMATION
        ),
        last_candle=candle,
        processed_candles=processed_candles,
        intact_liquidity_levels=intact_levels,
    )
    if _is_invalidated(snapshot.drive, candle):
        return _terminal(base, SetupState.INVALIDATED, "drive_origin_crossed")

    touch = snapshot.touch
    touched_now = touch is None and _is_touch(snapshot.drive, candle)
    if touched_now:
        touch = PullbackTouch(
            touched_at=candle.close_time,
            candle_open_time=candle.open_time,
            midpoint=snapshot.drive.midpoint,
        )
        base = replace(base, state=SetupState.WAITING_CONFIRMATION, touch=touch)
    if touch is None:
        return base
    if not _is_confirmation(
        snapshot.spec.confirmation,
        snapshot.drive,
        snapshot.last_candle,
        candle,
    ):
        return base
    confirmation = Confirmation(
        kind=snapshot.spec.confirmation,
        confirmed_at=candle.close_time,
        candle_open_time=candle.open_time,
        same_candle_as_touch=touched_now,
    )
    return replace(base, confirmation=confirmation)


def _fixed_target(
    direction: DriveDirection,
    entry: Decimal,
    stop: Decimal,
    multiple: Decimal,
) -> Decimal:
    risk = abs(entry - stop)
    if direction is DriveDirection.LONG:
        return entry + multiple * risk
    return entry - multiple * risk


def _liquidity_target(
    direction: DriveDirection,
    entry: Decimal,
    levels: tuple[LiquidityLevel, ...],
) -> LiquidityLevel | None:
    candidates = tuple(
        item
        for item in levels
        if (
            item.price > entry
            if direction is DriveDirection.LONG
            else item.price < entry
        )
    )
    if not candidates:
        return None
    return min(candidates, key=lambda item: (abs(item.price - entry), item.label))


def _intent_id(payload: dict[str, str | bool]) -> str:
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def observe_entry_price(
    snapshot: StrategySnapshot, observation: EntryPriceObservation
) -> StrategySnapshot:
    """Emit exactly one intent from the first causal executable price observation."""

    if snapshot.state.terminal:
        return snapshot
    if (
        snapshot.drive is None
        or snapshot.touch is None
        or snapshot.confirmation is None
    ):
        raise StrategyContractError("entry price requires a confirmed setup")
    if observation.market != snapshot.spec.market:
        return _terminal(snapshot, SetupState.INVALIDATED, "entry_market_mismatch")
    if observation.observed_at < snapshot.confirmation.confirmed_at:
        return _terminal(snapshot, SetupState.INVALIDATED, "entry_price_lookahead")
    if observation.observed_at > snapshot.confirmation.confirmed_at + timedelta(
        seconds=snapshot.spec.max_entry_delay_seconds
    ):
        return _terminal(snapshot, SetupState.INVALIDATED, "entry_price_stale")

    drive = snapshot.drive
    stop = drive.low if drive.direction is DriveDirection.LONG else drive.high
    if (
        drive.direction is DriveDirection.LONG
        and observation.price <= stop
        or drive.direction is DriveDirection.SHORT
        and observation.price >= stop
    ):
        return _terminal(snapshot, SetupState.INVALIDATED, "entry_gapped_beyond_stop")

    target_label: str
    if snapshot.spec.target is TargetKind.FIXED_1R:
        target = _fixed_target(drive.direction, observation.price, stop, Decimal(1))
        target_label = TargetKind.FIXED_1R.value
    elif snapshot.spec.target is TargetKind.FIXED_2R:
        target = _fixed_target(drive.direction, observation.price, stop, Decimal(2))
        target_label = TargetKind.FIXED_2R.value
    else:
        selected = _liquidity_target(
            drive.direction,
            observation.price,
            snapshot.intact_liquidity_levels,
        )
        if selected is None:
            return _terminal(
                snapshot, SetupState.INVALIDATED, "causal_liquidity_target_missing"
            )
        target = selected.price
        target_label = selected.label

    identity = {
        "confirmation": snapshot.confirmation.kind.value,
        "confirmation_time": snapshot.confirmation.confirmed_at.isoformat(),
        "direction": drive.direction.value,
        "entry_price": str(observation.price),
        "entry_source": observation.source,
        "market": snapshot.spec.market,
        "session_id": snapshot.spec.session_id,
        "spec_id": snapshot.spec.spec_id,
        "source_data_sha256": snapshot.source_data_sha256,
        "stop_price": str(stop),
        "t0": snapshot.t0.isoformat(),
        "target_label": target_label,
        "target_price": str(target),
        "touch_time": snapshot.touch.touched_at.isoformat(),
        "same_candle": snapshot.confirmation.same_candle_as_touch,
    }
    intent = TradeIntent(
        intent_id=_intent_id(identity),
        spec_id=snapshot.spec.spec_id,
        market=snapshot.spec.market,
        session_id=snapshot.spec.session_id,
        t0=snapshot.t0,
        decided_at=observation.observed_at,
        direction=drive.direction,
        confirmation=snapshot.confirmation.kind,
        touch_time=snapshot.touch.touched_at,
        confirmation_time=snapshot.confirmation.confirmed_at,
        same_candle_touch_confirmation=snapshot.confirmation.same_candle_as_touch,
        entry_source=observation.source,
        entry_price=observation.price,
        stop_price=stop,
        target_kind=snapshot.spec.target,
        target_price=target,
        target_label=target_label,
        midpoint=drive.midpoint,
        source_data_sha256=snapshot.source_data_sha256,
        config_sha256=snapshot.spec.config_sha256,
        calendar_version=snapshot.spec.calendar_version,
        strategy_code_version=snapshot.spec.code_version,
    )
    return replace(snapshot, state=SetupState.INTENT, intent=intent)
