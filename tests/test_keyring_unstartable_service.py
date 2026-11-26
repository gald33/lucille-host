"""A Secret Service the session bus lists, and that fails to start: a store error the host can fall
back from, not ``internal`` (issue 38).

A private ``dbus-daemon`` lists ``org.freedesktop.secrets`` as a service it starts on demand, and
starting it either runs ``/bin/false``, which exits, or names a program that does not exist. The
host's check of the Secret Service only asks the bus whether the name is owned or can be started,
so it passes; the first read starts the service, and the bus replies
``org.freedesktop.DBus.Error.Spawn.ChildExited``, or ``Spawn.ExecFailed``, which secretstorage
turns into its own error and keyring wraps in its ``InitError``. Each runs in a fresh interpreter,
started with ``-I``, an empty ``$HOME`` and only ``PATH`` and the bus's address in its
environment:

- the host's own backend is built and checked, and a read of the pair item is
  :class:`store.KeyringUnusable`, naming the bus's error;
- the commands: ``status`` reports the pair unreadable with ``store_error``; ``pair begin``
  gives ``store_error``; with ``--store-file`` it falls back to the file store, says so, and reaches
  the question of the chat service.

Linux only, and only where ``dbus-daemon`` is installed. These containers run as root, which the
host refuses; the child then stands in for the host's own root check only, never for the uid the
bus is told, which jeepney reads itself.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from lucille_host import paths, store

SOURCE = Path(store.__file__).resolve().parents[1]
DAEMON = shutil.which("dbus-daemon")
CHILD_LIMIT = 60.0

pytestmark = [
    pytest.mark.skipif(
        paths.os_family() != "linux", reason="the Secret Service path is the host's on Linux only"
    ),
    pytest.mark.skipif(DAEMON is None, reason="needs dbus-daemon"),
]

SERVICE = """\
[D-BUS Service]
Name=org.freedesktop.secrets
Exec={exec_line}
"""

# Each way the service fails to start: what Exec names, and what the store says of the first read.
STARTS = [
    pytest.param(
        "/bin/false",
        "DBusErrorResponse: org.freedesktop.DBus.Error.Spawn.ChildExited",
        id="exits",
    ),
    pytest.param(
        "/nonexistent/secret-service",
        "InitError, from DBusErrorResponse: org.freedesktop.DBus.Error.Spawn.ExecFailed",
        id="missing",
    ),
]

# The stock session bus's policy (dbus 1.14's session.conf), on a socket and services of its own.
CONFIG = """\
<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:path={socket}</listen>
  <auth>EXTERNAL</auth>
  <servicedir>{services}</servicedir>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""

# Run as ``python -I -c COMMAND <source folder> <argument>...``: the host's commands, in process,
# with standard input from ``input=``. Only the host's own root check is stood in for, as root.
COMMAND = """\
import os
import sys

sys.path.insert(0, sys.argv[1])
from lucille_host import cli, state
from lucille_host.cli import main

if os.geteuid() == 0:
    # Not os.geteuid: the D-Bus sign-in under test reads it too.
    state.startup_checks = lambda environ=None: None
    cli._is_root = lambda: False
sys.exit(main(sys.argv[2:]))
"""

# Run as ``python -I -c CHILD <source folder> <state folder>``.
CHILD = """\
import json
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from lucille_host import store

backend = store.check_backend()
seen = {"backend": type(backend).__qualname__}
try:
    store.Item(Path(sys.argv[2]), "default").read()
except store.StoreError as exc:
    seen["error"] = type(exc).__name__
    seen["message"] = str(exc)
print(json.dumps(seen))
"""


@pytest.fixture
def unstartable_bus(request: pytest.FixtureRequest) -> Iterator[str]:
    """The address of a session bus that lists the Secret Service, which fails to start by running
    what ``request.param`` names."""
    assert DAEMON is not None
    # A short folder of its own: a unix socket's path is limited to about 100 bytes.
    folder = Path(tempfile.mkdtemp(prefix="lh-bus-"))
    services = folder / "services"
    services.mkdir()
    (services / "org.freedesktop.secrets.service").write_text(
        SERVICE.format(exec_line=request.param)
    )
    socket = folder / "bus"
    config = folder / "session.conf"
    config.write_text(CONFIG.format(socket=socket, services=services))
    daemon = subprocess.Popen(
        [DAEMON, f"--config-file={config}", "--nofork", "--print-address"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        assert daemon.stdout is not None
        address = daemon.stdout.readline().strip()
        assert address.startswith("unix:"), "dbus-daemon gave no address"
        yield address
    finally:
        daemon.terminate()
        daemon.wait(10)
        shutil.rmtree(folder, ignore_errors=True)


def _child_env(bus: str, home: Path) -> dict[str, str]:
    home.mkdir(exist_ok=True)
    return {"HOME": str(home), "PATH": os.defpath, "DBUS_SESSION_BUS_ADDRESS": bus}


def _run(
    code: str, *argv: str, env: dict[str, str], stdin: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", "-c", code, str(SOURCE), *argv],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=CHILD_LIMIT,
        check=False,
    )


@pytest.mark.parametrize(("unstartable_bus", "detail"), STARTS, indirect=["unstartable_bus"])
def test_a_listed_secret_service_that_fails_to_start_is_a_store_error(
    unstartable_bus: str, detail: str, tmp_path: Path
) -> None:
    pytest.importorskip("secretstorage")
    state = tmp_path / "state"
    state.mkdir()
    done = _run(CHILD, str(state), env=_child_env(unstartable_bus, tmp_path / "home"))
    assert done.returncode == 0, done.stderr
    seen = json.loads(done.stdout)
    # The check passes: the bus lists the name, and nothing asks it to start the service.
    assert seen["backend"] == "Keyring"
    assert seen.get("error") == "KeyringUnusable", seen
    assert seen["message"] == f"the keyring could not be read ({detail})"


@pytest.mark.parametrize(("unstartable_bus", "detail"), STARTS, indirect=["unstartable_bus"])
def test_the_commands_on_a_listed_secret_service_that_fails_to_start(
    unstartable_bus: str, detail: str, tmp_path: Path
) -> None:
    pytest.importorskip("secretstorage")
    message = f"the keyring could not be read ({detail})"
    env = _child_env(unstartable_bus, tmp_path / "home")
    # status reports the pair it cannot read, and the rest.
    done = _run(COMMAND, "--json", "status", env=env)
    assert done.returncode == 0, done.stderr
    found = json.loads(done.stdout)
    assert found["pair"] == {"state": "unreadable", "code": "store_error", "message": message}
    # pair begin, without --store-file: the store error, and nothing else.
    done = _run(COMMAND, "--json", "pair", "begin", env=env, stdin="telegram\nada\n")
    assert done.returncode == 1, done.stderr
    assert json.loads(done.stdout.splitlines()[-1]) == {
        "v": 1,
        "type": "error",
        "code": "store_error",
        "message": message,
    }
    assert "Traceback" not in done.stderr
    # With --store-file it falls back, says so and why, and reaches the question of the chat
    # service, which it refuses here before it reaches any hub.
    done = _run(
        COMMAND, "--json", "pair", "begin", "--store-file", env=env, stdin="not-a-service\n"
    )
    assert done.returncode == 1, done.stderr
    assert (
        f"lucille-host: warning: {message}; as --store-file asks, the pair is kept in a file "
        "instead\n"
    ) in done.stderr
    assert json.loads(done.stdout.splitlines()[-1])["code"] == "bad_service"
    assert "Traceback" not in done.stderr
