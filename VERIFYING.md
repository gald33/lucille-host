# Verifying lucille-host

Each step works on `0.1.0`, the first release. Nobody who did not build it has followed this file yet on a
clean machine; until someone has, and has reported each step they could not complete, treat it as
untested by a third party.

## 1. Read the source, and what CI enforces on it

**What to read.**
- `src/lucille_host/ops/` is the complete list of operations Lucille can ask the host to run. In
  `0.1.0` it holds `host.describe`, `host.sleep`, `files.sha256`, `files.search`, `github.read`,
  `gmail.check`, `gmail.mailboxes`, `gmail.search` and `gmail.read`; `lucille-host ops list` prints them. `src/lucille_host/_exec.py` is the one place a process starts.
- [CLAIMS.md](CLAIMS.md) maps each sentence of the privacy claim to the code and test behind it.
- [decisions/0001-host-design.md](decisions/0001-host-design.md) is the design.
- [spec/PROTOCOL.md](spec/PROTOCOL.md) is the protocol between Lucille and the host, `lucille.exec/1`:
  every message Lucille can send, every reply the host can give, and the rules each side follows. Its
  JSON Schemas and golden fixtures are in `src/lucille_host/spec/v1/`, and ship in the wheel.

**What CI runs.** The workflow `ci` (`.github/workflows/ci.yml`) runs on every push to `main`, on every
pull request, and when started by hand. It has no path filter.
- Job `test`, on GitHub-hosted `ubuntu-latest` and `macos-latest`, with Python 3.11.15, 3.13.14 and
  3.14.6, and on `ubuntu-latest` with 3.12.13 too (below): it runs `uv sync --locked --extra conformance`, then `pytest`. `--locked` fails if
  `uv.lock` does not match `pyproject.toml`, so the exact pins you read are the pins that ran.
- Job `guards`, on `ubuntu-latest`: it runs the five guards below on the whole repository, then
  `ruff check` and `ruff format --check`.
