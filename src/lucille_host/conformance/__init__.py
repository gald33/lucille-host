"""The conformance suite: a hostile fake Lucille that checks the installed host's invariants.

Running it (``python -m lucille_host.conformance``, with the ``python`` of the virtual environment
``lucille-host[conformance]`` is installed in) does this, offline, on 127.0.0.1 only:

1. It starts a Switchboard hub, ``switchboard serve``, on 127.0.0.1 and a port the hub picks, with
   its database in a temporary folder.
2. It gives the installed ``lucille-host`` a home of its own in that folder, and pairs it with the
   suite's Lucille through the real flow: ``pair begin --json --hub URL --store-file
   --test-key=KEY``, where KEY is a throwaway key the suite signs its pairing code with (never
   Lucille's own), then ``pair confirm`` once the SAS the host shows is the one Lucille holds.
3. It sets the local policy as the user would, with the host's own ``ops enable files.sha256`` and
   ``roots add -- work <folder>``: the suite plays a hostile Lucille, not a hostile user.
4. It starts ``run --json-events --exit-on-stdin-eof``, offers a work room over the pair, and runs
   each scenario in ``_scenarios.py``: each posts requests there, reads the host's replies and
   ``run``'s events, and checks them against spec/PROTOCOL.md.
5. It prints one line per scenario, ``PASS <name>`` or ``FAIL <name>: <why>``, then a summary, and
   exits 0 only when there is at least one scenario and every one passed. If the harness cannot
   set the host up, it says why and exits 1.

The scenarios cover the design record's first invariant, "Nothing Lucille sends can widen what the
host permits" (decisions/0001-host-design.md, "Host invariants and their tests"), and a positive
control; ``_scenarios.py`` says what each asserts and how the suite observes it.

The host refuses to run as root (decisions/0004-running-as-root.md), so the suite does too: run it
as an ordinary user.

This package is not part of the runtime. The runtime lints exempt it by name, and it has a lint of
its own: it may start only the local hub (``switchboard serve``) and the installed
``lucille-host``, from a single module, ``_spawn.py``. The lint lets that module name each program
by a constant bound to its path beside the interpreter running the suite
(``Path(sys.executable).parent / "lucille-host"``), so that the suite starts the copy installed
with it rather than one found first on ``PATH``. ``tools/guards/ast_lint.py`` lists the forms it
accepts and the ones it refuses; this package is not held to the runtime's import allowlist, so a
standard-library module that starts a process inside (``platform.architecture`` runs ``file``) is
not refused here, and review covers it. The runtime may not import this package.
"""

from __future__ import annotations

import contextlib
import os
import sys
import tempfile
import time
import traceback
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import TextIO

__all__ = ["main", "run_scenarios"]

WORK_ROOM = "conformance"
# Variables the suite's own Switchboard client would read, set for the length of a run: proxies
# (so that it reaches only 127.0.0.1) and the runtime folder where it would look for a signer.
_OWN_ENV_DROPPED = (
    "http_proxy",
    "HTTP_PROXY",
    "https_proxy",
    "HTTPS_PROXY",
    "all_proxy",
    "ALL_PROXY",
)


def run_scenarios(scenarios: Sequence[object], context: object, out: TextIO) -> tuple[int, int]:
    """Run each scenario on ``context``, printing one line for each; (passed, failed)."""
    from lucille_host.conformance._scenarios import Failed, Scenario

    passed = failed = 0
    for found in scenarios:
        assert isinstance(found, Scenario)
        try:
            found.run(context)  # type: ignore[arg-type]
        except Failed as failure:
            failed += 1
            print(f"FAIL {found.name}: {_one_line(str(failure))}", file=out, flush=True)
        except Exception as error:  # noqa: BLE001 - any error is a failure, never a pass
            failed += 1
            print(
                f"FAIL {found.name}: {type(error).__name__}: {_one_line(str(error))}",
                file=out,
                flush=True,
            )
        else:
            passed += 1
            print(f"PASS {found.name}", file=out, flush=True)
    return passed, failed


def verdict(passed: int, failed: int) -> int:
    """The exit code: 0 only when at least one scenario ran and none failed."""
    return 0 if passed > 0 and failed == 0 else 1


def _one_line(text: str) -> str:
    """The first line of ``text``; the whole of it goes to standard error when it has more."""
    first, _, rest = text.partition("\n")
    if not rest:
        return first
    print(text, file=sys.stderr, flush=True)
    return f"{first} [details on stderr]"


