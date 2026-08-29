from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from bot05.collector import (
    AuthenticationMode,
    FeedHealthCode,
    FeedHealthPolicy,
    GapRepairEvidence,
    PublicChannel,
    PublicCollectorSpec,
    connect_feed,
    disconnect_feed,
    evaluate_feed_health,
    initial_feed_health,
    observe_clock_offset,
    observe_public_record,
    repair_sequence_gap,
)
from bot05.features import DriveDirection
from bot05.models import (
    BookLevel,
    BookSnapshot,
    DatasetProvenance,
    ExternalPriceState,
    MarketStatus,
)
from bot05.risk import RiskContext, RiskLimits, RiskSnapshot, RiskSupervisor
from bot05.shadow import (
    KillSwitch,
    RecoveryApproval,
    ShadowLedger,
    ShadowStatus,
    apply_shadow_decision,
    build_shadow_daily_report,
    evaluate_shadow_intent,
    latch_kill_switch,
    reconcile_shadow,
    reset_kill_switch,
    write_shadow_daily_report,
)
from bot05.strategy import ConfirmationKind, TargetKind, TradeIntent

T0 = datetime(2026, 8, 21, 13, 30, tzinfo=UTC)


def _policy() -> FeedHealthPolicy:
    return FeedHealthPolicy(1_000, 500, 100)


def _healthy_state():  # type: ignore[no-untyped-def]
    state = connect_feed(initial_feed_health("BTC"), T0)
    state = observe_clock_offset(state, offset_ms=10, sampled_at=T0)
    return observe_public_record(
        state,
        sequence=1,
        exchange_time=T0,
        received_at=T0 + timedelta(milliseconds=20),
    )


def test_feed_gap_is_fail_closed_until_exact_checksummed_repair() -> None:
    state = _healthy_state()
    state = observe_public_record(
        state,
        sequence=4,
        exchange_time=T0 + timedelta(milliseconds=30),
        received_at=T0 + timedelta(milliseconds=50),
    )
    unhealthy = evaluate_feed_health(
        state,
        _policy(),
        checked_at=T0 + timedelta(milliseconds=60),
        theoretical_position_open=True,
    )

    assert FeedHealthCode.SEQUENCE_GAP in unhealthy.codes
    assert unhealthy.kill_required
    with pytest.raises(ValueError, match="missing interval"):
        repair_sequence_gap(
            state,
            GapRepairEvidence(2, 2, "a" * 64, T0 + timedelta(milliseconds=70)),
        )

    repaired = repair_sequence_gap(
        state,
        GapRepairEvidence(2, 3, "a" * 64, T0 + timedelta(milliseconds=70)),
    )
    healthy = evaluate_feed_health(
        repaired,
        _policy(),
        checked_at=T0 + timedelta(milliseconds=80),
        theoretical_position_open=True,
    )
    assert healthy.healthy


def test_public_collector_spec_requires_all_public_evidence_channels() -> None:
    spec = PublicCollectorSpec(
        markets=("xyz:SILVER", "HYPE", "xyz:SP500", "xyz:GOLD", "BTC", "ETH", "SOL"),
        channels=tuple(PublicChannel),
        authentication=AuthenticationMode.NONE,
        health_policy=_policy(),
        reconnect_backoff_ms=1_000,
        max_reconnect_backoff_ms=30_000,
    )

    assert len(spec.spec_id) == 64
    with pytest.raises(ValueError, match="required public channel"):
        PublicCollectorSpec(
            markets=("BTC",),
            channels=(PublicChannel.TRADES, PublicChannel.BBO),
            authentication=AuthenticationMode.NONE,
            health_policy=_policy(),
            reconnect_backoff_ms=1_000,
            max_reconnect_backoff_ms=30_000,
        )


