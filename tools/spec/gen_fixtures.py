"""Write the fixtures for each place in each pattern of the lucille.exec/1 schemas.

Usage: ``python tools/spec/gen_fixtures.py [ROOT] [--check]``

ROOT is the repository (default: the one holding this file). The script writes into
``src/lucille_host/spec/v1/fixtures/`` and prints what it changed. With ``--check`` it writes
nothing, prints each difference between the committed files and what it would write, and exits 1
if there is one. ``tests/spec/test_generated_fixtures.py`` runs that check.

For each pattern in the schemas other than the one that refuses only U+0000, and in each message
or document that holds it (a request, a reply, the ``host.describe`` document), it writes one
invalid body for each of these, with no other fault:

- the empty string;
- for each character class, and each kind of character in :data:`patterns.KINDS` whose one
  character the class refuses, that character in the class's place;
- for each separator: the separator left out; doubled; and in its place each kind in
  :data:`patterns.SEPARATOR_KINDS`;
- for each counted repetition: one over its most, and, for an exact count (``{8}``), one under;
- in ``min_host_version``, for each part: the part empty, and ``00``.

Each value is found by a search over a fixed list of starting values, and only a value that pins
its place (see :mod:`tools.spec.patterns`) is written. The job ids of the fixtures it writes are
numbered from :data:`FIRST_NUMBER`, in the order above, so the output depends only on the
schemas and this file. The fixtures it does not write (every other fixture, and the few listed in
:data:`HAND_WRITTEN` whose names start like the ones it writes) are written by hand.
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # run as a script: make ``tools.spec`` importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.spec import patterns as P  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC_DIR = Path("src/lucille_host/spec/v1")
KINDS = ("request", "reply", "host.describe")
V = "lucille.exec/1"
DIGEST = "0123456789abcdef" * 4
NOT_AFTER = 1767229200
FIRST_NUMBER = 1001
ORDINAL = ("first", "second", "third", "fourth", "fifth")
KEY_ORDER = ("description", "valid", "schema_valid", "expect", "body")

# Fixtures whose names start like a family this script writes, but which it does not write.
HAND_WRITTEN = {
    "request": frozenset(
        {
            "bad-request-min-host-version-build-metadata",
            "bad-request-min-host-version-pre-release",
            "bad-request-min-host-version-suffix",
            "bad-request-min-host-version-trailing-newline",
            "bad-request-op-malformed",
            "bad-request-op-oversize",
            "no-reply-job-id-hyphens-misplaced",
            "no-reply-job-id-not-a-string",
            "no-reply-job-id-trailing-newline",
        }
    ),
    "reply": frozenset({"invalid-job-id-hyphens-misplaced", "invalid-job-id-malformed"}),
    "host.describe": frozenset({"invalid-enabled-op-oversize", "invalid-root-alias-a-path"}),
}


def fake_job_id(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012d}"


def request(job_id: str, op: str = "host.describe", args: Any = None, **extra: Any) -> dict:
    body = {
        "v": V,
        "dir": "request",
        "job_id": job_id,
        "op": op,
        "args": {} if args is None else args,
        "not_after": NOT_AFTER,
    }
    body.update(extra)
    return body


def reply(job_id: str, **extra: Any) -> dict:
    body = {
        "v": V,
        "dir": "reply",
        "job_id": job_id,
        "status": "refused",
        "code": "paused",
        "policy_digest": DIGEST,
        "host_version": "0.1.0",
    }
    body.update(extra)
    return body


def document(**extra: Any) -> dict:
    body = {
        "host_version": "0.1.0",
        "protocol": V,
        "os_family": "linux",
        "catalog_version": "0.1.0",
        "enabled_ops": ["host.describe", "files.sha256"],
        "root_aliases": ["docs"],
        "policy_digest": DIGEST,
    }
    body.update(extra)
    return body


def _refused(code: str) -> dict:
    return {"status": "refused", "code": code}


@dataclass(frozen=True)
class Field:
    """One pattern in one message or document."""

    msg: str  # the fixture kind: "request", "reply" or "host.describe"
    name: str  # the value's name, as fixture descriptions give it
    words: str  # the value, as a description names it ("a root alias")
    prefix: str  # the start of each fixture name
    pattern: str
    min_length: int
    max_length: int
    places: tuple[str, ...]  # a name for each character class
    seps: tuple[str, ...]  # a name for each separator
    reps: tuple[str, ...]  # a name for each counted repetition
    bases: tuple[str, ...]  # starting values; empty: the fixture's own job id
    make: Callable[[str, str], dict]  # (value, job id) -> body
    path: tuple[str | int, ...]  # where the value sits in the body
    expect: dict | None = None  # for a request: the reply it draws

    def bounds_ok(self, value: str) -> bool:
        return self.min_length <= len(value) <= self.max_length

    def value_of(self, body: Any) -> str | None:
        """The value at :attr:`path`, or None."""
        for key in self.path:
            if isinstance(key, int) and isinstance(body, list) and key < len(body):
                body = body[key]
            elif isinstance(key, str) and isinstance(body, dict) and key in body:
                body = body[key]
            else:
                return None
        return body if isinstance(body, str) else None


@dataclass(frozen=True)
class Slot:
    """One fixture this script writes."""

    field: Field
    name: str
    number: int
    value: str
    description: str
    check: Callable[[str], bool]  # whether a value shows this fixture's one fault

    def fixture(self) -> dict:
        out: dict[str, Any] = {
            "description": self.description,
            "valid": False,
            "schema_valid": False,
        }
        if self.field.msg == "request":
            out["expect"] = {"reply": self.field.expect}
        out["body"] = self.field.make(self.value, fake_job_id(self.number))
        return out


def _node(schema: Mapping[str, Any], pointer: tuple[str, ...]) -> Mapping[str, Any]:
    for part in pointer:
        schema = schema[part]
    return schema


# For each pattern: where each schema that holds it keeps it, a name for each class, separator
# and counted repetition, and the values the search starts from (none: the fixture's job id).
SHAPES: dict[str, dict[str, Any]] = {
    "job_id": {
        "where": ("$defs", "job_id"),
        "places": tuple(f"group-{i}" for i in range(1, 6)),
        "seps": tuple(f"after-group-{i}" for i in range(1, 5)),
        "reps": tuple(f"group-{i}" for i in range(1, 6)),
        "bases": (),
    },
    "op": {
        "where": ("$defs", "op_name"),
        "places": ("starts-with", "holds", "part-starts-with", "part-holds"),
        "seps": ("",),
        "reps": (),
        "bases": ("host.describe", "host1.describe", "host.de1scribe", "host.d1escribe"),
    },
    "root": {
        "where": ("$defs", "root"),
        "places": ("starts-with", "holds"),
        "seps": (),
        "reps": ("",),
        "bases": ("docs",),
    },
    "min_host_version": {
        "where": ("$defs", "min_host_version"),
        "places": tuple(
            f"{part}-{where}"
            for part in ("major", "minor", "patch")
            for where in ("starts-with", "holds")
        ),
        "seps": ("after-major", "after-minor"),
        "reps": ("major", "minor", "patch"),
        "bases": ("9.1.1", "0.1.1", "9.0.1", "9.1.0", "0.0.1", "10.0.0", "0.10.0", "0.0.10"),
    },
    "policy_digest": {
        "where": ("$defs", "policy_digest"),
        "places": ("",),
        "seps": (),
        "reps": ("",),
        "bases": (DIGEST,),
    },
    "host_version": {
        "where": ("$defs", "host_version"),
        "places": ("starts-with", "holds"),
        "seps": (),
        "reps": ("",),
        "bases": ("0.1.0",),
    },
    "catalog_version": {
        "where": ("properties", "catalog_version"),
        "places": ("starts-with", "holds"),
        "seps": (),
        "reps": ("",),
        "bases": ("0.1.0",),
    },
}
_SHA256 = "files.sha256"
# For each pattern in each message or document that holds it: (pattern, message, how a
# description names the value, the start of each fixture name, the body, where the value sits,
# and for a request the reply it draws).
USES: tuple[tuple[Any, ...], ...] = (
    ("job_id", "request", "a job_id", "no-reply-job-id",
     lambda v, j: request(v), ("job_id",), None),
    ("job_id", "reply", "a job_id", "invalid-job-id",
     lambda v, j: reply(v), ("job_id",), None),
    ("op", "request", "an op", "bad-request-op",
     lambda v, j: request(j, op=v), ("op",), _refused("bad_request")),
    ("op", "host.describe", "an enabled operation's name", "invalid-enabled-op",
     lambda v, j: document(enabled_ops=["host.describe", v]), ("enabled_ops", 1), None),
    ("root", "request", "a root alias", "bad-args-root",
     lambda v, j: request(j, _SHA256, {"root": v, "path": "notes/todo.txt"}),
     ("args", "root"), _refused("bad_args")),
    ("root", "host.describe", "a folder alias", "invalid-root-alias",
     lambda v, j: document(root_aliases=[v]), ("root_aliases", 0), None),
    ("min_host_version", "request", "a min_host_version", "bad-request-min-host-version",
     lambda v, j: request(j, min_host_version=v), ("min_host_version",), _refused("bad_request")),
    ("policy_digest", "reply", "a policy_digest", "invalid-policy-digest",
     lambda v, j: reply(j, policy_digest=v), ("policy_digest",), None),
    ("policy_digest", "host.describe", "a policy_digest", "invalid-policy-digest",
     lambda v, j: document(policy_digest=v), ("policy_digest",), None),
    ("host_version", "reply", "a host_version", "invalid-host-version",
     lambda v, j: reply(j, host_version=v), ("host_version",), None),
    ("host_version", "host.describe", "a host_version", "invalid-host-version",
     lambda v, j: document(host_version=v), ("host_version",), None),
    ("catalog_version", "host.describe", "a catalog_version", "invalid-catalog-version",
     lambda v, j: document(catalog_version=v), ("catalog_version",), None),
)  # fmt: skip


def fields(schemas: Mapping[str, Mapping[str, Any]]) -> list[Field]:
    """Each pattern in each message or document that holds it, in the order of :data:`USES`."""
    out = []
    for name, msg, words, prefix, make, path, expect in USES:
        shape = SHAPES[name]
        node = _node(schemas[msg], shape["where"])
        out.append(
            Field(
                msg=msg,
                name=name,
                words=words,
                prefix=prefix,
                pattern=node["pattern"],
                min_length=node.get("minLength", 0),
                max_length=node["maxLength"],
                places=shape["places"],
                seps=shape["seps"],
                reps=shape["reps"],
                bases=shape["bases"],
                make=make,
                path=path,
                expect=expect,
            )
        )
    return out


# One over the most of each {0,n} repetition, by field and repetition.
OVERSIZE = {
    ("root", 0): "d" * 33,
    ("host_version", 0): "0.1.0+" + "a" * 27,
    ("catalog_version", 0): "1" + "a" * 64,
    ("min_host_version", 0): "1234567890.0.0",
    ("min_host_version", 1): "0.1234567890.0",
    ("min_host_version", 2): "0.1.1234567890",
}
# Values chosen by hand where the search's first find reads less plainly. Each is still checked.
PREFERRED = {
    "bad-request-min-host-version-major-holds-dot": "1.0.0.0",
    "bad-request-min-host-version-minor-holds-dot": "0.1.0.0",
    "bad-request-min-host-version-patch-holds-dot": "0.0.1.0",
    "bad-request-min-host-version-no-dot-after-major": "100.1",
    "bad-request-min-host-version-no-dot-after-minor": "1.100",
    "bad-request-op-not-namespaced": "describe",
    "invalid-enabled-op-not-namespaced": "describe",
}
PART_VALUES = {
    "empty": (".10.1", "10..1", "10.1."),
    "two-zeros": ("00.1.0", "0.00.1", "0.1.00"),
}


def _shown(value: str) -> str:
    return f"'{value}'" if " " in value or not value.isascii() or value == "" else value


def _not(kind: str, what: str) -> str:
    """'not a digit', or for a non-ASCII kind 'not an ASCII digit', which never contradicts."""
    if not kind.startswith("non-ascii"):
        return f"not {what}"
    return "not an ASCII " + what.removeprefix("a ").replace("letter or a digit", "letter or digit")


def _outcome(field: Field) -> str:
    if field.msg != "request":
        return ""
    if field.expect is None:
        return " The host posts nothing."
    return f" The host refuses it {field.expect['code']}."


def class_description(field: Field, value: str, index: int, kind: str) -> str:
    phrase, v, place = P.KIND[kind].phrase, _shown(value), field.places[index]
    if field.name == "job_id":
        where = f"in its {ORDINAL[index]} group"
        return f"A job_id, {v}, with {phrase} {where}: not lowercase hex.{_outcome(field)}"
    if field.name == "op":
        where = {
            "starts-with": f"that starts with {phrase}",
            "holds": f"with {phrase} in its first part, after its first letter",
            "part-starts-with": f"with a later part that starts with {phrase}",
            "part-holds": f"with {phrase} in a later part, after its first letter",
        }[place]
        return f"{field.words.capitalize()}, {v}, {where}.{_outcome(field)}"
    if field.name == "root":
        where = (
            f"that starts with {phrase}, {_not(kind, 'a lowercase letter')}"
            if place == "starts-with"
            else f"with {phrase} after its first letter"
        )
        return f"{field.words.capitalize()}, {v}, {where}.{_outcome(field)}"
    if field.name == "min_host_version":
        part = place.split("-")[0]
        if place.endswith("starts-with") and kind == "digit":
            zero = "has a leading zero"
            return f"A min_host_version, {v}, whose {part} part {zero}.{_outcome(field)}"
        if place.endswith("starts-with"):
            return (
                f"A min_host_version, {v}, whose {part} part starts with {phrase}, "
                f"{_not(kind, 'a digit')}.{_outcome(field)}"
            )
        return (
            f"A min_host_version, {v}, whose {part} part holds {phrase} after its first "
            f"digit.{_outcome(field)}"
        )
    if field.name == "policy_digest":
        return f"A policy_digest of 64 characters, one of them {phrase}: not lowercase hex."
    first = "a digit" if field.name == "host_version" else "a letter or a digit"
    if place == "starts-with":
        return f"A {field.name}, {v}, that starts with {phrase}, {_not(kind, first)}."
    return f"A {field.name}, {v}, with {phrase} after its first character."


def _separator_words(field: Field, index: int) -> str:
    if field.name == "job_id":
        return f"the hyphen after its {ORDINAL[index]} group"
    if field.name == "op":
        return "the dot"
    return f"the dot after its {('major', 'minor')[index]} part"


def separator_description(field: Field, value: str, index: int, what: str) -> str:
    """``what``: a kind's name, "missing" or "doubled"."""
    sep = _separator_words(field, index)
    if what == "missing":
        change = f"with no {sep.removeprefix('the ')}"
    elif what == "doubled":
        change = f"with {sep} doubled"
    else:
        change = f"with {P.KIND[what].phrase} in place of {sep}"
    return f"{field.words.capitalize()}, {_shown(value)}, {change}.{_outcome(field)}"


