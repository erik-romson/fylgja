"""Target resolution over a real registry directory and fake Unix sockets (WP3.6, A9, A13, A14).

Every socket lives under a short ``/tmp/fy-XXXX`` directory that the test injects as the allowed
root; ``/tmp/cc-socks*`` is never touched.
"""

from __future__ import annotations

import os
import stat
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from api_support import BASE_URL, make_daemon
from claude_support import PS, T0, PsRunner
from message_support import (
    PID,
    SESSION,
    FakeInbox,
    LineEncoder,
    World,
    short_tmp,
    socket_dir,
)

from fylgja.api.app import create_app
from fylgja.collectors.seams import FileInfo
from fylgja.core.daemon import Daemon
from fylgja.core.system import LocalFileSystem
from fylgja.messaging.claude.encoders.v1 import V1Encoder
from fylgja.messaging.claude.messenger import ClaudeMessenger
from fylgja.messaging.claude.receipts import ReceiptState
from fylgja.messaging.claude.resolve import (
    KNOWN_PEER_PROTOCOLS,
    MIN_VERSION,
    Refusal,
    Resolver,
    Target,
    check_socket,
    default_roots,
    parse_version,
    ps_start_time,
    read_rows,
    supplied_socket_fields,
    version_gate,
)
from fylgja.messaging.claude.transport import Progress
from fylgja.messaging.service import MessageService, WriterReceipts

SECRET = "k7Qz-test-secret-9fA2xLmN0pRsTuVwXyZ_abcdefghij"
AUTH = {"Authorization": f"Bearer {SECRET}", "Content-Type": "application/json"}


@pytest.fixture
def tmp_socks() -> Iterator[Path]:
    with short_tmp() as root:
        yield root


@pytest.fixture
def world(isolated_env: Path, tmp_socks: Path) -> World:
    return World(isolated_env / "registry", socket_dir(tmp_socks))


@pytest.fixture
def inbox(world: World) -> Iterator[FakeInbox]:
    with FakeInbox(world.sock_path()) as server:
        yield server


async def _refusal(world: World, session: str = SESSION) -> Refusal:
    with pytest.raises(Refusal) as caught:
        await world.resolver.resolve(session)
    return caught.value


def _stat_with(info: os.stat_result, **changes: int) -> os.stat_result:
    fields = list(info[:10])
    names = ["st_mode", "st_ino", "st_dev", "st_nlink", "st_uid", "st_gid", "st_size"]
    for name, value in changes.items():
        fields[names.index(name)] = value
    return os.stat_result(fields)


# --- a live, safe target -----------------------------------------------------------------------


async def test_a_live_row_with_a_safe_socket_resolves(world: World, inbox: FakeInbox) -> None:
    world.add()
    target = await world.resolver.resolve(SESSION)
    assert target == Target(
        session_id=SESSION,
        pid=PID,
        socket_path=str(world.sock_path()),
        proc_start=target.proc_start,
        version=(2, 1, 289),
        peer_protocol=1,
    )
    assert abs(target.proc_start_epoch - T0) < 1
    await world.resolver.confirm(target)  # nothing changed since
    assert world.starts.calls == [PID, PID]  # liveness at resolution and right before connecting
    assert inbox.connections == 0  # resolving never connects


# --- A14: symlinks, owner, mode, roots, path equality -------------------------------------------


async def test_a_symlinked_socket_is_refused(world: World, tmp_socks: Path) -> None:
    elsewhere = socket_dir(tmp_socks, "real")
    with FakeInbox(elsewhere / "r.sock") as real:
        world.sock_path().symlink_to(elsewhere / "r.sock")
        world.add()
        refusal = await _refusal(world)
        assert real.connections == 0
    assert (refusal.state, refusal.kind) == (ReceiptState.REFUSED_LOCALLY, "socket_symlink")


async def test_a_symlinked_socket_directory_is_refused(world: World, tmp_socks: Path) -> None:
    link = tmp_socks / "link"
    link.symlink_to(world.socks)
    with FakeInbox(world.sock_path()):
        resolver = Resolver(
            world.registry,
            fs=LocalFileSystem(),
            start_time=world.starts,
            roots=(str(link),),
            uid=os.getuid(),
        )
        world.add(socket_path=str(link / f"{PID}.sock"))
        with pytest.raises(Refusal) as caught:
            await resolver.resolve(SESSION)
    assert caught.value.kind == "socket_dir_symlink"


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o711, 0o770, 0o777])
async def test_a_socket_directory_open_to_others_is_refused(
    world: World, inbox: FakeInbox, mode: int
) -> None:
    world.socks.chmod(mode)
    world.add()
    refusal = await _refusal(world)
    world.socks.chmod(0o700)
    assert (refusal.state, refusal.kind) == (ReceiptState.REFUSED_LOCALLY, "socket_dir_mode")


