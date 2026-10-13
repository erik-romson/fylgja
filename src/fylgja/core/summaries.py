"""The summary service: session, workspace, day and folder summaries by a local model (WP8.1, A8).

``PLAN-final-plan.md`` §5 WP8.1, §4.2 ("Summary responses") and §4.4 (model calls); the reference is
Lidskjalv's ``_story_one``: its prompt, its facts block, its TTL and its "skip when the story is
newer than the transcript" gate. Off by default (``[summaries] enabled = false``): while it is off
nothing is queued and no model is ever called.

**Model calls run only here**, in the daemon: never in the CLI and never on a hook path. A call is
``claude -p --model haiku --strict-mcp-config`` with the prompt on stdin (so it never shows in a
process listing), run in the summarizer cwd (:func:`fylgja.paths.summarizer_dir`, created on
demand, always on the Claude collector's ``ignore_cwd`` list) with ``FYLGJA_SUMMARY=1`` and
``LIDSKJALV_TRACK_SUMMARY=1`` set, so Fylgja's hook and Lidskjalv's exit at once and the summarizer
session never becomes a user session. The runner is a seam (:class:`SummaryRunner`):
:class:`ClaudeRunner` shells out, tests pass a fake.

**The queue** is keyed by ``(scope_kind, scope_ref)``. A request for a key whose job is queued or
running joins that job. At most ``[summaries] max_concurrent`` jobs call the model at once; the
others wait for a slot and are ``pending`` meanwhile. A call that outlives :data:`RUN_TIMEOUT` (60 s
on the daemon's clock) or exits non-zero fails the job with an ``error_kind``.

**Inputs** (:class:`SummaryInputs`) are taken when a job is requested, from the ``/now`` picture
(:mod:`fylgja.api.summary_inputs` builds them): for a session the facts block and its one-liner,
producing ARC and NOW as ``_story_one`` does; for a workspace its label, one-liner, projects,
sessions, top context items and open todos, producing one short paragraph. They are normalized
(empty values dropped) and hashed into ``input_digest``.

**The cache** holds the latest summary of each key in memory, loaded from ``summaries`` at start and
written back through the single writer after every job. A request

* whose digest equals the cached row's gets the row as ``fresh``, with no model call;
* for a session whose cached summary is as new as its transcript's last line or newer gets it as
  ``fresh`` too: that session is never regenerated, whatever else changed (its state, say);
* with a changed digest starts a job, but at most once per ``ttl_seconds`` per key: inside the TTL
  (counted from the last attempt) the cached row is answered as ``stale`` (or ``failed`` when the
  last attempt failed);
* for a **past day** (``settled`` inputs, WP8.2) is judged by the digest alone: unchanged, it is
  ``fresh`` for good, and changed (a late import) it is regenerated at once, with no TTL. Only a
  failed attempt waits out the TTL.

**The day scope** (WP8.2): ``ref`` is a local date ``YYYY-MM-DD`` (:func:`parse_day`); its inputs
(:mod:`fylgja.api.summary_inputs`) are the day's ``/history`` totals and ordered spans, the
sessions active that day with their one-liners and the todos created or finished that day, and
the output is one paragraph (``kind = text``).

**The folder scope** (``docs/decisions/0053``): ``ref`` is the id of a navigation item (16 hex
digits, :func:`fylgja.core.items.item_id`), a git-root folder or an app. Its inputs
(:mod:`fylgja.api.summary_inputs`) are the item's name, tickets, branch, PR state and ``dirty``,
its places (label and title), its sessions with their one-liners and cached ARC, and its open
todos, with no seconds and no times, so the digest moves only when the content does. The output is
one paragraph (``kind = text``). A folder summary is made only on ``POST``; a read only shows the
cached one.

``generated_at`` is the instant a summary describes: when its job took its inputs. A failed attempt
keeps the previous text and records its ``error_kind``. Model output is redacted with the privacy
filter and cut to :data:`MAX_CHARS` before it is stored, and it is never logged.

**When a job finishes** every listener is told (the API publishes ``summary.ready`` with the scope,
the ref and the new state on SSE) and ``on_change`` runs (the live view's ``touch``: ``/now``
changed).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from functools import cached_property
from pathlib import Path
from typing import Any, Final, Literal, Protocol, cast, runtime_checkable

from fylgja import paths
from fylgja.collectors.base import Clock
from fylgja.collectors.supervisor import terminate_process_group
from fylgja.config import SummariesConfig
from fylgja.core.writer import Tx, Writer, WriterClosed
from fylgja.privacy import PrivacyFilter
from fylgja.store import db, queries
from fylgja.store.rows import SummaryRow

__all__ = [
    "CLAUDE_ARGS",
    "GUARD_ENV",
    "MAX_CHARS",
    "MODEL",
    "PARTS",
    "RUN_TIMEOUT",
    "SCOPES",
    "CachedSummary",
    "ClaudeRunner",
    "RunOutput",
    "ScopeKind",
    "SummariesDisabled",
    "SummaryAnswer",
    "SummaryInputs",
    "SummaryRunner",
    "SummaryService",
    "SummaryState",
    "claude_command",
    "input_digest",
    "normalize",
    "day_bounds",
    "day_prompt",
    "folder_prompt",
    "parse_day",
    "parse_story",
    "parse_text",
    "session_prompt",
    "summary_environment",
    "workspace_prompt",
]

logger = logging.getLogger("fylgja.summaries")

ScopeKind = Literal["session", "workspace", "day", "folder"]
SCOPES: Final[tuple[ScopeKind, ...]] = ("session", "workspace", "day", "folder")
PARTS: Final[Mapping[str, tuple[str, ...]]] = {
    "session": ("arc", "now"),
    "workspace": ("text",),
    "day": ("text",),
    "folder": ("text",),
}
"""The ``summaries.kind`` rows a summary of each scope consists of."""

MODEL: Final = "haiku"
CLAUDE_ARGS: Final = ("-p", "--model", MODEL, "--strict-mcp-config")
"""What follows the ``claude`` command; the prompt goes to stdin."""
RUN_TIMEOUT: Final = 60.0
"""Seconds one model call may take."""
MAX_CHARS: Final = 600
"""The longest ARC, NOW or paragraph that is kept (Lidskjalv's ``LIDSKJALV_STORY_MAX``)."""
GUARD_ENV: Final[Mapping[str, str]] = {"FYLGJA_SUMMARY": "1", "LIDSKJALV_TRACK_SUMMARY": "1"}
"""Set for every model call: the hooks of the spawned session exit at once."""
_ENV_ALLOWED: Final = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TMPDIR",
        "TERM",
        "LANG",
        "TZ",
        paths.CONFIG_DIR_ENV,
        paths.DATA_DIR_ENV,
        paths.STATE_DIR_ENV,
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "CLAUDE_CONFIG_DIR",
        "ANTHROPIC_API_KEY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "NODE_EXTRA_CA_CERTS",
    }
)
_ENV_ALLOWED_PREFIXES: Final = ("LC_",)
"""What a model call inherits from the daemon. An allowlist, so the messaging token, the cmux
capability and the parent-session markers (``CLAUDECODE``, ``CLAUDE_CODE_ENTRYPOINT``) of a
daemon started from a terminal inside a Claude session never reach the child. The Fylgja
directory overrides do, so the child's hooks see the daemon's paths. Proxy variables are matched
case-insensitively."""

