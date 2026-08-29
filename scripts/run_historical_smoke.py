#!/usr/bin/env python3
"""Run one immutable, explicitly limited historical BOT05 pipeline smoke."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import NoReturn

from bot05.replay.historical import (
    HistoricalSmokeError,
    load_historical_smoke_spec,
    run_historical_smoke,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def _immutable_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        if path.read_bytes() != payload:
            raise HistoricalSmokeError(
                f"refusing to overwrite immutable report: {path}"
            ) from None


def _markdown(report: dict[str, object], digest: str) -> str:
    strategy = report["strategy"]
    risk = report["risk"]
    replays = report["replays"]
    assert isinstance(strategy, dict) and isinstance(risk, dict)
    assert isinstance(replays, list)
    lines = [
        "# BOT05 — premier smoke replay historique",
        "",
        f"- SHA-256 du JSON : `{digest}`",
        f"- Marché/session : `{report['market']}` / `{report['session_id']}`",
        f"- Signal : `{strategy['direction']}` à `{strategy['decision_time']}`",
        f"- Gate risque : `{risk['accepted']}`",
        "- Conclusion : `données insuffisantes`",
        "- Promotion : interdite",
        "",
        "## Modèles",
        "",
    ]
    for item in replays:
        assert isinstance(item, dict)
        lines.append(
            f"- `{item['model']}` : `{item['status']}`, "
            f"sortie `{item['exit_reason']}`, PnL net `{item['net_pnl']}`"
        )
    lines.extend(
        (
            "",
            "## Limites",
            "",
            "Une seule session H1 est observée. Les frais, le calendrier, la",
            "définition de marché et le funding ne disposent pas tous d’un snapshot",
            "historique local qualifié. Ce run valide la chaîne technique, pas l’edge.",
            "",
        )
    )
    return "\n".join(lines)


def _fail(message: str) -> NoReturn:
    raise SystemExit(message)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = run_historical_smoke(load_historical_smoke_spec(args.config))
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
            output.with_suffix(".md"), _markdown(report, digest).encode("utf-8")
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        _fail(str(exc))
    print(f"report_id={report['report_id']} report_sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
