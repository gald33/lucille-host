"""``lucille-host verify``'s checks (``lucille_host.verify``), with PyPI and sigstore stood in for.

Each check is shown to fail the command when what it checks is not so: a development build; a
listing that is not exactly the release's two files, or is yanked; bytes PyPI serves that are not
the ones it lists; provenance that is missing, names another publisher, holds an attestation that
does not verify, or a certificate that names another repository, tag or runner; and installed files
that are not the attested wheel's. The real network and sigstore are not reached: the one test that
loads pypi-attestations gives it an attestation that fails before any network call.
"""

from __future__ import annotations

import os
import ssl
import sys
from pathlib import Path
from typing import Any

import pytest

from lucille_host import verify
from tests import _fake_pypi
from tests._fake_pypi import FakeRelease


@pytest.fixture
def release(tmp_path: Path) -> FakeRelease:
    return FakeRelease(tmp_path / "site-packages")


def run(release: FakeRelease) -> verify.Result:
    return verify.check(fetch=release.fetch, verify=release.verify, where=release.where)


def refused(release: FakeRelease) -> verify.Unverified:
    with pytest.raises(verify.Unverified) as raised:
        run(release)
    return raised.value


# --- a release that passes ------------------------------------------------------------------------


def test_a_release_as_published_and_installed_passes(release: FakeRelease) -> None:
    result = run(release)
    assert result.version == "1.2.3"
    assert result.files == (
        verify.Checked(release.wheel_name, _fake_pypi.sha256(release.wheel), 1),
        verify.Checked(release.sdist_name, _fake_pypi.sha256(release.sdist), 1),
    )
    assert result.site == release.site
    # Every file of the wheel but its RECORD; the installer's own files are not counted.
    assert result.installed == len(release.wheel_files) - 1
    assert result.bytecode == 2
    # It asks only PyPI: the listing, then each file and its provenance, by name.
    assert [url for url, _ in release.requests] == [
        "https://pypi.org/pypi/lucille-host/1.2.3/json",
        f"{_fake_pypi.FILES}/{release.wheel_name}",
        f"https://pypi.org/integrity/lucille-host/1.2.3/{release.wheel_name}/provenance",
        f"{_fake_pypi.FILES}/{release.sdist_name}",
        f"https://pypi.org/integrity/lucille-host/1.2.3/{release.sdist_name}/provenance",
    ]


def test_every_attestation_in_every_bundle_is_verified_and_counted(release: FakeRelease) -> None:
    bundle = release.provenance[release.wheel_name]["attestation_bundles"][0]
    bundle["attestations"].append(bundle["attestations"][0])
    release.provenance[release.wheel_name]["attestation_bundles"].append(bundle)
    counts = {checked.name: checked.attestations for checked in run(release).files}
    assert counts == {release.wheel_name: 4, release.sdist_name: 1}


# --- the host is a release ------------------------------------------------------------------------


@pytest.mark.parametrize("version", ["0.1.0.dev0", "1.2.3rc1", "1.2.3.post1", "1.2", "01.2.3"])
def test_a_development_build_is_refused_before_anything_is_fetched(
    tmp_path: Path, version: str
) -> None:
    release = FakeRelease(tmp_path / "site", version=version)
    assert refused(release).code == "not_a_release"
    assert release.requests == []


def test_the_running_host_is_checked_by_default() -> None:
    version, site = verify.installed()
    assert version == verify.__version__
    assert (site / "lucille_host" / "verify.py").is_file()
    # The checkout's build is a development one.
    with pytest.raises(verify.Unverified) as raised:
        verify.check(fetch=lambda *_: pytest.fail("fetched"), verify=lambda *_: {})
    assert raised.value.code == "not_a_release"


def test_without_the_verify_extra_it_says_to_install_it(
    release: FakeRelease, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "pypi_attestations", None)
    with pytest.raises(verify.Unverified) as raised:
        verify.check(fetch=release.fetch, where=release.where)
    assert raised.value.code == "no_verifier"
    assert "lucille-host[verify]" in str(raised.value)
    assert release.requests == []


def test_pypi_attestations_refuses_an_attestation_it_cannot_read() -> None:
    pytest.importorskip("pypi_attestations")
    check = verify.sigstore_verifier()
    with pytest.raises(ValueError):
        check({"version": 1}, "lucille_host-1.2.3.tar.gz", "0" * 64)


# --- PyPI's listing -------------------------------------------------------------------------------


