"""What this release builds on and still leaves to later ones: every part of Switchboard the host
may use exists in the pinned library, and the conformance suite has no scenarios yet."""

from __future__ import annotations

import subprocess
import sys

from switchboard import client, config, crypto, pairing, sharing, signing

from lucille_host import _switchboard
from tools.guards.switchboard_lint import SURFACE


def test_switchboard_surface_names_exist_in_the_pinned_library() -> None:
    # The managed hub's published token is sent to this URL only, byte for byte.
    assert _switchboard.LUCILLE_HUB == "https://switchboard.lucille-ai.com"
    assert config.MANAGED_HUB_URL == _switchboard.LUCILLE_HUB
    # Every member the Switchboard-surface lint lets the runtime name.
    modules = {
        "client": client,
        "config": config,
        "crypto": crypto,
        "pairing": pairing,
        "sharing": sharing,
        "signing": signing,
    }
    assert set(modules) == set(SURFACE)
    for name, members in SURFACE.items():
        for member in sorted(members):
            assert hasattr(modules[name], member), f"switchboard.{name}.{member}"
    # And the methods the host calls on what those give it, which the lint does not follow.
    for owner, method in [
        (pairing.PairingCode, "decode"),
        (pairing.PairRecord, "to_json"),
        (pairing.PairRecord, "from_json"),
        (pairing.PendingHandshake, "to_json"),
        (pairing.PendingHandshake, "from_json"),
        (client.Client, "inbox"),
        (client.Client, "post"),
        (client.Client, "read_channels"),
        (signing.SigningIdentity, "generate"),
    ]:
        assert callable(getattr(owner, method)), f"{owner.__name__}.{method}"


def test_conformance_suite_without_scenarios_fails() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "lucille_host.conformance"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "no conformance scenarios" in result.stderr
