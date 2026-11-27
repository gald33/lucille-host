"""The conformance suite ships in the wheel, and ``[conformance]`` installs what it needs.

This test builds the sdist and the wheel from this checkout with ``uv build`` (the wheel is built
from the sdist, as a release builds it), and checks:

- that every file of ``src/lucille_host`` is in the wheel, the conformance package and the spec
  data among them, and nothing of it is missing or added;
- that the wheel's metadata declares the ``conformance`` extra as decisions/0001 says
  ("Packaging"): ``agent-switchboard[server]`` at the runtime pin, and ``starlette>=1.9``;
- that ``lucille-host[conformance]``, installed from that wheel into a fresh virtual environment,
  imports ``lucille_host.conformance`` from that install (never from this source tree), and brings
  the in-process ``switchboard.testing`` hub with a starlette of 1.9 or later.

The install resolves the extra within this repository's lock (``uv export`` of the lock, used as
constraints), so it installs the versions CI tests, from uv's cache where it has them. It needs
``uv`` on ``PATH``, as the development setup does.
"""

from __future__ import annotations

import email
import json
import os
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = REPO_ROOT / "src" / "lucille_host"
MARKER = "CONFORMANCE-PROBE:"

# Run in the fresh environment's interpreter, in isolated mode, so nothing of this checkout is on
# the import path.
PROBE = "\n".join(
    [
        "import json, sys",
        "from importlib.metadata import version",
        "import lucille_host.conformance as conformance",
        "from switchboard.testing import hub",
        "import fastapi, uvicorn",
        "print(" + repr(MARKER) + " + json.dumps({",
        "    'conformance_file': conformance.__file__,",
        "    'has_main': callable(conformance.main),",
        "    'hub_callable': callable(hub),",
        "    'starlette': version('starlette'),",
        "    'switchboard': version('agent-switchboard'),",
        "    'prefix': sys.prefix,",
        "}))",
    ]
)


def _run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    done = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False, timeout=900)
    assert done.returncode == 0, f"{argv[:3]} failed:\n{done.stdout}\n{done.stderr}"
    return done


def _uv() -> str:
    found = shutil.which("uv") or os.environ.get("UV")
    if not found:
        pytest.fail("this test builds and installs the wheel with uv, and uv is not on PATH")
    return found


def _version_tuple(text: str) -> tuple[int, ...]:
    parts = []
    for piece in text.split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    work = tmp_path_factory.mktemp("conformance-packaging")
    uv = _uv()
    dist = work / "dist"
    _run([uv, "build", "--out-dir", str(dist), str(REPO_ROOT)], work)
    wheels = sorted(dist.glob("*.whl"))
    assert len(wheels) == 1, wheels
    with zipfile.ZipFile(wheels[0]) as wheel:
        names = set(wheel.namelist())
        metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
        metadata = email.message_from_bytes(wheel.read(metadata_name))
    return {"work": work, "wheel": wheels[0], "names": names, "metadata": metadata}


@pytest.fixture(scope="module")
def installed(built: dict[str, Any]) -> dict[str, Any]:
    work: Path = built["work"]
    uv = _uv()
    constraints = work / "constraints.txt"
    _run(
        [
            uv,
            "export",
            "--frozen",
            "--no-dev",
            "--no-hashes",
            "--no-emit-project",
            "--extra",
            "conformance",
            "--project",
            str(REPO_ROOT),
            "--output-file",
            str(constraints),
        ],
        work,
    )
    venv = work / "venv"
    _run([uv, "venv", "--python", sys.executable, str(venv)], work)
    python = venv / "bin" / "python"
    requirement = f"lucille-host[conformance] @ {built['wheel'].resolve().as_uri()}"
    _run(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(python),
            "--constraint",
            str(constraints),
            requirement,
        ],
        work,
    )
    done = _run([str(python), "-I", "-c", PROBE], work)
    line = next(line for line in done.stdout.splitlines() if line.startswith(MARKER))
    report = json.loads(line[len(MARKER) :])
    report["venv"] = str(venv.resolve())
    return report


def _source_files() -> set[str]:
    found = set()
    for path in PACKAGE_DIR.rglob("*"):
        if path.is_dir() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        found.add(path.relative_to(PACKAGE_DIR.parent).as_posix())
    return found


def test_the_wheel_holds_every_file_of_the_package(built: dict[str, Any]) -> None:
    shipped = {name for name in built["names"] if name.startswith("lucille_host/")}
    assert shipped == _source_files()


def test_the_wheel_holds_the_conformance_package_and_the_spec_data(
    built: dict[str, Any],
) -> None:
    names = built["names"]
    assert "lucille_host/conformance/__init__.py" in names
    assert "lucille_host/conformance/__main__.py" in names
    for prefix in ("lucille_host/spec/v1/", "lucille_host/spec/cli/v1/"):
        assert any(name.startswith(prefix) and name.endswith(".json") for name in names), prefix


def _requires(metadata: Any, extra: str | None) -> set[str]:
    found = set()
    for line in metadata.get_all("Requires-Dist") or []:
        requirement, _, marker = line.partition(";")
        marker = marker.strip()
        if extra is None and not marker:
            found.add(requirement.strip())
        elif extra is not None and marker in (f'extra == "{extra}"', f"extra == '{extra}'"):
            found.add(requirement.strip())
    return found


def test_the_conformance_extra_is_switchboards_server_at_the_runtime_pin(
    built: dict[str, Any],
) -> None:
    metadata = built["metadata"]
    assert "conformance" in (metadata.get_all("Provides-Extra") or [])
    runtime = {r for r in _requires(metadata, None) if r.startswith("agent-switchboard")}
    assert runtime == {"agent-switchboard==2.6.*"}
    pin = runtime.pop().removeprefix("agent-switchboard")
    assert _requires(metadata, "conformance") == {
        f"agent-switchboard[server]{pin}",
        "starlette>=1.9",
    }


def test_pyproject_declares_the_same_extra() -> None:
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    runtime = [d for d in project["dependencies"] if d.startswith("agent-switchboard")]
    assert runtime == ["agent-switchboard==2.6.*"]
    pin = runtime[0].removeprefix("agent-switchboard")
    assert project["optional-dependencies"]["conformance"] == [
        f"agent-switchboard[server]{pin}",
        "starlette>=1.9",
    ]


def test_the_extra_installs_and_imports_from_the_wheel(installed: dict[str, Any]) -> None:
    venv = Path(installed["venv"])
    loaded_from = Path(installed["conformance_file"]).resolve()
    assert loaded_from.is_relative_to(venv)
    assert not loaded_from.is_relative_to(REPO_ROOT)
    assert Path(installed["prefix"]).resolve() == venv
    assert installed["has_main"] is True
    assert installed["hub_callable"] is True
    assert installed["switchboard"].startswith("2.6.")
    assert _version_tuple(installed["starlette"]) >= (1, 9)