def candidates(bases: tuple[str, ...], char: str) -> Iterator[str]:
    """Each base with ``char`` inserted at each place, then with it in place of each character."""
    for base in bases:
        for at in range(len(base) + 1):
            yield base[:at] + char + base[at:]
    for base in bases:
        for at in range(len(base)):
            yield base[:at] + char + base[at + 1 :]


def _first(bases: tuple[str, ...], char: str, test: Callable[[str], bool], name: str) -> str:
    for value in candidates(bases, char):
        if test(value):
            return value
    raise ValueError(f"{name}: no value pins its place")


def _field_slots(field: Field, numbers: Iterator[int]) -> Iterator[Slot]:
    pattern = field.pattern

    def slot(name: str, value_for: Callable[[tuple[str, ...]], str], describe, check) -> Slot:
        number = next(numbers)
        bases = field.bases or (fake_job_id(number),)
        value = PREFERRED[name] if name in PREFERRED else value_for(bases)
        if not check(value):
            raise ValueError(f"{field.msg}/{name}: {value!r} does not show its fault")
        return Slot(field, name, number, value, describe(value), check)

    yield slot(
        f"{field.prefix}-empty",
        lambda bases: "",
        lambda v: (
            f"An empty {field.words.removeprefix('a ').removeprefix('an ')}." + _outcome(field)
        ),
        lambda v: v == "" and not P.accepts(pattern, v),
    )
    for index in range(len(P.classes(pattern))):
        for kind in P.KINDS:
            if not P.class_refuses(index, pattern, kind.name):
                continue
            place = field.places[index]
            if (
                field.name == "min_host_version"
                and place.endswith("starts-with")
                and (kind.name == "digit")
            ):
                name = f"{field.prefix}-leading-zero-{place.split('-')[0]}"
            else:
                name = "-".join(p for p in (field.prefix, place, kind.name) if p)

            def check(v: str, index: int = index, kind: str = kind.name) -> bool:
                return field.bounds_ok(v) and P.pins_class(pattern, v, index, kind)

            yield slot(
                name,
                lambda bases, char=kind.char, check=check, name=name: _first(
                    bases, char, check, name
                ),
                lambda v, index=index, kind=kind.name: class_description(field, v, index, kind),
                check,
            )
    count = len(P.separators(pattern))
    for index, (_, _, sep) in enumerate(P.separators(pattern)):
        where = field.seps[index]
        for kind in P.SEPARATOR_KINDS[sep]:
            name = "-".join(p for p in (field.prefix, kind, where or "for-dot") if p)

            def check(v: str, index: int = index, kind: str = kind) -> bool:
                return field.bounds_ok(v) and P.pins_separator(pattern, v, index, kind)

            yield slot(
                name,
                lambda bases, char=P.KIND[kind].char, check=check, name=name: _first(
                    bases, char, check, name
                ),
                lambda v, index=index, kind=kind: separator_description(field, v, index, kind),
                check,
            )
        for what, change in (("missing", P.optional_separator), ("doubled", P.doubled_separator)):
            mark = {
                "missing": f"no-{'hyphen' if sep == '-' else 'dot'}",
                "doubled": f"doubled-{'hyphen' if sep == '-' else 'dot'}",
            }[what]
            name = "-".join(p for p in (field.prefix, mark, where) if p)
            if field.name == "op" and what == "missing":
                name = f"{field.prefix}-not-namespaced"

            def check(v: str, index: int = index, change=change) -> bool:
                return P.only_place(pattern, v, lambda i: change(pattern, i), count, index)

            def value_for(
                bases: tuple[str, ...], index: int = index, what: str = what, sep: str = sep
            ) -> str:
                # The base's index-th separator character, left out or doubled.
                base = bases[0]
                cut = [i for i, c in enumerate(base) if c == sep][index]
                if what == "missing":
                    return base[:cut] + base[cut + 1 :]
                return base[:cut] + sep + base[cut:]

            yield slot(
                name,
                value_for,
                lambda v, index=index, what=what: separator_description(field, v, index, what),
                check,
            )
    reps = P.repetitions(pattern)
    for index, match in enumerate(reps):
        low, high = int(match.group(1)), match.group(2)
        steps = (1,) if high is not None else (-1, 1)
        for step in steps:
            where = field.reps[index]
            if field.name == "job_id":
                name = f"{field.prefix}-{where}-{'short' if step < 0 else 'long'}"
            elif field.name == "policy_digest":
                name = f"{field.prefix}-{low + step}-digits"
            else:
                name = "-".join(p for p in (field.prefix, where, "oversize") if p)

            def check(v: str, index: int = index, step: int = step) -> bool:
                return P.only_place(
                    pattern, v, lambda i: P.widen_repetition(pattern, i, step), len(reps), index
                )

            def value_for(bases: tuple[str, ...], index: int = index, step: int = step) -> str:
                if field.name == "job_id":
                    groups = bases[0].split("-")
                    group = groups[index]
                    groups[index] = group[1:] if step < 0 else group + "0"
                    return "-".join(groups)
                if field.name == "policy_digest":
                    return DIGEST[:63] if step < 0 else DIGEST + "0"
                return OVERSIZE[(field.name, index)]

            def describe(v: str, index: int = index, step: int = step) -> str:
                if field.name == "job_id":
                    length = "a digit short" if step < 0 else "a digit long"
                    return (
                        f"A job_id whose {ORDINAL[index]} group is {length} ({len(v)} "
                        f"characters).{_outcome(field)}"
                    )
                if field.name == "policy_digest":
                    return f"A policy_digest of {len(v)} hex digits, not 64."
                if field.name == "min_host_version":
                    return (
                        f"A min_host_version, {v}, whose {field.reps[index]} part has 10 "
                        f"digits.{_outcome(field)}"
                    )
                return (
                    f"{field.words.capitalize()} of {len(v)} characters, one over the most its "
                    f"pattern allows.{_outcome(field)}"
                )

            yield slot(name, value_for, describe, check)
    if field.name == "min_host_version":
        for what, change in (("empty", P.optional_part), ("two-zeros", P.zeros_part)):
            for index, part in enumerate(("major", "minor", "patch")):

                def check(v: str, index: int = index, change=change) -> bool:
                    return field.bounds_ok(v) and P.only_place(
                        pattern, v, lambda i: change(pattern, i), 3, index
                    )

                words = "is empty" if what == "empty" else "is 00"
                yield slot(
                    f"{field.prefix}-{part}-{what}",
                    lambda bases, what=what, index=index: PART_VALUES[what][index],
                    lambda v, part=part, words=words: (
                        f"A min_host_version, {_shown(v)}, whose {part} part {words}."
                        f"{_outcome(field)}"
                    ),
                    check,
                )