def test_disconnect_latches_kill_and_reconnect_does_not_auto_reset() -> None:
    disconnected = disconnect_feed(_healthy_state(), T0 + timedelta(milliseconds=100))
    unhealthy = evaluate_feed_health(
        disconnected,
        _policy(),
        checked_at=T0 + timedelta(milliseconds=200),
        theoretical_position_open=True,
    )
    reconciliation = reconcile_shadow(("intent-1",), ("intent-1",))
    switch = latch_kill_switch(
        KillSwitch(),
        feed_health=unhealthy,
        reconciliation=reconciliation,
        checked_at=T0 + timedelta(milliseconds=200),
    )

    assert switch.latched
    reconnected = connect_feed(disconnected, T0 + timedelta(milliseconds=300))
    reconnected = observe_public_record(
        reconnected,
        sequence=2,
        exchange_time=T0 + timedelta(milliseconds=310),
        received_at=T0 + timedelta(milliseconds=330),
    )
    healthy = evaluate_feed_health(
        reconnected,
        _policy(),
        checked_at=T0 + timedelta(milliseconds=350),
        theoretical_position_open=True,
    )
    assert healthy.healthy
    assert (
        latch_kill_switch(
            switch,
            feed_health=healthy,
            reconciliation=reconciliation,
            checked_at=T0 + timedelta(milliseconds=350),
        )
        == switch
    )
    assert switch.incident_id is not None
    reset = reset_kill_switch(
        switch,
        RecoveryApproval(
            switch.incident_id,
            "human-reviewer",
            "b" * 64,
            T0 + timedelta(seconds=1),
        ),
        feed_health=healthy,
        reconciliation=reconciliation,
    )
    assert not reset.latched


def _provenance() -> DatasetProvenance:
    return DatasetProvenance(
        dataset_id="shadow-fixture",
        evidence_tier="H2",
        source="bot05_collector",
        source_path_or_url="tests/unit/test_shadow.py",
        raw_sha256="1" * 64,
        manifest_sha256="2" * 64,
        adapter_version="fixture-v1",
        calendar_version="us_cash:test",
        code_version="shadow-test",
        config_sha256="3" * 64,
        source_timezone="UTC",
        period_start=T0,
        period_end=T0 + timedelta(hours=1),
    )


def _intent() -> TradeIntent:
    confirmation = T0 + timedelta(minutes=20)
    return TradeIntent(
        intent_id="intent-1",
        spec_id="spec-1",
        market="BTC",
        session_id="us_cash_open",
        t0=T0,
        decided_at=confirmation + timedelta(seconds=1),
        direction=DriveDirection.LONG,
        confirmation=ConfirmationKind.BREAKOUT,
        touch_time=confirmation,
        confirmation_time=confirmation,
        same_candle_touch_confirmation=True,
        entry_source="shadow_next_price",
        entry_price=Decimal("100"),
        stop_price=Decimal("90"),
        target_kind=TargetKind.FIXED_2R,
        target_price=Decimal("120"),
        target_label="fixed_2r",
        midpoint=Decimal("95"),
        source_data_sha256="4" * 64,
        config_sha256="3" * 64,
        calendar_version="us_cash:test",
        strategy_code_version="shadow-test",
    )


def _book() -> BookSnapshot:
    decided_at = _intent().decided_at
    return BookSnapshot(
        market="BTC",
        exchange_time=decided_at + timedelta(milliseconds=10),
        received_at=decided_at + timedelta(milliseconds=20),
        bids=(BookLevel(Decimal("99.9"), Decimal("2")),),
        asks=(BookLevel(Decimal("100.1"), Decimal("2")),),
        provenance=_provenance(),
    )


def _context() -> RiskContext:
    observed = _intent().decided_at
    return RiskContext(
        market="BTC",
        trading_day=date(2026, 8, 21),
        observed_at=observed,
        data_timestamp=observed - timedelta(milliseconds=10),
        market_status=MarketStatus.ACTIVE,
        external_price_state=ExternalPriceState.UNAVAILABLE,
        external_price_required=False,
        opening_drive_complete=True,
        clock_synchronized=True,
        session_unambiguous=True,
        market_definition_validated=True,
        feed_healthy=True,
        spread_bps=Decimal("2"),
        expected_slippage_bps=Decimal("1"),
        mark_price=Decimal("100"),
        oracle_price=Decimal("100"),
        equity=Decimal("10000"),
        requested_size=Decimal("0.1"),
        leverage=Decimal(1),
        expected_win_cost_bps=Decimal("50"),
        expected_loss_cost_bps=Decimal("50"),
        orphan_order=False,
        unknown_fill=False,
        position_divergence=False,
        snapshot=RiskSnapshot(trading_day=date(2026, 8, 21)),
    )


