"""The v0.1 catalog: its entries, and that each entry's parameter schema is the spec's."""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path
from types import MappingProxyType
from typing import Any

import jsonschema
import pytest

from lucille_host import _exec, spec
from lucille_host.jobs import Context, Job, Ready, check
from lucille_host.ops import (
    CATALOG_VERSION,
    MAX_TIMEOUT_S,
    OPERATIONS,
    Operation,
    args_valid,
    catalog_version,
)
from tests._host import MakeContext
from tools.guards import macos_lint

REPO_ROOT = Path(__file__).resolve().parents[1]
PLACEHOLDER = re.compile(r"\{[a-z_][a-z0-9_]*\}")


def _resolved(value: Any, defs: dict[str, Any]) -> Any:
    """``value`` with every ``$ref`` into ``#/$defs/`` replaced and every description dropped."""
    if isinstance(value, dict):
        if "$ref" in value:
            target = defs[value["$ref"].removeprefix("#/$defs/")]
            merged = {**target, **{k: v for k, v in value.items() if k != "$ref"}}
            return _resolved(merged, defs)
        return {k: _resolved(v, defs) for k, v in value.items() if k != "description"}
    if isinstance(value, list):
        return [_resolved(v, defs) for v in value]
    return value


def _spec_args(op: str) -> dict[str, Any]:
    schema = spec.load_schema("request")
    return _resolved(schema["$defs"][f"args.{op}"], schema["$defs"])


def test_catalog_entries() -> None:
    assert set(OPERATIONS) == {"host.describe", "host.sleep", "files.sha256"}
    with pytest.raises(TypeError):
        OPERATIONS["host.shell"] = OPERATIONS["host.describe"]  # type: ignore[index]

    describe = OPERATIONS["host.describe"]
    assert describe.argv is None and not describe.runs_child
    assert describe.default_enabled

    sleep = OPERATIONS["host.sleep"]
    assert not sleep.default_enabled
    assert dict(sleep.argv or {}) == {
        "linux": ("/bin/sleep", "{n}"),
        "macos": ("/bin/sleep", "{n}"),
    }
    assert sleep.build_argv("linux", {"n": 7}) == ("/bin/sleep", "7")
    assert sleep.timeout({"n": 1}) == 31
    assert sleep.timeout({"n": 240}) == 270

    sha = OPERATIONS["files.sha256"]
    assert not sha.default_enabled
    assert dict(sha.argv or {}) == {
        "linux": ("/usr/bin/sha256sum", "--", "{path}"),
        "macos": ("/usr/bin/shasum", "-a", "256", "--", "{path}"),
    }
    assert sha.timeout({"root": "docs", "path": "a"}) == 30
    hostile = "a b; rm -rf ~ $(id) `id`"
    assert sha.build_argv("linux", {"root": "docs", "path": hostile}) == (
        "/usr/bin/sha256sum",
        "--",
        hostile,
    )

    for op in OPERATIONS.values():
        if op.argv is None:
            continue
        for family, template in op.argv.items():
            assert template[0].startswith("/"), (op.name, family)
            properties = op.params()["properties"]
            for index, element in enumerate(template):
                if PLACEHOLDER.fullmatch(element) is None:
                    continue
                kind = properties[element[1:-1]]["type"]
                if kind == "string":
                    # A string from a request always comes after "--".
                    assert "--" in template[:index], (op.name, family, element)
                else:
                    assert kind == "integer" and properties[element[1:-1]]["minimum"] >= 1

    assert re.fullmatch(r"[0-9a-f]{64}", CATALOG_VERSION)


def test_catalog_params_equal_spec_args() -> None:
    for name, op in OPERATIONS.items():
        assert op.params() == _spec_args(name), name


def test_definition_hash_covers_the_entry() -> None:
    sha = OPERATIONS["files.sha256"]
    changed_argv = dataclasses.replace(
        sha, argv=MappingProxyType({"linux": ("/bin/cat", "--", "{path}"), "macos": ("/bin/cat",)})
    )
    changed_timeout = dataclasses.replace(sha, timeout_s=31)
    hashes = {sha.definition_hash, changed_argv.definition_hash, changed_timeout.definition_hash}
    assert len(hashes) == 3
    # Whether a fresh install enables it does not change what the entry does.
    assert dataclasses.replace(sha, default_enabled=True).definition_hash == sha.definition_hash


def test_definition_hash_covers_operands_schema_limits_and_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An upgrade that changes any part of what an enabled entry does disables it (acceptance 3),
    so each part is in the hash: dropping the './-' rewrite, a schema, the extra time an argument
    buys, the cap on time, or a variable added to the child's environment."""
    sha, sleep = OPERATIONS["files.sha256"], OPERATIONS["host.sleep"]
    other_schema = sha.params_json.replace("1024", "1023")
    assert other_schema != sha.params_json
    for base, changed in (
        (sha, dataclasses.replace(sha, file_operands=())),
        (sha, dataclasses.replace(sha, params_json=other_schema)),
        (sleep, dataclasses.replace(sleep, timeout_plus_arg=None)),
    ):
        assert changed.definition_hash != base.definition_hash, changed
    before = sha.definition_hash
    monkeypatch.setattr("lucille_host.ops.CHILD_ENV", {**_exec.CHILD_ENV, "HOME": "/"})
    assert sha.definition_hash != before
    monkeypatch.undo()
    monkeypatch.setattr("lucille_host.ops.MAX_TIMEOUT_S", 301)
    assert sha.definition_hash != before


