"""Versioned fee, funding, rounding, slippage and book-impact calculations."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from bot05.features.opening_drive import DriveDirection
from bot05.models import BookLevel
from bot05.replay.contracts import LiquidityRole, OrderSide, ReplayContractError

_SHA256 = re.compile(r"^[a-f0-9]{64}$")


def _require_utc(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ReplayContractError(f"{name} must be timezone-aware UTC")


def _finite(value: Decimal, name: str, *, non_negative: bool = False) -> None:
    if not value.is_finite() or (non_negative and value < 0):
        qualifier = " finite and non-negative" if non_negative else " finite"
        raise ReplayContractError(f"{name} must be{qualifier}")


@dataclass(frozen=True, slots=True)
class FeeSnapshot:
    """Explicit effective rates plus the inputs needed to audit their origin."""

    market: str
    effective_at: datetime
    account_tier: str
    base_maker_rate: Decimal
    base_taker_rate: Decimal
    growth_mode: bool
    deployer_fee_scale: Decimal
    staking_discount_rate: Decimal
    referral_discount_rate: Decimal
    builder_fee_rate: Decimal
    effective_maker_rate: Decimal
    effective_taker_rate: Decimal
    source_sha256: str

    def __post_init__(self) -> None:
        if not self.market.strip() or not self.account_tier.strip():
            raise ReplayContractError("fee market and account tier are required")
        _require_utc(self.effective_at, "fee effective_at")
        for name, value in (
            ("base_maker_rate", self.base_maker_rate),
            ("base_taker_rate", self.base_taker_rate),
            ("effective_maker_rate", self.effective_maker_rate),
            ("effective_taker_rate", self.effective_taker_rate),
        ):
            _finite(value, name)
            if value < Decimal("-0.01") or value > Decimal("0.01"):
                raise ReplayContractError(f"{name} is outside the supported fee range")
        for name, value in (
            ("deployer_fee_scale", self.deployer_fee_scale),
            ("staking_discount_rate", self.staking_discount_rate),
            ("referral_discount_rate", self.referral_discount_rate),
            ("builder_fee_rate", self.builder_fee_rate),
        ):
            _finite(value, name, non_negative=True)
        if self.staking_discount_rate > 1 or self.referral_discount_rate > 1:
            raise ReplayContractError("fee discounts cannot exceed one")
        if _SHA256.fullmatch(self.source_sha256) is None:
            raise ReplayContractError("fee source_sha256 must be a SHA-256 digest")

    def effective_rate(self, role: LiquidityRole) -> Decimal:
        if role is LiquidityRole.MAKER:
            return self.effective_maker_rate
        return self.effective_taker_rate

    def canonical_payload(self) -> dict[str, object]:
        return {
            "account_tier": self.account_tier,
            "base_maker_rate": str(self.base_maker_rate),
            "base_taker_rate": str(self.base_taker_rate),
            "builder_fee_rate": str(self.builder_fee_rate),
            "deployer_fee_scale": str(self.deployer_fee_scale),
            "effective_at": self.effective_at.isoformat(),
            "effective_maker_rate": str(self.effective_maker_rate),
            "effective_taker_rate": str(self.effective_taker_rate),
            "growth_mode": self.growth_mode,
            "market": self.market,
            "referral_discount_rate": str(self.referral_discount_rate),
            "source_sha256": self.source_sha256,
            "staking_discount_rate": str(self.staking_discount_rate),
        }


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    snapshots: tuple[FeeSnapshot, ...]

    def __post_init__(self) -> None:
        if not self.snapshots:
            raise ReplayContractError("fee schedule cannot be empty")
        ordered = tuple(
            sorted(self.snapshots, key=lambda item: (item.market, item.effective_at))
        )
        if ordered != self.snapshots:
            raise ReplayContractError("fee snapshots must be sorted")
        identities = {(item.market, item.effective_at) for item in self.snapshots}
        if len(identities) != len(self.snapshots):
            raise ReplayContractError("fee snapshots must have unique effective times")

    @property
    def schedule_sha256(self) -> str:
        encoded = json.dumps(
            [item.canonical_payload() for item in self.snapshots],
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    def snapshot_at(self, market: str, timestamp: datetime) -> FeeSnapshot:
        _require_utc(timestamp, "fee lookup timestamp")
        candidates = tuple(
            item
            for item in self.snapshots
            if item.market == market and item.effective_at <= timestamp
        )
        if not candidates:
            raise ReplayContractError("no effective fee snapshot for fill")
        return candidates[-1]

    def calculate(
        self,
        *,
        market: str,
        timestamp: datetime,
        role: LiquidityRole,
        notional: Decimal,
        multiplier: Decimal,
    ) -> tuple[Decimal, Decimal]:
        _finite(notional, "fee notional", non_negative=True)
        _finite(multiplier, "fee multiplier", non_negative=True)
        rate = self.snapshot_at(market, timestamp).effective_rate(role) * multiplier
        return rate, notional * rate


@dataclass(frozen=True, slots=True)
class FundingEvent:
    market: str
    effective_at: datetime
    rate: Decimal
    mark_price: Decimal
    source_sha256: str

    def __post_init__(self) -> None:
        if not self.market.strip():
            raise ReplayContractError("funding market is required")
        _require_utc(self.effective_at, "funding effective_at")
        _finite(self.rate, "funding rate")
        _finite(self.mark_price, "funding mark_price", non_negative=True)
        if self.mark_price == 0:
            raise ReplayContractError("funding mark_price must be positive")
        if _SHA256.fullmatch(self.source_sha256) is None:
            raise ReplayContractError("funding source_sha256 must be a SHA-256 digest")


def funding_pnl(
    events: tuple[FundingEvent, ...],
    *,
    market: str,
    direction: DriveDirection,
    quantity: Decimal,
    entry_time: datetime,
    exit_time: datetime,
) -> Decimal:
    """Apply funding on boundaries strictly after entry and through exit."""

    if exit_time < entry_time:
        raise ReplayContractError("funding interval is reversed")
    relevant = tuple(
        item
        for item in events
        if item.market == market and entry_time < item.effective_at <= exit_time
    )
    if tuple(sorted(relevant, key=lambda item: item.effective_at)) != relevant:
        raise ReplayContractError("funding events must be sorted")
    direction_sign = Decimal(1) if direction is DriveDirection.LONG else Decimal(-1)
    return sum(
        (-direction_sign * quantity * item.mark_price * item.rate for item in relevant),
        Decimal(0),
    )


def round_quantity(quantity: Decimal, size_step: Decimal) -> Decimal:
    _finite(quantity, "quantity", non_negative=True)
    _finite(size_step, "size_step", non_negative=True)
    if size_step == 0:
        raise ReplayContractError("size_step must be positive")
    units = (quantity / size_step).to_integral_value(rounding=ROUND_FLOOR)
    return units * size_step


def round_price(price: Decimal, price_tick: Decimal, side: OrderSide) -> Decimal:
    """Round against the simulated position: buys up and sells down."""

    _finite(price, "price", non_negative=True)
    _finite(price_tick, "price_tick", non_negative=True)
    if price == 0 or price_tick == 0:
        raise ReplayContractError("price and price_tick must be positive")
    rounding = ROUND_CEILING if side is OrderSide.BUY else ROUND_FLOOR
    units = (price / price_tick).to_integral_value(rounding=rounding)
    return units * price_tick


def apply_adverse_slippage(
    price: Decimal,
    *,
    side: OrderSide,
    slippage_bps: Decimal,
    price_tick: Decimal,
) -> Decimal:
    _finite(slippage_bps, "slippage_bps", non_negative=True)
    fraction = slippage_bps / Decimal(10_000)
    adjusted = price * (
        Decimal(1) + fraction if side is OrderSide.BUY else Decimal(1) - fraction
    )
    return round_price(adjusted, price_tick, side)


@dataclass(frozen=True, slots=True)
class BookImpact:
    average_price: Decimal
    levels_consumed: int


def sweep_book(levels: tuple[BookLevel, ...], quantity: Decimal) -> BookImpact | None:
    """Return full-fill VWAP, or None rather than inventing missing depth."""

    _finite(quantity, "sweep quantity", non_negative=True)
    if quantity == 0:
        raise ReplayContractError("sweep quantity must be positive")
    remaining = quantity
    notional = Decimal(0)
    consumed = 0
    for level in levels:
        take = min(level.size, remaining)
        if take > 0:
            notional += take * level.price
            remaining -= take
            consumed += 1
        if remaining == 0:
            return BookImpact(notional / quantity, consumed)
    return None
