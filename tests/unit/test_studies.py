from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from bot05.data.contracts import EvidenceTier, Qualification
from bot05.features.opening_drive import DriveDirection
from bot05.models import Candle, DatasetProvenance
from bot05.replay import (
    ExitReason,
    LiquidityRole,
    OrderSide,
    ReplayModel,
    ReplayResult,
    ReplayStatus,
    SimulatedFill,
)
from bot05.strategy import ConfirmationKind, TargetKind, TradeIntent
from bot05.studies import (
    MFE_HORIZONS_MINUTES,
    ExperimentSpec,
    MarketStudyScope,
    SessionObservation,
    StudyConclusion,
    StudyContractError,
    StudyDataset,
    StudyPurpose,
    allowed_purposes,
    build_market_study_report,
    calculate_excursions,
    calculate_performance,
)

T0 = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
ENTRY = T0 + timedelta(minutes=16)


def _spec() -> ExperimentSpec:
    return ExperimentSpec(
        name="d5-fixture",
        preregistered_at=T0 - timedelta(days=1),
        universe=(
            "xyz:GOLD",
            "xyz:SILVER",
            "xyz:SP500",
            "HYPE",
            "BTC",
            "ETH",
            "SOL",
        ),
        sessions=("us_cash_open",),
        strategy_spec_ids=("1" * 64,),
        replay_models=tuple(ReplayModel),
        excursion_horizons_minutes=MFE_HORIZONS_MINUTES,
        selector_enabled=False,
        calendar_versions=("us_cash_2026:test",),
        config_sha256="2" * 64,
        code_version="d5-test",
    )


def _dataset(
    tier: EvidenceTier = EvidenceTier.HYPERLIQUID_ARCHIVE,
    *,
    market: str = "BTC",
    instrument: str = "BTC",
) -> StudyDataset:
    return StudyDataset(
        dataset_id=f"fixture-{tier.value}-{market}",
        canonical_market=market,
        source_instrument=instrument,
        tier=tier,
        qualification=Qualification.QUALIFIED,
        channels=("trades", "bbo"),
        period_start=T0,
        period_end=T0 + timedelta(days=1),
        record_count=100,
        critical_gap_count=0,
        raw_sha256="3" * 64,
        manifest_sha256="4" * 64,
        derived_sha256="5" * 64,
        adapter_version="fixture-v1",
    )


def _session(*, trade: bool = True) -> SessionObservation:
    return SessionObservation(
        session_key="BTC:2026-01-05:us_cash_open",
        canonical_market="BTC",
        session_id="us_cash_open",
        t0=T0,
        complete=True,
        session_eligible=True,
        drive=True,
        pullback=True,
        confirmation=True,
        economic_gate=trade,
        trade=trade,
        direction=DriveDirection.LONG,
        intent_id="intent-1" if trade else None,
        rejection_reason=None if trade else "economic_gate_failed",
        source_data_sha256="6" * 64,
    )


def _fill(
    timestamp: datetime,
    side: OrderSide,
    price: str,
    fee: str,
) -> SimulatedFill:
    return SimulatedFill(
        timestamp=timestamp,
        side=side,
        role=LiquidityRole.TAKER,
        price=Decimal(price),
        quantity=Decimal(1),
        fee_rate=Decimal("0.0005"),
        fee=Decimal(fee),
        latency_ms=100,
        slippage_bps=Decimal(0),
        book_levels_consumed=1,
    )


def _result(
    *,
    run_digit: str = "7",
    intent_id: str = "intent-1",
    net_pnl: str = "9.9",
    pnl_r: str = "1.98",
    reason: ExitReason = ExitReason.TARGET,
    offset_days: int = 0,
) -> ReplayResult:
    entry_time = ENTRY + timedelta(days=offset_days)
    gross = Decimal(net_pnl) + Decimal("0.1")
    return ReplayResult(
        run_id=run_digit * 64,
        intent_id=intent_id,
        market="BTC",
        session_id="us_cash_open",
        direction=DriveDirection.LONG,
        model=ReplayModel.TRADE_BBO_CENTRAL,
        status=ReplayStatus.CLOSED,
        config_sha256="8" * 64,
        fee_schedule_sha256="9" * 64,
        signal_data_sha256="a" * 64,
        replay_data_sha256="b" * 64,
        requested_quantity=Decimal(1),
        filled_quantity=Decimal(1),
        entry=_fill(entry_time, OrderSide.BUY, "100", "0.05"),
        exit=_fill(
            entry_time + timedelta(minutes=10),
            OrderSide.SELL,
            "110" if Decimal(net_pnl) > 0 else "95",
            "0.05",
        ),
        exit_reason=reason,
        failure_code=None,
        same_bar_collision=False,
        target_rested=reason is ExitReason.TARGET,
        target_trade_through=reason is ExitReason.TARGET,
        gross_pnl=gross,
        funding_pnl=Decimal(0),
        net_pnl=Decimal(net_pnl),
        pnl_r=Decimal(pnl_r),
    )


