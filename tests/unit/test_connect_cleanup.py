"""
Tests for closing pyatv connections when a connect fails, and for reconnecting after command timeouts.

:copyright: (c) 2026 by Unfolded Circle ApS.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

# ruff: noqa: SLF001  # tests access private members of the patched classes
# pyright: reportPrivateUsage=false

import asyncio
from collections.abc import Awaitable, Callable
from ipaddress import IPv4Address
from typing import Any

import pyatv
from pyatv.conf import AppleTV, ManualService
from pyatv.const import Protocol
from pyatv.core import CoreStateDispatcher, SetupData
from pyatv.core.facade import FacadeAppleTV
from pyatv.settings import Settings
from pyatv.support import http
import pytest

from config import AtvDevice
import monkey_patch
import tv


class ProtocolStub:
    """Protocol setup data that records when it is closed."""

    def __init__(self, protocol: Protocol, connect: Callable[[], Awaitable[bool]]) -> None:
        """Create a stub for the given protocol."""
        self.closed = False
        self.setup_data = SetupData(protocol, connect, self._close, dict, {}, set())

    def _close(self) -> set[asyncio.Task[Any]]:
        self.closed = True
        return set()


async def _connect_ok() -> bool:
    return True


async def _connect_fail() -> bool:
    msg = "Failed to set up remote control channel"
    raise pyatv.exceptions.ProtocolError(msg)


async def _connect_hang() -> bool:
    await asyncio.sleep(100)
    return True


def _config() -> AppleTV:
    conf = AppleTV(IPv4Address("127.0.0.1"), "Living Room")
    conf.add_service(ManualService("id", Protocol.Companion, 1, {}))
    return conf


async def _facade(*stubs: ProtocolStub) -> FacadeAppleTV:
    atv = FacadeAppleTV(_config(), await http.create_session(), CoreStateDispatcher(), Settings())
    for stub in stubs:
        atv.add_protocol(stub.setup_data)
    return atv


def _apple_tv() -> tv.AppleTv:
    atv = tv.AppleTv(AtvDevice(identifier="id", name="Living Room", credentials=[]))
    atv._is_enabled = False  # no connect loop after a disconnect
    return atv


async def _wait_close_tasks() -> None:
    await asyncio.gather(*monkey_patch._close_tasks)


@pytest.fixture
def patched_pyatv(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install the pyatv patches for one test."""
    monkeypatch.setattr(FacadeAppleTV, "add_protocol", monkey_patch.patched_facade_add_protocol)
    monkeypatch.setattr(FacadeAppleTV, "connect", monkey_patch.patched_facade_connect)


# --- patched_facade_add_protocol and patched_facade_connect ---


@pytest.mark.asyncio
@pytest.mark.usefixtures("patched_pyatv")
async def test_facade_closes_all_protocols_when_connect_fails() -> None:
    mrp = ProtocolStub(Protocol.MRP, _connect_ok)
    companion = ProtocolStub(Protocol.Companion, _connect_fail)
    atv = await _facade(mrp, companion)

    with pytest.raises(pyatv.exceptions.ProtocolError):
        await atv.connect()
    await _wait_close_tasks()
    assert mrp.closed
    assert companion.closed


@pytest.mark.asyncio
@pytest.mark.usefixtures("patched_pyatv")
async def test_facade_closes_failed_first_protocol() -> None:
    """The MRP tunnel fails: it is closed, and also the Companion that connected at the same time."""
    mrp = ProtocolStub(Protocol.MRP, _connect_fail)
    companion = ProtocolStub(Protocol.Companion, _connect_ok)
    atv = await _facade(mrp, companion)

    with pytest.raises(pyatv.exceptions.ProtocolError):
        await atv.connect()
    await _wait_close_tasks()
    assert mrp.closed
    assert companion.closed


@pytest.mark.asyncio
@pytest.mark.usefixtures("patched_pyatv")
async def test_facade_closes_pending_protocols_when_first_fails() -> None:
    mrp = ProtocolStub(Protocol.MRP, _connect_fail)
    companion = ProtocolStub(Protocol.Companion, _connect_hang)
    atv = await _facade(mrp, companion)

    with pytest.raises(pyatv.exceptions.ProtocolError):
        await atv.connect()
    await asyncio.sleep(0.01)  # let the canceled connect finish
    await _wait_close_tasks()
    assert mrp.closed
    assert companion.closed


