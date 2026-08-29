"""Preregistered BOT05 strategy matrix and false-positive control families."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from itertools import product

from bot05.features import DriveFilter
from bot05.strategy import ConfirmationKind, StrategySpec, TargetKind
from bot05.studies.validation import ValidationContractError

_SHA256 = re.compile(r"^[a-f0-9]{64}$")


class ControlKind(StrEnum):
    RETRACEMENT_0382 = "retracement_0_382"
    RETRACEMENT_0500 = "retracement_0_500"
    RETRACEMENT_0618 = "retracement_0_618"
    RETRACEMENT_ZONE_30_70 = "retracement_zone_30_70"
    NO_CONFIRMATION = "no_confirmation"
    OPENING_DRIVE_NO_PULLBACK = "opening_drive_no_pullback"
    SHIFTED_OPEN = "shifted_open"
    RANDOM_STRATIFIED_OPEN = "random_stratified_open"


@dataclass(frozen=True, slots=True)
class ControlSpec:
    kind: ControlKind
    parameter: str

    def __post_init__(self) -> None:
        if not self.parameter.strip():
            raise ValidationContractError("control parameter is required")


@dataclass(frozen=True, slots=True)
class AblationMatrix:
    strategy_specs: tuple[StrategySpec, ...]
    controls: tuple[ControlSpec, ...]

    def __post_init__(self) -> None:
        if len(self.strategy_specs) != 27:
            raise ValidationContractError(
                "primary ablation matrix must contain 27 specs"
            )
        ids = tuple(item.spec_id for item in self.strategy_specs)
        if len(set(ids)) != len(ids):
            raise ValidationContractError("ablation strategy specs must be unique")
        if not self.controls:
            raise ValidationContractError("false-positive controls are required")

    @property
    def matrix_id(self) -> str:
        payload = {
            "controls": [
                {"kind": item.kind.value, "parameter": item.parameter}
                for item in self.controls
            ],
            "strategy_spec_ids": [item.spec_id for item in self.strategy_specs],
        }
        return hashlib.sha256(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()


def build_ablation_matrix(
    *,
    market: str,
    session_id: str,
    config_sha256: str,
    calendar_version: str,
    code_version: str,
    random_open_seed: int,
) -> AblationMatrix:
    """Build the complete 3×3×3 family and preregistered controls."""

    if _SHA256.fullmatch(config_sha256) is None or random_open_seed < 0:
        raise ValidationContractError("ablation config hash and seed are required")
    specs = tuple(
        StrategySpec(
            market=market,
            session_id=session_id,
            drive_filter=drive_filter,
            confirmation=confirmation,
            target=target,
            config_sha256=config_sha256,
            calendar_version=calendar_version,
            code_version=code_version,
        )
        for drive_filter, confirmation, target in product(
            tuple(DriveFilter), tuple(ConfirmationKind), tuple(TargetKind)
        )
    )
    controls = (
        ControlSpec(ControlKind.RETRACEMENT_0382, "0.382"),
        ControlSpec(ControlKind.RETRACEMENT_0500, "0.500"),
        ControlSpec(ControlKind.RETRACEMENT_0618, "0.618"),
        ControlSpec(ControlKind.RETRACEMENT_ZONE_30_70, "0.30:0.70"),
        ControlSpec(ControlKind.NO_CONFIRMATION, "entry_after_touch"),
        ControlSpec(ControlKind.OPENING_DRIVE_NO_PULLBACK, "entry_after_drive"),
        *(
            ControlSpec(ControlKind.SHIFTED_OPEN, str(value))
            for value in (-120, -60, -30, 30, 60, 120)
        ),
        ControlSpec(ControlKind.RANDOM_STRATIFIED_OPEN, f"seed:{random_open_seed}"),
    )
    return AblationMatrix(specs, controls)
