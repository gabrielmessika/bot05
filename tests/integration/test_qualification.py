from __future__ import annotations

import gzip
import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from bot05.data.contracts import Qualification, TimeRange
from bot05.data.inventory import discover_local_inventory
from bot05.data.normalizer import NormalizationResult, NormalizedRecord
from bot05.data.qualification import (
    audit_hyperbot_segment,
    build_qualification_report,
    load_hyperbot_segment_spec,
    provenance_for_hyperbot_segment,
    select_hyperbot_segment_windows,
    write_qualification_report,
)
from bot05.data.reader import QualifiedStoreError, read_qualified_event_records
from bot05.data.store import AppendOnlyStore, QualificationEvidence


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()


def _h1_record(
    *,
    sequence: int,
    previous: str,
    exchange_ms: int,
    market: str,
    channel: str,
) -> tuple[bytes, str]:
    if channel == "trades":
        inner: dict[str, object] = {
            "coin": market,
            "hash": "0x" + ("0" * 64),
            "px": "100.5",
            "side": "B",
            "sz": "0.10",
            "tid": sequence,
            "time": exchange_ms,
            "users": ["0xmaker", "0xtaker"],
        }
    else:
        inner = {
            "bbo": [
                {"n": 2, "px": "100.4", "sz": "2.0"},
                {"n": 3, "px": "100.6", "sz": "3.0"},
            ],
            "coin": market,
            "time": exchange_ms,
        }
    payload = {
        "channel": channel,
        "coin": market,
        "context": {
            "code_version": "test",
            "config_hash": "c" * 64,
            "run_id": "fixture",
            "time_source": "exchange",
        },
        "exchange_ts_ms": exchange_ms,
        "local_sequence": sequence,
        "payload_json": _canonical(inner).decode(),
        "receive_monotonic_ns": sequence + 1,
        "receive_ts_ms": exchange_ms + 1,
    }
    base = {
        "event_type": "PublicMarketDataEvent",
        "payload": payload,
        "payload_sha256": hashlib.sha256(_canonical(payload)).hexdigest(),
        "previous_record_sha256": previous,
        "recorded_at_ms": exchange_ms,
        "schema_version": 2,
        "sequence": sequence,
        "stream": "public-market-data",
    }
    record_sha256 = hashlib.sha256(_canonical(base)).hexdigest()
    return _canonical({**base, "record_sha256": record_sha256}) + b"\n", record_sha256


def _source_fixture(root: Path) -> tuple[Path, str, int, int]:
    start_ms = 1_776_000_000_000
    definitions = (
        ("BTC", "bbo", 0),
        ("ETH", "bbo", 1_000),
        ("BTC", "trades", 2_000),
        ("BTC", "bbo", 5_000),
        ("BTC", "bbo", 10_000),
    )
    previous = "0" * 64
    lines: list[bytes] = []
    hashes: list[str] = []
    for sequence, (market, channel, offset) in enumerate(definitions):
        line, previous = _h1_record(
            sequence=sequence,
            previous=previous,
            exchange_ms=start_ms + offset,
            market=market,
            channel=channel,
        )
        lines.append(line)
        hashes.append(previous)
    content = b"".join(lines)
    source = root / "segment.jsonl.gz"
    root.mkdir(parents=True)
    with gzip.open(source, "wb") as handle:
        handle.write(content)
    storage_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    content_sha256 = hashlib.sha256(content).hexdigest()
    manifest = root / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "manifest_schema_version": 1,
                "stream": "public-market-data",
                "config": {"record_schema_version": 2},
                "config_sha256": "d" * 64,
                "segments": [
                    {
                        "begin_recorded_at_ms": start_ms,
                        "compression": "gzip",
                        "content_sha256": content_sha256,
                        "end_recorded_at_ms": start_ms + 10_000,
                        "first_record_sha256": hashes[0],
                        "first_sequence": 0,
                        "last_record_sha256": hashes[-1],
                        "last_sequence": len(lines) - 1,
                        "path": source.name,
                        "previous_segment_sha256": "0" * 64,
                        "record_count": len(lines),
                        "storage_sha256": storage_sha256,
                        "storage_tier": "hot",
                        "utc_date": "2026-04-12",
                    }
                ],
            },
            sort_keys=True,
        )
    )
    return manifest, source.name, start_ms, start_ms + 10_001


