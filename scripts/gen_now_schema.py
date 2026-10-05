"""Write ``docs/api/now.schema.json`` from the ``fylgja.now/1`` models (dev-only).

Run it by hand after changing :mod:`fylgja.api.now_models` (and bumping ``NOW_SCHEMA``), then
commit the output::

    uv run --frozen python scripts/gen_now_schema.py           # write the file
    uv run --frozen python scripts/gen_now_schema.py --check   # exit 1 if it is out of date

Tests never write the file; ``tests/unit/test_now_schema_guard.py`` compares it with the models.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from fylgja.api.now_models import now_json_schema

TARGET = Path(__file__).resolve().parent.parent / "docs" / "api" / "now.schema.json"


def render(schema: dict[str, Any]) -> str:
    """The committed text: sorted keys, two-space indent, a final newline."""
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="only report whether it is current")
    parser.add_argument("--output", type=Path, default=TARGET)
    args = parser.parse_args(argv)
    text = render(now_json_schema())
    if args.check:
        current = args.output.read_text(encoding="utf-8") if args.output.exists() else ""
        if current != text:
            print(f"{args.output} is out of date; run scripts/gen_now_schema.py", file=sys.stderr)
            return 1
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text, encoding="utf-8")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
