"""The OpenCode plugin's envelope and the Python reader agree (WP3.2).

``tests/fixtures/envelope/opencode-session-status.json`` is the golden envelope that
``src/fylgja/collectors/opencode/fylgja.ts`` produces (``scripts/gen_opencode_envelope.py``
regenerates it with ``bun``). These tests check, without Node or Bun:

* the Python reader accepts the golden;
* its keys and their types match the ``fylgja.hook/1`` golden of ``fylgja hook``;
* every envelope field name, environment variable and payload field in the TypeScript source is in
  the documented contract, and the source follows the writer rules (atomic rename from an
  ``O_EXCL`` temporary file, mode 0600, an inbox with mode 0700).

When ``bun`` is on ``PATH`` the plugin itself also runs against a temporary data directory.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest

from fylgja import hook
from fylgja.collectors.claude.normalize import (
    ENVELOPE_SCHEMA,
    TERMINAL_ID_VARIABLES,
    EnvelopeError,
    normalize,
    parse_envelope,
)
from fylgja.models import HookEvent
from fylgja.privacy import minimize_hook_payload

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "envelope"
GOLDEN = FIXTURES / "opencode-session-status.json"
HOOK_GOLDEN = FIXTURES / "fylgja.hook-1.json"
PLUGIN = REPO_ROOT / "src" / "fylgja" / "collectors" / "opencode" / "fylgja.ts"
CONTRACT = REPO_ROOT / "docs" / "api" / "inbox-envelope.md"

SESSION = "ses_00000000000000000000000001"
FORWARDED_EVENTS = ["session.status", "permission.asked", "todo.updated"]


def _load(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _contract_section(heading: str) -> str:
    text = CONTRACT.read_text(encoding="utf-8")
    match = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", text, re.M | re.DOTALL)
    assert match, f"no section {heading!r} in {CONTRACT.name}"
    return match.group(1)


def _contract_fields() -> list[str]:
    return re.findall(r"^\| `([a-z_]+)` \|", _contract_section("Fields"), re.MULTILINE)


def _contract_env() -> list[str]:
    section = _contract_section("Environment allowlist (A3)")
    return re.findall(r"^- `([A-Z_]+)`", section, re.MULTILINE)


def _source() -> str:
    return PLUGIN.read_text(encoding="utf-8")


# --------------------------------------------------------------------------------------------
# The Python reader accepts the golden
# --------------------------------------------------------------------------------------------


def test_the_python_reader_accepts_the_golden() -> None:
    envelope = parse_envelope(GOLDEN.read_bytes())
    assert envelope.source == "opencode"
    assert envelope.event_arg == "session.status"
    assert envelope.truncated is False
    assert envelope.fylgja_version == "0.1.0"
    event = normalize(envelope, source="opencode", kind="opencode")
    assert isinstance(event, HookEvent)
    assert (event.source, event.entity_id) == ("opencode", f"opencode:{SESSION}")
    payload = event.payload
    assert (payload.session_id, payload.event, payload.reason) == (
        SESSION,
        "session.status",
        "busy",
    )
    assert payload.cwd == "/work/example/api-service"
    assert payload.envelope_id == "7a2b9c1d-3e4f-4a5b-8c6d-0e1f2a3b4c5d"
    assert payload.ids == {
        "CMUX_WORKSPACE_ID": "00000000-0000-4000-8000-0000000000a1",
        "CMUX_SURFACE_ID": "00000000-0000-4000-8000-0000000000a2",
        "CMUX_TAB_ID": "00000000-0000-4000-8000-0000000000a3",
        "CMUX_PANEL_ID": "00000000-0000-4000-8000-0000000000a4",
        "TERM_PROGRAM": "ghostty",
    }


def test_the_golden_is_not_a_claude_envelope() -> None:
    envelope = parse_envelope(GOLDEN.read_bytes())
    with pytest.raises(EnvelopeError, match="source does not match"):
        normalize(envelope, source="claude", kind="claude")


def test_the_reader_keeps_only_the_payload_fields_the_plugin_sends() -> None:
    payload = json.loads(_load(GOLDEN)["payload_text"])
    assert set(minimize_hook_payload(payload)) == {"session_id", "event", "reason", "cwd"}


# --------------------------------------------------------------------------------------------
# Keys and types match the hook's golden
# --------------------------------------------------------------------------------------------


def test_the_golden_has_the_keys_of_the_hook_golden_in_order() -> None:
    assert list(_load(GOLDEN)) == list(_load(HOOK_GOLDEN))


def test_the_golden_has_the_types_of_the_hook_golden() -> None:
    golden = _load(GOLDEN)
    for key, value in _load(HOOK_GOLDEN).items():
        assert type(golden[key]) is type(value), key
    assert all(isinstance(v, str) for v in golden["env"].values())


def test_the_golden_follows_the_contract_values() -> None:
    golden = _load(GOLDEN)
    assert golden["schema"] == hook.SCHEMA == ENVELOPE_SCHEMA
    assert uuid.UUID(golden["id"]).version == 4
    assert golden["received_at_ns"] > 10**18
    assert golden["source"] == "opencode"
    assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", golden["source"])
    assert len(golden["event_arg"]) <= hook.EVENT_ARG_MAX_CHARS
    assert set(golden["env"]) <= set(TERMINAL_ID_VARIABLES)
    assert set(golden["env"]) <= set(hook.ENV_ALLOWLIST)
    assert all(len(v) <= hook.ENV_VALUE_MAX_CHARS for v in golden["env"].values())
    assert json.loads(golden["payload_text"])["hook_event_name"] == golden["event_arg"]
    assert golden["truncated"] is False


def test_the_golden_round_trips_through_the_python_encoder() -> None:
    golden = _load(GOLDEN)
    assert json.loads(hook.encode_envelope(golden)) == golden


# --------------------------------------------------------------------------------------------
# The TypeScript source against the contract (static)
# --------------------------------------------------------------------------------------------


def test_every_envelope_field_in_the_plugin_is_in_the_contract() -> None:
    encode = re.search(r"function encode\(.*?\n}\n", _source(), re.DOTALL)
    assert encode
    keys = re.findall(r'`"([a-z_]+)":', encode.group(0))
    assert keys == _contract_fields() == list(_load(GOLDEN))


def test_every_environment_variable_in_the_plugin_is_documented() -> None:
    block = re.search(r"const ENV_ALLOWLIST = \[(.*?)\]", _source(), re.DOTALL)
    assert block
    names = re.findall(r'"([A-Z][A-Z0-9_]+)"', block.group(1))
    assert names
    assert set(names) <= set(_contract_env())
    assert set(names) <= set(TERMINAL_ID_VARIABLES)
    assert not {"CLAUDE_CODE_MESSAGING_TOKEN", "CMUX_SOCKET_CAPABILITY"} & set(names)
    # Nothing else of the environment is read, except the data directory override.
    read = set(re.findall(r"process\.env\.([A-Z_]+)", _source()))
    assert read == {"FYLGJA_DATA_DIR"}
    assert "process.env[name]" in _source()


def test_every_payload_field_in_the_plugin_survives_minimization() -> None:
    source = _source()
    fields = set(re.findall(r"\bpayload\.([a-z_]+) =", source))
    fields |= set(re.findall(r"\b(session_id|hook_event_name):", source))
    assert fields == {"session_id", "hook_event_name", "cwd", "reason", "tool_name"}
    kept = minimize_hook_payload({name: "value" for name in fields})
    assert set(kept) == {"session_id", "event", "cwd", "reason", "tool_name"}


def test_the_plugin_forwards_exactly_the_three_events() -> None:
    block = re.search(r"const EVENTS = \[(.*?)\]", _source(), re.DOTALL)
    assert block
    assert re.findall(r'"([a-z.]+)"', block.group(1)) == FORWARDED_EVENTS


def test_the_plugin_uses_the_contract_constants() -> None:
    source = _source()
    assert f'const SCHEMA = "{hook.SCHEMA}"' in source
    assert 'const SOURCE = "opencode"' in source
    assert "hook-inbox" in source
    assert f"const ENV_VALUE_MAX = {hook.ENV_VALUE_MAX_CHARS}" in source
    assert f"const EVENT_ARG_MAX = {hook.EVENT_ARG_MAX_CHARS}" in source
    assert hook.BACKLOG_STAMP in source
    assert 'startsWith("full")' in source
    assert "[A-Za-z0-9][A-Za-z0-9_-]{0,127}" in source  # the drain's session id rule


def test_the_plugin_follows_the_atomic_write_rules() -> None:
    source = _source()
    for needle in (
        "O_CREAT",
        "O_EXCL",
        "O_WRONLY",
        "O_NOFOLLOW",
        "0o600",
        "0o700",
        "fsyncSync",
        "renameSync",
        "lstatSync",
        ".tmp-${id}",
        "${ns.toString()}-${id}.json",
    ):
        assert needle in source, needle
    assert source.index("fsyncSync(fd)") < source.index("renameSync(tmp")
    assert "isSymbolicLink()" in source
    assert "info.uid === process.getuid" in source


def test_the_plugin_is_quiet_and_offline_and_exports_only_the_plugin() -> None:
    source = _source()
    for forbidden in ("console.", "fetch(", "http", "child_process", "Bun.", "readdir"):
        assert forbidden not in source, forbidden
    exports = re.findall(r"^export (\w+) (\w+)", source, re.MULTILINE)
    assert exports == [("const", "FylgjaPlugin")]


def test_the_plugin_ships_as_package_data() -> None:
    assert PLUGIN.is_file()
    assert PLUGIN.parent.joinpath("__init__.py").is_file()


# --------------------------------------------------------------------------------------------
# The plugin itself, when bun is available
# --------------------------------------------------------------------------------------------

BUN = shutil.which("bun")

DRIVER = """
const { FylgjaPlugin } = await import(process.argv[1])
const hooks = await FylgjaPlugin({ directory: process.argv[2] })
for (const event of JSON.parse(process.argv[3])) await hooks.event({ event })
"""


def _run_plugin(tmp_path: Path, events: list[dict[str, Any]], **env: str) -> Path:
    """Run the plugin in ``bun`` with its data directory under ``tmp_path``; return the inbox."""
    assert BUN is not None
    data = tmp_path / "data"
    environ = {k: v for k, v in os.environ.items() if k not in TERMINAL_ID_VARIABLES}
    environ.update({"FYLGJA_DATA_DIR": str(data), **env})
    subprocess.run(
        [BUN, "-e", DRIVER, str(PLUGIN), "/work/example/api-service", json.dumps(events)],
        check=True,
        env=environ,
        cwd=tmp_path,
        capture_output=True,
        timeout=60,
    )
    return data / "hook-inbox" / "opencode"


def _status(session: str = SESSION, kind: str = "busy") -> dict[str, Any]:
    return {
        "type": "session.status",
        "properties": {"sessionID": session, "status": {"type": kind}},
    }


def _envelopes(inbox: Path) -> list[dict[str, Any]]:
    return [json.loads(p.read_text()) for p in sorted(inbox.glob("*.json"))]


@pytest.mark.skipif(BUN is None, reason="bun is not on PATH")
def test_the_plugin_output_matches_the_golden_and_the_rules(tmp_path: Path) -> None:
    inbox = _run_plugin(tmp_path, [_status()], TERM_PROGRAM="ghostty")
    files = sorted(inbox.iterdir())
    assert len(files) == 1
    assert re.fullmatch(r"[0-9]+-[0-9a-f-]{36}\.json", files[0].name)
    assert (inbox.stat().st_mode & 0o777, files[0].stat().st_mode & 0o777) == (0o700, 0o600)
    assert not [p for p in inbox.iterdir() if p.name.startswith(".tmp-")]
    text = files[0].read_text(encoding="ascii")
    assert text.endswith("\n")
    assert text.count("\n") == 1
    envelope = json.loads(text)
    golden = _load(GOLDEN)
    assert list(envelope) == list(golden)
    for key, value in golden.items():
        assert type(envelope[key]) is type(value), key
    assert files[0].name == f"{envelope['received_at_ns']}-{envelope['id']}.json"
    assert envelope["env"] == {"TERM_PROGRAM": "ghostty"}
    for key in ("schema", "source", "event_arg", "payload_text", "truncated", "fylgja_version"):
        assert envelope[key] == golden[key], key
    # The Python reader takes what the plugin wrote.
    event = normalize(parse_envelope(text), source="opencode", kind="opencode")
    assert event.payload.reason == "busy"


@pytest.mark.skipif(BUN is None, reason="bun is not on PATH")
def test_the_plugin_forwards_the_three_events_and_nothing_else(tmp_path: Path) -> None:
    events = [
        _status(kind="idle"),
        {"type": "permission.asked", "properties": {"sessionID": SESSION, "permission": "bash"}},
        {
            "type": "todo.updated",
            "properties": {"sessionID": SESSION, "todos": [{"content": "TODO-CANARY"}]},
        },
        {"type": "message.updated", "properties": {"sessionID": SESSION}},
        {"type": "session.idle", "properties": {"sessionID": SESSION}},
        {"type": "session.status", "properties": {"sessionID": "has space"}},
        {"type": "session.status", "properties": {}},
        {"type": "todo.updated"},
        {"properties": {"sessionID": SESSION}},
    ]
    inbox = _run_plugin(tmp_path, events)
    sent = _envelopes(inbox)
    assert [e["event_arg"] for e in sent] == FORWARDED_EVENTS
    payloads = [json.loads(e["payload_text"]) for e in sent]
    assert payloads[0]["reason"] == "idle"
    assert payloads[1]["tool_name"] == "bash"
    assert "reason" not in payloads[2]
    assert "TODO-CANARY" not in sent[2]["payload_text"]  # no todo text travels
    names = [p.name for p in sorted(inbox.glob("*.json"))]
    assert names == sorted(names)  # name order is production order
    assert len({e["id"] for e in sent}) == 3
    assert len({e["received_at_ns"] for e in sent}) == 3


@pytest.mark.skipif(BUN is None, reason="bun is not on PATH")
def test_the_plugin_sends_only_the_allowlisted_environment(tmp_path: Path) -> None:
    inbox = _run_plugin(
        tmp_path,
        [_status()],
        CMUX_WORKSPACE_ID="workspace-1",
        CLAUDE_CODE_MESSAGING_TOKEN="canary-token-value",
        CMUX_SOCKET_CAPABILITY="canary-capability-value",
        OPENAI_API_KEY="canary-key-value",
    )
    text = next(inbox.glob("*.json")).read_text()
    assert json.loads(text)["env"] == {"CMUX_WORKSPACE_ID": "workspace-1"}
    assert "canary" not in text


@pytest.mark.skipif(BUN is None, reason="bun is not on PATH")
def test_the_plugin_escapes_non_ascii_and_cuts_long_values(tmp_path: Path) -> None:
    long_dir = "/work/" + "é" * 600
    assert BUN is not None
    data = tmp_path / "data"
    subprocess.run(
        [BUN, "-e", DRIVER, str(PLUGIN), long_dir, json.dumps([_status()])],
        check=True,
        env={**os.environ, "FYLGJA_DATA_DIR": str(data)},
        cwd=tmp_path,
        capture_output=True,
        timeout=60,
    )
    raw = next((data / "hook-inbox" / "opencode").glob("*.json")).read_bytes()
    raw.decode("ascii")  # plain ASCII, as the Python writer's files are
    cwd = json.loads(json.loads(raw)["payload_text"])["cwd"]
    assert cwd == long_dir[:512]


@pytest.mark.skipif(BUN is None, reason="bun is not on PATH")
@pytest.mark.parametrize("case", ["wrong-mode", "symlink", "backlog-full", "not-a-directory"])
def test_the_plugin_refuses_an_unsafe_inbox(tmp_path: Path, case: str) -> None:
    inbox = tmp_path / "data" / "hook-inbox" / "opencode"
    inbox.parent.mkdir(parents=True, mode=0o700)
    elsewhere = tmp_path / "elsewhere"
    if case == "wrong-mode":
        inbox.mkdir(mode=0o755)
        inbox.chmod(0o755)
    elif case == "symlink":
        elsewhere.mkdir(mode=0o700)
        inbox.symlink_to(elsewhere)
    elif case == "backlog-full":
        inbox.mkdir(mode=0o700)
        (inbox / ".backlog-stamp").write_text("full\n")
    else:
        inbox.write_text("a file")
    _run_plugin(tmp_path, [_status()])
    if case == "symlink":
        assert list(elsewhere.iterdir()) == []
    elif case == "not-a-directory":
        assert inbox.read_text() == "a file"
    else:
        assert [p.name for p in inbox.iterdir() if p.name != ".backlog-stamp"] == []


@pytest.mark.skipif(BUN is None, reason="bun is not on PATH")
def test_the_plugin_never_throws_into_opencode(tmp_path: Path) -> None:
    # An unusable data directory (a file where the directory should be): the event is lost silently.
    blocker = tmp_path / "data"
    blocker.write_text("a file")
    result = subprocess.run(
        [BUN, "-e", DRIVER, str(PLUGIN), "/work/x", json.dumps([_status(), {"type": 5}])],
        env={**os.environ, "FYLGJA_DATA_DIR": str(blocker)},
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
