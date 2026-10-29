"""Helpers for the spec tests.

- :data:`EcmaValidator` reads schema patterns as ECMA-262 does, which is what JSON Schema
  patterns are. Python's ``re`` reads ``$`` as "the end, or just before a final newline";
  ECMA-262 reads it as "the end". Every pattern in the schemas starts with ``^`` and ends with one
  ``$`` (``test_schemas.py`` checks), so ending it with ``\\Z`` instead gives the ECMA-262 meaning.
- :func:`prose_violations` checks the rules ``spec/PROTOCOL.md`` states that a JSON Schema cannot:
  ``strings``, ``integers``, ``request-size``, ``stream-size`` and ``not-after-range``.
- :func:`decide` is a reference reading of the host's refusal order (``spec/PROTOCOL.md``, "Which
  refusal wins"), used only to check that each request fixture's expected reply agrees with the
  spec. It is not the host. The host's own checks, when they are written, live in the runtime
  package and are held to the same fixtures.
- :func:`same_hub` is a reference reading of check 3 of the host's accept policy
  (``spec/PROTOCOL.md``, section 6), and :func:`largest_host_describe` and
  :func:`shortest_host_describe` build the largest and the shortest document
  ``host.describe.schema.json`` allows.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from typing import Any

from jsonschema import Draft202012Validator, validators
from jsonschema.exceptions import ValidationError

from lucille_host import spec

REQUEST_LIMIT = 16384  # bytes, serialized as spec/PROTOCOL.md says
STREAM_LIMIT = 65536  # bytes of one stream's JSON-encoded string, quotes included
NOT_AFTER_RANGE = 86400  # seconds after the host received a request
NOT_AFTER_MAX = 4294967295
OP_MAX = 64  # characters in an op name
CAP_MIN = 2  # the empty stream, "", measures 2 bytes
CAP_MAX = 2147483647
CATALOG = ("host.describe", "host.sleep", "files.sha256", "files.search")
REFUSED_CODES = (
    "unknown_op",
    "not_enabled",
    "bad_request",
    "bad_args",
    "path_outside_root",
    "expired",
    "paused",
    "host_too_old",
)
FAILED_CODES = ("timeout", "killed_locally", "interrupted")
ENVELOPE_REQUIRED = frozenset({"v", "dir", "job_id", "op", "args", "not_after"})
ENVELOPE_OPTIONAL = frozenset({"caps", "min_host_version"})
EVENTS = {
    "runs_past_timeout": "timeout",
    "killed_locally": "killed_locally",
    "restarted_after_decision": "interrupted",
}
PROSE_RULES = ("strings", "integers", "request-size", "stream-size", "not-after-range")


def ecma_regex(pattern: str) -> re.Pattern[str]:
    """A schema pattern compiled to match as ECMA-262 does (see the module docstring)."""
    if not (pattern.startswith("^") and pattern.endswith("$")):
        raise ValueError(f"pattern {pattern!r} is not anchored with ^ and $")
    return re.compile(pattern[:-1] + r"\Z")


def _pattern(
    validator: Any, pattern: str, instance: Any, schema: Mapping[str, Any]
) -> Iterator[ValidationError]:
    if validator.is_type(instance, "string") and ecma_regex(pattern).search(instance) is None:
        yield ValidationError(f"{instance!r} does not match {pattern!r}")


EcmaValidator = validators.extend(Draft202012Validator, validators={"pattern": _pattern})


def validator_for(schema: Mapping[str, Any]) -> Any:
    return EcmaValidator(schema)


def schema_accepts(name: str, instance: Any, schema: Mapping[str, Any] | None = None) -> bool:
    """Whether the named schema (or the one given, for a copy loaded elsewhere) accepts it."""
    return validator_for(schema or spec.load_schema(name)).is_valid(instance)


def serialized_size(value: Any) -> int:
    """Bytes of ``value`` serialized with every non-ASCII character escaped and no whitespace."""
    return len(json.dumps(value, ensure_ascii=True, separators=(",", ":")))


def walk(value: Any) -> Iterator[Any]:
    """Every value inside ``value``, object keys included, ``value`` itself first."""
    yield value
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item)


def bad_string(text: str) -> bool:
    """U+0000, or a surrogate code point: in a parsed string, any surrogate is unpaired."""
    return any(char == "\x00" or 0xD800 <= ord(char) <= 0xDFFF for char in text)


def is_integer(value: Any) -> bool:
    """A JSON integer as parsed: an int, never a bool and never a float such as 1.0."""
    return isinstance(value, int) and not isinstance(value, bool)


def prose_violations(
    kind: str, body: Any, host_state: Mapping[str, Any] | None = None
) -> list[str]:
    """The rules a schema cannot express that ``body`` breaks, in :data:`PROSE_RULES` order."""
    broken: list[str] = []
    values = list(walk(body))
    if any(isinstance(v, str) and bad_string(v) for v in values):
        broken.append("strings")
    if any(isinstance(v, float) for v in values):
        broken.append("integers")
    if kind == "request" and serialized_size(body) > REQUEST_LIMIT:
        broken.append("request-size")
    if kind == "reply" and isinstance(body, dict):
        for stream in ("stdout", "stderr"):
            text = body.get(stream)
            if isinstance(text, str) and serialized_size(text) > STREAM_LIMIT:
                broken.append("stream-size")
                break
    if kind == "request" and host_state is not None and isinstance(body, dict):
        not_after = body.get("not_after")
        if is_integer(not_after) and not_after > host_state["received_at"] + NOT_AFTER_RANGE:
            broken.append("not-after-range")
    return broken


# --- The reference refusal order ---------------------------------------------------------------


def _defs() -> Mapping[str, Any]:
    return spec.load_schema("request")["$defs"]


def _regex(name: str) -> re.Pattern[str]:
    return ecma_regex(_defs()[name]["pattern"])


def host_state(overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
    state = spec.default_host_state()
    state.update(overrides or {})
    return state


def refused(code: str) -> dict[str, str]:
    return {"status": "refused", "code": code}


def _bad_request(body: Mapping[str, Any], state: Mapping[str, Any]) -> bool:
    if serialized_size(body) > REQUEST_LIMIT:
        return True
    if any(isinstance(v, str) and bad_string(v) for v in walk(body)):
        return True
    keys = set(body)
    if not ENVELOPE_REQUIRED <= keys or keys - ENVELOPE_REQUIRED - ENVELOPE_OPTIONAL:
        return True
    if body["v"] != spec.PROTOCOL:
        return True
    op = body["op"]
    if not isinstance(op, str) or len(op) > OP_MAX or _regex("op_name").search(op) is None:
        return True
    if not isinstance(body["args"], dict):
        return True
    not_after = body["not_after"]
    if not is_integer(not_after) or not 0 <= not_after <= NOT_AFTER_MAX:
        return True
    if not_after > state["received_at"] + NOT_AFTER_RANGE:
        return True
    if "caps" in body:
        caps = body["caps"]
        if not isinstance(caps, dict) or set(caps) != {"stdout_bytes", "stderr_bytes"}:
            return True
        if not all(is_integer(v) and CAP_MIN <= v <= CAP_MAX for v in caps.values()):
            return True
    if "min_host_version" in body:
        wanted = body["min_host_version"]
        if not isinstance(wanted, str) or _regex("min_host_version").search(wanted) is None:
            return True
    return False


def older(host_version: str, minimum: str) -> bool:
    """Whether a host of ``host_version`` is older than ``minimum`` (spec: min_host_version).

    The host compares the numeric triple its version starts with, as a tuple. A version with
    anything after that triple counts as older than the same triple, and a version that does not
    start with one counts as older than every minimum.
    """
    match = re.match(r"([0-9]+)\.([0-9]+)\.([0-9]+)", host_version)
    if match is None:
        return True
    have = tuple(int(part) for part in match.groups())
    want = tuple(int(part) for part in minimum.split("."))
    return have < want or (have == want and match.end() != len(host_version))


def _bad_args(op: str, args: Any) -> bool:
    schema = dict(spec.load_schema("request"))
    schema.pop("oneOf")
    schema["$ref"] = f"#/$defs/args.{op}"
    if not validator_for(schema).is_valid(args):
        return True
    return any(isinstance(v, float) for v in walk(args))


def _outside_root(args: Mapping[str, Any], state: Mapping[str, Any]) -> bool:
    root = args.get("root")
    if root is None:
        return False
    if root not in state["roots"]:
        return True
    path = args.get("path")
    if path is None:
        return False
    if path.startswith("/") or ".." in path.split("/"):
        return True
    return path in state["resolves_outside_root"]


def _too_old(body: Mapping[str, Any], state: Mapping[str, Any]) -> bool:
    """A well-formed ``min_host_version`` the host is older than. Checked before the envelope."""
    wanted = body.get("min_host_version")
    if not isinstance(wanted, str) or _regex("min_host_version").search(wanted) is None:
        return False
    return older(state["host_version"], wanted)


def decide(body: Any, state: Mapping[str, Any]) -> dict[str, str] | None:
    """The reply the spec's order gives, as ``{status, code?}``, or None for no reply at all."""
    if not isinstance(body, dict) or body.get("dir") != "request":
        return None
    job_id = body.get("job_id")
    if not isinstance(job_id, str) or _regex("job_id").search(job_id) is None:
        return None
    if job_id in state["seen_job_ids"]:
        return None
    if _too_old(body, state):
        return refused("host_too_old")
    if _bad_request(body, state):
        return refused("bad_request")
    if state["received_at"] > body["not_after"]:
        return refused("expired")
    if state["paused"]:
        return refused("paused")
    op = body["op"]
    if op not in CATALOG:
        return refused("unknown_op")
    # Section 10 gives not_enabled only to an op in the catalog. Every op that reaches this line
    # is in it, so the check reads the local policy alone.
    if op not in state["enabled_ops"]:
        return refused("not_enabled")
    if _bad_args(op, body["args"]):
        return refused("bad_args")
    if _outside_root(body["args"], state):
        return refused("path_outside_root")
    if state.get("event"):
        return {"status": "failed", "code": EVENTS[state["event"]]}
    return {"status": "done"}


