"""``lucille-host verify``: this host is a release PyPI serves, attested, and installed as released.

decisions/0001, "Packaging, releases and verification" and "How a third party verifies the host".
It checks the release whose code is running, in this order, and stops at the first check that
fails:

1. This host is a release: its version is a final ``X.Y.Z``, not a development build.
2. PyPI lists exactly the release's two files, the wheel and the sdist, and does not mark the
   release or either file yanked.
3. For each file: the bytes PyPI serves have the sha256 PyPI lists, and each is attested by this
   repository's Trusted Publisher: PyPI records at least one attestation bundle for it, each naming
   repository :data:`REPOSITORY`, workflow :data:`WORKFLOW` and environment :data:`ENVIRONMENT`;
   every attestation in each verifies with sigstore (``pypi-attestations``) against that
   repository and workflow and those bytes; and each attestation's certificate names that
   repository, the release's tag ``refs/tags/v<version>``, and a GitHub-hosted runner. The
   environment is not in the certificate, so it is PyPI's record of it that is checked. These are
   the checks ``tools/release/check_release.py published`` makes after a release is published.
4. The installed files are the wheel's: every file the verified wheel holds but its ``RECORD`` is
   in the folder the running package was imported from, with the same sha256; and the package
   folder holds no other file, but bytecode (``.pyc``) in a ``__pycache__`` folder, which is
   counted and not checked.

It reaches ``pypi.org`` and ``files.pythonhosted.org`` over HTTPS (:func:`fetch_url`), and
``pypi-attestations`` reaches sigstore's trust root through its own client. It reads no file of a
profile, and writes nothing (sigstore keeps a cache of its trust root in the user's cache folder).
Only the ``verify`` verb uses this module. ``pypi-attestations`` is the ``[verify]`` extra's, and
is imported only when a check runs.
"""

from __future__ import annotations

import dataclasses
import hashlib
import http.client
import io
import json
import os
import re
import ssl
import zipfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Final

import certifi

from lucille_host import __version__

PROJECT: Final = "lucille-host"
# The Trusted Publisher of every release (decisions/0001, "How a third party verifies the host").
REPOSITORY: Final = "gald33/lucille-host"
WORKFLOW: Final = "release.yml"
ENVIRONMENT: Final = "pypi"
PYPI_HOST: Final = "pypi.org"
FILES_HOST: Final = "files.pythonhosted.org"
INTEGRITY_ACCEPT: Final = {"Accept": "application/vnd.pypi.integrity.v1+json"}
# A final release only: no pre-release, post-release, dev or local part, no leading zero, and at
# most nine digits a part.
FINAL_VERSION: Final = re.compile(r"(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})")
SHA256: Final = re.compile(r"[0-9a-f]{64}")
# Fulcio's certificate extensions (sigstore's OID registry), as pypi-attestations names claims.
RUNNER_ENVIRONMENT: Final = "1.3.6.1.4.1.57264.1.11"
SOURCE_REPOSITORY_URI: Final = "1.3.6.1.4.1.57264.1.12"
SOURCE_REPOSITORY_REF: Final = "1.3.6.1.4.1.57264.1.14"
# How long one request may wait, and the most one response may hold.
TIMEOUT_S: Final = 60.0
MAX_RESPONSE: Final = 64 * 1024 * 1024
# The most problems with the installed files one message names.
NAMED_PROBLEMS: Final = 5
# The most attestations of one file, and the most files in the wheel or bytecode files in the
# package's folders, that a check takes (spec/CLI.md's `verify` object bounds each count).
MAX_ATTESTATIONS: Final = 100
MAX_FILES: Final = 100_000

# GET a URL, with these headers: its status and body.
Fetch = Callable[[str, Mapping[str, str]], tuple[int, bytes]]
# Verifies one attestation, as PyPI serves it, against the publisher and a file's name and sha256;
# returns its certificate's claims, and raises on any failure.
Verify = Callable[[Mapping[str, Any], str, str], Mapping[str, str]]


class Unverified(Exception):
    """A check that failed or could not be made; ``code`` is what ``--json`` calls it
    (spec/CLI.md section 10), and the message says why."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclasses.dataclass(frozen=True)
class Checked:
    """One of the release's files on PyPI: its sha256, and how many attestations verified."""

    name: str
    sha256: str
    attestations: int


