from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from bot05.features.opening_drive import DriveDirection
from bot05.models import (
    AggressorSide,
    BookLevel,
    BookSnapshot,
    Candle,
    DatasetProvenance,
    Trade,
)
from bot05.replay import (
    ExitReason,
    FailureCode,
    FeeSchedule,
    FeeSnapshot,
    FundingEvent,
    LiquidityRole,
    OrderSide,
    ReplayConfig,
    ReplayContractError,
    ReplayModel,
    ReplayRequest,
    ReplayStatus,
    run_replay,
)
from bot05.replay.costs import round_price, round_quantity, sweep_book
from bot05.risk import RiskDecision
from bot05.strategy import ConfirmationKind, TargetKind, TradeIntent

T0 = datetime(2026, 8, 21, 10, 0, tzinfo=UTC)
ENTRY_TIME = T0 + timedelta(minutes=16)


def _provenance() -> DatasetProvenance:
    return DatasetProvenance(
        dataset_id="d4-fixture",
        evidence_tier="H1",
        source="synthetic",
        source_path_or_url="tests/unit/test_replay.py",
        raw_sha256="a" * 64,
        manifest_sha256="b" * 64,
        adapter_version="fixture-v1",
        calendar_version="fixture-v1",
        code_version="test",
        config_sha256="c" * 64,
        source_timezone="UTC",
        period_start=T0,
        period_end=T0 + timedelta(hours=4),
    )


def _intent() -> TradeIntent:
    return TradeIntent(
        intent_id="intent-d4-long",
        spec_id="spec-d4",
        market="BTC",
        session_id="fixture",
        t0=T0,
        decided_at=ENTRY_TIME,
        direction=DriveDirection.LONG,
        confirmation=ConfirmationKind.BREAKOUT,
        touch_time=T0 + timedelta(minutes=15),
        confirmation_time=ENTRY_TIME,
        same_candle_touch_confirmation=True,
        entry_source="screening_next_open",
        entry_price=Decimal("100"),
        stop_price=Decimal("95"),
        target_kind=TargetKind.FIXED_2R,
        target_price=Decimal("110"),
        target_label="fixed_2r",
        midpoint=Decimal("100"),
        source_data_sha256="d" * 64,
        config_sha256="e" * 64,
        calendar_version="fixture-v1",
        strategy_code_version="test",
    )


def _risk() -> RiskDecision:
    return RiskDecision(
        intent_id=_intent().intent_id,
        limits_sha256="f" * 64,
        checked_at=ENTRY_TIME,
        accepted=True,
        refusal_codes=(),
        gross_reward_bps=Decimal("1000"),
        gross_stop_bps=Decimal("500"),
        net_reward_risk=Decimal("1.9"),
        position_risk_fraction=Decimal("0.001"),
    )


def _config(model: ReplayModel) -> ReplayConfig:
    stress = model is ReplayModel.TRADE_BBO_STRESS
    return ReplayConfig(
        model=model,
        requested_quantity=Decimal("1.05"),
        price_tick=Decimal("0.1"),
        size_step=Decimal("0.1"),
        entry_latency_ms=100,
        stop_latency_ms=100,
        target_ack_latency_ms=100,
        latency_multiplier=Decimal(2) if stress else Decimal(1),
        max_staleness_ms=1_000,
        max_position_seconds=120,
        slippage_bps=(
            Decimal(10)
            if stress
            else Decimal(5)
            if model is ReplayModel.OHLC_CONSERVATIVE
            else Decimal(0)
        ),
        fee_multiplier=Decimal("1.5") if stress else Decimal(1),
        code_version="d4-test",
    )


def _request(model: ReplayModel) -> ReplayRequest:
    return ReplayRequest(_intent(), _risk(), _config(model))


def _fee(
    effective_at: datetime = T0,
    *,
    maker: str = "0.0002",
    taker: str = "0.0005",
    source: str = "1",
) -> FeeSnapshot:
    return FeeSnapshot(
        market="BTC",
        effective_at=effective_at,
        account_tier="tier_0",
        base_maker_rate=Decimal(maker),
        base_taker_rate=Decimal(taker),
        growth_mode=False,
        deployer_fee_scale=Decimal(1),
        staking_discount_rate=Decimal(0),
        referral_discount_rate=Decimal(0),
        builder_fee_rate=Decimal(0),
        effective_maker_rate=Decimal(maker),
        effective_taker_rate=Decimal(taker),
        source_sha256=source * 64,
    )