def effective_caps(body: Mapping[str, Any], state: Mapping[str, Any]) -> dict[str, int]:
    """For each stream, the lower of the request's cap, if any, and the host's own.

    ``host.describe`` runs no child, and a request's caps do not apply to its document: the host
    holds it to its own caps only.
    """
    own = state["caps"]
    asked = {} if body.get("op") == "host.describe" else body.get("caps", {})
    return {stream: min(asked.get(stream, own[stream]), own[stream]) for stream in own}


def same_hub(room_url: str, code_url: str) -> bool:
    """Check 3: the room's hub is the one the pairing code named.

    Both URLs are compared as strings with every trailing ``/`` removed, as agent-switchboard 2.6.0
    removes them when it decodes a pairing code (its ``pairing.py:599``) and when it keeps an
    offered room (its ``sharing.py:1613``).
    """
    return room_url.rstrip("/") == code_url.rstrip("/")


def largest_host_describe(schema: Mapping[str, Any]) -> dict[str, Any]:
    """The largest document ``schema`` (``host.describe.schema.json``) allows.

    Every string at its most characters and both lists at their most entries. Every character
    the patterns allow is ASCII that JSON writes as one byte, and none is a quote or a backslash,
    so the size depends only on those counts.
    """
    props = schema["properties"]
    defs = schema["$defs"]

    def most(sub: Mapping[str, Any], key: str) -> int:
        target = defs[sub["$ref"].removeprefix("#/$defs/")] if "$ref" in sub else sub
        return int(target[key])

    op_length = most(props["enabled_ops"]["items"], "maxLength")
    root_length = most(props["root_aliases"]["items"], "maxLength")
    return {
        "host_version": "1" + "a" * (most(props["host_version"], "maxLength") - 1),
        "protocol": props["protocol"]["const"],
        "os_family": max(props["os_family"]["enum"], key=len),
        "catalog_version": "1" + "a" * (most(props["catalog_version"], "maxLength") - 1),
        "enabled_ops": [
            f"op{i:03d}." + "a" * (op_length - 6) for i in range(props["enabled_ops"]["maxItems"])
        ],
        "root_aliases": [
            f"r{i:03d}" + "a" * (root_length - 4) for i in range(props["root_aliases"]["maxItems"])
        ],
        "policy_digest": "f" * most(props["policy_digest"], "maxLength"),
    }


