"""Auditable orchestration for a limited, research-only historical smoke run."""

from __future__ import annotations

import hashlib
import json
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import cast

from bot05.data.contracts import TimeRange
from bot05.data.reader import QualifiedRecordSet, read_qualified_event_records
from bot05.data.report import code_sha256
from bot05.features import DriveFilter, DriveThreshold, build_opening_drive
from bot05.features.candles import aggregate_trades
from bot05.models import DatasetProvenance, ExternalPriceState, MarketStatus
from bot05.replay.contracts import ReplayConfig, ReplayModel, ReplayRequest
from bot05.replay.costs import FeeSchedule, FeeSnapshot
from bot05.replay.engine import run_replay
from bot05.replay.reporting import result_payload
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


class HistoricalSmokeError(ValueError):
    """Raised when a historical smoke run lacks explicit, causal evidence."""


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise HistoricalSmokeError(f"{name} must be a table")
    return cast(Mapping[str, object], value)


def _exact(value: Mapping[str, object], name: str, keys: set[str]) -> None:
    if set(value) != keys:
        raise HistoricalSmokeError(
            f"{name} keys mismatch; missing={sorted(keys - set(value))}, "
            f"extra={sorted(set(value) - keys)}"
        )


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HistoricalSmokeError(f"{name} must be a non-empty string")
    return value


def _integer(value: object, name: str) -> int:
    if type(value) is not int:
        raise HistoricalSmokeError(f"{name} must be an integer")
    return value


