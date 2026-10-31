"""AST lint: no shell, no code from strings, no dynamic import; processes start in one place.

The host's promise is that it runs only reviewed operations, and runs them as argv lists. These
rules keep the code where a reader can check that promise. The rule name of each finding is in
brackets.

Everywhere (every Python file in the repository: source, tests and tools):

- [shell] no ``shell=`` keyword on any call unless it is the literal ``False``, and no API that
  hands a command line to a shell: ``os.system``, ``os.popen`` (and their ``posix`` twins),
  ``subprocess.getoutput``, ``subprocess.getstatusoutput``, ``asyncio.create_subprocess_shell``
  (also reached as ``asyncio.subprocess.create_subprocess_shell``, the same function), an event
  loop's ``subprocess_shell``, or the module ``pipes``, which runs its pipelines through a shell;
- [launcher] a process launcher (``subprocess.run``, ``Popen``, ``call``, ``check_call``,
  ``check_output``, ``asyncio.create_subprocess_exec``, which is also
  ``asyncio.subprocess.create_subprocess_exec``, an event loop's ``subprocess_exec``) is only
  ever called directly: never assigned, passed, wrapped or reached through one of its members. A
  ``subprocess`` launcher takes argv as its only positional argument, because Popen's third and
  ninth positional parameters are ``executable`` and ``shell``; and no launcher call takes ``**``
  keywords, which could carry either. A launcher named in a type annotation is fine;
- [eval-exec] no ``eval``, ``exec`` or ``compile``, and no ``__builtins__``, which reaches them by
  name, whether as a name or as the string ``'__builtins__'`` anywhere (a key of ``globals()``,
  say); no ``globals``, ``locals``, ``vars`` with no argument (which is ``locals()``, and so
  ``vars`` used as a value, or called with an unpacked argument list that could be empty),
  ``sys._getframe``, ``sys._current_frames`` or the frame functions of ``inspect``
  (``currentframe``, ``stack``, ``trace``, ``getouterframes``, ``getinnerframes``), which reach a
  namespace holding them; no ``gc``, whose ``get_objects``, ``get_referrers`` and
  ``get_referents`` reach every live object, frames and module namespaces among them; no
  ``types.CodeType``, which builds code from bytes; no ``typing.get_type_hints``,
  ``typing.ForwardRef``, ``typing.evaluate_forward_ref`` or ``typing._eval_type``, which evaluate
  strings as code; no ``sys.remote_exec``, which runs a Python file in another process; no
  ``sys.settrace``, ``sys.setprofile``, ``sys.monitoring``, ``threading.settrace``,
  ``threading.setprofile`` (or their ``_all_threads`` forms) or ``logging.currentframe``, which
  hand frames or code objects to a callback (a ``signal`` handler is handed a frame too; ``signal``
  is allowed, because the frame attributes that lead on are refused under [dunder]); and no
  module that runs strings or stored data as code: ``code``, ``codeop``, ``timeit``, ``pdb``,
  ``bdb``, ``profile``, ``cProfile``, ``trace``, ``doctest``, ``pickle``, ``_pickle``,
  ``marshal``, ``shelve``, ``_interpreters``, ``_xxsubinterpreters`` and
  ``concurrent.interpreters`` (which run source strings in a subinterpreter), and ``distutils``
  (which runs setup scripts and starts compilers);
- [dynamic-import] no import of a module named at run time: ``__import__``, any member of
  ``importlib`` except ``importlib.metadata.version``, ``importlib.metadata.metadata``,
  ``importlib.metadata.distribution``, ``importlib.metadata.files``,
  ``importlib.metadata.PackagePath``, ``importlib.metadata.PackageNotFoundError``,
  ``importlib.resources.as_file`` and the type ``importlib.resources.abc.Traversable`` (not
  ``importlib.resources.files``, which imports the module its argument names), and the modules
  ``runpy``, ``imp``, ``pkgutil``, ``zipimport``, ``pydoc``, ``pkg_resources`` and
  ``logging.config`` (whose configuration names objects to import); no ``help`` by name, as a
  value or through ``builtins``, which imports the module or object a string names (a variable
  can hold the string); no
  ``breakpoint``, ``sys.breakpointhook`` or ``sys.__breakpointhook__``, which import whatever
  ``PYTHONBREAKPOINT`` names; no ``keyring.core.load_keyring`` or
  ``keyring.core._load_keyring_class``, which import the module a string names; and no attribute
  ``entry_points``, on any object, because an entry point imports the object it names (a
  distribution's ``entry_points`` is how the allowed ``distribution`` and ``files`` could reach
  one);
- [native-code] no ``ctypes``, ``_ctypes``, ``cffi``, ``_cffi_backend`` or ``win32ctypes``, which
  call native code that can do anything, starting a process included (``cffi`` is in the runtime
  dependencies, through ``cryptography``, and ``win32ctypes`` is, on Windows, through keyring);
- [module-object] a module this lint watches (see :data:`WATCHED` and :data:`ROOTED_WATCHED`) is
  never used as a value, only as the base of a member it names: ``getattr(os, name)``,
  ``vars(os)`` or passing ``os`` to a function would let a string pick any member. An attribute
  that merely shares a :data:`WATCHED` module's name counts as that module
  (``pytest.mark.asyncio`` is ``asyncio``). A test that must replace a member of a watched module
  patches it by its dotted path, as a string (``monkeypatch.setattr("os.getuid", fake)``), which
  this rule allows;
- [dunder] no attribute that leads from an object to modules or code by the object graph (an
  attribute a ``match`` class pattern reads, ``case C(__globals__=g)``, or a ``__match_args__``
  entry names, counts as an attribute read):
  ``__dict__``, ``__globals__``, ``__builtins__``, ``__self__``, ``__class__``, ``__bases__``,
  ``__base__``, ``__mro__``, ``__subclasses__``, ``__getattribute__``, ``__code__``,
  ``__closure__``, ``__func__``, ``__loader__``, ``__spec__``, ``__import__``, the frame
  attributes ``f_globals``, ``f_builtins``, ``f_locals``, ``gi_frame``, ``cr_frame``,
  ``ag_frame`` and ``tb_frame``, and the attributes that lead to a code object or its bytes,
  ``gi_code``, ``cr_code``, ``ag_code``, ``f_code`` and ``co_code`` (``code.replace(co_code=...)``
  and ``type(function)(code, {})`` run code built from bytes). Each dunder in this list is refused
  as a bare name too: a module's own ``__loader__`` and ``__spec__``, a method's ``__class__``;
- the same names spelled as a string are refused where a string becomes an attribute: a string
  argument to ``getattr``, ``setattr``, ``delattr``, ``hasattr``, ``operator.attrgetter`` or the
  first argument of ``operator.methodcaller`` that names one of the members in
  :data:`MEMBERS_BY_STRING` (``"system"``, ``"exec"``, ``"import_module"``, ...) or one of those
  attributes. It is reported under the rule of the member it names;
- [star-import] no ``from <module> import *`` from a module this lint watches, which would hide
  the names above (in ``src/lucille_host/``, from any module: see below);
- [parse] a file that does not parse is a finding; so is a name the file's imports bind to two
  different things (``import os as x`` here, ``import json as x`` there), because names are
  resolved without regard to scope and a use could mean either; and so is a name that stands for
  more names through re-exported modules (see "How names are resolved") than the lint follows: the
  lint fails closed;
- [symlink] no symbolic link anywhere in the repository, tracked or untracked and not ignored by
  git, whatever it points to. Python and the wheel build follow a link and this lint does not, so
  the code behind it would be checked under the rules of the place it really is, not the place it
  is imported from (a link in ``src/lucille_host/`` to a directory under ``tests/`` would make test
  code a runtime module), or, behind a linked directory, not at all;
- [compiled] no compiled file (``.pyc``, ``.pyo``, ``.so``, ``.pyd`` or ``.dylib``, in any case)
  anywhere in the repository, tracked or untracked and not ignored by git. The lint reads only
  ``.py`` source, and Python loads an extension module ahead of the ``.py`` file of the same name,
  and a lone ``.pyc`` file where there is no ``.py`` file. A compiled file git ignores is not part
  of the repository and is not reported; one added to git anyway is. Outside a git work tree the
  tree is walked, skipping ``__pycache__`` (which Python writes on every run) and the other build
  caches.

In ``src/lucille_host/``, the runtime and the conformance package alike:

- [dynamic-import] no ``sys.modules``, which reaches a module by a string;
- [import-path] no ``sys.path``, ``sys.meta_path``, ``sys.path_hooks``,
  ``sys.path_importer_cache`` or ``site``, which change what an ``import`` statement loads; no
  ``__path__``, ``__package__``, ``__spec__`` or ``__loader__``, as a name or an attribute, read
  or written, or named by a string to ``getattr`` and its relatives (a package's ``__path__``
  decides where its submodules are found, and a relative import is resolved through the module's
  ``__package__``, ``__spec__`` and ``__name__``); and nothing that binds, assigns or deletes those
  or ``__name__`` (``__name__ = ...``, ``import x as __package__``, ``module.__name__ = ...``,
  ``setattr(module, "__name__", ...)``), though ``__name__`` may be read
  (``if __name__ == "__main__"``). These are the ways the lint knows to make an ``import``
  statement load something other than what the lint resolves it to;
- [star-import] no ``from <module> import *`` at all: it binds names the lint cannot see.

In the runtime package, ``src/lucille_host/`` except ``src/lucille_host/conformance/``:

- [runtime-import] every module an ``import`` statement names, anywhere in a file, and every
  module a name reaches through a re-export the lint follows (see "How names are resolved"), is on
  an allowlist, :data:`RUNTIME_IMPORTS`: a short list of standard-library modules, each with the
  reason it is there, plus ``keyring`` (through :data:`KEYRING_SURFACE`), ``switchboard`` (which
  the Switchboard-surface lint keeps to one module and a listed surface) and ``lucille_host``
  itself. ``src/lucille_host/_exec.py`` may also import the modules in :data:`EXEC_IMPORTS`. A
  relative import resolves into this package and is allowed, except one of the conformance
  package: the runtime may not import or use ``lucille_host.conformance``, which is not held to
  this list. The list names top-level modules, so a submodule of an allowed module comes with it
  (``os.path``, ``logging.handlers``), but a module an allowed module merely imports does not:
  ``from logging.handlers import socket`` imports ``socket``, and ``from enum import bltns``
  imports ``builtins``. Widening the list is a reviewed change, and the re-export table is then
  written again;
- [keyring-surface] of ``keyring``, only the members in :data:`KEYRING_SURFACE` (getting, setting
  and deleting a password, the backend in use, the errors, and the backend classes the host must
  recognise), the members of those, and, as an import or in a type annotation, the modules on the
  way to them. ``keyring.core.load_keyring`` imports a module a string names, and
  ``keyring.util.platform_.platform`` is ``platform``;
- [module-object] no module is used as a value, not only the watched ones: not an allowed
  standard-library module or one of its submodules or re-exports, not keyring, and not a module of
  this package. A module used as a value lets ``getattr`` search it with a string;
- [spawn-outside-exec] nothing starts a process except ``src/lucille_host/_exec.py``. Starting a
  process means importing or referring to ``subprocess``, ``pty``, ``multiprocessing``,
  ``asyncio.subprocess``, ``asyncio.create_subprocess_*``, an event loop's
  ``subprocess_exec``/``subprocess_shell``, ``concurrent.futures.ProcessPoolExecutor``,
  ``_posixsubprocess``, ``webbrowser`` (which starts a browser), ``pipes``, ``distutils``, or
  ``os.spawn*``, ``os.exec*``, ``os.posix_spawn*``, ``os.fork*`` or ``os.startfile`` (and their
  ``posix`` twins). The exemption is for that exact path only;
- [launcher] in ``src/lucille_host/_exec.py``, anything that starts a process (the list above) is
  called directly: never assigned, passed or wrapped, so that it cannot leave the module under a
  name the lint does not follow.

In the conformance package, ``src/lucille_host/conformance/``, which drives a local hub and the
installed host and is not part of the runtime (it is not held to the runtime's import allowlist,
so a standard-library module that starts a process inside, as ``platform.architecture`` runs
``file``, is not refused there):

- [conformance-spawn] only ``src/lucille_host/conformance/_spawn.py`` may start a process. Other
  modules of the package may import ``subprocess`` or ``asyncio.subprocess`` only for the members
  that start nothing (:data:`SAFE_MEMBERS`: pipes, results and errors), and may name a launcher in
  a type annotation;
- [conformance-argv] ``_spawn.py`` starts a process only by calling ``subprocess.run``,
  ``Popen``, ``call``, ``check_call`` or ``check_output`` with an argv list literal, or
  ``asyncio.create_subprocess_exec`` with literal arguments. The first element must be
  ``"switchboard"`` (followed by ``"serve"``: the local hub) or ``"lucille-host"`` (the installed
  host), written literally or as one of the constants in :data:`PROGRAM_CONSTANTS`, assigned once
  at module level to that name, to ``shutil.which(<name>)``, to ``Path(sys.executable).parent /
  <name>`` (the program installed beside the interpreter running the suite), or to
  ``Path(sysconfig.get_path("scripts")) / <name>``. Later elements may be unpacked
  (``[LUCILLE_HOST_EXE, *args]``). ``executable=``, ``program=`` and ``**`` keywords are refused,
  because each could replace the program, and so is ``env=`` when the program is a bare name,
  because the child's ``PATH`` decides which program a bare name runs. ``Path``, ``shutil.which``,
  ``sys.executable`` and ``sysconfig.get_path`` must mean what they say, so ``_spawn.py`` binds
  each name that stands for ``pathlib``, ``shutil``, ``sys`` or ``sysconfig`` (or one of their
  members) exactly once, by its import; never assigns to or deletes an attribute or an item of a
  name an import binds (``shutil.which = ...``, ``sys.executable = ...``,
  ``os.environ["PATH"] = ...``); and never calls ``setattr`` or ``delattr``. A method call that
  changes a module's state (``os.environ.update(...)``) is not seen; review covers it.

How names are resolved. A name is resolved through the file's imports (``import os as o;
o.system`` is ``os.system``). A module is then followed wherever the lint can see it re-exported,
at any position in a chain, and every rule above applies to each name that gives:

- through :data:`~tools.guards._reexports.REEXPORTS`, a table of the attributes of the runtime's
  allowed standard-library modules that are modules (``tempfile._os`` is ``os``, ``enum.bltns``
  is ``builtins``, ``logging.handlers.socket`` is ``socket``). The lint cannot import a module to
  look, so ``tools/guards/reexport_probe.py`` writes the table, and
  ``tests/guards/test_reexports.py`` fails on any interpreter CI tests that it does not cover;
- through what this package's own modules bind, by an import or by a plain assignment of a chain
  that starts at an import (``paths.osmod`` is ``os`` when ``paths`` has ``import os as osmod``,
  and ``_exec.launch`` is ``subprocess.Popen`` when ``_exec`` has ``from subprocess import Popen
  as launch``). A function or class a module defines is not followed: that is how ``_exec.run``
  is called;
- through a watched module's name, or that name with leading underscores, wherever it sits in a
  chain (``helpers.subprocess``, ``random._os``).

A module of this package is itself, whatever its name: ``lucille_host/profile.py`` or
``lucille_host.ops.types`` is not the standard library's ``profile`` or ``types``. A watched module
re-exported through a module of this package (``lucille_host.cli.inspect``) is still the watched
module.

What this lint is, and what it cannot see. The everywhere rules above are a denylist: they name
the forms of a shell, of code run from a string and of a dynamic import that are known, and a form
they do not name passes. A denylist is not a sandbox. Python has more ways to reach a namespace, a
module or a code object than any list holds, and these lists will grow as more are found. The
runtime package is held to more than the denylist: the modules its imports name, and the modules
its names reach through the re-exports above, must be on an allowlist, and no module may be used
there as a value. That binds ``import`` statements as the lint resolves them; [import-path] refuses
the ways the lint knows to make Python resolve them otherwise. Known blind spots:

- values returned by calls: an attribute of what a call returns (``f().system``), and a module,
  function or attribute reached that way or through an object that is not a module, is not
  followed;
- a module re-exported where the lint does not look: by a module outside this package and outside
  the table (one that tests, tools or the conformance package import), under a name that is
  neither a watched name nor one with leading underscores; or by one of this package's modules
  other than by an import or a plain assignment (in a tuple, as a class attribute, as what a
  function returns). The runtime package is the least exposed: its imports and the modules they
  re-export must be on the allowlist, and it may use no module as a value. The table covers the
  interpreters it was written with and the ones CI tests, not others;
- ``getattr`` passed as a value, or called with a name that is not a string literal: the string
  rules read only literal arguments of a call written as ``getattr(...)`` (or ``setattr``,
  ``operator.attrgetter`` and the rest), so ``lookup = getattr`` or ``getattr(config, name)``
  on an object that is not a watched module is not checked;
- ``vars(<object>)``, which returns the object's ``__dict__``, the attribute [dunder] refuses;
- format-string field lookups: ``"{0.__globals__}".format(f)`` reads attributes, dunders included,
  that are named inside a string, and ``string.Formatter`` hands back what it looks up; the lint
  does not read format strings. ``str.format`` needs no import; ``string``, and so
  ``string.Formatter``, is kept out of the runtime, also where an allowed module re-exports it
  (``tomllib._parser.string``);
- compiled modules: the lint does not read ``.pyc``, ``.so`` or other compiled files. It refuses
  those in the repository ([compiled]), and cannot see one installed anywhere else;
- code outside this repository. The standard library and the dependencies can start processes,
  import by name or run strings on the code's behalf, and the lint does not trace calls into them:
  agent-switchboard 2.6.0's client can run ``git``; keyring imports the backend an environment
  variable, its configuration file or an installed entry point names, when the host first asks it
  for a password; ``platform`` runs ``uname`` and ``file``, and ``uuid.getnode`` runs ``ip`` or
  ``ifconfig``. The allowlist keeps ``platform`` and ``uuid`` out of the runtime, also where an
  allowed module re-exports them (keyring re-exports ``platform``; :data:`KEYRING_SURFACE` keeps
  it out); it cannot keep an allowed module from doing what it does inside. The native-code list
  was checked against the dependencies locked in ``uv.lock`` on 2026-10-28: ``cffi`` is the module
  there that exists to call native code on every platform, and ``win32ctypes`` (pywin32-ctypes,
  which keyring installs on Windows only) on Windows. Check it again when the lock changes;
- anything about the files it does not parse: configuration, data and shell scripts.

Review covers the rest, and the tests of the code that is written.
"""

