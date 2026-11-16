"""A Secret Service the session bus lists, and that fails to start: a store error, not ``internal``
(issue 38).

A private ``dbus-daemon`` lists ``org.freedesktop.secrets`` as a service it starts on demand, and
starting it runs ``/bin/false``. The host's check of the Secret Service only asks the bus whether
the name is owned or can be started, so it passes; the first read starts the service, and the bus
replies ``org.freedesktop.DBus.Error.Spawn.ChildExited``. In a fresh interpreter, started with
``-I``, an empty ``$HOME`` and only ``PATH`` and the bus's address in its environment, the host's
own backend is built and checked, and a read of the pair item must be :class:`store.KeyringUnusable`
naming that error. Linux only, and only where ``dbus-daemon`` is installed.
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
Exec=/bin/false
"""

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
def unstartable_bus() -> Iterator[str]:
    """The address of a session bus that lists the Secret Service, which fails to start."""
    assert DAEMON is not None
    # A short folder of its own: a unix socket's path is limited to about 100 bytes.
    folder = Path(tempfile.mkdtemp(prefix="lh-bus-"))
    services = folder / "services"
    services.mkdir()
    (services / "org.freedesktop.secrets.service").write_text(SERVICE)
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


def test_a_listed_secret_service_that_fails_to_start_is_a_store_error(
    unstartable_bus: str, tmp_path: Path
) -> None:
    pytest.importorskip("secretstorage")
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    env = {"HOME": str(home), "PATH": os.defpath, "DBUS_SESSION_BUS_ADDRESS": unstartable_bus}
    done = subprocess.run(
        [sys.executable, "-I", "-c", CHILD, str(SOURCE), str(state)],
        capture_output=True,
        text=True,
        env=env,
        timeout=CHILD_LIMIT,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    seen = json.loads(done.stdout)
    # The check passes: the bus lists the name, and nothing asks it to start the service.
    assert seen["backend"] == "Keyring"
    assert seen.get("error") == "KeyringUnusable", seen
    assert seen["message"] == (
        "the keyring could not be read "
        "(DBusErrorResponse: org.freedesktop.DBus.Error.Spawn.ChildExited)"
    )