def shortest_host_describe(schema: Mapping[str, Any]) -> dict[str, Any]:
    """The shortest document ``schema`` (``host.describe.schema.json``) allows.

    Every string at its fewest characters, both lists empty, and the shorter ``os_family``. The
    size depends only on those counts, as in :func:`largest_host_describe`. The schema sets no
    least number of entries for either list; a schema that did would need entries written here.
    """
    props = schema["properties"]
    defs = schema["$defs"]
    for name in ("enabled_ops", "root_aliases"):
        if "minItems" in props[name]:
            raise ValueError(f"{name} has a least number of entries")

    def least(sub: Mapping[str, Any]) -> int:
        target = defs[sub["$ref"].removeprefix("#/$defs/")] if "$ref" in sub else sub
        return int(target.get("minLength", 0))

    return {
        "host_version": "1" * least(props["host_version"]),
        "protocol": props["protocol"]["const"],
        "os_family": min(props["os_family"]["enum"], key=len),
        "catalog_version": "1" * least(props["catalog_version"]),
        "enabled_ops": [],
        "root_aliases": [],
        "policy_digest": "0" * least(props["policy_digest"]),
    }


# --- Fixture labels --------------------------------------------------------------------------

FIXTURE_KEYS = {
    "request": (
        {"description", "valid", "schema_valid", "expect", "body"},
        {"prose_rule", "host_state"},
    ),
    "reply": ({"description", "valid", "schema_valid", "body"}, {"prose_rule", "op"}),
    "host.describe": ({"description", "valid", "schema_valid", "body"}, set()),
}


