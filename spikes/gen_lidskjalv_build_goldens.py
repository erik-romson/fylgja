#!/usr/bin/env python3
"""Generate the build-fold parity goldens (WP5.4, §4.7, A21). Dev-only: never imported or packaged.

Reads Lidskjalv's ``_BUILDS_FOLD_JQ`` program from its checkout (read-only), runs ``jq`` over every
sanitized runs-response fixture in ``tests/fixtures/project/gh/actions-*.json`` and writes
``tests/fixtures/lidskjalv/builds/<fixture>.json``. The tests compare the Python port in
``fylgja.collectors.project.builds`` with those files and never run or need Lidskjalv.

    python3 spikes/gen_lidskjalv_build_goldens.py [--lidskjalv DIR] [--out DIR]

The program takes the repository as ``--arg owner_repo``; the fixtures use ``example/repo``, which
the test passes to the port as well. The goldens are the program's own output, so they use its
names (``run_duration``, ``typical_duration``, epoch seconds for ``run_started_at``); the test maps
them to Fylgja's.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_LIDSKJALV = Path.home() / "projects" / "erik-romson" / "lidskjalv" / "main"
PROGRAM_START = "_BUILDS_FOLD_JQ='"
OWNER_REPO = "example/repo"


def extract_program(script: str) -> str:
    """The text of the single-quoted ``_BUILDS_FOLD_JQ`` assignment."""
    start = script.index(PROGRAM_START) + len(PROGRAM_START)
    end = re.search(r"^'$", script[start:], re.MULTILINE)
    if end is None:
        raise SystemExit("cannot find the end of _BUILDS_FOLD_JQ")
    return script[start : start + end.start()]


def run_jq(program: str, fixture: Path) -> object:
    result = subprocess.run(
        ["jq", "--arg", "owner_repo", OWNER_REPO, program, str(fixture)],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if result.returncode != 0:
        raise SystemExit(f"jq failed on {fixture.name}: {result.stderr.strip()}")
    return json.loads(result.stdout)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lidskjalv", type=Path, default=DEFAULT_LIDSKJALV)
    parser.add_argument("--out", type=Path, default=REPO / "tests/fixtures/lidskjalv/builds")
    args = parser.parse_args(argv)
    script = (args.lidskjalv / "lib" / "builds.sh").read_text(encoding="utf-8")
    program = extract_program(script)
    args.out.mkdir(parents=True, exist_ok=True)
    for fixture in sorted((REPO / "tests/fixtures/project/gh").glob("actions-*.json")):
        fold = run_jq(program, fixture)
        target = args.out / fixture.name
        target.write_text(
            json.dumps(fold, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(target.relative_to(REPO))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
