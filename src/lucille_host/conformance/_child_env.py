"""Scenario: a child of the host gets the built environment, and no key.

The invariant "No shell; one spawn site; children get a built environment and no key"
(decisions/0001, "Host invariants and their tests"), through the installed host. ``run`` is
restarted with variables planted in its own environment: names a key or token could travel
under, and one that would change what a child does (``RIPGREP_CONFIG_PATH``). Not
``SWITCHBOARD_*`` names, with which the host refuses to start at all, nor ``LD_PRELOAD``, which
would load into the host itself.
Lucille then asks for ``host.sleep``, and while the child sleeps the suite reads its starting
environment and open descriptors from ``/proc``. The child must hold exactly the allowlisted
variables with their fixed values, none of the planted names or values, no descriptor beyond its
three standard streams, and standard input on ``/dev/null``.

What it does not cover: the operations whose entries copy more of the host's environment into
their child (``github.read`` and the ``gmail.`` operations, which get what the keyring needs).
They reach the network, which the suite does not.

This needs Linux's ``/proc``. Where there is none (macOS), the scenario fails, saying that it
could not check, rather than pass.
"""

from __future__ import annotations

import os
import time

from lucille_host.conformance import _procfs
from lucille_host.conformance._restore import restore, scenario, until_describe
from lucille_host.conformance._scenarios import Context, Failed, _short, check

# The child's whole environment, as decisions/0001 ("Execution rules") states it. The suite
# states it again rather than importing the host's own constant, so that a change to the host
# cannot change what the suite expects.
EXPECTED_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}
SENTINEL = "conformance-planted-value-7c41"
PLANTED = {
    "LUCILLE_HOST_KEY": SENTINEL,
    "GITHUB_TOKEN": SENTINEL,
    "AWS_SECRET_ACCESS_KEY": SENTINEL,
    "API_KEY": SENTINEL,
    "RIPGREP_CONFIG_PATH": "/nonexistent/" + SENTINEL,
}
SLEEP_S = 3
FIND_FOR_S = SLEEP_S - 0.5


def check_child(proc: _procfs.Process) -> list[str]:
    """What is wrong with a child's environment and descriptors, as plain sentences."""
    problems: list[str] = []
    if proc.env != EXPECTED_ENV:
        extra = sorted(set(proc.env) - set(EXPECTED_ENV))
        missing = sorted(set(EXPECTED_ENV) - set(proc.env))
        changed = sorted(
            n for n in EXPECTED_ENV if n in proc.env and proc.env[n] != EXPECTED_ENV[n]
        )
        problems.append(
            f"the child's environment is not the built one: extra {extra}, missing {missing}, "
            f"changed {changed}"
        )
    if any(SENTINEL in value for value in proc.env.values()):
        problems.append("a planted value reached the child's environment")
    if proc.fds != (0, 1, 2):
        problems.append(
            f"the child holds descriptors beyond its standard streams: {list(proc.fds)}"
        )
    return problems


def find_child(host_pid: int, argv: tuple[str, ...], seconds: float) -> _procfs.Process | None:
    """The first process below ``host_pid`` running ``argv``, looked for for ``seconds``."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for pid in _procfs.descendants(host_pid):
            proc = _procfs.read(pid)
            if proc is not None and proc.argv == argv:
                return proc
        time.sleep(0.02)
    return None


@scenario
def child_env_built_and_no_key(ctx: Context) -> None:
    """host.sleep's child, started by a run whose own environment holds planted keys, tokens and
    RIPGREP_CONFIG_PATH, has exactly PATH, LANG and LC_ALL with their fixed
    values, only its three standard streams, and standard input on /dev/null (Linux; read from
    /proc)."""
    check(_procfs.available(), "not checked: this computer has no /proc to read a child from")
    ctx.host.enable("host.sleep")
    ctx.host.restart(env=PLANTED)
    try:
        until_describe(ctx, lambda d: "host.sleep" in d["enabled_ops"], "enabling host.sleep")
        spawns = ctx.spawn_count()
        job_id = ctx.post(ctx.body("host.sleep", {"n": SLEEP_S}))
        proc = find_child(ctx.host.pid, ("/bin/sleep", str(SLEEP_S)), FIND_FOR_S)
        stdin = None
        if proc is not None:
            try:
                stdin = os.readlink(f"/proc/{proc.pid}/fd/0")
            except OSError:
                stdin = None
        reply = ctx.wait(job_id, SLEEP_S + 60)
        check(proc is not None, "host.sleep's child was not found below run's process")
        assert proc is not None
        problems = check_child(proc)
        if stdin != "/dev/null":
            problems.append(f"the child's standard input is {stdin!r}, not /dev/null")
        if reply.get("status") != "done" or reply.get("exit_code") != 0:
            problems.append(f"host.sleep did not run to the end: {_short(reply)}")
        if ctx.spawn_count() != spawns + 1:
            problems.append("run did not count the one process it started")
        if SENTINEL in str(reply):
            problems.append("a planted value appears in the reply")
        if problems:
            raise Failed("; ".join(problems))
    finally:
        restore(ctx, restart=True)
