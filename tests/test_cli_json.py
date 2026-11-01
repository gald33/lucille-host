"""The command line's ``--json`` contract (spec/CLI.md): every verb's objects, their schemas and the
golden fixtures, the exit codes, and the codes of every failure.

Each fixture is run as a user would run it, in a profile set up for it in a home of its own: the
case's set-up below, then its command line. Its objects must validate against their schemas and
equal the fixture once the sample values are put in place. The same set-up is made again in another
home, the line is run without ``--json``, and the exit code and standard error must be the same.
"""

from __future__ import annotations

import contextlib
import dataclasses
import errno
import io
import json
import os
import re
import select
import subprocess
import sys
import time
import unicodedata
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
from keyring.backends.fail import Keyring as FailKeyring
from keyring.errors import KeyringError
from switchboard import pairing
from switchboard.testing import hub

from lucille_host import (
    __version__,
    _switchboard,
    gate,
    link,
    paths,
    policy,
    rooms,
    spec,
    state,
    store,
)
from lucille_host.cli import INTERNAL_MESSAGE, main
from lucille_host.ops import OPERATIONS, Operation
from tests._cli import HostCli, collecting
from tests._host import HostEnv, lock_held
from tests._keyring import MemoryKeyring
from tests.spec import _support

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated"
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE = REPO_ROOT / "src" / "lucille_host" / "spec" / "cli" / "v1"
CLI_MD = REPO_ROOT / "spec" / "CLI.md"
CASES = sorted(path.stem for path in (SOURCE / "fixtures").glob("*.json"))

# The sample values the fixtures hold.
SAMPLE_HOME = "/home/me"
SAMPLE_VERSION = "0.1.0.dev0"
SAMPLE_TIME = "2026-11-01T12:00:00Z"
SAMPLE_SAS = "1234-5678"
SAMPLE_PAIR = "pair-abcdef"
SAMPLE_GENERATION = 1
TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
PROFILES = ("default", "mac-app")
USAGE_MESSAGE = "the command line could not be parsed; standard error says why"

# What a test patched away, so that a second set-up in the same test starts from the real thing.
_OPEN_LINK = _switchboard.open_link
_PAIR_WAIT = _switchboard.PairNotes.wait
_ACCEPT = _switchboard.Link.accept
_RESOLVE = _switchboard.Link.resolve
_HELD = link.Session.held


@dataclass(frozen=True)
class Ran:
    rc: int
    out: str
    err: str

    @property
    def objects(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.out.splitlines()]


class World:
    """A home, a keyring in memory and, when asked, Switchboard's test hub, for one command line."""

    def __init__(
        self,
        root: Path,
        stack: contextlib.ExitStack,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        *,
        with_hub: bool = False,
    ) -> None:
        self.root = root
        self.stack = stack
        self.capsys = capsys
        self.monkeypatch = monkeypatch
        self.home = root / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HOME", str(self.home))
        self.keyring = MemoryKeyring()
        monkeypatch.setattr("keyring.core._keyring_backend", self.keyring)
        monkeypatch.setattr("lucille_host._switchboard.open_link", _OPEN_LINK)
        monkeypatch.setattr("lucille_host._switchboard.PairNotes.wait", _PAIR_WAIT)
        monkeypatch.setattr(_switchboard.Link, "accept", _ACCEPT)
        monkeypatch.setattr(_switchboard.Link, "resolve", _RESOLVE)
        monkeypatch.setattr(link.Session, "held", _HELD)
        self.cli: HostCli | None = None
        if with_hub:
            h = stack.enter_context(hub())
            env = HostEnv(home=self.home, dirs=paths.dirs(), keyring=self.keyring)
            self.cli = HostCli(h, env, root, capsys, monkeypatch)
            stack.callback(self.cli.stopping.set)
        self.lucille: Any = None
        self.stdin = ""
        self.during: Callable[[], contextlib.AbstractContextManager[Any]] = contextlib.nullcontext

    @property
    def dirs(self) -> paths.Dirs:
        return paths.dirs()

    @property
    def host(self) -> HostCli:
        assert self.cli is not None, "this set-up needs the hub"
        return self.cli

    def path(self, sample: str) -> str:
        """A path of a fixture, under this home."""
        return str(self.home) + sample.removeprefix(SAMPLE_HOME)

    def run(self, *argv: str, stdin: str = "") -> Ran:
        self.capsys.readouterr()
        self.monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
        try:
            rc = main(list(argv))
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 1
        out, err = self.capsys.readouterr()
        return Ran(rc, out, err)

    def ok(self, *argv: str, stdin: str = "") -> Ran:
        ran = self.run(*argv, stdin=stdin)
        assert ran.rc == 0, (argv, ran.err)
        return ran

    def every_executable(self, missing: tuple[str, ...] = ()) -> None:
        """Every operation's executable present here, but those named ``missing``."""
        self.monkeypatch.setattr(Operation, "available", lambda op, _family: op.name not in missing)

    def about(self, name: str, **changes: Any) -> None:
        """A catalog entry's words changed, until the test ends."""
        op = OPERATIONS[name]
        before = op.about
        object.__setattr__(op, "about", dataclasses.replace(before, **changes))
        self.stack.callback(object.__setattr__, op, "about", before)

    def policy_file(self, enabled: dict[str, str], roots: dict[str, str] | None = None) -> None:
        paths.ensure_dir(self.dirs.config)
        folders = {alias: policy.Root(path=path) for alias, path in (roots or {}).items()}
        policy.save(
            self.dirs.policy_file,
            policy.Policy(enabled=MappingProxyType(enabled), roots=MappingProxyType(folders)),
        )

    def folder(self, name: str) -> str:
        made = self.home / name
        made.mkdir(parents=True, exist_ok=True)
        return str(made)

    # --- pairing --------------------------------------------------------------------------

    def code(self) -> str:
        self.lucille = self.host.lucille()
        return self.lucille.start()

    def paired(self, *, confirm: bool = False) -> None:
        self.lucille = self.host.lucille()
        self.host.paired(self.lucille)
        if confirm:
            self.ok("pair", "confirm")

    def interrupted(self) -> None:
        self.host.interrupted(self.code())

    def sas(self) -> str | None:
        if self.lucille is None or self.lucille.name is None:
            return None
        record = self.lucille.pairs.get(self.lucille.name)
        return None if record is None or not record.sas else str(record.sas)


# --- the set-up of each fixture -------------------------------------------------------------------


def _ops_every_state(w: World) -> None:
    w.every_executable(missing=("host.sleep",))
    w.policy_file(
        {
            "host.sleep": OPERATIONS["host.sleep"].definition_hash,
            "files.sha256": "0" * 64,
            "host.nothing": "1" * 64,
        }
    )


def _status_paired(w: World) -> None:
    w.paired(confirm=True)
    w.ok("roots", "add", "docs", w.folder("Documents"))
    now = w.host.h.now
    found = w.host.gate()
    assert found is not None
    record = rooms.Record(
        pair=w.lucille.name,
        made=found.created_at,
        held={
            "room-1": rooms.Held("workspace-1", now),
            "room-2": rooms.Held("workspace-2", now, rooms.LUCILLE_LEFT),
        },
    )
    rooms.save(w.dirs.rooms_file, record)
    state.write_json(
        w.dirs.status_file,
        {
            "v": 1,
            "running": False,
            "pid": 4242,
            "written_at": now,
            "last_contact": now,
            "skew": 1.5,
            "skewed": False,
            "reading": [],
            "read_stuck": [],
        },
    )


def _leaked(w: World) -> None:
    code = w.code()
    other = pairing.PairStore(w.root / "other", env={})
    pairing.accept(code, None, store=other, connect=w.host.h.client_class(), now=w.host.h.now)
    w.stdin = code + "\n"


def _pair_code_stdin(w: World) -> None:
    w.every_executable()
    w.ok("ops", "enable", "host.sleep")
    w.stdin = w.code() + "\n"
    lucille = w.lucille
    w.during = lambda: collecting(lucille)


def _sas_pending(w: World) -> None:
    w.interrupted()
    assert w.lucille.collect().state == "paired"


def _log(w: World) -> None:
    folder = paths.ensure_dir(w.dirs.state)
    (folder / "audit.log").write_text('{"kind":"test","n":1}\n{"kind":"test","n":2}\n')


def _policy_previous(w: World) -> None:
    paths.ensure_dir(w.dirs.config)
    previous = policy.Policy(
        enabled=MappingProxyType(
            {name: OPERATIONS[name].definition_hash for name in ("host.sleep", "files.sha256")}
        ),
        roots=MappingProxyType({"docs": policy.Root(path=w.path("/home/me/Documents"))}),
    )
    policy.save(w.dirs.previous_policy_file, previous)


def _roots_check(w: World) -> None:
    docs = w.folder("Documents")
    Path(docs, "a.txt").write_text("a\n")
    w.ok("roots", "add", "docs", docs)
    gone = w.folder("Gone")
    w.ok("roots", "add", "gone", gone)
    os.rmdir(gone)


def _status_unreadable(w: World) -> None:
    w.ok("pause")
    w.monkeypatch.setattr("keyring.core._keyring_backend", FailKeyring())


def _sends_file_content(w: World) -> None:
    w.every_executable()
    w.about("files.sha256", sends_file_content=True)


def _two_folders(w: World) -> None:
    w.ok("roots", "add", "docs", w.folder("Documents"))
    w.ok("roots", "add", "photos", w.folder("Photos"))


def _documents(w: World) -> None:
    w.folder("Documents")


def _docs_added(w: World) -> None:
    w.ok("roots", "add", "docs", w.folder("Documents"))


def _cafe_notes_taken(w: World) -> None:
    w.ok("roots", "add", "cafe-notes", w.folder("Notes"))


def _paused(w: World) -> None:
    w.ok("pause")


