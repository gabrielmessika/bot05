"""Immutable contracts for preregistered, evidence-separated market studies."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from bot05.data.contracts import EvidenceTier, Qualification
from bot05.features.opening_drive import DriveDirection
from bot05.models import Candle, encode_domain_record
from bot05.replay import ReplayModel
from bot05.strategy import TradeIntent

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
PRIMARY_MARKETS = frozenset({"xyz:GOLD", "xyz:SILVER", "xyz:SP500", "HYPE", "BTC"})
CONTROL_MARKETS = frozenset({"ETH", "SOL"})
MFE_HORIZONS_MINUTES = (15, 30, 60, 120)


class StudyContractError(ValueError):
    """Raised when study inputs could mix evidence or hide missing coverage."""


class MarketRole(StrEnum):
    PRIMARY = "primary"
    CONTROL = "control"


class StudyPurpose(StrEnum):
    ALPHA_STRUCTURE = "alpha_structure"
    SIGNAL_PARITY = "signal_parity"
    EXECUTION_REPLAY = "execution_replay"
    CAUSAL_EXECUTION = "causal_execution"


class StudyConclusion(StrEnum):
    DESCRIPTIVE_ONLY = "descriptive_only"
    DATA_INSUFFICIENT = "data_insufficient"
    RESEARCH_ONLY = "research_only"


def _require_utc(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise StudyContractError(f"{name} must be timezone-aware UTC")


def _require_sha256(value: str, name: str) -> None:
    if _SHA256.fullmatch(value) is None:
        raise StudyContractError(f"{name} must be a SHA-256 digest")


def _positive(value: Decimal, name: str, *, allow_zero: bool = False) -> None:
    if not value.is_finite() or value < 0 or (not allow_zero and value == 0):
        qualifier = "non-negative" if allow_zero else "positive"
        raise StudyContractError(f"{name} must be finite and {qualifier}")


def market_role(market: str) -> MarketRole:
    if market in PRIMARY_MARKETS:
        return MarketRole.PRIMARY
    if market in CONTROL_MARKETS:
        return MarketRole.CONTROL
    raise StudyContractError(f"market {market!r} is outside the D5 universe")


@dataclass(frozen=True, slots=True)
class ExperimentSpec:
    """Preregistered D5 experiment identity, before inspecting result PnL."""

    name: str
    preregistered_at: datetime
    universe: tuple[str, ...]
    sessions: tuple[str, ...]
    strategy_spec_ids: tuple[str, ...]
    replay_models: tuple[ReplayModel, ...]
    excursion_horizons_minutes: tuple[int, ...]
    selector_enabled: bool
    calendar_versions: tuple[str, ...]
    config_sha256: str
    code_version: str

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.code_version.strip():
            raise StudyContractError("experiment name and code version are required")
        _require_utc(self.preregistered_at, "preregistered_at")
        for name, values in (
            ("universe", self.universe),
            ("sessions", self.sessions),
            ("strategy_spec_ids", self.strategy_spec_ids),
            ("replay_models", self.replay_models),
            ("calendar_versions", self.calendar_versions),
        ):
            if not values or len(set(values)) != len(values):
                raise StudyContractError(f"{name} must be non-empty and unique")
        if any(not item.strip() for item in self.universe + self.sessions):
            raise StudyContractError("experiment universe and sessions cannot be blank")
        for market in self.universe:
            market_role(market)
        for digest in self.strategy_spec_ids:
            _require_sha256(digest, "strategy_spec_id")
        if set(self.replay_models) != set(ReplayModel):
            raise StudyContractError(
                "experiment must preregister all four replay models"
            )
        if self.excursion_horizons_minutes != MFE_HORIZONS_MINUTES:
            raise StudyContractError("experiment MFE/MAE horizons must be 15/30/60/120")
        if self.selector_enabled:
            raise StudyContractError("portfolio selector remains disabled during D5")
        if any(not item.strip() for item in self.calendar_versions):
            raise StudyContractError("calendar versions cannot be blank")
        _require_sha256(self.config_sha256, "experiment config_sha256")

    @property
    def spec_sha256(self) -> str:
        payload = {
            "calendar_versions": list(self.calendar_versions),
            "code_version": self.code_version,
            "config_sha256": self.config_sha256,
            "excursion_horizons_minutes": list(self.excursion_horizons_minutes),
            "name": self.name,
            "preregistered_at": self.preregistered_at.isoformat(),
            "replay_models": [item.value for item in self.replay_models],
            "selector_enabled": self.selector_enabled,
            "sessions": list(self.sessions),
            "strategy_spec_ids": list(self.strategy_spec_ids),
            "universe": list(self.universe),
        }
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class StudyDataset:
    """Small manifest identity used by a study; raw records remain out of reports."""

    dataset_id: str
    canonical_market: str
    source_instrument: str
    tier: EvidenceTier
    qualification: Qualification
    channels: tuple[str, ...]
    period_start: datetime
    period_end: datetime
    record_count: int
    critical_gap_count: int
    raw_sha256: str
    manifest_sha256: str
    derived_sha256: str
    adapter_version: str
    transformations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name, value in (
            ("dataset_id", self.dataset_id),
            ("canonical_market", self.canonical_market),
            ("source_instrument", self.source_instrument),
            ("adapter_version", self.adapter_version),
        ):
            if not value.strip():
                raise StudyContractError(f"{name} is required")
        market_role(self.canonical_market)
        if not self.channels or len(set(self.channels)) != len(self.channels):
            raise StudyContractError("dataset channels must be non-empty and unique")
        _require_utc(self.period_start, "dataset period_start")
        _require_utc(self.period_end, "dataset period_end")
        if self.period_start >= self.period_end:
            raise StudyContractError("dataset period must be non-empty")
        if self.record_count < 0 or self.critical_gap_count < 0:
            raise StudyContractError("dataset counts must be non-negative")
        if self.qualification is Qualification.QUALIFIED and self.record_count == 0:
            raise StudyContractError("qualified dataset cannot be empty")
        for name, digest in (
            ("raw_sha256", self.raw_sha256),
            ("manifest_sha256", self.manifest_sha256),
            ("derived_sha256", self.derived_sha256),
        ):
            _require_sha256(digest, name)
        if len(set(self.transformations)) != len(self.transformations):
            raise StudyContractError("dataset transformations must be unique")

    def payload(self) -> dict[str, object]:
        return {
            "adapter_version": self.adapter_version,
            "canonical_market": self.canonical_market,
            "channels": list(self.channels),
            "critical_gap_count": self.critical_gap_count,
            "dataset_id": self.dataset_id,
            "derived_sha256": self.derived_sha256,
            "manifest_sha256": self.manifest_sha256,
            "period_end": self.period_end.isoformat(),
            "period_start": self.period_start.isoformat(),
            "qualification": self.qualification.value,
            "raw_sha256": self.raw_sha256,
            "record_count": self.record_count,
            "source_instrument": self.source_instrument,
            "tier": self.tier.value,
            "transformations": list(self.transformations),
        }


@dataclass(frozen=True, slots=True)
class MarketStudyScope:
    canonical_market: str
    source_instrument: str
    session_id: str
    tier: EvidenceTier
    replay_model: ReplayModel

    def __post_init__(self) -> None:
        market_role(self.canonical_market)
        if not self.source_instrument.strip() or not self.session_id.strip():
            raise StudyContractError("study instrument and session are required")


@dataclass(frozen=True, slots=True)
class SessionObservation:
    session_key: str
    canonical_market: str
    session_id: str
    t0: datetime
    complete: bool
    session_eligible: bool
    drive: bool
    pullback: bool
    confirmation: bool
    economic_gate: bool
    trade: bool
    direction: DriveDirection | None
    intent_id: str | None
    rejection_reason: str | None
    source_data_sha256: str

    def __post_init__(self) -> None:
        if not self.session_key.strip() or not self.canonical_market.strip():
            raise StudyContractError("session key and market are required")
        if not self.session_id.strip():
            raise StudyContractError("session id is required")
        _require_utc(self.t0, "session t0")
        _require_sha256(self.source_data_sha256, "session source_data_sha256")
        stages = (
            self.complete,
            self.session_eligible,
            self.drive,
            self.pullback,
            self.confirmation,
            self.economic_gate,
            self.trade,
        )
        if any(
            stages[index] and not stages[index - 1] for index in range(1, len(stages))
        ):
            raise StudyContractError("session funnel stages must be monotonic")
        if self.drive != (self.direction is not None):
            raise StudyContractError("drive stage and direction must agree")
        if self.trade != (self.intent_id is not None):
            raise StudyContractError("trade stage and intent id must agree")
        if self.trade == (self.rejection_reason is not None):
            raise StudyContractError("trade outcome and rejection reason disagree")


@dataclass(frozen=True, slots=True)
class ExcursionHorizon:
    minutes: int
    complete: bool
    mfe_bps: Decimal | None
    mae_bps: Decimal | None
    mfe_r: Decimal | None
    mae_r: Decimal | None

    def __post_init__(self) -> None:
        if self.minutes not in MFE_HORIZONS_MINUTES:
            raise StudyContractError("unsupported MFE/MAE horizon")
        values = (self.mfe_bps, self.mae_bps, self.mfe_r, self.mae_r)
        if self.complete != all(value is not None for value in values):
            raise StudyContractError("excursion completeness and values disagree")
        for value in values:
            if value is not None:
                _positive(value, "excursion value", allow_zero=True)


@dataclass(frozen=True, slots=True)
class ExcursionObservation:
    intent_id: str
    canonical_market: str
    session_id: str
    direction: DriveDirection
    start_at: datetime
    reference_price: Decimal
    structural_risk: Decimal
    replay_data_sha256: str
    horizons: tuple[ExcursionHorizon, ...]

    def __post_init__(self) -> None:
        if not self.intent_id.strip() or not self.canonical_market.strip():
            raise StudyContractError("excursion intent and market are required")
        if not self.session_id.strip():
            raise StudyContractError("excursion session is required")
        _require_utc(self.start_at, "excursion start_at")
        _positive(self.reference_price, "excursion reference_price")
        _positive(self.structural_risk, "excursion structural_risk")
        _require_sha256(self.replay_data_sha256, "excursion replay_data_sha256")
        if tuple(item.minutes for item in self.horizons) != MFE_HORIZONS_MINUTES:
            raise StudyContractError("excursion horizons must be ordered 15/30/60/120")


def _complete_window(
    candles: tuple[Candle, ...], start: datetime, end: datetime
) -> bool:
    if not candles or candles[0].open_time != start or candles[-1].close_time != end:
        return False
    return all(
        left.close_time == right.open_time
        and left.interval_seconds == right.interval_seconds
        for left, right in zip(candles, candles[1:], strict=False)
    )


def calculate_excursions(
    intent: TradeIntent,
    candles: tuple[Candle, ...],
    *,
    horizons_minutes: tuple[int, ...] = MFE_HORIZONS_MINUTES,
) -> ExcursionObservation:
    """Measure post-confirmation excursions without consulting the intent target."""

    if horizons_minutes != MFE_HORIZONS_MINUTES:
        raise StudyContractError("MFE/MAE horizons must remain preregistered")
    ordered = tuple(sorted(candles, key=lambda item: item.open_time))
    if len({item.open_time for item in ordered}) != len(ordered):
        raise StudyContractError("excursion candles contain duplicate opens")
    if any(item.market != intent.market for item in ordered):
        raise StudyContractError("excursion candle market disagrees with intent")
    candidates = tuple(
        item for item in ordered if item.open_time >= intent.confirmation_time
    )
    if not candidates:
        raise StudyContractError("excursion requires a next-open candle")
    start_at = candidates[0].open_time
    if candidates[0].open != intent.entry_price:
        raise StudyContractError("excursion next-open disagrees with intent entry")
    structural_risk = abs(intent.entry_price - intent.stop_price)
    digest = hashlib.sha256()
    for candle in ordered:
        digest.update(encode_domain_record(candle))
        digest.update(b"\n")
    horizons: list[ExcursionHorizon] = []
    for minutes in horizons_minutes:
        end_at = start_at + timedelta(minutes=minutes)
        window = tuple(
            item
            for item in candidates
            if item.open_time >= start_at and item.close_time <= end_at
        )
        if not _complete_window(window, start_at, end_at):
            horizons.append(ExcursionHorizon(minutes, False, None, None, None, None))
            continue
        highest = max(item.high for item in window)
        lowest = min(item.low for item in window)
        if intent.direction is DriveDirection.LONG:
            favorable = max(highest - intent.entry_price, Decimal(0))
            adverse = max(intent.entry_price - lowest, Decimal(0))
        else:
            favorable = max(intent.entry_price - lowest, Decimal(0))
            adverse = max(highest - intent.entry_price, Decimal(0))
        horizons.append(
            ExcursionHorizon(
                minutes=minutes,
                complete=True,
                mfe_bps=Decimal(10_000) * favorable / intent.entry_price,
                mae_bps=Decimal(10_000) * adverse / intent.entry_price,
                mfe_r=favorable / structural_risk,
                mae_r=adverse / structural_risk,
            )
        )
    return ExcursionObservation(
        intent_id=intent.intent_id,
        canonical_market=intent.market,
        session_id=intent.session_id,
        direction=intent.direction,
        start_at=start_at,
        reference_price=intent.entry_price,
        structural_risk=structural_risk,
        replay_data_sha256=digest.hexdigest(),
        horizons=tuple(horizons),
    )


def allowed_purposes(tier: EvidenceTier) -> tuple[StudyPurpose, ...]:
    if tier is EvidenceTier.UNDERLYING:
        return (StudyPurpose.ALPHA_STRUCTURE,)
    if tier is EvidenceTier.HYPERLIQUID_CANDLES:
        return (StudyPurpose.SIGNAL_PARITY,)
    if tier is EvidenceTier.HYPERLIQUID_ARCHIVE:
        return (StudyPurpose.SIGNAL_PARITY, StudyPurpose.EXECUTION_REPLAY)
    if tier is EvidenceTier.BOT05_COLLECTOR:
        return (
            StudyPurpose.SIGNAL_PARITY,
            StudyPurpose.EXECUTION_REPLAY,
            StudyPurpose.CAUSAL_EXECUTION,
        )
    return (StudyPurpose.ALPHA_STRUCTURE,)
