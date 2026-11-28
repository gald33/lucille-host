"""The installed ``lucille-host``, driven as a user drives it, in a home of the suite's own.

A :class:`Host` runs the installed program (``_spawn.py``) with ``HOME`` set to a fresh folder
under the suite's temporary folder, no ``XDG_*``, ``SWITCHBOARD_*``, keyring or proxy variable,
``TMPDIR`` in that folder too, and ``--store-file``, so that the pair is kept in files there and no
keyring is read. Its verbs:

- :meth:`Host.cli` runs one verb with ``--json`` and returns the objects it wrote;
- :meth:`Host.enable` and :meth:`Host.add_root` set the local policy with ``ops enable`` and
  ``roots add``, as the user would: the suite plays a hostile Lucille, but the policy is the
  user's;
- :meth:`Host.pair` runs ``pair begin --json --hub URL --store-file --test-key=KEY``, has Lucille
  answer it, then ``pair confirm`` once the SAS the host shows is the one Lucille holds;
- :meth:`Host.start_run` starts ``run --json-events --exit-on-stdin-eof`` and waits until it is
  ready; :meth:`Host.stop_run` closes its standard input, which drains it; :meth:`Host.restart`
  does both, optionally with extra environment variables for the new ``run``.

Every wait has a bound.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from lucille_host.conformance import _spawn
from lucille_host.conformance._lucille import Lucille, NoRequest

# The longest one wait may take, in seconds.
WAIT = 60.0
# Variables of the suite's own environment that the host is given; every other is left out.
PASSED_ON = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ")


class HarnessError(Exception):
    """The harness could not set the host up: not a finding about the host."""


def until(
    condition: Callable[[], Any],
    what: str,
    *,
    seconds: float = WAIT,
    detail: Callable[[], str] | None = None,
) -> Any:
    """What ``condition()`` returns once it is truthy, asked every 50 ms for at most
    ``seconds``; :class:`HarnessError` naming ``what`` if it never is, followed by what
    ``detail()`` says then."""
    deadline = time.monotonic() + seconds
    while True:
        found = condition()
        if found:
            return found
        if time.monotonic() >= deadline:
            said = f"\n{detail()}" if detail is not None else ""
            raise HarnessError(f"{what} did not happen within {seconds:g} s{said}")
        time.sleep(0.05)


def outline(child: _spawn.Child) -> str:
    """What a pairing failure shows of ``pair begin --json``: its exit code, the type and state of
    each object it wrote, with an error's code and kind, and how many lines it wrote on standard
    error. Nothing else of what it wrote: no connect code, pairing code, name, note or SAS."""
    shown = []
    for found in child.objects():
        part = str(found.get("type"))
        if "state" in found:
            part += f" {found['state']}"
        if found.get("type") == "error":
            part += f" (code {found.get('code')}, kind {found.get('kind')})"
        shown.append(part)
    return (
        f"pair begin: exit {child.proc.poll()}; wrote {', '.join(shown) or 'nothing'}; "
        f"{len(child.err)} line(s) on stderr"
    )


def host_env(home: Path, tmp: Path, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment the host is started with."""
    env = {name: os.environ[name] for name in PASSED_ON if name in os.environ}
    env.setdefault("PATH", "/usr/bin:/bin")
    env["HOME"] = str(home)
    env["TMPDIR"] = str(tmp)
    env.update(extra or {})
    return env