def _intent(direction: DriveDirection = DriveDirection.LONG) -> TradeIntent:
    long = direction is DriveDirection.LONG
    return TradeIntent(
        intent_id="intent-1",
        spec_id="1" * 64,
        market="BTC",
        session_id="us_cash_open",
        t0=T0,
        decided_at=ENTRY,
        direction=direction,
        confirmation=ConfirmationKind.BREAKOUT,
        touch_time=T0 + timedelta(minutes=15),
        confirmation_time=ENTRY,
        same_candle_touch_confirmation=True,
        entry_source="screening_next_open",
        entry_price=Decimal(100),
        stop_price=Decimal(95 if long else 105),
        target_kind=TargetKind.FIXED_2R,
        target_price=Decimal(110 if long else 90),
        target_label="fixed_2r",
        midpoint=Decimal(100),
        source_data_sha256="6" * 64,
        config_sha256="2" * 64,
        calendar_version="us_cash_2026:test",
        strategy_code_version="d5-test",
    )


def _provenance(market: str) -> DatasetProvenance:
    return DatasetProvenance(
        dataset_id=f"excursion-{market}",
        evidence_tier="H1",
        source="synthetic",
        source_path_or_url="tests/unit/test_studies.py",
        raw_sha256="c" * 64,
        manifest_sha256="d" * 64,
        adapter_version="fixture-v1",
        calendar_version="test",
        code_version="test",
        config_sha256="e" * 64,
        source_timezone="UTC",
        period_start=T0,
        period_end=T0 + timedelta(hours=4),
    )


def _excursion_candles(
    *,
    high: str = "112",
    low: str = "98",
) -> tuple[Candle, ...]:
    provenance = _provenance("BTC")
    return tuple(
        Candle(
            market="BTC",
            interval_seconds=60,
            open_time=ENTRY + timedelta(minutes=index),
            close_time=ENTRY + timedelta(minutes=index + 1),
            closed_at=ENTRY + timedelta(minutes=index + 1),
            open=Decimal(100),
            high=Decimal(high),
            low=Decimal(low),
            close=Decimal(100),
            volume=Decimal(1),
            trade_count=1,
            provenance=provenance,
        )
        for index in range(120)
    )


def test_experiment_is_content_addressed_and_selector_stays_disabled() -> None:
    assert _spec().spec_sha256 == _spec().spec_sha256
    with pytest.raises(StudyContractError, match="selector"):
        replace(_spec(), selector_enabled=True)
    with pytest.raises(StudyContractError, match="four replay models"):
        replace(_spec(), replay_models=(ReplayModel.OHLC_CONSERVATIVE,))


def test_session_funnel_cannot_skip_a_causal_stage() -> None:
    with pytest.raises(StudyContractError, match="monotonic"):
        replace(_session(), pullback=False, confirmation=True)


def test_excursions_use_fixed_horizons_and_expose_incomplete_coverage() -> None:
    candles = _excursion_candles()
    complete = calculate_excursions(_intent(), candles)

    assert all(item.complete for item in complete.horizons)
    assert complete.horizons[0].mfe_bps == Decimal(1200)
    assert complete.horizons[0].mae_bps == Decimal(200)
    assert complete.horizons[0].mfe_r == Decimal("2.4")
    assert complete.horizons[0].mae_r == Decimal("0.4")

    missing = calculate_excursions(_intent(), candles[:20] + candles[21:])
    assert missing.horizons[0].complete
    assert not any(item.complete for item in missing.horizons[1:])

    short = calculate_excursions(
        _intent(DriveDirection.SHORT),
        _excursion_candles(high="102", low="88"),
    )
    assert short.horizons[0].mfe_bps == Decimal(1200)
    assert short.horizons[0].mae_bps == Decimal(200)


