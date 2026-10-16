"""The pure attention-alert decision core (R1–R12 of ``docs/decisions/0055``).

Every scenario drives :func:`fylgja.core.notify.decide` with hand-built snapshots: times are
monotonic seconds, and the wall time of a snapshot at ``t`` is ``BASE + t``. Unless a test says
otherwise the settings are the defaults with ``desk_command = ["/n"]``, and one ``permission``
entry for ``claude:a`` (folder ``fylgja``) is first seen at t=0 with ``since`` equal to that
snapshot's wall time. The scenario numbers of the plan are in each test's docstring.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from fylgja.config import NotifyConfig
from fylgja.core.notify import (
    LATE_SIGHT_FLOOR,
    Alert,
    Decision,
    Episode,
    NotifyState,
    Snapshot,
    candidates,
    decide,
    on_screen,
    render_argv,
    url_for,
)
from fylgja.core.waiting import WaitingEntry
from fylgja.store.rows import SessionRow
from fylgja.testing.fakes import FakeClock

BASE = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
REASON = "waiting for permission: Bash"
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def wall(t: float) -> datetime:
    return BASE + timedelta(seconds=t)


def row(
    key: str,
    *,
    folder: str | None = "fylgja",
    window: str | None = None,
    parent: str | None = None,
    kind: str | None = None,
) -> SessionRow:
    prefix, _, native = key.partition(":")
    project = None if folder is None else f"/work/{folder}"
    return SessionRow(
        session_key=key,
        kind=prefix if kind is None else kind,
        native_id=native,
        source="agents",
        pid=None,
        process_key=None,
        cwd=project,
        project_path=project,
        host_kind=None,
        host_ref=None,
        window_id=window,
        workspace=None,
        name=None,
        title="a title that never shows",
        model=None,
        branch=None,
        state="waiting_permission",
        state_since=BASE,
        waiting_reason="Bash",
        parent_key=parent,
        predecessor_key=None,
        remote_url=None,
        first_seen=BASE,
        ended_at=None,
        last_seen=BASE,
    )


def waiting(
    target: str,
    kind: str = "permission",
    *,
    since: datetime | None = BASE,
    reason: str = REASON,
    target_kind: str = "session",
) -> WaitingEntry:
    return WaitingEntry(
        kind=kind, target=target, target_kind=target_kind, reason=reason, since=since
    )


def config(**update: Any) -> NotifyConfig:
    return NotifyConfig(**{"desk_command": ["/n"], **update})


def assert_well_formed(alert: Alert) -> None:
    """R11: every title and message starts with a letter or digit, is clean and is clipped."""
    for text, limit in ((alert.title, 64), (alert.message, 200)):
        assert text[0].isalnum(), text
        assert len(text) <= limit
        assert CONTROL.search(text) is None


class Run:
    """Carries the state from one evaluation to the next, as the daemon's loop does."""

    def __init__(self, *sessions: SessionRow, settings: NotifyConfig | None = None) -> None:
        self.sessions = list(sessions) or [row("claude:a")]
        self.settings = config() if settings is None else settings
        self.state = NotifyState()
        self.alerts: list[Alert] = []

    def at(
        self,
        t: float,
        entries: Sequence[WaitingEntry] | None = None,
        *,
        screen: frozenset[str] = frozenset(),
        away: bool = False,
    ) -> Decision:
        listed = [waiting("claude:a")] if entries is None else entries
        snapshot = Snapshot(
            now=t,
            wall_now=wall(t),
            candidates=candidates(listed, self.sessions),
            on_screen=screen,
            away=away,
        )
        decision = decide(snapshot, self.state, self.settings)
        self.state = decision.state
        if decision.alert is not None:
            assert_well_formed(decision.alert)
            self.alerts.append(decision.alert)
        return decision

    def episodes_of(self, target: str) -> list[Episode]:
        return [episode for key, episode in self.state.episodes.items() if key.target == target]


def announced(run: Run, entries: Sequence[WaitingEntry] | None = None) -> Alert:
    """First sighting at t=0 and the first alert at t=20."""
    assert run.at(0, entries).alert is None
    alert = run.at(20, entries).alert
    assert alert is not None
    return alert