async def test_a_socket_directory_of_another_owner_is_refused(
    world: World, inbox: FakeInbox
) -> None:
    resolver = Resolver(
        world.registry,
        fs=LocalFileSystem(),
        start_time=world.starts,
        roots=(str(world.socks),),
        uid=os.getuid() + 1,
    )
    world.add()
    with pytest.raises(Refusal) as caught:
        await resolver.resolve(SESSION)
    assert caught.value.kind == "socket_dir_owner"


async def test_a_socket_of_another_owner_is_refused(world: World, inbox: FakeInbox) -> None:
    socket_path = str(world.sock_path())

    def lstat(path: str) -> os.stat_result:
        info = os.lstat(path)
        return _stat_with(info, st_uid=os.getuid() + 1) if path == socket_path else info

    resolver = Resolver(
        world.registry,
        fs=LocalFileSystem(),
        start_time=world.starts,
        roots=(str(world.socks),),
        lstat=lstat,
    )
    world.add()
    with pytest.raises(Refusal) as caught:
        await resolver.resolve(SESSION)
    assert caught.value.kind == "socket_owner"


async def test_a_regular_file_or_directory_is_not_a_socket(world: World) -> None:
    world.sock_path().write_text("not a socket")
    world.add()
    assert (await _refusal(world)).kind == "not_a_socket"
    socks_file = world.socks.parent / "plain"
    socks_file.write_text("x")
    with pytest.raises(Refusal) as caught:
        check_socket(
            str(socks_file / "s.sock"),
            registry_path=str(socks_file / "s.sock"),
            roots=(str(socks_file),),
            uid=os.getuid(),
        )
    assert caught.value.kind == "socket_dir_not_a_directory"


async def test_a_socket_outside_the_allowed_roots_is_refused(world: World, tmp_socks: Path) -> None:
    other = socket_dir(tmp_socks, "other")
    with FakeInbox(other / "o.sock") as server:
        world.add(socket_path=str(other / "o.sock"))
        refusal = await _refusal(world)
        assert server.connections == 0
    assert (refusal.state, refusal.kind) == (
        ReceiptState.REFUSED_LOCALLY,
        "socket_outside_allowed_roots",
    )


async def test_a_path_that_is_not_normalized_is_refused(world: World, inbox: FakeInbox) -> None:
    sneaky = f"{world.socks}/../{world.socks.name}/{PID}.sock"
    world.add(socket_path=sneaky)
    assert (await _refusal(world)).kind == "socket_path_not_normal"
    world.add(socket_path="relative/4242.sock")
    assert (await _refusal(world)).kind == "socket_path_not_normal"


def test_the_path_must_equal_the_registry_rows_path(world: World, inbox: FakeInbox) -> None:
    path = str(world.sock_path())
    roots = (str(world.socks),)
    check_socket(path, registry_path=path, roots=roots, uid=os.getuid())
    for registry_path in (str(world.socks / "other.sock"), None, path + "/"):
        with pytest.raises(Refusal) as caught:
            check_socket(path, registry_path=registry_path, roots=roots, uid=os.getuid())
        assert caught.value.kind == "socket_path_mismatch"


async def test_a_registry_path_changed_before_connecting_stops_the_send(
    world: World, inbox: FakeInbox
) -> None:
    world.add()
    target = await world.resolver.resolve(SESSION)
    with FakeInbox(world.socks / "moved.sock") as moved:
        world.add(socket_path=str(world.socks / "moved.sock"))  # a safe path, but not the target's
        with pytest.raises(Refusal) as caught:
            await world.resolver.confirm(target)
        assert moved.connections == 0
    assert (caught.value.state, caught.value.kind) == (
        ReceiptState.REFUSED_LOCALLY,
        "socket_path_mismatch",
    )


