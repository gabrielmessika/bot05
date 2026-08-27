from __future__ import annotations

import json
from pathlib import Path

from bot05.config import load_config
from bot05.data.report import build_report, canonical_report_bytes, write_report


def test_empty_local_roots_produce_deterministic_no_network_report(
    tmp_path: Path,
) -> None:
    hyperbot = tmp_path / "hyperbot"
    trident = tmp_path / "trident"
    (hyperbot / "data" / "replay_datasets").mkdir(parents=True)
    (hyperbot / "reports" / "legacy_inventory").mkdir(parents=True)
    (hyperbot / "reports" / "legacy_inventory" / "manifest.json").write_text(
        '{"files": []}'
    )
    (trident / "data" / "research").mkdir(parents=True)
    config = tmp_path / "config" / "research.toml"
    config.parent.mkdir()
    config.write_text(
        f"""
[mode]
live_enabled = false
shadow_only = true
public_data_only = true
[data]
local_data_dir = "data"
hyperbot_root = "{hyperbot}"
trident_root = "{trident}"
local_first = true
remote_fetch_enabled = false
[coverage]
start_utc = "2026-08-16T00:00:00Z"
end_utc = "2026-08-17T00:00:00Z"
channels = ["trades"]
[universe]
markets = ["BTC"]
[reporting]
output_json = "reports/coverage.json"
output_markdown = "reports/coverage.md"
""".strip()
        + "\n"
    )
    loaded = load_config(config)

    first = build_report(loaded)
    second = build_report(loaded)

    assert canonical_report_bytes(first) == canonical_report_bytes(second)
    assert first["configuration"]["network_performed"] is False  # type: ignore[index]
    digest = write_report(loaded, first)
    assert len(digest) == 64
    stored = json.loads(loaded.config.reporting.output_json.read_text())
    assert stored["requirements"][0]["action"] == "remote_fetch_disabled"
    write_report(loaded, second)
