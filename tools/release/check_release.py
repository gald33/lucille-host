"""The checks ``.github/workflows/release.yml`` runs, one subcommand per step.

- ``tag REF_NAME``: the tag is ``v`` and the package's version, and that version is a final
  release, ``X.Y.Z``. The version is read from ``src/lucille_host/__init__.py`` as text, so the
  check imports nothing of the package.
- ``sums DIST --version V --out FILE``: ``DIST`` holds exactly the release's two files, the wheel
  and the sdist, and ``FILE`` gets their sha256s in ``sha256sum``'s format.
- ``check-sums DIST SUMS --version V``: ``DIST`` still holds exactly those files, with those
  sha256s.
- ``published DIST SUMS --version V``: PyPI lists exactly those files for the version, with those
  sha256s; the bytes it serves are those bytes; and each file is attested by this repository's
  Trusted Publisher (:data:`HOST`).
- ``dependency NAME``: the release of ``NAME`` that ``uv.lock`` pins is, file for file, what PyPI
  serves, with the lock's sha256s, and each file is attested by that project's own Trusted
  Publisher (:data:`DEPENDENCIES`). Only ``agent-switchboard`` is listed.

A file is attested by a publisher when PyPI records an attestation bundle for it naming that
publisher's repository, workflow and environment, every attestation in each bundle verifies
(sigstore, with ``pypi-attestations``) against that repository and workflow and the file's
bytes, and each attestation's certificate names the repository, the release's tag, and a
GitHub-hosted runner (decisions/0001, "How a third party verifies the host"). The environment is
not in the certificate, so it is PyPI's record of it that is checked.

``published`` and ``dependency`` need ``pypi-attestations`` (the ``[verify]`` extra) and the
network. Everything else is the standard library. A failed check prints why and exits 1.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import re
import sys
import time
import tomllib
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
VERSION_FILE: Final = REPO_ROOT / "src" / "lucille_host" / "__init__.py"
LOCK_FILE: Final = REPO_ROOT / "uv.lock"
PROJECT: Final = "lucille-host"
# A final release only: no pre-release, post-release, dev or local part, and no leading zero.
FINAL_VERSION: Final = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
VERSION_LINE: Final = re.compile(r'^__version__ = "([^"]*)"$', re.MULTILINE)
SHA256: Final = re.compile(r"[0-9a-f]{64}")
PYPI: Final = "https://pypi.org"
FILES_HOST: Final = "files.pythonhosted.org"
INTEGRITY_ACCEPT: Final = {"Accept": "application/vnd.pypi.integrity.v1+json"}
# Fulcio's certificate extensions (sigstore's OID registry), as pypi-attestations names claims.
RUNNER_ENVIRONMENT: Final = "1.3.6.1.4.1.57264.1.11"
SOURCE_REPOSITORY_URI: Final = "1.3.6.1.4.1.57264.1.12"
SOURCE_REPOSITORY_REF: Final = "1.3.6.1.4.1.57264.1.14"


@dataclasses.dataclass(frozen=True)
class Publisher:
    """A GitHub Trusted Publisher: the repository, the workflow file and the environment a
    release must have been published from. Its tags are ``v<version>``."""

    repository: str
    workflow: str
    environment: str

    def ref(self, version: str) -> str:
        return f"refs/tags/v{version}"


HOST: Final = Publisher("gald33/lucille-host", "release.yml", "pypi")
# The dependencies whose provenance a release checks, each with its own publisher
# (decisions/0001, "How a third party verifies the host", step 2).
DEPENDENCIES: Final[Mapping[str, Publisher]] = {
    "agent-switchboard": Publisher("gald33/switchboard", "publish.yml", "pypi"),
}

Fetch = Callable[[str, Mapping[str, str]], bytes]
# Verifies one attestation, as PyPI serves it, against a publisher's repository and workflow and
# the file at a path; returns its certificate's claims, and raises on any failure.
Verify = Callable[[Mapping[str, Any], Path, Publisher], Mapping[str, str]]


class CheckError(Exception):
    """A check failed; the message says why."""


def release_files(version: str) -> tuple[str, str]:
    """The names of a host release's two files: the wheel, then the sdist."""
    return (f"lucille_host-{version}-py3-none-any.whl", f"lucille_host-{version}.tar.gz")


def package_version(text: str) -> str:
    """``__version__`` as ``src/lucille_host/__init__.py``'s text assigns it, exactly once."""
    found = VERSION_LINE.findall(text)
    if len(found) != 1:
        raise CheckError(f'expected one `__version__ = "..."` line, found {len(found)}')
    return found[0]