@dataclasses.dataclass(frozen=True)
class Result:
    """A release that passed every check: its files on PyPI, the folder whose files were checked,
    how many files there are the wheel's, and how many bytecode files were not checked."""

    version: str
    files: tuple[Checked, ...]
    site: Path
    installed: int
    bytecode: int


def release_files(version: str) -> tuple[str, str]:
    """The names of a release's two files: the wheel, then the sdist."""
    return (f"lucille_host-{version}-py3-none-any.whl", f"lucille_host-{version}.tar.gz")


def installed() -> tuple[str, Path]:
    """The running host's version, and the folder its package was imported from (``site-packages``
    in an installed host)."""
    return __version__, Path(__file__).absolute().parent.parent


def fetch_url(url: str, headers: Mapping[str, str]) -> tuple[int, bytes]:
    """GET ``url``, which must be PyPI or its file host over HTTPS, following no redirect; the
    certificate is checked against certifi's bundle, TLS 1.2 or later."""
    scheme, sep, rest = url.partition("://")
    host, slash, path = rest.partition("/")
    if scheme != "https" or not sep or not slash or host not in (PYPI_HOST, FILES_HOST):
        raise Unverified("not_attested", f"PyPI named {url!r}, which is not PyPI over HTTPS")
    if not all("!" <= char <= "~" for char in url):
        raise Unverified("not_attested", f"PyPI named {url!r}, which a request cannot carry")
    context = ssl.create_default_context(cafile=certifi.where())
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    connection = http.client.HTTPSConnection(host, timeout=TIMEOUT_S, context=context)
    try:
        connection.request("GET", "/" + path, headers={"User-Agent": PROJECT, **headers})
        response = connection.getresponse()
        body = response.read(MAX_RESPONSE + 1)
    except (OSError, http.client.HTTPException) as exc:
        raise Unverified(
            "unreachable", f"{host} could not be reached ({type(exc).__name__}); try again"
        ) from None
    finally:
        connection.close()
    if len(body) > MAX_RESPONSE:
        raise Unverified("unreachable", f"{host} answered with more than {MAX_RESPONSE} bytes")
    return response.status, body


def _get(fetch: Fetch, url: str, what: str, headers: Mapping[str, str] | None = None) -> bytes:
    """The body PyPI answers ``url`` with. PyPI not holding ``what`` fails the check; any status
    but 200 and 404 is PyPI that could not be asked."""
    status, body = fetch(url, headers or {})
    if status == 404:
        raise Unverified("not_attested", f"PyPI holds no {what}")
    if status != 200:
        raise Unverified("unreachable", f"PyPI answered {status} for {what}; try again")
    return body


def _json(raw: bytes, what: str) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        raise Unverified("unreachable", f"PyPI's {what} is not JSON; try again") from None


def listed_files(release: Any, version: str) -> dict[str, dict[str, Any]]:
    """The files PyPI's JSON for ``version`` lists, by name: exactly the release's two, and none
    of them, nor the release, yanked."""
    info = release.get("info") if isinstance(release, dict) else None
    urls = release.get("urls") if isinstance(release, dict) else None
    if not isinstance(info, dict) or info.get("version") != version or not isinstance(urls, list):
        raise Unverified("not_attested", f"PyPI's JSON is not a listing of release {version}")
    files: dict[str, dict[str, Any]] = {}
    for entry in urls:
        name = entry.get("filename") if isinstance(entry, dict) else None
        if not isinstance(name, str) or name in files:
            raise Unverified("not_attested", "PyPI lists a file that is malformed or repeated")
        files[name] = entry
    if info.get("yanked") is True or any(entry.get("yanked") is True for entry in files.values()):
        raise Unverified(
            "yanked",
            f"PyPI marks {PROJECT} {version} yanked: its maintainers withdrew it. Read the "
            f"repository's security advisories, and install a release that is not yanked",
        )
    expected = sorted(release_files(version))
    if sorted(files) != expected:
        raise Unverified("not_attested", f"PyPI lists {sorted(files)}, not exactly {expected}")
    return files