async def test_a_socket_swapped_for_a_symlink_before_connecting_stops_the_send(
    world: World, tmp_socks: Path
) -> None:
    elsewhere = socket_dir(tmp_socks, "real")
    with FakeInbox(world.sock_path()), FakeInbox(elsewhere / "r.sock") as real:
        world.add()
        target = await world.resolver.resolve(SESSION)
        world.sock_path().unlink()
        world.sock_path().symlink_to(elsewhere / "r.sock")
        with pytest.raises(Refusal) as caught:
            await world.resolver.confirm(target)
        assert real.connections == 0
    assert caught.value.kind == "socket_symlink"


async def test_missing_sockets_and_directories_are_unreachable(
    world: World, tmp_socks: Path
) -> None:
    world.add()
    refusal = await _refusal(world)
    assert (refusal.state, refusal.kind) == (ReceiptState.UNREACHABLE, "socket_missing")
    gone = tmp_socks / "gone"
    with pytest.raises(Refusal) as caught:
        check_socket(
            str(gone / "s.sock"),
            registry_path=str(gone / "s.sock"),
            roots=(str(gone),),
            uid=os.getuid(),
        )
    assert (caught.value.state, caught.value.kind) == (
        ReceiptState.UNREACHABLE,
        "socket_dir_missing",
    )


def test_an_unreadable_directory_or_socket_is_refused(world: World, inbox: FakeInbox) -> None:
    path = str(world.sock_path())

    def denied_on(target: str) -> Any:
        def lstat(candidate: str) -> os.stat_result:
            if candidate == target:
                raise PermissionError(13, "denied")
            return os.lstat(candidate)

        return lstat

    for target, kind in ((str(world.socks), "socket_dir_unreadable"), (path, "socket_unreadable")):
        with pytest.raises(Refusal) as caught:
            check_socket(
                path,
                registry_path=path,
                roots=(str(world.socks),),
                uid=os.getuid(),
                lstat=denied_on(target),
            )
        assert caught.value.kind == kind


async def test_a_row_without_an_inbox_socket_is_unreachable(world: World) -> None:
    world.add(messagingSocketPath=None)
    refusal = await _refusal(world)
    assert (refusal.state, refusal.kind) == (ReceiptState.UNREACHABLE, "no_inbox_socket")


# --- liveness: pid reuse and stale rows ---------------------------------------------------------


async def test_a_reused_pid_is_refused(world: World, inbox: FakeInbox) -> None:
    world.add()
    world.starts.table[PID] = T0 + 3600  # another process now has the pid
    refusal = await _refusal(world)
    assert (refusal.state, refusal.kind) == (ReceiptState.REFUSED_LOCALLY, "pid_reused")
    assert inbox.connections == 0


async def test_a_pid_reused_between_resolution_and_connect_is_refused(
    world: World, inbox: FakeInbox
) -> None:
    world.add()
    target = await world.resolver.resolve(SESSION)
    world.starts.table[PID] = T0 + 60
    with pytest.raises(Refusal) as caught:
        await world.resolver.confirm(target)
    assert caught.value.kind == "pid_reused"
    world.starts.table[PID] = T0
    world.add(start=T0 + 120)  # the row itself now names another process start
    with pytest.raises(Refusal) as caught:
        await world.resolver.confirm(target)
    assert caught.value.kind == "pid_reused"


async def test_a_row_without_proc_start_cannot_be_proven(world: World, inbox: FakeInbox) -> None:
    world.add(start=None)
    refusal = await _refusal(world)
    assert (refusal.state, refusal.kind) == (ReceiptState.REFUSED_LOCALLY, "no_proc_start")


async def test_stale_rows_are_unreachable(world: World, inbox: FakeInbox) -> None:
    world.add(alive=False)
    refusal = await _refusal(world)
    assert (refusal.state, refusal.kind) == (ReceiptState.UNREACHABLE, "stale_row")
    missing = await _refusal(world, "00000000-0000-4000-8000-000000000000")
    assert (missing.state, missing.kind) == (ReceiptState.UNREACHABLE, "no_live_row")
    assert inbox.connections == 0


