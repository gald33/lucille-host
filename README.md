# lucille-host

## Status: no release yet

There is no release. This repository holds version `0.1.0.dev0`: the guards, CI, the draft protocol spec
([spec/PROTOCOL.md](spec/PROTOCOL.md)), the draft `--json` contract of the command line for apps that
drive it ([spec/CLI.md](spec/CLI.md)), the catalog, the executor, the local policy, the audit log,
`pause`, `resume` and `kill`, and the parts that reach Lucille: `pair`, `run`, `status`, `log` and
`forget`, with `run`'s event stream and exit codes for an app that supervises it, `policy reset`, and
`credential`, which stores credentials for online services on this computer, `github.read`, an
operation, off by default, that reads one GitHub REST API endpoint with a credential stored here, and
four Gmail operations, off by default, that list, search and read a Gmail account over IMAP with a Google
app password stored here (`gmail.check`, `gmail.mailboxes`, `gmail.search`, `gmail.read`).
Those are tested against Switchboard's in-process hub and a stand-in for Lucille, not against Lucille
itself. It also holds `verify`, which checks that the host installed is a release PyPI serves, attested
by this repository's release workflow, and that its files are the release's
([VERIFYING.md](VERIFYING.md), step 2); with no release to check, it is tested with PyPI and sigstore
stood in for. Do not install it.

## What it will be

lucille-host will be a small program you install on your own computer. It will let your own Lucille
account run a closed set of named operations on that computer, and nothing else.

