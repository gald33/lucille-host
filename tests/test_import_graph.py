"""The runtime's import graph never reaches the server stack (fastapi, uvicorn).

The ``[conformance]`` extra installs Switchboard's server, which brings fastapi and uvicorn. The
host itself must never load them: a program that opens no listening port has no use for a web
server, and loading one would widen what runs on the user's computer.

This test runs where that extra IS installed, so the server stack is importable and the test
would see it if the runtime reached it. The runtime modules are imported in a fresh interpreter
by explicit ``import`` statements, and a second test checks that the list names every module file
in the runtime package, so a new module cannot slip past it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = REPO_ROOT / "src" / "lucille_host"

# Every module of the runtime package: src/lucille_host/ except src/lucille_host/conformance/.
RUNTIME_MODULES = (
    "lucille_host",
    "lucille_host.__main__",
    "lucille_host._exec",
    "lucille_host._output",
    "lucille_host._switchboard",
    "lucille_host.audit",
    "lucille_host.cli",
    "lucille_host.containment",
    "lucille_host.controls",
    "lucille_host.jobs",
    "lucille_host.mounts",
    "lucille_host.ops",
    "lucille_host.paths",
    "lucille_host.policy",
    "lucille_host.spec",
)
SERVER_PACKAGES = ("fastapi", "uvicorn")
MARKER = "LOADED-MODULES:"


def _python(code: str) -> subprocess.CompletedProcess[str]:
    """Run code in a fresh, isolated interpreter of this environment."""
    return subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=False
    )


def test_server_stack_is_installed_here() -> None:
    result = _python("import fastapi\nimport uvicorn\n")
    assert result.returncode == 0, (
        "fastapi and uvicorn must be importable for this test to mean anything; "
        "install the conformance extra (uv sync --extra conformance).\n" + result.stderr
    )


def test_runtime_imports_never_reach_the_server_stack() -> None:
    code = "\n".join(
        [
            *(f"import {module}" for module in RUNTIME_MODULES),
            "import json, sys",
            f"print({MARKER!r} + json.dumps(sorted(sys.modules)))",
        ]
    )
    result = _python(code)
    assert result.returncode == 0, result.stderr
    line = next(line for line in result.stdout.splitlines() if line.startswith(MARKER))
    loaded = json.loads(line[len(MARKER) :])
    # The graph includes the whole Switchboard surface the host uses.
    for module in ("switchboard.pairing", "switchboard.client", "switchboard.sharing"):
        assert module in loaded
    reached = [name for name in loaded if name.split(".")[0] in SERVER_PACKAGES]
    assert reached == []


def test_module_list_covers_every_runtime_module() -> None:
    # rglob does not descend into a linked directory, so a link would hide modules from this walk.
    # The AST lint refuses every symbolic link in the repository; this walk refuses them too.
    links = [path for path in PACKAGE_DIR.rglob("*") if path.is_symlink()]
    assert links == []
    found = set()
    for path in PACKAGE_DIR.rglob("*.py"):
        parts = list(path.relative_to(PACKAGE_DIR.parent).with_suffix("").parts)
        if parts[1:2] == ["conformance"]:
            continue
        if parts[-1] == "__init__":
            parts.pop()
        found.add(".".join(parts))
    assert found == set(RUNTIME_MODULES)
