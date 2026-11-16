"""A session bus that never answers: the host's keyring check gives up, and refuses, in time.

On Linux the host asks keyring's Secret Service backend whether it can be used here, which
connects to the session bus at ``DBUS_SESSION_BUS_ADDRESS``. jeepney bounds the connect and the
authentication, but sends the bus's ``Hello`` with no timeout, and neither secretstorage nor
keyring lets a caller pass one (issue 39). Here a socket completes the authentication and then
never answers, and ``store.check_backend()`` runs in a fresh interpreter, started with ``-I``, an
empty ``$HOME`` and only ``PATH`` and that address in its environment, as a real host's first look
at its keyring store. It must refuse with ``InsecureKeyring`` and the sentence for a bus that did
not answer, well within the bound; the bound asserted is loose, so a slow machine does not fail it.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from lucille_host import paths, store

SOURCE = Path(store.__file__).resolve().parents[1]
# The longest the child may take, start-up included: far past the host's own deadline, so only a
# check that waits without end fails it.
CHILD_LIMIT = 60.0

pytestmark = pytest.mark.skipif(
    paths.os_family() != "linux", reason="the Secret Service path is the host's on Linux only"
)

# Run as ``python -I -c CHILD <source folder>``.
CHILD = """\
import json
import sys
import time

sys.path.insert(0, sys.argv[1])
from lucille_host import store

started = time.monotonic()
try:
    store.check_backend()
except store.InsecureKeyring as refused:
    refusal = str(refused)
else:
    refusal = None
print(json.dumps({"refusal": refusal, "elapsed": time.monotonic() - started}))
"""


def _serve_silently(server: socket.socket, stop: threading.Event) -> None:
    """Accept each connection, complete D-Bus's EXTERNAL authentication, then read everything
    (``BEGIN``, then the ``Hello``) and never write again."""
    server.settimeout(0.2)
    held: list[socket.socket] = []
    try:
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except TimeoutError:
                continue
            held.append(conn)
            conn.settimeout(5)
            buffer = b""
            while b"\r\n" not in buffer:
                data = conn.recv(4096)
                if not data:
                    break
                buffer += data
            conn.sendall(b"OK 0123456789abcdef0123456789abcdef\r\n")
            conn.setblocking(False)
    finally:
        for conn in held:
            conn.close()


@pytest.fixture
def silent_bus() -> Iterator[str]:
    """The address of a session bus that authenticates and never answers."""
    # A short folder of its own: a unix socket's path is limited to about 100 bytes.
    folder = tempfile.mkdtemp(prefix="lh-bus-")
    path = os.path.join(folder, "bus")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(8)
    stop = threading.Event()
    thread = threading.Thread(target=_serve_silently, args=(server, stop), daemon=True)
    thread.start()
    try:
        yield f"unix:path={path}"
    finally:
        stop.set()
        thread.join(5)
        server.close()
        shutil.rmtree(folder, ignore_errors=True)


def test_a_session_bus_that_never_answers_is_no_keyring(silent_bus: str, tmp_path: Path) -> None:
    pytest.importorskip("secretstorage")
    home = tmp_path / "home"
    home.mkdir()
    env = {"HOME": str(home), "PATH": os.defpath, "DBUS_SESSION_BUS_ADDRESS": silent_bus}
    started = time.monotonic()
    done = subprocess.run(
        [sys.executable, "-I", "-c", CHILD, str(SOURCE)],
        capture_output=True,
        text=True,
        env=env,
        timeout=CHILD_LIMIT,
        check=False,
    )
    took = time.monotonic() - started
    assert done.returncode == 0, done.stderr
    seen = json.loads(done.stdout)
    assert seen["refusal"] is not None, "the check passed on a bus that never answered"
    assert "the session bus did not answer" in seen["refusal"]
    assert "--store-file" in seen["refusal"]
    # The deadline, and more than enough on a slow machine; never the child limit.
    assert store.PROBE_TIMEOUT <= seen["elapsed"] < store.PROBE_TIMEOUT + 20
    assert took < CHILD_LIMIT - 10