def _supervisor() -> RiskSupervisor:
    return RiskSupervisor(
        RiskLimits(
            max_staleness_ms=1_000,
            max_spread_bps=Decimal(5),
            max_slippage_bps=Decimal(5),
            max_oracle_mark_divergence_bps=Decimal(25),
            max_risk_fraction=Decimal("0.0025"),
            max_leverage=Decimal(3),
            max_daily_trades=2,
            max_daily_loss_r=Decimal(1),
            min_net_reward_risk=Decimal("1.5"),
        )
    )


def test_shadow_observes_public_executable_quote_and_is_exact_once() -> None:
    state = observe_public_record(
        _healthy_state(),
        sequence=2,
        exchange_time=_intent().decided_at - timedelta(milliseconds=20),
        received_at=_intent().decided_at,
    )
    health = evaluate_feed_health(
        state,
        _policy(),
        checked_at=_intent().decided_at + timedelta(milliseconds=1),
        theoretical_position_open=False,
    )
    decision = evaluate_shadow_intent(
        _intent(),
        _context(),
        _supervisor(),
        health,
        _book(),
        requested_quantity=Decimal("0.1"),
        size_step=Decimal("0.01"),
        kill_switch=KillSwitch(),
    )

    assert decision.status is ShadowStatus.OBSERVED
    assert decision.quote is not None
    assert decision.quote.executable_price == Decimal("100.1")
    opened = apply_shadow_decision(ShadowLedger(), decision)
    assert apply_shadow_decision(opened, decision) == opened
    assert opened.theoretical_open_market == "BTC"


def test_shadow_daily_report_is_bit_exact_and_contains_no_action_path(
    tmp_path: Path,
) -> None:
    state = observe_public_record(
        _healthy_state(),
        sequence=2,
        exchange_time=_intent().decided_at - timedelta(milliseconds=20),
        received_at=_intent().decided_at,
    )
    health = evaluate_feed_health(
        state,
        _policy(),
        checked_at=_intent().decided_at + timedelta(milliseconds=1),
        theoretical_position_open=False,
    )
    decision = evaluate_shadow_intent(
        _intent(),
        _context(),
        _supervisor(),
        health,
        _book(),
        requested_quantity=Decimal("0.1"),
        size_step=Decimal("0.01"),
        kill_switch=KillSwitch(),
    )
    collector_spec = PublicCollectorSpec(
        markets=("BTC",),
        channels=tuple(PublicChannel),
        authentication=AuthenticationMode.NONE,
        health_policy=_policy(),
        reconnect_backoff_ms=1_000,
        max_reconnect_backoff_ms=30_000,
    )
    kwargs = {
        "trading_day": date(2026, 8, 21),
        "generated_at": T0 + timedelta(days=1),
        "collector_spec_id": collector_spec.spec_id,
        "config_sha256": "5" * 64,
        "code_sha256": "6" * 64,
        "decisions": (decision,),
        "health_checks": (health,),
        "reconciliation": reconcile_shadow(("intent-1",), ("intent-1",)),
        "kill_switch": KillSwitch(),
    }
    first = build_shadow_daily_report(**kwargs)
    second = build_shadow_daily_report(**kwargs)

    assert first.json_bytes() == second.json_bytes()
    assert first.metrics.observed_count == 1
    output, sidecar = write_shadow_daily_report(first, tmp_path / "shadow.json")
    assert output.is_file() and first.report_id in output.read_text()
    assert sidecar.is_file()