def _fees(*snapshots: FeeSnapshot) -> FeeSchedule:
    return FeeSchedule(snapshots or (_fee(),))


def _candle(
    offset: int,
    *,
    open: str,
    high: str,
    low: str,
    close: str,
    closed_delay_ms: int = 0,
) -> Candle:
    open_time = ENTRY_TIME + timedelta(minutes=offset)
    close_time = open_time + timedelta(minutes=1)
    return Candle(
        market="BTC",
        interval_seconds=60,
        open_time=open_time,
        close_time=close_time,
        closed_at=close_time + timedelta(milliseconds=closed_delay_ms),
        open=Decimal(open),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal("10"),
        trade_count=10,
        provenance=_provenance(),
    )


def _book(
    milliseconds: int,
    *,
    bids: tuple[tuple[str, str], ...] = (("99.9", "2"),),
    asks: tuple[tuple[str, str], ...] = (("100.1", "2"),),
) -> BookSnapshot:
    received_at = ENTRY_TIME + timedelta(milliseconds=milliseconds)
    return BookSnapshot(
        market="BTC",
        exchange_time=received_at - timedelta(milliseconds=10),
        received_at=received_at,
        bids=tuple(BookLevel(Decimal(price), Decimal(size)) for price, size in bids),
        asks=tuple(BookLevel(Decimal(price), Decimal(size)) for price, size in asks),
        provenance=_provenance(),
    )


def _trade(
    milliseconds: int,
    price: str,
    side: AggressorSide,
    trade_id: str,
) -> Trade:
    received_at = ENTRY_TIME + timedelta(milliseconds=milliseconds)
    return Trade(
        market="BTC",
        trade_id=trade_id,
        exchange_time=received_at - timedelta(milliseconds=10),
        received_at=received_at,
        aggressor_side=side,
        price=Decimal(price),
        size=Decimal(1),
        provenance=_provenance(),
    )


def test_ohlc_collision_is_stop_first_and_optimistic_is_only_a_ceiling() -> None:
    candle = _candle(0, open="100", high="111", low="94", close="100")

    conservative = run_replay(
        _request(ReplayModel.OHLC_CONSERVATIVE), _fees(), candles=(candle,)
    )
    optimistic = run_replay(
        _request(ReplayModel.OHLC_OPTIMISTIC), _fees(), candles=(candle,)
    )

    assert conservative.exit_reason is ExitReason.STOP
    assert conservative.exit is not None
    assert conservative.exit.price == Decimal("94.9")
    assert conservative.exit.role is LiquidityRole.TAKER
    assert conservative.same_bar_collision
    assert optimistic.exit_reason is ExitReason.TARGET
    assert optimistic.exit is not None
    assert optimistic.exit.price == Decimal("110")
    assert optimistic.same_bar_collision


def test_ohlc_stop_gap_uses_open_and_missing_bar_fails_closed() -> None:
    entry = _candle(0, open="100", high="102", low="99", close="101")
    gap = _candle(1, open="94", high="96", low="93", close="95")
    result = run_replay(
        _request(ReplayModel.OHLC_CONSERVATIVE),
        _fees(),
        candles=(entry, gap),
    )

    assert result.exit_reason is ExitReason.STOP
    assert result.exit is not None
    assert result.exit.timestamp == gap.open_time
    assert result.exit.price == Decimal("93.9")

    missing = _candle(2, open="94", high="96", low="93", close="95")
    failed = run_replay(
        _request(ReplayModel.OHLC_CONSERVATIVE),
        _fees(),
        candles=(entry, missing),
    )
    assert failed.status is ReplayStatus.FAILED_CLOSED
    assert failed.failure_code is FailureCode.FEED_LOSS
    assert failed.net_pnl is None


