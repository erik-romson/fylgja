"""Privacy filter (``PLAN-final-plan.md`` §4.4, A5; ``PLAN.md`` §11).

The ingest gate runs :meth:`PrivacyFilter.filter` on every record **before** it is queued for the
writer, so nothing below reaches SQLite, the live view or SSE in raw form:

* **Ignore lists.** A window, focus or context-item record whose bundle id is in
  ``ignore_bundle_ids``, or whose title matches one of ``ignore_title_patterns``, is dropped. In a
  snapshot only the matching items are dropped; a complete snapshot then closes them like any
  other missing entity.
* **Redaction.** ``redact_patterns`` are applied to titles and prompt text: window, focus and
  context titles (and the string values of a context item's ``data``), session names, every free
  text field of :class:`~fylgja.models.SessionActivity` and todo text.
* **Caps.** ``last_prompt`` is cut to 300 characters and ``last_reply`` to 800, after redaction
  (so a secret that straddles the cut is still redacted). The same holds for the ``prompt`` and
  ``said`` of a session's ``facts`` block, whose strings are all redacted.

:func:`minimize_hook_payload` is the A5 minimizer for raw hook payloads: it keeps only the
allowlisted normalized fields and drops tool input and response bodies.

Nothing here logs record content.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Final, cast

from pydantic import BaseModel

from fylgja.config import PrivacyConfig
from fylgja.models import (
    ContextItemPayload,
    Event,
    FocusPayload,
    HookEventPayload,
    Record,
    SessionActivity,
    SessionPayload,
    Snapshot,
    SnapshotItem,
    TodoPayload,
    WindowPayload,
)

__all__ = [
    "HOOK_FIELDS",
    "PROMPT_MAX_CHARS",
    "REDACTED",
    "REPLY_MAX_CHARS",
    "PrivacyFilter",
    "minimize_hook_payload",
    "truncate",
]

REDACTED: Final = "[redacted]"
PROMPT_MAX_CHARS: Final = 300
REPLY_MAX_CHARS: Final = 800

_FIELD_MAX_CHARS: Final = 1024
"""Cap on every other string the hook minimizer keeps (ids, paths, names)."""

_TEXT_FIELDS: Final[Mapping[type[BaseModel], tuple[str, ...]]] = {
    WindowPayload: ("title",),
    FocusPayload: ("title",),
    ContextItemPayload: ("title",),
    SessionPayload: ("name",),
    SessionActivity: (
        "title",
        "last_prompt",
        "last_reply",
        "open_task",
        "current_target",
        "waiting_reason",
    ),
    TodoPayload: ("text",),
    HookEventPayload: ("prompt",),
}
"""Free-text fields that are redacted, per payload model."""

_CAPS: Final[Mapping[tuple[type[BaseModel], str], int]] = {
    (SessionActivity, "last_prompt"): PROMPT_MAX_CHARS,
    (SessionActivity, "last_reply"): REPLY_MAX_CHARS,
    (HookEventPayload, "prompt"): PROMPT_MAX_CHARS,
}

_FACT_CAPS: Final[Mapping[str, int]] = {"prompt": PROMPT_MAX_CHARS, "said": REPLY_MAX_CHARS}
"""Caps on the facts block's ``prompt`` and ``said``, as for ``last_prompt`` and ``last_reply``."""

_IGNORABLE: Final = (WindowPayload, FocusPayload, ContextItemPayload)
"""Payloads that name an app and a window title, and so can match the ignore lists."""


def truncate(text: str, limit: int) -> str:
    """``text`` cut to at most ``limit`` characters."""
    return text if len(text) <= limit else text[:limit]


