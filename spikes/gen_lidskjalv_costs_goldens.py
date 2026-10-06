#!/usr/bin/env python3
"""Generate the token-sum parity goldens (WP5.4, §4.7, A21). Dev-only: never imported or packaged.

Reads Lidskjalv's ``_COSTS_JQ`` program from its checkout (read-only), runs ``jq`` over every
sanitized transcript fixture and writes ``tests/fixtures/lidskjalv/costs/<name>.json``. The tests
compare the Python port in ``fylgja.collectors.claude.costs`` with those files and never run or
need Lidskjalv or ``jq``.

    python3 spikes/gen_lidskjalv_costs_goldens.py [--lidskjalv DIR] [--out DIR]

Fixtures: ``tests/fixtures/lidskjalv/transcripts/*.jsonl`` (named ``<stem>``),
``tests/fixtures/ratatosk/<scenario>/*.jsonl`` (named ``ratatosk-<scenario>-<stem>``) and
``tests/fixtures/usage/transcripts/*.jsonl`` (named ``<stem>``: the synthetic ones that carry the
edge cases, a torn last line, repeated messages, side chains and values of every type).

The program is run exactly as ``_costs_one_session`` runs it, over the file with ``jq -n -R``, but
without the ``+ {session_id: $sid}`` that function appends: the id is the caller's, not a fact of
the transcript. The golden is the program's own output, so it has the program's four names.
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
PROGRAM_START = "_COSTS_JQ='"


def extract_program(script: str) -> str:
    """The text of the single-quoted ``_COSTS_JQ`` assignment."""
    start = script.index(PROGRAM_START) + len(PROGRAM_START)
    end = re.search(r"^'$", script[start:], re.MULTILINE)
    if end is None:
        raise SystemExit("cannot find the end of _COSTS_JQ")
    return script[start : start + end.start()]


def fixtures() -> list[tuple[str, Path]]:
    found: list[tuple[str, Path]] = []
    for path in sorted((REPO / "tests/fixtures/lidskjalv/transcripts").glob("*.jsonl")):
        found.append((path.stem, path))
    for path in sorted((REPO / "tests/fixtures/ratatosk").glob("*/*.jsonl")):
        found.append((f"ratatosk-{path.parent.name}-{path.stem}", path))
    for path in sorted((REPO / "tests/fixtures/usage/transcripts").glob("*.jsonl")):
        found.append((path.stem, path))
    return found


def run_jq(program: str, transcript: Path) -> object:
    result = subprocess.run(
        ["jq", "-n", "-R", program, str(transcript)],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if result.returncode != 0:
        raise SystemExit(f"jq failed on {transcript.name}: {result.stderr.strip()}")
    return json.loads(result.stdout)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lidskjalv", type=Path, default=DEFAULT_LIDSKJALV)
    parser.add_argument("--out", type=Path, default=REPO / "tests/fixtures/lidskjalv/costs")
    args = parser.parse_args(argv)
    script = (args.lidskjalv / "lib" / "costs.sh").read_text(encoding="utf-8")
    program = extract_program(script)
    args.out.mkdir(parents=True, exist_ok=True)
    for name, transcript in fixtures():
        sums = run_jq(program, transcript)
        target = args.out / f"{name}.json"
        target.write_text(
            json.dumps(sums, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(target.relative_to(REPO))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
