"""'How do I do this?' end to end (J20; ``docs/api/journal.md``).

The world of ``test_journal_actions``: a real daemon over ASGI with the journal on, the model a
:class:`FakeRunner`, ``git`` a :class:`FakeProcessRunner` and the filesystem a :class:`FakeFS`. The
first item ``n1`` is given the source each test needs. Nothing calls a real model, a real ``git``
or reads a real file.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any

import httpx
import pytest
from api_support import BASE_URL
from summary_support import SECRET, wait_until
from test_journal_actions import AUTH, N1, acting, item
from test_journal_daemon import COMMIT, GIT, ITEM, MAIN_ID, REPO, World, world

from fylgja.api.app import create_app
from fylgja.core.journal_service import item_values
from fylgja.core.summaries import RunOutput

PLAN = f"{REPO}/docs/plan.md"
DOC = (
    "# Plan\n\nIntro line.\n\n## Setup\n\nInstall the tool with TOKEN-ABC123 first.\n\n"
    "### Detail\n\nPick a version.\n\n## Run\n\nStart it.\n"
)
STEPS = "Here are the steps:\n1. Install the tool\n2. Pick a version\n- Start it\n"
FILE_SOURCE = {"kind": "file", "ref": "docs/plan.md", "label": "Plan: Setup"}
FILE_SOURCE = {**FILE_SOURCE, "path": "docs/plan.md", "section": "Setup"}
HOW = f"/journal/items/{N1}/how"
PRIVACY = {"privacy": {"redact_patterns": ["TOKEN-[A-Z0-9]+"]}}


@asynccontextmanager
async def how_world() -> AsyncIterator[tuple[World, httpx.AsyncClient]]:
    config = {"journal": {"enabled": True}, **PRIVACY}
    async with acting(user_config=config) as (w, http):
        w.runner.default = STEPS
        yield w, http


async def give(w: World, source: dict[str, Any]) -> None:
    """Replace the source of ``n1`` in the store."""
    row = replace(item(w), source=source)
    await w.daemon.writer.execute(
        lambda tx: tx.connection.execute(
            "UPDATE journal_items SET source = ? WHERE id = ?",
            (item_values(row)["source"], row.id),
        )
    )


def fs_of(w: World) -> Any:
    return w.daemon.fs


async def post(http: httpx.AsyncClient) -> httpx.Response:
    return await http.post(HOW, headers=AUTH, json={})


async def finished(w: World, events: int = 1) -> None:
    await wait_until(lambda: len(w.changed) == events and not w.journal._how_jobs)  # noqa: SLF001


def how_prompts(w: World) -> list[str]:
    return [call.prompt for call in w.runner.calls[1:]]


# --------------------------------------------------------------------------------------------
# A file source
# --------------------------------------------------------------------------------------------


async def test_a_file_section_becomes_a_checklist_cached_by_its_digest() -> None:
    async with how_world() as (w, http):
        fs_of(w).add_file(PLAN, DOC)
        await give(w, FILE_SOURCE)
        missing = await http.get(HOW)
        assert missing.status_code == 200
        assert missing.json() == {"item": N1, "state": "missing"}

        first = await post(http)
        assert first.status_code == 202, first.text
        assert first.json() == {"item": N1, "state": "pending"}
        await finished(w)
        assert w.changed == [MAIN_ID]

        [prompt] = how_prompts(w)
        assert "## Setup" in prompt
        assert "Pick a version." in prompt  # the deeper heading stays in the section
        assert "Start it." not in prompt  # the next section does not
        assert "Intro line." not in prompt
        assert "TOKEN-ABC123" not in prompt
        assert "[redacted]" in prompt
        assert ITEM in prompt

        done = await http.get(HOW)
        body = done.json()
        assert (done.status_code, body["state"]) == (200, "fresh")
        assert body["steps"] == ["Install the tool", "Pick a version", "Start it"]
        assert "generated_at" in body
        assert "error_kind" not in body
        assert item(w).how_text == "Install the tool\nPick a version\nStart it"
        assert [
            r["has_how"]
            for r in (await http.get(f"/journal/threads/{MAIN_ID}")).json()["open"]["next"]
        ] == [True]

        again = await post(http)
        assert again.status_code == 200
        assert again.json() == body
        await w.quiet()
        assert len(how_prompts(w)) == 1  # no second call
        assert w.changed == [MAIN_ID]  # and no second event


async def test_a_changed_section_is_stale_and_a_post_makes_a_new_call() -> None:
    async with how_world() as (w, http):
        fs_of(w).add_file(PLAN, DOC)
        await give(w, FILE_SOURCE)
        await post(http)
        await finished(w)
        fs_of(w).add_file(PLAN, DOC.replace("Install the tool", "Build the tool"))
        stale = (await http.get(HOW)).json()
        assert stale["state"] == "stale"
        assert stale["steps"] == ["Install the tool", "Pick a version", "Start it"]
        w.runner.default = "- Build it"
        response = await post(http)
        assert response.status_code == 202
        assert response.json()["state"] == "pending"
        await finished(w, 2)
        assert len(how_prompts(w)) == 2
        assert "Build the tool" in how_prompts(w)[1]
        assert (await http.get(HOW)).json()["steps"] == ["Build it"]
        # a change outside the section does not make the answer stale
        built = DOC.replace("Install the tool", "Build the tool")
        fs_of(w).add_file(PLAN, built.replace("Start it.", "Stop it."))
        assert (await http.get(HOW)).json()["state"] == "fresh"


async def test_a_missing_heading_uses_the_start_of_the_file() -> None:
    async with how_world() as (w, http):
        fs_of(w).add_file(PLAN, DOC)
        await give(w, {**FILE_SOURCE, "section": "Nowhere"})
        await post(http)
        await finished(w)
        assert "Intro line." in how_prompts(w)[0]


# --------------------------------------------------------------------------------------------
# Files that are not read
# --------------------------------------------------------------------------------------------


async def prompt_without_excerpt(w: World, http: httpx.AsyncClient, secret: str) -> None:
    response = await post(http)
    assert response.status_code == 202
    await finished(w)
    [prompt] = how_prompts(w)
    assert secret not in prompt
    assert ITEM in prompt
    assert "\nExcerpt:\n" not in prompt
    assert (await http.get(HOW)).json()["state"] == "fresh"  # the checklist came from the text


async def test_a_link_out_of_the_checkout_is_not_read() -> None:
    async with how_world() as (w, http):
        fs = fs_of(w)
        fs.links[f"{REPO}/link.md"] = "/Users/x/.ssh/id"
        fs.add_file("/Users/x/.ssh/id", "PRIVATE KEY MATERIAL")
        await give(w, {"kind": "file", "ref": "link.md", "label": "l", "path": "link.md"})
        await prompt_without_excerpt(w, http, "PRIVATE")
        assert PurePosixPath("/Users/x/.ssh/id") not in fs.reads


async def test_dot_dot_and_absolute_paths_cannot_leave_the_checkout() -> None:
    async with how_world() as (w, http):
        fs = fs_of(w)
        fs.add_file("/w/other/notes.md", "OTHER CHECKOUT")
        fs.add_file("/w/repo2/notes.md", "SIBLING")
        for path in ("../other/notes.md", "/w/other/notes.md", "../repo2/notes.md"):
            await give(w, {"kind": "file", "ref": path, "label": "l", "path": path})
            w.runner.calls.clear()
            w.changed.clear()
            await post(http)
            await wait_until(lambda: len(w.runner.calls) == 1)
            await finished(w)
            assert "OTHER CHECKOUT" not in w.runner.calls[0].prompt
            assert "SIBLING" not in w.runner.calls[0].prompt
        assert fs.reads == []


async def test_a_file_under_a_private_directory_is_refused_even_inside_the_checkout() -> None:
    async with how_world() as (w, http):
        fs = fs_of(w)
        home = str(Path.home())
        fs.links[REPO] = f"{home}/.config/repo"
        real = f"{home}/.config/repo/notes.md"
        fs.add_file(real, "CONFIG SECRET")
        await give(w, {"kind": "file", "ref": "notes.md", "label": "n", "path": "notes.md"})
        await prompt_without_excerpt(w, http, "CONFIG SECRET")
        assert PurePosixPath(real) not in fs.reads


async def test_a_file_of_two_mebibytes_is_refused() -> None:
    async with how_world() as (w, http):
        fs = fs_of(w)
        fs.add_file(f"{REPO}/big.md", "BIG BODY" + "x" * (2 << 20))
        await give(w, {"kind": "file", "ref": "big.md", "label": "b", "path": "big.md"})
        await prompt_without_excerpt(w, http, "BIG BODY")
        assert PurePosixPath(f"{REPO}/big.md") not in fs.reads


async def test_a_file_with_a_nul_byte_or_bad_utf8_is_refused() -> None:
    async with how_world() as (w, http):
        fs = fs_of(w)
        fs.add_file(f"{REPO}/bin.md", b"BINARY TEXT\x00more")
        await give(w, {"kind": "file", "ref": "bin.md", "label": "b", "path": "bin.md"})
        await prompt_without_excerpt(w, http, "BINARY TEXT")
        w.runner.calls.clear()
        w.changed.clear()
        fs.add_file(f"{REPO}/latin.md", b"LATIN \xe9 TEXT")
        await give(w, {"kind": "file", "ref": "latin.md", "label": "b", "path": "latin.md"})
        await prompt_without_excerpt_again(w, http, "LATIN")


async def prompt_without_excerpt_again(w: World, http: httpx.AsyncClient, secret: str) -> None:
    await post(http)
    await wait_until(lambda: len(w.runner.calls) == 1)
    await finished(w)
    assert secret not in w.runner.calls[0].prompt


@pytest.mark.parametrize("name", [".env.local", "id_ed25519", "server.pem", "api.key"])
async def test_a_key_file_is_refused_by_its_name(name: str) -> None:
    async with how_world() as (w, http):
        fs = fs_of(w)
        fs.add_file(f"{REPO}/{name}", "KEY MATERIAL")
        await give(w, {"kind": "file", "ref": name, "label": "k", "path": name})
        await prompt_without_excerpt(w, http, "KEY MATERIAL")
        assert PurePosixPath(f"{REPO}/{name}") not in fs.reads


async def test_without_a_path_resolver_no_file_is_read() -> None:
    async with how_world() as (w, http):
        w.journal._fs = object()  # type: ignore[assignment]  # noqa: SLF001
        await give(w, FILE_SOURCE)
        await prompt_without_excerpt(w, http, "Install the tool with")


# --------------------------------------------------------------------------------------------
# Other sources
# --------------------------------------------------------------------------------------------


async def test_a_commit_source_puts_its_stat_into_the_prompt() -> None:
    async with how_world() as (w, http):
        w.processes.script(
            GIT, "--no-optional-locks", "-C", REPO, "show",
            stdout="Validate the email field\n\n src/validate.py | 12 ++++\n",
        )  # fmt: skip
        await give(w, {"kind": "commit", "ref": COMMIT, "label": "Validate the email field"})
        await post(http)
        await finished(w)
        [prompt] = how_prompts(w)
        assert "src/validate.py | 12 ++++" in prompt
        shown = [c for c in w.processes.calls if "show" in c]
        assert shown == [
            (GIT, "--no-optional-locks", "-C", REPO, "show", "--stat", "--format=%s%n%n%b")
            + (COMMIT, "--")
        ]


async def test_a_failed_git_show_gives_a_call_from_the_item_text_only() -> None:
    async with how_world() as (w, http):
        w.processes.script(GIT, "--no-optional-locks", "-C", REPO, "show", returncode=128)
        await give(w, {"kind": "commit", "ref": COMMIT, "label": "Validate the email field"})
        await post(http)
        await finished(w)
        [prompt] = how_prompts(w)
        assert ITEM in prompt
        assert "\nExcerpt:\n" not in prompt


async def test_a_commit_ref_that_is_not_a_hash_never_reaches_git() -> None:
    async with how_world() as (w, http):
        await give(w, {"kind": "commit", "ref": "--output=/x", "label": "odd"})
        await post(http)
        await finished(w)
        assert not [c for c in w.processes.calls if "show" in c]


async def test_a_session_source_shows_the_live_facts() -> None:
    async with how_world() as (w, http):
        await give(w, {"kind": "session", "ref": "claude:a", "label": "Fix the login form"})
        await post(http)
        await finished(w)
        [prompt] = how_prompts(w)
        assert "Files: form.py, validate.py" in prompt
        assert "Open task: validate the email" in prompt
        assert "Done task: read the form" in prompt
        assert "Asked: make the email field validate" in prompt


async def test_a_session_source_without_facts_uses_its_newest_stored_activity() -> None:
    async with how_world() as (w, http):
        await give(w, {"kind": "session", "ref": "claude:b", "label": "Tidy up"})
        await post(http)
        await finished(w)
        [prompt] = how_prompts(w)
        assert "Asked: tidy the docs" in prompt
        assert "Files:" not in prompt


async def test_an_unknown_session_and_a_person_source_give_no_excerpt() -> None:
    async with how_world() as (w, http):
        await give(w, {"kind": "session", "ref": "claude:gone", "label": "Gone"})
        await post(http)
        await finished(w)
        assert "\nExcerpt:\n" not in how_prompts(w)[0]


async def test_a_todo_source_gives_the_todos_text() -> None:
    async with how_world() as (w, http):
        todo = await w.daemon.todos.add("Call the printer about TOKEN-XYZ9")
        await give(w, {"kind": "todo", "ref": todo.id, "label": "Call the printer"})
        await post(http)
        await finished(w)
        [prompt] = how_prompts(w)
        assert "Call the printer about [redacted]" in prompt


# --------------------------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------------------------


async def test_two_concurrent_posts_make_one_call() -> None:
    async with how_world() as (w, http):
        w.hold()
        first, second = await asyncio.gather(post(http), post(http))
        assert sorted(r.status_code for r in (first, second)) == [202, 202]
        assert first.json() == second.json() == {"item": N1, "state": "pending"}
        await wait_until(lambda: len(w.runner.calls) == 2)
        third = await post(http)
        assert (third.status_code, third.json()["state"]) == (202, "pending")
        assert (await http.get(HOW)).json()["state"] == "pending"
        w.release()
        await finished(w)
        assert len(how_prompts(w)) == 1
        assert (await http.get(HOW)).json()["state"] == "fresh"


async def test_a_failed_job_keeps_the_earlier_steps_and_publishes_one_event() -> None:
    async with how_world() as (w, http):
        await give(w, FILE_SOURCE)
        fs_of(w).add_file(PLAN, DOC)
        await post(http)
        await finished(w)
        fs_of(w).add_file(PLAN, DOC.replace("Install the tool", "Build the tool"))
        w.runner.script.append(RunOutput(1, ""))
        await post(http)
        await finished(w, 2)
        failed = (await http.get(HOW)).json()
        assert failed["state"] == "failed"
        assert failed["error_kind"] == "nonzero_exit"
        assert failed["steps"] == ["Install the tool", "Pick a version", "Start it"]
        assert w.changed == [MAIN_ID, MAIN_ID]
        # the next POST tries again
        again = await post(http)
        assert again.status_code == 202
        await finished(w, 3)
        assert (await http.get(HOW)).json()["state"] == "fresh"


async def test_a_reply_without_a_step_is_bad_output() -> None:
    async with how_world() as (w, http):
        w.runner.default = "I cannot help with that."
        await post(http)
        await finished(w)
        body = (await http.get(HOW)).json()
        assert (body["state"], body["error_kind"]) == ("failed", "bad_output")
        assert "steps" not in body


async def test_no_log_line_carries_the_excerpt_the_steps_or_the_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with how_world() as (w, http):
        fs_of(w).add_file(PLAN, DOC)
        await give(w, FILE_SOURCE)
        with caplog.at_level(logging.DEBUG, logger="fylgja.journal"):
            await post(http)
            await finished(w)
        text = "\n".join(r.getMessage() for r in caplog.records)
        for secret in ("Pick a version", "Install the tool", "docs/plan.md", ITEM):
            assert secret not in text


async def test_unknown_items_are_404() -> None:
    async with how_world() as (_, http):
        assert (
            await http.post("/journal/items/nope/how", headers=AUTH, json={})
        ).status_code == 404
        assert (await http.get("/journal/items/nope/how")).status_code == 404


async def test_both_routes_are_409_while_the_journal_is_off() -> None:
    async with world(user_config={}) as w:
        transport = httpx.ASGITransport(app=create_app(w.daemon, secret=SECRET))
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as http:
            posted = await http.post("/journal/items/x/how", headers=AUTH, json={})
            read = await http.get("/journal/items/x/how")
            assert (posted.status_code, read.status_code) == (409, 409)
