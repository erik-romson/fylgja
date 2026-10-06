"""Parity of the Python token sums with Lidskjalv's ``_COSTS_JQ`` (WP5.4, §4.7, A21).

``tests/fixtures/lidskjalv/costs/<fixture>.json`` is the recorded output of the jq program, written
once by ``spikes/gen_lidskjalv_costs_goldens.py``. The tests never run or need Lidskjalv or ``jq``.
They also hold the incremental path to the one-shot one: the transcript tail feeds the same sums
line by line as the file grows, torn lines and rewrites included.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fylgja.collectors.claude import facts
from fylgja.collectors.claude.costs import TokenAccumulator, TokenSums, tokens_of
from fylgja.collectors.claude.transcript import SessionTranscript, TranscriptTail
from fylgja.testing.fakes import FakeFS

ROOT = Path(__file__).resolve().parent.parent / "fixtures"
GOLDENS = ROOT / "lidskjalv" / "costs"
NAMES = ("input", "output", "cache_read", "cache_write")


def _fixtures() -> list[tuple[str, Path]]:
    found = [(p.stem, p) for p in sorted((ROOT / "lidskjalv" / "transcripts").glob("*.jsonl"))]
    found += [
        (f"ratatosk-{p.parent.name}-{p.stem}", p)
        for p in sorted((ROOT / "ratatosk").glob("*/*.jsonl"))
    ]
    found += [(p.stem, p) for p in sorted((ROOT / "usage" / "transcripts").glob("*.jsonl"))]
    return found


def _entries(path: Path) -> list[dict[str, Any]]:
    """What ``jq -R`` plus ``fromjson?`` sees: every line that is a JSON object."""
    decoded = (facts.decode_line(line) for line in path.read_text(encoding="utf-8").split("\n"))
    return [entry for entry in decoded if entry is not None]


def _golden(name: str) -> dict[str, int]:
    golden: dict[str, int] = json.loads((GOLDENS / f"{name}.json").read_text(encoding="utf-8"))
    return golden


def test_there_is_a_golden_for_every_fixture_and_no_other() -> None:
    names = {name for name, _ in _fixtures()}
    assert len(names) == len(_fixtures()), "two fixtures share a golden name"
    assert names, "no transcript fixture found"
    assert {p.stem for p in GOLDENS.glob("*.json")} == names


def test_every_golden_has_exactly_the_four_sums() -> None:
    for path in GOLDENS.glob("*.json"):
        assert set(json.loads(path.read_text(encoding="utf-8"))) == set(NAMES), path.name


def test_the_goldens_are_not_all_zero() -> None:
    """A golden of zeros proves little: the parity set must hold transcripts with real usage."""
    sums = {name: sum(_golden(name).values()) for name, _ in _fixtures()}
    assert sum(1 for total in sums.values() if total > 0) >= 5, sums
    assert {"usage-none", "test-1"} & {n for n, total in sums.items() if total == 0}


@pytest.mark.parametrize(("name", "path"), _fixtures(), ids=[n for n, _ in _fixtures()])
def test_token_sums_equal_the_golden(name: str, path: Path) -> None:
    assert tokens_of(_entries(path)).as_dict() == _golden(name)


@pytest.mark.parametrize(("name", "path"), _fixtures(), ids=[n for n, _ in _fixtures()])
def test_feeding_line_by_line_equals_the_full_recount(name: str, path: Path) -> None:
    accumulator = TokenAccumulator()
    for entry in _entries(path):
        accumulator.feed(entry)
    assert accumulator.sums() == tokens_of(_entries(path))
    assert accumulator.sums().as_dict() == _golden(name)


@pytest.mark.parametrize(("name", "path"), _fixtures(), ids=[n for n, _ in _fixtures()])
def test_every_split_of_a_transcript_gives_the_same_sums(name: str, path: Path) -> None:
    entries = _entries(path)
    whole = tokens_of(entries)
    for cut in range(len(entries) + 1):
        accumulator = TokenAccumulator()
        accumulator.feed_all(entries[:cut])
        accumulator.feed_all(entries[cut:])
        assert accumulator.sums() == whole, (name, cut)


# -- the transcript tail: the sums follow the file as it grows ---------------------------------

PATH = Path("/projects/p/s.jsonl")


def _line(i: int) -> bytes:
    usage = {
        "input_tokens": i,
        "output_tokens": 2 * i,
        "cache_read_input_tokens": 3 * i,
        "cache_creation_input_tokens": 4 * i,
    }
    entry = {"type": "assistant", "message": {"id": f"m{i}", "usage": usage}}
    return json.dumps(entry, separators=(",", ":")).encode() + b"\n"


def _expected(count: int) -> TokenSums:
    n = sum(range(1, count + 1))
    return TokenSums(n, 2 * n, 3 * n, 4 * n)


def _transcript(fs: FakeFS) -> SessionTranscript:
    return SessionTranscript(TranscriptTail(fs, PATH))


def test_the_tail_accumulates_across_appended_lines() -> None:
    fs = FakeFS()
    transcript = _transcript(fs)
    assert transcript.tokens() is None  # nothing read yet: no data
    data = b""
    for i in range(1, 6):
        data += _line(i)
        fs.add_file(PATH, data)
        transcript.update()
        assert transcript.tokens() == _expected(i)
    assert tokens_of(facts.decode_line(x) or {} for x in data.split(b"\n") if x) == _expected(5)


def test_a_torn_line_is_counted_once_when_it_completes() -> None:
    fs = FakeFS()
    transcript = _transcript(fs)
    first, second = _line(1), _line(2)
    fs.add_file(PATH, first + second[:25])
    transcript.update()
    assert transcript.tokens() == _expected(1)
    fs.add_file(PATH, first + second)
    transcript.update()
    assert transcript.tokens() == _expected(2)
    transcript.update()  # nothing new: nothing added again
    assert transcript.tokens() == _expected(2)


def test_a_rewritten_transcript_is_recounted_from_the_start() -> None:
    fs = FakeFS()
    transcript = _transcript(fs)
    fs.add_file(PATH, _line(1) + _line(2) + _line(3))
    transcript.update()
    assert transcript.tokens() == _expected(3)
    fs.add_file(PATH, _line(5))  # shorter: the file was replaced
    transcript.update()
    assert transcript.tokens() == TokenSums(5, 10, 15, 20)


def test_a_transcript_without_usage_has_no_tokens_yet() -> None:
    fs = FakeFS()
    transcript = _transcript(fs)
    fs.add_file(PATH, b'{"type":"user","message":{"content":"hi"}}\n')
    transcript.update()
    assert transcript.tokens() is None


def test_a_small_read_limit_catches_up_over_several_polls() -> None:
    fs = FakeFS()
    data = b"".join(_line(i) for i in range(1, 9))
    fs.add_file(PATH, data)
    transcript = SessionTranscript(TranscriptTail(fs, PATH, max_read=len(_line(1)) * 2 + 3))
    polls = 0
    while True:
        transcript.update()
        polls += 1
        if not transcript.backlog:
            break
        assert polls < 20
    assert polls > 1
    assert transcript.tokens() == _expected(8)


# -- what jq would abort on, the port ignores --------------------------------------------------


@pytest.mark.parametrize(
    "entry",
    [
        {"type": "assistant", "message": {"usage": "lots"}},
        {"type": "assistant", "message": {"usage": [1, 2]}},
        {"type": "assistant", "message": "text"},
        {"type": "assistant", "message": {"usage": {"input_tokens": "5"}}},
        {"type": "assistant", "message": {"usage": {"input_tokens": True}}},
        {"type": "assistant", "message": {"usage": {"input_tokens": 1.5}}},
        {"type": "assistant", "message": {"usage": {"input_tokens": float("inf")}}},
        {"type": "assistant", "message": {"usage": {"input_tokens": [4]}}},
        {"type": "user", "message": {"usage": {"input_tokens": 4}}},
    ],
)
def test_malformed_usage_counts_as_nothing(entry: dict[str, Any]) -> None:
    assert tokens_of(
        [entry, {"type": "assistant", "message": {"usage": {"output_tokens": 2}}}]
    ) == (TokenSums(0, 2, 0, 0))


def test_the_total_and_the_record_count() -> None:
    accumulator = TokenAccumulator()
    accumulator.feed_all(
        [
            {"type": "assistant", "message": {"usage": {"input_tokens": 1, "output_tokens": 2}}},
            {"type": "assistant", "message": {"usage": {}}},
            {"type": "assistant", "message": {}},
        ]
    )
    assert accumulator.records == 2
    assert accumulator.sums().total == 3
    assert accumulator.sums().as_dict() == {
        "input": 1,
        "output": 2,
        "cache_read": 0,
        "cache_write": 0,
    }