def label_errors(kind: str, fixture: Mapping[str, Any], schema: Mapping[str, Any]) -> list[str]:
    """Where a request, reply or document fixture's labels disagree with ``schema`` and the rules.

    ``schema_valid`` must be what the schema says. A fixture the schema accepts is ``valid``
    exactly when it breaks no prose rule, and an invalid one names the one rule it breaks in
    ``prose_rule``. A fixture the schema refuses is never valid and names no prose rule.
    """
    errors: list[str] = []
    required, optional = FIXTURE_KEYS[kind]
    keys = set(fixture)
    if not required <= keys or keys - required - optional:
        errors.append(f"fixture keys {sorted(keys)}: need {sorted(required)}, may add {optional}")
        return errors
    body = fixture["body"]
    accepted = schema_accepts(kind, body, schema)
    if accepted != fixture["schema_valid"]:
        errors.append(
            f"schema says {accepted}, fixture says schema_valid={fixture['schema_valid']}"
        )
    state = host_state(fixture.get("host_state")) if kind == "request" else None
    broken = prose_violations(kind, body, state)
    if fixture["schema_valid"]:
        expected = [] if fixture["valid"] else [fixture.get("prose_rule")]
        if broken != expected:
            errors.append(f"prose rules broken: {broken}, fixture expects {expected}")
    else:
        if fixture["valid"]:
            errors.append("a fixture the schema refuses cannot be valid")
        if "prose_rule" in fixture:
            errors.append("prose_rule names a rule the schema does not catch; this one it does")
    return errors
