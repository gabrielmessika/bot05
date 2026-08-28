from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bot05.data.contracts import (
    AcquisitionAction,
    DataRequirement,
    EvidenceTier,
    LocalInventory,
    Qualification,
    SourceProject,
    TimeRange,
)
from bot05.data.hyperliquid import H0CandleAdapter
from bot05.data.normalizer import (
    NormalizationResult,
    normalize_rows,
    source_row_from_mapping,
)
from bot05.data.planner import plan_requirement
from bot05.data.store import (
    AppendOnlyStore,
    AppendOnlyStoreError,
    QualificationEvidence,
)
from bot05.models import Candle, DatasetProvenance


def _provenance() -> DatasetProvenance:
    return DatasetProvenance(
        dataset_id="h0-gold-fixture",
        evidence_tier="H0",
        source="hyperliquid_public",
        source_path_or_url="https://api.hyperliquid.xyz/info#fixture",
        raw_sha256="a" * 64,
        manifest_sha256="b" * 64,
        adapter_version="bot05-h0-candle-v1",
        calendar_version="test:2026:1234567890abcdef",
        code_version="0.1.0+test",
        config_sha256="c" * 64,
        source_timezone="UTC",
        period_start=datetime(2026, 8, 16, tzinfo=UTC),
        period_end=datetime(2026, 8, 17, tzinfo=UTC),
        transformations=("candle_snapshot_v1",),
    )


def _normalization_result() -> NormalizationResult:
    open_time = datetime(2026, 8, 16, 9, 0, tzinfo=UTC)
    open_ms = int(open_time.timestamp() * 1000)
    row = source_row_from_mapping(
        1,
        {
            "t": open_ms,
            "T": open_ms + 299_999,
            "s": "xyz:GOLD",
            "i": "5m",
            "o": "2500.1",
            "c": "2501.5",
            "h": "2502.0",
            "l": "2499.9",
            "v": "12.5",
            "n": 42,
        },
    )
    return normalize_rows(
        (row,),
        H0CandleAdapter(
            _provenance(),
            observed_at=open_time + timedelta(minutes=6),
            expected_market="xyz:GOLD",
        ),
    )


def test_store_is_content_addressed_idempotent_and_bit_exact(tmp_path: Path) -> None:
    provenance = _provenance()
    result = _normalization_result()
    first_store = AppendOnlyStore(tmp_path / "first")
    second_store = AppendOnlyStore(tmp_path / "second")

    first = first_store.append(
        provenance.dataset_id,
        provenance,
        result,
    )
    retry = first_store.append(
        provenance.dataset_id,
        provenance,
        result,
    )
    second = second_store.append(
        provenance.dataset_id,
        provenance,
        result,
    )

    assert retry.segment_id == first.segment_id == second.segment_id
    assert first.manifest_path.read_bytes() == second.manifest_path.read_bytes()
    assert first.records_path.read_bytes() == second.records_path.read_bytes()
    decoded = first_store.read_records(first)
    assert len(decoded) == 1
    assert isinstance(decoded[0], Candle)
    assert first_store.discover_manifests(provenance.dataset_id) == (
        first.manifest_path,
    )


def test_zero_reject_segment_becomes_qualified_bot05_asset(tmp_path: Path) -> None:
    provenance = _provenance()
    segment = AppendOnlyStore(tmp_path).append(
        provenance.dataset_id,
        provenance,
        _normalization_result(),
    )

    candidate = segment.as_data_asset()
    report = (tmp_path / "qualification.json").resolve()
    assert segment.coverage is not None
    qualified_coverage = (
        TimeRange(segment.coverage.start_ms, segment.coverage.end_ms),
    )
    report.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "bot05_dataset_qualification",
                "dataset_id": segment.dataset_id,
                "segment_id": segment.segment_id,
                "source_raw_sha256": segment.source_raw_sha256,
                "records_sha256": segment.records_sha256,
                "qualified": True,
                "critical_gap_count": 0,
                "duplicate_count": 0,
                "reject_count": 0,
                "coverage": [
                    {
                        "start_ms": qualified_coverage[0].start_ms,
                        "end_ms": qualified_coverage[0].end_ms,
                    }
                ],
            },
            sort_keys=True,
        )
        + "\n"
    )
    report_sha256 = hashlib.sha256(report.read_bytes()).hexdigest()
    evidence = QualificationEvidence(
        report_path=report,
        report_sha256=report_sha256,
        coverage=qualified_coverage,
    )

    asset = segment.as_data_asset(evidence)

    assert candidate.qualification is Qualification.CANDIDATE
    assert "requires_event_level_qualification" in candidate.quality_flags
    assert asset.source_project is SourceProject.BOT05
    assert asset.tier is EvidenceTier.HYPERLIQUID_CANDLES
    assert asset.qualification is Qualification.QUALIFIED
    assert asset.channels == ("candles_5m",)
    assert asset.coverage[0].end_ms - asset.coverage[0].start_ms == 300_000
    plan = plan_requirement(
        LocalInventory((asset,), ()),
        DataRequirement("xyz:GOLD", "candles_5m", asset.coverage[0]),
        remote_fetch_enabled=False,
    )
    assert plan.action is AcquisitionAction.REUSE_LOCAL


def test_store_detects_post_write_corruption(tmp_path: Path) -> None:
    provenance = _provenance()
    store = AppendOnlyStore(tmp_path)
    segment = store.append(
        provenance.dataset_id,
        provenance,
        _normalization_result(),
    )
    segment.records_path.write_bytes(segment.records_path.read_bytes() + b"corrupt")

    with pytest.raises(AppendOnlyStoreError, match="records checksum"):
        store.read_records(segment)


def test_qualification_report_cannot_hide_a_critical_gap(tmp_path: Path) -> None:
    provenance = _provenance()
    segment = AppendOnlyStore(tmp_path).append(
        provenance.dataset_id, provenance, _normalization_result()
    )
    assert segment.coverage is not None
    coverage = (segment.coverage,)
    report = (tmp_path / "invalid-qualification.json").resolve()
    report.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "bot05_dataset_qualification",
                "dataset_id": segment.dataset_id,
                "segment_id": segment.segment_id,
                "source_raw_sha256": segment.source_raw_sha256,
                "records_sha256": segment.records_sha256,
                "qualified": True,
                "critical_gap_count": 1,
                "duplicate_count": 0,
                "reject_count": 0,
                "coverage": [
                    {
                        "start_ms": coverage[0].start_ms,
                        "end_ms": coverage[0].end_ms,
                    }
                ],
            },
            sort_keys=True,
        )
        + "\n"
    )
    evidence = QualificationEvidence(
        report.resolve(), hashlib.sha256(report.read_bytes()).hexdigest(), coverage
    )

    with pytest.raises(AppendOnlyStoreError, match="clean segment"):
        segment.as_data_asset(evidence)


def test_store_rejects_empty_or_mismatched_batches(tmp_path: Path) -> None:
    provenance = _provenance()
    store = AppendOnlyStore(tmp_path)
    empty = NormalizationResult((), ())

    with pytest.raises(AppendOnlyStoreError, match="empty"):
        store.append(provenance.dataset_id, provenance, empty)
    with pytest.raises(AppendOnlyStoreError, match="match provenance"):
        store.append("another-dataset", provenance, _normalization_result())
