"""Explicit underlying/Hyperliquid candle parity without mixing economic PnL."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from bot05.data.contracts import EvidenceTier, Qualification
from bot05.models import Candle, domain_record_sha256
from bot05.replay.contracts import utc_text
from bot05.studies.contracts import (
    ExperimentSpec,
    StudyContractError,
    StudyDataset,
    market_role,
)

PARITY_REPORT_SCHEMA_VERSION = 1


class ParityStatus(StrEnum):
    COMMON_WINDOW_COMPLETE = "common_window_complete"
    INCOMPLETE_COVERAGE = "incomplete_coverage"


@dataclass(frozen=True, slots=True)
class CandleParityPoint:
    open_time: datetime
    underlying_sha256: str
    hyperliquid_sha256: str
    open_difference_bps: Decimal
    high_difference_bps: Decimal
    low_difference_bps: Decimal
    close_difference_bps: Decimal

    def payload(self) -> dict[str, str]:
        return {
            "close_difference_bps": str(self.close_difference_bps),
            "high_difference_bps": str(self.high_difference_bps),
            "hyperliquid_sha256": self.hyperliquid_sha256,
            "low_difference_bps": str(self.low_difference_bps),
            "open_difference_bps": str(self.open_difference_bps),
            "open_time": utc_text(self.open_time),
            "underlying_sha256": self.underlying_sha256,
        }


@dataclass(frozen=True, slots=True)
class CandleParityMetrics:
    underlying_count: int
    hyperliquid_count: int
    matched_count: int
    missing_underlying_count: int
    missing_hyperliquid_count: int
    mean_absolute_open_bps: Decimal
    mean_absolute_high_bps: Decimal
    mean_absolute_low_bps: Decimal
    mean_absolute_close_bps: Decimal
    max_absolute_field_bps: Decimal

    def payload(self) -> dict[str, object]:
        return {
            "hyperliquid_count": self.hyperliquid_count,
            "matched_count": self.matched_count,
            "max_absolute_field_bps": str(self.max_absolute_field_bps),
            "mean_absolute_close_bps": str(self.mean_absolute_close_bps),
            "mean_absolute_high_bps": str(self.mean_absolute_high_bps),
            "mean_absolute_low_bps": str(self.mean_absolute_low_bps),
            "mean_absolute_open_bps": str(self.mean_absolute_open_bps),
            "missing_hyperliquid_count": self.missing_hyperliquid_count,
            "missing_underlying_count": self.missing_underlying_count,
            "underlying_count": self.underlying_count,
        }


@dataclass(frozen=True, slots=True)
class CandleParityReport:
    experiment_spec_sha256: str
    generated_at: datetime
    canonical_market: str
    source_instrument: str
    session_id: str
    underlying_dataset: StudyDataset
    hyperliquid_dataset: StudyDataset
    interval_seconds: int
    status: ParityStatus
    points: tuple[CandleParityPoint, ...]
    missing_underlying: tuple[datetime, ...]
    missing_hyperliquid: tuple[datetime, ...]
    metrics: CandleParityMetrics
    report_id: str

    def _body(self) -> dict[str, object]:
        return {
            "canonical_market": self.canonical_market,
            "experiment_spec_sha256": self.experiment_spec_sha256,
            "generated_at": utc_text(self.generated_at),
            "hyperliquid_dataset": self.hyperliquid_dataset.payload(),
            "interval_seconds": self.interval_seconds,
            "market_role": market_role(self.canonical_market).value,
            "metrics": self.metrics.payload(),
            "missing_hyperliquid": [
                utc_text(item) for item in self.missing_hyperliquid
            ],
            "missing_underlying": [utc_text(item) for item in self.missing_underlying],
            "points": [item.payload() for item in self.points],
            "schema_version": PARITY_REPORT_SCHEMA_VERSION,
            "session_id": self.session_id,
            "source_instrument": self.source_instrument,
            "status": self.status.value,
            "underlying_dataset": self.underlying_dataset.payload(),
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
        return "\n".join(
            (
                f"# Parité sous-jacent / Hyperliquid — {self.canonical_market}",
                "",
                f"- Rapport : `{self.report_id}`",
                f"- Instrument source : `{self.source_instrument}`",
                f"- Étage Hyperliquid : `{self.hyperliquid_dataset.tier.value}`",
                f"- Bougies appariées : {self.metrics.matched_count}",
                f"- Bougies source manquantes : "
                f"{self.metrics.missing_underlying_count}",
                f"- Bougies Hyperliquid manquantes : "
                f"{self.metrics.missing_hyperliquid_count}",
                f"- Écart absolu close moyen : "
                f"{self.metrics.mean_absolute_close_bps} bps",
                f"- Écart absolu maximal, tous champs : "
                f"{self.metrics.max_absolute_field_bps} bps",
                f"- Statut : `{self.status.value}`",
                "",
                "> Cette parité ne fusionne aucun PnL et ne prouve pas "
                "l'exécution du perp Hyperliquid.",
                "",
            )
        )


def _difference_bps(underlying: Decimal, hyperliquid: Decimal) -> Decimal:
    return Decimal(10_000) * (hyperliquid - underlying) / underlying


def _mean_absolute(values: tuple[Decimal, ...]) -> Decimal:
    return sum((abs(item) for item in values), Decimal(0)) / Decimal(len(values))


def _validate_dataset_pair(
    canonical_market: str,
    source_instrument: str,
    underlying: StudyDataset,
    hyperliquid: StudyDataset,
) -> None:
    market_role(canonical_market)
    if (
        underlying.canonical_market != canonical_market
        or hyperliquid.canonical_market != canonical_market
    ):
        raise StudyContractError("parity datasets must share the canonical market")
    if underlying.source_instrument != source_instrument:
        raise StudyContractError(
            "underlying dataset instrument disagrees with parity scope"
        )
    if underlying.tier is not EvidenceTier.UNDERLYING:
        raise StudyContractError("parity source dataset must use tier U")
    if hyperliquid.tier not in {
        EvidenceTier.HYPERLIQUID_CANDLES,
        EvidenceTier.HYPERLIQUID_ARCHIVE,
        EvidenceTier.BOT05_COLLECTOR,
    }:
        raise StudyContractError("parity target must be a Hyperliquid evidence tier")
    if any(
        item.qualification is not Qualification.QUALIFIED
        or item.critical_gap_count != 0
        for item in (underlying, hyperliquid)
    ):
        raise StudyContractError(
            "parity datasets must be qualified without critical gaps"
        )


def build_candle_parity_report(
    spec: ExperimentSpec,
    *,
    canonical_market: str,
    source_instrument: str,
    session_id: str,
    underlying_dataset: StudyDataset,
    hyperliquid_dataset: StudyDataset,
    underlying_candles: tuple[Candle, ...],
    hyperliquid_candles: tuple[Candle, ...],
    generated_at: datetime,
) -> CandleParityReport:
    utc_text(generated_at)
    if canonical_market not in spec.universe or session_id not in spec.sessions:
        raise StudyContractError("parity scope is absent from ExperimentSpec")
    _validate_dataset_pair(
        canonical_market, source_instrument, underlying_dataset, hyperliquid_dataset
    )
    if not underlying_candles or not hyperliquid_candles:
        raise StudyContractError("parity requires non-empty candle series")
    intervals = {
        item.interval_seconds for item in underlying_candles + hyperliquid_candles
    }
    if len(intervals) != 1:
        raise StudyContractError("parity candles must share one interval")
    if any(item.market != source_instrument for item in underlying_candles):
        raise StudyContractError("underlying candle instrument disagrees with scope")
    if any(item.market != canonical_market for item in hyperliquid_candles):
        raise StudyContractError("Hyperliquid candle market disagrees with scope")
    if any(
        item.open_time < underlying_dataset.period_start
        or item.close_time > underlying_dataset.period_end
        for item in underlying_candles
    ) or any(
        item.open_time < hyperliquid_dataset.period_start
        or item.close_time > hyperliquid_dataset.period_end
        for item in hyperliquid_candles
    ):
        raise StudyContractError("parity candle escapes dataset coverage")
    underlying_by_time = {item.open_time: item for item in underlying_candles}
    hyperliquid_by_time = {item.open_time: item for item in hyperliquid_candles}
    if len(underlying_by_time) != len(underlying_candles) or len(
        hyperliquid_by_time
    ) != len(hyperliquid_candles):
        raise StudyContractError("parity candle timestamps must be unique")
    common = tuple(sorted(set(underlying_by_time) & set(hyperliquid_by_time)))
    if not common:
        raise StudyContractError("parity datasets have no common candle")
    points = tuple(
        CandleParityPoint(
            open_time=timestamp,
            underlying_sha256=domain_record_sha256(underlying_by_time[timestamp]),
            hyperliquid_sha256=domain_record_sha256(hyperliquid_by_time[timestamp]),
            open_difference_bps=_difference_bps(
                underlying_by_time[timestamp].open,
                hyperliquid_by_time[timestamp].open,
            ),
            high_difference_bps=_difference_bps(
                underlying_by_time[timestamp].high,
                hyperliquid_by_time[timestamp].high,
            ),
            low_difference_bps=_difference_bps(
                underlying_by_time[timestamp].low,
                hyperliquid_by_time[timestamp].low,
            ),
            close_difference_bps=_difference_bps(
                underlying_by_time[timestamp].close,
                hyperliquid_by_time[timestamp].close,
            ),
        )
        for timestamp in common
    )
    missing_underlying = tuple(
        sorted(set(hyperliquid_by_time) - set(underlying_by_time))
    )
    missing_hyperliquid = tuple(
        sorted(set(underlying_by_time) - set(hyperliquid_by_time))
    )
    opens = tuple(item.open_difference_bps for item in points)
    highs = tuple(item.high_difference_bps for item in points)
    lows = tuple(item.low_difference_bps for item in points)
    closes = tuple(item.close_difference_bps for item in points)
    metrics = CandleParityMetrics(
        underlying_count=len(underlying_candles),
        hyperliquid_count=len(hyperliquid_candles),
        matched_count=len(points),
        missing_underlying_count=len(missing_underlying),
        missing_hyperliquid_count=len(missing_hyperliquid),
        mean_absolute_open_bps=_mean_absolute(opens),
        mean_absolute_high_bps=_mean_absolute(highs),
        mean_absolute_low_bps=_mean_absolute(lows),
        mean_absolute_close_bps=_mean_absolute(closes),
        max_absolute_field_bps=max(abs(item) for item in opens + highs + lows + closes),
    )
    status = (
        ParityStatus.COMMON_WINDOW_COMPLETE
        if not missing_underlying and not missing_hyperliquid
        else ParityStatus.INCOMPLETE_COVERAGE
    )
    provisional = CandleParityReport(
        experiment_spec_sha256=spec.spec_sha256,
        generated_at=generated_at,
        canonical_market=canonical_market,
        source_instrument=source_instrument,
        session_id=session_id,
        underlying_dataset=underlying_dataset,
        hyperliquid_dataset=hyperliquid_dataset,
        interval_seconds=next(iter(intervals)),
        status=status,
        points=points,
        missing_underlying=missing_underlying,
        missing_hyperliquid=missing_hyperliquid,
        metrics=metrics,
        report_id="0" * 64,
    )
    encoded = json.dumps(
        provisional._body(), separators=(",", ":"), sort_keys=True
    ).encode()
    return CandleParityReport(
        experiment_spec_sha256=provisional.experiment_spec_sha256,
        generated_at=provisional.generated_at,
        canonical_market=provisional.canonical_market,
        source_instrument=provisional.source_instrument,
        session_id=provisional.session_id,
        underlying_dataset=provisional.underlying_dataset,
        hyperliquid_dataset=provisional.hyperliquid_dataset,
        interval_seconds=provisional.interval_seconds,
        status=provisional.status,
        points=provisional.points,
        missing_underlying=provisional.missing_underlying,
        missing_hyperliquid=provisional.missing_hyperliquid,
        metrics=provisional.metrics,
        report_id=hashlib.sha256(encoded).hexdigest(),
    )


def _write_immutable(path: Path, content: bytes) -> None:
    if path.exists():
        if path.read_bytes() != content:
            raise FileExistsError(f"refusing to overwrite published parity: {path}")
        return
    path.write_bytes(content)


def write_candle_parity_report(
    report: CandleParityReport, output_directory: Path
) -> tuple[Path, Path, Path]:
    output_directory.mkdir(parents=True, exist_ok=True)
    stem = f"{report.canonical_market.replace(':', '-')}-{report.report_id}"
    json_path = output_directory / f"{stem}.json"
    checksum_path = output_directory / f"{stem}.json.sha256"
    markdown_path = output_directory / f"{stem}.md"
    content = report.json_bytes()
    checksum = hashlib.sha256(content).hexdigest()
    _write_immutable(json_path, content)
    _write_immutable(checksum_path, f"{checksum}  {json_path.name}\n".encode())
    _write_immutable(markdown_path, report.markdown().encode())
    return json_path, checksum_path, markdown_path
