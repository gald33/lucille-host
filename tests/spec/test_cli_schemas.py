"""The JSON Schemas of the command line's contract (spec/CLI.md): closed at every level, bounded
where the host bounds a value, portable, and consistent with one another and with the fixtures.

The walk is the one ``test_schemas.py`` makes of the protocol's schemas. A value the host does not
bound (a path, a sentence for people, a line of the log, a skew) is named in :data:`UNBOUNDED`.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from lucille_host import spec
from tests.spec import _support
from tests.spec.test_schemas import KNOWN, subschemas

DIALECT = "https://json-schema.org/draft/2020-12/schema"
ID_BASE = "https://github.com/gald33/lucille-host/blob/main/src/lucille_host/spec/cli/v1/"
NO_NUL = "^[^\\u0000]*$"
PATH = "^/[^\\u0000]*$"
# A path inside a folder alias, as a `request` object writes it: never one that starts with /.
RELATIVE = "^[^/\\u0000][^\\u0000]*$"
TYPES = {"object", "string", "integer", "number", "boolean", "array", "null"}
# Where the host sets no bound: the subschemas, by their place, that may have none.
UNBOUNDED = (
    "#/$defs/path",
    "#/$defs/message",
    "#/$defs/policy_op_name",
    "log_line:#/properties/text",
    "/properties/skew_s",
    "/properties/enabled",
    "/properties/disabled",
    "/properties/work_rooms",
    "/properties/ops",
)


def _schemas() -> list[tuple[str, dict[str, Any]]]:
    return [(name, spec.load_cli_schema(name)) for name in spec.CLI_TYPES]


SCHEMAS = _schemas()
NODES = [
    (f"{name}:{where}", node) for name, schema in SCHEMAS for where, node in subschemas(schema)
]


def _unbounded(where: str) -> bool:
    return any(where.endswith(place) or place in where for place in UNBOUNDED)


@pytest.mark.parametrize(("name", "schema"), SCHEMAS, ids=[n for n, _ in SCHEMAS])
def test_cli_schemas_closed(name: str, schema: dict[str, Any]) -> None:
    Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == DIALECT
    assert schema["$id"] == f"{ID_BASE}{name}.schema.json"
    # Every object the type allows is closed, lists every field it allows as required, and
    # carries v 1 and its own type.
    branches = [
        node
        for _, node in subschemas(schema)
        if node.get("type") == "object" and "type" in node.get("properties", {})
    ]
    assert branches, name
    for branch in branches:
        assert branch["required"] == list(branch["properties"])
        assert branch["properties"]["v"] == {"const": 1}
        assert branch["properties"]["type"] == {"const": name}
        assert list(branch["properties"])[:2] == ["v", "type"]


def test_every_object_is_closed_and_lists_its_fields() -> None:
    problems = []
    for where, node in NODES:
        names_properties = {"properties", "required", "additionalProperties"} & set(node)
        if node.get("type") == "object" or names_properties:
            if node.get("type") != "object" or node.get("additionalProperties") is not False:
                problems.append(where)
            elif "properties" not in node or node.get("required") != list(node["properties"]):
                problems.append(where)
    assert problems == []


def test_every_subschema_is_constrained_with_known_keywords() -> None:
    unconstrained = [
        where for where, node in NODES if not {"type", "const", "enum", "$ref", "oneOf"} & set(node)
    ]
    assert unconstrained == []
    unknown = [(where, sorted(set(node) - KNOWN)) for where, node in NODES if set(node) - KNOWN]
    assert unknown == []
    assert all(node["type"] in TYPES for _, node in NODES if "type" in node)


def test_every_value_is_bounded_where_the_host_bounds_it() -> None:
    unbounded = []
    for where, node in NODES:
        kind = node.get("type")
        if _unbounded(where):
            continue
        if kind == "string" and "enum" not in node and "maxLength" not in node:
            unbounded.append(where)
        if kind == "integer" and not {"minimum", "maximum"} <= set(node):
            unbounded.append(where)
        if kind == "array" and "maxItems" not in node:
            unbounded.append(where)
        if kind == "number":
            unbounded.append(where)
    assert unbounded == []


def test_every_string_refuses_nul_and_every_pattern_is_portable() -> None:
    problems = []
    for where, node in NODES:
        if node.get("type") == "string" and "enum" not in node and "pattern" not in node:
            problems.append((where, "a string with neither a fixed value nor a pattern"))
        pattern = node.get("pattern")
        if pattern is None:
            continue
        if not (pattern.startswith("^") and pattern.endswith("$")) or pattern.count("$") != 1:
            problems.append((where, "not anchored with one ^ and one $"))
        if re.search(r"\\[dDwWsSbB]|\(\?", pattern):
            problems.append((where, "a class or construct that differs between dialects"))
        outside_classes = re.sub(r"\[(?:\\.|[^\]])*\]", "", pattern)
        # Negated classes that cannot match U+0000, since each names it.
        free = pattern in (NO_NUL, PATH, RELATIVE)
        if not free and ("[^" in pattern or re.search(r"(?<!\\)\.", outside_classes)):
            problems.append((where, "a negated class or a bare '.' could match U+0000"))
        if _support.ecma_regex(pattern).search("\x00") is not None:
            problems.append((where, "matches U+0000"))
    assert problems == []


def test_shared_definitions_agree_across_files() -> None:
    defs: dict[str, dict[str, Any]] = {}
    for name, schema in SCHEMAS:
        for key, value in schema.get("$defs", {}).items():
            if key in defs:
                assert defs[key] == value, f"$defs/{key} differs in {name}"
            defs[key] = value
    # The ones the protocol's schemas have too are the protocol's, word for word.
    protocol: dict[str, dict[str, Any]] = {}
    for name in spec.SCHEMA_NAMES:
        protocol.update(spec.load_schema(name).get("$defs", {}))
    for key in (
        "host_version",
        "op_name",
        "root",
        "job_id",
        "min_host_version",
        "refused_code",
        "failed_code",
        "exit_code",
        "duration_ms",
    ):
        assert defs[key] == protocol[key], key


def test_status_never_has_a_field_for_the_sas() -> None:
    def names(value: Any) -> set[str]:
        if isinstance(value, dict):
            found = set(value.get("properties", {}))
            for item in value.values():
                found |= names(item)
            return found
        if isinstance(value, list):
            return set().union(*(names(item) for item in value)) if value else set()
        return set()

    assert "sas" not in names(spec.load_cli_schema("status"))
    assert "sas" in names(spec.load_cli_schema("pair"))


def test_every_type_has_a_schema_and_a_fixture() -> None:
    files = sorted(path.name for path in spec.cli_spec_dir().glob("*.schema.json"))
    assert files == sorted(f"{name}.schema.json" for name in spec.CLI_TYPES)
    written = set()
    for path in spec.cli_fixture_paths():
        fixture = spec.load_cli_fixture(path)
        assert set(fixture) == {"description", "argv", "exit", "stdout"}, path.name
        written |= {found["type"] for found in fixture["stdout"]}
    assert written == set(spec.CLI_TYPES)


def test_every_fixture_validates() -> None:
    errors = {}
    for path in spec.cli_fixture_paths():
        fixture = spec.load_cli_fixture(path)
        for index, found in enumerate(fixture["stdout"]):
            schema = spec.load_cli_schema(found["type"])
            messages = [
                error.message for error in _support.validator_for(schema).iter_errors(found)
            ]
            if messages:
                errors[f"{path.name}[{index}]"] = messages
        # Fixtures are ASCII, as the output is.
        assert path.read_bytes().isascii(), path.name
        assert json.loads(path.read_text(encoding="ascii")) == fixture
    assert errors == {}
