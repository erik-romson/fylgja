#!/usr/bin/env python3
"""Generate the facts parity goldens (WP1.7, A21). Dev-only: never imported, never packaged.

Reads Lidskjalv's ``_SUMMARY_JQ`` program from its checkout (read-only), runs ``jq`` over every
sanitized transcript fixture and writes ``tests/fixtures/lidskjalv/facts/<name>.json``. The tests
compare the Python port in ``fylgja.collectors.claude.facts`` against those files and never run
or need Lidskjalv.

    python3 spikes/gen_lidskjalv_facts_goldens.py [--lidskjalv DIR] [--out DIR]

Fixtures: ``tests/fixtures/lidskjalv/transcripts/*.jsonl`` (named ``<stem>``) and
``tests/fixtures/ratatosk/<scenario>/*.jsonl`` (named ``ratatosk-<scenario>-<stem>``).
The session id passed to the program is the golden's name with ``ratatosk-`` shortened to ``rt-``
(a name that ends in ``sk-…`` would look like a secret to the fixture-hygiene scan).
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
PROGRAM_START = "_SUMMARY_JQ='"


def extract_program(script: str) -> str:
    """The text of the single-quoted ``_SUMMARY_JQ`` assignment."""
    start = script.index(PROGRAM_START) + len(PROGRAM_START)
    end = re.search(r"^'$", script[start:], re.MULTILINE)
    if end is None:
        raise SystemExit("cannot find the end of _SUMMARY_JQ")
    return script[start : start + end.start()]


def fixtures() -> list[tuple[str, Path]]:
    found: list[tuple[str, Path]] = []
    for path in sorted((REPO / "tests/fixtures/lidskjalv/transcripts").glob("*.jsonl")):
        found.append((path.stem, path))
    for path in sorted((REPO / "tests/fixtures/ratatosk").glob("*/*.jsonl")):
        found.append((f"ratatosk-{path.parent.name}-{path.stem}", path))
    return found


def session_id(name: str) -> str:
    return name.replace("ratatosk-", "rt-")


def run_jq(program: str, name: str, transcript: Path) -> object:
    result = subprocess.run(
        ["jq", "-n", "-R", "--arg", "sid", session_id(name), program, str(transcript)],
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
    parser.add_argument("--out", type=Path, default=REPO / "tests/fixtures/lidskjalv/facts")
    args = parser.parse_args(argv)
    script = (args.lidskjalv / "bin" / "lidskjalv.sh").read_text(encoding="utf-8")
    program = extract_program(script)
    args.out.mkdir(parents=True, exist_ok=True)
    for name, transcript in fixtures():
        facts = run_jq(program, name, transcript)
        target = args.out / f"{name}.json"
        target.write_text(
            json.dumps(facts, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"{target.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