class PrivacyFilter:
    """Applies the ``[privacy]`` configuration to records."""

    def __init__(
        self,
        *,
        redact_patterns: Iterable[str] = (),
        ignore_bundle_ids: Iterable[str] = (),
        ignore_title_patterns: Iterable[str] = (),
        replacement: str = REDACTED,
    ) -> None:
        self._redact = tuple(re.compile(pattern) for pattern in redact_patterns)
        self._ignore_bundles = frozenset(ignore_bundle_ids)
        self._ignore_titles = tuple(re.compile(pattern) for pattern in ignore_title_patterns)
        self._replacement = replacement

    @classmethod
    def from_config(cls, config: PrivacyConfig) -> PrivacyFilter:
        return cls(
            redact_patterns=config.redact_patterns,
            ignore_bundle_ids=config.ignore_bundle_ids,
            ignore_title_patterns=config.ignore_title_patterns,
        )

    # -- primitives ---------------------------------------------------------------------------

    def redact(self, text: str) -> str:
        """Replace every match of every redaction pattern."""
        for pattern in self._redact:
            text = pattern.sub(self._replacement, text)
        return text

    def ignores_bundle(self, bundle_id: str | None) -> bool:
        return bundle_id is not None and bundle_id in self._ignore_bundles

    def ignores_title(self, title: str | None) -> bool:
        return title is not None and any(p.search(title) for p in self._ignore_titles)

    def minimize_hook_payload(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        """:func:`minimize_hook_payload` with this filter's redaction applied to the prompt."""
        return minimize_hook_payload(raw, redact=self.redact)

    # -- records ------------------------------------------------------------------------------

    def filter(self, record: Record) -> Record | None:
        """The record as it may be stored, or ``None`` when it must be dropped entirely."""
        if isinstance(record, Snapshot):
            return cast(Record, self._filter_snapshot(record))
        if isinstance(record, Event):
            payload = self._payload(record.payload)
            if payload is None:
                return None
            if payload is record.payload:
                return record
            return cast(Record, record.model_copy(update={"payload": payload}))
        raise TypeError(f"not a record: {type(record).__name__}")

    def _filter_snapshot(self, record: Snapshot[Any]) -> Snapshot[Any]:
        kept: list[SnapshotItem[Any]] = []
        changed = False
        for item in record.items:
            payload = self._payload(item.payload)
            if payload is None:
                changed = True
                continue
            if payload is not item.payload:
                item = item.model_copy(update={"payload": payload})
                changed = True
            kept.append(item)
        if not changed:
            return record
        return record.model_copy(update={"items": tuple(kept)})

    def _payload(self, payload: BaseModel) -> BaseModel | None:
        if isinstance(payload, _IGNORABLE) and (
            self.ignores_bundle(payload.app_bundle_id) or self.ignores_title(payload.title)
        ):
            return None
        kind = type(payload)
        update: dict[str, Any] = {}
        for name in _TEXT_FIELDS.get(kind, ()):
            value = getattr(payload, name)
            if not isinstance(value, str):
                continue
            cleaned = self.redact(value)
            cap = _CAPS.get((kind, name))
            if cap is not None:
                cleaned = truncate(cleaned, cap)
            if cleaned != value:
                update[name] = cleaned
        if isinstance(payload, SessionActivity) and payload.facts:
            facts = self._redact_facts(payload.facts)
            if facts != payload.facts:
                update["facts"] = facts
        if isinstance(payload, SessionActivity) and payload.report:
            report = [
                section.model_copy(
                    update={
                        "heading": self.redact(section.heading),
                        "text": self.redact(section.text),
                    }
                )
                for section in payload.report
            ]
            if report != payload.report:
                update["report"] = report
        if isinstance(payload, ContextItemPayload) and payload.data:
            data = self._redact_json(payload.data)
            if data != payload.data:
                update["data"] = data
        if not update:
            return payload
        return payload.model_copy(update=update)

    def _redact_facts(self, facts: Mapping[str, Any]) -> dict[str, Any]:
        """Redact every string of a facts block, then cut ``prompt`` and ``said`` to their caps."""
        redacted: dict[str, Any] = self._redact_json(dict(facts))
        for name, cap in _FACT_CAPS.items():
            value = redacted.get(name)
            if isinstance(value, str):
                redacted[name] = truncate(value, cap)
        return redacted

    def _redact_json(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, dict):
            return {key: self._redact_json(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._redact_json(item) for item in value]
        return value


# --------------------------------------------------------------------------------------------
# A5: hook payload minimizer
# --------------------------------------------------------------------------------------------

HOOK_FIELDS: Final = (
    "session_id",
    "event",
    "tool_name",
    "notification_type",
    "reason",
    "cwd",
    "permission_mode",
    "edited_paths",
    "prompt",
)
"""The only keys :func:`minimize_hook_payload` can return (A5)."""

_HOOK_SOURCES: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("session_id", ("session_id",)),
    ("event", ("hook_event_name", "event")),
    ("tool_name", ("tool_name",)),
    ("notification_type", ("notification_type",)),
    ("reason", ("reason",)),
    ("cwd", ("cwd",)),
    ("permission_mode", ("permission_mode",)),
)

_EDIT_TOOLS: Final = frozenset({"Edit", "MultiEdit", "Write", "NotebookEdit"})
_EDIT_PATH_KEYS: Final = ("file_path", "notebook_path")


def minimize_hook_payload(
    raw: Mapping[str, Any], *, redact: Callable[[str], str] | None = None
) -> dict[str, Any]:
    """Reduce a raw hook payload to the A5 allowlist.

    Kept: session id, event name, tool name, notification type, reason, cwd, permission mode,
    the file paths an editing tool touched, and the prompt (redacted with ``redact`` when given,
    then cut to 300 characters). Everything else, including tool input and response bodies, is
    dropped. Non-string values of kept fields are dropped too.
    """
    minimized: dict[str, Any] = {}
    for target, keys in _HOOK_SOURCES:
        for key in keys:
            value = raw.get(key)
            if isinstance(value, str) and value:
                minimized[target] = truncate(value, _FIELD_MAX_CHARS)
                break
    tool_input = raw.get("tool_input")
    if raw.get("tool_name") in _EDIT_TOOLS and isinstance(tool_input, Mapping):
        edited = [
            truncate(value, _FIELD_MAX_CHARS)
            for key in _EDIT_PATH_KEYS
            if isinstance(value := tool_input.get(key), str) and value
        ]
        if edited:
            minimized["edited_paths"] = edited
    prompt = raw.get("prompt")
    if isinstance(prompt, str):
        cleaned = prompt if redact is None else redact(prompt)
        minimized["prompt"] = truncate(cleaned, PROMPT_MAX_CHARS)
    return minimized
