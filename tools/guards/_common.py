"""Helpers the guards share: listing files, parsing Python, resolving names, running as a script."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from collections.abc import Callable, Collection, Container, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
# This package, and the directory that holds it.
OWN_PACKAGE = "lucille_host"
PACKAGE_PREFIX = "src/lucille_host/"

# Directories a tree walk skips. The walk is used only where git cannot list the files.
SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".tox",
        ".nox",
        "node_modules",
        "build",
        "dist",
    }
)


@dataclass(frozen=True, order=True)
class Violation:
    """One finding: where it is, which rule it breaks, and why."""

    path: str
    line: int
    rule: str
    message: str

    def __str__(self) -> str:
        where = f"{self.path}:{self.line}" if self.line > 0 else self.path
        return f"{where}: [{self.rule}] {self.message}"


# ---------------------------------------------------------------------------------------------
# Files


def _git_ls_files(root: Path, *, untracked: bool) -> list[str] | None:
    """Paths git lists under root, or None if root is not in a git work tree or git is missing."""
    argv = ["git", "-C", str(root), "ls-files", "-z", "--cached"]
    if untracked:
        argv += ["--others", "--exclude-standard"]
    try:
        done = subprocess.run(argv, capture_output=True, check=False, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0:
        return None
    return [name for name in os.fsdecode(done.stdout).split("\0") if name]


def _walk(root: Path) -> list[str]:
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        # A symbolic link to a directory is listed, not followed (os.walk does not follow it).
        links = [d for d in dirnames if (Path(dirpath) / d).is_symlink()]
        for name in [*filenames, *links]:
            found.append((Path(dirpath) / name).relative_to(root).as_posix())
    return found


def _kept(path: Path, symlinks: bool) -> bool:
    if path.is_symlink():
        return symlinks or path.is_file()
    return path.is_file()


def tracked_paths(root: Path) -> list[str]:
    """Every path git tracks under root, whatever it is (a gitlink to another repository, too),
    or, outside a work tree, every file the walk finds. Sorted."""
    names = _git_ls_files(root, untracked=False)
    if not names:
        names = _walk(root)
    return sorted(set(names))


def repo_files(root: Path, *, untracked: bool = False, symlinks: bool = False) -> list[str]:
    """Root-relative POSIX paths of the files a guard should read.

    In a git work tree these are the files git tracks, plus, with ``untracked=True``, the untracked
    files git does not ignore (so a new file is checked before it is added). Outside a work tree,
    or where nothing is tracked yet, the tree is walked instead, skipping :data:`SKIP_DIRS`; that
    reads more files, never fewer. A symbolic link is listed when it points to a file; with
    ``symlinks=True`` every symbolic link is listed, whatever it points to, for a caller that reads
    the link itself.
    """
    names = _git_ls_files(root, untracked=untracked)
    if not names:
        names = _walk(root)
    return sorted({name for name in names if _kept(root / name, symlinks)})


def python_files(root: Path) -> list[str]:
    """Every Python file in the repository: source, tests and tools alike."""
    return [name for name in repo_files(root, untracked=True) if name.endswith(".py")]


def symbolic_links(root: Path) -> list[str]:
    """Every symbolic link in the repository, whatever it points to: tracked, or untracked and not
    ignored by git (outside a work tree, every one the walk finds)."""
    return [
        name
        for name in repo_files(root, untracked=True, symlinks=True)
        if (root / name).is_symlink()
    ]


def own_modules(root: Path) -> frozenset[str]:
    """The dotted names of this package's modules and subpackages under ``root``."""
    found = {OWN_PACKAGE}
    for rel in python_files(root):
        if rel.startswith(PACKAGE_PREFIX):
            parts = rel[len("src/") : -len(".py")].split("/")
            if parts[-1] == "__init__":
                parts = parts[:-1]
            found.update(".".join(parts[:end]) for end in range(1, len(parts) + 1))
    return frozenset(found)


# ---------------------------------------------------------------------------------------------
# Python source


def parse(source: str | bytes, rel: str) -> ast.Module | Violation:
    """Parse one file. A file that does not parse is itself a violation: fail closed."""
    try:
        return ast.parse(source, filename=rel)
    except SyntaxError as exc:
        return Violation(rel, exc.lineno or 1, "parse", f"cannot parse: {exc.msg}")
    except ValueError as exc:
        return Violation(rel, 1, "parse", f"cannot parse: {exc}")


def package_of(rel: str) -> str:
    """The dotted package a file at ``rel`` belongs to, for resolving its relative imports."""
    parts = rel.split("/")
    if parts[0] == "src":
        parts = parts[1:]
    return ".".join(parts[:-1])


