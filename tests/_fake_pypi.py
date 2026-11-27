"""A release of lucille-host as PyPI would serve it, and a host installed from it, for ``verify``.

:class:`FakeRelease` writes an installed host into a folder (the package and its ``.dist-info``, as
an installer leaves them), builds the wheel those files came from, and answers the requests
``lucille_host.verify`` makes: PyPI's JSON listing of the release, the bytes of each file, and each
file's provenance. Its attestations are stood-in objects that :meth:`FakeRelease.verify` checks in
place of sigstore: each names the file and sha256 it was made for, and the claims its certificate
would carry. Nothing reaches the network.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from lucille_host import verify

VERSION = "1.2.3"
PYPI = "https://pypi.org"
FILES = "https://files.pythonhosted.org/packages/ab/cd"

# The package's files, as the wheel holds them and an installer writes them.
PACKAGE_FILES = {
    "lucille_host/__init__.py": b'__version__ = "1.2.3"\n',
    "lucille_host/cli.py": b"def main():\n    return 0\n",
    "lucille_host/ops/__init__.py": b"OPERATIONS = {}\n",
    "lucille_host/spec/cli/v1/version.schema.json": b"{}\n",
}


def dist_info(version: str = VERSION) -> str:
    return f"lucille_host-{version}.dist-info"


def wheel_bytes(files: Mapping[str, bytes]) -> bytes:
    """A wheel holding ``files``, the same bytes each time it is built."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        for name, data in files.items():
            info = zipfile.ZipInfo(name, date_time=(2026, 11, 1, 12, 0, 0))
            info.external_attr = 0o644 << 16
            archive.writestr(info, data)
    return buffer.getvalue()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FakeRelease:
    """A release on a PyPI that answers only from what this object holds, and a host installed
    from it into ``site``."""

    def __init__(self, site: Path, version: str = VERSION) -> None:
        self.site = site
        self.version = version
        info = dist_info(version)
        # What the wheel holds, and what an installer copies from it as it is.
        metadata = f"Metadata-Version: 2.4\nName: lucille-host\nVersion: {version}\n"
        self.wheel_files: dict[str, bytes] = {
            **PACKAGE_FILES,
            f"{info}/METADATA": metadata.encode(),
            f"{info}/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            f"{info}/entry_points.txt": (
                b"[console_scripts]\nlucille-host = lucille_host.cli:main\n"
            ),
            f"{info}/licenses/LICENSE": b"MIT\n",
            f"{info}/RECORD": b"lucille_host/__init__.py,sha256=x,1\n",
        }
        self.wheel_name, self.sdist_name = verify.release_files(version)
        self.wheel = wheel_bytes(self.wheel_files)
        self.sdist = b"an sdist of lucille-host " + version.encode()
        # What PyPI serves, by URL: (status, body). Tests change these.
        self.served: dict[str, tuple[int, bytes]] = {}
        self.listing: dict[str, Any] = {
            "info": {"version": version, "yanked": False, "yanked_reason": None},
            "urls": [
                self._entry(self.wheel_name, self.wheel),
                self._entry(self.sdist_name, self.sdist),
            ],
        }
        self.provenance: dict[str, Any] = {
            name: {
                "version": 1,
                "attestation_bundles": [
                    {
                        "publisher": {
                            "kind": "GitHub",
                            "repository": "gald33/lucille-host",
                            "workflow": "release.yml",
                            "environment": "pypi",
                        },
                        "attestations": [self.attestation(name, sha256(data))],
                    }
                ],
            }
            for name, data in ((self.wheel_name, self.wheel), (self.sdist_name, self.sdist))
        }
        # Every request made, in order.
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.install()

    def _entry(self, name: str, data: bytes) -> dict[str, Any]:
        return {
            "filename": name,
            "digests": {"sha256": sha256(data)},
            "url": f"{FILES}/{name}",
            "yanked": False,
        }

    def attestation(self, name: str, digest: str, **claims: str) -> dict[str, Any]:
        """A stood-in attestation of ``name`` with sha256 ``digest``, by this repository at the
        release's tag from a GitHub-hosted runner unless ``claims`` say otherwise."""
        return {
            "signed": name,
            "sha256": digest,
            "claims": {
                verify.RUNNER_ENVIRONMENT: "github-hosted",
                verify.SOURCE_REPOSITORY_URI: "https://github.com/gald33/lucille-host",
                verify.SOURCE_REPOSITORY_REF: f"refs/tags/v{self.version}",
                **claims,
            },
        }

    def install(self) -> None:
        """Write the installed host into ``site``, as an installer does: every file of the wheel
        but its RECORD, a RECORD and an installer's own files of its own, and bytecode."""
        info = dist_info(self.version)
        for name, data in self.wheel_files.items():
            if name == f"{info}/RECORD":
                continue
            path = self.site / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        (self.site / info / "RECORD").write_text("rewritten by the installer\n")
        (self.site / info / "INSTALLER").write_text("uv\n")
        (self.site / info / "REQUESTED").write_text("")
        cache = self.site / "lucille_host" / "__pycache__"
        cache.mkdir(exist_ok=True)
        (cache / "__init__.cpython-313.pyc").write_bytes(b"bytecode")
        (cache / "cli.cpython-313.pyc").write_bytes(b"bytecode")

    def where(self) -> tuple[str, Path]:
        return self.version, self.site

    def fetch(self, url: str, headers: Mapping[str, str]) -> tuple[int, bytes]:
        """What this PyPI answers ``url`` with."""
        self.requests.append((url, dict(headers)))
        if url in self.served:
            return self.served[url]
        if url == f"{PYPI}/pypi/lucille-host/{self.version}/json":
            return 200, json.dumps(self.listing).encode()
        for name, data in ((self.wheel_name, self.wheel), (self.sdist_name, self.sdist)):
            if url == f"{FILES}/{name}":
                return 200, data
            if url == f"{PYPI}/integrity/lucille-host/{self.version}/{name}/provenance":
                assert headers == {"Accept": "application/vnd.pypi.integrity.v1+json"}
                if name not in self.provenance:
                    return 404, b'{"message": "no provenance"}'
                return 200, json.dumps(self.provenance[name]).encode()
        return 404, b"not found"

    def verify(self, attestation: Mapping[str, Any], name: str, digest: str) -> Mapping[str, str]:
        """Sigstore's verdict, stood in for: the attestation was made for this file's name and
        sha256, or it raises ValueError, as pypi-attestations' errors are."""
        if attestation.get("signed") != name or attestation.get("sha256") != digest:
            raise ValueError("subject does not match distribution digest")
        return attestation["claims"]