def test_a_release_pypi_does_not_hold_is_not_attested(release: FakeRelease) -> None:
    release.served["https://pypi.org/pypi/lucille-host/1.2.3/json"] = (404, b"{}")
    found = refused(release)
    assert (found.code, str(found)) == ("not_attested", "PyPI holds no release 1.2.3")


@pytest.mark.parametrize(
    ("status", "body"), [(503, b"{}"), (301, b""), (200, b"<html>not json</html>")]
)
def test_a_pypi_that_cannot_be_asked_is_unreachable(
    release: FakeRelease, status: int, body: bytes
) -> None:
    release.served["https://pypi.org/pypi/lucille-host/1.2.3/json"] = (status, body)
    assert refused(release).code == "unreachable"


def test_a_listing_of_another_version_is_not_attested(release: FakeRelease) -> None:
    release.listing["info"]["version"] = "1.2.4"
    assert refused(release).code == "not_attested"


@pytest.mark.parametrize("change", ["extra", "missing", "repeated", "malformed"])
def test_a_listing_that_is_not_exactly_the_two_files_is_not_attested(
    release: FakeRelease, change: str
) -> None:
    urls = release.listing["urls"]
    if change == "extra":
        urls.append({**urls[0], "filename": "lucille_host-1.2.3-py3-none-macosx_11_0_arm64.whl"})
    elif change == "missing":
        urls.pop()
    elif change == "repeated":
        urls.append(urls[0])
    else:
        urls.append("lucille_host-1.2.3.zip")
    assert refused(release).code == "not_attested"


@pytest.mark.parametrize("where", ["release", "file"])
def test_a_yanked_release_is_refused(release: FakeRelease, where: str) -> None:
    if where == "release":
        release.listing["info"]["yanked"] = True
    else:
        release.listing["urls"][1]["yanked"] = True
    found = refused(release)
    assert found.code == "yanked"
    assert "lucille-host 1.2.3 yanked" in str(found)
    assert len(release.requests) == 1


@pytest.mark.parametrize("digest", ["", "A" * 64, "0" * 63, None])
def test_a_file_listed_without_a_sha256_is_not_attested(
    release: FakeRelease, digest: str | None
) -> None:
    release.listing["urls"][0]["digests"] = {"sha256": digest}
    assert refused(release).code == "not_attested"


def test_bytes_pypi_serves_that_are_not_the_ones_it_lists_are_not_attested(
    release: FakeRelease,
) -> None:
    release.served[f"{_fake_pypi.FILES}/{release.wheel_name}"] = (200, release.wheel + b"x")
    found = refused(release)
    assert found.code == "not_attested"
    assert f"{release.wheel_name}: PyPI serves sha256" in str(found)


def test_a_file_url_off_pypi_is_refused_without_a_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def connect(*_: Any, **__: Any) -> Any:
        raise AssertionError("connected")

    monkeypatch.setattr("http.client.HTTPSConnection", connect)
    for url in (
        "http://files.pythonhosted.org/packages/x.whl",
        "https://files.pythonhosted.org.example/x.whl",
        "https://example.com/x.whl",
        "https://pypi.org",
        "ftp://pypi.org/x",
    ):
        with pytest.raises(verify.Unverified) as raised:
            verify.fetch_url(url, {})
        assert raised.value.code == "not_attested", url


class _Response:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self, most: int) -> bytes:
        return self._body[:most]


class _Connection:
    """http.client.HTTPSConnection, stood in for: what it was asked, and its answer."""

    made: list[_Connection] = []
    answer: Any = _Response(200, b"{}")

    def __init__(self, host: str, *, timeout: float, context: Any) -> None:
        self.host = host
        self.timeout = timeout
        self.context = context
        self.asked: list[tuple[str, str, dict[str, str]]] = []
        self.closed = False
        _Connection.made.append(self)

    def request(self, method: str, path: str, headers: dict[str, str]) -> None:
        self.asked.append((method, path, headers))

    def getresponse(self) -> Any:
        if isinstance(_Connection.answer, BaseException):
            raise _Connection.answer
        return _Connection.answer

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def connection(monkeypatch: pytest.MonkeyPatch) -> type[_Connection]:
    _Connection.made = []
    _Connection.answer = _Response(200, b"{}")
    monkeypatch.setattr("http.client.HTTPSConnection", _Connection)
    return _Connection


