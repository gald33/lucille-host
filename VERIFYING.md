# Verifying lucille-host (DRAFT)

**DRAFT.** No release exists yet. Step 1 works on this repository today. Steps 2 to 4 need a release and
cannot be done yet. This file is finished with the first release. Then someone who did not build that
release follows it once, on a clean machine, and reports each step they could not complete.

## 1. Read the source, and what CI enforces on it

**What to read.**
- `src/lucille_host/ops/` is the complete list of operations Lucille can ask the host to run. In
  `0.1.0.dev0` it holds `host.describe`, `host.sleep` and `files.sha256`; `lucille-host ops list` prints
  them. `src/lucille_host/_exec.py` is the one place a process starts.
- [CLAIMS.md](CLAIMS.md) maps each sentence of the privacy claim to the code and test behind it.
- [decisions/0001-host-design.md](decisions/0001-host-design.md) is the design.
- [spec/PROTOCOL.md](spec/PROTOCOL.md) is the protocol between Lucille and the host, `lucille.exec/1`:
  every message Lucille can send, every reply the host can give, and the rules each side follows. Its
  JSON Schemas and golden fixtures are in `src/lucille_host/spec/v1/`, and ship in the wheel.

**What CI runs.** The workflow `ci` (`.github/workflows/ci.yml`) runs on every push to `main` and on every
pull request.
- Job `test`, on GitHub-hosted `ubuntu-latest` and `macos-latest`, with Python 3.11 and 3.13: it runs
  `uv sync --locked --extra conformance`, then `pytest`. `--locked` fails if `uv.lock` does not match
  `pyproject.toml`, so the exact pins you read are the pins that ran.
- Job `guards`, on `ubuntu-latest`: it runs the four guards below on the whole repository, then
  `ruff check` and `ruff format --check`.
- The workflow's token can only read the repository (`permissions: contents: read`), and the checkout
  keeps no credentials (`persist-credentials: false`).

