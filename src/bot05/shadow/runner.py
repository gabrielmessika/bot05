"""Pure shadow runner that observes executable prices but cannot send anything."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from bot05.collector import FeedHealthDecision
from bot05.features import DriveDirection
from bot05.models import BookSnapshot
from bot05.replay import OrderSide
from bot05.replay.costs import round_quantity, sweep_book
from bot05.risk import RiskContext, RiskDecision, RiskSupervisor
from bot05.strategy import TradeIntent

_SHA256 = re.compile(r"^[a-f0-9]{64}$")


class ShadowContractError(ValueError):
    """Raised when a shadow-only transition is incomplete or unsafe."""


class ShadowStatus(StrEnum):
    OBSERVED = "observed"
    REFUSED = "refused"
    KILLED = "killed"


class ShadowRefusalCode(StrEnum):
    FEED_UNHEALTHY = "feed_unhealthy"
    QUOTE_BEFORE_DECISION = "quote_before_decision"
    QUOTE_SCOPE_MISMATCH = "quote_scope_mismatch"
    INSUFFICIENT_DEPTH = "insufficient_depth"
    KILL_SWITCH_LATCHED = "kill_switch_latched"


def _require_utc(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ShadowContractError(f"{name} must be timezone-aware UTC")


@dataclass(frozen=True, slots=True)
class ShadowQuote:
    market: str
    observed_at: datetime
    side: OrderSide
    quantity: Decimal
    executable_price: Decimal
    benchmark_price: Decimal
    spread_bps: Decimal
    impact_bps: Decimal
    book_levels_consumed: int
    book_sha256: str


@dataclass(frozen=True, slots=True)
class ShadowDecision:
    intent_id: str
    checked_at: datetime
    status: ShadowStatus
    risk_decision: RiskDecision
    quote: ShadowQuote | None
    refusal_codes: tuple[ShadowRefusalCode, ...]

    def __post_init__(self) -> None:
        _require_utc(self.checked_at, "shadow checked_at")
        if self.status is ShadowStatus.OBSERVED:
            if (
                self.quote is None
                or self.refusal_codes
                or not self.risk_decision.accepted
            ):
                raise ShadowContractError("observed shadow decision is inconsistent")
        elif self.quote is not None or not self.refusal_codes:
            raise ShadowContractError("refused shadow decision is inconsistent")


@dataclass(frozen=True, slots=True)
class ShadowLedger:
    processed_intent_ids: tuple[str, ...] = ()
    theoretical_open_market: str | None = None
    killed: bool = False


@dataclass(frozen=True, slots=True)
class ShadowReconciliation:
    expected_intent_ids: tuple[str, ...]
    replay_intent_ids: tuple[str, ...]
    missing_intent_ids: tuple[str, ...]
    unexpected_intent_ids: tuple[str, ...]

    @property
    def matches(self) -> bool:
        return not self.missing_intent_ids and not self.unexpected_intent_ids


@dataclass(frozen=True, slots=True)
class KillSwitch:
    latched: bool = False
    reasons: tuple[str, ...] = ()
    latched_at: datetime | None = None
    incident_id: str | None = None

    def __post_init__(self) -> None:
        if self.latched:
            if not self.reasons or self.latched_at is None or self.incident_id is None:
                raise ShadowContractError("latched kill switch requires incident facts")
            _require_utc(self.latched_at, "kill switch latched_at")
            if _SHA256.fullmatch(self.incident_id) is None:
                raise ShadowContractError("incident_id must be a SHA-256")
        elif (
            self.reasons or self.latched_at is not None or self.incident_id is not None
        ):
            raise ShadowContractError("open kill switch cannot retain incident facts")


@dataclass(frozen=True, slots=True)
class RecoveryApproval:
    incident_id: str
    operator: str
    evidence_sha256: str
    approved_at: datetime

    def __post_init__(self) -> None:
        if (
            _SHA256.fullmatch(self.incident_id) is None
            or _SHA256.fullmatch(self.evidence_sha256) is None
        ):
            raise ShadowContractError("recovery approval hashes are invalid")
        if not self.operator.strip():
            raise ShadowContractError("recovery operator is required")
        _require_utc(self.approved_at, "recovery approved_at")


def observe_executable_quote(
    intent: TradeIntent,
    book: BookSnapshot,
    *,
    requested_quantity: Decimal,
    size_step: Decimal,
) -> ShadowQuote:
    """Calculate a theoretical marketable fill from one public book snapshot."""

    if book.market != intent.market:
        raise ShadowContractError(ShadowRefusalCode.QUOTE_SCOPE_MISMATCH.value)
    if book.received_at < intent.decided_at:
        raise ShadowContractError(ShadowRefusalCode.QUOTE_BEFORE_DECISION.value)
    quantity = round_quantity(requested_quantity, size_step)
    if quantity == 0:
        raise ShadowContractError(ShadowRefusalCode.INSUFFICIENT_DEPTH.value)
    long = intent.direction is DriveDirection.LONG
    side = OrderSide.BUY if long else OrderSide.SELL
    levels = book.asks if long else book.bids
    impact = sweep_book(levels, quantity)
    if impact is None:
        raise ShadowContractError(ShadowRefusalCode.INSUFFICIENT_DEPTH.value)
    benchmark = levels[0].price
    midpoint = (book.bids[0].price + book.asks[0].price) / Decimal(2)
    spread_bps = Decimal(10_000) * (book.asks[0].price - book.bids[0].price) / midpoint
    impact_bps = Decimal(10_000) * (
        (impact.average_price - benchmark) / benchmark
        if long
        else (benchmark - impact.average_price) / benchmark
    )
    payload = json.dumps(
        {
            "asks": [(str(item.price), str(item.size)) for item in book.asks],
            "bids": [(str(item.price), str(item.size)) for item in book.bids],
            "exchange_time": book.exchange_time.isoformat(),
            "received_at": book.received_at.isoformat(),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return ShadowQuote(
        market=book.market,
        observed_at=book.received_at,
        side=side,
        quantity=quantity,
        executable_price=impact.average_price,
        benchmark_price=benchmark,
        spread_bps=spread_bps,
        impact_bps=impact_bps,
        book_levels_consumed=impact.levels_consumed,
        book_sha256=hashlib.sha256(payload).hexdigest(),
    )


def evaluate_shadow_intent(
    intent: TradeIntent,
    context: RiskContext,
    supervisor: RiskSupervisor,
    feed_health: FeedHealthDecision,
    book: BookSnapshot,
    *,
    requested_quantity: Decimal,
    size_step: Decimal,
    kill_switch: KillSwitch,
) -> ShadowDecision:
    """Use the production risk contract and attach only a theoretical quote."""

    if kill_switch.latched:
        risk = supervisor.review(intent, replace(context, feed_healthy=False))
        return ShadowDecision(
            intent.intent_id,
            context.observed_at,
            ShadowStatus.KILLED,
            risk,
            None,
            (ShadowRefusalCode.KILL_SWITCH_LATCHED,),
        )
    feed_is_current = (
        feed_health.healthy and feed_health.checked_at >= intent.decided_at
    )
    effective_context = replace(
        context, feed_healthy=context.feed_healthy and feed_is_current
    )
    risk = supervisor.review(intent, effective_context)
    if not risk.accepted:
        codes = (
            (ShadowRefusalCode.FEED_UNHEALTHY,)
            if not feed_is_current
            else (ShadowRefusalCode.QUOTE_SCOPE_MISMATCH,)
        )
        return ShadowDecision(
            intent.intent_id,
            context.observed_at,
            ShadowStatus.REFUSED,
            risk,
            None,
            codes,
        )
    try:
        quote = observe_executable_quote(
            intent,
            book,
            requested_quantity=requested_quantity,
            size_step=size_step,
        )
    except ShadowContractError as exc:
        try:
            refusal = ShadowRefusalCode(str(exc))
        except ValueError:
            refusal = ShadowRefusalCode.QUOTE_SCOPE_MISMATCH
        return ShadowDecision(
            intent.intent_id,
            context.observed_at,
            ShadowStatus.REFUSED,
            risk,
            None,
            (refusal,),
        )
    return ShadowDecision(
        intent.intent_id,
        context.observed_at,
        ShadowStatus.OBSERVED,
        risk,
        quote,
        (),
    )


def apply_shadow_decision(
    ledger: ShadowLedger, decision: ShadowDecision
) -> ShadowLedger:
    if decision.intent_id in ledger.processed_intent_ids:
        return ledger
    market = (
        decision.quote.market
        if decision.status is ShadowStatus.OBSERVED and decision.quote is not None
        else ledger.theoretical_open_market
    )
    return ShadowLedger(
        processed_intent_ids=ledger.processed_intent_ids + (decision.intent_id,),
        theoretical_open_market=market,
        killed=ledger.killed or decision.status is ShadowStatus.KILLED,
    )


def reconcile_shadow(
    expected_intent_ids: tuple[str, ...], replay_intent_ids: tuple[str, ...]
) -> ShadowReconciliation:
    if len(set(expected_intent_ids)) != len(expected_intent_ids) or len(
        set(replay_intent_ids)
    ) != len(replay_intent_ids):
        raise ShadowContractError("reconciliation intent identities must be unique")
    expected = set(expected_intent_ids)
    replayed = set(replay_intent_ids)
    return ShadowReconciliation(
        expected_intent_ids,
        replay_intent_ids,
        tuple(sorted(expected - replayed)),
        tuple(sorted(replayed - expected)),
    )


def latch_kill_switch(
    switch: KillSwitch,
    *,
    feed_health: FeedHealthDecision,
    reconciliation: ShadowReconciliation,
    checked_at: datetime,
) -> KillSwitch:
    _require_utc(checked_at, "kill checked_at")
    if switch.latched:
        return switch
    feed_reasons = (
        tuple(item.value for item in feed_health.codes)
        if feed_health.kill_required
        else ()
    )
    reconciliation_reasons = (
        () if reconciliation.matches else ("shadow_reconciliation_divergence",)
    )
    reasons = (*feed_reasons, *reconciliation_reasons)
    if not reasons:
        return switch
    payload = json.dumps(
        {
            "checked_at": checked_at.isoformat(),
            "market": feed_health.market,
            "reasons": reasons,
            "missing": reconciliation.missing_intent_ids,
            "unexpected": reconciliation.unexpected_intent_ids,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return KillSwitch(
        latched=True,
        reasons=reasons,
        latched_at=checked_at,
        incident_id=hashlib.sha256(payload).hexdigest(),
    )


def reset_kill_switch(
    switch: KillSwitch,
    approval: RecoveryApproval,
    *,
    feed_health: FeedHealthDecision,
    reconciliation: ShadowReconciliation,
) -> KillSwitch:
    """Require explicit operator evidence; recovery is never automatic."""

    if not switch.latched or switch.incident_id != approval.incident_id:
        raise ShadowContractError("recovery does not match a latched incident")
    if not feed_health.healthy or not reconciliation.matches:
        raise ShadowContractError("recovery checks are not clean")
    if switch.latched_at is not None and approval.approved_at <= switch.latched_at:
        raise ShadowContractError("recovery approval must follow the incident")
    return KillSwitch()