def check_tag(ref_name: str, version: str) -> None:
    """The tag ``ref_name`` names the final release ``version``."""
    if FINAL_VERSION.fullmatch(version) is None:
        raise CheckError(f"version {version!r} is not a final release, X.Y.Z")
    if ref_name != f"v{version}":
        raise CheckError(f"tag {ref_name!r} is not v{version}, the package's version")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def dist_sums(dist: Path, version: str) -> dict[str, str]:
    """The sha256 of each file in ``dist``, which must hold exactly the release's two files."""
    expected = sorted(release_files(version))
    found = sorted(entry.name for entry in dist.iterdir())
    if found != expected:
        raise CheckError(f"{dist} holds {found}, not exactly {expected}")
    for name in found:
        if (dist / name).is_symlink() or not (dist / name).is_file():
            raise CheckError(f"{dist / name} is not a regular file")
    return {name: sha256_file(dist / name) for name in found}


def format_sums(sums: Mapping[str, str]) -> str:
    """``sha256sum``'s format: one ``<sha256>  <name>`` line per file, by name."""
    return "".join(f"{sums[name]}  {name}\n" for name in sorted(sums))


def parse_sums(text: str, version: str) -> dict[str, str]:
    """A SHA256SUMS file naming exactly the release's two files, each once, as ``sums`` writes
    it."""
    sums: dict[str, str] = {}
    for line in text.splitlines():
        digest, sep, name = line.partition("  ")
        if not sep or SHA256.fullmatch(digest) is None or name in sums:
            raise CheckError(f"SHA256SUMS line {line!r} is not `<sha256>  <name>`, once a name")
        sums[name] = digest
    if sorted(sums) != sorted(release_files(version)):
        raise CheckError(f"SHA256SUMS names {sorted(sums)}, not {sorted(release_files(version))}")
    if text != format_sums(sums):
        raise CheckError("SHA256SUMS is not in the form `sums` writes")
    return sums


def check_sums(dist: Path, sums_text: str, version: str) -> dict[str, str]:
    """``dist`` holds exactly the files SHA256SUMS names, with those sha256s."""
    expected = parse_sums(sums_text, version)
    found = dist_sums(dist, version)
    for name, digest in expected.items():
        if found[name] != digest:
            raise CheckError(f"{name} has sha256 {found[name]}, SHA256SUMS says {digest}")
    return expected


