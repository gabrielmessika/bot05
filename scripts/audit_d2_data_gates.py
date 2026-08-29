#!/usr/bin/env python3
"""Publish local-only evidence for the remaining BOT05 D2 data gates."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NoReturn, cast

from bot05.config import load_config
from bot05.data.contracts import EvidenceTier, Qualification, SourceProject, TimeRange
from bot05.data.inventory import discover_local_inventory
from bot05.data.normalizer import file_sha256


class DataGateAuditError(ValueError):
    """Raised when local gate metadata is invalid or ambiguous."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=Path("config/research_btc_open_qualified.toml")
    )
    parser.add_argument(
        "--source-manifest",
        type=Path,
        default=Path(
            "/workspaces/hyperbot/data/server-fetches/"
            "fetch-20260822T070249Z-5d551f5b/payload/data/raw/collector/"
            "public-market-data/manifest.json"
        ),
    )
    parser.add_argument("--required-prior-sessions", type=int, default=20)
    parser.add_argument("--max-candidate-gap-ms", type=int, default=15_000)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/data_quality/d2_local_data_gates_2026-08-21.json"),
    )
    return parser


def _fail(message: str) -> NoReturn:
    raise SystemExit(message)


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise DataGateAuditError(f"{name} must be an object")
    return cast(Mapping[str, object], value)


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DataGateAuditError(f"{name} must be an integer")
    return value


