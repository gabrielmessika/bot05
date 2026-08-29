"""Immutable contracts for deterministic, research-only replay simulations."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from bot05.features.opening_drive import DriveDirection
from bot05.risk import RiskDecision
from bot05.strategy import TradeIntent

_SHA256 = re.compile(r"^[a-f0-9]{64}$")


class ReplayContractError(ValueError):
    """Raised when a replay contract is incomplete or internally inconsistent."""


class ReplayModel(StrEnum):
    OHLC_CONSERVATIVE = "ohlc_conservative"
    OHLC_OPTIMISTIC = "ohlc_optimistic"
    TRADE_BBO_CENTRAL = "trade_bbo_central"
    TRADE_BBO_STRESS = "trade_bbo_stress"


class ReplayStatus(StrEnum):
    CLOSED = "closed"
    UNFILLED = "unfilled"
    FAILED_CLOSED = "failed_closed"


class ExitReason(StrEnum):
    STOP = "stop"
    TARGET = "target"
    TIME = "time"


class FailureCode(StrEnum):
    ENTRY_DATA_MISMATCH = "entry_data_mismatch"
    ENTRY_GAP_THROUGH_STOP = "entry_gap_through_stop"
    ENTRY_OUTSIDE_BRACKET = "entry_outside_bracket"
    FEED_LOSS = "feed_loss"
    INSUFFICIENT_DEPTH = "insufficient_depth"
    INVALID_SEQUENCE = "invalid_sequence"
    MISSING_FEE = "missing_fee"
    NO_ENTRY_LIQUIDITY = "no_entry_liquidity"
    NO_EXIT_LIQUIDITY = "no_exit_liquidity"
    STALE_DATA = "stale_data"


class LiquidityRole(StrEnum):
    MAKER = "maker"
    TAKER = "taker"


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


def _require_utc(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ReplayContractError(f"{name} must be timezone-aware UTC")


def _non_negative(value: Decimal, name: str, *, positive: bool = False) -> None:
    if not value.is_finite() or value < 0 or (positive and value == 0):
        qualifier = "positive" if positive else "non-negative"
        raise ReplayContractError(f"{name} must be finite and {qualifier}")


def _decimal_text(value: Decimal) -> str:
    return str(value)


@dataclass(frozen=True, slots=True)
class ReplayConfig:
    """All model assumptions needed to reproduce one simulated execution."""

    model: ReplayModel
    requested_quantity: Decimal
    price_tick: Decimal
    size_step: Decimal
    entry_latency_ms: int
    stop_latency_ms: int
    target_ack_latency_ms: int
    latency_multiplier: Decimal
    max_staleness_ms: int
    max_position_seconds: int
    slippage_bps: Decimal
    fee_multiplier: Decimal
    code_version: str

    def __post_init__(self) -> None:
        for name, value in (
            ("requested_quantity", self.requested_quantity),
            ("price_tick", self.price_tick),
            ("size_step", self.size_step),
            ("latency_multiplier", self.latency_multiplier),
            ("fee_multiplier", self.fee_multiplier),
        ):
            _non_negative(value, name, positive=True)
        _non_negative(self.slippage_bps, "slippage_bps")
        if any(
            value < 0
            for value in (
                self.entry_latency_ms,
                self.stop_latency_ms,
                self.target_ack_latency_ms,
            )
        ):
            raise ReplayContractError("replay latencies must be non-negative")
        if self.max_staleness_ms <= 0 or self.max_position_seconds <= 0:
            raise ReplayContractError(
                "staleness and position horizons must be positive"
            )
        if not self.code_version.strip():
            raise ReplayContractError("replay code_version is required")
        if self.model is ReplayModel.TRADE_BBO_STRESS:
            if self.latency_multiplier != Decimal("2"):
                raise ReplayContractError("trade_bbo_stress requires 2x latency")
            if self.fee_multiplier != Decimal("1.5"):
                raise ReplayContractError("trade_bbo_stress requires 1.5x fees")
            if self.slippage_bps <= 0:
                raise ReplayContractError("trade_bbo_stress requires p95 slippage")
        elif self.latency_multiplier != Decimal(1):
            raise ReplayContractError("non-stress models require 1x latency")
        if (
            self.model is not ReplayModel.TRADE_BBO_STRESS
            and self.fee_multiplier != Decimal(1)
        ):
            raise ReplayContractError("non-stress models require 1x fees")
        if self.model is ReplayModel.OHLC_OPTIMISTIC and self.slippage_bps != 0:
            raise ReplayContractError("ohlc_optimistic is a zero-slippage ceiling")

    @property
    def config_sha256(self) -> str:
        payload = {
            "code_version": self.code_version,
            "entry_latency_ms": self.entry_latency_ms,
            "fee_multiplier": _decimal_text(self.fee_multiplier),
            "latency_multiplier": _decimal_text(self.latency_multiplier),
            "max_position_seconds": self.max_position_seconds,
            "max_staleness_ms": self.max_staleness_ms,
            "model": self.model.value,
            "price_tick": _decimal_text(self.price_tick),
            "requested_quantity": _decimal_text(self.requested_quantity),
            "size_step": _decimal_text(self.size_step),
            "slippage_bps": _decimal_text(self.slippage_bps),
            "stop_latency_ms": self.stop_latency_ms,
            "target_ack_latency_ms": self.target_ack_latency_ms,
        }
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        return hashlib.sha256(encoded).hexdigest()

    def effective_latency_ms(self, base_ms: int) -> int:
        value = Decimal(base_ms) * self.latency_multiplier
        if value != value.to_integral_value():
            raise ReplayContractError("effective latency must resolve to milliseconds")
        return int(value)


@dataclass(frozen=True, slots=True)
class ReplayRequest:
    """One accepted causal intent and the immutable assumptions used to replay it."""

    intent: TradeIntent
    risk_decision: RiskDecision
    config: ReplayConfig

    def __post_init__(self) -> None:
        if self.risk_decision.intent_id != self.intent.intent_id:
            raise ReplayContractError("risk decision does not match replay intent")
        if not self.risk_decision.accepted:
            raise ReplayContractError("a refused risk decision cannot be replayed")
        if self.risk_decision.checked_at < self.intent.decided_at:
            raise ReplayContractError("risk decision predates the trade intent")


@dataclass(frozen=True, slots=True)
class SimulatedFill:
    timestamp: datetime
    side: OrderSide
    role: LiquidityRole
    price: Decimal
    quantity: Decimal
    fee_rate: Decimal
    fee: Decimal
    latency_ms: int
    slippage_bps: Decimal
    book_levels_consumed: int
    benchmark_price: Decimal | None = None
    spread_bps: Decimal | None = None
    impact_bps: Decimal | None = None

    def __post_init__(self) -> None:
        _require_utc(self.timestamp, "fill timestamp")
        _non_negative(self.price, "fill price", positive=True)
        _non_negative(self.quantity, "fill quantity", positive=True)
        if not self.fee_rate.is_finite() or not self.fee.is_finite():
            raise ReplayContractError("fill fee values must be finite")
        if self.latency_ms < 0 or self.book_levels_consumed < 0:
            raise ReplayContractError(
                "fill latency and level count must be non-negative"
            )
        _non_negative(self.slippage_bps, "fill slippage_bps")
        if self.benchmark_price is not None:
            _non_negative(self.benchmark_price, "fill benchmark_price", positive=True)
        if self.spread_bps is not None:
            _non_negative(self.spread_bps, "fill spread_bps")
        if self.impact_bps is not None:
            _non_negative(self.impact_bps, "fill impact_bps")


@dataclass(frozen=True, slots=True)
class ReplayResult:
    """Auditable terminal result; failed-open positions never fabricate PnL."""

    run_id: str
    intent_id: str
    market: str
    session_id: str
    direction: DriveDirection
    model: ReplayModel
    status: ReplayStatus
    config_sha256: str
    fee_schedule_sha256: str
    signal_data_sha256: str
    replay_data_sha256: str
    requested_quantity: Decimal
    filled_quantity: Decimal
    entry: SimulatedFill | None
    exit: SimulatedFill | None
    exit_reason: ExitReason | None
    failure_code: FailureCode | None
    same_bar_collision: bool
    target_rested: bool
    target_trade_through: bool
    gross_pnl: Decimal | None
    funding_pnl: Decimal | None
    net_pnl: Decimal | None
    pnl_r: Decimal | None

    def __post_init__(self) -> None:
        for name, digest in (
            ("run_id", self.run_id),
            ("config_sha256", self.config_sha256),
            ("fee_schedule_sha256", self.fee_schedule_sha256),
            ("signal_data_sha256", self.signal_data_sha256),
            ("replay_data_sha256", self.replay_data_sha256),
        ):
            if _SHA256.fullmatch(digest) is None:
                raise ReplayContractError(f"{name} must be a SHA-256 digest")
        if (
            not self.intent_id.strip()
            or not self.market.strip()
            or not self.session_id.strip()
        ):
            raise ReplayContractError("result intent, market and session are required")
        _non_negative(self.requested_quantity, "requested_quantity", positive=True)
        _non_negative(self.filled_quantity, "filled_quantity")
        closed = self.status is ReplayStatus.CLOSED
        if closed != (self.entry is not None and self.exit is not None):
            raise ReplayContractError("closed result requires entry and exit fills")
        if closed != (self.exit_reason is not None):
            raise ReplayContractError("closed result requires one exit reason")
        if closed == (self.failure_code is not None):
            raise ReplayContractError("terminal status and failure code disagree")
        pnl_values = (self.gross_pnl, self.funding_pnl, self.net_pnl, self.pnl_r)
        if closed != all(value is not None for value in pnl_values):
            raise ReplayContractError("PnL is defined only for closed results")
        if any(value is not None and not value.is_finite() for value in pnl_values):
            raise ReplayContractError("result PnL values must be finite")


def utc_text(value: datetime) -> str:
    _require_utc(value, "serialized timestamp")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
