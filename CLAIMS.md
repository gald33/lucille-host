# Claims and their proof

This file takes each sentence of the claim in [PRIVACY.md](PRIVACY.md) and says what backs it.

**Status: `0.1.0`, the first release, for the repository owner's own use.** It has the catalog, the executor, the local policy, the
audit log, the local controls, the stores and Switchboard client set-up, and pairing: `lucille-host pair`
answers Lucille's code and keeps the local gate. `lucille-host run` keeps the pair once the gate is open,
takes the work rooms Lucille offers over it by the host's accept policy, and reads and answers Lucille's
requests there, tested against Switchboard's in-process hub and a stand-in for Lucille. `status` shows
what the host holds and what `run` last saw, with no hub call, and then this claim; `log` prints the audit log; `forget` ends
the pair on the computer. With `--json`, every verb but `run` writes the objects of
[spec/CLI.md](spec/CLI.md) instead of its text, and `run --json-events` writes `run`'s events, for an app
that supervises it with `--exit-on-stdin-eof`. The conformance suite (`python -m lucille_host.conformance`)
runs its scenarios against the installed host; each item below names the scenarios behind it. Some tests
named below do not exist yet: the items that say "pending, conformance" have no scenario. The release workflow's provenance checks are tested, and run on each release's
tag; they have not run yet, and `v0.1.0` is the first. `lucille-host verify` is tested with PyPI and
sigstore stood in for, and is in no release yet.

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
tests of the workflow, AST, Switchboard-surface and Mac lints also show that this repository is clean
today.
The private-string test does not: it checks the repository against a synthetic list, and only the CI
step "Private-string check", run with the repository secret, shows that no private string is committed.
None of these tests proves the behaviour of code that does not exist yet. The guards catch accidental
and casual violations, not a determined author's; a maintainer's review of every pull request is the
backstop ([VERIFYING.md](VERIFYING.md) says what each guard cannot see).

## Paragraph 1: what the host can do

**C1.** "lucille-host does only what you switch on, on your computer."
- Code: `src/lucille_host/ops/__init__.py` (the closed operation set: eight entries in an immutable
  mapping); `src/lucille_host/_exec.py` (the only place the program may start a process);
  `src/lucille_host/policy.py` (the local policy file; with no file, only `host.describe` is enabled,
  and a new pair starts from that fresh policy, the earlier one set aside unused).
- Tests: `tests/guards/test_ast_lint.py` (no process-spawning call in the runtime package outside
  `_exec.py`, in the forms the lint lists, and no import there of a module outside the lint's
  allowlist, as the lint resolves imports and the re-exports it follows; with the limit stated under
  C2); `tests/test_catalog.py` (`test_catalog_entries`: the operation set is exactly eight entries and
  read-only; `github.read` makes one `GET`, `tests/test_proxy.py`; the `gmail.` operations send only
  read-only IMAP commands, `tests/test_gmail.py`); `tests/test_policy.py` (`test_fresh_policy`: only `host.describe` is on after install);
  `tests/test_pair.py` (`test_new_pair_starts_from_fresh_policy`,
  `test_previous_policy_archived_not_applied`: only `host.describe` is on after a new pair, and the
  policy set aside is listed by `lucille-host policy previous` but not applied); the conformance suite
  (`src/lucille_host/conformance/_scenarios.py`, run by `tests/test_conformance.py`), invariant "Nothing
  Lucille sends can widen what the host permits": `unknown-op` and `not-enabled-op` are refused through the
  installed host with no process started and the policy digest unchanged, `policy-unchanged` finds the
  policy as the user set it after every scenario, and `control-enabled-op-runs` shows an enabled operation
  running.
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
  taken and a request answered, to `status`, `forget` and `log`; and `tests/test_supervision.py`
  (`test_supervision_spawns_nothing`) through `run --json-events --exit-on-stdin-eof` as an app runs it,
  whose only process is the operation's own. In those tests the keyring backend is the tests' own, in
  memory, in place of the one the host builds; `tests/test_keyring_backend.py`
  (`test_opening_the_keyring_store_starts_no_process`) opens the keyring store as a real host does, in
  a process of its own with an audit hook and nothing of the tests in it, and finds no process started.

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
  host permits" (conformance scenarios `unknown-op`, `not-enabled-op`, `traversal`, `absolute-path`,
  `symlink-escape`, `unknown-root`, `oversize-path` (a path of 1025 characters, `bad_args`) and
  `oversize-request` (a request over 16 KiB, `bad_request`): each is refused with its code through the
  installed host, with no
  `started` event from `run`, which the control `control-enabled-op-runs` shows is written when a process
  starts).
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
  when it takes a new code (`roots add --suggest-alias` and `roots check` read it and leave it as it is),
  and `policy reset`, which only sets an invalid file aside and writes the fresh policy; it fails closed
  when invalid: `run` acts on nothing until it is valid again (`src/lucille_host/loop.py`, which holds
  the offers in `src/lucille_host/link.py`); the protocol, which has no
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
  what the host permits", through the installed host: conformance scenarios `policy-shaped-fields` and
  `update-shaped-fields` (refused `bad_request`, the policy digest unchanged) and `policy-unchanged`;
  `caps-above-host` (`caps` far above the host's own are not refused) and `caps-below-output` (a lower
  request cap is applied). The conformance suite does not yet show that the host's cap holds against
  longer output; `tests/test_exec.py` (`test_request_caps_never_raise_the_host_cap`) shows it; `tests/test_policy.py` (`test_upgrade_disables_changed_entry`,
  `test_invalid_policy_fails_closed`); `tests/test_read_loop.py`
  (`test_invalid_policy_idles`: with a policy file that is not valid TOML, `run` accepts no room, writes
  down, decides, runs and posts nothing for a request Lucille sends, never rewrites the file, and goes on
  once the file is valid again, posting meanwhile a reply it decided before the file broke);
  `tests/test_supervision.py` (`test_removed_root_refused_on_next_request`: a folder removed while `run`
  runs is refused `path_outside_root` on the next request, with no restart, also when the file is
  rewritten in place with its modification time put back;
  `test_an_invalid_file_holds_offers_while_an_operation_runs`: a file made invalid while an operation runs,
  not TOML or with an unknown key, holds the offers at once;
  `test_a_changed_file_holds_offers_until_it_is_loaded`: a hand edit adding the home folder without its
  flag, valid by what it holds, is found invalid while an operation runs, and no offer is taken meanwhile;
  `test_a_folder_turned_into_the_home_folder_is_found_within_the_period`: a root's link repointed at the
  home folder, the file unchanged, is found within a few periods, idle or while an operation runs;
  `test_a_file_changed_while_main_waits_on_the_hub_holds_the_offer`: a file that changes while a step
  waits on the hub holds the offer that step brings, until a load says the file is valid;
  `test_a_folder_turned_into_the_home_folder_while_main_waits_on_the_hub_holds_the_offer`: a root's link
  repointed at the home folder while a step waits on the hub for several periods, the file unchanged,
  holds the offer that step brings until a new load, which finds the file invalid;
  `test_an_offer_held_for_an_old_load_is_taken_once_a_new_load_says_valid`: `state` says `checking` for
  it; `test_no_room_is_taken_under_a_load_that_began_a_period_ago`: a load's age counts from when it
  began, not from when its verdict reached main;
  `test_an_interrupted_accept_waits_when_the_file_changes_while_main_waits_on_the_hub`: an accept a stop
  interrupted is not completed by a step during which the file stopped being TOML;
  `test_an_invalid_file_made_valid_stays_invalid_until_it_is_loaded`;
  `test_a_late_verdict_from_an_earlier_load_counts_for_nothing` and
  `test_a_request_waiting_for_the_policy_goes_on_once_the_file_is_valid`;
  `test_no_reader_starts_while_the_policy_is_invalid`;
  `test_an_offer_waiting_at_start_waits_for_a_valid_policy`,
  `test_an_interrupted_accept_waits_for_a_valid_policy` and
  `test_no_room_is_read_while_the_policy_is_invalid_at_start`: nothing is taken or read at start while the
  policy is not checked yet or invalid; `test_a_verdict_on_an_earlier_file_counts_for_nothing`: a load of
  the file as it was before it turned invalid lets no offer through; `test_policy_reset`: `policy reset` sets aside only an invalid file, and never
  the policy a pairing set aside; `test_policy_reset_sets_aside_a_file_invalid_by_a_folder`;
  `test_policy_is_never_loaded_by_main`);
  `tests/test_catalog.py` (`test_catalog_params_equal_spec_args`,
  `test_args_valid_agrees_with_the_spec_schema`; `test_copy_fields_outside_definition_hash`: the words a
  shell shows for an operation are outside its definition hash, and a change that lets a reply carry file
  contents is inside it, so it turns the operation off until enabled again);
  `tests/test_cli_json.py` (`test_suggest_alias_writes_nothing`, `test_roots_check_maps_errno`,
  `test_roots_list_and_check_take_no_lock_and_write_nothing`: none writes anything); `tests/test_envelope.py`
  (`test_steps_1_to_4_hold_to_the_request_fixtures`: the host's own envelope check gives each request
  fixture the reply, or the silence, that the spec's order gives, extra keys such as `argv` and `exe`
  included); `tests/test_read_loop.py` (`test_refusal_order`: requests carrying `argv` or `exe` are
  refused through `run`, and nothing refused starts a process); `tests/test_pair.py`
  (`test_new_pair_starts_from_fresh_policy`: a new pair starts with only `host.describe` enabled and no
  folder); invariant "An upgrade never runs a changed op under old consent" (pending, conformance).