from __future__ import annotations

import ast
import functools
import re
import sys
from collections import Counter
from collections.abc import Mapping
from pathlib import Path

if __package__ in (None, ""):  # run as a script: make ``tools.guards`` importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.guards._common import (  # noqa: E402
    OWN_PACKAGE,
    PACKAGE_PREFIX,
    REPO_ROOT,
    RESOLVE_LIMIT,
    Ref,
    Violation,
    absolute_module,
    anchored,
    dotted_name,
    import_alias_conflicts,
    import_aliases,
    matches,
    module_exports,
    own_modules,
    package_of,
    parse,
    python_files,
    references,
    repo_files,
    resolve,
    run_guard,
    symbolic_links,
)
from tools.guards._reexports import REEXPORTS, SUBMODULES  # noqa: E402

CONFORMANCE_PREFIX = "src/lucille_host/conformance/"
CONFORMANCE_PACKAGE = "lucille_host.conformance"
EXEC_MODULE = "src/lucille_host/_exec.py"
SPAWN_MODULE = "src/lucille_host/conformance/_spawn.py"

# What the runtime package may import: top-level modules, each for the reason given. Each is a
# standard-library module the runtime needs now or plainly will soon, or keyring, switchboard or
# this package itself. A standard-library module here can still start a process or run a string as
# code through some of its members and through the modules it imports: the rules below refuse the
# members they name, and a module an allowed module re-exports is checked as itself, through the
# table in tools/guards/_reexports.py (``tempfile._os`` is ``os``; ``logging.handlers.socket`` is
# ``socket``, which is not on this list). Widening this list is a reviewed change, and the table
# must then be written again (tools/guards/reexport_probe.py).
RUNTIME_IMPORTS = frozenset(
    {
        "__future__",  # compiler directives only
        "argparse",  # the command line; parses arguments and starts nothing
        "base64",  # encodes bytes
        "binascii",  # encodes bytes
        "collections",  # containers; namedtuple builds its class from identifiers only
        "contextlib",  # context managers
        "copy",  # copies objects
        "dataclasses",  # builds methods from field names, which must be identifiers
        "datetime",  # dates and times
        "enum",  # enumerations
        "errno",  # error numbers
        "functools",  # wraps functions; wrapping a launcher is refused by [launcher]
        "hashlib",  # digests
        "hmac",  # keyed digests
        "io",  # in-memory and file streams
        "itertools",  # iterators
        "json",  # the wire format
        "logging",  # the logs; logging.config, which imports what it is told, is refused
        "math",  # arithmetic
        "os",  # files, the environment, ids; every way os starts a process or a shell is refused
        "pathlib",  # paths
        "re",  # regular expressions
        "secrets",  # random tokens
        "shutil",  # copies and removes files and finds a program on PATH; starts nothing
        "signal",  # stops the read loop cleanly on a signal
        "stat",  # file modes
        "sys",  # the interpreter; its frame, module, import-path and breakpoint hooks are refused
        "tempfile",  # private temporary files
        "termios",  # discards what was typed ahead at a terminal before `pair` asks; runs nothing
        "textwrap",  # formats messages
        "threading",  # the read loop and its timers
        "time",  # clocks and sleeps
        "tomllib",  # reads the local policy file, as the design says; parses and runs nothing
        "types",  # MappingProxyType and other read-only types; types.CodeType is refused
        "typing",  # annotations; get_type_hints and ForwardRef, which eval strings, are refused
        # The packages the runtime is built on.
        "keyring",  # the store for the pair secret, through KEYRING_SURFACE only
        "switchboard",  # the transport; switchboard_lint keeps it to _switchboard.py
        OWN_PACKAGE,  # this package's own modules, the conformance package excepted
    }
)
# Not on the list, though they look harmless: platform (platform.architecture runs ``file`` and
# platform.processor runs ``uname -p``), uuid (getnode and uuid1 can run ``ip`` or ``ifconfig``),
# string (string.Formatter looks up the attributes a format string names, dunders included, and
# returns them) and socket (the host opens no socket of its own; Switchboard's client does that).
# Each stays off the list when an allowed module re-exports it, as tomllib._parser.string and
# logging.handlers.socket do.

