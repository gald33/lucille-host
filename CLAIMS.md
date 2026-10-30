# Claims and their proof

This file takes each sentence of the claim in [PRIVACY.md](PRIVACY.md) and says what backs it.

**Status: no release exists.** Version `0.1.0.dev0` has the catalog, the executor, the local policy, the
audit log, the local controls, the stores and Switchboard client set-up, and pairing: `lucille-host pair`
answers Lucille's code and keeps the local gate. `lucille-host run` keeps the pair once the gate is open,
takes the work rooms Lucille offers over it by the host's accept policy, and reads and answers Lucille's
requests there, tested against Switchboard's in-process hub and a stand-in for Lucille. `status` shows
what the host holds and what `run` last saw, with no hub call; `log` prints the audit log; `forget` ends
the pair on the computer. Some tests named below do not exist yet: the conformance suite's,
`files.search`'s, and a release's provenance check.

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
  `src/lucille_host/policy.py` (the local policy file; with no file, only `host.describe` is enabled,
  and a new pair starts from that fresh policy, the earlier one set aside unused).
- Tests: `tests/guards/test_ast_lint.py` (no process-spawning call in the runtime package outside
  `_exec.py`, in the forms the lint lists, and no import there of a module outside the lint's
  allowlist, as the lint resolves imports and the re-exports it follows; with the limit stated under
  C2); `tests/test_catalog.py` (`test_catalog_entries`: the operation set is exactly three entries and
  read-only); `tests/test_policy.py` (`test_fresh_policy`: only `host.describe` is on after install);
  `tests/test_pair.py` (`test_new_pair_starts_from_fresh_policy`,
  `test_previous_policy_archived_not_applied`: only `host.describe` is on after a new pair, and the
  policy set aside is listed by `lucille-host policy previous` but not applied); invariant "Nothing
  Lucille sends can widen what the host permits" (pending, conformance).
