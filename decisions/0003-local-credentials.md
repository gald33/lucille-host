# 0003: Local credentials (draft)

**Status: storage only.** `src/lucille_host/credentials.py` can save, list and remove a credential
for an online service, kept only on this computer. Nothing yet writes to it from the command line, and
nothing Lucille sends can reach it. This record says what was built, and what is still to be decided
before a credential kept here can be used.

## Why

A Lucille user can choose where a credential they give Lucille (a password, an app token) is kept:

1. on their own computer, the private option, in this program;
2. in Lucille's own storage, encrypted with Lucille's key, as integration tokens are kept today;
3. later, with a third-party execution runtime.

This repository builds only the first. The other two belong to Lucille's backend.

## What is built

- **A separate keyring item.** Service `lucille-host.credentials`, user name the profile, one JSON
  document `{"v": 1, "credentials": {"<service>/<name>": {"service", "name", "value", "saved_at"}}}`.
  It is not the pair's item (`store.py`), so:
  - a damaged credential item never risks the only copy of the pair secret;
  - `forget`, which ends the pair, leaves the user's credentials alone.
- **The same keyring rule as the pair.** `store.check_backend` refuses a backend that stores nothing or
  stores in the clear. There is no file fallback like `--store-file`: a credential is never written to
  a plain file by this program.
- **Names, never values.** Service and credential names follow Lucille's rule (`^[a-z0-9][a-z0-9._-]{0,63}$`).
  `names()` returns names and save times only. Error messages hold nothing read from the item, and a
  value is returned only by `value()`.
- **An item this version did not write is left as it is.** It may hold the only copy of the user's
  credentials.
- **Unreachable from Lucille.** No runtime module but `credentials.py` imports it
  (`tests/test_credentials.py`, `test_nothing_else_in_the_runtime_imports_it`). No request, operation or
  reply can write, read or list it.

## What is not decided

1. **How a credential gets in.** The value must be entered on the computer and must never pass through
   Lucille, or this would not be the private option. The likely shape is `lucille-host credential set
   <service> <name>`, reading the value from stdin with no echo, plus `list` and `remove`. That needs
   `spec/CLI.md` objects for `--json`, and the Mac app's panel. Not built.
2. **Whether Lucille learns the names.** Lucille's backend keeps a row for each credential the user said
   is on their computer, so it can show the user where each one is. Whether the host reports its names
   (for example in `host.describe`) or the user tells Lucille, is open. Reporting names would add a
   sentence to the claim: "Lucille learns the names of the credentials you saved here, never their values."
3. **How a credential is used. This is the decision that matters.** A credential on this computer is
   useful only if something on this computer uses it. Today the host makes outbound HTTPS to one hub only
   (0001, "Transport"), and runs only catalog operations. Possible answers, none chosen:
   - **a catalog operation that calls a named service** (for example an `http.*` family with a
     catalog-listed set of hosts per operation), with the credential added by the host and never put in
     the reply. This ends "outbound HTTPS to one hub only" and changes the claim, the threat model and the
     AST and Switchboard lints;
   - **a local program the user installs** that asks the host for a credential. This widens who can read
     the item past this program;
   - **handing the value to Lucille for each use.** This is not the private option, and the claim says
     passwords stay on the computer. Ruled out unless the claim changes.

   Until one is chosen, `value()` has no caller, and the claim's sentence "So do your passwords, keys and
   browser sessions [stay on your computer]" covers credentials kept here without change.
