"""
Bucket B — async shell (AppleTv) integration tests with a fake pyatv.

Verifies that the shell honours the machine's actions and the I/O-side invariants of
spec 001 (INV-3/INV-7/INV-8/INV-9, failure modes F3/F4/F8/F9). pyatv scan/connect are
monkeypatched to return canned fakes; no network, no real Apple TV.

:copyright: (c) 2026 by Unfolded Circle ApS.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pyatv.exceptions
import pytest
from ucapi import StatusCodes

from config import AtvDevice
from connection_machine import ConnectionState, Event
import tv
from tv import EVENTS, AppleTv

IDENTIFIER = "fake-id"
DEVICE_NAME = "Fake Apple TV"


class FakePushUpdater:
    """Stand-in for pyatv's push updater."""

    def __init__(self, *, fail_start: bool = False) -> None:
        self.listener: Any = None
        self.started = False
        self._fail_start = fail_start

    def start(self) -> None:
        """Start push updates; optionally simulate a blocked facade (F4)."""
        if self._fail_start:
            raise pyatv.exceptions.BlockedStateError("facade is blocked")
        self.started = True

    def stop(self) -> None:
        """Stop push updates."""
        self.started = False


class FakeFeatures:
    """Stand-in for pyatv's feature interface: nothing is available."""

    def in_state(self, *_args: Any) -> bool:
        """Report every feature as unavailable."""
        return False


class FakeAtv:
    """Minimal stand-in for a connected pyatv.interface.AppleTV handle."""

    def __init__(self, *, fail_adoption: bool = False, fire_closed_on_close: bool = False) -> None:
        self.listener: Any = None
        self.push_updater = FakePushUpdater(fail_start=fail_adoption)
        self.audio = SimpleNamespace(listener=None, output_devices=[])
        self.features = FakeFeatures()
        self.device_info = None
        self.close_calls = 0
        self.fire_closed_on_close = fire_closed_on_close

    def close(self) -> None:
        """Close the handle; optionally re-enter via the sync connection_closed callback (F3)."""
        self.close_calls += 1
        if self.fire_closed_on_close and self.listener is not None:
            self.listener.connection_closed()


class FakeConf:
    """Minimal stand-in for pyatv.interface.BaseConfig."""

    name = DEVICE_NAME
    identifier = IDENTIFIER
    all_identifiers = (IDENTIFIER,)

    def get_service(self, _protocol: Any) -> Any:
        """No services configured (tests use empty credentials)."""
        return None

    def set_credentials(self, _protocol: Any, _credentials: str) -> None:
        """Ignore credentials."""


