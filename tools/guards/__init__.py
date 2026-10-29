"""CI guards for this repository.

Each guard is a script, ``python tools/guards/<name>.py [ROOT]``, and a ``check`` function that
returns the violations it found. A script exits 0 when the tree is clean, 1 on violations, and 2
on a usage error, including a tree with nothing for it to check: a guard that looked at nothing
does not report clean.

- ``workflow_lint``: GitHub-hosted runners only, named by an exact list of image labels; every
  action pinned by commit SHA and every image by digest, local actions included; no reusable
  workflow from another repository.
- ``ast_lint``: no shell, no ``eval``/``exec``/``compile``, no dynamic import anywhere; process
  launchers called directly, with argv their only positional argument; in the runtime package no
  process is started outside ``_exec.py``; the conformance package starts only the local hub and
  the installed ``lucille-host``.
- ``switchboard_lint``: the runtime imports and uses Switchboard in one module; nothing registers,
  invites, or mints a shared room; only the parts of Switchboard the host needs are used.
- ``private_strings``: no tracked file contains a string from a list kept in a repository secret.

They read source code and configuration. Each module's docstring is its exact list of rules, and
ends with what that guard cannot see: they catch the forms of what they forbid that are written out
in a file, not every indirection (``getattr`` with a name built at run time, for one), and not code
outside this repository. So the guards catch accidental and casual violations. They are not a
sandbox, and not a defence against an author who sets out to get past them: a maintainer reviews
every pull request before it merges, and that review is the backstop.
"""
