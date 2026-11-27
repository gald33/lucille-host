"""The scenarios, and what a scenario is given.

A scenario is a function of one :class:`Context`, registered with :func:`scenario`. It passes when
it returns, and fails when it raises :class:`Failed` (what the host did is not what the spec says)
or anything else (the suite counts that as a failure too, never as a pass). Scenarios run in the
order they are registered, against one host, paired once, with one ``run`` reading one work room.

What a scenario is given (``ctx``):

- ``ctx.body(op, args=None, **extra)``: a request body, with a fresh ``job_id`` and ``not_after``
  ten minutes ahead; ``extra`` adds keys to the envelope (or replaces one).
- ``ctx.post(body)``: post it in the work room and return at once; its ``job_id``.
- ``ctx.wait(job_id, seconds=60)``: the host's reply to it, once Lucille can read it and ``run``
  has written its ``finished`` event (so every event of the job has been read).
- ``ctx.request(body)``: ``post`` then ``wait``.
- ``ctx.events(job_id=None)``: the ``request`` events ``run`` has written, of one job if named.
- ``ctx.spawn_count()``: how many ``started`` events the running ``run`` has written (a restart
  starts the count again). A
  ``started`` event is the host's own word that the process of a request is about to start
  (spec/CLI.md section 6, ``request``); the suite sees no process the host starts by any other
  means. The positive control shows that it is seen when a process does start.
- ``ctx.policy_digest()``: the ``policy_digest`` of a fresh ``host.describe`` reply.
  ``ctx.baseline`` holds the digest, ``status`` policy, policy file bytes and ``host.describe``
  document taken once the user had set the policy, before any scenario ran.
- ``ctx.host``: the installed host (:class:`lucille_host.conformance._host.Host`):
  ``cli(...)``, ``status()``, ``enable(op)``, ``add_root(alias, path)``, ``policy_file``,
  ``restart(env=None)`` (stops ``run`` and starts it again, with extra environment variables;
  ``run`` reads the policy file as it changes, so a policy edit needs no restart), ``pid`` (of the
  running ``run``) and ``events()``.
- ``ctx.root``: the folder the policy holds as alias ``work`` (:data:`ROOT`); ``ctx.workdir``: a
  folder beside it, outside every root, to plant files in.

A scenario that changes the policy or restarts ``run`` must put things back as it found them, or
be registered last: the scenarios after it check against ``ctx.baseline``.

:func:`expect_refused` posts a request and checks what every refusal must show: status
``refused`` with the code given, the reply's ``policy_digest`` and a fresh ``host.describe``'s
both equal to the baseline, the ``decided`` event naming that code, no ``started`` event for the
job and no change in ``spawn_count``.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROTOCOL = "lucille.exec/1"
# The folder alias the user's policy holds, and the operation the suite enables beside the
# fresh policy's host.describe. host.sleep stays off: the not-enabled scenario asks for it.
ROOT = "work"
ENABLED = ("files.sha256",)
# The host's own cap on each output stream, measured as spec/PROTOCOL.md section 13 measures it.
STREAM_CAP = 65536
# How long a scenario waits for a reply, in seconds.
REPLY_WAIT = 60.0


class Failed(Exception):
    """The host did not do what the spec says."""


def check(ok: bool, message: str) -> None:
    """Raise :class:`Failed` with ``message`` unless ``ok``."""
    if not ok:
        raise Failed(message)


def measure(value: Any) -> int:
    """A value's size as spec/PROTOCOL.md section 13 measures it."""
    return len(json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("ascii"))


@dataclass
class Baseline:
    digest: str
    policy: dict[str, Any]
    policy_file: bytes | None
    describe: dict[str, Any]