The workflow `macos-app` (`.github/workflows/macos-app.yml`) runs on pull requests that change `macos/`,
`src/`, `pyproject.toml`, `uv.lock` or the workflow itself, on pushes to the branch `macos-app`, and by
hand.
- Job `app`, on GitHub-hosted `macos-15` (Apple silicon): it builds the Mac app and its disk image,
  signed ad hoc, then runs the checks [macos/README.md](macos/README.md#what-ci-checks) lists, then the
  host's test suite on the app's trimmed Python runtime.
- Its token can only read the repository, its checkout keeps no credentials, and it uses no secret. It
  uploads the disk image only when every check passed. Its own copy skips the upload for a pull request
  from a fork, but such a pull request runs the fork's copy of the workflow, which can upload anything:
  an artifact on that run is the fork's build, whatever its name. The artifact's name says which event
  and which commit built it, and the image's name ends `-adhoc-macOS-arm64.dmg`: it is not a release.
  Whether a fork's pull request runs workflows before a maintainer approves it is a repository setting
  ("Approval for running fork pull request workflows from contributors"), which no file here can show.

**The four guards.** What the guards are for: they catch accidental and casual violations of the rules
below, in the forms they list. They are not a sandbox, and they are not a defence against an author who
sets out to get past them. A maintainer reviews every pull request before it merges, and that review is
the backstop. Each guard's section below says what it cannot see.

Each one runs as `python tools/guards/<name>.py [ROOT]`. It exits 0 when the tree is clean, 1 on a
violation and 2 on a usage error. A guard that finds no file to check exits 2 rather than report a clean
tree. Each has a test under `tests/guards/` that plants a violation and checks that the guard reports it,
and a test that this repository is clean (for the private-string check, only the CI step with the secret
can show the repository is clean; its test uses a synthetic list). The summary below is short. Each
guard's module docstring states its rules in more detail, and the lists in its code are what it
enforces.

1. **Workflow lint** (`tools/guards/workflow_lint.py`, tested by `tests/guards/test_workflow_lint.py`).
   In every workflow under `.github/workflows/`, and every `action.yml` in the repository, it fails on:
   - `self-hosted` anywhere, in any case;
   - a `runs-on` that is not exactly one GitHub-hosted image label from the lint's own list
     (`ubuntu-latest`, `macos-latest`, `windows-2025` and the like), written literally or as a matrix
     value whose values, `include` entries among them, are all such labels. A runner group, another
     expression, a custom label (even one shaped like `ubuntu-mybox`) or a list of several labels is
     refused, because each could select a self-hosted runner without saying so;
   - a step's `uses:` that is not `<owner>/<repo>[/<path>]@<sha>`, with `<sha>` a full 40-character
     lower-case hexadecimal commit SHA. Anything else a step uses is refused, a local action (`./...`)
     and a Docker image (`docker://...`, even one pinned by digest) included; no workflow here uses
     either. A job's `container` and its `services` must be pinned by `sha256` digest. An `action.yml`
     in the repository is still checked, by the same rules: its steps are pinned the same way, a Docker
     action's image must be pinned by `sha256` digest, and a `Dockerfile` is refused;
   - a job that calls a reusable workflow from outside this repository, which would choose its own
     runner.

   It cannot see what a pinned action does when it runs, a JavaScript action's dependencies, or
   repository settings such as which runners are registered.
2. **AST lint** (`tools/guards/ast_lint.py`, tested by `tests/guards/test_ast_lint.py`). It parses every
   Python file in the repository (source, tests and tools) and fails:
   - anywhere, on:
     - a `shell=` argument other than the literal `False`, and any API that hands a command line to a
       shell (`os.system`, `os.popen`, `subprocess.getoutput`, `subprocess.getstatusoutput`,
       `asyncio.create_subprocess_shell` under either of its names, an event loop's
       `subprocess_shell`, and the module `pipes`);
     - a process launcher that is not called directly, or is called with more than argv as a
       positional argument or with `**` keywords (either could pass `shell` or `executable`);
     - `eval`, `exec`, `compile`, and `__builtins__` (as a name, or as the string `'__builtins__'`
       anywhere); `globals`, `locals`, `vars` with no argument or used as a value, and the frame
       functions of `sys` and `inspect`; `gc`; `types.CodeType`; the parts of `typing` that evaluate
       strings (`get_type_hints`, `ForwardRef` and the rest of its list); `sys.remote_exec`; the hooks
       that hand frames or code objects to a callback (`sys.settrace`, `sys.setprofile`,
       `sys.monitoring`, their `threading` twins, `logging.currentframe`); and the modules that run
       strings or stored data as code (`code`, `timeit`, `pdb`, `pickle`, `marshal`, the
       subinterpreter modules, `distutils` and the rest of its list);
     - a dynamic import: `__import__`, every part of `importlib` but the few it lists (not
       `importlib.resources.files`, which imports the module its argument names), `runpy`, `imp`,
       `pkgutil`, `zipimport`, `pydoc`, `pkg_resources`, `logging.config`, `help` by name, as a value
       or through `builtins`,
       `breakpoint`, `sys.breakpointhook`, `keyring.core.load_keyring`, and an `entry_points`
       attribute on any object;
     - `ctypes`, `cffi` and `win32ctypes`, which call native code;
     - a watched module used as a value (`getattr(os, name)`, `vars(os)`);
     - attributes such as `__dict__`, `__globals__`, `__self__`, a frame's `f_globals` and a
       generator's `gi_code` that lead from an object to modules or code (the dunders among them as a
       bare name too, such as a module's own `__loader__`; an attribute a `match` class pattern
       reads, `case C(__globals__=g)`, or a `__match_args__` entry names, counts as an attribute);
       and those names, or a member on the lint's `MEMBERS_BY_STRING` list (`"system"`, `"exec"`,
       `"import_module"` and the rest), spelled as a string literal to `getattr` and its relatives;
     - a `*` import from a module it watches;
     - a Python file that does not parse, and a name it cannot finish resolving (below);
     - a symbolic link anywhere in the repository that git tracks, or that is untracked and not
       ignored. Python and the wheel build follow a link and the lint does not, so code behind a link
       would be checked under the rules of where it really is (a linked directory in
       `src/lucille_host/` would make test code a runtime module), or not at all;
     - a compiled file (`.pyc`, `.pyo`, `.so`, `.pyd` or `.dylib`, in any case) anywhere in the
       repository that git tracks, or that is untracked and not ignored (outside a git work tree,
       every one it finds, skipping `__pycache__` and the other build caches). The lint reads only
       `.py` source, and Python loads an extension module ahead of the `.py` file of the same name,
       and a lone `.pyc` file where there is no `.py` file;
   - in `src/lucille_host/`:
     - on `sys.modules`, and on `sys.path`, `sys.meta_path`, `sys.path_hooks`,
       `sys.path_importer_cache` and `site`, which change what an `import` statement loads;
     - on `__path__`, `__package__`, `__spec__` and `__loader__`, as a name or an attribute, read or
       written, and on anything that binds or sets those or `__name__` (`__name__` may be read):
       a package's `__path__` decides where its submodules are found, and a relative import is
       resolved through the module's `__package__`, `__spec__` and `__name__`;
     - on any `*` import;
   - in the runtime package:
     - on an import of any top-level module not on the lint's allowlist, `RUNTIME_IMPORTS`:
       `__future__`, `argparse`, `base64`, `binascii`, `collections`, `contextlib`, `copy`,
       `dataclasses`, `datetime`, `enum`, `errno`, `functools`, `hashlib`, `hmac`, `io`, `itertools`,
       `json`, `logging`, `math`, `os`, `pathlib`, `re`, `secrets`, `shutil`, `signal`, `stat`, `sys`,
       `tempfile`, `textwrap`, `threading`, `time`, `tomllib`, `types`, `typing`, `keyring`,
       `switchboard` and `lucille_host` itself. `src/lucille_host/_exec.py` may also import
       `subprocess`. An import anywhere in a file counts. A submodule comes with its module
       (`os.path`, `logging.handlers`), and what the rules above refuse stays refused inside an
       allowed module (`os.system`, `logging.config`). A module an allowed module re-exports is
       checked as itself, by the same list: `from logging.handlers import socket` imports `socket`,
       and `enum.bltns` is `builtins`. The runtime may not import or use `lucille_host.conformance`,
       which is not held to the list. `platform`, `uuid`, `string` and `socket` are left off on
       purpose. The code gives the reason for each entry and for each of those four, and widening the
       list is a reviewed change;
     - on any part of `keyring` outside the lint's `KEYRING_SURFACE`: getting, setting and deleting
       a password, the backend in use, the errors, and the backend classes the host must recognise;
     - on a module used as a value, any module, this package's own included (`getattr(json, name)`);
     - on anything that can start a process (`subprocess`, `os.spawn*`, `os.exec*`, `os.fork*`,
       `multiprocessing`, `webbrowser`, `pipes` and the rest of its list) outside
       `src/lucille_host/_exec.py`, that exact path, including a launcher that module re-exports under
       another name; and, inside it, on anything that starts a process and is not called directly.

   **How it resolves a name.** Through the file's imports (`import os as o; o.system` is
   `os.system`), and then through the re-exports it can see, at any place in a chain: a table of the
   modules the runtime's allowed standard-library modules bind (`tempfile._os` is `os`), which
   `tools/guards/reexport_probe.py` writes and `tests/guards/test_reexports.py` checks on every
   interpreter CI tests; what this package's own modules bind by an import or by assigning an
   imported name (`paths.osmod` is `os` when `paths` has `import os as osmod`); and a watched
   module's name, or that name with leading underscores (`random._os`). It reads a file's imports
   without regard to scope, so a name the imports bind to two different things (`import os as x`
   in one place, `import json as x` in another) is a finding: a use could mean either. So is a name
   that stands for more names than it follows: it fails closed.

   **What it is, and what it cannot see.** The rules that apply everywhere are a denylist: they name
   the known forms of a shell, of code run from a string and of a dynamic import, and a form they do
   not name passes. A denylist is not a sandbox, and its lists grow as more forms are found. The
   runtime package is held to the import allowlist as well. The allowlist binds `import` statements as
   the lint resolves them, and the modules a name reaches through the re-exports above; the
   `src/lucille_host/` rules above refuse the ways the lint knows to make Python resolve an import
   differently. The lint cannot keep an allowed module from doing what it does inside. It does not
   see:
   - values returned by calls (`f().system`), or a module, function or attribute reached through such
     a value or through an object that is not a module;
   - a module re-exported where it does not look: by a module that is neither one of this package's
     nor one of the runtime's allowed standard-library modules (one that tests, tools or the
     conformance package import), under a name that is not a watched name; or by one of this
     package's modules other than by an import or a plain assignment (in a tuple, as a class
     attribute, as a function's return value). The runtime package is the least exposed: its imports
     and what they re-export must be on the allowlist, and it may use no module as a value;
   - `getattr` passed as a value, or called with a name that is not a string literal;
   - `vars(<object>)` with an argument, which returns the object's `__dict__`;
   - format-string field lookups, such as `"{0.__globals__}".format(f)` (`str.format` needs no
     import; the runtime may not import `string`, whose `Formatter` hands back what it looks up,
     directly or through a module that re-exports it);
   - compiled modules installed outside this repository;
   - code outside this repository. The standard library and the dependencies can start processes,
     import by name or run strings on the code's behalf, and the lint does not trace calls into them.
     agent-switchboard 2.6.0's `switchboard/client.py`, which the runtime imports, imports
     `subprocess` and can run `git` to derive an agent id; keyring imports the backend that an
     environment variable, its configuration file or an installed entry point names, when the host
     first asks it for a password. Whether the host's own calls ever reach Switchboard's `git` path
     is not checked yet; the design's invariant "The host's stores and Switchboard footprint are its
     own" (no subprocess from pairing) is the pending test for it;
   - files it does not parse: configuration, data and shell scripts.

   Review covers the rest.

   "The runtime package" is `src/lucille_host/` without `src/lucille_host/conformance/`, which is
   excluded by name: from the import allowlist, from the process rule above, and from the single-import
   rule of the Switchboard lint. The conformance package has a rule of its own: only
   `src/lucille_host/conformance/_spawn.py`, which does not exist yet, may start a process, and only
   by calling a subprocess launcher with an argv list literal, or `asyncio.create_subprocess_exec` with
   literal arguments, whose first element is the local hub (`switchboard`, then `serve`) or
   the installed `lucille-host`, named literally or by a constant assigned once to
   `shutil.which(<name>)`, to `Path(sys.executable).parent / <name>` or to
   `Path(sysconfig.get_path("scripts")) / <name>`.
   `executable=`, `program=` and `**` keywords are refused, and so is `env=` when the program is a bare
   name. `_spawn.py` binds each name that stands for `pathlib`, `shutil`, `sys` or `sysconfig` exactly
   once, by its import; never assigns to or deletes an attribute or item of a name an import binds
   (`shutil.which = ...`, `sys.executable = ...`, `os.environ["PATH"] = ...`); and never calls
   `setattr` or `delattr`. A method call that changes a module's state (`os.environ.update(...)`) is
   not seen; review covers it. The package's other modules may use
   `subprocess` only for what starts nothing (pipes, results, errors), and a launcher `_spawn.py`
   re-exports is refused in them like any other. The conformance package is not held to the
   runtime's import allowlist, so a standard-library module that starts a process inside
   (`platform.architecture` runs `file`) is not refused there; review covers it.