def fetch_url(url: str, headers: Mapping[str, str]) -> bytes:
    """GET ``url``, which must be PyPI or its file host over HTTPS."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in ("pypi.org", FILES_HOST):
        raise CheckError(f"refusing to fetch {url}: not PyPI over HTTPS")
    request = urllib.request.Request(url, headers=dict(headers))
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 (checked above)
        return response.read()


def _json(raw: bytes, what: str) -> Any:
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise CheckError(f"{what} is not JSON: {exc}") from None


def listed_files(release: Any, version: str, names: Sequence[str]) -> dict[str, dict[str, Any]]:
    """The files PyPI's JSON for a release lists, by name. They must be exactly ``names``, and
    the JSON must be for ``version``."""
    if not isinstance(release, dict) or not isinstance(release.get("urls"), list):
        raise CheckError("PyPI's JSON for the release has no list of files")
    info = release.get("info")
    if not isinstance(info, dict) or info.get("version") != version:
        raise CheckError(f"PyPI's JSON is not for version {version}")
    files: dict[str, dict[str, Any]] = {}
    for entry in release["urls"]:
        name = entry.get("filename") if isinstance(entry, dict) else None
        if not isinstance(name, str) or name in files:
            raise CheckError(f"PyPI lists a file entry that is malformed or repeated: {entry!r}")
        files[name] = entry
    if sorted(files) != sorted(names):
        raise CheckError(f"PyPI lists {sorted(files)}, not exactly {sorted(names)}")
    return files


def check_provenance(
    provenance: Any, name: str, path: Path, version: str, publisher: Publisher, verify: Verify
) -> int:
    """``name`` (its bytes at ``path``) is attested by ``publisher`` (module docstring), as
    release ``version``. Returns how many attestations verified, at least one."""
    bundles = provenance.get("attestation_bundles") if isinstance(provenance, dict) else None
    if not isinstance(bundles, list) or not bundles:
        raise CheckError(f"{name}: PyPI's provenance has no attestation bundles")
    expected = {
        "kind": "GitHub",
        "repository": publisher.repository,
        "workflow": publisher.workflow,
        "environment": publisher.environment,
    }
    wanted = {
        RUNNER_ENVIRONMENT: "github-hosted",
        SOURCE_REPOSITORY_URI: f"https://github.com/{publisher.repository}",
        SOURCE_REPOSITORY_REF: publisher.ref(version),
    }
    count = 0
    for bundle in bundles:
        recorded = bundle.get("publisher") if isinstance(bundle, dict) else None
        if not isinstance(recorded, dict) or {k: recorded.get(k) for k in expected} != expected:
            raise CheckError(f"{name}: the publisher is {recorded!r}, not {expected}")
        attestations = bundle.get("attestations")
        if not isinstance(attestations, list) or not attestations:
            raise CheckError(f"{name}: an attestation bundle holds no attestation")
        for attestation in attestations:
            if not isinstance(attestation, dict):
                raise CheckError(f"{name}: an attestation is not an object")
            try:
                claims = verify(attestation, path, publisher)
            except Exception as exc:  # pypi-attestations' errors, and sigstore's beneath them
                raise CheckError(f"{name}: an attestation does not verify: {exc}") from None
            for oid, value in wanted.items():
                if claims.get(oid) != value:
                    raise CheckError(
                        f"{name}: the certificate's {oid} is {claims.get(oid)!r}, not {value!r}"
                    )
            count += 1
    return count


def verify_attestation(
    attestation: Mapping[str, Any], path: Path, publisher: Publisher
) -> Mapping[str, str]:
    """Verify one attestation with pypi-attestations against ``publisher``'s repository and
    workflow, which the certificate carries, and the file at ``path``; return the certificate's
    claims."""
    import pypi_attestations

    parsed = pypi_attestations.Attestation.model_validate(dict(attestation))
    identity = pypi_attestations.GitHubPublisher(
        repository=publisher.repository,
        workflow=publisher.workflow,
        environment=publisher.environment,
    )
    parsed.verify(identity, pypi_attestations.Distribution.from_file(path))
    return parsed.certificate_claims


def _wait_for_listing(
    url: str,
    version: str,
    names: Sequence[str],
    fetch: Fetch,
    *,
    wait: float,
    every: float,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> dict[str, dict[str, Any]]:
    """PyPI's listing of a release, read every ``every`` seconds for up to ``wait`` seconds until
    it lists ``names``; PyPI can take a while to list a new upload."""
    deadline = clock() + wait
    while True:
        try:
            return listed_files(_json(fetch(url, {}), "PyPI's JSON"), version, names)
        except (CheckError, OSError) as exc:
            if clock() >= deadline:
                raise CheckError(f"PyPI does not list the release: {exc}") from None
            print(f"waiting for PyPI to list the release: {exc}", flush=True)
            sleep(every)


def _check_served(
    project: str,
    version: str,
    files: Mapping[str, Mapping[str, Any]],
    sums: Mapping[str, str],
    place: Callable[[str, bytes], Path],
    publisher: Publisher,
    fetch: Fetch,
    verify: Verify,
) -> None:
    """Each listed file has the sha256 ``sums`` gives it, in PyPI's listing and in the bytes PyPI
    serves, and is attested by ``publisher``. ``place`` gives the path of a file's bytes."""
    for name, entry in sorted(files.items()):
        digests = entry.get("digests")
        listed = digests.get("sha256") if isinstance(digests, dict) else None
        if listed != sums[name]:
            raise CheckError(f"{name}: PyPI lists sha256 {listed}, expected {sums[name]}")
        file_url = entry.get("url")
        if not isinstance(file_url, str):
            raise CheckError(f"{name}: PyPI lists no URL")
        data = fetch(file_url, {})
        served = hashlib.sha256(data).hexdigest()
        if served != sums[name]:
            raise CheckError(f"{name}: PyPI serves sha256 {served}, expected {sums[name]}")
        provenance = _json(
            fetch(f"{PYPI}/integrity/{project}/{version}/{name}/provenance", INTEGRITY_ACCEPT),
            f"{name}'s provenance",
        )
        count = check_provenance(provenance, name, place(name, data), version, publisher, verify)
        print(f"{name}: sha256 {served}, served by PyPI; {count} attestation(s) by {publisher}")


