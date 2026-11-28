"""release.yml, and tools/release/check_release.py, the checks it runs.

The workflow itself runs only on a pushed ``v*`` tag, so these tests read it: what triggers it,
which job may do what, the order of the jobs, the pins, and that every value from GitHub reaches a
shell through ``env:``. The checks are tested here with PyPI and sigstore stood in for; the one
that verifies an attestation with ``pypi-attestations`` was run against agent-switchboard 2.6.0's
real files, and reports what it saw in the release job's log.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml

from tools.release import check_release as cr

ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / ".github" / "workflows" / "release.yml"
PINNED_ACTIONS = {
    "actions/checkout": "3d3c42e5aac5ba805825da76410c181273ba90b1",
    "astral-sh/setup-uv": "1c37ad07a6a961277cf70c0d37d6f313000f5884",
    "actions/upload-artifact": "cf430e030ddbb5b0abf93d22962f4752f3646cd9",
    "actions/download-artifact": "9000827ccba6bdab643e8b6fd33ac0654aef8333",
    "pypa/gh-action-pypi-publish": "7245906fb61fa33a13554f9d5fb856fe61758086",
}
JOBS = ("build", "conformance-built", "publish", "provenance", "conformance", "github-release")
VERSION = "1.2.3"


def _workflow() -> dict[str, Any]:
    return yaml.safe_load(RELEASE.read_text(encoding="utf-8"))


def _steps(job: str) -> list[dict[str, Any]]:
    return _workflow()["jobs"][job]["steps"]


def _runs(job: str) -> str:
    return "\n".join(step.get("run", "") for step in _steps(job))


def _uses(job: str) -> list[str]:
    return [step["uses"] for step in _steps(job) if "uses" in step]


# --- the workflow ---------------------------------------------------------------------------


def test_it_runs_on_a_pushed_v_tag_and_nothing_else() -> None:
    workflow = _workflow()
    # PyYAML reads the key `on` as True.
    assert workflow[True] == {"push": {"tags": ["v*"]}}
    assert workflow["concurrency"]["cancel-in-progress"] is False


def test_the_jobs_and_their_order() -> None:
    jobs = _workflow()["jobs"]
    assert tuple(jobs) == JOBS
    needs = {name: job.get("needs") for name, job in jobs.items()}
    assert needs == {
        "build": None,
        "conformance-built": "build",
        "publish": ["build", "conformance-built"],
        "provenance": ["build", "publish"],
        "conformance": ["build", "publish"],
        "github-release": ["build", "provenance", "conformance"],
    }


def test_each_job_has_only_the_permissions_it_needs() -> None:
    workflow = _workflow()
    assert workflow["permissions"] == {}
    assert {name: job["permissions"] for name, job in workflow["jobs"].items()} == {
        "build": {"contents": "read"},
        "conformance-built": {},
        "publish": {"id-token": "write"},
        "provenance": {"contents": "read"},
        "conformance": {},
        "github-release": {"contents": "write"},
    }


def test_only_publish_is_in_environment_pypi_and_it_runs_nothing_of_the_repository() -> None:
    jobs = _workflow()["jobs"]
    assert [name for name, job in jobs.items() if "environment" in job] == ["publish"]
    assert jobs["publish"]["environment"]["name"] == "pypi"
    assert not any(uses.startswith("actions/checkout@") for uses in _uses("publish"))
    assert "uv " not in _runs("publish") and "python" not in _runs("publish")
    publish = [s for s in _steps("publish") if s.get("uses", "").startswith("pypa/")]
    assert len(publish) == 1
    assert publish[0]["with"] == {"packages-dir": "release/dist/", "attestations": True}


def test_no_secret_is_named_and_no_value_from_github_is_inside_a_run() -> None:
    text = RELEASE.read_text(encoding="utf-8")
    assert "secrets." not in text
    for name in JOBS:
        for step in _steps(name):
            assert "${{" not in step.get("run", ""), (name, step.get("name"))


def test_every_action_is_one_of_the_pinned_ones() -> None:
    used = {uses for name in JOBS for uses in _uses(name)}
    assert used == {f"{action}@{sha}" for action, sha in PINNED_ACTIONS.items()}


def test_every_checkout_keeps_no_credentials() -> None:
    for name in JOBS:
        for step in _steps(name):
            if step.get("uses", "").startswith("actions/checkout@"):
                assert step["with"]["persist-credentials"] is False


def test_the_build_checks_the_tag_main_and_switchboard_before_it_builds() -> None:
    names = [step.get("name", step.get("uses")) for step in _steps("build")]
    runs = [step.get("run", "") for step in _steps("build")]

    def index(fragment: str) -> int:
        return next(i for i, run in enumerate(runs) if fragment in run)

    tag = index("check_release.py tag")
    main = index("git merge-base --is-ancestor")
    switchboard = index("check_release.py dependency agent-switchboard")
    build = index("uv build")
    sums = index("check_release.py sums")
    assert tag < main < switchboard < build < sums, names
    assert _workflow()["jobs"]["build"]["steps"][0]["with"]["fetch-depth"] == 0
    built = runs[build]
    assert 'SOURCE_DATE_EPOCH=$(git log -1 --format=%ct "$GITHUB_SHA")' in built
    assert "export SOURCE_DATE_EPOCH" in built
    for flag in ("--no-config", "--no-cache", "--sdist", "--wheel", "--require-hashes"):
        assert flag in built
    assert "--build-constraints tools/macos/build-constraints.txt" in built
    assert "rm dist/.gitignore" in built


def test_the_build_constraints_are_pyproject_build_requires_by_hash() -> None:
    requires = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["build-system"][
        "requires"
    ]
    text = (ROOT / "tools" / "macos" / "build-constraints.txt").read_text(encoding="utf-8")
    pinned = re.findall(
        r"^([A-Za-z0-9_.\-]+==[^\s\\]+) \\\n\s+--hash=sha256:[0-9a-f]{64}$", text, re.M
    )
    assert sorted(pinned) == sorted(requires)


def test_publish_and_the_release_check_the_files_against_sha256sums() -> None:
    for name in ("publish", "github-release"):
        assert "sha256sum --check --strict ../sums/SHA256SUMS" in _runs(name)
    # macOS has shasum, not sha256sum.
    assert "shasum -a 256 --check --strict ../sums/SHA256SUMS" in _runs("conformance-built")


def test_the_built_wheel_passes_the_conformance_suite_before_anything_is_published() -> None:
    job = _workflow()["jobs"]["conformance-built"]
    assert job["strategy"]["matrix"]["os"] == ["ubuntu-latest", "macos-latest"]
    assert not any(uses.startswith("actions/checkout@") for uses in _uses("conformance-built"))
    runs = [step.get("run", "") for step in _steps("conformance-built")]
    install = next(run for run in runs if "uv pip install" in run)
    assert "uv venv --no-config verify-env" in install
    assert (
        '"lucille-host[conformance] @ file://$PWD/release/dist/'
        'lucille_host-$VERSION-py3-none-any.whl"' in install
    )
    assert 'test "$(verify-env/bin/lucille-host --version)" = "lucille-host $VERSION"' in runs
    assert runs[-1] == "verify-env/bin/python -m lucille_host.conformance"
    # Nothing reaches PyPI until it has passed.
    assert "conformance-built" in _workflow()["jobs"]["publish"]["needs"]


def test_provenance_runs_the_published_check() -> None:
    runs = _runs("provenance")
    assert "uv sync --locked --no-install-project --no-dev --extra verify" in runs
    assert "check_release.py published release/dist release/sums/SHA256SUMS --version" in runs


def test_conformance_installs_from_pypi_into_a_fresh_environment_and_runs_the_suite() -> None:
    job = _workflow()["jobs"]["conformance"]
    assert job["strategy"]["matrix"]["os"] == ["ubuntu-latest", "macos-latest"]
    assert not any(uses.startswith("actions/checkout@") for uses in _uses("conformance"))
    runs = _runs("conformance")
    assert (
        "uv pip install --no-config --no-cache --python verify-env"
        ' "lucille-host[conformance]==$VERSION"' in runs
    )
    assert 'test "$(verify-env/bin/lucille-host --version)" = "lucille-host $VERSION"' in runs
    assert runs.rstrip().endswith("verify-env/bin/python -m lucille_host.conformance")


@pytest.mark.parametrize("job", ["conformance-built", "conformance"])
def test_conformance_jobs_install_ripgrep_where_files_search_looks(job: str) -> None:
    # The catalog's files.search program on each OS, as ci.yml's test job installs it.
    names = [step.get("name") for step in _steps(job)]
    install = names.index("Install ripgrep where files.search looks for it")
    assert install < names.index("Run the conformance suite")
    runs = _steps(job)[install]["run"]
    # A package source of the image's own that fails does not stop it; the install still must work.
    assert (
        'sudo apt-get update -q || echo "apt-get update failed for a package source; '
        'installing from the lists it fetched"\n'
        "  sudo apt-get install -y -q --no-install-recommends ripgrep\n" in runs
    )
    assert "/usr/bin/rg --version" in runs
    # Without a `brew update` first: the formula of the runner image's own Homebrew, and no minutes
    # spent fetching every formula there is.
    assert "HOMEBREW_NO_AUTO_UPDATE=1 brew install ripgrep" in runs
    assert "/opt/homebrew/bin/rg --version" in runs


def test_the_github_release_carries_the_files_and_sha256sums() -> None:
    runs = _runs("github-release")
    assert 'gh release create "$REF_NAME" --verify-tag' in runs
    assert "release/dist/* release/sums/SHA256SUMS" in runs


# --- tag ------------------------------------------------------------------------------------


def test_the_version_is_read_from_the_package_as_text() -> None:
    import lucille_host

    text = cr.VERSION_FILE.read_text(encoding="utf-8")
    assert cr.package_version(text) == lucille_host.__version__
    with pytest.raises(cr.CheckError):
        cr.package_version(text + '__version__ = "9.9.9"\n')
    with pytest.raises(cr.CheckError):
        cr.package_version("")


@pytest.mark.parametrize("version", ["0.1.0", "1.0.0", "10.20.30"])
def test_a_tag_for_a_final_release_passes(version: str) -> None:
    cr.check_tag(f"v{version}", version)


@pytest.mark.parametrize(
    ("ref", "version"),
    [
        ("v0.1.0", "0.1.0.dev0"),
        ("v0.1.0rc1", "0.1.0rc1"),
        ("v0.1.0.post1", "0.1.0.post1"),
        ("v01.0.0", "01.0.0"),
        ("v0.1", "0.1"),
        ("v0.1.0+local", "0.1.0+local"),
        ("0.1.0", "0.1.0"),
        ("v0.1.1", "0.1.0"),
        ("v0.1.0 ", "0.1.0"),
    ],
)
def test_any_other_tag_is_refused(ref: str, version: str) -> None:
    with pytest.raises(cr.CheckError):
        cr.check_tag(ref, version)


def test_main_tag_refuses_the_development_version(capsys: pytest.CaptureFixture[str]) -> None:
    import lucille_host

    if cr.FINAL_VERSION.fullmatch(lucille_host.__version__):
        pytest.skip("the package is at a final version")
    assert cr.main(["tag", f"v{lucille_host.__version__}"]) == 1
    assert "not a final release" in capsys.readouterr().err


# --- sums -----------------------------------------------------------------------------------


def _dist(tmp_path: Path, version: str = VERSION) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir()
    wheel, sdist = cr.release_files(version)
    (dist / wheel).write_bytes(b"wheel bytes")
    (dist / sdist).write_bytes(b"sdist bytes")
    return dist


def test_sums_are_sha256sum_lines_by_name(tmp_path: Path) -> None:
    dist = _dist(tmp_path)
    text = cr.format_sums(cr.dist_sums(dist, VERSION))
    wheel, sdist = cr.release_files(VERSION)
    assert text == (
        f"{hashlib.sha256(b'wheel bytes').hexdigest()}  {wheel}\n"
        f"{hashlib.sha256(b'sdist bytes').hexdigest()}  {sdist}\n"
    )
    assert cr.check_sums(dist, text, VERSION) == cr.parse_sums(text, VERSION)
    out = tmp_path / "SHA256SUMS"
    assert cr.main(["sums", str(dist), "--version", VERSION, "--out", str(out)]) == 0
    assert out.read_text(encoding="utf-8") == text
    assert cr.main(["check-sums", str(dist), str(out), "--version", VERSION]) == 0


@pytest.mark.parametrize("change", ["extra", "missing", "symlink", "folder", "other-version"])
def test_a_dist_folder_holds_exactly_the_two_files(tmp_path: Path, change: str) -> None:
    dist = _dist(tmp_path)
    wheel, sdist = cr.release_files(VERSION)
    if change == "extra":
        (dist / ".gitignore").write_text("*\n")
    elif change == "missing":
        (dist / sdist).unlink()
    elif change == "symlink":
        (dist / sdist).unlink()
        (tmp_path / "elsewhere").write_bytes(b"sdist bytes")
        (dist / sdist).symlink_to(tmp_path / "elsewhere")
    elif change == "folder":
        (dist / sdist).unlink()
        (dist / sdist).mkdir()
    else:
        with pytest.raises(cr.CheckError):
            cr.dist_sums(dist, "1.2.4")
        return
    with pytest.raises(cr.CheckError):
        cr.dist_sums(dist, VERSION)


def test_a_changed_file_fails_check_sums(tmp_path: Path) -> None:
    dist = _dist(tmp_path)
    text = cr.format_sums(cr.dist_sums(dist, VERSION))
    (dist / cr.release_files(VERSION)[0]).write_bytes(b"other bytes")
    with pytest.raises(cr.CheckError, match="SHA256SUMS says"):
        cr.check_sums(dist, text, VERSION)


@pytest.mark.parametrize(
    "mangle",
    [
        lambda t: t.replace("  ", " ", 1),
        lambda t: t.upper(),
        lambda t: t + t.splitlines(keepends=True)[0],
        lambda t: "".join(reversed(t.splitlines(keepends=True))),
        lambda t: t.rstrip("\n"),
        lambda t: t.splitlines(keepends=True)[0],
        lambda t: t + f"{'0' * 64}  other.whl\n",
        lambda t: t.replace(VERSION, "1.2.4"),
    ],
)
def test_a_sums_file_is_exactly_as_sums_writes_it(tmp_path: Path, mangle: Any) -> None:
    text = cr.format_sums(cr.dist_sums(_dist(tmp_path), VERSION))
    with pytest.raises(cr.CheckError):
        cr.parse_sums(mangle(text), VERSION)


# --- published ------------------------------------------------------------------------------

GOOD_CLAIMS = {
    cr.RUNNER_ENVIRONMENT: "github-hosted",
    cr.SOURCE_REPOSITORY_URI: "https://github.com/gald33/lucille-host",
    cr.SOURCE_REPOSITORY_REF: f"refs/tags/v{VERSION}",
}


class FakePyPI:
    """PyPI's JSON, file and provenance endpoints for one host release, and a stand-in for
    sigstore that accepts an attestation when it names the file's sha256."""

    def __init__(self, dist: Path, version: str = VERSION) -> None:
        self.version = version
        self.files = {p.name: p.read_bytes() for p in sorted(dist.iterdir())}
        self.listing: list[dict[str, Any]] = [
            {
                "filename": name,
                "url": f"https://{cr.FILES_HOST}/packages/xx/{name}",
                "digests": {"sha256": hashlib.sha256(data).hexdigest()},
            }
            for name, data in self.files.items()
        ]
        self.info_version = version
        self.served = dict(self.files)
        self.publisher: dict[str, Any] = {
            "kind": "GitHub",
            "repository": "gald33/lucille-host",
            "workflow": "release.yml",
            "environment": "pypi",
        }
        self.claims: dict[str, str] = dict(GOOD_CLAIMS)
        self.attestations: list[Any] | None = None
        self.bundles: list[Any] | None = None
        self.unlisted_reads = 0
        self.fetched: list[str] = []

    def fetch(self, url: str, headers: Mapping[str, str]) -> bytes:
        self.fetched.append(url)
        if url == f"{cr.PYPI}/pypi/lucille-host/{self.version}/json":
            if self.unlisted_reads:
                self.unlisted_reads -= 1
                return json.dumps({"message": "Not Found"}).encode()
            return json.dumps(
                {"info": {"version": self.info_version}, "urls": self.listing}
            ).encode()
        for name in self.files:
            if url == f"https://{cr.FILES_HOST}/packages/xx/{name}":
                return self.served[name]
            if url == f"{cr.PYPI}/integrity/lucille-host/{self.version}/{name}/provenance":
                assert headers == cr.INTEGRITY_ACCEPT
                digest = hashlib.sha256(self.files[name]).hexdigest()
                attestations = (
                    self.attestations if self.attestations is not None else [{"for": digest}]
                )
                bundles = self.bundles
                if bundles is None:
                    bundles = [{"publisher": self.publisher, "attestations": attestations}]
                return json.dumps({"version": 1, "attestation_bundles": bundles}).encode()
        raise AssertionError(f"unexpected fetch {url}")

    def verify(
        self, attestation: Mapping[str, Any], path: Path, publisher: cr.Publisher
    ) -> Mapping[str, str]:
        assert publisher == cr.HOST
        if attestation.get("for") != hashlib.sha256(path.read_bytes()).hexdigest():
            raise ValueError("the attestation is for other bytes")
        return self.claims