3. **Switchboard-surface lint** (`tools/guards/switchboard_lint.py`, tested by
   `tests/guards/test_switchboard_lint.py`). It fails:
   - in the runtime package, on an import or use of `switchboard` outside
     `src/lucille_host/_switchboard.py`, as far as the lint resolves names (through the file's
     imports and through that module), including one that reaches it through that module
     (`from lucille_host._switchboard import client`, or `from lucille_host._switchboard import *`).
     A name taken from that module is presumed to be Switchboard unless the module shows it is its
     own: a function, class or constant it defines that uses no part of Switchboard in its
     decorators, bases or value;
   - everywhere, on any reference to a name `register`, called or not (an attribute a `match` class
     pattern reads, or a `__match_args__` entry names, included), and on the string `"register"`
     passed to `getattr` and its relatives or used as a `**` key;
   - everywhere, on any identifier, attribute, import, keyword argument, parameter, `except ... as`
     name or such string whose name contains `invite`, in any case (function and class names are not
     checked, so a test may be named for what it checks), and on any string that contains the prefix
     of a Switchboard invite code;
   - outside `src/lucille_host/conformance/` and `tests/`, on any use of `share_new`; on any member of
     `switchboard.sharing` other than `touch`, `accept`, `resolve_room` and `leave` and the types they
     take and return, however `sharing` is imported; and on any part of `switchboard` outside the
     surface the lint lists (so not the CLI, the MCP bridge, rendezvous, the hub server, or
     `pairing.start`, `heal` and `export`). Those calls, and the types, constants and errors that go
     with them, are what the host needs to pair, to keep its pair in a store of its own, to read the
     library's notes and to take a room Lucille offers. The conformance package and the tests are
     exempt from these rules, and only these, because their fake Lucille must mint and offer rooms the
     way Lucille does;
   - in the runtime package, on a private member (`_name`) of anything but `self`, `cls` or one of
     this package's own modules, and only directly on them (`self._data`, not `self.hub._call`),
     such as the client's `_call`, and on a private name imported from outside this package
     (`from tempfile import _os`).

   It checks names written in the source. It does not follow a method called on an object, such as a
   client or a share store: of those it refuses only `register`, invites and private members. Nor does
   it follow the members of a member (`pairing.PairingCode.decode`), a value a wrapper function
   returns, or a name built at run time and passed to `getattr`. Review covers the rest.