- Note: the AST lint reads this repository's code, not its dependencies'. agent-switchboard 2.6.0's
  `switchboard/client.py`, which the runtime imports, can run `git` to detect an agent's identity.
  `tests/test_store.py` (`test_no_write_under_home_switchboard`) makes the pairing and sharing calls the
  host will make (accept a code, resolve, touch, accept an offered room, look it up, leave it, forget the
  pair), the way it will make them (`base=None`, its own stores and its own client factory), against the
  library's in-process hub, and an audit hook records that none of them starts a process.
  `tests/test_pair.py` (`test_pairing_spawns_nothing`) does the same through the `pair` verbs (taking
  a code, waiting for Lucille to collect it, `pair status`, `pair confirm` and `pair reject`), and
  `tests/test_rooms.py` (`test_no_write_under_home_switchboard`) through the logic that takes work
  rooms (touching, accepting an offered room, leaving it after a heal, taking the replacement, and
  forgetting the pair), and `tests/test_read_loop.py` (`test_pairing_and_read_loop_spawn_nothing`)
  through `run`: pairing, taking a room, answering a `host.describe` request, a heal, and the
  replacement room taken and used. `tests/test_end_to_end.py` (`test_no_write_under_home_switchboard`)
  does the same through the commands a user runs, from `pair` and `pair confirm` through `run`, a room
  taken and a request answered, to `status`, `forget` and `log`.

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
  enable|disable` and `roots add|remove`, and by `pair`, which only sets it back to the fresh policy
  when it takes a new code; it fails closed when invalid; the protocol, which has no
  field that maps onto local policy ([spec/PROTOCOL.md](spec/PROTOCOL.md) §3 and §10), and whose JSON
  Schemas in `src/lucille_host/spec/v1/` set `additionalProperties: false` at every level. The host checks
  a request's `args` against its operation's closed schema (`args_valid` in
  `src/lucille_host/ops/__init__.py`), and the rest of the envelope (`early_code` in
  `src/lucille_host/envelope.py`): a key the envelope does not list is `bad_request`, and is never
  interpreted.
- Tests: `tests/spec/test_schemas.py` (every object in every schema is closed) and
  `tests/spec/test_fixtures.py` (requests carrying `argv`, `exe`, `env`, `cwd`, `timeout` or
  policy-shaped keys are invalid and expect `bad_request`; request `caps` above the host's own leave the
  host's own in force). These test the spec, not the host. Invariant "Nothing Lucille sends can widen
  what the host permits" (pending, conformance:
  policy- and update-shaped fields are refused and the policy digest is unchanged; `caps` above the
  host's own never raise the effective cap); `tests/test_policy.py` (`test_upgrade_disables_changed_entry`,
  `test_invalid_policy_fails_closed`); `tests/test_catalog.py` (`test_catalog_params_equal_spec_args`,
  `test_args_valid_agrees_with_the_spec_schema`); `tests/test_envelope.py`
  (`test_steps_1_to_4_hold_to_the_request_fixtures`: the host's own envelope check gives each request
  fixture the reply, or the silence, that the spec's order gives, extra keys such as `argv` and `exe`
  included); `tests/test_read_loop.py` (`test_refusal_order`: requests carrying `argv` or `exe` are
  refused through `run`, and nothing refused starts a process); `tests/test_pair.py`
  (`test_new_pair_starts_from_fresh_policy`: a new pair starts with only `host.describe` enabled and no
  folder); invariant "An upgrade never runs a changed op under old consent" (pending, conformance).

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
  no key" (pending, conformance); `tests/test_envelope.py`
  (`test_steps_1_to_4_hold_to_the_request_fixtures`: requests carrying `exe`, `argv`, `env`, `cwd` or
  `timeout` are refused `bad_request` by the host's own check); invariant "Nothing Lucille sends can
  widen what the host permits" (pending, conformance: the same through the installed host).
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
  least every 24 hours (`Session.step` in `src/lucille_host/link.py`, which `run` drives:
  `src/lucille_host/loop.py`).
- Tests: `tests/test_cli.py` (the program reports its version); `tests/test_jobs.py`
  (`test_host_describe_document_in_stdout`: `host.describe` returns aliases, never the root's path, its
  real path or `$HOME`); `tests/test_policy.py` (`test_policy_digest_not_plain_hash`);
  `tests/test_exec.py` (`test_files_sha256_runs_in_the_root`: the reply holds no root path); invariant
  "Output is bounded, decodable and path-free" (pending, conformance: no reply contains a root's realpath
  or `$HOME`); `tests/test_rooms.py` (`test_resolves_at_least_daily`: with nothing else asking, the
  session resolves the pair again a day after the last resolve).

## Paragraph 4: transport

**C10.** "Requests and replies travel through Lucille's Switchboard hub, encrypted between your computer and
Lucille's backend."
- Code: `src/lucille_host/_switchboard.py` (the one runtime module that imports `switchboard`: the
  host's stores, its client factory and the pairing and sharing calls); the hub check in `pair`
  (`check_code` in `src/lucille_host/link.py`: a code is answered only if it names Lucille's hub, or the
  loopback hub `pair --hub` names, and carries no hub token); checks 3 and 4 of the accept policy for a
  work room (`failed_check` in `src/lucille_host/rooms.py`, run by `Session` in
  `src/lucille_host/link.py`: a room whose hub is not the one the pairing code named, or whose offer
  carried a hub token, is left at once).
- Tests: `tests/guards/test_switchboard_lint.py` (`switchboard` is imported in one runtime module only,
  as the lint resolves names, through each file's imports and through that module; no `register` or
  invite is used anywhere, and no `share_new` outside the conformance package and the tests); `tests/test_import_graph.py` (the runtime never imports the hub server's `fastapi` or
  `uvicorn`); `tests/test_pair.py` (`test_pair_refuses_foreign_hub`, `test_pair_refuses_code_with_token`,
  `test_pair_refuses_unconfigured_loopback`, `test_pair_accepts_configured_loopback`: each refused code
  is never answered, and changes neither the policy nor the profile's configuration;
  `test_pair_compares_the_hub_as_a_string`: another spelling of Lucille's hub, by scheme, case, port,
  path, query or fragment, is another hub);
  `tests/test_rooms.py` (`test_check_3_or_4_failure_leaves_at_once`: such a room is left as soon as it
  is accepted, and no client is ever built for it); `tests/test_read_loop.py`
  (`test_no_listening_socket`: an audit hook sees no socket bound while the host pairs, runs and answers
  a request; it would not see one bound by code outside Python's own socket module).
- Not proven here: the encryption itself is agent-switchboard 2.6.0's, not this repository's. That the
  far end is Lucille's backend is Lucille policy as of 2026-10-27; not proven by this repository.

**C11.** "The hub, and the network in front of it, can see your computer's network address, when it
connects, and roughly how large each message is; they cannot read the messages."
- Host part. Code: every job meaning is inside the sealed body, with message type `note` and no thread
  ([spec/PROTOCOL.md](spec/PROTOCOL.md) §5): `RoomClient.post` in `src/lucille_host/_switchboard.py`, which
  the read loop in `src/lucille_host/loop.py` posts replies with. Tests: `tests/test_read_loop.py`
  (`test_reply_is_note_without_thread`: Lucille reads the host's reply as a note with no thread;
  `test_non_note_or_threaded_ignored`: a message of another type, or with a thread, is logged and not
  answered). The golden fixtures in `src/lucille_host/spec/v1/fixtures/` cover message bodies only, not
  the Switchboard message around them.
- Not proven here: sealing and size padding are agent-switchboard 2.6.0's (its `switchboard/crypto.py`).
  This repository pins that release exactly; it does not test it.

**C12.** "Lucille's backend can."
- Lucille policy as of 2026-10-27; not proven by this repository. It is a limit, not a protection.

**C13.** "A request your computer has not answered within its wait (an hour unless chosen otherwise, never
more than 23 h 45 min) expires and the job fails."
- The wait, its default, its cap and the job failing: Lucille policy as of 2026-10-27; not proven by
  this repository.
- Host part. Code: the expiry check, `early_code` in `src/lucille_host/envelope.py`, made when the
  request is decided; the reply's TTL, `reply_ttl` there; the journal of job ids,
  `src/lucille_host/journal.py`. Tests: `tests/test_read_loop.py` (`test_refusal_order`: an expired
  request is refused `expired` and starts no process; `test_reply_ttl_explicit`: a reply lives
  `(not_after - now) + 900` s, at most a day, passed explicitly; `test_reply_ttl_without_valid_not_after`;
  `test_duplicate_runs_once`, in one room and across two; `test_restart_gives_interrupted`: a restart
  between the decision to run and the reply gives `failed interrupted`, with no second run);
  `tests/test_envelope.py` (`test_steps_1_to_4_hold_to_the_request_fixtures`). An operation that started
  in time can still finish after `not_after`: the check is made once, before the start.

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
- Code: the audit log, `src/lucille_host/audit.py`, written by `process` in `src/lucille_host/jobs.py`,
  which the read loop calls for each request (`src/lucille_host/loop.py`), and by the read loop for each
  message it does not answer and each reply it posts or gives up; the `log` verb in
  `src/lucille_host/cli.py`, which prints the log's last lines, oldest first, across its rotated files
  (`tail` in `src/lucille_host/audit.py`).
- Tests: `tests/test_audit.py` (`test_audit_order`: each line is written before the act it records, by
  fault injection between steps; `test_audit_start_line_is_written_before_the_child_starts`;
  `test_each_audit_line_is_synced_before_the_act_it_records`;
  `test_audit_lines_record_what_the_design_names`, including the sha256 and size of the exact bytes to be
  posted; `test_threads_writing_at_once_lose_no_line`); `tests/test_jobs.py`
  (`test_received_is_written_before_deciding`); `tests/test_read_loop.py`
  (`test_restart_refusal_reposted_unchanged`: a refusal posted again after a restart is the same bytes,
  with the same fingerprint in the log); `tests/test_status.py` (`test_log_prints_last_lines`: the last
  lines, oldest first, across the rotated files, a line still being written left out, and no control
  character printed); `tests/test_end_to_end.py` (`test_end_to_end_pair_run_forget`: through the
  commands, the log holds the request's `received` and `decision` lines and the reply posted, and the
  fingerprint in its `sent` line is that of the reply Lucille read).

**C19.** "`lucille-host pause` stops it starting anything, even offline, and `lucille-host kill` also stops
what is running."
- Code: the pause flag file and kill request in `src/lucille_host/controls.py`, and the `pause`,
  `resume` and `kill` verbs in `src/lucille_host/cli.py`, each dispatched by name; `check` in `src/lucille_host/jobs.py` refuses
  `paused`, `execute` reads the flag again just before the start, and `_exec.run` polls for a kill;
  `run` (`src/lucille_host/loop.py`) installs the signal handlers, and drains on the first one.
- Tests: `tests/test_cli.py` (`test_dispatch_is_explicit`: a verb with no handler of its own is
  refused, never run as `kill`); `tests/test_controls.py` (`test_pause_refuses_and_resume_clears`,
  `test_kill_kills_the_running_group_and_pauses`, `test_first_sigterm_or_sighup_drains`,
  `test_second_ctrl_c_kills`, `test_each_control_waits_for_the_controls_lock`,
  `test_the_controls_work_where_the_filesystem_cannot_lock`,
  `test_no_order_of_a_kill_and_a_resume_leaves_a_kill_request_alone`,
  `test_an_ignored_sighup_stays_ignored`, `test_the_signal_handler_takes_no_lock`); `tests/test_jobs.py`
  (`test_draining_starts_nothing_and_records_nothing`, `test_a_pause_after_the_decision_stops_the_start`,
  `test_a_drain_signal_after_the_decision_stops_the_start`);
  `tests/test_exec.py` (`test_exec_timeout_kills_group`, `test_a_user_kill_takes_the_whole_group`,
  `test_no_child_outlives_an_exception_in_the_wait`); `tests/test_read_loop.py`
  (`test_pause_kill_drain_in_run`: through `run`, a paused host refuses `paused`, `kill` kills the running
  child's group and the reply is `failed killed_locally`, and a drain lets the running operation finish
  and post its reply and starts nothing more).
- Limit: only a running host enforces a kill or a time limit. An operation running when the host itself
  is killed or crashes keeps running without either (THREAT_MODEL.md, "Known gaps").

**C20.** "Removing the computer in Lucille stops Lucille using the pairing at once."
- Lucille policy as of 2026-10-27; not proven by this repository.

**C21.** "Lucille then deletes its half and tries once, best effort, to tell your computer through its
Switchboard hub; Lucille's page says if your computer was not told."
- Lucille policy as of 2026-10-27; not proven by this repository.
- Host part, when it is told. Code: resolve handling `ended_by_peer` in `Session` in
  `src/lucille_host/link.py`: it leaves the pair's work rooms, forgets the pair and says so, in the
  pairing verbs and in `run`. Tests:
  `tests/test_pair.py` (`test_a_pair_lucille_ended_is_forgotten_here`, before the local gate opens);
  `tests/test_rooms.py` (`test_ended_by_peer_deletes_local_copy`: with a work room held, the room is
  left before the pair is forgotten, and the session stops saying the pair was ended);
  `tests/test_read_loop.py` (`test_run_stops_when_lucille_ends_the_pair`: the same through `run`, which
  then stops).

**C22.** "If Lucille cannot use its hub at all for 7 days, it deletes its half without telling your
computer."
- Lucille policy as of 2026-10-27; not proven by this repository.

**C23.** "`lucille-host forget` deletes the computer's half at once, and tells Lucille if the hub is
reachable then."
- Code: the `forget` verb in `src/lucille_host/cli.py`, through `Session.forget` in
  `src/lucille_host/link.py`, which leaves the pair's work rooms first and then calls `pairing.forget`
  in `src/lucille_host/_switchboard.py`. `pairing.forget` deletes the local record first and then tells
  the other side, best effort. A room's leave drops its keys from the share store before it tells
  anyone.
- Tests: `tests/test_rooms.py` (`test_forget_leaves_rooms_first`: the share store holds no room when
  the pair is forgotten, Lucille is told when the hub is reachable, and a room that cannot be left keeps
  the pair; `test_forget_and_run_never_change_the_room_record_at_once`: a room `run` takes while `forget`
  runs is not dropped from the record, so no room of the pair keeps its keys after it); `tests/test_status.py` (`test_forget_reports_whether_told`: through the verb, Lucille is
  told when the hub is reachable, and when it is not the pair is forgotten all the same and the verb
  says Lucille was not told; `test_forget_with_the_hub_out_of_reach`: so with a room held too, which is
  left first; `test_forget_verb_leaves_rooms_first`: no room in the share store when the pair is
  forgotten, a room that cannot be left keeps the pair, and the keyring item keeps neither the pair
  secret nor the room's keys; `test_forget_while_run_runs_stops_it`: a `run` going at the time stops,
  and kills the request it was running with no reply); `tests/test_end_to_end.py`.

**C24.** "A side that is never told keeps its half until it is removed or forgotten there too."
- Host part. Code: the host deletes its half only on `forget`; when the local SAS gate is rejected (`n`
  at `pair`'s prompt, or `pair reject`) or runs out; and when a pairing call says the pair or the pairing
  is over, which Switchboard says in these states: ended (`ended`, `ended_by_peer`), the code leaked
  (`already_redeemed`, `aborted`, `superseded`), the pairing ran out of time or its answer never reached
  Lucille (`expired`, `never_confirmed`, `unsent` once void, `wrong_hub`), or no such pair (`unknown`).
  Each of these comes from a resolve, or from answering a code, which forgets what it saved when it is
  refused so (`Session` in `src/lucille_host/link.py`). So the host can delete its half without being
  told anything, when its pairing runs out of time. Tests: `tests/test_status.py`
  (`test_forget_reports_whether_told`); `tests/test_pair.py`
  (`test_reject_forgets_and_says_whether_told`, `test_n_at_the_prompt_forgets`,
  `test_gate_deadline_from_when_the_sas_is_first_shown`, `test_y_past_the_deadline_forgets`,
  `test_a_pair_lucille_ended_is_forgotten_here`, `test_pair_leaked_states_stop`,
  `test_pair_again_states_stop`); `tests/test_rooms.py` (`test_ended_by_peer_deletes_local_copy`,
  `test_the_gate_deadline_forgets_the_pair`).
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
| `tests/guards/test_switchboard_lint.py` | Part of invariant "The host never invites anyone into its pair's rooms, and offers no room of its own": the source names no invite and no `register`, and, outside the conformance package and the tests, no `share_new` and no Switchboard function outside the listed surface (so no `pairing.start`, `heal` or `export`). It does not cover a method called on a client object, other than `register`, invites and private members, or a value a wrapper function returns; review, and `tests/test_read_loop.py` (`test_host_never_invites`, `test_sharing_calls_limited`), cover that. |
| `tests/guards/test_workflow_lint.py` | Invariant "Every release comes from a hosted runner and environment `pypi`" (the hosted-runner half; that a release publishes from environment `pypi` is checked by the provenance step in [VERIFYING.md](VERIFYING.md), not by this test). |
| `tests/test_import_graph.py` | The runtime imports no hub server. (That the base install has none is `pyproject.toml`'s dependency list: the server comes only with the `[conformance]` extra.) The README's "opens no listening port" also needs `tests/test_read_loop.py` (`test_no_listening_socket`). |
| `tests/test_cli.py` | `lucille-host` and `lucille-host --version` print the version, and the version has one source; every verb the parser takes has a handler of its own, and a verb without one is refused. |
| `tests/spec/` | The protocol spec, [spec/PROTOCOL.md](spec/PROTOCOL.md): every object in its schemas is closed and every value bounded; each fixture validates, or fails, as labelled, and each request fixture expects the reply the spec's refusal order gives, and the fixtures for each place in each pattern are the ones `tools/spec/gen_fixtures.py` writes; check 3 of the accept policy compares hub URLs as strings with every trailing `/` removed, which is how the pinned agent-switchboard keeps both a pairing code's hub and an offered room's, shown by running the library, and the host's own check 3 reads the spec's cases the same way; check 4 reads a held room's token source, which the pinned agent-switchboard sets to `shared` exactly when the room's offer carried a hub token, shown by running the library with each of the host's calls made as the host makes them (`base=None`, stores that read nothing from the environment); the wheel ships the schemas and fixtures, and an installed copy loads them. These test the spec. The host's own checks of steps 5 to 9 are held to the request fixtures by `tests/test_catalog.py`, and its decoding to the decode fixtures by `tests/test_output.py` and `tests/test_exec.py`; its check of the envelope and steps 1 to 4 by `tests/test_envelope.py`. |
| `tests/test_store.py`, `tests/test_state.py`, `tests/vendored/test_pairing_store.py` | Part of invariant "The host's stores and Switchboard footprint are its own". The pair and the work rooms' keys are kept in one OS-keyring item per profile, and the pair store over it passes the eight cases of agent-switchboard v2.6.0's own store test that exercise the store under test; under `--store-file`, they are kept in files of mode 0600 in the host's state folder, with a warning that this is weaker. Neither store writes under `~/.switchboard`, and the calls through them start no process. A keyring backend that stores nothing, or that writes secrets unencrypted to a file (one named `PlaintextKeyring`, as keyrings.alt names it), is refused, and so is a chain that holds one; `pair --store-file` still pairs there. The startup checks refuse running as root and any `SWITCHBOARD_*` variable. A failure of either store, a full disk under `--store-file` or a keyring backend's own `OSError`, is never read as the hub out of reach. The stores' locks keep out other threads and other processes, and a deadlock the kernel reports between a thread of `run` and a command, which is none, is waited out; keyring writes that would only record when something was last used are made at most once an hour. The host's client factory builds no Switchboard client unless `XDG_RUNTIME_DIR` names the host's private runtime folder, and gives each a fresh signing identity. Through the `pair` verbs, `tests/test_pair.py` shows the same (nothing under `~/.switchboard`, every file in the profile's own folders, no process started), and so does `tests/test_rooms.py` through the logic that takes work rooms; over a simulated day of looking up a room before each read and touching every 5 minutes, that logic writes the keyring item about once an hour. Through `run`, `tests/test_read_loop.py` shows that no process is started and that a signer socket planted where Switchboard's client would look for one for the host's agent id, under the `XDG_RUNTIME_DIR` the host was started with and under the system temporary folder with that variable unset, is never connected to, while the host pairs, takes a room and answers a request. `tests/test_end_to_end.py` shows it through the commands a user runs, from `pair` to `forget`: nothing under `~/.switchboard`, every file in the profile's own folders, no process started and no socket bound, with either store. |
| `tests/test_pair.py` | Pairing, as `spec/PROTOCOL.md` §16 and the design record describe it. `pair` refuses while a pair is held, and reads the code from stdin with `--code-stdin`, never from the command line. After answering a code it resolves the pair until Lucille has collected the answer, every few seconds and at once on one of the library's notes, so it finishes even when that note is lost, and stops with `leaked` or `expired` when the code leaked or the pairing ran out of time, forgetting what is left of the pair. Once Lucille has collected the answer, `pair` shows the SAS as Lucille's page shows it, and asks for `y` if the two match: `y` opens the local gate, `n` forgets the pair and tells Lucille, any other answer (the SAS itself included) is asked again, and the end of stdin or Ctrl-C keeps the pair for later. On a terminal, what was typed before the question, a whole line or a part of one, answers nothing; where it cannot be discarded, nothing is asked. `pair status` shows the SAS again while the gate is shut, and `pair confirm`, the `y` given later, opens the gate only on a SAS the host has shown, refusing one it has not (`sas_ready`). A pair with no gate record is refused by `pair`, `pair status` and `pair confirm` alike, with no SAS shown. The deadline is 24 hours from when the SAS is first shown on the host; a resolve that brings the SAS without showing it starts nothing. `n`, `pair reject` and the deadline each forget the pair; a gate opened in time is past no deadline. Neither the gate's record nor the audit log holds the SAS. `pair status`, `pair confirm` and `pair reject` work after `pair` has exited. Each pair's offered note is shown only after sanitizing. |
| `tests/test_read_loop.py`, `tests/test_envelope.py` | `run`, the read loop, as `spec/PROTOCOL.md` §5, §7 and §9 to §16 describe it, against Switchboard's in-process hub and a stand-in for Lucille, with real threads and long-polls. It reads `lucille.exec` in each room it accepted and may use, and the `pair` channel in pair:<name>, each with its own long-poll running at the same time, always as a peek past a cursor (`since`, 25 s, 200 messages, a 40 s client timeout); a page that came back full moves the cursor no further than its last message; a message the room's key cannot open, or whose body opens but is not JSON or nests more than 64 levels deep, is logged and moved past without losing the messages around it, in a work room and in pair:<name>, or, when the recovery read cannot place it, logged once as `read_stuck` with no move; a cursor is written only after the message's audit line and journal entry; the hub's `retry_after` is honoured. It acts only on `lucille.exec` in a room it accepted, ignores and logs its own posts and messages that are not notes or carry a thread, and refuses in the spec's order, its envelope and steps 1 to 4 held to the request fixtures. A job id is answered once per profile, across rooms, and remembered until 15 minutes past the later of its `not_after` and its receipt, or a day and 15 minutes past its receipt without a valid `not_after`; a change to an entry already pruned changes nothing. A reply is a note with no thread and an explicit TTL, `(not_after - now) + 900` s at most a day, or `(received - now) + 900` s without a valid `not_after`, is not posted under 1 s, and is retried until that TTL runs out, never becoming `failed`. After a restart, a decision to run with no reply sent gets `failed interrupted` with no second run, an unsent refusal is posted again unchanged, a request received and not decided is decided from the journal as it was first received, and nothing is posted in a room abandoned or left. Abandoning a room kills the child it runs with no reply and leaves the room after; a move of pair:<name> is followed and a request posted in the replacement room before the accept still runs. Nothing is touched, accepted, posted or run before the local gate opens. Pause, kill and drain reach the loop, a wake from sleep makes it read the offers, and `state/status.json` says what it sees. |
| `tests/test_rooms.py` | Taking work rooms, as `spec/PROTOCOL.md` §6, §7 and §9 describe it, in the logic the `run` verb drives (driven here one event at a time, with no thread; `tests/test_read_loop.py` drives it through `run`). Before the local gate opens the host reads no offer and accepts nothing; after it, it reads the pair's offers when the gate opens, at start, on the library's share wake, after each resolve of its own except one a failed read of the offers asked for, on a wake from sleep and every 5 minutes, always naming its pair; it resolves at start, on the library's other notes, after a hub error (backing off when a resolve fails) and at least daily, and treats each note only as a wake. It writes down each offer the library notes in its share store before deciding, whichever call made the note, one that then refused included; an accept that failed without refusing the offer (the hub or the pair room could not be read) is tried again after the next touch that reads the pair room. It accepts by all five checks of the accept policy: checks 1, 2 and 5 before the accept, where a refusal leaves nothing and keeps the offer to be considered again (shown for checks 2 and 5); checks 3 and 4 right after it, where a failure leaves the room at once, names the check in the log and records that the host holds no work room. It accepts nothing while more than one offer is pending, after dropping offers the share store no longer knows; holds at most 2 rooms; never uses again a room it left (one offered over a new pair is left as soon as it is accepted); and judges whether a room's list names its pair only after a touch of that pair. A room that passes replaces the others over the pair, only then, and a stop between the accept and the replacement is completed at the next start. A heal or a lapse of pair:<name>, found by its generation, Lucille leaving a room, and the end of the pair each abandon the rooms held, which are logged, stopped and left, and the replacement Lucille offers is taken; a move found at start abandons the room of an accept a stop interrupted too, and a heal another command followed first (`pair status`) is found by the next touch, by the generation the store keeps. A `pair confirm` made in time is never expired by a step of `run` or by `pair status` judging the gate beside it, and neither a confirmation nor the showing of the SAS writes the gate's record back for a pair forgotten beside it. Each sharing call says when the hub refused one of its writes for this computer's clock. When Lucille ends the pair, or it is forgotten here, its rooms are left before the pair, and a room that cannot be left keeps the pair. |
| `tests/test_status.py` | `status`, `log` and `forget`, as the design record's "CLI verbs" describes them. `status` makes no hub call, and shows the version, the policy check, the enabled operations and folder aliases, the controls, where the pair is kept, the hub and a configured loopback hub, the pair's state, the work rooms held or that the host holds none with the check its last room failed, and what `run` last saw: whether it runs, its last contact with the hub, and the clock, `skewed` when the hub refused a write of the host's for a clock more than 300 s from its own: a reply it posted, or one the library made for it in a touch, an accept or a leave; and `pair` warns when the hub refuses its answer so. It never shows the SAS, which `pair status` shows: not while the pair waits, nor once the SAS has reached the host and no command has shown it (`sas_ready`, when it starts no deadline either), nor while the SAS waits to be confirmed, nor past its deadline. `log` and `forget` as C18 and C23 say. |
| `tests/test_end_to_end.py` | The host end to end through the commands, against Switchboard's in-process hub and a stand-in for Lucille: `pair --code-stdin`, `pair confirm`, `run`, a room Lucille offers taken, a `host.describe` request answered with a note with no thread, `status`, `forget` beside the running `run`, which then stops, and `log`. |
| `tests/test_skeleton.py` | What the host builds on and `0.1.0.dev0` still leaves to later releases: every part of Switchboard the Switchboard-surface lint allows exists in the pinned library, as do the methods the host calls on what those return, and the managed hub's URL in the library is the host's; the conformance suite with no scenarios exits 1 rather than reporting a pass. |