# --------------------------------------------------------------------------------------------
# Grace, the first alert and the reminder (R3, R7)
# --------------------------------------------------------------------------------------------


def test_nothing_is_due_before_the_grace_ends() -> None:
    """Scenario 1."""
    run = Run()
    assert run.at(0).next_check == 20
    decision = run.at(19.9)
    assert decision.alert is None
    assert decision.next_check == 20


def test_the_first_alert_comes_when_the_grace_ends() -> None:
    """Scenario 2."""
    run = Run()
    run.at(0)
    alert = run.at(20).alert
    assert alert is not None
    assert alert.channel == "desk"
    assert alert.title == "Fylgja"
    assert not alert.reminder_only
    assert alert.message.startswith("Claude in fylgja:")
    assert "waiting for permission" in alert.message
    assert alert.message == "Claude in fylgja: waiting for permission: Bash"
    assert alert.families == 1
    assert alert.kinds == ("permission",)
    assert alert.session_keys == ("claude:a",)
    assert alert.key == "claude:a"


def test_an_announced_episode_waits_for_its_reminder() -> None:
    """Scenario 3."""
    run = Run()
    announced(run)
    decision = run.at(30)
    assert decision.alert is None
    assert decision.next_check == 620


def test_one_reminder_and_then_silence() -> None:
    """Scenario 4."""
    run = Run()
    announced(run)
    assert run.at(619.9).alert is None
    reminder = run.at(620).alert
    assert reminder is not None
    assert reminder.title == "Fylgja: still waiting"
    assert reminder.reminder_only
    decision = run.at(5000)
    assert decision.alert is None
    assert decision.next_check is None
    assert len(run.alerts) == 2


def test_reminder_seconds_zero_turns_reminders_off() -> None:
    """Scenario 5."""
    run = Run(settings=config(reminder_seconds=0))
    run.at(0)
    decision = run.at(20)
    assert decision.alert is not None
    assert decision.next_check is None
    for t in (30, 620, 5000, 90_000):
        assert run.at(t).alert is None
    assert len(run.alerts) == 1


def test_grace_counts_from_since() -> None:
    """Scenario 29: a ``since`` 5 s old at the first sighting leaves 15 s of grace."""
    run = Run()
    entry = [waiting("claude:a", since=wall(-5))]
    assert run.at(0, entry).next_check == 15
    decision = run.at(14.9, entry)
    assert decision.alert is None
    assert decision.next_check == 15
    assert run.at(15, entry).alert is not None


def test_a_late_first_sighting_waits_for_the_floor() -> None:
    """Scenario 30: past its grace, an entry is due 5 s after it is first seen."""
    entry = [waiting("claude:a", since=wall(-300))]
    run = Run()
    decision = run.at(0, entry)
    assert decision.alert is None
    assert decision.next_check == LATE_SIGHT_FLOOR == 5
    assert run.at(4.9, entry).alert is None
    assert run.at(5, entry).alert is not None

    instant = Run(settings=config(grace_seconds=0))
    assert instant.at(0, entry).alert is not None


def test_an_unknown_or_future_since_gets_the_full_grace() -> None:
    """Scenario 31: clock skew and an unknown ``since`` both count as age 0."""
    skewed = Run()
    entry = [waiting("claude:a", since=wall(60))]
    assert skewed.at(0, entry).next_check == 20
    assert skewed.at(19.9, entry).alert is None
    assert skewed.at(20, entry).alert is not None

    project = Run(settings=config(kinds=["build_failed"]))
    unknown = [
        waiting(
            "/work/fylgja",
            "build_failed",
            since=None,
            reason="build failed on fylgja",
            target_kind="project",
        )
    ]
    assert project.at(0, unknown).next_check == 20
    assert project.at(19.9, unknown).alert is None
    assert project.at(20, unknown).alert is not None