SETUPS: dict[str, Callable[[World], None]] = {
    "ops-list": lambda w: w.every_executable(),
    "ops-list-every-state": _ops_every_state,
    "ops-list-sends-file-content": _sends_file_content,
    "ops-enable": lambda w: w.every_executable(),
    "ops-disable-not-in-catalog": lambda w: w.policy_file({"host.nothing": "1" * 64}),
    "roots-list": _two_folders,
    "roots-add": _documents,
    "roots-add-suggest-alias": _cafe_notes_taken,
    "roots-remove": _docs_added,
    "roots-check": _roots_check,
    "resume": _paused,
    "status-unreadable": _status_unreadable,
    "status-paired": _status_paired,
    "log": _log,
    "forget": lambda w: w.paired(confirm=True),
    "pair-code-stdin": _pair_code_stdin,
    "pair-code-stdin-leaked": _leaked,
    "pair-status-waiting": lambda w: w.interrupted(),
    "pair-status-sas-pending": _sas_pending,
    "pair-status-confirmed": lambda w: w.paired(confirm=True),
    "pair-confirm": lambda w: w.paired(),
    "pair-reject": lambda w: w.paired(),
    "policy-previous": _policy_previous,
}
# The cases whose set-up pairs, or whose command reaches the hub.
HUB_CASES = frozenset(
    {
        "status-paired",
        "forget",
        "pair-code-stdin",
        "pair-code-stdin-leaked",
        "pair-status-waiting",
        "pair-status-sas-pending",
        "pair-status-confirmed",
        "pair-confirm",
        "pair-reject",
    }
)


# --- putting the sample values in place -----------------------------------------------------------


def _family() -> str:
    try:
        return paths.os_family()
    except paths.UnsupportedPlatform:
        return "linux"


def _replacements(w: World) -> list[tuple[str, str]]:
    """Real values and the samples the fixtures hold in their place, the longest paths first."""
    pairs: list[tuple[str, str]] = []
    for profile in PROFILES:
        # This world's own home: HOME in the environment is the last world made.
        found = paths.dirs(profile, family=_family(), environ={"HOME": str(w.home)})
        pairs.append((str(found.state), f"{SAMPLE_HOME}/.local/state/lucille-host/{profile}"))
        pairs.append((str(found.config), f"{SAMPLE_HOME}/.config/lucille-host/{profile}"))
    pairs.append((str(w.home), SAMPLE_HOME))
    if w.lucille is not None and w.lucille.name is not None:
        pairs.append((w.lucille.name, SAMPLE_PAIR))
    sas = w.sas()
    if sas is not None:
        pairs.append((sas, SAMPLE_SAS))
    return pairs


