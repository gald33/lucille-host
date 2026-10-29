"""The fixtures for each place in each pattern, and what the other fixtures hold in those places.

``tools/spec/gen_fixtures.py`` writes one invalid body for each place in each pattern, in each
message or document that holds the pattern (spec/PROTOCOL.md, "Fixtures"). These tests check:

- that running it writes exactly the committed files, and that no committed file in one of its
  families is left over from an earlier run;
- that each of those files, as committed, still shows its one fault: the value pins its place
  (``tools/spec/patterns.py``), or is the empty string, a separator left out or doubled, a
  counted repetition one past its bound, or a ``min_host_version`` part empty or ``00``; and
  that the body has no other fault: with a value the pattern accepts in that place, the schema
  accepts it and it breaks no named rule;
- for each character class and each kind of character it allows, that some fixture of the same
  message holds a value the pattern accepts only because that class admits a character of that
  kind; and for each counted repetition of a range (``{0,31}``), values at its least and at its
  most.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from lucille_host import spec
from tests.spec import _support
from tools.spec import gen_fixtures
from tools.spec import patterns as P

SCHEMAS = gen_fixtures.load_schemas(gen_fixtures.REPO_ROOT)
SLOTS = gen_fixtures.slots(SCHEMAS)
FIXTURES_DIR = gen_fixtures.REPO_ROOT / gen_fixtures.SPEC_DIR / "fixtures"


def test_running_the_generator_writes_exactly_the_committed_files() -> None:
    assert gen_fixtures.differences(gen_fixtures.REPO_ROOT) == []


def test_the_generator_writes_into_the_packaged_fixtures() -> None:
    assert FIXTURES_DIR.resolve() == spec.fixture_paths("request")[0].parent.parent.resolve()


@pytest.mark.parametrize("slot", SLOTS, ids=[f"{s.field.msg}/{s.name}" for s in SLOTS])
def test_each_generated_fixture_shows_its_one_fault(slot: gen_fixtures.Slot) -> None:
    fixture = spec.load_fixture(FIXTURES_DIR / slot.field.msg / f"{slot.name}.json")
    value = slot.field.value_of(fixture["body"])
    assert isinstance(value, str)
    assert slot.check(value)
    assert not P.accepts(slot.field.pattern, value)
    # No other fault: with a value the pattern accepts in its place, the body is valid.
    repaired = _with_value(fixture["body"], slot.field.path, _accepted_value(slot))
    state = _support.host_state() if slot.field.msg == "request" else None
    assert _support.schema_accepts(slot.field.msg, repaired)
    assert _support.prose_violations(slot.field.msg, repaired, state) == []


def _accepted_value(slot: gen_fixtures.Slot) -> str:
    """A value the pattern accepts, for the place the slot's fixture fills."""
    field = slot.field
    if field.name == "job_id":
        value = gen_fixtures.fake_job_id(slot.number)
    elif field.msg == "host.describe" and field.name == "op":
        value = "files.sha256"  # the document's other entry is host.describe; no entry twice
    else:
        value = field.bases[0]
    assert P.accepts(field.pattern, value)
    return value


def _with_value(body: Any, path: tuple[str | int, ...], value: str) -> Any:
    """A copy of ``body`` with ``value`` at ``path``."""
    out = copy.deepcopy(body)
    node = out
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return out


def _values(field: gen_fixtures.Field, body: Any) -> list[str]:
    """Every value in ``body`` at the field's place: for a list, each of its entries."""
    node = body
    for key in field.path[:-1]:
        if not (isinstance(node, dict) and key in node):
            return []
        node = node[key]
    last = field.path[-1]
    if isinstance(last, int):
        items = node if isinstance(node, list) else []
    else:
        items = [node[last]] if isinstance(node, dict) and last in node else []
    return [v for v in items if isinstance(v, str) and P.accepts(field.pattern, v)]


def _distinct_fields() -> list[gen_fixtures.Field]:
    seen, out = set(), []
    for field in gen_fixtures.fields(SCHEMAS):
        if (field.msg, field.pattern, field.path[0]) not in seen:
            seen.add((field.msg, field.pattern, field.path[0]))
            out.append(field)
    return out


FIELDS = _distinct_fields()


@pytest.mark.parametrize("field", FIELDS, ids=[f"{f.msg}/{f.prefix}" for f in FIELDS])
def test_fixtures_hold_each_kind_each_class_allows(field: gen_fixtures.Field) -> None:
    fixtures = [spec.load_fixture(path) for path in spec.fixture_paths(field.msg)]
    values = [v for fixture in fixtures for v in _values(field, fixture["body"])]
    missing = []
    for index in range(len(P.classes(field.pattern))):
        for kind in P.KINDS:
            if P.class_refuses(index, field.pattern, kind.name):
                continue
            narrower = P.without_kind(field.pattern, index, kind.name)
            if all(P.accepts(narrower, v) for v in values):
                missing.append(f"class {index} with {kind.name}")
    for index, match in enumerate(P.repetitions(field.pattern)):
        if match.group(2) is None:
            continue  # an exact count: every accepted value is at it
        for end in ("least", "most"):
            narrower = P.narrow_repetition(field.pattern, index, end)
            if all(P.accepts(narrower, v) for v in values):
                missing.append(f"repetition {index} at its {end}")
    assert missing == []