async def test_grace_runs_on_the_monotonic_clock_only() -> None:
    """Scenario 27: sleep moves the wall clock, not the deadline."""
    clock = FakeClock(BASE)
    run = Run()
    entry = [waiting("claude:a", since=clock.now())]

    def evaluate() -> Decision:
        snapshot = Snapshot(
            now=clock.monotonic(),
            wall_now=clock.now(),
            candidates=candidates(entry, run.sessions),
            on_screen=frozenset(),
            away=False,
        )
        decision = decide(snapshot, run.state, run.settings)
        run.state = decision.state
        return decision

    assert evaluate().next_check == 20
    await clock.advance(5)
    clock.suspend(3600)
    assert clock.monotonic() == 5
    decision = evaluate()
    assert decision.alert is None
    assert decision.next_check == 20
    await clock.advance_to(19.9)
    assert evaluate().alert is None
    await clock.advance_to(20)
    assert evaluate().alert is not None


# --------------------------------------------------------------------------------------------
# Episodes, tombstones and recurrence (R2, R10)
# --------------------------------------------------------------------------------------------


def test_a_new_since_is_a_new_episode() -> None:
    """Scenario 6."""
    run = Run()
    announced(run)
    again = [waiting("claude:a", since=wall(100))]
    decision = run.at(100, again)
    assert decision.alert is None
    assert decision.next_check == 120
    alert = run.at(120, again).alert
    assert alert is not None
    assert alert.title == "Fylgja"
    assert not alert.reminder_only


def test_the_same_prompt_repeated_is_the_same_episode() -> None:
    """The same ``since`` evaluation after evaluation keeps one episode."""
    run = Run()
    for t in (0, 5, 10, 15):
        run.at(t)
    assert len(run.state.episodes) == 1
    assert run.at(20).alert is not None
    assert len(run.state.episodes) == 1


def test_a_return_with_the_same_since_continues_the_episode() -> None:
    """Scenario 7, first part: a flap does not alert again; the reminder keeps its time."""
    run = Run()
    announced(run)
    assert run.at(25, []).alert is None
    decision = run.at(100)
    assert decision.alert is None
    assert decision.next_check == 620
    reminder = run.at(620).alert
    assert reminder is not None
    assert reminder.reminder_only


def test_a_return_after_a_day_is_a_new_episode() -> None:
    """Scenario 7, second part: the tombstone is gone, and a day-old ``since`` gets the floor."""
    run = Run()
    announced(run)
    run.at(25, [])
    back = 25 + 86_401
    decision = run.at(back)
    assert decision.alert is None
    assert decision.next_check == back + 5
    alert = run.at(back + 5).alert
    assert alert is not None
    assert alert.title == "Fylgja"
    assert not alert.reminder_only


def test_an_entry_that_leaves_within_the_grace_never_alerts() -> None:
    """Scenario 8: no alert, and nothing of a "resolved" kind."""
    run = Run()
    run.at(0)
    decision = run.at(10, [])
    assert decision.alert is None
    assert decision.next_check is None
    for t in (20, 620, 5000):
        assert run.at(t, []).alert is None
    assert run.alerts == []
    [episode] = run.episodes_of("claude:a")
    assert episode.closed_at == 10
    assert episode.sent_at is None


def test_a_pending_episode_that_returns_keeps_its_due_time() -> None:
    """A tombstone continues the state it had: a pending episode stays due at its first time."""
    run = Run()
    run.at(0)
    run.at(10, [])
    decision = run.at(15)
    assert decision.alert is None
    assert decision.next_check == 20
    assert run.at(20).alert is not None


def test_a_project_since_that_moves_is_one_episode() -> None:
    """Scenario 19: a project or window episode is keyed without ``since`` while listed."""
    run = Run(settings=config(kinds=["build_failed"]))

    def build(since: datetime) -> list[WaitingEntry]:
        return [
            waiting(
                "/work/fylgja",
                "build_failed",
                since=since,
                reason="build failed on fylgja",
                target_kind="project",
            )
        ]

    assert run.at(0, build(wall(0))).alert is None
    assert run.at(10, build(wall(8))).alert is None
    assert len(run.state.episodes) == 1
    alert = run.at(20, build(wall(8))).alert
    assert alert is not None
    assert alert.message == "A project in fylgja: build failed on fylgja"
    [episode] = run.episodes_of("/work/fylgja")
    assert episode.last_since == wall(8)
    run.at(30, build(wall(3)))
    [episode] = run.episodes_of("/work/fylgja")
    assert episode.last_since == wall(8)
    assert len(run.alerts) == 1