def absolute_module(module: str | None, level: int, package: str) -> str:
    """The absolute name of ``from <level dots><module> import ...`` inside ``package``."""
    if level == 0:
        return module or ""
    base = package.split(".") if package else []
    if level > 1:
        base = base[: max(len(base) - (level - 1), 0)]
    return ".".join([*base, *([module] if module else [])])


@dataclass(frozen=True)
class Ref:
    """One name the code refers to: an import, or a whole ``a.b.c`` chain of names.

    ``written`` is the chain as written (``""`` stands for a root that is an expression, as in
    ``f().x``). ``qualified`` is the same chain with its root resolved through the file's imports,
    so ``import os as o; o.system`` gives ``os.system``. ``call`` is set when the chain is the
    function of a call. ``rooted`` is true for an import, and for a chain whose root is a name the
    file's imports bind. ``annotation`` is true for a chain inside a type annotation.
    """

    line: int
    kind: str  # "import", "star" (``from m import *``) or "use"
    written: tuple[str, ...]
    qualified: str
    call: ast.Call | None = None
    rooted: bool = True
    annotation: bool = False

    def words(self) -> set[str]:
        """Every component, as written and as resolved."""
        return {*self.written, *self.qualified.split(".")} - {"", "<expr>"}


def import_aliases(tree: ast.Module, package: str) -> dict[str, str]:
    """Each name the file's imports bind, mapped to the dotted name it stands for.

    Scopes are ignored: an import inside a function counts for the whole file. That makes a guard
    stricter only while each name stands for one target, so a name the imports bind to two
    different targets is itself a finding (:func:`import_alias_conflicts`): here the last binding
    wins, and a use could mean the other one.
    """
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    aliases[alias.asname] = alias.name
                else:
                    top = alias.name.split(".")[0]
                    aliases[top] = top
        elif isinstance(node, ast.ImportFrom):
            module = absolute_module(node.module, node.level, package)
            for alias in node.names:
                if alias.name != "*":
                    target = f"{module}.{alias.name}" if module else alias.name
                    aliases[alias.asname or alias.name] = target
    return aliases


