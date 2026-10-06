"""Token sums of a Claude transcript: a pure port of Lidskjalv's ``_COSTS_JQ`` (WP5.4, §4.7).

The jq program sums the usage of every assistant line::

    [inputs | fromjson? | select(type == "object" and .type == "assistant"
                                 and .message != null and .message.usage != null)
     | .message.usage]
    | {input:       (map(.input_tokens // 0) | add // 0),
       output:      (map(.output_tokens // 0) | add // 0),
       cache_read:  (map(.cache_read_input_tokens // 0) | add // 0),
       cache_write: (map(.cache_creation_input_tokens // 0) | add // 0)}

:class:`TokenAccumulator` folds decoded lines (oldest first) into the same four sums, one line at a
time, so the transcript tail never rereads a file; :func:`tokens_of` is the one-shot form. Both
give exactly the jq program's output for every transcript the program can read
(``tests/fixtures/lidskjalv/costs/*.json`` are its recorded output).

Like the jq program, nothing is deduplicated: a sidechain line counts, and so does a line that
repeats an earlier message id. Where jq would abort on a malformed value (a string where it expects
a number, a ``usage`` that is not an object), the port ignores that value instead of failing: a live
transcript is never trusted. ``null`` and ``false`` count as 0, as ``// 0`` does.

Nothing here does I/O or logging.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

__all__ = ["TokenAccumulator", "TokenSums", "tokens_of"]


@dataclass(frozen=True)
class TokenSums:
    """The four sums of ``_COSTS_JQ``."""

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "input": self.input,
            "output": self.output,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
        }

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_write


def _count(usage: Mapping[str, Any], name: str) -> int:
    """``.<name> // 0`` as a whole number; a value that is not a number counts as 0."""
    value = usage.get(name)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0
    if isinstance(value, float):
        return int(value) if math.isfinite(value) and value == int(value) else 0
    return value


class TokenAccumulator:
    """Folds transcript lines into the token sums. Feed lines oldest first."""

    def __init__(self) -> None:
        self._input = 0
        self._output = 0
        self._cache_read = 0
        self._cache_write = 0
        self.records = 0
        """Assistant lines with a usage object counted so far."""

    def feed(self, entry: Mapping[str, Any]) -> None:
        if entry.get("type") != "assistant":
            return
        message = entry.get("message")
        if not isinstance(message, Mapping):
            return
        usage = message.get("usage")
        if not isinstance(usage, Mapping):
            return
        self.records += 1
        self._input += _count(usage, "input_tokens")
        self._output += _count(usage, "output_tokens")
        self._cache_read += _count(usage, "cache_read_input_tokens")
        self._cache_write += _count(usage, "cache_creation_input_tokens")

    def feed_all(self, entries: Iterable[Mapping[str, Any]]) -> None:
        for entry in entries:
            self.feed(entry)

    def sums(self) -> TokenSums:
        return TokenSums(self._input, self._output, self._cache_read, self._cache_write)


def tokens_of(entries: Iterable[Mapping[str, Any]]) -> TokenSums:
    """The token sums of a whole transcript given as decoded lines, oldest first."""
    accumulator = TokenAccumulator()
    accumulator.feed_all(entries)
    return accumulator.sums()