def _resolved_build(since: datetime | None) -> Run:
    """Scenario 32's start: announced at t=20, gone at t=100, back at t=200 with ``since``."""
    run = Run(settings=config(kinds=["build_failed"]))
    failed = [
        waiting(
            "/work/fylgja",
            "build_failed",
            since=wall(0),
            reason="build failed on fylgja",
            target_kind="project",
        )
    ]
    announced(run, failed)
    assert run.at(100, []).alert is None
    back = [
        waiting(
            "/work/fylgja",
            "build_failed",
            since=since,
            reason="build failed on fylgja",
            target_kind="project",
        )
    ]
    run.at(200, back)
    return run


def test_a_build_that_fails_again_alerts_again() -> None:
    """Scenario 32: a later ``since`` after the entry left is a new occurrence."""
    run = _resolved_build(wall(200))
    assert len(run.alerts) == 1
    again = [
        waiting(
            "/work/fylgja",
            "build_failed",
            since=wall(200),
            reason="build failed on fylgja",
            target_kind="project",
        )
    ]
    assert run.at(219.9, again).alert is None
    alert = run.at(220, again).alert
    assert alert is not None
    assert alert.title == "Fylgja"
    assert not alert.reminder_only


def test_a_build_that_flaps_does_not_alert_again() -> None:
    """Scenario 32: the same or an unknown ``since`` continues the old episode."""
    for since in (wall(0), None):
        run = _resolved_build(since)
        assert len(run.alerts) == 1
        flap = [
            waiting(
                "/work/fylgja",
                "build_failed",
                since=since,
                reason="build failed on fylgja",
                target_kind="project",
            )
        ]
        decision = run.at(210, flap)
        assert decision.alert is None
        assert decision.next_check == 620
        reminder = run.at(620, flap).alert
        assert reminder is not None
        assert reminder.reminder_only


def test_an_unread_marker_that_returns_later_alerts_again() -> None:
    """Scenario 33."""
    run = Run(settings=config(kinds=["unread"]))

    def unread(since: datetime) -> list[WaitingEntry]:
        return [waiting("w9", "unread", since=since, reason="2 unread", target_kind="window")]

    first = announced(run, unread(wall(0)))
    assert first.message == "A window: 2 unread"
    assert first.key == ""
    assert run.at(50, []).alert is None
    decision = run.at(60, unread(wall(60)))
    assert decision.alert is None
    assert decision.next_check == 80
    assert run.at(80, unread(wall(60))).alert is not None


def test_decide_leaves_its_input_state_alone() -> None:
    """``decide`` returns a new state; the state it was given is unchanged."""
    run = Run()
    run.at(0)
    before = run.state
    snapshot_of = dict(before.episodes)
    decision = decide(
        Snapshot(
            now=20,
            wall_now=wall(20),
            candidates=candidates([waiting("claude:a")], run.sessions),
            on_screen=frozenset(),
            away=False,
        ),
        before,
        run.settings,
    )
    assert decision.alert is not None
    assert dict(before.episodes) == snapshot_of
    assert decision.state is not before


# --------------------------------------------------------------------------------------------
# Families and the screen (R4, R5, R6)
# --------------------------------------------------------------------------------------------


def test_a_focused_session_is_held_until_it_leaves_the_screen() -> None:
    """Scenario 9."""
    sessions = [row("claude:a", window="w1")]
    run = Run(*sessions)
    screen = on_screen("w1", sessions)
    assert screen == {"claude:a"}
    run.at(0, screen=screen)
    decision = run.at(20, screen=screen)
    assert decision.alert is None
    assert decision.next_check is None
    alert = run.at(50, screen=frozenset()).alert
    assert alert is not None
    assert alert.title == "Fylgja"


def test_a_family_is_held_when_its_child_is_on_screen() -> None:
    """Scenario 10, first part: the run waits, its child's window has focus."""
    sessions = [row("norn:r", window="w2"), row("claude:c", window="w1", parent="norn:r")]
    run = Run(*sessions)
    screen = on_screen("w1", sessions)
    assert screen == {"norn:r"}
    entry = [waiting("norn:r", "input", reason="waiting for input: failure recovery")]
    run.at(0, entry, screen=screen)
    assert run.at(20, entry, screen=screen).alert is None
    assert run.at(30, entry).alert is not None


