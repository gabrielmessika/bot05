from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path

import bot05.shadow as shadow


def test_package_has_no_execution_gateway() -> None:
    assert importlib.util.find_spec("bot05.execution") is None


def test_runtime_dependencies_contain_no_exchange_or_secret_client() -> None:
    document = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    assert document["project"]["dependencies"] == []


def test_source_does_not_load_environment_or_private_keys() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in Path("src/bot05").rglob("*.py")
    )
    forbidden = ("load_dotenv", "private_key", "secret_key", "exchange.order")
    assert not any(token in source for token in forbidden)


def test_shadow_public_surface_has_no_action_method() -> None:
    action_prefixes = ("send", "submit", "place", "cancel")
    assert not any(name.lower().startswith(action_prefixes) for name in dir(shadow))


def test_collector_contains_contracts_but_no_network_transport() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in Path("src/bot05/collector").rglob("*.py")
    )
    forbidden = ("requests", "aiohttp", "websockets", "http.client", "socket.")
    assert not any(token in source for token in forbidden)
