# 0004: Running as root, uid 0 (draft)

**Status: proposed — awaiting operator ruling.** Nothing in this record is enforced, and it changes no
runtime code, test or CI job. Sections 1 to 3 say what the host does today, with the file and line of each
claim, what was run, and what could not be checked. Section 4 proposes a rule and lists the code it would
change later. It extends [0001](0001-host-design.md), which says the host "refuses to run as uid 0"
("Pairing", `0001-host-design.md:230`), and [0003](0003-local-credentials.md).

Line numbers are those of `main` at commit cbec58f. The commands below were first run on 6d66668; the
per-verb table in 1.2 and the user-first repro were run again on cbec58f. That range changed `policy.py`,
`state.py` and `store.py` (nesting limits), and nothing this record describes.

**How it was checked.** Linux container, Python 3.13, the lock file's dependencies and the `conformance`
extra. "As root" is uid 0; "as a user" is uid 65534 (`nobody`). Every command ran with a throwaway
`HOME` under `/tmp` and an empty environment (`env -i`), so nothing touched a real home folder. That means
the observations say what the code does for uid 0, not what it does with `HOME=/root`: that was not run
(section 1.2 reads the code for it). Not checked anywhere: macOS (no Mac in the container), a desktop session
bus, a Secret Service, a real Home Assistant add-on.

## 1. What the host does today under uid 0

### 1.1 One refusal, at five call sites

`state.startup_checks` raises `Refused("lucille-host does not run as root (uid 0); run it as the user whose
files it may read")` when `os.geteuid() == 0` (`src/lucille_host/state.py:205-211`). It is called from:

| Call site | Reached by |
|---|---|
| `link.open_session`, `link.py:1785` | every verb that opens the pair's stores: `pair` (the `pair status`, `confirm` and `reject` actions through `cli.py:1063`), `status` (`cli.py:1427`), `forget` (`cli.py:1453`), and `run` (`cli.py:1313`) |
| `cli._pair`, `cli.py:1148` | `pair` with a code, before its lock file |
| `cli._run`, `cli.py:1307` | `run`, before its lock file |
| `cli._credential_verb`, `cli.py:1610` | `credential set`, `list`, `remove`, `test` |
| `library.start`, `library.py:182` | the in-process `Host` API |

The refusal is `startup_refused`: exit 1 for a command, 24 for `run` (`spec/CLI.md:710`, `:907`),
and, under `--json`, a `{"type":"error","code":"startup_refused"}` object for every verb but `run` (`FAILURE_CODES` and
`_failure`, `cli.py:1785`, `:1800`); `run` reports it as a `stopped` object (`cli.py:1371`, and below).

**Observed as root:** `status`, `pair status`, `credential list`, `forget` exit 1 with that sentence, `run`
exits 24, `status --json` writes the `startup_refused` object, and `pair --store-file` is refused too.
`run` takes no `--json`: `run --json` is a `usage` error for both uids. `run --json-events` as root writes
`{"v":1,"type":"stopped","reason":"startup_refused"}` and exits 24.
`--version` works. The refused verbs wrote no file: the tree left after all of them held only what `pause` made.

### 1.2 What is not refused: the verbs that never open a store

No uid check stands before `log`, `pause`, `resume`, `kill`, `ops`, `roots`, `policy` or `--version`.
What each does as root, run once on a fresh empty `HOME` (cbec58f); "writes" lists every file made, under
`<state>` (`.local/state/lucille-host/default`) or `<config>` (`.config/lucille-host/default`), and every
folder above it that was missing is created too, mode 0700, owned by root (`paths.ensure_dir`,
`paths.py:168-182`):

| Verb as root | Exit | Writes |
|---|---|---|
| `log`, `ops list`, `roots list`, `roots check`, `policy previous` | 0 | nothing |
| `pause` | 0 | `<state>/paused`, `.controls.lock` (`controls.py:70`, `:114`) |
| `resume` | 0 | `<state>/.controls.lock`, and the folders above it |
| `kill` | 0 | `<state>/kill-request`, `paused`, `.controls.lock` |
| `ops enable files.sha256` | 0 | `<config>/.policy.lock`, `policy.toml` |
| `roots add a <folder>` | 0 | `<config>/.policy.lock`, `policy.toml` |
| `ops disable files.sha256` (not enabled), `ops enable nosuch.op`, `roots remove a` (no such alias), `policy reset` (no invalid file) | 1, each refused | `<config>/.policy.lock` and the folders above it |
| `status`, `pair status`, `forget`, `credential list` | 1 | nothing |
| `run` | 24 | nothing |