def test_a_family_is_held_when_its_root_is_on_screen() -> None:
    """Scenario 10, second part: the child asks, the run's window has focus."""
    sessions = [row("norn:r", window="w1"), row("claude:c", window="w2", parent="norn:r")]
    run = Run(*sessions)
    screen = on_screen("w1", sessions)
    assert screen == {"norn:r"}
    entry = [waiting("claude:c")]
    assert candidates(entry, sessions)[0].family == "norn:r"
    run.at(0, entry, screen=screen)
    assert run.at(20, entry, screen=screen).alert is None


def test_every_family_in_the_focused_window_is_held() -> None:
    """Scenario 11 (D5): focus is per window, and holds nothing in another window."""
    sessions = [
        row("claude:a", window="w1"),
        row("claude:b", window="w1"),
        row("claude:z", window="w2"),
    ]
    run = Run(*sessions)
    w1 = on_screen("w1", sessions)
    assert w1 == {"claude:a", "claude:b"}
    entries = [waiting("claude:a"), waiting("claude:z")]
    run.at(0, entries, screen=w1)
    alert = run.at(20, entries, screen=w1).alert
    assert alert is not None
    assert alert.session_keys == ("claude:z",)
    w2 = on_screen("w2", sessions)
    assert w2 == {"claude:z"}
    moved = run.at(40, entries, screen=w2).alert
    assert moved is not None
    assert moved.session_keys == ("claude:a",)


def test_nothing_is_held_while_away() -> None:
    """Scenario 12."""
    sessions = [row("claude:a", window="w1")]
    run = Run(*sessions)
    run.at(0, screen=frozenset({"claude:a"}), away=True)
    assert run.at(20, screen=frozenset({"claude:a"}), away=True).alert is not None

    assert on_screen(None, sessions) == frozenset()
    nobody = Run(*sessions)
    nobody.at(0, screen=on_screen(None, sessions))
    assert nobody.at(20, screen=on_screen(None, sessions)).alert is not None


def test_a_held_reminder_waits_too() -> None:
    """Assumption 2: reminders are held by focus as first alerts are."""
    sessions = [row("claude:a", window="w1")]
    run = Run(*sessions)
    announced(run)
    screen = on_screen("w1", sessions)
    decision = run.at(620, screen=screen)
    assert decision.alert is None
    assert decision.next_check is None
    reminder = run.at(700).alert
    assert reminder is not None
    assert reminder.reminder_only


def test_the_family_walk_stops_at_a_cycle_a_gap_and_eight_steps() -> None:
    """R4: a missing parent, a repeated key and a long chain all end the walk."""
    cycle = [row("claude:x", parent="claude:y"), row("claude:y", parent="claude:x")]
    assert candidates([waiting("claude:x")], cycle)[0].family == "claude:y"

    orphan = [row("claude:o", parent="norn:gone")]
    assert candidates([waiting("claude:o")], orphan)[0].family == "claude:o"

    chain = [row("claude:s0")]
    chain += [row(f"claude:s{n}", parent=f"claude:s{n - 1}") for n in range(1, 11)]
    assert candidates([waiting("claude:s10")], chain)[0].family == "claude:s2"
    assert candidates([waiting("claude:s5")], chain)[0].family == "claude:s0"


def test_on_screen_counts_only_live_sessions_of_the_window() -> None:
    ended = replace(row("claude:e", window="w1"), ended_at=BASE)
    sessions = [row("claude:a", window="w1"), ended, row("claude:b", window="w2")]
    assert on_screen("w1", sessions) == {"claude:a"}
    assert on_screen("w3", sessions) == frozenset()


# --------------------------------------------------------------------------------------------
# Coalescing and the channel (R8, R9)
# --------------------------------------------------------------------------------------------