# Of keyring, the runtime may use only these members (and the members of these, which this lint
# does not follow), the modules on the way to them, and nothing else: keyring.core.load_keyring,
# for one, imports any module a string names. What keyring loads inside these calls (the backend an
# environment variable, its configuration file or an installed entry point names) is outside this
# lint.
KEYRING_SURFACE = frozenset(
    {
        "keyring.get_password",
        "keyring.set_password",
        "keyring.delete_password",
        "keyring.get_keyring",
        "keyring.errors.KeyringError",
        "keyring.errors.PasswordSetError",
        "keyring.errors.PasswordDeleteError",
        "keyring.errors.InitError",
        "keyring.errors.KeyringLocked",
        "keyring.errors.NoKeyringError",
        "keyring.backend.KeyringBackend",
        # The backends that store nothing, so that the host can refuse them.
        "keyring.backends.fail.Keyring",
        "keyring.backends.null.Keyring",
        "keyring.backends.chainer.ChainerBackend",
    }
)

# What src/lucille_host/_exec.py may import as well: it is the one place a process starts. It reads
# the child's output with reader threads (threading is on the list above): a selector reads nothing
# until a file is registered with it, and switchboard_lint refuses ``register`` everywhere.
EXEC_IMPORTS = frozenset(
    {
        "subprocess",  # starts the process, argv only
    }
)

# Compiled files, which the lint cannot read, by suffix (compared in lower case).
COMPILED_SUFFIXES = (".pyc", ".pyo", ".so", ".pyd", ".dylib")


def _watch(*names: str) -> dict[str, str]:
    return {name: name for name in names}


# Modules this lint follows, even when reached through another module or an attribute of the same
# name. None of them may be used as a value.
WATCHED: Mapping[str, str] = _watch(
    "os",
    "posix",
    "sys",
    "subprocess",
    "pty",
    "multiprocessing",
    "asyncio",
    "concurrent",
    "importlib",
    "runpy",
    "imp",
    "builtins",
    "_posixsubprocess",
    "pkgutil",
    "zipimport",
    "pydoc",
    "pkg_resources",
    "ctypes",
    "_ctypes",
    "webbrowser",
    "codeop",
    "timeit",
    "pdb",
    "bdb",
    "cProfile",
    "doctest",
    "pickle",
    "_pickle",
    "marshal",
    "shelve",
    "cffi",
    "_cffi_backend",
    "_interpreters",
    "_xxsubinterpreters",
    "distutils",
    "win32ctypes",
)
# Modules whose names are also common attribute and variable names (``exc.code``, a local
# ``site``, ``config.types``, a process's ``pipes``). They are followed, and banned, only through
# an import or a chain whose root an import binds.
ROOTED_WATCHED: Mapping[str, str] = _watch(
    "code", "profile", "trace", "inspect", "types", "typing", "gc", "pipes"
)
ROOTED_ONLY = frozenset({*ROOTED_WATCHED, "site"})

