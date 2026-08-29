"""Deterministic OHLC and trade/BBO replay engine with fail-closed outcomes."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TypeAlias

from bot05.features.opening_drive import DriveDirection
from bot05.models import BookSnapshot, Candle, Trade, encode_domain_record
from bot05.replay.contracts import (
    ExitReason,
    FailureCode,
    LiquidityRole,
    OrderSide,
    ReplayConfig,
    ReplayContractError,
    ReplayModel,
    ReplayRequest,
    ReplayResult,
    ReplayStatus,
    SimulatedFill,
)
from bot05.replay.costs import (
    FeeSchedule,
    FundingEvent,
    apply_adverse_slippage,
    funding_pnl,
    round_price,
    round_quantity,
    sweep_book,
)

MarketEvent: TypeAlias = Trade | BookSnapshot


def _event_key(event: MarketEvent) -> tuple[datetime, int, datetime, str]:
    if isinstance(event, BookSnapshot):
        identity = hashlib.sha256(encode_domain_record(event)).hexdigest()
        return (event.received_at, 0, event.exchange_time, identity)
    return (event.received_at, 1, event.exchange_time, event.trade_id)


def _funding_payload(event: FundingEvent) -> dict[str, str]:
    return {
        "effective_at": event.effective_at.isoformat(),
        "market": event.market,
        "mark_price": str(event.mark_price),
        "rate": str(event.rate),
        "source_sha256": event.source_sha256,
    }


def _replay_data_sha256(
    records: tuple[Candle | MarketEvent, ...],
    funding_events: tuple[FundingEvent, ...],
) -> str:
    digest = hashlib.sha256()
    for record in records:
        digest.update(encode_domain_record(record))
        digest.update(b"\n")
    for event in funding_events:
        encoded = json.dumps(
            _funding_payload(event), separators=(",", ":"), sort_keys=True
        ).encode()
        digest.update(encoded)
        digest.update(b"\n")
    return digest.hexdigest()


def _run_id(
    request: ReplayRequest,
    fee_schedule: FeeSchedule,
    replay_data_sha256: str,
) -> str:
    payload = {
        "config_sha256": request.config.config_sha256,
        "fee_schedule_sha256": fee_schedule.schedule_sha256,
        "intent_id": request.intent.intent_id,
        "limits_sha256": request.risk_decision.limits_sha256,
        "replay_data_sha256": replay_data_sha256,
        "signal_data_sha256": request.intent.source_data_sha256,
    }
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _entry_side(direction: DriveDirection) -> OrderSide:
    return OrderSide.BUY if direction is DriveDirection.LONG else OrderSide.SELL


def _exit_side(direction: DriveDirection) -> OrderSide:
    return OrderSide.SELL if direction is DriveDirection.LONG else OrderSide.BUY


def _is_entry_gap_through_stop(
    direction: DriveDirection, price: Decimal, stop: Decimal
) -> bool:
    if direction is DriveDirection.LONG:
        return price <= stop
    return price >= stop


def _is_outside_bracket(
    direction: DriveDirection,
    price: Decimal,
    stop: Decimal,
    target: Decimal,
) -> bool:
    if direction is DriveDirection.LONG:
        return not stop < price < target
    return not target < price < stop


def _failure_result(
    request: ReplayRequest,
    fee_schedule: FeeSchedule,
    replay_data_sha256: str,
    *,
    status: ReplayStatus,
    failure_code: FailureCode,
    quantity: Decimal,
    entry: SimulatedFill | None = None,
    target_rested: bool = False,
) -> ReplayResult:
    return ReplayResult(
        run_id=_run_id(request, fee_schedule, replay_data_sha256),
        intent_id=request.intent.intent_id,
        market=request.intent.market,
        session_id=request.intent.session_id,
        direction=request.intent.direction,
        model=request.config.model,
        status=status,
        config_sha256=request.config.config_sha256,
        fee_schedule_sha256=fee_schedule.schedule_sha256,
        signal_data_sha256=request.intent.source_data_sha256,
        replay_data_sha256=replay_data_sha256,
        requested_quantity=request.config.requested_quantity,
        filled_quantity=quantity if entry is not None else Decimal(0),
        entry=entry,
        exit=None,
        exit_reason=None,
        failure_code=failure_code,
        same_bar_collision=False,
        target_rested=target_rested,
        target_trade_through=False,
        gross_pnl=None,
        funding_pnl=None,
        net_pnl=None,
        pnl_r=None,
    )


def _closed_result(
    request: ReplayRequest,
    fee_schedule: FeeSchedule,
    replay_data_sha256: str,
    funding_events: tuple[FundingEvent, ...],
    *,
    entry: SimulatedFill,
    exit: SimulatedFill,
    exit_reason: ExitReason,
    same_bar_collision: bool = False,
    target_rested: bool = False,
    target_trade_through: bool = False,
) -> ReplayResult:
    direction_sign = (
        Decimal(1) if request.intent.direction is DriveDirection.LONG else Decimal(-1)
    )
    gross_pnl = direction_sign * (exit.price - entry.price) * entry.quantity
    funding = funding_pnl(
        funding_events,
        market=request.intent.market,
        direction=request.intent.direction,
        quantity=entry.quantity,
        entry_time=entry.timestamp,
        exit_time=exit.timestamp,
    )
    net_pnl = gross_pnl - entry.fee - exit.fee + funding
    initial_risk = abs(entry.price - request.intent.stop_price) * entry.quantity
    if initial_risk <= 0:
        raise ReplayContractError("simulated entry has no positive structural risk")
    return ReplayResult(
        run_id=_run_id(request, fee_schedule, replay_data_sha256),
        intent_id=request.intent.intent_id,
        market=request.intent.market,
        session_id=request.intent.session_id,
        direction=request.intent.direction,
        model=request.config.model,
        status=ReplayStatus.CLOSED,
        config_sha256=request.config.config_sha256,
        fee_schedule_sha256=fee_schedule.schedule_sha256,
        signal_data_sha256=request.intent.source_data_sha256,
        replay_data_sha256=replay_data_sha256,
        requested_quantity=request.config.requested_quantity,
        filled_quantity=entry.quantity,
        entry=entry,
        exit=exit,
        exit_reason=exit_reason,
        failure_code=None,
        same_bar_collision=same_bar_collision,
        target_rested=target_rested,
        target_trade_through=target_trade_through,
        gross_pnl=gross_pnl,
        funding_pnl=funding,
        net_pnl=net_pnl,
        pnl_r=net_pnl / initial_risk,
    )


def _make_fill(
    fee_schedule: FeeSchedule,
    config: ReplayConfig,
    *,
    market: str,
    timestamp: datetime,
    side: OrderSide,
    role: LiquidityRole,
    price: Decimal,
    quantity: Decimal,
    latency_ms: int,
    slippage_bps: Decimal,
    book_levels_consumed: int,
    benchmark_price: Decimal | None = None,
    spread_bps: Decimal | None = None,
    impact_bps: Decimal | None = None,
) -> SimulatedFill:
    fee_rate, fee = fee_schedule.calculate(
        market=market,
        timestamp=timestamp,
        role=role,
        notional=price * quantity,
        multiplier=config.fee_multiplier,
    )
    return SimulatedFill(
        timestamp=timestamp,
        side=side,
        role=role,
        price=price,
        quantity=quantity,
        fee_rate=fee_rate,
        fee=fee,
        latency_ms=latency_ms,
        slippage_bps=slippage_bps,
        book_levels_consumed=book_levels_consumed,
        benchmark_price=benchmark_price,
        spread_bps=spread_bps,
        impact_bps=impact_bps,
    )


def _ohlc_exit_fill(
    request: ReplayRequest,
    fee_schedule: FeeSchedule,
    *,
    timestamp: datetime,
    reference_price: Decimal,
    quantity: Decimal,
) -> SimulatedFill:
    side = _exit_side(request.intent.direction)
    price = apply_adverse_slippage(
        reference_price,
        side=side,
        slippage_bps=request.config.slippage_bps,
        price_tick=request.config.price_tick,
    )
    return _make_fill(
        fee_schedule,
        request.config,
        market=request.intent.market,
        timestamp=timestamp,
        side=side,
        role=LiquidityRole.TAKER,
        price=price,
        quantity=quantity,
        latency_ms=0,
        slippage_bps=request.config.slippage_bps,
        book_levels_consumed=0,
        benchmark_price=reference_price,
    )


def run_ohlc_replay(
    request: ReplayRequest,
    candles: tuple[Candle, ...],
    fee_schedule: FeeSchedule,
    *,
    funding_events: tuple[FundingEvent, ...] = (),
) -> ReplayResult:
    """Replay next-open screening with explicit pessimistic intrabar ordering."""

    if request.config.model not in {
        ReplayModel.OHLC_CONSERVATIVE,
        ReplayModel.OHLC_OPTIMISTIC,
    }:
        raise ReplayContractError("OHLC replay requires an OHLC model")
    replay_data_sha256 = _replay_data_sha256(candles, funding_events)
    quantity = round_quantity(
        request.config.requested_quantity, request.config.size_step
    )
    if quantity == 0:
        return _failure_result(
            request,
            fee_schedule,
            replay_data_sha256,
            status=ReplayStatus.UNFILLED,
            failure_code=FailureCode.INSUFFICIENT_DEPTH,
            quantity=quantity,
        )
    if not candles or any(item.market != request.intent.market for item in candles):
        return _failure_result(
            request,
            fee_schedule,
            replay_data_sha256,
            status=ReplayStatus.UNFILLED,
            failure_code=FailureCode.ENTRY_DATA_MISMATCH,
            quantity=quantity,
        )
    ordered = tuple(sorted(candles, key=lambda item: item.open_time))
    if ordered != candles or len({item.open_time for item in candles}) != len(candles):
        return _failure_result(
            request,
            fee_schedule,
            replay_data_sha256,
            status=ReplayStatus.UNFILLED,
            failure_code=FailureCode.INVALID_SEQUENCE,
            quantity=quantity,
        )
    eligible = tuple(
        item for item in candles if item.open_time >= request.intent.confirmation_time
    )
    if not eligible:
        return _failure_result(
            request,
            fee_schedule,
            replay_data_sha256,
            status=ReplayStatus.UNFILLED,
            failure_code=FailureCode.NO_ENTRY_LIQUIDITY,
            quantity=quantity,
        )
    entry_candle = eligible[0]
    if entry_candle.open != request.intent.entry_price:
        return _failure_result(
            request,
            fee_schedule,
            replay_data_sha256,
            status=ReplayStatus.UNFILLED,
            failure_code=FailureCode.ENTRY_DATA_MISMATCH,
            quantity=quantity,
        )
    if entry_candle.closed_at - entry_candle.close_time > timedelta(
        milliseconds=request.config.max_staleness_ms
    ):
        return _failure_result(
            request,
            fee_schedule,
            replay_data_sha256,
            status=ReplayStatus.UNFILLED,
            failure_code=FailureCode.STALE_DATA,
            quantity=quantity,
        )
    if _is_entry_gap_through_stop(
        request.intent.direction, entry_candle.open, request.intent.stop_price
    ):
        return _failure_result(
            request,
            fee_schedule,
            replay_data_sha256,
            status=ReplayStatus.UNFILLED,
            failure_code=FailureCode.ENTRY_GAP_THROUGH_STOP,
            quantity=quantity,
        )
    entry_side = _entry_side(request.intent.direction)
    entry_price = apply_adverse_slippage(
        entry_candle.open,
        side=entry_side,
        slippage_bps=request.config.slippage_bps,
        price_tick=request.config.price_tick,
    )
    if _is_outside_bracket(
        request.intent.direction,
        entry_price,
        request.intent.stop_price,
        request.intent.target_price,
    ):
        return _failure_result(
            request,
            fee_schedule,
            replay_data_sha256,
            status=ReplayStatus.UNFILLED,
            failure_code=FailureCode.ENTRY_OUTSIDE_BRACKET,
            quantity=quantity,
        )
    try:
        entry = _make_fill(
            fee_schedule,
            request.config,
            market=request.intent.market,
            timestamp=entry_candle.open_time,
            side=entry_side,
            role=LiquidityRole.TAKER,
            price=entry_price,
            quantity=quantity,
            latency_ms=0,
            slippage_bps=request.config.slippage_bps,
            book_levels_consumed=0,
            benchmark_price=entry_candle.open,
        )
    except ReplayContractError:
        return _failure_result(
            request,
            fee_schedule,
            replay_data_sha256,
            status=ReplayStatus.UNFILLED,
            failure_code=FailureCode.MISSING_FEE,
            quantity=quantity,
        )

    entry_index = candles.index(entry_candle)
    position_candles = candles[entry_index:]
    deadline = entry.timestamp + timedelta(seconds=request.config.max_position_seconds)
    previous: Candle | None = None
    conservative = request.config.model is ReplayModel.OHLC_CONSERVATIVE
    for candle in position_candles:
        if previous is not None and candle.open_time != previous.close_time:
            return _failure_result(
                request,
                fee_schedule,
                replay_data_sha256,
                status=ReplayStatus.FAILED_CLOSED,
                failure_code=FailureCode.FEED_LOSS,
                quantity=quantity,
                entry=entry,
            )
        if candle.interval_seconds != entry_candle.interval_seconds:
            return _failure_result(
                request,
                fee_schedule,
                replay_data_sha256,
                status=ReplayStatus.FAILED_CLOSED,
                failure_code=FailureCode.INVALID_SEQUENCE,
                quantity=quantity,
                entry=entry,
            )
        if candle.closed_at - candle.close_time > timedelta(
            milliseconds=request.config.max_staleness_ms
        ):
            return _failure_result(
                request,
                fee_schedule,
                replay_data_sha256,
                status=ReplayStatus.FAILED_CLOSED,
                failure_code=FailureCode.STALE_DATA,
                quantity=quantity,
                entry=entry,
            )

        long = request.intent.direction is DriveDirection.LONG
        stop_gap = (
            candle.open <= request.intent.stop_price
            if long
            else candle.open >= request.intent.stop_price
        )
        target_gap = (
            candle.open >= request.intent.target_price
            if long
            else candle.open <= request.intent.target_price
        )
        stop_touch = (
            candle.low <= request.intent.stop_price
            if long
            else candle.high >= request.intent.stop_price
        )
        target_touch = (
            candle.high >= request.intent.target_price
            if long
            else candle.low <= request.intent.target_price
        )
        collision = stop_touch and target_touch
        reason: ExitReason | None = None
        reference_price: Decimal | None = None
        if stop_gap:
            reason, reference_price = ExitReason.STOP, candle.open
        elif target_gap:
            reason = ExitReason.TARGET
            reference_price = (
                request.intent.target_price if conservative else candle.open
            )
        elif collision:
            reason = ExitReason.STOP if conservative else ExitReason.TARGET
            reference_price = (
                request.intent.stop_price
                if reason is ExitReason.STOP
                else request.intent.target_price
            )
        elif stop_touch:
            reason, reference_price = ExitReason.STOP, request.intent.stop_price
        elif target_touch:
            reason, reference_price = ExitReason.TARGET, request.intent.target_price
        elif candle.close_time >= deadline:
            reason, reference_price = ExitReason.TIME, candle.close

        if reason is not None and reference_price is not None:
            try:
                exit_fill = _ohlc_exit_fill(
                    request,
                    fee_schedule,
                    timestamp=(
                        candle.open_time
                        if stop_gap or target_gap
                        else candle.close_time
                        if reason is ExitReason.TIME
                        else candle.close_time
                    ),
                    reference_price=reference_price,
                    quantity=quantity,
                )
            except ReplayContractError:
                return _failure_result(
                    request,
                    fee_schedule,
                    replay_data_sha256,
                    status=ReplayStatus.FAILED_CLOSED,
                    failure_code=FailureCode.MISSING_FEE,
                    quantity=quantity,
                    entry=entry,
                )
            return _closed_result(
                request,
                fee_schedule,
                replay_data_sha256,
                funding_events,
                entry=entry,
                exit=exit_fill,
                exit_reason=reason,
                same_bar_collision=collision,
            )
        previous = candle

    return _failure_result(
        request,
        fee_schedule,
        replay_data_sha256,
        status=ReplayStatus.FAILED_CLOSED,
        failure_code=FailureCode.FEED_LOSS,
        quantity=quantity,
        entry=entry,
    )


@dataclass(frozen=True, slots=True)
class _BookExecution:
    price: Decimal
    levels_consumed: int
    benchmark_price: Decimal
    spread_bps: Decimal
    impact_bps: Decimal


def _book_execution_price(
    request: ReplayRequest,
    book: BookSnapshot,
    *,
    side: OrderSide,
    quantity: Decimal,
) -> _BookExecution | None:
    levels = book.asks if side is OrderSide.BUY else book.bids
    impact = sweep_book(levels, quantity)
    if impact is None:
        return None
    price = apply_adverse_slippage(
        impact.average_price,
        side=side,
        slippage_bps=request.config.slippage_bps,
        price_tick=request.config.price_tick,
    )
    benchmark_price = levels[0].price
    impact_bps = Decimal(10_000) * (
        (impact.average_price - benchmark_price) / benchmark_price
        if side is OrderSide.BUY
        else (benchmark_price - impact.average_price) / benchmark_price
    )
    midpoint = (book.bids[0].price + book.asks[0].price) / Decimal(2)
    spread_bps = Decimal(10_000) * (book.asks[0].price - book.bids[0].price) / midpoint
    return _BookExecution(
        price=price,
        levels_consumed=impact.levels_consumed,
        benchmark_price=benchmark_price,
        spread_bps=spread_bps,
        impact_bps=impact_bps,
    )


def _transport_failure(event: MarketEvent, max_staleness_ms: int) -> FailureCode | None:
    delay = event.received_at - event.exchange_time
    if delay < timedelta(0):
        return FailureCode.INVALID_SEQUENCE
    if delay > timedelta(milliseconds=max_staleness_ms):
        return FailureCode.STALE_DATA
    return None


def _book_crosses_stop(request: ReplayRequest, book: BookSnapshot) -> bool:
    if request.intent.direction is DriveDirection.LONG:
        return book.bids[0].price <= request.intent.stop_price
    return book.asks[0].price >= request.intent.stop_price


def _trade_crosses_stop(request: ReplayRequest, trade: Trade) -> bool:
    if request.intent.direction is DriveDirection.LONG:
        return trade.price <= request.intent.stop_price
    return trade.price >= request.intent.stop_price


def _trade_through_target(request: ReplayRequest, trade: Trade) -> bool:
    from bot05.models import AggressorSide

    target_price = round_price(
        request.intent.target_price,
        request.config.price_tick,
        _exit_side(request.intent.direction),
    )
    if request.intent.direction is DriveDirection.LONG:
        return trade.price > target_price and trade.aggressor_side is AggressorSide.BUY
    return trade.price < target_price and trade.aggressor_side is AggressorSide.SELL


def _event_exit_fill(
    request: ReplayRequest,
    fee_schedule: FeeSchedule,
    book: BookSnapshot,
    *,
    quantity: Decimal,
    latency_ms: int,
) -> SimulatedFill | None:
    side = _exit_side(request.intent.direction)
    execution = _book_execution_price(request, book, side=side, quantity=quantity)
    if execution is None:
        return None
    return _make_fill(
        fee_schedule,
        request.config,
        market=request.intent.market,
        timestamp=book.received_at,
        side=side,
        role=LiquidityRole.TAKER,
        price=execution.price,
        quantity=quantity,
        latency_ms=latency_ms,
        slippage_bps=request.config.slippage_bps,
        book_levels_consumed=execution.levels_consumed,
        benchmark_price=execution.benchmark_price,
        spread_bps=execution.spread_bps,
        impact_bps=execution.impact_bps,
    )


def run_event_replay(
    request: ReplayRequest,
    events: tuple[MarketEvent, ...],
    fee_schedule: FeeSchedule,
    *,
    funding_events: tuple[FundingEvent, ...] = (),
) -> ReplayResult:
    """Replay BBO impact and trade triggers without assuming touch equals fill."""

    if request.config.model not in {
        ReplayModel.TRADE_BBO_CENTRAL,
        ReplayModel.TRADE_BBO_STRESS,
    }:
        raise ReplayContractError("event replay requires a trade/BBO model")
    ordered = tuple(sorted(events, key=_event_key))
    replay_data_sha256 = _replay_data_sha256(ordered, funding_events)
    quantity = round_quantity(
        request.config.requested_quantity, request.config.size_step
    )
    if quantity == 0:
        return _failure_result(
            request,
            fee_schedule,
            replay_data_sha256,
            status=ReplayStatus.UNFILLED,
            failure_code=FailureCode.INSUFFICIENT_DEPTH,
            quantity=quantity,
        )
    if not events or any(item.market != request.intent.market for item in events):
        return _failure_result(
            request,
            fee_schedule,
            replay_data_sha256,
            status=ReplayStatus.UNFILLED,
            failure_code=FailureCode.ENTRY_DATA_MISMATCH,
            quantity=quantity,
        )
    duplicate_books = {
        (item.exchange_time, item.received_at)
        for item in ordered
        if isinstance(item, BookSnapshot)
    }
    if len(duplicate_books) != sum(isinstance(item, BookSnapshot) for item in ordered):
        return _failure_result(
            request,
            fee_schedule,
            replay_data_sha256,
            status=ReplayStatus.UNFILLED,
            failure_code=FailureCode.INVALID_SEQUENCE,
            quantity=quantity,
        )
    trade_ids = [item.trade_id for item in ordered if isinstance(item, Trade)]
    if len(set(trade_ids)) != len(trade_ids):
        return _failure_result(
            request,
            fee_schedule,
            replay_data_sha256,
            status=ReplayStatus.UNFILLED,
            failure_code=FailureCode.INVALID_SEQUENCE,
            quantity=quantity,
        )

    entry_latency = request.config.effective_latency_ms(request.config.entry_latency_ms)
    entry_ready_at = request.risk_decision.checked_at + timedelta(
        milliseconds=entry_latency
    )
    entry_book = next(
        (
            item
            for item in ordered
            if isinstance(item, BookSnapshot) and item.received_at >= entry_ready_at
        ),
        None,
    )
    if entry_book is None:
        return _failure_result(
            request,
            fee_schedule,
            replay_data_sha256,
            status=ReplayStatus.UNFILLED,
            failure_code=FailureCode.NO_ENTRY_LIQUIDITY,
            quantity=quantity,
        )
    entry_transport_failure = _transport_failure(
        entry_book, request.config.max_staleness_ms
    )
    if entry_transport_failure is not None:
        return _failure_result(
            request,
            fee_schedule,
            replay_data_sha256,
            status=ReplayStatus.UNFILLED,
            failure_code=entry_transport_failure,
            quantity=quantity,
        )
    if entry_book.received_at - entry_ready_at > timedelta(
        milliseconds=request.config.max_staleness_ms
    ):
        return _failure_result(
            request,
            fee_schedule,
            replay_data_sha256,
            status=ReplayStatus.UNFILLED,
            failure_code=FailureCode.STALE_DATA,
            quantity=quantity,
        )
    entry_side = _entry_side(request.intent.direction)
    entry_execution = _book_execution_price(
        request, entry_book, side=entry_side, quantity=quantity
    )
    if entry_execution is None:
        return _failure_result(
            request,
            fee_schedule,
            replay_data_sha256,
            status=ReplayStatus.UNFILLED,
            failure_code=FailureCode.INSUFFICIENT_DEPTH,
            quantity=quantity,
        )
    entry_price = entry_execution.price
    if _is_entry_gap_through_stop(
        request.intent.direction, entry_price, request.intent.stop_price
    ):
        return _failure_result(
            request,
            fee_schedule,
            replay_data_sha256,
            status=ReplayStatus.UNFILLED,
            failure_code=FailureCode.ENTRY_GAP_THROUGH_STOP,
            quantity=quantity,
        )
    if _is_outside_bracket(
        request.intent.direction,
        entry_price,
        request.intent.stop_price,
        request.intent.target_price,
    ):
        return _failure_result(
            request,
            fee_schedule,
            replay_data_sha256,
            status=ReplayStatus.UNFILLED,
            failure_code=FailureCode.ENTRY_OUTSIDE_BRACKET,
            quantity=quantity,
        )
    try:
        entry = _make_fill(
            fee_schedule,
            request.config,
            market=request.intent.market,
            timestamp=entry_book.received_at,
            side=entry_side,
            role=LiquidityRole.TAKER,
            price=entry_price,
            quantity=quantity,
            latency_ms=entry_latency,
            slippage_bps=request.config.slippage_bps,
            book_levels_consumed=entry_execution.levels_consumed,
            benchmark_price=entry_execution.benchmark_price,
            spread_bps=entry_execution.spread_bps,
            impact_bps=entry_execution.impact_bps,
        )
    except ReplayContractError:
        return _failure_result(
            request,
            fee_schedule,
            replay_data_sha256,
            status=ReplayStatus.UNFILLED,
            failure_code=FailureCode.MISSING_FEE,
            quantity=quantity,
        )

    target_ack_latency = request.config.effective_latency_ms(
        request.config.target_ack_latency_ms
    )
    target_ack_at = entry.timestamp + timedelta(milliseconds=target_ack_latency)
    stop_latency = request.config.effective_latency_ms(request.config.stop_latency_ms)
    deadline = entry.timestamp + timedelta(seconds=request.config.max_position_seconds)
    last_received_at = entry.timestamp
    pending_reason: ExitReason | None = None
    pending_ready_at: datetime | None = None
    target_rested = False

    start = ordered.index(entry_book) + 1
    for event in ordered[start:]:
        transport_failure = _transport_failure(event, request.config.max_staleness_ms)
        if transport_failure is not None:
            return _failure_result(
                request,
                fee_schedule,
                replay_data_sha256,
                status=ReplayStatus.FAILED_CLOSED,
                failure_code=transport_failure,
                quantity=quantity,
                entry=entry,
                target_rested=target_rested,
            )
        if event.received_at - last_received_at > timedelta(
            milliseconds=request.config.max_staleness_ms
        ):
            return _failure_result(
                request,
                fee_schedule,
                replay_data_sha256,
                status=ReplayStatus.FAILED_CLOSED,
                failure_code=FailureCode.FEED_LOSS,
                quantity=quantity,
                entry=entry,
                target_rested=target_rested,
            )
        last_received_at = event.received_at
        if event.received_at > target_ack_at:
            target_rested = True

        if pending_reason is None and event.received_at >= deadline:
            pending_reason = ExitReason.TIME
            pending_ready_at = deadline + timedelta(milliseconds=stop_latency)

        if (
            pending_reason is not None
            and pending_ready_at is not None
            and isinstance(event, BookSnapshot)
            and event.received_at >= pending_ready_at
        ):
            try:
                exit_fill = _event_exit_fill(
                    request,
                    fee_schedule,
                    event,
                    quantity=quantity,
                    latency_ms=stop_latency,
                )
            except ReplayContractError:
                return _failure_result(
                    request,
                    fee_schedule,
                    replay_data_sha256,
                    status=ReplayStatus.FAILED_CLOSED,
                    failure_code=FailureCode.MISSING_FEE,
                    quantity=quantity,
                    entry=entry,
                    target_rested=target_rested,
                )
            if exit_fill is None:
                return _failure_result(
                    request,
                    fee_schedule,
                    replay_data_sha256,
                    status=ReplayStatus.FAILED_CLOSED,
                    failure_code=FailureCode.NO_EXIT_LIQUIDITY,
                    quantity=quantity,
                    entry=entry,
                    target_rested=target_rested,
                )
            return _closed_result(
                request,
                fee_schedule,
                replay_data_sha256,
                funding_events,
                entry=entry,
                exit=exit_fill,
                exit_reason=pending_reason,
                target_rested=target_rested,
            )

        if pending_reason is not None:
            continue

        stop_crossed = (
            _book_crosses_stop(request, event)
            if isinstance(event, BookSnapshot)
            else _trade_crosses_stop(request, event)
        )
        if stop_crossed:
            pending_reason = ExitReason.STOP
            pending_ready_at = event.received_at + timedelta(milliseconds=stop_latency)
            if (
                stop_latency == 0
                and isinstance(event, BookSnapshot)
                and event.received_at >= pending_ready_at
            ):
                try:
                    exit_fill = _event_exit_fill(
                        request,
                        fee_schedule,
                        event,
                        quantity=quantity,
                        latency_ms=stop_latency,
                    )
                except ReplayContractError:
                    return _failure_result(
                        request,
                        fee_schedule,
                        replay_data_sha256,
                        status=ReplayStatus.FAILED_CLOSED,
                        failure_code=FailureCode.MISSING_FEE,
                        quantity=quantity,
                        entry=entry,
                        target_rested=target_rested,
                    )
                if exit_fill is None:
                    return _failure_result(
                        request,
                        fee_schedule,
                        replay_data_sha256,
                        status=ReplayStatus.FAILED_CLOSED,
                        failure_code=FailureCode.NO_EXIT_LIQUIDITY,
                        quantity=quantity,
                        entry=entry,
                        target_rested=target_rested,
                    )
                return _closed_result(
                    request,
                    fee_schedule,
                    replay_data_sha256,
                    funding_events,
                    entry=entry,
                    exit=exit_fill,
                    exit_reason=ExitReason.STOP,
                    target_rested=target_rested,
                )
            continue

        if (
            isinstance(event, Trade)
            and event.received_at > target_ack_at
            and _trade_through_target(request, event)
        ):
            side = _exit_side(request.intent.direction)
            target_price = round_price(
                request.intent.target_price, request.config.price_tick, side
            )
            try:
                exit_fill = _make_fill(
                    fee_schedule,
                    request.config,
                    market=request.intent.market,
                    timestamp=event.received_at,
                    side=side,
                    role=LiquidityRole.MAKER,
                    price=target_price,
                    quantity=quantity,
                    latency_ms=target_ack_latency,
                    slippage_bps=Decimal(0),
                    book_levels_consumed=0,
                    benchmark_price=target_price,
                    impact_bps=Decimal(0),
                )
            except ReplayContractError:
                return _failure_result(
                    request,
                    fee_schedule,
                    replay_data_sha256,
                    status=ReplayStatus.FAILED_CLOSED,
                    failure_code=FailureCode.MISSING_FEE,
                    quantity=quantity,
                    entry=entry,
                    target_rested=target_rested,
                )
            return _closed_result(
                request,
                fee_schedule,
                replay_data_sha256,
                funding_events,
                entry=entry,
                exit=exit_fill,
                exit_reason=ExitReason.TARGET,
                target_rested=True,
                target_trade_through=True,
            )

    return _failure_result(
        request,
        fee_schedule,
        replay_data_sha256,
        status=ReplayStatus.FAILED_CLOSED,
        failure_code=FailureCode.FEED_LOSS,
        quantity=quantity,
        entry=entry,
        target_rested=target_rested,
    )


def run_replay(
    request: ReplayRequest,
    fee_schedule: FeeSchedule,
    *,
    candles: tuple[Candle, ...] = (),
    events: tuple[MarketEvent, ...] = (),
    funding_events: tuple[FundingEvent, ...] = (),
) -> ReplayResult:
    """Dispatch one request while rejecting ambiguous mixed replay inputs."""

    if request.config.model in {
        ReplayModel.OHLC_CONSERVATIVE,
        ReplayModel.OHLC_OPTIMISTIC,
    }:
        if events:
            raise ReplayContractError("OHLC replay cannot consume event inputs")
        return run_ohlc_replay(
            request, candles, fee_schedule, funding_events=funding_events
        )
    if candles:
        raise ReplayContractError("event replay cannot consume candle inputs")
    return run_event_replay(
        request, events, fee_schedule, funding_events=funding_events
    )
