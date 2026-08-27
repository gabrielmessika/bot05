"""Strict, fail-closed BOT05 research configuration."""

from __future__ import annotations

import hashlib
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

ALLOWED_CHANNELS = frozenset(
    {"trades", "bbo", "l2", "candles_1m", "candles_5m", "market_context"}
)


class ConfigurationError(ValueError):
    """Raised when a BOT05 configuration is incomplete or inconsistent."""


class UnsafeConfigurationError(ConfigurationError):
    """Raised when a configuration attempts to enable an unsafe mode."""


@dataclass(frozen=True, slots=True)
class ModeConfig:
    live_enabled: bool
    shadow_only: bool
    public_data_only: bool

    def __post_init__(self) -> None:
        if self.live_enabled:
            raise UnsafeConfigurationError(
                "live trading is not implemented or authorized"
            )
        if not self.shadow_only:
            raise UnsafeConfigurationError("research requires shadow_only = true")
        if not self.public_data_only:
            raise UnsafeConfigurationError("research requires public_data_only = true")


@dataclass(frozen=True, slots=True)
class DataConfig:
    local_data_dir: Path
    hyperbot_root: Path
    trident_root: Path
    local_first: bool
    remote_fetch_enabled: bool

    def __post_init__(self) -> None:
        if not self.local_first:
            raise UnsafeConfigurationError("local_first must remain enabled")
        for name, root in (
            ("hyperbot_root", self.hyperbot_root),
            ("trident_root", self.trident_root),
        ):
            if not root.is_absolute():
                raise ConfigurationError(f"{name} must be an absolute path")


@dataclass(frozen=True, slots=True)
class CoverageConfig:
    start_utc: datetime
    end_utc: datetime
    channels: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.start_utc >= self.end_utc:
            raise ConfigurationError("coverage start_utc must precede end_utc")
        if not self.channels:
            raise ConfigurationError("coverage channels must not be empty")
        if len(set(self.channels)) != len(self.channels):
            raise ConfigurationError("coverage channels must be unique")
        unknown = set(self.channels) - ALLOWED_CHANNELS
        if unknown:
            raise ConfigurationError(
                f"unsupported coverage channels: {sorted(unknown)}"
            )


@dataclass(frozen=True, slots=True)
class UniverseConfig:
    markets: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.markets or any(not market.strip() for market in self.markets):
            raise ConfigurationError("universe markets must be non-empty strings")
        if len(set(self.markets)) != len(self.markets):
            raise ConfigurationError("universe markets must be unique")


@dataclass(frozen=True, slots=True)
class ReportingConfig:
    output_json: Path
    output_markdown: Path

    def __post_init__(self) -> None:
        if self.output_json.suffix != ".json":
            raise ConfigurationError("output_json must have a .json suffix")
        if self.output_markdown.suffix != ".md":
            raise ConfigurationError("output_markdown must have a .md suffix")


@dataclass(frozen=True, slots=True)
class Bot05Config:
    mode: ModeConfig
    data: DataConfig
    coverage: CoverageConfig
    universe: UniverseConfig
    reporting: ReportingConfig


@dataclass(frozen=True, slots=True)
class LoadedConfig:
    config: Bot05Config
    source_path: Path
    sha256: str


def _exact_keys(section: Mapping[str, object], name: str, expected: set[str]) -> None:
    actual = set(section)
    missing = expected - actual
    extra = actual - expected
    if missing or extra:
        raise ConfigurationError(
            f"[{name}] keys mismatch; missing={sorted(missing)}, extra={sorted(extra)}"
        )


def _section(document: Mapping[str, object], name: str) -> Mapping[str, object]:
    value = document.get(name)
    if not isinstance(value, dict):
        raise ConfigurationError(f"missing or invalid [{name}] section")
    return cast(Mapping[str, object], value)


def _boolean(section: Mapping[str, object], key: str) -> bool:
    value = section[key]
    if not isinstance(value, bool):
        raise ConfigurationError(f"{key} must be a boolean")
    return value


def _string(section: Mapping[str, object], key: str) -> str:
    value = section[key]
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{key} must be a non-empty string")
    return value


def _strings(section: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = section[key]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigurationError(f"{key} must be an array of strings")
    strings = cast(list[str], value)
    if any(not item.strip() for item in strings):
        raise ConfigurationError(f"{key} must not contain blank strings")
    return tuple(strings)


def _utc_datetime(section: Mapping[str, object], key: str) -> datetime:
    raw = _string(section, key)
    if not raw.endswith("Z"):
        raise ConfigurationError(f"{key} must use an explicit UTC Z suffix")
    try:
        parsed = datetime.fromisoformat(raw.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise ConfigurationError(f"{key} must be an ISO-8601 UTC timestamp") from exc
    if parsed.tzinfo != UTC:
        raise ConfigurationError(f"{key} must be UTC")
    return parsed


def _project_path(project_root: Path, raw: str) -> Path:
    path = Path(raw)
    if path.is_absolute():
        return path
    return project_root / path


def load_config(path: str | Path) -> LoadedConfig:
    """Load a strict TOML file and return its exact SHA-256."""

    source_path = Path(path).resolve()
    raw = source_path.read_bytes()
    try:
        parsed = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError(f"cannot parse {source_path}") from exc
    document = cast(Mapping[str, object], parsed)
    _exact_keys(document, "root", {"mode", "data", "coverage", "universe", "reporting"})

    mode_section = _section(document, "mode")
    _exact_keys(
        mode_section,
        "mode",
        {"live_enabled", "shadow_only", "public_data_only"},
    )
    mode = ModeConfig(
        live_enabled=_boolean(mode_section, "live_enabled"),
        shadow_only=_boolean(mode_section, "shadow_only"),
        public_data_only=_boolean(mode_section, "public_data_only"),
    )

    project_root = source_path.parent.parent
    data_section = _section(document, "data")
    _exact_keys(
        data_section,
        "data",
        {
            "local_data_dir",
            "hyperbot_root",
            "trident_root",
            "local_first",
            "remote_fetch_enabled",
        },
    )
    data = DataConfig(
        local_data_dir=_project_path(
            project_root, _string(data_section, "local_data_dir")
        ),
        hyperbot_root=Path(_string(data_section, "hyperbot_root")),
        trident_root=Path(_string(data_section, "trident_root")),
        local_first=_boolean(data_section, "local_first"),
        remote_fetch_enabled=_boolean(data_section, "remote_fetch_enabled"),
    )

    coverage_section = _section(document, "coverage")
    _exact_keys(coverage_section, "coverage", {"start_utc", "end_utc", "channels"})
    coverage = CoverageConfig(
        start_utc=_utc_datetime(coverage_section, "start_utc"),
        end_utc=_utc_datetime(coverage_section, "end_utc"),
        channels=_strings(coverage_section, "channels"),
    )

    universe_section = _section(document, "universe")
    _exact_keys(universe_section, "universe", {"markets"})
    universe = UniverseConfig(markets=_strings(universe_section, "markets"))

    reporting_section = _section(document, "reporting")
    _exact_keys(reporting_section, "reporting", {"output_json", "output_markdown"})
    reporting = ReportingConfig(
        output_json=_project_path(
            project_root, _string(reporting_section, "output_json")
        ),
        output_markdown=_project_path(
            project_root, _string(reporting_section, "output_markdown")
        ),
    )

    return LoadedConfig(
        config=Bot05Config(
            mode=mode,
            data=data,
            coverage=coverage,
            universe=universe,
            reporting=reporting,
        ),
        source_path=source_path,
        sha256=hashlib.sha256(raw).hexdigest(),
    )
