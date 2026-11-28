"""files.search: ripgrep over the whole of one folder (decisions/0001, "The closed operation set").

The decisions (the argv, the folder, the refusals) and the host's reading of ripgrep's ``--json``
messages (``_ripgrep.document``, fed made-up output) are tested everywhere. The searches
themselves run the real ripgrep the catalog names, through the host's own executor; they need it
at that place (``/usr/bin/rg`` on Linux, ``/opt/homebrew/bin/rg`` on macOS), which CI's test jobs
install. Every document the host makes here is held to ``files.search.schema.json``.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from lucille_host import _exec, _output, _ripgrep, paths
from lucille_host.containment import holds_own
from lucille_host.jobs import Job, Outcome, Ready, check, execute
from lucille_host.ops import OPERATIONS, Operation, _check_argv
from tests._host import MakeContext
from tests.spec import _support

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
    # Its output is ripgrep's JSON, which the host reads; that is in its definition.
    assert SEARCH.output == "ripgrep_json"
    assert SEARCH._definition()["output"] == "ripgrep_json"
    assert all("output" not in op._definition() for op in OPERATIONS.values() if op is not SEARCH)
    with pytest.raises(ValueError, match="output form"):
        dataclasses.replace(SEARCH, output="text")


@pytest.mark.parametrize(
    ("fixed", "flag"), [(True, "--fixed-strings"), (False, "--no-fixed-strings")]
)
def test_the_argv_puts_the_pattern_after_double_dash(fixed: bool, flag: str) -> None:
    built = SEARCH.build_argv("linux", _args(pattern="--pre=sh", fixed_strings=fixed, max_count=7))
    assert built == (
        "/usr/bin/rg",
        "--json",
        "--no-config",
        "--no-ignore",
        "--no-follow",
        "--line-number",
        "--sort",
        "path",
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


# --- reading ripgrep's messages ------------------------------------------------------------


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _rg_match(path: Any = None, line: Any = None, number: Any = 1, **extra: Any) -> dict[str, Any]:
    data = {
        "path": {"text": "a.txt"} if path is None else path,
        "lines": {"text": "hello\n"} if line is None else line,
        "line_number": number,
        "absolute_offset": 0,
        "submatches": [{"match": {"text": "hello"}, "start": 0, "end": 5}],
        **extra,
    }
    return {"type": "match", "data": data}


def _rg(*messages: Any) -> bytes:
    """ripgrep's output: one compact JSON message a line, each ending with a newline."""
    return b"".join(
        (m if isinstance(m, bytes) else json.dumps(m, separators=(",", ":")).encode()) + b"\n"
        for m in messages
    )


def _read(raw: bytes, cap: int = _output.STREAM_CAP, *, cut: bool = False) -> dict[str, Any]:
    stdout, truncated = _ripgrep.document(raw, cap, cut=cut)
    assert _output.json_size(stdout) <= cap
    found = json.loads(stdout)
    assert _support.schema_accepts("files.search", found), found
    assert _support.prose_violations("files.search", found) == []
    assert truncated is found["truncated"]
    return found


UNREADABLE = {"matches": [], "truncated": True, "error": "unreadable_output"}


def test_only_match_messages_are_kept() -> None:
    begin = {"type": "begin", "data": {"path": {"text": "a.txt"}}}
    context = {"type": "context", "data": {"path": {"text": "a.txt"}, "lines": {"text": "x\n"}}}
    end = {"type": "end", "data": {"path": {"text": "a.txt"}, "stats": {}}}
    summary = {"type": "summary", "data": {"stats": {}}}
    found = _read(_rg(begin, context, _rg_match(number=2), end, summary))
    assert found == {
        "matches": [
            {
                "path": {"text": "a.txt"},
                "line_number": 2,
                "lines": {"text": "hello\n"},
                "line_truncated": False,
            }
        ],
        "truncated": False,
        "error": None,
    }
    assert _read(_rg(summary)) == {"matches": [], "truncated": False, "error": None}
    assert _read(b"") == {"matches": [], "truncated": False, "error": None}


def test_text_and_bytes_are_given_as_ripgrep_gives_them() -> None:
    path = {"bytes": _b64(b"bad\xffname")}
    line = {"bytes": _b64(b"hello \xfe\n")}
    (match,) = _read(_rg(_rg_match(path, line)))["matches"]
    assert (match["path"], match["lines"]) == (path, line)
    # A text that holds NUL goes as its bytes, since no reply string may hold U+0000.
    (match,) = _read(_rg(_rg_match(line={"text": "hel\u0000lo\n"})))["matches"]
    assert match["lines"] == {"bytes": _b64(b"hel\x00lo\n")}