def test_funding_boundary_and_taker_fees_are_in_net_pnl() -> None:
    candle = _candle(0, open="100", high="111", low="99", close="110")
    funding = FundingEvent(
        market="BTC",
        effective_at=candle.close_time,
        rate=Decimal("0.001"),
        mark_price=Decimal("100"),
        source_sha256="9" * 64,
    )

    result = run_replay(
        _request(ReplayModel.OHLC_CONSERVATIVE),
        _fees(),
        candles=(candle,),
        funding_events=(funding,),
    )

    assert result.funding_pnl == Decimal("-0.100")
    assert result.entry is not None and result.exit is not None
    assert result.net_pnl == (
        result.gross_pnl - result.entry.fee - result.exit.fee - Decimal("0.100")
    )


def test_rounding_and_book_impact_are_adverse_and_full_fill_only() -> None:
    assert round_quantity(Decimal("1.09"), Decimal("0.1")) == Decimal("1.0")
    assert round_price(Decimal("100.01"), Decimal("0.1"), OrderSide.BUY) == Decimal(
        "100.1"
    )
    assert round_price(Decimal("100.09"), Decimal("0.1"), OrderSide.SELL) == Decimal(
        "100.0"
    )

    levels = (
        BookLevel(Decimal("100.1"), Decimal("0.5")),
        BookLevel(Decimal("100.3"), Decimal("0.5")),
    )
    impact = sweep_book(levels, Decimal(1))
    assert impact is not None
    assert impact.average_price == Decimal("100.2")
    assert impact.levels_consumed == 2
    assert sweep_book(levels, Decimal("1.1")) is None


def test_event_entry_sweeps_depth_and_target_requires_trade_through_after_ack() -> None:
    entry = _book(
        100,
        asks=(("100.1", "0.5"), ("100.2", "1")),
    )
    touch = _trade(250, "110", AggressorSide.BUY, "touch")
    through = _trade(300, "110.1", AggressorSide.BUY, "through")

    result = run_replay(
        _request(ReplayModel.TRADE_BBO_CENTRAL),
        _fees(),
        events=(through, entry, touch),
    )

    assert result.status is ReplayStatus.CLOSED
    assert result.entry is not None and result.exit is not None
    assert result.entry.price == Decimal("100.2")
    assert result.entry.quantity == Decimal("1.0")
    assert result.entry.book_levels_consumed == 2
    assert result.entry.benchmark_price == Decimal("100.1")
    assert result.entry.spread_bps == Decimal(20)
    assert result.entry.impact_bps is not None and result.entry.impact_bps > 0
    assert result.exit.timestamp == through.received_at
    assert result.exit.role is LiquidityRole.MAKER
    assert result.target_rested and result.target_trade_through


def test_short_replay_is_symmetric_and_requires_sell_aggression() -> None:
    short_intent = replace(
        _intent(),
        intent_id="intent-d4-short",
        direction=DriveDirection.SHORT,
        stop_price=Decimal("105"),
        target_price=Decimal("90"),
    )
    request = ReplayRequest(
        short_intent,
        replace(_risk(), intent_id=short_intent.intent_id),
        _config(ReplayModel.TRADE_BBO_CENTRAL),
    )

    result = run_replay(
        request,
        _fees(),
        events=(
            _book(100),
            _trade(250, "89.9", AggressorSide.SELL, "short-through"),
        ),
    )

    assert result.entry is not None and result.exit is not None
    assert result.entry.side is OrderSide.SELL
    assert result.entry.price == Decimal("99.9")
    assert result.exit.side is OrderSide.BUY
    assert result.exit.price == Decimal("90")
    assert result.exit_reason is ExitReason.TARGET


def test_event_entry_gap_through_stop_is_never_opened() -> None:
    result = run_replay(
        _request(ReplayModel.TRADE_BBO_CENTRAL),
        _fees(),
        events=(
            _book(
                100,
                bids=(("93.9", "2"),),
                asks=(("94.0", "2"),),
            ),
        ),
    )

    assert result.status is ReplayStatus.UNFILLED
    assert result.failure_code is FailureCode.ENTRY_GAP_THROUGH_STOP
    assert result.entry is None


