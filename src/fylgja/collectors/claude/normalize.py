"""Hook envelope to minimized core events (``PLAN-final-plan.md`` WP2.2, A5; pure).

The envelope is the ``fylgja.hook/1`` file ``fylgja hook`` writes (``docs/api/inbox-envelope.md``).
:func:`parse_envelope` checks its shape; :func:`normalize` turns it into one
:class:`~fylgja.models.HookEvent` that holds only the A5 allowlist:

* **kept:** session id, event name, tool name, notification type (read from the payload, because the
  hook matcher is ``""``), ``SessionEnd`` reason, cwd, permission mode, the file paths an editing
  tool touched, the prompt cut to 300 characters, Claude's own pid, and the allowlisted terminal ids
  of the environment (``CMUX_*``, iTerm2 and Terminal ids: step 13's cmux adapter uses them);
* **dropped:** tool input and response bodies, transcript paths and every other payload key. The
  environment is never copied wholesale, so a secret that is not on the allowlist cannot get here.

The prompt is redacted later, by the ingest gate's privacy filter, which also repeats the cap.

Nothing here does I/O, and every failure is an :class:`EnvelopeError` whose message names the rule
that failed and never quotes content.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, TypeGuard

from fylgja.models import HookEvent, HookEventPayload, compute_dedup_key
from fylgja.privacy import minimize_hook_payload

__all__ = [
    "ENVELOPE_SCHEMA",
    "TERMINAL_ID_VARIABLES",
    "Envelope",
    "EnvelopeError",
    "datetime_of_ns",
    "normalize",
    "parse_envelope",
    "payload_object",
    "session_id_ok",
]

ENVELOPE_SCHEMA: Final = "fylgja.hook/1"

TERMINAL_ID_VARIABLES: Final = (
    "CMUX_WORKSPACE_ID",
    "CMUX_SURFACE_ID",
    "CMUX_TAB_ID",
    "CMUX_PANEL_ID",
    "ITERM_SESSION_ID",
    "TERM_SESSION_ID",
    "TERM_PROGRAM",
)
"""The environment variables of an envelope that normalization keeps (A3, A5)."""

_ENVELOPE_ID: Final = re.compile(r"[0-9a-f-]{8,64}")
_SOURCE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_SESSION_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_EVENT_NAME: Final = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,63}")
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)


class EnvelopeError(ValueError):
    """The envelope is not usable. The message names the failed rule, never the content."""


@dataclass(frozen=True)
class Envelope:
    """A validated ``fylgja.hook/1`` envelope."""

    id: str
    received_at_ns: int
    source: str
    event_arg: str | None
    ppid: int
    env: Mapping[str, str]
    payload_text: str
    truncated: bool
    fylgja_version: str


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def parse_envelope(data: bytes | str) -> Envelope:
    """Parse and check an envelope file's content. Raises :class:`EnvelopeError`."""
    try:
        document = json.loads(data)
    except (ValueError, RecursionError) as exc:
        raise EnvelopeError("not JSON") from exc
    if not isinstance(document, dict):
        raise EnvelopeError("not a JSON object")
    if document.get("schema") != ENVELOPE_SCHEMA:
        raise EnvelopeError("unknown schema")
    envelope_id = document.get("id")
    if not isinstance(envelope_id, str) or _ENVELOPE_ID.fullmatch(envelope_id) is None:
        raise EnvelopeError("bad id")
    received = document.get("received_at_ns")
    if not _is_int(received) or received < 0:
        raise EnvelopeError("bad received_at_ns")
    source = document.get("source")
    if not isinstance(source, str) or _SOURCE.fullmatch(source) is None:
        raise EnvelopeError("bad source")
    event_arg = document.get("event_arg")
    if event_arg is not None and not isinstance(event_arg, str):
        raise EnvelopeError("bad event_arg")
    ppid = document.get("ppid")
    if not _is_int(ppid):
        raise EnvelopeError("bad ppid")
    env = document.get("env")
    if not isinstance(env, dict) or not all(
        isinstance(name, str) and isinstance(value, str) for name, value in env.items()
    ):
        raise EnvelopeError("bad env")
    payload_text = document.get("payload_text")
    if not isinstance(payload_text, str):
        raise EnvelopeError("bad payload_text")
    truncated = document.get("truncated")
    if not isinstance(truncated, bool):
        raise EnvelopeError("bad truncated")
    version = document.get("fylgja_version")
    return Envelope(
        id=envelope_id,
        received_at_ns=received,
        source=source,
        event_arg=event_arg,
        ppid=ppid,
        env=dict(env),
        payload_text=payload_text,
        truncated=truncated,
        fylgja_version=version if isinstance(version, str) else "",
    )


def datetime_of_ns(nanoseconds: int) -> datetime:
    """The UTC time of a nanosecond epoch stamp (integer arithmetic, no float rounding)."""
    try:
        return _EPOCH + timedelta(microseconds=nanoseconds // 1000)
    except OverflowError as exc:
        raise EnvelopeError("received_at_ns out of range") from exc


def session_id_ok(value: str) -> bool:
    return _SESSION_ID.fullmatch(value) is not None


def payload_object(envelope: Envelope) -> dict[str, Any]:
    """The payload as a JSON object; ``{}`` when it is invalid, a marker or not an object.

    A payload cut by the writer's caps (``{"_malformed_oversize": n}``) is still an object: it
    simply holds none of the allowlisted keys, and the session id then comes from the environment.
    """
    try:
        payload = json.loads(envelope.payload_text)
    except (ValueError, RecursionError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _terminal_ids(env: Mapping[str, str]) -> dict[str, str] | None:
    ids = {name: env[name] for name in TERMINAL_ID_VARIABLES if env.get(name)}
    return ids or None


def _pid(env: Mapping[str, str]) -> int | None:
    value = env.get("CLAUDE_PID", "")
    return int(value) if value.isascii() and value.isdigit() and len(value) <= 9 else None


def normalize(envelope: Envelope, *, source: str = "claude", kind: str = "claude") -> HookEvent:
    """The minimized core event of one envelope. Raises :class:`EnvelopeError`.

    The session id is the payload's ``session_id``, or ``CLAUDE_CODE_SESSION_ID`` when the payload
    has none (a marker or unparseable payload); the event is the payload's ``hook_event_name``, or
    the command-line event. An envelope that names no session or no event cannot be attributed.
    """
    if envelope.source != source:
        raise EnvelopeError("source does not match the inbox")
    kept = minimize_hook_payload(payload_object(envelope))
    session_id = kept.pop("session_id", None) or envelope.env.get("CLAUDE_CODE_SESSION_ID")
    event = kept.pop("event", None) or envelope.event_arg
    if not session_id or not session_id_ok(session_id):
        raise EnvelopeError("no usable session id")
    if not event or _EVENT_NAME.fullmatch(event) is None:
        raise EnvelopeError("no usable event name")
    observed_at = datetime_of_ns(envelope.received_at_ns)
    entity_id = f"{kind}:{session_id}"
    try:
        payload = HookEventPayload(
            session_id=session_id,
            event=event,
            pid=_pid(envelope.env),
            ids=_terminal_ids(envelope.env),
            envelope_id=envelope.id,
            **kept,
        )
        return HookEvent(
            source=source,
            entity_id=entity_id,
            observed_at=observed_at,
            payload=payload,
            dedup_key=compute_dedup_key(
                source, "hook_event", entity_id, observed_at, {"envelope": envelope.id}
            ),
        )
    except ValueError as exc:  # pydantic's ValidationError
        raise EnvelopeError("fields do not fit the event model") from exc