def test_fetch_asks_pypi_over_tls_and_gives_its_status_and_body(
    connection: type[_Connection],
) -> None:
    connection.answer = _Response(404, b"gone")
    assert verify.fetch_url("https://pypi.org/pypi/lucille-host/1.2.3/json", {"A": "b"}) == (
        404,
        b"gone",
    )
    [made] = connection.made
    assert made.host == "pypi.org" and made.closed
    assert made.timeout == verify.TIMEOUT_S
    assert made.context.verify_mode.name == "CERT_REQUIRED" and made.context.check_hostname
    assert made.context.minimum_version.name == "TLSv1_2"
    assert made.asked == [
        ("GET", "/pypi/lucille-host/1.2.3/json", {"User-Agent": "lucille-host", "A": "b"})
    ]


@pytest.mark.parametrize(
    "error", [ConnectionRefusedError(61, "refused"), TimeoutError(), ssl.SSLError()]
)
def test_a_connection_that_fails_is_unreachable(
    connection: type[_Connection], error: BaseException
) -> None:
    connection.answer = error
    with pytest.raises(verify.Unverified) as raised:
        verify.fetch_url("https://pypi.org/x", {})
    assert raised.value.code == "unreachable"
    assert connection.made[0].closed


@pytest.mark.parametrize("char", [" ", "\n", "\x7f", "\u00e9"])
def test_a_url_a_request_cannot_carry_is_not_attested(
    connection: type[_Connection], char: str
) -> None:
    with pytest.raises(verify.Unverified) as raised:
        verify.fetch_url(f"https://files.pythonhosted.org/packages/a{char}b.whl", {})
    assert raised.value.code == "not_attested"
    assert connection.made == []


def test_a_response_too_large_is_unreachable(
    connection: type[_Connection], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(verify, "MAX_RESPONSE", 4)
    connection.answer = _Response(200, b"12345")
    with pytest.raises(verify.Unverified) as raised:
        verify.fetch_url("https://pypi.org/x", {})
    assert raised.value.code == "unreachable"


# --- provenance -----------------------------------------------------------------------------------


def test_a_file_without_provenance_is_not_attested(release: FakeRelease) -> None:
    del release.provenance[release.sdist_name]
    found = refused(release)
    assert (found.code, str(found)) == (
        "not_attested",
        f"PyPI holds no provenance for {release.sdist_name}",
    )


@pytest.mark.parametrize("bundles", [None, [], "x", [None]])
def test_provenance_without_a_bundle_is_not_attested(release: FakeRelease, bundles: Any) -> None:
    release.provenance[release.wheel_name]["attestation_bundles"] = bundles
    assert refused(release).code == "not_attested"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("kind", "GitLab"),
        ("repository", "gald33/lucille-host-fork"),
        ("workflow", "other.yml"),
        ("environment", "staging"),
        ("environment", None),
    ],
)
def test_a_bundle_by_another_publisher_is_not_attested(
    release: FakeRelease, field: str, value: Any
) -> None:
    release.provenance[release.wheel_name]["attestation_bundles"][0]["publisher"][field] = value
    found = refused(release)
    assert found.code == "not_attested"
    assert "another publisher" in str(found)


@pytest.mark.parametrize("attestations", [None, [], ["x"]])
def test_a_bundle_without_attestations_is_not_attested(
    release: FakeRelease, attestations: Any
) -> None:
    release.provenance[release.wheel_name]["attestation_bundles"][0]["attestations"] = attestations
    assert refused(release).code == "not_attested"


def test_an_attestation_of_other_bytes_is_not_attested(release: FakeRelease) -> None:
    bundle = release.provenance[release.wheel_name]["attestation_bundles"][0]
    bundle["attestations"].append(release.attestation(release.wheel_name, "0" * 64))
    found = refused(release)
    assert found.code == "not_attested"
    assert "does not verify: subject does not match distribution digest" in str(found)


def test_a_trust_root_that_cannot_be_fetched_is_unreachable(release: FakeRelease) -> None:
    def failing(*_: Any) -> Any:
        raise RuntimeError("TUF refresh failed")

    with pytest.raises(verify.Unverified) as raised:
        verify.check(fetch=release.fetch, verify=failing, where=release.where)
    assert raised.value.code == "unreachable"
    assert "sigstore's trust root" in str(raised.value)


@pytest.mark.parametrize(
    ("oid", "value"),
    [
        (verify.RUNNER_ENVIRONMENT, "self-hosted"),
        (verify.SOURCE_REPOSITORY_URI, "https://github.com/someone/lucille-host"),
        (verify.SOURCE_REPOSITORY_REF, "refs/tags/v1.2.4"),
        (verify.SOURCE_REPOSITORY_REF, "refs/heads/main"),
    ],
)
def test_a_certificate_that_names_another_repository_tag_or_runner_is_not_attested(
    release: FakeRelease, oid: str, value: str
) -> None:
    digest = _fake_pypi.sha256(release.sdist)
    bundle = release.provenance[release.sdist_name]["attestation_bundles"][0]
    bundle["attestations"] = [release.attestation(release.sdist_name, digest, **{oid: value})]
    found = refused(release)
    assert found.code == "not_attested"
    assert oid in str(found)