Key = tuple[str, str]
_NEVER: Final = datetime.min.replace(tzinfo=UTC)


class SummaryState(StrEnum):
    """The state of a summary (§4.2 "Summary responses"); the same values as ``/now``'s."""

    FRESH = "fresh"
    STALE = "stale"
    PENDING = "pending"
    MISSING = "missing"
    FAILED = "failed"
    DISABLED = "disabled"


class SummariesDisabled(Exception):
    """``[summaries] enabled = false``: nothing is generated."""


# --------------------------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------------------------


def normalize(value: Any) -> Any:
    """``value`` with ``None``, empty strings and empty lists and objects dropped (recursively)
    and strings stripped: two pictures that differ only in absent values have the same digest."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        found = {str(k): normalize(v) for k, v in value.items()}
        return {k: v for k, v in sorted(found.items()) if not _empty(v)}
    if isinstance(value, list | tuple):
        items = [normalize(v) for v in value]
        return [v for v in items if not _empty(v)]
    return value


def _empty(value: Any) -> bool:
    return value is None or (isinstance(value, str | list | dict) and not value)


def input_digest(scope: str, data: Mapping[str, Any]) -> str:
    """The SHA-256 of the canonical JSON of ``scope`` and the normalized inputs."""
    canonical = json.dumps(
        {"scope": scope, "inputs": data},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SummaryInputs:
    """What one summary is made from. Build it with :meth:`create`."""

    scope: ScopeKind
    ref: str
    data: Mapping[str, Any]
    """The normalized inputs: the prompt is built from them and the digest covers them."""
    activity_at: datetime | None = None
    """When the scope last changed at its source (a session: its transcript's last line). A
    cached summary as of this instant or later is never regenerated."""
    settled: bool = False
    """The scope can no longer change by itself (a day that is over): a cached summary is judged
    by its digest alone and a changed digest is regenerated without waiting out the TTL."""

    @classmethod
    def create(
        cls,
        scope: ScopeKind,
        ref: str,
        data: Mapping[str, Any],
        *,
        activity_at: datetime | None = None,
        settled: bool = False,
    ) -> SummaryInputs:
        return cls(scope, ref, cast(dict[str, Any], normalize(data)), activity_at, settled)

    @property
    def key(self) -> Key:
        return (self.scope, self.ref)

    @cached_property
    def digest(self) -> str:
        return input_digest(self.scope, self.data)


# --------------------------------------------------------------------------------------------
# Prompts (pure)
# --------------------------------------------------------------------------------------------

_SESSION_INTRO: Final = (
    "Someone keeps many agent sessions open at once and is looking at a panel that shows one\n"
    "card per session. Write the two summary lines for this one."
)
_SESSION_REPLY: Final = """Reply with exactly these two lines and nothing else:

ARC: one sentence for what this whole session has been about, start to finish.
NOW: two short sentences for what it is working on at this moment, and where it
has got to.

The ARC line covers the session end to end, so it must not be a restatement of
the newest thing -- if the session began somewhere else, say so. Present tense.
Be concrete: name the thing being changed rather than saying "the code". No
preamble, no markdown, no bullet points, no quotes."""

_SESSION_FACTS: Final = (
    ("agent", "Agent"),
    ("project_path", "Project"),
    ("title", "Conversation title"),
    ("branch", "Branch"),
    ("state", "State"),
    ("one_liner", "What it is doing now"),
    ("stage", "Pipeline stage"),
    ("turns", "Turns so far"),
    ("tools", "Tools used"),
    ("files", "Files edited"),
    ("open_tasks", "Still open"),
    ("prompt", "Their last message"),
    ("said", "Your last reply"),
)
_JOINERS: Final = {"tools": " ", "files": ", ", "open_tasks": "; "}

_WORKSPACE_REPLY: Final = """Reply with one short paragraph of two or three sentences and nothing
else: what this workspace is for and what is going on in it right now. Present
tense. Be concrete: name the projects and the work rather than saying "the code".
No preamble, no markdown, no bullet points, no quotes."""


def _line(label: str, value: Any, joiner: str = ", ") -> str:
    shown = joiner.join(str(v) for v in value) if isinstance(value, list) else str(value)
    return f"{label}: {shown}"


def session_prompt(
    data: Mapping[str, Any], previous_arc: str | None = None, previous_now: str | None = None
) -> str:
    """The ``_story_one`` prompt over a session's normalized inputs (see
    :mod:`fylgja.api.summary_inputs`), with the previous ARC and NOW as context."""
    facts = [
        _line(label, data[name], _JOINERS.get(name, ", "))
        for name, label in _SESSION_FACTS
        if name in data
    ]
    parts = [_SESSION_INTRO, "", *facts]
    asked = [str(prompt) for prompt in data.get("asked", [])]
    if asked:
        parts.append("Everything they have asked in this session, oldest first:")
        parts.extend(f"  {number}. {prompt}" for number, prompt in enumerate(asked, start=1))
    parts += [
        "",
        f"Previous ARC: {previous_arc or '(none yet)'}",
        f"Previous NOW: {previous_now or '(none yet)'}",
        "",
        _SESSION_REPLY,
    ]
    return "\n".join(parts)


def workspace_prompt(data: Mapping[str, Any]) -> str:
    """The prompt for one short paragraph about a workspace."""
    label = str(data.get("label") or data.get("name") or "")
    parts = [
        "Someone works across several workspaces of a tiling window manager, one per piece of\n"
        "work, and is looking at a panel that shows one card per workspace. Write the summary\n"
        f'for the workspace "{label}".',
        "",
    ]
    for name, title in (("name", "Workspace"), ("label", "Label"), ("one_liner", "At a glance")):
        if name in data:
            parts.append(f"{title}: {data[name]}")
    if data.get("projects"):
        parts.append(_line("Projects", data["projects"]))
    sessions = [s for s in data.get("sessions", []) if isinstance(s, Mapping)]
    if sessions:
        parts.append("Agent sessions here:")
        for item in sessions:
            head = ", ".join(str(item[k]) for k in ("agent", "state") if k in item)
            parts.append(f"  - {head}: {item.get('one_liner') or item.get('title') or '—'}")
    if data.get("context"):
        parts.append("Open in its windows:")
        parts.extend(f"  - {entry}" for entry in data["context"])
    todos = [t for t in data.get("todos", []) if isinstance(t, Mapping)]
    if todos:
        parts.append("Open todos:")
        parts.extend(f"  - [{t.get('status', 'open')}] {t.get('text', '')}" for t in todos)
    parts += ["", _WORKSPACE_REPLY]
    return "\n".join(parts)


_DAY_REF: Final = re.compile(r"\d{4}-\d{2}-\d{2}")


def parse_day(ref: str) -> date:
    """The local date of a ``YYYY-MM-DD`` ref. Raises :class:`ValueError` for anything else."""
    if _DAY_REF.fullmatch(ref) is None:
        raise ValueError("a day is written YYYY-MM-DD")
    try:
        return date.fromisoformat(ref)
    except ValueError:
        raise ValueError("a day is written YYYY-MM-DD") from None


def day_bounds(day: date) -> tuple[datetime, datetime]:
    """The instants a local day starts and ends (UTC); the day is 23 or 25 hours long when the
    clocks change."""
    start = datetime(day.year, day.month, day.day).astimezone()  # noqa: DTZ001 - local midnight
    following = day + timedelta(days=1)
    stop = datetime(following.year, following.month, following.day).astimezone()  # noqa: DTZ001
    return start.astimezone(UTC), stop.astimezone(UTC)


_DAY_REPLY: Final = """Reply with one short paragraph of three to five sentences and nothing
else: tell the story of the day in the order it happened, what the attention went to
and what got done. Past tense. Be concrete: name the projects and the work rather
than saying "the code", and say so plainly when the day was quiet. No preamble, no
markdown, no bullet points, no quotes."""


def _minutes(seconds: Any) -> str:
    total = int(float(seconds)) // 60
    return f"{total // 60} h {total % 60:02d} min" if total >= 60 else f"{total} min"


def _totals(title: str, rows: Any, key: str) -> list[str]:
    found = [r for r in rows or () if isinstance(r, Mapping)]
    if not found:
        return []
    return [
        f"{title}:",
        *(f"  - {r.get(key) or '(unknown)'}: {_minutes(r.get('seconds', 0))}" for r in found),
    ]


def _span_line(span: Mapping[str, Any]) -> str:
    where = ", ".join(str(span[k]) for k in ("workspace", "app", "project_name") if span.get(k))
    title = f" — {span['title']}" if span.get("title") else ""
    length = f" ({span['minutes']} min)" if span.get("minutes") else ""
    return (
        f"  - {span.get('start', '?')}–{span.get('end', '?')}{length} {where or '(unknown)'}{title}"
    )


def day_prompt(data: Mapping[str, Any]) -> str:
    """The prompt for one paragraph that tells the story of a day, over its normalized inputs
    (see :mod:`fylgja.api.summary_inputs`)."""
    parts = [
        "Someone works across several workspaces of a tiling window manager and runs agent\n"
        "sessions beside it. Below is what was recorded of one day: where the attention went,\n"
        "which agent sessions were active and which todos were added or finished. Write the\n"
        f"story of {data.get('date', 'the day')}.",
        "",
    ]
    if "total_seconds" in data:
        parts.append(f"Time at the screen: {_minutes(data['total_seconds'])}")
    parts += _totals("Per workspace", data.get("workspaces"), "workspace")
    parts += _totals("Per project", data.get("projects"), "project_name")
    parts += _totals("Per app", data.get("apps"), "app")
    if data.get("worked_on"):
        parts.append(_line("Projects Claude was prompted in", data["worked_on"]))
    spans = [s for s in data.get("spans", []) if isinstance(s, Mapping)]
    if spans:
        parts.append("What had the focus, in order:")
        parts.extend(_span_line(s) for s in spans)
        if data.get("spans_omitted"):
            parts.append(f"  (and {data['spans_omitted']} more)")
    sessions = [s for s in data.get("sessions", []) if isinstance(s, Mapping)]
    if sessions:
        parts.append("Agent sessions active that day:")
        for item in sessions:
            head = ", ".join(str(item[k]) for k in ("agent", "project_name") if k in item)
            parts.append(f"  - {head}: {item.get('one_liner') or item.get('title') or '—'}")
    for title, name in (("Todos added", "todos_added"), ("Todos finished", "todos_done")):
        todos = [t for t in data.get(name, []) if isinstance(t, Mapping)]
        if todos:
            parts.append(f"{title}:")
            parts.extend(f"  - {t.get('text', '')}" for t in todos)
    parts += ["", _DAY_REPLY]
    return "\n".join(parts)


_FOLDER_REPLY: Final = """Reply with one short paragraph of at most three sentences and
nothing else: what was being done here and where it stands now. Present tense. Be
concrete: name the work rather than saying "the code". Do not repeat window, session
or todo titles word for word; a ticket key may be named as it is. No preamble, no
markdown, no bullet points, no quotes."""

_FOLDER_FACTS: Final = (
    ("name", "Name"),
    ("group", "Group"),
    ("tickets", "Tickets"),
    ("branch", "Branch"),
    ("pr", "Pull request"),
)


def folder_prompt(data: Mapping[str, Any]) -> str:
    """The prompt for one paragraph about a navigation item, a folder or an app, over its
    normalized inputs (see :mod:`fylgja.api.summary_inputs`)."""
    what = "app" if data.get("kind") == "app" else "folder"
    parts = [
        "Someone works in many folders (git checkouts and worktrees) and apps at once and is\n"
        "looking at a list that shows one row per folder or app. Write the summary for the\n"
        f'{what} "{data.get("name", "")}".',
        "",
    ]
    parts.extend(_line(label, data[name]) for name, label in _FOLDER_FACTS if name in data)
    if data.get("dirty"):
        parts.append("Uncommitted changes: yes")
    places = [p for p in data.get("places", []) if isinstance(p, Mapping)]
    if places:
        parts.append("Its windows:")
        parts.extend(f"  - {p.get('label', 'window')}: {p.get('title') or '—'}" for p in places)
    sessions = [s for s in data.get("sessions", []) if isinstance(s, Mapping)]
    if sessions:
        parts.append("Agent sessions here:")
        for item in sessions:
            parts.append(f"  - {item.get('agent', 'agent')}: {item.get('one_liner') or '—'}")
            if item.get("title"):
                parts.append(f"    Conversation title: {item['title']}")
            if item.get("arc"):
                parts.append(f"    The session so far: {item['arc']}")
    todos = [t for t in data.get("todos", []) if isinstance(t, Mapping)]
    if todos:
        parts.append("Open todos:")
        parts.extend(f"  - [{t.get('status', 'open')}] {t.get('text', '')}" for t in todos)
    parts += ["", _FOLDER_REPLY]
    return "\n".join(parts)


def _day_prompt(inputs: SummaryInputs, previous: CachedSummary | None) -> str:
    return day_prompt(inputs.data)


def _folder_prompt(inputs: SummaryInputs, previous: CachedSummary | None) -> str:
    return folder_prompt(inputs.data)


def _session_prompt(inputs: SummaryInputs, previous: CachedSummary | None) -> str:
    if previous is None:
        return session_prompt(inputs.data)
    return session_prompt(inputs.data, previous.arc, previous.now)


def _workspace_prompt(inputs: SummaryInputs, previous: CachedSummary | None) -> str:
    return workspace_prompt(inputs.data)


_PROMPTS: Final[Mapping[str, Callable[[SummaryInputs, CachedSummary | None], str]]] = {
    "session": _session_prompt,
    "workspace": _workspace_prompt,
    "day": _day_prompt,
    "folder": _folder_prompt,
}
"""The scopes the service can summarize, and their prompts."""


# --------------------------------------------------------------------------------------------
# Parsing the reply (pure)
# --------------------------------------------------------------------------------------------

_SPACES: Final = re.compile(r"\s+")


def _clean(text: str) -> str:
    return _SPACES.sub(" ", text).strip(" *")[:MAX_CHARS].strip()


def _field(label: str, text: str) -> str | None:
    """The text of the first line labelled ``label``; ``None`` when no line is."""
    pattern = re.compile(rf"^\s*[*_]*{label}[*_]*\s*:\s*(.*)$")
    for line in text.split("\n"):
        found = pattern.match(line)
        if found:
            return _clean(found.group(1))
    return None


def parse_story(
    output: str, previous_arc: str | None = None, previous_now: str | None = None
) -> tuple[str | None, str | None]:
    """ARC and NOW from a model reply, as ``_story_one`` reads them.

    Asterisks go first (the reply often arrives as ``**ARC:**``), then each label is put back on
    its own line (the two lines often arrive as one). A reply with no label at all is taken as the
    NOW line. A missing or empty line keeps the previous one.
    """
    text = output.replace("\r", " ").replace("\n", " ").replace("*", "")
    text = text.replace(" ARC:", "\nARC:").replace(" NOW:", "\nNOW:").lstrip()
    arc = _field("ARC", text)
    now = _field("NOW", text)
    if arc is None and now is None:
        now = _clean(text)
    return (arc or previous_arc or None), (now or previous_now or None)


def parse_text(output: str) -> str | None:
    """One paragraph from a model reply: whitespace collapsed, asterisks dropped, capped."""
    return _clean(output.replace("*", "")) or None


# --------------------------------------------------------------------------------------------
# The runner
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RunOutput:
    returncode: int
    stdout: str


@runtime_checkable
class SummaryRunner(Protocol):
    """Runs one model call to completion.

    A missing ``claude`` raises :class:`FileNotFoundError`; a call that outlives ``timeout``
    raises :class:`TimeoutError` and leaves nothing running. A non-zero exit is a result.
    """

    async def run(
        self,
        prompt: str,
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float,  # noqa: ASYNC109 - mirrors subprocess timeouts
    ) -> RunOutput: ...


def claude_command(
    home: Path | None = None, which: Callable[[str], str | None] = shutil.which
) -> str:
    """The ``claude`` CLI: ``~/.local/bin/claude`` (where the native installer links it) when it
    is there, else the one on ``PATH``. launchd's ``PATH`` is minimal, so the first matters."""
    native = (Path.home() if home is None else home) / ".local" / "bin" / "claude"
    if os.access(native, os.X_OK):
        return str(native)
    found = which("claude")
    if found is None:
        raise FileNotFoundError("claude")
    return found


