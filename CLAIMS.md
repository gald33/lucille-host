# Claims and their proof

This file takes each sentence of the claim in [PRIVACY.md](PRIVACY.md) and says what backs it.

**Status: no release exists.** Version `0.1.0.dev0` has the catalog, the executor, the local policy, the
audit log and the local controls. It cannot pair with Lucille or read a request yet, so many tests named
below do not exist yet.

**How to read it.**
- **Code** names the file that makes the sentence true. "Not written yet" means the code does not exist.
- **Tests** names each test that would fail if the sentence became false.
  - A path such as `tests/guards/test_ast_lint.py` is a test that exists and runs in CI today.
  - An item marked **(pending)** is a test named before it exists. It is either an invariant from the
    table "Host invariants and their tests" in [decisions/0001-host-design.md](decisions/0001-host-design.md),
    quoted as invariant "...", or a test name chosen here (`test_...`), which may change when the test
    is written; this file changes with it.
- **Lucille policy as of 2026-10-27; not proven by this repository** marks a sentence, or the part of
  one, that is about what Lucille's backend does. Nothing in this repository can test it.
- Some sentences have a host part and a Lucille part. Each part is mapped on its own.

The existing guard tests prove that each guard catches a planted violation in the forms it lists. The
tests of the workflow, AST and Switchboard-surface lints also show that this repository is clean today.
The private-string test does not: it checks the repository against a synthetic list, and only the CI
step "Private-string check", run with the repository secret, shows that no private string is committed.
None of these tests proves the behaviour of code that does not exist yet. The guards catch accidental
and casual violations, not a determined author's; a maintainer's review of every pull request is the
backstop ([VERIFYING.md](VERIFYING.md) says what each guard cannot see).

## Paragraph 1: what the host can do

**C1.** "lucille-host does only what you switch on, on your computer."
- Code: `src/lucille_host/ops/__init__.py` (the closed operation set: three entries in an immutable
  mapping); `src/lucille_host/_exec.py` (the only place the program may start a process);
  `src/lucille_host/policy.py` (the local policy file; with no file, only `host.describe` is enabled).
- Tests: `tests/guards/test_ast_lint.py` (no process-spawning call in the runtime package outside
  `_exec.py`, in the forms the lint lists, and no import there of a module outside the lint's
  allowlist, as the lint resolves imports and the re-exports it follows; with the limit stated under
  C2); `tests/test_catalog.py` (`test_catalog_entries`: the operation set is exactly three entries and
  read-only); `tests/test_policy.py` (`test_fresh_policy`: only `host.describe` is on after install);
  invariant "Nothing Lucille sends can widen what the host permits" (pending, conformance).
- Note: the AST lint reads this repository's code, not its dependencies'. agent-switchboard 2.6.0's
  `switchboard/client.py`, which the runtime imports, can run `git` to detect an agent's identity.
  Whether the host's own calls reach that path is not checked yet; invariant "The host's stores and
  Switchboard footprint are its own" (pending: no subprocess from pairing) is the test for it.

**C2.** "It can run only the operations built into this release (listed in `src/lucille_host/ops/` and by
`lucille-host ops list`), and only on folders you add."
- Code: `src/lucille_host/ops/__init__.py`; the `ops list` and `roots add` verbs in
  `src/lucille_host/cli.py`; `check` in `src/lucille_host/jobs.py` (an operation must be in the catalog
  and enabled, its `root` must be an alias the policy holds, and its path must stay inside that folder,
  as `src/lucille_host/containment.py` resolves it).
- Tests: `tests/guards/test_ast_lint.py` (no `eval`, `exec`, `compile` or dynamic import anywhere, in
  the forms the lint lists, so no operation can be loaded from outside the release by any of them; and,
  in the runtime package, no import of a module outside the lint's allowlist, as the lint resolves
  it); `tests/test_catalog.py` (`test_catalog_entries`; `test_steps_5_to_9_hold_to_the_request_fixtures`:
  an unknown op, a not-enabled op, bad arguments and each path escape in the spec's fixtures get the
  refusal the spec expects); `tests/test_exec.py` (`test_exec_symlink_escape`);
  `tests/test_containment.py` (links that lead out, past PATH_MAX included; a tree deeper than
  PATH_MAX; every lookup naming a single component; agreement with `realpath` wherever a path
  resolves; `..` and absolute link targets resolved from the right directory; a lookup error, a
  directory the host may only search, and too many lookups each refused; FIFOs, sockets and devices
  refused; no path through `/proc`; no path into the host's own files, hard links and linked
  folders included); `tests/test_jobs.py` (`test_unheld_root_is_path_outside_root`); invariant "Nothing Lucille sends can widen what the
  host permits" (pending, conformance: an unknown op, a not-enabled op, traversal, an absolute path and
  a symlink escape are each refused with zero spawns, through the installed host).