# This release's hashes, from before the catalog carried words for shells: adding the words changed
# none of them, so no install's enabled entry turned off.
HASHES = {
    "host.describe": "734b854a4e4e0fb7128dc9cce6147de7e7386c20a92b44cf77086031240cc0c4",
    "host.sleep": "868830c4433044a8d4cb3e3a39ab7985d44a6cce394362a23ae7fcfaa79a5e8d",
    "files.sha256": "1c29598400572d7e1f93e5cc3ee6df17edfad5be1a78c7e33432a2704bfdddea",
}
VERSION_BEFORE_WORDS = "06fe2f327b0916227970e377eaad94b064a7631bd7ff2696b3cef675bd607f35"


def _with_about(op: Operation, **changes: Any) -> Operation:
    return dataclasses.replace(op, about=dataclasses.replace(op.about, **changes))


def test_copy_fields_outside_definition_hash() -> None:
    assert {name: op.definition_hash for name, op in OPERATIONS.items()} == HASHES
    assert CATALOG_VERSION == VERSION_BEFORE_WORDS == catalog_version(tuple(OPERATIONS.values()))
    # summary, sends and diagnostic describe an entry and decide nothing: changing them leaves
    # every hash, and the catalog version, as they are.
    reworded = []
    for op in OPERATIONS.values():
        for changes in (
            {"summary": "Other words"},
            {"sends": "Lucille gets other words."},
            {"diagnostic": not op.about.diagnostic},
        ):
            assert _with_about(op, **changes).definition_hash == op.definition_hash, changes
        reworded.append(
            _with_about(op, summary="Other words", sends="Other words.", diagnostic=True)
        )
    assert catalog_version(tuple(reworded)) == CATALOG_VERSION
    # sends_file_content is what a shell's consent rests on, so it is part of the definition when
    # it is true: a release that makes an entry send file contents turns it off until enabled
    # again. False is not written, which is why the hashes above did not change.
    sha = OPERATIONS["files.sha256"]
    assert "sends_file_content" not in sha._definition()
    flipped = _with_about(sha, sends_file_content=True)
    assert flipped.definition_hash != sha.definition_hash
    others = tuple(op for op in OPERATIONS.values() if op.name != sha.name)
    assert catalog_version((*others, flipped)) != CATALOG_VERSION
    # What decides what the entry does is still in it.
    assert dataclasses.replace(sha, timeout_s=31).definition_hash != sha.definition_hash


def _must_never_claim() -> list[str]:
    # One reading of PRIVACY.md's list, the Mac lint's: a phrase the file wraps across lines is
    # one phrase, and fewer than 15 phrases fails.
    text = (REPO_ROOT / "PRIVACY.md").read_text(encoding="utf-8")
    return macos_lint.must_not_claim_phrases(text)


def test_catalog_copy_must_not_claim() -> None:
    phrases = _must_never_claim()
    for name, op in OPERATIONS.items():
        for words in (op.about.summary, op.about.sends):
            assert macos_lint.claims(words, phrases) == [], (name, words)
        assert 0 < len(op.about.summary) <= 60 and 0 < len(op.about.sends) <= 300
        assert op.about.summary.isascii() and op.about.sends.isascii()
    sha = OPERATIONS["files.sha256"].about
    assert sha.summary == "Check a file's fingerprint"
    assert sha.sends == (
        "Lucille gets a code that identifies the file's exact contents, not the contents."
    )
    # host.sleep is the one diagnostic entry, and no entry in this release sends file contents.
    assert [n for n, op in OPERATIONS.items() if op.about.diagnostic] == ["host.sleep"]
    assert not any(op.about.sends_file_content for op in OPERATIONS.values())


def test_the_spec_and_the_catalog_say_the_same_words() -> None:
    # spec/CLI.md section 8 lists each entry's words, as the catalog holds them.
    text = (REPO_ROOT / "spec" / "CLI.md").read_text(encoding="utf-8")
    for name, op in OPERATIONS.items():
        about = op.about
        row = (
            f"| `{name}` | {about.summary} | {about.sends} | "
            f"{str(about.sends_file_content).lower()} | {str(about.diagnostic).lower()} |"
        )
        assert row in text, row


def test_no_time_limit_passes_300_s() -> None:
    assert MAX_TIMEOUT_S == 300
    longer = dataclasses.replace(OPERATIONS["host.sleep"], timeout_s=100)
    assert longer.timeout({"n": 240}) == 300 and longer.timeout({"n": 100}) == 200


