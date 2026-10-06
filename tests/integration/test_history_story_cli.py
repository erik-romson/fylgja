"""``fylgja history --story [--date YYYY-MM-DD]`` (WP8.2, A8) against a running in-process daemon
with a fake model runner.

The daemon runs behind a real Uvicorn server on a free loopback port. The done condition of WP8.2:
the story of a day is printed, and a second run on the same day with no new activity returns the
cached text **without a model call**.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import date, timedelta

import pytest
import uvicorn
from api_support import T0, focus_event
from summary_support import SECRET, FakeRunner, story_world, wait_until
from test_summaries_cli import Served, free_port
from test_summaries_day import insert_todo

from fylgja import paths
from fylgja.api.app import create_app, server_config
from fylgja.cli import app as cli_app
from fylgja.cli import client
from fylgja.cli.app import STORY_DISABLED
from fylgja.core.summaries import RunOutput
from fylgja.testing.fakes import FakeClock

STORY = "The day began in alpha with the login form and ended with the release notes."
DAY = "2026-10-05"


@pytest.fixture(autouse=True)
def utc(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.delenv("TZ", raising=False)
    time.tzset()


@asynccontextmanager
async def serving(
    runner: FakeRunner, *, enabled: bool = True, later: timedelta = timedelta(hours=13)
) -> AsyncIterator[Served]:
    """A daemon that runs ``later`` after the picture was recorded: by default on the day after
    ``2026-10-05``, so that day is a past day."""
    port = free_port()
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text(f"[api]\nport = {port}\n", encoding="utf-8")
    paths.api_secret_file().write_text(SECRET + "\n", encoding="utf-8")
    async with story_world(
        runner, enabled=enabled, user_config={"api": {"port": port}}, clock=FakeClock(T0 + later)
    ) as daemon:
        daemon.sink("wm").emit(focus_event("wm", "100", "3", T0 - timedelta(hours=3)))
        await daemon.settle()
        server = uvicorn.Server(server_config(create_app(daemon, secret=SECRET), daemon.config))
        task = asyncio.create_task(server.serve())
        while not server.started:  # noqa: ASYNC110 - Uvicorn offers no readiness event
            await asyncio.sleep(0.01)
        try:
            yield Served(daemon, runner, port)
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 20)


@pytest.fixture
async def runner() -> FakeRunner:
    return FakeRunner(default=STORY)


@pytest.fixture
async def served(runner: FakeRunner) -> AsyncIterator[Served]:
    async with serving(runner) as found:
        yield found


async def released_after_the_call(served: Served, *args: str, code: int = 0) -> str:
    """Run the CLI while the model holds its answer; release it once the daemon called it, so the
    CLI can only print the story by waiting for ``summary.ready``."""
    served.runner.hold()
    calls = len(served.runner.calls)
    command = asyncio.create_task(served.fylgja(*args, code=code))
    await wait_until(lambda: len(served.runner.calls) > calls or command.done())
    await asyncio.sleep(0.05)
    assert not command.done()  # it is waiting on the event stream
    served.runner.release()
    return await command


async def test_history_story_prints_the_story_and_the_second_run_makes_no_model_call(
    served: Served,
) -> None:
    first = await released_after_the_call(served, "history", "--story", "--date", DAY)
    assert STORY in first.replace("\n", " ")
    assert "[fresh, as of " in first
    assert len(served.runner.calls) == 1
    assert "Per workspace:" in served.runner.calls[0].prompt  # the day's totals went in

    # the same day, nothing new: the cached text, at once, with no model call
    second = await served.fylgja("history", "--story", "--date", DAY)
    assert second == first
    assert len(served.runner.calls) == 1
    assert (await served.get(f"/summaries/day/{DAY}"))["state"] == "fresh"


async def test_a_late_import_changes_the_story_of_the_past_day(served: Served) -> None:
    await released_after_the_call(served, "history", "--story", "--date", DAY)
    await insert_todo(served.daemon, "the late one", created=T0 - timedelta(hours=1))
    served.runner.default = "A late todo joined the day."
    output = await released_after_the_call(served, "history", "--story", "--date", DAY)
    assert "A late todo joined the day." in output
    assert len(served.runner.calls) == 2
    assert "the late one" in served.runner.calls[1].prompt


async def test_history_story_prints_json(served: Served) -> None:
    output = await released_after_the_call(served, "history", "--story", "--date", DAY, "--json")
    answer = json.loads(output)
    assert (answer["scope"], answer["ref"], answer["state"]) == ("day", DAY, "fresh")
    assert answer["text"] == STORY
    assert "arc" not in answer


async def test_history_story_defaults_to_today(
    served: Served, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Day(date):
        @classmethod
        def today(cls) -> Day:
            return cls(2026, 10, 5)

    monkeypatch.setattr(cli_app, "date", Day)
    output = await released_after_the_call(served, "history", "--story")
    assert STORY in output.replace("\n", " ")
    assert (await served.get(f"/summaries/day/{DAY}"))["state"] == "fresh"


async def test_history_story_of_today_on_the_daemons_own_day() -> None:
    runner = FakeRunner(default=STORY)
    async with serving(runner, later=timedelta(0)) as served:
        first = await released_after_the_call(served, "history", "--story", "--date", DAY)
        assert STORY in first.replace("\n", " ")
        again = await served.fylgja("history", "--story", "--date", DAY)
        assert again == first
    assert len(runner.calls) == 1


async def test_history_story_exits_1_with_a_clear_message_when_summaries_are_disabled() -> None:
    runner = FakeRunner()
    async with serving(runner, enabled=False) as served:
        output = await served.fylgja("history", "--story", "--date", DAY, code=1)
        assert STORY_DISABLED in output
    assert runner.calls == []


async def test_history_story_of_a_day_with_nothing_recorded_exits_1(served: Served) -> None:
    output = await served.fylgja("history", "--story", "--date", "2026-09-01", code=1)
    assert "nothing was recorded on that day" in output
    assert served.runner.calls == []


async def test_history_story_says_why_it_failed(served: Served) -> None:
    served.runner.script.append(RunOutput(1, ""))
    output = await released_after_the_call(served, "history", "--story", "--date", DAY, code=1)
    assert "[failed" in output
    assert "nonzero_exit" in output


async def test_history_story_exits_1_when_no_story_arrives_in_time(served: Served) -> None:
    served.runner.hold()
    output = await served.fylgja("history", "--story", "--date", DAY, "--wait", "0.3", code=1)
    assert "no summary after 0.3 s" in output
    served.runner.release()
    await served.daemon.summaries.drain()
    assert (await served.get(f"/summaries/day/{DAY}"))["state"] == "fresh"


@pytest.mark.parametrize("given", ["2026-13-01", "20261005", "yesterday", "2026-10-5"])
async def test_a_date_that_is_not_a_day_exits_2(served: Served, given: str) -> None:
    output = await served.fylgja("history", "--story", "--date", given, code=2)
    assert "YYYY-MM-DD" in output
    assert served.runner.calls == []


async def test_date_without_story_exits_2(served: Served) -> None:
    output = await served.fylgja("history", "--date", DAY, code=2)
    assert "--date goes with --story" in output


async def test_history_story_exits_3_when_the_daemon_is_down() -> None:
    port = free_port()
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text(f"[api]\nport = {port}\n", encoding="utf-8")
    paths.api_secret_file().write_text(SECRET + "\n", encoding="utf-8")
    from typer.testing import CliRunner

    result = await asyncio.to_thread(
        CliRunner().invoke, cli_app.app, ["history", "--story", "--date", DAY]
    )
    assert result.exit_code == client.EXIT_DAEMON_DOWN
    assert "daemon not running" in result.output


def test_history_story_is_in_the_help() -> None:
    from typer.testing import CliRunner

    output = CliRunner().invoke(cli_app.app, ["history", "--help"]).output
    assert "--story" in output
    assert "--date" in output