@pytest.mark.parametrize(
    "line",
    [
        b"not json",
        b'{"type":"match"}',
        b'{"type":"match","data":{}}',
        b'{"type":"match","data":[]}',
        b'{"type":"other","data":{}}',
        b'{"type":"summary","data":{},"extra":1}',
        b'{"type":"match","type":"summary","data":{}}',
        b'{"type":"summary","data":{"x":NaN}}',
        b'{"type":"summary","data":{"x":' + b"[" * 7 + b"]" * 7 + b"}}",
        b'{"type":"summary","data":{"x":"\xff"}}',
        b"[]",
    ],
)
def test_a_line_that_is_not_a_message_reads_as_unreadable(line: bytes) -> None:
    assert _read(_rg(_rg_match(), line, _rg_match(number=2))) == UNREADABLE


@pytest.mark.parametrize(
    "match",
    [
        _rg_match(path={"text": "/etc/passwd"}),
        _rg_match(path={"text": "../outside.txt"}),
        _rg_match(path={"text": "sub/../../outside.txt"}),
        _rg_match(path={"text": "sub//a.txt"}),
        _rg_match(path={"text": "sub/"}),
        _rg_match(path={"text": ".."}),
        _rg_match(path={"bytes": _b64(b"/abs")}),
        _rg_match(path={"text": ""}),
        _rg_match(path="a.txt"),
        _rg_match(path={"text": "a", "bytes": "YQ=="}),
        _rg_match(line={"bytes": "not base64!"}),
        _rg_match(line={"text": "\ud800\n"}),
        _rg_match(line={"text": 1}),
        _rg_match(number=0),
        _rg_match(number=True),
        _rg_match(number=None),
        _rg_match(number=1.0),
        _rg_match(number=2**53),
        {"type": "match", "data": {"path": {"text": "a.txt"}, "line_number": 1}},
    ],
)
def test_a_match_not_as_ripgrep_writes_it_reads_as_unreadable(match: dict[str, Any]) -> None:
    assert _read(_rg(_rg_match(), match)) == UNREADABLE


def test_the_depth_check_runs_before_the_parser() -> None:
    # A match nests five deep; a message past DEPTH_MOST is not handed to the parser.
    assert _ripgrep.DEPTH_MOST == 8
    # The message, its data, and six arrays: eight deep, which is read.
    deep = b'{"type":"summary","data":{"x":' + b"[" * 6 + b"]" * 6 + b"}}"
    assert _read(_rg(deep))["error"] is None
    # Nine deep, a message the parser would read and the host does not.
    deeper = b'{"type":"summary","data":{"x":' + b"[" * 7 + b"]" * 7 + b"}}"
    assert _read(_rg(deeper)) == UNREADABLE


def test_a_last_line_cut_short_is_dropped_only_when_the_host_cut_the_output() -> None:
    whole = _rg(_rg_match(), _rg_match(number=2))
    part = whole[:-10]
    found = _read(part, cut=True)
    assert [m["line_number"] for m in found["matches"]] == [1] and found["truncated"]
    # With nothing cut, a line with no newline is not ripgrep's.
    assert _read(part) == UNREADABLE
    # Output the host cut is not whole even when it ends at a line's end.
    assert _read(whole, cut=True)["truncated"]


def test_at_most_matches_most_and_each_line_cut() -> None:
    many = _rg(*(_rg_match(number=n) for n in range(1, _ripgrep.MATCHES_MOST + 2)))
    found = _read(many, cap=10**9)
    assert len(found["matches"]) == _ripgrep.MATCHES_MOST and found["truncated"]
    exactly = _rg(*(_rg_match(number=n) for n in range(1, _ripgrep.MATCHES_MOST + 1)))
    assert not _read(exactly, cap=10**9)["truncated"]
    # Text is cut at a character's end; bytes at the byte.
    long = "\u00e9" * 999 + "\n"  # 1999 bytes of UTF-8
    (match,) = _read(_rg(_rg_match(line={"text": long})))["matches"]
    assert match["lines"] == {"text": "\u00e9" * 500} and match["line_truncated"]
    (match,) = _read(_rg(_rg_match(line={"bytes": _b64(b"\xff" * 5000)})))["matches"]
    assert match["lines"] == {"bytes": _b64(b"\xff" * 1000)} and match["line_truncated"]
    (match,) = _read(_rg(_rg_match(line={"text": "x" * 1000})))["matches"]
    assert len(match["lines"]["text"]) == 1000 and not match["line_truncated"]