def test_bounded_h1_qualification_is_deterministic_and_planner_ready(
    tmp_path: Path,
) -> None:
    manifest, segment_name, start_ms, end_ms = _source_fixture(tmp_path / "source")
    spec = load_hyperbot_segment_spec(manifest, segment_name)
    provenance = provenance_for_hyperbot_segment(
        spec,
        dataset_id="h1-btc-fixture",
        code_version="sha256:" + ("e" * 64),
        config_sha256="f" * 64,
        market="BTC",
        channels=("trades", "bbo"),
    )
    window = TimeRange(start_ms, end_ms)

    audit = audit_hyperbot_segment(
        spec,
        provenance,
        market="BTC",
        channels=("trades", "bbo"),
        window=window,
    )
    assert audit.qualified is True
    assert len(audit.result.records) == 4
    assert {item.record.market for item in audit.result.records} == {"BTC"}
    assert audit.critical_gap_count == 0
    stored = AppendOnlyStore(tmp_path / "normalized").append(
        provenance.dataset_id, provenance, audit.result
    )
    report = build_qualification_report(audit, stored)
    report_path = (tmp_path / "reports" / "qualification.json").resolve()
    first_digest = write_qualification_report(report_path, report)
    second_digest = write_qualification_report(report_path, report)
    asset = stored.as_data_asset(
        QualificationEvidence(report_path, first_digest, (window,))
    )
    assert first_digest == second_digest
    assert asset.qualification is Qualification.QUALIFIED
    assert asset.coverage == (window,)
    inventory = discover_local_inventory(
        tmp_path / "source",
        tmp_path / "trident",
        bot05_data_root=tmp_path,
        qualification_root=report_path.parent,
    )
    discovered = next(
        item
        for item in inventory.assets
        if item.dataset_id.startswith("bot05-qualified")
    )
    assert discovered.qualification is Qualification.QUALIFIED
    assert discovered.coverage == (window,)


def test_heartbeat_gap_blocks_qualification(tmp_path: Path) -> None:
    manifest, segment_name, start_ms, end_ms = _source_fixture(tmp_path / "source")
    spec = load_hyperbot_segment_spec(manifest, segment_name)
    provenance = provenance_for_hyperbot_segment(
        spec,
        dataset_id="h1-btc-gap-fixture",
        code_version="test",
        config_sha256="f" * 64,
        market="BTC",
        channels=("trades", "bbo"),
    )

    audit = audit_hyperbot_segment(
        spec,
        provenance,
        market="BTC",
        channels=("trades", "bbo"),
        window=TimeRange(start_ms, end_ms),
        max_heartbeat_gap_ms=4_000,
    )

    assert audit.qualified is False
    assert audit.critical_gap_count > 0


def test_window_partition_resolves_a_bounded_physical_source(tmp_path: Path) -> None:
    manifest, segment_name, start_ms, end_ms = _source_fixture(tmp_path / "source")

    partitions = select_hyperbot_segment_windows(
        manifest, TimeRange(start_ms + 1_000, end_ms - 1_000)
    )

    assert len(partitions) == 1
    assert partitions[0][0].source_path.name == segment_name
    assert partitions[0][1] == TimeRange(start_ms + 1_000, end_ms - 1_000)


def test_qualified_reader_rechecks_store_and_combined_heartbeat(
    tmp_path: Path,
) -> None:
    manifest, segment_name, start_ms, end_ms = _source_fixture(tmp_path / "source")
    spec = load_hyperbot_segment_spec(manifest, segment_name)
    provenance = provenance_for_hyperbot_segment(
        spec,
        dataset_id="h1-btc-reader-fixture",
        code_version="reader-test",
        config_sha256="f" * 64,
        market="BTC",
        channels=("trades", "bbo"),
    )
    window = TimeRange(start_ms, end_ms)
    audit = audit_hyperbot_segment(
        spec,
        provenance,
        market="BTC",
        channels=("trades", "bbo"),
        window=window,
    )
    original_trade = next(
        item for item in audit.result.records if item.source_channel == "trades"
    )
    retransmission = NormalizedRecord(
        source_index=max(item.source_index for item in audit.result.records) + 1,
        source_record_sha256="9" * 64,
        source_channel="trades",
        record=replace(
            original_trade.record,
            received_at=original_trade.record.received_at + timedelta(milliseconds=1),
        ),
    )
    audit = replace(
        audit,
        result=NormalizationResult(
            audit.result.records + (retransmission,), audit.result.rejects
        ),
    )
    stored = AppendOnlyStore(tmp_path / "normalized").append(
        provenance.dataset_id, provenance, audit.result
    )
    report_root = tmp_path / "reports"
    report_path = (report_root / "qualification.json").resolve()
    write_qualification_report(report_path, build_qualification_report(audit, stored))

    first = read_qualified_event_records(
        (stored.manifest_path,),
        qualification_root=report_root,
        market="BTC",
        requested=window,
    )
    second = read_qualified_event_records(
        (stored.manifest_path,),
        qualification_root=report_root,
        market="BTC",
        requested=window,
    )

    assert len(first.trades) == 1
    assert len(first.books) == 3
    assert first.records_sha256 == second.records_sha256
    assert first.max_bbo_gap_ms == 5_000
    assert first.duplicate_trade_count == 1

    stored.records_path.write_bytes(stored.records_path.read_bytes() + b"corrupt")
    with pytest.raises(QualifiedStoreError, match="records checksum"):
        read_qualified_event_records(
            (stored.manifest_path,),
            qualification_root=report_root,
            market="BTC",
            requested=window,
        )
