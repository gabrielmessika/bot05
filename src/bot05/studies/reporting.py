"""Evidence-gated, immutable per-market D5 reports."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from bot05.data.contracts import EvidenceTier, Qualification
from bot05.replay import ReplayResult
from bot05.replay.contracts import utc_text
from bot05.replay.reporting import result_payload
from bot05.studies.contracts import (
    ExcursionObservation,
    ExperimentSpec,
    MarketStudyScope,
    SessionObservation,
    StudyConclusion,
    StudyContractError,
    StudyDataset,
    StudyPurpose,
    allowed_purposes,
    market_role,
)
from bot05.studies.metrics import MarketStudyMetrics, calculate_market_metrics

STUDY_REPORT_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class EvidenceAssessment:
    tier: EvidenceTier
    allowed_uses: tuple[StudyPurpose, ...]
    conclusion: StudyConclusion
    economic_metrics_publishable: bool
    promotion_permitted: bool
    limitation_codes: tuple[str, ...]

    def payload(self) -> dict[str, object]:
        return {
            "allowed_uses": [item.value for item in self.allowed_uses],
            "conclusion": self.conclusion.value,
            "economic_metrics_publishable": self.economic_metrics_publishable,
            "limitation_codes": list(self.limitation_codes),
            "promotion_permitted": self.promotion_permitted,
            "tier": self.tier.value,
        }


def assess_evidence(
    scope: MarketStudyScope,
    *,
    observed_sessions: int,
    closed_replays: int,
) -> EvidenceAssessment:
    limitations: list[str] = []
    if scope.tier is EvidenceTier.UNDERLYING:
        limitations.append("underlying_does_not_prove_hyperliquid_execution")
    elif scope.tier is EvidenceTier.HYPERLIQUID_CANDLES:
        limitations.append("h0_short_smoke_and_signal_parity_only")
    elif scope.tier is EvidenceTier.HYPERLIQUID_ARCHIVE:
        limitations.append("h1_archive_coverage_must_be_interpreted_per_market")
    elif scope.tier is EvidenceTier.BOT05_COLLECTOR:
        limitations.append("h2_requires_accumulated_causal_sessions")
    else:
        limitations.append("legacy_never_sufficient_for_promotion")
    if market_role(scope.canonical_market).value == "control":
        limitations.append("control_market_no_standalone_edge_claim")
    if observed_sessions < 20:
        limitations.append("fewer_than_20_observed_sessions")
    if closed_replays < 30:
        limitations.append("fewer_than_30_closed_replays_for_market")

    economic_publishable = scope.tier in {
        EvidenceTier.HYPERLIQUID_ARCHIVE,
        EvidenceTier.BOT05_COLLECTOR,
    }
    if scope.tier in {EvidenceTier.HYPERLIQUID_CANDLES, EvidenceTier.LEGACY}:
        conclusion = StudyConclusion.DESCRIPTIVE_ONLY
    elif observed_sessions < 20 or (economic_publishable and closed_replays < 30):
        conclusion = StudyConclusion.DATA_INSUFFICIENT
    else:
        conclusion = StudyConclusion.RESEARCH_ONLY
    return EvidenceAssessment(
        tier=scope.tier,
        allowed_uses=allowed_purposes(scope.tier),
        conclusion=conclusion,
        economic_metrics_publishable=economic_publishable,
        promotion_permitted=False,
        limitation_codes=tuple(limitations),
    )


def _session_payload(item: SessionObservation) -> dict[str, object]:
    return {
        "canonical_market": item.canonical_market,
        "complete": item.complete,
        "confirmation": item.confirmation,
        "direction": None if item.direction is None else item.direction.value,
        "drive": item.drive,
        "economic_gate": item.economic_gate,
        "intent_id": item.intent_id,
        "pullback": item.pullback,
        "rejection_reason": item.rejection_reason,
        "session_eligible": item.session_eligible,
        "session_id": item.session_id,
        "session_key": item.session_key,
        "source_data_sha256": item.source_data_sha256,
        "t0": utc_text(item.t0),
        "trade": item.trade,
    }


def _excursion_payload(item: ExcursionObservation) -> dict[str, object]:
    return {
        "canonical_market": item.canonical_market,
        "direction": item.direction.value,
        "horizons": [
            {
                "complete": horizon.complete,
                "mae_bps": None if horizon.mae_bps is None else str(horizon.mae_bps),
                "mae_r": None if horizon.mae_r is None else str(horizon.mae_r),
                "mfe_bps": None if horizon.mfe_bps is None else str(horizon.mfe_bps),
                "mfe_r": None if horizon.mfe_r is None else str(horizon.mfe_r),
                "minutes": horizon.minutes,
            }
            for horizon in item.horizons
        ],
        "intent_id": item.intent_id,
        "reference_price": str(item.reference_price),
        "replay_data_sha256": item.replay_data_sha256,
        "session_id": item.session_id,
        "start_at": utc_text(item.start_at),
        "structural_risk": str(item.structural_risk),
    }


@dataclass(frozen=True, slots=True)
class MarketStudyReport:
    experiment_spec_sha256: str
    generated_at: datetime
    scope: MarketStudyScope
    datasets: tuple[StudyDataset, ...]
    sessions: tuple[SessionObservation, ...]
    excursions: tuple[ExcursionObservation, ...]
    replay_results: tuple[ReplayResult, ...]
    metrics: MarketStudyMetrics
    evidence: EvidenceAssessment
    report_id: str

    def _body(self) -> dict[str, object]:
        return {
            "datasets": [item.payload() for item in self.datasets],
            "evidence": self.evidence.payload(),
            "excursions": [_excursion_payload(item) for item in self.excursions],
            "experiment_spec_sha256": self.experiment_spec_sha256,
            "generated_at": utc_text(self.generated_at),
            "metrics": self.metrics.payload(),
            "replay_results": [result_payload(item) for item in self.replay_results],
            "schema_version": STUDY_REPORT_SCHEMA_VERSION,
            "scope": {
                "canonical_market": self.scope.canonical_market,
                "market_role": market_role(self.scope.canonical_market).value,
                "replay_model": self.scope.replay_model.value,
                "session_id": self.scope.session_id,
                "source_instrument": self.scope.source_instrument,
                "tier": self.scope.tier.value,
            },
            "sessions": [_session_payload(item) for item in self.sessions],
        }

    def json_bytes(self) -> bytes:
        payload = {**self._body(), "report_id": self.report_id}
        return (
            json.dumps(
                payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            )
            + "\n"
        ).encode()

    def markdown(self) -> str:
        performance = self.metrics.performance
        net_pnl = (
            str(performance.net_pnl)
            if self.evidence.economic_metrics_publishable
            else "n/a — interdit pour cet étage de preuve"
        )
        return "\n".join(
            (
                f"# Étude BOT05 — {self.scope.canonical_market}",
                "",
                f"- Rapport : `{self.report_id}`",
                f"- ExperimentSpec : `{self.experiment_spec_sha256}`",
                f"- Session : `{self.scope.session_id}`",
                f"- Instrument source : `{self.scope.source_instrument}`",
                f"- Étage de preuve : `{self.scope.tier.value}`",
                f"- Modèle : `{self.scope.replay_model.value}`",
                f"- Conclusion : `{self.evidence.conclusion.value}`",
                f"- Sessions observées : {self.metrics.funnel.observed_sessions}",
                f"- Trades de l'entonnoir : {self.metrics.funnel.trades}",
                f"- Replays clos : {performance.closed_count}",
                f"- PnL net descriptif : {net_pnl}",
                "",
                "## Limites",
                "",
                *(f"- `{code}`" for code in self.evidence.limitation_codes),
                "",
                "> Les résultats restent propres à cet étage de preuve et à ce "
                "marché. Ils ne constituent ni une validation OOS, ni une preuve "
                "live, ni une garantie.",
                "",
            )
        )


def _validate_scope(
    spec: ExperimentSpec,
    scope: MarketStudyScope,
    datasets: tuple[StudyDataset, ...],
    sessions: tuple[SessionObservation, ...],
    excursions: tuple[ExcursionObservation, ...],
    results: tuple[ReplayResult, ...],
) -> None:
    if (
        scope.canonical_market not in spec.universe
        or scope.session_id not in spec.sessions
    ):
        raise StudyContractError("study scope is absent from ExperimentSpec")
    if scope.replay_model not in spec.replay_models:
        raise StudyContractError("study replay model is absent from ExperimentSpec")
    if not datasets or not sessions:
        raise StudyContractError("study requires datasets and observed sessions")
    if any(
        item.canonical_market != scope.canonical_market
        or item.source_instrument != scope.source_instrument
        or item.tier is not scope.tier
        for item in datasets
    ):
        raise StudyContractError(
            "study datasets must match one market and evidence tier"
        )
    if any(
        item.qualification is not Qualification.QUALIFIED
        or item.critical_gap_count != 0
        for item in datasets
    ):
        raise StudyContractError(
            "study datasets must be qualified without critical gaps"
        )
    if len({item.dataset_id for item in datasets}) != len(datasets):
        raise StudyContractError("study dataset ids must be unique")

    def covered(start: datetime, end: datetime) -> bool:
        cursor = start
        for dataset in sorted(datasets, key=lambda item: item.period_start):
            if dataset.period_end <= cursor or dataset.period_start > cursor:
                continue
            cursor = max(cursor, dataset.period_end)
            if cursor >= end:
                return True
        return False

    if any(
        item.canonical_market != scope.canonical_market
        or item.session_id != scope.session_id
        for item in sessions
    ):
        raise StudyContractError("session observations escape study scope")
    if len({item.session_key for item in sessions}) != len(sessions) or len(
        {item.t0 for item in sessions}
    ) != len(sessions):
        raise StudyContractError("study sessions must be unique")
    if any(
        not covered(item.t0, item.t0 + timedelta(microseconds=1)) for item in sessions
    ):
        raise StudyContractError("session observation escapes dataset coverage")
    intent_ids = {item.intent_id for item in sessions if item.intent_id is not None}
    if any(
        item.canonical_market != scope.canonical_market
        or item.session_id != scope.session_id
        or item.intent_id not in intent_ids
        for item in excursions
    ):
        raise StudyContractError("excursions escape study scope or known intents")
    if len({item.intent_id for item in excursions}) != len(excursions):
        raise StudyContractError("excursion intent ids must be unique")
    if any(
        not covered(item.start_at, item.start_at + timedelta(minutes=120))
        for item in excursions
    ):
        raise StudyContractError("excursion horizon escapes dataset coverage")
    if any(
        item.market != scope.canonical_market
        or item.session_id != scope.session_id
        or item.model is not scope.replay_model
        or item.intent_id not in intent_ids
        for item in results
    ):
        raise StudyContractError("replay result escapes study scope")
    if len({item.run_id for item in results}) != len(results):
        raise StudyContractError("replay run ids must be unique")
    if any(
        item.entry is not None
        and not covered(
            item.entry.timestamp,
            (
                item.exit.timestamp + timedelta(microseconds=1)
                if item.exit is not None
                else item.entry.timestamp + timedelta(microseconds=1)
            ),
        )
        for item in results
    ):
        raise StudyContractError("replay fills escape dataset coverage")
    execution_allowed = StudyPurpose.EXECUTION_REPLAY in allowed_purposes(scope.tier)
    if results and not execution_allowed:
        raise StudyContractError("this evidence tier cannot publish execution PnL")
    if execution_allowed and {item.intent_id for item in results} != intent_ids:
        raise StudyContractError(
            "every executable funnel trade needs one replay result"
        )


def build_market_study_report(
    spec: ExperimentSpec,
    scope: MarketStudyScope,
    datasets: tuple[StudyDataset, ...],
    sessions: tuple[SessionObservation, ...],
    excursions: tuple[ExcursionObservation, ...],
    replay_results: tuple[ReplayResult, ...],
    *,
    generated_at: datetime,
) -> MarketStudyReport:
    utc_text(generated_at)
    ordered_datasets = tuple(sorted(datasets, key=lambda item: item.dataset_id))
    ordered_sessions = tuple(
        sorted(sessions, key=lambda item: (item.t0, item.session_key))
    )
    ordered_excursions = tuple(sorted(excursions, key=lambda item: item.intent_id))
    ordered_results = tuple(sorted(replay_results, key=lambda item: item.run_id))
    _validate_scope(
        spec,
        scope,
        ordered_datasets,
        ordered_sessions,
        ordered_excursions,
        ordered_results,
    )
    metrics = calculate_market_metrics(
        ordered_sessions, ordered_excursions, ordered_results
    )
    evidence = assess_evidence(
        scope,
        observed_sessions=metrics.funnel.observed_sessions,
        closed_replays=metrics.performance.closed_count,
    )
    provisional = MarketStudyReport(
        experiment_spec_sha256=spec.spec_sha256,
        generated_at=generated_at,
        scope=scope,
        datasets=ordered_datasets,
        sessions=ordered_sessions,
        excursions=ordered_excursions,
        replay_results=ordered_results,
        metrics=metrics,
        evidence=evidence,
        report_id="0" * 64,
    )
    encoded = json.dumps(
        provisional._body(), separators=(",", ":"), sort_keys=True
    ).encode()
    return MarketStudyReport(
        experiment_spec_sha256=provisional.experiment_spec_sha256,
        generated_at=provisional.generated_at,
        scope=provisional.scope,
        datasets=provisional.datasets,
        sessions=provisional.sessions,
        excursions=provisional.excursions,
        replay_results=provisional.replay_results,
        metrics=provisional.metrics,
        evidence=provisional.evidence,
        report_id=hashlib.sha256(encoded).hexdigest(),
    )


def _write_immutable(path: Path, content: bytes) -> None:
    if path.exists():
        if path.read_bytes() != content:
            raise FileExistsError(f"refusing to overwrite published study: {path}")
        return
    path.write_bytes(content)


def write_market_study_report(
    report: MarketStudyReport, output_directory: Path
) -> tuple[Path, Path, Path]:
    output_directory.mkdir(parents=True, exist_ok=True)
    stem = f"{report.scope.canonical_market.replace(':', '-')}-{report.report_id}"
    json_path = output_directory / f"{stem}.json"
    checksum_path = output_directory / f"{stem}.json.sha256"
    markdown_path = output_directory / f"{stem}.md"
    content = report.json_bytes()
    checksum = hashlib.sha256(content).hexdigest()
    _write_immutable(json_path, content)
    _write_immutable(checksum_path, f"{checksum}  {json_path.name}\n".encode())
    _write_immutable(markdown_path, report.markdown().encode())
    return json_path, checksum_path, markdown_path
