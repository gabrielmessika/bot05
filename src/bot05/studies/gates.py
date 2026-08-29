"""Mechanical evaluation of PLAN.md section 8.1 research promotion gates."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from bot05.studies.validation import ValidationContractError


class GateStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    INSUFFICIENT = "insufficient"


class ResearchConclusion(StrEnum):
    OOS_CANDIDATE = "oos_candidate"
    REJECTED = "rejected"
    DATA_INSUFFICIENT = "data_insufficient"


@dataclass(frozen=True, slots=True)
class GateResult:
    name: str
    status: GateStatus
    observed: str
    requirement: str


@dataclass(frozen=True, slots=True)
class ResearchGateInputs:
    logic_tests_passed: bool
    parity_acceptable: bool
    critical_trade_gap_count: int
    all_variants_published: bool
    holdout_access_count: int
    oos_trade_count: int
    claimed_market_trade_counts: tuple[tuple[str, int], ...]
    net_profit_factor: Decimal | None
    fold_expectancies_r: tuple[Decimal, ...]
    holdout_expectancy_r: Decimal | None
    bootstrap_lower_95_r: Decimal | None
    max_market_or_direction_pnl_contribution: Decimal | None
    top_five_pnl_contribution: Decimal | None
    stress_expectancy_r: Decimal | None
    max_drawdown_r: Decimal | None
    true_open_expectancy_r: Decimal | None
    pseudo_open_expectancy_r: Decimal | None
    setup_expectancy_r: Decimal | None
    naive_drive_expectancy_r: Decimal | None

    def __post_init__(self) -> None:
        if self.critical_trade_gap_count < 0 or self.oos_trade_count < 0:
            raise ValidationContractError("gate counts must be non-negative")
        if self.holdout_access_count < 0:
            raise ValidationContractError("holdout access count must be non-negative")
        markets = tuple(name for name, _ in self.claimed_market_trade_counts)
        if len(markets) != len(set(markets)) or any(
            not name.strip() or count < 0
            for name, count in self.claimed_market_trade_counts
        ):
            raise ValidationContractError("claimed market counts are invalid")


@dataclass(frozen=True, slots=True)
class ResearchGateEvaluation:
    results: tuple[GateResult, ...]
    conclusion: ResearchConclusion


def _boolean(name: str, passed: bool, requirement: str) -> GateResult:
    return GateResult(
        name,
        GateStatus.PASS if passed else GateStatus.FAIL,
        str(passed).lower(),
        requirement,
    )


def _missing_or(
    name: str,
    value: Decimal | None,
    passed: bool,
    requirement: str,
) -> GateResult:
    if value is None:
        return GateResult(name, GateStatus.INSUFFICIENT, "missing", requirement)
    return GateResult(
        name,
        GateStatus.PASS if passed else GateStatus.FAIL,
        str(value),
        requirement,
    )


def evaluate_research_gates(inputs: ResearchGateInputs) -> ResearchGateEvaluation:
    """Evaluate every gate without converting small samples into validation."""

    results: list[GateResult] = [
        _boolean("logic_tests", inputs.logic_tests_passed, "all causal tests pass"),
        _boolean(
            "data_parity",
            inputs.parity_acceptable and inputs.critical_trade_gap_count == 0,
            "acceptable parity and zero critical trade gaps",
        ),
        _boolean(
            "all_variants_published",
            inputs.all_variants_published,
            "publish every preregistered variant",
        ),
        _boolean(
            "single_holdout_access",
            inputs.holdout_access_count == 1,
            "open holdout exactly once",
        ),
    ]
    sample_passed = inputs.oos_trade_count >= 100 and all(
        count >= 30 for _, count in inputs.claimed_market_trade_counts
    )
    results.append(
        GateResult(
            "oos_sample_size",
            GateStatus.PASS if sample_passed else GateStatus.INSUFFICIENT,
            f"total={inputs.oos_trade_count};markets={dict(inputs.claimed_market_trade_counts)}",
            "at least 100 OOS trades and 30 per claimed market",
        )
    )
    results.extend(
        (
            _missing_or(
                "net_profit_factor",
                inputs.net_profit_factor,
                inputs.net_profit_factor is not None
                and inputs.net_profit_factor >= Decimal("1.20"),
                ">= 1.20",
            ),
            GateResult(
                "positive_folds_and_holdout",
                (
                    GateStatus.INSUFFICIENT
                    if inputs.holdout_expectancy_r is None
                    or len(inputs.fold_expectancies_r) < 3
                    else GateStatus.PASS
                    if sum(item > 0 for item in inputs.fold_expectancies_r) >= 3
                    and inputs.holdout_expectancy_r > 0
                    else GateStatus.FAIL
                ),
                (
                    f"folds={[str(item) for item in inputs.fold_expectancies_r]};"
                    f"holdout={inputs.holdout_expectancy_r}"
                ),
                "positive expectancy in at least three folds and holdout",
            ),
            _missing_or(
                "bootstrap_lower_95",
                inputs.bootstrap_lower_95_r,
                inputs.bootstrap_lower_95_r is not None
                and inputs.bootstrap_lower_95_r > 0,
                "strictly positive",
            ),
            _missing_or(
                "pnl_concentration",
                inputs.max_market_or_direction_pnl_contribution,
                inputs.max_market_or_direction_pnl_contribution is not None
                and inputs.max_market_or_direction_pnl_contribution <= Decimal("0.40"),
                "market or direction contribution <= 0.40",
            ),
            _missing_or(
                "top_five_concentration",
                inputs.top_five_pnl_contribution,
                inputs.top_five_pnl_contribution is not None
                and inputs.top_five_pnl_contribution <= Decimal("0.35"),
                "top five contribution <= 0.35",
            ),
            _missing_or(
                "cost_stress",
                inputs.stress_expectancy_r,
                inputs.stress_expectancy_r is not None
                and inputs.stress_expectancy_r >= 0,
                "+50% cost expectancy >= 0",
            ),
            _missing_or(
                "max_drawdown",
                inputs.max_drawdown_r,
                inputs.max_drawdown_r is not None
                and inputs.max_drawdown_r < Decimal(10),
                "strictly below 10R",
            ),
            GateResult(
                "true_open_control",
                (
                    GateStatus.INSUFFICIENT
                    if inputs.true_open_expectancy_r is None
                    or inputs.pseudo_open_expectancy_r is None
                    else GateStatus.PASS
                    if inputs.true_open_expectancy_r > inputs.pseudo_open_expectancy_r
                    else GateStatus.FAIL
                ),
                f"true={inputs.true_open_expectancy_r};pseudo={inputs.pseudo_open_expectancy_r}",
                "true open beats pseudo opens",
            ),
            GateResult(
                "naive_drive_control",
                (
                    GateStatus.INSUFFICIENT
                    if inputs.setup_expectancy_r is None
                    or inputs.naive_drive_expectancy_r is None
                    else GateStatus.PASS
                    if inputs.setup_expectancy_r > inputs.naive_drive_expectancy_r
                    else GateStatus.FAIL
                ),
                f"setup={inputs.setup_expectancy_r};naive={inputs.naive_drive_expectancy_r}",
                "full setup beats naive opening drive",
            ),
        )
    )
    if any(item.status is GateStatus.INSUFFICIENT for item in results):
        conclusion = ResearchConclusion.DATA_INSUFFICIENT
    elif any(item.status is GateStatus.FAIL for item in results):
        conclusion = ResearchConclusion.REJECTED
    else:
        conclusion = ResearchConclusion.OOS_CANDIDATE
    return ResearchGateEvaluation(tuple(results), conclusion)