4. **Private-string check** (`tools/guards/private_strings.py`, tested by
   `tests/guards/test_private_strings.py`). It fails if a file in the repository contains a string from a
   private list: strings private to Lucille that must never appear in this public repository.
   - A committed list would publish the strings it protects. So the list lives in the repository secret
     `LUCILLE_HOST_PRIVATE_STRINGS`, and reaches the check only through the environment variable of the
     same name, on that one CI step.
   - The list is one case-sensitive literal substring per line. Blank lines and lines starting with `#`
     are ignored.
   - It reads what `git ls-files` lists: every path, a gitlink to another repository included, and
     the contents of every file, whatever bytes it holds. It reads each line as UTF-8 (a byte that is
     not UTF-8 is replaced, and a NUL byte does not stop it), and the raw bytes again for each entry
     in UTF-16, little- and big-endian; a UTF-16 hit names no line. A symbolic link is read as the
     path it holds, which is what git publishes for it; a gitlink by its path only, since its
     contents are another repository's. Outside a git checkout it walks the tree, skipping `.git`,
     `.venv`, `__pycache__` and other build caches.
   - On a hit it prints the file, the line number, and the entry's line number in the list. It never
     prints the entry or the line, because the CI log of a public repository is public. When a file's
     own path matches, the path is withheld too.
   - On a pull request from a fork, GitHub passes no secrets. CI sets `IS_FORK_PR=true` there, and the
     check says it is skipped, adds a warning to the pull request's checks page, and passes; a
     maintainer reviews every fork pull request. Anywhere else, a missing or empty list fails the check.
   - It reads files, not commit messages, authors, branch names or pull request text, which are
     public too.
   - GitHub gives no Actions secrets to a pull request Dependabot opens, and such a pull request is not
     from a fork. So the check fails there, closed, unless the same list is also stored as a
     Dependabot secret.
   - So you cannot run this check yourself without the list. You can read that the code does what this
     section says.