@pytest.mark.asyncio
@pytest.mark.usefixtures("patched_pyatv")
async def test_facade_connects_protocols_at_same_time() -> None:
    running = 0
    max_running = 0

    async def _connect_slow() -> bool:
        nonlocal running, max_running
        running += 1
        max_running = max(max_running, running)
        await asyncio.sleep(0.01)
        running -= 1
        return True

    mrp = ProtocolStub(Protocol.MRP, _connect_slow)
    companion = ProtocolStub(Protocol.Companion, _connect_slow)
    atv = await _facade(mrp, companion)

    await atv.connect()
    assert max_running == 2
    assert list(atv._protocol_handlers) == [Protocol.MRP, Protocol.Companion]
    await asyncio.gather(*atv.close())


@pytest.mark.asyncio
@pytest.mark.usefixtures("patched_pyatv")
async def test_facade_connects_duplicate_protocol_only_when_first_fails() -> None:
    calls: list[str] = []

    async def _connect_first() -> bool:
        calls.append("first")
        return True

    async def _connect_second() -> bool:
        calls.append("second")
        return True

    first = ProtocolStub(Protocol.MRP, _connect_first)
    second = ProtocolStub(Protocol.MRP, _connect_second)
    atv = await _facade(first, second)

    await atv.connect()
    assert calls == ["first"]
    await asyncio.gather(*atv.close())


@pytest.mark.asyncio
@pytest.mark.usefixtures("patched_pyatv")
async def test_facade_closes_connected_protocols_when_cancelled() -> None:
    mrp = ProtocolStub(Protocol.MRP, _connect_ok)
    companion = ProtocolStub(Protocol.Companion, _connect_hang)
    atv = await _facade(mrp, companion)

    task = asyncio.create_task(atv.connect())
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _wait_close_tasks()
    assert mrp.closed
    assert companion.closed


@pytest.mark.asyncio
@pytest.mark.usefixtures("patched_pyatv")
async def test_facade_stays_open_when_connect_succeeds() -> None:
    mrp = ProtocolStub(Protocol.MRP, _connect_ok)
    atv = await _facade(mrp)

    await atv.connect()
    assert not mrp.closed
    await asyncio.gather(*atv.close())


# --- AppleTv._connect timeout ---


@pytest.mark.asyncio
@pytest.mark.usefixtures("patched_pyatv")
async def test_connect_timeout_closes_connected_protocols(monkeypatch: pytest.MonkeyPatch) -> None:
    mrp = ProtocolStub(Protocol.MRP, _connect_ok)
    companion = ProtocolStub(Protocol.Companion, _connect_hang)
    facade = await _facade(mrp, companion)

    async def connect(*_args: Any) -> FacadeAppleTV:
        await facade.connect()
        return facade

    monkeypatch.setattr(tv.pyatv, "connect", connect)
    monkeypatch.setattr(tv, "CONNECT_TIMEOUT", 0.05)
    atv = _apple_tv()

    with pytest.raises(TimeoutError):
        await atv._connect(_config())
    await asyncio.sleep(0.01)  # let the canceled connect task finish
    await _wait_close_tasks()
    assert mrp.closed
    assert companion.closed
    assert atv._atv is None


@pytest.mark.asyncio
async def test_discard_connect_task_closes_result_of_finished_connect() -> None:
    """Connect finished at the same time as the timeout: the returned instance is closed."""
    mrp = ProtocolStub(Protocol.MRP, _connect_ok)
    facade = await _facade(mrp)
    await facade.connect()

    async def connect() -> FacadeAppleTV:
        return facade

    task = asyncio.create_task(connect())
    await task
    tv.AppleTv._discard_connect_task(task)
    await asyncio.sleep(0)  # let the session close task finish

    assert mrp.closed


# --- Reconnect after command timeouts ---