def _sampled(value: Any, w: World, key: str | None = None) -> Any:
    """``value`` with the real values put back to the fixtures' samples."""
    if isinstance(value, dict):
        return {k: _sampled(v, w, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_sampled(v, w) for v in value]
    if key == "generation" and isinstance(value, int):
        return SAMPLE_GENERATION
    if not isinstance(value, str):
        return value
    if TIME.fullmatch(value):
        return SAMPLE_TIME
    if value == __version__:
        return SAMPLE_VERSION
    family = _family()
    for op in OPERATIONS.values():
        if value == op.executable(family):
            return op.executable("linux")
    for real, sample in _replacements(w):
        value = value.replace(real, sample)
    return value


def _real_argv(argv: list[str], w: World) -> list[str]:
    """``argv`` with the fixtures' home, and ``{state}`` for the profile's state folder, made
    real."""
    real = [w.path(word) if word.startswith(SAMPLE_HOME) else word for word in argv]
    return [str(w.dirs.state) if word == "{state}" else word for word in real]


def _without_json(argv: list[str]) -> list[str]:
    if "--" in argv:
        cut = argv.index("--")
        return [word for word in argv[:cut] if word != "--json"] + argv[cut:]
    return [word for word in argv if word != "--json"]


@pytest.fixture
def stack() -> Iterator[contextlib.ExitStack]:
    with contextlib.ExitStack() as opened:
        yield opened


def _run_case(
    case: str,
    argv: list[str],
    root: Path,
    stack: contextlib.ExitStack,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Ran, World]:
    world = World(root, stack, capsys, monkeypatch, with_hub=case in HUB_CASES)
    SETUPS.get(case, lambda _w: None)(world)
    with world.during():
        ran = world.run(*_real_argv(argv, world), stdin=world.stdin)
    return ran, world


# --- the verbs each error code belongs to, from spec/CLI.md ---------------------------------------


def _cli_md() -> str:
    return CLI_MD.read_text(encoding="utf-8")


def general_codes() -> set[str]:
    found = re.search(r"the codes every verb can give: (.*?)\n\n", _cli_md(), re.DOTALL)
    assert found is not None
    return set(re.findall(r"`([a-z_]+)`", found.group(1)))


def verb_codes() -> dict[str, set[str]]:
    """Each verb's row of the table in spec/CLI.md section 7: its key, and its codes."""
    text = _cli_md()
    start = text.index("| Verb | On success | Codes |")
    rows = text[start:].split("\n\n", 1)[0].splitlines()[2:]
    table: dict[str, set[str]] = {}
    for row in rows:
        verb, _, codes = (cell.strip() for cell in row.strip("|").split("|"))
        for chunk in re.findall(r"`([^`]+)`", verb):
            words = []
            for word in chunk.split():
                if word.startswith(("[", "<")):
                    break
                words.append(word)
            table[" ".join(words)] = set(re.findall(r"`([a-z_]+)`", codes))
    return table


def verb_of(argv: list[str]) -> str:
    """The key of the table row an argv belongs to."""
    words: list[str] = []
    skip = False
    for word in argv:
        if word == "--":
            break
        if skip:
            skip = False
            continue
        if word == "--profile":
            skip = True
            continue
        if word != "--json":
            words.append(word)
    if not words or words[0] == "--version":
        return "--version"
    verb = words[0]
    if verb in ("ops", "roots", "policy"):
        key = f"{verb} {words[1]}"
        if key == "roots add" and any(w.startswith("--suggest-alias") for w in words):
            key += " --suggest-alias"
        return key
    if verb == "pair":
        if len(words) > 1 and words[1] in ("status", "confirm", "reject"):
            return f"pair {words[1]}"
        return "pair --code-stdin"
    return verb


def assert_listed(argv: list[str], code: str) -> None:
    allowed = verb_codes().get(verb_of(argv), set()) | general_codes()
    assert code in allowed, f"{code} is not listed for {verb_of(argv)} in spec/CLI.md"


def assert_objects(ran: Ran) -> list[dict[str, Any]]:
    """Every line on standard output is one object of the contract, written compactly, that its
    schema accepts."""
    assert ran.out.isascii()
    objects = _support.cli_objects(ran.out)
    assert not any(line.lstrip().startswith("{") for line in ran.err.splitlines()), ran.err
    return objects


# --- the fixtures ---------------------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES)
def test_cli_json_matches_schema(
    case: str,
    tmp_path: Path,
    host_env: HostEnv,
    stack: contextlib.ExitStack,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = json.loads((SOURCE / "fixtures" / f"{case}.json").read_text(encoding="ascii"))
    assert set(fixture) == {"description", "argv", "exit", "stdout"}
    argv = fixture["argv"]
    ran, world = _run_case(case, argv, tmp_path / "json", stack, capsys, monkeypatch)
    assert ran.rc == fixture["exit"], ran.err
    objects = assert_objects(ran)
    assert _sampled(objects, world) == fixture["stdout"]
    # Written with their fields in the fixture's order, which is spec/CLI.md section 6's.
    in_order = [_support.compact(found) for found in _sampled(objects, world)]
    assert in_order == [_support.compact(found) for found in fixture["stdout"]]
    if ran.rc != 0:
        assert objects and objects[-1]["type"] == "error"
        assert_listed(argv, objects[-1]["code"])
    # The same line, without --json, from the same set-up made again: the same exit and stderr.
    # `run` without --json would run.
    if case == "run-json":
        return
    text, text_world = _run_case(
        case, _without_json(argv), tmp_path / "text", stack, capsys, monkeypatch
    )
    assert text.rc == ran.rc, text.err
    assert _sampled(text.err, text_world) == _sampled(ran.err, world)


# --- ops ------------------------------------------------------------------------------------------


def _ops(w: World) -> dict[str, dict[str, Any]]:
    ran = w.ok("--json", "ops", "list")
    return {found["name"]: found for found in assert_objects(ran)}


def test_ops_list_json_enabled_and_sends_file_content(
    tmp_path: Path,
    host_env: HostEnv,
    stack: contextlib.ExitStack,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = World(tmp_path, stack, capsys, monkeypatch)
    family = paths.os_family()
    # files.sha256 enabled and host.sleep not, both missing their executables here: the switch's
    # position tells them apart.
    w.every_executable(missing=("files.sha256", "host.sleep"))
    w.policy_file({"files.sha256": OPERATIONS["files.sha256"].definition_hash})
    listed = _ops(w)
    sha, sleep = listed["files.sha256"], listed["host.sleep"]
    assert (sha["state"], sha["enabled"]) == ("unavailable", True)
    assert (sleep["state"], sleep["enabled"]) == ("unavailable", False)
    assert sha["missing_executable"] == OPERATIONS["files.sha256"].executable(family)
    assert sleep["missing_executable"] == "/bin/sleep"
    assert "missing_executable" not in listed["host.describe"]
    # Changed by an upgrade: off, and says so; a name the catalog lacks has only its five fields.
    w.every_executable()
    w.policy_file({"files.sha256": "0" * 64, "host.nothing": "1" * 64})
    listed = _ops(w)
    assert (listed["files.sha256"]["state"], listed["files.sha256"]["enabled"]) == (
        "disabled_by_upgrade",
        False,
    )
    assert listed["host.nothing"] == {
        "v": 1,
        "type": "op",
        "name": "host.nothing",
        "state": "not_in_catalog",
        "enabled": False,
    }
    # sends_file_content and diagnostic are booleans from the entry, never from its words.
    for name in OPERATIONS:
        assert listed[name]["sends_file_content"] is False
        assert listed[name]["diagnostic"] is (name == "host.sleep")
    w.about("files.sha256", sends_file_content=True)
    assert _ops(w)["files.sha256"]["sends_file_content"] is True
    # Changed by an upgrade and its executable missing here: `ops enable` refuses it, so it is
    # unavailable and says what is missing, in `ops list` and in `status` alike.
    w.every_executable(missing=("files.sha256",))
    w.policy_file({"files.sha256": "0" * 64})
    missing = OPERATIONS["files.sha256"].executable(family)
    sha = _ops(w)["files.sha256"]
    assert (sha["state"], sha["enabled"]) == ("unavailable", False)
    assert sha["missing_executable"] == missing
    assert f"files.sha256  disabled; not on this computer: {missing} is missing\n" in (
        w.ok("ops", "list").out
    )
    ran = w.run("--json", "ops", "enable", "files.sha256")
    assert ran.rc == 1 and assert_objects(ran)[-1]["code"] == "unavailable"
    (status,) = assert_objects(w.ok("--json", "status"))
    assert status["policy"]["disabled"] == [{"name": "files.sha256", "why": "unavailable"}]
    assert "disabled: files.sha256: not on this computer\n" in w.ok("status").out


def test_ops_enable_and_disable_write_the_switch_as_it_is(
    tmp_path: Path,
    host_env: HostEnv,
    stack: contextlib.ExitStack,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = World(tmp_path, stack, capsys, monkeypatch)
    w.every_executable(missing=("files.sha256",))
    (enabled,) = assert_objects(w.ok("--json", "ops", "enable", "host.sleep"))
    assert (enabled["state"], enabled["enabled"]) == ("enabled", True)
    # Enabled, and then its executable went: disabling it says what it is now.
    w.policy_file({"files.sha256": OPERATIONS["files.sha256"].definition_hash})
    (disabled,) = assert_objects(w.ok("--json", "ops", "disable", "files.sha256"))
    assert (disabled["state"], disabled["enabled"]) == ("unavailable", False)
    # A name the catalog lacks is taken out, and the next list has no row for it.
    w.policy_file({"host.nothing": "1" * 64})
    assert assert_objects(w.ok("--json", "ops", "disable", "host.nothing")) == [
        {"v": 1, "type": "op_removed", "name": "host.nothing"}
    ]
    assert "host.nothing" not in _ops(w)


# --- roots ----------------------------------------------------------------------------------------


def _vectors() -> list[dict[str, Any]]:
    return json.loads((REPO_ROOT / "macos" / "Tests" / "alias-vectors.json").read_text())


# Names the first build of the Mac app writes in Latin letters with the system's transliteration,
# which the core does not have: the core gives `folder`, which the user can change.
OTHER_SCRIPTS = re.compile("[\u0400-\u04ff\u3040-\u30ff\u4e00-\u9fff]")


def test_suggest_alias_valid_and_unused(
    tmp_path: Path,
    host_env: HostEnv,
    stack: contextlib.ExitStack,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    valid = re.compile(r"[a-z][a-z0-9_-]{0,31}")
    cases = []
    for vector in _vectors():
        taken = set(vector.get("taken", []))
        expected = vector["alias"]
        if OTHER_SCRIPTS.search(vector["folder"]):
            expected = "folder-2" if "folder" in taken else "folder"
        cases.append((vector["folder"], taken, expected))
    cases += [
        ("\u0414\u043e\u043a\u0443\u043c\u0435\u043d\u0442\u044b", {"folder"}, "folder-2"),
        ("\u0130stanbul", set(), "istanbul"),
        ("\uff24\uff2f\uff23\uff33", set(), "docs"),
        ("\u0141\u00d3D\u0179 \u0152uvre \u00deorn \u00c6SIR", set(), "lodz-oeuvre-thorn-aesir"),
        ("x" * 300, set(), "x" * 32),
        ("x" * 300, {"x" * 32}, "x" * 30 + "-2"),
        ("caf\udce9", set(), "caf"),
        # A `_` left at the end by the cut to 32, or by the cut before a suffix, is dropped too.
        ("a" * 31 + "_b", set(), "a" * 31),
        ("a" * 29 + "_bb", {"a" * 29 + "_bb"}, "a" * 29 + "-2"),
        # A name in decomposed form, each mark after its letter, gives what the composed one gives.
        ("Re\u0301sume\u0301", set(), "resume"),
        ("Zu\u0308rich Fotos", set(), "zurich-fotos"),
        ("A\u030angstro\u0308m", set(), "angstrom"),
        # A Latin letter past U+017F is not in the table: it is `-`, as spec/CLI.md 9.1 says. In
        # decomposed form its mark is dropped and its plain letter stays, so the two differ.
        ("Bra\u0219ov", set(), "bra-ov"),
        ("Bras\u0326ov", set(), "brasov"),
        ("Ti\u1ebfng Vi\u1ec7t", set(), "ti-ng-vi-t"),
        ("Tie\u0302\u0301ng Vie\u0323\u0302t", set(), "tieng-viet"),
    ]
    for name, taken, expected in cases:
        found = policy.suggest_alias(name, taken)
        assert valid.fullmatch(found) and found not in taken, (name, found)
        assert found == expected, (name, taken, found)
    # Through the command line: a suggestion `roots add` takes, then `-2` for a folder named alike.
    w = World(tmp_path, stack, capsys, monkeypatch)
    first = w.folder("Tax Returns")
    (suggested,) = assert_objects(w.ok("--json", "roots", "add", "--suggest-alias", first))
    assert suggested == {"v": 1, "type": "alias_suggestion", "alias": "tax-returns"}
    w.ok("roots", "add", "--", suggested["alias"], first)
    second = w.folder("elsewhere/Tax Returns")
    ran = w.ok("roots", "add", f"--suggest-alias={second}", "--json")
    assert assert_objects(ran)[0]["alias"] == "tax-returns-2"
    assert w.ok("roots", "add", "--suggest-alias", second).out == "tax-returns-2\n"
    # The name is the last part of the folder's absolute path: given with a `/` after it, as a
    # completed path is, or as `.`.
    papers = w.folder("Papers")
    ran = w.ok("--json", "roots", "add", "--suggest-alias", papers + "/")
    assert assert_objects(ran)[0]["alias"] == "papers"
    monkeypatch.chdir(papers)
    assert w.ok("roots", "add", "--suggest-alias", ".").out == "papers\n"


def _tree(root: Path) -> dict[str, tuple[int, int, int, bytes]]:
    seen = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        content = path.read_bytes() if path.is_file() else b""
        seen[str(path)] = (info.st_mode, info.st_size, info.st_mtime_ns, content)
    return seen


def test_suggest_alias_writes_nothing(
    tmp_path: Path,
    host_env: HostEnv,
    stack: contextlib.ExitStack,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = World(tmp_path, stack, capsys, monkeypatch)
    folder = w.folder("Documents")
    for with_policy in (False, True):
        if with_policy:
            w.ok("roots", "add", "documents", folder)
        before = _tree(w.home)
        with monkeypatch.context() as patch:
            # It takes no lock: another verb may hold the policy lock meanwhile.
            patch.setattr(
                "lucille_host.state.wait_for_lock", lambda _d: pytest.fail("a lock was taken")
            )
            ran = w.ok("--json", "roots", "add", "--suggest-alias", folder)
            text = w.ok("roots", "add", "--suggest-alias", folder)
        assert _tree(w.home) == before
        alias = "documents-2" if with_policy else "documents"
        assert ran.objects[0]["alias"] == alias and text.out == f"{alias}\n"


def test_roots_add_after_double_dash(
    tmp_path: Path,
    host_env: HostEnv,
    stack: contextlib.ExitStack,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = World(tmp_path, stack, capsys, monkeypatch)
    folder = w.folder("Documents")
    assert w.ok("roots", "add", "--", "docs", folder).out == "added docs\n"
    (added,) = assert_objects(w.ok("roots", "add", "--json", "--", "papers", folder))
    assert added == {"v": 1, "type": "root", "alias": "papers", "path": folder, "broad": False}
    # After --, a word that starts with - is an alias, which is refused as an alias.
    ran = w.run("--json", "roots", "add", "--", "-odd", folder)
    assert ran.rc == 1 and ran.objects[-1]["code"] == "bad_alias", ran.err
    # A missing path is still the parser's usage error, in its own words.
    ran = w.run("roots", "add", "docs")
    assert ran.rc == 2 and ran.out == ""
    assert ran.err.splitlines()[-1] == (
        "lucille-host roots add: error: the following arguments are required: path"
    )
    assert "usage: lucille-host roots add [-h] [--allow-home-or-root] alias path" in ran.err
    ran = w.run("roots", "add")
    assert ran.rc == 2
    assert ran.err.splitlines()[-1].endswith("the following arguments are required: alias, path")
    # --suggest-alias stands alone.
    for extra in (["docs"], ["docs", folder], ["--allow-home-or-root"]):
        ran = w.run("--json", "roots", "add", "--suggest-alias", folder, *extra)
        assert ran.rc == 2 and ran.objects == [
            {"v": 1, "type": "error", "code": "usage", "message": USAGE_MESSAGE}
        ]


def test_lists_by_alias_and_name_whatever_order_the_file_holds(
    tmp_path: Path,
    host_env: HostEnv,
    stack: contextlib.ExitStack,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A policy file written by hand keeps its own order, and so does one a pairing set aside as it
    # was. Each list is by alias, or by name, all the same.
    w = World(tmp_path, stack, capsys, monkeypatch)
    zeta, alpha = w.folder("zeta"), w.folder("alpha")
    sleep, describe = (OPERATIONS[name].definition_hash for name in ("host.sleep", "host.describe"))
    paths.ensure_dir(w.dirs.config)
    w.dirs.policy_file.write_text(
        "version = 1\nlog_results = false\n"
        f'[ops."host.sleep"]\ndefinition = "{sleep}"\n'
        f'[ops."host.describe"]\ndefinition = "{describe}"\n'
        f'[roots."zeta"]\npath = "{zeta}"\nbroad = false\n'
        f'[roots."alpha"]\npath = "{alpha}"\nbroad = false\n'
    )
    held = policy.load(w.dirs.policy_file)
    assert (list(held.enabled), list(held.roots)) == (
        ["host.sleep", "host.describe"],
        ["zeta", "alpha"],
    )
    for verb in ("list", "check"):
        objects = assert_objects(w.ok("--json", "roots", verb))
        assert [found["alias"] for found in objects] == ["alpha", "zeta"]
        text = w.ok("roots", verb).out.splitlines()
        assert [line.split()[0] for line in text] == ["alpha", "zeta"]
    (status,) = assert_objects(w.ok("--json", "status"))
    assert status["policy"]["folders"] == ["alpha", "zeta"]
    assert status["policy"]["enabled"] == ["host.describe", "host.sleep"]
    assert "folders: alpha, zeta\n" in w.ok("status").out
    w.dirs.previous_policy_file.write_bytes(w.dirs.policy_file.read_bytes())
    (previous,) = assert_objects(w.ok("--json", "policy", "previous"))
    assert previous["ops"] == ["host.describe", "host.sleep"]
    assert [found["alias"] for found in previous["folders"]] == ["alpha", "zeta"]
    assert w.ok("policy", "previous").out.splitlines()[1:] == [
        "operations:",
        "  host.describe",
        "  host.sleep",
        "folders:",
        f"  alpha  {alpha}",
        f"  zeta  {zeta}",
    ]


class _Unlisted:
    """What ``os.scandir`` gives for a folder that opens and cannot be listed: its first entry
    fails with ``raised``."""

    def __init__(self, raised: int) -> None:
        self.raised = raised

    def __enter__(self) -> _Unlisted:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def __iter__(self) -> _Unlisted:
        return self

    def __next__(self) -> Any:
        raise OSError(self.raised, os.strerror(self.raised))


def test_roots_check_with_no_folder_writes_nothing_and_exits_0(
    tmp_path: Path,
    host_env: HostEnv,
    stack: contextlib.ExitStack,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Empty standard output is no folder to check, not a failure (spec/CLI.md section 7).
    w = World(tmp_path, stack, capsys, monkeypatch)
    for argv in (["--json", "roots", "check"], ["roots", "check"]):
        ran = w.run(*argv)
        assert ran.rc == 0, ran.err
        assert ran.out == ""


def test_roots_check_maps_errno(
    tmp_path: Path,
    host_env: HostEnv,
    stack: contextlib.ExitStack,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = World(tmp_path, stack, capsys, monkeypatch)
    readable = w.folder("readable")
    Path(readable, "a.txt").write_text("a\n")
    empty = w.folder("empty")
    removed = w.folder("removed")
    replaced = w.folder("replaced")
    shut = w.folder("shut")
    for alias, folder in (
        ("readable", readable),
        ("empty", empty),
        ("removed", removed),
        ("replaced", replaced),
        ("shut", shut),
    ):
        w.ok("roots", "add", alias, folder)
    os.rmdir(removed)
    os.rmdir(replaced)
    Path(replaced).write_text("a file now\n")
    as_root = os.getuid() == 0
    if not as_root:
        os.chmod(shut, 0)
    try:
        if as_root:
            real = os.scandir

            def refusing(path: Any = ".") -> Any:
                if str(path) == shut:
                    raise PermissionError(errno.EACCES, "Permission denied", str(path))
                return real(path)

            monkeypatch.setattr("os.scandir", refusing)
        monkeypatch.setattr(
            "lucille_host.link.open_session",
            lambda *_, **__: pytest.fail("roots check opened a session"),
        )
        before = _tree(w.home)
        ran = w.ok("--json", "roots", "check")
        text = w.ok("roots", "check")
        assert _tree(w.home) == before
    finally:
        os.chmod(shut, 0o700)
    states = {found["alias"]: found["state"] for found in assert_objects(ran)}
    assert [found["alias"] for found in ran.objects] == sorted(states)
    assert states == {
        "empty": "ok",
        "readable": "ok",
        "removed": "missing",
        "replaced": "missing",
        "shut": "denied",
    }
    assert text.out.splitlines() == [
        "empty  ok",
        "readable  ok",
        "removed  missing",
        "replaced  missing",
        "shut  denied",
    ]
    # EPERM is macOS's privacy protection on macOS, and the folder's permissions on Linux; any
    # other error is named.
    for family, raised, expected in (
        ("macos", errno.EPERM, ("denied_by_macos", None)),
        ("linux", errno.EPERM, ("denied", None)),
        ("linux", errno.EIO, ("error", "EIO")),
        ("macos", errno.ESTALE, ("error", "ESTALE")),
    ):

        def failing(path: Any = ".", raised: int = raised) -> Any:
            raise OSError(raised, os.strerror(raised), str(path))

        with monkeypatch.context() as patch:
            patch.setattr("os.scandir", failing)
            assert policy.check_root(readable, family) == expected
        # A folder that opens, and whose first entry cannot be read, is judged the same.
        with monkeypatch.context() as patch:
            patch.setattr("os.scandir", lambda _path=".", raised=raised: _Unlisted(raised))
            assert policy.check_root(readable, family) == expected
    # Through the command line: the error's name, in the object and in the text.
    real_scandir = os.scandir

    def stale(path: Any = ".") -> Any:
        if str(path) == readable:
            raise OSError(errno.EIO, os.strerror(errno.EIO), str(path))
        return real_scandir(path)

    with monkeypatch.context() as patch:
        patch.setattr("os.scandir", stale)
        ran = w.ok("--json", "roots", "check", "readable")
        text = w.ok("roots", "check", "readable")
    assert assert_objects(ran) == [
        {
            "v": 1,
            "type": "root_check",
            "alias": "readable",
            "path": readable,
            "state": "error",
            "errno": "EIO",
        }
    ]
    assert text.out == "readable  error: EIO\n"
    # On macOS, EPERM is macOS's privacy protection, in the object and in the text.
    real_check = policy.check_root

    def refused(path: Any = ".") -> Any:
        if str(path) == readable:
            raise PermissionError(errno.EPERM, os.strerror(errno.EPERM), str(path))
        return real_scandir(path)

    with monkeypatch.context() as patch:
        patch.setattr("os.scandir", refused)
        patch.setattr(policy, "check_root", lambda path, _family: real_check(path, "macos"))
        ran = w.ok("--json", "roots", "check", "readable")
        text = w.ok("roots", "check", "readable")
    assert assert_objects(ran) == [
        {
            "v": 1,
            "type": "root_check",
            "alias": "readable",
            "path": readable,
            "state": "denied_by_macos",
        }
    ]
    assert text.out == "readable  denied_by_macos\n"
    (one,) = assert_objects(w.ok("--json", "roots", "check", "readable"))
    assert one == {
        "v": 1,
        "type": "root_check",
        "alias": "readable",
        "path": readable,
        "state": "ok",
    }
    ran = w.run("--json", "roots", "check", "nosuch")
    assert ran.rc == 1 and ran.objects[-1]["code"] == "no_such_alias"


# --- the contract's general rules -----------------------------------------------------------------


def test_json_flag_anywhere_before_double_dash(
    tmp_path: Path,
    host_env: HostEnv,
    stack: contextlib.ExitStack,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = World(tmp_path, stack, capsys, monkeypatch)
    _log(w)
    folder = w.folder("Documents")
    w.ok("roots", "add", "docs", folder)
    # Every verb and action, each once as it succeeds or fails here: the same exit and the same
    # objects wherever --json is.
    for line in (
        ("ops", "list"),
        ("ops", "enable", "host.describe"),
        ("ops", "disable", "host.nothing"),
        ("roots", "list"),
        ("roots", "check"),
        ("roots", "add", "--suggest-alias", folder),
        ("roots", "add", "docs", folder),
        ("roots", "remove", "nosuch"),
        ("pair", "status"),
        ("pair", "confirm"),
        ("pair", "reject"),
        ("pair", "--code-stdin"),
        ("status",),
        ("log", "--limit", "1"),
        ("forget",),
        ("policy", "previous"),
        ("pause",),
        ("resume",),
        ("kill",),
    ):
        verb, rest = line[0], list(line[1:])
        places = [
            ["--json", verb, *rest],
            [verb, "--json", *rest],
            [verb, *rest, "--json"],
        ]
        if verb in ("ops", "roots", "policy") and len(rest) > 1:
            places.append([verb, rest[0], "--json", *rest[1:]])
        ran = [w.run(*argv) for argv in places]
        assert len({(found.rc, found.out) for found in ran}) == 1, (line, ran)
        assert ran[0].rc in (0, 1) and assert_objects(ran[0]), (line, ran[0].err)
    for index, argv in enumerate(
        (
            ["--json", "roots", "add"],
            ["roots", "--json", "add"],
            ["roots", "add", "--json"],
        )
    ):
        (added,) = assert_objects(w.ok(*argv, "--", f"docs{index}", folder))
        assert added["alias"] == f"docs{index}"
    # After --, --json is an argument: refused as an alias, or one argument too many, in text.
    ran = w.run("roots", "add", "--", "--json", folder)
    assert ran.rc == 1 and ran.out == "" and "an alias is" in ran.err
    ran = w.run("roots", "remove", "--", "docs", "--json")
    assert ran.rc == 2 and ran.out == "" and "unrecognized arguments: --json" in ran.err


def test_version_json_either_order(capsys: pytest.CaptureFixture[str]) -> None:
    def run(*argv: str) -> Ran:
        try:
            rc = main(list(argv))
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 1
        out, err = capsys.readouterr()
        return Ran(rc, out, err)

    for argv in (["--json"], ["--json", "--version"], ["--version", "--json"]):
        ran = run(*argv)
        assert ran.rc == 0, argv
        assert assert_objects(ran) == [
            {"v": 1, "type": "version", "host_version": __version__, "contract": "1.0"}
        ]
    # --version ends the line at once, as before.
    for argv in (["--version", "ops"], ["--version", "bogus"]):
        assert run(*argv) == Ran(0, f"lucille-host {__version__}\n", "")


def test_json_usage_errors_exit_2(
    tmp_path: Path,
    host_env: HostEnv,
    stack: contextlib.ExitStack,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = World(tmp_path, stack, capsys, monkeypatch)
    usage = [{"v": 1, "type": "error", "code": "usage", "message": USAGE_MESSAGE}]
    for argv in (
        ["--json", "nosuch"],
        ["--json", "ops", "nosuch"],
        ["--json", "ops"],
        ["--json", "ops", "enable"],
        ["roots", "add", "docs", "--json"],
        ["--json", "log", "--limit", "many"],
        ["--json", "--no-such-flag"],
        ["--json", "pair", "status", "extra"],
    ):
        ran = w.run(*argv)
        text = w.run(*_without_json(argv))
        assert (ran.rc, text.rc) == (2, 2), argv
        assert ran.objects == usage, argv
        assert text.out == "" and ran.err == text.err and "error:" in ran.err, argv
    # Forms version 1 does not have: run has no JSON form, and pair reads its code only with
    # --code-stdin.
    for argv in (["--json", "run"], ["run", "--json"], ["pair", "--json"]):
        ran = w.run(*argv)
        assert ran.rc == 2 and ran.objects == usage, argv
        assert ran.err.startswith("usage: lucille-host"), argv
    # Help is for people, on standard output, also with --json.
    ran = w.run("--json", "--help")
    assert ran.rc == 0 and ran.out.startswith("usage: lucille-host")


# What --json calls each failure that carries no code of its own, the first class that matches:
# spec/CLI.md section 10, written out here, so that a wrong entry in the command's own table is
# seen.
FAILURE_CODES = [
    (_switchboard.PairFailed, "pair_unusable"),
    (_switchboard.HubError, "hub_error"),
    (store.InsecureKeyring, "insecure_keyring"),
    (state.Refused, "startup_refused"),
    (state.StateError, "state_invalid"),
    (state.Unprepared, "runtime_folder"),
    (paths.UnsupportedPlatform, "unsupported_platform"),
    (policy.PolicyError, "policy_invalid"),
    (store.StoreError, "store_error"),
    (OSError, "os_error"),
]


def _instance(kind: type[BaseException]) -> BaseException:
    for args in (("a sentence for people",), ("error", "a sentence for people"), (5, "a sentence")):
        try:
            return kind(*args)
        except TypeError:
            continue
    raise AssertionError(kind)


def test_every_failure_has_a_code(
    tmp_path: Path,
    host_env: HostEnv,
    stack: contextlib.ExitStack,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ast

    from lucille_host import cli

    enum = error_codes()
    source = REPO_ROOT / "src" / "lucille_host"
    literal: list[tuple[str, str]] = []
    for name in ("cli.py", "link.py", "loop.py"):
        tree = ast.parse((source / name).read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            called = node.func.attr if isinstance(node.func, ast.Attribute) else None
            called = called or (node.func.id if isinstance(node.func, ast.Name) else None)
            if called not in ("_Refused", "Stopped", "Failed"):
                continue
            keyword = {k.arg: k.value for k in node.keywords}
            given = keyword.get("code")
            if given is None and called != "Failed" and node.args:
                given = node.args[0]
            if called == "Failed" and given is None:
                continue
            if isinstance(given, ast.Name) and given.id == "word":
                continue  # a word outcome() gave, checked below
            assert isinstance(given, ast.Constant), f"{name}:{node.lineno}: no literal code"
            literal.append((f"{name}:{node.lineno}", given.value))
    # `rejected` is the interactive `n`, which `pair --code-stdin` never asks.
    assert [(where, code) for where, code in literal if code not in enum | {"rejected"}] == []
    assert len(literal) > 40
    assert {link.outcome(word) for word in ("aborted", "expired", "ended", "unknown")} <= enum
    # Every other failure main catches has its code: the table below, which spec/CLI.md section
    # 10 gives, in the order the classes are tried.
    assert list(cli.FAILURE_CODES) == FAILURE_CODES
    raised_as: list[tuple[BaseException, str, str]] = [
        (_instance(kind), code, "") for kind, code in FAILURE_CODES
    ]
    # Each with the line standard error shows, and the message it gives the object.
    raised_as += [
        (
            _switchboard.PairFailed("aborted_here", "a sentence for people"),
            "pair_unusable",
            "error: the pair cannot be used here (aborted_here)",
        ),
        (
            _switchboard.HubError("the hub answered 503"),
            "hub_error",
            "error: the hub answered 503; try again",
        ),
        (
            PermissionError(errno.EACCES, "Permission denied", "/a/file"),
            "os_error",
            "lucille-host: Permission denied (/a/file)",
        ),
        # A store error with no errno, as the store raises when the keyring cannot be read.
        (
            store.StoreError("the keyring could not be read (KeyringError)"),
            "store_error",
            "lucille-host: the keyring could not be read (KeyringError)",
        ),
        (
            store.StoreError(errno.EIO, "the keyring item could not be read"),
            "store_error",
            "lucille-host: the keyring item could not be read",
        ),
    ]
    for index, (raised, code, line) in enumerate(raised_as):
        assert code in enum
        w = World(tmp_path / f"failure-{index}", stack, capsys, monkeypatch)

        def failing(*_: Any, raised: BaseException = raised) -> Any:
            raise raised

        monkeypatch.setitem(cli.VERBS, "pause", failing)
        ran = w.run("--json", "pause")
        assert ran.rc == 1, raised
        assert ran.objects[-1]["code"] == code, raised
        assert ran.err and not ran.err.startswith("{"), raised
        if line:
            assert ran.err == line + "\n", raised
            assert line.endswith(": " + ran.objects[-1]["message"]), raised
    # An unexpected error: the object, then the traceback as before.
    w = World(tmp_path / "internal", stack, capsys, monkeypatch)
    monkeypatch.setitem(cli.VERBS, "pause", lambda *_: 1 / 0)
    capsys.readouterr()
    with pytest.raises(ZeroDivisionError):
        main(["--json", "pause"])
    out = capsys.readouterr().out
    assert [json.loads(line)["code"] for line in out.splitlines()] == ["internal"]


def error_codes() -> set[str]:
    schema = spec.load_cli_schema("error")
    codes: set[str] = set()
    for branch in schema["$defs"].values():
        code = branch.get("properties", {}).get("code", {})
        codes |= set(code.get("enum", [])) | ({code["const"]} if "const" in code else set())
    return codes


def test_cli_md_names_every_type_and_code() -> None:
    text = _cli_md()
    headings = re.findall(r"^### `([a-z_]+)`", text, re.MULTILINE)
    assert [*headings, "error"] == list(spec.CLI_TYPES)
    assert "\n## 5. The error object\n" in text
    section = text[text.index("## 10. Error codes") : text.index("## 11. Golden fixtures")]
    listed = re.findall(r"^\| `([a-z_]+)` \|", section, re.MULTILINE)
    assert len(listed) == len(set(listed))
    assert set(listed) == error_codes()
    for verb, codes in verb_codes().items():
        assert codes <= error_codes(), verb
    assert general_codes() <= error_codes()


# --- failures, as JSON and as text ----------------------------------------------------------------


def _too_many(w: World) -> None:
    roots = {f"r{index}": w.folder(f"r{index}") for index in range(policy.MAX_ROOTS)}
    w.policy_file({}, roots)


def _lock_fails(w: World) -> None:
    def failing(_descriptor: int) -> None:
        raise OSError(errno.ENOLCK, "No locks available")

    w.monkeypatch.setattr("lucille_host.state.wait_for_lock", failing)


def _pair_lock_fails(w: World) -> None:
    real = os.lockf

    def failing(descriptor: int, command: int, length: int) -> None:
        if command == os.F_TLOCK:
            raise OSError(errno.ENOLCK, "No locks available")
        real(descriptor, command, length)

    w.monkeypatch.setattr("os.lockf", failing)


def _busy(w: World) -> None:
    w.stack.enter_context(lock_held(paths.ensure_dir(w.dirs.state) / ".pair.lock"))


def _bad_policy(w: World) -> None:
    paths.ensure_dir(w.dirs.config)
    w.dirs.policy_file.write_text("x = 1\n")


def _bad_previous(w: World) -> None:
    paths.ensure_dir(w.dirs.config)
    w.dirs.previous_policy_file.write_text("version = 2\n")


def _a_file(w: World) -> None:
    Path(w.path("/home/me/file")).write_text("x\n")


def _state_made(w: World) -> None:
    paths.ensure_dir(w.dirs.state)


def _not_a_code(w: World) -> None:
    w.stdin = "not a code\n"


def _held(w: World) -> None:
    w.paired()
    w.stdin = w.code() + "\n"


def _recoded(w: World, **changes: Any) -> None:
    code = w.code()
    w.stdin = dataclasses.replace(pairing.PairingCode.decode(code), **changes).encode() + "\n"


def _no_gate(w: World) -> None:
    w.paired()
    w.dirs.gate_file.unlink()


def _expired(w: World) -> None:
    w.paired()
    w.host.h.advance(86400 + 1)


def _ended(w: World) -> None:
    w.paired()
    assert w.lucille.forget().peer_told


def _bad_rooms(w: World) -> None:
    paths.ensure_dir(w.dirs.state)
    rooms.save(
        w.dirs.rooms_file,
        rooms.Record(pair="pair-x", made=1.0, held={"room-1": rooms.Held("w", 1.0, "no idea")}),
    )


def _accept_fails(raised: BaseException) -> Callable[[World], None]:
    """A code to answer, and the pairing library failing the answer with ``raised``."""

    def setup(w: World) -> None:
        def failing(_link: Any, _code: Any) -> str:
            raise raised

        w.monkeypatch.setattr(_switchboard.Link, "accept", failing)
        w.stdin = w.code() + "\n"

    return setup


def _resolve_fails(w: World) -> None:
    w.interrupted()

    def failing(_link: Any, _name: str) -> Any:
        raise _switchboard.PairFailed("aborted_here", "the library refused the pair")

    w.monkeypatch.setattr(_switchboard.Link, "resolve", failing)


def _held_in_the_file_store(w: World) -> None:
    w.lucille = w.host.lucille()
    w.host.paired(w.lucille, "--store-file")
    # The profile set back to the keyring, which holds nothing: the pair is in the other store.
    state.save_host_config(w.dirs, state.HostConfig())
    w.stdin = w.code() + "\n"


def _two_pairs(w: World) -> None:
    w.monkeypatch.setattr(link.Session, "held", lambda _session: ["pair-a", "pair-b"])


# The word the last line on standard error starts with, without --json: a pairing that stopped,
# or a refusal of the pairing verbs, prints its state word first; every other failure the
# program's name.
TEXT_WORDS = {
    "bad_code": "refused",
    "wrong_hub": "refused",
    "code_has_token": "refused",
    "already_confirmed": "refused",
    "no_gate": "refused",
    "none": "none",
    "waiting": "waiting",
    "sas_ready": "sas_ready",
    "expired": "expired",
    "ended": "ended",
    "hub_error": "error",
    "answer_failed": "error",
    "pair_unusable": "error",
    "several_pairs": "error",
}

# (case, needs the hub, set-up, argv, the code it fails with)
FAILURES: list[tuple[str, bool, Callable[[World], None] | None, list[str], str]] = [
    ("bad-profile", False, None, ["--profile", "Bad!", "--json", "pause"], "bad_profile"),
    ("bad-profile-digit", False, None, ["--profile", "2nd", "--json", "pause"], "bad_profile"),
    ("policy-invalid", False, _bad_policy, ["--json", "ops", "list"], "policy_invalid"),
    (
        "policy-lock",
        False,
        _lock_fails,
        ["--json", "ops", "disable", "host.describe"],
        "policy_lock_failed",
    ),
    (
        "unavailable",
        False,
        lambda w: w.every_executable(missing=("host.sleep",)),
        ["--json", "ops", "enable", "host.sleep"],
        "unavailable",
    ),
    ("not-enabled", False, None, ["--json", "ops", "disable", "host.sleep"], "not_enabled"),
    ("bad-alias", False, None, ["--json", "roots", "add", "Docs", "/home/me"], "bad_alias"),
    ("bad-alias-long", False, None, ["--json", "roots", "add", "a" * 33, "/home/me"], "bad_alias"),
    (
        "alias-taken",
        False,
        _docs_added,
        ["--json", "roots", "add", "docs", "/home/me/Documents"],
        "alias_taken",
    ),
    (
        "too-many",
        False,
        _too_many,
        ["--json", "roots", "add", "more", "/home/me"],
        "too_many_roots",
    ),
    (
        "not-a-folder",
        False,
        _a_file,
        ["--json", "roots", "add", "file", "/home/me/file"],
        "not_a_folder",
    ),
    ("own-folder", False, _state_made, ["--json", "roots", "add", "own", "{state}"], "own_folder"),
    ("no-such-alias", False, None, ["--json", "roots", "remove", "nosuch"], "no_such_alias"),
    (
        "previous-invalid",
        False,
        _bad_previous,
        ["--json", "policy", "previous"],
        "previous_invalid",
    ),
    ("bad-limit", False, None, ["--json", "log", "--limit", "0"], "bad_limit"),
    (
        "bad-options",
        False,
        None,
        ["--json", "pair", "status", "--hub", "http://127.0.0.1:8765"],
        "bad_options",
    ),
    ("code-in-argv", False, None, ["--json", "pair", "--code-stdin", "swbp1_x"], "code_in_argv"),
    (
        "bad-hub",
        False,
        None,
        ["--json", "pair", "--code-stdin", "--hub", "https://hub.example"],
        "bad_hub",
    ),
    ("busy", False, _busy, ["--json", "pair", "--code-stdin"], "busy"),
    ("lock-failed", False, _pair_lock_fails, ["--json", "pair", "--code-stdin"], "lock_failed"),
    ("no-code", False, None, ["--json", "pair", "--code-stdin"], "no_code"),
    ("bad-code", False, _not_a_code, ["--json", "pair", "--code-stdin"], "bad_code"),
    ("none-confirm", False, None, ["--json", "pair", "confirm"], "none"),
    ("none-reject", False, None, ["--json", "pair", "reject"], "none"),
    (
        "startup-refused",
        False,
        lambda w: w.monkeypatch.setenv("SWITCHBOARD_URL", "https://hub.example"),
        ["--json", "status"],
        "startup_refused",
    ),
    (
        "insecure-keyring",
        False,
        lambda w: w.monkeypatch.setattr("keyring.core._keyring_backend", FailKeyring()),
        ["--json", "pair", "status"],
        "insecure_keyring",
    ),
    ("state-invalid", False, _bad_rooms, ["--json", "status"], "state_invalid"),
    (
        "unsupported-platform",
        False,
        lambda w: w.monkeypatch.setattr("sys.platform", "win32"),
        ["--json", "pause"],
        "unsupported_platform",
    ),
    ("held", True, _held, ["--json", "pair", "--code-stdin"], "held"),
    (
        "wrong-hub",
        True,
        lambda w: _recoded(w, url="https://hub.example"),
        ["--json", "pair", "--code-stdin"],
        "wrong_hub",
    ),
    (
        "code-has-token",
        True,
        lambda w: _recoded(w, token="sb_a_hub_token"),
        ["--json", "pair", "--code-stdin"],
        "code_has_token",
    ),
    ("waiting", True, lambda w: w.interrupted(), ["--json", "pair", "confirm"], "waiting"),
    ("sas-ready", True, _sas_pending, ["--json", "pair", "confirm"], "sas_ready"),
    (
        "already-confirmed",
        True,
        lambda w: w.paired(confirm=True),
        ["--json", "pair", "reject"],
        "already_confirmed",
    ),
    (
        "wrong-hub-loopback",
        True,
        lambda w: _recoded(w, url="http://127.0.0.1:8765"),
        ["--json", "pair", "--code-stdin"],
        "wrong_hub",
    ),
    (
        "hub-error",
        True,
        _accept_fails(_switchboard.HubError("the hub is out of reach")),
        ["--json", "pair", "--code-stdin"],
        "hub_error",
    ),
    (
        "answer-failed",
        True,
        _accept_fails(_switchboard.PairFailed("aborted_here", "refused")),
        ["--json", "pair", "--code-stdin"],
        "answer_failed",
    ),
    ("pair-unusable", True, _resolve_fails, ["--json", "pair", "status"], "pair_unusable"),
    (
        "held-in-the-other-store",
        True,
        _held_in_the_file_store,
        ["--json", "pair", "--code-stdin", "--store-file"],
        "held",
    ),
    ("several-pairs-forget", False, _two_pairs, ["--json", "forget"], "several_pairs"),
    ("several-pairs-status", False, _two_pairs, ["--json", "pair", "status"], "several_pairs"),
    ("several-pairs-confirm", False, _two_pairs, ["--json", "pair", "confirm"], "several_pairs"),
    ("several-pairs-reject", False, _two_pairs, ["--json", "pair", "reject"], "several_pairs"),
    ("no-gate", True, _no_gate, ["--json", "pair", "status"], "no_gate"),
    ("expired", True, _expired, ["--json", "pair", "status"], "expired"),
    ("ended", True, _ended, ["--json", "pair", "status"], "ended"),
]
if sys.platform.startswith("linux"):
    FAILURES.append(
        ("on-proc", False, None, ["--json", "roots", "add", "proc", "/proc"], "on_proc")
    )


@pytest.mark.parametrize(
    ("with_hub", "setup", "argv", "code"),
    [row[1:] for row in FAILURES],
    ids=[row[0] for row in FAILURES],
)
def test_json_failure_ends_with_error_object(
    with_hub: bool,
    setup: Callable[[World], None] | None,
    argv: list[str],
    code: str,
    tmp_path: Path,
    host_env: HostEnv,
    stack: contextlib.ExitStack,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs = []
    for where, line in (("json", argv), ("text", _without_json(argv))):
        w = World(tmp_path / where, stack, capsys, monkeypatch, with_hub=with_hub)
        if setup is not None:
            setup(w)
        runs.append((w, w.run(*_real_argv(line, w), stdin=w.stdin)))
    (json_world, ran), (text_world, text) = runs
    assert ran.rc == 1, ran.err
    objects = assert_objects(ran)
    assert objects[-1]["type"] == "error" and objects[-1]["code"] == code, ran.out
    assert all(found["type"] != "error" for found in objects[:-1])
    assert_listed(argv, code)
    # The message is the sentence standard error shows, without its prefix.
    assert objects[-1]["message"] in ran.err
    if code == "several_pairs":
        assert ran.err == (
            "error: this profile holds 2 pairs (pair-a, pair-b), and the host keeps one\n"
        )
    # A name refused for any of its rules, its length included, is told all of them, as spec/CLI.md
    # section 10 gives them.
    rule = "lowercase letters, digits, '-' and '_', starting with a letter, at most 32"
    if code == "bad_profile":
        assert objects[-1]["message"] == f"a profile name is {rule}"
    if code == "bad_alias":
        assert objects[-1]["message"] == f"an alias is {rule}"
    assert text.rc == 1
    word = TEXT_WORDS.get(code, "lucille-host")
    assert text.err.splitlines()[-1].startswith(f"{word}: "), text.err
    assert _sampled(text.err, text_world) == _sampled(ran.err, json_world)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="APFS refuses such a name")
def test_json_strings_are_valid_unicode(
    tmp_path: Path,
    host_env: HostEnv,
    stack: contextlib.ExitStack,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Run as a process of its own, so that its streams are the ones a shell reads.
    w = World(tmp_path, stack, capsys, monkeypatch)
    name = os.fsdecode(b"caf\xe9")
    assert "\udce9" in name
    absent = str(w.home / name)
    environment = {**os.environ, "HOME": str(w.home)}

    def run(*argv: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [sys.executable, "-m", "lucille_host", *argv],
            capture_output=True,
            env=environment,
            check=False,
        )

    done = run("--json", "roots", "add", "docs", absent)
    assert done.returncode == 1 and done.stdout.isascii()
    (failed,) = assert_objects(Ran(1, done.stdout.decode("ascii"), ""))
    assert failed["code"] == "not_a_folder"
    assert failed["message"] == f"{w.home}/caf\ufffd is not a folder"
    assert b"\\ufffd" in done.stdout and b"\\udce9" not in done.stdout
    os.mkdir(os.fsencode(absent))
    done = run("--json", "roots", "add", "docs", absent)
    assert done.returncode == 1 and done.stdout.isascii()
    assert assert_objects(Ran(1, done.stdout.decode("ascii"), ""))[-1]["code"] == "unstorable_path"
    done = run("--json", "roots", "add", "--suggest-alias", absent)
    assert done.returncode == 0
    assert json.loads(done.stdout)["alias"] == "caf"


def test_status_json_reports_an_unreadable_store(
    tmp_path: Path,
    host_env: HostEnv,
    stack: contextlib.ExitStack,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = World(tmp_path, stack, capsys, monkeypatch)
    w.ok("pause")

    def broken(_link: Any) -> list[str]:
        raise store.StoreError(errno.EIO, "the keyring item could not be read")

    monkeypatch.setattr(_switchboard.Link, "names", broken)
    for argv in (["--json", "status"], ["status"]):
        ran = w.ok(*argv)
        if "--json" in argv:
            (status,) = assert_objects(ran)
            assert status["pair"] == {
                "state": "unreadable",
                "code": "store_error",
                "message": "the keyring item could not be read",
            }
            assert status["paused"] is True and status["policy"]["state"] == "ok"
        else:
            assert "pair: unreadable: the keyring item could not be read" in ran.out.splitlines()
            assert "controls: paused: nothing starts until `lucille-host resume`" in ran.out


def test_status_json_reports_a_pair_it_cannot_use(
    tmp_path: Path,
    host_env: HostEnv,
    stack: contextlib.ExitStack,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = World(tmp_path, stack, capsys, monkeypatch)
    # The store opened, then the pair could not be read from it, or the library refused it.
    for raised, code, message in (
        (
            store.StoreError(errno.EIO, "the keyring item could not be read"),
            "store_error",
            "the keyring item could not be read",
        ),
        (
            _switchboard.PairFailed("aborted_here", "refused"),
            "pair_unusable",
            "the pair cannot be used here (aborted_here)",
        ),
    ):

        def failing(_session: Any, raised: BaseException = raised) -> Any:
            raise raised

        with monkeypatch.context() as patch:
            patch.setattr(link.Session, "pair_view", failing)
            (status,) = assert_objects(w.ok("--json", "status"))
            text = w.ok("status")
        assert status["pair"] == {"state": "unreadable", "code": code, "message": message}
        assert f"pair: unreadable: {message}" in text.out.splitlines()
    # A profile that holds two pairs: the host keeps one, and says so in the pair.
    _two_pairs(w)
    message = "this profile holds 2 pairs (pair-a, pair-b), and the host keeps one"
    (status,) = assert_objects(w.ok("--json", "status"))
    assert status["pair"] == {"state": "several_pairs", "message": message}
    assert f"pair: error: {message}" in w.ok("status").out.splitlines()


def test_status_json_reports_a_keyring_that_cannot_be_read(
    tmp_path: Path,
    host_env: HostEnv,
    stack: contextlib.ExitStack,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The keyring refuses the read, as a locked keychain does. The store's error then has a
    # sentence and no errno: `status` reports it in its pair, and a verb that needs the pair fails
    # with it.
    w = World(tmp_path, stack, capsys, monkeypatch)
    w.ok("pause")

    def locked(*_: Any, **__: Any) -> Any:
        raise KeyringError("the keychain is locked")

    monkeypatch.setattr(w.keyring, "get_password", locked)
    message = "the keyring could not be read (KeyringError)"
    (status,) = assert_objects(w.ok("--json", "status"))
    assert status["pair"] == {"state": "unreadable", "code": "store_error", "message": message}
    assert status["paused"] is True
    assert f"pair: unreadable: {message}" in w.ok("status").out.splitlines()
    ran, text = w.run("--json", "pair", "status"), w.run("pair", "status")
    assert (ran.rc, text.rc) == (1, 1)
    assert assert_objects(ran) == [
        {"v": 1, "type": "error", "code": "store_error", "message": message}
    ]
    assert ran.err == text.err == f"lucille-host: {message}\n"


# Nested past what Python's parsers follow, on 3.11 and on 3.13: TOML from about 500 arrays, JSON
# from about 1000 on 3.11 and about 10000 on 3.13.
_DEEP_TOML = "version = 1\nx = " + "[" * 5000 + "]" * 5000 + "\n"
_DEEP_JSON = "[" * 100000 + "]" * 100000


def test_a_file_nested_too_deep_is_an_invalid_one(
    tmp_path: Path,
    host_env: HostEnv,
    stack: contextlib.ExitStack,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A file of the host's own nested too deep to parse is invalid as any other is: the parser's
    # RecursionError is never an `internal` error.
    w = World(tmp_path, stack, capsys, monkeypatch, with_hub=True)
    paths.ensure_dir(w.dirs.config)
    w.dirs.policy_file.write_text(_DEEP_TOML)
    said = f"{w.dirs.policy_file} nests too deep to be read"
    for argv in (["--json", "ops", "list"], ["--json", "roots", "list"]):
        ran = w.run(*argv)
        assert ran.rc == 1 and assert_objects(ran) == [
            {
                "v": 1,
                "type": "error",
                "code": "policy_invalid",
                "message": f"the policy file {w.dirs.policy_file} is invalid: {said}",
            }
        ], argv
    (status,) = assert_objects(w.ok("--json", "status"))
    assert status["policy"] == {"state": "invalid", "error": said}
    assert status["pair"] == {"state": "none"}
    # `pair` sets such a policy aside, as it does any invalid one, and the pairing goes ahead.
    code = w.code()
    with collecting(w.lucille):
        ran = w.ok("--json", "pair", "--code-stdin", stdin=code + "\n")
    found = assert_objects(ran)
    assert [obj["type"] for obj in found[:2]] == ["pair_code", "policy_reset"]
    assert found[-1]["state"] == "sas_pending"
    assert policy.load(w.dirs.policy_file) == policy.fresh()
    # Each state file of the host's own, with the pair held: `status` stops with state_invalid.
    for path in (w.dirs.host_file, w.dirs.gate_file, w.dirs.rooms_file, w.dirs.status_file):
        before = path.read_bytes() if path.exists() else None
        path.write_text(_DEEP_JSON)
        ran = w.run("--json", "status")
        assert ran.rc == 1 and assert_objects(ran) == [
            {
                "v": 1,
                "type": "error",
                "code": "state_invalid",
                "message": f"{path} nests too deep to be read",
            }
        ], path
        if before is None:
            path.unlink()
        else:
            path.write_bytes(before)
    assert_objects(w.ok("--json", "status"))


def test_status_reports_a_store_nested_too_deep_as_unreadable(
    tmp_path: Path,
    host_env: HostEnv,
    stack: contextlib.ExitStack,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The keyring item, then the file store's record of the pair, nested too deep to parse:
    # `status` says the pair is unreadable and fills in every other field; `pair status` fails
    # with store_error.
    w = World(tmp_path, stack, capsys, monkeypatch, with_hub=True)
    w.ok("pause")
    w.keyring.set_password(store.SERVICE, "default", _DEEP_JSON)
    message = (
        f"the keyring item {store.SERVICE}/default is not one this version wrote; it may hold the "
        "only copy of the pair secret, so it is left as it is"
    )
    (status,) = assert_objects(w.ok("--json", "status"))
    assert status["pair"] == {"state": "unreadable", "code": "store_error", "message": message}
    assert status["paused"] is True and status["policy"]["state"] == "ok"
    w.keyring.delete_password(store.SERVICE, "default")
    w.lucille = w.host.lucille()
    w.host.paired(w.lucille, "--store-file")
    record = w.dirs.store_files / f"{w.lucille.name}.json"
    assert record.is_file()
    record.write_text(_DEEP_JSON)
    message = "the file store could not be used (a file in it nests too deep to be read)"
    (status,) = assert_objects(w.ok("--json", "status"))
    assert status["pair"] == {"state": "unreadable", "code": "store_error", "message": message}
    assert status["paused"] is True and status["store"] == "file"
    ran = w.run("--json", "pair", "status")
    assert ran.rc == 1 and assert_objects(ran) == [
        {"v": 1, "type": "error", "code": "store_error", "message": message}
    ]


def _run_record(**changes: Any) -> dict[str, Any]:
    return {
        "v": 1,
        "running": True,
        "pid": 4242,
        "written_at": time.time(),
        "last_contact": time.time(),
        "skew": 1.5,
        "skewed": False,
        "reading": [],
        "read_stuck": [],
        **changes,
    }


def test_status_refuses_values_no_record_of_the_host_holds(
    tmp_path: Path,
    host_env: HostEnv,
    stack: contextlib.ExitStack,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A value the host never writes in a file of its own stops `status`, as any other such file
    # does: NaN or an infinity would make a line that is not JSON, a pid or a time out of range an
    # object its schema refuses, or an error that is not state_invalid.
    w = World(tmp_path, stack, capsys, monkeypatch)
    paths.ensure_dir(w.dirs.state)
    nan, inf = float("nan"), float("inf")
    huge = 10.0**30
    # The first second past 9999-12-31T23:59:59Z, written out so that the test does not move
    # with the host's own limit.
    past_9999 = 253402300800
    bad: list[tuple[str, Any]] = [
        ("skew", nan),
        ("skew", inf),
        ("skew", -inf),
        ("skew", huge),
        ("skew", past_9999),
        ("skew", True),
        ("skew", "1.5"),
        ("pid", 0),
        ("pid", -1),
        ("pid", True),
        ("pid", 2**31),
        ("pid", 1.5),
        ("written_at", nan),
        ("written_at", inf),
        ("written_at", -1),
        ("written_at", huge),
        ("written_at", past_9999),
        ("written_at", True),
        ("written_at", "1800000000"),
        ("last_contact", nan),
        ("last_contact", -inf),
        ("last_contact", -1),
        ("last_contact", huge),
        ("last_contact", past_9999),
        ("last_contact", "1800000000"),
    ]
    for key, value in bad:
        w.dirs.status_file.write_text(json.dumps(_run_record(**{key: value})))
        ran, text = w.run("--json", "status"), w.run("status")
        assert (ran.rc, text.rc) == (1, 1), (key, value, ran.out)
        assert [found["code"] for found in assert_objects(ran)] == ["state_invalid"], (key, value)
        assert ran.err == text.err and ran.err.endswith("is not a lucille-host run record\n")
    # The edges of each range are values `run` may write.
    edges: list[tuple[str, Any]] = [
        ("pid", 1),
        ("pid", 2**31 - 1),
        ("written_at", 0),
        ("written_at", past_9999 - 1),
        ("last_contact", past_9999 - 1),
        ("skew", -86400.0),
        ("skew", past_9999 - 1),
        ("skew", 1 - past_9999),
    ]
    for key, value in edges:
        w.dirs.status_file.write_text(json.dumps(_run_record(**{key: value})))
        assert_objects(w.ok("--json", "status"))
    # The times of the room record and the gate's record, the same.
    record = rooms.Record(
        pair="pair-x",
        made=1.0,
        offers={"offer-1": 1.0},
        held={"room-1": rooms.Held("workspace-1", 1.0)},
        left={"workspace-0": rooms.Left("room-0", rooms.REPLACED, 1.0)},
    )
    rooms.save(w.dirs.rooms_file, record)
    assert_objects(w.ok("--json", "status"))
    good = json.loads(w.dirs.rooms_file.read_text())
    places: list[Callable[[dict[str, Any], Any], None]] = [
        lambda data, value: data.update(made=value),
        lambda data, value: data["offers"].update({"offer-1": value}),
        lambda data, value: data["held"]["room-1"].update(accepted_at=value),
        lambda data, value: data["left"]["workspace-0"].update(at=value),
    ]
    for index, place in enumerate(places):
        for value in (nan, inf, -1, huge):
            data = json.loads(json.dumps(good))
            place(data, value)
            w.dirs.rooms_file.write_text(json.dumps(data))
            ran = w.run("--json", "status")
            assert ran.rc == 1, (index, value)
            assert [found["code"] for found in assert_objects(ran)] == ["state_invalid"]
            assert "is not a lucille-host room record" in ran.err
    found = gate.Gate(pair="pair-x", hub=_switchboard.LUCILLE_HUB, created_at=1.0)
    found = gate.shown(found, 2.0)
    gate.save(w.dirs.gate_file, found)
    assert gate.load(w.dirs.gate_file) == found
    for key in ("created_at", "shown_at", "deadline"):
        for value in (nan, inf, -1, huge):
            data = json.loads(w.dirs.gate_file.read_text())
            data[key] = value
            bad_gate = w.dirs.state / "bad-gate.json"
            bad_gate.write_text(json.dumps(data))
            with pytest.raises(state.StateError, match="is not a lucille-host gate record"):
                gate.load(bad_gate)


def test_json_never_writes_a_value_that_is_not_json(
    tmp_path: Path,
    host_env: HostEnv,
    stack: contextlib.ExitStack,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lucille_host import cli

    w = World(tmp_path, stack, capsys, monkeypatch)
    monkeypatch.setitem(
        cli.VERBS, "pause", lambda _args, _dirs, out: out.obj("controls", paused=float("nan"))
    )
    w.run("pause")
    capsys.readouterr()
    # A bug that hands --json NaN is an internal error, and NaN is never written.
    with pytest.raises(ValueError, match="not JSON compliant"):
        main(["--json", "pause"])
    out = capsys.readouterr().out
    assert [json.loads(line) for line in out.splitlines()] == [
        {"v": 1, "type": "error", "code": "internal", "message": INTERNAL_MESSAGE}
    ]


def test_roots_list_and_check_take_no_lock_and_write_nothing(
    tmp_path: Path,
    host_env: HostEnv,
    stack: contextlib.ExitStack,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = World(tmp_path, stack, capsys, monkeypatch)
    w.folder("Documents")
    for with_policy in (False, True):
        if with_policy:
            w.ok("roots", "add", "docs", w.path("/home/me/Documents"))
        before = _tree(w.home)
        with monkeypatch.context() as patch:
            # A `roots add` holding the policy lock on a stalled folder does not hold them up.
            patch.setattr(
                "lucille_host.state.wait_for_lock", lambda _d: pytest.fail("a lock was taken")
            )
            for argv in (["roots", "list"], ["roots", "check"]):
                assert_objects(w.ok("--json", *argv))
                w.ok(*argv)
        assert _tree(w.home) == before


def test_broad_folder_says_which_kind(
    tmp_path: Path,
    host_env: HostEnv,
    stack: contextlib.ExitStack,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = World(tmp_path, stack, capsys, monkeypatch)
    spelled = tmp_path / "home-again"
    spelled.symlink_to(w.home)
    # `/` spelled another way is `/` too: through a link, or with the two `/` a path may start with.
    top = tmp_path / "top"
    top.symlink_to("/")
    cases = [
        ("/", "root"),
        (str(top), "root"),
        ("//", "root"),
        (str(w.home), "home"),
        (str(spelled), "home"),
        (str(w.home.parent), "holds_home"),
    ]
    # Where the filesystem ignores case (macOS by default), the home folder spelled in capitals,
    # which its real path does not change.
    capitals = w.home.with_name(w.home.name.upper())
    if capitals.exists():
        cases.append((str(capitals), "home"))
    for path, kind in cases:
        ran = w.run("--json", "roots", "add", "everything", path)
        assert ran.rc == 1, (path, ran.err)
        (failed,) = assert_objects(ran)
        assert (failed["code"], failed["kind"]) == ("broad_folder", kind), path


def test_broad_folder_added_with_the_flag_says_so(
    tmp_path: Path,
    host_env: HostEnv,
    stack: contextlib.ExitStack,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The text never says a folder is broad: only `broad` does, in `roots add` and `roots list`.
    w = World(tmp_path, stack, capsys, monkeypatch)
    home = str(w.home)
    ran = w.ok("--json", "roots", "add", "--allow-home-or-root", "--", "home", home)
    assert assert_objects(ran) == [
        {"v": 1, "type": "root", "alias": "home", "path": home, "broad": True}
    ]
    docs = w.folder("Documents")
    w.ok("roots", "add", "docs", docs)
    assert assert_objects(w.ok("--json", "roots", "list")) == [
        {"v": 1, "type": "root", "alias": "docs", "path": docs, "broad": False},
        {"v": 1, "type": "root", "alias": "home", "path": home, "broad": True},
    ]
    assert w.ok("roots", "list").out == f"docs  {docs}\nhome  {home}\n"


# The letters from U+00C0 to U+017F that do not decompose into a plain letter and marks, and the
# two signs there (U+00D7 and U+00F7), as spec/CLI.md section 9.1 folds them: Ae, Eth, Oslash,
# Thorn, sharp s, D and H with stroke, dotless i, IJ, kra, L with middle dot and with stroke,
# apostrophe n, Eng, OE, T with stroke and long s.
UNDECOMPOSED = {
    "Æ": "ae",
    "æ": "ae",
    "Ð": "d",
    "ð": "d",
    "×": "-",
    "÷": "-",
    "Ø": "o",
    "ø": "o",
    "Þ": "th",
    "þ": "th",
    "ß": "ss",
    "Đ": "d",
    "đ": "d",
    "Ħ": "h",
    "ħ": "h",
    "ı": "i",
    "Ĳ": "ij",
    "ĳ": "ij",
    "ĸ": "k",
    "Ŀ": "l",
    "ŀ": "l",
    "Ł": "l",
    "ł": "l",
    "ŉ": "n",
    "Ŋ": "n",
    "ŋ": "n",
    "Œ": "oe",
    "œ": "oe",
    "Ŧ": "t",
    "ŧ": "t",
    "ſ": "s",
}


def test_suggest_alias_folds_every_latin_letter_and_full_width_form() -> None:
    # Every other letter of the range is its canonical decomposition with the marks taken off,
    # lowercase.
    for code in range(0x00C0, 0x0180):
        letter = chr(code)
        plain = "".join(
            part for part in unicodedata.normalize("NFD", letter) if not unicodedata.combining(part)
        )
        folded = UNDECOMPOSED.get(letter, plain.lower())
        assert folded == "-" or (folded.isascii() and folded.isalpha()), hex(code)
        assert policy.suggest_alias(f"x{letter}x", ()) == f"x{folded}x", hex(code)
        # Decomposed, it gives the same: each combining mark is dropped.
        decomposed = unicodedata.normalize("NFD", letter)
        assert policy.suggest_alias(f"x{decomposed}x", ()) == f"x{folded}x", hex(code)
    # Every combining mark of U+0300 to U+036F is dropped, and the characters around it are not.
    for code in range(0x0300, 0x0370):
        assert policy.suggest_alias(f"x{chr(code)}x", ()) == "xx", hex(code)
    for letter in ("\u02ff", "\u0370"):
        assert policy.suggest_alias(f"x{letter}x", ()) == "x-x"
    # Around the range: other letters are `-`.
    for letter in ("¿", "ƀ", "Ѐ"):
        assert policy.suggest_alias(f"x{letter}x", ()) == "x-x"
    # A full-width form is the ASCII character it stands for.
    for code in range(0xFF01, 0xFF5F):
        assert policy.suggest_alias(f"x{chr(code)}x", ()) == policy.suggest_alias(
            f"x{chr(code - 0xFEE0)}x", ()
        ), hex(code)
    for letter in ("＀", "｟"):
        assert policy.suggest_alias(f"x{letter}x", ()) == "x-x"
    # Past U+FF5E, the half-width letters and the full-width signs (U+FFE1, U+FFE5, ...) are no
    # full-width form of ASCII: each is `-`, whatever a shift by U+FEE0 would land on.
    for code in range(0xFF5F, 0xFFF0):
        assert policy.suggest_alias(f"x{chr(code)}x", ()) == "x-x", hex(code)


# Run as a process of its own: a second folder whose listing stalls until the test lets it go.
_STALLED = """
import os, sys, time
from pathlib import Path
release, real = Path(sys.argv[1]), os.scandir
def stalled(path="."):
    if os.path.basename(str(path)) == "b":
        deadline = time.monotonic() + 60
        while not release.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
    return real(path)
os.scandir = stalled
from lucille_host.cli import main
sys.exit(main(sys.argv[2:]))
"""


def test_each_line_is_written_before_the_next_step(
    tmp_path: Path,
    host_env: HostEnv,
    stack: contextlib.ExitStack,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Its stdout a pipe, as a shell reads it, and not unbuffered by the environment: the first
    # folder's answer arrives while the second folder stalls.
    w = World(tmp_path, stack, capsys, monkeypatch)
    for name in ("a", "b"):
        w.ok("roots", "add", name, w.folder(name))
    environment = {k: v for k, v in os.environ.items() if k != "PYTHONUNBUFFERED"}
    environment["HOME"] = str(w.home)
    for argv in (["--json", "roots", "check"], ["roots", "check"]):
        release = tmp_path / f"release-{len(argv)}"
        child = subprocess.Popen(
            [sys.executable, "-c", _STALLED, str(release), *argv],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )
        assert child.stdout is not None
        try:
            ready, _, _ = select.select([child.stdout], [], [], 20)
            first = child.stdout.readline() if ready else b""
        finally:
            release.touch()
            rest, err = child.communicate(timeout=60)
        assert child.returncode == 0, err
        assert first, f"nothing was written while the second folder stalled: {argv}"
        lines = [first.decode(), *rest.decode().splitlines(keepends=True)]
        if "--json" in argv:
            found = assert_objects(Ran(0, "".join(lines), ""))
            assert [(o["alias"], o["state"]) for o in found] == [("a", "ok"), ("b", "ok")]
        else:
            assert lines == ["a  ok\n", "b  ok\n"]