def test_families_due_together_give_one_alert() -> None:
    """Scenario 13."""
    three = [row(f"claude:{name}", folder=name) for name in "abc"]
    entries = [waiting(r.session_key) for r in three]
    alert = announced(Run(*three), entries)
    assert alert.message == "3 sessions wait for you: a, b, c"
    assert alert.families == 3
    assert alert.key == ""
    assert alert.session_keys == ("claude:a", "claude:b", "claude:c")

    five = [row(f"claude:{name}", folder=name) for name in "abcde"]
    alert = announced(Run(*five), [waiting(r.session_key) for r in five])
    assert alert.message == "5 sessions wait for you: a, b, c, +2 more"

    terse = announced(Run(*three, settings=config(desk_detail="kind")), entries)
    assert terse.message == "3 sessions wait for you"


def test_coalescing_orders_by_since_then_target() -> None:
    """R9: the oldest ``since`` first, an unknown one last, then the target."""
    sessions = [row(f"claude:{name}", folder=name) for name in "abcd"]
    entries = [
        waiting("claude:a", since=None),
        waiting("claude:b", since=wall(0)),
        waiting("claude:c", since=wall(-2)),
        waiting("claude:d", since=wall(-2)),
    ]
    run = Run(*sessions, settings=config(grace_seconds=0))
    alert = run.at(0, entries).alert
    assert alert is not None
    assert alert.session_keys == ("claude:c", "claude:d", "claude:b", "claude:a")
    assert alert.message == "4 sessions wait for you: c, d, b, +1 more"


def test_one_family_with_two_waiting_sessions_is_named_once() -> None:
    """Scenario 14: a run and its child, both due, are one family."""
    sessions = [row("norn:r"), row("claude:c", parent="norn:r")]
    entries = [
        waiting("norn:r", "input", reason="waiting for input: failure recovery"),
        waiting("claude:c"),
    ]
    alert = announced(Run(*sessions), entries)
    assert alert.families == 1
    assert alert.message == "Claude in fylgja: waiting for permission: Bash"
    assert "wait for you" not in alert.message
    assert alert.session_keys == ("claude:c", "norn:r")
    assert len(alert.episodes) == 2
    assert alert.kinds == ("permission", "input")
    assert alert.key == "claude:c"


def test_a_family_counts_once_among_several() -> None:
    """R9, R11: a run and its child count as one family; the family's first entry names it."""
    sessions = [
        row("norn:r", folder="run"),
        row("claude:c", folder="child", parent="norn:r"),
        row("claude:z", folder="other"),
    ]
    entries = [waiting("claude:c"), waiting("norn:r", "input"), waiting("claude:z")]
    alert = announced(Run(*sessions), entries)
    assert alert.families == 2
    assert alert.message == "2 sessions wait for you: child, other"
    assert alert.session_keys == ("claude:c", "claude:z", "norn:r")


def test_the_away_channel_carries_only_kind_and_source() -> None:
    """Scenario 15."""
    sessions = [row("norn:r", folder="customer-folder")]
    entry = [waiting("norn:r", "input", reason="waiting for input: failure recovery")]
    run = Run(*sessions, settings=config(away_command=["/p"]))
    run.at(0, entry, away=True)
    alert = run.at(20, entry, away=True).alert
    assert alert is not None
    assert alert.channel == "away"
    assert alert.message == "Norn waits for input"
    assert "customer-folder" not in alert.message
    assert "failure recovery" not in alert.message


def test_away_without_an_away_command_uses_the_desk() -> None:
    """Scenario 16."""
    sessions = [row("norn:r")]
    entry = [waiting("norn:r", "input", reason="waiting for input: failure recovery")]
    run = Run(*sessions, settings=config(away_command=[]))
    run.at(0, entry, away=True)
    alert = run.at(20, entry, away=True).alert
    assert alert is not None
    assert alert.channel == "desk"
    assert alert.message == "Norn in fylgja: waiting for input: failure recovery"


def test_without_a_channel_the_episode_waits_unannounced() -> None:
    """Scenario 17: phone-only, at the desk: nothing until away begins."""
    run = Run(settings=config(desk_command=[], away_command=["/p"]))
    run.at(0)
    decision = run.at(20)
    assert decision.alert is None
    assert decision.next_check is None
    [episode] = run.episodes_of("claude:a")
    assert episode.sent_at is None
    alert = run.at(30, away=True).alert
    assert alert is not None
    assert alert.channel == "away"
    assert alert.message == "Claude needs permission"


