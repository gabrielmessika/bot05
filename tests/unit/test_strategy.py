from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from bot05.features.opening_drive import (
    DriveDirection,
    DriveFilter,
    DriveThreshold,
    OpeningDrive,
)
from bot05.models import Candle, DatasetProvenance
from bot05.strategy import (
    ConfirmationKind,
    EntryPriceObservation,
    LiquidityLevel,
    SetupState,
    StrategyContractError,
    StrategySnapshot,
    StrategySpec,
    TargetKind,
    advance_candle,
    initialize_strategy,
    observe_entry_price,
    register_opening_drive,
)


def _provenance(t0: datetime) -> DatasetProvenance:
    return DatasetProvenance(
        dataset_id="strategy-fixture",
        evidence_tier="H1",
        source="bot05",
        source_path_or_url="/tmp/candles.jsonl",
        raw_sha256="a" * 64,
        manifest_sha256="b" * 64,
        adapter_version="fixture",
        calendar_version="fixture",
        code_version="test",
        config_sha256="c" * 64,
        source_timezone="UTC",
        period_start=t0,
        period_end=t0 + timedelta(hours=2),
    )


def _drive(t0: datetime, direction: DriveDirection) -> OpeningDrive:
    if direction is DriveDirection.LONG:
        close = Decimal("108")
        body = Decimal("800")
        location = Decimal("0.9")
    else:
        close = Decimal("92")
        body = Decimal("-800")
        location = Decimal("0.1")
    return OpeningDrive(
        market="BTC",
        session_id="us_cash_open",
        t0=t0,
        observed_at=t0 + timedelta(minutes=15),
        direction=direction,
        open=Decimal("100"),
        high=Decimal("110"),
        low=Decimal("90"),
        close=close,
        body_bps=body,
        range_bps=Decimal("2000"),
        close_location=location,
        midpoint=Decimal("100"),
    )


def _threshold(t0: datetime, filter: DriveFilter = DriveFilter.NONE) -> DriveThreshold:
    return DriveThreshold(
        market="BTC",
        session_id="us_cash_open",
        filter=filter,
        as_of=t0,
        sample_count=20,
        value=Decimal(0),
        eligible=True,
    )


def _spec(
    *,
    confirmation: ConfirmationKind = ConfirmationKind.BREAKOUT,
    target: TargetKind = TargetKind.FIXED_2R,
    drive_filter: DriveFilter = DriveFilter.NONE,
) -> StrategySpec:
    return StrategySpec(
        market="BTC",
        session_id="us_cash_open",
        drive_filter=drive_filter,
        confirmation=confirmation,
        target=target,
        config_sha256="c" * 64,
        calendar_version="us_cash_2026:test",
        code_version="test",
    )


def _candle(
    t0: datetime,
    minute: int,
    *,
    open: str,
    high: str,
    low: str,
    close: str,
) -> Candle:
    open_time = t0 + timedelta(minutes=minute)
    return Candle(
        market="BTC",
        interval_seconds=60,
        open_time=open_time,
        close_time=open_time + timedelta(minutes=1),
        closed_at=open_time + timedelta(minutes=1),
        open=Decimal(open),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal("1"),
        trade_count=1,
        provenance=_provenance(t0),
    )


def _registered(
    t0: datetime,
    *,
    direction: DriveDirection = DriveDirection.LONG,
    confirmation: ConfirmationKind = ConfirmationKind.BREAKOUT,
    target: TargetKind = TargetKind.FIXED_2R,
    levels: tuple[LiquidityLevel, ...] = (),
) -> StrategySnapshot:
    snapshot = initialize_strategy(
        _spec(confirmation=confirmation, target=target),
        t0=t0,
        source_data_sha256="d" * 64,
        liquidity_levels=levels,
    )
    return register_opening_drive(snapshot, _drive(t0, direction), _threshold(t0))


def _confirmed_long(t0: datetime) -> StrategySnapshot:
    snapshot = _registered(t0)
    previous = _candle(t0, 15, open="108", high="109", low="103", close="104")
    snapshot = advance_candle(snapshot, previous, observed_at=previous.closed_at)
    confirmation = _candle(t0, 16, open="104", high="110", low="99", close="109.5")
    return advance_candle(snapshot, confirmation, observed_at=confirmation.closed_at)


def test_long_breakout_emits_one_intent_after_causal_entry_price() -> None:
    t0 = datetime(2026, 8, 21, 13, 30, tzinfo=UTC)
    snapshot = _registered(t0)
    assert snapshot.state is SetupState.DRIVE_COMPLETE
    previous = _candle(t0, 15, open="108", high="109", low="103", close="104")
    snapshot = advance_candle(snapshot, previous, observed_at=previous.closed_at)
    assert snapshot.state is SetupState.WAITING_PULLBACK
    confirmation = _candle(t0, 16, open="104", high="110", low="99", close="109.5")
    snapshot = advance_candle(
        snapshot, confirmation, observed_at=confirmation.closed_at
    )

    assert snapshot.state is SetupState.WAITING_CONFIRMATION
    assert snapshot.touch is not None
    assert snapshot.confirmation is not None
    assert snapshot.confirmation.same_candle_as_touch
    assert (
        advance_candle(snapshot, confirmation, observed_at=confirmation.closed_at)
        == snapshot
    )

    observation = EntryPriceObservation(
        market="BTC",
        observed_at=confirmation.close_time + timedelta(seconds=1),
        price=Decimal("109"),
        source="screening_next_open",
    )
    completed = observe_entry_price(snapshot, observation)

    assert completed.state is SetupState.INTENT
    assert completed.intent is not None
    assert completed.intent.stop_price == Decimal("90")
    assert completed.intent.target_price == Decimal("147")
    assert completed.intent.same_candle_touch_confirmation
    assert observe_entry_price(completed, observation) == completed
    assert (
        advance_candle(completed, previous, observed_at=previous.closed_at) == completed
    )


