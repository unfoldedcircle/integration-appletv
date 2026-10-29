#!/usr/bin/env python3
"""
This module handles monkey patching of the pyatv library.

:copyright: (c) 2023-2024 by Unfolded Circle ApS.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""
# pyright: reportPrivateUsage=false

import asyncio
from collections.abc import Callable
from contextvars import ContextVar
from copy import copy
import logging
from random import randint
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from pyatv import exceptions
from pyatv.auth import hap_tlv8
from pyatv.auth.hap_channel import setup_channel
from pyatv.auth.hap_pairing import HapCredentials, PairSetupProcedure, parse_credentials
from pyatv.auth.hap_srp import SRPAuthHandler
from pyatv.core import SetupData
from pyatv.core.facade import FacadeAppleTV
from pyatv.protocols.airplay.ap2_session import (
    DATASTREAM_INPUT_INFO,
    DATASTREAM_OUTPUT_INFO,
    DATASTREAM_SALT,
    EVENTS_READ_INFO,
    EVENTS_SALT,
    EVENTS_WRITE_INFO,
    AP2Session,
)
from pyatv.protocols.airplay.auth.hap import _AIRPLAY_HEADERS, AirPlayHapPairSetupProcedure, _get_pairing_data
from pyatv.protocols.airplay.auth.legacy import AirPlayLegacyPairSetupProcedure
from pyatv.protocols.airplay.channels import DataStreamChannel, EventChannel
from pyatv.protocols.airplay.pairing import AirPlayMajorVersion, AirPlayPairingHandler, AuthenticationType
from pyatv.protocols.airplay.srp import LegacySRPAuthHandler, new_credentials
from pyatv.protocols.companion.api import CompanionAPI
from pyatv.protocols.companion.connection import CompanionConnection
from pyatv.protocols.companion.protocol import CompanionProtocol
from pyatv.protocols.mrp import messages, protobuf
from pyatv.protocols.mrp.protocol import MrpProtocol, ProtocolState
from pyatv.support import error_handler
from pyatv.support.http import HttpConnection, decode_bplist_from_body, http_connect

if TYPE_CHECKING:
    from pyatv.const import Protocol

_LOG = logging.getLogger(__name__)
HapPairSetupProcedureFactory = Callable[[HttpConnection, SRPAuthHandler, str | None], AirPlayHapPairSetupProcedure]

_original_facade_add_protocol = FacadeAppleTV.add_protocol
_original_facade_connect = FacadeAppleTV.connect
_close_tasks: set[asyncio.Task[Any]] = set()

connect_progress: ContextVar[Callable[[], None] | None] = ContextVar("connect_progress", default=None)
"""Called when a connect step is done. Set it before the connect task is created."""


def _report_progress() -> None:
    if (callback := connect_progress.get()) is not None:
        callback()


def patched_airplay_hap_pair_setup(
    auth_type: AuthenticationType,
    connection: HttpConnection,
    display_name: str | None = None,
) -> PairSetupProcedure:
    """Return Pair-Setup procedure with an optional receiver-visible name."""
    _LOG.debug("Setting up new AirPlay Pair-Setup procedure with type %s", auth_type)

    if auth_type == AuthenticationType.Legacy:
        legacy_srp = LegacySRPAuthHandler(new_credentials())
        legacy_srp.initialize()
        return AirPlayLegacyPairSetupProcedure(connection, legacy_srp)
    if auth_type == AuthenticationType.HAP:
        srp = SRPAuthHandler()
        srp.initialize()
        hap_pair_setup_procedure = cast("HapPairSetupProcedureFactory", AirPlayHapPairSetupProcedure)
        return hap_pair_setup_procedure(connection, srp, display_name)

    msg = f"authentication type {auth_type} does not support Pair-Setup"
    raise exceptions.NotSupportedError(msg)


def patched_airplay_hap_pair_setup_procedure_init(
    self: AirPlayHapPairSetupProcedure,
    http: HttpConnection,
    auth_handler: SRPAuthHandler,
    display_name: str | None = None,
):
    """Initialize HAP pairing with an optional receiver-visible name."""
    self.http = http
    self.srp = auth_handler
    headers = copy(_AIRPLAY_HEADERS)
    if display_name:
        headers["X-Apple-Client-Name"] = display_name
    setup = cast("Any", self)
    setup._headers = headers  # noqa: SLF001
    self._atv_salt = None
    self._atv_pub_key = None


async def patched_airplay_hap_pair_setup_procedure_start_pairing(self: AirPlayHapPairSetupProcedure) -> None:
    """Start the authentication process.

    This method will show the expected PIN on screen.
    """
    self.srp.initialize()
    headers = cast("Any", self)._headers  # noqa: SLF001

    await self.http.post("/pair-pin-start", headers=headers)

    data = {hap_tlv8.TlvValue.Method: b"\x00", hap_tlv8.TlvValue.SeqNo: b"\x01"}
    resp = await self.http.post("/pair-setup", body=hap_tlv8.write_tlv(data), headers=headers)
    pairing_data = _get_pairing_data(resp)

    self._atv_salt = pairing_data[hap_tlv8.TlvValue.Salt]
    self._atv_pub_key = pairing_data[hap_tlv8.TlvValue.PublicKey]


async def patched_airplay_hap_pair_setup_procedure_finish_pairing(
    self: AirPlayHapPairSetupProcedure,
    username: str,  # noqa: ARG001
    pin_code: int,
    display_name: str | None,
) -> HapCredentials:
    """Finish authentication process.

    A username (generated by new_credentials) and the PIN code shown on
    screen must be provided.
    """
    # Step 1
    self.srp.step1(pin_code)
    headers = cast("Any", self)._headers  # noqa: SLF001

    pub_key, proof = self.srp.step2(self._atv_pub_key, self._atv_salt)
    data = {
        hap_tlv8.TlvValue.SeqNo: b"\x03",
        hap_tlv8.TlvValue.PublicKey: pub_key,
        hap_tlv8.TlvValue.Proof: proof,
    }
    await self.http.post("/pair-setup", body=hap_tlv8.write_tlv(data), headers=headers)

    data = {
        hap_tlv8.TlvValue.SeqNo: b"\x05",
        hap_tlv8.TlvValue.EncryptedData: self.srp.step3(name=display_name),
    }
    resp = await self.http.post("/pair-setup", body=hap_tlv8.write_tlv(data), headers=headers)
    pairing_data = _get_pairing_data(resp)

    encrypted_data = pairing_data[hap_tlv8.TlvValue.EncryptedData]
    return self.srp.step4(encrypted_data)


async def patched_airplay_pairing_begin(self: AirPlayPairingHandler) -> None:
    """Start pairing process."""
    self.http = await http_connect(self.address, self.service.port)
    self.pairing_procedure = patched_airplay_hap_pair_setup(
        (
            AuthenticationType.HAP
            if self.airplay_version == AirPlayMajorVersion.AirPlayV2
            else AuthenticationType.Legacy
        ),
        self.http,
        self._name,
    )
    self._has_paired = False
    return await error_handler(self.pairing_procedure.start_pairing, exceptions.PairingError)


def _keep_close_tasks(tasks: set[asyncio.Task[Any]]) -> None:
    """Keep a reference to the close tasks until they are done, so they are not garbage collected."""
    for task in tasks:
        _close_tasks.add(task)
        task.add_done_callback(_close_tasks.discard)


def patched_facade_add_protocol(self: FacadeAppleTV, setup_data: SetupData) -> None:
    """Add a protocol. Close the protocol if its connect fails or is canceled.

    Without this, a failed connect leaves the connection open, e.g. the AirPlay remote control session on the Apple TV.
    """
    connect = setup_data.connect
    close = setup_data.close

    async def _connect() -> bool:
        loop = asyncio.get_running_loop()
        start = loop.time()
        try:
            result = await connect()
        except BaseException:
            _keep_close_tasks(close())
            raise
        _report_progress()
        _LOG.debug("Connected to %s in %.2fs", setup_data.protocol.name, loop.time() - start)
        return result

    _original_facade_add_protocol(self, setup_data._replace(connect=_connect))


def _retrieve_exception(task: asyncio.Task[bool]) -> None:
    """Mark the exception as retrieved. pyatv only awaits the protocols up to the first failure."""
    if not task.cancelled():
        task.exception()


def _start_connects(atv: FacadeAppleTV) -> list[tuple[SetupData, asyncio.Task[bool]]]:
    """Start the connect of all protocols at the same time.

    pyatv connects one protocol after the other. The queued protocols get the started task as connect,
    so pyatv still registers them in the same order.
    """
    queue = atv._protocols_to_setup  # noqa: SLF001
    queued: list[SetupData] = []
    started: list[tuple[SetupData, asyncio.Task[bool]]] = []
    protocols: set[Protocol] = set()
    while not queue.empty():
        setup_data = queue.get()
        # pyatv only connects a duplicate protocol if the first one did not connect. Keep that order for duplicates.
        if setup_data.protocol not in protocols:
            protocols.add(setup_data.protocol)
            task = asyncio.ensure_future(setup_data.connect())
            task.add_done_callback(_retrieve_exception)
            started.append((setup_data, task))
            setup_data = setup_data._replace(connect=lambda task=task: task)
        queued.append(setup_data)
    for setup_data in queued:
        queue.put(setup_data)
    return started


def _close_started(atv: FacadeAppleTV, started: list[tuple[SetupData, asyncio.Task[bool]]]) -> None:
    """Stop the started protocols that pyatv has not registered."""
    for setup_data, task in started:
        if not task.done():
            # The connect wrapper of patched_facade_add_protocol closes the protocol
            task.cancel()
        elif (
            not task.cancelled()
            and task.exception() is None
            and task.result()
            and setup_data.protocol not in atv._protocol_handlers  # noqa: SLF001
        ):
            _keep_close_tasks(setup_data.close())


async def patched_facade_connect(self: FacadeAppleTV) -> None:
    """Connect all protocols at the same time. Close the connected protocols if connect fails or is canceled."""
    started = _start_connects(self)
    try:
        await _original_facade_connect(self)
    except BaseException:
        _close_started(self, started)
        _keep_close_tasks(self.close())
        raise


async def patched_companion_api_connect(self: CompanionAPI) -> None:
    """Connect to the Apple TV. Send the setup commands without waiting for each reply.

    The commands are sent in the same order as in pyatv. The replies are matched by transaction id.
    """
    if self._protocol:
        return

    _LOG.debug("Connect to Companion from API")
    self._connection = CompanionConnection(
        self.core.loop,
        str(self.core.config.address),
        self.core.service.port,
        self.core.device_listener,
    )
    self._protocol = CompanionProtocol(self._connection, SRPAuthHandler(), self.core.service)
    self._protocol.listener = self

    loop = asyncio.get_running_loop()
    start = loop.time()
    await self._protocol.start()
    verified = loop.time()
    _report_progress()

    await asyncio.gather(
        self.system_info(),
        self._touch_start(),
        self._session_start(),
        self._tv_rc_session_start(),
        self._text_input_start(),
    )
    await self.subscribe_event("_iMC")
    _LOG.debug("Companion: verified in %.2fs, setup in %.2fs", verified - start, loop.time() - verified)


async def patched_mrp_protocol_start(self: MrpProtocol, skip_initial_messages: bool = False) -> None:  # noqa: FBT001, FBT002 same signature as pyatv
    """Connect to the Apple TV. Send the last two setup messages without waiting for each reply.

    The messages are sent in the same order as in pyatv. The replies are matched by identifier.
    """
    if self._state != ProtocolState.NOT_CONNECTED:
        raise exceptions.InvalidStateError(self._state.name)

    self._state = ProtocolState.CONNECTING
    loop = asyncio.get_running_loop()
    start = loop.time()

    try:
        await self.connection.connect()
        _report_progress()

        self._state = ProtocolState.CONNECTED

        # Credentials from outside of pairing: use their client id
        if self.service.credentials:
            self.srp.pairing_id = parse_credentials(self.service.credentials).client_id

        # The first message must always be DEVICE_INFORMATION, otherwise the device does not respond
        self.device_info = await self.send_and_receive(
            messages.device_information(self.info, self.srp.pairing_id.decode())
        )
        _report_progress()
        # send_and_receive does not send the device information to the listeners
        self.dispatch(protobuf.DEVICE_INFO_MESSAGE, self.device_info)

        if skip_initial_messages:
            return

        await error_handler(self._enable_encryption, exceptions.AuthenticationError)
        _report_progress()

        # First message after encryption is enabled
        await self.send(messages.set_connection_state())

        await asyncio.gather(
            self.send_and_receive(messages.client_updates_config()),
            self.send_and_receive(messages.get_keyboard_session()),
        )
    except Exception:
        self.stop()
        raise

    self._state = ProtocolState.READY
    _LOG.debug("MRP: started in %.2fs", loop.time() - start)


async def patched_ap2_session_setup_remote_control(self: AP2Session) -> None:
    """Set up the remote control session. Send RECORD and the data stream SETUP without waiting for each reply.

    The Apple TV replies to RECORD only after the event channel is connected.
    """
    if self.connection is None or self.rtsp is None or self.verifier is None:
        msg = "not connected to remote"
        raise exceptions.InvalidStateError(msg)

    loop = asyncio.get_running_loop()
    start = loop.time()
    address = self.connection.remote_ip
    # A 64 bit random seed is part of the data channel encryption salt
    seed = randint(0, 2**64)  # noqa: S311 same as pyatv, not a secret

    try:
        event_resp = await self.rtsp.setup(
            body={
                "isRemoteControlOnly": True,
                "osName": self._info.os_name,
                "sourceVersion": "550.10",
                "timingProtocol": "None",
                "model": self._info.model,
                "deviceID": self._info.device_id,
                "osVersion": self._info.os_version,
                "osBuildVersion": self._info.os_build,
                "macAddress": self._info.mac,
                "sessionUUID": str(uuid4()).upper(),
                "name": self._info.name,
            }
        )
        # The event channel is not used, but it must be set up.
        # Read and write info are swapped: the receiver starts the event connection.
        event_transport, _ = await setup_channel(
            EventChannel,
            self.verifier,
            address,
            decode_bplist_from_body(event_resp)["eventPort"],
            EVENTS_SALT,
            EVENTS_READ_INFO,
            EVENTS_WRITE_INFO,
        )
        self._channels.append(event_transport)
        event_done = loop.time()
        _report_progress()

        _, data_resp = await asyncio.gather(
            self.rtsp.record(),
            self.rtsp.setup(
                body={
                    "streams": [
                        {
                            "controlType": 2,
                            "channelID": str(uuid4()).upper(),
                            "seed": seed,
                            "clientUUID": str(uuid4()).upper(),
                            "type": 130,
                            "wantsDedicatedSocket": True,
                            "clientTypeUUID": "1910A70F-DBC0-4242-AF95-115DB30604E1",
                        }
                    ]
                }
            ),
        )
        requested = loop.time()
        _report_progress()

        data_transport, data_channel = await setup_channel(
            DataStreamChannel,
            self.verifier,
            address,
            decode_bplist_from_body(data_resp)["streams"][0]["dataPort"],
            DATASTREAM_SALT + str(seed),
            DATASTREAM_OUTPUT_INFO,
            DATASTREAM_INPUT_INFO,
        )
    except Exception as ex:
        _LOG.warning("AirPlay: remote control setup failed: %r", ex)
        raise
    self._channels.append(data_transport)
    self.data_channel = cast("DataStreamChannel", data_channel)
    _LOG.debug(
        "AirPlay: event channel in %.2fs, RECORD and data SETUP in %.2fs, data channel in %.2fs",
        event_done - start,
        requested - event_done,
        loop.time() - requested,
    )