- Limit: the AST lint reads the forms written out in a file. Its rules for the whole repository are a
  denylist of known forms, not a sandbox: a form it does not name passes. In the runtime package it
  also holds imports to an allowlist, and checks a module that an allowed standard-library module or
  one of this package's modules re-exports as the module it is, but it cannot keep an allowed module
  from doing what it does inside. It does not follow values returned by calls, a module re-exported
  where it does not look (by a module outside this package and outside its table of the runtime's
  standard-library modules, under a name it does not watch, or by one of this package's modules
  other than by an import or a plain assignment), `getattr` passed as a value or given a name that
  is not a string literal, `vars(<object>)`, format-string field lookups, or code in the standard
  library or a dependency that imports or runs something on the code's behalf. Its docstring and
  [VERIFYING.md](VERIFYING.md) name these blind spots; review covers the rest.

**C3.** "Nothing Lucille sends can add an operation, add a folder, or loosen a limit."
- Code: the local policy file, `src/lucille_host/policy.py`, changed only by the local CLI verbs `ops
  enable|disable` and `roots add|remove`, and failing closed when invalid; the protocol, which has no
  field that maps onto local policy ([spec/PROTOCOL.md](spec/PROTOCOL.md) §3 and §10), and whose JSON
  Schemas in `src/lucille_host/spec/v1/` set `additionalProperties: false` at every level. The host checks
  a request's `args` against its operation's closed schema (`args_valid` in
  `src/lucille_host/ops/__init__.py`); its check of the rest of the envelope is not written yet.
- Tests: `tests/spec/test_schemas.py` (every object in every schema is closed) and
  `tests/spec/test_fixtures.py` (requests carrying `argv`, `exe`, `env`, `cwd`, `timeout` or
  policy-shaped keys are invalid and expect `bad_request`; request `caps` above the host's own leave the
  host's own in force). These test the spec, not the host. Invariant "Nothing Lucille sends can widen
  what the host permits" (pending, conformance:
  policy- and update-shaped fields are refused and the policy digest is unchanged; `caps` above the
  host's own never raise the effective cap); `tests/test_policy.py` (`test_upgrade_disables_changed_entry`,
  `test_invalid_policy_fails_closed`); `tests/test_catalog.py` (`test_catalog_params_equal_spec_args`,
  `test_args_valid_agrees_with_the_spec_schema`); invariant "An upgrade never runs a changed op under old
  consent" (pending, conformance).

**C4.** "It never runs code, a shell or a command line sent by Lucille, and the programs it starts get none
of its keys and none of your environment variables."
- Code: `src/lucille_host/_exec.py` (argv only, `shell=False`, the environment `CHILD_ENV` and nothing
  else, stdin `/dev/null`, each child in its own session); the catalog's argv templates in
  `src/lucille_host/ops/__init__.py` (an absolute executable; each string from a request after `--`, as
  one whole argv element).
- Tests: `tests/guards/test_ast_lint.py` (no `shell=True`, `os.system`, `eval`, `exec`, `compile` or
  dynamic import; a process launcher only called directly, with argv its only positional argument; one
  spawn site; with the limit stated under C2); `tests/test_exec.py` (`test_exec_env_allowlist`: a child
  dumps its environment, which is exactly `PATH`, `LANG` and `LC_ALL`, and a canary variable is absent;
  `test_exec_dash_file`: files named `--version` and `-` are hashed, not read as an option or as
  standard input);
  `tests/test_catalog.py` (`test_catalog_entries`: every executable is absolute and every string
  placeholder follows `--`); invariant "No shell; one spawn site; children get a built environment and
  no key" (pending, conformance); invariant "Nothing Lucille sends can widen what the host permits"
  (pending: extra keys `exe`, `argv`, `env`, `cwd` and `timeout` are refused).
- Note: "get none of its keys" means the host passes them no key. An enabled operation that reads a
  folder holding the host's own state could still read what is stored there. That is inferred from the
  design, not tested. [PRIVACY.md](PRIVACY.md) says that keys leave if they are inside a file an enabled
  operation reads.