The lock file comes first because every verb that may change the policy takes it before it looks at its
arguments (`cli.py:788-793`), so a verb the host then refuses still leaves it, and the root-owned folders
around it. `policy.toml` is written by renaming a new 0600 file over the old one (`policy.py:513-519`),
so the new file belongs to whoever ran the verb. Of the table's verbs, those that write are:
`pause`, `resume`, `kill`, `ops enable`/`disable`, `roots add`/`remove` and `policy reset`, whether or not
they succeed.

**Consequence, observed, in either order.**
- *Root first.* After a root `pause` or `resume` in a `HOME` a user also uses, **every folder `ensure_dir`
  had to create** is `drwx------ root`: on a `HOME` with no `.local` that is `.local`, `.local/state`,
  `.local/state/lucille-host` and the profile folder. (The config side, `.config/...`, is created only by the
  verbs that take the policy lock, never by `pause`, `resume` or `kill`.) The user's `status` and `resume` then fail with `Permission denied
  (.../lucille-host/default)` (exit 1). Where `~/.local` or `~/.config` was created this way, other programs
  that use them are locked out too.
- *Root after the user.* A root `roots add` writes `policy.toml` by renaming a new file over the user's, and
  the file is then `-rw------- root`. The user's `roots list`, `ops list` and `roots add` then fail with
  `the policy file ... is invalid: ... cannot be read: Permission denied` (exit 1, `ops list` too). The folders stay the user's, since a rename needs no change of owner there.
- *A refused root verb.* `ops enable nosuch.op` as root exits 1 ("not in this release's catalog"), yet left
  `.config/lucille-host/default/.policy.lock` and every folder above it, `.config` included, owned by root, in
  a `HOME` that had none; the user's `status` then failed reading `host.json`.
- *Root reading the user's files* works without error, because root ignores the modes.

So the seven verbs do not all lock the user out: `log`, `ops list`, `roots list`, `roots check` and `policy
previous` write nothing. Every verb in the table that writes does, a refused one that took the policy lock
included. Nothing in
`src/` compares a file's owner with the process: a search for `st_uid`, `getuid`, `umask`, `chown`,
`setuid`, `setgid`, `initgroups`, `preexec_fn` and `user=`/`group=` in `src/lucille_host/` finds only the
one `geteuid` in `state.py:208` and the one in `runs.py:133`.

### 1.3 Files, modes and ownership

- There is no `umask` call. Files are created with an explicit mode, 0600: `audit.py:107-109`
  (`O_CREAT` then `fchmod`), `controls.py:70`, `:114`, `cli.py:793`, `loop.py:244`, `state.py:102`,
  `policy.py:518`, `runs.py:183`, `store.py:632`. Directories are 0700 (`paths.py:178-182`). Under uid 0
  the same modes are used, and they keep other **users** out but not root.
- The only "is it private" check reads the mode, never the owner: `state._check_private` refuses the
  runtime folder when it is not a directory or `mode & 0o077` is set (`state.py:222-228`). A world-writable
  state folder is not refused anywhere else.
- Run record: `runs.record` returns without writing when `os.geteuid() == 0` (`runs.py:133`). Observed: as
  a user the first command made `runs.jsonl`; as root none was made.

### 1.4 `HOME`, XDG and the per-user runtime folder

- `paths._home` takes `$HOME` when it is an absolute path and otherwise `Path.home()` (`paths.py:138-140`),
  which Python takes from the password database for uid 0 (normally `/root`; not run with `HOME=/root`).
  Config is `$XDG_CONFIG_HOME` or `~/.config`, state is `$XDG_STATE_HOME` or `~/.local/state`, each
  `lucille-host/<profile>` (`paths.py:143-145`, `:163-164`). A relative XDG value is ignored.
- `store.py:488` looks for keyring's `python_keyring/keyringrc.cfg` under `$XDG_CONFIG_HOME` or
  `Path.home()/.config`. It raises nothing when no home can be found: that is "no setting".
- The host never uses the system's per-user runtime folder (`/run/user/<uid>`). It points
  `XDG_RUNTIME_DIR` at `<state>/run`, mode 0700 (`state.py:231-236`), so a socket planted for Switchboard's
  client is never found. This is the same for uid 0.

### 1.5 Which folders may be added, under uid 0

The "broad folder" refusal asks whether a folder is **the caller's own** home, holds it, or is `/`
(`policy.is_broad`, `policy.py:350-353`; `cli.py:885-890`, `:943`). Observed through `policy.is_broad`:

| `home` | `/` | `/home` | `/root` | `/etc` | `/home/alice` |
|---|---|---|---|---|---|
| `/root` | broad | **not broad** | broad | not | not |
| `/home/alice` | broad | broad | not | not | broad |

So as root, `lucille-host roots add homes /home` is accepted without `--allow-home-or-root` (observed), and
with it every user's home becomes a folder an operation may read. As a user the same command is refused
when `/home` holds their home. A root-owned process also ignores file modes, so an enabled `files.*`
operation on any folder reads whatever is there. `containment.py` decides by path, never by owner.

### 1.6 Children, privileges and the network

- Operation children are started in one place, `_exec.run` (`_exec.py:235-243`): `argv` only, no shell,
  `env` set to `CHILD_ENV` plus the names the catalog lists, `start_new_session=True`, `close_fds=True`.
  There is **no privilege drop**: no `user`, `group`, `extra_groups` or `preexec_fn`. A child runs as the
  host's uid, so under uid 0 it would run as root.
- The keyring-using operations (`github.read` and the four `gmail.` ones) get `DBUS_SESSION_BUS_ADDRESS` and `XDG_RUNTIME_DIR` on Linux, `HOME` on
  macOS, copied from the host's environment (`ops/__init__.py:473`, `:663`, `:674`).
- The credential proxy refuses these destination networks: `0.0.0.0/8`, `10/8`, `127/8`, `169.254/16`,
  `172.16/12`, `192.168/16`, `100.64/10` (carrier-grade NAT) and others; IPv6 only inside `2000::/3`
  (`proxy.py:104-128`, `address_allowed` at `:204-215`). It vets the resolved address on every connection.
  Nothing there depends on the uid.
- Anything that assumes a login session: the Linux keyring needs a session bus (see 2.2), and the macOS
  app and launchd are outside this record (not checked).

## 2. The run record, profile state and keyring under root

### 2.1 Where each lives

| Thing | Path | Under uid 0 |
|---|---|---|
| Config: `policy.toml`, `host.json` | `$XDG_CONFIG_HOME` or `<home>/.config`, then `lucille-host/<profile>` | root's own tree; unreadable to users |
| State: `pair.json`, `rooms.json`, `journal.json`, `cursors.json`, `status.json`, `paused`, `audit.log`, locks | `$XDG_STATE_HOME` or `<home>/.local/state`, then `lucille-host/<profile>` | same |
| Run record | `<state>/runs.jsonl` | **not written** (`runs.py:133`) |
| Pair secret | the OS keyring item, or `<state>/pairs/` under `--store-file` | **not reachable**: the verbs that open it refuse first |
| Credentials | the keyring item `lucille-host.credentials`; no file fallback (`spec/CLI.md:1165-1166`) | not reachable |

### 2.2 The keyring, with and without the off switches

The host builds only keyring's Secret Service backend on Linux (`store.KNOWN_BACKENDS`, `store.py:204-206`;
`_build`, `store.py:518-541`). Run through `store._build("linux")` and `store._unfit` in a container with no
session bus, **as root and as a user the answer is the same**:

| `PYTHON_KEYRING_BACKEND` | Backend result | `_unfit` says |
|---|---|---|
| unset | `FailKeyring` | "has no keyring to store in" |
| `keyring.backends.null.Keyring` | `NullKeyring`, `keyring_turned_off()` is true | "stores nothing" |
| `keyring.backends.fail.Keyring` | the same | "stores nothing" |

As a user, `pair status` then exits 1 with `insecure_keyring` and the sentence that points to `--store-file`;
`run` exits 23; `credential list` exits 1 (`cli.py:139`, `:1369`, `:1708`). **As root none of that is
reached:** `startup_checks` refuses first, so a root process shows `startup_refused` where a user shows
`insecure_keyring`. If the uid check were lifted, root would meet the same `insecure_keyring` refusal, and
the only way on would be `pair --store-file` (0600 files in `<state>/pairs`, with the weaker-store warning).
That path was run as a user (it reached "no pairing code was given on stdin" after the warning); as root it
is refused. `credential` has no `--store-file`, so no credential can be saved without a keyring.

### 2.3 Can root's files be read by other users, or the reverse?

Observed (1.2): files and folders root creates are 0600/0700 owned by root, so a user gets `Permission
denied`; root can read and rewrite a user's files, because the modes do not bind it. A profile that a root
command touched, first or later (1.2), therefore breaks the user's commands until someone `chown`s or removes
the root-owned file or folder. The host does nothing about it, and nothing in this record or in the code
repairs a profile that is already root-owned.

### 2.4 The test suite as root and as a user

`pytest -q` over the whole suite, once as root and once as `nobody`, same tree and dependencies, each with
its own `HOME` and temp folder:

| | passed | failed | errors | skipped |
|---|---|---|---|---|
| root | 9511 | 3 | 0 | 4 |
| user (`nobody`) | 9501 | 4 | 11 | 2 |

- The same three tests failed in both: `tests/test_ci_workflow.py::test_the_environment_check_on_a_real_interpreter`,
  `::test_a_control_that_cannot_fire_fails_the_environment_check` and `::test_the_environment_check_sees_a_pycache_prefix`.
  The only message captured for them was a `PYTHONHOME` planted-file check; they are not about the uid.
- **No test passed as a user and failed as root.** The suite's tests of the root refusal were not looked at one by one; none of them is among the failures.
- The user run's extra failures are the container's, not the uid's: eleven setup errors in
  `tests/spec/test_packaged.py` and `tests/test_macos_payload.py`, which run `uv build` (the user cannot read
  the proxy's CA bundle under `/root`, so `uv` cannot reach the package index), and one
  `PermissionError` in `tests/test_run_process.py::test_run_as_separate_processes`, not traced to its line.
  A first pair of runs, made before the `conformance` extra was installed, differed the same way and
  otherwise gave the same set of failures for both uids.
- So the suite gives no evidence that behaviour differs under root beyond what sections 1 and 2 show by hand.

## 3. A Home Assistant add-on

Home Assistant renamed "add-ons" to "apps"; its developer pages for them moved from
`developers.home-assistant.io/docs/add-ons/` to `contributors.home-assistant.io/apps/` (the old addresses
redirect). The pages read, 2026-11-19:

- [configuration](https://contributors.home-assistant.io/apps/reference/configuration/), the keys of
  `config.yaml`;
- [how apps work](https://contributors.home-assistant.io/apps/how-apps-work/);
- [S6 overlay](https://contributors.home-assistant.io/apps/guides/s6-overlay/);
- [security](https://contributors.home-assistant.io/apps/guides/security/);
- [runtime environment](https://contributors.home-assistant.io/apps/reference/environment/);
- [communication](https://contributors.home-assistant.io/apps/guides/communication/);
- [options](https://contributors.home-assistant.io/apps/guides/options/).

### 3.1 What the model is

- An app is a container the Supervisor manages (how apps work). Its base images start with S6 overlay as
  process 1, so `init: false` is needed or the container stops with `can only run as pid 1` (S6 overlay;
  configuration, `init`). Services are described in `rootfs/etc/s6-overlay/s6-rc.d/`, a setup step is a
  oneshot (its example runs `mkdir -p /data/config` there), and a service not in the user bundle never
  starts (S6 overlay).
- `/data` is "the private, persistent storage of your app": it survives restarts and updates and is part of
  every backup of the app. "Everything outside `/data` and the mapped folders is lost when the container is
  recreated" (how apps work; runtime environment). Backups are hot by default.
- Settings: `options` and `schema` in `config.yaml`; the Supervisor writes the user's values to
  `/data/options.json` before each start (options; how apps work). Scripts need `#!/command/with-contenv
  bashio` to see the container's environment, `SUPERVISOR_TOKEN` among it (runtime environment).
- Folders: `map` mounts host folders (`share` at `/share`, `media`, `ssl`, `backup`, `homeassistant_config`
  at `/homeassistant`, ...), read-only unless `read_only: false` (configuration, `map`).
- Network: by default an internal network, with the Supervisor at `172.30.32.2` and each app reachable by
  name (communication); `host_network: true` uses the host's network instead; `ports` publishes a container
  port (configuration, `Network`).
- Confinement: every app runs with Docker's default AppArmor profile, unless it ships an `apparmor.txt`,
  which replaces that profile and raises the app's security rating, or sets `apparmor: false`, which turns
  AppArmor off and lowers the rating (security page; configuration page, `apparmor`). Those are two
  separate mechanisms. "Protection mode" is on by default and withholds `full_access`, `docker_api` and
  `host_pid` (security page). The example profile allows `/data/** rw`.
- D-Bus: `host_dbus: true` mounts the host's **system** bus, `/run/dbus`, read-only (configuration). The
  pages describe no session bus and no Secret Service.

The pages I read never state which user an app's processes run as. Docker's default, with no `USER`, is
root, and the S6 pages show the init running as pid 1; I did not check the base images' source, so
"root by default" is an inference here, not a quotation. Whether the base images ship `s6-setuidgid` or
another way to drop privileges is not checked.

### 3.2 Which of the host's assumptions break there

1. **The uid-0 refusal (1.1) stops everything that matters.** `pair`, `run`, `status`, `forget` and
   `credential` exit as `startup_refused` in a default app. This is the assumption that breaks first.
2. **The OS keyring (2.2).** No session bus, no Secret Service, so `insecure_keyring`; `run` exits 23. The
   only route is `pair --store-file`, which keeps the pair secret and the work rooms' keys unencrypted in
   `<state>/pairs`. If that sits in `/data`, it is in every backup of the app, by the pages above. The
   credential operations (`github.read`, the four `gmail.` operations) cannot be used at all: they need a
   keyring credential and `credential` has no file fallback.
3. **Where files live (1.4).** With `HOME` and the XDG variables unset or in the image, state is under a
   path the container loses on recreation: a pairing, the policy, the audit log and the run record would
   vanish on update. The host already takes an absolute `XDG_CONFIG_HOME` and `XDG_STATE_HOME`
   (`paths.py:143-145`), so pointing both inside `/data` needs no host change (not run in an app).
4. **No listening port, so none to publish.** The host opens none (README; 0001), so `ports` and
   `host_network` are not needed. Its outbound connections go to the hub and, for the two credential
   families, `api.github.com` and `imap.gmail.com`. The proxy's refused ranges (1.6) include `172.16/12`,
   which holds the app network above, and `100.64/10`: it refuses a name that a local resolver answers
   with an address there. A resolver that rewrites `api.github.com` to a private address (a DNS filter app)
   would make `github.read` fail with exit 4. Not tested in an app.
5. **Pairing is interactive (0001, "Pairing").** The SAS is confirmed by typing `y` in `pair` or later with
   `pair confirm`. An app has no terminal; the pages describe `stdin: true` with the
   `hassio.app_stdin` action as the way to write to an app's standard input (communication), a shell into
   the container, or a web page through ingress. None is part of the host today. Not checked.
6. **Stop and supervision.** `run` drains on SIGTERM and on the end of standard input
   (`--exit-on-stdin-eof`, 0001 "Pause, kill and shutdown"), which fits S6 stopping a longrun. Not run
   under S6.
7. **Reach of files.** If an app maps `share`, `media` or `homeassistant_config` read-write, a `files.*`
   operation enabled on it runs as uid 0 there, with no mode to stop it (1.5, 1.6). AppArmor's profile, not
   the host, would then be the limit. `homeassistant_config` holds `secrets.yaml`.
8. **Out of scope there.** macOS, launchd and the Mac app's login item do not apply.

## 4. Proposed rule

### 4.1 Options

**A. Keep refusing uid 0, for every verb, and run an add-on's host as an unprivileged user.**
The host's own check stays, and moves to the front of the command line so that no verb (`pause`, `roots`,
`policy`, `ops` included) can write a profile as root. The app's image creates a user, a setup step of the
S6 overlay makes the state folders under `/data` its own, and the service starts the host as that user.
- For: keeps invariant 0001 and the claim "files only your user can read" true everywhere; stops *new*
  root-owned files and folders from any verb (1.2) and the `/home` hole in 1.5 without new policy; no code runs
  as root.
- Against: it does nothing for a profile that is already root-owned (4.3); the image must carry the
  privilege drop and a `chown` step (neither checked here); the keyring is
  still missing, so the app can only use `--store-file` and no credential operation.

**B. Allow uid 0 under conditions.** For example: an explicit flag; `XDG_CONFIG_HOME` and `XDG_STATE_HOME`
required, absolute, and not under `/root`; the file store required (no keyring under root); children
dropping privileges to a fixed unprivileged id.
- For: runs in a default app with no image work.
- Against: the most code (a privilege drop in `_exec.py` that does not exist today, root-aware
  `is_broad`, owner checks that do not exist today); each condition is one more thing to keep true; a
  root-run host reads any folder it is given; and "does only what you switch on" would rest on a flag.

**C. An add-on mode.** One explicit switch (for example `--addon`) that allows uid 0 only with a fixed set of
paths under `/data`, the file store, and no credential operation.
- For: one named, testable case.
- Against: B's code with a smaller surface; the mode is detected from a flag, not from anything the host can
  check; a second code path to review in a program whose point is that it is small.

### 4.2 Recommendation: A

Refuse uid 0 for every verb except `--version` and `--help` (the read-only `log`, `ops list`, `roots list`,
`roots check` and `policy previous` could stay open to root; that is for the ruling to say); give an add-on an unprivileged user in its own
image; settle the keyring question for the add-on separately, in 0003, before any add-on claims
credential operations.

Why: the refusal already exists and is already the documented invariant; the only gap found (1.2) is that
seven verbs skip it (`log`, `pause`, `resume`, `kill`, `ops`, `roots`, `policy`), and the ones that write (all but the read-only `log`, `ops list`, `roots list`, `roots check` and `policy previous`) leave root-owned files or folders that lock the real user out of their own profile; and every
option that allows root buys the add-on convenience with code that makes the host larger and its claim
conditional. If the ruling is that an add-on must run as root, C is the narrower of the two, and the
conditions in B (state under `/data`, file store, no credential operation, and a privilege drop for
children) should be its minimum.

### 4.3 What the code would change later (not written here)

For A:
- `cli.py`: run the uid check once, before dispatch, for every verb but `--version` and `--help`; keep
  `state.startup_checks`'s other half (the `SWITCHBOARD_*` check) where it is, or fold both together.
- `state.py:205-211`, `library.py:171-182`: keep the check in the library entry point.
- A host verb, run as the user, for a profile root has touched (4.3): resolves the paths as `paths.py` does,
  reports root-owned folders and files between the profile and the first folder the user owns, and prints the
  exact repair; and the refusal that points to it. Tests plant a root-owned file, a root-owned profile folder
  and a root-owned ancestor, in the default layout, with a custom and a relative `XDG_*_HOME`, with a symlink,
  and one that a user-owned profile passes.
- `runs.py:133`: becomes unreachable; keep it as a second guard or remove it.
- `spec/CLI.md`: `startup_refused` (`:710`, `:907`), the "does not run as root" lines (`:1030`, `:1166`,
  `:1199`), the verbs table; and `spec/` fixtures if a verb's refusal is listed there.
- Tests: `tests/test_state.py` and `tests/test_pair.py` (refused as root) gain `log`, `pause`, `resume`,
  `kill`, `ops`, `roots`, `policy`; a test that no file is written for a root run of any of them.
- `CLAIMS.md` (the "startup checks refuse running as root" row and the run-record row), `VERIFYING.md` if it
  names the verbs, and 0001's invariant line.
- The add-on's image (outside this repository): a user, a setup step for `/data`, and the service started
  as that user.

#### Profiles that are already root-owned

No option here changes a profile that a root command has already touched; refusing root from now on leaves it
as it is. What follows was checked on cbec58f, as a user, for the **default layout only** (`HOME` the user's,
no `XDG_*_HOME` set).

- *What the damage is.* A folder or file that exists, is owned by another uid (root), and sits between the
  profile and the first ancestor the user owns, bounded by `HOME`. Folders root created are `drwx------`, so
  the user cannot look inside them, and a file root wrote (a `policy.toml`) is `-rw-------`. The host does not
  detect it. It reports `Permission denied` or `cannot be read` with a path, and never says that the owner is
  another uid (1.2).
- *Detection, default layout.* The folders the host creates there are, on the state side,
  `.local`, `.local/state`, `.local/state/lucille-host` and `.local/state/lucille-host/<profile>`, and on the
  config side `.config`, `.config/lucille-host` and `.config/lucille-host/<profile>` (`paths.py:163-164`;
  `ensure_dir`, `paths.py:168-182`; `.local` and `.config` only where they did not exist). Run
  `ls -ld` on each of them from `HOME`. A folder root owns shows as `root`; the ones below it show `Permission
  denied` (the user cannot look inside), which is expected and is not a second finding; `No such file or
  directory` means it was never made, which is not damage. The **highest** folder shown owned by root is the
  one to repair. Looking inside the profile folders cannot find it (`find <config> <state> ! -user $(id -u)`
  printed `Permission denied` and listed nothing, run), and a `find` with a fixed depth can miss it (with a
  deeper layout `find "$HOME" -maxdepth 4 -user root` printed nothing and exited 0 while the user was locked
  out, run). For a root-owned *file* in a folder the user owns, `ls -l <config>/lucille-host/<profile>` shows
  it.
- *Repair, default layout.* It needs root. Run `chown -R <user> <folder>` on the highest folder owned by root,
  stopping at `HOME` (never `HOME` itself or anything above it). The profile folder alone is **not** enough:
  `chown -R` of `.local/state/lucille-host/default` left the user's `resume` failing with the same
  `Permission denied`, because `.local` and `.local/state` above it were root-owned 0700; `chown -R` of
  `.local` made it work (run, exit 0). For a root-owned file such as `policy.toml`, `chown` the file, or
  `chown -R` the profile folder; the user's own `lucille-host policy reset` also works, as below.
- *Look first.* The folder to repair may hold root's files from another program: a `~/.config` or `~/.local`
  that root created can by now hold other programs' data, and nothing here proves the host created the folder
  named. As root, `ls -lR` it first and repair only what is the host's, or the folders leading to it; do
  not run `chown -R` on a folder you have not looked at.
- *A root-owned `policy.toml` in a folder the user owns.* The user's `lucille-host policy reset` works: it
  sets the file aside as `policy.invalid.toml` and starts from the fresh policy (only `host.describe` enabled,
  no folder). The set-aside file stays root-owned and unreadable, and whatever the user's own policy was is
  gone (root's rename had replaced it); `ops enable` and `roots add` must be run again.
- *Not checked.* These layouts and cases were not worked out, and the advice above does not cover them:
  custom `XDG_STATE_HOME` or `XDG_CONFIG_HOME` paths (the profile can then sit at any depth, or outside
  `HOME`); relative `XDG_*_HOME` values, which the host ignores (`paths.py:143-145`) and relative `HOME`
  (`paths.py:138-140`); symlinked profile folders or symlinked `~/.local` or `~/.config`; macOS (a different
  layout, `paths.py:160-162`, and no Linux tools); anything outside `HOME`. In those cases a person has to look.
- *What the host says, and what would be built.* Today: the sentences above. Proposed, for the code after the
  ruling and as part of option A, not here: a host verb, run as the user, that resolves the profile paths
  exactly as `paths.py` does (including a custom or relative `XDG_*_HOME`, the macOS layout and symlinks),
  reports each root-owned folder or file it finds between the profile and the first folder the user owns, and
  prints the exact repair for that case. The refusal a user sees when the profile is not theirs would point to
  that verb, or, until it exists, to the default-layout advice above. That verb is new code, built and
  tested in the implementation change (nothing compares owners today, 1.2). Without it, A only stops new
  damage.

For B or C, add to the above: a privilege drop in `_exec.py:235-243` and its tests; a root-aware
`policy.is_broad` (or a rule that a root caller's "home" is every home); the path conditions in `paths.py`;
and a new row in `CLAIMS.md` that says what the host promises when run as root.

## 5. Not decided here

- Whether a file-held credential store should exist at all (0003). Without it, an add-on cannot use
  `github.read` or the Gmail operations under any option above.
- How an add-on confirms a pairing (5 in 3.2).
- Whether to adopt the owner check and its refusal for profiles that are already root-owned (4.3), or to leave
  those users with `policy reset` and a manual `chown`. The refusal's wording was not tried.
- How an add-on's image drops privileges for the service (A): the S6 pages read describe a oneshot setup step
  and the user bundle, and no step that starts a service as another user; whether the base images carry a
  tool for it is not checked.
- For the ruling: whether the detection and repair for a profile root has touched belong in the host, as
  one verb run as the user, built and tested in the implementation change after the ruling (4.3), or are
  left to the default-layout advice in 4.3 and a person's judgement. This record gives that advice for the
  default layout only and lists, in 4.3, what is not checked.
- Whether to document `sudo` use (`sudo` may keep the user's `HOME`, in which case the files would be the
  user's but made by root). Not tested.
