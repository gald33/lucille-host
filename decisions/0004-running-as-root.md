# 0004: Running as root, uid 0

**Status: decided 2026-11-20 by the repository owner: Option A (4.2)**, in
[a comment on PR #78](https://github.com/gald33/lucille-host/pull/78#issuecomment-7032741923):
"The host refuses uid 0 for every verb except `--version` and `--help`, and a Home Assistant add-on runs the
host as its own unprivileged user. The items in §5 stay open." **Built:** the refusal is in
`cli.main`, before the profile's paths are resolved (see "How it was built", the end of 4.3), and the
repair verb of 4.3 and section 5, as `lucille-host profile check` (see "The repair verb, as built", after
it). Sections 1 to 3 describe the host
as it was before that change, on `main` at the commit named below, with the file and line of each
claim, what was run, and what could not be checked. Section 4 weighs the options, records the rule decided and lists the code it
changes. It extends [0001](0001-host-design.md), which says the host "refuses to run as uid 0"
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
the root-owned file or folder. The host does nothing about it today; 4.3 gives manual steps for the default
layout, and proposes a host verb for the rest.

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

## 4. The rule

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

### 4.2 Decision: A

Decided: refuse uid 0 for every verb except `--version` and `--help`, the read-only verbs included; give an
add-on an unprivileged user in its own image; settle the keyring question for the add-on separately, in 0003, before any add-on claims
credential operations.

Why: the refusal already exists and is already the documented invariant; the only gap found (1.2) is that
seven verbs skip it (`log`, `pause`, `resume`, `kill`, `ops`, `roots`, `policy`), and the ones that write (all but the read-only `log`, `ops list`, `roots list`, `roots check` and `policy previous`) leave root-owned files or folders that lock the real user out of their own profile; and every
option that allows root buys the add-on convenience with code that makes the host larger and its claim
conditional. The ruling chose A and the add-on runs as its own unprivileged user, so B and C are not taken.
The read-only verbs are refused too: one check before dispatch, with no list of verbs to keep true.

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
  directory` means it was never made, which is not damage. On each side, state and config, the **highest** folder shown
  owned by root is the one to repair (a profile can be damaged on one side only, or both). Looking inside the
  profile folders cannot find it (`find ~/.config/lucille-host/<profile> ~/.local/state/lucille-host/<profile>
  ! -user $(id -u)` printed `Permission denied` and listed nothing, run), and a `find` with a fixed depth can miss it (with a
  deeper layout `find "$HOME" -maxdepth 4 -user root` printed nothing and exited 0 while the user was locked
  out, run). For a root-owned *file* in a folder the user owns, `ls -l ~/.config/lucille-host/<profile>` (for
  `policy.toml`) shows it as owned by `root`. Write the path in full: the `<config>` and `<state>` of 1.2 already
  include `lucille-host/<profile>`, so appending that to them names a folder that does not exist, and `ls`
  then says `No such file or directory`, which must not be read as "no damage".
- *Repair, default layout.* It needs root. Run `chown -R <user> <folder>` on the highest folder owned by root on
  each side, stopping at `HOME` (never `HOME` itself or anything above it); the folder's group stays root's,
  which does no harm (`<user>:<group>` changes it too). The profile folder alone is **not** enough:
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
- *What the host says, and what would be built.* Before the verb was built: the sentences above. Proposed,
  for the code after the ruling and as part of option A, not here: a host verb, run as the user, that resolves the profile paths
  exactly as `paths.py` does (including a custom or relative `XDG_*_HOME`, the macOS layout and symlinks),
  reports each root-owned folder or file it finds between the profile and the first folder the user owns, and
  prints the exact repair for that case. The refusal a user sees when the profile is not theirs would point to
  that verb, or, until it exists, to the default-layout advice above. That verb is new code, built and
  tested in the implementation change (nothing compares owners today, 1.2). Without it, A only stops new
  damage. **Built** as `profile check`: see "The repair verb, as built", below.

**How it was built** (the implementation change, [PR #80](https://github.com/gald33/lucille-host/pull/80), after the ruling): `cli.main` refuses uid 0 for every verb
but `--version`, `--help` and a command with no verb, after the command line parses and before `_dirs`;
`run` refuses at the top of `_serve`'s caller (`cli._run`), before a lifeline, a signal handler, the
profile or a lock. The message is `cli.ROOT_REFUSED`: the sentence of `state.startup_checks`, with a pointer
to the default-layout advice above (since the repair verb was built, below, a pointer to `lucille-host
profile check`, run as the user). The code is `startup_refused`
(exit 1, 24 for `run`; an `error` object, or `stopped` under `--json-events`), so spec section 10 and the
schemas needed no new name. `state.startup_checks`, and with it `library.start`, is unchanged, and so is
`runs.py:133`, kept as a second guard. Tests: `tests/test_root_refused.py`. Not built: the file-held store,
add-on pairing, the privilege drop and `sudo` handling (section 5); none of them is decided. (The repair
verb, listed here at first, is built: below.) The check reads the
*effective* uid, `os.geteuid()`, as `state.startup_checks` does: a process whose real uid is 0 and whose effective
uid is not is not refused (not tested). That covers a setuid program and `sudo` alike: what counts is the
effective uid the process holds when it starts, and `sudo` handling stays open (section 5). A command with no
verb is not refused either: it only shows the version, as `--version` does, so the ruling's "every verb"
covers it, since it runs no verb.

**The repair verb, as built** (`src/lucille_host/owners.py`, `cli._profile_check`; spec/CLI.md section 17
and the type `profile_check`; `tests/test_profile_check.py`). Section 5 left open whether the detection
and repair belong in the host at all. It is built in the smallest form that answers 4.3 and changes
nothing on its own:
- `lucille-host profile check`, run as the user, **finds and says**; it never repairs. The repair needs
  root, which the host never is (as root, the verb is refused like every other). It takes no lock, sends
  nothing, and writes nothing but its line in the run record, and it exits 0 whatever it finds. Writing
  that line, as every command does (`runs.record`, through `paths.ensure_dir`), makes the state folder and
  each missing folder above it, each at mode 0700, and sets the state folder to mode 0700 where it was
  there already, whatever mode it had.
- It resolves the profile's folders with `paths.dirs`, as every verb does (an absolute `XDG_*_HOME`, a
  relative one ignored, a relative `HOME`, the macOS layout, where the state folder is inside the config
  folder and is looked at once), then follows every symbolic link (`os.path.realpath`), since the host
  follows them too.
- For each folder it looks up the chain, from the profile to the first folder the user owns, never at `/`
  and, for a folder inside the home folder, never at the home folder or above, and reports the highest folder another uid owns, which `chown -R` of repairs the
  rest (4.3: "the highest folder shown owned by root is the one to repair"). A folder the user cannot look
  at is reported as `unreadable` only when no folder above it is another uid's (4.3: below a root-owned
  folder that is "expected and is not a second finding"). Where the profile folder is the user's, it looks
  at every folder and file inside it (`policy.toml` among them). It follows no symbolic link into a
  folder, and a link's own owner does not matter; what a link points to, which the host follows, is
  looked at and reported by its own path, so that a `policy.toml` linked to a root-owned file names that
  file. A link to the home folder or a folder above it (`/`, or a home folder root owns) is not reported:
  root owning one is not what a run as root leaves, and a link inside the profile does not make it the
  profile's. That rule is for link targets only. The walk up from a profile folder outside the home folder
  (a custom `XDG_*_HOME`) reports the highest folder another uid owns as `outside_home` even where that
  folder holds the home folder (`/home/cfg` beside `/home/u`), since the host is refused below it. Any
  finding that is the home folder itself is `outside_home`, never a `chown -R` of all of it.
- Inside the home folder, each finding carries two command lines for a person to run as root: `ls -lR`
  (`ls -l` for a file) first, as "Look first" asks, then `chown -R <uid>` (`chown -h <uid>` for a file, so
  that a file swapped for a symbolic link before root runs it changes the link and not what it points
  to). The path is always in single quotes. The user is named by **uid**, not by name: that needs no user database (the
  runtime's import allowlist has no `pwd`), and is exact. The group is left as it is, which 4.3 says does
  no harm.
- **Outside the home folder** (a custom `XDG_*_HOME` there, or a link that points there) it reports what it
  finds with **no** command (`outside_home`). 4.3 lists "anything outside `HOME`" as not checked, and
  nothing tells the host whether another program needs such a folder as it is: a person decides.
- The verbs other than `run` that a profile's permissions refuse (a `PermissionError` on one of its folders,
  on a folder above one, or on a file inside one, or a `PolicyError` or `StateError` raised from one) add a
  pointer to the verb to their message: "if an earlier run as root left it so, `lucille-host profile check`
  names what to repair". The code is unchanged. `run`'s `stopped` objects carry no message, so `run` has
  none.
- Tests: planted owners, since only root can make a root-owned file and the suite never runs as root. They
  replace only `owners._uid`, which reads the owner out of a `stat` result, and plant by device and inode,
  so every `stat` and `lstat` is real: a link and what it points to are told apart, and a folder with no
  permissions refuses a `stat` below it for real, as a root-owned 0700 folder does. They cover a
  root-owned file, a root-owned profile folder, a root-owned chain the user cannot look into, walked up to
  its highest folder, and a walk that reaches the home folder and stops there; the default layout, a
  custom `XDG_STATE_HOME` inside and outside the home folder, a relative `XDG_CONFIG_HOME` and a relative
  `HOME`, symbolic links inside and outside the home folder and inside the profile (a link to a root-owned
  folder or file, one that points at nothing, one that cannot be looked at), the macOS layout, a quote in a
  path, and a profile the user owns, which passes. Each of these mutations of the check fails a test: no
  stop at the home folder, a stop at the first folder that cannot be looked at, an unreadable folder put
  before a root-owned one above it, `stat` for `lstat` inside the profile, and either arm of the pointer's
  path test. Not run here: a real root-owned profile on a real system (nothing here runs as root); the
  review of the change made some as root on Linux, and the verb named the right folder each time. Run by
  hand on macOS; CI runs the tests on Linux and macOS.

For B or C, add to the above: a privilege drop in `_exec.py:235-243` and its tests; a root-aware
`policy.is_broad` (or a rule that a root caller's "home" is every home); the path conditions in `paths.py`;
and a new row in `CLAIMS.md` that says what the host promises when run as root.

## 5. Not decided here

- Whether a file-held credential store should exist at all (0003). Without it, an add-on cannot use
  `github.read` or the Gmail operations under any option above.
- How an add-on confirms a pairing (5 in 3.2).
- How an add-on's image drops privileges for the service (A): the S6 pages read describe a oneshot setup step
  and the user bundle, and no step that starts a service as another user; whether the base images carry a
  tool for it is not checked.
- For the ruling: whether the detection and repair for a profile root has touched belong in the host, as
  one verb run as the user, built and tested in the implementation change after the ruling (4.3), or are
  left to the default-layout advice in 4.3 and a person's judgement. This record gives that advice for the
  default layout only and lists, in 4.3, what is not checked. **Built since**, in its smallest form: the
  detection, with the repair printed for a person and never run (`profile check`, "The repair verb, as
  built", 4.3). Not ruled on: whether the host should ever do more than print it.
- Whether to document `sudo` use (`sudo` may keep the user's `HOME`, in which case the files would be the
  user's but made by root). Not tested.
