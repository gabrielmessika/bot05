"""Deterministic block bootstrap that resamples complete trading days."""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from bot05.studies.validation import ValidationContractError


@dataclass(frozen=True, slots=True)
class DayBlock:
    day: date
    pnl_r: tuple[Decimal, ...]

    def __post_init__(self) -> None:
        if not self.pnl_r or any(not item.is_finite() for item in self.pnl_r):
            raise ValidationContractError("day block requires finite trade returns")


@dataclass(frozen=True, slots=True)
class BootstrapInterval:
    point_estimate: Decimal
    lower_95: Decimal
    upper_95: Decimal
    sample_count: int
    seed: int
    input_sha256: str


def _quantile(values: tuple[Decimal, ...], probability: Decimal) -> Decimal:
    ordered = tuple(sorted(values))
    if len(ordered) == 1:
        return ordered[0]
    position = Decimal(len(ordered) - 1) * probability
    lower = int(position)
    fraction = position - Decimal(lower)
    return ordered[lower] + (ordered[lower + 1] - ordered[lower]) * fraction


def bootstrap_expectancy(
    blocks: tuple[DayBlock, ...], *, sample_count: int = 10_000, seed: int
) -> BootstrapInterval:
    """Resample days with replacement and preserve all within-day trades."""

    if len(blocks) < 2 or len({item.day for item in blocks}) != len(blocks):
        raise ValidationContractError("bootstrap requires unique multiple day blocks")
    if tuple(sorted(blocks, key=lambda item: item.day)) != blocks:
        raise ValidationContractError("bootstrap day blocks must be chronological")
    if sample_count < 100 or seed < 0:
        raise ValidationContractError("bootstrap samples and seed are invalid")
    all_returns = tuple(value for block in blocks for value in block.pnl_r)
    point = sum(all_returns, Decimal(0)) / Decimal(len(all_returns))
    generator = random.Random(seed)
    estimates: list[Decimal] = []
    for _ in range(sample_count):
        sampled = tuple(generator.choice(blocks) for _ in blocks)
        returns = tuple(value for block in sampled for value in block.pnl_r)
        estimates.append(sum(returns, Decimal(0)) / Decimal(len(returns)))
    payload = [
        {"day": item.day.isoformat(), "pnl_r": [str(value) for value in item.pnl_r]}
        for item in blocks
    ]
    digest = hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    values = tuple(estimates)
    return BootstrapInterval(
        point_estimate=point,
        lower_95=_quantile(values, Decimal("0.025")),
        upper_95=_quantile(values, Decimal("0.975")),
        sample_count=sample_count,
        seed=seed,
        input_sha256=digest,
    )