async def test_a_row_gone_or_changed_before_connecting_is_unreachable(
    world: World, inbox: FakeInbox
) -> None:
    path = world.add()
    target = await world.resolver.resolve(SESSION)
    world.add(session_id="00000000-0000-4000-8000-0000000000aa")  # /clear: a new session id
    with pytest.raises(Refusal) as caught:
        await world.resolver.confirm(target)
    assert (caught.value.state, caught.value.kind) == (ReceiptState.UNREACHABLE, "session_moved")
    path.unlink()
    with pytest.raises(Refusal) as caught:
        await world.resolver.confirm(target)
    assert (caught.value.state, caught.value.kind) == (ReceiptState.UNREACHABLE, "stale_row")
    world.add()
    world.starts.table.pop(PID)  # the process exited
    with pytest.raises(Refusal) as caught:
        await world.resolver.confirm(target)
    assert caught.value.kind == "stale_row"


async def test_two_rows_of_one_session_are_settled_by_liveness(
    world: World, inbox: FakeInbox
) -> None:
    world.add()
    world.add(pid=4300, alive=False)  # a stale row of the same session (an old process)
    assert (await world.resolver.resolve(SESSION)).pid == PID
    world.add(pid=4301)  # a second live process claims the session
    assert (await _refusal(world)).kind == "ambiguous_target"
    world.starts.table.clear()
    refusal = await _refusal(world)
    assert (refusal.state, refusal.kind) == (ReceiptState.UNREACHABLE, "stale_row")


async def test_ps_answers_liveness_and_a_ps_failure_refuses() -> None:
    runner = PsRunner()
    runner.starts[PID] = T0
    start_time = ps_start_time(runner, PS)
    assert abs((await start_time(PID) or 0) - T0) < 1
    assert await start_time(PID + 1) is None
    assert runner.calls[0][:3] == (PS, "-o", "pid=,lstart=")
    runner.error = FileNotFoundError()
    with pytest.raises(Refusal) as caught:
        await start_time(PID)
    assert (caught.value.state, caught.value.kind) == (
        ReceiptState.REFUSED_LOCALLY,
        "liveness_unverified",
    )


# --- A9: per-target version and peerProtocol gates ---------------------------------------------


@pytest.mark.parametrize(
    ("version", "protocol", "kind"),
    [
        ("2.1.223", 1, "version_too_old"),
        ("2.0.999", 1, "version_too_old"),
        (None, 1, "version_unknown"),
        ("next", 1, "version_unknown"),
        ("2.1", 1, "version_unknown"),
        ("2.1.289", None, "peer_protocol_unknown"),
        ("2.1.289", 2, "peer_protocol_unknown"),
        ("2.1.289", 0, "peer_protocol_unknown"),
    ],
)
async def test_the_version_gates_refuse_as_unsupported(
    world: World, inbox: FakeInbox, version: str | None, protocol: int | None, kind: str
) -> None:
    world.add(version=version, peerProtocol=protocol)
    refusal = await _refusal(world)
    assert (refusal.state, refusal.kind) == (ReceiptState.UNSUPPORTED, kind)
    assert inbox.connections == 0


@pytest.mark.parametrize("version", ["2.1.224", "2.1.284", "2.1.289", "2.1.291", "3.0.0-beta.1"])
async def test_supported_versions_pass_the_gate(
    world: World, inbox: FakeInbox, version: str
) -> None:
    world.add(version=version)
    target = await world.resolver.resolve(SESSION)
    assert target.version >= MIN_VERSION
    assert target.peer_protocol in KNOWN_PEER_PROTOCOLS


async def test_the_gate_is_per_target_not_per_installed_cli(world: World, tmp_socks: Path) -> None:
    old, new = "00000000-0000-4000-8000-0000000000a1", "00000000-0000-4000-8000-0000000000a2"
    with FakeInbox(world.sock_path(5001)), FakeInbox(world.sock_path(5002)):
        world.add(pid=5001, session_id=old, version="2.1.200")
        world.add(pid=5002, session_id=new, version="2.1.291")
        assert (await _refusal(world, old)).kind == "version_too_old"
        assert (await world.resolver.resolve(new)).version == (2, 1, 291)


def test_version_parsing_and_the_gate_function() -> None:
    assert parse_version("2.1.289") == (2, 1, 289)
    assert parse_version(" 2.1.224 ") == (2, 1, 224)
    assert parse_version("2.1.290+build.7") == (2, 1, 290)
    for text in (None, "", "v2.1.289", "2.1.x", "2.1.289.1"):
        assert parse_version(text) is None
    assert version_gate("2.1.224", 1) == ((2, 1, 224), 1)