def test_short_breakout_is_price_symmetric() -> None:
    t0 = datetime(2026, 8, 21, 13, 30, tzinfo=UTC)
    snapshot = _registered(
        t0, direction=DriveDirection.SHORT, target=TargetKind.FIXED_1R
    )
    previous = _candle(t0, 15, open="92", high="97", low="91", close="96")
    snapshot = advance_candle(snapshot, previous, observed_at=previous.closed_at)
    confirmation = _candle(t0, 16, open="96", high="101", low="90", close="90.5")
    snapshot = advance_candle(
        snapshot, confirmation, observed_at=confirmation.closed_at
    )
    completed = observe_entry_price(
        snapshot,
        EntryPriceObservation(
            "BTC",
            confirmation.close_time + timedelta(seconds=1),
            Decimal("91"),
            "screening_next_open",
        ),
    )

    assert completed.intent is not None
    assert completed.intent.direction is DriveDirection.SHORT
    assert completed.intent.stop_price == Decimal("110")
    assert completed.intent.target_price == Decimal("72")


@pytest.mark.parametrize(
    ("kind", "previous", "candidate"),
    [
        (
            ConfirmationKind.ENGULF,
            ("104", "105", "102", "103"),
            ("102.5", "106", "99", "105"),
        ),
        (
            ConfirmationKind.MIDPOINT_RECLAIM,
            ("104", "105", "103", "104"),
            ("99", "103", "98", "102"),
        ),
    ],
)
def test_confirmation_ablations_are_fixed_per_spec(
    kind: ConfirmationKind,
    previous: tuple[str, str, str, str],
    candidate: tuple[str, str, str, str],
) -> None:
    t0 = datetime(2026, 8, 21, 13, 30, tzinfo=UTC)
    snapshot = _registered(t0, confirmation=kind)
    first = _candle(
        t0,
        15,
        open=previous[0],
        high=previous[1],
        low=previous[2],
        close=previous[3],
    )
    snapshot = advance_candle(snapshot, first, observed_at=first.closed_at)
    second = _candle(
        t0,
        16,
        open=candidate[0],
        high=candidate[1],
        low=candidate[2],
        close=candidate[3],
    )
    snapshot = advance_candle(snapshot, second, observed_at=second.closed_at)

    assert snapshot.confirmation is not None
    assert snapshot.confirmation.kind is kind


def test_origin_cross_and_expiry_fail_closed() -> None:
    t0 = datetime(2026, 8, 21, 13, 30, tzinfo=UTC)
    snapshot = _registered(t0)
    crossed = _candle(t0, 15, open="108", high="111", low="89", close="109")
    invalidated = advance_candle(snapshot, crossed, observed_at=crossed.closed_at)
    assert invalidated.state is SetupState.INVALIDATED
    assert invalidated.reason == "drive_origin_crossed"

    snapshot = _registered(t0)
    after_expiry = _candle(t0, 60, open="108", high="109", low="103", close="104")
    expired = advance_candle(snapshot, after_expiry, observed_at=after_expiry.closed_at)
    assert expired.state is SetupState.EXPIRED


def test_candle_retry_is_idempotent_but_revision_invalidates() -> None:
    t0 = datetime(2026, 8, 21, 13, 30, tzinfo=UTC)
    snapshot = _registered(t0)
    candle = _candle(t0, 15, open="108", high="109", low="103", close="104")
    observed = advance_candle(snapshot, candle, observed_at=candle.closed_at)

    assert advance_candle(observed, candle, observed_at=candle.closed_at) == observed
    revision = replace(candle, high=Decimal("110"))
    invalidated = advance_candle(observed, revision, observed_at=revision.closed_at)
    assert invalidated.state is SetupState.INVALIDATED
    assert invalidated.reason == "candle_revision_detected"


