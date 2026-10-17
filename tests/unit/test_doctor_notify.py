"""``check_notify``: the ``[notify]`` commands and the daemon's last delivery failure (R18, D2).

Executables are files under ``tmp_path``; the daemon is a scripted fetch. No command is run.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from fylgja.cli import client, doctor
from fylgja.cli.doctor import Check, Status, check_notify, render_json, run_doctor
from fylgja.cli.install import Layout
from fylgja.config import Config, parse_config
from fylgja.testing.fakes import FakeProcessRunner


class _Fetch:
    """A fake ``client.get_json`` that records every request."""

    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.requests: list[str] = []

    def __call__(self, path: str, *, url: str | None = None, timeout: float | None = None) -> Any:
        self.requests.append(path)
        answer = self.answers[path]
        if isinstance(answer, BaseException):
            raise answer
        return answer


def _exe(tmp_path: Path, name: str = "notifier", mode: int = 0o755) -> str:
    path = tmp_path / name
    path.write_text("#!/bin/sh\n")
    path.chmod(mode)
    return str(path)


def _config(**notify: Any) -> Config:
    return parse_config({"notify": {"enabled": True, **notify}}, source="test")


def _pipeline(**block: Any) -> dict[str, Any]:
    return {"notify": {"enabled": True, "sent": 0, "failed": 0, **block}}


def test_alerts_off_checks_nothing_and_fetches_nothing() -> None:
    fetch = _Fetch({})
    assert check_notify(parse_config({}, source="test"), True, fetch) == []
    assert fetch.requests == []


def test_an_executable_desk_command_is_ok_and_names_its_path(tmp_path: Path) -> None:
    exe = _exe(tmp_path)
    (check,) = check_notify(_config(desk_command=[exe, "-title", "{title}"]), False)
    assert (check.name, check.status) == ("notify:desk", Status.OK)
    assert exe in check.detail
    assert "{title}" not in check.detail


def test_a_missing_or_non_executable_command_fails(tmp_path: Path) -> None:
    missing = str(tmp_path / "nope")
    (check,) = check_notify(_config(desk_command=[missing, "secret-arg"]), False)
    assert check.status is Status.FAIL
    assert check.detail == f"{missing} is missing or not executable"
    plain = _exe(tmp_path, "plain", 0o644)
    (check,) = check_notify(_config(away_command=[plain]), False)
    assert (check.name, check.status) == ("notify:away", Status.FAIL)


def test_a_short_ntfy_topic_warns_without_printing_it(tmp_path: Path) -> None:
    exe = _exe(tmp_path)
    checks = check_notify(_config(away_command=[exe, "-d", "x", "https://ntfy.sh/abc"]), False)
    (check,) = checks
    assert (check.name, check.status) == ("notify:away", Status.WARN)
    assert "shorter than 20" in check.detail
    assert "abc" not in render_json(checks)
    long_topic = "https://ntfy.sh/" + "a" * 24
    (check,) = check_notify(_config(away_command=[exe, long_topic]), False)
    assert check.status is Status.OK
    assert "a" * 24 not in check.detail


def test_both_commands_are_checked_in_order(tmp_path: Path) -> None:
    exe = _exe(tmp_path)
    checks = check_notify(_config(desk_command=[exe], away_command=[exe]), False)
    assert [c.name for c in checks] == ["notify:desk", "notify:away"]


def test_delivery_reports_the_last_failure_or_the_sent_count(tmp_path: Path) -> None:
    config = _config(desk_command=[_exe(tmp_path)])
    failing = _Fetch({"/inspect/pipeline": _pipeline(last_failure_kind="command_timeout")})
    checks = check_notify(config, True, failing)
    delivery = checks[-1]
    assert (delivery.name, delivery.status) == ("notify:delivery", Status.WARN)
    assert delivery.detail == "last delivery failed: command_timeout"
    ok = _Fetch({"/inspect/pipeline": _pipeline(sent=3, last_failure_kind=None)})
    assert check_notify(config, True, ok)[-1] == Check("notify:delivery", Status.OK, "3 sent")


def test_delivery_warns_on_failed_evaluations_before_the_last_send(tmp_path: Path) -> None:
    config = _config(desk_command=[_exe(tmp_path)])
    block = _pipeline(evaluation_failures=2, last_failure_kind="command_timeout")
    delivery = check_notify(config, True, _Fetch({"/inspect/pipeline": block}))[-1]
    assert delivery == Check(
        "notify:delivery", Status.WARN, "2 evaluations failed; see the daemon log"
    )


def test_delivery_warns_when_the_pipeline_cannot_be_read(tmp_path: Path) -> None:
    config = _config(desk_command=[_exe(tmp_path)])
    down = _Fetch({"/inspect/pipeline": client.DaemonDown("u", "timed out")})
    check = check_notify(config, True, down)[-1]
    assert check.status is Status.WARN
    assert "cannot read /inspect/pipeline" in check.detail
    odd = _Fetch({"/inspect/pipeline": {"schema": "x"}})
    assert check_notify(config, True, odd)[-1].status is Status.WARN


def test_a_daemon_that_is_down_is_not_asked(tmp_path: Path) -> None:
    fetch = _Fetch({})
    checks = check_notify(_config(desk_command=[_exe(tmp_path)]), False, fetch)
    assert [c.name for c in checks] == ["notify:desk"]
    assert fetch.requests == []


def test_run_doctor_puts_the_notify_checks_after_messaging(
    fake_home: Layout, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(desk_command=[_exe(tmp_path)])
    monkeypatch.setattr(doctor, "check_config", lambda: (Check("config", Status.OK, "ok"), config))
    fetch = _Fetch(
        {"/health": {"status": "ok"}, "/collectors": [], "/inspect/pipeline": _pipeline()}
    )
    checks = asyncio.run(
        run_doctor(
            runner=FakeProcessRunner(),
            layout=fake_home,
            fetch=fetch,
            probe=lambda p: True,
            source=lambda: [],
        )
    )
    names = [c.name for c in checks]
    start = names.index("messaging") + 1
    assert names[start : start + 3] == ["notify:desk", "notify:delivery", "applescript:chrome"]
