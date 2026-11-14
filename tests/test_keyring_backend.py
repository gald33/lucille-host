"""The keyring backend a real host gets, in a process with nothing of the tests in it.

Every other test replaces the host's keyring backend (tests/conftest.py), so none of them runs what
a host runs the first time it opens its keyring store. This one does: ``store.check_backend()``, the
call the host makes when it opens the store (``_switchboard.open_link``), in a fresh interpreter
started with ``-I``, an empty ``$HOME`` and only ``PATH`` in its environment (so no session bus
address), with an audit hook added before anything else is imported; then again with keyring's own
setting naming another backend, in ``PYTHON_KEYRING_BACKEND`` or in ``keyringrc.cfg``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from lucille_host import paths, store
from tests import _audit_hook

SOURCE = Path(store.__file__).resolve().parents[1]
# What every command prints where there is no keyring to store in, as the golden fixture pins it.
_FIXTURE = SOURCE / "lucille_host" / "spec" / "cli" / "v1" / "fixtures" / "status-unreadable.json"
FAIL_SENTENCE = json.loads(_FIXTURE.read_text(encoding="utf-8"))["stdout"][0]["pair"]["message"]
# The module of each backend that keyring 25.7.0 names by an entry point, apart from its chain.
# keyring's detection imports every one of them; the host imports its own system's alone.
SYSTEM_MODULES = {"linux": "keyring.backends.SecretService", "macos": "keyring.backends.macOS"}
OTHER_MODULES = {
    "keyring.backends.kwallet",
    "keyring.backends.libsecret",
    "keyring.backends.Windows",
}

# Run as ``python -I -c CHILD <source folder> <spawn events as JSON>``.
CHILD = """\
import json
import sys

SPAWNS = frozenset(json.loads(sys.argv[2]))
started = []


def hook(event, args):
    if event in SPAWNS:
        started.append([event, repr(args)])


sys.addaudithook(hook)
sys.path.insert(0, sys.argv[1])
from lucille_host import store

try:
    backend = store.check_backend()
except store.InsecureKeyring as refused:
    built, refusal = None, str(refused)
else:
    built, refusal = [type(backend).__module__, type(backend).__qualname__], None
from keyring import core

print(
    json.dumps(
        {
            "started": started,
            "built": built,
            "refusal": refusal,
            "detected": core._keyring_backend is not None,
            "modules": sorted(name for name in sys.modules if name.startswith("keyring.")),
        }
    )
)
"""


# keyring's null backend, which stores nothing: what each setting below names. keyring would build
# it, and the host would refuse it with its own sentence ("stores nothing"), if keyring were asked.
NULL = "keyring.backends.null.Keyring"


@pytest.mark.parametrize("setting", ["none", "variable", "configuration"])
def test_opening_the_keyring_store_starts_no_process(setting: str, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    env = {"HOME": str(home), "PATH": os.defpath}
    if setting == "variable":
        env["PYTHON_KEYRING_BACKEND"] = NULL
    elif setting == "configuration":
        # Where keyring reads it on both systems, with XDG_CONFIG_HOME unset.
        config = home / ".config" / "python_keyring"
        config.mkdir(parents=True)
        (config / "keyringrc.cfg").write_text(f"[backend]\ndefault-keyring={NULL}\n", "utf-8")
    spawns = json.dumps(sorted(_audit_hook.SPAWN_EVENTS))
    done = subprocess.run(
        [sys.executable, "-I", "-c", CHILD, str(SOURCE), spawns],
        capture_output=True,
        text=True,
        env=env,
        cwd=home,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    seen = json.loads(done.stdout.splitlines()[-1])
    # With neither setting, keyring's detection started ldconfig, gcc and ld here on Linux, three
    # times each: it imported its macOS backend, whose ctypes.util.find_library calls run them. On
    # macOS, find_library starts nothing, so this holds there with or without the host building its
    # backend itself.
    assert seen["started"] == []
    family = paths.os_family()
    if family == "macos":
        # The backend the Mac app keeps the pair in, which nothing else in CI builds the host's way.
        assert [seen["built"], seen["refusal"]] == [[SYSTEM_MODULES["macos"], "Keyring"], None]
    else:
        # The Secret Service needs a session bus, and there is none here: refused as keyring's
        # fail backend always was, with the sentence the golden fixture pins.
        assert [seen["built"], seen["refusal"]] == [None, FAIL_SENTENCE]
    # keyring never chose a backend, so it read neither setting and loaded no entry point: of its
    # backends, only this system's own module was imported.
    assert seen["detected"] is False
    imported = set(seen["modules"])
    assert SYSTEM_MODULES[family] in imported
    others = OTHER_MODULES | (set(SYSTEM_MODULES.values()) - {SYSTEM_MODULES[family]})
    assert imported & others == set()