@contextlib.contextmanager
def _own_environment(runtime: Path) -> Iterator[None]:
    saved = {name: os.environ.get(name) for name in (*_OWN_ENV_DROPPED, "XDG_RUNTIME_DIR")}
    try:
        for name in _OWN_ENV_DROPPED:
            os.environ.pop(name, None)
        os.environ["XDG_RUNTIME_DIR"] = str(runtime)
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def main(argv: Sequence[str] | None = None, *, scenarios: Sequence[object] | None = None) -> int:
    """Run the suite; the exit code. ``scenarios``, for the suite's own tests, replaces the
    registered ones."""
    from lucille_host.conformance import _scenarios, _spawn
    from lucille_host.conformance._host import HarnessError

    out = sys.stdout
    chosen = list(_scenarios.REGISTRY if scenarios is None else scenarios)
    if not chosen:
        print("lucille-host conformance: no scenarios to run", file=out)
        return 1
    if os.geteuid() == 0:
        print(
            "lucille-host conformance: the host refuses to run as root; run the suite as an "
            "ordinary user",
            file=out,
        )
        return 1
    missing = _spawn.programs_missing()
    if missing:
        print(
            "lucille-host conformance: not installed beside this interpreter: "
            + ", ".join(str(path) for path in missing)
            + ". Run the suite with the python of the virtual environment that has "
            "lucille-host[conformance].",
            file=out,
        )
        return 1
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="lucille-host-conformance-") as name:
        folder = Path(name)
        runtime = folder / "suite-runtime"
        runtime.mkdir(mode=0o700)
        with _own_environment(runtime):
            try:
                with _harness(folder) as context:
                    print(f"lucille-host conformance: {len(chosen)} scenarios", file=out)
                    passed, failed = run_scenarios(chosen, context, out)
            except HarnessError as error:
                print(f"lucille-host conformance: the harness failed: {error}", file=out)
                return 1
            except Exception as error:  # noqa: BLE001 - reported, and never a pass
                traceback.print_exc()
                print(
                    f"lucille-host conformance: the harness failed: {type(error).__name__}: "
                    f"{_one_line(str(error))}",
                    file=out,
                )
                return 1
    seconds = time.monotonic() - started
    print(
        f"lucille-host conformance: {passed} passed, {failed} failed, in {seconds:.0f} s",
        file=out,
    )
    return verdict(passed, failed)


@contextlib.contextmanager
def _harness(folder: Path) -> Iterator[object]:
    """The hub, the paired host with the user's policy set, ``run``, the offered work room,
    and the baseline: a :class:`_scenarios.Context`."""
    from lucille_host.conformance import _scenarios, _spawn
    from lucille_host.conformance._host import HarnessError, Host, host_env, until
    from lucille_host.conformance._lucille import Lucille

    hub_folder = folder / "hub"
    hub_folder.mkdir(mode=0o700)
    hub = _spawn.start_hub(hub_folder, host_env(hub_folder, hub_folder))
    host: Host | None = None
    try:

        def listening() -> str | None:
            if not hub.alive():
                raise HarnessError(f"the hub exited\n{hub.describe()}")
            found = _spawn.LISTENING.search(hub.stderr)
            return found.group(1) if found else None

        url = until(listening, "the hub listening")
        lucille_folder = folder / "lucille"
        lucille_folder.mkdir(mode=0o700)
        lucille = Lucille(url, lucille_folder)
        host = Host(folder / "host")
        host.pair(lucille)

        workdir = folder / "work"
        root = workdir / "root"
        root.mkdir(parents=True)
        _scenarios.plant(root, workdir)
        for op in _scenarios.ENABLED:
            host.enable(op)
        host.add_root(_scenarios.ROOT, root)

        host.start_run()
        lucille.offer(WORK_ROOM)

        def reading() -> bool:
            return any(
                room.get("state") == "reading"
                for found in host.events()
                if found.get("type") == "state"
                for room in found.get("rooms", [])
            )

        until(reading, "run reading the work room")
        context = _scenarios.Context(
            host=host, lucille=lucille, room=WORK_ROOM, root=root, workdir=workdir
        )
        context.baseline = _baseline(context)
        yield context
    finally:
        if host is not None:
            host.close()
        hub.stop()


def _baseline(context: object) -> object:
    from lucille_host.conformance._scenarios import Baseline, Context

    assert isinstance(context, Context)
    describe = context.describe()
    named = context.host.policy_file
    return Baseline(
        digest=str(describe["policy_digest"]),
        policy=context.host.status()["policy"],
        policy_file=named.read_bytes() if named is not None else None,
        describe=describe,
    )
