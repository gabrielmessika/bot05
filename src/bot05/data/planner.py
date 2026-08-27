"""Local-first acquisition planning; this module performs no network calls."""

from __future__ import annotations

from collections.abc import Iterable

from bot05.data.contracts import (
    AcquisitionAction,
    DataAsset,
    DataRequirement,
    LocalInventory,
    Qualification,
    RequirementPlan,
    TimeRange,
)


def merge_ranges(ranges: Iterable[TimeRange]) -> tuple[TimeRange, ...]:
    """Merge overlapping or adjacent half-open intervals deterministically."""

    ordered = sorted(ranges)
    if not ordered:
        return ()
    merged: list[TimeRange] = [ordered[0]]
    for current in ordered[1:]:
        previous = merged[-1]
        if current.start_ms <= previous.end_ms:
            merged[-1] = TimeRange(
                previous.start_ms, max(previous.end_ms, current.end_ms)
            )
        else:
            merged.append(current)
    return tuple(merged)


def subtract_ranges(
    base: TimeRange, covered: Iterable[TimeRange]
) -> tuple[TimeRange, ...]:
    """Return the exact portions of base not covered by the supplied intervals."""

    cursor = base.start_ms
    missing: list[TimeRange] = []
    for span in merge_ranges(covered):
        if span.end_ms <= cursor or span.start_ms >= base.end_ms:
            continue
        clipped_start = max(span.start_ms, base.start_ms)
        clipped_end = min(span.end_ms, base.end_ms)
        if clipped_start > cursor:
            missing.append(TimeRange(cursor, clipped_start))
        cursor = max(cursor, clipped_end)
        if cursor >= base.end_ms:
            break
    if cursor < base.end_ms:
        missing.append(TimeRange(cursor, base.end_ms))
    return tuple(missing)


def _support(asset: DataAsset, requirement: DataRequirement) -> tuple[bool, str | None]:
    if requirement.market not in asset.markets:
        return False, None
    if requirement.channel in asset.channels:
        return True, None
    if (
        requirement.channel in {"candles_1m", "candles_5m"}
        and "trades" in asset.channels
    ):
        return True, f"{requirement.channel}_from_trades"
    return False, None


def _overlaps(asset: DataAsset, requirement: DataRequirement) -> bool:
    return any(
        span.start_ms < requirement.coverage.end_ms
        and span.end_ms > requirement.coverage.start_ms
        for span in asset.coverage
    )


def plan_requirement(
    inventory: LocalInventory,
    requirement: DataRequirement,
    *,
    remote_fetch_enabled: bool,
) -> RequirementPlan:
    """Plan reuse, qualification and only then remote acquisition for one need."""

    qualified: list[DataAsset] = []
    candidates: list[DataAsset] = []
    derivations: set[str] = set()
    for asset in inventory.assets:
        supported, derivation = _support(asset, requirement)
        if not supported or not _overlaps(asset, requirement):
            continue
        if derivation is not None:
            derivations.add(derivation)
        if asset.qualification is Qualification.QUALIFIED:
            qualified.append(asset)
        else:
            candidates.append(asset)

    qualified_ranges = [span for asset in qualified for span in asset.coverage]
    after_qualified = subtract_ranges(requirement.coverage, qualified_ranges)
    needed_candidates = [
        asset
        for asset in candidates
        if any(
            span.start_ms < remaining.end_ms and span.end_ms > remaining.start_ms
            for span in asset.coverage
            for remaining in after_qualified
        )
    ]
    candidate_ranges = [span for asset in needed_candidates for span in asset.coverage]
    remote_ranges: list[TimeRange] = []
    for remaining in after_qualified:
        remote_ranges.extend(subtract_ranges(remaining, candidate_ranges))

    if not remote_ranges and not needed_candidates:
        action = AcquisitionAction.REUSE_LOCAL
    elif not remote_ranges:
        action = AcquisitionAction.QUALIFY_LOCAL
    elif not remote_fetch_enabled and needed_candidates:
        action = AcquisitionAction.QUALIFY_LOCAL_REMOTE_DISABLED
    elif needed_candidates:
        action = AcquisitionAction.QUALIFY_THEN_FETCH_GAPS
    elif remote_fetch_enabled:
        action = AcquisitionAction.FETCH_MISSING
    else:
        action = AcquisitionAction.REMOTE_FETCH_DISABLED

    return RequirementPlan(
        requirement=requirement,
        action=action,
        reusable_dataset_ids=tuple(sorted(asset.dataset_id for asset in qualified)),
        qualification_dataset_ids=tuple(
            sorted(asset.dataset_id for asset in needed_candidates)
        ),
        remote_fetch_ranges=merge_ranges(remote_ranges),
        derivations=tuple(sorted(derivations)),
    )


def plan_inventory(
    inventory: LocalInventory,
    requirements: Iterable[DataRequirement],
    *,
    remote_fetch_enabled: bool,
) -> tuple[RequirementPlan, ...]:
    return tuple(
        plan_requirement(
            inventory,
            requirement,
            remote_fetch_enabled=remote_fetch_enabled,
        )
        for requirement in requirements
    )