- It will pair with your Lucille account through Switchboard pairing. Switchboard is an open-source
  message hub and client ([agent-switchboard](https://github.com/gald33/switchboard), MIT).
- The operations will be built into each release. You will choose which ones are switched on, and on
  which folders.
- It will run no AI model and open no listening port. It will never run code, a shell or a command line
  sent by Lucille.

The source is public so that anyone can check that the program does what it claims.

## Installing

- Do not install it yet. When a release exists, this README will name its version. You will install it
  with `pipx install lucille-host==<version>` or `uv tool install lucille-host==<version>`. It needs
  Python 3.11 or later, and it installs one command, `lucille-host`.
- Until this README names a release, a package called `lucille-host` on PyPI did not come from this
  repository.

## What lucille-host claims

The text below is the claim the first release must meet. The same text is in [PRIVACY.md](PRIVACY.md).
`lucille-host status` is meant to print it from the first release on; the development version's does not.
[CLAIMS.md](CLAIMS.md) maps each sentence to the code and test behind it, or marks it "Lucille policy as
of <date>; not proven by this repository". Until a release exists, none of it describes a program you
can install.

> lucille-host does only what you switch on, on your computer. It can run only the operations built into
> this release (listed in `src/lucille_host/ops/` and by `lucille-host ops list`), and only on folders
> you add. Nothing Lucille sends can add an operation, add a folder, or loosen a limit. It never runs code,
> a shell or a command line sent by Lucille, and the programs it starts get none of its keys and none of
> your environment variables, except those of `github.read` and the four `gmail.` operations: each reads
> a credential you saved for it, and gets `HOME` (macOS) or `DBUS_SESSION_BUS_ADDRESS` and
> `XDG_RUNTIME_DIR` (Linux), which the keyring needs.
>
> Any file no enabled operation reads stays on your computer. So do your passwords, keys and browser
> sessions, unless they are inside a file an enabled operation reads.
>
> What leaves it: each request Lucille makes (which operation, and its inputs, such as a file name or a
> search pattern), and what an enabled operation returns, up to 64 KiB of output and 64 KiB of errors.
> `files.search` returns lines of the files it searches. `github.read` returns GitHub's reply to the read
> it names. `gmail.mailboxes` returns the names of your Gmail mailboxes and labels. `gmail.search` returns
> how many messages matched, and the sender, recipients, subject, date, size, ids and whether it is unread
> of up to 50 of them, and `gmail.read` returns one
> message's headers, up to 32 KiB of its text, and its attachments' names, types and sizes, never their
> contents. Lucille learns the names of the credentials it asks these operations to use, never their
> values.
> Lucille also learns the host's version, OS family, enabled operation names and folder aliases (never
> folder paths), each run's exit code and duration, a fingerprint that changes when your local policy
> changes, and, to within a day, when lucille-host was last running and when your computer last
> answered.
>
> Requests and replies travel through Lucille's Switchboard hub, encrypted between your computer and
> Lucille's backend. The hub, and the network in front of it, can see your computer's network address,
> when it connects, and roughly how large each message is; they cannot read the messages. Lucille's
> backend can. A request your computer has not answered within its wait (an hour unless chosen otherwise,
> never more than 23 h 45 min) expires and the job fails. With `github.read` on, your computer also
> connects to `api.github.com`, for each read Lucille asks for, with the token you saved for it. With a
> `gmail.` operation on, it connects to `imap.gmail.com`, for each request to it, with the Gmail address
> and app password you saved for it, and only reads: it changes, moves, sends and deletes no mail.
>
> Lucille stores what is returned with the job. Lucille's operators, Lucille's assistant and the AI
> provider it uses can read it. Lucille blanks it 24 hours after the job ends. That is Lucille's policy,
> not code in this repository, and it does not reach Lucille's database backups.
>
> Every request your computer receives, what it decided, and a fingerprint of what it sent back are in a
> log on your computer (`lucille-host log`). `lucille-host pause` stops it starting anything, even
> offline, and `lucille-host kill` also stops what is running. Removing the computer in Lucille stops
> Lucille using the pairing at once. Lucille then deletes its half and tries once, best effort, to tell
> your computer through its Switchboard hub; Lucille's page says if your computer was not told. If Lucille
> cannot use its hub at all for 7 days, it deletes its half without telling your computer.
> `lucille-host forget` deletes the computer's half at once, and tells Lucille if the hub is reachable
> then. A side that is never told keeps its half until it is removed or forgotten there too. The source is
> public at github.com/gald33/lucille-host.

## How it will work, in short

1. **Pairing.** `lucille-host pair` asks for your own chat username, then shows a one-time connect code.
   You send it to Lucille in your chat. Lucille answers through its Switchboard hub, signed, and the
   computer takes the pairing code in that answer only if the signature is Lucille's, the account is the
   one you typed, and the code names the computer's own request. No pairing code is ever pasted. You
   compare a short code on both screens and confirm on the computer (`y` at the prompt, or
   `lucille-host pair confirm`). Until you confirm, the host takes no work room, posts nothing and runs
   nothing.
2. **Work.** While `lucille-host run` runs, Lucille offers the host a work room over the pair and posts a
   request there. The host checks the request against your local policy, runs the operation or refuses
   it, and posts its reply.
3. **Control.** `lucille-host pause`, `kill` and `forget` work on the computer, even offline. Nothing
   Lucille sends can unpause the host or widen its policy. `lucille-host status` and `log` show what the
   host holds and what it did.
4. **Checking.** `lucille-host verify`, installed with the `[verify]` extra, checks that the host is a
   release PyPI serves, attested by this repository's release workflow, and that the files installed are
   the release's. It is the installed code checking itself: [VERIFYING.md](VERIFYING.md) says how to
   check from a fresh install, and what it cannot show.

`0.1.0.dev0` has these verbs, but no release exists yet.

What Lucille does in these steps is Lucille policy as of 2026-10-27; not proven by this repository.
This repository proves only the host's side. The full design is
[decisions/0001-host-design.md](decisions/0001-host-design.md).

## What is in this repository

| Path | What it is |
|---|---|
| `src/lucille_host/` | The program: the `lucille-host` command and everything it runs. |
| `src/lucille_host/ops/` | The complete list of operations a release can run: `host.describe`, `host.sleep` and `files.sha256` in `0.1.0.dev0`. |
| `src/lucille_host/_exec.py` | The only place in the runtime (the program without its conformance suite) allowed to start a process. |
| `src/lucille_host/_switchboard.py` | The only module in the runtime allowed to import or use `switchboard`. |
| `src/lucille_host/conformance/` | A test suite that plays a hostile Lucille against the installed program. No scenarios yet. |
| [spec/](spec/) | The protocol between Lucille and the host, `lucille.exec/1`: [PROTOCOL.md](spec/PROTOCOL.md) and its changelog. And the contract between the command line and any app that drives it, `--json`: [CLI.md](spec/CLI.md). Drafts; no release implements them yet. |
| `src/lucille_host/spec/` | The JSON Schemas and golden fixtures of the protocol and of the command line's `--json` contract, which ship in the wheel. |
| `tools/spec/` | Writes the protocol fixtures for each place in each schema pattern; a test checks that the committed fixtures are what it writes. |
| `tools/guards/` | Checks CI runs on every change. They catch accidental and casual violations; they are not a sandbox or a defence against a determined author, and maintainer review of every pull request is the backstop. [VERIFYING.md](VERIFYING.md) describes each one and what it cannot see. |
| [macos/](macos/README.md) | A first build of Capsule, the Mac app, which lets Lucille use only the folders you choose: a menu bar app, in Swift, that drives the host's command line. Not a release. |
| `tools/macos/` | The scripts that build Capsule and check the built app, the files that pin what it is built from, and the reviewed list of what the build removes from its Python runtime. [macos/README.md](macos/README.md) says how to build it. |
| [PRIVACY.md](PRIVACY.md) | The claim, and what this project must never claim. |
| [CLAIMS.md](CLAIMS.md) | Each sentence of the claim, mapped to its code and test. |
| [THREAT_MODEL.md](THREAT_MODEL.md) | What the host defends against, and what is left. |
| [VERIFYING.md](VERIFYING.md) | How a third party checks the program (draft). |
| [SECURITY.md](SECURITY.md) | How to report a vulnerability, and what a bad release means for you. |
| [decisions/](decisions/) | Design records: [0001](decisions/0001-host-design.md), the host, [0002](decisions/0002-macos-app.md), Capsule, the Mac app, and [0004](decisions/0004-running-as-root.md), running as root (decided: refuse uid 0 for every verb but `--version` and `--help`; built). |

## Development

You need `uv` and Python 3.11 or later. The lock file, `uv.lock`, pins every dependency exactly.

```sh
uv sync --locked --extra conformance
uv run pytest
uv run lucille-host --version    # prints: lucille-host 0.1.0.dev0
```

The guards catch accidental and casual violations; they are not a sandbox or a defence against a
determined author, and maintainer review of every pull request is the backstop. They run on their own
as well. Each exits 0 when clean, 1 on a violation and 2 on a usage error:

```sh
uv run python tools/guards/workflow_lint.py .
uv run python tools/guards/ast_lint.py .
uv run python tools/guards/switchboard_lint.py .
uv run python tools/guards/macos_lint.py .
uv run python tools/guards/private_strings.py .
```

The private-string check reads its list from a repository secret that is never committed. Without the
list it fails closed, so it fails on a machine that does not have it. [VERIFYING.md](VERIFYING.md) says
why.

## Security

Report vulnerabilities privately. See [SECURITY.md](SECURITY.md).

## License

MIT. See [LICENSE](LICENSE).
