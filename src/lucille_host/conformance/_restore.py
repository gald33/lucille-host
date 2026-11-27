"""For scenarios that change the user's policy or restart ``run``: refusals checked against the
policy in force at the time, and putting the policy and ``run`` back as the baseline found them.

:func:`~lucille_host.conformance._scenarios.expect_refused` checks a refusal against the
baseline's policy digest, which is right for every scenario that leaves the policy alone. A
scenario that enables an operation has changed that digest on purpose; :func:`refused_here`
checks the same things against the digest in force just before the request.

:func:`restore` writes the baseline's policy file bytes back, restarts ``run`` without any extra
environment variables if asked, and waits until ``host.describe`` shows the baseline's document
again, so that the scenarios after it check against the policy they expect.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from lucille_host.conformance import _scenarios
from lucille_host.conformance._scenarios import Context, Failed, _short, check

__all__ = ["refused_here", "restore", "scenario", "until_describe", "write_policy"]

FINAL = "policy-unchanged"

WAIT = 30.0


def scenario(function: Callable[[Context], None]) -> Callable[[Context], None]:
    """Register ``function`` as :func:`_scenarios.scenario` does, and keep ``policy-unchanged``
    the last scenario, so that it checks the policy after this one has put it back too."""
    _scenarios.scenario(function)
    registry = _scenarios.REGISTRY
    final = [index for index, found in enumerate(registry) if found.name == FINAL]
    if final:
        registry.append(registry.pop(final[0]))
    return function


def write_policy(path: Path, data: bytes) -> None:
    """Replace the policy file with ``data`` in one rename, so that ``run``, which reads the file
    as it changes, never reads half of it."""
    temporary = path.with_name(path.name + ".conformance")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def until_describe(ctx: Context, condition: Any, what: str) -> dict[str, Any]:
    """A fresh ``host.describe`` document for which ``condition`` holds, within :data:`WAIT`
    seconds: ``run`` takes up a policy edit when it next notices the file has changed."""
    deadline = time.monotonic() + WAIT
    while True:
        document = ctx.describe()
        if condition(document):
            return document
        if time.monotonic() >= deadline:
            raise Failed(f"{what} did not happen within {WAIT:g} s: host.describe is {document!r}")
        time.sleep(0.2)


def refused_here(ctx: Context, body: dict[str, Any], code: str) -> dict[str, Any]:
    """Post ``body`` and check that it is refused ``code``, starts nothing, and leaves the policy
    digest as it was just before; the reply."""
    digest = ctx.policy_digest()
    spawns = ctx.spawn_count()
    job_id = ctx.post(body)
    reply = ctx.wait(job_id)
    check(
        (reply.get("status"), reply.get("code")) == ("refused", code),
        f"expected refused {code}, got {_short(reply)}",
    )
    started = [e for e in ctx.events(job_id) if e.get("phase") == "started"]
    check(not started, f"run wrote a started event for a refused request: {started!r}")
    check(ctx.spawn_count() == spawns, "run started a process while refusing")
    check(reply.get("policy_digest") == digest, "the refusal carries another policy digest")
    check(ctx.policy_digest() == digest, "the policy digest changed after the refusal")
    return reply


def restore(ctx: Context, *, restart: bool = False) -> None:
    """Put the policy file, and ``run`` if ``restart``, back as the baseline found them."""
    baseline = ctx.baseline
    assert baseline is not None
    named = ctx.host.policy_file
    if named is None or baseline.policy_file is None:
        raise Failed("the policy file is gone, or the baseline had none to put back")
    if named.read_bytes() != baseline.policy_file:
        write_policy(named, baseline.policy_file)
    if restart:
        ctx.host.restart()
    until_describe(ctx, lambda document: document == baseline.describe, "the baseline policy")
    check(ctx.policy_digest() == baseline.digest, "the policy digest is not the baseline's again")
