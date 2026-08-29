"""Deterministic daily observability reports for the no-signature shadow path."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from bot05.collector import FeedHealthDecision
from bot05.shadow.runner import (
    KillSwitch,
    ShadowContractError,
    ShadowDecision,
    ShadowReconciliation,
    ShadowStatus,
)

_SHA256 = re.compile(r"^[a-f0-9]{64}$")


def _require_utc(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ShadowContractError(f"{name} must be timezone-aware UTC")


def _mean(values: tuple[Decimal, ...]) -> Decimal | None:
    return None if not values else sum(values, Decimal(0)) / Decimal(len(values))


@dataclass(frozen=True, slots=True)
class ShadowDailyMetrics:
    decision_count: int
    observed_count: int
    refused_count: int
    killed_count: int
    unhealthy_check_count: int
    reconciliation_divergence_count: int
    mean_spread_bps: Decimal | None
    mean_impact_bps: Decimal | None

    def payload(self) -> dict[str, object]:
        return {
            "decision_count": self.decision_count,
            "killed_count": self.killed_count,
            "mean_impact_bps": (
                None if self.mean_impact_bps is None else str(self.mean_impact_bps)
            ),
            "mean_spread_bps": (
                None if self.mean_spread_bps is None else str(self.mean_spread_bps)
            ),
            "observed_count": self.observed_count,
            "reconciliation_divergence_count": self.reconciliation_divergence_count,
            "refused_count": self.refused_count,
            "unhealthy_check_count": self.unhealthy_check_count,
        }


@dataclass(frozen=True, slots=True)
class ShadowDailyReport:
    trading_day: date
    generated_at: datetime
    collector_spec_id: str
    config_sha256: str
    code_sha256: str
    decisions: tuple[ShadowDecision, ...]
    health_checks: tuple[FeedHealthDecision, ...]
    reconciliation: ShadowReconciliation
    kill_switch: KillSwitch
    metrics: ShadowDailyMetrics
    report_id: str

    def __post_init__(self) -> None:
        _require_utc(self.generated_at, "shadow report generated_at")
        for name, value in (
            ("collector_spec_id", self.collector_spec_id),
            ("config_sha256", self.config_sha256),
            ("code_sha256", self.code_sha256),
            ("report_id", self.report_id),
        ):
            if _SHA256.fullmatch(value) is None:
                raise ShadowContractError(f"{name} must be a SHA-256")

    def payload(self) -> dict[str, object]:
        return {
            "code_sha256": self.code_sha256,
            "collector_spec_id": self.collector_spec_id,
            "config_sha256": self.config_sha256,
            "decisions": [
                {
                    "checked_at": item.checked_at.isoformat(),
                    "intent_id": item.intent_id,
                    "quote": (
                        None
                        if item.quote is None
                        else {
                            "book_sha256": item.quote.book_sha256,
                            "executable_price": str(item.quote.executable_price),
                            "impact_bps": str(item.quote.impact_bps),
                            "observed_at": item.quote.observed_at.isoformat(),
                            "quantity": str(item.quote.quantity),
                            "spread_bps": str(item.quote.spread_bps),
                        }
                    ),
                    "refusal_codes": [code.value for code in item.refusal_codes],
                    "risk_refusal_codes": [
                        code.value for code in item.risk_decision.refusal_codes
                    ],
                    "status": item.status.value,
                }
                for item in self.decisions
            ],
            "generated_at": self.generated_at.astimezone(UTC).isoformat(),
            "health_checks": [
                {
                    "checked_at": item.checked_at.isoformat(),
                    "codes": [code.value for code in item.codes],
                    "healthy": item.healthy,
                    "kill_required": item.kill_required,
                    "market": item.market,
                }
                for item in self.health_checks
            ],
            "kill_switch": {
                "incident_id": self.kill_switch.incident_id,
                "latched": self.kill_switch.latched,
                "latched_at": (
                    None
                    if self.kill_switch.latched_at is None
                    else self.kill_switch.latched_at.isoformat()
                ),
                "reasons": list(self.kill_switch.reasons),
            },
            "kind": "bot05_shadow_daily_observability",
            "metrics": self.metrics.payload(),
            "network_transport_in_reporter": False,
            "orders_possible": False,
            "reconciliation": {
                "matches": self.reconciliation.matches,
                "missing_intent_ids": list(self.reconciliation.missing_intent_ids),
                "unexpected_intent_ids": list(
                    self.reconciliation.unexpected_intent_ids
                ),
            },
            "report_id": self.report_id,
            "schema_version": 1,
            "trading_day": self.trading_day.isoformat(),
        }

    def json_bytes(self) -> bytes:
        return (
            json.dumps(self.payload(), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        ).encode("utf-8")


def build_shadow_daily_report(
    *,
    trading_day: date,
    generated_at: datetime,
    collector_spec_id: str,
    config_sha256: str,
    code_sha256: str,
    decisions: tuple[ShadowDecision, ...],
    health_checks: tuple[FeedHealthDecision, ...],
    reconciliation: ShadowReconciliation,
    kill_switch: KillSwitch,
) -> ShadowDailyReport:
    if len({item.intent_id for item in decisions}) != len(decisions):
        raise ShadowContractError("daily shadow decisions must be exact-once")
    spreads = tuple(
        item.quote.spread_bps for item in decisions if item.quote is not None
    )
    impacts = tuple(
        item.quote.impact_bps for item in decisions if item.quote is not None
    )
    metrics = ShadowDailyMetrics(
        decision_count=len(decisions),
        observed_count=sum(item.status is ShadowStatus.OBSERVED for item in decisions),
        refused_count=sum(item.status is ShadowStatus.REFUSED for item in decisions),
        killed_count=sum(item.status is ShadowStatus.KILLED for item in decisions),
        unhealthy_check_count=sum(not item.healthy for item in health_checks),
        reconciliation_divergence_count=0 if reconciliation.matches else 1,
        mean_spread_bps=_mean(spreads),
        mean_impact_bps=_mean(impacts),
    )
    identity = {
        "collector_spec_id": collector_spec_id,
        "config_sha256": config_sha256,
        "decision_ids": [item.intent_id for item in decisions],
        "generated_at": generated_at.isoformat(),
        "health": [
            [item.market, item.checked_at.isoformat(), item.healthy]
            for item in health_checks
        ],
        "reconciliation": [
            list(reconciliation.missing_intent_ids),
            list(reconciliation.unexpected_intent_ids),
        ],
        "trading_day": trading_day.isoformat(),
    }
    report_id = hashlib.sha256(
        json.dumps(identity, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    return ShadowDailyReport(
        trading_day,
        generated_at,
        collector_spec_id,
        config_sha256,
        code_sha256,
        decisions,
        health_checks,
        reconciliation,
        kill_switch,
        metrics,
        report_id,
    )


def _immutable_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        if path.read_bytes() != payload:
            raise ShadowContractError(
                f"refusing to overwrite shadow report: {path}"
            ) from None


def write_shadow_daily_report(
    report: ShadowDailyReport, output: Path
) -> tuple[Path, Path]:
    payload = report.json_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    resolved = output.resolve()
    sidecar = resolved.with_suffix(resolved.suffix + ".sha256")
    _immutable_write(resolved, payload)
    _immutable_write(sidecar, f"{digest}  {resolved.name}\n".encode("ascii"))
    return resolved, sidecar
