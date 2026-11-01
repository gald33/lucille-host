"""The ``lucille.exec/1`` spec as this package ships it, and the command line's ``--json``
contract: their JSON Schemas and golden fixtures.

The prose specs are ``spec/PROTOCOL.md``, with ``spec/PROTOCOL-CHANGELOG.md``, and ``spec/CLI.md``,
in the source repository and its sdist; the wheel does not carry them. The schemas and fixtures
live here, the protocol's under ``v1/`` and the command line's under ``cli/v1/``, so that the wheel
ships them and anyone holding an installed host can read the exact files it was built with.

Every file is found relative to this module's own file, so it is read from wherever the package is
installed. Nothing here imports by name or runs anything: the functions read JSON files and return
what they hold.

The runtime does not validate messages, or its own ``--json`` output, against these schemas. It
holds no JSON Schema library; its own checks are held to the fixtures, as Lucille's are, and its
tests hold every verb's output to the command line's schemas.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

__all__ = [
    "CLI_MAJOR",
    "CLI_TYPES",
    "FIXTURE_KINDS",
    "MAJOR",
    "PROTOCOL",
    "SCHEMA_NAMES",
    "cli_fixture_paths",
    "cli_schema_path",
    "cli_spec_dir",
    "default_host_state",
    "fixture_paths",
    "load_cli_fixture",
    "load_cli_schema",
    "load_fixture",
    "load_schema",
    "schema_path",
    "spec_dir",
]

PROTOCOL = "lucille.exec/1"
MAJOR = 1
# The schema files of this major: ``<name>.schema.json``.
SCHEMA_NAMES = ("request", "reply", "host.describe")
# The fixture directories of this major: ``fixtures/<kind>/*.json``.
FIXTURE_KINDS = ("request", "reply", "host.describe", "decode")

# The command line's contract (spec/CLI.md): its major, and the type of every object it writes,
# each with a schema file ``cli/v<major>/<type>.schema.json``: those of its section 6, in its
# order, then ``error`` (its section 5).
CLI_MAJOR = 1
CLI_TYPES = (
    "version",
    "op",
    "op_removed",
    "root",
    "root_removed",
    "root_check",
    "alias_suggestion",
    "controls",
    "status",
    "log_line",
    "forget",
    "pair_code",
    "policy_reset",
    "pair",
    "previous_policy",
    "error",
)

_HERE = Path(__file__).resolve().parent


def spec_dir() -> Path:
    """The directory that holds this major's schemas and fixtures."""
    return _HERE / f"v{MAJOR}"


def schema_path(name: str) -> Path:
    """The path of one schema, by its name in :data:`SCHEMA_NAMES`."""
    if name not in SCHEMA_NAMES:
        raise ValueError(f"no schema named {name!r}; the schemas are {', '.join(SCHEMA_NAMES)}")
    return spec_dir() / f"{name}.schema.json"


def load_schema(name: str) -> dict[str, Any]:
    """One schema, parsed."""
    return _read(schema_path(name))


def fixture_paths(kind: str) -> list[Path]:
    """The fixture files of one kind in :data:`FIXTURE_KINDS`, sorted by name."""
    if kind not in FIXTURE_KINDS:
        raise ValueError(f"no fixture kind {kind!r}; the kinds are {', '.join(FIXTURE_KINDS)}")
    return sorted((spec_dir() / "fixtures" / kind).glob("*.json"))


def load_fixture(path: Path) -> dict[str, Any]:
    """One fixture file, parsed. A string in it may hold an unpaired surrogate, on purpose.

    It reads whatever path it is given; :func:`fixture_paths` lists the ones this package ships.
    """
    return _read(path)


def default_host_state() -> dict[str, Any]:
    """The host state a request fixture assumes, before its own ``host_state`` overrides keys."""
    return _read(spec_dir() / "fixtures" / "host-state.json")


def cli_spec_dir() -> Path:
    """The directory that holds the command line contract's schemas and fixtures."""
    return _HERE / "cli" / f"v{CLI_MAJOR}"


def cli_schema_path(name: str) -> Path:
    """The path of the schema of one type in :data:`CLI_TYPES`."""
    if name not in CLI_TYPES:
        raise ValueError(f"no command line type {name!r}")
    return cli_spec_dir() / f"{name}.schema.json"


def load_cli_schema(name: str) -> dict[str, Any]:
    """One schema of the command line contract, parsed."""
    return _read(cli_schema_path(name))


def cli_fixture_paths() -> list[Path]:
    """The command line contract's fixture files, sorted by name."""
    return sorted((cli_spec_dir() / "fixtures").glob("*.json"))


def load_cli_fixture(path: Path) -> dict[str, Any]:
    """One fixture file of the command line contract, parsed."""
    return _read(path)


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} does not hold a JSON object")
    return value