# The name of the builtins namespace, built from two literals so that this file does not hold the
# string its own [eval-exec] rule refuses.
BUILTINS = "__" + "builtins__"
FRAME_REASON = "a frame reaches its globals and builtins, and through them eval and exec"
BREAKPOINT_REASON = "the breakpoint hook imports whatever PYTHONBREAKPOINT names"
TRACE_REASON = "a trace, profile or monitoring callback is handed frames or code objects"

# Banned everywhere: (name, rule, reason). A member of a banned name is banned too.
BANNED_EVERYWHERE: tuple[tuple[str, str, str], ...] = (
    ("os.system", "shell", "os.system runs a command line through a shell"),
    ("os.popen", "shell", "os.popen runs a command line through a shell"),
    ("posix.system", "shell", "posix.system runs a command line through a shell"),
    ("posix.popen", "shell", "posix.popen runs a command line through a shell"),
    ("subprocess.getoutput", "shell", "getoutput runs a command line through a shell"),
    ("subprocess.getstatusoutput", "shell", "getstatusoutput runs a command line through a shell"),
    ("asyncio.create_subprocess_shell", "shell", "runs a command line through a shell"),
    ("pipes", "shell", "pipes runs its pipelines through a shell"),
    ("eval", "eval-exec", "eval runs a string as code"),
    ("exec", "eval-exec", "exec runs a string as code"),
    ("compile", "eval-exec", "compile turns a string into code"),
    ("builtins.eval", "eval-exec", "eval runs a string as code"),
    ("builtins.exec", "eval-exec", "exec runs a string as code"),
    ("builtins.compile", "eval-exec", "compile turns a string into code"),
    (BUILTINS, "eval-exec", f"{BUILTINS} reaches eval, exec and __import__ by name"),
    ("globals", "eval-exec", f"globals() holds {BUILTINS}, which reaches eval and exec by key"),
    ("locals", "eval-exec", f"locals() can hold {BUILTINS}, which reaches eval and exec by key"),
    ("builtins.globals", "eval-exec", f"globals() holds {BUILTINS}"),
    ("builtins.locals", "eval-exec", f"locals() can hold {BUILTINS}"),
    ("sys._getframe", "eval-exec", FRAME_REASON),
    ("sys._current_frames", "eval-exec", FRAME_REASON),
    ("sys.remote_exec", "eval-exec", "remote_exec runs a Python file in another process"),
    ("sys.settrace", "eval-exec", TRACE_REASON),
    ("sys.setprofile", "eval-exec", TRACE_REASON),
    ("sys.monitoring", "eval-exec", TRACE_REASON),
    ("threading.settrace", "eval-exec", TRACE_REASON),
    ("threading.setprofile", "eval-exec", TRACE_REASON),
    ("threading.settrace_all_threads", "eval-exec", TRACE_REASON),
    ("threading.setprofile_all_threads", "eval-exec", TRACE_REASON),
    ("logging.currentframe", "eval-exec", FRAME_REASON),
    ("gc", "eval-exec", "gc reaches every live object, frames and module namespaces among them"),
    ("inspect.currentframe", "eval-exec", FRAME_REASON),
    ("inspect.stack", "eval-exec", FRAME_REASON),
    ("inspect.trace", "eval-exec", FRAME_REASON),
    ("inspect.getouterframes", "eval-exec", FRAME_REASON),
    ("inspect.getinnerframes", "eval-exec", FRAME_REASON),
    ("types.CodeType", "eval-exec", "CodeType builds a code object from bytes"),
    ("typing.get_type_hints", "eval-exec", "get_type_hints evaluates string annotations as code"),
    ("typing.ForwardRef", "eval-exec", "a ForwardRef is evaluated as code"),
    ("typing.evaluate_forward_ref", "eval-exec", "evaluates a string as code"),
    ("typing._eval_type", "eval-exec", "evaluates string annotations as code"),
    ("code", "eval-exec", "code runs strings as Python"),
    ("codeop", "eval-exec", "codeop compiles strings as Python"),
    ("timeit", "eval-exec", "timeit compiles and runs strings"),
    ("pdb", "eval-exec", "pdb runs strings as code"),
    ("bdb", "eval-exec", "bdb runs strings as code"),
    ("profile", "eval-exec", "profile runs strings as code"),
    ("cProfile", "eval-exec", "cProfile runs strings as code"),
    ("trace", "eval-exec", "trace runs strings as code"),
    ("doctest", "eval-exec", "doctest runs docstrings as code"),
    ("pickle", "eval-exec", "unpickling can call any function the data names"),
    ("_pickle", "eval-exec", "unpickling can call any function the data names"),
    ("marshal", "eval-exec", "marshal loads code objects"),
    ("shelve", "eval-exec", "shelve unpickles what it reads"),
    ("_interpreters", "eval-exec", "runs source strings in a subinterpreter"),
    ("_xxsubinterpreters", "eval-exec", "runs source strings in a subinterpreter"),
    ("concurrent.interpreters", "eval-exec", "runs source strings in a subinterpreter"),
    ("distutils", "eval-exec", "distutils runs setup scripts as code and starts compilers"),
    ("__import__", "dynamic-import", "__import__ imports a module named at run time"),
    ("builtins.__import__", "dynamic-import", "__import__ imports a module named at run time"),
    ("runpy", "dynamic-import", "runpy runs a module or path named at run time"),
    ("imp", "dynamic-import", "imp loads modules named at run time"),
    ("pkgutil", "dynamic-import", "pkgutil resolves and loads modules named at run time"),
    ("zipimport", "dynamic-import", "zipimport loads modules from a path"),
    ("pydoc", "dynamic-import", "pydoc.locate imports objects named at run time"),
    ("pkg_resources", "dynamic-import", "entry points import objects named at run time"),
    ("logging.config", "dynamic-import", "logging.config imports objects its config names"),
    ("help", "dynamic-import", "help imports the module or object a string names"),
    ("builtins.help", "dynamic-import", "help imports the module or object a string names"),
    ("breakpoint", "dynamic-import", BREAKPOINT_REASON),
    ("builtins.breakpoint", "dynamic-import", BREAKPOINT_REASON),
    ("sys.breakpointhook", "dynamic-import", BREAKPOINT_REASON),
    ("sys.__breakpointhook__", "dynamic-import", BREAKPOINT_REASON),
    ("keyring.core.load_keyring", "dynamic-import", "imports the module a string names"),
    ("keyring.core._load_keyring_class", "dynamic-import", "imports the module a string names"),
    ("ctypes", "native-code", "ctypes calls native code, which can start a process"),
    ("_ctypes", "native-code", "ctypes calls native code, which can start a process"),
    ("cffi", "native-code", "cffi calls native code, which can start a process"),
    ("_cffi_backend", "native-code", "cffi calls native code, which can start a process"),
    ("win32ctypes", "native-code", "win32ctypes calls Windows APIs, which can start a process"),
)
# Banned in src/lucille_host/ only.
BANNED_IN_PACKAGE: tuple[tuple[str, str, str], ...] = (
    ("sys.modules", "dynamic-import", "sys.modules reaches a module by a string"),
    ("sys.path", "import-path", "sys.path changes where import statements load code from"),
    ("sys.meta_path", "import-path", "sys.meta_path changes how import statements load code"),
    ("sys.path_hooks", "import-path", "sys.path_hooks changes how import statements load code"),
    ("sys.path_importer_cache", "import-path", "changes how import statements load code"),
    ("site", "import-path", "site changes where import statements load code from"),
)
# Module attributes that steer what an import statement loads: a package's __path__, and the
# __package__, __spec__ and __loader__ a relative import is resolved through. Refused in
# src/lucille_host/ as a name or an attribute, read or written.
IMPORT_PATH_DUNDERS = frozenset({"__path__", "__package__", "__spec__", "__loader__"})
# Module attributes src/lucille_host/ may not bind or assign. __name__ may be read
# (``if __name__ == "__main__"``) but not set: a relative import can be resolved through it.
MODULE_DUNDERS = IMPORT_PATH_DUNDERS | {"__name__"}
IMPORT_PATH_REASON = "steers what an import statement loads"