class Host:
    """The installed host, in a home under ``folder``."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        folder.mkdir(mode=0o700)
        self.home = folder / "home"
        self.tmp = folder / "tmp"
        for made in (self.home, self.tmp):
            made.mkdir(mode=0o700)
        self.env = host_env(self.home, self.tmp)
        self.run: _spawn.Child | None = None
        self._past: list[_spawn.Child] = []

    # --- one verb at a time ---------------------------------------------------------------

    def cli(self, *args: str, ok: bool = True) -> list[dict[str, Any]]:
        """Run ``lucille-host --json <args>``; the objects it wrote. With ``ok``, an exit other
        than 0 is a :class:`HarnessError`."""
        done = _spawn.run_host(["--json", *args], env=self.env, cwd=self.folder, timeout=WAIT)
        if ok and done.returncode != 0:
            raise HarnessError(
                f"lucille-host {' '.join(args)} exited {done.returncode}\n"
                f"stdout:\n{done.stdout}stderr:\n{done.stderr}"
            )
        found = []
        for line in done.stdout.splitlines():
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                found.append(value)
        return found

    def status(self) -> dict[str, Any]:
        found = self.cli("status")
        if len(found) != 1 or found[0].get("type") != "status":
            raise HarnessError(f"status wrote {found!r}")
        return found[0]

    def enable(self, op: str) -> dict[str, Any]:
        """``ops enable <op>``: the ``op`` object it wrote."""
        return self.cli("ops", "enable", op)[-1]

    def add_root(self, alias: str, path: Path) -> dict[str, Any]:
        """``roots add -- <alias> <path>``: the ``root`` object it wrote."""
        return self.cli("roots", "add", "--", alias, str(path))[-1]

    @property
    def policy_file(self) -> Path | None:
        """The policy file, as ``status`` names it; None before one is written."""
        named = self.status()["policy"].get("file")
        return Path(named) if isinstance(named, str) else None

    # --- pairing ---------------------------------------------------------------------------

    def pair(self, lucille: Lucille) -> None:
        """Pair with ``lucille`` through the real flow, and confirm the SAS."""
        pair = _spawn.start_host(
            [
                "pair",
                "begin",
                "--json",
                "--hub",
                lucille.url,
                "--store-file",
                f"--test-key={lucille.signer.test_key}",
            ],
            env=self.env,
            cwd=self.folder,
        )
        self._past.append(pair)
        try:
            pair.write(b"telegram\nada\n")

            def shown_code() -> str | None:
                if pair.proc.poll() not in (None, 0):
                    raise HarnessError(f"pair begin failed\n{pair.describe()}")
                for found in pair.objects():
                    if found.get("type") == "connect" and found.get("state") == "code":
                        return str(found["code"])
                return None

            code = until(shown_code, "pair begin showing its connect code")
            try:
                offset = lucille.deliver(code, sender="telegram:123456789", handle="@ada")
            except NoRequest as error:
                raise HarnessError(f"Lucille found no request: {error}\n{outline(pair)}") from None
            collected: list[str] = []

            def answered() -> bool:
                if pair.proc.poll() not in (None, 0):
                    raise HarnessError(f"pair begin failed\n{outline(pair)}")
                collected[:] = [lucille.collect()]
                return collected[0] == "paired"

            def how_far() -> str:
                state = collected[0] if collected else "never collected"
                return (
                    f"{outline(pair)}\nLucille: delivered in the drop room of epoch {offset:+d} "
                    f"of its clock; its pairing is {state}"
                )

            until(answered, "Lucille collecting the host's answer", detail=how_far)
            if pair.wait(WAIT) != 0:
                raise HarnessError(f"pair begin did not end well\n{pair.describe()}")
            shown = pair.objects()[-1]
            if shown.get("state") != "sas_pending" or shown.get("sas") != lucille.sas():
                raise HarnessError(f"the host showed {shown!r}, Lucille holds {lucille.sas()!r}")
        finally:
            pair.stop()
        confirmed = self.cli("pair", "confirm")
        if confirmed[-1].get("state") != "confirmed":
            raise HarnessError(f"pair confirm wrote {confirmed!r}")

    # --- run -------------------------------------------------------------------------------

    def start_run(self, env: Mapping[str, str] | None = None) -> _spawn.Child:
        """Start ``run --json-events --exit-on-stdin-eof``, with ``env`` added to its
        environment, and wait for its ``ready``."""
        if self.run is not None:
            raise HarnessError("run is already started")
        run = _spawn.start_host(
            ["run", "--json-events", "--exit-on-stdin-eof"],
            env={**self.env, **(env or {})},
            cwd=self.folder,
        )
        self.run = run

        def ready() -> bool:
            if not run.alive():
                raise HarnessError(f"run exited\n{run.describe()}")
            return any(found.get("type") == "ready" for found in run.objects())

        until(ready, "run writing ready")
        return run

    def stop_run(self) -> int | None:
        """Close ``run``'s standard input, which drains it; its exit code."""
        run, self.run = self.run, None
        if run is None:
            return None
        self._past.append(run)
        run.close_stdin()
        code = run.wait(WAIT)
        run.stop()
        return code

    def restart(self, env: Mapping[str, str] | None = None) -> _spawn.Child:
        """Stop ``run`` and start it again, with ``env`` added to its environment. Its events
        start over: :meth:`events` reads the new one's."""
        self.stop_run()
        return self.start_run(env)

    @property
    def pid(self) -> int:
        """The process id of the running ``run``."""
        if self.run is None:
            raise HarnessError("run is not started")
        return self.run.proc.pid

    def events(self) -> list[dict[str, Any]]:
        """What the running ``run`` has written so far."""
        if self.run is None:
            raise HarnessError("run is not started")
        if not self.run.alive():
            raise HarnessError(f"run exited\n{self.run.describe()}")
        return self.run.objects()

    def describe(self) -> str:
        """What a failure shows of the running ``run``."""
        return "" if self.run is None else self.run.describe()

    def close(self) -> None:
        """Stop every process this host started."""
        self.stop_run()
        for child in self._past:
            child.stop()