def _published(tmp_path: Path, pypi: FakePyPI | None = None, **kwargs: Any) -> FakePyPI:
    dist = tmp_path / "dist"
    if not dist.exists():
        _dist(tmp_path)
    text = cr.format_sums(cr.dist_sums(dist, VERSION))
    pypi = pypi or FakePyPI(dist)
    cr.check_published(
        dist,
        text,
        VERSION,
        fetch=pypi.fetch,
        verify=pypi.verify,
        sleep=kwargs.pop("sleep", lambda _: None),
        **kwargs,
    )
    return pypi


def test_a_release_pypi_serves_and_attests_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pypi = _published(tmp_path)
    out = capsys.readouterr().out
    for name in cr.release_files(VERSION):
        assert f"{name}: sha256 {hashlib.sha256(pypi.files[name]).hexdigest()}" in out
        assert f"https://{cr.FILES_HOST}/packages/xx/{name}" in pypi.fetched


def test_it_waits_for_pypi_to_list_the_release_then_gives_up(tmp_path: Path) -> None:
    _dist(tmp_path)
    pypi = FakePyPI(tmp_path / "dist")
    pypi.unlisted_reads = 3
    slept: list[float] = []
    _published(tmp_path, pypi, sleep=slept.append, every=15.0)
    assert slept == [15.0, 15.0, 15.0]

    now = [0.0]
    pypi.unlisted_reads = 1000

    def sleep(seconds: float) -> None:
        now[0] += seconds

    with pytest.raises(cr.CheckError, match="PyPI does not list the release"):
        _published(tmp_path, pypi, sleep=sleep, clock=lambda: now[0], wait=60.0)
    assert now[0] == 60.0


