"""tools/guards/ast_lint.py reports each planted violation, allows what it should, and passes
the repo."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from tools.guards import ast_lint
from tools.guards._common import file_exports, references, resolve

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "tools" / "guards" / "ast_lint.py"

RUNTIME = "src/lucille_host/example.py"
EXEC = "src/lucille_host/_exec.py"
CONFORMANCE = "src/lucille_host/conformance/scenario.py"
SPAWN = "src/lucille_host/conformance/_spawn.py"
TEST = "tests/test_example.py"
TOOL = "tools/example.py"
EVERY_ZONE = (RUNTIME, EXEC, CONFORMANCE, SPAWN, TEST, TOOL)
RUNTIME_ZONES = (RUNTIME, EXEC)


def rules(source: str, rel: str, modules: frozenset[str] | None = None) -> set[str]:
    return {violation.rule for violation in ast_lint.check_source(source, rel, modules)}


def resolved(source: str, rel: str = TOOL) -> set[str]:
    """Every dotted name the references in ``source`` stand for, as the lint follows them."""
    tree = ast.parse(source)
    return {name for ref in references(tree, rel) for name in ast_lint._names(ref, frozenset())}


# --- Everywhere ------------------------------------------------------------------------------

BANNED_EVERYWHERE = [
    ("shell", "import subprocess\nsubprocess.run(['ls'], shell=True)\n"),
    ("shell", "import subprocess\nflag = True\nsubprocess.run(['ls'], shell=flag)\n"),
    ("shell", "launch('ls', shell=1)\n"),
    ("shell", "import os\nos.system('ls')\n"),
    ("shell", "import os as o\no.popen('ls')\n"),
    ("shell", "from os import system\nsystem('ls')\n"),
    ("shell", "import subprocess\nsubprocess.getoutput('ls')\n"),
    ("shell", "from subprocess import getstatusoutput\n"),
    ("shell", "import asyncio\nasyncio.create_subprocess_shell('ls')\n"),
    ("shell", "async def start(loop):\n    await loop.subprocess_shell(object, 'ls')\n"),
    ("eval-exec", "eval('1 + 1')\n"),
    ("eval-exec", "exec('x = 1')\n"),
    ("eval-exec", "import builtins\nbuiltins.exec('x = 1')\n"),
    ("eval-exec", "run_text = eval\n"),
    ("dynamic-import", "__import__('os')\n"),
    ("dynamic-import", "import importlib\nimportlib.import_module('os')\n"),
    ("dynamic-import", "from importlib import import_module\nimport_module('os')\n"),
    ("dynamic-import", "import importlib\nimportlib.__import__('os')\n"),
    (
        "dynamic-import",
        "import importlib.util\nimportlib.util.spec_from_file_location('m', 'm.py')\n",
    ),
    ("dynamic-import", "from importlib.util import module_from_spec\n"),
    ("dynamic-import", "import importlib.util as u\nu.spec_from_loader('m', None)\n"),
    ("dynamic-import", "import runpy\nrunpy.run_path('x.py')\n"),
    ("dynamic-import", "from runpy import run_module\n"),
    ("dynamic-import", "import imp\n"),
    ("star-import", "from os import *\n"),
    ("parse", "def broken(:\n"),
    # The posix and builtins twins of the names above.
    ("shell", "import posix\nposix.system('ls')\n"),
    ("shell", "import posix\nposix.popen('ls')\n"),
    ("eval-exec", "import builtins\nbuiltins.eval('1')\n"),
    ("dynamic-import", "import builtins\nbuiltins.__import__('os')\n"),
    # A watched module reached through another module is still that module.
    ("shell", "from helpers import os as o\no.system('ls')\n"),
    # Every place a name can sit: a keyword value, a call's own function, an argument.
    ("eval-exec", "f(cb=eval)\n"),
    ("dynamic-import", "__import__('os').system('x')\n"),
    ("eval-exec", "eval('x')()\n"),
    ("eval-exec", "print(eval('1'))\n"),
    # Code from strings, under other names.
    ("eval-exec", "compile('x = 1', '<s>', 'exec')\n"),
    ("eval-exec", "import types\ntypes.FunctionType(compile(src, '<s>', 'exec'), {})()\n"),
    ("eval-exec", "__builtins__['exec']('x = 1')\n"),
    ("eval-exec", "__builtins__.__import__('os')\n"),
    ("eval-exec", "import timeit\ntimeit.timeit('import os')\n"),
    ("eval-exec", "import code\ncode.InteractiveInterpreter().runsource('x = 1')\n"),
    ("eval-exec", "import codeop\n"),
    ("eval-exec", "import pdb\npdb.run('x = 1')\n"),
    ("eval-exec", "import bdb\n"),
    ("eval-exec", "import cProfile\ncProfile.run('x = 1')\n"),
    ("eval-exec", "import profile\nprofile.run('x = 1')\n"),
    ("eval-exec", "from trace import Trace\nTrace().run('x = 1')\n"),
    ("eval-exec", "import doctest\n"),
    ("eval-exec", "import pickle\npickle.loads(data)\n"),
    ("eval-exec", "import marshal\n"),
    ("eval-exec", "import shelve\n"),
    # Modules loaded by name, through the standard library's other loaders.
    ("dynamic-import", "import pkgutil\npkgutil.resolve_name('os:system')\n"),
    ("dynamic-import", "from pkgutil import get_loader\n"),
    (
        "dynamic-import",
        "import importlib.machinery\nimportlib.machinery.SourceFileLoader('m', p).load_module()\n",
    ),
    (
        "dynamic-import",
        "import importlib.util\nimportlib.util.find_spec('m').loader.exec_module(m)\n",
    ),
    ("dynamic-import", "import importlib\nimportlib.reload(module)\n"),
    ("dynamic-import", "from importlib import metadata\nmetadata.entry_points()\n"),
    (
        "dynamic-import",
        "import importlib.metadata\nimportlib.metadata.EntryPoint('x', 'os:system', 'g').load()\n",
    ),
    ("dynamic-import", "from importlib import metadata\nhand_over(metadata)\n"),
    ("dynamic-import", "import zipimport\nzipimport.zipimporter(p).load_module('m')\n"),
    ("dynamic-import", "import pydoc\npydoc.locate('os.system')\n"),
    ("dynamic-import", "import pkg_resources\n"),
    # importlib.resources.files imports the module its argument names, which a variable can hold.
    ("dynamic-import", "import importlib.resources\nimportlib.resources.files('lucille_host')\n"),
    (
        "dynamic-import",
        "from importlib.resources import files\ndef load(name):\n    return files(name)\n",
    ),
    ("dynamic-import", "import logging.config\nlogging.config.dictConfig(config)\n"),
    ("dynamic-import", "from logging import config\nconfig.fileConfig(path)\n"),
    ("native-code", "import ctypes\nctypes.CDLL(None).system(b'ls')\n"),
    ("native-code", "from ctypes import CDLL\n"),
    ("native-code", "import _ctypes\n"),
    # A module used as a value: a string could then pick any member.
    ("module-object", "import importlib\ngetattr(importlib, 'import_module')('os')\n"),
    ("module-object", "import builtins\ngetattr(builtins, 'exec')('x = 1')\n"),
    ("module-object", "import os\ngetattr(os, name)('ls')\n"),
    ("module-object", "import os\nvars(os)['fork']()\n"),
    ("module-object", "import subprocess\nhand_over(subprocess)\n"),
    ("module-object", "from lucille_host import _exec\ngetattr(_exec.subprocess, 'Popen')\n"),
    ("module-object", "import sys\ngetattr(sys, 'modules')['os']\n"),
    # Attributes that lead from any object to modules or code.
    ("dunder", "import os\nos.__dict__['system']\n"),
    ("dunder", "print.__self__.exec('x = 1')\n"),
    ("dunder", "f.__globals__['__builtins__']\n"),
    ("dunder", "().__class__.__base__.__subclasses__()\n"),
    ("dunder", "getattr(print, '__self__')\n"),
    # Banned members named by a string.
    ("shell", "getattr(o, 'system')('ls')\n"),
    ("eval-exec", "getattr(b, 'exec')('x = 1')\n"),
    ("eval-exec", "import operator\noperator.attrgetter('x.exec')(b)\n"),
    ("eval-exec", "from operator import methodcaller\nmethodcaller('eval', '1')(b)\n"),
    ("dynamic-import", "getattr(m, 'import_module')('os')\n"),
    # Launchers: called directly, argv the only positional argument, no ** keywords.
    (
        "launcher",
        "import subprocess\nsubprocess.Popen('ls', -1, None, None, None, None, None, True)\n",
    ),
    ("launcher", "import subprocess\nsubprocess.run(['lucille-host', 'status'], -1, '/bin/sh')\n"),
    ("launcher", "import subprocess\nsubprocess.run(*argv)\n"),
    ("launcher", "import subprocess\nsubprocess.run(['ls'], **{'shell': True})\n"),
    ("launcher", "import subprocess\nopts = {'shell': True}\nsubprocess.run('ls', **opts)\n"),
    ("launcher", "import subprocess\nlaunch = subprocess.Popen\n"),
    (
        "launcher",
        "import functools\nimport subprocess\nfunctools.partial(subprocess.run, ['ls'])\n",
    ),
    ("launcher", "import subprocess\nsubprocess.Popen.__init__(process, 'ls')\n"),
    ("launcher", "import asyncio\nasyncio.create_subprocess_exec('ls', **kw)\n"),
    ("launcher", "async def run(loop):\n    await loop.subprocess_exec(object, 'ls', **kw)\n"),
    ("launcher", "def start(loop):\n    return loop.subprocess_exec\n"),
    # asyncio.subprocess.create_subprocess_shell and _exec are the asyncio functions of those names.
    ("shell", "import asyncio.subprocess\nasyncio.subprocess.create_subprocess_shell('ls')\n"),
    ("shell", "from asyncio.subprocess import create_subprocess_shell\n"),
    ("shell", "from asyncio import subprocess as aps\naps.create_subprocess_shell('ls')\n"),
    ("launcher", "from asyncio.subprocess import create_subprocess_exec as c\nc('ls', **kw)\n"),
    ("launcher", "import asyncio.subprocess\nlaunch = asyncio.subprocess.create_subprocess_exec\n"),
    (
        "launcher",
        "import asyncio.subprocess\nasyncio.subprocess.create_subprocess_exec('ls', **kw)\n",
    ),
    # Native code through cffi, which the runtime dependencies install.
    ("native-code", "import cffi\ncffi.FFI().dlopen(None).system(b'ls')\n"),
    ("native-code", "from cffi import FFI\n"),
    ("native-code", "import _cffi_backend\n"),
    ("native-code", "from helpers import cffi as native\nnative.FFI()\n"),
    ("native-code", "from helpers import _cffi_backend as native\nnative.FFI()\n"),
    # Namespaces that hold the builtins, and the frames that reach them.
    ("eval-exec", "globals()['__builtins__']['exec']('x = 1')\n"),
    ("eval-exec", "vars()['__builtins__']\n"),
    ("eval-exec", "namespace.get('__builtins__')\n"),
    ("eval-exec", "locals()\n"),
    ("eval-exec", "namespace = globals()\n"),
    ("eval-exec", "import builtins\nbuiltins.globals()\n"),
    ("eval-exec", "import sys\nsys._getframe()\n"),
    ("eval-exec", "import sys\nsys._current_frames()\n"),
    ("eval-exec", "import inspect\ninspect.currentframe().f_back\n"),
    ("eval-exec", "from inspect import stack\n"),
    ("eval-exec", "import inspect\ninspect.trace()\n"),
    ("eval-exec", "import inspect\ninspect.getouterframes(frame)\n"),
    ("eval-exec", "import inspect as i\ni.getinnerframes(tb)\n"),
    ("eval-exec", "import types\ntypes.CodeType\n"),
    ("eval-exec", "from types import CodeType\n"),
    ("dunder", "frame.f_builtins['eval']('1')\n"),
    ("dunder", "error.__traceback__.tb_frame.f_globals\n"),
    ("dunder", "getattr(frame, 'f_locals')\n"),
    # Entry points import the object they name, from whatever object they are reached through.
    ("dynamic-import", "dist.entry_points\n"),
    (
        "dynamic-import",
        "from importlib import metadata\nmetadata.distribution('x').entry_points[0].load()\n",
    ),
    ("dynamic-import", "getattr(dist, 'entry_points')\n"),
    # One planted case for each list entry that the cases above do not reach alone.
    ("eval-exec", "import builtins\nbuiltins.compile('x = 1', '<s>', 'exec')\n"),
    ("eval-exec", "import _pickle\n"),
    ("shell", "setattr(o, 'system', f)\n"),
    ("eval-exec", "delattr(b, 'exec')\n"),
    ("shell", "hasattr(o, 'popen')\n"),
    # A shell pipeline module, subinterpreters that run strings, and distutils.
    ("shell", "import pipes\npipes.Template().open('f', 'w')\n"),
    ("shell", "from pipes import Template\n"),
    ("eval-exec", "import _interpreters\n_interpreters.run_string(i, 'x = 1')\n"),
    ("eval-exec", "import _xxsubinterpreters\n"),
    ("eval-exec", "from concurrent import interpreters\n"),
    ("eval-exec", "from distutils import spawn\n"),
    ("eval-exec", "from distutils.core import run_setup\nrun_setup('setup.py')\n"),
    # vars() with no argument is locals().
    ("eval-exec", "vars()['x']\n"),
    ("eval-exec", "namespace = vars\n"),
    ("eval-exec", "vars(*args)\n"),
    ("eval-exec", "import builtins\nbuiltins.vars()\n"),
    # gc reaches every live object.
    ("eval-exec", "import gc\ngc.get_objects()\n"),
    ("eval-exec", "from gc import get_referrers\n"),
    ("eval-exec", "import gc\ngc.get_referents(value)\n"),
    ("eval-exec", "import gc\ngc.collect()\n"),
    # typing evaluates strings as code; sys.remote_exec runs a file in another process.
    ("eval-exec", "import typing\ntyping.get_type_hints(f)\n"),
    ("eval-exec", "from typing import ForwardRef\nForwardRef('x')\n"),
    ("eval-exec", "from typing import evaluate_forward_ref\n"),
    ("eval-exec", "import typing\ntyping._eval_type(t, g, l)\n"),
    ("eval-exec", "import sys\nsys.remote_exec(pid, path)\n"),
    # help and the breakpoint hook import what a string names.
    ("dynamic-import", "help('os')\n"),
    ("dynamic-import", "help(name)\n"),
    ("dynamic-import", "show = help\n"),
    ("dynamic-import", "import builtins\nbuiltins.help('os')\n"),
    ("dynamic-import", "breakpoint()\n"),
    ("dynamic-import", "import builtins\nbuiltins.breakpoint()\n"),
    ("dynamic-import", "import sys\nsys.breakpointhook()\n"),
    ("dynamic-import", "import sys\nsys.__breakpointhook__()\n"),
    # A module's own loader and spec, as bare names.
    ("dunder", "type(__loader__)('platform', path).load_module()\n"),
    ("dunder", "print(__spec__)\n"),
    # Code objects, from which code is rebuilt and run, and the hooks that are handed frames.
    ("dunder", "def g():\n    yield\ncode = g().gi_code.replace(co_code=blob)\n"),
    ("dunder", "frame.f_code\n"),
    ("eval-exec", "import sys\nsys.settrace(tracer)\n"),
    ("eval-exec", "import sys\nsys.setprofile(profiler)\n"),
    ("eval-exec", "import sys\nsys.monitoring.register_callback(tool, event, f)\n"),
    ("eval-exec", "import threading\nthreading.settrace(tracer)\n"),
    ("eval-exec", "from threading import setprofile_all_threads\n"),
    ("eval-exec", "import logging\nlogging.currentframe()\n"),
    # Windows APIs, and keyring's loader of a module named by a string.
    ("native-code", "import win32ctypes\n"),
    ("native-code", "from win32ctypes.core import ctypes\n"),
    ("dynamic-import", "from keyring.core import load_keyring\nload_keyring('socket.socket')\n"),
    ("dynamic-import", "import keyring.core\nkeyring.core._load_keyring_class(name)\n"),
    # One name imported as two things: a use could mean either, so the lint refuses the name.
    ("parse", "import os as x\ndef _unused():\n    import json as x\nx.system('id')\n"),
    ("parse", "import builtins as b\ndef _unused():\n    import json as b\nb.eval('1')\n"),
    ("parse", "from os import system as run\nfrom json import loads as run\n"),
    # A class pattern reads the attributes it names, as an attribute does.
    (
        "dunder",
        "match _f:\n    case object(__globals__=g, __builtins__=b):\n        b['x']('...')\n",
    ),
    ("dunder", "match _f:\n    case object(gi_frame=frame):\n        pass\n"),
    # __match_args__ names the attributes a positional class pattern reads.
    ("dunder", "class C:\n    __match_args__ = ('__globals__',)\n"),
    ("dunder", "class C:\n    __match_args__: tuple = ('f_globals',)\n"),
]

# Every attribute the [dunder] rule refuses, written out here so that dropping one from the lint
# fails a test. The builtins name is built from two literals: this file must not hold the string.
BANNED_ATTRIBUTE_NAMES = [
    "__dict__",
    "__globals__",
    "__" + "builtins__",
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
    "f_globals",
    "f_builtins",
    "f_locals",
    "gi_frame",
    "cr_frame",
    "ag_frame",
    "tb_frame",
    "gi_code",
    "cr_code",
    "ag_code",
    "f_code",
    "co_code",
]


@pytest.mark.parametrize("rel", EVERY_ZONE)
@pytest.mark.parametrize("attr", BANNED_ATTRIBUTE_NAMES)
def test_each_banned_attribute_is_reported(attr: str, rel: str) -> None:
    assert "dunder" in rules(f"value.{attr}\n", rel)
    assert "dunder" in rules(f"getattr(value, {attr!r})\n", rel)


def test_the_attribute_list_is_the_lints_list() -> None:
    assert set(BANNED_ATTRIBUTE_NAMES) == (
        ast_lint.BANNED_DUNDERS | ast_lint.FRAME_ATTRIBUTES | ast_lint.CODE_ATTRIBUTES
    )


@pytest.mark.parametrize("rel", EVERY_ZONE)
@pytest.mark.parametrize(("rule", "source"), BANNED_EVERYWHERE)
def test_everywhere_rule_reports_planted_violation(rule: str, source: str, rel: str) -> None:
    assert rule in rules(source, rel)


CLEAN_EVERYWHERE = [
    "import importlib.metadata\nimportlib.metadata.version('lucille-host')\n",
    "from importlib import metadata\nmetadata.version('lucille-host')\n",
    "from importlib.metadata import PackageNotFoundError, version\n",
    "from importlib.resources import as_file\n",
    "import ast\nast.literal_eval('1')\n",
    "cursor.execute('select 1')\n",
    "class Job:\n    def exec(self):\n        pass\n\nJob().exec()\n",
    "import platform\nplatform.system()\n",
    "from lucille_host import _exec\n_exec.run(['x'])\n",
    "import os\nos.environ.get('HOME')\n",
    "import re\nre.compile('x')\n",
    # Local names and attributes that share a watched module's name are not that module.
    "code = 'x = 1'\nprint(code)\n",
    "site = 'example.org'\nprint(site)\n",
    "def describe(exc):\n    return exc.code, exc.profile, exc.trace\n",
    "class A(B):\n    def __init__(self):\n        super().__init__()\n",
    "print(type(value).__name__)\n",
    "getattr(obj, 'name', None)\n",
    # The parts of importlib that load no code.
    "from importlib import metadata\nmetadata.distribution('lucille-host').read_text('RECORD')\n",
    "from importlib.metadata import PackagePath, files\nfiles('lucille-host')\n",
    "from importlib.resources.abc import Traversable\n"
    "def load(root: Traversable) -> None:\n    pass\n",
    # A member of a watched module patched by its dotted path.
    "def test(monkeypatch):\n    monkeypatch.setattr('os.getuid', lambda: 0)\n",
    # inspect and types are followed only through an import.
    "from types import MappingProxyType\nMappingProxyType({})\n",
    "import inspect\ninspect.signature(print)\n",
    "def describe(config):\n    return config.types, config.inspect\n",
    # vars with an argument reads that object's attributes; with none it is locals().
    "def describe(options):\n    return vars(options)\n",
    # Local names that share a rooted module's name are not that module.
    "def close(pipes, gc, typing):\n    return pipes[0], gc, typing\n",
    "class Child:\n    def close(self):\n        return self.pipes, self.gc, self.typing\n",
    # Annotations, read from typing, evaluate nothing.
    "from typing import Any, Final\nLIMIT: Final[int] = 1\ndef f(x: Any) -> None:\n    pass\n",
    # An attribute called help is not the builtin.
    "def describe(action):\n    return action.help\n",
]


@pytest.mark.parametrize("rel", EVERY_ZONE)
@pytest.mark.parametrize("source", CLEAN_EVERYWHERE)
def test_ordinary_code_is_clean_everywhere(source: str, rel: str) -> None:
    found = rules(source, rel)
    if rel in RUNTIME_ZONES:
        # Several of these import modules the runtime may not; that rule is tested on its own.
        found.discard("runtime-import")
    assert found == set()


# --- In src/lucille_host/ --------------------------------------------------------------------

BANNED_IN_PACKAGE = [
    ("dynamic-import", "import sys\nsys.modules['subprocess'].Popen(['ls'])\n"),
    ("import-path", "import sys\nsys.path.insert(0, '/tmp/x')\n"),
    ("import-path", "import sys\nsys.meta_path.append(finder)\n"),
    ("import-path", "import sys\nsys.path_hooks.append(hook)\n"),
    ("import-path", "import site\nsite.addsitedir('/tmp/x')\n"),
    ("import-path", "import sys\nsys.path_importer_cache.clear()\n"),
]


@pytest.mark.parametrize("rel", [RUNTIME, EXEC, CONFORMANCE, SPAWN])
@pytest.mark.parametrize(("rule", "source"), BANNED_IN_PACKAGE)
def test_package_rule_reports_planted_violation(rule: str, source: str, rel: str) -> None:
    assert rule in rules(source, rel)


@pytest.mark.parametrize("rel", [TEST, TOOL])
@pytest.mark.parametrize(("rule", "source"), BANNED_IN_PACKAGE)
def test_tests_and_tools_may_touch_the_import_path(rule: str, source: str, rel: str) -> None:
    assert rule not in rules(source, rel)


# --- Starting a process ----------------------------------------------------------------------

# Importing a subprocess module: forbidden in the runtime, but allowed in the conformance package
# for the members that start nothing.
SUBPROCESS_MODULE_IMPORTS = [
    "import subprocess\n",
    "from asyncio import subprocess\n",
    "import asyncio.subprocess\n",
]

# Modules that start a process and are also refused everywhere, under another rule.
SPAWNING_BANNED_EVERYWHERE = ["import pipes\n", "from distutils.spawn import spawn\n"]

SPAWNING = [
    *SUBPROCESS_MODULE_IMPORTS,
    "from subprocess import Popen\n",
    "import subprocess as sp\nsp.run(['ls'])\n",
    "import pty\n",
    "import multiprocessing\n",
    "from multiprocessing import Process\n",
    "import os\nos.execv('/bin/ls', ['ls'])\n",
    "import os\nos.spawnl(os.P_WAIT, '/bin/ls', 'ls')\n",
    "import os\nos.posix_spawn('/bin/ls', ['ls'], {})\n",
    "import os\nos.fork()\n",
    "import os\nos.forkpty()\n",
    "import os\nos.startfile('x')\n",
    "import asyncio\nasyncio.create_subprocess_exec('ls')\n",
    "from asyncio import subprocess\n",
    "import asyncio.subprocess\n",
    "async def run(loop):\n    await loop.subprocess_exec(object, 'ls')\n",
    "from concurrent.futures import ProcessPoolExecutor\n",
    "from concurrent.futures.process import BrokenProcessPool\n",
    "import _posixsubprocess\n",
    "import posix\nposix.fork()\n",
    "import posix\nposix.execv('/bin/ls', ['ls'])\n",
    "import webbrowser\nwebbrowser.open('https://example.org')\n",
    *SPAWNING_BANNED_EVERYWHERE,
    # A module re-exported through another module is still that module.
    "from lucille_host._exec import subprocess\nsubprocess.run(['ls'])\n",
    "from lucille_host import _exec\n_exec.subprocess.Popen(['ls'])\n",
]
STARTS_A_PROCESS = [source for source in SPAWNING if source not in SUBPROCESS_MODULE_IMPORTS]


@pytest.mark.parametrize("source", SPAWNING)
def test_runtime_package_spawning_outside_exec_is_reported(source: str) -> None:
    assert "spawn-outside-exec" in rules(source, RUNTIME)


@pytest.mark.parametrize(
    "rel",
    [
        "src/lucille_host/_switchboard.py",
        "src/lucille_host/ops/_exec.py",
        "src/lucille_host/_exec_helpers.py",
        "src/lucille_host/sub/_exec.py",
    ],
)
@pytest.mark.parametrize("source", ["import subprocess\n", "import os\nos.fork()\n"])
def test_the_exec_exemption_is_for_that_exact_path(source: str, rel: str) -> None:
    assert "spawn-outside-exec" in rules(source, rel)


@pytest.mark.parametrize("source", STARTS_A_PROCESS)
def test_conformance_spawning_outside_spawn_module_is_reported(source: str) -> None:
    assert "conformance-spawn" in rules(source, CONFORMANCE)


CONFORMANCE_ALLOWED = [
    *SUBPROCESS_MODULE_IMPORTS,
    "import subprocess\ntry:\n    pass\nexcept subprocess.TimeoutExpired:\n    pass\n",
    "from subprocess import PIPE, CalledProcessError\n",
    "import subprocess\ndef wait(process: subprocess.Popen[bytes]) -> None:\n    pass\n",
]


@pytest.mark.parametrize("source", CONFORMANCE_ALLOWED)
def test_conformance_may_name_what_starts_nothing(source: str) -> None:
    assert rules(source, CONFORMANCE) == set()


CONFORMANCE_DENIED = [
    "import subprocess\nsubprocess.run(['lucille-host'])\n",
    "import subprocess\nsubprocess.Popen\n",
    "from subprocess import Popen\n",
    "from subprocess import PIPE, run\n",
]


@pytest.mark.parametrize("source", CONFORMANCE_DENIED)
def test_conformance_may_not_start_a_process_outside_spawn(source: str) -> None:
    assert "conformance-spawn" in rules(source, CONFORMANCE)


@pytest.mark.parametrize("source", SPAWNING)
def test_exec_module_is_exempt_from_the_spawn_rule(source: str) -> None:
    # The exec module may start a process. What it may import is a separate rule, below, so
    # most of these modules are still refused there, as [runtime-import].
    assert rules(source, EXEC) - {"runtime-import", "shell", "eval-exec"} == set()
    assert "spawn-outside-exec" not in rules(source, EXEC)


EXEC_ALLOWED = [
    "import subprocess\nsubprocess.run(['/bin/true'], check=True)\n",
    # Output is read by a reader thread: a selector would need register, which is refused.
    "import subprocess\nimport threading\n"
    "process = subprocess.Popen(['/bin/true'], stdout=subprocess.PIPE)\n"
    "threading.Thread(target=process.wait).start()\n",
    "import os\nimport signal\nos.killpg(group, signal.SIGKILL)\n",
]


@pytest.mark.parametrize("source", EXEC_ALLOWED)
def test_exec_module_may_start_a_process(source: str) -> None:
    assert rules(source, EXEC) == set()


@pytest.mark.parametrize("rel", [TEST, TOOL])
@pytest.mark.parametrize(
    "source", [source for source in SPAWNING if source not in SPAWNING_BANNED_EVERYWHERE]
)
def test_tests_and_tools_may_spawn(source: str, rel: str) -> None:
    assert rules(source, rel) == set()


def test_conformance_is_excluded_from_the_runtime_rule_by_name() -> None:
    assert ast_lint.zone_of("src/lucille_host/conformance/scenario.py") == "conformance"
    assert ast_lint.zone_of("src/lucille_host/conformance_helpers.py") == "runtime"


# --- conformance/_spawn.py -------------------------------------------------------------------

SPAWN_ALLOWED = [
    'import subprocess\nsubprocess.Popen(["switchboard", "serve", "--port", "0"])\n',
    'import subprocess\nsubprocess.run(["lucille-host", "status"], stdout=subprocess.PIPE)\n',
    'from subprocess import run\nrun(["lucille-host", "--version"], check=True)\n',
    'import subprocess\nsubprocess.run(args=["lucille-host", "status"])\n',
    "import shutil\nimport subprocess\n"
    'LUCILLE_HOST_EXE = shutil.which("lucille-host")\n'
    'subprocess.run([LUCILLE_HOST_EXE, "status"])\n',
    "import subprocess\nfrom typing import Final\n"
    'SWITCHBOARD_EXE: Final = "switchboard"\n'
    'subprocess.Popen([SWITCHBOARD_EXE, "serve"])\n',
    "import asyncio\n"
    "async def start():\n"
    '    await asyncio.create_subprocess_exec("switchboard", "serve", "--port", "0")\n',
    "import asyncio\n"
    "async def start():\n"
    '    await asyncio.create_subprocess_exec(*["lucille-host", "status"])\n',
    "import subprocess\ntry:\n    pass\nexcept subprocess.TimeoutExpired:\n    pass\n",
    # The program installed beside the interpreter running the suite, by absolute path.
    "import subprocess\nimport sys\nfrom pathlib import Path\n"
    'LUCILLE_HOST_EXE = Path(sys.executable).parent / "lucille-host"\n'
    'subprocess.run([LUCILLE_HOST_EXE, "status"], env={"LANG": "C"})\n',
    "import subprocess\nimport sysconfig\nfrom pathlib import Path\n"
    'SWITCHBOARD_EXE = Path(sysconfig.get_path("scripts")) / "switchboard"\n'
    'subprocess.Popen([SWITCHBOARD_EXE, "serve"])\n',
    "import shutil\nimport subprocess\n"
    'LUCILLE_HOST_EXE = shutil.which("lucille-host")\n'
    "subprocess.run([LUCILLE_HOST_EXE], env={})\n",
    # Arguments after the program may be unpacked.
    'import subprocess\ndef start(args):\n    return subprocess.run(["lucille-host", *args])\n',
    # A launcher in a type annotation starts nothing.
    "import subprocess\n"
    "def start() -> subprocess.Popen[bytes]:\n"
    '    return subprocess.Popen(["lucille-host"])\n',
    # asyncio.subprocess.create_subprocess_exec is asyncio.create_subprocess_exec.
    "import asyncio.subprocess\n"
    "async def start():\n"
    '    await asyncio.subprocess.create_subprocess_exec("switchboard", "serve")\n',
]


@pytest.mark.parametrize("source", SPAWN_ALLOWED)
def test_spawn_module_may_start_the_hub_and_the_host(source: str) -> None:
    assert rules(source, SPAWN) == set()


SPAWN_DENIED = [
    ("conformance-argv", 'import subprocess\nsubprocess.run(["sh", "-c", "ls"])\n'),
    ("conformance-argv", 'import subprocess\nargv = ["lucille-host"]\nsubprocess.run(argv)\n'),
    ("conformance-argv", 'import subprocess\nsubprocess.run(("lucille-host",))\n'),
    ("conformance-argv", 'import subprocess\nsubprocess.Popen(["switchboard", "pair", "x"])\n'),
    ("conformance-argv", 'import subprocess\nsubprocess.Popen(["switchboard"])\n'),
    (
        "conformance-argv",
        'import subprocess\nsubprocess.run(["lucille-host"], executable="/bin/sh")\n',
    ),
    ("conformance-argv", 'import subprocess\nkw = {}\nsubprocess.run(["lucille-host"], **kw)\n'),
    ("conformance-argv", 'import subprocess\nsubprocess.run([*rest, "x"])\n'),
    ("conformance-argv", "import subprocess\nsubprocess.run([])\n"),
    (
        "conformance-argv",
        'import subprocess\nOTHER_EXE = "lucille-host"\nsubprocess.run([OTHER_EXE])\n',
    ),
    (
        "conformance-argv",
        'import subprocess\nLUCILLE_HOST_EXE = "/bin/sh"\nsubprocess.run([LUCILLE_HOST_EXE])\n',
    ),
    (
        "conformance-argv",
        'import subprocess\nLUCILLE_HOST_EXE = "lucille-host"\nLUCILLE_HOST_EXE = "/bin/sh"\n'
        "subprocess.run([LUCILLE_HOST_EXE])\n",
    ),
    (
        "conformance-argv",
        'import subprocess\nLUCILLE_HOST_EXE = "lucille-host"\n'
        "def start(LUCILLE_HOST_EXE):\n    subprocess.run([LUCILLE_HOST_EXE])\n",
    ),
    (
        "conformance-argv",
        "import asyncio\n"
        "async def start(program):\n"
        '    await asyncio.create_subprocess_exec(program, "x")\n',
    ),
    (
        "conformance-argv",
        "import asyncio\nasync def start(argv):\n    await asyncio.create_subprocess_exec(*argv)\n",
    ),
    (
        "conformance-argv",
        "import shutil\nimport subprocess\n"
        'LUCILLE_HOST_EXE = shutil.which("sh")\nsubprocess.run([LUCILLE_HOST_EXE])\n',
    ),
    (
        "conformance-argv",
        'import subprocess\nLUCILLE_HOST_EXE = f("lucille-host")\n'
        "subprocess.run([LUCILLE_HOST_EXE])\n",
    ),
    (
        "conformance-argv",
        "import subprocess\nimport sys\nfrom pathlib import Path\n"
        'LUCILLE_HOST_EXE = Path(sys.executable).parent / "sh"\n'
        "subprocess.run([LUCILLE_HOST_EXE])\n",
    ),
    # Positional executable: Popen's third positional parameter replaces the program.
    (
        "conformance-argv",
        'import subprocess\nsubprocess.run(["lucille-host", "status"], -1, "/bin/sh")\n',
    ),
    ("conformance-argv", 'import subprocess\nsubprocess.Popen(["lucille-host"], -1, "/bin/sh")\n'),
    # A bare program name is looked up on the child's PATH, which env= sets.
    (
        "conformance-argv",
        'import subprocess\nsubprocess.run(["lucille-host"], env={"PATH": "/tmp/x"})\n',
    ),
    (
        "conformance-argv",
        'import subprocess\nLUCILLE_HOST_EXE = "lucille-host"\n'
        'subprocess.run([LUCILLE_HOST_EXE], env={"PATH": "/tmp/x"})\n',
    ),
    ("conformance-argv", 'import subprocess\nsubprocess.run(["switchboard", *args])\n'),
    (
        "conformance-argv",
        "import asyncio.subprocess\n"
        "async def start(argv):\n"
        "    await asyncio.subprocess.create_subprocess_exec(*argv)\n",
    ),
    # A program constant must be one of the allowed forms, not any path ending in the name.
    (
        "conformance-argv",
        "import subprocess\nfrom pathlib import Path\n"
        'LUCILLE_HOST_EXE = Path("/tmp/evil") / "lucille-host"\n'
        "subprocess.run([LUCILLE_HOST_EXE])\n",
    ),
    # What a program constant is resolved through may not be changed first.
    (
        "conformance-argv",
        "import shutil\nimport subprocess\n"
        'shutil.which = lambda name: "/bin/sh"\n'
        'LUCILLE_HOST_EXE = shutil.which("lucille-host")\n'
        "subprocess.run([LUCILLE_HOST_EXE])\n",
    ),
    (
        "conformance-argv",
        "import subprocess\nimport sys\nfrom pathlib import Path\n"
        'sys.executable = "/tmp/evil/python"\n'
        'LUCILLE_HOST_EXE = Path(sys.executable).parent / "lucille-host"\n'
        "subprocess.run([LUCILLE_HOST_EXE])\n",
    ),
    (
        "conformance-argv",
        "import os\nimport shutil\nimport subprocess\n"
        'os.environ["PATH"] = "/tmp/evil"\n'
        'LUCILLE_HOST_EXE = shutil.which("lucille-host")\n'
        "subprocess.run([LUCILLE_HOST_EXE])\n",
    ),
    (
        "conformance-argv",
        "import shutil\nimport subprocess\n"
        'setattr(shutil, "which", fake)\n'
        'LUCILLE_HOST_EXE = shutil.which("lucille-host")\n'
        "subprocess.run([LUCILLE_HOST_EXE])\n",
    ),
    (
        "conformance-argv",
        "import subprocess\nimport sys\nfrom pathlib import Path\n"
        "Path = Fake\n"
        'LUCILLE_HOST_EXE = Path(sys.executable).parent / "lucille-host"\n'
        "subprocess.run([LUCILLE_HOST_EXE])\n",
    ),
    ("conformance-argv", "import sys\ndel sys.executable\n"),
    ("conformance-spawn", "import subprocess\nlaunch = subprocess.run\n"),
    ("conformance-spawn", "import subprocess\nlaunch = subprocess.Popen\n"),
    ("conformance-spawn", "import webbrowser\n"),
    ("conformance-spawn", 'import subprocess\nsubprocess.getoutput("ls")\n'),
    ("conformance-spawn", "from subprocess import getoutput\n"),
    ("conformance-spawn", 'import asyncio\nasyncio.create_subprocess_shell("ls")\n'),
    ("conformance-spawn", 'import os\nos.execv("/usr/bin/env", ["env"])\n'),
    ("conformance-spawn", "import pty\n"),
    ("shell", 'import subprocess\nsubprocess.run(["lucille-host"], shell=True)\n'),
]


@pytest.mark.parametrize(("rule", "source"), SPAWN_DENIED)
def test_spawn_module_rejects_any_other_process(rule: str, source: str) -> None:
    assert rule in rules(source, SPAWN)


@pytest.mark.parametrize(
    ("rel", "rule"),
    [
        ("src/lucille_host/conformance/my_spawn.py", "conformance-spawn"),
        ("src/lucille_host/conformance/sub/_spawn.py", "conformance-spawn"),
        ("src/lucille_host/_spawn.py", "spawn-outside-exec"),
    ],
)
def test_the_spawn_exemption_is_for_that_exact_path(rel: str, rule: str) -> None:
    source = 'import subprocess\nsubprocess.run(["lucille-host", "status"])\n'
    assert rule in rules(source, rel)


# --- The runtime's import allowlist -----------------------------------------------------------

RUNTIME_IMPORT_DENIED = [
    "import pipes\n",
    "import _interpreters\n",
    "from distutils import spawn\n",
    "import code\n",
    "import socket\n",
    "import platform\n",
    "import uuid\n",
    "import string\n",
    "import selectors\n",
    "import subprocess\n",
    "from importlib import metadata\n",
    "import os.path, socket\n",
    "from socket import *\n",
    "import urllib.request\n",
    "from http.client import HTTPSConnection\n",
    # The conformance package is not part of the runtime and is not held to the allowlist.
    "from lucille_host.conformance import main\n",
    "import lucille_host.conformance._spawn\n",
    "from .conformance import main\n",
    "from . import conformance\n",
    "import lucille_host\nlucille_host.conformance.main()\n",
    # An import anywhere in the file counts, not only at module level.
    "def f():\n    import socket\n",
    "try:\n    import socket\nexcept ImportError:\n    socket = None\n",
    "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import platform\n",
    "class Probe:\n    import uuid\n",
    "async def f():\n    from string import Formatter\n",
]


@pytest.mark.parametrize("source", RUNTIME_IMPORT_DENIED)
def test_runtime_import_off_the_allowlist_is_reported(source: str) -> None:
    assert "runtime-import" in rules(source, RUNTIME)


@pytest.mark.parametrize(
    "source",
    [
        "from ..conformance import main\n",
        "from .. import conformance\n",
        "import socket\n",
    ],
)
def test_runtime_import_is_checked_in_subpackages(source: str) -> None:
    assert "runtime-import" in rules(source, "src/lucille_host/ops/catalog.py")


RUNTIME_IMPORT_ALLOWED = [
    "from __future__ import annotations\n",
    "import json\n",
    "from collections.abc import Sequence\n",
    "from typing import Final\n",
    "import keyring\n",
    "from switchboard import pairing\n",
    "import lucille_host\n",
    "from lucille_host import cli\n",
    "from . import _exec\n",
    "from .ops import OPERATIONS\n",
    # A submodule comes with its top-level module.
    "import os.path\n",
    "import logging.handlers\n",
]


@pytest.mark.parametrize("rel", RUNTIME_ZONES)
@pytest.mark.parametrize("source", RUNTIME_IMPORT_ALLOWED)
def test_runtime_may_import_what_the_allowlist_names(source: str, rel: str) -> None:
    assert rules(source, rel) == set()


def test_exec_module_may_import_its_extra() -> None:
    assert rules("import subprocess\n", EXEC) == set()


@pytest.mark.parametrize(
    "source", ["import pipes\n", "import socket\n", "import pty\n", "import selectors\n"]
)
def test_exec_module_may_not_import_anything_else(source: str) -> None:
    assert "runtime-import" in rules(source, EXEC)


@pytest.mark.parametrize("rel", [CONFORMANCE, SPAWN, TEST, TOOL])
@pytest.mark.parametrize("source", RUNTIME_IMPORT_DENIED)
def test_runtime_import_rule_is_for_the_runtime_only(source: str, rel: str) -> None:
    assert "runtime-import" not in rules(source, rel)


def test_the_runtime_import_allowlist_is_pinned() -> None:
    assert ast_lint.RUNTIME_IMPORTS == {
        "__future__",
        "argparse",
        "base64",
        "binascii",
        "collections",
        "contextlib",
        "copy",
        "dataclasses",
        "datetime",
        "enum",
        "errno",
        "functools",
        "hashlib",
        "hmac",
        "io",
        "itertools",
        "json",
        "logging",
        "math",
        "os",
        "pathlib",
        "re",
        "secrets",
        "shutil",
        "signal",
        "stat",
        "sys",
        "tempfile",
        "termios",
        "textwrap",
        "threading",
        "time",
        "tomllib",
        "types",
        "typing",
        "keyring",
        "switchboard",
        "lucille_host",
    }
    assert ast_lint.EXEC_IMPORTS == {"subprocess"}


# --- Modules of this package that share a watched module's name ----------------------------

OWN = frozenset(
    {
        "lucille_host",
        "lucille_host.cli",
        "lucille_host.code",
        "lucille_host.inspect",
        "lucille_host.ops",
        "lucille_host.ops.types",
        "lucille_host.pickle",
        "lucille_host.profile",
        "lucille_host.trace",
    }
)

OWN_MODULE_SOURCES = [
    "from lucille_host import profile\nprofile.run('x')\nhand_over(profile)\n",
    "import lucille_host.profile\nlucille_host.profile.run('x')\n",
    "from lucille_host.ops import types\ntypes.CodeType\nhand_over(types)\n",
    "import lucille_host.ops.types\nlucille_host.ops.types.CodeType\n",
    "from lucille_host import code, inspect, trace\ncode.run(inspect.stack(), trace.Trace)\n",
    # The same holds for a module named like one this lint follows everywhere.
    "from lucille_host import pickle\npickle.loads(data)\nhand_over(pickle)\n",
]


@pytest.mark.parametrize("rel", EVERY_ZONE)
@pytest.mark.parametrize("source", OWN_MODULE_SOURCES)
def test_a_module_of_this_package_is_not_the_stdlib_module_of_its_name(
    source: str, rel: str
) -> None:
    found = ast_lint.check_source(source, rel, OWN)
    if rel in RUNTIME_ZONES:
        # In the runtime no module is a value, this package's own included; the finding names the
        # package's module, not the standard library's.
        found = [
            v
            for v in found
            if not (v.rule == "module-object" and v.message.startswith("lucille_host."))
        ]
    assert found == []


@pytest.mark.parametrize(
    "source",
    [
        "from . import profile\nprofile.run('x')\n",
        "from .ops import types\nhand_over(types)\n",
        "from . import inspect\ninspect.currentframe()\n",
    ],
)
def test_a_relative_import_of_this_package_is_its_own_module(source: str) -> None:
    found = ast_lint.check_source(source, RUNTIME, OWN)
    assert [
        v
        for v in found
        if not (v.rule == "module-object" and v.message.startswith("lucille_host."))
    ] == []


STDLIB_MODULES_STILL_CAUGHT = [
    ("eval-exec", "import profile\nprofile.run('x')\n"),
    ("eval-exec", "from helpers import profile as p\np.run('x')\n"),
    ("module-object", "import types\nhand_over(types)\n"),
    # A standard-library module re-exported through a module of this package is still that module.
    ("eval-exec", "from lucille_host.cli import inspect\ninspect.currentframe()\n"),
    ("eval-exec", "import lucille_host.cli\nlucille_host.cli.types.CodeType\n"),
    ("module-object", "from lucille_host.cli import code\nhand_over(code)\n"),
    ("eval-exec", "from lucille_host import profile\nprofile.trace.Trace\n"),
    ("eval-exec", "from lucille_host.cli import pickle\npickle.loads(data)\n"),
    ("spawn-outside-exec", "from lucille_host.ops import subprocess\nsubprocess.run(['x'])\n"),
]


@pytest.mark.parametrize(("rule", "source"), STDLIB_MODULES_STILL_CAUGHT)
def test_stdlib_modules_are_still_caught_beside_own_modules(rule: str, source: str) -> None:
    assert rule in rules(source, RUNTIME, OWN)


def test_the_tree_check_knows_the_packages_own_modules(tmp_path: Path) -> None:
    package = tmp_path / "src" / "lucille_host"
    (package / "ops").mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "profile.py").write_text("def run(name):\n    return name\n")
    (package / "ops" / "__init__.py").write_text("")
    (package / "ops" / "types.py").write_text("Kind = str\n")
    (package / "cli.py").write_text(
        "from lucille_host import profile\nfrom lucille_host.ops import types\n"
        "profile.run(types.Kind)\n"
    )
    assert ast_lint.check(tmp_path) == []
    (package / "cli.py").write_text("import profile\nprofile.run('x')\n")
    assert {v.rule for v in ast_lint.check(tmp_path)} >= {"eval-exec", "runtime-import"}


# --- Compiled files ---------------------------------------------------------------------------

COMPILED = [
    "src/lucille_host/helper.cpython-313-x86_64-linux-gnu.so",
    "src/lucille_host/helper.abi3.so",
    "src/lucille_host/helper.pyc",
    "src/lucille_host/helper.pyo",
    "src/lucille_host/helper.pyd",
    "src/lucille_host/libhelper.dylib",
    "src/lucille_host/HELPER.SO",
    # Outside the package too: a compiled module beside a guard would shadow the guard's source.
    "tools/guards/_common.cpython-313-x86_64-linux-gnu.so",
    "tests/helper.pyc",
]


@pytest.mark.parametrize("rel", COMPILED)
def test_a_compiled_file_is_reported(tmp_path: Path, rel: str) -> None:
    source = tmp_path / "src" / "lucille_host" / "cli.py"
    source.parent.mkdir(parents=True)
    source.write_text("import json\n")
    compiled = tmp_path / rel
    compiled.parent.mkdir(parents=True, exist_ok=True)
    compiled.write_bytes(b"\x7fELF")
    assert [(v.path, v.rule) for v in ast_lint.check(tmp_path)] == [(rel, "compiled")]


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def test_compiled_files_follow_git(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    package = tmp_path / "src" / "lucille_host"
    (package / "__pycache__").mkdir(parents=True)
    (package / "cli.py").write_text("import json\n")
    (tmp_path / ".gitignore").write_text("__pycache__/\n*.py[cod]\n")
    (package / "__pycache__" / "cli.cpython-313.pyc").write_bytes(b"\0")  # ignored
    (package / "lone.pyc").write_bytes(b"\0")  # ignored
    (package / "native.so").write_bytes(b"\0")  # untracked, not ignored
    found = ast_lint.check(tmp_path)
    assert [(v.path, v.rule) for v in found] == [("src/lucille_host/native.so", "compiled")]
    # A compiled file added to git anyway is part of the repository.
    _git(tmp_path, "add", "-f", "src/lucille_host/lone.pyc")
    _git(tmp_path, "add", "-f", "src/lucille_host/__pycache__/cli.cpython-313.pyc")
    found = ast_lint.check(tmp_path)
    assert {v.path for v in found} == {
        "src/lucille_host/native.so",
        "src/lucille_host/lone.pyc",
        "src/lucille_host/__pycache__/cli.cpython-313.pyc",
    }


def test_outside_git_the_bytecode_cache_is_not_reported(tmp_path: Path) -> None:
    # Outside a git work tree the walk skips __pycache__, which Python writes on every run.
    package = tmp_path / "src" / "lucille_host"
    (package / "__pycache__").mkdir(parents=True)
    (package / "cli.py").write_text("import json\n")
    (package / "__pycache__" / "cli.cpython-313.pyc").write_bytes(b"\0")
    assert ast_lint.check(tmp_path) == []


# --- Re-exported modules ----------------------------------------------------------------------
# A module that an allowed standard-library module binds is that module, under whatever name it
# is bound (tools/guards/_reexports.py); so is a watched module bound under its name with leading
# underscores; and so is a module another module of this package binds.

REEXPORT_EVERYWHERE = [
    ("shell", "from tempfile import _os\n_os.system('id')\n"),
    ("shell", "import tempfile\ntempfile._os.popen('id')\n"),
    ("shell", "from argparse import _os\n_os.system('id')\n"),
    ("shell", "from threading import _os as o\no.system('id')\n"),
    ("eval-exec", "from enum import bltns\nbltns.eval('6 * 7')\n"),
    ("eval-exec", "import enum\nenum.bltns.exec('x = 1')\n"),
    ("eval-exec", "from enum import bltns as b\nb.compile('1', 'f', 'eval')\n"),
    ("eval-exec", "from tempfile import _types\n_types.CodeType\n"),
    ("eval-exec", "from logging.handlers import pickle\npickle.loads(data)\n"),
    # Not in the table: a watched module bound under its name with leading underscores.
    ("shell", "from random import _os\n_os.system('id')\n"),
    ("module-object", "from tempfile import _os\nhand_over(_os)\n"),
]


@pytest.mark.parametrize("rel", EVERY_ZONE)
@pytest.mark.parametrize(("rule", "source"), REEXPORT_EVERYWHERE)
def test_a_re_exported_module_is_that_module(rule: str, source: str, rel: str) -> None:
    assert rule in rules(source, rel)


REEXPORT_SPAWNING = [
    "from tempfile import _os\n_os.posix_spawn('/bin/true', ['true'], {})\n",
    "from argparse import _os\n_os.execv('/bin/true', ['true'])\n",
    "from threading import _os\n_os.fork()\n",
    "from tempfile import _os as o\no.fork()\n",
    "import logging.handlers\nlogging.handlers.os.posix_spawn('/bin/true', ['true'], {})\n",
]


@pytest.mark.parametrize("source", REEXPORT_SPAWNING)
def test_a_re_exported_module_starts_no_process_outside_its_place(source: str) -> None:
    assert "spawn-outside-exec" in rules(source, RUNTIME)
    assert "conformance-spawn" in rules(source, CONFORMANCE)
    assert "conformance-spawn" in rules(source, SPAWN)


RUNTIME_REEXPORT_DENIED = [
    "from enum import bltns\n",
    "from logging.handlers import socket\nsocket.socket()\n",
    "import logging.handlers\nlogging.handlers.socket.socket()\n",
    "from tomllib._parser import string\nstring.Formatter().get_field('0', (1,), {})\n",
    "import tomllib._parser\ntomllib._parser.string.Formatter()\n",
    "from logging import traceback\n",
    "import logging\nlogging.traceback.print_stack()\n",
]


@pytest.mark.parametrize("rel", RUNTIME_ZONES)
@pytest.mark.parametrize("source", RUNTIME_REEXPORT_DENIED)
def test_the_runtime_allowlist_binds_the_module_a_re_export_is(source: str, rel: str) -> None:
    assert "runtime-import" in rules(source, rel)


def test_resolve_follows_the_table_and_this_packages_modules() -> None:
    exports = {"lucille_host.paths": {"osmod": frozenset({"os"}), "util": frozenset({"x.y"})}}
    table = {"tempfile._os": ("os",)}
    assert resolve("tempfile._os.system", table, {}) == ["tempfile._os.system", "os.system"]
    assert "os.fork" in resolve("lucille_host.paths.osmod.fork", {}, exports)
    assert resolve("json.dumps", table, exports) == ["json.dumps"]


def test_the_exports_of_a_file_are_its_imports_and_imported_chains() -> None:
    tree = ast.parse(
        "import os as osmod\nfrom subprocess import Popen as launch\nimport logging.handlers\n"
        "spawn = os.posix_spawn\nimport os\nlocal = config.path\ndef run(argv):\n    pass\n"
    )
    exports = file_exports(tree, "src/lucille_host/paths.py")
    assert exports["osmod"] == {"os"}
    assert exports["launch"] == {"subprocess.Popen"}
    assert exports["logging"] == {"logging"}
    assert exports["spawn"] == {"os.posix_spawn"}
    assert "local" not in exports and "run" not in exports


def _write_package(root: Path, files: dict[str, str]) -> None:
    package = root / "src" / "lucille_host"
    for name, text in {"__init__.py": "", **files}.items():
        (package / name).parent.mkdir(parents=True, exist_ok=True)
        (package / name).write_text(text)


# (files of src/lucille_host/, the file reported, the rule)
CROSS_MODULE = [
    (
        {
            "_exec.py": "from subprocess import Popen as launch\n",
            "cli.py": "from lucille_host._exec import launch\n"
            "launch('true', -1, None, None, None, None, None, True, True)\n",
        },
        "src/lucille_host/cli.py",
        "spawn-outside-exec",
    ),
    (
        {
            "_exec.py": "from subprocess import Popen as launch\n",
            "cli.py": "from lucille_host._exec import launch\n"
            "launch('true', -1, None, None, None, None, None, True, True)\n",
        },
        "src/lucille_host/cli.py",
        "launcher",
    ),
    (
        {
            "_exec.py": "from subprocess import Popen\n",
            "ops/sneaky.py": "from lucille_host import _exec\n"
            "_exec.Popen(['/bin/sh', '-c', 'id'])\n",
        },
        "src/lucille_host/ops/sneaky.py",
        "spawn-outside-exec",
    ),
    (
        {
            "_exec.py": "from subprocess import Popen\n",
            "ops/sneaky.py": "from lucille_host import _exec\nspawn = _exec.Popen\n",
        },
        "src/lucille_host/ops/sneaky.py",
        "launcher",
    ),
    (
        {
            "_exec.py": "import subprocess as sp\n",
            "ops/sneaky.py": "from lucille_host import _exec\n"
            "_exec.sp.Popen(['/bin/true'], -1, None, None, None, None, None, True, True)\n",
        },
        "src/lucille_host/ops/sneaky.py",
        "launcher",
    ),
    (
        {
            "_exec.py": "import subprocess as sp\n",
            "cli.py": "from lucille_host._exec import sp\nsp.run(['true'])\n",
        },
        "src/lucille_host/cli.py",
        "spawn-outside-exec",
    ),
    (
        {
            "_exec.py": "import os\nspawn = os.posix_spawn\n",
            "cli.py": "from lucille_host import _exec\n_exec.spawn('/bin/sh', ['sh'], {})\n",
        },
        "src/lucille_host/cli.py",
        "spawn-outside-exec",
    ),
    (
        {
            "paths.py": "import os as _os\nimport os as osmod\n",
            "ops/probe.py": "from lucille_host import paths\npaths.osmod.system('id')\n",
        },
        "src/lucille_host/ops/probe.py",
        "shell",
    ),
    (
        {
            "paths.py": "import os as _os\n",
            "ops/probe.py": "from lucille_host import paths\n"
            "paths._os.posix_spawn('/bin/sh', [], {})\n",
        },
        "src/lucille_host/ops/probe.py",
        "spawn-outside-exec",
    ),
    (
        {
            "paths.py": "import os as osmod\n",
            "ops/probe.py": "from lucille_host import paths\npaths.osmod.fork()\n",
        },
        "src/lucille_host/ops/probe.py",
        "spawn-outside-exec",
    ),
    (
        {
            "paths.py": "import sys as s\n",
            "ops/probe.py": "from lucille_host import paths\npaths.s.modules['subprocess']\n",
        },
        "src/lucille_host/ops/probe.py",
        "dynamic-import",
    ),
    (
        {
            "paths.py": "import enum as en\n",
            "cli.py": "from lucille_host import paths\npaths.en.bltns.exec('x = 1')\n",
        },
        "src/lucille_host/cli.py",
        "eval-exec",
    ),
    (
        {
            "conformance/__init__.py": "",
            "conformance/_spawn.py": "from subprocess import Popen\n",
            "conformance/scenario.py": "from lucille_host.conformance._spawn import Popen\n"
            "Popen(['/usr/bin/curl', 'https://example.org'])\n",
        },
        "src/lucille_host/conformance/scenario.py",
        "conformance-spawn",
    ),
]


@pytest.mark.parametrize(("files", "rel", "rule"), CROSS_MODULE)
def test_a_name_taken_from_another_module_of_this_package_is_followed(
    tmp_path: Path, files: dict[str, str], rel: str, rule: str
) -> None:
    _write_package(tmp_path, files)
    assert (rel, rule) in {(v.path, v.rule) for v in ast_lint.check(tmp_path)}


def test_a_name_that_stands_for_too_many_names_fails_closed(tmp_path: Path) -> None:
    # A name bound many ways would push the dangerous one past what the lint follows.
    decoys = "".join(f"x = json.decoy{index}\n" for index in range(300))
    _write_package(
        tmp_path,
        {
            "paths.py": f"import json\n{decoys}import os as x\n",
            "cli.py": "from lucille_host import paths\npaths.x.fork()\n",
        },
    )
    found = {(v.path, v.rule) for v in ast_lint.check(tmp_path)}
    assert ("src/lucille_host/cli.py", "parse") in found


def test_a_function_this_package_defines_is_not_followed(tmp_path: Path) -> None:
    _write_package(
        tmp_path,
        {
            "_exec.py": "import subprocess\n"
            "def run(argv):\n    return subprocess.run(argv, check=False)\n",
            "cli.py": "from lucille_host import _exec\n_exec.run(['true'])\n",
        },
    )
    assert ast_lint.check(tmp_path) == []


# --- The runtime: no module as a value; keyring's surface; no * import ------------------------

RUNTIME_MODULE_VALUES = [
    "import json\nhand_over(json)\n",
    "import json\ngetattr(json, name)\n",
    "from collections import abc\nhand_over(abc)\n",
    "import os.path\nhand_over(os.path)\n",
    "from lucille_host import cli\ngetattr(cli, name)\n",
    "import lucille_host\ngetattr(lucille_host, 'conformance')\n",
    "import tempfile\nT = tempfile\n",
    "from logging import handlers\nkept = handlers\n",
    "import keyring\nhand_over(keyring)\n",
]


@pytest.mark.parametrize("rel", RUNTIME_ZONES)
@pytest.mark.parametrize("source", RUNTIME_MODULE_VALUES)
def test_the_runtime_uses_no_module_as_a_value(source: str, rel: str) -> None:
    assert "module-object" in rules(source, rel)


@pytest.mark.parametrize("rel", [CONFORMANCE, TEST, TOOL])
def test_only_watched_modules_are_refused_as_values_elsewhere(rel: str) -> None:
    assert rules("import json\nhand_over(json)\n", rel) == set()


RUNTIME_MODULE_MEMBERS = [
    "import json\njson.dumps({})\n",
    "from collections import abc\n"
    "def f(x: abc.Mapping) -> bool:\n    return isinstance(x, abc.Mapping)\n",
    "import types\ndef f(m: types.ModuleType) -> None:\n    pass\n",
    "from lucille_host import cli\ncli.main([])\n",
]


@pytest.mark.parametrize("rel", RUNTIME_ZONES)
@pytest.mark.parametrize("source", RUNTIME_MODULE_MEMBERS)
def test_the_runtime_may_use_a_modules_members(source: str, rel: str) -> None:
    assert rules(source, rel) == set()


KEYRING_ALLOWED = [
    "import keyring\nkeyring.get_password('lucille-host', 'pair')\n",
    "import keyring\nkeyring.set_password('lucille-host', 'pair', secret)\n",
    "import keyring\nkeyring.delete_password('lucille-host', 'pair')\n",
    "import keyring\nbackend = keyring.get_keyring()\n",
    "from keyring import errors\ntry:\n    pass\nexcept errors.PasswordDeleteError:\n    pass\n",
    "import keyring.errors\nraise keyring.errors.KeyringError('x')\n",
    "from keyring.backends import fail, null\nisinstance(backend, (fail.Keyring, null.Keyring))\n",
    "from keyring.backend import KeyringBackend\ndef f(b: KeyringBackend) -> None:\n    pass\n",
]


@pytest.mark.parametrize("rel", RUNTIME_ZONES)
@pytest.mark.parametrize("source", KEYRING_ALLOWED)
def test_the_runtime_may_use_keyrings_surface(source: str, rel: str) -> None:
    assert rules(source, rel) == set()


KEYRING_DENIED = [
    "from keyring.core import load_keyring\n",
    "import keyring.core\n",
    "from keyring.util.platform_ import platform\nplatform.processor()\n",
    "import keyring\nkeyring.backends.macOS.platform.processor()\n",
    "import keyring\nkeyring.set_keyring(backend)\n",
    "from keyring import errors\nhand_over(errors)\n",
    "from keyring.testing.util import string\n",
]


@pytest.mark.parametrize("rel", RUNTIME_ZONES)
@pytest.mark.parametrize("source", KEYRING_DENIED)
def test_the_runtime_uses_nothing_else_of_keyring(source: str, rel: str) -> None:
    assert "keyring-surface" in rules(source, rel)


def test_the_keyring_surface_is_pinned() -> None:
    assert ast_lint.KEYRING_SURFACE == {
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
        "keyring.backends.fail.Keyring",
        "keyring.backends.null.Keyring",
        "keyring.backends.chainer.ChainerBackend",
    }


@pytest.mark.parametrize("rel", [CONFORMANCE, TEST, TOOL])
def test_keyrings_surface_binds_the_runtime_only(rel: str) -> None:
    assert rules("from keyring.util import platform_\n", rel) == set()


@pytest.mark.parametrize("rel", [RUNTIME, EXEC, CONFORMANCE, SPAWN])
@pytest.mark.parametrize(
    "source",
    [
        "from json import *\n",
        "from ._switchboard import *\n",
        "from lucille_host.cli import *\n",
        "from keyring import *\n",
    ],
)
def test_no_star_import_in_the_package(source: str, rel: str) -> None:
    assert "star-import" in rules(source, rel)


@pytest.mark.parametrize("rel", [TEST, TOOL])
def test_star_import_of_an_unwatched_module_elsewhere(rel: str) -> None:
    assert rules("from json import *\n", rel) == set()


# --- What steers an import statement ----------------------------------------------------------

IMPORT_PATH_DENIED = [
    "__package__ = 'concurrent.futures'\nfrom . import process\n",
    "__spec__ = None\n",
    "__name__ = 'concurrent.futures.x'\n",
    "def f():\n    global __name__\n    __name__ = 'x'\n",
    "del __loader__\n",
    "import lucille_host\nlucille_host.__path__.append(directory)\n",
    "print(__package__)\n",
    "print(__path__)\n",
    "type(__loader__)('platform', path).load_module()\n",
    "import lucille_host\nsetattr(lucille_host, '__path__', [directory])\n",
    "import lucille_host\ngetattr(lucille_host, '__package__')\n",
    "import lucille_host\nsetattr(lucille_host, '__name__', 'x')\n",
    "import lucille_host\nlucille_host.__name__ = 'x'\n",
    "from lucille_host import cli as __package__\n",
    "from lucille_host import __path__\n",
]


@pytest.mark.parametrize("rel", [RUNTIME, EXEC, CONFORMANCE, SPAWN])
@pytest.mark.parametrize("source", IMPORT_PATH_DENIED)
def test_the_package_does_not_steer_its_imports(source: str, rel: str) -> None:
    assert "import-path" in rules(source, rel)


@pytest.mark.parametrize("rel", [TEST, TOOL])
@pytest.mark.parametrize("source", IMPORT_PATH_DENIED)
def test_tests_and_tools_may_read_their_import_state(source: str, rel: str) -> None:
    assert "import-path" not in rules(source, rel)


@pytest.mark.parametrize("rel", [RUNTIME, EXEC, CONFORMANCE, SPAWN])
@pytest.mark.parametrize(
    "source",
    [
        'if __name__ == "__main__":\n    pass\n',
        "def describe(value):\n    return type(value).__name__\n",
        "def describe(value):\n    return getattr(value, '__name__', None)\n",
    ],
)
def test_the_package_may_read_a_name(source: str, rel: str) -> None:
    assert rules(source, rel) == set()


# --- _exec.py: what starts a process is only called directly ----------------------------------

EXEC_SPAWNER_VALUES = [
    "import os\nspawn = os.posix_spawn\n",
    "import os\nhand_over(os.fork)\n",
    "from os import execv\nrun = execv\n",
    "import os\nSPAWNERS = (os.spawnv,)\n",
]


@pytest.mark.parametrize("source", EXEC_SPAWNER_VALUES)
def test_exec_module_only_calls_what_starts_a_process(source: str) -> None:
    assert "launcher" in rules(source, EXEC)


def test_exec_module_may_call_os_spawn_functions_directly() -> None:
    assert rules("import os\nos.posix_spawn('/bin/true', ['true'], {})\n", EXEC) == set()


# --- Symbolic links ---------------------------------------------------------------------------


def _plant_links(root: Path) -> None:
    package = root / "src" / "lucille_host"
    package.mkdir(parents=True)
    (package / "cli.py").write_text("import json\n")
    fixture = root / "tests" / "fixture_pkg"
    fixture.mkdir(parents=True)
    (fixture / "__init__.py").write_text(
        "import subprocess\nsubprocess.run(['true'], check=False)\n"
    )
    (package / "helpers").symlink_to("../../tests/fixture_pkg")
    (package / "util.py").symlink_to("../../tests/fixture_pkg/__init__.py")
    (root / "tools").mkdir()
    (root / "tools" / "linked").symlink_to("../tests")


LINKS = {"src/lucille_host/helpers", "src/lucille_host/util.py", "tools/linked"}


def test_a_symbolic_link_is_reported(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    _plant_links(tmp_path)
    found = {v.path for v in ast_lint.check(tmp_path) if v.rule == "symlink"}
    assert found == LINKS  # untracked and not ignored
    _git(tmp_path, "add", "-A")
    found = {v.path for v in ast_lint.check(tmp_path) if v.rule == "symlink"}
    assert found == LINKS  # tracked


def test_outside_git_a_symbolic_link_is_reported(tmp_path: Path) -> None:
    _plant_links(tmp_path)
    assert {v.path for v in ast_lint.check(tmp_path) if v.rule == "symlink"} == LINKS


# --- The tables, pinned ----------------------------------------------------------------------
# Each table is written out here, so that dropping an entry from the lint fails a test.

PINNED_WATCHED = {
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
}
PINNED_ROOTED_WATCHED = {"code", "profile", "trace", "inspect", "types", "typing", "gc", "pipes"}


def test_the_watched_modules_are_pinned() -> None:
    assert ast_lint.WATCHED == {name: name for name in PINNED_WATCHED}
    assert ast_lint.ROOTED_WATCHED == {name: name for name in PINNED_ROOTED_WATCHED}
    assert ast_lint.ROOTED_ONLY == PINNED_ROOTED_WATCHED | {"site"}


@pytest.mark.parametrize("module", sorted(PINNED_WATCHED | PINNED_ROOTED_WATCHED))
def test_each_watched_module_is_refused_as_a_value(module: str) -> None:
    assert "module-object" in rules(f"import {module}\nhand_over({module})\n", TOOL)


@pytest.mark.parametrize("module", sorted(PINNED_WATCHED | PINNED_ROOTED_WATCHED))
def test_each_watched_module_is_followed_through_a_re_export(module: str) -> None:
    source = f"from helpers import {module} as x\nx.anything\n"
    assert f"{module}.anything" in resolved(source)
    assert "module-object" in rules(f"from helpers import {module} as x\nhand_over(x)\n", TOOL)


@pytest.mark.parametrize("module", sorted(PINNED_WATCHED))
def test_each_watched_module_is_followed_as_an_attribute(module: str) -> None:
    assert f"{module}.anything" in resolved(f"config.{module}.anything\n")


@pytest.mark.parametrize("module", sorted(PINNED_ROOTED_WATCHED))
def test_a_rooted_module_is_not_followed_as_an_attribute(module: str) -> None:
    assert f"{module}.anything" not in resolved(f"config.{module}.anything\n")


PINNED_MEMBERS_BY_STRING = {
    "system": "shell",
    "popen": "shell",
    "getoutput": "shell",
    "getstatusoutput": "shell",
    "create_subprocess_shell": "shell",
    "subprocess_shell": "shell",
    "eval": "eval-exec",
    "exec": "eval-exec",
    "compile": "eval-exec",
    "get_objects": "eval-exec",
    "get_referrers": "eval-exec",
    "get_referents": "eval-exec",
    "get_type_hints": "eval-exec",
    "remote_exec": "eval-exec",
    "__import__": "dynamic-import",
    "import_module": "dynamic-import",
    "find_spec": "dynamic-import",
    "spec_from_file_location": "dynamic-import",
    "module_from_spec": "dynamic-import",
    "spec_from_loader": "dynamic-import",
    "resolve_name": "dynamic-import",
    "breakpoint": "dynamic-import",
    "breakpointhook": "dynamic-import",
}


def test_the_members_named_by_string_are_pinned() -> None:
    table = {name: rule for name, (rule, _reason) in ast_lint.MEMBERS_BY_STRING.items()}
    assert table == PINNED_MEMBERS_BY_STRING


# A member that is also a banned attribute is reported under the attribute's rule.
REPORTED_AS_ATTRIBUTE = {"__import__": "dunder"}


@pytest.mark.parametrize(("member", "rule"), sorted(PINNED_MEMBERS_BY_STRING.items()))
def test_each_member_named_by_string_is_reported(member: str, rule: str) -> None:
    rule = REPORTED_AS_ATTRIBUTE.get(member, rule)
    assert rule in rules(f"getattr(value, {member!r})\n", TOOL)
    assert rule in rules(f"import operator\noperator.methodcaller({member!r})(value)\n", TOOL)


PINNED_SUBPROCESS_LAUNCHERS = {
    "subprocess.run",
    "subprocess.Popen",
    "subprocess.call",
    "subprocess.check_call",
    "subprocess.check_output",
}
PINNED_LAUNCHERS = PINNED_SUBPROCESS_LAUNCHERS | {"asyncio.create_subprocess_exec"}


def test_the_launchers_are_pinned() -> None:
    assert ast_lint.SUBPROCESS_LAUNCHERS == PINNED_SUBPROCESS_LAUNCHERS
    assert ast_lint.LAUNCHERS == PINNED_LAUNCHERS
    assert ast_lint.LOOP_LAUNCHER == "subprocess_exec"
    assert ast_lint.SPAWN_ATTRIBUTES == {"subprocess_exec", "subprocess_shell"}


@pytest.mark.parametrize("rel", EVERY_ZONE)
@pytest.mark.parametrize("launcher", sorted(PINNED_LAUNCHERS))
def test_each_launcher_may_only_be_called_directly(launcher: str, rel: str) -> None:
    module = launcher.rsplit(".", 1)[0]
    assert "launcher" in rules(f"import {module}\nlaunch = {launcher}\n", rel)


@pytest.mark.parametrize("launcher", sorted(PINNED_SUBPROCESS_LAUNCHERS))
def test_each_subprocess_launcher_takes_argv_alone(launcher: str) -> None:
    source = f"import subprocess\n{launcher}(['ls'], -1, '/bin/sh')\n"
    assert "launcher" in rules(source, TOOL)


PINNED_SPAWN_MODULES = (
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


def test_the_spawn_modules_are_pinned() -> None:
    assert ast_lint.SPAWN_MODULES == PINNED_SPAWN_MODULES


@pytest.mark.parametrize("module", PINNED_SPAWN_MODULES)
def test_each_spawn_module_is_reported_in_the_runtime(module: str) -> None:
    top, _, rest = module.partition(".")
    source = f"import {top}\n{module}\n" if rest else f"import {module}\n"
    assert "spawn-outside-exec" in rules(source, RUNTIME)


# Every name banned everywhere, with its rule.
PINNED_BANNED_EVERYWHERE = {
    "os.system": "shell",
    "os.popen": "shell",
    "posix.system": "shell",
    "posix.popen": "shell",
    "subprocess.getoutput": "shell",
    "subprocess.getstatusoutput": "shell",
    "asyncio.create_subprocess_shell": "shell",
    "pipes": "shell",
    "eval": "eval-exec",
    "exec": "eval-exec",
    "compile": "eval-exec",
    "builtins.eval": "eval-exec",
    "builtins.exec": "eval-exec",
    "builtins.compile": "eval-exec",
    "__" + "builtins__": "eval-exec",
    "globals": "eval-exec",
    "locals": "eval-exec",
    "builtins.globals": "eval-exec",
    "builtins.locals": "eval-exec",
    "sys._getframe": "eval-exec",
    "sys._current_frames": "eval-exec",
    "sys.remote_exec": "eval-exec",
    "sys.settrace": "eval-exec",
    "sys.setprofile": "eval-exec",
    "sys.monitoring": "eval-exec",
    "threading.settrace": "eval-exec",
    "threading.setprofile": "eval-exec",
    "threading.settrace_all_threads": "eval-exec",
    "threading.setprofile_all_threads": "eval-exec",
    "logging.currentframe": "eval-exec",
    "gc": "eval-exec",
    "inspect.currentframe": "eval-exec",
    "inspect.stack": "eval-exec",
    "inspect.trace": "eval-exec",
    "inspect.getouterframes": "eval-exec",
    "inspect.getinnerframes": "eval-exec",
    "types.CodeType": "eval-exec",
    "typing.get_type_hints": "eval-exec",
    "typing.ForwardRef": "eval-exec",
    "typing.evaluate_forward_ref": "eval-exec",
    "typing._eval_type": "eval-exec",
    "code": "eval-exec",
    "codeop": "eval-exec",
    "timeit": "eval-exec",
    "pdb": "eval-exec",
    "bdb": "eval-exec",
    "profile": "eval-exec",
    "cProfile": "eval-exec",
    "trace": "eval-exec",
    "doctest": "eval-exec",
    "pickle": "eval-exec",
    "_pickle": "eval-exec",
    "marshal": "eval-exec",
    "shelve": "eval-exec",
    "_interpreters": "eval-exec",
    "_xxsubinterpreters": "eval-exec",
    "concurrent.interpreters": "eval-exec",
    "distutils": "eval-exec",
    "__import__": "dynamic-import",
    "builtins.__import__": "dynamic-import",
    "runpy": "dynamic-import",
    "imp": "dynamic-import",
    "pkgutil": "dynamic-import",
    "zipimport": "dynamic-import",
    "pydoc": "dynamic-import",
    "pkg_resources": "dynamic-import",
    "logging.config": "dynamic-import",
    "help": "dynamic-import",
    "builtins.help": "dynamic-import",
    "breakpoint": "dynamic-import",
    "builtins.breakpoint": "dynamic-import",
    "sys.breakpointhook": "dynamic-import",
    "sys.__breakpointhook__": "dynamic-import",
    "keyring.core.load_keyring": "dynamic-import",
    "keyring.core._load_keyring_class": "dynamic-import",
    "ctypes": "native-code",
    "_ctypes": "native-code",
    "cffi": "native-code",
    "_cffi_backend": "native-code",
    "win32ctypes": "native-code",
}
PINNED_BANNED_IN_PACKAGE = {
    "sys.modules": "dynamic-import",
    "sys.path": "import-path",
    "sys.meta_path": "import-path",
    "sys.path_hooks": "import-path",
    "sys.path_importer_cache": "import-path",
    "site": "import-path",
}


def test_the_banned_names_are_pinned() -> None:
    everywhere = {name: rule for name, rule, _reason in ast_lint.BANNED_EVERYWHERE}
    assert everywhere == PINNED_BANNED_EVERYWHERE
    assert len(ast_lint.BANNED_EVERYWHERE) == len(PINNED_BANNED_EVERYWHERE)
    in_package = {name: rule for name, rule, _reason in ast_lint.BANNED_IN_PACKAGE}
    assert in_package == PINNED_BANNED_IN_PACKAGE


# The banned names that are builtins, used without an import. (This file may not name the
# builtins namespace, so it is built from two literals here as in the lint.)
BANNED_BUILTINS = {
    "eval",
    "exec",
    "compile",
    "globals",
    "locals",
    "__import__",
    "help",
    "breakpoint",
    "__" + "builtins__",
}


@pytest.mark.parametrize(("name", "rule"), sorted(PINNED_BANNED_EVERYWHERE.items()))
def test_each_banned_name_is_reported(name: str, rule: str) -> None:
    top = name.split(".")[0]
    source = f"{name}\n" if top in BANNED_BUILTINS else f"import {top}\n{name}\n"
    assert rule in rules(source, TOOL)


# --- The real repository and the script ------------------------------------------------------


def test_repository_is_clean() -> None:
    files = ast_lint.python_files(REPO_ROOT)
    assert "src/lucille_host/_exec.py" in files
    assert "tools/guards/ast_lint.py" in files
    assert ast_lint.check(REPO_ROOT) == []


def _run_script(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, check=False
    )


def test_script_exit_codes(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert _run_script(str(empty)).returncode == 2
    assert _run_script(str(empty), "extra").returncode == 2

    module = tmp_path / "tree" / "src" / "lucille_host" / "helper.py"
    module.parent.mkdir(parents=True)
    module.write_text("import json\n")
    assert _run_script(str(tmp_path / "tree")).returncode == 0

    module.write_text("import json\nimport subprocess\n")
    result = _run_script(str(tmp_path / "tree"))
    assert result.returncode == 1
    assert "src/lucille_host/helper.py:2: [spawn-outside-exec]" in result.stdout
