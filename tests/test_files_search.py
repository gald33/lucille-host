"""files.search: ripgrep over the whole of one folder (decisions/0001, "The closed operation set").

The decisions (the argv, the folder, the refusals) are tested everywhere. The searches themselves
run the real ripgrep the catalog names, through the host's own executor; they need it at that
place (``/usr/bin/rg`` on Linux, ``/opt/homebrew/bin/rg`` on macOS), which CI's test jobs install.
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from typing import Any

import pytest

from lucille_host import paths
from lucille_host.containment import holds_own
from lucille_host.jobs import Job, Outcome, Ready, check, execute
from lucille_host.ops import OPERATIONS, Operation, _check_argv
from tests._host import MakeContext

JOB_ID = "00000000-0000-4000-8000-000000000001"
SEARCH = OPERATIONS["files.search"]
FAMILY = paths.os_family()
RIPGREP = SEARCH.argv[FAMILY][0] if SEARCH.argv else ""
needs_ripgrep = pytest.mark.skipif(
    not SEARCH.available(FAMILY), reason=f"needs ripgrep at {RIPGREP}, files.search's program"
)


def _args(**changes: Any) -> dict[str, Any]:
    return {"root": "docs", "pattern": "hello", "fixed_strings": True, "max_count": 200, **changes}


# --- the entry ------------------------------------------------------------------------------


def test_the_entry() -> None:
    assert dict(SEARCH.argv or {}) == {
        "linux": ("/usr/bin/rg", *SEARCH.argv["linux"][1:]),
        "macos": ("/opt/homebrew/bin/rg", *SEARCH.argv["linux"][1:]),
    }
    assert SEARCH.walks_root and SEARCH.uses_root and not SEARCH.file_operands
    assert SEARCH.about.sends_file_content and not SEARCH.default_enabled
    assert SEARCH.timeout(_args()) == 30


@pytest.mark.parametrize(
    ("fixed", "flag"), [(True, "--fixed-strings"), (False, "--no-fixed-strings")]
)
def test_the_argv_puts_the_pattern_after_double_dash(fixed: bool, flag: str) -> None:
    built = SEARCH.build_argv("linux", _args(pattern="--pre=sh", fixed_strings=fixed, max_count=7))
    assert built == (
        "/usr/bin/rg",
        "--no-config",
        "--no-ignore",
        "--no-follow",
        "--no-heading",
        "--with-filename",
        "--line-number",
        "--color",
        "never",
        "--sort",
        "path",
        "--max-columns",
        "1000",
        "--max-columns-preview",
        flag,
        "--max-count",
        "7",
        "--",
        "--pre=sh",
    )
    # No path: ripgrep searches the folder it runs in, and prints each path relative to it.
    assert built.index("--") == len(built) - 2


def test_a_boolean_that_is_not_a_bool_is_refused_by_build_argv() -> None:
    with pytest.raises(ValueError, match="not a boolean"):
        SEARCH.build_argv("linux", _args(fixed_strings=1))


def _with_template(template: tuple[str, ...], **param_types: str) -> Operation:
    properties = {key: {"type": kind} for key, kind in param_types.items()}
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": sorted(properties),
        "properties": properties,
    }
    return dataclasses.replace(
        SEARCH,
        params_json=json.dumps(schema, sort_keys=True, separators=(",", ":")),
        argv={"linux": template},
    )


@pytest.mark.parametrize(
    ("template", "types"),
    [
        (("/bin/x", "{flag?--a:--b}", "--", "{s}"), {"flag": "string", "s": "string"}),
        (("/bin/x", "{flag?--a:--b}"), {"flag": "integer"}),
        (("/bin/x", "--", "{flag?--a:--b}"), {"flag": "boolean"}),
        (("/bin/x", "{flag?--a:--a}", "--"), {"flag": "boolean"}),
        (("/bin/x", "{flag}", "--"), {"flag": "boolean"}),
        (("/bin/x", "--", "{flag}"), {"flag": "boolean"}),
    ],
)
def test_a_template_that_places_a_boolean_otherwise_is_refused(
    template: tuple[str, ...], types: dict[str, str]
) -> None:
    with pytest.raises(RuntimeError):
        _check_argv(_with_template(template, **types))


def test_a_choice_names_only_the_catalog_flags() -> None:
    # The flags are the template's text; nothing of the request reaches them.
    for value in (True, False):
        built = SEARCH.build_argv("macos", _args(fixed_strings=value))
        assert sum(1 for e in built if e in ("--fixed-strings", "--no-fixed-strings")) == 1


# --- the decision ---------------------------------------------------------------------------


def test_it_runs_in_the_folder_given_no_path(tmp_path: Path, make_context: MakeContext) -> None:
    folder = tmp_path / "roots" / "docs"
    folder.mkdir(parents=True)
    ctx = make_context(enabled=["files.search"], roots={"docs": folder})
    decided = check(Job(job_id=JOB_ID, op="files.search", args=_args()), ctx)
    if not SEARCH.available(FAMILY):
        assert decided == "not_enabled"
        return
    assert isinstance(decided, Ready)
    assert decided.cwd == os.path.realpath(folder)
    assert decided.argv == SEARCH.build_argv(FAMILY, _args())


def test_an_unknown_alias_is_outside_the_root(tmp_path: Path, make_context: MakeContext) -> None:
    ctx = make_context(enabled=["files.search"], roots=["docs"])
    decided = check(Job(job_id=JOB_ID, op="files.search", args=_args(root="photos")), ctx)
    assert decided in ("path_outside_root", "not_enabled")
    if SEARCH.available(FAMILY):
        assert decided == "path_outside_root"


@needs_ripgrep
def test_a_folder_that_holds_the_hosts_own_folder_is_refused(
    tmp_path: Path, state_dir: Path, make_context: MakeContext
) -> None:
    # The state folder is tmp_path/state: tmp_path holds it, and so does a link to tmp_path.
    link = tmp_path / "roots" / "via-link"
    link.parent.mkdir()
    link.symlink_to(tmp_path, target_is_directory=True)
    for folder in (tmp_path, link, state_dir):
        ctx = make_context(enabled=["files.search"], roots={"docs": folder})
        decided = check(Job(job_id=JOB_ID, op="files.search", args=_args()), ctx)
        assert decided == "path_outside_root", folder
    # A folder beside the state folder holds none of it.
    beside = tmp_path / "beside"
    beside.mkdir()
    ctx = make_context(enabled=["files.search"], roots={"docs": beside})
    assert isinstance(check(Job(job_id=JOB_ID, op="files.search", args=_args()), ctx), Ready)


def test_holds_own(tmp_path: Path) -> None:
    own = tmp_path / "a" / "b" / "state"
    own.mkdir(parents=True)
    other = tmp_path / "other"
    other.mkdir()
    linked_own = other / "state-link"
    linked_own.symlink_to(own, target_is_directory=True)
    assert holds_own(str(tmp_path), [str(own)])
    assert holds_own(str(tmp_path / "a"), [str(own)])
    assert holds_own(str(own), [str(own)])
    assert not holds_own(str(own / ".." / ".." / ".." / "other"), [str(own)])
    assert not holds_own(str(other), [str(tmp_path / "missing")])
    # An own folder reached through a link is judged by its real path.
    assert holds_own(str(tmp_path / "a"), [str(linked_own)])
    assert not holds_own(str(other), [str(linked_own)])
    # A folder that cannot be looked up is refused.
    assert holds_own(str(tmp_path / "missing"), [])


# --- the searches ---------------------------------------------------------------------------


def _search(folder: Path, make_context: MakeContext, **changes: Any) -> Outcome:
    ctx = make_context(enabled=["files.search"], roots={"docs": folder})
    ready = check(Job(job_id=JOB_ID, op="files.search", args=_args(**changes)), ctx)
    assert isinstance(ready, Ready), ready
    return execute(ready, ctx)


def _lines(outcome: Outcome) -> list[str]:
    assert outcome.status == "done", outcome
    return outcome.stdout.splitlines()


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    folder = tmp_path / "roots" / "docs"
    (folder / "sub").mkdir(parents=True)
    (folder / "a.txt").write_text("hello world\nnothing\nhello again\n")
    (folder / "sub" / "b.txt").write_text("say hello\n")
    return folder


@needs_ripgrep
def test_matches_come_back_with_paths_relative_to_the_folder(
    folder: Path, make_context: MakeContext
) -> None:
    outcome = _search(folder, make_context)
    assert _lines(outcome) == [
        "a.txt:1:hello world",
        "a.txt:3:hello again",
        "sub/b.txt:1:say hello",
    ]
    assert outcome.exit_code == 0
    assert str(folder) not in outcome.stdout + outcome.stderr
    assert os.path.realpath(folder) not in outcome.stdout + outcome.stderr


@needs_ripgrep
def test_no_match_is_done_with_exit_code_1(folder: Path, make_context: MakeContext) -> None:
    outcome = _search(folder, make_context, pattern="absent")
    assert (outcome.status, outcome.exit_code, outcome.stdout) == ("done", 1, "")


@needs_ripgrep
def test_max_count_is_the_most_lines_a_file(folder: Path, make_context: MakeContext) -> None:
    outcome = _search(folder, make_context, max_count=1)
    assert _lines(outcome) == ["a.txt:1:hello world", "sub/b.txt:1:say hello"]


@needs_ripgrep
def test_fixed_strings_decides_whether_the_pattern_is_a_regular_expression(
    folder: Path, make_context: MakeContext
) -> None:
    (folder / "c.txt").write_text("h.llo literally\n")
    literal = _search(folder, make_context, pattern="h.llo", fixed_strings=True)
    assert _lines(literal) == ["c.txt:1:h.llo literally"]
    regex = _search(folder, make_context, pattern="h.llo", fixed_strings=False)
    assert [line.split(":")[0] for line in _lines(regex)] == [
        "a.txt",
        "a.txt",
        "c.txt",
        "sub/b.txt",
    ]


@needs_ripgrep
@pytest.mark.parametrize("pattern", ["--version", "-e", "--pre=/bin/sh", "--files", "-"])
def test_a_pattern_that_looks_like_a_flag_is_a_pattern(
    folder: Path, make_context: MakeContext, pattern: str
) -> None:
    (folder / "flags.txt").write_text(f"x {pattern} y\n")
    outcome = _search(folder, make_context, pattern=pattern)
    assert _lines(outcome) == [f"flags.txt:1:x {pattern} y"]


@needs_ripgrep
def test_a_file_named_like_a_flag_is_a_file(folder: Path, make_context: MakeContext) -> None:
    (folder / "--version").write_text("hello flag\n")
    (folder / "-").write_text("hello dash\n")
    outcome = _search(folder, make_context, max_count=1)
    assert "--version:1:hello flag" in _lines(outcome)
    assert "-:1:hello dash" in _lines(outcome)


@needs_ripgrep
def test_hidden_files_are_never_searched_whatever_an_ignore_file_says(
    tmp_path: Path, folder: Path, make_context: MakeContext
) -> None:
    (folder / ".env").write_text("hello secret\n")
    (folder / ".ssh").mkdir()
    (folder / ".ssh" / "id").write_text("hello key\n")
    (folder / "sub" / ".hidden").write_text("hello hidden\n")
    # Each of these would name the hidden files to search, were ignore files read.
    for name in (".ignore", ".gitignore", ".rgignore"):
        (folder / name).write_text("!.env\n!.ssh/\n!sub/.hidden\n")
    (tmp_path / "roots" / ".ignore").write_text("!docs/.env\n")
    (folder / ".git").mkdir()
    outcome = _search(folder, make_context)
    assert "secret" not in outcome.stdout and "key" not in outcome.stdout
    assert "hidden" not in outcome.stdout
    assert _lines(outcome) == [
        "a.txt:1:hello world",
        "a.txt:3:hello again",
        "sub/b.txt:1:say hello",
    ]


@needs_ripgrep
def test_ignored_files_are_searched_since_no_ignore_file_is_read(
    folder: Path, make_context: MakeContext
) -> None:
    (folder / ".gitignore").write_text("build/\n")
    (folder / "build").mkdir()
    (folder / "build" / "out.txt").write_text("hello built\n")
    outcome = _search(folder, make_context, max_count=1)
    assert "build/out.txt:1:hello built" in _lines(outcome)


@needs_ripgrep
def test_no_link_is_followed_out_of_the_folder(
    tmp_path: Path, folder: Path, make_context: MakeContext
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("hello outside\n")
    (folder / "file-link").symlink_to(outside / "secret.txt")
    (folder / "dir-link").symlink_to(outside, target_is_directory=True)
    outcome = _search(folder, make_context)
    assert "outside" not in outcome.stdout + outcome.stderr
    assert _lines(outcome) == [
        "a.txt:1:hello world",
        "a.txt:3:hello again",
        "sub/b.txt:1:say hello",
    ]


@needs_ripgrep
def test_no_configuration_file_is_read(
    tmp_path: Path, folder: Path, make_context: MakeContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "ripgreprc"
    config.write_text("--hidden\n--follow\n--no-filename\n")
    monkeypatch.setenv("RIPGREP_CONFIG_PATH", str(config))
    (folder / ".env").write_text("hello secret\n")
    ctx = make_context(
        enabled=["files.search"],
        roots={"docs": folder},
        environ={"RIPGREP_CONFIG_PATH": str(config)},
    )
    ready = check(Job(job_id=JOB_ID, op="files.search", args=_args()), ctx)
    assert isinstance(ready, Ready)
    outcome = execute(ready, ctx)
    assert "secret" not in outcome.stdout
    assert _lines(outcome)[0] == "a.txt:1:hello world"


@needs_ripgrep
def test_a_long_line_is_cut(folder: Path, make_context: MakeContext) -> None:
    (folder / "long.txt").write_text("hello " + "x" * 5000 + "\n")
    outcome = _search(folder, make_context, max_count=1)
    long = next(line for line in _lines(outcome) if line.startswith("long.txt:"))
    assert len(long.encode()) < 1200 and "x" * 5000 not in long


@needs_ripgrep
def test_a_file_the_host_cannot_read_is_named_relative_on_stderr(
    folder: Path, make_context: MakeContext
) -> None:
    if os.getuid() == 0:
        pytest.skip("root reads every file; CI runs this test as an ordinary user")
    locked = folder / "locked.txt"
    locked.write_text("hello locked\n")
    locked.chmod(0)
    try:
        outcome = _search(folder, make_context)
    finally:
        locked.chmod(0o600)
    assert "locked" not in outcome.stdout
    assert str(folder) not in outcome.stderr and os.path.realpath(folder) not in outcome.stderr