def _ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _iso_ms(value: int) -> str:
    return (
        datetime.fromtimestamp(value / 1000, tz=UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _overlaps(left: TimeRange, right: TimeRange) -> bool:
    return left.start_ms < right.end_ms and right.start_ms < left.end_ms


def _window_metadata(
    date_start: datetime,
    duration: timedelta,
    segments: tuple[tuple[TimeRange, Path, str], ...],
) -> dict[str, object]:
    window = TimeRange(_ms(date_start), _ms(date_start + duration))
    relevant = tuple(item for item in segments if _overlaps(item[0], window))
    clipped = tuple(
        TimeRange(
            max(item[0].start_ms, window.start_ms),
            min(item[0].end_ms, window.end_ms),
        )
        for item in relevant
    )
    cursor = window.start_ms
    gaps: list[int] = []
    for span in sorted(clipped):
        if span.start_ms > cursor:
            gaps.append(span.start_ms - cursor)
        cursor = max(cursor, span.end_ms)
    if cursor < window.end_ms:
        gaps.append(window.end_ms - cursor)
    return {
        "date": date_start.date().isoformat(),
        "weekday": date_start.weekday() < 5,
        "start_utc": _iso_ms(window.start_ms),
        "end_utc": _iso_ms(window.end_ms),
        "segment_count": len(relevant),
        "all_segment_files_present": all(item[1].is_file() for item in relevant),
        "max_manifest_time_gap_ms": max(gaps, default=0),
        "segments": [
            {
                "path": str(item[1]),
                "storage_sha256": item[2],
            }
            for item in relevant
        ],
    }


def _immutable_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        if path.read_bytes() != payload:
            raise DataGateAuditError(
                f"refusing to overwrite immutable report: {path}"
            ) from None


def _render_markdown(report: Mapping[str, object], digest: str) -> str:
    h0 = _mapping(report["official_h0_gate"], "official_h0_gate")
    history = _mapping(report["rolling_history_gate"], "rolling_history_gate")
    return "\n".join(
        (
            "# BOT05 — audit local des gates de données D2",
            "",
            "Cet audit inspecte uniquement les manifests et assets locaux. Aucun appel",
            "réseau et aucun scan événementiel massif n’ont été effectués.",
            "",
            f"- SHA-256 du JSON : `{digest}`",
            (
                "- Assets H0 officiels chevauchant la fenêtre : "
                f"{h0['overlapping_asset_count']}"
            ),
            f"- Sessions antérieures requises : {history['required_prior_sessions']}",
            (
                "- Borne haute locale de dates candidates : "
                f"{history['candidate_upper_bound']}"
            ),
            f"- Déficit minimal : {history['minimum_shortfall']}",
            "",
            "## Conclusion",
            "",
            "- La parité H0 ne peut pas être fermée avec les fichiers locaux.",
            "- Le seuil de 20 historiques ne peut pas être atteint avant la cible,",
            "  même en comptant les week-ends comme candidats.",
            "- Les fenêtres H1 restent manifest-only : chacune doit",
            "  encore passer la qualification événementielle avant réutilisation.",
            "",
        )
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.required_prior_sessions <= 0 or args.max_candidate_gap_ms <= 0:
            raise DataGateAuditError("gate limits must be positive")
        loaded = load_config(args.config)
        config = loaded.config
        if len(config.universe.markets) != 1:
            raise DataGateAuditError("gate config must contain exactly one market")
        market = config.universe.markets[0]
        target_start = config.coverage.start_utc
        target_end = config.coverage.end_utc
        duration = target_end - target_start
        source_manifest = args.source_manifest.resolve()
        manifest = _mapping(json.loads(source_manifest.read_bytes()), "source manifest")
        if (
            manifest.get("manifest_schema_version") != 1
            or manifest.get("stream") != "public-market-data"
        ):
            raise DataGateAuditError("unsupported source manifest")
        raw_segments = manifest.get("segments")
        if not isinstance(raw_segments, list) or not raw_segments:
            raise DataGateAuditError("source manifest has no segments")
        segments: list[tuple[TimeRange, Path, str]] = []
        for raw in cast(list[object], raw_segments):
            segment = _mapping(raw, "source segment")
            start_ms = _integer(segment.get("begin_recorded_at_ms"), "segment start")
            end_ms = _integer(segment.get("end_recorded_at_ms"), "segment end") + 1
            relative = segment.get("path")
            storage_sha256 = segment.get("storage_sha256")
            if not isinstance(relative, str) or not isinstance(storage_sha256, str):
                raise DataGateAuditError("source segment identity is invalid")
            path = (source_manifest.parent / relative).resolve()
            path.relative_to(source_manifest.parent.resolve())
            segments.append((TimeRange(start_ms, end_ms), path, storage_sha256))
        ordered_segments = tuple(sorted(segments, key=lambda item: item[0]))

        earliest = datetime.fromtimestamp(
            ordered_segments[0][0].start_ms / 1000, tz=UTC
        )
        current_day_start = target_start.replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        session_offset = target_start - current_day_start
        candidate_windows: list[dict[str, object]] = []
        cursor_day = earliest.replace(hour=0, minute=0, second=0, microsecond=0)
        while cursor_day < current_day_start:
            candidate_windows.append(
                _window_metadata(
                    cursor_day + session_offset,
                    duration,
                    ordered_segments,
                )
            )
            cursor_day += timedelta(days=1)
        usable_candidates = [
            item
            for item in candidate_windows
            if item["segment_count"]
            and item["all_segment_files_present"] is True
            and cast(int, item["max_manifest_time_gap_ms"]) <= args.max_candidate_gap_ms
        ]

        qualification_root = Path("reports/data_quality/qualifications").resolve()
        inventory = discover_local_inventory(
            config.data.hyperbot_root,
            config.data.trident_root,
            bot05_data_root=config.data.local_data_dir,
            qualification_root=qualification_root,
        )
        if inventory.issues:
            raise DataGateAuditError("local inventory contains issues")
        target_range = TimeRange(_ms(target_start), _ms(target_end))
        h0_assets = tuple(
            item
            for item in inventory.assets
            if item.tier is EvidenceTier.HYPERLIQUID_CANDLES
            and market in item.markets
            and any(_overlaps(span, target_range) for span in item.coverage)
        )
        prior_qualified_dates = {
            datetime.fromtimestamp(span.start_ms / 1000, tz=UTC).date().isoformat()
            for item in inventory.assets
            if item.source_project is SourceProject.BOT05
            and item.qualification is Qualification.QUALIFIED
            and market in item.markets
            for span in item.coverage
            if span.end_ms <= target_range.start_ms
        }
        candidate_upper_bound = len(usable_candidates)
        report: dict[str, object] = {
            "schema_version": 1,
            "kind": "bot05_d2_local_data_gate_audit",
            "network_performed": False,
            "market": market,
            "target_start_utc": _iso_ms(target_range.start_ms),
            "target_end_utc": _iso_ms(target_range.end_ms),
            "config_sha256": loaded.sha256,
            "script_sha256": file_sha256(Path(__file__).resolve()),
            "source_manifest": {
                "path": str(source_manifest),
                "sha256": file_sha256(source_manifest),
                "integrity_scope": "manifest_only_no_segment_rehash",
            },
            "official_h0_gate": {
                "ready": bool(h0_assets),
                "overlapping_asset_count": len(h0_assets),
                "dataset_ids": [item.dataset_id for item in h0_assets],
            },
            "rolling_history_gate": {
                "ready": len(prior_qualified_dates) >= args.required_prior_sessions,
                "required_prior_sessions": args.required_prior_sessions,
                "qualified_prior_date_count": len(prior_qualified_dates),
                "candidate_upper_bound": candidate_upper_bound,
                "minimum_shortfall": max(
                    0, args.required_prior_sessions - candidate_upper_bound
                ),
                "upper_bound_includes_weekends": True,
                "calendar_eligibility_status": "not_evaluated",
                "candidate_integrity_status": "manifest_only_pending_event_audit",
                "candidate_windows": usable_candidates,
            },
        }
        payload = (
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        output = args.output.resolve()
        _immutable_write(output, payload)
        _immutable_write(
            output.with_suffix(output.suffix + ".sha256"),
            f"{digest}  {output.name}\n".encode("ascii"),
        )
        _immutable_write(
            output.with_suffix(".md"),
            _render_markdown(report, digest).encode("utf-8"),
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        _fail(str(exc))
    print(
        f"h0_assets={len(h0_assets)} candidate_upper_bound={candidate_upper_bound} "
        f"minimum_shortfall={report['rolling_history_gate']['minimum_shortfall']} "
        f"report_sha256={digest}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
