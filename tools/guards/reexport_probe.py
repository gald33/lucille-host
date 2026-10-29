"""Write the AST lint's table of modules that the runtime's standard-library modules re-export.

A module reaches other modules through its attributes: ``tempfile._os`` is ``os``, ``enum.bltns``
is ``builtins``, ``logging.handlers.socket`` is ``socket``. The AST lint resolves those names
before it applies its rules, so ``from tempfile import _os`` followed by ``_os.system`` is
``os.system``, and ``from logging.handlers import socket`` is an import of ``socket``. It cannot
import a module to look, because its own rules refuse a dynamic import, so it reads a table,
``tools/guards/_reexports.py``, which this script writes.

The probe runs each interpreter it is given in a fresh, isolated process (``python -I``). That
process imports, by explicit ``import`` statements, every standard-library module the runtime may
import (``RUNTIME_IMPORTS`` and ``EXEC_IMPORTS`` in ``tools/guards/ast_lint.py``) and every
submodule of those that are packages, then lists each attribute of each of them whose value is a
module. The table keeps the union over the interpreters given, because a module's attributes
change between Python versions. ``tests/guards/test_reexports.py`` runs the probe on the
interpreter running the tests, on every CI runner, and fails if the table does not cover it.

Usage: ``python tools/guards/reexport_probe.py [PYTHON ...]`` (default: this interpreter).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):  # run as a script: make ``tools.guards`` importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.guards.ast_lint import EXEC_IMPORTS, RUNTIME_IMPORTS  # noqa: E402

TABLE = Path(__file__).resolve().parent / "_reexports.py"
# Allowlisted top-level modules that are not part of the standard library.
NOT_STDLIB = frozenset({"keyring", "switchboard", "lucille_host"})
MARKER = "REEXPORT-PROBE:"


def scanned_modules() -> tuple[str, ...]:
    """The standard-library modules the runtime may import, which the probe scans."""
    return tuple(sorted((RUNTIME_IMPORTS | EXEC_IMPORTS) - NOT_STDLIB))


def _run(python: str, code: str) -> str:
    """Run ``code`` in a fresh, isolated interpreter and return what it printed after the marker."""
    done = subprocess.run(
        [python, "-I", "-c", code], capture_output=True, text=True, check=False, timeout=300
    )
    if done.returncode != 0:
        raise RuntimeError(f"{python} failed:\n{done.stderr}")
    lines = [line for line in done.stdout.splitlines() if line.startswith(MARKER)]
    if len(lines) != 1:
        raise RuntimeError(f"{python} printed no result:\n{done.stdout}")
    return lines[0][len(MARKER) :]


def _submodules(python: str, modules: tuple[str, ...]) -> list[str]:
    """Every submodule of the given packages, found as files in that interpreter's stdlib."""
    stdlib = Path(
        _run(python, f"import sysconfig\nprint({MARKER!r} + sysconfig.get_path('stdlib'))\n")
    )
    found: list[str] = []
    for top in modules:
        package = stdlib / top
        if not package.is_dir():
            continue
        for path in sorted(package.rglob("*.py")):
            parts = list(path.relative_to(stdlib).with_suffix("").parts)
            if parts[-1] == "__init__":
                parts.pop()
            if parts[-1] == "__main__" or not all(part.isidentifier() for part in parts):
                continue
            if len(parts) > 1:
                found.append(".".join(parts))
    return found


# What the probe process runs after its import statements. It reads only modules it imported
# itself, by name, from sys.modules.
_SCAN = """
import json as _probe_json
import sys as _probe_sys
import types as _probe_types

_roots = {roots!r}
_names = sorted(n for n in _probe_sys.modules if n.split(".")[0] in _roots)
_reexports = {{}}
for _name in _names:
    _module = _probe_sys.modules[_name]
    for _attr, _value in sorted(vars(_module).items()):
        _dotted = _name + "." + _attr
        if isinstance(_value, _probe_types.ModuleType) and _dotted not in _probe_sys.modules:
            _reexports[_dotted] = _value.__name__
_submodules = [n for n in _names if "." in n]
print({marker!r} + _probe_json.dumps({{"reexports": _reexports, "submodules": _submodules}}))
"""


def probe(python: str = sys.executable) -> tuple[dict[str, str], set[str]]:
    """(re-exports, submodules) for one interpreter.

    The re-exports map each module-valued attribute of a scanned module, under every name it was
    imported as, to the module's own name, leaving out an attribute that is itself an import path
    (``os.path``, ``logging.handlers``). The submodules are the import paths, under the scanned
    modules, that name a module.
    """
    modules = scanned_modules()
    imports = [*modules, *_submodules(python, modules)]
    code = "".join(f"import {name}\n" for name in imports)
    code += _SCAN.format(roots=set(modules), marker=MARKER)
    result = json.loads(_run(python, code))
    return dict(result["reexports"]), set(result["submodules"])


def _tuple(names: set[str]) -> str:
    quoted = [f'"{name}"' for name in sorted(names)]
    return f"({quoted[0]},)" if len(quoted) == 1 else f"({', '.join(quoted)})"


def render(reexports: dict[str, set[str]], submodules: set[str], interpreters: list[str]) -> str:
    """The source of ``tools/guards/_reexports.py``."""
    lines = [
        '"""Modules the runtime\'s standard-library modules re-export. Generated: do not edit.',
        "",
        "Written by ``python tools/guards/reexport_probe.py``, which says how. Probed:",
        "",
        *(f"- {name}" for name in interpreters),
        "",
        "``tests/guards/test_reexports.py`` fails if this table does not cover the interpreter",
        "running the tests.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "from collections.abc import Mapping",
        "",
        "# The standard-library modules scanned: RUNTIME_IMPORTS and EXEC_IMPORTS, less the rest.",
        "SCANNED: frozenset[str] = frozenset(",
        "    {",
        *(f'        "{name}",' for name in scanned_modules()),
        "    }",
        ")",
        "",
        "# An attribute that is a module, and is not an import path, mapped to the module it is.",
        "REEXPORTS: Mapping[str, tuple[str, ...]] = {",
        *(f'    "{name}": {_tuple(targets)},' for name, targets in sorted(reexports.items())),
        "}",
        "",
        "# Import paths, under the scanned modules, that name a module.",
        "SUBMODULES: frozenset[str] = frozenset(",
        "    {",
        *(f'        "{name}",' for name in sorted(submodules)),
        "    }",
        ")",
        "",
    ]
    return "\n".join(lines)


def _describe(python: str) -> str:
    return _run(
        python,
        "import platform, sys\n"
        f"print({MARKER!r} + platform.python_implementation() + ' ' + platform.python_version()"
        " + ' on ' + sys.platform)\n",
    )


def main(argv: list[str] | None = None) -> int:
    """Probe each interpreter named (default: this one) and write the union to the table."""
    pythons = (sys.argv[1:] if argv is None else argv) or [sys.executable]
    reexports: dict[str, set[str]] = {}
    submodules: set[str] = set()
    described: list[str] = []
    for python in pythons:
        found, subs = probe(python)
        for name, target in found.items():
            reexports.setdefault(name, set()).add(target)
        submodules |= subs
        described.append(_describe(python))
    TABLE.write_text(render(reexports, submodules, sorted(set(described))))
    print(f"wrote {TABLE}: {len(reexports)} re-exports, {len(submodules)} submodules")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