def _break(pypi: FakePyPI, how: str) -> None:
    wheel, _ = cr.release_files(VERSION)
    if how == "listed-sha":
        pypi.listing[0]["digests"]["sha256"] = "0" * 64
    elif how == "served-bytes":
        pypi.served[wheel] = b"other bytes"
    elif how == "no-url":
        del pypi.listing[0]["url"]
    elif how == "publisher-missing-environment":
        del pypi.publisher["environment"]
    elif how.startswith("publisher-"):
        field = how.removeprefix("publisher-")
        pypi.publisher[field] = {"kind": "GitLab"}.get(field, "other")
    elif how == "no-bundles":
        pypi.bundles = []
    elif how == "no-attestations":
        pypi.attestations = []
    elif how == "attestation-other-bytes":
        pypi.attestations = [{"for": "0" * 64}]
    elif how == "one-bad-attestation":
        pypi.attestations = None
        pypi.bundles = None
        good = pypi.fetch  # attestations default to one good one; add a second, bad one

        def fetch(url: str, headers: Mapping[str, str]) -> bytes:
            raw = good(url, headers)
            if "/provenance" in url:
                doc = json.loads(raw)
                doc["attestation_bundles"][0]["attestations"].append({"for": "1" * 64})
                return json.dumps(doc).encode()
            return raw

        pypi.fetch = fetch  # type: ignore[method-assign]
    elif how == "claim-missing":
        del pypi.claims[cr.RUNNER_ENVIRONMENT]
    elif how.startswith("claim-"):
        oid = {
            "claim-runner": cr.RUNNER_ENVIRONMENT,
            "claim-repository": cr.SOURCE_REPOSITORY_URI,
            "claim-ref": cr.SOURCE_REPOSITORY_REF,
        }[how]
        pypi.claims[oid] = {
            cr.RUNNER_ENVIRONMENT: "self-hosted",
            cr.SOURCE_REPOSITORY_URI: "https://github.com/someone/lucille-host",
            cr.SOURCE_REPOSITORY_REF: "refs/heads/main",
        }[oid]
    elif how == "extra-file":
        pypi.listing.append(
            dict(pypi.listing[0], filename="lucille_host-1.2.3-py3-none-macosx.whl")
        )
    elif how == "repeated-file":
        pypi.listing.append(dict(pypi.listing[0]))
    elif how == "other-version":
        pypi.info_version = "1.2.4"
    else:
        raise AssertionError(how)