def test_text_is_cut_at_a_characters_end() -> None:
    # One byte, then two-byte characters: byte 1000 falls inside a character, which goes whole.
    line = "a" + "\u00e9" * 999 + "\n"
    (match,) = _read(_rg(_rg_match(line={"text": line})))["matches"]
    assert match["lines"] == {"text": "a" + "\u00e9" * 499} and match["line_truncated"]
    # Four-byte characters: 1000 is a character's end, and the cut keeps 250 of them.
    (match,) = _read(_rg(_rg_match(line={"text": "\U0001f600" * 300})))["matches"]
    assert match["lines"] == {"text": "\U0001f600" * 250}
    # Three bytes, then four-byte characters: the cut drops the one byte 1000 falls inside.
    (match,) = _read(_rg(_rg_match(line={"text": "abc" + "\U0001f600" * 300})))["matches"]
    assert match["lines"] == {"text": "abc" + "\U0001f600" * 249}


def test_the_fit_counts_each_comma() -> None:
    raw = _rg(*(_rg_match(number=n) for n in range(1, 6)))
    whole, _ = _ripgrep.document(raw, _output.STREAM_CAP, cut=False)
    size = _output.json_size(whole)
    # At exactly its size the whole document fits; one byte less, the last match goes, though
    # the document without that match's comma would have been small enough.
    assert _ripgrep.document(raw, size, cut=False) == (whole, False)
    found = _read(raw, size - 1)
    assert [m["line_number"] for m in found["matches"]] == [1, 2, 3, 4] and found["truncated"]
    # The first match has no comma before it: a cap of the one-match document holds it.
    one, _ = _ripgrep.document(_rg(_rg_match()), _output.STREAM_CAP, cut=False)
    assert _read(raw, _output.json_size(one))["matches"] == [json.loads(one)["matches"][0]]


def test_a_path_past_its_most_is_left_out() -> None:
    long = {"text": "p" * (_ripgrep.PATH_MOST + 1)}
    found = _read(_rg(_rg_match(), _rg_match(path=long), _rg_match(number=3)))
    assert [m["line_number"] for m in found["matches"]] == [1, 3] and found["truncated"]
    at_most = {"text": "p" * _ripgrep.PATH_MOST}
    assert not _read(_rg(_rg_match(path=at_most)))["truncated"]


def test_the_document_fits_the_cap() -> None:
    raw = _rg(*(_rg_match(line={"text": "x" * 900 + "\n"}, number=n) for n in range(1, 200)))
    found = _read(raw)
    assert 0 < len(found["matches"]) < 199 and found["truncated"]
    for cap in (60, 200, 1000, 5000):
        _read(raw, cap)
    # A document of what fits: one match more would not.
    (one,) = _read(_rg(_rg_match()))["matches"]
    whole, _ = _ripgrep.document(_rg(_rg_match()), _output.STREAM_CAP, cut=False)
    assert _ripgrep.document(_rg(_rg_match()), _output.json_size(whole), cut=False)[0] == whole
    smaller = _read(_rg(_rg_match()), _output.json_size(whole) - 1)
    assert smaller == {"matches": [], "truncated": True, "error": None}
    assert one["line_number"] == 1
    # A cap too small for any document: no stdout, and the flag.
    assert _ripgrep.document(_rg(_rg_match()), 2, cut=False) == ("", True)
    assert _ripgrep.document(b"bad\n", 30, cut=False) == ("", True)


def test_redaction_runs_on_each_path_and_line() -> None:
    token = "ghp_" + "a" * 36
    (match,) = _read(_rg(_rg_match(line={"text": f"key {token}\n"})))["matches"]
    assert match["lines"] == {"text": "key [redacted]\n"}
    (match,) = _read(_rg(_rg_match(line={"bytes": _b64(b"\xff " + token.encode())})))["matches"]
    assert base64.b64decode(match["lines"]["bytes"]) == b"\xff [redacted]"
    # A key block a line opens and does not end runs on: no later match is kept.
    begin = "-----BEGIN PRIVATE KEY-----"
    found = _read(_rg(_rg_match(line={"text": f"x {begin}\n"}), _rg_match(number=2)))
    assert found["matches"][0]["lines"] == {"text": "x [redacted private key]"}
    assert len(found["matches"]) == 1 and found["truncated"]
    # One a path opens leaves its match out too.
    found = _read(_rg(_rg_match(), _rg_match(path={"text": begin}), _rg_match(number=3)))
    assert [m["line_number"] for m in found["matches"]] == [1] and found["truncated"]