## Paragraph 2: what stays on your computer

**C5.** "Any file no enabled operation reads stays on your computer."
- Code: the reply is built only from an operation's output and the fields of `host.describe`: the reply
  schema, `src/lucille_host/spec/v1/reply.schema.json`, allows three closed shapes and no other field;
  `reply_body` in `src/lucille_host/jobs.py` builds them; `src/lucille_host/ops/`.
- Tests: invariant "Nothing Lucille sends can widen what the host permits" (pending: a refused request
  spawns nothing); `tests/spec/test_fixtures.py` (a reply carrying any other field, such as error text,
  is invalid; this tests the spec); `tests/test_jobs.py` (`test_replies_of_each_shape_hold_to_the_schema`:
  the replies the host builds hold to that schema).
- Partly proven by review only. No test can show that no other code path sends a file. A reader checks
  that by reading `src/lucille_host/`, which the guards keep small: one spawn site and one Switchboard
  module.

**C6.** "So do your passwords, keys and browser sessions, unless they are inside a file an enabled operation
reads."
- Code and tests: as for C5. The redaction pass in `src/lucille_host/_output.py` is not relied on here;
  it is best effort.

## Paragraph 3: what leaves your computer

**C7.** "What leaves it: each request Lucille makes (which operation, and its inputs, such as a file name or
a search pattern), and what an enabled operation returns, up to 64 KiB of output and 64 KiB of errors."
- Code: the output caps in `src/lucille_host/_output.py`, applied by `src/lucille_host/_exec.py`; the
  request and reply schemas in `src/lucille_host/spec/v1/`, and the rules `stream-size` and `raw-ceiling`
  in [spec/PROTOCOL.md](spec/PROTOCOL.md) §13: each stream's JSON-encoded string is at most 64 KiB, which
  holds at most 64 KiB of output.
- Tests: `tests/spec/test_schemas.py` and `tests/spec/test_fixtures.py` (a stream at the cap is valid, one
  byte over it is not; these test the spec, not the host); `tests/test_exec.py` (`test_exec_caps`: 1 MiB
  of output comes back truncated and flagged, its JSON string exactly 64 KiB;
  `test_exec_raw_ceiling_drops_the_rest_and_flags_it`; `test_request_caps_never_raise_the_host_cap`); `tests/test_output.py`; invariant "Output is
  bounded, decodable and path-free" (pending, conformance: the serialized reply stays under 256 KiB).

**C8.** "`files.search` returns lines of the files it searches."
- Code: the `files.search` catalog entry (not written yet).
- Tests: `test_search_returns_file_lines` (pending: a search reply carries the matching line).

**C9.** "Lucille also learns the host's version, OS family, enabled operation names and folder aliases (never
folder paths), each run's exit code and duration, a fingerprint that changes when your local policy
changes, and, to within a day, when lucille-host was last running and when your computer last answered."
- Code: `src/lucille_host/__init__.py` (`__version__`); `describe_document` and `reply_body` in
  `src/lucille_host/jobs.py` (`host.describe` and the reply fields `host_version`, `exit_code`,
  `duration_ms` and `policy_digest`); `digest` in `src/lucille_host/policy.py`; resolving the pair at
  least every 24 hours (not written yet).
- Tests: `tests/test_cli.py` (the program reports its version); `tests/test_jobs.py`
  (`test_host_describe_document_in_stdout`: `host.describe` returns aliases, never the root's path, its
  real path or `$HOME`); `tests/test_policy.py` (`test_policy_digest_not_plain_hash`);
  `tests/test_exec.py` (`test_files_sha256_runs_in_the_root`: the reply holds no root path); invariant
  "Output is bounded, decodable and path-free" (pending, conformance: no reply contains a root's realpath
  or `$HOME`). The 24-hour resolve has no named test yet (pending).

## Paragraph 4: transport

**C10.** "Requests and replies travel through Lucille's Switchboard hub, encrypted between your computer and
Lucille's backend."
- Code: `src/lucille_host/_switchboard.py` (the one runtime module that imports `switchboard`; stubs in
  `0.1.0.dev0`); the hub check in `pair` (not written yet).
- Tests: `tests/guards/test_switchboard_lint.py` (`switchboard` is imported in one runtime module only,
  as the lint resolves names, through each file's imports and through that module; no `register` or
  invite is used anywhere, and no `share_new` outside the conformance package and the tests); `tests/test_import_graph.py` (the runtime never imports the hub server's `fastapi` or
  `uvicorn`); `test_pair_refuses_foreign_hub`, `test_pair_refuses_code_with_token` and
  `test_pair_refuses_unconfigured_loopback` (pending); invariant "No listening socket" (pending).
