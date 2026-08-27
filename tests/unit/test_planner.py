from __future__ import annotations

from pathlib import Path

from bot05.data.contracts import (
    AcquisitionAction,
    DataAsset,
    DataRequirement,
    EvidenceTier,
    LocalInventory,
    Qualification,
    SourceProject,
    TimeRange,
)
from bot05.data.planner import merge_ranges, plan_requirement, subtract_ranges


def _asset(
    dataset_id: str,
    coverage: TimeRange,
    qualification: Qualification,
    *,
    channels: tuple[str, ...] = ("trades",),
) -> DataAsset:
    return DataAsset(
        dataset_id=dataset_id,
        source_project=SourceProject.HYPERBOT,
        tier=EvidenceTier.HYPERLIQUID_ARCHIVE,
        path=Path(f"/shared/{dataset_id}.json"),
        markets=("BTC",),
        channels=channels,
        coverage=(coverage,),
        provenance_sha256="a" * 64,
        qualification=qualification,
        quality_flags=("test",),
    )


def test_range_math_is_half_open_and_deterministic() -> None:
    assert merge_ranges([TimeRange(10, 20), TimeRange(20, 30), TimeRange(5, 8)]) == (
        TimeRange(5, 8),
        TimeRange(10, 30),
    )
    assert subtract_ranges(
        TimeRange(0, 100), [TimeRange(20, 40), TimeRange(60, 80)]
    ) == (
        TimeRange(0, 20),
        TimeRange(40, 60),
        TimeRange(80, 100),
    )


def test_candidate_is_qualified_before_only_true_gaps_are_fetched() -> None:
    inventory = LocalInventory(
        assets=(_asset("candidate", TimeRange(20, 80), Qualification.CANDIDATE),),
        issues=(),
    )
    requirement = DataRequirement("BTC", "trades", TimeRange(0, 100))

    plan = plan_requirement(inventory, requirement, remote_fetch_enabled=True)

    assert plan.action is AcquisitionAction.QUALIFY_THEN_FETCH_GAPS
    assert plan.qualification_dataset_ids == ("candidate",)
    assert plan.remote_fetch_ranges == (TimeRange(0, 20), TimeRange(80, 100))


def test_remote_disabled_is_explicit_even_with_local_candidates() -> None:
    inventory = LocalInventory(
        assets=(_asset("candidate", TimeRange(20, 80), Qualification.CANDIDATE),),
        issues=(),
    )
    requirement = DataRequirement("BTC", "trades", TimeRange(0, 100))

    plan = plan_requirement(inventory, requirement, remote_fetch_enabled=False)

    assert plan.action is AcquisitionAction.QUALIFY_LOCAL_REMOTE_DISABLED
    assert plan.remote_fetch_ranges == (TimeRange(0, 20), TimeRange(80, 100))


def test_trade_data_is_candidate_input_for_causal_candle_derivation() -> None:
    inventory = LocalInventory(
        assets=(_asset("trades", TimeRange(0, 100), Qualification.CANDIDATE),),
        issues=(),
    )
    requirement = DataRequirement("BTC", "candles_5m", TimeRange(0, 100))

    plan = plan_requirement(inventory, requirement, remote_fetch_enabled=True)

    assert plan.action is AcquisitionAction.QUALIFY_LOCAL
    assert plan.derivations == ("candles_5m_from_trades",)
    assert plan.remote_fetch_ranges == ()


def test_fully_qualified_coverage_does_not_require_irrelevant_candidate() -> None:
    inventory = LocalInventory(
        assets=(
            _asset("qualified", TimeRange(0, 100), Qualification.QUALIFIED),
            _asset("duplicate", TimeRange(0, 100), Qualification.CANDIDATE),
        ),
        issues=(),
    )
    requirement = DataRequirement("BTC", "trades", TimeRange(0, 100))

    plan = plan_requirement(inventory, requirement, remote_fetch_enabled=True)

    assert plan.action is AcquisitionAction.REUSE_LOCAL
    assert plan.reusable_dataset_ids == ("qualified",)
    assert plan.qualification_dataset_ids == ()