def slots(schemas: Mapping[str, Mapping[str, Any]]) -> list[Slot]:
    """Every fixture this script writes, in order."""
    numbers = itertools.count(FIRST_NUMBER)
    out = [s for field in fields(schemas) for s in _field_slots(field, numbers)]
    names = [(s.field.msg, s.name) for s in out]
    duplicates = {n for n in names if names.count(n) > 1}
    if duplicates:
        raise ValueError(f"two fixtures share a name: {sorted(duplicates)}")
    return out


def render(fixture: Mapping[str, Any]) -> str:
    """A fixture's file: its keys in order, two-space indent, every non-ASCII character escaped."""
    ordered = {key: fixture[key] for key in KEY_ORDER if key in fixture}
    return json.dumps(ordered, indent=2, ensure_ascii=True) + "\n"


def load_schemas(root: Path) -> dict[str, dict[str, Any]]:
    return {
        name: json.loads((root / SPEC_DIR / f"{name}.schema.json").read_text(encoding="utf-8"))
        for name in KINDS
    }


def owned(slot_list: list[Slot]) -> dict[str, tuple[str, ...]]:
    """For each fixture kind, the name prefixes of the families this script writes."""
    out: dict[str, set[str]] = {kind: set() for kind in KINDS}
    for s in slot_list:
        out[s.field.msg].add(s.field.prefix + "-")
    return {kind: tuple(sorted(prefixes)) for kind, prefixes in out.items()}


