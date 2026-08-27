from __future__ import annotations

import hashlib
import json
from pathlib import Path

from bot05.data.contracts import EvidenceTier, SourceProject, TimeRange
from bot05.data.inventory import _event_coverage, discover_local_inventory


def test_near_utc_boundaries_are_normalized_without_expanding_smoke_windows() -> None:
    day = 86_400_000
    assert _event_coverage(26, (2 * day) - 84) == TimeRange(0, 2 * day)
    assert _event_coverage(10_000_000, 10_100_000) == TimeRange(10_000_000, 10_100_001)


def _write_hyperbot_replay(root: Path) -> Path:
    path = (
        root
        / "data"
        / "replay_datasets"
        / "fetch-example"
        / "2026-08-16-BTC-aabbccddeeff0011.json"
    )
    path.parent.mkdir(parents=True)
    payload = {
        "schema_version": 3,
        "events": [],
        "metrics": {
            "book_count": 12,
            "bbo_count": 25,
            "trade_count": 8,
            "first_exchange_ts_ms": 1_766_016_000_010,
            "last_exchange_ts_ms": 1_766_102_399_990,
        },
    }
    raw = (json.dumps(payload) + "\n").encode()
    path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    path.with_suffix(".json.sha256").write_text(f"{digest}  {path.name}\n")
    return path


def _write_legacy_manifest(hyperbot_root: Path, trident_root: Path) -> Path:
    source = trident_root / "data" / "gbot_archive" / "trades" / "HYPE" / "day.jsonl"
    source.parent.mkdir(parents=True)
    source.write_text('{"coin":"HYPE"}\n')
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest = hyperbot_root / "reports" / "legacy_inventory" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "files": [
                    {
                        "source_path": str(source),
                        "first_timestamp_utc": "2026-04-01T09:50:00Z",
                        "last_timestamp_utc": "2026-04-01T16:40:00Z",
                        "sha256": digest,
                    }
                ]
            }
        )
    )
    return source


def test_discovers_shared_data_without_modifying_sources(tmp_path: Path) -> None:
    hyperbot_root = tmp_path / "hyperbot"
    trident_root = tmp_path / "trident"
    replay = _write_hyperbot_replay(hyperbot_root)
    legacy = _write_legacy_manifest(hyperbot_root, trident_root)
    (trident_root / "data" / "research").mkdir(parents=True)
    before = {
        path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (replay, legacy)
    }

    inventory = discover_local_inventory(hyperbot_root, trident_root)

    after = {
        path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (replay, legacy)
    }
    assert before == after
    assert len(inventory.assets) == 2
    replay_asset = next(
        asset
        for asset in inventory.assets
        if asset.source_project is SourceProject.HYPERBOT
    )
    assert replay_asset.tier is EvidenceTier.HYPERLIQUID_ARCHIVE
    assert replay_asset.channels == ("trades", "bbo", "l2")
    assert "shared_hyperbot_not_bot05_h2" in replay_asset.quality_flags
    legacy_asset = next(
        asset
        for asset in inventory.assets
        if asset.source_project is SourceProject.TRIDENT
    )
    assert legacy_asset.tier is EvidenceTier.LEGACY
    assert "legacy_research_only" in legacy_asset.quality_flags


def test_replay_without_checksum_is_reported_not_used(tmp_path: Path) -> None:
    hyperbot_root = tmp_path / "hyperbot"
    trident_root = tmp_path / "trident"
    replay = _write_hyperbot_replay(hyperbot_root)
    replay.with_suffix(".json.sha256").unlink()
    (hyperbot_root / "reports" / "legacy_inventory").mkdir(parents=True)
    (hyperbot_root / "reports" / "legacy_inventory" / "manifest.json").write_text(
        '{"files": []}'
    )
    (trident_root / "data" / "research").mkdir(parents=True)

    inventory = discover_local_inventory(hyperbot_root, trident_root)

    assert inventory.assets == ()
    assert any("SHA-256" in issue.reason for issue in inventory.issues)


def test_discovers_physical_raw_xyz_segments_before_remote_fetch(
    tmp_path: Path,
) -> None:
    hyperbot_root = tmp_path / "hyperbot"
    trident_root = tmp_path / "trident"
    (hyperbot_root / "data" / "replay_datasets").mkdir(parents=True)
    legacy_manifest = hyperbot_root / "reports" / "legacy_inventory" / "manifest.json"
    legacy_manifest.parent.mkdir(parents=True)
    legacy_manifest.write_text('{"files": []}')
    (trident_root / "data" / "research").mkdir(parents=True)

    relative = "data/raw/collector/public-market-data/segment.jsonl.gz"
    fetch_root = hyperbot_root / "data" / "server-fetches" / "fetch-example"
    segment = fetch_root / "payload" / relative
    segment.parent.mkdir(parents=True)
    segment.write_bytes(b"raw-segment")
    segment_sha = hashlib.sha256(segment.read_bytes()).hexdigest()
    manifest = fetch_root / "export" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "files": [{"path": relative, "sha256": segment_sha}],
                "runtime_status": {
                    "collector_status.json": {
                        "markets": ["xyz:GOLD", "BTC"],
                        "channels": ["l2Book", "bbo", "trades"],
                    }
                },
                "store_manifests": [
                    {
                        "manifest": {
                            "stream": "public-market-data",
                            "segments": [
                                {
                                    "path": segment.name,
                                    "begin_recorded_at_ms": 100,
                                    "end_recorded_at_ms": 200,
                                    "first_sequence": 0,
                                    "last_sequence": 10,
                                    "storage_sha256": segment_sha,
                                }
                            ],
                        }
                    }
                ],
            }
        )
    )

    inventory = discover_local_inventory(hyperbot_root, trident_root)

    asset = next(item for item in inventory.assets if "raw-export" in item.dataset_id)
    assert "xyz:GOLD" in asset.markets
    assert asset.channels == ("l2", "bbo", "trades")
    assert "physical_segments_present" in asset.quality_flags