def check_provenance(provenance: Any, name: str, sha256: str, version: str, verify: Verify) -> int:
    """``name``, whose bytes have ``sha256``, is attested by the publisher as release
    ``version`` (module docstring, step 3). Returns how many attestations verified."""
    bundles = provenance.get("attestation_bundles") if isinstance(provenance, dict) else None
    if not isinstance(bundles, list) or not bundles:
        raise Unverified("not_attested", f"{name}: PyPI's provenance holds no attestation bundle")
    expected = {
        "kind": "GitHub",
        "repository": REPOSITORY,
        "workflow": WORKFLOW,
        "environment": ENVIRONMENT,
    }
    wanted = {
        RUNNER_ENVIRONMENT: "github-hosted",
        SOURCE_REPOSITORY_URI: f"https://github.com/{REPOSITORY}",
        SOURCE_REPOSITORY_REF: f"refs/tags/v{version}",
    }
    count = 0
    for bundle in bundles:
        recorded = bundle.get("publisher") if isinstance(bundle, dict) else None
        if not isinstance(recorded, dict) or {k: recorded.get(k) for k in expected} != expected:
            raise Unverified(
                "not_attested", f"{name}: an attestation bundle names another publisher"
            )
        attestations = bundle.get("attestations")
        if not isinstance(attestations, list) or not attestations:
            raise Unverified("not_attested", f"{name}: an attestation bundle holds no attestation")
        for attestation in attestations:
            if not isinstance(attestation, dict):
                raise Unverified("not_attested", f"{name}: an attestation is not an object")
            try:
                claims = verify(attestation, name, sha256)
            except ValueError as exc:
                # pypi-attestations' errors, which hold sigstore's, and a malformed attestation.
                raise Unverified(
                    "not_attested", f"{name}: an attestation does not verify: {exc}"
                ) from None
            except Exception as exc:
                # Sigstore's trust root could not be fetched or read: nothing was verified.
                raise Unverified(
                    "unreachable",
                    f"{name}: an attestation could not be checked, since sigstore's trust root "
                    f"could not be fetched or read ({type(exc).__name__}); try again",
                ) from None
            for oid, value in wanted.items():
                if claims.get(oid) != value:
                    raise Unverified(
                        "not_attested",
                        f"{name}: the certificate's {oid} is {claims.get(oid)!r}, not {value!r}",
                    )
            count += 1
            if count > MAX_ATTESTATIONS:
                raise Unverified(
                    "not_attested", f"{name}: PyPI holds more than {MAX_ATTESTATIONS} attestations"
                )
    return count


def sigstore_verifier() -> Verify:
    """The check of one attestation with ``pypi-attestations``, against :data:`REPOSITORY`,
    :data:`WORKFLOW` and :data:`ENVIRONMENT` and a file's name and sha256."""
    try:
        import pypi_attestations
    except ImportError:
        raise Unverified(
            "no_verifier",
            f"verify needs pypi-attestations, which the [verify] extra installs: install "
            f"{PROJECT}[verify] rather than {PROJECT}",
        ) from None
    identity = pypi_attestations.GitHubPublisher(
        repository=REPOSITORY, workflow=WORKFLOW, environment=ENVIRONMENT
    )

    def verify(attestation: Mapping[str, Any], name: str, sha256: str) -> Mapping[str, str]:
        parsed = pypi_attestations.Attestation.model_validate(dict(attestation))
        parsed.verify(identity, pypi_attestations.Distribution(name=name, digest=sha256))
        return parsed.certificate_claims

    return verify


