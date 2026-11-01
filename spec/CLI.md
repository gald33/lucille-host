# The lucille-host command line, contract version 1

What a program that drives lucille-host (a "shell": the Mac app, a later Linux or Windows app, a script)
can rely on: the verbs it calls, the JSON objects each one writes, and the exit codes.

**Version 1.0, draft.** No released host implements it yet. This document, the JSON Schemas and the golden
fixtures are the contract together. It is separate from the protocol between Lucille and the host
([PROTOCOL.md](PROTOCOL.md)), and has its own version.

## 1. Files

| File | What it is |
|---|---|
| `spec/CLI.md` | This document. |
| `src/lucille_host/spec/cli/v1/<type>.schema.json` | One JSON Schema per object type (section 6). |
| `src/lucille_host/spec/cli/v1/fixtures/<case>.json` | The golden fixtures: a command line, its exit code, and the objects it writes (section 11). |
| `src/lucille_host/spec/__init__.py` | `lucille_host.spec`: finds and reads the files above, and the protocol's, from wherever the package is installed. |

- The schemas and fixtures ship in the wheel as package data, at `lucille_host/spec/cli/v1/`, beside the
  protocol's. The tests load them from an installed wheel (`tests/spec/test_packaged.py`).
- The schemas are JSON Schema draft 2020-12. Each schema's `$id` is
  `https://github.com/gald33/lucille-host/blob/main/src/lucille_host/spec/cli/v1/<file>`. It identifies the
  schema; nothing fetches it.
- Every object in every schema sets `additionalProperties: false`. Where a type has a `state`, the schema is
  a `oneOf` with one closed branch per state, and each branch lists every field it allows as required.
- Patterns are ECMA-262, anchored with `^` and one final `$`, as in the protocol's schemas.
- The host does not validate its own output against these schemas at run time. It holds no JSON Schema
  library. Its tests hold every verb's output to them.

## 2. The rules for a shell

- **A shell never parses human output.** Everything it needs to decide is a field of an object below. The
  words on standard error, and the words in an object's `message`, `summary`, `sends` and `text`, are for
  people. A shell shows them; it never branches on them.
- **A shell never writes the host's files.** Every change goes through a verb: `ops enable`, `roots add`,
  `pause`, `pair confirm`, and the rest. It does not read them either for anything a verb reports.
- **A secret is never passed in argv.** A pairing code goes to `pair --code-stdin --json` on standard input.
  Any secret a later version takes is read the same way. With `--json`, `pair` refuses a code on its
  command line (`code_in_argv`), and does not repeat it. The host never writes a pairing code it read from
  standard input, in JSON or in text. A word given on the command line may be quoted on standard error by
  a usage error; with `--json`, the `usage` object never quotes it.
- **The user compares the SAS.** A shell shows the user the `sas` the host wrote, and runs `pair confirm`
  only when the user says Lucille's page shows the same. It never confirms on its own, and never before it
  has shown that SAS. It calls `pair status` only while it shows the user the `sas` that call returns. To
  learn where the pair is without showing the SAS, it calls `status` (section 7, "The SAS in JSON").
- **A reader ignores a `type` it does not know**, and a field it does not know. Section 12 says what may be
  added within version 1, and what a reader does with a value it does not know.
- **A reader checks `v`.** Every object carries `"v": 1`. An object with another `v` is from another major
  version of this contract; a reader that does not know it stops.
- **A shell validates in its tests, not at run time.** It holds its decoders to the schemas and fixtures of
  the host release it ships with.
- **A shell runs every verb with a time limit.** Any verb that reads the local policy may wait on a folder
  of it that is a stalled network mount, since loading the policy looks at each folder; `roots add` and
  `roots check` also open the folder, and on macOS `roots check` may wait while macOS asks the user about a
  protected folder. A shell stops a verb that runs too long with `SIGINT` or `SIGTERM`.

## 3. Asking for JSON

- `--json` asks for JSON. It may come before the verb, after it, or after the verb's action:
  `lucille-host --json ops list`, `lucille-host ops --json list` and `lucille-host ops list --json` are the
  same. It must come before a `--`: after `--`, every word is an argument (`roots add --json -- docs ~/Docs`).
  A shell writes it in full; the parser also takes a unique prefix of it, as it does of every long option,
  and that is not part of this contract.
- `--profile NAME` selects a separate config folder, state folder and store, so that one computer can hold
  more than one host. It comes before the verb: `lucille-host --profile mac-app --json pause`.
- `--json` with no verb, `--json --version` and `--version --json` write one `version` object and exit 0. As
  without `--json`, `--version` ends the command line at once: nothing after it runs.
- `-h` and `--help` print help for people on standard output, also with `--json`, and exit 0: they are the
  one exception to section 4.1. A shell never asks for help.
- `run` has no JSON form in version 1. `lucille-host run --json` is a usage error (exit 2). Supervising
  `run` comes in a later version (section 13).

## 4. The output