# importlib: only these may be imported (modules) or used (members).
IMPORTLIB_MODULES = frozenset(
    {"importlib", "importlib.metadata", "importlib.resources", "importlib.resources.abc"}
)
IMPORTLIB_MEMBERS = frozenset(
    {
        "importlib.metadata.version",
        "importlib.metadata.metadata",
        "importlib.metadata.distribution",
        "importlib.metadata.files",
        "importlib.metadata.PackagePath",
        "importlib.metadata.PackageNotFoundError",
        "importlib.resources.as_file",
        "importlib.resources.abc.Traversable",
    }
)

# Attributes that lead from an object to modules or code: (rule, reason) by attribute name.
BANNED_DUNDERS = frozenset(
    {
        "__dict__",
        "__globals__",
        BUILTINS,
        "__self__",
        "__class__",
        "__bases__",
        "__base__",
        "__mro__",
        "__subclasses__",
        "__getattribute__",
        "__code__",
        "__closure__",
        "__func__",
        "__loader__",
        "__spec__",
        "__import__",
    }
)
FRAME_ATTRIBUTES = frozenset(
    {"f_globals", "f_builtins", "f_locals", "gi_frame", "cr_frame", "ag_frame", "tb_frame"}
)
# Attributes that lead to a code object, or to the bytes of one: code.replace(co_code=...) and
# type(function)(code, {}) build and run code from bytes, which types.CodeType is refused for.
CODE_ATTRIBUTES = frozenset({"gi_code", "cr_code", "ag_code", "f_code", "co_code"})
BANNED_ATTRIBUTES: Mapping[str, tuple[str, str]] = {
    **{
        name: ("dunder", f"{name} leads from an object to modules or code")
        for name in BANNED_DUNDERS
    },
    **{
        name: ("dunder", f"{name} leads from a frame to its namespaces")
        for name in FRAME_ATTRIBUTES
    },
    **{name: ("dunder", f"{name} leads to a code object") for name in CODE_ATTRIBUTES},
    "entry_points": ("dynamic-import", "an entry point imports the object it names"),
}
# Banned members, as a string may name them: (rule, reason).
MEMBERS_BY_STRING: Mapping[str, tuple[str, str]] = {
    **{
        name: ("shell", f"{name} runs a command line through a shell")
        for name in (
            "system",
            "popen",
            "getoutput",
            "getstatusoutput",
            "create_subprocess_shell",
            "subprocess_shell",
        )
    },
    **{
        name: ("eval-exec", f"{name} runs a string as code") for name in ("eval", "exec", "compile")
    },
    **{
        name: ("eval-exec", f"{name} reaches frames and module namespaces")
        for name in ("get_objects", "get_referrers", "get_referents")
    },
    "get_type_hints": ("eval-exec", "get_type_hints evaluates string annotations as code"),
    "remote_exec": ("eval-exec", "remote_exec runs a Python file in another process"),
    **{
        name: ("dynamic-import", f"{name} imports what PYTHONBREAKPOINT names")
        for name in ("breakpoint", "breakpointhook")
    },
    **{
        name: ("dynamic-import", f"{name} imports a module named at run time")
        for name in (
            "__import__",
            "import_module",
            "find_spec",
            "spec_from_file_location",
            "module_from_spec",
            "spec_from_loader",
            "resolve_name",
        )
    },
}
# Calls that turn a string into an attribute lookup.
ATTRIBUTE_BY_STRING = frozenset(
    {"getattr", "setattr", "delattr", "hasattr"}
    | {f"builtins.{name}" for name in ("getattr", "setattr", "delattr", "hasattr")}
    | {"operator.attrgetter", "operator.methodcaller"}
)

SPAWN_MODULES = (
    "subprocess",
    "pty",
    "multiprocessing",
    "asyncio.subprocess",
    "concurrent.futures.process",
    "concurrent.futures.ProcessPoolExecutor",
    "_posixsubprocess",
    "webbrowser",
    "pipes",
    "distutils",
)
SPAWN_PATTERN = re.compile(
    r"^(os|posix)\.(spawn\w*|exec\w*|posix_spawn\w*|fork\w*|startfile|system|popen)(\.|$)"
    r"|^asyncio\.create_subprocess_\w+(\.|$)"
)
# Event-loop methods that start a process, whatever object they are reached through.
SPAWN_ATTRIBUTES = frozenset({"subprocess_exec", "subprocess_shell"})
# Of those, the one that hands a command line to a shell: banned everywhere.
SHELL_ATTRIBUTE = "subprocess_shell"
LOOP_LAUNCHER = "subprocess_exec"

# The launchers: what may start a process where starting one is allowed.
SUBPROCESS_LAUNCHERS = frozenset(
    {
        "subprocess.run",
        "subprocess.Popen",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
    }
)
LAUNCHERS = SUBPROCESS_LAUNCHERS | {"asyncio.create_subprocess_exec"}
# Members of the subprocess modules that start nothing (pipes, results, errors).
SAFE_MEMBERS = frozenset(
    f"{module}.{member}"
    for module in ("subprocess", "asyncio.subprocess")
    for member in (
        "PIPE",
        "DEVNULL",
        "STDOUT",
        "CompletedProcess",
        "CalledProcessError",
        "SubprocessError",
        "TimeoutExpired",
    )
)
SPAWN_IMPORTABLE_MODULES = frozenset({"subprocess", "asyncio.subprocess"})
ALLOWED_PROGRAMS = frozenset({"switchboard", "lucille-host"})
# Module-level constants of conformance/_spawn.py that may stand for an allowed program.
PROGRAM_CONSTANTS: Mapping[str, str] = {
    "SWITCHBOARD_EXE": "switchboard",
    "LUCILLE_HOST_EXE": "lucille-host",
}


def zone_of(rel: str) -> str:
    """Which rule set a file falls under: exec, spawn, conformance, runtime or other."""
    if rel == EXEC_MODULE:
        return "exec"
    if rel == SPAWN_MODULE:
        return "spawn"
    if rel.startswith(CONFORMANCE_PREFIX):
        return "conformance"
    if rel.startswith(PACKAGE_PREFIX):
        return "runtime"
    return "other"


# asyncio.subprocess.create_subprocess_exec and _shell are asyncio.create_subprocess_exec and
# _shell, the same functions: each name below is also checked as the name it stands for.
SAME_FUNCTION_PREFIXES = (("asyncio.subprocess.create_subprocess_", "asyncio.create_subprocess_"),)


Exports = Mapping[str, Mapping[str, frozenset[str]]]


@functools.cache
def _default_modules() -> frozenset[str]:
    return own_modules(REPO_ROOT)


@functools.cache
def _default_exports() -> Exports:
    return module_exports(REPO_ROOT)


def _resolved(ref: Ref, exports: Exports) -> list[str]:
    """The reference's name, plus each name it stands for through a re-exported module.

    Only a reference rooted in an import is followed: its root is a module, so its chain can
    reach a module that module re-exports. A reference rooted in a local name is not.
    """
    if not ref.rooted:
        return [ref.qualified]
    return resolve(ref.qualified, REEXPORTS, exports)


def _unfollowed(ref: Ref, rel: str, exports: Exports) -> Violation | None:
    """A reference whose re-exports could not all be followed: the lint fails closed."""
    if len(_resolved(ref, exports)) < RESOLVE_LIMIT:
        return None
    return Violation(
        rel,
        ref.line,
        "parse",
        f"{ref.qualified} stands for more than {RESOLVE_LIMIT} names through re-exported "
        "modules; the lint cannot follow them all, and fails closed",
    )


def _names(ref: Ref, modules: frozenset[str], exports: Exports | None = None) -> list[str]:
    """Every dotted name a reference can stand for, after following re-exported and watched
    modules.

    ``modules`` is this package's own modules: a component that names one is not followed as the
    watched module of the same name. ``exports`` is what the names those modules bind stand for
    (:func:`~tools.guards._common.module_exports`).
    """
    names: list[str] = []
    for base in _resolved(ref, {} if exports is None else exports):
        if ref.rooted:
            names += anchored(base, WATCHED, ROOTED_WATCHED, own=modules, underscored=True)
        else:
            names += anchored(base, WATCHED, own=modules, underscored=True)
    for name in list(names):
        for prefix, same in SAME_FUNCTION_PREFIXES:
            if name.startswith(prefix):
                names.append(same + name[len(prefix) :])
    return list(dict.fromkeys(names))


