"""The pure helpers of "How do I do this?" (J20): sections, containment, refused names, the prompt
and the steps of a reply. Synthetic text only."""

from __future__ import annotations

import pytest

from fylgja.core.journal import (
    EXCERPT_MAX_BYTES,
    Source,
    cut_bytes,
    how_prompt,
    inside,
    parse_steps,
    refused_name,
    section_excerpt,
)

DOC = """# Plan

Intro text.

## Setup

Install the tool.

### Detail

Pick a version.

```sh
# not a heading
## still not a heading
```

## Run

Start it.
"""


def test_a_section_stops_at_the_next_heading_of_its_level_but_not_at_a_deeper_one() -> None:
    found = section_excerpt(DOC, "Setup", EXCERPT_MAX_BYTES)
    assert found.startswith("## Setup")
    assert "### Detail" in found
    assert "Pick a version." in found
    assert "Start it." not in found
    assert "Intro text." not in found


def test_the_heading_is_matched_without_marks_and_case() -> None:
    assert section_excerpt(DOC, "## setup", 1000) == section_excerpt(DOC, "Setup", 1000)
    assert section_excerpt(DOC, "Detail", 1000).startswith("### Detail")


def test_a_heading_inside_a_fenced_block_is_ignored() -> None:
    found = section_excerpt(DOC, "not a heading", 1000)
    assert found.startswith("# Plan")  # no such heading: the start of the file
    inner = section_excerpt(DOC, "Setup", 1000)
    assert "## still not a heading" in inner  # the fence did not end the section


def test_a_missing_or_absent_heading_gives_the_start_of_the_file() -> None:
    assert section_excerpt(DOC, "Nowhere", 10_000) == DOC
    assert section_excerpt(DOC, "Nowhere", 20) == DOC[:20]
    assert section_excerpt(DOC, None, 20) == DOC[:20]


def test_a_long_section_is_cut_to_the_limit_in_bytes() -> None:
    long = "## Setup\n" + "x" * 40_000 + "\n## Next\n"
    assert len(section_excerpt(long, "Setup", EXCERPT_MAX_BYTES).encode()) == 16384
    assert cut_bytes("é" * 10, 5) == "éé"


@pytest.mark.parametrize(
    ("path", "root", "expected"),
    [
        ("/repo/a/b.md", "/repo", True),
        ("/repo", "/repo", True),
        ("/repo2/x", "/repo", False),
        ("/other/repo/x", "/repo", False),
        ("/", "/repo", False),
    ],
)
def test_inside_compares_components(path: str, root: str, expected: bool) -> None:
    assert inside(path, root) is expected


@pytest.mark.parametrize(
    ("name", "refused"),
    [
        (".env", True),
        (".env.local", True),
        ("id_ed25519", True),
        ("server.pem", True),
        ("api.KEY", True),
        ("plan.md", False),
        ("environment.md", False),
        ("idea.md", False),
    ],
)
def test_refused_names(name: str, refused: bool) -> None:
    assert refused_name(name) is refused


def redact(text: str) -> str:
    return text.replace("SECRET", "[x]")


def test_steps_lose_their_markers_and_are_at_most_six() -> None:
    reply = "Here you go:\n" + "\n".join(f"{n}. Do **step** {n} with `code`" for n in range(1, 9))
    steps = parse_steps(reply, redact)
    assert steps is not None
    assert steps == [f"Do step {n} with code" for n in range(1, 7)]


def test_steps_accept_dashes_stars_checkboxes_and_links_and_are_redacted_and_cut() -> None:
    reply = f"- [ ] Open [the page](http://x) SECRET\n* {'y' * 300}\n\n   - indented\n**bold line**"
    steps = parse_steps(reply, redact)
    assert steps is not None
    assert steps[0] == "Open the page [x]"
    assert len(steps[1]) == 160
    assert steps[2] == "indented"
    assert len(steps) == 3


@pytest.mark.parametrize("reply", ["", "Nothing to list here.", "-\n*\n1.", "- \n"])
def test_a_reply_without_a_list_line_gives_none(reply: str) -> None:
    assert parse_steps(reply, redact) is None


def test_the_prompt_holds_the_item_the_source_and_the_excerpt() -> None:
    source = Source("file", "docs/plan.md", "Plan: Setup", path="docs/plan.md", section="Setup")
    prompt = how_prompt("  Install   the tool ", source, "## Setup\nInstall it.")
    assert "at most six short steps" in prompt
    assert "Install the tool" in prompt
    assert "Source (file): Plan: Setup" in prompt
    assert "Excerpt:\n## Setup\nInstall it." in prompt
    bare = how_prompt("Install the tool", source, None)
    assert "Excerpt:\n" not in bare