@pytest.mark.parametrize(
    "how",
    [
        "listed-sha",
        "served-bytes",
        "no-url",
        "publisher-kind",
        "publisher-repository",
        "publisher-workflow",
        "publisher-environment",
        "publisher-missing-environment",
        "no-bundles",
        "no-attestations",
        "attestation-other-bytes",
        "one-bad-attestation",
        "claim-runner",
        "claim-repository",
        "claim-ref",
        "claim-missing",
        "extra-file",
        "repeated-file",
        "other-version",
    ],
)
def test_anything_else_pypi_serves_or_records_fails(tmp_path: Path, how: str) -> None:
    _dist(tmp_path)
    pypi = FakePyPI(tmp_path / "dist")
    _break(pypi, how)
    now = [0.0]
    with pytest.raises(cr.CheckError):
        _published(tmp_path, pypi, clock=lambda: now[0], wait=0.0)


def test_fetch_refuses_anything_but_pypi_over_https() -> None:
    for url in (
        "http://pypi.org/pypi/lucille-host/json",
        "https://pypi.org.example.com/x",
        "https://example.com/files.pythonhosted.org/x",
        "file:///etc/passwd",
    ):
        with pytest.raises(cr.CheckError, match="not PyPI over HTTPS"):
            cr.fetch_url(url, {})