def _spawn_hits(names: list[str], ref: Ref) -> list[str]:
    hits = [
        name
        for name in names
        if (ref.rooted or name.split(".")[0] not in ROOTED_ONLY)
        and (any(matches(name, module) for module in SPAWN_MODULES) or SPAWN_PATTERN.match(name))
    ]
    hits += sorted(SPAWN_ATTRIBUTES.intersection(ref.written))
    return hits


def _banned(names: list[str], ref: Ref, rel: str) -> list[Violation]:
    found: list[Violation] = []
    banned = BANNED_EVERYWHERE
    if rel.startswith(PACKAGE_PREFIX):
        banned = banned + BANNED_IN_PACKAGE
    for name in names:
        if not ref.rooted and name.split(".")[0] in ROOTED_ONLY:
            continue
        for target, rule, reason in banned:
            if matches(name, target):
                found.append(Violation(rel, ref.line, rule, f"{name}: {reason}"))
        if matches(name, "importlib"):
            allowed = name in IMPORTLIB_MEMBERS or any(
                matches(name, member) for member in IMPORTLIB_MEMBERS
            )
            if ref.kind == "import" and name in IMPORTLIB_MODULES:
                allowed = True
            if not allowed and name != "importlib":
                members = ", ".join(sorted(IMPORTLIB_MEMBERS))
                found.append(
                    Violation(
                        rel,
                        ref.line,
                        "dynamic-import",
                        f"{name}: of importlib, only these may be used: {members}",
                    )
                )
        if ref.kind == "use" and not ref.annotation and name in _module_values(ref):
            found.append(
                Violation(
                    rel,
                    ref.line,
                    "module-object",
                    f"{name} is used as a value; name the member you use",
                )
            )
    return found


def _module_values(ref: Ref) -> set[str]:
    modules = set(WATCHED.values())
    if ref.rooted:
        modules |= set(ROOTED_WATCHED.values())
    return modules


def _dunder(ref: Ref, rel: str) -> list[Violation]:
    """Banned attributes in a chain, and a banned dunder as the chain's root name (a module's own
    ``__loader__`` or ``__spec__``, or a method's ``__class__``)."""
    if ref.kind != "use":
        return []
    found = [
        Violation(rel, ref.line, *BANNED_ATTRIBUTES[attr])
        for attr in ref.written[1:]
        if attr in BANNED_ATTRIBUTES
    ]
    if ref.written[0] in BANNED_DUNDERS:
        found.append(Violation(rel, ref.line, *BANNED_ATTRIBUTES[ref.written[0]]))
    return found


def _string_member_rule(text: str) -> tuple[str, str] | None:
    """The rule a string names, when it names a banned member or attribute: (rule, reason)."""
    return BANNED_ATTRIBUTES.get(text) or MEMBERS_BY_STRING.get(text)


def _attribute_strings(ref: Ref, rel: str) -> list[Violation]:
    """Banned members named as strings where a string becomes an attribute."""
    call = ref.call
    if call is None or ref.qualified not in ATTRIBUTE_BY_STRING:
        return []
    if ref.qualified == "operator.methodcaller":
        candidates = call.args[:1]
    else:
        candidates = list(call.args)
    found: list[Violation] = []
    for argument in candidates:
        if not (isinstance(argument, ast.Constant) and isinstance(argument.value, str)):
            continue
        parts = argument.value.split(".") if ref.qualified == "operator.attrgetter" else []
        for text in parts or [argument.value]:
            named = _string_member_rule(text)
            if named is not None:
                rule, reason = named
                found.append(Violation(rel, ref.line, rule, f"{text!r} by string: {reason}"))
    return found


def _launcher_problem(names: list[str], ref: Ref) -> str | None:
    """Why this reference to a process launcher is not a plain direct call, or None."""
    if ref.kind != "use":
        return None
    launcher = next((n for n in names for lau in LAUNCHERS if matches(n, lau)), None)
    loop_method = ref.written[-1] == LOOP_LAUNCHER
    if launcher is None and not loop_method:
        return None
    if ref.annotation and ref.call is None:
        return None
    exact = loop_method or launcher in LAUNCHERS
    if ref.call is None or not exact:
        what = launcher if launcher is not None else LOOP_LAUNCHER
        return f"{what}: a process launcher may only be called directly"
    if any(keyword.arg is None for keyword in ref.call.keywords):
        return "** keywords could pass shell= or executable=; pass each keyword explicitly"
    if launcher in SUBPROCESS_LAUNCHERS and (
        len(ref.call.args) > 1 or any(isinstance(a, ast.Starred) for a in ref.call.args)
    ):
        return (
            f"{launcher}: argv must be the only positional argument "
            "(Popen's positional parameters include executable and shell)"
        )
    return None


def _binding_lines(tree: ast.Module) -> dict[str, list[int]]:
    """The line of each place a name is bound, anywhere in the file."""
    lines: dict[str, list[int]] = {}
    line = 1
    for node in ast.walk(tree):
        line = getattr(node, "lineno", line)
        name: str | None = None
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            name = node.id
        elif isinstance(node, ast.arg):
            name = node.arg
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            name = node.name
        elif isinstance(node, ast.ExceptHandler | ast.MatchAs | ast.MatchStar):
            name = node.name
        elif isinstance(node, ast.MatchMapping):
            name = node.rest
        elif isinstance(node, ast.alias):
            name = node.asname or node.name.split(".")[0]
        if name:
            lines.setdefault(name, []).append(line)
    return lines


def _bindings(tree: ast.Module) -> Counter[str]:
    """How many times each name is bound anywhere in the file."""
    return Counter({name: len(lines) for name, lines in _binding_lines(tree).items()})


def _is_call_of(node: ast.expr, name: str, aliases: Mapping[str, str]) -> ast.Call | None:
    if isinstance(node, ast.Call) and dotted_name(node.func, aliases) == name:
        return node
    return None


def _scripts_dir(node: ast.expr, aliases: Mapping[str, str]) -> bool:
    """True for ``Path(sys.executable).parent`` or ``Path(sysconfig.get_path("scripts"))``."""
    if isinstance(node, ast.Attribute) and node.attr == "parent":
        path = _is_call_of(node.value, "pathlib.Path", aliases)
        return (
            path is not None
            and len(path.args) == 1
            and not path.keywords
            and dotted_name(path.args[0], aliases) == "sys.executable"
        )
    path = _is_call_of(node, "pathlib.Path", aliases)
    if path is None or len(path.args) != 1 or path.keywords:
        return False
    get_path = _is_call_of(path.args[0], "sysconfig.get_path", aliases)
    return (
        get_path is not None
        and len(get_path.args) == 1
        and not get_path.keywords
        and isinstance(get_path.args[0], ast.Constant)
        and get_path.args[0].value == "scripts"
    )


def _program_value(
    value: ast.expr, program: str, aliases: Mapping[str, str]
) -> tuple[str, bool] | None:
    """(program, absolute) when ``value`` names ``program`` in an allowed way, else None."""
    if isinstance(value, ast.Constant) and value.value == program:
        return program, False
    which = _is_call_of(value, "shutil.which", aliases)
    if which is not None:
        if (
            len(which.args) == 1
            and not which.keywords
            and isinstance(which.args[0], ast.Constant)
            and which.args[0].value == program
        ):
            return program, True
        return None
    if (
        isinstance(value, ast.BinOp)
        and isinstance(value.op, ast.Div)
        and isinstance(value.right, ast.Constant)
        and value.right.value == program
        and _scripts_dir(value.left, aliases)
    ):
        return program, True
    return None


def _program_constants(tree: ast.Module, aliases: Mapping[str, str]) -> dict[str, tuple[str, bool]]:
    """The allowlisted constants bound once, at module level, to their program."""
    bound = _bindings(tree)
    valid: dict[str, tuple[str, bool]] = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target, value = stmt.targets[0], stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            target, value = stmt.target, stmt.value
        else:
            continue
        if not isinstance(target, ast.Name):
            continue
        program = PROGRAM_CONSTANTS.get(target.id)
        if program is None or bound[target.id] != 1:
            continue
        resolved = _program_value(value, program, aliases)
        if resolved is not None:
            valid[target.id] = resolved
    return valid


