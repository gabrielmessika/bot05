from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from bot05.studies import (
    DayBlock,
    GateStatus,
    HoldoutLedger,
    ResearchConclusion,
    ResearchGateInputs,
    SplitRole,
    ValidationContractError,
    bootstrap_expectancy,
    build_ablation_matrix,
    build_anchored_walk_forward,
    build_chronological_split,
    evaluate_research_gates,
)


def _days(count: int) -> tuple[date, ...]:
    start = date(2026, 1, 1)
    return tuple(start + timedelta(days=index) for index in range(count))


def test_chronological_split_preserves_days_and_two_purge_boundaries() -> None:
    split = build_chronological_split(_days(22))

    assert len(split.days(SplitRole.DEVELOPMENT)) == 10
    assert len(split.days(SplitRole.VALIDATION)) == 4
    assert len(split.days(SplitRole.HOLDOUT)) == 6
    assert len(split.days(SplitRole.PURGE)) == 2
    assert len(split.split_id) == 64


def test_anchored_walk_forward_expands_training_over_three_folds() -> None:
    folds = build_anchored_walk_forward(_days(20))

    assert len(folds) == 3
    assert [len(item.training_days) for item in folds] == [5, 10, 15]
    assert all(len(item.purge_days) == 1 for item in folds)
    assert all(len(item.evaluation_days) == 4 for item in folds)


def test_holdout_immutable_ledger_refuses_second_access() -> None:
    split = build_chronological_split(_days(22))
    ledger, access = HoldoutLedger().open_once(
        split,
        experiment_id="a" * 64,
        strategy_spec_id="b" * 64,
        opened_at=datetime(2026, 2, 1, tzinfo=UTC),
    )

    assert access.split_id == split.split_id
    with pytest.raises(ValidationContractError, match="already"):
        ledger.open_once(
            split,
            experiment_id="a" * 64,
            strategy_spec_id="b" * 64,
            opened_at=datetime(2026, 2, 2, tzinfo=UTC),
        )


def test_day_block_bootstrap_is_deterministic() -> None:
    blocks = tuple(
        DayBlock(day, (Decimal(index), Decimal(index) / Decimal(2)))
        for index, day in enumerate(_days(5), 1)
    )

    first = bootstrap_expectancy(blocks, sample_count=500, seed=42)
    second = bootstrap_expectancy(blocks, sample_count=500, seed=42)

    assert first == second
    assert first.lower_95 <= first.point_estimate <= first.upper_95
    assert len(first.input_sha256) == 64


def test_ablation_matrix_contains_all_27_primary_variants_and_controls() -> None:
    matrix = build_ablation_matrix(
        market="BTC",
        session_id="us_cash_open",
        config_sha256="c" * 64,
        calendar_version="calendar:test",
        code_version="validation-test",
        random_open_seed=7,
    )

    assert len(matrix.strategy_specs) == 27
    assert len(matrix.controls) == 13
    assert len(matrix.matrix_id) == 64


def _passing_gate_inputs() -> ResearchGateInputs:
    return ResearchGateInputs(
        logic_tests_passed=True,
        parity_acceptable=True,
        critical_trade_gap_count=0,
        all_variants_published=True,
        holdout_access_count=1,
        oos_trade_count=120,
        claimed_market_trade_counts=(("BTC", 60), ("HYPE", 60)),
        net_profit_factor=Decimal("1.25"),
        fold_expectancies_r=(
            Decimal("0.1"),
            Decimal("0.2"),
            Decimal("0.05"),
        ),
        holdout_expectancy_r=Decimal("0.1"),
        bootstrap_lower_95_r=Decimal("0.01"),
        max_market_or_direction_pnl_contribution=Decimal("0.4"),
        top_five_pnl_contribution=Decimal("0.35"),
        stress_expectancy_r=Decimal(0),
        max_drawdown_r=Decimal("9.9"),
        true_open_expectancy_r=Decimal("0.2"),
        pseudo_open_expectancy_r=Decimal("0.1"),
        setup_expectancy_r=Decimal("0.2"),
        naive_drive_expectancy_r=Decimal("0.1"),
    )


def test_research_gates_pass_only_when_every_section_8_1_gate_passes() -> None:
    evaluation = evaluate_research_gates(_passing_gate_inputs())

    assert evaluation.conclusion is ResearchConclusion.OOS_CANDIDATE
    assert all(item.status is GateStatus.PASS for item in evaluation.results)


def test_research_gates_call_a_small_sample_insufficient_not_validated() -> None:
    passing = _passing_gate_inputs()
    inputs = replace(
        passing,
        oos_trade_count=1,
        claimed_market_trade_counts=(("BTC", 1),),
    )

    evaluation = evaluate_research_gates(inputs)

    assert evaluation.conclusion is ResearchConclusion.DATA_INSUFFICIENT
    sample_gate = next(
        item for item in evaluation.results if item.name == "oos_sample_size"
    )
    assert sample_gate.status is GateStatus.INSUFFICIENT