def check_published(
    dist: Path,
    sums_text: str,
    version: str,
    *,
    fetch: Fetch = fetch_url,
    verify: Verify = verify_attestation,
    wait: float = 600.0,
    every: float = 15.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """PyPI serves the host release in ``dist`` exactly, attested by :data:`HOST`."""
    sums = check_sums(dist, sums_text, version)
    url = f"{PYPI}/pypi/{PROJECT}/{version}/json"
    names = release_files(version)
    files = _wait_for_listing(
        url, version, names, fetch, wait=wait, every=every, sleep=sleep, clock=clock
    )
    # The bytes checked against each attestation are the build job's, which must equal PyPI's.
    _check_served(PROJECT, version, files, sums, lambda name, _: dist / name, HOST, fetch, verify)


def locked_release(lock_text: str, name: str) -> tuple[str, dict[str, str]]:
    """The version of ``name`` that ``uv.lock`` pins, and the sha256 of each of its files there,
    by file name. It must come from PyPI and list at least one file."""
    lock = tomllib.loads(lock_text)
    found = [p for p in lock.get("package", []) if isinstance(p, dict) and p.get("name") == name]
    if len(found) != 1:
        raise CheckError(f"uv.lock pins {name} {len(found)} times, not once")
    package = found[0]
    if package.get("source") != {"registry": "https://pypi.org/simple"}:
        raise CheckError(f"uv.lock's {name} is not from PyPI: {package.get('source')!r}")
    version = package.get("version")
    if not isinstance(version, str):
        raise CheckError(f"uv.lock gives {name} no version")
    entries = [package["sdist"]] if "sdist" in package else []
    entries += package.get("wheels", [])
    sums: dict[str, str] = {}
    for entry in entries:
        url, digest = entry.get("url"), entry.get("hash")
        if not (isinstance(url, str) and isinstance(digest, str) and digest.startswith("sha256:")):
            raise CheckError(f"uv.lock's {name} lists a file without a URL and sha256: {entry!r}")
        file_name = urllib.parse.unquote(url.rsplit("/", 1)[-1])
        if file_name in sums or SHA256.fullmatch(digest[len("sha256:") :]) is None:
            raise CheckError(f"uv.lock's {name} lists {file_name} twice or with a bad sha256")
        sums[file_name] = digest[len("sha256:") :]
    if not sums:
        raise CheckError(f"uv.lock lists no file of {name}")
    return version, sums


def check_dependency(
    name: str,
    lock_text: str,
    scratch: Path,
    *,
    fetch: Fetch = fetch_url,
    verify: Verify = verify_attestation,
) -> None:
    """The release of ``name`` that the lock pins is what PyPI serves, attested by
    ``DEPENDENCIES[name]``. Its files are written under ``scratch`` to be verified."""
    publisher = DEPENDENCIES.get(name)
    if publisher is None:
        raise CheckError(f"{name} is not a dependency whose provenance is checked")
    version, sums = locked_release(lock_text, name)
    release = _json(fetch(f"{PYPI}/pypi/{name}/{version}/json", {}), "PyPI's JSON")
    files = listed_files(release, version, sorted(sums))

    def place(file_name: str, data: bytes) -> Path:
        path = scratch / file_name
        path.write_bytes(data)
        return path

    _check_served(name, version, files, sums, place, publisher, fetch, verify)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="check_release.py", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    tag = sub.add_parser("tag", help="the tag is v<version>, a final release")
    tag.add_argument("ref_name")
    sums = sub.add_parser("sums", help="write SHA256SUMS for the release's two files")
    sums.add_argument("dist", type=Path)
    sums.add_argument("--version", required=True)
    sums.add_argument("--out", type=Path, required=True)
    again = sub.add_parser("check-sums", help="the files still have those sha256s")
    again.add_argument("dist", type=Path)
    again.add_argument("sums", type=Path)
    again.add_argument("--version", required=True)
    published = sub.add_parser("published", help="PyPI serves and attests exactly these files")
    published.add_argument("dist", type=Path)
    published.add_argument("sums", type=Path)
    published.add_argument("--version", required=True)
    dependency = sub.add_parser("dependency", help="the locked release is PyPI's, attested")
    dependency.add_argument("name", choices=sorted(DEPENDENCIES))
    dependency.add_argument("--scratch", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Script entry point: ``python tools/release/check_release.py <command> ...``."""
    args = _parser().parse_args(argv)
    try:
        if args.command == "tag":
            version = package_version(VERSION_FILE.read_text(encoding="utf-8"))
            check_tag(args.ref_name, version)
            print(version)
        elif args.command == "sums":
            args.out.write_text(format_sums(dist_sums(args.dist, args.version)), encoding="utf-8")
            print(args.out.read_text(encoding="utf-8"), end="")
        elif args.command == "check-sums":
            check_sums(args.dist, args.sums.read_text(encoding="utf-8"), args.version)
            print(f"{args.dist} matches {args.sums}")
        elif args.command == "published":
            check_published(args.dist, args.sums.read_text(encoding="utf-8"), args.version)
        else:
            args.scratch.mkdir(parents=True, exist_ok=True)
            check_dependency(args.name, LOCK_FILE.read_text(encoding="utf-8"), args.scratch)
    except CheckError as exc:
        print(f"check_release.py {args.command}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
