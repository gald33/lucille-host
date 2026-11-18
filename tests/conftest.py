"""Shared fixtures: a private home, a host context built in it, and a keyring in memory.

No test reaches the system keyring: every test starts with keyring's ``fail`` backend in place,
which stores nothing, and a test that needs a keyring asks for :func:`memory_keyring` (or
:func:`host_env`, which includes it). A test puts its backend where keyring keeps the one in use,
``keyring.core._keyring_backend``. The host never asks keyring for that one: it builds its own
system's backend itself (``store._system_backend``), so here that function is replaced by one that
returns what the test put there. The host keeps the pair only in the one backend it knows on each
system (``store.KNOWN_BACKENDS``); in the tests, ``MemoryKeyring`` and every subclass of it is taken
as that one too, through ``store._known``, never by widening the list. A test of the rule itself
uses a backend that is not a ``MemoryKeyring``, and ``tests/test_keyring_backend.py`` runs the
host's own ``store._system_backend``, in a process with none of this.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable
from pathlib import Path
from types import MappingProxyType

import pytest
from keyring import core as keyring_core
from keyring.backends.fail import Keyring as FailKeyring

from lucille_host import paths, policy, store
from lucille_host.audit import AuditLog
from lucille_host.controls import Controls
from lucille_host.jobs import Context
from lucille_host.ops import OPERATIONS
from tests._host import HostEnv, MakeContext
from tests._keyring import MemoryKeyring

# The non-root user id the host sees under host_env: the tests may run as root, which the host
# refuses.
TEST_UID = 1000


@pytest.fixture(autouse=True)
def _no_system_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    # keyring's own off switch, which the host honours where it builds its own backend
    # (store._build): a setting in the environment that runs the tests would otherwise turn off the
    # keyring of every test that builds it. A test of the off switch sets it.
    monkeypatch.delenv("PYTHON_KEYRING_BACKEND", raising=False)
    monkeypatch.setattr("keyring.core._keyring_backend", FailKeyring())
    monkeypatch.setattr(store, "_system_backend", lambda: keyring_core._keyring_backend)
    known = store._known
    monkeypatch.setattr(
        store, "_known", lambda backend: isinstance(backend, MemoryKeyring) or known(backend)
    )


@pytest.fixture
def memory_keyring(monkeypatch: pytest.MonkeyPatch) -> MemoryKeyring:
    """A keyring in memory, in place of the system's, for this test."""
    backend = MemoryKeyring()
    monkeypatch.setattr("keyring.core._keyring_backend", backend)
    return backend


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private ``$HOME`` with no XDG overrides, so every file the host writes lands under it."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    return home


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    return paths.ensure_dir(tmp_path / "state")


@pytest.fixture
def make_context(tmp_path: Path, state_dir: Path) -> MakeContext:
    """Build a Context: the ops named enabled, each root alias mapped to a fresh folder (or to the
    path given), a real audit log and controls in ``state_dir``, which is the host's own folder."""

    def make(
        enabled: Iterable[str] = ("host.describe",),
        roots: Iterable[str] | dict[str, Path] = (),
        *,
        log_results: bool = False,
        audit: AuditLog | None = None,
        interpreter: str | None = sys.executable,
        environ: dict[str, str] | None = None,
    ) -> Context:
        if isinstance(roots, dict):
            root_map = {alias: policy.Root(path=str(path)) for alias, path in roots.items()}
        else:
            root_map = {}
            for alias in roots:
                folder = tmp_path / "roots" / alias
                folder.mkdir(parents=True, exist_ok=True)
                root_map[alias] = policy.Root(path=str(folder))
        stated = policy.Policy(
            enabled=MappingProxyType({name: OPERATIONS[name].definition_hash for name in enabled}),
            roots=MappingProxyType(root_map),
            log_results=log_results,
        )
        family = paths.os_family()
        eff = policy.effective(stated, family)
        return Context(
            os_family=family,
            policy=eff,
            policy_digest=policy.digest(eff, policy.digest_key(state_dir)),
            controls=Controls(state_dir),
            audit=audit if audit is not None else AuditLog(state_dir),
            own_dirs=(str(state_dir),),
            interpreter=interpreter,
            state_dir=str(state_dir),
            profile="default",
            environ=environ if environ is not None else {},
        )

    return make


@pytest.fixture
def host_env(home: Path, memory_keyring: MemoryKeyring, monkeypatch: pytest.MonkeyPatch) -> HostEnv:
    """What the host needs to start in-process: a private home, a keyring in memory, no
    ``SWITCHBOARD_*`` variable, no ``XDG_RUNTIME_DIR`` (the host sets its own, and the variable
    is put back after the test), and a user id that is not root."""
    for name in list(os.environ):
        if name.startswith("SWITCHBOARD_"):
            monkeypatch.delenv(name)
    # Recorded first, so that the value the host sets is undone after the test.
    monkeypatch.setenv("XDG_RUNTIME_DIR", "")
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    monkeypatch.setattr("os.geteuid", lambda: TEST_UID)
    return HostEnv(home=home, dirs=paths.dirs(), keyring=memory_keyring)