def _argv_problem(
    call: ast.Call, launcher: str, constants: Mapping[str, tuple[str, bool]]
) -> str | None:
    """Why this launcher call's argv is not an allowed one, or None if it is."""
    for keyword in call.keywords:
        if keyword.arg is None:
            return "**kwargs could replace the program; pass keywords explicitly"
        if keyword.arg in ("executable", "program"):
            return f"{keyword.arg}= could replace the program named in argv"
    if launcher == "asyncio.create_subprocess_exec":
        args = call.args
        if len(args) == 1 and isinstance(args[0], ast.Starred):
            if not isinstance(args[0].value, ast.List):
                return "the arguments must be literal, or one unpacked list literal"
            elements = args[0].value.elts
        else:
            elements = args
    else:
        if len(call.args) > 1:
            return "argv must be the only positional argument"
        argv = call.args[0] if call.args else None
        for keyword in call.keywords:
            if keyword.arg == "args":
                argv = keyword.value
        if not isinstance(argv, ast.List):
            return "argv must be a list literal"
        elements = argv.elts
    if not elements:
        return "argv is empty"
    first = elements[0]
    if isinstance(first, ast.Starred):
        return "an unpacked value in argv could replace the program"
    if isinstance(first, ast.Constant) and first.value in ALLOWED_PROGRAMS:
        program, absolute = first.value, False
    elif isinstance(first, ast.Name) and first.id in constants:
        program, absolute = constants[first.id]
    else:
        allowed = ", ".join(sorted(ALLOWED_PROGRAMS))
        return f"argv[0] must be one of: {allowed} (a literal, or an allowlisted constant)"
    if not absolute and any(keyword.arg == "env" for keyword in call.keywords):
        return (
            "env= sets the PATH a bare program name is looked up on; name the program by a "
            "constant bound to its path"
        )
    if program == "switchboard":
        second = elements[1] if len(elements) > 1 else None
        if not (isinstance(second, ast.Constant) and second.value == "serve"):
            return 'switchboard may be started only as the local hub: ["switchboard", "serve", ...]'
    return None


# What a program constant of conformance/_spawn.py is resolved through.
RESOLVER_MODULES = ("pathlib", "shutil", "sys", "sysconfig")


def _chain_root(node: ast.expr) -> ast.expr:
    while isinstance(node, ast.Attribute | ast.Subscript):
        node = node.value
    return node


def _store_targets(node: ast.AST) -> list[ast.expr]:
    """What an assignment, augmented assignment or ``del`` writes to, tuples unpacked."""
    if isinstance(node, ast.Assign):
        pending = list(node.targets)
    elif isinstance(node, ast.AugAssign | ast.AnnAssign):
        pending = [node.target]
    elif isinstance(node, ast.Delete):
        pending = list(node.targets)
    else:
        return []
    found: list[ast.expr] = []
    while pending:
        target = pending.pop()
        if isinstance(target, ast.Tuple | ast.List):
            pending.extend(target.elts)
        elif isinstance(target, ast.Starred):
            pending.append(target.value)
        else:
            found.append(target)
    return found


def _resolver_problems(tree: ast.Module, aliases: Mapping[str, str], rel: str) -> list[Violation]:
    """conformance/_spawn.py: what could change what a program constant names."""
    found: list[Violation] = []
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 1)
        for target in _store_targets(node):
            root = _chain_root(target)
            if target is not root and isinstance(root, ast.Name) and root.id in aliases:
                found.append(
                    Violation(
                        rel,
                        line,
                        "conformance-argv",
                        f"writes to {aliases[root.id]}: a module's state may not change here",
                    )
                )
        if isinstance(node, ast.Call) and dotted_name(node.func, aliases) in (
            "setattr",
            "delattr",
            "builtins.setattr",
            "builtins.delattr",
        ):
            found.append(
                Violation(rel, line, "conformance-argv", "setattr and delattr may change a module")
            )
    bound = _binding_lines(tree)
    for name, target in sorted(aliases.items()):
        if not any(matches(target, module) for module in RESOLVER_MODULES):
            continue
        if len(bound.get(name, [])) > 1:
            found.extend(
                Violation(
                    rel,
                    line,
                    "conformance-argv",
                    f"{name} stands for {target} and is bound more than once; bind it once, by "
                    "its import",
                )
                for line in bound[name]
            )
    return found


VARS = ("vars", "builtins.vars")


def _vars_problem(ref: Ref) -> str | None:
    """Why this reference to ``vars`` could be ``vars()``, which is ``locals()``, or None."""
    if ref.kind != "use" or ref.qualified not in VARS:
        return None
    call = ref.call
    if call is None:
        return "vars used as a value can be called with no argument, which is locals()"
    if not call.args or any(isinstance(argument, ast.Starred) for argument in call.args):
        return "vars() with no argument is locals(), which can hold the builtins"
    return None


def _runtime_imports(
    tree: ast.Module, refs: list[Ref], rel: str, zone: str, exports: Exports
) -> list[Violation]:
    """The runtime package: imports off the allowlist, modules reached through a re-export that
    are off it, and any import or use of the conformance package."""
    allowed = RUNTIME_IMPORTS | EXEC_IMPORTS if zone == "exec" else RUNTIME_IMPORTS
    where = "RUNTIME_IMPORTS" + (" and EXEC_IMPORTS" if zone == "exec" else "")
    package = package_of(rel)
    found: list[Violation] = []

    def report(line: int, name: str, written: str) -> None:
        through = f" (reached through {written})" if name != written else ""
        top = name.split(".")[0]
        if matches(name, CONFORMANCE_PACKAGE):
            message = (
                "the runtime may not import or use the conformance package, which is not held to "
                "the runtime's import allowlist"
            )
        elif top not in allowed:
            message = (
                f"{top} is not on the runtime's import allowlist ({where} in "
                "tools/guards/ast_lint.py); widening it is a reviewed change"
            )
        else:
            return
        found.append(Violation(rel, line, "runtime-import", f"{name}{through}: {message}"))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            module = absolute_module(node.module, node.level, package)
            targets = [
                f"{module}.{alias.name}" if module else alias.name
                for alias in node.names
                if alias.name != "*"
            ]
            if any(alias.name == "*" for alias in node.names):
                targets.append(module)
        else:
            continue
        for target in targets:
            for name in resolve(target, REEXPORTS, exports):
                report(node.lineno, name, target)
    for ref in refs:
        if ref.kind != "use":
            continue
        if matches(ref.qualified, CONFORMANCE_PACKAGE):
            report(ref.line, ref.qualified, ref.qualified)
        # The name as written was checked at its import; what it is re-exported as was not.
        for name in _resolved(ref, exports)[1:]:
            report(ref.line, name, ref.qualified)
    return found


# Names that are modules: what the runtime may import, the submodules and re-exported modules of the
# standard-library modules among them, and (added per file) this package's modules and what the
# file's import statements bind.
KNOWN_MODULES = (
    RUNTIME_IMPORTS
    | EXEC_IMPORTS
    | SUBMODULES
    | {target for targets in REEXPORTS.values() for target in targets}
)