@dataclass
class Context:
    host: Any  # lucille_host.conformance._host.Host
    lucille: Any  # lucille_host.conformance._lucille.Lucille
    room: str
    root: Path
    workdir: Path
    baseline: Baseline | None = None

    def body(self, op: str, args: Any = None, **extra: Any) -> dict[str, Any]:
        return {
            "v": PROTOCOL,
            "dir": "request",
            "job_id": str(uuid.uuid4()),
            "op": op,
            "args": {} if args is None else args,
            "not_after": int(time.time()) + 600,
            **extra,
        }

    def post(self, body: dict[str, Any]) -> str:
        self.lucille.post(self.room, body)
        return str(body["job_id"])

    def events(self, job_id: str | None = None) -> list[dict[str, Any]]:
        return [
            found
            for found in self.host.events()
            if found.get("type") == "request" and (job_id is None or found.get("job_id") == job_id)
        ]

    def wait(self, job_id: str, seconds: float = REPLY_WAIT) -> dict[str, Any]:
        deadline = time.monotonic() + seconds
        while True:
            replies = [r for r in self.lucille.replies(self.room) if r.get("job_id") == job_id]
            finished = any(e.get("phase") == "finished" for e in self.events(job_id))
            if replies and finished:
                return replies[0]
            if time.monotonic() >= deadline:
                raise Failed(
                    f"no reply to {job_id} within {seconds:g} s (reply seen: {bool(replies)}, "
                    f"finished event seen: {finished})\n{self.host.describe()}"
                )
            time.sleep(0.1)

    def request(self, body: dict[str, Any], seconds: float = REPLY_WAIT) -> dict[str, Any]:
        return self.wait(self.post(body), seconds)

    def spawn_count(self) -> int:
        return sum(1 for found in self.events() if found.get("phase") == "started")

    def policy_digest(self) -> str:
        reply = self.request(self.body("host.describe"))
        check(reply.get("status") == "done", f"host.describe was not done: {reply!r}")
        return str(reply["policy_digest"])

    def describe(self) -> dict[str, Any]:
        """A fresh ``host.describe`` document."""
        reply = self.request(self.body("host.describe"))
        check(reply.get("status") == "done", f"host.describe was not done: {reply!r}")
        document = json.loads(reply["stdout"])
        check(isinstance(document, dict), f"host.describe's document is {document!r}")
        return document


def expect_refused(ctx: Context, body: dict[str, Any], code: str) -> dict[str, Any]:
    """Post ``body`` and check that it is refused ``code`` and widened nothing (module
    docstring); the reply."""
    assert ctx.baseline is not None
    spawns = ctx.spawn_count()
    job_id = ctx.post(body)
    reply = ctx.wait(job_id)
    check(
        (reply.get("status"), reply.get("code")) == ("refused", code),
        f"expected refused {code}, got {_short(reply)}",
    )
    decided = [e for e in ctx.events(job_id) if e.get("phase") == "decided"]
    check(
        [(e.get("decision"), e.get("code")) for e in decided] == [("refused", code)],
        f"run's decided events for the job are {decided!r}",
    )
    started = [e for e in ctx.events(job_id) if e.get("phase") == "started"]
    check(not started, f"run wrote a started event for a refused request: {started!r}")
    check(ctx.spawn_count() == spawns, "run started a process while refusing")
    check(
        reply.get("policy_digest") == ctx.baseline.digest,
        f"the refusal carries policy digest {reply.get('policy_digest')}, not the baseline's",
    )
    check(ctx.policy_digest() == ctx.baseline.digest, "the policy digest changed after the refusal")
    return reply


def _short(reply: dict[str, Any]) -> str:
    shown = {k: reply.get(k) for k in ("status", "code", "exit_code") if k in reply}
    return json.dumps(shown)


@dataclass(frozen=True)
class Scenario:
    name: str
    run: Callable[[Context], None]
    doc: str = field(default="")


REGISTRY: list[Scenario] = []


def scenario(
    function: Callable[[Context], None], *, name: str | None = None, doc: str | None = None
) -> Callable[[Context], None]:
    """Register ``function`` as a scenario: under ``name`` if given, else under the function's
    name with ``_`` as ``-``; described by ``doc`` if given, else by its docstring."""
    named = name if name is not None else function.__name__.replace("_", "-")
    if any(found.name == named for found in REGISTRY):
        raise ValueError(f"two scenarios named {named}")
    told = doc if doc is not None else (function.__doc__ or "")
    REGISTRY.append(Scenario(named, function, told.strip()))
    return function


# --- the files the scenarios use, planted before the first one runs --------------------------

HELLO = b"hello from the conformance suite\n"
SECRET = b"a file outside every root\n"
DASH = b"a file whose name is an option\n"