def import_alias_conflicts(tree: ast.Module, package: str, rel: str) -> list[Violation]:
    """A ``parse`` finding for each name the file's imports bind to more than one target
    (``import os as x`` in one place, ``import json as x`` in another): the guards resolve a name
    through one binding for the whole file, so they cannot tell which one a use means."""
    bound: dict[str, dict[str, int]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname or alias.name.split(".")[0]
                target = alias.name if alias.asname else name
                bound.setdefault(name, {}).setdefault(target, node.lineno)
        elif isinstance(node, ast.ImportFrom):
            module = absolute_module(node.module, node.level, package)
            for alias in node.names:
                if alias.name != "*":
                    target = f"{module}.{alias.name}" if module else alias.name
                    bound.setdefault(alias.asname or alias.name, {}).setdefault(target, node.lineno)
    found: list[Violation] = []
    for name, targets in sorted(bound.items()):
        if len(targets) > 1:
            line = max(targets.values())
            listed = ", ".join(sorted(targets))
            found.append(
                Violation(
                    rel,
                    line,
                    "parse",
                    f"{name} is imported as more than one thing ({listed}); give each its own name",
                )
            )
    return found


def annotation_nodes(tree: ast.Module) -> set[int]:
    """The ids of every node that sits inside a type annotation."""
    found: set[int] = set()
    for node in ast.walk(tree):
        annotation: ast.expr | None = None
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            annotation = node.returns
        elif isinstance(node, ast.arg | ast.AnnAssign):
            annotation = node.annotation
        if annotation is not None:
            found.update(id(sub) for sub in ast.walk(annotation))
    return found


class _Collector(ast.NodeVisitor):
    def __init__(self, aliases: dict[str, str], package: str, annotations: set[int]) -> None:
        self.aliases = aliases
        self.package = package
        self.annotations = annotations
        self.refs: list[Ref] = []

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            written = (*alias.name.split("."), *([alias.asname] if alias.asname else []))
            self.refs.append(Ref(node.lineno, "import", written, alias.name))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = absolute_module(node.module, node.level, self.package)
        from_parts = tuple(p for p in (node.module or "").split(".") if p)
        for alias in node.names:
            if alias.name == "*":
                self.refs.append(Ref(node.lineno, "star", from_parts, module))
                continue
            written = (*from_parts, alias.name, *([alias.asname] if alias.asname else []))
            qualified = f"{module}.{alias.name}" if module else alias.name
            self.refs.append(Ref(node.lineno, "import", written, qualified))

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name | ast.Attribute):
            self._chain(node.func, node)
        else:
            self.visit(node.func)
        for arg in node.args:
            self.visit(arg)
        for keyword in node.keywords:
            self.visit(keyword.value)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self._chain(node, None)

    def visit_MatchClass(self, node: ast.MatchClass) -> None:
        # ``case C(name=p)`` reads the attribute ``name`` of the subject, as ``subject.name`` does.
        for attr in node.kwd_attrs:
            self.refs.append(Ref(node.lineno, "use", ("", attr), f"<expr>.{attr}"))
        self.generic_visit(node)

    def _match_args(self, targets: list[ast.expr], value: ast.expr | None) -> None:
        # ``__match_args__`` names the attributes a positional class pattern reads.
        named = any(isinstance(t, ast.Name) and t.id == "__match_args__" for t in targets)
        if not named or not isinstance(value, ast.Tuple | ast.List):
            return
        for element in value.elts:
            if isinstance(element, ast.Constant) and isinstance(element.value, str):
                text = element.value
                self.refs.append(Ref(element.lineno, "use", ("", text), f"<expr>.{text}"))

    def visit_Assign(self, node: ast.Assign) -> None:
        self._match_args(node.targets, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._match_args([node.target], node.value)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        self._chain(node, None)

    def _chain(self, node: ast.expr, call: ast.Call | None) -> None:
        attrs: list[str] = []
        base = node
        while isinstance(base, ast.Attribute):
            attrs.append(base.attr)
            base = base.value
        attrs.reverse()
        rooted = isinstance(base, ast.Name) and base.id in self.aliases
        if isinstance(base, ast.Name):
            written = (base.id, *attrs)
            qualified = ".".join([self.aliases.get(base.id, base.id), *attrs])
        else:
            self.visit(base)
            written = ("", *attrs)
            qualified = ".".join(["<expr>", *attrs])
        in_annotation = id(node) in self.annotations
        self.refs.append(Ref(node.lineno, "use", written, qualified, call, rooted, in_annotation))


def references(tree: ast.Module, rel: str) -> list[Ref]:
    """Every import and every whole name chain in the file, in source order."""
    package = package_of(rel)
    collector = _Collector(import_aliases(tree, package), package, annotation_nodes(tree))
    collector.visit(tree)
    return collector.refs


def dotted_name(node: ast.expr, aliases: Mapping[str, str]) -> str | None:
    """The resolved dotted name of a ``Name``/``Attribute`` chain, or None for anything else."""
    attrs: list[str] = []
    while isinstance(node, ast.Attribute):
        attrs.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    return ".".join([aliases.get(node.id, node.id), *reversed(attrs)])


def anchored(
    qualified: str,
    *watched: Mapping[str, str],
    own: Container[str] = frozenset(),
    underscored: bool = False,
) -> list[str]:
    """``qualified``, plus each tail of it that starts at a watched component.

    The tail is re-anchored at that component's module, so a module reached through another
    module is still recognised: with ``subprocess`` watched, ``helpers.subprocess.Popen`` also
    gives ``subprocess.Popen``. With ``underscored``, a component spelled as a watched name with
    leading underscores counts as that name, because that is how modules bind the modules they
    import for their own use (``tempfile._os``, ``collections._sys``). Several mappings may be
    given; each is applied.

    A component that, with the components before it, names one of ``own`` (this package's own
    modules, :func:`own_modules`) is that module, not the watched module of the same name, and is
    not re-anchored: ``lucille_host.profile`` is a module of this package, not ``profile``. A
    watched module re-exported through one of this package's modules
    (``lucille_host.cli.inspect``) is not a module of this package, and is still re-anchored.
    """
    parts = qualified.split(".")
    names = [qualified]
    for mapping in watched:
        for index, part in enumerate(parts):
            module = mapping.get(part)
            if module is None and underscored:
                module = mapping.get(part.lstrip("_"))
            if module is not None and ".".join(parts[: index + 1]) not in own:
                names.append(".".join([module, *parts[index + 1 :]]))
    return list(dict.fromkeys(names))


def module_exports(root: Path) -> dict[str, dict[str, frozenset[str]]]:
    """What the names each of this package's modules binds stand for, by module.

    For each module under ``src/lucille_host/`` (the conformance package included), each name
    the module binds by an import, anywhere in the file, maps to what it imports
    (``import os as osmod`` gives ``osmod`` -> ``os``), and each name it binds by a plain
    assignment of a name chain that starts at one of its imports maps to that chain
    (``spawn = os.posix_spawn`` gives ``spawn`` -> ``os.posix_spawn``). A name bound more than one
    such way maps to each. Other bindings (a ``def``, a ``class``, any other value) are not
    followed. :func:`resolve` uses this to follow a name one module takes from another.
    """
    found: dict[str, dict[str, frozenset[str]]] = {}
    for rel in python_files(root):
        if not rel.startswith(PACKAGE_PREFIX):
            continue
        tree = parse((root / rel).read_bytes(), rel)
        if not isinstance(tree, ast.Module):
            continue
        parts = rel[len("src/") : -len(".py")].split("/")
        if parts[-1] == "__init__":
            parts = parts[:-1]
        found[".".join(parts)] = file_exports(tree, rel)
    return found


def file_exports(tree: ast.Module, rel: str) -> dict[str, frozenset[str]]:
    """The names one file binds by import or by assigning an imported chain (see
    :func:`module_exports`), each mapped to what it stands for."""
    package = package_of(rel)
    aliases = import_aliases(tree, package)
    exports: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    exports.setdefault(alias.asname, set()).add(alias.name)
                else:
                    top = alias.name.split(".")[0]
                    exports.setdefault(top, set()).add(top)
        elif isinstance(node, ast.ImportFrom):
            module = absolute_module(node.module, node.level, package)
            for alias in node.names:
                if alias.name != "*":
                    target = f"{module}.{alias.name}" if module else alias.name
                    exports.setdefault(alias.asname or alias.name, set()).add(target)
        elif isinstance(node, ast.Assign | ast.AnnAssign) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            root = node.value
            while isinstance(root, ast.Attribute):
                root = root.value
            value = dotted_name(node.value, aliases)
            if value is None or not (isinstance(root, ast.Name) and root.id in aliases):
                continue
            for target in targets:
                if isinstance(target, ast.Name):
                    exports.setdefault(target.id, set()).add(value)
    return {name: frozenset(targets) for name, targets in exports.items()}


# The most names :func:`resolve` returns for one reference.
RESOLVE_LIMIT = 256


def resolve(
    qualified: str,
    reexports: Mapping[str, Sequence[str]],
    exports: Mapping[str, Mapping[str, Collection[str]]],
    limit: int = RESOLVE_LIMIT,
) -> list[str]:
    """``qualified``, plus every name it stands for once re-exported modules are followed.

    Two kinds of re-export are followed, at any position in the chain and repeatedly:

    - ``reexports``, a table of attributes of known modules that are themselves modules
      (``tempfile._os`` is ``os``, so ``tempfile._os.system`` gives ``os.system``);
    - ``exports`` (:func:`module_exports`), the names this package's own modules bind
      (``lucille_host.paths.osmod.system`` gives ``os.system`` when ``paths`` has
      ``import os as osmod``).

    Each rewrite adds a name; none is dropped, so a name that could stand for several things is
    checked as each of them. At most ``limit`` names are returned; a caller that gets ``limit``
    names back cannot know it has them all, and should fail closed.
    """
    found = [qualified]
    pending = [qualified]
    while pending and len(found) < limit:
        parts = pending.pop().split(".")
        for end in range(1, len(parts) + 1):
            prefix = ".".join(parts[:end])
            targets = list(reexports.get(prefix, ()))
            if end > 1:
                owner = exports.get(".".join(parts[: end - 1]), {})
                targets += sorted(owner.get(parts[end - 1], ()))
            for target in targets:
                name = ".".join([target, *parts[end:]])
                if target != prefix and name not in found:
                    found.append(name)
                    pending.append(name)
    return found[:limit]


def matches(name: str, target: str) -> bool:
    """True when ``name`` is ``target`` or a member of it (``os.system`` matches ``os``)."""
    return name == target or name.startswith(target + ".")


# ---------------------------------------------------------------------------------------------
# Running a guard as a script


def run_guard(
    name: str,
    argv: Sequence[str],
    files: Callable[[Path], Sequence[str]],
    check: Callable[[Path], list[Violation]],
) -> int:
    """Run a guard over ``[ROOT]`` (default: this repository) and return its exit code.

    0 clean, 1 violations, 2 usage error. Finding nothing to check is a usage error, because a
    guard that read no file has nothing to say about the tree.
    """
    usage = f"usage: python tools/guards/{name}.py [ROOT]"
    args = list(argv)
    if args and args[0] in ("-h", "--help"):
        print(usage)
        return 0
    if len(args) > 1 or (args and args[0].startswith("-")):
        print(usage, file=sys.stderr)
        return 2
    root = Path(args[0]).resolve() if args else REPO_ROOT
    if not root.is_dir():
        print(f"{name}: not a directory: {root}", file=sys.stderr)
        return 2
    count = len(files(root))
    if count == 0:
        print(f"{name}: nothing to check under {root}; not reporting clean", file=sys.stderr)
        return 2
    violations = check(root)
    for violation in violations:
        print(violation)
    print(f"{name}: {count} file(s) checked, {len(violations)} violation(s)")
    return 1 if violations else 0