- Not proven here: the encryption itself is agent-switchboard 2.6.0's, not this repository's. That the
  far end is Lucille's backend is Lucille policy as of 2026-10-27; not proven by this repository.

**C11.** "The hub, and the network in front of it, can see your computer's network address, when it
connects, and roughly how large each message is; they cannot read the messages."
- Host part. Code: every job meaning is inside the sealed body, with message type `note` and no thread
  ([spec/PROTOCOL.md](spec/PROTOCOL.md) §5; the host's read loop and the code that posts replies are not
  written yet). Tests: pending. The golden fixtures in `src/lucille_host/spec/v1/fixtures/` cover message
  bodies only, not the Switchboard message around them.
- Not proven here: sealing and size padding are agent-switchboard 2.6.0's (its `switchboard/crypto.py`).
  This repository pins that release exactly; it does not test it.

**C12.** "Lucille's backend can."
- Lucille policy as of 2026-10-27; not proven by this repository. It is a limit, not a protection.

**C13.** "A request your computer has not answered within its wait (an hour unless chosen otherwise, never
more than 23 h 45 min) expires and the job fails."
- The wait, its default, its cap and the job failing: Lucille policy as of 2026-10-27; not proven by
  this repository.
- Host part. Code: the expiry check and the reply's TTL (not written yet). Tests: invariant "One run per
  job id, never after `not_after`" (pending: an expired request is refused with no spawn);
  `test_reply_ttl_explicit` (pending).

## Paragraph 5: what Lucille keeps

**C14.** "Lucille stores what is returned with the job."
- Lucille policy as of 2026-10-27; not proven by this repository.

**C15.** "Lucille's operators, Lucille's assistant and the AI provider it uses can read it."
- Lucille policy as of 2026-10-27; not proven by this repository. It is a limit, not a protection.

**C16.** "Lucille blanks it 24 hours after the job ends."
- Lucille policy as of 2026-10-27; not proven by this repository. The claim assumes the blank is in
  effect, and this repository cannot tell whether it is.

**C17.** "That is Lucille's policy, not code in this repository, and it does not reach Lucille's database
backups."
- Lucille policy as of 2026-10-27; not proven by this repository. The sentence says so itself.

## Paragraph 6: your controls

**C18.** "Every request your computer receives, what it decided, and a fingerprint of what it sent back are
in a log on your computer (`lucille-host log`)."
- Code: the audit log, `src/lucille_host/audit.py`, written by `process` in `src/lucille_host/jobs.py`;
  the `log` verb (not written yet), and the read loop that will call `process` (not written yet).
- Tests: `tests/test_audit.py` (`test_audit_order`: each line is written before the act it records, by
  fault injection between steps; `test_audit_start_line_is_written_before_the_child_starts`;
  `test_each_audit_line_is_synced_before_the_act_it_records`;
  `test_audit_lines_record_what_the_design_names`, including the sha256 and size of the exact bytes to be
  posted); `tests/test_jobs.py` (`test_received_is_written_before_deciding`).

**C19.** "`lucille-host pause` stops it starting anything, even offline, and `lucille-host kill` also stops
what is running."
- Code: the pause flag file and kill request in `src/lucille_host/controls.py`, and the `pause`,
  `resume` and `kill` verbs in `src/lucille_host/cli.py`; `check` in `src/lucille_host/jobs.py` refuses
  `paused`, `execute` reads the flag again just before the start, and `_exec.run` polls for a kill. The
  read loop that will run them is not written yet.
- Tests: `tests/test_controls.py` (`test_pause_refuses_and_resume_clears`,
  `test_kill_kills_the_running_group_and_pauses`, `test_first_sigterm_or_sighup_drains`,
  `test_second_ctrl_c_kills`, `test_each_control_waits_for_the_controls_lock`,
  `test_the_controls_work_where_the_filesystem_cannot_lock`,
  `test_no_order_of_a_kill_and_a_resume_leaves_a_kill_request_alone`,
  `test_an_ignored_sighup_stays_ignored`, `test_the_signal_handler_takes_no_lock`); `tests/test_jobs.py`
  (`test_draining_starts_nothing_and_records_nothing`, `test_a_pause_after_the_decision_stops_the_start`,
  `test_a_drain_signal_after_the_decision_stops_the_start`);
  `tests/test_exec.py` (`test_exec_timeout_kills_group`, `test_a_user_kill_takes_the_whole_group`,
  `test_no_child_outlives_an_exception_in_the_wait`).
