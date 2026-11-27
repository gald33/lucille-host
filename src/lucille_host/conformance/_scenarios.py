"""The scenario registry, the context a scenario gets, and the runner.

A scenario is a function of one :class:`Context`, registered with :func:`scenario`::

    @scenario("allowed-op")
    def allowed_op(ctx: Context) -> None:
        reply = ctx.request("host.describe")
        ctx.expect_status(reply, "done")

It passes when it returns, and fails when it raises: :class:`Failed`, an ``AssertionError``, or
anything else (an unexpected error is a failure too, never a pass). Scenarios run in the order
they were registered, all against one :class:`~lucille_host.conformance._session.Session`: one
paired host, one ``run``, one work room. A scenario that changes the policy puts it back.

:func:`run_all` runs every registered scenario, prints one line per scenario and a summary, and
returns 0 only when at least one scenario ran and every one passed.
"""

from __future__ import annotations

import time
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from lucille_host import jobs
from lucille_host.conformance._session import Session, SessionError

__all__ = ["PROTOCOL", "REGISTRY", "Context", "Failed", "Scenario", "run_all", "scenario"]

PROTOCOL = jobs.PROTOCOL
# How long a request is given to be answered, in seconds.
REPLY_WAIT = 30.0
# How long ``post`` waits before it says the host posted nothing.
SILENCE_WAIT = 5.0


class Failed(Exception):
    """A scenario found the host doing what it must not."""


@dataclass(frozen=True)
class Scenario:
    name: str
    check: Callable[[Context], None]


REGISTRY: list[Scenario] = []


def scenario(name: str) -> Callable[[Callable[[Context], None]], Callable[[Context], None]]:
    """Register the decorated function as the scenario ``name``; names are unique."""

    def add(check: Callable[[Context], None]) -> Callable[[Context], None]:
        if any(found.name == name for found in REGISTRY):
            raise ValueError(f"a scenario named {name!r} is registered already")
        REGISTRY.append(Scenario(name, check))
        return check

    return add


class Context:
    """What a scenario may do: post requests as Lucille, read the host's replies, run the host's
    own verbs on its profile, and read what the host recorded."""

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def workdir(self) -> Path:
        """A scratch folder of the run's own, for folders to add as roots, links and files."""
        return self._session.workdir

    # --- requests -------------------------------------------------------------------------------

    def body(self, op: str, args: Any = None, **extra: Any) -> dict[str, Any]:
        """A valid request body: a fresh job id and ten minutes to run. ``extra`` keys are added
        verbatim, so a scenario can add ``exe``, ``argv`` or anything else Lucille might send."""
        return {
            "v": PROTOCOL,
            "dir": "request",
            "job_id": str(uuid.uuid4()),
            "op": op,
            "args": {} if args is None else args,
            "not_after": int(time.time()) + 600,
            **extra,
        }

    def post(self, body: Any, *, wait: float = SILENCE_WAIT) -> dict[str, Any] | None:
        """Post ``body`` as Lucille in the work room and wait up to ``wait`` seconds for the
        host's reply to its ``job_id``; None when the host posted none in that time (a body with
        no well-formed job id is never answered)."""
        session = self._session
        lucille = session.lucille
        if lucille is None:
            raise SessionError("the session has not started")
        lucille.post(session.room, body)
        job_id = body.get("job_id") if isinstance(body, dict) else None
        if not isinstance(job_id, str):
            time.sleep(wait)
            return None
        deadline = time.monotonic() + wait
        while True:
            found = lucille.reply(session.room, job_id)
            if found is not None:
                return found
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.1)

    def request(self, op: str, args: Any = None, **extra: Any) -> dict[str, Any]:
        """Post a request for ``op`` (see :meth:`body`) and return the host's reply body; a
        request the host does not answer within the wait is a failure."""
        sent = self.body(op, args, **extra)
        found = self.post(sent, wait=REPLY_WAIT)
        if found is None:
            raise Failed(f"no reply to {op} ({sent['job_id']}) within {REPLY_WAIT} s")
        return found

    # --- what the host shows ------------------------------------------------------------------

    def policy_digest(self) -> str:
        """The host's policy digest now, as its reply to ``host.describe`` carries it (every
        reply carries the digest of the policy it was decided under)."""
        reply = self.request("host.describe")
        digest = reply.get("policy_digest")
        if not isinstance(digest, str):
            raise Failed(f"host.describe's reply carries no policy digest: {reply}")
        return digest

    def spawn_count(self) -> int:
        """How many processes the host has started for operations: the ``start`` lines of its
        audit log, which it writes before it starts each one."""
        return sum(1 for line in self._session.audit_lines() if line.get("kind") == "start")

    def audit_lines(self) -> list[dict[str, Any]]:
        """Every line of the host's audit log, oldest first."""
        return self._session.audit_lines()

    def host(self, *args: str) -> Any:
        """Run a ``lucille-host`` verb on the host's profile (``ops enable ...``, ``roots add
        ...``) to its end: a ``subprocess.CompletedProcess`` with text output."""
        return self._session.host(*args)

    # --- checks --------------------------------------------------------------------------------

    def expect_status(self, reply: dict[str, Any], status: str, code: str | None = None) -> None:
        """Fail unless ``reply`` has ``status`` and, when given, ``code``."""
        if reply.get("status") != status or (code is not None and reply.get("code") != code):
            wanted = status if code is None else f"{status} {code}"
            raise Failed(
                f"expected {wanted}, got {reply.get('status')} {reply.get('code', '')}".rstrip()
            )

    def expect_refused(self, reply: dict[str, Any], code: str) -> None:
        self.expect_status(reply, "refused", code)


def _first_line(error: BaseException) -> str:
    text = str(error).strip().splitlines()
    return f"{type(error).__name__}: {text[0]}" if text else type(error).__name__


def _summary(passed: int, failed: int) -> str:
    verdict = "PASS" if failed == 0 and passed > 0 else "FAIL"
    return f"summary: {passed + failed} scenarios, {passed} passed, {failed} failed: {verdict}"


def run_all(
    out: IO[str],
    err: IO[str],
    *,
    registry: list[Scenario] | None = None,
    session_factory: Callable[[], Session] = Session,
) -> int:
    """Run every registered scenario against one session; one line per scenario on ``out``,
    then a summary. 0 only when at least one ran and every one passed."""
    scenarios = list(REGISTRY if registry is None else registry)
    if not scenarios:
        print("lucille-host conformance: no scenarios are registered", file=err)
        print(_summary(0, 0), file=out)
        return 1
    session = session_factory()
    passed = failed = 0
    try:
        try:
            session.start()
        except Exception as error:  # noqa: BLE001 - any failure to set up fails every scenario
            print(f"setup failed: {error}", file=err)
            for found in scenarios:
                print(f"FAIL {found.name}: not run: the setup failed", file=out)
            print(_summary(0, len(scenarios)), file=out)
            return 1
        ctx = Context(session)
        for found in scenarios:
            began = time.monotonic()
            try:
                found.check(ctx)
            except Exception as error:  # noqa: BLE001 - any error is a failure, never a pass
                failed += 1
                print(f"FAIL {found.name}: {_first_line(error)}", file=out)
                traceback.print_exc(file=err)
            else:
                passed += 1
                print(f"PASS {found.name} ({time.monotonic() - began:.1f} s)", file=out)
            out.flush()
    finally:
        session.close()
    print(_summary(passed, failed), file=out)
    return 0 if failed == 0 and passed > 0 else 1
