"""Report what an installed lucille-host holds of the spec. Not a test: test_packaged.py runs it.

``test_packaged.py`` runs this file with the interpreter of a fresh virtual environment that has
the built wheel installed, in isolated mode (``-I``): neither this file's directory, nor the
current directory, nor ``PYTHONPATH`` is on the import path, so ``lucille_host`` comes from that
install and never from the source tree. It writes the spec's directory, schemas, fixtures and
default host state, as the installed package loads them, to the JSON file its first argument
names.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

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
    }
    Path(out).write_text(json.dumps(report, ensure_ascii=True), encoding="ascii")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
