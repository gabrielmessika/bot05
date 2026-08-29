from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from bot05.data.contracts import EvidenceTier, Qualification, TimeRange
from bot05.features import (
    DriveFilter,
    DriveThreshold,
    build_opening_drive,
)
from bot05.features.candles import aggregate_trades
from bot05.models import (
    AggressorSide,
    DatasetProvenance,
    ExternalPriceState,
    MarketStatus,
    Trade,
)
from bot05.replay import (
    FeeSchedule,
    FeeSnapshot,
    ReplayConfig,
    ReplayModel,
    ReplayRequest,
    run_replay,
)
from bot05.risk import RiskContext, RiskLimits, RiskSnapshot, RiskSupervisor
from bot05.strategy import (
    ConfirmationKind,
    EntryPriceObservation,
    StrategySpec,
    TargetKind,
    advance_candle,
    initialize_strategy,
    observe_entry_price,
    register_opening_drive,
)
from bot05.studies import (
    MFE_HORIZONS_MINUTES,
    ExperimentSpec,
    MarketStudyScope,
    SessionObservation,
    StudyConclusion,
    StudyDataset,
    build_market_study_report,
    calculate_excursions,
    write_market_study_report,
)

T0 = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)


def _ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _provenance() -> DatasetProvenance:
    return DatasetProvenance(
        dataset_id="d5-pipeline-h1",
        evidence_tier="H1",
        source="synthetic",
        source_path_or_url="tests/integration/test_study_pipeline.py",
        raw_sha256="1" * 64,
        manifest_sha256="2" * 64,
        adapter_version="fixture-v1",
        calendar_version="us_cash_2026:test",
        code_version="d5-test",
        config_sha256="3" * 64,
        source_timezone="UTC",
        period_start=T0,
        period_end=T0 + timedelta(minutes=145),
    )


def _bar_prices(index: int) -> tuple[str, str, str, str]:
    fixed = {
        0: ("100", "105", "99", "104"),
        1: ("104", "108", "103", "107"),
        2: ("107", "110", "106", "109"),
        3: ("108", "109", "105", "106"),
        4: ("106", "110", "104", "109.5"),
        5: ("109", "130", "108", "120"),
    }
    return fixed.get(index, ("120", "122", "118", "120"))


def _trades() -> tuple[Trade, ...]:
    provenance = _provenance()
    trades: list[Trade] = []
    for index in range(29):
        prices = _bar_prices(index)
        bucket = T0 + timedelta(minutes=5 * index)
        for position, (seconds, price) in enumerate(
            zip((1, 60, 120, 240), prices, strict=True)
        ):
            timestamp = bucket + timedelta(seconds=seconds)
            trades.append(
                Trade(
                    market="BTC",
                    trade_id=f"trade-{index:02d}-{position}",
                    exchange_time=timestamp,
                    received_at=timestamp + timedelta(milliseconds=10),
                    aggressor_side=(
                        AggressorSide.BUY if position % 2 == 0 else AggressorSide.SELL
                    ),
                    price=Decimal(price),
                    size=Decimal(1),
                    provenance=provenance,
                )
            )
    return tuple(trades)


def _risk_context(intent_time: datetime) -> RiskContext:
    return RiskContext(
        market="BTC",
        trading_day=date(2026, 1, 5),
        observed_at=intent_time,
        data_timestamp=intent_time,
        market_status=MarketStatus.ACTIVE,
        external_price_state=ExternalPriceState.UNAVAILABLE,
        external_price_required=False,
        opening_drive_complete=True,
        clock_synchronized=True,
        session_unambiguous=True,
        market_definition_validated=True,
        feed_healthy=True,
        spread_bps=Decimal(1),
        expected_slippage_bps=Decimal(1),
        mark_price=Decimal(109),
        oracle_price=Decimal(109),
        equity=Decimal("100000"),
        requested_size=Decimal(1),
        leverage=Decimal(1),
        expected_win_cost_bps=Decimal(5),
        expected_loss_cost_bps=Decimal(5),
        orphan_order=False,
        unknown_fill=False,
        position_divergence=False,
        snapshot=RiskSnapshot(trading_day=date(2026, 1, 5)),
    )