- Job `macos-app`, on GitHub-hosted `macos-15` (Apple silicon): it builds the Mac app, Capsule, and its
  disk image with the Xcode version `tools/macos/pins.toml` names, signed ad hoc, then runs the checks
  [macos/README.md](macos/README.md#what-ci-checks) lists, among them the host's whole test suite on a copy
  of the app's own Python runtime, and then, with that copy's interpreter, the end-to-end check of pairing
  and supervision, against a hub of its own and a stand-in for Lucille. It also builds the unsigned app a
  second time, in another folder, and writes to the job summary whether the two tarballs are the same.
  That is recorded, not a gate.
- Jobs `macos-smoke`, on `macos-26` and on `xcode-27` (GitHub's macOS 27 image): each takes the disk image
  `macos-app` built from the Actions cache, refuses it unless its sha256 is the one `macos-app` gave, and
  runs the checks that need no build on it. They build nothing, and do not run the pairing check, which
  needs the suite's copy of the runtime. Both fail the workflow run when they fail, like every other job.
  The `xcode-27` job did not from 2026-11-02 to 2026-11-18 (decisions/0002 §18).
- **Each Python the `test` job runs is pinned to one patch**: 3.11.15, 3.12.13, 3.13.14 and 3.14.6, one
  for each minor version `pyproject.toml`'s classifiers name, and a test holds the two lists to each
  other. 3.12 runs on `ubuntu-latest` only: macOS runners are short of capacity, and 3.11 and 3.13 there
  bracket it.
  - **Only uv's own builds:** the job sets `UV_PYTHON_PREFERENCE: only-managed`, so every run uses the
    Python that uv downloads, never one already on the runner's image. Given only a minor version, macOS
    had run its image's 3.11.9 and 3.13.17 and Homebrew's 3.14.10, and Ubuntu its system 3.14.4.
  - **Proof in the log:** the step "Show the interpreter, and check it is the pinned one" prints the
    version and build (`python -VV`), the real path of the interpreter the project's virtual environment
    links to, and its base prefix, the folder uv installed it in. It fails unless the version is the pin.
    The version alone cannot tell two builds of one patch apart; the path and uv's "Downloading" line
    in the sync step show whose build ran.
  - **The other jobs:** `guards`, `macos-app` and both smoke jobs run their tools on 3.13.14, the `test`
    job's 3.13 pin, with the same setting, so no job takes a Python from its image. The uv commands the
    build scripts run get no `UV_*` variable, that setting among them, so `build_app.py` builds the host
    wheel with `uv build --python` and the interpreter running it, and prints that interpreter as "build
    interpreter". Where the setting is `only-managed`, as in CI, it refuses to build unless that
    interpreter is one of uv's own: in the folder `uv python dir` names, and named as uv names its install
    of that version (`cpython-<version>-<os>-<arch>-<libc>`), so a folder such as `/usr` named by
    `UV_PYTHON_INSTALL_DIR` does not pass the system's Python. The line says which folder, and whether that
    variable named it. Elsewhere the line says the interpreter was not checked. The app bundles a runtime
    of its own, which this does not touch.
  - **3.14** joined the matrix on 2026-11-20, after the advisory workflow `python-next`, now gone, passed
    on five pushes to `main` in a row (decisions/0002 §18).
  - **Who moves the pins, and when:** the maintainer moves each to the newest patch of its minor version
    that the pinned uv (`setup-uv`'s `version`) can download, in the pull request that bumps that uv
    version. A patch that fixes a security issue moves its pin on its own, in a pull request of its own.
  - **How to find them:** `uv python list --only-downloads --all-versions --all-platforms --all-arches`,
    with that uv, lists what it can download. Each patch must be there for both `linux-x86_64-gnu` and
    `macos-aarch64-none`.
- The Mac app ships a `checked-hash` `.pyc` beside each `.py` of its runtime. `check_app.py bytecode`, in
  `macos-app` and both smoke jobs, checks that each is byte for byte what the bundled interpreter compiles
  from that `.py`. It is meant to be run on a copy you downloaded too
  ([macos/README.md](macos/README.md#the-disk-image-from-ci)); that is not yet tried on a Mac.
- The workflow's token can only read the repository (`permissions: contents: read`), and the checkout
  keeps no credentials (`persist-credentials: false`). The only secret it uses is the private-string
  list, in the `guards` job; no Mac job uses a secret, and none signs with a certificate.
- Every run uploads the Mac jobs' logs and screenshots, kept 7 days. The ad-hoc disk image is uploaded
  only by a run started by hand whose every check passed, kept 3 days; any signed-in GitHub user can
  download it meanwhile. A pull request from a fork runs the fork's copy of the workflow, which can upload
  anything: an artifact on that run is the fork's build, whatever its name. The artifact's name says which
  event and which commit built it, and the image's name ends `-adhoc-macOS-arm64.dmg`: it is not a
  release. Whether a fork's pull request runs workflows before a maintainer approves it is a repository
  setting ("Approval for running fork pull request workflows from contributors"), which no file here can
  show.

**The five guards.** What the guards are for: they catch accidental and casual violations of the rules
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
       `__future__`, `argparse`, `base64`, `binascii`, `collections`, `configparser`, `contextlib`,
       `copy`, `dataclasses`, `datetime`, `enum`, `errno`, `functools`, `hashlib`, `hmac`, `io`,
       `itertools`, `json`, `logging`, `math`, `os`, `pathlib`, `re`, `secrets`, `shutil`, `signal`,
       `stat`, `sys`, `tempfile`, `termios`, `textwrap`, `threading`, `time`, `tomllib`, `types`,
       `typing`, `keyring`, `switchboard` and `lucille_host` itself. `src/lucille_host/_exec.py` may
       also import `subprocess`, and `src/lucille_host/verify.py`, the checks of `lucille-host
       verify`, `certifi`, `http`, `ssl`, `zipfile` and `pypi_attestations`. An import anywhere in
       a file counts. A submodule comes with its module (`os.path`, `logging.handlers`), and what
       the rules above refuse stays refused inside an allowed module (`os.system`,
       `logging.config`). A module an allowed module re-exports is
       checked as itself, by the same list: `from logging.handlers import socket` imports `socket`,
       and `enum.bltns` is `builtins`. The runtime may not import or use `lucille_host.conformance`,
       which is not held to the list. `platform`, `uuid`, `string` and `socket` are left off on
       purpose. The code gives the reason for each entry and for each of those four, and widening
       the list is a reviewed change;
     - on any part of `keyring` outside the lint's `KEYRING_SURFACE`: the errors, the two backend
       classes the host builds (keyring's Secret Service backend for Linux and its Keychain backend
       for macOS), and the backend classes the host must recognise. `keyring.get_keyring`, and
       keyring's own `get_password`, `set_password` and `delete_password`, are outside it: each asks
       keyring for the backend it chooses, and the host builds its own and reads and writes through
       it;
     - on a module used as a value, any module, this package's own included (`getattr(json, name)`);
     - on anything that can start a process (`subprocess`, `os.spawn*`, `os.exec*`, `os.fork*`,
       `multiprocessing`, `webbrowser`, `pipes` and the rest of its list) outside
       `src/lucille_host/_exec.py`, that exact path, including a launcher that module re-exports under
       another name; and, inside it, on anything that starts a process and is not called directly.

   **How it resolves a name.** Through the file's imports (`import os as o; o.system` is
   `os.system`), and then through the re-exports it can see, at any place in a chain: a table of the
   modules the runtime's allowed standard-library modules bind (`tempfile._os` is `os`), which
   `tools/guards/reexport_probe.py` writes and `tests/guards/test_reexports.py` checks on every
   interpreter CI tests (when it fails, it prints one line with everything the table lacks, which
   `reexport_probe.py --add <log>` adds, for an interpreter not at hand); what this package's own modules bind by an import or by assigning an
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
     import by name or run strings on the code's behalf, and the lint does not trace calls into
     them. agent-switchboard 2.6.0's `switchboard/client.py`, which the runtime imports, imports
     `subprocess` and can run `git` to derive an agent id; the host imports keyring's backend module
     for its own system alone and builds that backend itself, so keyring consults neither
     `PYTHON_KEYRING_BACKEND` nor its configuration file, `keyringrc.cfg` (the host reads both as
     plain text, for keyring's off switch alone, and imports nothing they name), and loads none of
     the backends its installed entry points name; what that module does inside is not traced (on
     Linux it connects to the session bus through secretstorage and jeepney, and on macOS it loads
     the Security framework through `ctypes`). Tests with an audit hook find that the host's pairing
     and sharing calls, and `run`, start no process on the paths they take, against Switchboard's
     in-process hub ([CLAIMS.md](CLAIMS.md), C1), `run`'s events and lifeline included, with the
     tests' own keyring backend in memory; and that opening the keyring store as a real host opens
     it, in a process of its own with nothing of the tests in it, starts none
     (`tests/test_keyring_backend.py`). A path no test takes is not checked;
   - files it does not parse: configuration, data and shell scripts.

   Review covers the rest.

   "The runtime package" is `src/lucille_host/` without `src/lucille_host/conformance/`, which is
   excluded by name: from the import allowlist, from the process rule above, and from the single-import
   rule of the Switchboard lint. The conformance package has a rule of its own: only
   `src/lucille_host/conformance/_spawn.py` may start a process, and only
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
   - A committed list would publish the strings it protects. So the list lives in a repository secret,
     and reaches the check only through the environment variable of the same name, on that one CI step:
     `LUCILLE_HOST_PRIVATE_STRINGS_GZ`, the list compressed with gzip and then written in base64, so that
     a list longer than a secret can hold (48 KB) still fits; else `LUCILLE_HOST_PRIVATE_STRINGS`, the
     list as it is. When the first holds anything, the second is not read. A compressed value that cannot
     be read (not base64, not one whole gzip stream, over 8 MiB unpacked, or not UTF-8) fails the check,
     and the check's last line names the variable the list came from.
   - To set the compressed secret from a plain list file (one substring per line), with the GitHub CLI:

     ```sh
     gzip -9c list.txt | base64 | tr -d '\n' | gh secret set LUCILLE_HOST_PRIVATE_STRINGS_GZ --repo gald33/lucille-host
     ```

     The value is the output of `gzip -9c list.txt | base64 | tr -d '\n'`, wherever it is set; the name
     is `LUCILLE_HOST_PRIVATE_STRINGS_GZ`, and does not go into the value. To check a value against the
     file without printing either:
     `printf %s "$LUCILLE_HOST_PRIVATE_STRINGS_GZ" | base64 -d | gunzip | cmp -s - list.txt && echo same || echo differs`.
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
5. **Mac lint** (`tools/guards/macos_lint.py`, tested by `tests/guards/test_macos_lint.py`). It reads
   every Swift file, the Python files and pins under `tools/macos/`, any plist, entitlements or strings
   file, and every workflow, and fails:
   - in Swift, on the names [decisions/0002](decisions/0002-macos-app.md) §20 lists and on other
     ways to start a process, load code, use the network, listen (a Mach or distributed-objects port
     included), read or make a URL that is not a file, open a URL, open, end, find, register, drive or
     hand something to another application, or ask macOS for more rights: `URLSession`, `NW*`,
     `WebKit`, XPC, sockets, streams, `LSOpen*`, `LSSetDefault*`, `LSRegister*`, `AESend*`,
     `NSAppleEventDescriptor`, `openURL`, `Link(`, a Markdown link, `NSSharingService`,
     `forceTerminate`, `requestAuthorization`, `NSUser*Task`, `fork`, `exec*`, `dlopen`, a bundle's
     `load()`, `performClick`, `_silgen_name` and the like, `/bin/sh` and the like, `NSWorkspace` and
     every `URL(string:` or `URL.init(string:` outside the one opener (below), and `UserDefaults` and
     `SWITCHBOARD_`. It reads the Swift three ways: the text with its comments, the code with its
     comments blanked (so `connect/**/(` is still a call), and each string literal decoded (so
     `https:\u{2F}/` is still a URL). One exception, in tests only: in a Swift file under a `Tests`
     folder, `#!/bin/sh` alone on the first line of a multi-line string literal, the script the
     self-test writes and starts through the app's child runner as a stand-in for the interpreter
     (decisions/0002 §20). Any other shell, and `/bin/sh` anywhere else, is still a failure there;
   - in Swift, on `Process` anywhere but `macos/App/HostCLI.swift`, and if that file does not write
     the bundled runtime's path, `Contents/Resources/runtime/bin/lucille-host-runtime`, and its
     arguments, `["-I", "-B", "-X", "utf8", "-m", "lucille_host"]`, outside its comments;
   - in Swift, on anything in the app's one opener, `macos/App/SystemPlaces.swift`, but one call,
     `NSWorkspace.shared.open(<a name>)`, of the URLs written there: the two System Settings pages in
     the lint's `OPENABLE`, each once, and the Applications folder, in code that is the lint's
     `OPEN_DESIGN` token for token, with its strings in the order of `OPEN_LITERALS`, so that each
     place opens its own address (decisions/0002 §20); in any other Swift file, on a System Settings
     address (`x-apple.systempreferences`), comments included, on the opener named other than in a
     call, `SystemPlaces.open(`, on an extension of `URL` or of the opener's types or a `typealias`
     naming one, and on a backtick in code, which can hide such a name; and on `Privacy_AllFiles`,
     Full Disk Access, in any Swift file. That every call of the opener is a
     button's action is `tests/test_macos_build.py`'s, not the lint's: the lint makes every call one it
     can find by that text;
   - in Swift, on a read of the pasteboard or of what is dropped on the app (writing, for Copy
     Diagnostics, is allowed here; until Copy Diagnostics is built, `tests/test_macos_build.py` and
     `check_app.py links` refuse `NSPasteboard` altogether), and on an `http://`, `https://`, `ws://`,
     `wss://` or `ftp://` URL;
   - on a phrase [PRIVACY.md](PRIVACY.md) says this project must never claim, as a whole word in any
     case, in the string literals of the app's Swift (decoded; the self-test's are not read), in the
     strings of the functions that render `Info.plist`, in the pins the plist takes the app's name
     from, and in any plist or strings file. It reads the list from PRIVACY.md, and fails if it finds
     fewer than 15 phrases;
   - on `CFBundleURLTypes` or `CFBundleURLSchemes`, or a URL handler in Swift, anywhere it reads, except
     in the set of keys the build refuses; on a daemon (`LaunchDaemon`, `SMAppService.daemon`,
     `SMJobBless`, `launchctl` and the like); and on an installer package, a `*.pkg` file in the
     repository, or `.pkg`, `pkgbuild`, `productbuild`, `productsign` or `installer` with `-pkg` later
     on its line, in Swift, the build tools (where string constants joined with `+` are read joined) or
     a workflow;
   - on a Swift file it cannot read (an unterminated string or comment, an unknown escape, a regex
     literal), and on a file that does not parse.

   It reads names written in the files. It cannot see a phrase split by an interpolation, or across
   Swift literals joined with `+`; an invisible or look-alike character inside a phrase or a name; a
   function used without being called where it is named (`let make = socket`, then `make(...)`); a
   name built at run time; an Objective-C runtime lookup; shell quoting inside a word of a workflow; or
   whether the file that starts the host passes the runtime's path and arguments to the process it
   starts (only that it writes them). Review covers the rest.

**One more test that matters here.** `tests/test_import_graph.py` imports every runtime module in a fresh
Python process, with the `[conformance]` extra installed so that `fastapi` and `uvicorn` could be
imported, and fails if either was. It also checks that its list of modules covers every module file in
the runtime package, and that the package holds no symbolic link, which its walk would not follow.
The hub server lives only in the `[conformance]` extra.

**The spec's tests.** `tests/spec/` tests the protocol spec, and the library facts its accept policy
rests on; one of its tests also holds the host's check 3 to the spec's cases. The host checks each
request it reads against the spec (`journal.py` for step 1 of the refusal order, dedupe; `envelope.py`
for the envelope and steps 2 to 4; `jobs.py` for steps 5 to 9), and those checks are tested outside
`tests/spec/`: `tests/test_envelope.py` and `tests/test_catalog.py` hold them to the request fixtures,
and `tests/test_rooms.py` runs the host's checks 3 and 4 of the accept policy (`rooms.py`).
- `test_schemas.py` walks every subschema of every schema, and fails on an object without
  `additionalProperties: false`, a value without a bound, or a pattern that reads differently in
  ECMA-262 (the regular expressions JSON Schema uses) and in Python. It also checks that the largest
  `host.describe` document stays under 8 KiB, and that every character a pattern of that document admits
  is written as one byte, so that the size cannot drift with a pattern.
- `test_fixtures.py` checks that each golden fixture validates, or fails, as labelled, both against the
  schema and against the rules the spec states that a schema cannot express, and that each request
  fixture expects the reply the spec's refusal order gives.
- `test_generated_fixtures.py` checks that `tools/spec/gen_fixtures.py`, which writes the fixtures for
  each place in each pattern (spec section 20), writes exactly the committed files; that each of those
  files still shows its one fault, and no other (with a value the pattern accepts in that place, the
  body is valid); and that, for each pattern but the one that refuses only U+0000, the other fixtures hold
  each kind of character each class allows, and each counted range at its least and its most. After a change to a schema, `uv run python
  tools/spec/gen_fixtures.py` writes them again.
- `test_accept_policy.py` checks the spec's reading of check 3 of the accept policy (hub URLs compared as
  strings with every trailing `/` removed from each, and nothing else normalised) against the pinned
  agent-switchboard, which keeps a pairing code's URL and an offered room's that way. It decodes codes,
  and it pairs, offers and accepts rooms on the library's in-process test hub, which needs the
  `conformance` extra: one whose URL ends in `/` or `//`, and one minted from another spelling of the
  hub's URL (case, a port, a query, a fragment), which the library keeps as written. Its first test also
  holds the host's own check 3 (`rooms.py`) to the spec's cases. For check 4, it runs the library to
  show that a room's `token_source` is `shared` exactly when its offer carried a hub token.
- `test_packaged.py` builds the sdist and the wheel with `uv build`, installs the wheel into a fresh
  virtual environment, loads the schemas and fixtures from there, the command line's included, and
  checks them against the source tree. It needs `uv` on `PATH` and the build backend, which uv fetches or takes from its cache. It
  builds from the working tree, so every file there that `.gitignore` does not exclude goes into the
  sdist: keep any other virtual environment outside the checkout, or name it `.venv*` or `venv`.

**The command line's contract.** [spec/CLI.md](spec/CLI.md) is the contract between the `lucille-host`
command line and any app that drives it, such as the Mac app: with `--json`, every verb but `run` writes
JSON objects on stdout instead of text, with the same exit codes, and `run --json-events` writes `run`'s
events, with `run`'s own exit codes, for an app that supervises it. Its JSON Schemas and golden fixtures
ship in the wheel beside the protocol's, under `lucille_host/spec/cli/v1/`.
- `tests/spec/test_cli_schemas.py` walks those schemas as `test_schemas.py` walks the protocol's, and
  checks that every fixture validates.
- `tests/test_cli_json.py` runs every fixture's command line, each in a home set up for it, and holds
  what it writes to the schemas and to the fixture; runs it again without `--json` and checks that the
  exit code and stderr are the same; and triggers each failure it can reach and checks its code against
  spec/CLI.md's list for that verb. `tests/test_pair.py` and `tests/test_status.py` hold the pairing verbs,
  `status`, `log` and `forget` to it, and `tests/test_supervision.py` holds `run`'s events, its lifeline,
  its exit codes, the policy while it runs, `policy reset` and the version record to section 13.
- `tests/test_catalog.py` checks that the words a shell shows for each operation are outside its
  definition hash, and say nothing [PRIVACY.md](PRIVACY.md) says this project must never claim.

**Run it yourself.**

```sh
git clone https://github.com/gald33/lucille-host
cd lucille-host
uv sync --locked --extra conformance
uv run pytest
uv run python tools/guards/workflow_lint.py .
uv run python tools/guards/ast_lint.py .
uv run python tools/guards/switchboard_lint.py .
uv run python tools/guards/macos_lint.py .
```

## 2. Check provenance

Every file of a release on PyPI must carry a PEP 740 attestation:
- its publisher names repository `gald33/lucille-host`, workflow `release.yml` and environment `pypi`;
- its signing certificate's Runner Environment extension reads `github-hosted`.

- the certificate names the release's tag, `refs/tags/v<version>`.

The release workflow makes this check after publishing (`tools/release/check_release.py published`,
which also checks that PyPI serves exactly the bytes the workflow built), and the GitHub Release carries
`SHA256SUMS`. The environment is not in the signing certificate, so what is checked is PyPI's record of
it.

**`lucille-host verify`** makes the same check on an installed host, and then checks the installed files.
`0.1.0` does not hold it: the release after `0.1.0` is the first that does. For `0.1.0`, use the
`pypi-attestations` check below, which its `[verify]` extra installs.
It needs the `[verify]` extra, which installs `pypi-attestations`. Install the release with it into a
fresh virtual environment, then run it:

```sh
python3 -m venv verify-env
verify-env/bin/pip install 'lucille-host[verify]==<version>'
verify-env/bin/lucille-host verify
```

It checks, and stops at the first check that fails:
- the host is a release, not a development build;
- PyPI lists exactly the release's wheel and sdist, and has not marked the release yanked;
- the bytes PyPI serves for each have the sha256 it lists, and each file carries the attestations above,
  verified with sigstore against those bytes;
- every file the wheel holds but its `RECORD` is installed, with the same sha256, in the folder the
  running package came from, and the package folder holds no other file but bytecode in `__pycache__`.

On success it prints the release, each file's sha256 and how many attestations verified, the folder it
checked, how many files there are the wheel's, and how many bytecode files it did not check; with
`--json`, one `verify` object ([spec/CLI.md](spec/CLI.md)). It asks only `pypi.org`,
`files.pythonhosted.org` and sigstore's trust root, and reads none of the host's own files. A failure
says which check and which file, and exits 1: `not_attested` for a release that is not attested,
`files_differ` for files installed here that are not the release's, `yanked` for a withdrawn release,
`unreachable` when PyPI or sigstore could not be asked (nothing was verified: try again).

`lucille-host verify` is the installed host checking itself. It cannot vouch for an install whose files
were changed before it ran, since the changed code is what runs. Run it from a fresh install, as above.
To check a file without running any of the host's code, `pypi-attestations` checks the repository on its
own (not the workflow, tag or runner):

```sh
verify-env/bin/python -m pypi_attestations verify pypi --repository https://github.com/gald33/lucille-host pypi:lucille_host-<version>-py3-none-any.whl
```

The same check works for the pinned `agent-switchboard` release. Its publisher is `gald33/switchboard`,
workflow `publish.yml`, environment `pypi`, ref `refs/tags/v2.6.0`. The release workflow checks it before
it builds, and you can run the same check from a checkout:

```sh
uv sync --locked --extra verify
uv run python tools/release/check_release.py dependency agent-switchboard --scratch /tmp/sb-check
```

It downloads each file of the release `uv.lock` pins, checks it against the lock's sha256 and PyPI's,
and verifies its attestation with sigstore.

A release is called "reproducible" only when a rebuild from the tagged source produced the same sha256s.
No rebuild job exists yet.

## 3. Run the conformance suite

Install the release with its `[conformance]` extra into a fresh virtual environment, then run the suite:

```sh
python3 -m venv verify-env
verify-env/bin/pip install 'lucille-host[conformance]==<version>'
verify-env/bin/python -m lucille_host.conformance
```

It runs offline, against a local Switchboard hub it starts on 127.0.0.1. It pairs the installed
`lucille-host`, in a home of its own under a temporary folder, through the real `pair begin` flow,
signing its pairing code with a throwaway key it passes to `--test-key` (never Lucille's), and sets the
policy with the host's own `ops enable` and `roots add`, as you would. Then it plays a hostile Lucille:
it starts `run`, offers a work room, and posts the requests of its scenarios, each aimed at a row of
"Host invariants and their tests" in the design record, and a positive control. [CLAIMS.md](CLAIMS.md)
names the scenario behind each claim. It prints one line per scenario: `PASS`, `FAIL` with the reason, or
`SKIP` with the reason when the scenario cannot check on this computer: the child's environment is read
from Linux's `/proc`, which macOS does not have, and the scenarios that search need ripgrep where the
catalog says (`/usr/bin/rg` on Linux, `/opt/homebrew/bin/rg` on macOS). It exits 0 only when at least one scenario passed and
none failed; a skipped scenario is never a pass. The host refuses to run as root, and so does the
suite: run it as an ordinary user. What each scenario asserts is in its docstring, in
`lucille_host/conformance/`.

Run it with the virtual environment's own `python`, as above. The conformance lint lets the suite name
the programs it starts by their path beside that interpreter, so that it tests the `lucille-host`
installed with it, not one that happens to be first on your `PATH`.

## 4. Watch

`lucille-host log` shows every request received, every local decision, and a fingerprint of everything
sent. `lucille-host status` shows the pair, its work rooms, and when the host last reached the hub.

## What verification cannot establish

- Anything about what Lucille's backend does with a reply.
- Whether installed files are still the verified ones after any process running as your user has touched
  them. `lucille-host verify` checks them as they are when it runs, with their own code.
- The bytecode Python may load in place of an installed source file, the `lucille-host` script the
  installer wrote, the interpreter, and every other package in the environment: `lucille-host verify`
  checks none of them.
- The provenance of dependencies beyond the ones checked.
- Tag protection and the `pypi` environment's required reviewer. They are repository settings, which a
  reader cannot see.