def test_target_touch_does_not_fill_and_stop_waits_for_latency_book() -> None:
    request = _request(ReplayModel.TRADE_BBO_CENTRAL)
    entry = _book(100)
    touch = _trade(250, "110", AggressorSide.BUY, "touch")
    stop = _trade(300, "94.9", AggressorSide.SELL, "stop")
    too_early = _book(350, bids=(("94.5", "2"),), asks=(("94.6", "2"),))
    executable = _book(400, bids=(("93.8", "2"),), asks=(("93.9", "2"),))

    result = run_replay(
        request,
        _fees(),
        events=(entry, touch, stop, too_early, executable),
    )

    assert result.exit_reason is ExitReason.STOP
    assert result.exit is not None
    assert result.exit.timestamp == executable.received_at
    assert result.exit.latency_ms == 100
    assert not result.target_trade_through


def test_fee_change_is_selected_at_each_fill() -> None:
    old = _fee()
    new = _fee(
        ENTRY_TIME + timedelta(milliseconds=300),
        maker="0.0004",
        taker="0.0008",
        source="2",
    )
    result = run_replay(
        _request(ReplayModel.TRADE_BBO_CENTRAL),
        _fees(old, new),
        events=(
            _book(100),
            _trade(350, "110.1", AggressorSide.BUY, "through"),
        ),
    )

    assert result.entry is not None and result.exit is not None
    assert result.entry.fee_rate == Decimal("0.0005")
    assert result.exit.fee_rate == Decimal("0.0004")


def test_stress_doubles_latency_adds_p95_slippage_and_multiplies_fees() -> None:
    events = (
        _book(100, asks=(("100.1", "2"),)),
        _book(200, asks=(("100.5", "2"),)),
        _trade(450, "110.1", AggressorSide.BUY, "through"),
    )

    central = run_replay(
        _request(ReplayModel.TRADE_BBO_CENTRAL), _fees(), events=events
    )
    stress = run_replay(_request(ReplayModel.TRADE_BBO_STRESS), _fees(), events=events)

    assert central.entry is not None and stress.entry is not None
    assert central.entry.timestamp == events[0].received_at  # type: ignore[union-attr]
    assert stress.entry.timestamp == events[1].received_at  # type: ignore[union-attr]
    assert stress.entry.price > central.entry.price
    assert stress.entry.latency_ms == 200
    assert stress.entry.fee_rate == Decimal("0.00075")


def test_event_feed_loss_and_insufficient_depth_fail_closed() -> None:
    shallow = _book(100, asks=(("100.1", "0.2"),))
    unfilled = run_replay(
        _request(ReplayModel.TRADE_BBO_CENTRAL), _fees(), events=(shallow,)
    )
    assert unfilled.status is ReplayStatus.UNFILLED
    assert unfilled.failure_code is FailureCode.INSUFFICIENT_DEPTH

    failed = run_replay(
        _request(ReplayModel.TRADE_BBO_CENTRAL),
        _fees(),
        events=(
            _book(100),
            _trade(2_000, "110.1", AggressorSide.BUY, "late"),
        ),
    )
    assert failed.status is ReplayStatus.FAILED_CLOSED
    assert failed.failure_code is FailureCode.FEED_LOSS
    assert failed.net_pnl is None


def test_stale_transport_is_rejected_before_entry() -> None:
    stale = replace(
        _book(100),
        exchange_time=ENTRY_TIME - timedelta(seconds=2),
    )

    result = run_replay(
        _request(ReplayModel.TRADE_BBO_CENTRAL), _fees(), events=(stale,)
    )

    assert result.status is ReplayStatus.UNFILLED
    assert result.failure_code is FailureCode.STALE_DATA


def test_event_replay_is_bit_exact_independent_of_input_order() -> None:
    events = (
        _book(100),
        _trade(250, "110.1", AggressorSide.BUY, "through"),
    )

    first = run_replay(_request(ReplayModel.TRADE_BBO_CENTRAL), _fees(), events=events)
    second = run_replay(
        _request(ReplayModel.TRADE_BBO_CENTRAL),
        _fees(),
        events=tuple(reversed(events)),
    )

    assert first == second
    assert first.run_id == second.run_id


def test_model_contract_refuses_relaxed_stress_assumptions() -> None:
    with pytest.raises(ReplayContractError, match="2x latency"):
        replace(_config(ReplayModel.TRADE_BBO_STRESS), latency_multiplier=Decimal(1))
