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
from pyatv.core import Core, CoreStateDispatcher, SetupData
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


def _tunnel_factory(stub: ProtocolStub) -> Callable[[Core, Any], SetupData]:
    return lambda _core, _credentials: stub.setup_data


@pytest.fixture
def patched_pyatv(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install the pyatv patches for one test."""
    monkeypatch.setattr(FacadeAppleTV, "connect", monkey_patch.patched_facade_connect)


# --- patched_create_mrp_tunnel_data ---


@pytest.mark.asyncio
async def test_tunnel_closed_when_connect_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = ProtocolStub(Protocol.MRP, _connect_fail)
    monkeypatch.setattr(monkey_patch, "_original_create_mrp_tunnel_data", _tunnel_factory(stub))

    setup_data = monkey_patch.patched_create_mrp_tunnel_data(None, None)  # pyright: ignore[reportArgumentType]

    with pytest.raises(pyatv.exceptions.ProtocolError):
        await setup_data.connect()
    assert stub.closed


@pytest.mark.asyncio
async def test_tunnel_not_closed_when_connect_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = ProtocolStub(Protocol.MRP, _connect_ok)
    monkeypatch.setattr(monkey_patch, "_original_create_mrp_tunnel_data", _tunnel_factory(stub))

    setup_data = monkey_patch.patched_create_mrp_tunnel_data(None, None)  # pyright: ignore[reportArgumentType]

    assert await setup_data.connect()
    assert not stub.closed


def test_tunnel_patch_matches_pyatv() -> None:
    """The patch replaces a private pyatv function. Fail if pyatv renames it."""
    import pyatv.protocols.airplay as airplay_proto

    assert callable(airplay_proto._create_mrp_tunnel_data)


# --- patched_facade_connect ---


@pytest.mark.asyncio
@pytest.mark.usefixtures("patched_pyatv")
async def test_facade_closes_connected_protocols_when_connect_fails() -> None:
    mrp = ProtocolStub(Protocol.MRP, _connect_ok)
    companion = ProtocolStub(Protocol.Companion, _connect_fail)
    atv = await _facade(mrp, companion)

    with pytest.raises(pyatv.exceptions.ProtocolError):
        await atv.connect()
    assert mrp.closed


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
    assert mrp.closed


@pytest.mark.asyncio
@pytest.mark.usefixtures("patched_pyatv")
async def test_facade_stays_open_when_connect_succeeds() -> None:
    mrp = ProtocolStub(Protocol.MRP, _connect_ok)
    atv = await _facade(mrp)

    await atv.connect()
    assert not mrp.closed
    atv.close()


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
    assert mrp.closed
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


class AtvStub:
    """Connected pyatv instance with a stub remote control."""

    def __init__(self) -> None:
        """Create a connected instance."""
        self.remote_control = RemoteControlStub()

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
