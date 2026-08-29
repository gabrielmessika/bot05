"""Chronological splits, anchored walk-forward folds, and one-shot OOS access."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum

_SHA256 = re.compile(r"^[a-f0-9]{64}$")


class ValidationContractError(ValueError):
    """Raised when a validation protocol could leak or reopen holdout data."""


class SplitRole(StrEnum):
    DEVELOPMENT = "development"
    VALIDATION = "validation"
    PURGE = "purge"
    HOLDOUT = "holdout"


def _require_utc(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValidationContractError(f"{name} must be timezone-aware UTC")


@dataclass(frozen=True, slots=True)
class DayAssignment:
    day: date
    role: SplitRole


@dataclass(frozen=True, slots=True)
class ChronologicalSplit:
    assignments: tuple[DayAssignment, ...]
    purge_days: int
    development_fraction: str
    validation_fraction: str
    holdout_fraction: str

    def __post_init__(self) -> None:
        days = tuple(item.day for item in self.assignments)
        if not days or days != tuple(sorted(days)) or len(days) != len(set(days)):
            raise ValidationContractError("split days must be unique and chronological")
        if self.purge_days <= 0:
            raise ValidationContractError("at least one purge day is required")
        roles = tuple(item.role for item in self.assignments)
        for required in (
            SplitRole.DEVELOPMENT,
            SplitRole.VALIDATION,
            SplitRole.HOLDOUT,
        ):
            if required not in roles:
                raise ValidationContractError(f"split is missing {required.value}")
        role_order = {
            SplitRole.DEVELOPMENT: 0,
            SplitRole.PURGE: 1,
            SplitRole.VALIDATION: 2,
            SplitRole.HOLDOUT: 4,
        }
        phase = 0
        purge_runs = 0
        in_purge = False
        seen_validation = False
        for role in roles:
            if role is SplitRole.PURGE:
                if not in_purge:
                    purge_runs += 1
                in_purge = True
                phase += 1 if phase in {0, 2} else 0
                continue
            in_purge = False
            expected = role_order[role]
            if role is SplitRole.VALIDATION:
                seen_validation = True
            if role is SplitRole.HOLDOUT and seen_validation:
                expected = 4
            if expected < phase:
                raise ValidationContractError("split roles are not chronological")
            phase = expected
        if purge_runs != 2:
            raise ValidationContractError("split requires two purge boundaries")

    @property
    def split_id(self) -> str:
        payload = {
            "assignments": [
                {"day": item.day.isoformat(), "role": item.role.value}
                for item in self.assignments
            ],
            "development_fraction": self.development_fraction,
            "holdout_fraction": self.holdout_fraction,
            "purge_days": self.purge_days,
            "validation_fraction": self.validation_fraction,
        }
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        return hashlib.sha256(encoded).hexdigest()

    def days(self, role: SplitRole) -> tuple[date, ...]:
        return tuple(item.day for item in self.assignments if item.role is role)


def build_chronological_split(
    days: tuple[date, ...], *, purge_days: int = 1
) -> ChronologicalSplit:
    """Create 50/20/30 day-grouped splits after reserving boundary purges."""

    if days != tuple(sorted(days)) or len(days) != len(set(days)):
        raise ValidationContractError("input days must be unique and chronological")
    if purge_days <= 0:
        raise ValidationContractError("at least one purge day is required")
    usable = len(days) - 2 * purge_days
    if usable < 10:
        raise ValidationContractError("at least ten non-purge days are required")
    development_count = usable * 50 // 100
    validation_count = usable * 20 // 100
    holdout_count = usable - development_count - validation_count
    if min(development_count, validation_count, holdout_count) <= 0:
        raise ValidationContractError("all split partitions must be non-empty")
    cursor = 0
    assignments: list[DayAssignment] = []
    for role, count in (
        (SplitRole.DEVELOPMENT, development_count),
        (SplitRole.PURGE, purge_days),
        (SplitRole.VALIDATION, validation_count),
        (SplitRole.PURGE, purge_days),
        (SplitRole.HOLDOUT, holdout_count),
    ):
        assignments.extend(
            DayAssignment(day, role) for day in days[cursor : cursor + count]
        )
        cursor += count
    return ChronologicalSplit(
        tuple(assignments),
        purge_days,
        development_fraction="0.50",
        validation_fraction="0.20",
        holdout_fraction="0.30",
    )


@dataclass(frozen=True, slots=True)
class WalkForwardFold:
    index: int
    training_days: tuple[date, ...]
    purge_days: tuple[date, ...]
    evaluation_days: tuple[date, ...]

    def __post_init__(self) -> None:
        if self.index <= 0 or not self.training_days or not self.evaluation_days:
            raise ValidationContractError("walk-forward fold is incomplete")
        combined = (*self.training_days, *self.purge_days, *self.evaluation_days)
        if combined != tuple(sorted(combined)) or len(combined) != len(set(combined)):
            raise ValidationContractError("walk-forward days must be chronological")


def build_anchored_walk_forward(
    days: tuple[date, ...], *, fold_count: int = 3, purge_days: int = 1
) -> tuple[WalkForwardFold, ...]:
    """Build anchored folds with expanding training and disjoint evaluation blocks."""

    if fold_count < 3:
        raise ValidationContractError("at least three walk-forward folds are required")
    if purge_days <= 0:
        raise ValidationContractError("walk-forward purge must be positive")
    if days != tuple(sorted(days)) or len(days) != len(set(days)):
        raise ValidationContractError("walk-forward days must be unique and sorted")
    minimum = (fold_count + 1) * 2 + fold_count * purge_days
    if len(days) < minimum:
        raise ValidationContractError("insufficient days for requested walk-forward")
    evaluation_size = (len(days) - fold_count * purge_days) // (fold_count + 1)
    initial_training_end = len(days) - fold_count * (purge_days + evaluation_size)
    folds: list[WalkForwardFold] = []
    training_end = initial_training_end
    for index in range(1, fold_count + 1):
        purge_end = training_end + purge_days
        evaluation_end = purge_end + evaluation_size
        folds.append(
            WalkForwardFold(
                index=index,
                training_days=days[:training_end],
                purge_days=days[training_end:purge_end],
                evaluation_days=days[purge_end:evaluation_end],
            )
        )
        training_end = evaluation_end
    return tuple(folds)


@dataclass(frozen=True, slots=True)
class HoldoutAccess:
    split_id: str
    experiment_id: str
    strategy_spec_id: str
    opened_at: datetime

    def __post_init__(self) -> None:
        _require_utc(self.opened_at, "holdout opened_at")
        for name, value in (
            ("split_id", self.split_id),
            ("experiment_id", self.experiment_id),
            ("strategy_spec_id", self.strategy_spec_id),
        ):
            if _SHA256.fullmatch(value) is None:
                raise ValidationContractError(f"{name} must be a SHA-256 digest")


@dataclass(frozen=True, slots=True)
class HoldoutLedger:
    accesses: tuple[HoldoutAccess, ...] = ()

    def open_once(
        self,
        split: ChronologicalSplit,
        *,
        experiment_id: str,
        strategy_spec_id: str,
        opened_at: datetime,
    ) -> tuple[HoldoutLedger, HoldoutAccess]:
        """Return a new immutable ledger and reject every second holdout opening."""

        if any(item.split_id == split.split_id for item in self.accesses):
            raise ValidationContractError("holdout has already been opened")
        access = HoldoutAccess(
            split_id=split.split_id,
            experiment_id=experiment_id,
            strategy_spec_id=strategy_spec_id,
            opened_at=opened_at,
        )
        return HoldoutLedger(self.accesses + (access,)), access
