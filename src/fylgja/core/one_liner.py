"""Deterministic one-liners (``PLAN-final-plan.md`` §4.6): what a session is doing, in a sentence.

:func:`session_one_liner` is a pure function of a normalized
:class:`~fylgja.models.SessionActivity`. No model is called. The first rule that yields text wins:

1. ``waiting``: "Waiting for permission: <tool> <target>" when a tool call is pending, else
   "Waiting for input: <reason>";
2. ``stage``: a norn stage, "<pipeline> · <stage> (<index>/<total>)", or "<pipeline> <status>"
   once the run has finished (the pipeline is the activity's ``title``);
3. ``tool``: working with a current tool, "<tool> <target>";
4. ``task``: working with an open task, the task text;
5. ``title_prompt``: the title, ": " and the last prompt;
6. ``title_reply``: the title, ": " and the first sentence of the last reply (either one alone
   when the other is missing);
7. ``none``: "—".

The text is whitespace-collapsed, redacted with ``redact`` when one is given, and capped at
:data:`MAX_CHARS` characters (the last one is "…" when the text was cut). It is never empty.

The store keeps the text in ``session_snapshots.one_liner``; ``/now`` exposes it with the rule name
as ``one_liner_from``. The ingest gate has already redacted every free-text field of the activity
by the time the writer calls this, and ``redact`` makes the function safe on its own as well.

**Workspace hook (step 14).** The workspace one-liner (``PLAN.md`` §7) is
``<effective label> — <live context>``; it is built from session one-liners by the state engine
and shares :func:`finalize` for collapsing, redaction and the cap. :data:`WORKSPACE_SEPARATOR` is
its separator.

This module imports only the standard library and :mod:`fylgja.models`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Final

from fylgja.models import SessionActivity

__all__ = [
    "EMPTY",
    "MAX_CHARS",
    "RULES",
    "TERMINAL_STATES",
    "WORKSPACE_SEPARATOR",
    "finalize",
    "first_sentence",
    "session_one_liner",
]

MAX_CHARS: Final = 160
EMPTY: Final = "—"
WORKSPACE_SEPARATOR: Final = " — "
ELLIPSIS: Final = "…"

RULES: Final = ("waiting", "stage", "tool", "task", "title_prompt", "title_reply", "none")
"""The rule names, in the order §4.6 tries them; ``one_liner_from`` is one of these."""

TERMINAL_STATES: Final = frozenset(
    {"done", "finished", "succeeded", "failed", "cancelled", "aborted"}
)
"""Run states (norn) that end rule 2's stage line."""

_WHITESPACE: Final = re.compile(r"\s+")
_CONTROL: Final = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_SENTENCE_END: Final = re.compile(r"(?<=[.!?])\s+")

Redactor = Callable[[str], str]


def _clean(value: str | None) -> str:
    """Collapse whitespace and drop control characters; ``None`` is empty."""
    if value is None:
        return ""
    return _WHITESPACE.sub(" ", _CONTROL.sub(" ", value)).strip()


def first_sentence(text: str | None) -> str:
    """The first sentence of ``text`` (up to ``.``, ``!`` or ``?`` before a space), collapsed."""
    return _SENTENCE_END.split(_clean(text), maxsplit=1)[0]


def finalize(text: str, redact: Redactor | None = None) -> str:
    """Collapse, redact and cap ``text``; an empty result is :data:`EMPTY`."""
    cleaned = _clean(text)
    if redact is not None:
        cleaned = _clean(redact(cleaned))
    if not cleaned:
        return EMPTY
    if len(cleaned) > MAX_CHARS:
        return cleaned[: MAX_CHARS - 1].rstrip() + ELLIPSIS
    return cleaned


def _join(*parts: str | None, separator: str = " ") -> str:
    return separator.join(part for part in map(_clean, parts) if part)


def _waiting(activity: SessionActivity) -> str:
    if activity.state != "waiting":
        return ""
    tool = _clean(activity.current_tool)
    if tool:
        return "Waiting for permission: " + _join(tool, activity.current_target)
    reason = _clean(activity.waiting_reason)
    return f"Waiting for input: {reason}" if reason else "Waiting for input"


def _stage(activity: SessionActivity) -> str:
    stage = activity.stage
    if stage is None:
        return ""
    pipeline = _clean(activity.title)
    if activity.state in TERMINAL_STATES:
        return _join(pipeline or "Run", activity.state)
    name = _clean(stage.name)
    if not name:
        return ""
    counted = stage.index is not None and stage.total is not None
    position = f"({stage.index}/{stage.total})" if counted else ""
    return _join(_join(pipeline, name, separator=" · "), position)


def _tool(activity: SessionActivity) -> str:
    if activity.state != "working":
        return ""
    tool = _clean(activity.current_tool)
    return _join(tool, activity.current_target) if tool else ""


def _task(activity: SessionActivity) -> str:
    return _clean(activity.open_task) if activity.state == "working" else ""


def _title_prompt(activity: SessionActivity) -> str:
    title = _clean(activity.title)
    prompt = _clean(activity.last_prompt)
    return f"{title}: {prompt}" if title and prompt else prompt


def _title_reply(activity: SessionActivity) -> str:
    title = _clean(activity.title)
    reply = first_sentence(activity.last_reply)
    return f"{title}: {reply}" if title and reply else title or reply


_RULE_FUNCTIONS: Final = (
    ("waiting", _waiting),
    ("stage", _stage),
    ("tool", _tool),
    ("task", _task),
    ("title_prompt", _title_prompt),
    ("title_reply", _title_reply),
)


def session_one_liner(
    activity: SessionActivity, *, redact: Redactor | None = None
) -> tuple[str, str]:
    """``(text, rule)`` for a session: the one-liner and the rule that produced it (§4.6)."""
    for rule, build in _RULE_FUNCTIONS:
        text = build(activity)
        if text:
            return finalize(text, redact), rule
    return EMPTY, "none"
