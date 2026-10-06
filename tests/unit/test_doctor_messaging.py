"""Doctor's messaging lines: the effective inbound policy in plain words (WP3.6, R10).

The precedence rules are Anthropic's (settings reference, ``crossSessionInbound``): managed, then
``--settings``, then user settings, first value wins; project and local only when stricter, and
their ``hold`` or ``refuse`` when no trusted source sets a value; an unrecognized value holds (or,
in managed settings, refuses) from 2.1.248. With no value, the receiver's permission-mode class
decides. Doctor reads; it never sends a probe and never changes ``crossSessionInbound``.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from claude_support import T0, ps_text
from message_support import FakeInbox, World, short_tmp, socket_dir

from fylgja.cli.doctor import Status, check_messaging, run_doctor
from fylgja.cli.install import Layout
from fylgja.collectors.seams import ProcessResult
from fylgja.config import parse_config
from fylgja.messaging.claude import policy
from fylgja.messaging.claude.policy import (
    UNREADABLE,
    Effective,
    Inbound,
    InboundSources,
    PermissionClass,
    PermissionMode,
    ProcessFlags,
    Source,
)

V = (2, 1, 289)
BYPASS = PermissionMode("bypassPermissions", "--dangerously-skip-permissions", True)
PROMPTING = PermissionMode("default", "no mode flag or defaultMode")
UNKNOWN_MODE = PermissionMode(None, "its command line could not be read")


def _inbound(value: object) -> dict[str, Any]:
    return {"crossSessionInbound": value}


def _line(
    sources: InboundSources, mode: PermissionMode = BYPASS, version: tuple[int, int, int] = V
) -> str:
    effective = policy.effective_inbound(sources, version)
    return policy.describe_inbound(effective, mode, expiry=policy.dialog_expiry(sources))


# --- each source combination gives the right line -------------------------------------------------

CASES: list[tuple[str, InboundSources, PermissionMode, Inbound | None, Source | None, str]] = [
    (
        "nothing set, bypassing receiver (Erik's sessions)",
        InboundSources(),
        BYPASS,
        None,
        None,
        "messages to this session will be held for approval: no crossSessionInbound value "
        "applies and it bypasses permission prompts (bypassPermissions, from "
        "--dangerously-skip-permissions); an unanswered approval dialog drops the message "
        "after 5 minutes",
    ),
    (
        "nothing set, prompting receiver",
        InboundSources(),
        PROMPTING,
        None,
        None,
        "messages to this session will be delivered: no crossSessionInbound value applies and it "
        "prompts for permissions (default, from no mode flag or defaultMode); a Fylgja message "
        "claims no permission class",
    ),
    (
        "nothing set, mode unknown",
        InboundSources(),
        UNKNOWN_MODE,
        None,
        None,
        "messages to this session will be delivered if it prompts for permissions, or held for "
        "approval if it bypasses them: no crossSessionInbound value applies and its permission "
        "mode is unknown (its command line could not be read)",
    ),
    (
        "user accept",
        InboundSources(user=_inbound("accept")),
        BYPASS,
        Inbound.ACCEPT,
        Source.USER,
        "messages to this session will be delivered to Claude (crossSessionInbound = accept in "
        "user settings)",
    ),
    (
        "user hold",
        InboundSources(user=_inbound("hold")),
        BYPASS,
        Inbound.HOLD,
        Source.USER,
        "messages to this session will be held with a notice and not delivered until an accept "
        "applies (crossSessionInbound = hold in user settings)",
    ),
    (
        "user refuse",
        InboundSources(user=_inbound("refuse")),
        PROMPTING,
        Inbound.REFUSE,
        Source.USER,
        "messages to this session will be dropped without delivery, and Fylgja is not told "
        "(crossSessionInbound = refuse in user settings)",
    ),
    (
        "managed accept beats a user refuse (first trusted value wins)",
        InboundSources(managed=_inbound("accept"), user=_inbound("refuse")),
        BYPASS,
        Inbound.ACCEPT,
        Source.MANAGED,
        "delivered to Claude (crossSessionInbound = accept in managed settings)",
    ),
    (
        "--settings hold beats a user accept",
        InboundSources(flag=_inbound("hold"), user=_inbound("accept")),
        BYPASS,
        Inbound.HOLD,
        Source.FLAG,
        "(crossSessionInbound = hold in --settings)",
    ),
    (
        "--settings accept beats a user refuse",
        InboundSources(flag=_inbound("accept"), user=_inbound("refuse")),
        PROMPTING,
        Inbound.ACCEPT,
        Source.FLAG,
        "(crossSessionInbound = accept in --settings)",
    ),
    (
        "a project refuse applies over a trusted accept",
        InboundSources(managed=_inbound("accept"), project=_inbound("refuse")),
        PROMPTING,
        Inbound.REFUSE,
        Source.PROJECT,
        "dropped without delivery, and Fylgja is not told (crossSessionInbound = refuse in "
        "project settings)",
    ),
    (
        "a local hold applies over a user accept",
        InboundSources(user=_inbound("accept"), local=_inbound("hold")),
        PROMPTING,
        Inbound.HOLD,
        Source.LOCAL,
        "(crossSessionInbound = hold in local settings)",
    ),
    (
        "a project accept never loosens a trusted hold",
        InboundSources(user=_inbound("hold"), project=_inbound("accept")),
        PROMPTING,
        Inbound.HOLD,
        Source.USER,
        "(crossSessionInbound = hold in user settings)",
    ),
    (
        "a project accept alone does not apply: the default decides",
        InboundSources(project=_inbound("accept")),
        BYPASS,
        None,
        None,
        "messages to this session will be held for approval: no crossSessionInbound value applies",
    ),
    (
        "a project hold applies when no trusted source sets a value",
        InboundSources(project=_inbound("hold")),
        PROMPTING,
        Inbound.HOLD,
        Source.PROJECT,
        "(crossSessionInbound = hold in project settings)",
    ),
    (
        "an unrecognized user value holds",
        InboundSources(user=_inbound("allow")),
        PROMPTING,
        Inbound.HOLD,
        Source.USER,
        "held with a notice and not delivered until an accept applies (an unrecognized "
        "crossSessionInbound value in user settings)",
    ),
    (
        "an unrecognized --settings value holds even under a managed accept",
        InboundSources(managed=_inbound("accept"), flag=_inbound(True)),
        PROMPTING,
        Inbound.HOLD,
        Source.FLAG,
        "(an unrecognized crossSessionInbound value in --settings)",
    ),
    (
        "a refuse set elsewhere still applies over an unrecognized value",
        InboundSources(flag=_inbound("nope"), user=_inbound("refuse")),
        PROMPTING,
        Inbound.REFUSE,
        Source.USER,
        "(crossSessionInbound = refuse in user settings)",
    ),
    (
        "an unrecognized managed value refuses",
        InboundSources(managed=_inbound("ACCEPT"), user=_inbound("accept")),
        PROMPTING,
        Inbound.REFUSE,
        Source.MANAGED,
        "dropped without delivery, and Fylgja is not told (an unrecognized crossSessionInbound "
        "value in managed settings)",
    ),
    (
        "an unrecognized project value holds",
        InboundSources(user=_inbound("accept"), project=_inbound(["hold"])),
        PROMPTING,
        Inbound.HOLD,
        Source.PROJECT,
        "(an unrecognized crossSessionInbound value in project settings)",
    ),
]


@pytest.mark.parametrize(
    ("sources", "mode", "value", "source", "line"),
    [case[1:] for case in CASES],
    ids=[case[0] for case in CASES],
)
def test_each_source_combination_yields_the_right_line(
    sources: InboundSources,
    mode: PermissionMode,
    value: Inbound | None,
    source: Source | None,
    line: str,
) -> None:
    effective = policy.effective_inbound(sources, V)
    assert (effective.value, effective.source) == (value, source)
    text = _line(sources, mode)
    assert line in text
    assert text.startswith("messages to this session will be ")


def test_unrecognized_values_are_ignored_before_2_1_248() -> None:
    sources = InboundSources(managed=_inbound("bogus"), user=_inbound("maybe"))
    assert policy.effective_inbound(sources, (2, 1, 247)) == Effective(None)
    assert policy.effective_inbound(sources, (2, 1, 248)).value is Inbound.REFUSE
    old_project = InboundSources(project=_inbound("x"))
    assert policy.effective_inbound(old_project, (2, 1, 240)).value is None
    assert policy.effective_inbound(old_project, None).value is Inbound.HOLD


def test_the_spec_example_sentence_for_a_bypassing_session() -> None:
    assert "messages to this session will be held for approval" in _line(InboundSources())


def test_dialog_expiry_comes_from_managed_then_user() -> None:
    assert policy.dialog_expiry(InboundSources()) == "5m"
    assert policy.dialog_expiry(InboundSources(user={"dialogExpiry": "10m"})) == "10m"
    both = InboundSources(managed={"dialogExpiry": "60s"}, user={"dialogExpiry": "10m"})
    assert policy.dialog_expiry(both) == "60s"
    assert policy.dialog_expiry(InboundSources(user={"dialogExpiry": "3h"})) == "5m"
    assert policy.dialog_expiry(InboundSources(flag={"dialogExpiry": "10m"})) == "5m"
    assert "after 10 minutes" in _line(InboundSources(user={"dialogExpiry": "10m"}))
    assert "after 60 s" in _line(InboundSources(managed={"dialogExpiry": "60s"}))
    never = _line(InboundSources(user={"dialogExpiry": "never"}))
    assert "the approval dialog does not expire" in never


def test_a_non_interactive_bypassing_session_cannot_show_the_dialog() -> None:
    headless = PermissionMode("bypassPermissions", "--dangerously-skip-permissions", True, False)
    effective = Effective(None)
    text = policy.describe_inbound(effective, headless, expiry="5m")
    assert "it cannot show the dialog, so it keeps each for 5 minutes and then drops it" in text
    forever = policy.describe_inbound(effective, headless, expiry="never")
    assert "it keeps them until the session ends" in forever


def test_an_unreadable_source_is_said_not_guessed() -> None:
    sources = InboundSources(flag=UNREADABLE, user=_inbound("accept"))
    assert sources.unreadable == (Source.FLAG,)
    text = _line(sources, PROMPTING)
    assert text.endswith("; doctor could not read --settings, which may change this")
    assert repr(UNREADABLE) == "UNREADABLE"


# --- permission mode and class --------------------------------------------------------------------


def test_permission_classes() -> None:
    assert policy.permission_class(BYPASS) is PermissionClass.BYPASS
    for mode in ("default", "acceptEdits", "auto", "dontAsk"):
        assert policy.permission_class(PermissionMode(mode, "x")) is PermissionClass.PROMPTING
    plan_with_bypass = PermissionMode("plan", "x", bypass_available=True)
    assert policy.permission_class(plan_with_bypass) is PermissionClass.BYPASS
    headless_plan = PermissionMode("plan", "x", bypass_available=True, interactive=False)
    assert policy.permission_class(headless_plan) is PermissionClass.PROMPTING
    assert policy.permission_class(PermissionMode("plan", "x")) is PermissionClass.PROMPTING
    assert policy.permission_class(PermissionMode("someNewMode", "x")) is None
    assert policy.permission_class(UNKNOWN_MODE) is None


def test_the_start_mode_comes_from_flags_then_settings() -> None:
    none = InboundSources()
    assert policy.permission_mode(None, none).mode is None
    skip = policy.permission_mode(ProcessFlags(skip_permissions=True), none)
    assert (skip.mode, skip.source, skip.bypass_available) == (
        "bypassPermissions",
        "--dangerously-skip-permissions",
        True,
    )
    flagged = policy.permission_mode(
        ProcessFlags(permission_mode="plan", allow_skip_permissions=True), none
    )
    assert (flagged.mode, flagged.bypass_available) == ("plan", True)
    headless = policy.permission_mode(ProcessFlags(skip_permissions=True, print_mode=True), none)
    assert headless.interactive is False
    plain = policy.permission_mode(ProcessFlags(), none)
    assert (plain.mode, plain.source) == ("default", "no mode flag or defaultMode")

    def mode(**files: Any) -> str | None:
        sources = InboundSources(
            **{k: {"permissions": {"defaultMode": v}} for k, v in files.items()}
        )
        return policy.permission_mode(ProcessFlags(), sources, version=V).mode

    assert mode(user="acceptEdits") == "acceptEdits"
    assert mode(user="acceptEdits", project="plan") == "plan"
    assert mode(user="acceptEdits", local="dontAsk", project="plan") == "dontAsk"
    assert mode(flag="auto", user="acceptEdits") == "auto"
    assert mode(managed="default", flag="auto") == "default"
    # bypassPermissions and auto do not apply from project or local files since 2.1.257
    assert mode(project="bypassPermissions", user="acceptEdits") == "acceptEdits"
    old = InboundSources(project={"permissions": {"defaultMode": "bypassPermissions"}})
    assert policy.permission_mode(ProcessFlags(), old, version=(2, 1, 250)).mode == (
        "bypassPermissions"
    )
    source = policy.permission_mode(ProcessFlags(), InboundSources(user=_perm("auto")), version=V)
    assert source.source == "permissions.defaultMode in user settings"


def _perm(mode: str) -> dict[str, Any]:
    return {"permissions": {"defaultMode": mode}}


# --- the command line -----------------------------------------------------------------------------


SECRET_ARG = "SOCKET_AUTH_TOKEN-6c1e9b7f"


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ("claude --dangerously-skip-permissions --continue", ProcessFlags(skip_permissions=True)),
        ("/opt/claude --permission-mode acceptEdits", ProcessFlags(permission_mode="acceptEdits")),
        (
            "claude --permission-mode=plan --allow-dangerously-skip-permissions",
            ProcessFlags(allow_skip_permissions=True, permission_mode="plan"),
        ),
        ("claude -p --output-format json", ProcessFlags(print_mode=True)),
        ("claude --print hi", ProcessFlags(print_mode=True)),
        ("claude --settings /w/s.json --resume", ProcessFlags(settings="/w/s.json")),
        (
            'claude --settings {"crossSessionInbound": "hold", "x": "a b"} --verbose',
            ProcessFlags(settings='{"crossSessionInbound": "hold", "x": "a b"}'),
        ),
        ('claude --settings={"a":1}', ProcessFlags(settings='{"a":1}')),
        ("claude --settings {broken json", ProcessFlags(settings="{broken")),
        ("claude --permission-mode", ProcessFlags()),
        ("claude --permission-mode=", ProcessFlags()),
        ("", ProcessFlags()),
    ],
)
def test_the_four_flags_are_read_from_a_ps_line(args: str, expected: ProcessFlags) -> None:
    assert policy.parse_flags(args) == expected


def test_nothing_else_of_the_command_line_is_kept() -> None:
    line = (
        f'claude --mcp-config={{"env":{{"TOKEN":"{SECRET_ARG}"}}}} '
        "--settings /w/s.json --dangerously-skip-permissions"
    )
    flags = policy.parse_flags(line)
    assert SECRET_ARG not in repr(flags)


def test_ps_args_lines() -> None:
    text = "  101 claude --resume\n 7 /bin/zsh -l\nnot a line\n   55\n"
    assert policy.parse_ps_args(text) == {101: "claude --resume", 7: "/bin/zsh -l", 55: ""}


# --- settings files -------------------------------------------------------------------------------


def test_settings_files_are_read_strictly(isolated_env: Path) -> None:
    home = isolated_env / "home"
    good = home / "s.json"
    good.write_text(json.dumps(_inbound("hold")))
    assert policy.read_settings_file(good) == _inbound("hold")
    assert policy.read_settings_file(home / "none.json") is None
    assert policy.read_settings_file(home) is UNREADABLE
    (home / "bad.json").write_text("{nope")
    assert policy.read_settings_file(home / "bad.json") is UNREADABLE
    (home / "list.json").write_text("[1]")
    assert policy.read_settings_file(home / "list.json") is UNREADABLE
    protected = home / "Library" / "Containers"
    protected.mkdir(parents=True)
    (protected / "s.json").write_text(json.dumps(_inbound("accept")))
    assert policy.read_settings_file(protected / "s.json", [protected]) is UNREADABLE
    blocked = home / "blocked.json"
    blocked.write_text("{}")
    blocked.chmod(0)
    try:
        assert policy.read_settings_file(blocked) is UNREADABLE
    finally:
        blocked.chmod(0o600)
    huge = home / "huge.json"
    huge.write_bytes(b" " * (policy.MAX_SETTINGS_BYTES + 1))
    assert policy.read_settings_file(huge) is UNREADABLE


def test_the_settings_flag_gives_inline_json_or_a_file(isolated_env: Path) -> None:
    work = isolated_env / "home" / "work"
    work.mkdir()
    (work / "s.json").write_text(json.dumps(_inbound("refuse")))
    assert policy.flag_settings(None) is None
    assert policy.flag_settings(ProcessFlags()) is None
    inline = ProcessFlags(settings='{"crossSessionInbound": "accept"}')
    assert policy.flag_settings(inline) == _inbound("accept")
    assert policy.flag_settings(ProcessFlags(settings="{broken")) is UNREADABLE
    assert policy.flag_settings(ProcessFlags(settings='{"a": 1} trailing')) is UNREADABLE
    assert policy.flag_settings(ProcessFlags(settings=str(work / "s.json"))) == _inbound("refuse")
    relative = ProcessFlags(settings="s.json")
    assert policy.flag_settings(relative, cwd=str(work)) == _inbound("refuse")
    assert policy.flag_settings(relative) is UNREADABLE
    missing = ProcessFlags(settings=str(work / "missing.json"))
    assert policy.flag_settings(missing) is UNREADABLE


def test_setting_walks_keys() -> None:
    assert policy.setting({"a": {"b": 1}}, "a", "b") == 1
    assert policy.setting({"a": 1}, "a", "b") is None
    assert policy.setting(UNREADABLE, "a") is None
    assert policy.setting(None, "a") is None


# --- check_messaging: doctor over live sessions ---------------------------------------------------


class DoctorPs:
    """``ps -o pid=,lstart=`` and ``ps -o pid=,args=`` over two tables."""

    def __init__(self, starts: Mapping[int, float], args: Mapping[int, str]) -> None:
        self.starts = dict(starts)
        self.args = dict(args)
        self.calls: list[tuple[str, ...]] = []
        self.fail_args = False

    async def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float | None = None,  # noqa: ASYNC109 - mirrors subprocess
        stdin: bytes | None = None,
    ) -> ProcessResult:
        self.calls.append(tuple(argv))
        pids = [int(p) for p in argv[-1].split(",")]
        if argv[2] == "pid=,lstart=":
            text = "".join(f"{p:>6} {ps_text(self.starts[p])}\n" for p in pids if p in self.starts)
        else:
            if self.fail_args:
                raise TimeoutError
            text = "".join(f"{p:>6} {self.args[p]}\n" for p in pids if p in self.args)
        return ProcessResult(tuple(argv), 0 if text else 1, text.encode())


@pytest.fixture
def socks() -> Iterator[Path]:
    with short_tmp() as root:
        yield socket_dir(root)


@pytest.fixture
def layout(isolated_env: Path) -> Layout:
    home = isolated_env / "home"
    return Layout(home=home, dotfiles=home / "dotfiles")


def _config(enabled: bool = False) -> Any:
    return parse_config({"actions": {"message": {"enabled": enabled}}}, source="t.toml")


async def _doctor(
    world: World, layout: Layout, runner: Any, managed: Path, enabled: bool = False
) -> dict[str, Any]:
    checks = await check_messaging(
        _config(enabled),
        layout=layout,
        runner=runner,
        registry=world.registry,
        roots=(str(world.socks),),
        managed_settings=managed,
    )
    return {check.name: check for check in checks}


async def test_doctor_explains_each_live_session_and_never_connects(
    isolated_env: Path, socks: Path, layout: Layout
) -> None:
    world = World(isolated_env / "registry", socks)
    user = layout.home / ".claude"
    user.mkdir(parents=True)
    (user / "settings.json").write_text(json.dumps({"dialogExpiry": "10m"}))
    accept_file = isolated_env / "accept.json"
    accept_file.write_text(json.dumps(_inbound("accept")))
    project = isolated_env / "proj"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "settings.local.json").write_text(json.dumps(_inbound("refuse")))
    other = socket_dir(socks.parent, "other")

    world.add(101, "00000000-0000-4000-8000-000000000101")
    world.add(102, "00000000-0000-4000-8000-000000000102")
    world.add(103, "00000000-0000-4000-8000-000000000103", cwd=str(project))
    world.add(104, "00000000-0000-4000-8000-000000000104", version="2.1.100")
    world.add(105, "00000000-0000-4000-8000-000000000105", socket_path=str(other / "x.sock"))
    world.add(106, "00000000-0000-4000-8000-000000000106", alive=False)  # stale
    world.add(107, "00000000-0000-4000-8000-000000000107", start=None)  # unprovable
    world.add(108, "00000000-0000-4000-8000-000000000108", messagingSocketPath=None)
    (world.registry / "101.abcdef.key").write_text("canary-messaging-token-7f3a91c2")
    runner = DoctorPs(
        world.starts.table,
        {
            101: f'claude --mcp-config={{"t":"{SECRET_ARG}"}} --dangerously-skip-permissions',
            102: f"claude --settings {accept_file} --dangerously-skip-permissions",
            103: "claude",
            104: "claude --dangerously-skip-permissions",
            105: "claude --permission-mode acceptEdits",
        },
    )
    inboxes = [FakeInbox(world.sock_path(pid)) for pid in (101, 102, 103, 104)]
    for inbox in inboxes:
        inbox.__enter__()
    try:
        checks = await _doctor(world, layout, runner, isolated_env / "no-managed.json")
    finally:
        for inbox in inboxes:
            inbox.__exit__(None, None, None)

    assert list(checks) == [
        "messaging",
        "messaging:registry",
        "messaging:101",
        "messaging:102",
        "messaging:103",
        "messaging:104",
        "messaging:105",
        "messaging:108",
    ]
    summary = checks["messaging"]
    assert summary.status is Status.OK
    assert "off ([actions.message] enabled = false)" in summary.detail
    assert "6 live Claude session(s), 2 stale or unprovable row(s) ignored" in summary.detail
    assert "never sends a probe and never changes crossSessionInbound" in summary.detail
    assert checks["messaging:registry"].status is Status.OK

    bypass = checks["messaging:101"]
    assert bypass.status is Status.OK
    assert "Claude 2.1.289, peer protocol 1" in bypass.detail
    assert "no frame is supported (spike 0004)" in bypass.detail
    assert "socket safe (A14)" in bypass.detail
    assert "messages to this session will be held for approval" in bypass.detail
    assert "after 10 minutes" in bypass.detail
    assert "delivered to Claude (crossSessionInbound = accept in --settings)" in (
        checks["messaging:102"].detail
    )
    assert "dropped without delivery" in checks["messaging:103"].detail
    assert "local settings" in checks["messaging:103"].detail
    assert "sending unsupported (version_too_old" in checks["messaging:104"].detail
    outside = checks["messaging:105"]
    assert outside.status is Status.WARN
    assert "socket refused (socket_outside_allowed_roots, A14)" in outside.detail
    assert "prompts for permissions (acceptEdits, from --permission-mode)" in outside.detail
    assert "no inbox socket" in checks["messaging:108"].detail
    assert "permission mode is unknown" in checks["messaging:108"].detail

    everything = json.dumps([c.detail for c in checks.values()])
    assert SECRET_ARG not in everything
    assert str(accept_file) not in everything
    assert "canary-messaging-token" not in everything
    assert all(inbox.connections == 0 for inbox in inboxes)  # no probe, ever
    assert {call[2] for call in runner.calls} == {"pid=,lstart=", "pid=,args="}


async def test_doctor_without_sessions_or_a_runner_shows_the_summary_only(
    isolated_env: Path, socks: Path, layout: Layout
) -> None:
    world = World(isolated_env / "registry", socks)
    empty = await _doctor(world, layout, DoctorPs({}, {}), isolated_env / "m.json")
    assert list(empty) == ["messaging"]
    assert "no Claude session in the registry" in empty["messaging"].detail
    world.add()
    no_runner = await _doctor(world, layout, None, isolated_env / "m.json")
    assert list(no_runner) == ["messaging"]
    assert "sessions not checked (no process runner)" in no_runner["messaging"].detail


async def test_doctor_says_when_on_but_nothing_can_be_sent(
    isolated_env: Path, socks: Path, layout: Layout
) -> None:
    world = World(isolated_env / "registry", socks)
    checks = await _doctor(world, layout, DoctorPs({}, {}), isolated_env / "m.json", enabled=True)
    summary = checks["messaging"]
    assert summary.status is Status.WARN
    assert "no message frame is supported (spike 0004: no-go)" in summary.detail
    assert "focus-and-copy is the action" in summary.detail


async def test_doctor_reports_ps_failures_and_unsafe_registries(
    isolated_env: Path, socks: Path, layout: Layout
) -> None:
    world = World(isolated_env / "registry", socks)
    world.add()

    class Broken:
        async def run(self, argv: Sequence[str], **_: Any) -> ProcessResult:
            raise FileNotFoundError(argv[0])

    failed = await _doctor(world, layout, Broken(), isolated_env / "m.json")
    assert failed["messaging:sessions"].status is Status.WARN
    assert "liveness unknown" in failed["messaging:sessions"].detail

    runner = DoctorPs(world.starts.table, {})
    runner.fail_args = True
    os.chmod(world.registry, 0o777)
    try:
        checks = await _doctor(world, layout, runner, isolated_env / "m.json")
    finally:
        os.chmod(world.registry, 0o755)
    registry = checks["messaging:registry"]
    assert registry.status is Status.WARN
    assert "mode 0777" in registry.detail
    assert "permission mode is unknown" in checks["messaging:4242"].detail


async def test_doctor_reads_managed_settings_first(
    isolated_env: Path, socks: Path, layout: Layout
) -> None:
    world = World(isolated_env / "registry", socks)
    world.add()
    managed = isolated_env / "managed-settings.json"
    managed.write_text(json.dumps(_inbound("refuse")))
    user = layout.home / ".claude"
    user.mkdir(parents=True)
    (user / "settings.json").write_text(json.dumps(_inbound("accept")))
    with FakeInbox(world.sock_path()):
        checks = await _doctor(
            world, layout, DoctorPs(world.starts.table, {4242: "claude"}), managed
        )
    assert "crossSessionInbound = refuse in managed settings" in checks["messaging:4242"].detail


def test_run_doctor_ends_with_the_messaging_summary(layout: Layout) -> None:
    def down(path: str, **_: Any) -> Any:
        from fylgja.cli import client

        raise client.DaemonDown("u", "refused")

    checks = asyncio.run(
        run_doctor(layout=layout, fetch=down, probe=lambda port: False, source=lambda: [])
    )
    assert checks[-1].name == "messaging"
    assert checks[-1].status is Status.OK
    assert T0  # the registry helpers share claude_support's start time