def _env_allowed(name: str) -> bool:
    return name.upper() in _ENV_ALLOWED or name.startswith(_ENV_ALLOWED_PREFIXES)


def summary_environment(base: Mapping[str, str]) -> dict[str, str]:
    """The environment of a model call: the allowlisted part of ``base``, plus :data:`GUARD_ENV`."""
    env = {name: value for name, value in base.items() if _env_allowed(name)}
    env.update(GUARD_ENV)
    return env


class ClaudeRunner:
    """The real runner: ``<command> -p --model haiku --strict-mcp-config``, the prompt on stdin,
    in its own process group, which is killed on timeout or cancellation. ``command`` defaults to
    :func:`claude_command`, looked up at each call."""

    def __init__(self, command: Sequence[str] | None = None) -> None:
        self._command = None if command is None else tuple(command)

    def argv(self) -> list[str]:
        prefix = self._command if self._command is not None else (claude_command(),)
        return [*prefix, *CLAUDE_ARGS]

    async def run(
        self,
        prompt: str,
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float,  # noqa: ASYNC109 - mirrors subprocess timeouts
    ) -> RunOutput:
        process = await asyncio.create_subprocess_exec(
            *self.argv(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=cwd,
            env=dict(env),
            start_new_session=True,
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(prompt.encode("utf-8")), timeout)
        except BaseException:  # timeout or cancellation: leave nothing running
            await terminate_process_group(process)
            raise
        code = -1 if process.returncode is None else process.returncode
        return RunOutput(code, stdout.decode("utf-8", "replace"))


# --------------------------------------------------------------------------------------------
# The cache and the answers
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CachedSummary:
    """The latest summary of one key, as stored."""

    arc: str | None = None
    now: str | None = None
    text: str | None = None
    input_digest: str | None = None
    model: str | None = None
    generated_at: datetime | None = None
    error_kind: str | None = None
    """Set when the latest attempt failed (the text is then the previous one, if any)."""

    @property
    def has_text(self) -> bool:
        return bool(self.arc or self.now or self.text)

    def part(self, kind: str) -> str | None:
        return cast(str | None, getattr(self, kind))

    @classmethod
    def from_rows(cls, rows: Sequence[SummaryRow]) -> CachedSummary:
        parts = {row.kind: row.text for row in rows if row.kind in ("arc", "now", "text")}
        newest = max(rows, key=lambda r: r.generated_at or _NEVER)
        return cls(
            arc=parts.get("arc"),
            now=parts.get("now"),
            text=parts.get("text"),
            input_digest=newest.input_digest,
            model=newest.model,
            generated_at=newest.generated_at,
            error_kind=next((r.error_kind for r in rows if r.error_kind), None),
        )


@dataclass(frozen=True)
class SummaryAnswer:
    """What ``GET`` and ``POST /summaries/{scope}/{ref}`` answer."""

    scope: ScopeKind
    ref: str
    state: SummaryState
    arc: str | None = None
    now: str | None = None
    text: str | None = None
    generated_at: datetime | None = None
    input_digest: str | None = None
    error_kind: str | None = None

    @property
    def has_text(self) -> bool:
        return bool(self.arc or self.now or self.text)


def _answer(
    scope: ScopeKind, ref: str, state: SummaryState, cached: CachedSummary | None
) -> SummaryAnswer:
    if cached is None:
        return SummaryAnswer(scope, ref, state)
    return SummaryAnswer(
        scope,
        ref,
        state,
        arc=cached.arc,
        now=cached.now,
        text=cached.text,
        generated_at=cached.generated_at,
        input_digest=cached.input_digest,
        error_kind=cached.error_kind,
    )


def _state_of(cached: CachedSummary | None, inputs: SummaryInputs | None) -> SummaryState:
    """The state of a cached summary against the current inputs (no job running)."""
    if cached is None:
        return SummaryState.MISSING
    if not cached.has_text:
        return SummaryState.FAILED if cached.error_kind else SummaryState.MISSING
    if inputs is not None:
        if cached.input_digest == inputs.digest:
            return SummaryState.FRESH
        if (
            inputs.activity_at is not None
            and cached.generated_at is not None
            and cached.generated_at >= inputs.activity_at
        ):
            return SummaryState.FRESH  # newer than its transcript: never regenerated
    return SummaryState.FAILED if cached.error_kind else SummaryState.STALE


# --------------------------------------------------------------------------------------------
# The service
# --------------------------------------------------------------------------------------------

Listener = Callable[[str, str, str], object]
"""Called with ``(scope, ref, state)`` when a job finishes."""


@dataclass
class _Outcome:
    parts: dict[str, str | None] = field(default_factory=dict)
    error_kind: str | None = None


class SummaryService:
    """See the module docstring. Call every method on the event loop thread."""

    def __init__(
        self,
        writer: Writer,
        settings: SummariesConfig,
        *,
        clock: Clock,
        runner: SummaryRunner | None = None,
        privacy: PrivacyFilter | None = None,
        on_change: Callable[[], object] | None = None,
        timeout: float = RUN_TIMEOUT,
        cwd: Callable[[], Path] = paths.summarizer_dir,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._writer = writer
        self._settings = settings
        self._clock = clock
        self._runner: SummaryRunner = ClaudeRunner() if runner is None else runner
        self._privacy = privacy
        self._on_change = on_change
        self._timeout = timeout
        self._cwd = cwd
        self._environ = environ
        self._slots = asyncio.Semaphore(settings.max_concurrent)
        self._cache: dict[Key, CachedSummary] = {}
        self._jobs: dict[Key, asyncio.Task[None]] = {}
        self._attempts: dict[Key, datetime] = {}
        self._listeners: list[Listener] = []
        self._closed = False
        self.running = 0
        """Jobs calling the model right now."""
        self.peak_running = 0
        """The most jobs that called the model at once."""
        self.calls = 0
        """Model calls started."""

    # -- state ------------------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._settings.enabled and not self._closed

    @property
    def ttl(self) -> timedelta:
        return timedelta(seconds=self._settings.ttl_seconds)

    @property
    def max_concurrent(self) -> int:
        return self._settings.max_concurrent

    def pending(self, scope: str, ref: str) -> bool:
        """A job for this key is queued or running."""
        return (scope, ref) in self._jobs

    def cached(self, scope: str, ref: str) -> CachedSummary | None:
        return self._cache.get((scope, ref))

    def add_listener(self, listener: Listener) -> Callable[[], None]:
        """Call ``listener`` when a job finishes; returns the function that stops it."""
        self._listeners.append(listener)

        def remove() -> None:
            with contextlib.suppress(ValueError):
                self._listeners.remove(listener)

        return remove

    # -- start and stop ---------------------------------------------------------------------

    async def load(self) -> None:
        """Fill the cache from the store (run at start, after the writer opened it)."""
        rows = await asyncio.to_thread(self._read)
        grouped: dict[Key, list[SummaryRow]] = {}
        for row in rows:
            grouped.setdefault((row.scope_kind, row.scope_ref), []).append(row)
        self._cache = {key: CachedSummary.from_rows(found) for key, found in grouped.items()}

    def _read(self) -> list[SummaryRow]:
        connection = db.connect_reader(self._writer.database)
        try:
            return queries.load_summaries(connection)
        finally:
            connection.close()

    async def stop(self) -> None:
        """Cancel every queued and running job (a running model call is killed)."""
        self._closed = True
        jobs = list(self._jobs.values())
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        self._jobs.clear()

    async def drain(self) -> None:
        """Return once every job queued so far has finished."""
        while self._jobs:
            await asyncio.gather(*list(self._jobs.values()), return_exceptions=True)

    # -- answers ----------------------------------------------------------------------------

    def answer(
        self, scope: ScopeKind, ref: str, inputs: SummaryInputs | None
    ) -> SummaryAnswer | None:
        """The ``GET`` answer: the cached summary judged against ``inputs`` (``None`` when the
        scope has no current inputs, such as an ended session). ``None`` when nothing at all is
        known about the key. Never starts a job."""
        if not self.enabled:
            return SummaryAnswer(scope, ref, SummaryState.DISABLED)
        key = (scope, ref)
        cached = self._cache.get(key)
        if key in self._jobs:
            return _answer(scope, ref, SummaryState.PENDING, cached)
        if cached is None and inputs is None:
            return None
        return _answer(scope, ref, _state_of(cached, inputs), cached)

    def request(self, inputs: SummaryInputs) -> SummaryAnswer:
        """The ``POST`` answer: the cached summary when it is fresh (or stale or failed inside the
        TTL), else ``pending`` with a job queued for it (or joined, when one already is).

        Raises :class:`SummariesDisabled` while summaries are off and :class:`ValueError` for a
        scope the service cannot summarize yet.
        """
        if not self.enabled:
            raise SummariesDisabled
        if inputs.scope not in _PROMPTS:
            raise ValueError(f"summaries of scope {inputs.scope} are not available")
        key = inputs.key
        cached = self._cache.get(key)
        if key in self._jobs:  # join the job that is queued or running
            return _answer(inputs.scope, inputs.ref, SummaryState.PENDING, cached)
        state = _state_of(cached, inputs)
        held = (
            (SummaryState.FAILED,) if inputs.settled else (SummaryState.STALE, SummaryState.FAILED)
        )
        if state is SummaryState.FRESH or (state in held and self._inside_ttl(key, cached)):
            return _answer(inputs.scope, inputs.ref, state, cached)
        self._start(inputs, cached)
        return _answer(inputs.scope, inputs.ref, SummaryState.PENDING, cached)

    def _inside_ttl(self, key: Key, cached: CachedSummary | None) -> bool:
        last = self._attempts.get(key)
        if cached is not None and cached.generated_at is not None:
            last = cached.generated_at if last is None else max(last, cached.generated_at)
        return last is not None and self._clock.now() - last < self.ttl

    # -- jobs -------------------------------------------------------------------------------

    def _start(self, inputs: SummaryInputs, previous: CachedSummary | None) -> None:
        as_of = self._clock.now()
        self._attempts[inputs.key] = as_of
        self._jobs[inputs.key] = asyncio.create_task(
            self._job(inputs, previous, as_of), name=f"fylgja-summary-{inputs.scope}"
        )

    async def _job(
        self, inputs: SummaryInputs, previous: CachedSummary | None, as_of: datetime
    ) -> None:
        key = inputs.key
        try:
            async with self._slots:
                self.running += 1
                self.peak_running = max(self.peak_running, self.running)
                try:
                    outcome = await self._generate(inputs, previous)
                finally:
                    self.running -= 1
            cached = self._result(inputs, previous, outcome, as_of)
            await self._store(inputs, cached)
            self._cache[key] = cached
        except asyncio.CancelledError:
            self._jobs.pop(key, None)
            raise
        except Exception:
            logger.exception("summary job for %s failed", inputs.scope)
        self._jobs.pop(key, None)
        state = _state_of(self._cache.get(key), inputs)
        if state is SummaryState.FAILED:
            logger.warning(
                "summary of %s %s failed: %s",
                inputs.scope,
                inputs.ref,
                self._cache[key].error_kind,
            )
        self._notify(inputs.scope, inputs.ref, state)

    async def _generate(self, inputs: SummaryInputs, previous: CachedSummary | None) -> _Outcome:
        prompt = _PROMPTS[inputs.scope](inputs, previous)
        try:
            cwd = self._cwd()
            cwd.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError:
            return _Outcome(error_kind="cwd_unavailable")
        env = summary_environment(os.environ if self._environ is None else self._environ)
        self.calls += 1
        try:
            result = await self._race(
                self._runner.run(prompt, cwd=cwd, env=env, timeout=self._timeout)
            )
        except TimeoutError:
            return _Outcome(error_kind="timeout")
        except FileNotFoundError:
            return _Outcome(error_kind="not_installed")
        except OSError:
            return _Outcome(error_kind="spawn_failed")
        except Exception:
            logger.exception("the summary runner failed")
            return _Outcome(error_kind="runner_error")
        if result.returncode != 0:
            return _Outcome(error_kind="nonzero_exit")
        output = result.stdout if self._privacy is None else self._privacy.redact(result.stdout)
        if inputs.scope == "session":
            arc, now = parse_story(output)
            if not (arc or now):  # nothing new: the previous story stands, as a failure
                return _Outcome(error_kind="empty_output")
            if previous is not None:  # a line the reply lacks keeps the previous one
                arc, now = arc or previous.arc, now or previous.now
            return _Outcome({"arc": arc, "now": now})
        text = parse_text(output)
        if text is None:
            return _Outcome(error_kind="empty_output")
        return _Outcome({"text": text})

    async def _race(self, call: Awaitable[RunOutput]) -> RunOutput:
        """``call``, or :class:`TimeoutError` once the daemon's clock passes the timeout."""
        work = asyncio.ensure_future(call)
        timer = asyncio.ensure_future(self._clock.sleep(self._timeout))
        try:
            await asyncio.wait({work, timer}, return_when=asyncio.FIRST_COMPLETED)
            if not work.done():
                raise TimeoutError
            return work.result()
        finally:
            for task in (work, timer):
                if not task.done():
                    task.cancel()
            await asyncio.gather(work, timer, return_exceptions=True)

    def _result(
        self,
        inputs: SummaryInputs,
        previous: CachedSummary | None,
        outcome: _Outcome,
        as_of: datetime,
    ) -> CachedSummary:
        if outcome.error_kind is not None:
            kept = CachedSummary() if previous is None else previous
            return CachedSummary(
                arc=kept.arc,
                now=kept.now,
                text=kept.text,
                input_digest=kept.input_digest,
                model=kept.model,
                generated_at=kept.generated_at,
                error_kind=outcome.error_kind,
            )
        return CachedSummary(
            arc=outcome.parts.get("arc"),
            now=outcome.parts.get("now"),
            text=outcome.parts.get("text"),
            input_digest=inputs.digest,
            model=MODEL,
            generated_at=as_of,
        )

    async def _store(self, inputs: SummaryInputs, cached: CachedSummary) -> None:
        generated = None if cached.generated_at is None else db.to_db_time(cached.generated_at)
        rows = [
            {
                "scope_kind": inputs.scope,
                "scope_ref": inputs.ref,
                "kind": kind,
                "text": cached.part(kind),
                "input_digest": cached.input_digest,
                "model": cached.model,
                "generated_at": generated,
                "error_kind": cached.error_kind,
            }
            for kind in PARTS[inputs.scope]
        ]

        def job(tx: Tx) -> None:
            queries.put_summaries(tx.connection, rows)

        try:
            await self._writer.execute(job)
        except WriterClosed:
            logger.debug("the writer stopped before a summary was stored")

    def _notify(self, scope: str, ref: str, state: SummaryState) -> None:
        for listener in tuple(self._listeners):
            try:
                listener(scope, ref, state.value)
            except Exception:
                logger.exception("a summary listener failed")
        if self._on_change is not None:
            try:
                self._on_change()
            except Exception:
                logger.exception("the summary change hook failed")
