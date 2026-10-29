"""The schemas and fixtures ship in the wheel, and load from an installed copy of it.

This test builds the sdist and the wheel from this checkout with ``uv build`` (the wheel is built
from the sdist, as a release builds it), installs the wheel with no dependencies into a fresh
virtual environment, and runs ``packaged_probe.py`` there in isolated mode. The probe imports
``lucille_host.spec`` from that install, never from this source tree, and reports what it loads.
The test then checks the report against the source tree, and runs the fixture checks of
``test_fixtures.py`` again on the installed copies.

It needs ``uv`` on ``PATH``, as the development setup does, and the build backend, which uv
fetches or takes from its cache.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

from lucille_host import spec
from tests.spec import _support

REPO_ROOT = Path(__file__).resolve().parents[2]
PROBE = Path(__file__).resolve().with_name("packaged_probe.py")
SOURCE_SPEC = REPO_ROOT / "src" / "lucille_host" / "spec"


def _run(argv: list[str], cwd: Path) -> None:
    done = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False, timeout=600)
    assert done.returncode == 0, f"{argv[:3]} failed:\n{done.stdout}\n{done.stderr}"


def _uv() -> str:
    found = shutil.which("uv") or os.environ.get("UV")
    if not found:
        pytest.fail("this test builds and installs the wheel with uv, and uv is not on PATH")
    return found


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    work = tmp_path_factory.mktemp("packaged")
    uv = _uv()
    dist = work / "dist"
    _run([uv, "build", "--out-dir", str(dist), str(REPO_ROOT)], work)
    wheels = sorted(dist.glob("*.whl"))
    assert len(wheels) == 1, wheels
    venv = work / "venv"
    _run([uv, "venv", "--python", sys.executable, str(venv)], work)
    python = venv / "bin" / "python"
    _run([uv, "pip", "install", "--python", str(python), "--no-deps", str(wheels[0])], work)
    report = work / "report.json"
    _run([str(python), "-I", str(PROBE), str(report)], work)
    with zipfile.ZipFile(wheels[0]) as wheel:
        names = set(wheel.namelist())
    return {
        "venv": venv.resolve(),
        "wheel_names": names,
        "report": json.loads(report.read_text(encoding="ascii")),
    }


def _source_files() -> set[str]:
    root = SOURCE_SPEC / f"v{spec.MAJOR}"
    return {
        f"lucille_host/spec/v{spec.MAJOR}/{path.relative_to(root).as_posix()}"
        for path in root.rglob("*.json")
    }


def test_the_wheel_holds_every_schema_and_fixture(built: dict[str, Any]) -> None:
    shipped = {
        name
        for name in built["wheel_names"]
        if name.startswith(f"lucille_host/spec/v{spec.MAJOR}/")
    }
    assert shipped == _source_files()
    assert "lucille_host/spec/__init__.py" in built["wheel_names"]
    assert len(shipped) > 100


def test_the_installed_package_is_not_the_source_tree(built: dict[str, Any]) -> None:
    loaded_from = Path(built["report"]["spec_dir"]).resolve()
    assert loaded_from.is_relative_to(built["venv"])
    assert not loaded_from.is_relative_to(REPO_ROOT)


def test_installed_files_equal_the_source(built: dict[str, Any]) -> None:
    report = built["report"]
    assert report["schemas"] == {name: spec.load_schema(name) for name in spec.SCHEMA_NAMES}
    for kind in spec.FIXTURE_KINDS:
        source = {path.name: spec.load_fixture(path) for path in spec.fixture_paths(kind)}
        assert report["fixtures"][kind] == source, kind
    assert report["host_state"] == spec.default_host_state()


def test_installed_fixtures_validate_as_labelled(built: dict[str, Any]) -> None:
    report = built["report"]
    errors = {}
    for kind in ("request", "reply", "host.describe"):
        schema = report["schemas"][kind]
        for name, fixture in report["fixtures"][kind].items():
            found = _support.label_errors(kind, fixture, schema)
            if found:
                errors[f"{kind}/{name}"] = found
    assert errors == {}
    for name, fixture in report["fixtures"]["decode"].items():
        raw = base64.b64decode(fixture["raw_base64"])
        assert raw.decode("utf-8", errors="replace").replace("\x00", "") == fixture["text"], name