def test_redaction_runs_before_the_line_is_cut() -> None:
    # A token that straddles the 1000-byte cut: cut first, its first part would be too short for
    # the redaction to know it, and would be sent. Redacted first, none of it is.
    token = "ghp_" + "a" * 36
    before = "x" * (_ripgrep.LINE_MOST - 21) + " "  # the token starts a word
    (match,) = _read(_rg(_rg_match(line={"text": before + token + "\n"})))["matches"]
    assert match["lines"] == {"text": before + "[redacted]\n"}
    assert not match["line_truncated"]
    raw = b"\xff" + before.encode() + token.encode() + b"y" * 2000
    (match,) = _read(_rg(_rg_match(line={"bytes": _b64(raw)})))["matches"]
    kept = base64.b64decode(match["lines"]["bytes"])
    assert kept.startswith(b"\xff" + before.encode() + b"[redacted]") and b"ghp_" not in kept
    assert len(kept) == _ripgrep.LINE_MOST and match["line_truncated"]


def test_the_childs_stdin_is_dev_null(tmp_path: Path) -> None:
    # ripgrep given no path searches standard input when it is a file or a pipe; the executor
    # gives every child /dev/null, a device, so ripgrep searches its folder and can read no
    # pattern or data from there.
    probe = (
        "import os, sys; a = os.fstat(0); b = os.stat('/dev/null');"
        "sys.stdout.write(repr((a.st_rdev == b.st_rdev, a.st_ino == b.st_ino, sys.stdin.read())))"
    )
    command = _exec.Command(argv=(sys.executable, "-c", probe), cwd=str(tmp_path), timeout_s=30)
    result = _exec.run(command)
    assert (result.ended, result.exit_code, result.stdout) == ("exited", 0, "(True, True, '')")


def test_the_executor_hands_back_raw_stdout_only_when_asked(tmp_path: Path) -> None:
    argv = (sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'a\\xffb\\x00')")
    plain = _exec.run(_exec.Command(argv=argv, cwd=str(tmp_path), timeout_s=30))
    assert (plain.stdout, plain.stdout_raw) == ("a\ufffdb", None)
    raw = _exec.run(_exec.Command(argv=argv, cwd=str(tmp_path), timeout_s=30, raw_stdout=True))
    assert (raw.stdout, raw.stdout_raw, raw.stdout_truncated) == ("", b"a\xffb\x00", False)


@pytest.mark.parametrize(
    ("printed", "expect"),
    [
        ("not json\n", UNREADABLE),
        ('{"type":"summary","data":{}}\n', {"matches": [], "truncated": False, "error": None}),
    ],
)
def test_execute_reads_the_childs_output_as_ripgrep_json(
    tmp_path: Path, make_context: MakeContext, printed: str, expect: dict[str, Any]
) -> None:
    # The host's wiring, with a stand-in for ripgrep that prints what is given.
    ctx = make_context(enabled=[], roots=["docs"])
    program = f"import sys; sys.stdout.write({printed!r}); sys.stderr.write('err')"
    ready = Ready(
        job=Job(job_id=JOB_ID, op="files.search", args=_args()),
        operation=SEARCH,
        argv=(sys.executable, "-c", program),
        cwd=str(tmp_path),
        timeout_s=30,
    )
    outcome = execute(ready, ctx)
    assert (outcome.status, outcome.exit_code, outcome.stderr) == ("done", 0, "err")
    assert outcome.stdout is not None and json.loads(outcome.stdout) == expect
    assert outcome.stdout_truncated is expect["truncated"]


# --- the searches ---------------------------------------------------------------------------


def _search(folder: Path, make_context: MakeContext, **changes: Any) -> Outcome:
    ctx = make_context(enabled=["files.search"], roots={"docs": folder})
    caps = changes.pop("caps", None)
    job = Job(job_id=JOB_ID, op="files.search", args=_args(**changes), caps=caps)
    ready = check(job, ctx)
    assert isinstance(ready, Ready), ready
    return execute(ready, ctx)


def _document(outcome: Outcome) -> dict[str, Any]:
    assert outcome.status == "done", outcome
    assert outcome.stdout is not None
    found = json.loads(outcome.stdout)
    assert _support.schema_accepts("files.search", found), found
    assert outcome.stdout_truncated is found["truncated"]
    return found


def _text(item: dict[str, str]) -> str:
    assert set(item) == {"text"}, item
    return item["text"]