def plant(root: Path, workdir: Path) -> None:
    """The files the scenarios ask for: ``hello.txt`` and ``--version`` in the root;
    ``secret.txt`` outside it, in the work folder; and ``escape`` in the root, a symbolic link
    to that secret."""
    (root / "hello.txt").write_bytes(HELLO)
    (root / "--version").write_bytes(DASH)
    (workdir / "secret.txt").write_bytes(SECRET)
    (root / "escape").symlink_to(workdir / "secret.txt")


def _digest_line(reply: dict[str, Any], content: bytes, name: str) -> None:
    check(reply.get("status") == "done", f"expected done, got {_short(reply)}")
    check(reply.get("exit_code") == 0, f"exit code {reply.get('exit_code')}")
    expected = f"{hashlib.sha256(content).hexdigest()}  {name}\n"
    check(reply.get("stdout") == expected, f"stdout {reply.get('stdout')!r}, not {expected!r}")


# --- the positive control -----------------------------------------------------------------------


@scenario
def control_enabled_op_runs(ctx: Context) -> None:
    """files.sha256 of a file in the root runs, starts one process, and replies done with the
    file's digest under the baseline policy digest. Without it, a harness that saw every request
    refused, or saw no process start, could pass the scenarios that expect a refusal."""
    assert ctx.baseline is not None
    spawns = ctx.spawn_count()
    job_id = ctx.post(ctx.body("files.sha256", {"root": ROOT, "path": "hello.txt"}))
    reply = ctx.wait(job_id)
    _digest_line(reply, HELLO, "hello.txt")
    phases = [e.get("phase") for e in ctx.events(job_id)]
    check(phases == ["received", "decided", "started", "finished"], f"run's events: {phases}")
    check(ctx.spawn_count() == spawns + 1, "the control did not count one process started")
    check(reply.get("policy_digest") == ctx.baseline.digest, "the reply's policy digest differs")


# --- requests that must be refused, each widening nothing ---------------------------------------


@scenario
def unknown_op(ctx: Context) -> None:
    """An op this release's catalog does not have is refused unknown_op."""
    expect_refused(ctx, ctx.body("host.shell", {"cmd": "id"}), "unknown_op")


@scenario
def not_enabled_op(ctx: Context) -> None:
    """host.sleep, in the catalog and not enabled by the user's policy, is refused
    not_enabled."""
    expect_refused(ctx, ctx.body("host.sleep", {"n": 1}), "not_enabled")


def _extra_key(key: str, value: Any) -> Callable[[Context], None]:
    def run(ctx: Context) -> None:
        body = ctx.body("files.sha256", {"root": ROOT, "path": "hello.txt"}, **{key: value})
        expect_refused(ctx, body, "bad_request")

    return run


for _key, _value in (
    ("exe", "/bin/sh"),
    ("argv", ["/bin/sh", "-c", "id"]),
    ("env", {"LD_PRELOAD": "/tmp/x.so"}),
    ("cwd", "/"),
    ("timeout", 100000),
):
    scenario(
        _extra_key(_key, _value),
        name=f"extra-key-{_key}",
        doc=f"A files.sha256 request that would run, with `{_key}` added to the envelope, is "
        "refused bad_request.",
    )


@scenario
def policy_shaped_fields(ctx: Context) -> None:
    """Envelope keys shaped like the local policy (ops to enable, a folder for an alias, a
    policy object) are refused bad_request."""
    body = ctx.body(
        "host.describe",
        enable_ops=["host.sleep"],
        roots={"home": "/"},
        policy={"enabled": {"host.sleep": True}, "log_results": True},
    )
    expect_refused(ctx, body, "bad_request")


@scenario
def update_shaped_fields(ctx: Context) -> None:
    """Envelope keys shaped like an update (a catalog, a definition hash, a release to fetch)
    are refused bad_request."""
    body = ctx.body(
        "host.describe",
        catalog={"host.sleep": {"argv": ["/bin/sh"]}},
        definition_hash="0" * 64,
        update={"version": "9.9.9", "url": "http://127.0.0.1:1/lucille_host.whl"},
    )
    expect_refused(ctx, body, "bad_request")


@scenario
def extra_keys_in_args(ctx: Context) -> None:
    """`argv`, `exe` and `cwd` inside files.sha256's arguments are refused bad_args."""
    args = {"root": ROOT, "path": "hello.txt", "argv": ["/bin/sh"], "exe": "/bin/sh", "cwd": "/"}
    expect_refused(ctx, ctx.body("files.sha256", args), "bad_args")