def test_performance_reports_distribution_drawdown_and_costs() -> None:
    results = (
        _result(run_digit="1", net_pnl="10", pnl_r="2", offset_days=0),
        _result(
            run_digit="2",
            net_pnl="-5",
            pnl_r="-1",
            reason=ExitReason.STOP,
            offset_days=1,
        ),
        _result(run_digit="3", net_pnl="2", pnl_r="0.4", offset_days=2),
    )

    metrics = calculate_performance(results)

    assert metrics.win_count == 2
    assert metrics.loss_count == 1
    assert metrics.profit_factor == Decimal("2.4")
    assert metrics.max_drawdown_usd == Decimal(5)
    assert metrics.max_drawdown_r == Decimal(1)
    assert metrics.known_fees == Decimal("0.30")
    assert metrics.stop_exits == 1
    assert metrics.target_exits == 2


def test_market_report_keeps_one_tier_and_blocks_h0_execution_pnl(tmp_path) -> None:
    scope = MarketStudyScope(
        canonical_market="BTC",
        source_instrument="BTC",
        session_id="us_cash_open",
        tier=EvidenceTier.HYPERLIQUID_ARCHIVE,
        replay_model=ReplayModel.TRADE_BBO_CENTRAL,
    )
    excursion = calculate_excursions(_intent(), _excursion_candles())
    report = build_market_study_report(
        _spec(),
        scope,
        (_dataset(),),
        (_session(),),
        (excursion,),
        (_result(),),
        generated_at=T0 + timedelta(days=2),
    )

    assert report.evidence.conclusion is StudyConclusion.DATA_INSUFFICIENT
    assert report.evidence.promotion_permitted is False
    assert report.metrics.funnel.trades == 1
    assert report.metrics.performance.closed_count == 1
    assert (
        report.json_bytes()
        == build_market_study_report(
            _spec(),
            scope,
            (_dataset(),),
            (_session(),),
            (excursion,),
            (_result(),),
            generated_at=T0 + timedelta(days=2),
        ).json_bytes()
    )

    h0_scope = replace(scope, tier=EvidenceTier.HYPERLIQUID_CANDLES)
    h0_dataset = _dataset(EvidenceTier.HYPERLIQUID_CANDLES)
    descriptive = build_market_study_report(
        _spec(),
        h0_scope,
        (h0_dataset,),
        (_session(),),
        (excursion,),
        (),
        generated_at=T0 + timedelta(days=2),
    )
    assert descriptive.evidence.conclusion is StudyConclusion.DESCRIPTIVE_ONLY
    assert descriptive.metrics.performance.replay_count == 0
    with pytest.raises(StudyContractError, match="cannot publish execution PnL"):
        build_market_study_report(
            _spec(),
            h0_scope,
            (h0_dataset,),
            (_session(),),
            (excursion,),
            (_result(),),
            generated_at=T0 + timedelta(days=2),
        )
    with pytest.raises(StudyContractError, match="one market and evidence tier"):
        build_market_study_report(
            _spec(),
            scope,
            (_dataset(), _dataset(EvidenceTier.BOT05_COLLECTOR)),
            (_session(),),
            (excursion,),
            (_result(),),
            generated_at=T0 + timedelta(days=2),
        )
    with pytest.raises(StudyContractError, match="qualified without critical gaps"):
        build_market_study_report(
            _spec(),
            scope,
            (replace(_dataset(), critical_gap_count=1),),
            (_session(),),
            (excursion,),
            (_result(),),
            generated_at=T0 + timedelta(days=2),
        )


def test_evidence_permissions_never_treat_legacy_as_execution_proof() -> None:
    assert allowed_purposes(EvidenceTier.UNDERLYING) == (StudyPurpose.ALPHA_STRUCTURE,)
    assert StudyPurpose.EXECUTION_REPLAY in allowed_purposes(
        EvidenceTier.HYPERLIQUID_ARCHIVE
    )
    assert StudyPurpose.CAUSAL_EXECUTION in allowed_purposes(
        EvidenceTier.BOT05_COLLECTOR
    )
    assert StudyPurpose.EXECUTION_REPLAY not in allowed_purposes(EvidenceTier.LEGACY)