# --- dependency -----------------------------------------------------------------------------


def test_the_lock_pins_switchboard_from_pypi_with_both_files() -> None:
    version, sums = cr.locked_release(cr.LOCK_FILE.read_text(encoding="utf-8"), "agent-switchboard")
    assert re.fullmatch(r"2\.6\.\d+", version)
    assert sorted(sums) == sorted(
        [f"agent_switchboard-{version}-py3-none-any.whl", f"agent_switchboard-{version}.tar.gz"]
    )
    assert cr.DEPENDENCIES["agent-switchboard"] == cr.Publisher(
        "gald33/switchboard", "publish.yml", "pypi"
    )


BASE = "https://files.pythonhosted.org/packages"
LOCK = """
[[package]]
name = "agent-switchboard"
version = "2.6.0"
source = {{ registry = "https://pypi.org/simple" }}
sdist = {{ url = "{base}/aa/agent_switchboard-2.6.0.tar.gz", hash = "sha256:{sdist}" }}
wheels = [
    {{ url = "{base}/bb/agent_switchboard-2.6.0-py3-none-any.whl", hash = "sha256:{wheel}" }},
]
"""


class FakeSwitchboardPyPI:
    def __init__(self) -> None:
        self.files = {
            "agent_switchboard-2.6.0.tar.gz": b"sb sdist",
            "agent_switchboard-2.6.0-py3-none-any.whl": b"sb wheel",
        }
        self.claims = {
            cr.RUNNER_ENVIRONMENT: "github-hosted",
            cr.SOURCE_REPOSITORY_URI: "https://github.com/gald33/switchboard",
            cr.SOURCE_REPOSITORY_REF: "refs/tags/v2.6.0",
        }
        self.publisher = {
            "kind": "GitHub",
            "repository": "gald33/switchboard",
            "workflow": "publish.yml",
            "environment": "pypi",
        }

    def lock(self) -> str:
        return LOCK.format(
            base=BASE,
            sdist=hashlib.sha256(b"sb sdist").hexdigest(),
            wheel=hashlib.sha256(b"sb wheel").hexdigest(),
        )

    def fetch(self, url: str, headers: Mapping[str, str]) -> bytes:
        if url == f"{cr.PYPI}/pypi/agent-switchboard/2.6.0/json":
            listing = [
                {
                    "filename": name,
                    "url": f"https://{cr.FILES_HOST}/packages/zz/{name}",
                    "digests": {"sha256": hashlib.sha256(data).hexdigest()},
                }
                for name, data in self.files.items()
            ]
            return json.dumps({"info": {"version": "2.6.0"}, "urls": listing}).encode()
        for name, data in self.files.items():
            if url == f"https://{cr.FILES_HOST}/packages/zz/{name}":
                return data
            if url == f"{cr.PYPI}/integrity/agent-switchboard/2.6.0/{name}/provenance":
                bundle = {
                    "publisher": self.publisher,
                    "attestations": [{"for": hashlib.sha256(data).hexdigest()}],
                }
                return json.dumps({"version": 1, "attestation_bundles": [bundle]}).encode()
        raise AssertionError(url)

    def verify(
        self, attestation: Mapping[str, Any], path: Path, publisher: cr.Publisher
    ) -> Mapping[str, str]:
        assert publisher == cr.DEPENDENCIES["agent-switchboard"]
        if attestation["for"] != hashlib.sha256(path.read_bytes()).hexdigest():
            raise ValueError("other bytes")
        return self.claims


