"""Deterministic frequency, excursion and execution metrics for D5 studies."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from bot05.replay import ExitReason, ReplayResult, ReplayStatus
from bot05.studies.contracts import ExcursionObservation, SessionObservation


def _quantile(values: tuple[Decimal, ...], probability: Decimal) -> Decimal | None:
    if not values:
        return None
    ordered = tuple(sorted(values))
    if len(ordered) == 1:
        return ordered[0]
    position = Decimal(len(ordered) - 1) * probability
    lower = int(position)
    fraction = position - Decimal(lower)
    return ordered[lower] + (ordered[lower + 1] - ordered[lower]) * fraction


def _mean(values: tuple[Decimal, ...]) -> Decimal | None:
    if not values:
        return None
    return sum(values, Decimal(0)) / Decimal(len(values))


def _dispersion(values: tuple[Decimal, ...]) -> Decimal | None:
    mean = _mean(values)
    if mean is None:
        return None
    variance = sum(((item - mean) ** 2 for item in values), Decimal(0)) / Decimal(
        len(values)
    )
    return variance.sqrt()


@dataclass(frozen=True, slots=True)
class FunnelMetrics:
    observed_sessions: int
    complete_sessions: int
    eligible_sessions: int
    incomplete_sessions: int
    rejected_sessions: int
    drives: int
    pullbacks: int
    confirmations: int
    economic_gates: int
    trades: int

    def payload(self) -> dict[str, int]:
        return {
            "complete_sessions": self.complete_sessions,
            "confirmations": self.confirmations,
            "drives": self.drives,
            "economic_gates": self.economic_gates,
            "eligible_sessions": self.eligible_sessions,
            "incomplete_sessions": self.incomplete_sessions,
            "observed_sessions": self.observed_sessions,
            "pullbacks": self.pullbacks,
            "rejected_sessions": self.rejected_sessions,
            "trades": self.trades,
        }


def calculate_funnel(records: tuple[SessionObservation, ...]) -> FunnelMetrics:
    return FunnelMetrics(
        observed_sessions=len(records),
        complete_sessions=sum(item.complete for item in records),
        eligible_sessions=sum(item.session_eligible for item in records),
        incomplete_sessions=sum(not item.complete for item in records),
        rejected_sessions=sum(
            item.session_eligible and not item.trade for item in records
        ),
        drives=sum(item.drive for item in records),
        pullbacks=sum(item.pullback for item in records),
        confirmations=sum(item.confirmation for item in records),
        economic_gates=sum(item.economic_gate for item in records),
        trades=sum(item.trade for item in records),
    )


@dataclass(frozen=True, slots=True)
class ExcursionMetrics:
    horizon_minutes: int
    complete_count: int
    incomplete_count: int
    mean_mfe_bps: Decimal | None
    median_mfe_bps: Decimal | None
    mean_mae_bps: Decimal | None
    median_mae_bps: Decimal | None
    mean_mfe_r: Decimal | None
    median_mfe_r: Decimal | None
    mean_mae_r: Decimal | None
    median_mae_r: Decimal | None

    def payload(self) -> dict[str, object]:
        return {
            "complete_count": self.complete_count,
            "horizon_minutes": self.horizon_minutes,
            "incomplete_count": self.incomplete_count,
            "mean_mae_bps": _text(self.mean_mae_bps),
            "mean_mae_r": _text(self.mean_mae_r),
            "mean_mfe_bps": _text(self.mean_mfe_bps),
            "mean_mfe_r": _text(self.mean_mfe_r),
            "median_mae_bps": _text(self.median_mae_bps),
            "median_mae_r": _text(self.median_mae_r),
            "median_mfe_bps": _text(self.median_mfe_bps),
            "median_mfe_r": _text(self.median_mfe_r),
        }


def calculate_excursion_metrics(
    observations: tuple[ExcursionObservation, ...],
) -> tuple[ExcursionMetrics, ...]:
    metrics: list[ExcursionMetrics] = []
    for index, minutes in enumerate((15, 30, 60, 120)):
        horizons = tuple(item.horizons[index] for item in observations)
        complete = tuple(item for item in horizons if item.complete)
        mfe_bps = tuple(item.mfe_bps for item in complete if item.mfe_bps is not None)
        mae_bps = tuple(item.mae_bps for item in complete if item.mae_bps is not None)
        mfe_r = tuple(item.mfe_r for item in complete if item.mfe_r is not None)
        mae_r = tuple(item.mae_r for item in complete if item.mae_r is not None)
        metrics.append(
            ExcursionMetrics(
                horizon_minutes=minutes,
                complete_count=len(complete),
                incomplete_count=len(horizons) - len(complete),
                mean_mfe_bps=_mean(mfe_bps),
                median_mfe_bps=_quantile(mfe_bps, Decimal("0.5")),
                mean_mae_bps=_mean(mae_bps),
                median_mae_bps=_quantile(mae_bps, Decimal("0.5")),
                mean_mfe_r=_mean(mfe_r),
                median_mfe_r=_quantile(mfe_r, Decimal("0.5")),
                mean_mae_r=_mean(mae_r),
                median_mae_r=_quantile(mae_r, Decimal("0.5")),
            )
        )
    return tuple(metrics)


@dataclass(frozen=True, slots=True)
class PerformanceMetrics:
    replay_count: int
    closed_count: int
    unfilled_count: int
    failed_closed_count: int
    win_count: int
    loss_count: int
    win_rate: Decimal | None
    expectancy_net_r: Decimal | None
    expectancy_net_bps: Decimal | None
    profit_factor: Decimal | None
    gross_pnl: Decimal
    known_fees: Decimal
    funding_pnl: Decimal
    net_pnl: Decimal
    mean_configured_slippage_bps: Decimal | None
    mean_observed_spread_bps: Decimal | None
    mean_observed_impact_bps: Decimal | None
    max_observed_impact_bps: Decimal | None
    max_drawdown_r: Decimal
    max_drawdown_usd: Decimal
    max_drawdown_duration_seconds: int
    mean_pnl_r: Decimal | None
    median_pnl_r: Decimal | None
    dispersion_pnl_r: Decimal | None
    percentile_5_pnl_r: Decimal | None
    cvar_5_pnl_r: Decimal | None
    mean_position_seconds: Decimal | None
    median_position_seconds: Decimal | None
    stop_exits: int
    target_exits: int
    time_exits: int
    top_five_contribution: Decimal | None

    def payload(self) -> dict[str, object]:
        return {
            "closed_count": self.closed_count,
            "cvar_5_pnl_r": _text(self.cvar_5_pnl_r),
            "dispersion_pnl_r": _text(self.dispersion_pnl_r),
            "expectancy_net_bps": _text(self.expectancy_net_bps),
            "expectancy_net_r": _text(self.expectancy_net_r),
            "failed_closed_count": self.failed_closed_count,
            "funding_pnl": str(self.funding_pnl),
            "gross_pnl": str(self.gross_pnl),
            "known_fees": str(self.known_fees),
            "loss_count": self.loss_count,
            "max_drawdown_duration_seconds": self.max_drawdown_duration_seconds,
            "max_drawdown_r": str(self.max_drawdown_r),
            "max_drawdown_usd": str(self.max_drawdown_usd),
            "mean_pnl_r": _text(self.mean_pnl_r),
            "mean_configured_slippage_bps": _text(self.mean_configured_slippage_bps),
            "mean_observed_impact_bps": _text(self.mean_observed_impact_bps),
            "mean_observed_spread_bps": _text(self.mean_observed_spread_bps),
            "mean_position_seconds": _text(self.mean_position_seconds),
            "median_pnl_r": _text(self.median_pnl_r),
            "median_position_seconds": _text(self.median_position_seconds),
            "max_observed_impact_bps": _text(self.max_observed_impact_bps),
            "net_pnl": str(self.net_pnl),
            "percentile_5_pnl_r": _text(self.percentile_5_pnl_r),
            "profit_factor": _text(self.profit_factor),
            "replay_count": self.replay_count,
            "stop_exits": self.stop_exits,
            "target_exits": self.target_exits,
            "time_exits": self.time_exits,
            "top_five_contribution": _text(self.top_five_contribution),
            "unfilled_count": self.unfilled_count,
            "win_count": self.win_count,
            "win_rate": _text(self.win_rate),
        }


def _text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _drawdown(
    values: tuple[tuple[int, Decimal, Decimal], ...],
) -> tuple[Decimal, Decimal, int]:
    equity_r = Decimal(0)
    equity_usd = Decimal(0)
    peak_r = Decimal(0)
    peak_usd = Decimal(0)
    peak_time = values[0][0] if values else 0
    maximum_r = Decimal(0)
    maximum_usd = Decimal(0)
    maximum_duration = 0
    in_drawdown = False
    for timestamp, pnl_r, pnl_usd in values:
        equity_r += pnl_r
        equity_usd += pnl_usd
        peak_r = max(peak_r, equity_r)
        if equity_usd >= peak_usd:
            if in_drawdown:
                maximum_duration = max(maximum_duration, timestamp - peak_time)
            peak_usd = equity_usd
            peak_time = timestamp
            in_drawdown = False
        maximum_r = max(maximum_r, peak_r - equity_r)
        if equity_usd < peak_usd:
            in_drawdown = True
            maximum_usd = max(maximum_usd, peak_usd - equity_usd)
            maximum_duration = max(maximum_duration, timestamp - peak_time)
    return maximum_r, maximum_usd, maximum_duration


def calculate_performance(results: tuple[ReplayResult, ...]) -> PerformanceMetrics:
    closed = tuple(item for item in results if item.status is ReplayStatus.CLOSED)
    ordered = tuple(
        sorted(
            closed,
            key=lambda item: (
                item.exit.timestamp if item.exit is not None else item.run_id,
                item.run_id,
            ),
        )
    )
    pnl_r = tuple(item.pnl_r for item in closed if item.pnl_r is not None)
    pnl_usd = tuple(item.net_pnl for item in closed if item.net_pnl is not None)
    pnl_bps = tuple(
        Decimal(10_000) * item.net_pnl / (item.entry.price * item.entry.quantity)
        for item in closed
        if item.net_pnl is not None and item.entry is not None
    )
    wins = tuple(item for item in pnl_usd if item > 0)
    losses = tuple(item for item in pnl_usd if item <= 0)
    durations = tuple(
        Decimal(str((item.exit.timestamp - item.entry.timestamp).total_seconds()))
        for item in closed
        if item.entry is not None and item.exit is not None
    )
    drawdown_values = tuple(
        (
            int(item.exit.timestamp.timestamp()),
            item.pnl_r,
            item.net_pnl,
        )
        for item in ordered
        if item.exit is not None and item.pnl_r is not None and item.net_pnl is not None
    )
    max_drawdown_r, max_drawdown_usd, max_drawdown_duration = _drawdown(drawdown_values)
    p5 = _quantile(pnl_r, Decimal("0.05"))
    tail = tuple(item for item in pnl_r if p5 is not None and item <= p5)
    net_pnl = sum(pnl_usd, Decimal(0))
    positive_contribution = sum(sorted(wins, reverse=True)[:5], Decimal(0))
    known_fees = sum(
        (
            (item.entry.fee if item.entry is not None else Decimal(0))
            + (item.exit.fee if item.exit is not None else Decimal(0))
            for item in results
        ),
        Decimal(0),
    )
    fills = tuple(
        fill for item in results for fill in (item.entry, item.exit) if fill is not None
    )
    configured_slippage = tuple(item.slippage_bps for item in fills)
    observed_spreads = tuple(
        item.spread_bps for item in fills if item.spread_bps is not None
    )
    observed_impacts = tuple(
        item.impact_bps for item in fills if item.impact_bps is not None
    )
    return PerformanceMetrics(
        replay_count=len(results),
        closed_count=len(closed),
        unfilled_count=sum(item.status is ReplayStatus.UNFILLED for item in results),
        failed_closed_count=sum(
            item.status is ReplayStatus.FAILED_CLOSED for item in results
        ),
        win_count=len(wins),
        loss_count=len(losses),
        win_rate=(Decimal(len(wins)) / Decimal(len(closed)) if closed else None),
        expectancy_net_r=_mean(pnl_r),
        expectancy_net_bps=_mean(pnl_bps),
        profit_factor=(
            sum(wins, Decimal(0)) / abs(sum(losses, Decimal(0)))
            if losses and sum(losses, Decimal(0)) != 0
            else None
        ),
        gross_pnl=sum((item.gross_pnl or Decimal(0) for item in closed), Decimal(0)),
        known_fees=known_fees,
        funding_pnl=sum(
            (item.funding_pnl or Decimal(0) for item in closed), Decimal(0)
        ),
        net_pnl=net_pnl,
        mean_configured_slippage_bps=_mean(configured_slippage),
        mean_observed_spread_bps=_mean(observed_spreads),
        mean_observed_impact_bps=_mean(observed_impacts),
        max_observed_impact_bps=(max(observed_impacts) if observed_impacts else None),
        max_drawdown_r=max_drawdown_r,
        max_drawdown_usd=max_drawdown_usd,
        max_drawdown_duration_seconds=max_drawdown_duration,
        mean_pnl_r=_mean(pnl_r),
        median_pnl_r=_quantile(pnl_r, Decimal("0.5")),
        dispersion_pnl_r=_dispersion(pnl_r),
        percentile_5_pnl_r=p5,
        cvar_5_pnl_r=_mean(tail),
        mean_position_seconds=_mean(durations),
        median_position_seconds=_quantile(durations, Decimal("0.5")),
        stop_exits=sum(item.exit_reason is ExitReason.STOP for item in closed),
        target_exits=sum(item.exit_reason is ExitReason.TARGET for item in closed),
        time_exits=sum(item.exit_reason is ExitReason.TIME for item in closed),
        top_five_contribution=(
            positive_contribution / net_pnl if net_pnl > 0 else None
        ),
    )


@dataclass(frozen=True, slots=True)
class MarketStudyMetrics:
    funnel: FunnelMetrics
    excursions: tuple[ExcursionMetrics, ...]
    performance: PerformanceMetrics

    def payload(self) -> dict[str, object]:
        return {
            "excursions": [item.payload() for item in self.excursions],
            "funnel": self.funnel.payload(),
            "performance": self.performance.payload(),
        }


def calculate_market_metrics(
    sessions: tuple[SessionObservation, ...],
    excursions: tuple[ExcursionObservation, ...],
    results: tuple[ReplayResult, ...],
) -> MarketStudyMetrics:
    return MarketStudyMetrics(
        funnel=calculate_funnel(sessions),
        excursions=calculate_excursion_metrics(excursions),
        performance=calculate_performance(results),
    )
