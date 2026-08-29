"""Pure fail-closed pre-trade risk review for BOT05 trade intents."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from bot05.models import ExternalPriceState, MarketStatus
from bot05.strategy.contract import TradeIntent

_SHA256 = re.compile(r"^[a-f0-9]{64}$")


class RiskContractError(ValueError):
    """Raised when risk inputs are incomplete or internally inconsistent."""


class RiskRefusalCode(StrEnum):
    CONTEXT_SCOPE_MISMATCH = "context_scope_mismatch"
    RISK_SNAPSHOT_DAY_MISMATCH = "risk_snapshot_day_mismatch"
    DUPLICATE_INTENT = "duplicate_intent"
    OPENING_DRIVE_GAP = "opening_drive_gap"
    STALE_DATA = "stale_data"
    CLOCK_UNSYNCHRONIZED = "clock_unsynchronized"
    SESSION_AMBIGUOUS = "session_ambiguous"
    MARKET_UNAVAILABLE = "market_unavailable"
    MARKET_DEFINITION_UNVALIDATED = "market_definition_unvalidated"
    INTERNAL_ORACLE = "internal_oracle"
    SPREAD_LIMIT = "spread_limit"
    SLIPPAGE_LIMIT = "slippage_limit"
    ORACLE_MARK_DIVERGENCE = "oracle_mark_divergence"
    POSITION_ALREADY_OPEN = "position_already_open"
    DAILY_TRADE_LIMIT = "daily_trade_limit"
    DAILY_LOSS_LIMIT = "daily_loss_limit"
    COOLDOWN_ACTIVE = "cooldown_active"
    ORPHAN_ORDER = "orphan_order"
    UNKNOWN_FILL = "unknown_fill"
    POSITION_DIVERGENCE = "position_divergence"
    FEED_UNHEALTHY = "feed_unhealthy"
    POSITION_RISK_LIMIT = "position_risk_limit"
    LEVERAGE_LIMIT = "leverage_limit"
    NET_REWARD_RISK = "net_reward_risk"


def _require_utc(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise RiskContractError(f"{name} must be timezone-aware UTC")


def _positive(value: Decimal, name: str, *, allow_zero: bool = False) -> None:
    if not value.is_finite() or value < 0 or (value == 0 and not allow_zero):
        qualifier = "non-negative" if allow_zero else "positive"
        raise RiskContractError(f"{name} must be finite and {qualifier}")


@dataclass(frozen=True, slots=True)
class RiskLimits:
    max_staleness_ms: int
    max_spread_bps: Decimal
    max_slippage_bps: Decimal
    max_oracle_mark_divergence_bps: Decimal
    max_risk_fraction: Decimal
    max_leverage: Decimal
    max_daily_trades: int
    max_daily_loss_r: Decimal
    min_net_reward_risk: Decimal

    def __post_init__(self) -> None:
        if self.max_staleness_ms <= 0 or self.max_daily_trades <= 0:
            raise RiskContractError("risk count and staleness limits must be positive")
        for name, value in (
            ("max_spread_bps", self.max_spread_bps),
            ("max_slippage_bps", self.max_slippage_bps),
            (
                "max_oracle_mark_divergence_bps",
                self.max_oracle_mark_divergence_bps,
            ),
            ("max_risk_fraction", self.max_risk_fraction),
            ("max_leverage", self.max_leverage),
            ("max_daily_loss_r", self.max_daily_loss_r),
            ("min_net_reward_risk", self.min_net_reward_risk),
        ):
            _positive(value, name)
        if self.max_risk_fraction > Decimal(1):
            raise RiskContractError("max risk fraction cannot exceed one")

    @property
    def limits_id(self) -> str:
        payload = {
            "max_daily_loss_r": str(self.max_daily_loss_r),
            "max_daily_trades": self.max_daily_trades,
            "max_leverage": str(self.max_leverage),
            "max_oracle_mark_divergence_bps": str(self.max_oracle_mark_divergence_bps),
            "max_risk_fraction": str(self.max_risk_fraction),
            "max_slippage_bps": str(self.max_slippage_bps),
            "max_spread_bps": str(self.max_spread_bps),
            "max_staleness_ms": self.max_staleness_ms,
            "min_net_reward_risk": str(self.min_net_reward_risk),
        }
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class RiskSnapshot:
    trading_day: date
    processed_intent_ids: tuple[str, ...] = ()
    open_position_market: str | None = None
    daily_trade_count: int = 0
    daily_realized_r: Decimal = Decimal(0)
    cooldown_until: datetime | None = None

    def __post_init__(self) -> None:
        if self.daily_trade_count < 0 or not self.daily_realized_r.is_finite():
            raise RiskContractError("daily risk ledger values are invalid")
        if len(set(self.processed_intent_ids)) != len(self.processed_intent_ids):
            raise RiskContractError("processed intent identities must be unique")
        if (
            self.open_position_market is not None
            and not self.open_position_market.strip()
        ):
            raise RiskContractError("open position market must not be blank")
        if self.cooldown_until is not None:
            _require_utc(self.cooldown_until, "cooldown_until")


@dataclass(frozen=True, slots=True)
class RiskContext:
    market: str
    trading_day: date
    observed_at: datetime
    data_timestamp: datetime
    market_status: MarketStatus
    external_price_state: ExternalPriceState
    external_price_required: bool
    opening_drive_complete: bool
    clock_synchronized: bool
    session_unambiguous: bool
    market_definition_validated: bool
    feed_healthy: bool
    spread_bps: Decimal
    expected_slippage_bps: Decimal
    mark_price: Decimal
    oracle_price: Decimal
    equity: Decimal
    requested_size: Decimal
    leverage: Decimal
    expected_win_cost_bps: Decimal
    expected_loss_cost_bps: Decimal
    orphan_order: bool
    unknown_fill: bool
    position_divergence: bool
    snapshot: RiskSnapshot

    def __post_init__(self) -> None:
        if not self.market.strip():
            raise RiskContractError("risk context market is required")
        _require_utc(self.observed_at, "observed_at")
        _require_utc(self.data_timestamp, "data_timestamp")
        for name, value, allow_zero in (
            ("spread_bps", self.spread_bps, True),
            ("expected_slippage_bps", self.expected_slippage_bps, True),
            ("mark_price", self.mark_price, False),
            ("oracle_price", self.oracle_price, False),
            ("equity", self.equity, False),
            ("requested_size", self.requested_size, False),
            ("leverage", self.leverage, False),
            ("expected_win_cost_bps", self.expected_win_cost_bps, True),
            ("expected_loss_cost_bps", self.expected_loss_cost_bps, True),
        ):
            _positive(value, name, allow_zero=allow_zero)


@dataclass(frozen=True, slots=True)
class RiskDecision:
    intent_id: str
    limits_sha256: str
    checked_at: datetime
    accepted: bool
    refusal_codes: tuple[RiskRefusalCode, ...]
    gross_reward_bps: Decimal
    gross_stop_bps: Decimal
    net_reward_risk: Decimal
    position_risk_fraction: Decimal

    def __post_init__(self) -> None:
        _require_utc(self.checked_at, "checked_at")
        if _SHA256.fullmatch(self.limits_sha256) is None:
            raise RiskContractError("limits_sha256 must be a SHA-256 digest")
        if self.accepted == bool(self.refusal_codes):
            raise RiskContractError("accepted decision and refusals disagree")


@dataclass(frozen=True, slots=True)
class RiskSupervisor:
    limits: RiskLimits

    def review(self, intent: TradeIntent, context: RiskContext) -> RiskDecision:
        """Review all gates in a stable order and never mutate the risk ledger."""

        gross_reward_bps = (
            Decimal(10_000)
            * abs(intent.target_price - intent.entry_price)
            / intent.entry_price
        )
        gross_stop_bps = (
            Decimal(10_000)
            * abs(intent.entry_price - intent.stop_price)
            / intent.entry_price
        )
        net_reward = gross_reward_bps - context.expected_win_cost_bps
        net_loss = gross_stop_bps + context.expected_loss_cost_bps
        net_reward_risk = net_reward / net_loss
        position_risk_fraction = (
            (
                abs(intent.entry_price - intent.stop_price)
                + intent.entry_price * context.expected_loss_cost_bps / Decimal(10_000)
            )
            * context.requested_size
            / context.equity
        )
        effective_leverage = (
            intent.entry_price * context.requested_size / context.equity
        )

        refusals: list[RiskRefusalCode] = []
        if context.market != intent.market:
            refusals.append(RiskRefusalCode.CONTEXT_SCOPE_MISMATCH)
        if context.snapshot.trading_day != context.trading_day:
            refusals.append(RiskRefusalCode.RISK_SNAPSHOT_DAY_MISMATCH)
        if intent.intent_id in context.snapshot.processed_intent_ids:
            refusals.append(RiskRefusalCode.DUPLICATE_INTENT)
        if not context.opening_drive_complete:
            refusals.append(RiskRefusalCode.OPENING_DRIVE_GAP)
        staleness_ms = int(
            (context.observed_at - context.data_timestamp).total_seconds() * 1000
        )
        if staleness_ms < 0:
            refusals.append(RiskRefusalCode.CLOCK_UNSYNCHRONIZED)
        elif staleness_ms > self.limits.max_staleness_ms:
            refusals.append(RiskRefusalCode.STALE_DATA)
        if not context.clock_synchronized:
            refusals.append(RiskRefusalCode.CLOCK_UNSYNCHRONIZED)
        if not context.session_unambiguous:
            refusals.append(RiskRefusalCode.SESSION_AMBIGUOUS)
        if context.market_status is not MarketStatus.ACTIVE:
            refusals.append(RiskRefusalCode.MARKET_UNAVAILABLE)
        if not context.market_definition_validated:
            refusals.append(RiskRefusalCode.MARKET_DEFINITION_UNVALIDATED)
        if (
            context.external_price_required
            and context.external_price_state is not ExternalPriceState.EXTERNAL
        ):
            refusals.append(RiskRefusalCode.INTERNAL_ORACLE)
        if context.spread_bps > self.limits.max_spread_bps:
            refusals.append(RiskRefusalCode.SPREAD_LIMIT)
        if context.expected_slippage_bps > self.limits.max_slippage_bps:
            refusals.append(RiskRefusalCode.SLIPPAGE_LIMIT)
        divergence_bps = (
            Decimal(10_000)
            * abs(context.mark_price - context.oracle_price)
            / context.oracle_price
        )
        if divergence_bps > self.limits.max_oracle_mark_divergence_bps:
            refusals.append(RiskRefusalCode.ORACLE_MARK_DIVERGENCE)
        if context.snapshot.open_position_market is not None:
            refusals.append(RiskRefusalCode.POSITION_ALREADY_OPEN)
        if context.snapshot.daily_trade_count >= self.limits.max_daily_trades:
            refusals.append(RiskRefusalCode.DAILY_TRADE_LIMIT)
        if -context.snapshot.daily_realized_r >= self.limits.max_daily_loss_r:
            refusals.append(RiskRefusalCode.DAILY_LOSS_LIMIT)
        if (
            context.snapshot.cooldown_until is not None
            and context.observed_at < context.snapshot.cooldown_until
        ):
            refusals.append(RiskRefusalCode.COOLDOWN_ACTIVE)
        if context.orphan_order:
            refusals.append(RiskRefusalCode.ORPHAN_ORDER)
        if context.unknown_fill:
            refusals.append(RiskRefusalCode.UNKNOWN_FILL)
        if context.position_divergence:
            refusals.append(RiskRefusalCode.POSITION_DIVERGENCE)
        if not context.feed_healthy:
            refusals.append(RiskRefusalCode.FEED_UNHEALTHY)
        if position_risk_fraction > self.limits.max_risk_fraction:
            refusals.append(RiskRefusalCode.POSITION_RISK_LIMIT)
        if max(context.leverage, effective_leverage) > self.limits.max_leverage:
            refusals.append(RiskRefusalCode.LEVERAGE_LIMIT)
        if net_reward_risk < self.limits.min_net_reward_risk:
            refusals.append(RiskRefusalCode.NET_REWARD_RISK)

        ordered_refusals = tuple(dict.fromkeys(refusals))
        return RiskDecision(
            intent_id=intent.intent_id,
            limits_sha256=self.limits.limits_id,
            checked_at=context.observed_at,
            accepted=not ordered_refusals,
            refusal_codes=ordered_refusals,
            gross_reward_bps=gross_reward_bps,
            gross_stop_bps=gross_stop_bps,
            net_reward_risk=net_reward_risk,
            position_risk_fraction=position_risk_fraction,
        )


def apply_risk_decision(
    snapshot: RiskSnapshot,
    intent: TradeIntent,
    decision: RiskDecision,
) -> RiskSnapshot:
    """Record one reviewed intent idempotently in an immutable daily ledger."""

    if decision.intent_id != intent.intent_id:
        raise RiskContractError("risk decision does not match its intent")
    if intent.intent_id in snapshot.processed_intent_ids:
        return snapshot
    processed = snapshot.processed_intent_ids + (intent.intent_id,)
    if not decision.accepted:
        return RiskSnapshot(
            trading_day=snapshot.trading_day,
            processed_intent_ids=processed,
            open_position_market=snapshot.open_position_market,
            daily_trade_count=snapshot.daily_trade_count,
            daily_realized_r=snapshot.daily_realized_r,
            cooldown_until=snapshot.cooldown_until,
        )
    if snapshot.open_position_market is not None:
        raise RiskContractError("cannot accept an intent while a position is open")
    return RiskSnapshot(
        trading_day=snapshot.trading_day,
        processed_intent_ids=processed,
        open_position_market=intent.market,
        daily_trade_count=snapshot.daily_trade_count + 1,
        daily_realized_r=snapshot.daily_realized_r,
        cooldown_until=snapshot.cooldown_until,
    )


def close_risk_position(
    snapshot: RiskSnapshot,
    *,
    market: str,
    realized_r: Decimal,
    cooldown_until: datetime | None = None,
) -> RiskSnapshot:
    """Close the risk ledger position without changing historical intent ids."""

    if snapshot.open_position_market != market:
        raise RiskContractError("risk position close does not match the open market")
    if not realized_r.is_finite():
        raise RiskContractError("realized_r must be finite")
    if cooldown_until is not None:
        _require_utc(cooldown_until, "cooldown_until")
    return RiskSnapshot(
        trading_day=snapshot.trading_day,
        processed_intent_ids=snapshot.processed_intent_ids,
        open_position_market=None,
        daily_trade_count=snapshot.daily_trade_count,
        daily_realized_r=snapshot.daily_realized_r + realized_r,
        cooldown_until=cooldown_until,
    )