class RemoteControlStub:
    """Remote control that times out until `responding` is set."""

    def __init__(self) -> None:
        """Create a remote control that times out."""
        self.responding = False

    async def up(self) -> None:
        """Press key up."""
        if not self.responding:
            msg = "no response"
            raise pyatv.exceptions.OperationTimeoutError(msg)


class AudioStub:
    """Audio that does not get a volume change event."""

    async def volume_up(self) -> None:
        """Increase volume."""
        raise TimeoutError


class AtvStub:
    """Connected pyatv instance with a stub remote control."""

    def __init__(self) -> None:
        """Create a connected instance."""
        self.remote_control = RemoteControlStub()
        self.audio = AudioStub()

    def close(self) -> None:
        """Close the connection."""


@pytest.mark.asyncio
async def test_reconnect_after_command_timeouts() -> None:
    atv = _apple_tv()
    atv._atv = AtvStub()  # pyright: ignore[reportAttributeAccessIssue]

    for _ in range(tv.MAX_COMMAND_TIMEOUTS - 1):
        assert await atv.cursor_up() == tv.StatusCodes.TIMEOUT
    assert atv._atv is not None

    await atv.cursor_up()
    assert atv._atv is None


@pytest.mark.asyncio
async def test_successful_command_does_not_reset_timeouts() -> None:
    """A command over another protocol can succeed while the remote control channel is dead."""
    atv = _apple_tv()
    stub = AtvStub()
    atv._atv = stub  # pyright: ignore[reportAttributeAccessIssue]

    await atv.cursor_up()
    stub.remote_control.responding = True
    assert await atv.cursor_up() == tv.StatusCodes.OK
    stub.remote_control.responding = False
    for _ in range(tv.MAX_COMMAND_TIMEOUTS - 1):
        await atv.cursor_up()

    assert atv._atv is None


@pytest.mark.asyncio
async def test_no_reconnect_when_timeouts_are_spread_out(monkeypatch: pytest.MonkeyPatch) -> None:
    atv = _apple_tv()
    atv._atv = AtvStub()  # pyright: ignore[reportAttributeAccessIssue]
    now = [0.0]
    monkeypatch.setattr(atv._loop, "time", lambda: now[0])

    for _ in range(tv.MAX_COMMAND_TIMEOUTS):
        await atv.cursor_up()
        now[0] += tv.COMMAND_TIMEOUT_WINDOW / 2 + 1

    assert atv._atv is not None


@pytest.mark.asyncio
async def test_volume_timeouts_do_not_reconnect() -> None:
    """Volume commands wait for a volume change event, which does not come on every working connection."""
    atv = _apple_tv()
    atv._atv = AtvStub()  # pyright: ignore[reportAttributeAccessIssue]

    for _ in range(tv.MAX_COMMAND_TIMEOUTS):
        assert await atv.volume_up() == tv.StatusCodes.TIMEOUT

    assert atv._atv is not None


@pytest.mark.asyncio
async def test_companion_timeouts_reconnect() -> None:
    """Companion wraps a timeout in ProtocolError."""
    atv = _apple_tv()
    stub = AtvStub()
    atv._atv = stub  # pyright: ignore[reportAttributeAccessIssue]

    async def up() -> None:
        msg = "Command _hidC failed"
        raise pyatv.exceptions.ProtocolError(msg) from TimeoutError()

    stub.remote_control.up = up

    for _ in range(tv.MAX_COMMAND_TIMEOUTS):
        assert await atv.cursor_up() == tv.StatusCodes.TIMEOUT

    assert atv._atv is None


@pytest.mark.asyncio
async def test_other_protocol_errors_do_not_reconnect() -> None:
    atv = _apple_tv()
    stub = AtvStub()
    atv._atv = stub  # pyright: ignore[reportAttributeAccessIssue]

    async def up() -> None:
        msg = "missing content"
        raise pyatv.exceptions.ProtocolError(msg)

    stub.remote_control.up = up

    for _ in range(tv.MAX_COMMAND_TIMEOUTS):
        assert await atv.cursor_up() == tv.StatusCodes.SERVER_ERROR

    assert atv._atv is not None
