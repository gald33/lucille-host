"""Report what an installed lucille-host holds of the spec. Not a test: test_packaged.py runs it.

``test_packaged.py`` runs this file with the interpreter of a fresh virtual environment that has
the built wheel installed, in isolated mode (``-I``): neither this file's directory, nor the
current directory, nor ``PYTHONPATH`` is on the import path, so ``lucille_host`` comes from that
install and never from the source tree. It writes the spec's directory, schemas, fixtures and
default host state, the command line contract's directory, schemas and fixtures, and the claim
``status`` prints, as the installed package holds them, to the JSON file its first argument names.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import lucille_host
from lucille_host import spec


def main(out: str) -> int:
    report = {
        "spec_dir": str(spec.spec_dir()),
        "schemas": {name: spec.load_schema(name) for name in spec.SCHEMA_NAMES},
        "fixtures": {
            kind: {path.name: spec.load_fixture(path) for path in spec.fixture_paths(kind)}
            for kind in spec.FIXTURE_KINDS
        },
        "host_state": spec.default_host_state(),
        "cli": {
            "spec_dir": str(spec.cli_spec_dir()),
            "schemas": {name: spec.load_cli_schema(name) for name in spec.CLI_TYPES},
            "fixtures": {
                path.name: spec.load_cli_fixture(path) for path in spec.cli_fixture_paths()
            },
        },
        # Read beside the package's own file, as report.py reads it: report.py itself needs the
        # host's dependencies, which this install leaves out.
        "claim": Path(lucille_host.__file__).with_name("claim.txt").read_text(encoding="ascii"),
    }
    Path(out).write_text(json.dumps(report, ensure_ascii=True), encoding="ascii")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