**One more test that matters here.** `tests/test_import_graph.py` imports every runtime module in a fresh
Python process, with the `[conformance]` extra installed so that `fastapi` and `uvicorn` could be
imported, and fails if either was. It also checks that its list of modules covers every module file in
the runtime package, and that the package holds no symbolic link, which its walk would not follow.
The hub server lives only in the `[conformance]` extra.

**The spec's tests.** `tests/spec/` tests the protocol spec, not the host, which does not check messages
against it yet.
- `test_schemas.py` walks every subschema of every schema, and fails on an object without
  `additionalProperties: false`, a value without a bound, or a pattern that reads differently in
  ECMA-262 (the regular expressions JSON Schema uses) and in Python.
- `test_fixtures.py` checks that each golden fixture validates, or fails, as labelled, both against the
  schema and against the rules the spec states that a schema cannot express, and that each request
  fixture expects the reply the spec's refusal order gives.
- `test_packaged.py` builds the sdist and the wheel with `uv build`, installs the wheel into a fresh
  virtual environment, loads the schemas and fixtures from there, and checks them against the source
  tree. It needs `uv` on `PATH` and the build backend, which uv fetches or takes from its cache. It
  builds from the working tree, so every file there that `.gitignore` does not exclude goes into the
  sdist: keep any other virtual environment outside the checkout, or name it `.venv*` or `venv`.