def _lines(outcome: Outcome) -> list[str]:
    """Each match as ``path:line:text``, for reading; the document carries them apart."""
    found = _document(outcome)
    assert found["error"] is None
    return [
        f"{_text(m['path'])}:{m['line_number']}:{_text(m['lines']).removesuffix(chr(10))}"
        for m in found["matches"]
    ]


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
    assert _document(outcome)["matches"][0] == {
        "path": {"text": "a.txt"},
        "line_number": 1,
        "lines": {"text": "hello world\n"},
        "line_truncated": False,
    }
    assert outcome.exit_code == 0 and not outcome.stdout_truncated
    assert outcome.stdout is not None and outcome.stderr is not None
    assert str(folder) not in outcome.stdout + outcome.stderr
    assert os.path.realpath(folder) not in outcome.stdout + outcome.stderr


@needs_ripgrep
def test_no_match_is_done_with_exit_code_1(folder: Path, make_context: MakeContext) -> None:
    outcome = _search(folder, make_context, pattern="absent")
    assert (outcome.status, outcome.exit_code) == ("done", 1)
    assert _document(outcome) == {"matches": [], "truncated": False, "error": None}


@needs_ripgrep
def test_a_name_and_a_line_that_are_not_utf8_come_back_as_bytes(
    folder: Path, make_context: MakeContext
) -> None:
    if sys.platform == "darwin":
        pytest.skip("macOS's file systems refuse a name that is not UTF-8")
    name = b"bad\xffname.txt"
    with open(os.path.join(os.fsencode(folder), name), "wb") as handle:
        handle.write(b"hello \xfe\xff there\n")
    found = _document(_search(folder, make_context, max_count=1))
    (match,) = [m for m in found["matches"] if "bytes" in m["path"]]
    assert base64.b64decode(match["path"]["bytes"]) == name
    assert base64.b64decode(match["lines"]["bytes"]) == b"hello \xfe\xff there\n"


@needs_ripgrep
def test_a_newline_in_a_file_name_cannot_forge_a_match(
    folder: Path, make_context: MakeContext
) -> None:
    # In text output this name wrote a line that read as a match in sub/b.txt, line 9.
    (folder / "x\nsub:9:hello forged").write_text("hello real\n")
    found = _document(_search(folder, make_context, max_count=1))
    paths = [_text(m["path"]) for m in found["matches"]]
    assert paths == ["a.txt", "sub/b.txt", "x\nsub:9:hello forged"]
    assert found["matches"][2]["lines"] == {"text": "hello real\n"}


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
    assert outcome.stdout is not None
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
    assert outcome.stdout is not None and outcome.stderr is not None
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
    assert outcome.stdout is not None and "secret" not in outcome.stdout
    assert _lines(outcome)[0] == "a.txt:1:hello world"


@needs_ripgrep
def test_a_long_line_is_cut(folder: Path, make_context: MakeContext) -> None:
    # Under --json ripgrep writes the whole line whatever --max-columns says; the host cuts it.
    (folder / "long.txt").write_text("hello " + "x" * 5000 + "\n")
    found = _document(_search(folder, make_context, max_count=1))
    (long,) = [m for m in found["matches"] if m["path"] == {"text": "long.txt"}]
    assert long["lines"] == {"text": "hello " + "x" * 994} and long["line_truncated"]
    assert not found["truncated"]


@needs_ripgrep
def test_the_reply_is_bounded_and_says_when_it_is_not_whole(
    folder: Path, make_context: MakeContext
) -> None:
    for index in range(20):
        (folder / f"many-{index:02}.txt").write_text(("hello " + "y" * 900 + "\n") * 200)
    outcome = _search(folder, make_context)
    assert outcome.stdout is not None
    found = _document(outcome)
    assert found["truncated"] and outcome.stdout_truncated
    assert 0 < len(found["matches"]) < 4000
    assert _output.json_size(outcome.stdout) <= _output.STREAM_CAP
    # A request's lower cap is the one the document is fit to.
    small = _search(folder, make_context, caps={"stdout_bytes": 3000, "stderr_bytes": 100})
    assert small.stdout is not None and _output.json_size(small.stdout) <= 3000
    assert _document(small)["truncated"] and _document(small)["matches"]


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
    assert outcome.stdout is not None and outcome.stderr is not None
    assert "locked" not in outcome.stdout
    assert str(folder) not in outcome.stderr and os.path.realpath(folder) not in outcome.stderr
    assert _document(outcome)["error"] is None