def test_switchboard_pypi_serves_and_attests_passes(tmp_path: Path) -> None:
    pypi = FakeSwitchboardPyPI()
    cr.check_dependency(
        "agent-switchboard", pypi.lock(), tmp_path, fetch=pypi.fetch, verify=pypi.verify
    )
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(pypi.files)


@pytest.mark.parametrize("how", ["bytes", "publisher", "ref", "lock-hash", "not-listed"])
def test_switchboard_otherwise_fails(tmp_path: Path, how: str) -> None:
    pypi = FakeSwitchboardPyPI()
    lock = pypi.lock()
    if how == "bytes":
        pypi.files["agent_switchboard-2.6.0.tar.gz"] = b"other"
    elif how == "publisher":
        pypi.publisher["repository"] = "someone/switchboard"
    elif how == "ref":
        pypi.claims[cr.SOURCE_REPOSITORY_REF] = "refs/tags/v2.6.1"
    elif how == "lock-hash":
        lock = lock.replace(hashlib.sha256(b"sb wheel").hexdigest(), "0" * 64)
    else:
        del pypi.files["agent_switchboard-2.6.0-py3-none-any.whl"]
    with pytest.raises(cr.CheckError):
        cr.check_dependency(
            "agent-switchboard", lock, tmp_path, fetch=pypi.fetch, verify=pypi.verify
        )


@pytest.mark.parametrize(
    "lock",
    [
        "",
        LOCK + LOCK,
        LOCK.replace('registry = "https://pypi.org/simple"', 'git = "https://example.com/sb"'),
        LOCK.replace('hash = "sha256:{sdist}"', 'hash = "md5:{sdist}"'),
    ],
)
def test_a_lock_that_does_not_pin_switchboard_once_from_pypi_by_sha256_fails(lock: str) -> None:
    with pytest.raises(cr.CheckError):
        cr.locked_release(
            lock.format(base=BASE, sdist="a" * 64, wheel="b" * 64), "agent-switchboard"
        )


def test_only_listed_dependencies_are_checked(tmp_path: Path) -> None:
    with pytest.raises(cr.CheckError, match="not a dependency"):
        cr.check_dependency("keyring", "", tmp_path)
