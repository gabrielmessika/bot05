"""Immutable contracts for BOT05 dataset provenance and coverage."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class EvidenceTier(StrEnum):
    """Evidence levels defined by PLAN.md."""

    UNDERLYING = "U"
    HYPERLIQUID_CANDLES = "H0"
    HYPERLIQUID_ARCHIVE = "H1"
    BOT05_COLLECTOR = "H2"
    LEGACY = "L"


class SourceProject(StrEnum):
    BOT05 = "bot05"
    HYPERBOT = "hyperbot"
    TRIDENT = "trident"


class Qualification(StrEnum):
    CANDIDATE = "candidate"
    QUALIFIED = "qualified"


class AcquisitionAction(StrEnum):
    REUSE_LOCAL = "reuse_local"
    QUALIFY_LOCAL = "qualify_local"
    QUALIFY_THEN_FETCH_GAPS = "qualify_then_fetch_gaps"
    QUALIFY_LOCAL_REMOTE_DISABLED = "qualify_local_remote_disabled"
    FETCH_MISSING = "fetch_missing"
    REMOTE_FETCH_DISABLED = "remote_fetch_disabled"


@dataclass(frozen=True, slots=True, order=True)
class TimeRange:
    """Half-open UTC millisecond interval."""

    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        if self.start_ms < 0 or self.start_ms >= self.end_ms:
            raise ValueError("time range must be positive and non-empty")


@dataclass(frozen=True, slots=True)
class DataAsset:
    """One checksummed local dataset or manifest-backed candidate."""

    dataset_id: str
    source_project: SourceProject
    tier: EvidenceTier
    path: Path
    markets: tuple[str, ...]
    channels: tuple[str, ...]
    coverage: tuple[TimeRange, ...]
    provenance_sha256: str
    qualification: Qualification
    quality_flags: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.dataset_id.strip():
            raise ValueError("dataset_id must not be blank")
        if not self.path.is_absolute():
            raise ValueError("data asset path must be absolute")
        if not self.markets or not self.channels or not self.coverage:
            raise ValueError("data asset markets, channels and coverage are required")
        if len(self.provenance_sha256) != 64:
            raise ValueError("provenance_sha256 must be a SHA-256 hex digest")


@dataclass(frozen=True, slots=True)
class DataRequirement:
    market: str
    channel: str
    coverage: TimeRange

    def __post_init__(self) -> None:
        if not self.market.strip() or not self.channel.strip():
            raise ValueError("requirement market and channel must not be blank")


@dataclass(frozen=True, slots=True)
class RequirementPlan:
    requirement: DataRequirement
    action: AcquisitionAction
    reusable_dataset_ids: tuple[str, ...]
    qualification_dataset_ids: tuple[str, ...]
    remote_fetch_ranges: tuple[TimeRange, ...]
    derivations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class InventoryIssue:
    source: str
    reason: str


@dataclass(frozen=True, slots=True)
class LocalInventory:
    assets: tuple[DataAsset, ...]
    issues: tuple[InventoryIssue, ...]
