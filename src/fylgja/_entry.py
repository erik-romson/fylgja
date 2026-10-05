"""The ``fylgja`` console script (``[project.scripts] fylgja = "fylgja._entry:main"``).

Stdlib only at module level, so that dispatching a command stays cheap. ``fylgja hook …`` is
handed to the stdlib-only :mod:`fylgja.hook` before Typer, FastAPI or Pydantic are imported;
Claude Code runs it on every event (WP2.1). Every other command goes to the Typer application in
:mod:`fylgja.cli.app`, which is imported inside :func:`main`.
"""

from __future__ import annotations

import sys

__all__ = ["HOOK_COMMAND", "main"]

PROG_NAME = "fylgja"
HOOK_COMMAND = "hook"


def main() -> None:
    """Run the command line. Typer exits the process with the command's exit code.

    ``hook`` never returns: :func:`fylgja.hook.main` ends the process with exit code 0.
    """
    if len(sys.argv) > 1 and sys.argv[1] == HOOK_COMMAND:
        from fylgja import hook

        hook.main(sys.argv[2:])
    from fylgja.cli.app import app

    app(prog_name=PROG_NAME)


if __name__ == "__main__":  # pragma: no cover - exercised through ``python -m fylgja._entry``
    main()