**C4.** "It never runs code, a shell or a command line sent by Lucille, and the programs it starts get none
of its keys and none of your environment variables, except those of `github.read` and the four `gmail.`
operations: each reads a credential you saved for it, and gets `HOME` (macOS) or
`DBUS_SESSION_BUS_ADDRESS` and `XDG_RUNTIME_DIR` (Linux), which the keyring needs."
- Code: `src/lucille_host/_exec.py` (argv only, `shell=False`, the environment `CHILD_ENV` and nothing
  else, stdin `/dev/null`, each child in its own session); the catalog's argv templates in
  `src/lucille_host/ops/__init__.py` (an absolute executable; each string from a request after `--`, as
  one whole argv element).
- Tests: `tests/guards/test_ast_lint.py` (no `shell=True`, `os.system`, `eval`, `exec`, `compile` or
  dynamic import; a process launcher only called directly, with argv its only positional argument; one
  spawn site; with the limit stated under C2); `tests/test_exec.py` (`test_exec_env_allowlist`: a child
  dumps its environment, which is exactly `PATH`, `LANG` and `LC_ALL`, and a canary variable is absent;
  `test_exec_dash_file`: files named `--version` and `-` are hashed, not read as an option or as
  standard input; conformance scenario `file-named-version`, the same for `--version` through the
  installed host); `tests/test_keyring_backend.py` (`test_opening_the_keyring_store_starts_no_process`:
  opening the keyring store, as a real host does, starts no process, where keyring's own choice of
  backend ran `gcc` and `ld` on Linux with the whole environment);
  `tests/test_catalog.py` (`test_catalog_entries`: every executable is absolute and every string
  placeholder follows `--`); invariant "No shell; one spawn site; children get a built environment and
  no key" (pending, conformance); `tests/test_envelope.py`
  (`test_steps_1_to_4_hold_to_the_request_fixtures`: requests carrying `exe`, `argv`, `env`, `cwd` or
  `timeout` are refused `bad_request` by the host's own check); conformance scenarios `extra-key-exe`,
  `extra-key-argv`, `extra-key-env`, `extra-key-cwd` and `extra-key-timeout` (the same through the
  installed host, each with no process started) and `extra-keys-in-args` (those keys in `args` are
  `bad_args`).
- `github.read`. Code: the catalog entry's `env_copy` and `Operation.child_env` in
  `src/lucille_host/ops/__init__.py`, which `execute` in `src/lucille_host/jobs.py` passes as the
  child's whole environment; `src/lucille_host/proxy.py`, the child, which reads the one credential
  the request names (decisions/0003, "First use"). The `run` process never imports
  `lucille_host.credentials`. Tests: `tests/test_jobs.py`
  (`test_github_read_runs_the_hosts_interpreter_with_the_values_the_host_fills`: the child's
  environment is `PATH`, `LANG` and `LC_ALL` and that OS's allow-list, and a proxy setting,
  `SSL_CERT_FILE`, `PYTHONPATH` and `LD_PRELOAD` set in the host's are absent;
  `test_other_children_get_the_fixed_environment_only`); `tests/test_credentials.py`
  (`test_importing_the_command_line_does_not_load_it`: importing `cli`, `loop`, `jobs` and `library`
  loads neither `lucille_host.credentials` nor `lucille_host.proxy`;
  `test_only_the_credential_verb_and_the_proxy_import_it`); `tests/test_catalog.py`
  (`test_definition_hash_covers_operands_schema_limits_and_environment`: a change to the allow-list, or
  to which elements the host fills, turns an enabled `github.read` off).
- The `gmail.` operations. Code: their catalog entries share `github.read`'s `env_copy` and fill the
  same host elements, plus the request's arguments as one JSON element after `--`
  (`src/lucille_host/ops/__init__.py`); `src/lucille_host/gmail.py`, the child, reads the one
  `gmail/<name>` credential the request names (decisions/0003, "Second use"). Tests:
  `tests/test_credentials.py` (`test_importing_the_command_line_does_not_load_it`: importing the command
  line loads none of `lucille_host.credentials`, `lucille_host.proxy` and `lucille_host.gmail`;
  `test_only_the_credential_verb_and_the_proxy_import_it`: the importers are the command line,
  `proxy.py` and `gmail.py`); `tests/test_catalog.py` (`test_catalog_entries`: the four entries' argv
  and environment); `tests/test_gmail.py` (`test_the_child_writes_only_its_reply_and_ends_with_os_exit`:
  run as a real process). `lucille-host credential test gmail <name>` starts the same child, as
  `gmail.check`, from the command line only (`tests/test_credential_verb.py`,
  `test_no_request_can_reach_credential_test`).
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
  bounded, decodable and path-free" (conformance scenario `output-bounded`: a `files.search` whose output
  passes 1 MiB comes back truncated and flagged, and the serialized reply measures under 256 KiB);
  invariant "Failures cross the wire as codes only" (conformance scenario `failures-codes-only`: requests
  that carry a sentinel, in the op's name, a path, a folder alias, an extra envelope key or an extra
  argument, are each refused with a code alone, and the sentinel is in no body the host posted. The
  sentinel is one the request carries; the scenario puts none in an operation's stderr).

**C8.** "`files.search` returns lines of the files it searches."
- Code: the `files.search` catalog entry (`src/lucille_host/ops/__init__.py`), which runs ripgrep with
  `--json` in the folder with no path, and `jobs.check`, which refuses a folder holding one of the
  host's own (`containment.holds_own`); `src/lucille_host/_ripgrep.py`, which turns ripgrep's `match`
  messages into the reply's document (`files.search.schema.json`). Its entry says
  `sends_file_content`, so a shell asks before turning it on.
- Tests: `tests/test_files_search.py`, with the real ripgrep: each matching line comes back as a match
  with its path relative to the folder, never the folder's own path
  (`test_matches_come_back_with_paths_relative_to_the_folder`); a name and a line that are not UTF-8
  come back as their bytes (Linux only: macOS refuses such names); a newline in a file's name makes no
  other match (`test_a_newline_in_a_file_name_cannot_forge_a_match`); `max_count` and `fixed_strings`; a
  pattern or a file named like a flag; no hidden file (a name that starts with a dot) is searched
  whatever a `.ignore`, `.gitignore` or `.rgignore` in or above the folder says; no link is followed out
  of the folder; no configuration file is read; a long line is cut at 1000 bytes; a search with more than
  fits comes back fit to the cap, or a request's lower cap, and flagged; a folder holding the host's own
  folder, directly or through a link, is refused. With made-up output: only `match` messages are kept;
  a line that is not one of ripgrep's messages, or a match with an absolute or `..` path, gives
  `unreadable_output` and no match; a message nested past 8 levels is refused before the parser; a cut
  last line; at most 1000 matches; redaction of each path and line, before a line is cut
  (`test_redaction_runs_before_the_line_is_cut`); the child's stdin is `/dev/null`. Each document is held to its schema. `tests/test_catalog.py`: its words, and that
  `sends_file_content` is in its definition hash.

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
  "Output is bounded, decodable and path-free" (conformance scenario `replies-path-free`: no reply to
  `host.describe`, `files.sha256` or `files.search`, nor a refusal of a request naming those paths, holds
  the root's path or real path or the host's `$HOME`); `tests/test_rooms.py` (`test_resolves_at_least_daily`: with nothing else asking, the
  session resolves the pair again a day after the last resolve).

**C8a.** "`github.read` returns GitHub's reply to the read it names. ... Lucille learns the names of the
credentials it asks these operations to use, never their values."
- Code: `src/lucille_host/proxy.py` (standard output is the response body, standard error `HTTP
  <status>` and GitHub's `Link` header, each with the token replaced, raw and in base64, before anything
  is written); the request schema's `args.github.read` (`credential` is a name; nothing carries a value).
- Tests: `tests/test_proxy.py` (`test_one_get_with_the_fixed_headers_and_the_token_only_in_authorization`;
  `test_the_scrub_covers_the_token_and_its_base64_at_each_alignment`;
  `test_the_scrub_covers_url_and_json_escaped_echoes`; `test_a_chunked_body_is_joined_before_the_scrub`;
  `test_at_the_cut_a_beginning_of_the_token_is_dropped`; `test_a_keyrings_error_is_not_shown`).
- Note: the scrub is defence in depth, with the limits decisions/0003 states (JSON's optional `\uXXXX`
  escapes, base64 wrapped across lines, a change of case, a part of the token). What the token can read
  on GitHub is what Lucille can ask for while `github.read` is on (THREAT_MODEL.md).

**C8b.** "`gmail.mailboxes` returns the names of your Gmail mailboxes and labels. `gmail.search` returns how
many messages matched, and the sender, recipients, subject, date, size, ids and whether it is unread of up
to 50 of them, and `gmail.read` returns one message's headers, up to 32 KiB of its text, and its
attachments' names, types and sizes, never their contents."
- Code: `src/lucille_host/gmail.py`: search fetches only `UID`, `FLAGS`, `INTERNALDATE`, `RFC822.SIZE`,
  `X-GM-MSGID`, `X-GM-THRID` and seven header fields with `BODY.PEEK[HEADER.FIELDS (...)]`, and returns
  the ids, date, size, whether the message is unread, `From`, `To` and `Subject`; read fetches
  `BODY.PEEK[]<0.4194304>`, parses it with `email`, keeps a text part (HTML turned into text, with
  scripts, styles and hidden elements dropped) cut at 32 KiB, and lists attachments by name, type and
  size; every string is scrubbed of the app password (in any case, with spaces or hyphens between its
  letters, and in base64 at each alignment) before any field is cut; each document is then fit to 60
  KiB as rule `stream-size` measures it, leaving out, each with its flag, a read's attachments past an 8
  KiB share, then the end of its text, a search's oldest results and the last mailboxes; and the bytes
  written are scrubbed once more. The request schema's `args.gmail.*` (`credential` is a
  name; nothing carries a value).
