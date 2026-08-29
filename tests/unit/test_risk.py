from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from bot05.features.opening_drive import DriveDirection
from bot05.models import ExternalPriceState, MarketStatus
from bot05.risk import (
    RiskContext,
    RiskLimits,
    RiskRefusalCode,
    RiskSnapshot,
    RiskSupervisor,
    apply_risk_decision,
    close_risk_position,
)
from bot05.strategy import ConfirmationKind, TargetKind, TradeIntent


def _intent(t0: datetime) -> TradeIntent:
    confirmation_time = t0 + timedelta(minutes=20)
    return TradeIntent(
        intent_id="intent-1",
        spec_id="spec-1",
        market="BTC",
        session_id="us_cash_open",
        t0=t0,
        decided_at=confirmation_time + timedelta(seconds=1),
        direction=DriveDirection.LONG,
        confirmation=ConfirmationKind.BREAKOUT,
        touch_time=confirmation_time,
        confirmation_time=confirmation_time,
        same_candle_touch_confirmation=True,
        entry_source="screening_next_open",
        entry_price=Decimal("100"),
        stop_price=Decimal("90"),
        target_kind=TargetKind.FIXED_2R,
        target_price=Decimal("120"),
        target_label="fixed_2r",
        midpoint=Decimal("95"),
        source_data_sha256="d" * 64,
        config_sha256="c" * 64,
        calendar_version="us_cash_2026:test",
        strategy_code_version="test",
    )


def _context(t0: datetime) -> RiskContext:
    observed = t0 + timedelta(minutes=20, seconds=1)
    return RiskContext(
        market="BTC",
        trading_day=date(2026, 8, 21),
        observed_at=observed,
        data_timestamp=observed - timedelta(milliseconds=100),
        market_status=MarketStatus.ACTIVE,
        external_price_state=ExternalPriceState.UNAVAILABLE,
        external_price_required=False,
        opening_drive_complete=True,
        clock_synchronized=True,
        session_unambiguous=True,
        market_definition_validated=True,
        feed_healthy=True,
        spread_bps=Decimal("1"),
        expected_slippage_bps=Decimal("1"),
        mark_price=Decimal("100"),
        oracle_price=Decimal("100"),
        equity=Decimal("10000"),
        requested_size=Decimal("0.1"),
        leverage=Decimal("1"),
        expected_win_cost_bps=Decimal("50"),
        expected_loss_cost_bps=Decimal("50"),
        orphan_order=False,
        unknown_fill=False,
        position_divergence=False,
        snapshot=RiskSnapshot(trading_day=date(2026, 8, 21)),
    )


def _limits() -> RiskLimits:
    return RiskLimits(
        max_staleness_ms=1_000,
        max_spread_bps=Decimal("5"),
        max_slippage_bps=Decimal("5"),
        max_oracle_mark_divergence_bps=Decimal("25"),
        max_risk_fraction=Decimal("0.0025"),
        max_leverage=Decimal("3"),
        max_daily_trades=2,
        max_daily_loss_r=Decimal("1"),
        min_net_reward_risk=Decimal("1.5"),
    )


def test_risk_supervisor_accepts_complete_bounded_intent_without_mutation() -> None:
    t0 = datetime(2026, 8, 21, 13, 30, tzinfo=UTC)
    intent = _intent(t0)
    context = _context(t0)
    supervisor = RiskSupervisor(_limits())

    first = supervisor.review(intent, context)
    second = supervisor.review(intent, context)

    assert first == second
    assert first.accepted
    assert first.limits_sha256 == _limits().limits_id
    assert first.refusal_codes == ()
    assert first.gross_reward_bps == Decimal("2000")
    assert first.gross_stop_bps == Decimal("1000")
    assert first.net_reward_risk == Decimal("1950") / Decimal("1050")
    assert first.position_risk_fraction == Decimal("0.000105")
    assert context.snapshot.processed_intent_ids == ()


def test_risk_supervisor_accumulates_all_fail_closed_refusals() -> None:
    t0 = datetime(2026, 8, 21, 13, 30, tzinfo=UTC)
    intent = _intent(t0)
    base = _context(t0)
    context = replace(
        base,
        market="ETH",
        trading_day=date(2026, 8, 22),
        data_timestamp=base.observed_at - timedelta(seconds=2),
        market_status=MarketStatus.HALTED,
        external_price_state=ExternalPriceState.INTERNAL,
        external_price_required=True,
        opening_drive_complete=False,
        clock_synchronized=False,
        session_unambiguous=False,
        market_definition_validated=False,
        feed_healthy=False,
        spread_bps=Decimal("6"),
        expected_slippage_bps=Decimal("6"),
        mark_price=Decimal("101"),
        requested_size=Decimal("100"),
        leverage=Decimal("4"),
        expected_win_cost_bps=Decimal("2100"),
        orphan_order=True,
        unknown_fill=True,
        position_divergence=True,
        snapshot=RiskSnapshot(
            trading_day=date(2026, 8, 21),
            processed_intent_ids=(intent.intent_id,),
            open_position_market="BTC",
            daily_trade_count=2,
            daily_realized_r=Decimal("-1"),
            cooldown_until=base.observed_at + timedelta(minutes=5),
        ),
    )

    decision = RiskSupervisor(_limits()).review(intent, context)

    assert not decision.accepted
    assert set(decision.refusal_codes) == set(RiskRefusalCode)


def test_future_data_timestamp_yields_one_clock_refusal() -> None:
    t0 = datetime(2026, 8, 21, 13, 30, tzinfo=UTC)
    intent = _intent(t0)
    context = _context(t0)
    context = replace(
        context,
        data_timestamp=context.observed_at + timedelta(milliseconds=1),
    )

    decision = RiskSupervisor(_limits()).review(intent, context)

    assert decision.refusal_codes == (RiskRefusalCode.CLOCK_UNSYNCHRONIZED,)


def test_risk_ledger_applies_decision_exactly_once_and_records_loss() -> None:
    t0 = datetime(2026, 8, 21, 13, 30, tzinfo=UTC)
    intent = _intent(t0)
    context = _context(t0)
    supervisor = RiskSupervisor(_limits())
    accepted = supervisor.review(intent, context)

    opened = apply_risk_decision(context.snapshot, intent, accepted)
    retried = apply_risk_decision(opened, intent, accepted)

    assert retried == opened
    assert opened.processed_intent_ids == (intent.intent_id,)
    assert opened.daily_trade_count == 1
    assert opened.open_position_market == "BTC"

    closed = close_risk_position(
        opened,
        market="BTC",
        realized_r=Decimal("-1"),
        cooldown_until=context.observed_at + timedelta(minutes=5),
    )
    assert closed.open_position_market is None
    assert closed.daily_realized_r == Decimal("-1")
    refused = supervisor.review(intent, replace(context, snapshot=closed))
    assert RiskRefusalCode.DUPLICATE_INTENT in refused.refusal_codes
    assert RiskRefusalCode.DAILY_LOSS_LIMIT in refused.refusal_codes
