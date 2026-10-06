"""``fylgja install secret [--rotate]`` (WP3.4, A6, A22) and the CLI client's bearer."""

from __future__ import annotations

import base64
import json
import logging
import os
import stat
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from fylgja import paths
from fylgja.cli import client, install
from fylgja.cli.app import app

runner = CliRunner()


def _secret_text() -> str:
    return paths.api_secret_file().read_text(encoding="utf-8").strip()


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _run(*args: str) -> Any:
    return runner.invoke(app, ["install", "secret", *args])


def test_a_secret_is_32_random_bytes_url_safe_encoded_with_mode_0600() -> None:
    result = _run()
    assert result.exit_code == 0, result.output
    secret = _secret_text()
    assert len(secret) == 43
    assert len(base64.urlsafe_b64decode(secret + "=")) == 32
    assert set(secret) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
    assert _mode(paths.api_secret_file()) == 0o600


def test_the_secret_is_not_printed_by_default() -> None:
    result = _run()
    assert _secret_text() not in result.output
    assert "created" in result.output
    again = _run()
    rotated = _run("--rotate")
    assert _secret_text() not in again.output + rotated.output


def test_show_prints_the_secret_on_request() -> None:
    result = _run("--show")
    assert result.output.strip().splitlines()[-1] == _secret_text()
    kept = _run("--show")
    assert kept.output.strip().splitlines()[-1] == _secret_text()


def test_an_existing_secret_is_kept_without_rotate() -> None:
    _run()
    first = _secret_text()
    paths.api_secret_file().chmod(0o644)
    result = _run()
    assert result.exit_code == 0
    assert "kept" in result.output
    assert _secret_text() == first
    assert _mode(paths.api_secret_file()) == 0o600  # a kept secret is tightened


def test_rotate_replaces_the_secret_and_keeps_the_mode() -> None:
    _run()
    first = _secret_text()
    result = _run("--rotate")
    assert result.exit_code == 0
    assert "replaced" in result.output
    assert _secret_text() != first
    assert len(_secret_text()) == 43
    assert _mode(paths.api_secret_file()) == 0o600
    leftovers = [p.name for p in paths.config_dir().iterdir() if p.name != "api-secret"]
    assert leftovers == []


def test_rotate_without_a_secret_creates_one() -> None:
    result = _run("--rotate")
    assert result.exit_code == 0
    assert "created" in result.output
    assert len(_secret_text()) == 43


def test_an_empty_secret_file_is_replaced() -> None:
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.api_secret_file().write_text("\n", encoding="utf-8")
    assert _run().exit_code == 0
    assert len(_secret_text()) == 43


def test_the_secret_never_reaches_the_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    result = _run()
    secret = _secret_text()
    assert secret not in result.output
    assert secret not in caplog.text
    assert secrets_in_tree(paths.config_dir().parent, secret) == [paths.api_secret_file()]


def secrets_in_tree(root: Path, secret: str) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file() and secret in p.read_text(errors="ignore")]


def test_two_secrets_differ() -> None:
    assert install.new_secret() != install.new_secret()


def test_a_secret_location_inside_dotfiles_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = Path(os.environ["HOME"])
    inside = home / "dotfiles" / "fylgja" / ".config" / "fylgja"
    monkeypatch.setenv("FYLGJA_CONFIG_DIR", str(inside))
    result = _run()
    assert result.exit_code == 1
    assert "dotfiles" in result.output
    assert not (inside / "api-secret").exists()


def test_a_config_directory_linked_into_dotfiles_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = Path(os.environ["HOME"])
    real = home / "dotfiles" / "fylgja"
    real.mkdir(parents=True)
    link = home / "config-link"
    link.symlink_to(real)
    monkeypatch.setenv("FYLGJA_CONFIG_DIR", str(link))
    result = _run()
    assert result.exit_code == 1
    assert not (real / "api-secret").exists()


def test_an_unreadable_secret_is_reported_without_a_traceback() -> None:
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.api_secret_file().mkdir()  # a directory where the file belongs
    result = _run()
    assert result.exit_code == 1
    assert "api-secret" in result.output


def test_an_unwritable_directory_is_reported() -> None:
    directory = paths.config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o500)
    try:
        result = _run()
    finally:
        directory.chmod(0o700)
    assert result.exit_code == 1
    assert "cannot write" in result.output
    assert not paths.api_secret_file().exists()


def test_the_installer_function_reports_through_echo() -> None:
    lines: list[str] = []
    code = install.run_install_secret(echo=lines.append)
    assert code == 0
    assert _secret_text() not in "\n".join(lines)


# --- the CLI client sends the bearer ---------------------------------------------------------


class _Echo(BaseHTTPRequestHandler):
    seen: list[dict[str, Any]] = []

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        _Echo.seen.append({"headers": dict(self.headers), "body": json.loads(body)})
        payload = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def echo_server() -> Iterator[str]:
    _Echo.seen = []
    server = HTTPServer(("127.0.0.1", 0), _Echo)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_the_client_sends_the_bearer_and_json_and_no_origin(echo_server: str) -> None:
    _run()
    result = client.send_json("POST", "/todos", {"text": "x"}, url=echo_server)
    assert result == {"ok": True}
    headers = _Echo.seen[0]["headers"]
    assert headers["Authorization"] == f"Bearer {_secret_text()}"
    assert headers["Content-Type"] == "application/json"
    assert "Origin" not in headers
    assert _Echo.seen[0]["body"] == {"text": "x"}
    client.send_json("POST", "/todos", url=echo_server)
    assert _Echo.seen[1]["body"] == {}


def test_the_client_without_a_secret_says_what_to_do() -> None:
    with pytest.raises(client.ApiError, match="install secret"):
        client.send_json("POST", "/todos", url="http://127.0.0.1:9")


def test_the_client_reports_a_down_daemon_and_an_error_status(echo_server: str) -> None:
    _run()
    with pytest.raises(client.DaemonDown):
        client.send_json("POST", "/todos", url="http://127.0.0.1:1", timeout=1)
    with pytest.raises(client.ApiError) as raised:
        client.send_json("DELETE", "/todos/1", url=echo_server)  # the echo server has no DELETE
    assert raised.value.status == 501