- Tests: `tests/test_gmail.py` (`test_mailboxes_with_special_use_and_hebrew_names`,
  `test_at_most_500_mailboxes`, `test_many_results_newest_first_and_cut_at_the_limit`,
  `test_only_the_read_only_commands_and_fetch_items_are_sent`, `test_a_plain_text_message`,
  `test_an_html_only_message_is_read_as_text_with_hidden_text_dropped`,
  `test_a_multipart_alternative_message_prefers_the_plain_part`, `test_hebrew_in_headers_and_body`,
  `test_attachments_are_named_never_sent`,
  `test_a_long_text_is_cut_at_32_kib_and_the_document_fits_the_stream`,
  `test_the_childs_measure_is_the_hosts`, `test_a_long_hebrew_text_is_cut_to_what_the_stream_holds`,
  `test_many_attachments_are_cut_to_fit_and_the_text_is_kept`,
  `test_a_search_of_long_recipient_lists_drops_the_oldest_to_fit`,
  `test_many_long_mailbox_names_are_cut_to_fit`,
  `test_a_read_whose_headers_take_the_most_their_cuts_allow_still_fits`,
  `test_no_cut_leaves_a_part_of_the_app_password`,
  `test_the_scrub_covers_any_case_and_spaces_or_hyphens_between_letters`,
  `test_a_message_over_4_mib_is_fetched_in_part_and_flagged`,
  `test_the_app_password_is_removed_from_everything_written`,
  `test_nothing_gmail_writes_reaches_a_failure_line`).
- Note: what a message says can be aimed at Lucille's assistant (prompt injection), and HTML can hide
  text by ways the drop does not know (THREAT_MODEL.md). The scrub has the limits decisions/0003
  states. What the app password can read is the whole mailbox, every label; Lucille can ask for any of
  it while a `gmail.` operation is on.

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
  `src/lucille_host/journal.py`, which takes each job id once, under a lock other processes respect too;
  the run lock in `src/lucille_host/loop.py`, which keeps a profile to one `run`. Tests:
  `tests/test_read_loop.py` (`test_refusal_order`: an expired
  request is refused `expired` and starts no process; `test_reply_ttl_explicit`: a reply lives
  `(not_after - now) + 900` s, at most a day, passed explicitly; `test_reply_ttl_without_valid_not_after`;
  `test_duplicate_runs_once`, in one room and across two; `test_restart_gives_interrupted`: a restart
  between the decision to run and the reply gives `failed interrupted`, with no second run);
  `tests/test_envelope.py` (`test_steps_1_to_4_hold_to_the_request_fixtures`;
  `test_the_journal_takes_a_job_id_once`; `test_two_processes_never_take_one_job_id`: two processes
  taking the same job ids at once never both take one; `test_the_journal_waits_for_another_process`);
  `tests/test_status.py` (`test_a_profile_runs_one_run_at_a_time`: a running `run` holds the run lock
  where another process sees it, until it stops; it and
  `test_a_run_refuses_while_another_process_holds_the_lock`, `test_run_refused_says_why`: a second `run`
  of the profile, in this process or another, refuses before it does anything;
  `test_a_run_refuses_where_the_run_lock_cannot_be_taken`: where the filesystem cannot lock, `run` says
  so, naming itself, and does nothing); `tests/test_supervision.py` (`test_second_run_exits_21`: a second
  `run` of the profile exits 21 with one `stopped` object, and writes nothing but its line in the run
  record (spec/CLI.md section 16): no status, no audit line, no version record, no keyring call; `test_run_holds_its_lock_against_other_processes`: the same hold
  as `test_a_profile_runs_one_run_at_a_time`'s, through the command, which takes the lock before its
  other start checks: from `ready` until it exits, another process cannot take it). An operation that
  started in time can still finish after `not_after`: the check is made once, before the start.

**C13a.** "With `github.read` on, your computer also connects to `api.github.com`, for each read Lucille
asks for, with the token you saved for it."
- Code: `src/lucille_host/proxy.py`: the name `api.github.com` and port 443 are fixed; every address the
  name resolves to must be on the proxy's fixed list (`address_allowed`), and the connection goes to a
  vetted address with TLS checked for `api.github.com` against certifi's bundle; no redirect is
  followed, and no proxy setting is read. `tools/guards/ast_lint.py`: among the host's modules, only
  `proxy.py` may import `socket`, `ssl`, `http.client`, `ipaddress` or `certifi`, but for `verify.py`,
  which may import `ssl`, `http.client` and `certifi` for `lucille-host verify`, which `run` never calls.
- Tests: `tests/test_proxy.py` (`test_refused_addresses`, `test_allowed_addresses`,
  `test_one_refused_address_refuses_the_whole_request`, `test_a_redirect_is_reported_never_followed`,
  `test_it_connects_to_vetted_addresses_only_in_order`); `tests/test_jobs.py`
  (`test_github_read_is_not_enabled_where_the_host_knows_no_interpreter`: a program that embeds the
  host runs no `github.read`;
  `test_github_read_is_refused_before_its_child_starts_while_keyring_is_off`: with keyring turned off
  by `PYTHON_KEYRING_BACKEND` (its `null` or `fail` backend) or by a `keyringrc.cfg` under a custom
  `XDG_CONFIG_HOME`, `run` refuses it `not_enabled` and starts nothing).

**C13b.** "With a `gmail.` operation on, it connects to `imap.gmail.com`, for each request to it, with the
Gmail address and app password you saved for it, and only reads: it changes, moves, sends and deletes no
mail."
- Code: `src/lucille_host/gmail.py`: the name `imap.gmail.com` and port 993 are fixed; it resolves and
  vets the addresses with `proxy.py`'s `resolve` and connects with its `tls_connect` (implicit TLS
  checked for `imap.gmail.com` against certifi's bundle), then hands the socket to `imaplib`, whose own
  connect is never used and whose debug transcript is off. It sends only `CAPABILITY`, `LOGIN`,
  `LIST "" "*"`, `EXAMINE` (never `SELECT`), `UID SEARCH`, `UID FETCH` of `BODY.PEEK` items (which
  leave a message unread) and `LOGOUT`. At most 60 sign-ins in 10 minutes, and none for 15 minutes after
  Gmail refuses one. `tools/guards/ast_lint.py`: among the host's modules, only `proxy.py` and
  `gmail.py` may import `socket`, `ssl`, `imaplib` or `certifi`.
- Tests: `tests/test_gmail.py` (`test_it_connects_to_the_vetted_address_on_993_with_tls_for_imap_gmail_com`,
  `test_imaplib_never_opens_a_connection_of_its_own`, `test_one_refused_address_refuses_the_whole_request`,
  `test_only_the_read_only_commands_and_fetch_items_are_sent`,
  `test_a_wrong_or_revoked_app_password_is_exit_7_and_not_tried_again_for_15_minutes`,
  `test_more_than_60_sign_ins_in_10_minutes_is_exit_5_without_connecting`).