def _decimal(value: object, name: str, *, allow_zero: bool = False) -> Decimal:
    if not isinstance(value, str):
        raise HistoricalSmokeError(f"{name} must be a decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise HistoricalSmokeError(f"{name} must be a decimal string") from exc
    if not parsed.is_finite() or parsed < 0 or (not allow_zero and parsed == 0):
        qualifier = "non-negative" if allow_zero else "positive"
        raise HistoricalSmokeError(f"{name} must be finite and {qualifier}")
    return parsed


def _utc(value: object, name: str) -> datetime:
    text = _string(value, name)
    if not text.endswith("Z"):
        raise HistoricalSmokeError(f"{name} must have a UTC Z suffix")
    try:
        parsed = datetime.fromisoformat(text.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise HistoricalSmokeError(f"{name} must be ISO-8601 UTC") from exc
    return parsed


def _ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _iso(value: datetime) -> str:
    return (
        value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


@dataclass(frozen=True, slots=True)
class HistoricalSmokeSpec:
    source_path: Path
    config_sha256: str
    purpose: str
    window_report: Path
    qualification_root: Path
    market: str
    session_id: str
    t0: datetime
    generated_at: datetime
    confirmation: ConfirmationKind
    target: TargetKind
    requested_quantity: Decimal
    price_tick: Decimal
    size_step: Decimal
    max_staleness_ms: int
    max_position_seconds: int
    central_entry_latency_ms: int
    central_stop_latency_ms: int
    central_target_ack_latency_ms: int
    conservative_slippage_bps: Decimal
    stress_slippage_bps: Decimal
    account_tier: str
    maker_rate: Decimal
    taker_rate: Decimal
    equity: Decimal
    max_spread_bps: Decimal
    max_slippage_bps: Decimal
    max_oracle_mark_divergence_bps: Decimal
    max_risk_fraction: Decimal
    max_leverage: Decimal
    max_daily_trades: int
    max_daily_loss_r: Decimal
    min_net_reward_risk: Decimal
    expected_win_cost_bps: Decimal
    expected_loss_cost_bps: Decimal
    calendar_assumption: str
    market_definition_assumption: str
    funding_assumption: str


def load_historical_smoke_spec(path: Path) -> HistoricalSmokeSpec:
    """Load a strict TOML spec whose checksum identifies every assumption."""

    resolved = path.resolve()
    try:
        payload = resolved.read_bytes()
        document = tomllib.loads(payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise HistoricalSmokeError(f"invalid historical smoke config: {path}") from exc
    _exact(
        document,
        "root",
        {
            "schema_version",
            "purpose",
            "window_report",
            "qualification_root",
            "market",
            "session_id",
            "t0_utc",
            "generated_at_utc",
            "strategy",
            "replay",
            "fees",
            "risk",
            "assumptions",
        },
    )
    if document.get("schema_version") != 1:
        raise HistoricalSmokeError("unsupported historical smoke config schema")
    strategy = _mapping(document.get("strategy"), "strategy")
    replay = _mapping(document.get("replay"), "replay")
    fees = _mapping(document.get("fees"), "fees")
    risk = _mapping(document.get("risk"), "risk")
    assumptions = _mapping(document.get("assumptions"), "assumptions")
    _exact(strategy, "strategy", {"confirmation", "target"})
    _exact(
        replay,
        "replay",
        {
            "requested_quantity",
            "price_tick",
            "size_step",
            "max_staleness_ms",
            "max_position_seconds",
            "central_entry_latency_ms",
            "central_stop_latency_ms",
            "central_target_ack_latency_ms",
            "conservative_slippage_bps",
            "stress_slippage_bps",
        },
    )
    _exact(fees, "fees", {"account_tier", "maker_rate", "taker_rate"})
    _exact(
        risk,
        "risk",
        {
            "equity",
            "max_spread_bps",
            "max_slippage_bps",
            "max_oracle_mark_divergence_bps",
            "max_risk_fraction",
            "max_leverage",
            "max_daily_trades",
            "max_daily_loss_r",
            "min_net_reward_risk",
            "expected_win_cost_bps",
            "expected_loss_cost_bps",
        },
    )
    _exact(
        assumptions,
        "assumptions",
        {"calendar", "market_definition", "funding"},
    )
    root = resolved.parent
    window_report = (
        root / _string(document.get("window_report"), "window_report")
    ).resolve()
    qualification_root = (
        root / _string(document.get("qualification_root"), "qualification_root")
    ).resolve()
    try:
        confirmation = ConfirmationKind(
            _string(strategy.get("confirmation"), "confirmation")
        )
        target = TargetKind(_string(strategy.get("target"), "target"))
    except ValueError as exc:
        raise HistoricalSmokeError("unsupported strategy variant") from exc
    if target is TargetKind.CAUSAL_LIQUIDITY:
        raise HistoricalSmokeError(
            "historical smoke cannot assume missing causal liquidity levels"
        )
    spec = HistoricalSmokeSpec(
        source_path=resolved,
        config_sha256=hashlib.sha256(payload).hexdigest(),
        purpose=_string(document.get("purpose"), "purpose"),
        window_report=window_report,
        qualification_root=qualification_root,
        market=_string(document.get("market"), "market"),
        session_id=_string(document.get("session_id"), "session_id"),
        t0=_utc(document.get("t0_utc"), "t0_utc"),
        generated_at=_utc(document.get("generated_at_utc"), "generated_at_utc"),
        confirmation=confirmation,
        target=target,
        requested_quantity=_decimal(
            replay.get("requested_quantity"), "requested_quantity"
        ),
        price_tick=_decimal(replay.get("price_tick"), "price_tick"),
        size_step=_decimal(replay.get("size_step"), "size_step"),
        max_staleness_ms=_integer(replay.get("max_staleness_ms"), "max_staleness_ms"),
        max_position_seconds=_integer(
            replay.get("max_position_seconds"), "max_position_seconds"
        ),
        central_entry_latency_ms=_integer(
            replay.get("central_entry_latency_ms"), "central_entry_latency_ms"
        ),
        central_stop_latency_ms=_integer(
            replay.get("central_stop_latency_ms"), "central_stop_latency_ms"
        ),
        central_target_ack_latency_ms=_integer(
            replay.get("central_target_ack_latency_ms"),
            "central_target_ack_latency_ms",
        ),
        conservative_slippage_bps=_decimal(
            replay.get("conservative_slippage_bps"),
            "conservative_slippage_bps",
            allow_zero=True,
        ),
        stress_slippage_bps=_decimal(
            replay.get("stress_slippage_bps"), "stress_slippage_bps"
        ),
        account_tier=_string(fees.get("account_tier"), "account_tier"),
        maker_rate=_decimal(fees.get("maker_rate"), "maker_rate", allow_zero=True),
        taker_rate=_decimal(fees.get("taker_rate"), "taker_rate", allow_zero=True),
        equity=_decimal(risk.get("equity"), "equity"),
        max_spread_bps=_decimal(risk.get("max_spread_bps"), "max_spread_bps"),
        max_slippage_bps=_decimal(risk.get("max_slippage_bps"), "max_slippage_bps"),
        max_oracle_mark_divergence_bps=_decimal(
            risk.get("max_oracle_mark_divergence_bps"),
            "max_oracle_mark_divergence_bps",
        ),
        max_risk_fraction=_decimal(risk.get("max_risk_fraction"), "max_risk_fraction"),
        max_leverage=_decimal(risk.get("max_leverage"), "max_leverage"),
        max_daily_trades=_integer(risk.get("max_daily_trades"), "max_daily_trades"),
        max_daily_loss_r=_decimal(risk.get("max_daily_loss_r"), "max_daily_loss_r"),
        min_net_reward_risk=_decimal(
            risk.get("min_net_reward_risk"), "min_net_reward_risk"
        ),
        expected_win_cost_bps=_decimal(
            risk.get("expected_win_cost_bps"),
            "expected_win_cost_bps",
            allow_zero=True,
        ),
        expected_loss_cost_bps=_decimal(
            risk.get("expected_loss_cost_bps"),
            "expected_loss_cost_bps",
            allow_zero=True,
        ),
        calendar_assumption=_string(assumptions.get("calendar"), "calendar"),
        market_definition_assumption=_string(
            assumptions.get("market_definition"), "market_definition"
        ),
        funding_assumption=_string(assumptions.get("funding"), "funding"),
    )
    if any(
        value < 0
        for value in (
            spec.central_entry_latency_ms,
            spec.central_stop_latency_ms,
            spec.central_target_ack_latency_ms,
        )
    ):
        raise HistoricalSmokeError("latencies must be non-negative")
    if spec.max_staleness_ms <= 0 or spec.max_position_seconds != 7_200:
        raise HistoricalSmokeError(
            "smoke requires positive staleness and the v0 120-minute horizon"
        )
    return spec


def _window_input(spec: HistoricalSmokeSpec) -> tuple[Mapping[str, object], str]:
    try:
        payload = spec.window_report.read_bytes()
        report = _mapping(json.loads(payload), "window report")
        sidecar = spec.window_report.with_suffix(spec.window_report.suffix + ".sha256")
        parts = sidecar.read_text("ascii").strip().split()
    except (OSError, json.JSONDecodeError) as exc:
        raise HistoricalSmokeError("invalid qualified window report") from exc
    digest = hashlib.sha256(payload).hexdigest()
    if (
        len(parts) != 2
        or parts[0] != digest
        or parts[1] != spec.window_report.name
        or report.get("schema_version") != 1
        or report.get("kind") != "bot05_h1_window_qualification"
        or report.get("qualified") is not True
        or report.get("market") != spec.market
    ):
        raise HistoricalSmokeError("window report is not a qualified matching input")
    return report, digest


def _bounds(report: Mapping[str, object]) -> TimeRange:
    coverage = report.get("coverage")
    if not isinstance(coverage, list) or len(coverage) != 1:
        raise HistoricalSmokeError("window report must declare one coverage range")
    item = _mapping(coverage[0], "window coverage")
    return TimeRange(
        _integer(item.get("start_ms"), "window start"),
        _integer(item.get("end_ms"), "window end"),
    )


def _load_records(
    spec: HistoricalSmokeSpec, report: Mapping[str, object], requested: TimeRange
) -> QualifiedRecordSet:
    raw_segments = report.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        raise HistoricalSmokeError("window report contains no segments")
    paths = tuple(
        Path(
            _string(
                _mapping(item, "window segment").get("manifest_path"), "manifest_path"
            )
        )
        for item in cast(list[object], raw_segments)
    )
    records = read_qualified_event_records(
        paths,
        qualification_root=spec.qualification_root,
        market=spec.market,
        requested=requested,
    )
    if report.get("derived_records_sha256") != records.records_sha256:
        raise HistoricalSmokeError("derived window records checksum mismatch")
    if report.get("trade_retransmission_count") != records.duplicate_trade_count:
        raise HistoricalSmokeError("window retransmission audit mismatch")
    return records


def _replay_config(spec: HistoricalSmokeSpec, model: ReplayModel) -> ReplayConfig:
    stress = model is ReplayModel.TRADE_BBO_STRESS
    ohlc = model in {ReplayModel.OHLC_CONSERVATIVE, ReplayModel.OHLC_OPTIMISTIC}
    return ReplayConfig(
        model=model,
        requested_quantity=spec.requested_quantity,
        price_tick=spec.price_tick,
        size_step=spec.size_step,
        entry_latency_ms=0 if ohlc else spec.central_entry_latency_ms,
        stop_latency_ms=0 if ohlc else spec.central_stop_latency_ms,
        target_ack_latency_ms=0 if ohlc else spec.central_target_ack_latency_ms,
        latency_multiplier=Decimal(2) if stress else Decimal(1),
        max_staleness_ms=spec.max_staleness_ms,
        max_position_seconds=spec.max_position_seconds,
        slippage_bps=(
            Decimal(0)
            if model is ReplayModel.OHLC_OPTIMISTIC
            else spec.stress_slippage_bps
            if stress
            else spec.conservative_slippage_bps
            if model is ReplayModel.OHLC_CONSERVATIVE
            else Decimal(0)
        ),
        fee_multiplier=Decimal("1.5") if stress else Decimal(1),
        code_version=f"sha256:{code_sha256()}",
    )


def run_historical_smoke(spec: HistoricalSmokeSpec) -> dict[str, object]:
    """Run all four replay models while retaining explicit smoke limitations."""

    window_report, window_report_sha256 = _window_input(spec)
    requested = _bounds(window_report)
    if requested.start_ms > _ms(
        spec.t0 - timedelta(minutes=30)
    ) or requested.end_ms < _ms(
        spec.t0 + timedelta(minutes=60) + timedelta(seconds=7_200)
    ):
        raise HistoricalSmokeError("qualified window does not cover the v0 horizon")
    records = _load_records(spec, window_report, requested)
    start = datetime.fromtimestamp(requested.start_ms / 1000, tz=UTC)
    end = datetime.fromtimestamp(requested.end_ms / 1000, tz=UTC)
    provenance = DatasetProvenance(
        dataset_id=f"historical-smoke-{spec.market.lower()}-{spec.t0.date()}",
        evidence_tier="H1",
        source="bot05",
        source_path_or_url=str(spec.window_report),
        raw_sha256=records.source_records_sha256,
        manifest_sha256=records.manifests_sha256,
        adapter_version="bot05-qualified-event-reader-v1",
        calendar_version=f"assumption:{spec.calendar_assumption}",
        code_version=f"sha256:{code_sha256()}",
        config_sha256=spec.config_sha256,
        source_timezone="UTC",
        period_start=start,
        period_end=end,
        transformations=(
            "qualified_h1_trades_bbo_only",
            "retain_earliest_consistent_market_trade_id",
            "exchange_time_ohlcv_v1",
        ),
    )
    one_minute = aggregate_trades(
        records.trades,
        market=spec.market,
        interval_seconds=60,
        requested=requested,
        qualified_coverage=(requested,),
        provenance=provenance,
    )
    five_minute = aggregate_trades(
        records.trades,
        market=spec.market,
        interval_seconds=300,
        requested=requested,
        qualified_coverage=(requested,),
        provenance=provenance,
    )
    if not one_minute.complete or not five_minute.complete:
        raise HistoricalSmokeError("qualified window produced candle gaps")
    drive_bars = tuple(
        item
        for item in five_minute.candles
        if spec.t0 <= item.open_time < spec.t0 + timedelta(minutes=15)
    )
    drive_result = build_opening_drive(
        drive_bars,
        market=spec.market,
        session_id=spec.session_id,
        t0=spec.t0,
        observed_at=max(item.closed_at for item in drive_bars),
    )
    if drive_result.drive is None:
        raise HistoricalSmokeError(
            f"opening drive rejected: {drive_result.rejection_reason}"
        )
    strategy_spec = StrategySpec(
        market=spec.market,
        session_id=spec.session_id,
        drive_filter=DriveFilter.NONE,
        confirmation=spec.confirmation,
        target=spec.target,
        config_sha256=spec.config_sha256,
        calendar_version=provenance.calendar_version,
        code_version=provenance.code_version,
    )
    snapshot = initialize_strategy(
        strategy_spec, t0=spec.t0, source_data_sha256=records.records_sha256
    )
    snapshot = register_opening_drive(
        snapshot,
        drive_result.drive,
        DriveThreshold(
            market=spec.market,
            session_id=spec.session_id,
            filter=DriveFilter.NONE,
            as_of=spec.t0,
            sample_count=0,
            value=Decimal(0),
            eligible=True,
        ),
    )
    for candle in five_minute.candles:
        if not (
            spec.t0 + timedelta(minutes=15) <= candle.open_time
            and candle.close_time <= spec.t0 + timedelta(minutes=60)
        ):
            continue
        snapshot = advance_candle(snapshot, candle, observed_at=candle.closed_at)
        if snapshot.confirmation is not None:
            entry_candle = next(
                (
                    item
                    for item in one_minute.candles
                    if item.open_time == snapshot.confirmation.confirmed_at
                ),
                None,
            )
            if entry_candle is None:
                raise HistoricalSmokeError("next 1m entry candle is missing")
            entry_trade = min(
                (
                    item
                    for item in records.trades
                    if entry_candle.open_time
                    <= item.exchange_time
                    < entry_candle.close_time
                    and item.price == entry_candle.open
                ),
                key=lambda item: (item.exchange_time, item.received_at),
                default=None,
            )
            if entry_trade is None:
                raise HistoricalSmokeError("next-open observation cannot be proven")
            snapshot = observe_entry_price(
                snapshot,
                EntryPriceObservation(
                    market=spec.market,
                    observed_at=entry_trade.received_at,
                    price=entry_candle.open,
                    source="qualified_h1_next_1m_open",
                ),
            )
            break
    if snapshot.intent is None:
        raise HistoricalSmokeError(
            f"session produced no intent: {snapshot.state.value}:{snapshot.reason}"
        )
    intent = snapshot.intent
    book = max(
        (item for item in records.books if item.received_at <= intent.decided_at),
        key=lambda item: item.received_at,
        default=None,
    )
    if book is None:
        raise HistoricalSmokeError("no causal BBO is available for risk review")
    midpoint = (book.bids[0].price + book.asks[0].price) / Decimal(2)
    spread_bps = Decimal(10_000) * (book.asks[0].price - book.bids[0].price) / midpoint
    limits = RiskLimits(
        max_staleness_ms=spec.max_staleness_ms,
        max_spread_bps=spec.max_spread_bps,
        max_slippage_bps=spec.max_slippage_bps,
        max_oracle_mark_divergence_bps=spec.max_oracle_mark_divergence_bps,
        max_risk_fraction=spec.max_risk_fraction,
        max_leverage=spec.max_leverage,
        max_daily_trades=spec.max_daily_trades,
        max_daily_loss_r=spec.max_daily_loss_r,
        min_net_reward_risk=spec.min_net_reward_risk,
    )
    risk_context = RiskContext(
        market=spec.market,
        trading_day=spec.t0.date(),
        observed_at=intent.decided_at,
        data_timestamp=book.exchange_time,
        market_status=MarketStatus.ACTIVE,
        external_price_state=ExternalPriceState.UNAVAILABLE,
        external_price_required=False,
        opening_drive_complete=True,
        clock_synchronized=True,
        session_unambiguous=True,
        market_definition_validated=True,
        feed_healthy=True,
        spread_bps=spread_bps,
        expected_slippage_bps=spec.conservative_slippage_bps,
        mark_price=midpoint,
        oracle_price=midpoint,
        equity=spec.equity,
        requested_size=spec.requested_quantity,
        leverage=Decimal(1),
        expected_win_cost_bps=spec.expected_win_cost_bps,
        expected_loss_cost_bps=spec.expected_loss_cost_bps,
        orphan_order=False,
        unknown_fill=False,
        position_divergence=False,
        snapshot=RiskSnapshot(trading_day=spec.t0.date()),
    )
    decision = RiskSupervisor(limits).review(intent, risk_context)
    if not decision.accepted:
        raise HistoricalSmokeError(
            "risk review refused smoke intent: "
            + ",".join(item.value for item in decision.refusal_codes)
        )
    fee_schedule = FeeSchedule(
        (
            FeeSnapshot(
                market=spec.market,
                effective_at=start,
                account_tier=spec.account_tier,
                base_maker_rate=spec.maker_rate,
                base_taker_rate=spec.taker_rate,
                growth_mode=False,
                deployer_fee_scale=Decimal(1),
                staking_discount_rate=Decimal(0),
                referral_discount_rate=Decimal(0),
                builder_fee_rate=Decimal(0),
                effective_maker_rate=spec.maker_rate,
                effective_taker_rate=spec.taker_rate,
                source_sha256=spec.config_sha256,
            ),
        )
    )
    results = []
    for model in ReplayModel:
        config = _replay_config(spec, model)
        request = ReplayRequest(intent, decision, config)
        if model in {
            ReplayModel.OHLC_CONSERVATIVE,
            ReplayModel.OHLC_OPTIMISTIC,
        }:
            result = run_replay(request, fee_schedule, candles=one_minute.candles)
        else:
            result = run_replay(
                request,
                fee_schedule,
                events=(*records.trades, *records.books),
            )
        results.append(result)
    body: dict[str, object] = {
        "schema_version": 1,
        "kind": "bot05_historical_pipeline_smoke",
        "purpose": spec.purpose,
        "generated_at": _iso(spec.generated_at),
        "market": spec.market,
        "session_id": spec.session_id,
        "t0_utc": _iso(spec.t0),
        "evidence_tier": "H1",
        "promotion_eligible": False,
        "research_conclusion": "data_insufficient",
        "network_performed": False,
        "orders_possible": False,
        "config_path": str(spec.source_path),
        "config_sha256": spec.config_sha256,
        "code_sha256": code_sha256(),
        "window_report_path": str(spec.window_report),
        "window_report_sha256": window_report_sha256,
        "data_quality": {
            "source_record_count": sum(item.record_count for item in records.sources),
            "derived_record_count": len(records.records),
            "trade_count": len(records.trades),
            "bbo_count": len(records.books),
            "trade_retransmission_count": records.duplicate_trade_count,
            "max_bbo_gap_ms": records.max_bbo_gap_ms,
            "derived_records_sha256": records.records_sha256,
            "source_records_sha256": records.source_records_sha256,
            "manifests_sha256": records.manifests_sha256,
            "candles_1m": len(one_minute.candles),
            "candles_5m": len(five_minute.candles),
            "candle_gap_count": len(one_minute.gaps) + len(five_minute.gaps),
        },
        "strategy": {
            "spec_id": strategy_spec.spec_id,
            "drive_filter": DriveFilter.NONE.value,
            "confirmation": spec.confirmation.value,
            "target": spec.target.value,
            "direction": intent.direction.value,
            "confirmation_time": _iso(intent.confirmation_time),
            "decision_time": _iso(intent.decided_at),
            "entry_reference": str(intent.entry_price),
            "stop_price": str(intent.stop_price),
            "target_price": str(intent.target_price),
            "intent_id": intent.intent_id,
        },
        "risk": {
            "accepted": decision.accepted,
            "limits_sha256": decision.limits_sha256,
            "spread_bps": str(spread_bps),
            "net_reward_risk": str(decision.net_reward_risk),
            "position_risk_fraction": str(decision.position_risk_fraction),
        },
        "fee_schedule_sha256": fee_schedule.schedule_sha256,
        "replays": [result_payload(item) for item in results],
        "assumptions": {
            "calendar": spec.calendar_assumption,
            "market_definition": spec.market_definition_assumption,
            "fees": "explicit research assumption hashed by the run config",
            "funding": spec.funding_assumption,
        },
        "limitations": [
            "one H1 session cannot establish expectancy or an edge",
            "no H0 candle parity source is locally available",
            "fees are an explicit research assumption, not a historical snapshot",
            "funding events and market context are absent from this H1 input",
            "calendar and market-definition facts are explicit smoke assumptions",
            "ohlc_optimistic is an informational ceiling only",
        ],
    }
    report_id = hashlib.sha256(
        json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {**body, "report_id": report_id}
