"""The JSON Schemas of lucille.exec/1: closed at every level, bounded, portable and consistent.

The walk below visits every subschema of every schema file. A subschema is either a combinator
(``oneOf``), a reference, a fixed value (``const`` or ``enum``) or a typed schema. A schema with
none of these would accept any object, so it fails. A typed object schema must list its
properties and set ``additionalProperties`` to false; so must every schema that names properties.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from lucille_host import spec
from tests.spec import _support

DIALECT = "https://json-schema.org/draft/2020-12/schema"
ID_BASE = "https://github.com/gald33/lucille-host/blob/main/src/lucille_host/spec/v1/"
NO_NUL = "^[^\\u0000]*$"

# Keywords whose value is a map of names to subschemas, a list of subschemas, or one subschema.
SCHEMA_MAPS = {"$defs", "properties"}
SCHEMA_LISTS = {"oneOf"}
SCHEMA_VALUES = {"items"}
# Keywords whose value is data, not a subschema.
VALUE_KEYWORDS = {
    "$schema",
    "$id",
    "$ref",
    "title",
    "description",
    "type",
    "const",
    "enum",
    "required",
    "additionalProperties",
    "dependentRequired",
    "minLength",
    "maxLength",
    "pattern",
    "minimum",
    "maximum",
    "maxItems",
    "uniqueItems",
}
KNOWN = SCHEMA_MAPS | SCHEMA_LISTS | SCHEMA_VALUES | VALUE_KEYWORDS
TYPES = {"object", "string", "integer", "boolean", "array"}


def subschemas(schema: Mapping[str, Any], where: str = "#") -> Iterator[tuple[str, dict]]:
    """Every subschema with its JSON pointer, the schema itself first."""
    yield where, dict(schema)
    for keyword, value in schema.items():
        if keyword in SCHEMA_MAPS:
            for name, sub in value.items():
                yield from subschemas(sub, f"{where}/{keyword}/{name}")
        elif keyword in SCHEMA_LISTS:
            for index, sub in enumerate(value):
                yield from subschemas(sub, f"{where}/{keyword}/{index}")
        elif keyword in SCHEMA_VALUES:
            yield from subschemas(value, f"{where}/{keyword}")


def _schemas() -> list[tuple[str, dict[str, Any]]]:
    return [(name, spec.load_schema(name)) for name in spec.SCHEMA_NAMES]


SCHEMAS = _schemas()
NODES = [
    (f"{name}:{where}", node) for name, schema in SCHEMAS for where, node in subschemas(schema)
]


@pytest.mark.parametrize(("name", "schema"), SCHEMAS, ids=[n for n, _ in SCHEMAS])
def test_schema_is_a_valid_2020_12_schema(name: str, schema: dict[str, Any]) -> None:
    Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == DIALECT
    assert schema["$id"] == f"{ID_BASE}{name}.schema.json"


def test_every_object_has_additional_properties_false() -> None:
    open_objects = []
    for where, node in NODES:
        names_properties = {"properties", "required", "additionalProperties"} & set(node)
        if node.get("type") == "object" or names_properties:
            if node.get("type") != "object" or node.get("additionalProperties") is not False:
                open_objects.append(where)
            elif "properties" not in node:
                open_objects.append(where)
    assert open_objects == []


def test_every_subschema_is_constrained() -> None:
    # A subschema with no type, no fixed value, no reference and no combinator accepts any object.
    unconstrained = [
        where for where, node in NODES if not {"type", "const", "enum", "$ref", "oneOf"} & set(node)
    ]
    assert unconstrained == []


def test_only_known_keywords_and_single_types() -> None:
    unknown = [(where, sorted(set(node) - KNOWN)) for where, node in NODES if set(node) - KNOWN]
    assert unknown == []
    assert all(node["type"] in TYPES for _, node in NODES if "type" in node)


def test_every_value_is_bounded() -> None:
    unbounded = []
    for where, node in NODES:
        kind = node.get("type")
        if kind == "string" and "enum" not in node and "maxLength" not in node:
            unbounded.append(where)
        if kind == "integer" and not {"minimum", "maximum"} <= set(node):
            unbounded.append(where)
        if kind == "array" and "maxItems" not in node:
            unbounded.append(where)
    assert unbounded == []


def test_every_free_string_refuses_nul_and_every_pattern_is_portable() -> None:
    problems = []
    for where, node in NODES:
        if node.get("type") == "string" and "enum" not in node and "pattern" not in node:
            problems.append((where, "a string with neither a fixed value nor a pattern"))
        pattern = node.get("pattern")
        if pattern is None:
            continue
        # Anchored at both ends, so that '$' is the only end anchor (see _support.ecma_regex).
        if not (pattern.startswith("^") and pattern.endswith("$")) or pattern.count("$") != 1:
            problems.append((where, "not anchored with one ^ and one $"))
        # \d, \w, \s and \b mean more than ASCII in Python's re and not in ECMA-262; '(?' starts
        # constructs the two dialects do not share.
        if re.search(r"\\[dDwWsSbB]|\(\?", pattern):
            problems.append((where, "a class or construct that differs between dialects"))
        # A pattern either is the free-text one, or is built of positive classes and literals,
        # which cannot match U+0000.
        outside_classes = re.sub(r"\[(?:\\.|[^\]])*\]", "", pattern)
        if pattern != NO_NUL and ("[^" in pattern or re.search(r"(?<!\\)\.", outside_classes)):
            problems.append((where, "a negated class or a bare '.' could match U+0000"))
        if _support.ecma_regex(pattern).search("\x00") is not None:
            problems.append((where, "matches U+0000"))
    assert problems == []


def test_ecma_reading_of_dollar_differs_from_pythons() -> None:
    # Why the fixture tests read patterns as ECMA-262 does: plain Python validation would let a
    # trailing newline through a pattern with no length bound.
    schema = {"type": "string", "pattern": "^[0-9]+$"}
    assert Draft202012Validator(schema).is_valid("12\n")
    assert not _support.EcmaValidator(schema).is_valid("12\n")


def test_shared_definitions_agree_across_files() -> None:
    defs: dict[str, dict[str, Any]] = {}
    for name, schema in SCHEMAS:
        for key, value in schema.get("$defs", {}).items():
            if key in defs:
                assert defs[key] == value, f"$defs/{key} differs in {name}"
            defs[key] = value


def test_closed_value_lists() -> None:
    request = spec.load_schema("request")["$defs"]
    reply = spec.load_schema("reply")["$defs"]
    assert request["v"]["const"] == reply["v"]["const"] == spec.PROTOCOL
    assert reply["dir"]["enum"] == ["request", "reply"]
    assert reply["status"]["enum"] == ["done", "refused", "failed"]
    assert reply["refused_code"]["enum"] == list(_support.REFUSED_CODES)
    assert reply["failed_code"]["enum"] == list(_support.FAILED_CODES)


def test_request_has_one_closed_branch_per_operation() -> None:
    schema = spec.load_schema("request")
    defs = schema["$defs"]
    branches = [defs[ref["$ref"].removeprefix("#/$defs/")] for ref in schema["oneOf"]]
    ops = [branch["properties"]["op"]["const"] for branch in branches]
    assert ops == list(_support.CATALOG)
    op_name = _support.ecma_regex(defs["op_name"]["pattern"])
    for op, branch in zip(ops, branches, strict=True):
        assert op_name.search(op)
        assert branch["required"] == ["v", "dir", "job_id", "op", "args", "not_after"]
        assert set(branch["properties"]) == {
            "v",
            "dir",
            "job_id",
            "op",
            "args",
            "not_after",
            "caps",
            "min_host_version",
        }
        assert branch["properties"]["dir"]["const"] == "request"
        assert branch["properties"]["args"] == {"$ref": f"#/$defs/args.{op}"}


def test_reply_shapes() -> None:
    defs = spec.load_schema("reply")["$defs"]
    common = {"v", "dir", "job_id", "status", "policy_digest", "host_version"}
    streams = {"stdout", "stderr", "stdout_truncated", "stderr_truncated"}
    done, refused, failed = defs["reply.done"], defs["reply.refused"], defs["reply.failed"]
    assert (
        set(done["required"])
        == set(done["properties"])
        == common | streams | {"exit_code", "duration_ms"}
    )
    assert set(refused["required"]) == set(refused["properties"]) == common | {"code"}
    assert set(failed["required"]) == common | {"code", "duration_ms"}
    assert set(failed["properties"]) == common | streams | {"code", "exit_code", "duration_ms"}
    assert failed["dependentRequired"] == {
        "stdout": ["stdout_truncated"],
        "stdout_truncated": ["stdout"],
        "stderr": ["stderr_truncated"],
        "stderr_truncated": ["stderr"],
    }


def test_stream_bound_matches_the_size_rule() -> None:
    # 65534 characters is the most a string can hold and still encode to 65536 bytes with its
    # quotes; the byte rule itself is the prose rule 'stream-size'.
    stream = spec.load_schema("reply")["$defs"]["stream"]
    assert stream["maxLength"] == _support.STREAM_LIMIT - 2
    assert _support.serialized_size("a" * stream["maxLength"]) == _support.STREAM_LIMIT


def test_smallest_cap_is_the_empty_stream() -> None:
    # A cap is measured on the stream's JSON-encoded string, quotes included, so the empty stream
    # measures 2 bytes. A smaller cap could not be met by any reply, so the schema refuses it.
    cap = spec.load_schema("request")["$defs"]["cap"]
    assert cap["minimum"] == _support.CAP_MIN == _support.serialized_size("")


def test_bounded_request_fits_the_request_size_rule() -> None:
    # The largest request the schema admits, astral characters in every free string, stays under
    # the request-size rule: the 16 KiB bound is a backstop, not the only bound.
    defs = spec.load_schema("request")["$defs"]
    largest = 0
    for op in _support.CATALOG:
        args: dict[str, Any] = {}
        for key, sub in defs[f"args.{op}"]["properties"].items():
            target = defs[sub["$ref"].removeprefix("#/$defs/")] if "$ref" in sub else sub
            if target.get("type") == "string":
                args[key] = "\U0001f600" * target["maxLength"]
            elif target.get("type") == "integer":
                args[key] = target["maximum"]
            else:
                args[key] = True
        body = {
            "v": spec.PROTOCOL,
            "dir": "request",
            "job_id": "0" * 36,
            "op": op,
            "args": args,
            "not_after": defs["not_after"]["maximum"],
            "caps": {
                "stdout_bytes": defs["cap"]["maximum"],
                "stderr_bytes": defs["cap"]["minimum"],
            },
            "min_host_version": "9" * 9 + "." + "9" * 9 + "." + "9" * 9,
        }
        largest = max(largest, _support.serialized_size(body))
    assert largest < _support.REQUEST_LIMIT
