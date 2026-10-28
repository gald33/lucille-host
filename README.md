# lucille-host

## Status: no release yet

There is no release. This repository holds a skeleton, version `0.1.0.dev0`: the package layout, the
guards and CI. The skeleton prints its version and does nothing else. None of the behaviour described
on this page is built yet.

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

The text below is the claim the first release must meet. The same text is in [PRIVACY.md](PRIVACY.md),
and `lucille-host status` will print it. [CLAIMS.md](CLAIMS.md) maps each sentence to the code and test
behind it, or marks it "Lucille policy as of <date>; not proven by this repository". Until a release
exists, none of it describes a program you can install.

> lucille-host does only what you switch on, on your computer. It can run only the operations built into
> this release (listed in `src/lucille_host/ops/` and by `lucille-host ops list`), and only on folders
> you add. Nothing Lucille sends can add an operation, add a folder, or loosen a limit. It never runs code,
> a shell or a command line sent by Lucille, and the programs it starts get none of its keys and none of
> your environment variables.
>
> Any file no enabled operation reads stays on your computer. So do your passwords, keys and browser
> sessions, unless they are inside a file an enabled operation reads.
>
> What leaves it: each request Lucille makes (which operation, and its inputs, such as a file name or a
> search pattern), and what an enabled operation returns, up to 64 KiB of output and 64 KiB of errors.
> `files.search` returns lines of the files it searches. Lucille also learns the host's version, OS
> family, enabled operation names and folder aliases (never folder paths), each run's exit code and
> duration, a fingerprint that changes when your local policy changes, and, to within a day, when
> lucille-host was last running and when your computer last answered.
>
> Requests and replies travel through Lucille's Switchboard hub, encrypted between your computer and
> Lucille's backend. The hub, and the network in front of it, can see your computer's network address,
> when it connects, and roughly how large each message is; they cannot read the messages. Lucille's
> backend can. A request your computer has not answered within its wait (an hour unless chosen otherwise,
> never more than 23 h 45 min) expires and the job fails.
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

1. **Pairing.** Lucille shows you a one-time Switchboard pairing code (`swbp1_...`).
   `lucille-host pair <code>` accepts it. You compare a short code on both screens and confirm on the
   computer. Until you confirm, the host posts nothing and runs nothing.
2. **Work.** Lucille offers the host a work room over the pair and posts a request there. The host checks
   the request against your local policy, runs the operation or refuses it, and posts one reply.
3. **Control.** `lucille-host pause`, `kill` and `forget` work on the computer, even offline. Nothing
   Lucille sends can unpause the host or widen its policy.

None of these verbs exists in `0.1.0.dev0`.

What Lucille does in these steps is Lucille policy as of 2026-10-27; not proven by this repository.
This repository proves only the host's side. The full design is
[decisions/0001-host-design.md](decisions/0001-host-design.md).

## What is in this repository

| Path | What it is |
|---|---|
| `src/lucille_host/` | The program. In `0.1.0.dev0` it only prints its version. |
| `src/lucille_host/ops/` | The complete list of operations a release can run. Empty in `0.1.0.dev0`. |
| `src/lucille_host/_exec.py` | The only place in the runtime (the program without its conformance suite) allowed to start a process. It starts none yet. |
| `src/lucille_host/_switchboard.py` | The only module in the runtime allowed to import or use `switchboard`. |
| `src/lucille_host/conformance/` | A test suite that plays a hostile Lucille against the installed program. No scenarios yet. |
| `tools/guards/` | Checks CI runs on every change. They catch accidental and casual violations; they are not a sandbox or a defence against a determined author, and maintainer review of every pull request is the backstop. [VERIFYING.md](VERIFYING.md) describes each one and what it cannot see. |
| [PRIVACY.md](PRIVACY.md) | The claim, and what this project must never claim. |
| [CLAIMS.md](CLAIMS.md) | Each sentence of the claim, mapped to its code and test. |
| [THREAT_MODEL.md](THREAT_MODEL.md) | What the host defends against, and what is left. |
| [VERIFYING.md](VERIFYING.md) | How a third party checks the program (draft). |
| [SECURITY.md](SECURITY.md) | How to report a vulnerability, and what a bad release means for you. |
| [decisions/](decisions/) | Design records. |

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
uv run python tools/guards/private_strings.py .
```

The private-string check reads its list from a repository secret that is never committed. Without the
list it fails closed, so it fails on a machine that does not have it. [VERIFYING.md](VERIFYING.md) says
why.

## Security

Report vulnerabilities privately. See [SECURITY.md](SECURITY.md).

## License

MIT. See [LICENSE](LICENSE).