class PyatvStub:
    """Replaces pyatv.scan / pyatv.connect with controllable fakes."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.conf: FakeConf | None = FakeConf()
        self.handles: list[FakeAtv] = []
        self.connect_gate: asyncio.Event | None = None
        self.atv_at_connect: list[Any] = []
        self.device: AppleTv | None = None
        self.make_handle: Callable[[], FakeAtv] = FakeAtv
        monkeypatch.setattr(tv.pyatv, "scan", self._scan)
        monkeypatch.setattr(tv.pyatv, "connect", self._connect)

    async def _scan(self, _loop: Any, identifier: Any = None, hosts: Any = None, **_kwargs: Any) -> list[Any]:
        del hosts
        if identifier is None:
            # output-device refresh scan: nothing else on the network
            return []
        return [self.conf] if self.conf is not None else []

    async def _connect(self, _conf: Any, _loop: Any) -> FakeAtv:
        if self.device is not None:
            # B-SINGLE-WRITER: capture what the connect cycle sees; must be None (INV-3)
            self.atv_at_connect.append(self.device._atv)
        if self.connect_gate is not None:
            await self.connect_gate.wait()
        handle = self.make_handle()
        self.handles.append(handle)
        return handle


@pytest.fixture
async def stub(monkeypatch: pytest.MonkeyPatch) -> PyatvStub:
    """Provide the patched pyatv layer."""
    return PyatvStub(monkeypatch)


@pytest.fixture
async def device(stub: PyatvStub) -> Any:
    """Provide a fresh AppleTv wired to the fake pyatv; tears down supervisor/polling/tasks."""
    dev = AppleTv(AtvDevice(identifier=IDENTIFIER, name=DEVICE_NAME, credentials=[]))
    stub.device = dev
    yield dev
    await dev._stop_supervisor()
    await dev._stop_polling()
    for task in list(dev._background_tasks):
        task.cancel()


def record_events(dev: AppleTv) -> dict[EVENTS, list[Any]]:
    """Record emitted driver events per type."""
    recorded: dict[EVENTS, list[Any]] = {EVENTS.CONNECTED: [], EVENTS.DISCONNECTED: [], EVENTS.ERROR: []}
    dev.events.on(EVENTS.CONNECTED, recorded[EVENTS.CONNECTED].append)
    dev.events.on(EVENTS.DISCONNECTED, recorded[EVENTS.DISCONNECTED].append)
    dev.events.on(EVENTS.ERROR, lambda ident, msg: recorded[EVENTS.ERROR].append((ident, msg)))
    return recorded


async def until(predicate: Callable[[], bool], timeout_s: float = 2.0) -> None:
    """Wait until the predicate holds (bounded)."""
    async with asyncio.timeout(timeout_s):
        while not predicate():
            await asyncio.sleep(0.01)


async def test_b_wire(device: AppleTv, stub: PyatvStub) -> None:
    """B-WIRE: happy connect adopts once, wires listeners, starts polling, emits one CONNECTED."""
    recorded = record_events(device)
    await device.connect()
    assert await device.wait_for_state({ConnectionState.CONNECTED}, timeout_s=2.0)

    handle = stub.handles[0]
    assert device._atv is handle
    assert handle.listener is device
    assert handle.push_updater.listener is device
    assert handle.push_updater.started
    assert handle.audio.listener is device
    assert device._polling is not None
    assert recorded[EVENTS.CONNECTED] == [IDENTIFIER]
    assert recorded[EVENTS.DISCONNECTED] == []


async def test_b_single_writer(device: AppleTv, stub: PyatvStub) -> None:
    """B-SINGLE-WRITER: the connect cycle never assigns `_atv`; only the supervisor adopts (INV-3)."""
    await device.connect()
    assert await device.wait_for_state({ConnectionState.CONNECTED}, timeout_s=2.0)
    # what pyatv.connect saw while running inside the connect task
    assert stub.atv_at_connect == [None]
    assert device._atv is stub.handles[0]


async def test_b_orphan(device: AppleTv, stub: PyatvStub) -> None:
    """B-ORPHAN: a late CONNECT_SUCCEEDED after STOP is orphan-disposed, `_atv` stays None (F9, INV-8)."""
    stub.connect_gate = asyncio.Event()  # keep the connect attempt in flight forever
    await device.connect()
    device._post(Event.STOP)
    assert await device.wait_for_state({ConnectionState.STOPPED}, timeout_s=2.0)

    orphan = FakeAtv()
    device._post(Event.CONNECT_SUCCEEDED, orphan)  # pyright: ignore[reportArgumentType]
    await until(lambda: orphan.close_calls == 1)
    assert device._atv is None
    assert device.state is ConnectionState.STOPPED


async def test_b_reentrant_close(device: AppleTv, stub: PyatvStub) -> None:
    """B-REENTRANT: close() firing connection_closed synchronously causes no recursion (F3, INV-7)."""
    stub.make_handle = lambda: FakeAtv(fire_closed_on_close=True)
    recorded = record_events(device)
    await device.connect()
    assert await device.wait_for_state({ConnectionState.CONNECTED}, timeout_s=2.0)
    first = stub.handles[0]

    # physical drop reported by pyatv
    device.connection_lost(Exception("drop"))
    # teardown closes the handle exactly once; the sync re-entrant callback is a queued no-op
    await until(lambda: first.close_calls == 1)
    # the machine reconnects; a second handle is adopted
    await until(lambda: len(stub.handles) == 2 and device._atv is stub.handles[1])
    assert first.close_calls == 1
    assert recorded[EVENTS.DISCONNECTED] == [IDENTIFIER]
    assert recorded[EVENTS.CONNECTED] == [IDENTIFIER, IDENTIFIER]


async def test_b_adopt_fail(device: AppleTv, stub: PyatvStub) -> None:
    """B-ADOPT-FAIL: adoption failure (blocked facade) triggers teardown + reconnect (F4)."""
    fail_first = True

    def make_handle() -> FakeAtv:
        nonlocal fail_first
        handle = FakeAtv(fail_adoption=fail_first)
        fail_first = False
        return handle

    stub.make_handle = make_handle
    recorded = record_events(device)
    await device.connect()

    # first handle fails adoption and is closed by the machine's TEARDOWN; second connects clean
    await until(lambda: len(stub.handles) == 2 and device._atv is stub.handles[1])
    assert stub.handles[0].close_calls == 1
    # no CONNECTED was emitted for the failed adoption (EMIT_CONNECTED is ordered after ADOPT_CONNECTION)
    assert recorded[EVENTS.CONNECTED] == [IDENTIFIER]
    assert device.state is ConnectionState.CONNECTED


async def test_b_cmd_wait(device: AppleTv, stub: PyatvStub) -> None:
    """B-CMD-WAIT: a command while disconnected connects first and succeeds (F8)."""

    @tv.async_handle_atvlib_errors
    async def fake_command(self: AppleTv) -> StatusCodes:
        assert self._atv is not None
        return StatusCodes.OK

    assert device.state is ConnectionState.STOPPED
    result = await fake_command(device)
    assert result == StatusCodes.OK
    assert device.state is ConnectionState.CONNECTED
    assert stub.handles  # a real (fake) connection was established for the command


async def test_b_cmd_503(device: AppleTv, stub: PyatvStub, monkeypatch: pytest.MonkeyPatch) -> None:
    """B-CMD-503: a command that cannot connect returns SERVICE_UNAVAILABLE without blocking past the window."""
    monkeypatch.setattr(tv, "CONNECT_WAIT_FOR_COMMAND", 0.2)
    stub.conf = None  # scan never finds the device

    @tv.async_handle_atvlib_errors
    async def fake_command(_self: AppleTv) -> StatusCodes:
        return StatusCodes.OK

    result = await fake_command(device)
    assert result == StatusCodes.SERVICE_UNAVAILABLE
    assert device._atv is None


async def test_b_disconnect_await(device: AppleTv, stub: PyatvStub) -> None:
    """B-DISCONNECT-AWAIT: disconnect() returns only after STOPPED; handle closed; supervisor gone."""
    recorded = record_events(device)
    await device.connect()
    assert await device.wait_for_state({ConnectionState.CONNECTED}, timeout_s=2.0)

    await device.disconnect()
    assert device.state is ConnectionState.STOPPED
    assert device._atv is None
    assert stub.handles[0].close_calls == 1
    assert device._supervisor is None  # INV-9: no leaked task
    assert recorded[EVENTS.DISCONNECTED] == [IDENTIFIER]


async def test_b_shutdown_drains_queue(device: AppleTv, stub: PyatvStub) -> None:
    """B-SHUTDOWN: supervisor termination orphan-disposes a queued late CONNECT_SUCCEEDED (INV-8, INV-9)."""
    stub.connect_gate = asyncio.Event()  # connect attempt stays in flight
    await device.connect()
    device._post(Event.STOP)
    assert await device.wait_for_state({ConnectionState.STOPPED}, timeout_s=2.0)

    # a late success lands in the queue just as the supervisor is being stopped
    late = FakeAtv()
    device._post(Event.CONNECT_SUCCEEDED, late)  # pyright: ignore[reportArgumentType]
    await device._stop_supervisor()

    assert late.close_calls == 1
    assert device._atv is None
    assert device._supervisor is None
    assert device._event_queue.empty()


async def test_b_no_supervisor(stub: PyatvStub) -> None:
    """B-NO-SUPERVISOR: disconnect() on a never-connected (pairing-only) instance returns immediately."""
    del stub
    dev = AppleTv(AtvDevice(identifier=IDENTIFIER, name=DEVICE_NAME, credentials=[]))
    async with asyncio.timeout(1.0):  # far below STOP_TIMEOUT: must not wait for the state machine
        await dev.disconnect()
    assert dev._supervisor is None
    assert dev.state is ConnectionState.STOPPED


async def test_b_refresh_bootstrap(device: AppleTv, stub: PyatvStub) -> None:
    """B-REFRESH-BOOTSTRAP: reconnect re-arms the app-list latch and refresh timers (issue 6 preserved)."""
    await device.connect()
    assert await device.wait_for_state({ConnectionState.CONNECTED}, timeout_s=2.0)

    # simulate a device that latched "not supported" and stale timers during the previous connection
    device._app_list_supported = False
    device._next_app_list_refresh = 0.0
    device._next_output_refresh = 0.0

    device.connection_lost(Exception("drop"))
    await until(lambda: len(stub.handles) == 2 and device._atv is stub.handles[1])

    assert device._app_list_supported is True
    assert device._next_app_list_refresh > 0.0
    assert device._next_output_refresh > 0.0