def differences(root: Path) -> list[str]:
    """Where the committed fixtures under ``root`` differ from what this script writes."""
    slot_list = slots(load_schemas(root))
    fixtures_dir = root / SPEC_DIR / "fixtures"
    found = []
    written = {(s.field.msg, s.name) for s in slot_list}
    for s in slot_list:
        path = fixtures_dir / s.field.msg / f"{s.name}.json"
        if not path.is_file():
            found.append(f"missing: {s.field.msg}/{s.name}.json")
        elif path.read_text(encoding="utf-8") != render(s.fixture()):
            found.append(f"differs: {s.field.msg}/{s.name}.json")
    for kind, prefixes in owned(slot_list).items():
        for path in sorted((fixtures_dir / kind).glob("*.json")):
            name = path.stem
            if (
                name.startswith(prefixes)
                and (kind, name) not in written
                and name not in HAND_WRITTEN[kind]
            ):
                found.append(f"not written by this script: {kind}/{name}.json")
    return found


def write(root: Path) -> list[str]:
    """Write every fixture this script writes; return what changed."""
    changed = []
    fixtures_dir = root / SPEC_DIR / "fixtures"
    for s in slots(load_schemas(root)):
        path = fixtures_dir / s.field.msg / f"{s.name}.json"
        text = render(s.fixture())
        if not path.is_file() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
            changed.append(f"wrote {s.field.msg}/{s.name}.json")
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", nargs="?", type=Path, default=REPO_ROOT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if args.check:
        found = differences(root)
        print(*found, sep="\n")
        return 1 if found else 0
    print(*write(root), sep="\n")
    stray = [d for d in differences(root) if d.startswith("not written")]
    print(*stray, sep="\n")
    return 1 if stray else 0


if __name__ == "__main__":
    sys.exit(main())
