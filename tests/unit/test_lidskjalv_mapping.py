"""The Lidskjalv field mapping and the recorded ``/now`` documents (WP1.11, Fylgja side).

``docs/api/lidskjalv-mapping.md`` maps each field Lidskjalv reads to a ``/now`` path. These tests
hold the mapping to the contract: every mapped path exists in ``docs/api/now.schema.json`` and in
at least one document under ``tests/fixtures/now/``, and every document is a valid
``fylgja.now/1`` payload. The documents come from ``scripts/record_now_fixtures.py``; the tests
only read them.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from fylgja.api.now_models import NOW_SCHEMA, Now, dump_json

REPO = Path(__file__).resolve().parents[2]
MAPPING = REPO / "docs" / "api" / "lidskjalv-mapping.md"
SCHEMA = REPO / "docs" / "api" / "now.schema.json"
FIXTURES = REPO / "tests" / "fixtures" / "now"

COLUMNS = ["lidskjalv file", "field", "/now path", "notes"]
SESSION_FILES = ("sessions/<sid>.json", "lane.json", "summaries/<sid>.json", "roster.json")
NO_EQUIVALENT = "—"
SEGMENT = re.compile(r"^([a-z_]+)(\[\])?$")
SEPARATOR_CELL = re.compile(r"^:?-{3,}:?$")


# --------------------------------------------------------------------------------------------
# The table
# --------------------------------------------------------------------------------------------


class Row:
    def __init__(self, file: str, field: str, path: str, notes: str) -> None:
        self.file = file
        self.field = field
        self.path = path
        self.notes = notes

    def __repr__(self) -> str:
        return f"{self.file} / {self.field} -> {self.path}"


def _cells(line: str) -> list[str]:
    body = line.strip().removeprefix("|").removesuffix("|")
    cells = re.split(r"(?<!\\)\|", body)
    return [cell.replace("\\|", "|").replace("`", "").strip() for cell in cells]


def parse_mapping(text: str) -> list[Row]:
    """The rows of the mapping table (the one with the four documented columns)."""
    rows: list[Row] = []
    in_table = False
    for line in text.splitlines():
        if not line.lstrip().startswith("|"):
            in_table = False
            continue
        cells = _cells(line)
        if not in_table:
            in_table = [c.lower() for c in cells] == COLUMNS
            continue
        if all(SEPARATOR_CELL.match(c) for c in cells):
            continue
        assert len(cells) == len(COLUMNS), f"{len(cells)} cells in: {line}"
        rows.append(Row(*cells))
    return rows


def mapping_rows() -> list[Row]:
    return parse_mapping(MAPPING.read_text(encoding="utf-8"))


def mapped_paths() -> list[str]:
    return sorted({row.path for row in mapping_rows() if row.path != NO_EQUIVALENT})


# --------------------------------------------------------------------------------------------
# Paths in the schema and in documents
# --------------------------------------------------------------------------------------------


def _segments(path: str) -> list[tuple[str, bool]]:
    segments: list[tuple[str, bool]] = []
    for part in path.split("."):
        match = SEGMENT.match(part)
        assert match, f"bad path segment {part!r} in {path!r}"
        segments.append((match[1], match[2] is not None))
    return segments


def _resolve(schema: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in node:
        target: Any = schema
        for part in node["$ref"].removeprefix("#/").split("/"):
            target = target[part]
        node = target
    return node


def in_schema(schema: dict[str, Any], path: str) -> bool:
    """Does ``path`` name a property of the schema (through ``$ref`` and array ``items``)?"""
    node = _resolve(schema, schema)
    for name, is_array in _segments(path):
        properties = node.get("properties", {})
        if name not in properties:
            return False
        node = _resolve(schema, properties[name])
        if is_array:
            if node.get("type") != "array":
                return False
            node = _resolve(schema, node["items"])
    return True


def _values(node: Any, segments: list[tuple[str, bool]]) -> Iterator[Any]:
    if not segments:
        yield node
        return
    (name, is_array), rest = segments[0], segments[1:]
    if not isinstance(node, dict) or name not in node:
        return
    value = node[name]
    if not is_array:
        yield from _values(value, rest)
    elif isinstance(value, list):
        for item in value:
            yield from _values(item, rest)


def in_document(document: dict[str, Any], path: str) -> bool:
    """Is ``path`` present: for an array segment, in at least one element."""
    return any(True for _ in _values(document, _segments(path)))


def _documents() -> dict[str, dict[str, Any]]:
    documents = {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(FIXTURES.glob("*.json"))
    }
    assert documents, "no recorded /now documents; run scripts/record_now_fixtures.py"
    return documents


# --------------------------------------------------------------------------------------------
# The mapping
# --------------------------------------------------------------------------------------------


def test_the_mapping_table_has_rows_for_the_four_session_files() -> None:
    rows = mapping_rows()
    assert rows, "the mapping table was not found"
    files = {row.file for row in rows}
    assert set(SESSION_FILES) <= files, sorted(set(SESSION_FILES) - files)


def test_every_row_is_complete_and_unique() -> None:
    rows = mapping_rows()
    seen: set[tuple[str, str]] = set()
    for row in rows:
        assert row.file, row
        assert row.field, row
        assert row.path, row
        key = (row.file, row.field)
        assert key not in seen, f"duplicate row {row}"
        seen.add(key)


def test_a_field_without_a_path_says_why() -> None:
    gaps = [row for row in mapping_rows() if row.path == NO_EQUIVALENT]
    assert gaps, "the known gaps (cwd_now, iterm_session, ...) are listed"
    for row in gaps:
        assert len(row.notes) >= 20, f"{row} has no reason"


def test_every_lidskjalv_field_read_is_listed() -> None:
    listed = {(row.file, row.field) for row in mapping_rows()}
    read = {
        "sessions/<sid>.json": [
            "session_id",
            "state",
            "cwd",
            "cwd_now",
            "window_id",
            "iterm_session",
            "updated",
            "since",
            "pid",
            "detail",
        ],
        "lane.json": [
            "lane_id",
            "pid",
            "started_at",
            "ended_at",
            "cwd",
            "cwd_now",
            "window_id",
            "session_id",
        ],
        "summaries/<sid>.json": [
            "session_id",
            "title",
            "prompt",
            "said",
            "branch",
            "model",
            "turns",
            "tools",
            "files",
            "todos",
            "last_at",
        ],
        "roster.json": [
            "fetched_at",
            "sessions[].sessionId",
            "sessions[].kind",
            "sessions[].status",
            "sessions[].cwd",
            "sessions[].startedAt",
        ],
    }
    missing = [(f, name) for f, names in read.items() for name in names if (f, name) not in listed]
    assert missing == []


def test_lane_key_is_mapped_for_the_lane() -> None:
    lane = next(r for r in mapping_rows() if (r.file, r.field) == ("lane.json", "lane_id"))
    assert lane.path == "sessions[].lane_key"


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(SCHEMA.read_text(encoding="utf-8"))
    return loaded


@pytest.mark.parametrize("path", mapped_paths())
def test_every_mapped_path_is_in_the_schema(schema: dict[str, Any], path: str) -> None:
    assert in_schema(schema, path), f"{path} is not in docs/api/now.schema.json"


@pytest.mark.parametrize("path", mapped_paths())
def test_every_mapped_path_is_in_a_recorded_document(path: str) -> None:
    holders = [name for name, document in _documents().items() if in_document(document, path)]
    assert holders, f"{path} is in no document under tests/fixtures/now/"


# --------------------------------------------------------------------------------------------
# The recorded documents
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(_documents()))
def test_a_document_is_a_valid_now_payload(name: str) -> None:
    document = _documents()[name]
    assert document["schema"] == NOW_SCHEMA == "fylgja.now/1"
    model = Now.model_validate(document)
    # the document is what the API serialises: no null, nothing the models would add or drop
    assert json.loads(dump_json(model)) == document


def test_the_documents_cover_every_phase_1_state_and_host_kind() -> None:
    sessions = [s for d in _documents().values() for s in d.get("sessions", [])]
    assert {"idle", "busy", "waiting"} <= {s.get("state") for s in sessions}
    host_kinds = {s["host"].get("kind") for s in sessions}
    assert {"cmux", "jetbrains", "iterm2"} <= host_kinds
    assert any("unresolved" in s["host"] for s in sessions), "an unresolved host"
    assert any(s["host"].get("cmux_surface") and s["host"].get("ref") for s in sessions), (
        "a cmux session with its exact surface"
    )


def test_the_documents_cover_a_clear_predecessor_and_a_degraded_collector() -> None:
    documents = _documents()
    predecessors = [
        s for d in documents.values() for s in d.get("sessions", []) if "predecessor_key" in s
    ]
    assert predecessors
    for s in predecessors:
        assert s["lane_key"] == s["process_key"], "the lane survives /clear (A11)"
        assert s["predecessor_key"] != s["key"]
    degraded = [d for d in documents.values() if d["degraded"]]
    assert degraded
    for d in degraded:
        assert "sessions" not in d, "a degraded collector's section is absent"
        assert set(d["degraded"]) <= {c["name"] for c in d["collectors"]}


def test_the_documents_are_deterministic_and_clean() -> None:
    for name, document in _documents().items():
        assert document["generated_at"].startswith("2026-10-05T12:00:"), name
        text = (FIXTURES / name).read_text(encoding="utf-8")
        assert "/Users/" not in text
        assert text.endswith("}\n")


# --------------------------------------------------------------------------------------------
# The checkers themselves
# --------------------------------------------------------------------------------------------


def test_the_parser_reads_a_table_and_skips_others() -> None:
    text = (
        "| a | b |\n|---|---|\n| 1 | 2 |\n\n"
        "| Lidskjalv file | field | /now path | notes |\n|---|---|---|---|\n"
        "| `lane.json` | `pid` | `sessions[].pid` | same \\| pipe |\n"
        "| `lane.json` | `ended_at` | — | gone |\n"
    )
    rows = parse_mapping(text)
    assert [(r.file, r.field, r.path) for r in rows] == [
        ("lane.json", "pid", "sessions[].pid"),
        ("lane.json", "ended_at", NO_EQUIVALENT),
    ]
    assert rows[0].notes == "same | pipe"


def test_the_schema_checker_follows_refs_and_arrays_and_rejects_unknown_paths(
    schema: dict[str, Any],
) -> None:
    assert in_schema(schema, "sessions[].facts.tasks[].status")
    assert in_schema(schema, "sessions[].facts.prompts[]")
    assert in_schema(schema, "generated_at")
    assert not in_schema(schema, "sessions[].facts.nope")
    assert not in_schema(schema, "sessions.key")  # sessions is an array
    assert not in_schema(schema, "sessions[].pid[]")  # pid is not


def test_the_document_checker_needs_a_non_empty_array() -> None:
    document = {"sessions": [{"facts": {"files": [], "prompts": ["x"]}}, {"pid": 1}]}
    assert in_document(document, "sessions[].pid")
    assert in_document(document, "sessions[].facts.prompts[]")
    assert not in_document(document, "sessions[].facts.files[]")
    assert not in_document(document, "sessions[].facts.title")
    assert not in_document({}, "sessions[].pid")
