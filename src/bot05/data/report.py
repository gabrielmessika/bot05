"""Deterministic JSON and Markdown reports for local data coverage."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

from bot05.config import LoadedConfig, load_config
from bot05.data.contracts import (
    DataAsset,
    DataRequirement,
    InventoryIssue,
    RequirementPlan,
    TimeRange,
)
from bot05.data.inventory import discover_local_inventory
from bot05.data.planner import plan_inventory

REPORT_SCHEMA_VERSION = 2


class ReportExistsError(RuntimeError):
    """Raised when a published report would be overwritten with new content."""


def code_sha256() -> str:
    """Fingerprint package paths and bytes without relying on mutable Git state."""

    package_root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for path in sorted(package_root.rglob("*.py")):
        digest.update(path.relative_to(package_root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _iso(timestamp_ms: int) -> str:
    value = datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _range_dict(span: TimeRange) -> dict[str, object]:
    return {
        "start_ms": span.start_ms,
        "start_utc": _iso(span.start_ms),
        "end_ms": span.end_ms,
        "end_utc": _iso(span.end_ms),
    }


def _asset_dict(asset: DataAsset) -> dict[str, object]:
    return {
        "dataset_id": asset.dataset_id,
        "source_project": asset.source_project.value,
        "tier": asset.tier.value,
        "path": str(asset.path),
        "markets": list(asset.markets),
        "channels": list(asset.channels),
        "coverage": [_range_dict(span) for span in asset.coverage],
        "provenance_sha256": asset.provenance_sha256,
        "qualification": asset.qualification.value,
        "quality_flags": list(asset.quality_flags),
    }


def _issue_dict(issue: InventoryIssue) -> dict[str, str]:
    return {"source": issue.source, "reason": issue.reason}


def _plan_dict(plan: RequirementPlan) -> dict[str, object]:
    requirement = plan.requirement
    return {
        "market": requirement.market,
        "channel": requirement.channel,
        "required_coverage": _range_dict(requirement.coverage),
        "action": plan.action.value,
        "reusable_dataset_ids": list(plan.reusable_dataset_ids),
        "qualification_dataset_ids": list(plan.qualification_dataset_ids),
        "remote_fetch_ranges": [_range_dict(span) for span in plan.remote_fetch_ranges],
        "derivations": list(plan.derivations),
    }


def _milliseconds(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def build_report(loaded: LoadedConfig) -> dict[str, object]:
    """Build a deterministic report from configuration and local metadata."""

    config = loaded.config
    inventory = discover_local_inventory(
        config.data.hyperbot_root, config.data.trident_root
    )
    coverage = TimeRange(
        _milliseconds(config.coverage.start_utc),
        _milliseconds(config.coverage.end_utc),
    )
    requirements = tuple(
        DataRequirement(market=market, channel=channel, coverage=coverage)
        for market in config.universe.markets
        for channel in config.coverage.channels
    )
    plans = plan_inventory(
        inventory,
        requirements,
        remote_fetch_enabled=config.data.remote_fetch_enabled,
    )
    source_counts = Counter(asset.source_project.value for asset in inventory.assets)
    tier_counts = Counter(asset.tier.value for asset in inventory.assets)
    action_counts = Counter(plan.action.value for plan in plans)

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "kind": "bot05_local_first_acquisition_plan",
        "configuration": {
            "path": str(loaded.source_path),
            "sha256": loaded.sha256,
            "code_sha256": code_sha256(),
            "local_first": config.data.local_first,
            "remote_fetch_enabled": config.data.remote_fetch_enabled,
            "network_performed": False,
            "hyperbot_root": str(config.data.hyperbot_root),
            "trident_root": str(config.data.trident_root),
        },
        "policy": {
            "shared_hyperbot_tier": "H1",
            "shared_hyperbot_can_be_h2": False,
            "trident_default_tier": "L",
            "candidate_overlap_blocks_remote_fetch_until_qualified": True,
        },
        "inventory_summary": {
            "asset_count": len(inventory.assets),
            "issue_count": len(inventory.issues),
            "source_counts": dict(sorted(source_counts.items())),
            "tier_counts": dict(sorted(tier_counts.items())),
        },
        "plan_summary": {
            "requirement_count": len(plans),
            "action_counts": dict(sorted(action_counts.items())),
            "remote_fetch_range_count": sum(
                len(plan.remote_fetch_ranges) for plan in plans
            ),
        },
        "assets": [_asset_dict(asset) for asset in inventory.assets],
        "issues": [_issue_dict(issue) for issue in inventory.issues],
        "requirements": [_plan_dict(plan) for plan in plans],
    }


def canonical_report_bytes(report: dict[str, object]) -> bytes:
    return (
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _format_ranges(ranges: list[dict[str, object]]) -> str:
    if not ranges:
        return "—"
    if len(ranges) == 1:
        item = ranges[0]
        return f"{item['start_utc']} → {item['end_utc']}"
    return f"{len(ranges)} intervalles"


def render_markdown(report: dict[str, object], json_sha256: str) -> str:
    summary = report["inventory_summary"]
    plan_summary = report["plan_summary"]
    configuration = report["configuration"]
    assert isinstance(summary, dict)
    assert isinstance(plan_summary, dict)
    assert isinstance(configuration, dict)
    requirements = report["requirements"]
    issues = report["issues"]
    assert isinstance(requirements, list)
    assert isinstance(issues, list)

    lines = [
        "# BOT05 — couverture locale initiale",
        "",
        "Ce rapport est un plan d'acquisition local-first. Aucun appel réseau n'a été",
        "effectué. Les datasets partagés restent candidats tant que leur schéma, leur",
        "intégrité et leurs gaps n'ont pas été qualifiés par BOT05.",
        "",
        f"- SHA-256 du JSON : `{json_sha256}`",
        f"- SHA-256 du code : `{configuration['code_sha256']}`",
        f"- SHA-256 de configuration : `{configuration['sha256']}`",
        f"- Assets découverts : {summary['asset_count']}",
        f"- Problèmes d'inventaire : {summary['issue_count']}",
        f"- Besoins marché/canal : {plan_summary['requirement_count']}",
        f"- Fetch distant activé : `{configuration['remote_fetch_enabled']}`",
        "",
        "## Décisions par besoin",
        "",
        "| Marché | Canal | Action | Candidats locaux | Gaps distants |",
        "|---|---|---|---:|---|",
    ]
    for raw in requirements:
        assert isinstance(raw, dict)
        candidates = raw["qualification_dataset_ids"]
        ranges = raw["remote_fetch_ranges"]
        assert isinstance(candidates, list)
        assert isinstance(ranges, list)
        lines.append(
            f"| `{raw['market']}` | `{raw['channel']}` | `{raw['action']}` | "
            f"{len(candidates)} | {_format_ranges(ranges)} |"
        )

    lines.extend(["", "## Limites", ""])
    lines.extend(
        [
            "- H1 partagé ne vaut pas H2 BOT05 : la provenance du collector "
            "est conservée.",
            "- L legacy sert à la pré-recherche, jamais seul à une preuve d'exécution.",
            "- Un candidat local doit passer checksum, schéma, timestamps, "
            "doublons et gaps.",
            "- Les gaps listés ne sont exécutables que dans un futur lot de "
            "fetch public.",
        ]
    )
    if issues:
        lines.extend(["", "## Problèmes d'inventaire", ""])
        for raw in issues:
            assert isinstance(raw, dict)
            lines.append(f"- `{raw['source']}` : {raw['reason']}")
    return "\n".join(lines) + "\n"


def _immutable_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() == payload:
            return
        raise ReportExistsError(f"refusing to overwrite published report: {path}")
    path.write_bytes(payload)


def write_report(loaded: LoadedConfig, report: dict[str, object]) -> str:
    json_payload = canonical_report_bytes(report)
    json_sha256 = hashlib.sha256(json_payload).hexdigest()
    markdown = render_markdown(report, json_sha256).encode("utf-8")
    json_path = loaded.config.reporting.output_json
    markdown_path = loaded.config.reporting.output_markdown
    _immutable_write(json_path, json_payload)
    _immutable_write(markdown_path, markdown)
    _immutable_write(
        json_path.with_suffix(json_path.suffix + ".sha256"),
        f"{json_sha256}  {json_path.name}\n".encode(),
    )
    return json_sha256


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/research.toml", type=Path)
    return parser


def _fail(message: str) -> NoReturn:
    raise SystemExit(message)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        loaded = load_config(args.config)
        report = build_report(loaded)
        digest = write_report(loaded, report)
    except (OSError, ValueError, ReportExistsError) as exc:
        _fail(str(exc))
    print(f"wrote local-first report sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