### 4.1 Standard output

- With `--json`, standard output holds only JSON objects, one per line. Each line is one object, written
  whole with its newline and flushed before the host does the next thing, so a reader can act on each line
  as it arrives. A verb that streams (`pair --code-stdin`, `roots check`) writes each object when it is known.
- Each object has `"v": 1` and a `"type"` (section 6), then that type's fields.
- The output is ASCII: every character outside ASCII is written as a JSON `\u` escape. Every string is valid
  Unicode. A file name that is not valid UTF-8 reaches the host as unpaired surrogates; each is written as
  U+FFFD, so such a name can be shown but not passed back.
- Objects are written compactly, with their fields in the order section 6 lists them. A reader depends on
  neither the order nor the spacing.
- Times are UTC in ISO 8601 to the second: `2026-11-01T12:00:00Z`. Durations and skews are numbers of seconds.
- Every number is finite: the host never writes `NaN` or an infinity, which JSON does not have.

### 4.2 Standard error

Standard error stays human, and carries the same text with `--json` as without it: warnings
(`lucille-host: warning: ...`), notes, the failure line (`lucille-host: ...`, `<word>: ...` or
`error: ...`), the parser's usage text, and the traceback of an unexpected error. The lines that a verb
prints on standard output without `--json` are not printed with it: the objects take their place. With
`--json`, `pair` asks nothing, so it writes no prompt.

### 4.3 Exit codes

They are the same with `--json` as without it.

| Exit | Meaning | Standard output with `--json` |
|---|---|---|
| 0 | The verb did what was asked. | Its objects (section 7). |
| 1 | The verb failed or refused. | What it wrote before it stopped, then one `error` object, last. |
| 2 | Usage: the command line could not be parsed, or names a form version 1 does not have. | One `error` object with `code` `usage`, when the line holds `--json` before any `--`. |
| Ended by a signal | Ctrl-C outside a pairing wait, `SIGTERM`, `SIGKILL`. | What was written before; nothing more. |

- With `--json`, every exit of 1 or 2 ends standard output with an `error` object. Ctrl-C while `pair`
  waits for Lucille is the exception to the last row: it ends with an `error` object with `code`
  `interrupted`, exit 1, and the pair is kept, as without `--json`.