- Limit: only a running host enforces a kill or a time limit. An operation running when the host itself
  is killed or crashes keeps running without either (THREAT_MODEL.md, "Known gaps").

**C20.** "Removing the computer in Lucille stops Lucille using the pairing at once."
- Lucille policy as of 2026-10-27; not proven by this repository.

**C21.** "Lucille then deletes its half and tries once, best effort, to tell your computer through its
Switchboard hub; Lucille's page says if your computer was not told."
- Lucille policy as of 2026-10-27; not proven by this repository.
- Host part, when it is told. Code: resolve handling `ended_by_peer` (not written yet). Tests:
  `test_ended_by_peer_deletes_local_copy` (pending).

**C22.** "If Lucille cannot use its hub at all for 7 days, it deletes its half without telling your
computer."
- Lucille policy as of 2026-10-27; not proven by this repository.

**C23.** "`lucille-host forget` deletes the computer's half at once, and tells Lucille if the hub is
reachable then."
- Code: the `forget` verb, through `pairing.forget` in `src/lucille_host/_switchboard.py` (not written
  yet). `pairing.forget` deletes the local record first and then tells the other side, best effort.
- Tests: `test_forget_reports_whether_told` (pending).

**C24.** "A side that is never told keeps its half until it is removed or forgotten there too."
- Host part. Code: the host deletes its half only on `forget`, on being told the pair ended, or when the
  local SAS gate is rejected or runs out (not written yet). Tests: `test_ended_by_peer_deletes_local_copy`
  (pending).
- Lucille's half: Lucille policy as of 2026-10-27; not proven by this repository.

**C25.** "The source is public at github.com/gald33/lucille-host."
- This repository. Check it by opening it.
- That a release was built from this source: `tests/guards/test_workflow_lint.py` (workflows use
  GitHub-hosted runners only, and every action is pinned by commit SHA), and the provenance check in
  [VERIFYING.md](VERIFYING.md), step 2 (pending; no release exists).

## Guards that back no single sentence

These existing tests back the design rather than one sentence of the claim.

| Test | What it backs |
|---|---|
| `tests/guards/test_private_strings.py` | The CI step "Private-string check", run with the repository secret, is what checks that no string private to Lucille is committed here; this test shows that the check finds a planted string, never prints it, and fails closed without the list. Its own check of this repository uses a synthetic list, so it passes whatever is committed. |
| `tests/guards/test_switchboard_lint.py` | Part of invariant "The host never invites anyone into its pair's rooms, and offers no room of its own": the source names no invite and no `register`, and, outside the conformance package and the tests, no `share_new` and no Switchboard function outside the listed surface (so no `pairing.start`, `heal` or `export`). It does not cover a method called on a client object, other than `register`, invites and private members, or a value a wrapper function returns; review, and the pending invariant test, cover that. |
| `tests/guards/test_workflow_lint.py` | Invariant "Every release comes from a hosted runner and environment `pypi`" (the hosted-runner half; that a release publishes from environment `pypi` is checked by the provenance step in [VERIFYING.md](VERIFYING.md), not by this test). |
| `tests/test_import_graph.py` | The runtime imports no hub server. (That the base install has none is `pyproject.toml`'s dependency list: the server comes only with the `[conformance]` extra.) The README's "opens no listening port" also needs the pending socket test. |
| `tests/test_cli.py` | `lucille-host` and `lucille-host --version` print the version, and the version has one source. |
| `tests/spec/` | The protocol spec, [spec/PROTOCOL.md](spec/PROTOCOL.md): every object in its schemas is closed and every value bounded; each fixture validates, or fails, as labelled, and each request fixture expects the reply the spec's refusal order gives; the wheel ships the schemas and fixtures, and an installed copy loads them. These test the spec. The host's own checks of steps 5 to 9 are held to the request fixtures by `tests/test_catalog.py`, and its decoding to the decode fixtures by `tests/test_output.py` and `tests/test_exec.py`; its check of the envelope is not written yet. |
| `tests/test_skeleton.py` | What `0.1.0.dev0` still leaves to later releases: the Switchboard calls `_switchboard.py` names exist in the pinned library, and the conformance suite with no scenarios exits 1 rather than reporting a pass. |