def test_a_first_alert_and_a_reminder_go_out_together() -> None:
    """Scenario 28: one alert, not reminder-only; the reminder is marked."""
    sessions = [row("claude:a", folder="a"), row("claude:b", folder="b")]
    run = Run(*sessions)
    announced(run)
    both = [waiting("claude:a"), waiting("claude:b", since=wall(600))]
    assert run.at(600, both).next_check == 620
    alert = run.at(620, both).alert
    assert alert is not None
    assert alert.title == "Fylgja"
    assert not alert.reminder_only
    assert alert.families == 2
    [a] = run.episodes_of("claude:a")
    [b] = run.episodes_of("claude:b")
    assert a.reminded
    assert b.sent_at == 620
    assert not b.reminded
    assert run.at(1220, both).alert is not None
    assert run.at(5000, both).alert is None


# --------------------------------------------------------------------------------------------
# Kinds (R1)
# --------------------------------------------------------------------------------------------


def test_kinds_outside_the_setting_are_ignored() -> None:
    """Scenario 18."""
    run = Run()
    entry = [waiting("claude:a", "turn_done", reason="finished a turn nobody has looked at")]
    run.at(0, entry)
    decision = run.at(20, entry)
    assert decision.alert is None
    assert decision.next_check is None
    assert run.state.episodes == {}


# --------------------------------------------------------------------------------------------
# Text (R11)
# --------------------------------------------------------------------------------------------


def test_detail_kind_leaves_out_folder_and_reason() -> None:
    """Scenario 20."""
    sessions = [row("claude:a", folder="cbslink-acme")]
    entry = [waiting("claude:a", reason="secret plan")]
    alert = announced(Run(*sessions, settings=config(desk_detail="kind")), entry)
    assert alert.message == "Claude needs permission"
    for secret in ("cbslink-acme", "secret plan"):
        assert secret not in alert.message
        assert secret not in alert.title


def test_values_are_sanitised() -> None:
    """Scenario 21: control characters and a leading dash or bracket cannot lead or break it."""
    sessions = [row("claude:a", folder="-x\n[y\x00")]
    alert = announced(Run(*sessions))
    assert CONTROL.search(alert.message) is None
    assert alert.message[0].isalpha()
    assert alert.message == "Claude in -x [y: waiting for permission: Bash"
    assert len(alert.message) <= 200
    assert len(alert.title) <= 64


def test_a_long_message_is_clipped() -> None:
    sessions = [row("claude:a", folder="x" * 300)]
    alert = announced(Run(*sessions))
    assert len(alert.message) == 200
    assert alert.message.endswith("…")


def test_a_session_without_a_folder() -> None:
    sessions = [row("claude:a", folder=None)]
    alert = announced(Run(*sessions))
    assert alert.message == "Claude: waiting for permission: Bash"


def test_a_kind_that_does_not_start_with_a_letter_reads_agent() -> None:
    """Scenario 24."""
    sessions = [row("claude:a", kind="-x")]
    [candidate] = candidates([waiting("claude:a")], sessions)
    assert candidate.source == "Agent"
    alert = announced(Run(*sessions, settings=config(desk_detail="kind")))
    assert alert.message == "Agent needs permission"


def test_source_labels_come_from_the_target() -> None:
    """R11: the session kind upper-cased; ``A project``, ``A window``; ``Agent`` when unknown."""
    entries = [
        waiting("norn:r", "input"),
        waiting("claude:gone"),
        waiting("/work/fylgja", "build_failed", target_kind="project"),
        waiting("/", "review_requested", target_kind="project"),
        waiting("w1", "unread", target_kind="window"),
    ]
    found = candidates(entries, [row("norn:r", folder="fylgja")])
    assert [(c.source, c.folder, c.family) for c in found] == [
        ("Norn", "fylgja", "norn:r"),
        ("Agent", None, "claude:gone"),
        ("A project", "fylgja", "/work/fylgja"),
        ("A project", None, "/"),
        ("A window", None, "w1"),
    ]


