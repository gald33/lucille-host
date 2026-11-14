"""The keyring backend a real host gets, in a process with nothing of the tests in it.

Every other test replaces the host's keyring backend (tests/conftest.py), so none of them runs what
a host runs the first time it opens its keyring store. This one does: ``store.check_backend()``, the
call the host makes when it opens the store (``_switchboard.open_link``), in a fresh interpreter
started with ``-I``, an empty ``$HOME`` and only ``PATH`` in its environment (so no session bus
address), with an audit hook added before anything else is imported; then again with keyring's own
settings, in ``PYTHON_KEYRING_BACKEND`` or in ``keyringrc.cfg``: its off switch, which the host
honours, and another backend, which it does not consult.
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
from tests._keyring import (
    DISABLED,
    FAIL_BACKEND,
    NULL_BACKEND,
    NULL_SENTENCE,
    keyring_config,
    planted_module,
)

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
path = list(sys.path)
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
            "keyrings": sorted(name for name in sys.modules if name.split(".")[0] == "keyrings"),
            "path_changed": sys.path != path,
        }
    )
)
"""

# A backend somebody could name in keyring's settings, which is not its off switch: keyrings.alt's,
# which writes secrets unencrypted to a file. It is planted in the folder keyringrc.cfg's
# keyring-path names, where keyring would look for it first.
PLAINTEXT = "keyrings.alt.file.PlaintextKeyring"
# Each setting: PYTHON_KEYRING_BACKEND, and what keyringrc.cfg holds ("{path}" is the planted
# folder), or None; and whether it turns the keyring off.
SETTINGS: dict[str, tuple[str | None, str | None, bool]] = {
    "none": (None, None, False),
    "variable-null": (NULL_BACKEND, None, True),
    "variable-fail": (FAIL_BACKEND, None, True),
    # What `keyring --disable` writes.
    "configuration-null": (None, DISABLED, True),
    "configuration-other": (
        None,
        f"[backend]\ndefault-keyring={PLAINTEXT}\nkeyring-path={{path}}\n",
        False,
    ),
}


@pytest.mark.parametrize("setting", SETTINGS)
def test_opening_the_keyring_store_starts_no_process(setting: str, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    marker = tmp_path / "imported"
    planted = planted_module(tmp_path / "planted", "keyrings.alt.file", marker)
    variable, configuration, off = SETTINGS[setting]
    env = {"HOME": str(home), "PATH": os.defpath}
    if variable is not None:
        env["PYTHON_KEYRING_BACKEND"] = variable
    if configuration is not None:
        # Where keyring reads it on both systems, with XDG_CONFIG_HOME unset.
        config = keyring_config(home)
        config.parent.mkdir(parents=True)
        config.write_text(configuration.format(path=planted), "utf-8")
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
    # With no setting, keyring's detection started ldconfig, gcc and ld here on Linux, three times
    # each: it imported its macOS backend, whose ctypes.util.find_library calls run them. On macOS,
    # find_library starts nothing, so this holds there with or without the host building its
    # backend itself. keyring's off switch starts nothing either.
    assert seen["started"] == []
    family = paths.os_family()
    if off:
        # keyring's null backend, refused as storing nothing on both systems, with the sentence
        # that points to --store-file, before the host imports its own system's backend module.
        assert [seen["built"], seen["refusal"]] == [None, NULL_SENTENCE]
        assert SYSTEM_MODULES[family] not in seen["modules"]
    elif family == "macos":
        # The backend the Mac app keeps the pair in, which nothing else in CI builds the host's way.
        assert [seen["built"], seen["refusal"]] == [[SYSTEM_MODULES["macos"], "Keyring"], None]
        assert SYSTEM_MODULES[family] in seen["modules"]
    else:
        # The Secret Service needs a session bus, and there is none here: refused as keyring's
        # fail backend always was, with the sentence the golden fixture pins.
        assert [seen["built"], seen["refusal"]] == [None, FAIL_SENTENCE]
        assert SYSTEM_MODULES[family] in seen["modules"]
    # keyring never chose a backend, so it loaded no entry point: of its backends, at most this
    # system's own module was imported. Nothing a setting named was imported, the planted module
    # is not, and the folder keyringrc.cfg's keyring-path names was not put on sys.path.
    assert seen["detected"] is False
    others = OTHER_MODULES | (set(SYSTEM_MODULES.values()) - {SYSTEM_MODULES[family]})
    assert set(seen["modules"]) & others == set()
    assert seen["keyrings"] == []
    assert not marker.exists()
    assert seen["path_changed"] is False