@scenario
def traversal(ctx: Context) -> None:
    """A path with a `..` segment, to a file that exists outside the root, is refused
    path_outside_root."""
    args = {"root": ROOT, "path": "../secret.txt"}
    expect_refused(ctx, ctx.body("files.sha256", args), "path_outside_root")


@scenario
def absolute_path(ctx: Context) -> None:
    """An absolute path, to a file that exists outside the root, is refused
    path_outside_root."""
    args = {"root": ROOT, "path": str(ctx.workdir / "secret.txt")}
    expect_refused(ctx, ctx.body("files.sha256", args), "path_outside_root")


@scenario
def symlink_escape(ctx: Context) -> None:
    """A symbolic link in the root that leads to a file outside it is refused
    path_outside_root."""
    args = {"root": ROOT, "path": "escape"}
    expect_refused(ctx, ctx.body("files.sha256", args), "path_outside_root")


@scenario
def unknown_root(ctx: Context) -> None:
    """A folder alias the policy does not hold is refused path_outside_root."""
    args = {"root": "home", "path": "hello.txt"}
    expect_refused(ctx, ctx.body("files.sha256", args), "path_outside_root")


@scenario
def oversize_path(ctx: Context) -> None:
    """A path of 1025 characters, one past the schema's bound, is refused bad_args."""
    args = {"root": ROOT, "path": "a" * 1025}
    expect_refused(ctx, ctx.body("files.sha256", args), "bad_args")


@scenario
def oversize_request(ctx: Context) -> None:
    """A request that measures more than 16 KiB is refused bad_request."""
    body = ctx.body("files.sha256", {"root": ROOT, "path": "a" * 17000})
    check(measure(body) > 16384, "the body is not oversize")
    expect_refused(ctx, body, "bad_request")


# --- requests that run, and must run as the spec says -------------------------------------------


@scenario
def file_named_version(ctx: Context) -> None:
    """files.sha256 of a file named `--version` hashes the file: its name is not read as an
    option (the reply is the file's digest line, not the program's version)."""
    reply = ctx.request(ctx.body("files.sha256", {"root": ROOT, "path": "--version"}))
    _digest_line(reply, DASH, "--version")


@scenario
def caps_above_host(ctx: Context) -> None:
    """Request caps far above the host's own are not refused: the request runs, and each
    stream of the reply measures within the host's own cap. This output is short, so this
    shows the request is not refused and does not show the host's cap holding against longer
    output; caps_below_output shows the lower cap is the one applied."""
    caps = {"stdout_bytes": 2147483647, "stderr_bytes": 2147483647}
    reply = ctx.request(ctx.body("files.sha256", {"root": ROOT, "path": "hello.txt"}, caps=caps))
    _digest_line(reply, HELLO, "hello.txt")
    for stream in ("stdout", "stderr"):
        check(measure(reply[stream]) <= STREAM_CAP, f"{stream} measures past the host's cap")


@scenario
def caps_below_output(ctx: Context) -> None:
    """A request cap below the output's size is applied: stdout measures within it, and is
    flagged truncated."""
    caps = {"stdout_bytes": 20, "stderr_bytes": 2}
    reply = ctx.request(ctx.body("files.sha256", {"root": ROOT, "path": "hello.txt"}, caps=caps))
    check(reply.get("status") == "done", f"expected done, got {_short(reply)}")
    check(measure(reply["stdout"]) <= 20, f"stdout {reply['stdout']!r} measures past 20 bytes")
    check(reply.get("stdout_truncated") is True, "stdout is not flagged truncated")
    full = hashlib.sha256(HELLO).hexdigest()
    check(full.startswith(reply["stdout"]), f"stdout {reply['stdout']!r} is not the digest's start")


@scenario
def policy_unchanged(ctx: Context) -> None:
    """After every scenario above: `status` shows the same policy, the policy file holds the same
    bytes, and host.describe the same document (enabled ops, folder aliases, digest)."""
    assert ctx.baseline is not None
    check(ctx.host.status()["policy"] == ctx.baseline.policy, "status shows another policy")
    named = ctx.host.policy_file
    now = named.read_bytes() if named is not None else None
    check(now == ctx.baseline.policy_file, "the policy file changed")
    check(ctx.describe() == ctx.baseline.describe, "host.describe's document changed")