def test_causal_liquidity_uses_nearest_intact_prior_level() -> None:
    t0 = datetime(2026, 8, 21, 13, 30, tzinfo=UTC)
    levels = (
        LiquidityLevel("crossed_in_drive", Decimal("105"), t0 - timedelta(days=1)),
        LiquidityLevel("nearest", Decimal("120"), t0 - timedelta(days=1)),
        LiquidityLevel("far", Decimal("130"), t0 - timedelta(days=2)),
    )
    snapshot = _registered(
        t0,
        target=TargetKind.CAUSAL_LIQUIDITY,
        levels=levels,
    )
    assert [item.label for item in snapshot.intact_liquidity_levels] == [
        "nearest",
        "far",
    ]
    previous = _candle(t0, 15, open="108", high="109", low="103", close="104")
    snapshot = advance_candle(snapshot, previous, observed_at=previous.closed_at)
    confirmation = _candle(t0, 16, open="104", high="110", low="99", close="109.5")
    snapshot = advance_candle(
        snapshot, confirmation, observed_at=confirmation.closed_at
    )
    completed = observe_entry_price(
        snapshot,
        EntryPriceObservation(
            "BTC",
            confirmation.close_time + timedelta(seconds=1),
            Decimal("109"),
            "screening_next_open",
        ),
    )

    assert completed.intent is not None
    assert completed.intent.target_label == "nearest"
    assert completed.intent.target_price == Decimal("120")


def test_future_liquidity_level_and_unconfirmed_entry_are_rejected() -> None:
    t0 = datetime(2026, 8, 21, 13, 30, tzinfo=UTC)
    with pytest.raises(StrategyContractError, match="future"):
        initialize_strategy(
            _spec(),
            t0=t0,
            source_data_sha256="d" * 64,
            liquidity_levels=(
                LiquidityLevel("future", Decimal("120"), t0 + timedelta(seconds=1)),
            ),
        )
    snapshot = _registered(t0)
    with pytest.raises(StrategyContractError, match="confirmed"):
        observe_entry_price(
            snapshot,
            EntryPriceObservation(
                "BTC", t0 + timedelta(minutes=15), Decimal("108"), "test"
            ),
        )


def test_post_drive_candle_gap_fails_closed() -> None:
    t0 = datetime(2026, 8, 21, 13, 30, tzinfo=UTC)
    snapshot = _registered(t0)
    skipped_minute = _candle(t0, 16, open="108", high="109", low="103", close="104")

    invalidated = advance_candle(
        snapshot, skipped_minute, observed_at=skipped_minute.closed_at
    )

    assert invalidated.state is SetupState.INVALIDATED
    assert invalidated.reason == "candle_gap"


def test_drive_filter_family_is_locked_before_registration() -> None:
    t0 = datetime(2026, 8, 21, 13, 30, tzinfo=UTC)
    q50_spec = _spec(drive_filter=DriveFilter.Q50)
    q50 = initialize_strategy(q50_spec, t0=t0, source_data_sha256="d" * 64)
    q50_threshold = replace(_threshold(t0, DriveFilter.Q50), value=Decimal("700"))
    accepted = register_opening_drive(
        q50, _drive(t0, DriveDirection.LONG), q50_threshold
    )
    assert accepted.state is SetupState.DRIVE_COMPLETE

    q75_spec = _spec(drive_filter=DriveFilter.Q75)
    q75 = initialize_strategy(q75_spec, t0=t0, source_data_sha256="d" * 64)
    q75_threshold = replace(_threshold(t0, DriveFilter.Q75), value=Decimal("900"))
    rejected = register_opening_drive(
        q75, _drive(t0, DriveDirection.LONG), q75_threshold
    )
    assert rejected.state is SetupState.INVALIDATED
    assert rejected.reason == "drive_filter_failed"


def test_causal_liquidity_without_intact_target_emits_no_intent() -> None:
    t0 = datetime(2026, 8, 21, 13, 30, tzinfo=UTC)
    snapshot = _registered(t0, target=TargetKind.CAUSAL_LIQUIDITY)
    previous = _candle(t0, 15, open="108", high="109", low="103", close="104")
    snapshot = advance_candle(snapshot, previous, observed_at=previous.closed_at)
    confirmation = _candle(t0, 16, open="104", high="110", low="99", close="109.5")
    snapshot = advance_candle(
        snapshot, confirmation, observed_at=confirmation.closed_at
    )

    rejected = observe_entry_price(
        snapshot,
        EntryPriceObservation(
            "BTC",
            confirmation.close_time + timedelta(seconds=1),
            Decimal("109"),
            "screening_next_open",
        ),
    )

    assert rejected.state is SetupState.INVALIDATED
    assert rejected.intent is None
    assert rejected.reason == "causal_liquidity_target_missing"


@pytest.mark.parametrize(
    ("offset_seconds", "price", "reason"),
    [
        (-1, "109", "entry_price_lookahead"),
        (301, "109", "entry_price_stale"),
        (1, "89", "entry_gapped_beyond_stop"),
    ],
)
def test_entry_observation_guards_fail_closed(
    offset_seconds: int, price: str, reason: str
) -> None:
    t0 = datetime(2026, 8, 21, 13, 30, tzinfo=UTC)
    snapshot = _confirmed_long(t0)
    assert snapshot.confirmation is not None
    observation = EntryPriceObservation(
        "BTC",
        snapshot.confirmation.confirmed_at + timedelta(seconds=offset_seconds),
        Decimal(price),
        "screening_next_open",
    )

    rejected = observe_entry_price(snapshot, observation)

    assert rejected.state is SetupState.INVALIDATED
    assert rejected.intent is None
    assert rejected.reason == reason
