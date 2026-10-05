"""Logging rules of ``PLAN-final-plan.md`` §4.5: rotation, context fields and canaries."""

from __future__ import annotations

import logging
import os
import stat
from collections.abc import Iterator
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from fylgja import logsetup, paths

CANARY_PROMPT = "CANARY-PROMPT please delete the production database"
CANARY_TITLE = "CANARY-TITLE quarterly numbers"
CANARY_TOKEN = "sk-canary0123456789abcdefghij"  # noqa: S105 - not a real token
CANARY_ENV_TOKEN = "tok-canary-env-value-123"  # noqa: S105 - not a real token


@pytest.fixture
def handler() -> Iterator[logging.Handler]:
    installed = logsetup.setup_logging(level="DEBUG")
    try:
        yield installed
    finally:
        logsetup.teardown_logging()


def _log_text() -> str:
    for handler in logging.getLogger().handlers:
        handler.flush()
    return paths.log_file().read_text(encoding="utf-8")


def test_rotation_settings_and_location(handler: logging.Handler) -> None:
    assert isinstance(handler, RotatingFileHandler)
    assert handler.maxBytes == 5 * 1024 * 1024
    assert handler.backupCount == logsetup.LOG_BACKUP_COUNT == 5
    assert Path(handler.baseFilename) == paths.log_file()


def test_the_log_file_is_private(handler: logging.Handler) -> None:
    logging.getLogger("fylgja.test").info("hello")
    assert stat.S_IMODE(os.stat(paths.log_file()).st_mode) == 0o600


def test_a_line_carries_level_name_and_message(handler: logging.Handler) -> None:
    logging.getLogger("fylgja.thing").warning("disk is %s", "slow")
    assert "WARNING fylgja.thing: disk is slow" in _log_text()


def test_the_adapter_adds_collector_and_session_context(handler: logging.Handler) -> None:
    logsetup.get_logger("fylgja.ctx", collector="claude", session="claude:abc").info("polled")
    logsetup.get_logger("fylgja.ctx", collector="aerospace").info("snapshot")
    logsetup.get_logger("fylgja.ctx").info("plain")
    lines = _log_text().splitlines()
    assert lines[0].endswith("polled [collector=claude session=claude:abc]")
    assert lines[1].endswith("snapshot [collector=aerospace]")
    assert lines[2].endswith("plain")


def test_call_site_extra_is_merged_with_the_adapter_context(handler: logging.Handler) -> None:
    logsetup.get_logger("fylgja.ctx", collector="claude").info("x", extra={"session": "claude:1"})
    assert "[collector=claude session=claude:1]" in _log_text()


def test_context_cannot_inject_lines(handler: logging.Handler) -> None:
    logsetup.get_logger("fylgja.ctx", session="a\nFAKE LINE").info("x")
    assert len(_log_text().splitlines()) == 1


def test_a_canary_prompt_and_token_never_reach_the_file(handler: logging.Handler) -> None:
    log = logging.getLogger("fylgja.canary")
    log.info("got prompt", extra={"prompt": CANARY_PROMPT})
    log.error("failed", extra={"prompts": [CANARY_PROMPT], "env": {"T": CANARY_ENV_TOKEN}})
    log.info("auth with %s", CANARY_TOKEN)
    log.info("header Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123")
    log.info("env CLAUDE_CODE_MESSAGING_TOKEN=%s", CANARY_ENV_TOKEN)
    log.info("env CMUX_SOCKET_CAPABILITY: %s", CANARY_ENV_TOKEN)
    text = _log_text()
    for canary in (CANARY_PROMPT, CANARY_TOKEN, CANARY_ENV_TOKEN, "abcdefghijklmnopqrstuvwxyz0123"):
        assert canary not in text
    assert text.count(logsetup.MASK) >= 4


def test_registered_secrets_are_masked_even_without_a_pattern(handler: logging.Handler) -> None:
    secret = "plain-api-secret-value"  # noqa: S105
    logging.getLogger("fylgja.s").info("value %s", secret)
    assert secret in _log_text()  # not known yet
    logsetup.register_secret(secret)
    logging.getLogger("fylgja.s").info("again %s", secret)
    assert secret not in _log_text().splitlines()[-1]
    logsetup.register_secret("")  # an empty secret would mask everything: ignored
    logging.getLogger("fylgja.s").info("still readable")
    assert "still readable" in _log_text()


def test_configured_redact_patterns_apply(handler: logging.Handler) -> None:
    logsetup.setup_logging(redact_patterns=[r"ACME-\d+"])
    try:
        logging.getLogger("fylgja.p").info("order ACME-12345 shipped")
        assert "ACME-12345" not in _log_text()
    finally:
        logsetup.teardown_logging()


def test_titles_are_dropped_from_info_but_kept_for_debug(handler: logging.Handler) -> None:
    class Probe(logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            self.titles: list[str | None] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.titles.append(getattr(record, "title", None))

    probe = Probe()
    probe.addFilter(logsetup.PrivacyLogFilter())
    log = logging.getLogger("fylgja.title")
    log.addHandler(probe)
    try:
        log.debug("d", extra={"title": CANARY_TITLE})
        log.info("i", extra={"title": CANARY_TITLE})
        log.warning("w", extra={"title": CANARY_TITLE})
    finally:
        log.removeHandler(probe)
    assert probe.titles == [CANARY_TITLE, None, None]


def test_setup_is_idempotent_and_teardown_restores_the_root(tmp_path: Path) -> None:
    root = logging.getLogger()
    before_handlers = list(root.handlers)
    before_level = root.level
    logsetup.setup_logging(level="DEBUG")
    logsetup.setup_logging(level="WARNING")
    ours = [h for h in root.handlers if h not in before_handlers]
    assert len(ours) == 1
    assert root.level == logging.WARNING
    logsetup.teardown_logging()
    assert root.handlers == before_handlers
    assert root.level == before_level
    logsetup.teardown_logging()  # nothing left to undo


def test_an_explicit_path_is_used_and_its_directory_created(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "dir" / "x.log"
    try:
        logsetup.setup_logging(path=target)
        logging.getLogger("fylgja.x").info("hi")
    finally:
        logsetup.teardown_logging()
    assert "hi" in target.read_text(encoding="utf-8")