def _imported_modules(tree: ast.Module) -> set[str]:
    """Every dotted module name the file's ``import`` statements name (``import a.b`` gives ``a``
    and ``a.b``)."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                found.update(".".join(parts[:end]) for end in range(1, len(parts) + 1))
    return found


def _module_value(
    ref: Ref, rel: str, modules: frozenset[str], exports: Exports, imported: set[str]
) -> Violation | None:
    """The runtime package: a module used as a value, which ``getattr`` could then search."""
    if ref.kind != "use" or not ref.rooted or ref.annotation:
        return None
    for name in _resolved(ref, exports):
        if name in modules or name in KNOWN_MODULES or name in imported:
            return Violation(
                rel,
                ref.line,
                "module-object",
                f"{name} is a module, used as a value; in the runtime package no module is used "
                "as a value: name the member you use",
            )
    return None


def _keyring_problem(ref: Ref, exports: Exports) -> str | None:
    """The runtime package: a part of keyring that is not on :data:`KEYRING_SURFACE`."""
    for name in _resolved(ref, exports):
        if not matches(name, "keyring"):
            continue
        if any(matches(name, member) for member in KEYRING_SURFACE):
            continue
        on_the_way = any(member.startswith(name + ".") for member in KEYRING_SURFACE)
        if on_the_way and (ref.kind == "import" or ref.annotation):
            continue
        if on_the_way:
            return f"{name} may be used only through a member on KEYRING_SURFACE"
        return (
            f"{name} is not on the runtime's keyring surface (KEYRING_SURFACE in "
            "tools/guards/ast_lint.py)"
        )
    return None


def _import_path_problems(tree: ast.Module, refs: list[Ref], rel: str) -> list[Violation]:
    """src/lucille_host/: the module attributes that steer what an import statement loads."""
    found: list[Violation] = []
    for ref in refs:
        named = [part for part in ref.written if part in IMPORT_PATH_DUNDERS]
        if named:
            found.append(
                Violation(rel, ref.line, "import-path", f"{named[0]} {IMPORT_PATH_REASON}")
            )
        call = ref.call
        if call is None or ref.qualified not in ATTRIBUTE_BY_STRING:
            continue
        sets = ref.qualified.rsplit(".", 1)[-1] in ("setattr", "delattr")
        for argument in call.args:
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                for text in argument.value.split("."):
                    if text in IMPORT_PATH_DUNDERS or (sets and text in MODULE_DUNDERS):
                        found.append(
                            Violation(
                                rel, ref.line, "import-path", f"{text!r} {IMPORT_PATH_REASON}"
                            )
                        )
    for name, lines in _binding_lines(tree).items():
        if name in MODULE_DUNDERS:
            found.extend(
                Violation(rel, line, "import-path", f"binding {name} {IMPORT_PATH_REASON}")
                for line in lines
            )
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and not isinstance(node.ctx, ast.Load)
            and node.attr in MODULE_DUNDERS
        ):
            found.append(
                Violation(
                    rel, node.lineno, "import-path", f"setting {node.attr} {IMPORT_PATH_REASON}"
                )
            )
    return found


def _starts_nothing(hits: list[str], ref: Ref) -> bool:
    """True when a conformance-package reference names only what starts no process."""
    if ref.annotation and ref.call is None:
        return True
    if ref.kind == "import":
        return all(hit in SPAWN_IMPORTABLE_MODULES or hit in SAFE_MEMBERS for hit in hits)
    return all(hit in SAFE_MEMBERS for hit in hits)


def _spawn_module_violation(
    names: list[str], ref: Ref, rel: str, constants: Mapping[str, tuple[str, bool]]
) -> Violation | None:
    """Apply conformance/_spawn.py's rule to one reference that starts or names a process."""
    hits = _spawn_hits(names, ref)
    if not hits or _starts_nothing(hits, ref):
        return None
    if ref.kind == "import" and all(
        hit in SPAWN_IMPORTABLE_MODULES or hit in LAUNCHERS or hit in SAFE_MEMBERS for hit in hits
    ):
        return None
    if ref.kind == "use":
        launcher = next((hit for hit in hits if hit in LAUNCHERS), None)
        if launcher is not None and ref.call is not None:
            problem = _argv_problem(ref.call, launcher, constants)
            if problem is None:
                return None
            return Violation(rel, ref.line, "conformance-argv", f"{launcher}: {problem}")
    return Violation(
        rel,
        ref.line,
        "conformance-spawn",
        f"{hits[0]}: conformance/_spawn.py may start a process only by calling "
        "a launcher with an allowed argv",
    )


def check_source(
    source: str | bytes,
    rel: str,
    modules: frozenset[str] | None = None,
    exports: Exports | None = None,
) -> list[Violation]:
    """Check one file's source as if it were at ``rel`` (a root-relative POSIX path).

    ``modules`` is this package's own modules (:func:`own_modules`), and ``exports`` what the
    names they bind stand for (:func:`module_exports`); by default, this repository's.
    """
    tree = parse(source, rel)
    if isinstance(tree, Violation):
        return [tree]
    modules = _default_modules() if modules is None else modules
    exports = _default_exports() if exports is None else exports
    zone = zone_of(rel)
    in_package = rel.startswith(PACKAGE_PREFIX)
    found: list[Violation] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value == BUILTINS:
            found.append(
                Violation(
                    rel,
                    node.lineno,
                    "eval-exec",
                    f"the string {BUILTINS!r} reaches eval, exec and __import__ by key",
                )
            )
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                value = keyword.value
                if keyword.arg == "shell" and not (
                    isinstance(value, ast.Constant) and value.value is False
                ):
                    found.append(
                        Violation(rel, node.lineno, "shell", "shell= must be the literal False")
                    )

    constants = _program_constants(tree, import_aliases(tree, package_of(rel)))
    found.extend(import_alias_conflicts(tree, package_of(rel), rel))
    refs = references(tree, rel)
    imported = _imported_modules(tree)
    if zone in ("runtime", "exec"):
        found.extend(_runtime_imports(tree, refs, rel, zone, exports))
    if in_package:
        found.extend(_import_path_problems(tree, refs, rel))
    for ref in refs:
        names = _names(ref, modules, exports)
        unfollowed = _unfollowed(ref, rel, exports)
        if unfollowed is not None:
            found.append(unfollowed)
        problem = _vars_problem(ref)
        if problem is not None:
            found.append(Violation(rel, ref.line, "eval-exec", problem))
        if ref.kind == "star" and (
            in_package or any(matches(ref.qualified, m) for m in (*WATCHED, *ROOTED_WATCHED))
        ):
            found.append(
                Violation(
                    rel,
                    ref.line,
                    "star-import",
                    f"from {ref.qualified} import * hides the names this lint checks",
                )
            )
        if SHELL_ATTRIBUTE in ref.written:
            found.append(
                Violation(
                    rel, ref.line, "shell", f"{SHELL_ATTRIBUTE} runs a command line through a shell"
                )
            )
        found.extend(_banned(names, ref, rel))
        found.extend(_dunder(ref, rel))
        found.extend(_attribute_strings(ref, rel))
        problem = _launcher_problem(names, ref)
        if problem is not None:
            found.append(Violation(rel, ref.line, "launcher", problem))
        if zone in ("runtime", "exec"):
            value = _module_value(ref, rel, modules, exports, imported)
            if value is not None:
                found.append(value)
            problem = _keyring_problem(ref, exports)
            if problem is not None:
                found.append(Violation(rel, ref.line, "keyring-surface", problem))
        if zone == "runtime":
            hits = _spawn_hits(names, ref)
            if hits:
                found.append(
                    Violation(
                        rel,
                        ref.line,
                        "spawn-outside-exec",
                        f"{hits[0]}: in the runtime package only {EXEC_MODULE} may start a process",
                    )
                )
        elif zone == "exec":
            hits = [hit for hit in _spawn_hits(names, ref) if hit not in SAFE_MEMBERS]
            if ref.kind == "use" and hits and ref.call is None and not ref.annotation:
                found.append(
                    Violation(
                        rel,
                        ref.line,
                        "launcher",
                        f"{hits[0]}: in {EXEC_MODULE} what starts a process is only called "
                        "directly, never assigned, passed or wrapped",
                    )
                )
        elif zone == "conformance":
            hits = _spawn_hits(names, ref)
            if hits and not _starts_nothing(hits, ref):
                found.append(
                    Violation(
                        rel,
                        ref.line,
                        "conformance-spawn",
                        f"{hits[0]}: in the conformance package only {SPAWN_MODULE} "
                        "may start a process",
                    )
                )
        elif zone == "spawn":
            violation = _spawn_module_violation(names, ref, rel, constants)
            if violation is not None:
                found.append(violation)
    if zone == "spawn":
        found.extend(_resolver_problems(tree, import_aliases(tree, package_of(rel)), rel))
    return sorted(set(found))


def compiled_files(root: Path) -> list[str]:
    """The compiled files in the repository: tracked, or untracked and not ignored by git."""
    return [
        rel for rel in repo_files(root, untracked=True) if rel.lower().endswith(COMPILED_SUFFIXES)
    ]


def check(root: Path) -> list[Violation]:
    """Check every Python file under ``root``, and that no compiled file or symbolic link sits
    beside them."""
    modules = own_modules(root)
    exports = module_exports(root)
    found = [
        Violation(
            rel,
            0,
            "compiled",
            "a compiled file, which this lint cannot read; Python loads an extension module "
            "ahead of the .py file of the same name, and a lone .pyc where there is no .py",
        )
        for rel in compiled_files(root)
    ]
    found += [
        Violation(
            rel,
            0,
            "symlink",
            "a symbolic link; Python and the wheel build follow it and this lint does not, so the "
            "code behind it would be checked under the rules of where it really is, or not at all",
        )
        for rel in symbolic_links(root)
    ]
    for rel in python_files(root):
        found.extend(check_source((root / rel).read_bytes(), rel, modules, exports))
    return sorted(found)


def main(argv: list[str] | None = None) -> int:
    """Script entry point: ``python tools/guards/ast_lint.py [ROOT]``."""
    return run_guard("ast_lint", sys.argv[1:] if argv is None else argv, python_files, check)


if __name__ == "__main__":
    raise SystemExit(main())