# --- the installed files --------------------------------------------------------------------------


def _files_differ(release: FakeRelease) -> str:
    found = refused(release)
    assert found.code == "files_differ"
    assert str(release.site) in str(found)
    return str(found)


def test_an_installed_file_that_differs_is_found(release: FakeRelease) -> None:
    (release.site / "lucille_host" / "cli.py").write_bytes(b"import os\n")
    assert "lucille_host/cli.py is not the release's" in _files_differ(release)


def test_a_dist_info_file_that_differs_is_found(release: FakeRelease) -> None:
    (release.site / _fake_pypi.dist_info() / "entry_points.txt").write_text("[console_scripts]\n")
    assert "entry_points.txt is not the release's" in _files_differ(release)


def test_a_missing_file_is_found(release: FakeRelease) -> None:
    (release.site / "lucille_host" / "ops" / "__init__.py").unlink()
    assert "lucille_host/ops/__init__.py is missing" in _files_differ(release)


def test_a_file_that_cannot_be_read_is_found(release: FakeRelease) -> None:
    target = release.site / "lucille_host" / "cli.py"
    target.unlink()
    target.mkdir()
    assert "lucille_host/cli.py cannot be read" in _files_differ(release)


@pytest.mark.parametrize(
    "extra",
    [
        "lucille_host/ops/extra.py",
        "lucille_host/cli.cpython-313-darwin.so",
        "lucille_host/cli.pyc",
        "lucille_host/__pycache__/notes.txt",
        "lucille_host/spec/cli/v1/fixtures/new.json",
    ],
)
def test_a_file_the_release_does_not_ship_is_found(release: FakeRelease, extra: str) -> None:
    path = release.site / extra
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    assert f"{extra} is not a file the release ships" in _files_differ(release)


def test_a_linked_folder_is_found(release: FakeRelease, tmp_path: Path) -> None:
    (tmp_path / "elsewhere").mkdir()
    os.symlink(tmp_path / "elsewhere", release.site / "lucille_host" / "more")
    assert "lucille_host/more is a link the release lacks" in _files_differ(release)


def test_files_beside_the_package_are_not_the_checks(release: FakeRelease) -> None:
    # Another package, and an installer's own files in the .dist-info, are not looked at.
    (release.site / "other").mkdir()
    (release.site / "other" / "x.py").write_text("x\n")
    (release.site / _fake_pypi.dist_info() / "direct_url.json").write_text("{}\n")
    assert run(release).installed == len(release.wheel_files) - 1


def test_many_problems_are_counted_past_the_first_five(release: FakeRelease) -> None:
    for index in range(8):
        (release.site / "lucille_host" / f"extra{index}.py").write_text("x\n")
    said = _files_differ(release)
    assert said.count("is not a file the release ships") == 5
    assert "; and 3 more." in said


@pytest.mark.parametrize(
    "name",
    [
        "../evil.py",
        "lucille_host/../../evil.py",
        "/etc/evil",
        "lucille_host\\evil.py",
        "lucille_host-1.2.3.data/scripts/lucille-host",
        "other/x.py",
    ],
)
def test_a_wheel_with_a_path_this_check_does_not_compare_is_refused(
    release: FakeRelease, name: str
) -> None:
    files = {**release.wheel_files, name: b"x"}
    _replace_wheel(release, _fake_pypi.wheel_bytes(files))
    assert "outside the folders this check compares" in _files_differ_anywhere(release)


def test_a_wheel_that_is_not_a_zip_is_refused(release: FakeRelease) -> None:
    _replace_wheel(release, b"not a zip")
    assert "is not a zip file" in _files_differ_anywhere(release)


def _replace_wheel(release: FakeRelease, wheel: bytes) -> None:
    """Serve ``wheel`` as the release's wheel, listed and attested."""
    digest = _fake_pypi.sha256(wheel)
    release.served[f"{_fake_pypi.FILES}/{release.wheel_name}"] = (200, wheel)
    release.listing["urls"][0]["digests"]["sha256"] = digest
    bundle = release.provenance[release.wheel_name]["attestation_bundles"][0]
    bundle["attestations"] = [release.attestation(release.wheel_name, digest)]


def _files_differ_anywhere(release: FakeRelease) -> str:
    found = refused(release)
    assert found.code == "files_differ"
    return str(found)