def wheel_files(wheel: bytes, version: str) -> dict[str, str]:
    """The sha256 of each file the wheel holds but its ``RECORD``, by path. Each is in the
    package's folder or its ``.dist-info``, which an installer copies as they are."""
    dist_info = f"lucille_host-{version}.dist-info/"
    found: dict[str, str] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(wheel)) as archive:
            for member in archive.infolist():
                name = member.filename
                if member.is_dir():
                    continue
                parts = name.split("/")
                if (
                    not name.startswith(("lucille_host/", dist_info))
                    or "\\" in name
                    or any(part in ("", ".", "..") for part in parts)
                ):
                    raise Unverified(
                        "files_differ",
                        f"the release's wheel holds {name!r}, outside the folders this check "
                        f"compares",
                    )
                if name in found:
                    raise Unverified("files_differ", f"the release's wheel holds {name} twice")
                if name != dist_info + "RECORD":
                    found[name] = hashlib.sha256(archive.read(member)).hexdigest()
                if len(found) > MAX_FILES:
                    raise Unverified(
                        "files_differ", f"the release's wheel holds more than {MAX_FILES} files"
                    )
    except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError):
        raise Unverified("files_differ", "the release's wheel is not a zip file") from None
    return found


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def check_installed(expected: Mapping[str, str], site: Path) -> tuple[int, int]:
    """The files in ``site`` are ``expected``'s (module docstring, step 4). Returns how many were
    checked, and how many bytecode files were not."""
    problems: list[str] = []
    for name, digest in sorted(expected.items()):
        try:
            found = _sha256_file(site / name)
        except FileNotFoundError:
            problems.append(f"{name} is missing")
            continue
        except OSError as exc:
            problems.append(f"{name} cannot be read ({exc.strerror or type(exc).__name__})")
            continue
        if found != digest:
            problems.append(f"{name} is not the release's")
    bytecode = 0
    package = site / "lucille_host"
    for folder, folders, files in os.walk(package):
        here = Path(folder)
        folders.sort()
        for child in folders:
            if (here / child).is_symlink():
                problems.append(f"{(here / child).relative_to(site)} is a link the release lacks")
        for file in sorted(files):
            name = (here / file).relative_to(site).as_posix()
            if name in expected:
                continue
            if here.name == "__pycache__" and file.endswith(".pyc") and bytecode < MAX_FILES:
                bytecode += 1
                continue
            problems.append(f"{name} is not a file the release ships")
    if problems:
        named = "; ".join(problems[:NAMED_PROBLEMS])
        more = len(problems) - NAMED_PROBLEMS
        rest = f"; and {more} more" if more > 0 else ""
        raise Unverified(
            "files_differ",
            f"the files installed in {site} are not the release's: {named}{rest}. Install the "
            f"release again, into a fresh environment",
        )
    return len(expected), bytecode


def check(
    *,
    fetch: Fetch | None = None,
    verify: Verify | None = None,
    where: Callable[[], tuple[str, Path]] | None = None,
) -> Result:
    """Every check in the module docstring, on the release ``where`` names (by default, the one
    running: :func:`installed`), fetching with ``fetch`` (:func:`fetch_url`) and verifying each
    attestation with ``verify`` (:func:`sigstore_verifier`); raises :class:`Unverified` at the
    first check that fails."""
    version, site = (where or installed)()
    if FINAL_VERSION.fullmatch(version) is None:
        raise Unverified(
            "not_a_release",
            f"this {PROJECT} is a development build, not a release, so PyPI holds nothing to "
            f"check it against; `{PROJECT} --version` names it",
        )
    checker = sigstore_verifier() if verify is None else verify
    get = fetch or fetch_url
    release = _json(
        _get(get, f"https://{PYPI_HOST}/pypi/{PROJECT}/{version}/json", f"release {version}"),
        "listing of the release",
    )
    files = listed_files(release, version)
    checked: list[Checked] = []
    wheel = b""
    for name, entry in sorted(files.items()):
        digests = entry.get("digests")
        listed = digests.get("sha256") if isinstance(digests, dict) else None
        url = entry.get("url")
        if not isinstance(listed, str) or SHA256.fullmatch(listed) is None:
            raise Unverified("not_attested", f"{name}: PyPI lists no sha256")
        if not isinstance(url, str):
            raise Unverified("not_attested", f"{name}: PyPI lists no URL")
        data = _get(get, url, name)
        served = hashlib.sha256(data).hexdigest()
        if served != listed:
            raise Unverified(
                "not_attested", f"{name}: PyPI serves sha256 {served}, and lists {listed}"
            )
        provenance = _json(
            _get(
                get,
                f"https://{PYPI_HOST}/integrity/{PROJECT}/{version}/{name}/provenance",
                f"provenance for {name}",
                INTEGRITY_ACCEPT,
            ),
            f"provenance for {name}",
        )
        count = check_provenance(provenance, name, served, version, checker)
        checked.append(Checked(name, served, count))
        if name == release_files(version)[0]:
            wheel = data
    # Only once the wheel is attested is it opened.
    installed_count, bytecode = check_installed(wheel_files(wheel, version), site)
    return Result(version, tuple(checked), site, installed_count, bytecode)
