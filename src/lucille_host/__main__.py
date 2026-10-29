"""``python -m lucille_host`` runs the same entry point as the ``lucille-host`` command."""

from __future__ import annotations

from lucille_host.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
