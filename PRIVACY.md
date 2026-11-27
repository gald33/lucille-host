# Privacy

**Status: `0.1.0`, the first release, for the repository owner's own use.** The claim below is the one
`0.1.0` makes. [CLAIMS.md](CLAIMS.md) says which code and tests back each of its sentences, and which
tests are still pending.

## The claim

This is the whole claim. The same text is in the [README](README.md). `lucille-host status` is meant to
print it from the first release on; the development version's does not. [CLAIMS.md](CLAIMS.md) maps each
sentence to the code and test behind it, or marks it as Lucille policy that this repository cannot prove.

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

## Notes on the claim

- **Why it names Lucille's assistant.** This program cannot see how Lucille's backend changes. So the
  claim says that Lucille's assistant can read what an operation returns, whatever Lucille does with it
  today.
- **Some sentences are Lucille's policy, not this program.** Who at Lucille can read what is returned,
  the 24-hour blank, what Lucille's page shows, and what Lucille does when the computer is removed are
  Lucille's policy. This repository cannot prove them, and [CLAIMS.md](CLAIMS.md) marks each one
  "Lucille policy as of <date>; not proven by this repository". The claim assumes the 24-hour blank is
  in effect; this repository cannot tell whether it is.
- **"Encrypted", and where the far end is.** Messages are sealed between your computer and Lucille's
  backend. The hub cannot read them. Lucille's backend can. That is why this project does not call the
  traffic "end-to-end encrypted" without naming the far end.
- **Gmail.** The Gmail operations sign in to `imap.gmail.com` with a Google app password, not with
  OAuth. The password stays on your computer and goes only to Google, in the sign-in command. What a
  Gmail operation returns is not kept on your computer: it goes to Lucille like any other operation's
  reply, so Lucille's operators, assistant and AI provider can read the messages Lucille asks to read.
  Google, not this program, decides whether app passwords may be used with an account, and it can stop
  allowing them. The privacy boundary and the questions about Google's policies are in
  [decisions/0003](decisions/0003-local-credentials.md), "Second use".
- **When the claim must change.** This repository changes the claim before it ships any change of its own
  that would make one of the claim's sentences false. That Lucille amends the claim before it changes its
  own stated policy in a way that would make a sentence false is Lucille policy as of 2026-10-27; not
  proven by this repository. A second copy of returned output kept outside the 24-hour blank, for
  example in a chat answer, would be such a change.

## What this project must never claim

Each line below is a claim that would be false or misleading. This project does not make it, in this
repository, in a release, or in what `lucille-host` prints.

- **"Your data never leaves your device", "private" with no qualifier, or "Lucille cannot see your
  files".** What an enabled operation returns goes to Lucille's backend.
- **"End-to-end encrypted", without saying where the far end is.** The traffic is sealed between your
  computer and Lucille's backend, and Lucille reads it.
- **"Sandboxed", or "safe to enable anything".** Operations run with your own permissions. An allowlist
  is not a sandbox.
- **"Read-only" as a guarantee.** It describes catalog entries, and it is checked by review.
- **"A hacked Lucille cannot read your files".** A compromised or prompt-injected Lucille backend can
  request any enabled operation, with any valid argument, whenever the host runs. Only your local policy
  bounds it.
- **"Passwords and keys never leave".** They leave if they are inside a file an enabled operation reads.
- **"Tamper-proof log", "only lucille-host can read its key", or "secrets are removed from output".** The
  log is writable by your user account. Malware running as your user can read the host's stores (see
  [THREAT_MODEL.md](THREAT_MODEL.md)). Removing secrets from output is a best-effort convenience, not a
  guarantee.
- **"Removing it ends the pairing on both sides", without the caveat.** The other side is told only when
  the hub is reachable.
- **"Deleted", without the caveats.** The 24-hour blank is Lucille's policy, not code in this repository.
  It does not reach Lucille's database backups, which this repository cannot audit. Requests and replies
  also sit sealed on the hub until their time-to-live runs out.
- **"Verified" for a user who has not verified, "reproducible" for a release whose rebuild was not green,
  or "audited" before anyone has audited it.** As of 2026-10-27, nobody has audited this program.
- **"Your email stays on your computer" or "Lucille never sees your email".** What a Gmail operation
  returns goes to Lucille's backend.
- **"Approved by Google", "exempt from Google's verification" or "no security assessment needed".**
  Signing in over IMAP with an app password uses no OAuth client, so Google's OAuth verification does
  not apply to it, but this project has no ruling from Google on its use and claims none.
- **Anything about the AI provider's own retention.**
- **That open source, or a verified release, proves anything about Lucille's backend.** It proves things
  about this program only.