def test_every_kind_has_its_verb() -> None:
    """R11: the six verbs of detail ``kind``."""
    verbs = {
        "permission": "Claude needs permission",
        "input": "Claude waits for input",
        "turn_done": "Claude finished a turn",
        "build_failed": "A project has a failed build",
        "review_requested": "A project has a review request",
        "unread": "A window has unread messages",
    }
    targets = {"build_failed": "project", "review_requested": "project", "unread": "window"}
    for kind, message in verbs.items():
        target_kind = targets.get(kind, "session")
        target = {"session": "claude:a", "project": "/work/p", "window": "w1"}[target_kind]
        entry = [waiting(target, kind, target_kind=target_kind)]
        alert = announced(Run(settings=config(kinds=[kind], desk_detail="kind")), entry)
        assert alert.message == message


def test_several_items_that_are_not_all_sessions() -> None:
    """R11: ``items`` when a target is not a session; a label falls back to the source."""
    sessions = [row("claude:a", folder=None)]
    entries = [
        waiting("claude:a", since=wall(-2)),
        waiting("w1", "unread", since=wall(-1), reason="1 unread", target_kind="window"),
        waiting("/work/p", "build_failed", since=wall(0), target_kind="project"),
    ]
    settings = config(kinds=["permission", "unread", "build_failed"])
    alert = announced(Run(*sessions, settings=settings), entries)
    assert alert.message == "3 items wait for you: Claude, A window, p"
    assert alert.session_keys == ("claude:a",)


def test_labels_are_named_once() -> None:
    """R11: two families in the same folder give one label."""
    sessions = [row("claude:a"), row("claude:b")]
    alert = announced(Run(*sessions), [waiting("claude:a"), waiting("claude:b")])
    assert alert.message == "2 sessions wait for you: fylgja"


def test_fifty_families_stay_short_and_stable() -> None:
    """Scenario 25."""
    sessions = [row(f"claude:s{n:02}", folder=f"f{n:02}") for n in range(50)]
    entries = [waiting(r.session_key) for r in sessions]
    alert = announced(Run(*sessions), entries)
    assert len(alert.message) <= 200
    assert alert.message == "50 sessions wait for you: f00, f01, f02, +47 more"
    again = announced(Run(*reversed(sessions)), list(reversed(entries)))
    assert again.message == alert.message
    assert again.session_keys == alert.session_keys


# --------------------------------------------------------------------------------------------
# Substitution (R12)
# --------------------------------------------------------------------------------------------


def test_render_argv_substitutes_once() -> None:
    """Scenario 22."""
    command = ["/n", "{title}", "--data-raw={message}", '{"a": 1}', "{title}{key}"]
    values = {
        "title": "Fylgja",
        "message": "Claude waits {url} {key}",
        "url": "http://127.0.0.1:7319/#recall",
        "key": "claude:a",
    }
    argv = render_argv(command, values)
    assert argv == [
        "/n",
        "Fylgja",
        "--data-raw=Claude waits {url} {key}",
        '{"a": 1}',
        "Fylgjaclaude:a",
    ]
    assert len(argv) == len(command)
    assert render_argv(["/n", "{url}", "{other}"], {}) == ["/n", "", "{other}"]


def test_the_key_is_set_only_for_one_safe_session_family() -> None:
    """Scenario 23."""
    two = [row("claude:a"), row("claude:b")]
    assert announced(Run(*two), [waiting("claude:a"), waiting("claude:b")]).key == ""

    project = [waiting("/work/p", "build_failed", target_kind="project")]
    assert announced(Run(settings=config(kinds=["build_failed"])), project).key == ""

    for key, expected in (
        ("norn:plan@a1b2c3:7", "norn:plan@a1b2c3:7"),
        ("norn:a b@x:1", ""),
        ("claude:'x", ""),
        ("claude:a;rm", ""),
        ("-x:1", ""),
    ):
        alert = announced(Run(row(key)), [waiting(key)])
        assert alert.key == expected, key


def test_url_for() -> None:
    """Scenario 26."""
    assert url_for(7319) == "http://127.0.0.1:7319/#recall"
    assert url_for(0) == ""