- Note: the IMAP session reads with the account's full IMAP access; read-only is a property of the
  commands this program sends, not of the app password, which Google does not scope.

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
  posted; `test_threads_writing_at_once_lose_no_line`;
  `test_a_line_still_being_written_is_left_out_by_its_readers`: `tail`, and the tests' reader of a running
  `run`'s log, leave out a last line with no newline yet); `tests/test_jobs.py`
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
  `run` (`src/lucille_host/loop.py`) installs the signal handlers, and drains on the first one, and, under
  `--exit-on-stdin-eof`, at the end of its standard input (`Lifeline` there); the command drains on one
  during its start checks too (`_serve` in `src/lucille_host/cli.py`); its events wait for no reader
  (`_Events` in `src/lucille_host/cli.py`), so a shell that stops reading holds up no pause; a wait
  for one of the host's lock files goes on through a signal those handlers take
  (`wait_for_lock` in `src/lucille_host/state.py`).
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
  and post its reply and starts nothing more;
  `test_a_drain_signal_while_run_waits_for_the_record_lock_still_drains`: a drain signal that comes
  while `run`'s main thread waits for the record lock another process holds still drains once the lock
  is free: the running operation's reply is posted, and a request posted while main waited is neither
  decided nor written down); `tests/test_supervision.py` (`test_stdin_eof_drains`: under
  `--exit-on-stdin-eof`, the end of standard input, by a close or by the death of the only process holding
  the write end, drains `run` as the first signal does; `test_ctrl_c_after_stdin_eof_kills`;
  `test_a_stop_signal_during_the_start_checks_drains`: a SIGTERM or Ctrl-C while `run` reads its host
  configuration, checks its environment, takes the run lock, reads its version record, opens the store,
  reads its state files, its digest key or the pair's record, with or without the lifeline, stops it with
  `stopped` `drained`, no `ready` and no hub call, and a second Ctrl-C there leaves the profile unpaused;
  `test_a_stop_signal_and_the_end_of_stdin_say_which_came_first`: `cause` names whichever began the drain;
  `test_a_ctrl_c_while_stopped_waits_for_its_reader_changes_nothing`: a Ctrl-C while `stopped` waits for
  a slow reader neither escapes nor changes the exit code;
  `test_a_blocked_stdout_holds_up_nothing`: with standard output blocked, a pause still refuses before
  anything starts); `tests/test_store.py`
  (`test_a_signal_run_handles_does_not_end_a_wait_for_a_lock`: SIGTERM, SIGHUP or Ctrl-C, handled as
  `run` handles them, does not end a wait for the record lock, the controls' lock or the policy lock;
  `test_a_signal_run_handles_does_not_end_a_wait_on_another_thread`: nor a wait for the journal's lock
  on a thread other than the main one, when the signal reaches that thread).
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
  forgotten, a room that cannot be left keeps the pair (`status` then says its leave is tried again),
  and the keyring item keeps neither the pair secret nor the room's keys;
  `test_forget_verb_leaves_the_rooms_of_a_pair_the_library_dropped`: when
  the library drops the pair and its room cannot be left then, the command says the pair is gone and
  that `forget` leaves the room, `status` says so of the room, and `forget`, with no pair held, leaves
  it and drops its keys; `test_forget_verb_with_nothing_left_of_a_dropped_pair_says_no_pair_is_held`:
  with no room held over a pair the library dropped, `forget` says that no pair is held, not that it
  left rooms; `test_forget_while_run_runs_stops_it`: a `run` going at the time stops,
  and kills the request it was running with no reply;
  `test_forget_in_another_process_stops_run_when_lucille_is_not_told`: so too when `forget` runs in
  another process with the hub out of reach, so that no note of the library's wakes `run`;
  `test_forget_in_another_process_kills_at_once_while_it_waits_for_the_hub`: with a hub that is slow to
  fail each request, the request `run` was running is killed as soon as `forget` marks its room
  abandoned, while `forget` still waits for the hub with the record lock held;
  `test_a_request_queued_from_a_room_forget_marks_in_another_process_never_starts`: a request `run` had
  read and not yet taken is then neither decided nor started;
  `test_a_stop_signal_while_run_waits_for_forget_in_another_process`: a SIGTERM while `run` waits for
  that `forget` does not stop `run` with an error, the request it runs is still killed at once, while
  `forget` still waits for the hub, and `run` stops once `forget` is done;
  `test_a_command_another_forget_beat_says_the_pair_is_gone`: a `forget` or `pair reject` that found
  the pair held, and then finds that a `forget` in another process forgot it first, says the pair is
  gone, and never that Lucille could not be told);
  `tests/test_rooms.py` (`test_forget_marks_every_room_abandoned_before_it_leaves_one`: every room is
  marked abandoned, and stopped, before the first leave;
  `test_a_resolve_that_finds_the_pair_already_forgotten_says_it_is_gone`: a `pair status` whose resolve
  finds that another command forgot the pair after it was found held says the pair is gone (`none`),
  and writes no forget of its own;
  `test_a_command_beside_a_call_that_drops_the_pair_says_it_is_gone`: a `forget` or `pair reject` that
  found the pair held while a resolve (`pair status`, `pair confirm`, `pair`'s wait) or `pair`'s answer
  to a code had the library drop it waits for that call to forget what is left, then says the pair is
  gone, and never that Lucille could not be told;
  `test_a_command_beside_pairs_wait_never_forgets_the_pair_within_a_resolve`: a `forget` or `pair
  reject` made while `pair`'s wait is inside a resolve waits for it, so the wait ends as the resolve
  found the pair, and the pair is forgotten once, not also as `ended`;
  `test_a_forget_beside_an_answer_leaves_the_new_pairings_gate_record`: a `forget` with no pair held,
  beside `pair`'s answer to a new code under the name of a pair the library dropped, waits for the
  answer, and leaves the new pairing's gate record;
  `test_a_pair_the_library_drops_in_a_touch_of_run_is_not_said_forgotten_elsewhere`: when `run`'s own
  touch has the library drop the pair, `run` leaves its rooms, writes the forget to the audit log, and
  does not say that another command forgot the pair, all in that step, under the record lock, even
  while a resolve the hub turned away backs off;
  `test_a_touch_that_does_not_reach_the_pair_room_keeps_the_resolve_back_off`: a touch that does not
  reach the pair room for any other reason leaves that back-off as it is;
  `test_forget_leaves_what_a_process_stopped_after_the_library_drop_left`: the rooms and gate record
  that a process stopped right after the library dropped the pair left behind, the room an interrupted
  accept kept included, are left and deleted by `forget` with no pair held, and `status` says that
  `forget` leaves such a room, names it once when its leave fails, and says nothing of the kind while
  the pair is held;
  `test_a_forget_beside_a_drop_whose_room_cannot_be_left_says_the_pair_is_gone`: so too when that call
  could not leave a room, which the `forget` then leaves;
  `test_a_forget_whose_own_leave_has_the_library_drop_the_pair_says_lucille_was_told`: when a leave of
  `forget`'s own has the library drop the pair, as ended from this side, `forget` says that Lucille was
  told, since the pair room says that end already, and never that the hub could not be reached;
  `test_a_room_forget_cannot_leave_once_its_own_leave_dropped_the_pair_is_left_by_forget`: a room it
  then cannot leave keeps nothing, whether the share store or the keyring item fails: the gate record
  goes, `forget` says the pair is gone and that `forget` leaves the room, and it does;
  `test_a_command_beside_a_forget_and_a_pairing_under_the_same_name_says_it_is_gone`: a `forget` or
  `pair reject` that found the pair held, when another command forgot it and paired again under the
  same name meanwhile, says the pair is gone and leaves the new one as it is, confirmed or not;
  `test_a_forget_beside_a_resolve_before_the_sas_waits_for_the_judgement`: a `forget` beside
  `pair status` or `pair confirm` waits from their resolve to their judgement, so a pair it forgets is
  never said to be `waiting`;
  `test_a_room_the_library_dropped_pair_cannot_leave_is_left_by_forget`: the same as through the verb
  above, for a step of `run` and for `pair status`, whether the share store or the keyring item fails;
  `test_a_run_whose_pair_another_command_forgot_touches_no_other_pair`: a `run`
  whose pair was forgotten, and another pair made, under another name or the same one, stops at its next
  step and changes nothing of the new pair; `test_a_command_that_finds_its_pair_gone_leaves_another_pairs_state`:
  a command that finds its pair gone deletes no other pair's gate record and leaves none of its rooms);
  `tests/test_end_to_end.py`.

**C24.** "A side that is never told keeps its half until it is removed or forgotten there too."
- Host part. Code: the host deletes its half only on `forget`; when the local SAS gate is rejected (`n`
  at `pair`'s prompt, or `pair reject`) or runs out; and when a pairing call says the pair or the pairing
  is over, which Switchboard says in these states: ended (`ended`, `ended_by_peer`), the code leaked
  (`already_redeemed`, `aborted`, `superseded`), the pairing ran out of time or its answer never reached
  Lucille (`expired`, `never_confirmed`, `unsent` once void, `wrong_hub`), or no such pair (`unknown`).
  Each of these comes from a resolve (one the library makes within a touch of `run`'s included), or
  from answering a code, which forgets what it saved when it is refused so (`Session` in
  `src/lucille_host/link.py`). So the host can delete its half without being
  told anything, when its pairing runs out of time. Tests: `tests/test_status.py`
  (`test_forget_reports_whether_told`); `tests/test_pair.py`
  (`test_reject_forgets_and_says_whether_told`, `test_n_at_the_prompt_forgets`,
  `test_gate_deadline_from_when_the_sas_is_first_shown`, `test_y_past_the_deadline_forgets`,
  `test_a_pair_lucille_ended_is_forgotten_here`, `test_pair_leaked_states_stop`,
  `test_pair_again_states_stop`); `tests/test_rooms.py` (`test_ended_by_peer_deletes_local_copy`,
  `test_a_pair_the_library_drops_as_ended_here_still_has_its_rooms_left`,
  `test_a_pair_the_library_drops_in_a_touch_of_run_is_not_said_forgotten_elsewhere`,
  `test_a_pair_with_no_gate_record_the_library_drops_as_ended_here_is_ended`,
  `test_an_answer_refused_after_the_library_saved_the_pair_forgets_it`,
  `test_a_room_the_library_dropped_pair_cannot_leave_is_left_by_forget`,
  `test_the_gate_deadline_forgets_the_pair`).
- Lucille's half: Lucille policy as of 2026-10-27; not proven by this repository.

**C25.** "The source is public at github.com/gald33/lucille-host."
- This repository. Check it by opening it.
- That a release was built from this source: `tests/guards/test_workflow_lint.py` (workflows use
  GitHub-hosted runners only, and every action is pinned by commit SHA); `tests/test_release.py`
  (`release.yml` builds from a tag on `main` that names the package's version, publishes only from
  environment `pypi`, and then checks that PyPI serves those bytes, attested to this repository,
  `release.yml` and `pypi`, at that tag, from a GitHub-hosted runner); `tests/test_verify.py`
  (`lucille-host verify` passes only a release PyPI lists and serves exactly, not yanked, each file
  attested by this repository, `release.yml` and `pypi`, at its tag, from a GitHub-hosted runner, whose
  installed files are the attested wheel's, with PyPI and sigstore stood in for); and the provenance check
  in [VERIFYING.md](VERIFYING.md), step 2, which the workflow's `provenance` job runs on each release.

## Guards that back no single sentence

These existing tests back the design rather than one sentence of the claim.

| Test | What it backs |
|---|---|
| `tests/guards/test_private_strings.py` | The CI step "Private-string check", run with the repository secret, is what checks that no string private to Lucille is committed here; this test shows that the check finds a planted string, never prints it, and fails closed without the list. Its own check of this repository uses a synthetic list, so it passes whatever is committed. |
| `tests/guards/test_switchboard_lint.py` | Part of invariant "The host never invites anyone into its pair's rooms, and offers no room of its own": the source names no invite and no `register`, and, outside the conformance package and the tests, no `share_new` and no Switchboard function outside the listed surface (so no `pairing.start`, `heal` or `export`). It does not cover a method called on a client object, other than `register`, invites and private members, or a value a wrapper function returns; review, and `tests/test_read_loop.py` (`test_host_never_invites`, `test_sharing_calls_limited`), cover that. |
| `tests/guards/test_macos_lint.py` | The Mac app's part of decisions/0002 §20 and of its invariants M1, M2, M9 and M20, in the forms the lint lists: its Swift names no network, listener, XPC, shell or other way to start a process (a Swift file under `Tests` may start a multi-line string with `#!/bin/sh`, the stand-in the self-test runs through `HostCLI.swift`), starts `Process` only in `macos/App/HostCLI.swift`, which writes the bundled runtime's path and arguments, names `NSWorkspace` and makes a URL from a string only in `macos/App/SystemPlaces.swift`, the app's one opener, in one call of the two System Settings pages and the Applications folder written there, each place its own, in that file's designed code; names the opener elsewhere only in a call, extends none of its types, gives none another name and holds no backtick in its code; names a System Settings address nowhere else and Full Disk Access's page (`Privacy_AllFiles`) nowhere; uses none of the other ways it lists to open a URL or another application (the login item's `openSystemSettingsLoginItems()` is not its to see), reads no pasteboard and holds no `http(s)` URL; the app's strings, the `Info.plist` template and the pins it reads use no phrase [PRIVACY.md](PRIVACY.md) says this project must never claim; and nothing declares a URL scheme, a daemon or an installer package. It reads the source, not the built app. |
| `tests/guards/test_workflow_lint.py` | Invariant "Every release comes from a hosted runner and environment `pypi`" (the hosted-runner half; that a release publishes from environment `pypi` is `tests/test_release.py`'s, and the provenance step in [VERIFYING.md](VERIFYING.md)). |
| `tests/test_release.py` | `release.yml` and the checks it runs (`tools/release/check_release.py`), with PyPI and sigstore stood in for: it runs on a `v*` tag only, each job has only the permissions it needs, only `publish` is in environment `pypi`, holds the OIDC token and runs no code of the repository; the tag names a final release equal to the package's version, on `main`; the pinned Switchboard release's files are PyPI's and the lock's, attested by `gald33/switchboard`, `publish.yml`, `pypi`, at its tag, before anything is built; the build uses the pinned backend by hash and `SOURCE_DATE_EPOCH`; nothing is published until the built wheel, installed into a fresh environment, has passed the conformance suite; and after publishing, PyPI lists and serves exactly the built files, each attested by this repository, `release.yml` and `pypi`, at the tag, from a GitHub-hosted runner. Each check fails the test when it is removed. The check that verifies an attestation with sigstore was run by hand against agent-switchboard 2.6.0's files on PyPI. |
| `tests/test_import_graph.py` | The runtime imports no hub server. (That the base install has none is `pyproject.toml`'s dependency list: the server comes only with the `[conformance]` extra.) The README's "opens no listening port" also needs `tests/test_read_loop.py` (`test_no_listening_socket`). |
| `tests/test_gmail.py` | The `gmail.` operations' child (decisions/0003, "Second use"), against a fake IMAP server: a credential that is missing, removed, not bound to `imap.gmail.com` or not an address and a 16-letter app password, and a keyring's error, are exit 2 before anything is sent; a refused address, an unreachable service and a timeout are exit 4; a refused sign-in is exit 7 and starts a 15-minute wait; more than 60 sign-ins in 10 minutes, more than 8 MiB read, a line past its cap and more than 500 mailboxes are caps; only the read-only commands and `BODY.PEEK` fetch items are sent, each mailbox name quoted in modified UTF-7 and each text criterion as a UTF-8 literal; empty and many results, plain, HTML-only, multipart, Hebrew and other-charset messages, attachments named but never sent, text cut at 32 KiB, a message past 4 MiB fetched in part; every document fits 60 KiB as rule `stream-size` measures it (Hebrew text, 1,500 attachments, long recipient lists and mailbox names, headers at their limits), what is left out flagged; the app password scrubbed in any case and with spaces or hyphens between its letters, before any cut, so no cut leaves a part of it; no read asks for more than one byte past the 8 MiB; imaplib's `file`, read-only from Python 3.14, never set; no \All mailbox or no Gmail extensions is exit 3; the app password is scrubbed from everything written, and no text of Gmail's or an exception's reaches a failure line; the child ends with `os._exit`, writing only its reply. |
| `tests/test_proxy.py` | `github.read`'s child (decisions/0003, "First use"): it reads its arguments by position and writes one fixed line, never an exception's text, for a malformed list or anything unexpected (exit 6), also as a real process; it refuses a credential that is missing, not bound to `api.github.com` or not in a GitHub token's form, and a keyring's error, before anything is sent (exit 2); every address must be allowed by its fixed list, the shared address space for carrier-grade NAT built from its integer, with each network's edges and the addresses just outside them, and one refused address refuses the whole request (exit 4); it sends one `GET` with the fixed headers, the token in `Authorization` alone; a 3xx is reported, never followed; headers past 64 KiB or 100, a body past 1 MiB (cut there, a beginning of the token dropped at the cut) and a compressed body are exit 5; a connection that breaks before the response has been read is exit 4; the scrub replaces the token and its base64 at each alignment, after chunks are joined; each stream is written once, and the process ends with the exit code its reply carries. Each of these rules has a test that fails when the rule is removed: it connects to the vetted address, never the name; a real TLS handshake with a certificate for `api.github.com` that no root in certifi's bundle signed is refused, with `api.github.com` sent as the server name and certifi's bundle the only trust; the interpreter is `sys.executable` as given, never resolved through a link; the process ends with `os._exit`, so no `atexit` handler runs; it reads no further than one read past the 1 MiB cap, with or without a length; a transfer coding other than a plain `chunked` (`gzip, chunked` among them) is not read (exit 5); and nothing a library writes on standard output or error, through Python or from C, reaches the reply. |
| `tests/test_cli.py` | `lucille-host` and `lucille-host --version` print the version, and the version has one source; every verb the parser takes has a handler of its own, and a verb without one is refused. |
| `tests/test_cli_json.py` | The command line's `--json` contract, [spec/CLI.md](spec/CLI.md). Every golden fixture's command line, run in a home set up for it, writes on stdout exactly the fixture's objects, each one its schema accepts, in ASCII, written compactly with its fields in the fixture's order, and the exit code the fixture gives; run without `--json` from the same set-up, it exits the same and writes the same stderr. `--json` works before the verb, after it and after its action, for every verb and action, and before `--` only; `--version` writes one `version` object with it in either order; a usage error exits 2 with one `usage` object that quotes nothing of the command line, and `run --json` without `--json-events`, `pair --code-stdin`, and `run`'s options with another verb are usage errors; `run`'s fixtures end with the `stopped` its exit code goes with, and `policy reset`'s set an invalid file aside or refuse `not_invalid`. Every literal failure code in `cli.py`, `link.py` and `loop.py` is in the contract's list, every other failure `main` catches has the code spec/CLI.md section 10 gives it, in that table's order, with its own line on stderr, and a bug writes `internal` and then its traceback, and never a value that is not JSON. Each failure reachable without a hub, and several through the in-process hub, ends stdout with an `error` object whose code spec/CLI.md lists for that verb, with the same exit and stderr as without `--json`: among them a hub error and a refused answer, a loopback code with no `--hub`, a pair the library refuses, a pair held in the other store, and a profile that holds two pairs. A refusal of the library's that ends nothing says why in the host's words, after the host's words and the library's state, in the `error` object and on stderr alike, and repeats no text of the library's: an answer refused for want of `cryptography`, which leaves no pair and no gate record; the same refusal at a resolve, which keeps the pair; a record of the file store that is not JSON, in `status`, `forget`, `pair status` and `run`, which forget nothing and name neither the file nor any folder of the host's; a healing strategy Lucille wrote in the pair room, which `pair status` and `run` never repeat, whether it is shaped as a strategy's name or holds a code, a token and a SAS; and sentences and states holding a code, a pair's export, a room's join code, a token, a SAS, a path or words the other side wrote, at an answer, at a resolve and reading the pair's record, which no verb repeats in its `error`, its `status` or on stderr, saying instead that the host does not repeat the library's reason. With stdout a pipe, each object and each line is written while the next folder still stalls. `ops list` tells an enabled operation this computer cannot run from a disabled one, and its words and its flags come from the catalog; `roots add --suggest-alias` gives the Mac app's aliases for every Latin name in its test vectors, folds every letter from U+00C0 to U+017F and every full-width form as section 9.1 says, and drops every combining mark, so a letter from U+00C0 to U+017F gives the same decomposed as composed, while a decomposed Latin letter past U+017F keeps its plain letter where its composed form is `-`, gives `folder` for a name in another script, writes nothing and takes no lock; `roots add -- <alias> <path>` works, and a broad folder's refusal says which kind it is, while one added with `--allow-home-or-root` is `broad` in `roots add` and `roots list`; `roots check` reports each folder's state by its error number, a folder that opens and cannot be listed included, opens no session and writes nothing; a file name that is not UTF-8 is written as U+FFFD; and `status` reports a store it cannot read (a keyring that refuses the read included), or a pair the library refuses, in its `pair`, and the rest as it is, and stops as `state_invalid` on a run record, a room record or a gate record holding a value the host never writes there (NaN, an infinity, a pid or a time out of range, or a value of another type). A file nested past the host's own limit (`tests/test_nesting.py`), which 3.14's parsers read and those of 3.11 and 3.13 do not, is invalid as any other is, on every Python version, and never `internal`: the policy gives `policy_invalid`, and `pair` sets it aside and pairs; a state file gives `state_invalid`; the keyring item or the file store's record of the pair gives `status` a `pair` `unreadable`, and `pair status` `store_error`. |
| `tests/spec/test_cli_schemas.py`, `tests/spec/test_packaged.py` (`test_cli_spec_packaged`) | The command line's contract, [spec/CLI.md](spec/CLI.md): every object in its schemas is closed and lists every field it allows as required, every value the host bounds is bounded, every pattern reads the same in ECMA-262 and Python, a definition two schemas share is the same in both and the same as the protocol's, `status` has no field that could hold the SAS, every type has a schema and a fixture, and every fixture validates; the wheel ships the schemas and fixtures, and an installed copy loads them. |
| `tests/test_claim.py`, `tests/spec/test_packaged.py` (`test_the_claim_is_packaged`) | The claim `lucille-host status` prints (`src/lucille_host/claim.txt`, read by `src/lucille_host/report.py`) is the quoted block under "The claim" in [PRIVACY.md](PRIVACY.md) and under "What lucille-host claims" in the [README](README.md), word for word with the quoting taken off, so that none of the three can change alone; it is ASCII text, and the wheel carries it, as an installed copy reads it. |
| `tests/spec/` (the other files) | The protocol spec, [spec/PROTOCOL.md](spec/PROTOCOL.md): every object in its schemas is closed and every value bounded; each fixture validates, or fails, as labelled, and each request fixture expects the reply the spec's refusal order gives, and the fixtures for each place in each pattern are the ones `tools/spec/gen_fixtures.py` writes; check 3 of the accept policy compares hub URLs as strings with every trailing `/` removed, which is how the pinned agent-switchboard keeps both a pairing code's hub and an offered room's, shown by running the library, and the host's own check 3 reads the spec's cases the same way; check 4 reads a held room's token source, which the pinned agent-switchboard sets to `shared` exactly when the room's offer carried a hub token, shown by running the library with each of the host's calls made as the host makes them (`base=None`, stores that read nothing from the environment); the wheel ships the schemas and fixtures, and an installed copy loads them. These test the spec. The host's own checks of steps 5 to 9 are held to the request fixtures by `tests/test_catalog.py`, and its decoding to the decode fixtures by `tests/test_output.py` and `tests/test_exec.py`; its check of the envelope and steps 1 to 4 by `tests/test_envelope.py`. |
| `tests/test_store.py`, `tests/test_keyring_backend.py`, `tests/test_state.py`, `tests/vendored/test_pairing_store.py` | Part of invariant "The host's stores and Switchboard footprint are its own". The pair and the work rooms' keys are kept in one OS-keyring item per profile, and the pair store over it passes the eight cases of agent-switchboard v2.6.0's own store test that exercise the store under test; under `--store-file`, they are kept in files of mode 0600 in the host's state folder, with a warning that this is weaker. Neither store writes under `~/.switchboard`, and the calls through them start no process. The host builds its keyring backend itself, keyring's Secret Service backend on Linux or its Keychain backend on macOS, importing that system's module alone, once its class's `priority` says it can be used, and never asks keyring for one. Of `PYTHON_KEYRING_BACKEND` and the `default-keyring` of keyring's `keyringrc.cfg`, read as plain text, only keyring's off switch is honoured: where either names keyring's `null` backend (as the file `keyring --disable` writes does) or its `fail` backend, the host takes keyring's `null` backend before it asks anything of its system's backend, and refuses it as storing nothing, with the sentence that points to `--store-file`, `insecure_keyring` (exit 1 for `pair status` and `pair`, 23 for `run`, and in the pair `status` reports, which exits 0) and `pair --store-file` still pairing (`tests/test_pair.py`, `tests/test_supervision.py`); the file is found under `$XDG_CONFIG_HOME`, or under `~/.config` when that is unset or empty. A backend either names that is not keyring's off switch is not taken, and its module is not imported, though an import would find it; the file's `keyring-path` is never put on `sys.path`, and nothing in the folder it names is imported; and a file that is missing, is not a regular file (a FIFO there is not waited on, and is not read, and on Linux a terminal there does not become the process's controlling terminal), is longer than 4 KiB (the host reads no further than one byte past that), cannot be read or does not parse, including a file keyring itself fails on (one that starts with a byte-order mark, or holds a byte that is not UTF-8), or a home folder that cannot be found, is no setting; the file is otherwise read as keyring reads it: through a symbolic link, without interpolation, with the `[DEFAULT]` section's value as the fallback, the value stripped, and with universal newlines, of which U+0085, U+2028 and U+2029 are not. Where the system's backend cannot be used (no session bus, no Secret Service on it, secretstorage missing, failing or too old, or its module not importing), the host refuses as where keyring had no backend to offer, with the same sentence, `insecure_keyring` (1 for a command, 23 for `run`) and `pair --store-file` still pairing. The keyring backend is taken only when its class is exactly, by `__module__` and `__qualname__`, that one, and both names are checked against the classes keyring installs, on either system. Every other backend is refused when the store is opened and again before every read and every write of the item, and is never asked for the item or written to: keyring's `fail` and `null` backends; a backend that writes secrets unencrypted to a file, under any class name, `PlaintextKeyring` among them; a backend the host does not know (keyrings.alt's `EncryptedKeyring`, KWallet, libsecret, the other system's class, another class of the known module, a subclass of the known class); and every chain of backends, a chain of the known class alone included, which is never asked what it holds. `pair --store-file` still pairs there. What is checked is the class's identity, not how the backend keeps a secret. The sentence where keyring has no backend to offer is unchanged. In a fresh process, with an audit hook and a clean environment, the host's own way to its backend is checked too (`tests/test_keyring_backend.py`): opening the keyring store starts no process (keyring's detection, which it replaced, ran `ldconfig`, `gcc` and `ld` three times each on Linux), keyring chooses no backend and none of its other backends' modules is imported, and the host takes the Keychain backend on macOS and refuses, on Linux with no session bus, with that sentence; the same with a `keyringrc.cfg` naming another backend, planted in the folder its `keyring-path` names, of which nothing is imported, with `sys.path` left as it was. With `PYTHON_KEYRING_BACKEND` naming keyring's `null` or `fail` backend, or the `keyringrc.cfg` that `keyring --disable` writes, opening the keyring store starts no process either, and the host refuses as storing nothing, on both systems, without importing its system's backend module. In `src/lucille_host/`, keyring is never asked for a backend, and the item is read only in `store.Item._load` and written only in `store.Item._save`, through the backend the check returned. The startup checks refuse any `SWITCHBOARD_*` variable, and running as root (uid 0): every verb but `--version`, `--help` and a command with no verb is refused at the front of the command line, before the profile's paths are resolved, so a refused root run creates no folder, lock file or run record (`tests/test_root_refused.py`, which runs each verb as an injected uid 0 against a temporary home and compares the tree before and after, with `--json` and `--json-events` too, and shows `--version`, `--help` and a user unchanged; the library's own refusal is as before). A failure of either store, a full disk under `--store-file` or a keyring backend's own `OSError`, is never read as the hub out of reach. The stores' locks keep out other threads and other processes, a deadlock the kernel reports between a thread of `run` and a command, which is none, is waited out, and a signal `run` handles does not end a wait for one of the host's lock files, on the main thread or another, while one whose handler raises, as Ctrl-C's default handler does, ends it and leaves no lock file open; keyring writes that would only record when something was last used are made at most once an hour. The host's client factory builds no Switchboard client unless `XDG_RUNTIME_DIR` names the host's private runtime folder, and gives each a fresh signing identity. Through the `pair` verbs, `tests/test_pair.py` shows the same (nothing under `~/.switchboard`, every file in the profile's own folders, no process started), and so does `tests/test_rooms.py` through the logic that takes work rooms; over a simulated day of looking up a room before each read and touching every 5 minutes, that logic writes the keyring item about once an hour. Through `run`, `tests/test_read_loop.py` shows that no process is started and that a signer socket planted where Switchboard's client would look for one for the host's agent id, under the `XDG_RUNTIME_DIR` the host was started with and under the system temporary folder with that variable unset, is never connected to, while the host pairs, takes a room and answers a request. `tests/test_end_to_end.py` shows it through the commands a user runs, from `pair` to `forget`: nothing under `~/.switchboard`, every file in the profile's own folders, no process started and no socket bound, with either store. |
| `tests/test_nesting.py` | The host's own depth limit, 128 levels of arrays and objects (`src/lucille_host/nesting.py`), on every JSON and TOML file of its own it reads, so that the answer is the same on every Python version. The check counts what the text opens outside its strings and comments, as the parser reads them: on 400 generated values, in JSON and in TOML, it gives each one's depth as the parser does, brackets in each kind of string, escapes, comments and a multi-line string's extra closing quotes included, and it checks 4 MB in under 5 s. Each depth past the limit is one this Python's parsers read. At the limit each file is read, and one level past it each is refused before it is parsed: a state file (`state_invalid`'s message), the policy, the file store from a file of any name at any level of its folder (the pair store's `store_error`, the share store's refusal), the keyring items of the pair and of the credentials (not one this version wrote), and the Gmail child's arguments (`internal`) and its record of sign-ins (none). The journal reads back a request's body as deep as the read loop takes one. |
| `tests/test_pair.py` | Pairing, as `spec/PROTOCOL.md` §16 and the design record describe it. `pair` refuses while a pair is held, and while another `pair` of the profile runs, in this process or another, so that two cannot both answer a code, and says so, naming itself, where the filesystem cannot lock; refused as root (before any path is resolved, as every verb is, `tests/test_root_refused.py`) or with a `SWITCHBOARD_*` variable, it makes no lock file; `pair status`, `pair confirm` and `pair reject` still work while `pair` asks, and a `run` still starts: `pair` holds `state/.pair.lock`, not `run`'s lock. It takes no pairing code from its command line or its standard input: only the one in Lucille's signed answer to its own Connect request, delivered into that request's drop room. After answering a code it resolves the pair until Lucille has collected the answer, every few seconds and at once on one of the library's notes, so it finishes even when that note is lost, and stops with `leaked` or `expired` when the code leaked or the pairing ran out of time, forgetting what is left of the pair. Once Lucille has collected the answer, `pair` shows the SAS as Lucille's page shows it, and asks for `y` if the two match: `y` opens the local gate, `n` forgets the pair and tells Lucille, any other answer (the SAS itself included) is asked again, and the end of stdin or Ctrl-C keeps the pair for later. On a terminal, what was typed before the question, a whole line or a part of one, answers nothing, and so does a `y` typed while `pair` waited for Lucille; where it cannot be discarded, nothing is asked. While the hub turns away its reads of the pair room, `pair` waits as long as the hub asks. A `y` for a pair another command forgot meanwhile says the pair is gone. `pair status` shows the SAS again while the gate is shut, and `pair confirm`, the `y` given later, opens the gate only on a SAS the host has shown, refusing one it has not (`sas_ready`). A pair with no gate record is refused by `pair`, `pair status` and `pair confirm` alike, with no SAS shown. The deadline is 24 hours from when the SAS is first shown on the host; a resolve that brings the SAS without showing it starts nothing. `n`, `pair reject` and the deadline each forget the pair; a gate opened in time is past no deadline. Neither the gate's record nor the audit log holds the SAS. `pair status`, `pair confirm` and `pair reject` work after `pair` has exited. With the hub out of reach and no SAS on this computer, `pair status` says the pair is `waiting` and that whether Lucille has collected the answer is not known, and `pair confirm` refuses so (`waiting`). Each pair's offered note is shown only after sanitizing. A refusal says why in the host's words, never the library's: each sentence of switchboard 2.6.0's that the host knows, as the installed release writes it (for want of `cryptography`, a file of the file store that is not JSON, a record or an unfinished pairing of either store that is damaged, a code's handshake key that is a low-order point, a pair room record neither side wrote, a healing strategy this release does not know), and as 2.6.0 words it, gives the host's sentence for it, which names no path or value of the library's; any other gives a sentence saying the host does not repeat the library's reason: shown for a known sentence with text after it (after a space, a line break or nothing), with text before it, with a character a reader may not see before or after it, or in capitals, and for one with a variable part past its bounds, a number written in digits other than 0 to 9 included; an absolute path before a sentence that begins with a path reads as a longer path, and a secret put in a variable part within its bounds is a sentence of the library's all the same, and each gives one of the host's sentences and none of the secret; a sentence made at random from pieces of the known ones and of secrets gives one of the host's sentences and none of the secrets; the state is repeated only when it is one of the library's own words or the host's `invalid_sas`, and is `error` otherwise; a refusal with no sentence gives the host's words and the state alone; and spec/CLI.md section 5 lists every sentence a refusal can end with. `policy previous` lists the last policy a pairing set aside, and says so, also after a later pairing from the fresh policy set nothing aside; its help says the same. `pair` refuses a code on its command line, wherever it is, in text and in JSON, and writes it nowhere, nor the code Lucille delivers; `pair begin --json` writes `connect` `code` and `connect` `connecting`, then the code's `pair_code`, `policy_reset` when it set a policy aside, then `accepted`, `waiting` and `sas_pending` with the SAS and the deadline, and no prompt, also when stdin is a terminal, or an `error` with `leaked` or `expired`; its `pair_code` carries the note made safe as the text shows it; `pair status`, `pair confirm` and `pair reject` write their `pair` objects, `pair reject`'s saying whether Lucille was told, Ctrl-C while `pair` waits writes `interrupted`, and `policy previous` writes the policy set aside, with when it was set aside, not when it was last written, and which folders are broad, and refuses a time no file of the host's holds with `--json` and without it alike; a pair named with the longest name the library allows, and a policy of as many folders as it holds, are written whole by every verb that writes them, each object accepted by its schema; `pair_code` names the loopback hub `--hub` named; writing the SAS in JSON starts the deadline, as showing it does, and only the first time. |
| `tests/test_read_loop.py`, `tests/test_envelope.py` | `run`, the read loop, as `spec/PROTOCOL.md` §5, §7 and §9 to §16 describe it, against Switchboard's in-process hub and a stand-in for Lucille, with real threads and long-polls. It reads `lucille.exec` in each room it accepted and may use, and the `pair` channel in pair:<name>, each with its own long-poll running at the same time, always as a peek past a cursor (`since`, 25 s, 200 messages, a 40 s client timeout); a page that came back full moves the cursor no further than its last message; a message the room's key cannot open, or whose body opens but is not JSON or nests more than 64 levels deep, is logged and moved past without losing the messages around it, in a work room and in pair:<name>, or, when the recovery read cannot place it, logged once as `read_stuck` with no move; a cursor is written only after the message's audit line and journal entry; the hub's `retry_after` is honoured. It acts only on `lucille.exec` in a room it accepted, ignores and logs its own posts and messages that are not notes or carry a thread, and refuses in the spec's order, its envelope and steps 1 to 4 held to the request fixtures. A job id is answered once per profile, across rooms, even with two processes writing the journal at once, and remembered until 15 minutes past the later of its `not_after` and its receipt, or a day and 15 minutes past its receipt without a valid `not_after`; a change to an entry already pruned changes nothing. A reply is a note with no thread and an explicit TTL, `(not_after - now) + 900` s at most a day, or `(received - now) + 900` s without a valid `not_after`, is not posted under 1 s, and is retried until that TTL runs out, never becoming `failed`. After a restart, a decision to run with no reply sent gets `failed interrupted` with no second run, an unsent refusal is posted again unchanged, a request received and not decided is decided from the journal as it was first received, and nothing is posted in a room abandoned or left. Abandoning a room kills the child it runs with no reply and leaves the room after; a move of pair:<name> is followed and a request posted in the replacement room before the accept still runs. Nothing is touched, accepted, posted or run before the local gate opens. Pause, kill and drain reach the loop, a drain signal that comes while `run` waits for the record lock still drains once the lock is free, a wake from sleep makes it read the offers, and `state/status.json` says what it sees. |
| `tests/test_rooms.py` | Taking work rooms, as `spec/PROTOCOL.md` §6, §7 and §9 describe it, in the logic the `run` verb drives (driven here one event at a time, with no thread; `tests/test_read_loop.py` drives it through `run`). Before the local gate opens the host reads no offer and accepts nothing; after it, it reads the pair's offers when the gate opens, at start, on the library's share wake, after each resolve of its own except one a failed read of the offers asked for, on a wake from sleep and every 5 minutes, and at once when the wall clock was set back more than a minute since the last (a clock that steps back less, a little at a time, puts the next off by as much and makes no touch of its own), always naming its pair; it resolves at start, on the library's other notes, after a hub error (backing off when a resolve fails, and at once when the wall clock was set back more than a minute since the failed resolve, which holds up no room Lucille offers) and at least daily, and treats each note only as a wake. It writes down each offer the library notes in its share store before deciding, whichever call made the note, one that then refused included; an accept that failed without refusing the offer (the hub or the pair room could not be read) is tried again after the next touch that reads the pair room. It accepts by all five checks of the accept policy: checks 1, 2 and 5 before the accept, where a refusal leaves nothing and keeps the offer to be considered again (shown for checks 2 and 5); checks 3 and 4 right after it, where a failure leaves the room at once, names the check in the log and records that the host holds no work room. It accepts nothing while more than one offer is pending, after dropping offers the share store no longer knows; holds at most 2 rooms; never uses again a room it left (one offered over a new pair is left as soon as it is accepted); and judges whether a room's list names its pair only after a touch of that pair. A room that passes replaces the others over the pair, only then, and a stop between the accept and the replacement is completed at the next start. A heal or a lapse of pair:<name>, found by its generation, Lucille leaving a room, and the end of the pair each abandon the rooms held, which are logged, stopped and left, and the replacement Lucille offers is taken; a move found at start abandons the room of an accept a stop interrupted too, and a heal another command followed first (`pair status`) is found by the next touch, by the generation the store keeps. A `pair confirm` made in time is never expired by a step of `run` or by `pair status` judging the gate beside it, and neither a confirmation nor the showing of the SAS writes the gate's record back for a pair forgotten beside it. Each sharing call says when the hub refused one of its writes for this computer's clock. When Lucille ends the pair, or it is forgotten here, its rooms are left before the pair, each marked abandoned before the first is left, and a room that cannot be left keeps the pair while it is held. When the library itself drops the pair as ended from this side, with a gate record or without one, in a resolve or in a touch of `run`'s (which `run` follows with a resolve in the same step, whatever back-off is owed), or in a leave of `forget`'s (which then says Lucille was told: the pair room says that end already), its rooms are still left, and `run` does not say that another command forgot the pair; a room that cannot be left then keeps nothing: the gate record goes, the command says the pair is gone and that `forget` leaves the room, `status` says so of the room, and `forget`, with no pair held, leaves it. So it does with the rooms and gate record a process stopped right after the library's drop left, the room of an interrupted accept included, which `status` names too, in one line, and not while the pair is held. An answer to a code that is refused once the library saved the pair forgets that pair. A resolve the hub turns away (a 429, which the library returns unchecked rather than raising) is tried again after the hub's `retry_after`, and not before it on a touch that does not reach the pair room. The record lock keeps out another process, not only other threads: showing the SAS, opening the gate, a step, a room's check, `pair reject` and forgetting each wait while another process holds it, and `pair reject` beside a `pair confirm` waits for it and then refuses. A `forget` or `pair reject` that found the pair held, beside a resolve (`pair status`, `pair confirm`, `pair`'s wait) or an answer to a code in which the library drops the pair, waits until what is left of it is forgotten, and says the pair is gone; so it does when that call could not leave a room, which it then leaves, and when the pair was forgotten and paired again under the same name meanwhile, leaving the new pair as it is, confirmed or not. One made while `pair`'s wait is inside a resolve waits for it, and the pair is forgotten once, not also as `ended`; a `forget` with no pair held, beside the answer to a new code under the same name, waits for it and leaves the new gate record. A `forget` beside `pair status` or `pair confirm` waits from their resolve to their judgement, so a pair it forgets is never said to be `waiting`. A `run` whose pair another command forgot stops at its next step, by the gate's record, and touches nothing of a pair made since, under another name or the same; a command that finds its pair gone deletes no other pair's gate record and leaves none of its rooms; and `run` refuses a pair with no gate record. |
| `tests/test_status.py` | `status`, `log` and `forget`, as the design record's "CLI verbs" describes them. `status` makes no hub call, and shows the version, the policy check, the enabled operations and folder aliases, the controls, where the pair is kept, the hub and a configured loopback hub, the pair's state, the work rooms held, or, only when it holds none, that the host holds none with the check its last room failed, and what `run` last saw: whether it runs, its last contact with the hub, and the clock, `skewed` when the hub refused a write of the host's for a clock more than 300 s from its own: a reply it posted, or one the library made for it in a touch, an accept or a leave; and `pair` warns when the hub refuses its answer so. Its text ends with the claim, word for word as the README gives it, with no pair, with a pair whose SAS waits to be confirmed, with a confirmed pair, and in every state below where `--json` is held to the text; the object leaves the claim out. While the pair waits, it says only that no command has seen Lucille collect the answer, since it asks nobody. It never shows the SAS, which `pair status` shows: not while the pair waits, nor once the SAS has reached the host and no command has shown it (`sas_ready`, when it starts no deadline either), nor while the SAS waits to be confirmed, nor past its deadline. `log` and `forget` as C18 and C23 say. A profile runs one `run`: a second, in this process or another, refuses before it does anything. `run` stops when another process forgets its pair, Lucille told or not, and at once when its pair has no gate record; a stop signal while it waits for that `forget` stops it with no error once `forget` is done. While that `forget` waits on a slow hub with the record lock held, `run` kills the request it runs from the room `forget` marked abandoned, and starts none it had read, without waiting for the lock. A `forget` or `pair reject` that another process's `forget` beat says the pair is gone. When the library drops the pair and its work room cannot be left then, the command says so and that `forget` leaves the room, and `forget`, with no pair held, does; with no room left of it, `forget` says that no pair is held. With `--json`, each field of `status` says what its line says, in every state set up here (every work room state, every reason a room was abandoned or left, every reason an operation is disabled, both stores, a loopback hub or none, a pair with no gate record, a confirmed pair's generation or none, and a clock skewed before any skew was measured), each object its schema accepts, and never the SAS, nor starts the deadline; a room record that holds a value no status could report (a name that is not a room's or a pair's, an unknown reason) stops `status` as `state_invalid`; `log --json` writes the same lines oldest first; `pair status` never shows a SAS of another shape than the library's that the store holds, and stops with `pair_unusable`, as JSON and as text; a room that the last record of a `run` that stopped, saying so or not, lists as read is reported as not read; `forget --json` says whether Lucille was told, or that the rooms of a pair already gone were left, or that a room could not be left (`room_not_left`), with the pair kept or gone; `pair begin --json` says when the hub refused its answer for the clock. | | `tests/test_supervision.py` | Supervising `run`, [spec/CLI.md](spec/CLI.md) §13, through the command as an app runs it. Every event is one compact ASCII object of `"v": 1` its schema accepts, `ready` first and `stopped` last, nothing after `stopped`, each line within 16384 bytes and whole across threads; a `request` object never carries an absolute path, the home folder, a folder's path, the profile's folders, the hub, the SAS, a room's workspace or anything an operation returned, a path with a character a terminal must not show is not written, and its `reply_bytes` is the size the audit log's `sent` line gives; an op without an operation name's form, or a request over the protocol's size, or one whose arguments break its operation's schema, says nothing of what it asked; `finished` says what the posted reply says for a request done and one killed, and `abandoned` for one whose room was given up; `host_too_old` follows its refusal; `state` comes right after `ready`, follows the gate, the pause flag, the rooms, the policy, the offers waiting, the hub's last contact, the clock (skewed too) and the check of the room the host left on its own, says a room `read_stuck` and drops a room no longer read, and comes at least every period, also while main waits on a hub that never answers, up to the bound of one step, past which it goes quiet. Each exit code of section 13.3 comes with its `stopped` reason, each reason has its case, and so does each `failed` code of the schema but `hub_error`, which `test_every_reason_has_a_case` names as the one without, the same without `--json-events`, and the table equals the spec's; a failure `run` has no code for, from the command or from one of its threads, is `failed` `internal` and is raised, so that its traceback is shown, with no line in its place. A pair that this computer's side ended in the pair room (a `forget` here that stopped part way) is `ended` when `run`'s first resolve finds it. At its end `run` waits up to 5 s for its last events: a slow reader gets them all, and one that reads nothing holds `run` up no longer. Under `--exit-on-stdin-eof`, no standard input drains `run` before it serves, nor does one that ends during its start checks, with no later check and no hub call, and nor does a stop signal then; bytes written on standard input are discarded; a non-blocking read end is no end and is read blocking; a background job on a terminal is not stopped by its read, and Ctrl-D in the foreground ends it; a standard output nobody reads drains it (without the lifeline, `run` goes on), and the exit code stays the one `run` chose, also in a real process whose stdout, or stderr, is closed; on a real pipe the events are ASCII, a path that is not ASCII among them, and a write on the standard output descriptor does not reach the pipe; a pair record the file store cannot read, or the library refuses, stops `run` before `ready` (23, 20) at every start; a policy file invalid by what it holds, or that cannot be read, holds the offers at once and stops nothing, one changed while an operation runs or while a step waits on the hub holds them until a load says it is valid, and one invalid only by a folder is found by the next periodic load, also while an operation runs; an offer waiting, or an accept a stop interrupted, waits at start while the policy is not checked or invalid, no room is read then, and a verdict on an earlier file lets nothing through; a stalled policy load does not hold up the drain, a lifeline whose audit line cannot be written still stops `run`, and a drain keeps `status.json` written. A damaged digest key stops `run` (`state_invalid`), and so does a damaged `state/cursors.json`, before `ready`, after the store and before the digest key, even with no pair; a shut gate waits, and past its deadline exits 20. The version record refuses an older release (25), before any keyring call or store error, and only for its own profile, comparing each part as a number (0.10.0 comes after 0.9.0, and 10.0.0 after 9.99.99); it holds only a release, and `--allow-downgrade` gets past it without lowering it. Supervision starts no process but the operation's own. |
| `tests/test_runs.py` | The run record (spec/CLI.md section 16): one line per command, written before its work, holding the command's name and never an argument (a value typed on the command line by mistake is not recorded), the runtime's real path only where it is an app's, and nothing as root or for a bad profile; the command is read as the parser reads it (an option by an abbreviation, a verb after `--`); a line that cannot be written (a folder in its place, a symbolic link, a failing write, a lock held elsewhere past 0.2 s) never fails the command; a FIFO in its place never blocks it and is not written, nor is a device or a file linked elsewhere; a line is at most 1,024 bytes and never holds a time that is no finite number; the record is cut by bytes once larger than 64 KiB, reading only its last 32 KiB however large it was made (a 5 MB line, 20 MB of lines, 3 MB with no newline), and the cut is atomic: with eight processes appending at once, no line is lost, every line is in the record or in a record a cut replaced, and a reader never finds the record empty or half written. `tests/test_supervision.py` (`test_second_run_exits_21`) holds that a refused second `run` writes its line and nothing else. |
| `tests/test_end_to_end.py` | The host end to end through the commands, against Switchboard's in-process hub and a stand-in for Lucille: `pair begin` with Lucille's signed answer, `pair confirm`, `run`, a room Lucille offers taken, a `host.describe` request answered with a note with no thread, `status`, `forget` beside the running `run`, which then stops, and `log`. |
| `tests/test_skeleton.py` | What the host builds on and `0.1.0` still leaves to later releases: every part of Switchboard the Switchboard-surface lint allows exists in the pinned library, as do the methods the host calls on what those return, and the managed hub's URL in the library is the host's. |
| `tests/test_conformance.py` | The conformance suite: it passes against the host installed beside the interpreter running the tests, one `PASS` line per registered scenario; a scenario whose expectation does not hold (a refusal expected with another code, a request that runs expected refused) makes it exit 1; so do no scenarios, running as root, and programs missing beside the interpreter. |
