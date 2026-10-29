"""
Tests for sending the pyatv connect requests without waiting for each reply.

:copyright: (c) 2026 by Unfolded Circle ApS.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

# ruff: noqa: SLF001  # tests access private members of the patched classes
# pyright: reportPrivateUsage=false

import asyncio
import plistlib
from types import SimpleNamespace
from typing import Any

import pyatv
from pyatv.protocols.mrp import protobuf
from pyatv.protocols.mrp.protocol import ProtocolState
from pyatv.settings import Settings
import pytest

import monkey_patch


class Requests:
    """Records sent requests and how many requests were sent when each reply arrived."""

    def __init__(self) -> None:
        """Create an empty record."""
        self.sent: list[Any] = []
        self.sent_at_reply: list[int] = []

    async def request(self, name: Any) -> None:
        """Send a request and wait for its reply."""
        self.sent.append(name)
        await asyncio.sleep(0)
        self.sent_at_reply.append(len(self.sent))


# --- patched_companion_api_connect ---


class CompanionApiFake:
    """CompanionAPI with already connected protocol and recorded setup commands."""

    def __init__(self, requests: Requests) -> None:
        """Create a fake that records commands in `requests`."""
        self._protocol = None
        self.core = SimpleNamespace(
            loop=asyncio.get_running_loop(),
            config=SimpleNamespace(address="127.0.0.1"),
            service=SimpleNamespace(port=49153),
            device_listener=None,
        )
        self.requests = requests

    async def system_info(self) -> None:
        """Send system info."""
        await self.requests.request("_systemInfo")

    async def _touch_start(self) -> None:
        await self.requests.request("_touchStart")

    async def _session_start(self) -> None:
        await self.requests.request("_sessionStart")

    async def _tv_rc_session_start(self) -> None:
        await self.requests.request("_tvrcSessionStart")

    async def _text_input_start(self) -> None:
        await self.requests.request("_tiStart")

    async def subscribe_event(self, event: str) -> None:
        """Subscribe to an event."""
        await self.requests.request(event)


class CompanionConnectionFake:
    """CompanionConnection that is not used."""

    def __init__(self, *_args: Any) -> None:
        """Create a connection."""


class CompanionProtocolFake:
    """CompanionProtocol that connects at once."""

    def __init__(self, *_args: Any) -> None:
        """Create a protocol."""
        self.listener = None

    async def start(self) -> None:
        """Connect and verify."""


@pytest.mark.asyncio
async def test_companion_sends_setup_commands_before_replies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(monkey_patch, "CompanionConnection", CompanionConnectionFake)
    monkeypatch.setattr(monkey_patch, "CompanionProtocol", CompanionProtocolFake)
    requests = Requests()

    await monkey_patch.patched_companion_api_connect(CompanionApiFake(requests))  # pyright: ignore[reportArgumentType]

    assert requests.sent == ["_systemInfo", "_touchStart", "_sessionStart", "_tvrcSessionStart", "_tiStart", "_iMC"]
    # _iMC is sent after the other replies
    assert requests.sent_at_reply == [5, 5, 5, 5, 5, 6]


# --- patched_mrp_protocol_start ---


class MrpProtocolFake:
    """MrpProtocol with recorded messages."""

    def __init__(self, requests: Requests) -> None:
        """Create a fake that records messages in `requests`."""
        self._state = ProtocolState.NOT_CONNECTED
        self.connection = SimpleNamespace(connect=self._connect)
        self.service = SimpleNamespace(credentials=None)
        self.srp = SimpleNamespace(pairing_id=b"pairing id")
        self.info = Settings().info
        self.device_info = None
        self.requests = requests
        self.stopped = False

    async def _connect(self) -> None:
        pass

    async def _enable_encryption(self) -> None:
        pass

    async def send(self, message: Any) -> None:
        """Send a message without reply."""
        self.requests.sent.append(message.type)

    async def send_and_receive(self, message: Any) -> Any:
        """Send a message and wait for its reply."""
        await self.requests.request(message.type)
        return message

    def dispatch(self, *_args: Any) -> None:
        """Send a message to the listeners."""

    def stop(self) -> None:
        """Close the connection."""
        self.stopped = True


@pytest.mark.asyncio
async def test_mrp_sends_last_messages_before_replies() -> None:
    requests = Requests()
    protocol = MrpProtocolFake(requests)

    await monkey_patch.patched_mrp_protocol_start(protocol)  # pyright: ignore[reportArgumentType]

    assert requests.sent == [
        protobuf.DEVICE_INFO_MESSAGE,
        protobuf.SET_CONNECTION_STATE_MESSAGE,
        protobuf.CLIENT_UPDATES_CONFIG_MESSAGE,
        protobuf.GET_KEYBOARD_SESSION_MESSAGE,
    ]
    assert requests.sent_at_reply == [1, 4, 4]
    assert protocol._state == ProtocolState.READY


@pytest.mark.asyncio
async def test_mrp_stops_when_message_fails() -> None:
    protocol = MrpProtocolFake(Requests())

    async def send_and_receive(message: Any) -> Any:
        if message.type == protobuf.GET_KEYBOARD_SESSION_MESSAGE:
            msg = "no reply"
            raise pyatv.exceptions.ProtocolError(msg)
        return message

    protocol.send_and_receive = send_and_receive

    with pytest.raises(pyatv.exceptions.ProtocolError):
        await monkey_patch.patched_mrp_protocol_start(protocol)  # pyright: ignore[reportArgumentType]
    assert protocol.stopped


# --- patched_ap2_session_setup_remote_control ---


class RtspFake:
    """RTSP session with recorded requests."""

    def __init__(self, requests: Requests) -> None:
        """Create a fake that records requests in `requests`."""
        self.requests = requests

    async def setup(self, body: dict[str, Any]) -> Any:
        """Send SETUP."""
        if "streams" in body:
            await self.requests.request("SETUP streams")
            return SimpleNamespace(body=plistlib.dumps({"streams": [{"dataPort": 2}]}, fmt=plistlib.FMT_BINARY))
        await self.requests.request("SETUP event")
        return SimpleNamespace(body=plistlib.dumps({"eventPort": 1}, fmt=plistlib.FMT_BINARY))

    async def record(self) -> None:
        """Send RECORD."""
        await self.requests.request("RECORD")


def _ap2_session(requests: Requests) -> Any:
    info = SimpleNamespace(
        os_name="iPhone OS", model="iPhone14,3", device_id="id", os_version="17", os_build="21A", mac="mac", name="n"
    )
    return SimpleNamespace(
        connection=SimpleNamespace(remote_ip="127.0.0.1"),
        rtsp=RtspFake(requests),
        verifier=object(),
        _info=info,
        _channels=[],
        data_channel=None,
    )


@pytest.mark.asyncio
async def test_airplay_connects_event_channel_before_record(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = Requests()

    async def setup_channel(_factory: Any, _verifier: Any, _address: str, port: int, *_args: Any) -> tuple[Any, Any]:
        requests.sent.append(f"channel {port}")
        return f"transport {port}", f"channel {port}"

    monkeypatch.setattr(monkey_patch, "setup_channel", setup_channel)
    session = _ap2_session(requests)

    await monkey_patch.patched_ap2_session_setup_remote_control(session)

    # The Apple TV replies to RECORD only after the event channel is connected
    assert requests.sent == ["SETUP event", "channel 1", "RECORD", "SETUP streams", "channel 2"]
    # RECORD and SETUP streams are sent before the replies
    assert requests.sent_at_reply == [1, 4, 4]
    assert session._channels == ["transport 1", "transport 2"]
    assert session.data_channel == "channel 2"


@pytest.mark.asyncio
async def test_airplay_keeps_event_channel_when_data_channel_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    async def setup_channel(_factory: Any, _verifier: Any, _address: str, port: int, *_args: Any) -> tuple[Any, Any]:
        if port == 2:
            raise ConnectionRefusedError
        return f"transport {port}", f"channel {port}"

    monkeypatch.setattr(monkey_patch, "setup_channel", setup_channel)
    session = _ap2_session(Requests())

    with pytest.raises(ConnectionRefusedError):
        await monkey_patch.patched_ap2_session_setup_remote_control(session)
    assert session._channels == ["transport 1"]
    assert session.data_channel is None