**Run it yourself.**

```sh
git clone https://github.com/gald33/lucille-host
cd lucille-host
uv sync --locked --extra conformance
uv run pytest
uv run python tools/guards/workflow_lint.py .
uv run python tools/guards/ast_lint.py .
uv run python tools/guards/switchboard_lint.py .
```

## 2. Check provenance (needs a release)

Every file of a release on PyPI must carry a PEP 740 attestation:
- its publisher names repository `gald33/lucille-host`, workflow `release.yml` and environment `pypi`;
- its signing certificate's Runner Environment extension reads `github-hosted`.

The `[verify]` extra installs `pypi-attestations`, and a later release adds `lucille-host verify` to make
this check. Until then it is a manual check. The GitHub Release also carries `SHA256SUMS`.

The same check works for the pinned `agent-switchboard` release. Its publisher is `gald33/switchboard`,
workflow `publish.yml`, environment `pypi`, ref `refs/tags/v2.6.0`. That certificate was decoded on
2026-10-19 but not verified cryptographically. Check it yourself.

A release is called "reproducible" only when a rebuild from the tagged source produced the same sha256s.
No rebuild job exists yet.

## 3. Run the conformance suite (needs a release)

Install the release with its `[conformance]` extra into a fresh virtual environment, then run the suite:

```sh
python3 -m venv verify-env
verify-env/bin/pip install 'lucille-host[conformance]==<version>'
verify-env/bin/python -m lucille_host.conformance
```

It runs offline, against a local Switchboard hub it starts on 127.0.0.1. It plays a hostile Lucille and
attempts each widening listed under "Host invariants and their tests" in the design record. In
`0.1.0.dev0` it has no scenarios. It says so and exits 1, so it cannot be mistaken for a pass.

Run it with the virtual environment's own `python`, as above. The conformance lint lets the suite name
the programs it starts by their path beside that interpreter, so that it tests the `lucille-host`
installed with it, not one that happens to be first on your `PATH`.

## 4. Watch (needs a release)

`lucille-host log` and `lucille-host status` show every request received, every local decision, and a
fingerprint of everything sent.

## What verification cannot establish

- Anything about what Lucille's backend does with a reply.
- Whether installed files are still the verified ones after any process running as your user has touched
  them.
- The provenance of dependencies beyond the ones checked.
- Tag protection and the `pypi` environment's required reviewer. They are repository settings, which a
  reader cannot see.