- **A shell tells a signal from an exit by how the process ended, never by the number.** A host ended by
  Ctrl-C has exit status "ended by signal 2", which some APIs report as the number 2 beside a separate
  reason (Foundation's `Process.terminationReason` is `.uncaughtSignal`). With `--json`, a 1 or a 2 with
  no `error` object at the end of standard output was a signal.
- **How a shell stops a verb.** `SIGINT` (Ctrl-C) stops `pair` while it waits for Lucille with
  `interrupted`, and keeps the pair. `SIGTERM` ends any verb with nothing more written; the pair is then in
  whatever state the host had reached. After any stop, a shell calls `status` to learn where things are.

## 5. The error object

```json
{"v":1,"type":"error","code":"not_in_catalog","message":"host.nothing is not in this release's catalog"}
```

| Field | Type | |
|---|---|---|
| `code` | string | What failed, from the list in section 10. A shell branches on this. |
| `message` | string | The sentence standard error shows, without its `lucille-host: `, `<word>: ` or `error: ` prefix. For people only. |
| `kind` | `home`, `holds_home` or `root` | For `broad_folder` only: the folder is the home folder, holds it, or is `/`. |

- For `usage`, the `message` is always "the command line could not be parsed; standard error says why":
  the parser's own words, which can quote the command line, are on standard error only.
- An unexpected error (a bug) writes its traceback to standard error and an `error` object with `code`
  `internal`, and exits 1.

## 6. Types

Fields are listed in the order the host writes them, after `v` and `type`. A field is in an object exactly
when the table lists it for that object's state; there are no optional fields. "or null" marks a value that
can be unknown. Arrays are written in the order each type states.

### `version`

| Field | Type | |
|---|---|---|
| `host_version` | string | This release's version, as `lucille-host --version` prints it. |
| `contract` | string, `1.<minor>` | The version of this contract this release writes, `1.0` here. |

### `op`: one catalog operation, or a policy entry this release does not have

| Field | Type | In states |
|---|---|---|
| `name` | string | all |
| `state` | `enabled`, `disabled`, `disabled_by_upgrade`, `unavailable` or `not_in_catalog` | all |
| `enabled` | boolean | all |
| `summary` | string, at most 60 characters | all but `not_in_catalog` |
| `sends` | string, at most 300 characters | all but `not_in_catalog` |
| `sends_file_content` | boolean | all but `not_in_catalog` |
| `diagnostic` | boolean | all but `not_in_catalog` |
| `missing_executable` | string: an absolute path | `unavailable` only |

- `state`:
  - `enabled`: the local policy enables it, and this computer can run it.
  - `disabled`: the local policy does not enable it, and this computer could run it.
  - `disabled_by_upgrade`: the local policy enabled it under a definition this release changed, so it is off
    until enabled again (`ops enable` accepts the new definition). This computer can run it.
  - `unavailable`: this computer cannot run it: `missing_executable` is not an executable file here. The
    policy may enable it or not; `enabled` says which. This state comes first: an operation this computer
    cannot run is `unavailable` even when an upgrade changed its definition, because `ops enable` would
    refuse it.
  - `not_in_catalog`: the local policy names an operation this release does not have.
- `enabled` is the switch's position: true exactly when the local policy enables the operation with this
  release's definition, whether or not this computer can run it. So an `unavailable` operation the policy
  enables has `enabled` true, and one it does not, or enables under an earlier definition, has `enabled`
  false. `disabled_by_upgrade` and `not_in_catalog` are always false.
- `summary` is a few plain words for what it does. `sends` says what Lucille gets when it runs.
- `sends_file_content` is true when a reply can carry the contents of a file, as a search's matching lines
  do. A shell asks the user to acknowledge that before it enables such an operation, and decides so on this
  field, never on the words of `sends`. No operation in this release sends file contents.
- `diagnostic` is true for an operation meant for testing (`host.sleep`). A shell may hide it.
- Section 8 says where `summary`, `sends`, `sends_file_content` and `diagnostic` come from.

### `op_removed`

| Field | Type |
|---|---|
| `name` | string |

`ops disable` took out of the local policy an operation this release's catalog does not have. The next
`ops list` has no row for it, so a shell drops the row.

### `root`: one folder alias

| Field | Type | |
|---|---|---|
| `alias` | string, `^[a-z][a-z0-9_-]{0,31}$` | The name Lucille uses. |
| `path` | string: an absolute path | The folder, as the local policy holds it. |
| `broad` | boolean | It is the home folder, holds it, or is `/`, and was added with `--allow-home-or-root`. |

### `root_removed`

| Field | Type |
|---|---|
| `alias` | string |

### `root_check`: whether the host can open one folder

| Field | Type | In states |
|---|---|---|
| `alias` | string | all |
| `path` | string | all |
| `state` | `ok`, `denied_by_macos`, `denied`, `missing` or `error` | all |
| `errno` | string, `^E[A-Z0-9]{1,15}$` | `error` only |

Section 9 says how each state is decided. The text line shows the alias and the state; the path is the one
`roots list` shows.

### `alias_suggestion`

| Field | Type |
|---|---|
| `alias` | string, `^[a-z][a-z0-9_-]{0,31}$` |

### `controls`

| Field | Type | |
|---|---|---|
| `paused` | boolean | Whether the host is paused, once the verb is done: true after `pause` and `kill`, false after `resume`. |

### `status`: what `lucille-host status` shows, as one object

| Field | Type | |
|---|---|---|
| `host_version` | string | |
| `policy` | object, below | The local policy check. |
| `paused` | boolean | |
| `store` | `keyring` or `file` | Where the pair is kept: the OS keyring, or files under `--store-file`. |
| `hub` | string | Lucille's hub. |
| `loopback_hub` | string or null | The loopback hub `pair --hub` named, when one is configured. |
| `pair` | object, below | Where the pair is. **Never the SAS.** |
| `work_rooms` | array of objects, below | The work rooms held, by name, then the room of an interrupted accept. |
| `last_left_because` | `check_3`, `check_4`, `left_before` or null | When no room is held and the host left its last one on its own: the check that room failed ([PROTOCOL.md](PROTOCOL.md) section 6). |
| `offers_waiting` | integer | Offers of a room that wait to be judged. |
| `run` | object, below | What the latest `lucille-host run` last wrote. |
| `last_hub_contact` | string (a time) or null | When `run` last reached the hub. |
| `clock` | object, below | |

`status` reads only this computer's files and its store, and makes no hub call. It reports what it can:
when the store that keeps the pair cannot be read, `pair` says so (`unreadable`), and every other field is
still filled in. A file of the host's own that holds something else still stops it (`state_invalid`), and so
does one that holds a value the host never writes there: a time or a skew that is not a finite number in
its range, or a pid out of range.

`policy`:
- `{"state": "ok", "file": <path or null>, "enabled": [...], "disabled": [...], "folders": [...]}`. `file`
  is null when there is no policy file yet, and the policy is the fresh one. `enabled` lists the operations
  that run here, by name. `disabled` lists `{"name": ..., "why": ...}` for each the policy enables that does
  not run here, `why` being `disabled_by_upgrade`, `unavailable` or `not_in_catalog`, as `ops list` gives its
  `state`, in that order of `why` and then by name. `folders` lists the aliases, by alias.
- `{"state": "invalid", "error": <why>}`: the policy file is invalid, and the host acts on nothing.

`pair`, by `state`. It shares state words with the `pair` type, not its fields: a decoder keeps the two
apart.

| `state` | Other fields | |
|---|---|---|
| `none` | | This computer holds no pair. |
| `waiting` | `name` | Lucille has not collected this computer's answer yet. |
| `sas_ready` | `name` | The SAS has reached this computer, and no command has shown it yet. `pair status` shows it. |
| `sas_pending` | `name`, `deadline` | The SAS was shown, and is not confirmed. |
| `confirmed` | `name`, `generation` (integer or null) | The local gate is open. `generation` is the generation of the pair room this computer last followed. It is null when the store does not say, or holds one above 2^53 - 1, which Lucille's side could set and a JSON reader may not keep exactly. |
| `refused` | `name` | The pair has no local gate record, so it can never be confirmed here; `pair reject` forgets it. |
| `expired` | `name`, `deadline` | The SAS was not confirmed in time; the next command that resolves the pair forgets it. |
| `several_pairs` | `message` | The profile holds more than one pair, and the host keeps one. |
| `unreadable` | `code`, `message` | The store that keeps the pair could not be read: `code` is `insecure_keyring`, `store_error` or `pair_unusable` (section 10). |

Each of `work_rooms`, by `state`. With `pair` `unreadable`, whether a pair is held is not known, so no room
is reported as `pair_gone` or `accept_interrupted`.

| `state` | Other fields | |
|---|---|---|
| `reading` | `name`, `accepted_at` | `run` reads it. |
| `not_read` | `name`, `accepted_at` | Held, and `run` is not reading it now. |
| `read_stuck` | `name`, `accepted_at` | A message there cannot be opened, and `run` reads no further there. |
| `abandoned` | `name`, `accepted_at`, `why` | Nothing is taken from it; its leave is tried again, or `forget` leaves it. |
| `pair_gone` | `name`, `accepted_at` | The pair it was taken over is gone from this computer; `forget` leaves it. |
| `accept_interrupted` | `name` | An accept that a stop interrupted may have kept it, over a pair that is gone; `forget` leaves it. |

`why` is one of `replaced` (a newer room replaced it), `left_by_lucille`, `pair_moved` (pair:<name> was
healed or lapsed), `not_over_current_pair` (its list no longer names the pair), `pair_over`, `gone` (it left
the share store without this host leaving it), `check_3`, `check_4` and `left_before`. These words, and
those of `last_left_because`, are for branching; the text says them in words.

`run`, by `state`: `none` (no record); `running` with `pid` and `as_of`; `not_seen` with `pid` and `as_of`
(it stopped without saying so); `stopped` with `as_of`.

`clock`, by `state`: `ok` with `skew_s`; `skewed` with `skew_s` (a number or null); `unknown`. `skew_s` is
positive when this computer's clock is behind the hub's. `skewed` means the hub refuses what the host writes,
because this computer's clock is more than 300 s from the hub's.

### `log_line`

| Field | Type | |
|---|---|---|
| `text` | string | One line of the audit log, as `lucille-host log` prints it: printable ASCII, any other byte shown as `?`. |

The audit log's own format is not part of this contract. A line is one record the host wrote, but the log
is writable by the user's account and proves nothing, and its records change as the host grows. A shell
shows the text as it is, or not at all; it decides nothing from it.

### `forget`

| `state` | Other fields | |
|---|---|---|
| `forgotten` | `name`, `lucille_told` (boolean) | The pair's work rooms were left, then the pair was forgotten here. `lucille_told` says whether Lucille was told; if not, Lucille keeps its half until the computer is removed there. |
| `left` | `name` | No pair was held. The work rooms still held over a pair already gone from this computer were left. Lucille was told nothing. |

### `pair_code`: what a pairing code says, before it is answered

| Field | Type | |
|---|---|---|
| `name` | string, `^[a-z0-9][a-z0-9-]{0,31}$` | The pair's name. |
| `hub` | string | The hub the code names: Lucille's, or the loopback hub `--hub` named. |
| `note` | string, at most 200 characters | The code's note, made safe as the text output makes it: control, reordering and separator characters, and unpaired surrogates, become spaces, and it is trimmed; `""` when it has none. Whoever made the code chose it. |

### `policy_reset`

No other fields. The local policy was set aside for the new pair: only `host.describe` is enabled, and no
folder is held. `policy previous` lists what was set aside. It is written only when a policy was moved; a
pairing from the fresh policy moves nothing. It comes before the code is answered, so a `pair` that then
fails may already have written it; the policy was set aside all the same.

### `pair`

| `state` | Other fields | Written by |
|---|---|---|
| `none` | | `pair status` |
| `accepted` | `name`, `clock_skewed` (boolean) | `pair --code-stdin` |
| `waiting` | `name` | `pair --code-stdin`, `pair status` |
| `sas_pending` | `name`, `sas` (`^[0-9]{4}-[0-9]{4}$`), `deadline` | `pair --code-stdin`, `pair status` |
| `confirmed` | `name` | `pair status`, `pair confirm` |
| `rejected` | `name`, `lucille_told` (boolean) | `pair reject` |

`clock_skewed` is true when the hub refused the answer because this computer's clock is more than 300 s from
its own: the answer was kept, and is sent again while `pair` waits, but the clock must be set right first.

### `previous_policy`

| `state` | Other fields | |
|---|---|---|
| `none` | | No pairing has set a policy aside. |
| `found` | `set_aside_at`, `ops` (array of names, by name), `folders` (array of `{"alias", "path", "broad"}`, by alias) | The last policy a pairing set aside. Nothing uses it. `set_aside_at` is when it was set aside, to the second, and listing it never changes it. A pairing sets the policy aside in the second the pairing began or a later one, so a shell offers it back only when `set_aside_at` is at or after the second in which the shell started the pairing it is showing. |

## 7. Verbs

Each verb writes the objects below on success, and an `error` object (section 5) on failure, with one of
the codes listed for it, or one of the codes every verb can give: `usage` (exit 2), `internal`,
`bad_profile`, `unsupported_platform` and `os_error`.

| Verb | On success | Codes |
|---|---|---|
| `--version`, or no verb | one `version` | |
| `ops list` | one `op` per catalog entry, in catalog order, then one per policy entry the catalog does not have, by name | `policy_invalid` |
| `ops enable <op>` | the `op`, as it is now | `policy_invalid`, `policy_lock_failed`, `not_in_catalog`, `unavailable` |
| `ops disable <op>` | the `op`, as it is now; `op_removed` for a name this release's catalog does not have | `policy_invalid`, `policy_lock_failed`, `not_enabled` |
| `roots list` | one `root` per alias, by alias | `policy_invalid` |
| `roots add [--allow-home-or-root] [--] <alias> <path>` | the `root` added | `policy_invalid`, `policy_lock_failed`, `bad_alias`, `alias_taken`, `too_many_roots`, `not_a_folder`, `own_folder`, `on_proc`, `broad_folder`, `unstorable_path` |
| `roots add --suggest-alias <path>` | one `alias_suggestion` (section 9) | `policy_invalid` |
| `roots remove <alias>` | one `root_removed` | `policy_invalid`, `policy_lock_failed`, `no_such_alias` |
| `roots check [<alias>]` | one `root_check` per alias, by alias, or for the alias named | `policy_invalid`, `no_such_alias` |
| `pause`, `resume`, `kill` | one `controls` | |
| `status` | one `status` | `startup_refused`, `runtime_folder`, `state_invalid` |
| `log [--limit N]` | one `log_line` per line, oldest first; none when the log is empty | `bad_limit` |
| `forget` | one `forget` | `none`, `several_pairs`, `room_not_left`, `pair_unusable`, `startup_refused`, `insecure_keyring`, `store_error`, `runtime_folder`, `state_invalid` |
| `pair --code-stdin [--hub URL] [--store-file]` | `pair_code`; `policy_reset` if a policy was set aside; `pair` `accepted`; `pair` `waiting`; `pair` `sas_pending` | `code_in_argv`, `bad_hub`, `startup_refused`, `busy`, `lock_failed`, `held`, `no_code`, `bad_code`, `wrong_hub`, `code_has_token`, `policy_lock_failed`, `answer_failed`, `hub_error`, `leaked`, `expired`, `ended`, `none`, `no_gate`, `interrupted`, `pair_unusable`, `room_not_left`, `insecure_keyring`, `store_error`, `runtime_folder`, `state_invalid` |
| `pair status` | one `pair`: `none`, `waiting`, `sas_pending` or `confirmed` | `bad_options`, `several_pairs`, `leaked`, `expired`, `ended`, `none`, `no_gate`, `pair_unusable`, `room_not_left`, `startup_refused`, `insecure_keyring`, `store_error`, `runtime_folder`, `state_invalid` |
| `pair confirm` | one `pair` `confirmed` | `bad_options`, `none`, `several_pairs`, `waiting`, `sas_ready`, `no_gate`, `expired`, `leaked`, `ended`, `pair_unusable`, `room_not_left`, `startup_refused`, `insecure_keyring`, `store_error`, `runtime_folder`, `state_invalid` |
| `pair reject` | one `pair` `rejected` | `bad_options`, `none`, `several_pairs`, `already_confirmed`, `pair_unusable`, `room_not_left`, `startup_refused`, `insecure_keyring`, `store_error`, `runtime_folder`, `state_invalid` |
| `policy previous` | one `previous_policy` | `previous_invalid` |
| `run` | no JSON form in version 1 | `usage` |

Notes:
- **`pair` with `--json`** reads the code from standard input, and only with `--code-stdin`:
  `pair --json` without it is a usage error, and `pair <code> --json` is refused (`code_in_argv`). It never
  asks anything, and stops once the SAS is shown (`sas_pending`, exit 0). Once it has answered the code, it
  resolves the pair every 5 s, and at once when a note of the pairing library arrives, sending an unsent
  answer again, until Lucille has collected the answer. It may write `waiting` more than once; a reader
  takes the latest. A pairing that ends early writes an `error` with `leaked` (the code leaked: start again
  with a fresh code, and share it with nobody), `expired` (it did not finish in time: start again), `ended`
  or `none`, and exits 1. `pair` refuses while a pair is held (`held`: disconnect first) and while another
  `pair` of the profile runs (`busy`). `--hub` and `--store-file` are for testing against a local hub, and
  for a computer with no keyring; both are saved for the profile.
- **Standard input of `pair --code-stdin`** is one line: the code, with or without a newline after it.
  The host reads that line and nothing more. It may exit before it reads anything (`code_in_argv`,
  `bad_hub`, `busy`, `held`, ...), so a shell writes the code at once, closes standard input, and ignores
  a broken pipe.
- **`pair status`** resolves the pair once, sending an unsent answer again. A shell that was relaunched
  learns where the pair is from `status`, and calls `pair status` once its pairing screen is up; it calls it
  every 5 s while it reports `waiting`.
- **`pair confirm`** is the user's "yes, Lucille's page shows the same SAS". `pair reject` is "no".
- **`ops enable`** and **`ops disable`** write the operation's `op` object as it is once the change is made,
  so a shell sets its switch from the host's answer, not from what it asked for.
- **`roots add`** refuses a folder that is the home folder, holds it, or is `/`, unless
  `--allow-home-or-root` is given (`broad_folder`, with `kind`). A shell asks the user before it passes that
  flag. A shell passes `--` before the alias and the path, so that neither is read as an option.
- **`roots add --suggest-alias <path>`** takes no alias, no folder argument and no other option. A path that
  starts with `-` is written `--suggest-alias=<path>`. It writes nothing, and takes no lock: another verb may
  take the alias before `roots add` runs, which then refuses with `alias_taken`, and the shell asks again.
- **`roots check`** exits 0 whatever it found: a folder that cannot be opened is a result, not a failure.
- **`forget`** with no pair held, and no work room left over, refuses with `none`: there is nothing to
  forget. A shell disconnecting takes that as done.
- **`log`** writes no object, and exits 0, when the audit log has no lines yet; standard error says so.

### The SAS in JSON

This release's pairing gate is the one in
[decisions/0001-host-design.md](../decisions/0001-host-design.md), "Pairing": the computer shows the SAS,
the user compares it with Lucille's page, and confirms on the computer. So `sas_pending` carries the SAS
(`sas`) and the deadline, from `pair --code-stdin --json` and from `pair status --json`, as their text does.

Writing the SAS in JSON counts as showing it: the first time either verb writes it, the deadline starts, as
when the text shows it. The program that reads it shows it to the user, and the user's `y` (`pair confirm`)
must come after that (section 2). `lucille-host status` never reports the SAS, in JSON too: its `pair`
object has no field that could hold it, and it starts no deadline.

[decisions/0002-macos-app.md](../decisions/0002-macos-app.md) §11.2 proposes a gate in which the user
types Lucille's number into the computer, and the computer never shows its own SAS before the gate opens.
It is a proposal, pending the maintainer's decision, and is not built. If it is built, `sas_pending` loses
its `sas` field, and that is a new major version of this contract (section 12).

## 8. The catalog's words

Each catalog entry carries four things for shells beside its definition, in `src/lucille_host/ops/`:
`summary`, `sends`, `sends_file_content` and `diagnostic`. `summary`, `sends` and `diagnostic` are outside
the entry's definition hash and outside `CATALOG_VERSION`: they describe what the entry does, and decide
none of it. So a release that only changes them turns no operation off, and leaves the catalog version as
it was. `sends_file_content` is part of the definition hash when it is true, so a release that makes an
entry send file contents turns that operation off until the user enables it again, and with it sees the
acknowledgement a shell asks for. Their words must not make any claim on [PRIVACY.md](../PRIVACY.md)'s
list of what this project must never claim; a test checks them against it.

| Operation | `summary` | `sends` | `sends_file_content` | `diagnostic` |
|---|---|---|---|---|
| `host.describe` | Describe this computer's host | Lucille gets the host's version and protocol, the operating system family, which release's catalog it has, which operations are on, the names you gave your folders (never where they are), and a fingerprint of the local policy. | false | false |
| `host.sleep` | Wait up to 4 minutes, for testing | Lucille gets how long the wait took and how it ended. | false | true |
| `files.sha256` | Check a file's fingerprint | Lucille gets a code that identifies the file's exact contents, not the contents. | false | false |

## 9. Folder aliases and folder checks

### 9.1 `roots add --suggest-alias`

It suggests an alias made from the folder's name, which `roots add` will take, and which no folder of the
local policy has. It reads the policy and nothing else, and writes nothing.

1. The name is the last part of the folder's absolute path (`""` for `/`).
2. Each character becomes:
   - an ASCII letter: itself, lowercase; an ASCII digit, `-` or `_`: itself;
   - a full-width form (U+FF01 to U+FF5E): the ASCII character it stands for, then as above;
   - a Latin letter with a mark, or a Latin ligature, from U+00C0 to U+017F: its plain letters, lowercase
     (`é` is `e`, `ß` is `ss`, `æ` is `ae`, `œ` is `oe`, `ø` is `o`, `þ` is `th`, `ł` is `l`);
   - anything else, letters of other scripts included: `-`.
3. Each run of `-` becomes one `-`. Every `-` and `_` at either end is removed.
4. An empty result becomes `folder`. A result that starts with a digit gets `folder-` in front.
5. It is cut to 32 characters, and every `-` and `_` at its end is removed again.
6. If a folder of the policy already has that alias, `-2` is added, or `-3`, and so on, to the first that is
   free: the alias is cut first so that the whole stays within 32 characters, and every `-` and `_` at the
   end of what is left is removed, then the suffix is added.

`Documents` gives `documents`, `2026 Taxes` gives `folder-2026-taxes`, `Café Notes` gives `cafe-notes`, and
a second `Taxes` gives `taxes-2`. A name in another script, such as `Документы`, gives `folder`. A shell may
offer the user an alias of its own instead (one written in Latin letters, say); `roots add` checks it as it
checks any alias.

### 9.2 `roots check`

For each folder, it opens it and reads one entry, as an operation working there would need to. It sends
nothing to Lucille or the hub, and writes nothing.

| State | When |
|---|---|
| `ok` | The folder opened, and one entry could be read (an empty folder is `ok`). |
| `denied_by_macos` | On macOS, `EPERM`: macOS's privacy protection refuses the host this folder. |
| `denied` | `EACCES`, or `EPERM` on Linux: the folder's permissions refuse it. |
| `missing` | `ENOENT` or `ENOTDIR`: nothing is there, or not a folder. |
| `error` | Any other error; `errno` names it (`EIO`, `ELOOP`, `ESTALE`, ...). |

It checks one folder after another, so a stalled mount holds back the folders after it. Before it checks
any, it loads the local policy, and loading the policy looks at each folder (section 2). So a stalled mount
anywhere in the policy holds back every `roots check`, whether or not it names an alias: running
`roots check <alias>` once per folder, side by side, does not get around it. A shell runs it with a time
limit, as it runs every verb, and stops it when the limit passes.

## 10. Error codes

| Code | What failed |
|---|---|
| `usage` | The command line could not be parsed, or names a form version 1 does not have (exit 2). |
| `internal` | An unexpected error; the traceback is on standard error. |
| `bad_profile` | A `--profile` name is lowercase letters, digits, `-` and `_`, starting with a letter, at most 32. |
| `unsupported_platform` | This is not Linux or macOS. |
| `os_error` | A file the host needed could not be read or written; the message names it. |
| `startup_refused` | The host does not start here: it runs as root, or `SWITCHBOARD_*` variables are set. |
| `insecure_keyring` | The OS keyring would keep the pair where it is not safe, or stores nothing. |
| `store_error` | The store that keeps the pair could not be used. |
| `runtime_folder` | The host's private runtime folder is missing, or is not private. |
| `state_invalid` | A file of the host's own holds something else. |
| `policy_invalid` | The local policy file is invalid. Nothing changes it but a verb. |
| `policy_lock_failed` | The policy lock could not be taken on this filesystem, so the policy was not changed. |
| `not_in_catalog` | That operation is not in this release's catalog. |
| `unavailable` | That operation cannot run here: its executable is missing. |
| `not_enabled` | That operation is not enabled. |
| `bad_alias` | An alias is lowercase letters, digits, `-` and `_`, starting with a letter, at most 32. |
| `alias_taken` | A folder already has that alias. |
| `too_many_roots` | The policy holds at most 64 folder aliases. |
| `not_a_folder` | That path is not a folder. |
| `own_folder` | That folder is where lucille-host keeps its own files. |
| `on_proc` | That folder is on `/proc`. |
| `broad_folder` | That folder is the home folder, holds it, or is `/`, and `--allow-home-or-root` was not given. `kind` says which. |
| `unstorable_path` | That path cannot be stored in the policy (its name is not valid UTF-8). |
| `no_such_alias` | No folder has that alias. |
| `previous_invalid` | The policy a pairing set aside is invalid, or the time it was set aside (its file's modification time) is not from 1970 to the end of 9999. |
| `bad_limit` | `--limit` takes 1 or more. |
| `bad_options` | `pair status`, `pair confirm` and `pair reject` take no options. |
| `code_in_argv` | A pairing code was given on the command line, with `--code-stdin` or `--json`. |
| `bad_hub` | `--hub` names a hub that is not `http://127.0.0.1:<port>` or `http://localhost:<port>`. |
| `busy` | Another `pair` of this profile is running. |
| `lock_failed` | The lock that keeps one `pair` per profile could not be taken on this filesystem. |
| `held` | This computer already holds a pair: disconnect first. |
| `no_code` | No pairing code was given on standard input. |
| `bad_code` | That is not a pairing code. |
| `wrong_hub` | The code is for another hub, not Lucille's (or for a loopback hub `--hub` did not name). |
| `code_has_token` | The code carries a hub token, and the host takes only codes that do not. |
| `no_gate` | The pair has no local gate record, so it can never be confirmed here: `pair reject`, then start again. |
| `already_confirmed` | `pair reject` of a pair that is confirmed: `forget` ends it. |
| `answer_failed` | The pairing library refused to answer the code. Nothing was answered. |
| `hub_error` | The hub could not be reached, or answered with an error. Nothing was decided; the same command can be tried again, with the same code. |
| `leaked` | The pairing code leaked: another computer answered it, or somebody interfered. Start again with a fresh code. |
| `expired` | The pairing, or the SAS, was not finished in time. The pair is forgotten. Start again. |
| `ended` | The pair was ended, by Lucille or on this computer. |
| `none` | This computer holds no pair, or another command forgot it meanwhile. |
| `waiting` | Lucille has not collected this computer's answer yet, so there is no SAS to confirm. |
| `sas_ready` | This computer has not shown the SAS yet: `pair status` shows it. |
| `interrupted` | Ctrl-C while `pair` waited. The pair is kept; `pair status` resumes. |
| `pair_unusable` | The pairing library refused the pair here. |
| `room_not_left` | A work room could not be left, so its keys would stay behind; the command can be tried again, or `forget` leaves it. |
| `several_pairs` | The profile holds more than one pair, and the host keeps one. |

## 11. Golden fixtures

Each fixture is one command line and what it writes:

```json
{
  "description": "ops enable, with --json after the action",
  "argv": ["ops", "enable", "host.sleep", "--json"],
  "exit": 0,
  "stdout": [
    {"v": 1, "type": "op", "name": "host.sleep", "state": "enabled", "enabled": true, "...": "..."}
  ]
}
```

- `argv` is what follows `lucille-host`. `stdout` is the objects in order. Each fixture's `description`
  says what was set up before the command ran.
- Paths in a fixture are under `/home/me`, as Linux's folders name them; the version is a sample, and so is
  each pair name, SAS, time and generation. The tests put the real values in their place before they
  compare: the profile's folders, the home folder, the version, the pair's name, Lucille's SAS, a time, and
  the executable this computer's family names for an operation. A run record's pid and skew are set by
  the test.
- Every object in every fixture validates against its type's schema, and the tests run every fixture's
  command line and compare. They also run it without `--json`, from the same set-up, and check that the
  exit code and standard error are the same.
- `ops-list-sends-file-content` is written with a catalog entry stubbed to send file contents, so that a
  shell's decoder sees `sends_file_content` true. No operation in this release does.

## 12. Versioning

- `v` names the major version of this contract, independent of the host's version and of the protocol's.
  `version`'s `contract` names the minor too.
- **Within version 1**, a later release may add a type, add a field to a type (to one of its states), add
  an `error` code, add a verb, an action or a flag, or give `run` a JSON form. Each of these is a minor
  version. The schemas of each release describe exactly what that release writes. A reader ignores a type
  and a field it does not know.
- **Closed and open values.** Every `state` is closed: a new state is a new major version. So are `store`,
  `policy.disabled[].why`, `root_check`'s `state`, `broad_folder`'s `kind` and `status.pair`'s `code`. Three
  are open, and may gain values within version 1:
  - `error`'s `code`: a reader treats one it does not know as a failure: the exit code says so, and it
    shows the `message`.
  - `work_rooms[].why`: a reader treats one it does not know as "abandoned for another reason".
  - `last_left_because`: a reader treats one it does not know as "left by the host on its own".
- A field a reader must act on for safety is never added within version 1.
- **Anything else is a new major version:** removing or renaming a type or a field, changing a field's JSON
  type or what it means, or adding a value to a closed value. A new major gets its own schemas, under
  `cli/v2/`.
- Changes are recorded at the end of this document, by contract version, with the host release that first
  writes them.

## 13. Not in version 1

- Supervising `run`: an event stream from `run`, and its own exit codes. `run` keeps its codes meanwhile
  (0 stopped on purpose, 1 failed).
- `policy reset`, `diagnostics` and removing the host's data
  ([decisions/0002-macos-app.md](../decisions/0002-macos-app.md) §8).
- The typed pairing gate and the Connect flow (decisions/0002 §11.2 and §11.4): proposals, not built.
- A typed history of what the host did, for a shell to show in words. `log_line` carries text only, which a
  shell shows as it is.
- During pairing, whether the hub could be reached and whether it refuses this computer's clock, beside
  `waiting`. They can be added to `waiting` as fields within version 1.
- `pair` stopping on its own when its standard input closes, so that a shell that dies cannot leave a
  `pair` holding the profile's pairing lock until the code expires. Meanwhile a shell stops its `pair`
  before it exits.

## Changes

- **1.0** (no release yet): the first version.