def test_an_executable_without_an_execute_bit_is_not_available(tmp_path: Path) -> None:
    program = tmp_path / "program"
    program.write_bytes(Path("/bin/sleep").read_bytes())
    template = (str(program), "{n}")
    entry = dataclasses.replace(
        OPERATIONS["host.sleep"], argv=MappingProxyType({"linux": template, "macos": template})
    )
    program.chmod(0o644)
    assert not entry.available("linux") and not entry.available("macos")
    program.chmod(0o755)
    assert entry.available("linux") and entry.available("macos")


ARG_SAMPLES: dict[str, list[Any]] = {
    "host.describe": [{}, {"x": 1}, [], None],
    "host.sleep": [{"n": 1}, {"n": 240}, {"n": 0}, {"n": 241}, {"n": "5"}, {}, {"n": 5, "m": 1}],
    "files.sha256": [
        {"root": "docs", "path": "a.txt"},
        {"root": "a" * 32, "path": "x" * 1024},
        {"root": "a" * 33, "path": "x"},
        {"root": "docs", "path": "x" * 1025},
        {"root": "Docs", "path": "x"},
        {"root": "9docs", "path": "x"},
        {"root": "my-docs_2", "path": "\U0001f600" * 1024},
        {"root": "docs", "path": ""},
        {"root": "docs"},
        {"root": "docs", "path": "x", "cwd": "/"},
        {"root": "docs", "path": 5},
    ],
}


def test_args_valid_agrees_with_the_spec_schema() -> None:
    for name, samples in ARG_SAMPLES.items():
        schema = _spec_args(name)
        for sample in samples:
            expected = jsonschema.Draft202012Validator(schema).is_valid(sample)
            assert args_valid(OPERATIONS[name], sample) is expected, (name, sample)


def test_args_valid_refuses_what_json_schema_lets_through() -> None:
    # Rule `integers`: a float is not an integer here, though JSON Schema accepts 2.0.
    assert not args_valid(OPERATIONS["host.sleep"], {"n": 2.0})
    # A boolean is never an integer.
    assert not args_valid(OPERATIONS["host.sleep"], {"n": True})
    # '$' in a pattern matches only at the very end, as in ECMA-262. (The jsonschema library uses
    # Python's re, whose '$' also matches before a final newline, so it would accept this.)
    assert not args_valid(OPERATIONS["files.sha256"], {"root": "docs\n", "path": "a"})


SPEC_STEPS_HERE = {"paused", "unknown_op", "not_enabled", "bad_args", "path_outside_root"}


def _fixture_context(
    host_state: dict[str, Any], tmp_path: Path, make_context: MakeContext
) -> Context:
    outside = tmp_path / "outside"
    outside.mkdir()
    roots: dict[str, Path] = {}
    for alias in host_state["roots"]:
        folder = tmp_path / "roots" / alias
        folder.mkdir(parents=True)
        roots[alias] = folder
    for linked in host_state["resolves_outside_root"]:
        first = linked.split("/")[0]
        (roots[host_state["roots"][0]] / first).symlink_to(outside, target_is_directory=True)
    ctx = make_context(
        enabled=[op for op in host_state["enabled_ops"] if op in OPERATIONS],
        roots=roots,
    )
    if host_state["paused"]:
        ctx.controls.pause()
    return ctx


def _decided_here(fixture: dict[str, Any]) -> bool:
    """Whether this release decides the fixture alone: its expected refusal is one of steps 5 to
    9, or it runs, and its operation is not one the spec has but this catalog does not yet."""
    expect = fixture["expect"]["reply"]
    body = fixture["body"]
    if expect is None or not isinstance(body, dict) or body.get("op") == "files.search":
        return False
    return expect["status"] in ("done", "failed") or expect.get("code") in SPEC_STEPS_HERE


DECIDED_HERE = [p for p in spec.fixture_paths("request") if _decided_here(spec.load_fixture(p))]


def test_the_fixtures_decided_here_cover_steps_5_to_9() -> None:
    codes = {spec.load_fixture(p)["expect"]["reply"].get("code") for p in DECIDED_HERE}
    assert SPEC_STEPS_HERE <= codes


@pytest.mark.parametrize("path", DECIDED_HERE, ids=lambda p: p.stem)
def test_steps_5_to_9_hold_to_the_request_fixtures(
    path: Path, tmp_path: Path, make_context: MakeContext
) -> None:
    """The fixtures this release decides alone. The others are the read loop's (steps 1 to 4,
    dedupe and the messages the host does not answer) or files.search's."""
    fixture = spec.load_fixture(path)
    expect = fixture["expect"]["reply"]
    body = fixture["body"]
    state = {**spec.default_host_state(), **fixture.get("host_state", {})}
    ctx = _fixture_context(state, tmp_path, make_context)
    job = Job(job_id=body["job_id"], op=body["op"], args=body["args"], caps=body.get("caps"))
    decided = check(job, ctx)
    if expect["status"] in ("done", "failed"):
        assert isinstance(decided, Ready), decided
    else:
        assert decided == expect["code"]