def _risk_limits() -> RiskLimits:
    return RiskLimits(
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


def _fees() -> FeeSchedule:
    return FeeSchedule(
        (
            FeeSnapshot(
                market="BTC",
                effective_at=T0,
                account_tier="tier_0",
                base_maker_rate=Decimal("0.0002"),
                base_taker_rate=Decimal("0.0005"),
                growth_mode=False,
                deployer_fee_scale=Decimal(1),
                staking_discount_rate=Decimal(0),
                referral_discount_rate=Decimal(0),
                builder_fee_rate=Decimal(0),
                effective_maker_rate=Decimal("0.0002"),
                effective_taker_rate=Decimal("0.0005"),
                source_sha256="4" * 64,
            ),
        )
    )


def test_dataset_to_signal_risk_replay_and_study_report_is_bit_exact(tmp_path) -> None:
    trades = _trades()
    coverage = TimeRange(_ms(T0), _ms(T0 + timedelta(minutes=145)))
    built = aggregate_trades(
        trades,
        market="BTC",
        interval_seconds=300,
        requested=coverage,
        qualified_coverage=(coverage,),
        provenance=_provenance(),
    )
    assert built.complete
    assert len(built.candles) == 29

    drive_result = build_opening_drive(
        built.candles[:3],
        market="BTC",
        session_id="us_cash_open",
        t0=T0,
        observed_at=T0 + timedelta(minutes=15),
    )
    assert drive_result.drive is not None
    strategy_spec = StrategySpec(
        market="BTC",
        session_id="us_cash_open",
        drive_filter=DriveFilter.NONE,
        confirmation=ConfirmationKind.BREAKOUT,
        target=TargetKind.FIXED_2R,
        config_sha256="3" * 64,
        calendar_version="us_cash_2026:test",
        code_version="d5-test",
    )
    snapshot = initialize_strategy(
        strategy_spec,
        t0=T0,
        source_data_sha256="5" * 64,
    )
    snapshot = register_opening_drive(
        snapshot,
        drive_result.drive,
        DriveThreshold(
            market="BTC",
            session_id="us_cash_open",
            filter=DriveFilter.NONE,
            as_of=T0,
            sample_count=20,
            value=Decimal(0),
            eligible=True,
        ),
    )
    snapshot = advance_candle(
        snapshot, built.candles[3], observed_at=built.candles[3].closed_at
    )
    snapshot = advance_candle(
        snapshot, built.candles[4], observed_at=built.candles[4].closed_at
    )
    entry_candle = built.candles[5]
    snapshot = observe_entry_price(
        snapshot,
        EntryPriceObservation(
            market="BTC",
            observed_at=entry_candle.open_time + timedelta(milliseconds=1),
            price=entry_candle.open,
            source="coarse_execution",
        ),
    )
    assert snapshot.intent is not None
    intent = snapshot.intent

    risk_context = _risk_context(intent.decided_at)
    risk_decision = RiskSupervisor(_risk_limits()).review(intent, risk_context)
    assert risk_decision.accepted
    replay_request = ReplayRequest(
        intent,
        risk_decision,
        ReplayConfig(
            model=ReplayModel.OHLC_CONSERVATIVE,
            requested_quantity=Decimal(1),
            price_tick=Decimal("0.1"),
            size_step=Decimal("0.1"),
            entry_latency_ms=0,
            stop_latency_ms=0,
            target_ack_latency_ms=0,
            latency_multiplier=Decimal(1),
            max_staleness_ms=1_000,
            max_position_seconds=7_200,
            slippage_bps=Decimal(5),
            fee_multiplier=Decimal(1),
            code_version="d5-test",
        ),
    )
    replay_result = run_replay(
        replay_request,
        _fees(),
        candles=built.candles[5:],
    )
    assert replay_result.entry is not None and replay_result.exit is not None
    assert replay_result.net_pnl is not None and replay_result.net_pnl > 0

    excursion = calculate_excursions(intent, built.candles[5:])
    experiment = ExperimentSpec(
        name="d5-causal-pipeline-fixture",
        preregistered_at=T0 - timedelta(days=1),
        universe=("BTC",),
        sessions=("us_cash_open",),
        strategy_spec_ids=(strategy_spec.spec_id,),
        replay_models=tuple(ReplayModel),
        excursion_horizons_minutes=MFE_HORIZONS_MINUTES,
        selector_enabled=False,
        calendar_versions=(strategy_spec.calendar_version,),
        config_sha256="3" * 64,
        code_version="d5-test",
    )
    study_dataset = StudyDataset(
        dataset_id="d5-pipeline-h1",
        canonical_market="BTC",
        source_instrument="BTC",
        tier=EvidenceTier.HYPERLIQUID_ARCHIVE,
        qualification=Qualification.QUALIFIED,
        channels=("trades", "candles_5m"),
        period_start=T0,
        period_end=T0 + timedelta(minutes=145),
        record_count=len(trades),
        critical_gap_count=0,
        raw_sha256=_provenance().raw_sha256,
        manifest_sha256=_provenance().manifest_sha256,
        derived_sha256=excursion.replay_data_sha256,
        adapter_version="fixture-v1",
        transformations=("aggregate_trades_5m",),
    )
    session = SessionObservation(
        session_key="BTC:2026-01-05:us_cash_open",
        canonical_market="BTC",
        session_id="us_cash_open",
        t0=T0,
        complete=True,
        session_eligible=True,
        drive=True,
        pullback=True,
        confirmation=True,
        economic_gate=True,
        trade=True,
        direction=intent.direction,
        intent_id=intent.intent_id,
        rejection_reason=None,
        source_data_sha256=intent.source_data_sha256,
    )
    scope = MarketStudyScope(
        canonical_market="BTC",
        source_instrument="BTC",
        session_id="us_cash_open",
        tier=EvidenceTier.HYPERLIQUID_ARCHIVE,
        replay_model=ReplayModel.OHLC_CONSERVATIVE,
    )
    generated_at = T0 + timedelta(days=1)
    first = build_market_study_report(
        experiment,
        scope,
        (study_dataset,),
        (session,),
        (excursion,),
        (replay_result,),
        generated_at=generated_at,
    )
    second = build_market_study_report(
        experiment,
        scope,
        (study_dataset,),
        (session,),
        (excursion,),
        (replay_result,),
        generated_at=generated_at,
    )

    assert first.json_bytes() == second.json_bytes()
    assert first.evidence.conclusion is StudyConclusion.DATA_INSUFFICIENT
    assert first.metrics.funnel.trades == 1
    assert first.metrics.performance.known_fees > 0
    paths = write_market_study_report(first, tmp_path)
    assert hashlib.sha256(paths[0].read_bytes()).hexdigest() in paths[1].read_text(
        encoding="utf-8"
    )