async def test_the_shipped_encoder_makes_every_live_target_unsupported(
    world: World, inbox: FakeInbox
) -> None:
    world.add()
    messenger = ClaudeMessenger(world.resolver, V1Encoder())
    from fylgja.messaging.claude.encoders import OutboundMessage

    delivery = await messenger.send(SESSION, OutboundMessage("id-1", "hello"), Progress())
    assert (delivery.state, delivery.error_kind) == (
        ReceiptState.UNSUPPORTED,
        "no_supported_frame",
    )
    assert inbox.connections == 0


# --- the registry: A13 names only ---------------------------------------------------------------


class SpyFS(LocalFileSystem):
    def __init__(self) -> None:
        self.read: list[str] = []

    def read_bytes(self, path: Path | str) -> bytes:
        self.read.append(Path(path).name)
        return super().read_bytes(path)

    def stat(self, path: Path | str) -> FileInfo:  # pragma: no cover - not used
        raise AssertionError("not used")


async def test_only_registry_rows_are_opened_never_key_files(
    world: World, inbox: FakeInbox
) -> None:
    world.add()
    (world.registry / f"{PID}.4f3c2a.key").write_text("canary-messaging-token-7f3a91c2")
    (world.registry / "notes.txt").write_text("x")
    (world.registry / "123.json.tmp").write_text("{}")
    (world.registry / "77.json").write_text("not json")
    spy = SpyFS()
    resolver = Resolver(world.registry, fs=spy, start_time=world.starts, roots=(str(world.socks),))
    target = await resolver.resolve(SESSION)
    await resolver.confirm(target)
    assert set(spy.read) == {f"{PID}.json", "77.json"}
    assert [row.pid for row in read_rows(spy, world.registry)] == [PID]
    assert read_rows(spy, world.registry / "missing") == []


def test_default_roots_are_claudes_two_directories() -> None:
    assert default_roots(501) == ("/tmp/cc-socks", "/tmp/cc-socks-501")
    assert default_roots() == ("/tmp/cc-socks", f"/tmp/cc-socks-{os.getuid()}")
    resolver = Resolver(Path("/nonexistent"), fs=LocalFileSystem(), start_time=_never)
    assert resolver.roots == default_roots()
    assert resolver.uid == os.getuid()


async def _never(pid: int) -> float | None:  # pragma: no cover - never awaited
    raise AssertionError(pid)


# --- a socket path supplied through the API -----------------------------------------------------


def test_path_like_fields_are_found() -> None:
    assert supplied_socket_fields({"text": "hi"}) == []
    body = {
        "text": "hi",
        "socket_path": "/tmp/x",
        "messagingSocketPath": "/tmp/y",
        "SOCKET": "z",
        "peer_address": "a",
        "path": "b",
    }
    assert supplied_socket_fields(body) == sorted(set(body) - {"text"})


@pytest.fixture
async def messaging_daemon(world: World) -> AsyncIterator[Daemon]:
    instance = make_daemon(user_config={"actions": {"message": {"enabled": True}}})
    await instance.start()
    instance.messages = MessageService(
        enabled=True,
        max_chars=8000,
        messengers={"claude": world.messenger(LineEncoder())},
        receipts=WriterReceipts(instance.writer),
        clock=instance.clock,
    )
    try:
        yield instance
    finally:
        await instance.stop()


@pytest.mark.parametrize(
    "field", ["socket_path", "messagingSocketPath", "socket", "path", "peerAddress"]
)
async def test_an_api_supplied_socket_path_is_rejected(
    messaging_daemon: Daemon, world: World, tmp_socks: Path, field: str
) -> None:
    attacker = socket_dir(tmp_socks, "evil")
    with FakeInbox(world.sock_path()) as real, FakeInbox(attacker / "e.sock") as evil:
        world.add()
        app = create_app(messaging_daemon, secret=SECRET)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=BASE_URL
        ) as http:
            response = await http.post(
                f"/sessions/claude:{SESSION}/message",
                headers=AUTH,
                json={"text": "hello", field: str(attacker / "e.sock")},
            )
        assert response.status_code == 400
        assert "never accepted" in response.json()["detail"]
        assert str(attacker) not in response.text
        assert (real.connections, evil.connections) == (0, 0)


def test_the_socket_directory_mode_rule_is_exactly_0700(world: World, inbox: FakeInbox) -> None:
    assert stat.S_IMODE(os.lstat(world.socks).st_mode) == 0o700
