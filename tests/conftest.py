"""Shared fixtures: a private home, and a host context built in it."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from types import MappingProxyType

import pytest

from lucille_host import paths, policy
from lucille_host.audit import AuditLog
from lucille_host.controls import Controls
from lucille_host.jobs import Context
from lucille_host.ops import OPERATIONS
from tests._host import MakeContext


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
        )

    return make
