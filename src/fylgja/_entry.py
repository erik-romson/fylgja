"""The ``fylgja`` console script (``[project.scripts] fylgja = "fylgja._entry:main"``).

Stdlib only at module level, so that dispatching a command stays cheap: the hook fast path (a later
step) answers before Typer, FastAPI or Pydantic are imported. Every other command is handed to the
Typer application in :mod:`fylgja.cli.app`, which is imported inside :func:`main`.
"""

from __future__ import annotations

__all__ = ["main"]

PROG_NAME = "fylgja"


def main() -> None:
    """Run the command line. Typer exits the process with the command's exit code."""
    from fylgja.cli.app import app

    app(prog_name=PROG_NAME)


if __name__ == "__main__":  # pragma: no cover - exercised through ``python -m fylgja._entry``
    main()
