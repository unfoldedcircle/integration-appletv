"""
This module implements the Apple TV communication of the Remote Two integration driver.

Uses the [pyatv](https://github.com/postlund/pyatv) library with concepts borrowed from the Home Assistant
[Apple TV integration](https://github.com/postlund/home-assistant/tree/dev/homeassistant/components/apple_tv)

:copyright: (c) 2023-2024 by Unfolded Circle ApS.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import asyncio
from asyncio import AbstractEventLoop, Task
import base64
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import suppress
import datetime
from enum import Enum, StrEnum
from functools import wraps
import hashlib
import itertools
import json
import logging
import random
import re
from typing import TYPE_CHECKING, Any, Concatenate, ParamSpec, TypeVar, cast

import pyatv
from pyatv import interface
from pyatv.const import (
    DeviceState,
    FeatureName,
    FeatureState,
    InputAction,
    MediaType,
    PowerState,
    Protocol,
    RepeatState,
    ShuffleState,
)
from pyatv.interface import BaseConfig, OutputDevice, Playing
from typing_extensions import override

if TYPE_CHECKING:
    from pyatv.core.facade import FacadeAudio, FacadeRemoteControl, FacadeTouchGestures
    from pyatv.protocols.mrp.protocol import MrpProtocol
from pyatv.protocols.companion import CompanionApps
from pyatv.protocols.companion.api import CompanionAPI, MediaControlCommand, SystemStatus
from pyatv.protocols.mrp import MrpAudio, MrpRemoteControl, messages
from pyee.asyncio import AsyncIOEventEmitter
from ucapi import StatusCodes
from ucapi.media_player import Attributes as MediaAttr, MediaContentType, RepeatMode, States as MediaState

from config import AtvDevice, AtvProtocol
from connection_machine import Action, ConnectionMachine, ConnectionState, Event
from utils import replace_bad_chars

_LOG = logging.getLogger(__name__)

ARTWORK_WIDTH = 400
ARTWORK_HEIGHT = 400
ERROR_OS_WAIT = 0.5
CONNECT_TIMEOUT = 15.0
"""Maximum time in seconds to wait for pyatv.connect() to complete, comfortably above pyatv's own protocol timeouts."""
APP_LIST_REFRESH_INTERVAL = 300.0
OUTPUT_REFRESH_INTERVAL = 300.0
CONNECT_WAIT_FOR_COMMAND = 3.0
STOP_TIMEOUT = 5.0
"""Maximum time in seconds disconnect() waits for the state machine to reach STOPPED."""


class EVENTS(StrEnum):
    """Internal driver events."""

    CONNECTED = "CONNECTED"
    """Device connected event. Parameter: device identifier."""
    DISCONNECTED = "DISCONNECTED"
    """Device disconnected event. Parameter: device identifier."""
    ERROR = "ERROR"
    """Device error event. Parameters: device identifier, error message."""
    UPDATE = "UPDATE"
    """Device update event. Parameters: device identifier, update data dict."""


_AppleTvT = TypeVar("_AppleTvT", bound="AppleTv")
_P = ParamSpec("_P")


class PlaybackState(Enum):
    """Playback state for companion protocol."""

    NORMAL = 0
    FAST_FORWARD = 1
    REWIND = 2


MEDIA_STATE_MAPPING: dict[DeviceState, MediaState] = {
    DeviceState.Idle: MediaState.STANDBY,
    DeviceState.Stopped: MediaState.STANDBY,
    DeviceState.Playing: MediaState.PLAYING,
    DeviceState.Paused: MediaState.PAUSED,
    DeviceState.Loading: MediaState.BUFFERING,
    DeviceState.Seeking: MediaState.PLAYING,
}

MEDIA_TYPE_MAPPING = {
    MediaType.Video: MediaContentType.VIDEO,
    MediaType.Unknown: MediaContentType.VIDEO,
    MediaType.Music: MediaContentType.MUSIC,
    MediaType.TV: MediaContentType.TV_SHOW,
}

REPEAT_MAPPING = {RepeatState.Off: RepeatMode.OFF, RepeatState.All: RepeatMode.ALL, RepeatState.Track: RepeatMode.ONE}


def debounce(
    wait: float,
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Coroutine[Any, Any, Task[Any]]]]:
    """Debounce a coroutine method with delay in seconds (per-instance).

    The decorated function must be a method, i.e. its first positional argument is ``self``. The pending task is
    stored on the instance (keyed by the wrapped function's name) rather than in the decorator's closure, so
    multiple instances (e.g. several `AppleTv` devices) each get their own independent debounce timer instead of
    cancelling each other's pending calls.
    """

    def decorator(func: Callable[..., Awaitable[Any]]) -> Callable[..., Coroutine[Any, Any, Task[Any]]]:
        attr = f"_debounce_task_{func.__name__}"

        @wraps(func)
        async def debounced(self: Any, *args: Any, **kwargs: Any) -> Task[Any]:
            existing: Task[Any] | None = getattr(self, attr, None)
            if existing and not existing.done():
                existing.cancel()

            async def call_func() -> None:
                """Call wrapped function."""
                await asyncio.sleep(wait)
                await func(self, *args, **kwargs)

            new_task = asyncio.create_task(call_func())
            setattr(self, attr, new_task)
            return new_task

        return debounced

    return decorator


# Adapted from Home Assistant `asyncLOG_errors` in
# https://github.com/home-assistant/core/blob/fd1f0b0efeb5231d3ee23d1cb2a10cdeff7c23f1/homeassistant/components/denonavr/media_player.py
def async_handle_atvlib_errors(
    func: Callable[Concatenate[_AppleTvT, _P], Awaitable[StatusCodes]],
) -> Callable[Concatenate[_AppleTvT, _P], Coroutine[Any, Any, StatusCodes]]:
    """
    Handle errors when calling commands in the AppleTv class.

    Decorator for the AppleTv class:

    - Check if device is connected (``self._atv`` is set) with auto-reconnect if connection is not enabled.
    - Ensures that a ``StatusCodes`` is returned.
    - Log errors occurred when calling an Apple TV library function.
    - Translate errors into UC status codes to return to the Remote.

    Taken from Home-Assistant
    """

    @wraps(func)
    async def wrapper(self: _AppleTvT, *args: _P.args, **kwargs: _P.kwargs) -> StatusCodes:
        if self._atv is None:  # pyright: ignore[reportPrivateUsage]
            _LOG.debug("[%s] Command wrapper: not connected, requesting reconnect", self.log_id)
            await self.connect()
            # Give an in-flight connection a brief moment so a command right after wake can land.
            await self.wait_for_state({ConnectionState.CONNECTED}, timeout_s=CONNECT_WAIT_FOR_COMMAND)
            if self._atv is None:  # pyright: ignore[reportPrivateUsage]
                return StatusCodes.SERVICE_UNAVAILABLE

        result = StatusCodes.SERVER_ERROR
        try:
            res = await func(self, *args, **kwargs)
            return res or StatusCodes.OK
        except (TimeoutError, pyatv.exceptions.OperationTimeoutError):
            result = StatusCodes.TIMEOUT
            _LOG.warning(
                "[%s] Operation timed out: %s%s",
                self.log_id,
                func.__name__,
                args,
            )
        except (pyatv.exceptions.ConnectionFailedError, pyatv.exceptions.ConnectionLostError) as err:
            result = StatusCodes.SERVICE_UNAVAILABLE
            _LOG.warning("[%s] ATV network error (%s%s): %s", self.log_id, func.__name__, args, err)
            self._post(Event.CONNECTION_LOST)  # pyright: ignore[reportPrivateUsage]
        except pyatv.exceptions.AuthenticationError as err:
            result = StatusCodes.UNAUTHORIZED
            _LOG.warning("[%s] Authentication error (%s%s): %s", self.log_id, func.__name__, args, err)
        except (pyatv.exceptions.NoCredentialsError, pyatv.exceptions.InvalidCredentialsError) as err:
            result = StatusCodes.UNAUTHORIZED
            _LOG.warning("[%s] Credential error (%s%s): %s", self.log_id, func.__name__, args, err)

        except pyatv.exceptions.CommandError as err:
            result = StatusCodes.BAD_REQUEST
            _LOG.error(
                "[%s] Command %s%s failed: %s",
                self.log_id,
                func.__name__,
                args,
                err,
            )
        # pyatv: "Calling public interface methods after disconnecting now results in BlockedStateError being raised"
        # Even though we reconnect after a disconnect notification, this should handle remaining edge cases
        except pyatv.exceptions.BlockedStateError:
            result = StatusCodes.SERVICE_UNAVAILABLE
            _LOG.error("[%s] Command is blocked (%s%s), reconnecting...", self.log_id, func.__name__, args)
            self._post(Event.CONNECTION_LOST)  # pyright: ignore[reportPrivateUsage]
        except Exception as err:  # noqa: BLE001
            _LOG.exception("[%s] Error %s occurred in method %s%s", self.log_id, err, func.__name__, args)

        return result

    return wrapper


ARTWORK_CACHE: dict[str, bytes] = {}
PLAYING_STATE_CACHE: dict[str, int] = {}


class _AdoptFailed(Exception):
    """Adoption of a freshly connected handle failed; the supervisor triggers a reconnect (F4)."""


class AppleTv(interface.AudioListener, interface.DeviceListener):
    """Representing an Apple TV Device."""

    def __init__(
        self,
        device: AtvDevice,
        loop: AbstractEventLoop | None = None,
        pairing_atv: pyatv.interface.BaseConfig | None = None,
    ) -> None:
        """Create instance."""
        self._loop: AbstractEventLoop = loop or asyncio.get_running_loop()
        self.events = AsyncIOEventEmitter(self._loop)
        self._machine = ConnectionMachine()
        """Pure connection lifecycle state machine; owns the connection state (INV-2)."""
        self._event_queue: asyncio.Queue[tuple[Event, pyatv.interface.AppleTV | None]] = asyncio.Queue()
        """Single FIFO queue serializing all lifecycle events; payload only for CONNECT_SUCCEEDED (INV-4)."""
        self._supervisor: Task[None] | None = None
        """Single consumer of the event queue; the only writer of `_atv` (INV-3, INV-9)."""
        self._state_changed: asyncio.Event = asyncio.Event()
        """Pulsed by the supervisor after every applied transition; used by `wait_for_state`."""
        self._retry_task: Task[None] | None = None
        self._atv: pyatv.interface.AppleTV | None = None
        if not device.credentials:
            device.credentials = []
        self._device: AtvDevice = device
        self._connect_task: Task[Any] | None = None
        self._pairing_atv: pyatv.interface.BaseConfig | None = pairing_atv
        self._pairing_process: pyatv.interface.PairingHandler | None = None
        self._polling: Task[Any] | None = None
        self._poll_interval: int = 10
        self._device_state: DeviceState | None = None
        self._app_list: dict[str, str] = {}
        self._app_list_supported: bool = True
        self._next_app_list_refresh: float = 0.0
        self._next_output_refresh: float = 0.0
        self._available_output_devices: dict[str, str] = {}
        self._output_devices: OrderedDict[str, frozenset[str]] = OrderedDict[str, frozenset[str]]()
        self._output_devices[self._device.name] = frozenset()
        self._playback_state = PlaybackState.NORMAL
        self._output_devices_volume: dict[str, float] = {}
        self._volume_level: float = 0.0
        self._apple_tv_conf: pyatv.interface.BaseConfig | None = None
        self._media_content_type = MediaContentType.VIDEO
        self._media_image_url: str | None = None
        self._media_title: str | None = None
        self._media_album: str | None = None
        self._media_artist: str | None = None
        self._media_position: int | None = None
        self._media_duration: int | None = None
        self._media_position_updated_at: datetime.datetime | None = None
        self._repeat = RepeatMode.OFF
        self._shuffle: bool | None = False
        self._source: str | None = None
        self._background_tasks: set[asyncio.Task[Any]] = set()

    def _handle_background_task_done(self, task: asyncio.Task[Any]) -> None:
        """Remove completed background tasks and log any unhandled exception."""
        self._background_tasks.discard(task)
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            logging.getLogger(__name__).exception(
                "Background task failed for %s",
                self.log_id,
                exc_info=exc,
            )

    def _spawn_task(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        """Schedule a fire-and-forget coroutine and keep a strong reference until done."""
        task = self._loop.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._handle_background_task_done)
        return task

    @property
    def device_config(self) -> AtvDevice:
        """Return the device configuration."""
        return self._device

    @property
    def identifier(self) -> str:
        """Return the device identifier."""
        if not self._device.identifier:
            msg = "Instance not initialized, no identifier available"
            raise ValueError(msg)
        return self._device.identifier

    @property
    def log_id(self) -> str:
        """Return a log identifier."""
        return self._device.name or self._device.identifier

    @property
    def name(self) -> str:
        """Return the device name."""
        return self._device.name

    @property
    def address(self) -> str | None:
        """Return the optional device address."""
        return self._device.address

    @property
    def is_enabled(self) -> bool:
        """Return whether the device connection should be kept alive (derived from the state machine)."""
        return self._machine.state is not ConnectionState.STOPPED

    @property
    def state(self) -> ConnectionState:
        """Return the current connection lifecycle state."""
        return self._machine.state

    @property
    def media_state(self) -> MediaState:
        """Return the media-player state."""
        # DeviceState does not contain an OFF state: check power state first
        if self.power_state == PowerState.Off:
            return MediaState.OFF

        # Starting up, set to unavailable
        if self._device_state is None:
            return MediaState.UNAVAILABLE

        return MEDIA_STATE_MAPPING.get(self._device_state, MediaState.UNKNOWN)

    @property
    def media_content_type(self) -> MediaContentType:
        """Return the media content type."""
        return self._media_content_type

    @property
    def media_position_updated_at(self) -> str | None:
        """Return timestamp of urrent media position."""
        if self._media_position_updated_at:
            return self._media_position_updated_at.isoformat()
        return None

    @property
    def output_devices_combinations(self) -> list[str]:
        """Return the list of possible selection (combinations) of output devices."""
        return list(self._output_devices.keys())

    @property
    def output_devices(self) -> str:
        """Return current output device entry name."""
        if self._atv is None or self._atv.audio is None:  # pyright: ignore[reportUnnecessaryComparison]
            return ""
        device_info = self._atv.device_info
        current_id = (
            device_info.output_device_id if device_info is not None else None  # pyright: ignore[reportUnnecessaryComparison]
        )
        active = frozenset(d.identifier for d in self._atv.audio.output_devices if d.identifier != current_id)
        for name, ids in self._output_devices.items():
            if ids == active:
                return name
        return ""

    @property
    def app_name(self) -> str:
        """Return the current app name."""
        app_name = ""
        if self._atv is None:
            return app_name
        try:
            app = self._atv.metadata.app
            if app and app.name:
                app_name = app.name
        except Exception:  # noqa: BLE001
            # Most common exception is pyatv.exceptions.NotSupportedError, but there might be others
            _LOG.exception("[%s] Error getting app name", self.log_id)
        return app_name or ""

    @property
    def app_names(self) -> list[str]:
        """Return app names."""
        return list(self._app_list.keys())

    @property
    def attributes(self) -> dict[str, Any]:
        """Return device attributes."""
        return {
            MediaAttr.STATE: self.media_state,
            # MediaAttr.MUTED: self.is_volume_muted,
            MediaAttr.VOLUME: self._volume_level,
            MediaAttr.MEDIA_TYPE: self.media_content_type,
            MediaAttr.MEDIA_IMAGE_URL: self._media_image_url or "",
            MediaAttr.MEDIA_TITLE: self._media_title or "",
            MediaAttr.MEDIA_ALBUM: self._media_album or "",
            MediaAttr.MEDIA_ARTIST: self._media_artist or "",
            MediaAttr.MEDIA_POSITION: self._media_position or 0,
            MediaAttr.MEDIA_DURATION: self._media_duration or 0,
            MediaAttr.MEDIA_POSITION_UPDATED_AT: (self.media_position_updated_at or ""),
            MediaAttr.SOURCE_LIST: self.app_names,
            MediaAttr.SOURCE: self.app_name,
            MediaAttr.SOUND_MODE_LIST: self.output_devices_combinations,
            MediaAttr.SOUND_MODE: self.output_devices,
            MediaAttr.SHUFFLE: self._shuffle,
            MediaAttr.REPEAT: self._repeat,
            # TODO when UC library updated
            # MediaAttr.MEDIA_ID : self._media_id,
        }

    def playstatus_update(self, _updater: Any, playstatus: pyatv.interface.Playing) -> None:
        """Play status push update callback handler (push_updater.listener)."""
        if _LOG.isEnabledFor(logging.DEBUG):
            _LOG.debug("[%s] Push update: %s", self.log_id, re.sub(r"\n\s*", ", ", str(playstatus)))
        self._spawn_task(self._process_playing_update(playstatus))

    def playstatus_error(self, _updater: Any, exception: Exception) -> None:
        """Play status push update error callback handler (push_updater.listener)."""
        _LOG.warning("[%s] A %s error occurred: %s", self.log_id, exception.__class__, exception)
        data = pyatv.interface.Playing()
        self._spawn_task(self._process_playing_update(data))

    @override
    def connection_lost(self, exception: Exception | None) -> None:
        """
        Device was unexpectedly disconnected.

        This is a callback function from pyatv.interface.DeviceListener.
        It must never block or run teardown inline (INV-7).
        """
        _LOG.warning("[%s] Lost connection: %s", self.log_id, exception)
        self._post(Event.CONNECTION_LOST)

    @override
    def connection_closed(self) -> None:
        """Device connection was closed.

        This is a callback function from pyatv.interface.DeviceListener.
        It must never block or run teardown inline (INV-7).
        """
        _LOG.debug("[%s] Connection closed!", self.log_id)
        self._post(Event.CONNECTION_LOST)

    def _volume_notify(self) -> None:
        """Calculate the average volume level of all connected devices."""
        volume_level: float = self._volume_level

        # If global volume is enabled, calculate the average volume
        if self.device_config.global_volume:
            volume_level += sum(list(self._output_devices_volume.values()))
            count = len(self._output_devices_volume)
            # Exclude main volume from calculation  if not 0 otherwise it means it cannot be set
            if self._volume_level > 0.0:
                count += 1
            count = max(1, count)
            volume_level /= count

        update: dict[MediaAttr, Any] = {MediaAttr.VOLUME: volume_level}
        self.events.emit(EVENTS.UPDATE, self._device.identifier, update)

    @override
    def volume_update(self, old_level: float, new_level: float) -> None:
        """Volume level change callback."""
        _LOG.debug("[%s] Volume level update: %s -> %s", self.log_id, old_level, new_level)
        self._volume_level = new_level
        self._volume_notify()

    @override
    def volume_device_update(
        self,
        output_device: OutputDevice,
        old_level: float,
        new_level: float,
    ) -> None:
        """Output device volume was updated."""
        # Skip if volume does not concern an external device
        _LOG.debug("[%s] Volume level for device %s", self.log_id, output_device.identifier)
        if (
            self._atv
            and self._atv.device_info is not None  # pyright: ignore[reportUnnecessaryComparison]
            and output_device.identifier == self._atv.device_info.output_device_id
        ):
            return
        volume = round(new_level, 1)
        _LOG.debug("[%s] Volume level for device %s : %.2f", self.log_id, output_device.identifier, volume)
        self._output_devices_volume[output_device.identifier] = volume
        if self.device_config.global_volume:
            self._volume_notify()

    @override
    def outputdevices_update(
        self,
        old_devices: list[OutputDevice],
        new_devices: list[OutputDevice],
    ) -> None:
        """Output device change callback handler, for example airplay speaker."""
        _LOG.debug("[%s] Changed output devices to %s", self.log_id, self.output_devices)
        # Nudge the poll worker to refresh the available output devices list promptly instead of
        # waiting for the full OUTPUT_REFRESH_INTERVAL backoff.
        self._next_output_refresh = 0.0
        self.events.emit(EVENTS.UPDATE, self._device.identifier, {MediaAttr.SOUND_MODE: self.output_devices})

    async def _find_atv(self) -> pyatv.interface.BaseConfig | None:
        """Find a specific Apple TV on the network by identifier."""
        hosts = [self._device.address] if self._device.address else None
        identifier = self._device.mac_address or self._device.identifier
        if not identifier:
            _LOG.error("[%s] Cannot find device: no identifier/mac", self.log_id)
            return None
        _LOG.debug("Find AppleTV for identifier %s and hosts %s", identifier, hosts)
        atvs = await pyatv.scan(self._loop, identifier=identifier, hosts=hosts)
        match = next((atv for atv in atvs if identifier in atv.all_identifiers), None)
        if match is None:
            _LOG.debug("[%s] No scan result matched identifier %s", self.log_id, identifier)
        else:
            _LOG.debug("[%s] Found matching AppleTV for identifier %s", self.log_id, identifier)
        return match

    def add_credentials(self, credentials: dict[AtvProtocol, str]) -> None:
        """Append one credential record per (protocol, credential) pair."""
        for protocol, credential in credentials.items():
            self._device.credentials.append({"protocol": protocol.value, "credentials": credential})

    async def start_pairing(self, protocol: Protocol, name: str) -> int | None:
        """Start the pairing process with the Apple TV."""
        if not self._pairing_atv:
            _LOG.error("[%s] Pairing requires initialized ATV device!", self.log_id)
            return None

        _LOG.debug("[%s] Pairing started", self.log_id)
        self._pairing_process = await pyatv.pair(self._pairing_atv, protocol, self._loop, name=name)
        await self._pairing_process.begin()

        if self._pairing_process.device_provides_pin:
            _LOG.debug("[%s] Device provides PIN", self.log_id)
            return 0

        _LOG.debug("[%s] We provide PIN", self.log_id)
        pin = random.randint(1000, 9999)  # noqa: S311  # not security-sensitive, used as ATV pairing prompt
        self._pairing_process.pin(pin)
        return pin

    async def enter_pin(self, pin: int | str) -> None:
        """Pin code used for pairing."""
        if not self._pairing_process:
            _LOG.error("[%s] Pairing process not initialized", self.log_id)
            return

        _LOG.debug("[%s] Entering PIN", self.log_id)
        self._pairing_process.pin(pin)

    async def finish_pairing(self) -> pyatv.interface.BaseService | None:
        """Finish the pairing process."""
        if not self._pairing_process:
            _LOG.error("[%s] Pairing process not initialized", self.log_id)
            return None

        _LOG.debug("[%s] Pairing finished", self.log_id)
        res = None

        await self._pairing_process.finish()

        if self._pairing_process.has_paired:
            _LOG.debug("[%s] Paired with device!", self.log_id)
            res = self._pairing_process.service
        else:
            _LOG.warning("[%s] Did not pair with device", self.log_id)
            self.events.emit(EVENTS.ERROR, self._device.identifier, "Could not pair with device")

        await self._pairing_process.close()
        self._pairing_process = None
        return res

    def _post(self, event: Event, payload: pyatv.interface.AppleTV | None = None) -> None:
        """Enqueue a lifecycle event for the supervisor (safe from sync pyatv callbacks, INV-4/INV-7)."""
        self._event_queue.put_nowait((event, payload))

    async def connect(self) -> None:
        """Ensure the device is being supervised and connecting (idempotent)."""
        if self._supervisor is None or self._supervisor.done():
            self._supervisor = self._loop.create_task(self._run_supervisor())
        self._post(Event.START)

    async def disconnect(self) -> None:
        """Disconnect from ATV and stop supervising it; returns once the state machine reached STOPPED."""
        _LOG.debug("[%s] Disconnecting from device", self.log_id)
        if self._supervisor is None or self._supervisor.done():
            # Never supervised (e.g. pairing-only instance in the setup flow): nothing to stop.
            return
        self._post(Event.STOP)
        if not await self.wait_for_state({ConnectionState.STOPPED}, timeout_s=STOP_TIMEOUT):
            _LOG.warning("[%s] Timeout waiting for STOPPED state while disconnecting", self.log_id)
        await self._stop_supervisor()

    async def wait_for_state(self, targets: set[ConnectionState], timeout_s: float) -> bool:
        """
        Wait until the state machine reaches one of the target states.

        Returns immediately when already in a target state. Returns ``False`` on timeout
        instead of raising, so callers never leak a ``TimeoutError``.
        """
        if self._machine.state in targets:
            return True
        try:
            async with asyncio.timeout(timeout_s):
                while self._machine.state not in targets:
                    await self._state_changed.wait()
        except TimeoutError:
            return False
        return True

    async def _run_supervisor(self) -> None:
        """Drain the event queue and apply the machine's actions — the single owner of `_atv` (INV-3/INV-9)."""
        while True:
            event, payload = await self._event_queue.get()
            state_before = self._machine.state
            actions = self._machine.handle(event)
            _LOG.debug(
                "[%s] %s: %s -> %s %s", self.log_id, event, state_before, self._machine.state, [str(a) for a in actions]
            )
            # Orphan disposal: a successful connect the machine did not adopt must be closed (INV-8, F9)
            if event is Event.CONNECT_SUCCEEDED and Action.ADOPT_CONNECTION not in actions and payload is not None:
                payload.close()
            try:
                for action in actions:
                    await self._execute(action, payload)
            except _AdoptFailed:
                # Post-connect setup failed (e.g. blocked facade): reconnect via the machine (F4)
                self._post(Event.CONNECTION_LOST)
            # Wake up wait_for_state() waiters (pulse: set + clear releases all current waiters)
            self._state_changed.set()
            self._state_changed.clear()

    async def _execute(self, action: Action, payload: pyatv.interface.AppleTV | None) -> None:
        """Perform the I/O effect for a machine action. Runs only in the supervisor task."""
        match action:
            case Action.START_CONNECT:
                self._connect_task = self._loop.create_task(self._connect_cycle())
            case Action.SCHEDULE_RETRY:
                delay = self._machine.backoff_delay()
                _LOG.debug("[%s] Trying to connect again in %.1fs", self.log_id, delay)
                self._retry_task = self._loop.create_task(self._retry_after(delay))
            case Action.CANCEL_CONNECT:
                if self._connect_task is not None:
                    self._connect_task.cancel()
                    self._connect_task = None
                if self._retry_task is not None:
                    self._retry_task.cancel()
                    self._retry_task = None
            case Action.ADOPT_CONNECTION:
                await self._adopt_connection(payload)
            case Action.TEARDOWN:
                await self._teardown()
            case Action.EMIT_CONNECTED:
                _LOG.debug("[%s] Connected", self.log_id)
                self.events.emit(EVENTS.CONNECTED, self._device.identifier)
            case Action.EMIT_DISCONNECTED:
                self.events.emit(EVENTS.DISCONNECTED, self._device.identifier)
            case Action.EMIT_AUTH_ERROR:
                self.events.emit(EVENTS.ERROR, self._device.identifier, "authentication_failed")

    async def _retry_after(self, delay: float) -> None:
        """Arm the backoff timer and post BACKOFF_ELAPSED when it fires."""
        await asyncio.sleep(delay)
        self._post(Event.BACKOFF_ELAPSED)

    async def _adopt_connection(self, atv: pyatv.interface.AppleTV | None) -> None:
        """
        Promote a freshly connected handle to the live connection (the only place `_atv` is set, INV-3).

        Wires up listeners, starts push updates and polling, and performs the fresh-connection
        refresh bootstrap for the app list / output devices (issue #6). pyatv blocks the facade
        immediately after close(), so any access here can raise BlockedStateError if the
        connection was lost between the connect cycle returning and this point — any failure
        raises `_AdoptFailed`, which the supervisor turns into a reconnect (F4).
        """
        if atv is None:
            _LOG.error("[%s] CONNECT_SUCCEEDED without a connection handle", self.log_id)
            raise _AdoptFailed
        # Set the listener FIRST so that from this point forward connection_lost/connection_closed
        # callbacks will fire even if we crash mid-setup.
        try:
            self._atv = atv
            atv.listener = self
            atv.push_updater.listener = self
            atv.push_updater.start()
            atv.audio.listener = self

            await self._start_polling()

            # Fresh connection: allow one prompt fetch of the app list and output devices, then
            # let the poll worker's time-based backoff take over (see _poll_worker). Reset the
            # latch/timers *before* the eager spawns below, and push the timers into the future
            # right after so the poll worker's first pass (~2s later) doesn't re-fetch what these
            # spawned tasks already fetched (or are about to).
            self._app_list_supported = True
            self._next_app_list_refresh = 0.0
            self._next_output_refresh = 0.0

            if atv.features.in_state(FeatureState.Available, FeatureName.AppList):
                self._spawn_task(self._update_app_list())

            self._spawn_task(self._update_output_devices())

            now = self._loop.time()
            self._next_app_list_refresh = now + APP_LIST_REFRESH_INTERVAL
            self._next_output_refresh = now + OUTPUT_REFRESH_INTERVAL
        except Exception as err:
            _LOG.warning("[%s] Connection was lost during post-connect setup: %s", self.log_id, err)
            raise _AdoptFailed from err

    async def _teardown(self) -> None:
        """Stop polling and close the live connection; `close()` is called exactly once per handle (INV-8)."""
        await self._stop_polling()
        # Detach before close(): the sync `connection_closed` callback fires inside close() and
        # must find the machine no longer CONNECTED-with-this-handle (its CONNECTION_LOST is a queued no-op).
        atv, self._atv = self._atv, None
        if atv is not None:
            atv.close()

    async def _stop_supervisor(self) -> None:
        """Terminate the supervisor task and drain the queue, orphan-disposing pending handles (INV-8/INV-9)."""
        supervisor = self._supervisor
        self._supervisor = None
        if supervisor is not None:
            supervisor.cancel()
            with suppress(asyncio.CancelledError):
                await supervisor
        # Defensive: STOP normally cancels these via CANCEL_CONNECT, but not on a wait timeout.
        if self._connect_task is not None:
            self._connect_task.cancel()
            self._connect_task = None
        if self._retry_task is not None:
            self._retry_task.cancel()
            self._retry_task = None
        while not self._event_queue.empty():
            event, payload = self._event_queue.get_nowait()
            if event is Event.CONNECT_SUCCEEDED and payload is not None:
                payload.close()

    async def _connect_cycle(self) -> None:
        """
        Run one scan+connect attempt and post the result as an event.

        Never assigns `_atv` (INV-3) — a successful handle is handed to the supervisor via the
        CONNECT_SUCCEEDED payload. On cancellation it posts nothing and re-raises.
        """
        try:
            atv = await self._connect_attempt()
        except pyatv.exceptions.AuthenticationError:
            _LOG.warning("[%s] Could not connect: authentication error", self.log_id)
            self._post(Event.AUTH_REJECTED)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            _LOG.warning("[%s] Could not connect: %s", self.log_id, err)
            self._post(Event.CONNECT_FAILED)
        else:
            if atv is None:
                self._post(Event.CONNECT_FAILED)
            else:
                self._post(Event.CONNECT_SUCCEEDED, atv)

    async def _connect_attempt(self) -> pyatv.interface.AppleTV | None:
        """Resolve the device configuration (cached scan result if available) and connect to it."""
        try:
            # Reuse the latest AppleTV configuration (Mac and IP) if defined to avoid a scan
            if self._apple_tv_conf is None:
                self._apple_tv_conf = await self._find_atv()
            if self._apple_tv_conf is None:
                return None
            return await self._connect(self._apple_tv_conf)
        except pyatv.exceptions.AuthenticationError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as err:
            # OSError(101, 'Network is unreachable') or 10065 for Windows: the network stack may
            # not be ready yet (e.g. right after standby wake) - retry once quickly in-cycle.
            if err.__cause__ and isinstance(err.__cause__, OSError) and err.__cause__.errno in [101, 10065]:
                _LOG.warning("[%s] Network may not be ready yet %s : retry", self.log_id, err)
                await asyncio.sleep(ERROR_OS_WAIT)
                if self._apple_tv_conf is None:
                    self._apple_tv_conf = await self._find_atv()
                if self._apple_tv_conf is None:
                    return None
                return await self._connect(self._apple_tv_conf)
            # Reset AppleTV configuration in case this is the wrong conf (changed Mac or IP),
            # so the next attempt re-resolves the device with _find_atv()
            self._apple_tv_conf = None
            raise

    async def _connect(self, conf: pyatv.interface.BaseConfig) -> pyatv.interface.AppleTV:
        # We try to connect with all the protocols.
        # If something is not ready yet, we try again afterward
        missing_protocols = []

        for credential in self._device.credentials:
            if credential["protocol"] == AtvProtocol.COMPANION:
                protocol = Protocol.Companion
            elif credential["protocol"] == AtvProtocol.AIRPLAY:
                protocol = Protocol.AirPlay
            else:
                _LOG.error("[%s] Invalid protocol: %s", self.log_id, credential["protocol"])
                continue

            if conf.get_service(protocol) is not None:
                _LOG.debug("[%s] Setting credentials for %s", self.log_id, protocol)
                conf.set_credentials(protocol, credential["credentials"])
            else:
                missing_protocols.append(protocol.name)

        if missing_protocols:
            missing_protocols_str = ", ".join(missing_protocols)
            _LOG.warning(
                "[%s] Protocols %s not yet found for %s, trying later",
                self.log_id,
                missing_protocols_str,
                conf.name,
            )

        _LOG.debug("[%s] Connecting to device", conf.name)
        # In case the device has been renamed
        if self._device.name != conf.name:
            self._device.name = conf.name

        async with asyncio.timeout(CONNECT_TIMEOUT):
            return await pyatv.connect(conf, self._loop)

    def update_config(self, device: AtvDevice) -> None:
        """
        Swap in a reconfigured device (e.g. new address / mac_address / credentials).

        This drops the cached, resolved Apple TV configuration so the next connection
        attempt re-resolves the device with `_find_atv()` using the updated address and
        mac_address, instead of silently continuing to use the stale one.

        Does not itself disconnect or reconnect: callers that want the new configuration
        to take effect immediately should `disconnect()` before and `connect()` after.
        """
        if not device.credentials:
            device.credentials = []
        self._device = device
        self._apple_tv_conf = None

    async def _start_polling(self) -> None:
        if self._atv is None:
            _LOG.warning("[%s] Polling not started, AppleTv object is None", self.log_id)
            self.events.emit(EVENTS.ERROR, self._device.identifier, "Polling not started, AppleTv object is None")
            return

        _LOG.debug("[%s] Starting polling task", self.log_id)
        self._polling = self._loop.create_task(self._poll_worker())

    async def _stop_polling(self) -> None:
        if self._polling:
            self._polling.cancel()
            self._polling = None
            _LOG.debug("[%s] Polling stopped", self.log_id)
        else:
            _LOG.debug("[%s] Polling was already stopped", self.log_id)

    async def _analyze_updated_data(self, update: dict[MediaAttr, Any], data: Playing) -> None:
        """Analyze and report updated data."""
        await self._process_artwork(update, data)

        raw_title = data.title or ""
        # TODO this should be a generic function and not just for the title.
        #      There's already `_replace_bad_chars` in driver.py which could be made a generic utility function.
        if raw_title.startswith("(null):"):  # workaround for Plex DVR
            raw_title = raw_title.removeprefix("(null):").strip()
        if raw_title != self._media_title:
            self._media_title = raw_title
            update[MediaAttr.MEDIA_TITLE] = raw_title

        raw_artist = data.artist or ""
        if raw_artist != self._media_artist:
            self._media_artist = raw_artist
            update[MediaAttr.MEDIA_ARTIST] = raw_artist

        raw_album = data.album or ""
        if raw_album != self._media_album:
            self._media_album = raw_album
            update[MediaAttr.MEDIA_ALBUM] = raw_album

        if data.position is not None and data.position != self._media_position:
            self._media_position = data.position
            update[MediaAttr.MEDIA_POSITION] = self._media_position or 0
            self._media_position_updated_at = datetime.datetime.now(datetime.UTC)
            update[MediaAttr.MEDIA_POSITION_UPDATED_AT] = self.media_position_updated_at
        if data.total_time is not None and data.total_time != self._media_duration:
            self._media_duration = data.total_time
            update[MediaAttr.MEDIA_DURATION] = self._media_duration or 0

        if (
            data.media_type is not None  # pyright: ignore[reportUnnecessaryComparison]
            and (media_type := MEDIA_TYPE_MAPPING.get(data.media_type, MediaContentType.VIDEO))
            != self._media_content_type
        ):
            self._media_content_type = media_type
            update[MediaAttr.MEDIA_TYPE] = self._media_content_type

        if data.repeat is not None and (repeat := REPEAT_MAPPING.get(data.repeat, RepeatMode.OFF)) != self._repeat:
            self._repeat = repeat
            update[MediaAttr.REPEAT] = self._repeat

        if data.shuffle is not None and (data.shuffle != ShuffleState.Off) != self._shuffle:
            self._shuffle = data.shuffle != ShuffleState.Off
            update[MediaAttr.SHUFFLE] = self._shuffle

    async def _process_playing_update(self, data: pyatv.interface.Playing) -> None:
        # store current state: used in `media_state` property
        self._device_state = data.device_state

        update: dict[MediaAttr, Any] = {MediaAttr.STATE: self.media_state}

        reset_playback_info = self._device_state not in [
            DeviceState.Playing,
            DeviceState.Paused,
            DeviceState.Loading,
            DeviceState.Seeking,
        ]
        # image operations are expensive, so we only do it when the hash changed
        if reset_playback_info:
            self.reset_media_data(update)
            # Send None for data so that artwork is cleared
            await self._process_artwork(update, None)
        else:
            await self._analyze_updated_data(update, data)

        if (
            self._atv
            and self._atv.metadata.app
            and self._is_feature_available(FeatureName.App)
            and (raw_source := self._atv.metadata.app.name)
        ):
            # TODO: Not sure if safe
            source = replace_bad_chars(raw_source)
            if source != self._source:
                self._source = source
                update[MediaAttr.SOURCE] = self._source

        self.events.emit(EVENTS.UPDATE, self._device.identifier, update)

    async def _update_app_list(self) -> None:
        if not self._atv:
            _LOG.warning("[%s] App list not updated, ATV not initialized", self.log_id)
            return
        _LOG.debug("[%s] Updating app list", self.log_id)
        update: dict[MediaAttr, Any] = {}

        try:
            update[MediaAttr.SOURCE_LIST] = []
            app_list = sorted(await self._atv.apps.app_list(), key=lambda item: (item.name or "").lower())
            if not app_list:
                _LOG.info("[%s] No apps found, trying again later", self.log_id)
                return
            self._app_list.clear()
            for app in app_list:
                if app.name:
                    self._app_list[app.name] = app.identifier
                    update[MediaAttr.SOURCE_LIST].append(app.name)
        except pyatv.exceptions.NotSupportedError:
            _LOG.warning("[%s] App list is not supported", self.log_id)
            self._app_list_supported = False
        except pyatv.exceptions.ProtocolError:
            _LOG.warning("[%s] App list: protocol error", self.log_id)
        except pyatv.exceptions.BlockedStateError:
            # Connection was closed while we were fetching the app list; ignore silently.
            _LOG.debug("[%s] Connection closed during app list update, skipping", self.log_id)
            return

        self.events.emit(EVENTS.UPDATE, self._device.identifier, update)

    async def _update_output_devices(self) -> None:
        _LOG.debug("[%s] Updating available output devices list", self.log_id)
        try:
            atvs = await pyatv.scan(self._loop)
            if self._atv is None:
                return
            id_to_name = self._build_id_to_name_map(atvs)
            current_id = self._atv.device_info.output_device_id if self._atv.device_info else None
            # sort the devices, or we'd risk providing different results each time the method runs
            # depending on the order devices are discovered
            device_ids: list[str] = sorted(
                (d for d in id_to_name if d != current_id),
                key=lambda d: id_to_name[d].casefold(),
            )
            self._available_output_devices = {d: id_to_name[d] for d in device_ids}
        except pyatv.exceptions.NotSupportedError:
            _LOG.warning("[%s] Output devices listing is not supported", self.log_id)
            return
        except pyatv.exceptions.ProtocolError:
            _LOG.warning("[%s] Output devices: protocol error", self.log_id)
            return
        except pyatv.exceptions.BlockedStateError:
            # Connection was closed while we were fetching output devices; ignore silently.
            _LOG.debug("[%s] Connection closed during output device update, skipping", self.log_id)
            return
        update: dict[str, Any] = {}
        # Build combinations of output devices. The first device in the list is the current Apple TV.
        # When selecting this entry, it will disable all output devices
        self._output_devices = OrderedDict()
        self._output_devices[self._device.name] = frozenset()
        self._build_output_devices_list(id_to_name, device_ids)
        update[MediaAttr.SOUND_MODE_LIST] = self.output_devices_combinations
        update[MediaAttr.SOUND_MODE] = self.output_devices

        _LOG.debug("[%s] Updated sound mode list: %s", self.log_id, json.dumps(update))
        self.events.emit(EVENTS.UPDATE, self._device.identifier, update)

    # used to not let the output devices list grow uncontrolled
    _MAX_OUTPUT_DEVICE_ENTRIES = 64

    def _build_output_devices_list(self, id_to_name: dict[str, str], device_ids: list[str]) -> None:
        """Build output device list, capped at _MAX_OUTPUT_DEVICE_ENTRIES."""
        max_devices_per_group = min(len(device_ids), 4)
        for group_size in range(1, max_devices_per_group + 1):
            for combination in itertools.combinations(device_ids, group_size):
                if len(self._output_devices) >= self._MAX_OUTPUT_DEVICE_ENTRIES:
                    return
                names = [id_to_name[d] for d in combination if d in id_to_name]
                entry_name = ", ".join(sorted(names, key=str.casefold))
                self._output_devices[entry_name] = frozenset(combination)

    @staticmethod
    def _build_id_to_name_map(atvs: list[BaseConfig]) -> dict[str, str]:
        """Build a mapping of output_device_id -> name for the given scan results."""
        id_to_name: dict[str, str] = {}
        for atv in atvs:
            device_info = cast("Any", atv.device_info)
            if device_info is None:
                continue
            output_id = device_info.output_device_id
            if output_id is None:
                continue
            id_to_name[output_id] = atv.name
        return id_to_name

    async def _poll_worker(self) -> None:
        await asyncio.sleep(2)
        _LOG.debug("[%s] Polling started with interval %ds", self.log_id, self._poll_interval)
        while self._atv is not None:
            update: dict[MediaAttr, Any] = {}

            app_name = self.app_name
            if app_name:
                # TODO: Not sure if safe
                update[MediaAttr.SOURCE] = replace_bad_chars(app_name)

            try:
                if data := await self._atv.metadata.playing():
                    await self._analyze_updated_data(update, data)
                    self._device_state = data.device_state
                else:
                    # No playback data available, clear the artwork
                    await self._process_artwork(update, None)
            except pyatv.exceptions.BlockedStateError as ex:
                # The pyatv facade was closed under us; trigger a clean reconnect and exit.
                _LOG.warning("[%s] Polling: connection blocked, triggering reconnect: %s", self.log_id, ex)
                self._post(Event.CONNECTION_LOST)
                return
            except (pyatv.exceptions.ConnectionFailedError, pyatv.exceptions.ConnectionLostError) as ex:
                # Connection was lost during a poll; the state machine restarts the connect cycle.
                _LOG.warning("[%s] Polling: connection lost, triggering reconnect: %s", self.log_id, ex)
                self._post(Event.CONNECTION_LOST)
                return
            except pyatv.exceptions.NotSupportedError:
                pass
            except Exception as ex:  # noqa: BLE001
                _LOG.error("[%s] Polling error: %s", self.log_id, ex)

            update[MediaAttr.STATE] = self.media_state
            self.events.emit(EVENTS.UPDATE, self._device.identifier, update)

            now = self._loop.time()
            if self._app_list_supported and now >= self._next_app_list_refresh:
                self._next_app_list_refresh = now + APP_LIST_REFRESH_INTERVAL
                await self._update_app_list()
            if now >= self._next_output_refresh:
                self._next_output_refresh = now + OUTPUT_REFRESH_INTERVAL
                await self._update_output_devices()

            await asyncio.sleep(self._poll_interval)
        _LOG.debug("[%s] Polling task stopped", self.log_id)

    @property
    def power_state(self) -> PowerState:
        """
        Get the current power state of the device.

        :return: The power state or PowerState.Unknown if not available.
        """
        # Take special care accessing power state: it might not be available depending on protocol,
        # or if the device is not connected
        if self._atv and self._is_feature_available(FeatureName.PowerState):
            return self._atv.power.power_state
        return PowerState.Unknown

    async def _process_artwork(self, update: dict[Any, Any], data: pyatv.interface.Playing | None) -> None:
        if not self._atv:
            return
        current_media_image_url = self._media_image_url
        if self._device_state not in [DeviceState.Idle, DeviceState.Stopped]:
            try:
                if data:
                    playback_hash = hash((data.title, data.artist, data.album))
                    # Hash has changed, invalidate/update cache
                    if PLAYING_STATE_CACHE.get(self._device.identifier) != playback_hash:
                        ARTWORK_CACHE.pop(self._device.identifier, None)
                        PLAYING_STATE_CACHE[self._device.identifier] = playback_hash
                else:
                    # No way of knowing if playback changed, clear cache so that the artwork is sent again
                    ARTWORK_CACHE.pop(self._device.identifier, None)
                    PLAYING_STATE_CACHE.pop(self._device.identifier, None)
                    # Send empty artwork so that it's not stuck in the UI
                    self._media_image_url = ""
                    if self._media_image_url != current_media_image_url:
                        update[MediaAttr.MEDIA_IMAGE_URL] = self._media_image_url
                    return

                artwork = await self._atv.metadata.artwork(width=ARTWORK_WIDTH, height=ARTWORK_HEIGHT)
                if artwork:
                    artwork_hash = hashlib.md5(artwork.bytes, usedforsecurity=False).digest()
                    # Check hash of the artwork to avoid processing it again if it's unchanged
                    if ARTWORK_CACHE.get(self._device.identifier) == artwork_hash:
                        return
                    artwork_encoded = "data:image/png;base64," + base64.b64encode(artwork.bytes).decode("utf-8")
                    self._media_image_url = artwork_encoded
                    if self._media_image_url != current_media_image_url:
                        update[MediaAttr.MEDIA_IMAGE_URL] = self._media_image_url
                    ARTWORK_CACHE[self._device.identifier] = artwork_hash
            except Exception as err:  # noqa: BLE001
                _LOG.warning("[%s] Error while updating the artwork: %s", self.log_id, err)
        else:
            # Not playing - clear caches so that artwork is sent again when playback starts
            ARTWORK_CACHE.pop(self._device.identifier, None)
            PLAYING_STATE_CACHE.pop(self._device.identifier, None)
            # Send empty artwork so that it's not stuck in the UI
            self._media_image_url = ""
            if self._media_image_url != current_media_image_url:
                update[MediaAttr.MEDIA_IMAGE_URL] = self._media_image_url

    def _is_feature_available(self, feature: FeatureName) -> bool:
        if self._atv:
            try:
                return self._atv.features.in_state(FeatureState.Available, feature)
            except pyatv.exceptions.BlockedStateError:
                return False
        return False

    async def _system_status(self) -> SystemStatus:
        try:
            # TODO check if there's a nicer way to get to the CompanionAPI
            # Screensaver state is only accessible in SystemStatus
            # This call will raise an exception for tvOS >= 18.4 until it is resolved (or feature removed)
            # See https://github.com/postlund/pyatv/issues/2648
            if self._atv:
                main_instance = getattr(self._atv.apps, "main_instance", None)
                if isinstance(main_instance, CompanionApps):
                    api = getattr(main_instance, "api", None)
                    if isinstance(api, CompanionAPI):
                        return await api.fetch_attention_state()
        except Exception as ex:  # noqa: BLE001
            _LOG.debug("[%s] Failed to fetch system status: %s", self.log_id, ex)
        return SystemStatus.Unknown

    async def screensaver_active(self) -> bool:
        """Check if screensaver is active."""
        return await self._system_status() == SystemStatus.Screensaver

    @async_handle_atvlib_errors
    async def turn_on(self) -> StatusCodes:
        """Turn device on."""
        assert self._atv is not None  # noqa: S101 — guaranteed by @async_handle_atvlib_errors
        await self._atv.power.turn_on()
        return StatusCodes.OK

    @async_handle_atvlib_errors
    async def turn_off(self) -> StatusCodes:
        """Turn device off."""
        assert self._atv is not None  # noqa: S101 — guaranteed by @async_handle_atvlib_errors
        await self._atv.power.turn_off()
        return StatusCodes.OK

    @async_handle_atvlib_errors
    async def play_pause(self) -> StatusCodes:
        """Toggle between play and pause."""
        assert self._atv is not None  # noqa: S101 — guaranteed by @async_handle_atvlib_errors
        await self.stop_fast_forward_rewind()
        await self._atv.remote_control.play_pause()
        return StatusCodes.OK

    @async_handle_atvlib_errors
    async def play(self) -> StatusCodes:
        """Start playback."""
        assert self._atv is not None  # noqa: S101 — guaranteed by @async_handle_atvlib_errors
        await self.stop_fast_forward_rewind()
        await self._atv.remote_control.play()
        return StatusCodes.OK

    @async_handle_atvlib_errors
    async def pause(self) -> StatusCodes:
        """Pause playback."""
        assert self._atv is not None  # noqa: S101 — guaranteed by @async_handle_atvlib_errors
        await self.stop_fast_forward_rewind()
        await self._atv.remote_control.pause()
        return StatusCodes.OK

    @async_handle_atvlib_errors
    async def stop(self) -> StatusCodes:
        """Stop playback."""
        assert self._atv is not None  # noqa: S101 — guaranteed by @async_handle_atvlib_errors
        await self.stop_fast_forward_rewind()
        await self._atv.remote_control.stop()
        return StatusCodes.OK

    @async_handle_atvlib_errors
    async def fast_forward(self) -> StatusCodes:
        """Long press key right for fast-forward."""
        assert self._atv is not None  # noqa: S101 — guaranteed by @async_handle_atvlib_errors
        await self.stop_fast_forward_rewind()
        await self._atv.remote_control.right(InputAction.Hold)
        return StatusCodes.OK

    @async_handle_atvlib_errors
    async def rewind(self) -> StatusCodes:
        """Long press key left for rewind."""
        assert self._atv is not None  # noqa: S101 — guaranteed by @async_handle_atvlib_errors
        await self.stop_fast_forward_rewind()
        await self._atv.remote_control.left(InputAction.Hold)
        return StatusCodes.OK

    @async_handle_atvlib_errors
    async def fast_forward_companion(self) -> StatusCodes:
        """Fast-forward using companion protocol."""
        assert self._atv is not None  # noqa: S101 — guaranteed by @async_handle_atvlib_errors
        companion = cast("FacadeRemoteControl", self._atv.remote_control).get(Protocol.Companion)
        if companion:
            if self._playback_state == PlaybackState.REWIND:
                await self.stop_fast_forward_rewind()
            await companion.api.mediacontrol_command(command=MediaControlCommand.FastForwardBegin)
            self._playback_state = PlaybackState.FAST_FORWARD
        else:
            await self._atv.remote_control.right(InputAction.Hold)
        return StatusCodes.OK

    @async_handle_atvlib_errors
    async def rewind_companion(self) -> StatusCodes:
        """Rewind using companion protocol."""
        assert self._atv is not None  # noqa: S101 — guaranteed by @async_handle_atvlib_errors
        companion = cast("FacadeRemoteControl", self._atv.remote_control).get(Protocol.Companion)
        if companion:
            if self._playback_state == PlaybackState.FAST_FORWARD:
                await self.stop_fast_forward_rewind()
            await companion.api.mediacontrol_command(command=MediaControlCommand.RewindBegin)
            self._playback_state = PlaybackState.REWIND
        else:
            await self._atv.remote_control.left(InputAction.Hold)
        return StatusCodes.OK

    async def fast_forward_companion_end(self) -> None:
        """Fast-forward using companion protocol."""
        if self._atv is None:
            return
        companion = cast("FacadeRemoteControl", self._atv.remote_control).get(Protocol.Companion)
        if companion:
            await companion.api.mediacontrol_command(command=MediaControlCommand.FastForwardEnd)
            self._playback_state = PlaybackState.NORMAL

    async def rewind_companion_end(self) -> None:
        """Rewind using companion protocol."""
        if self._atv is None:
            return
        companion = cast("FacadeRemoteControl", self._atv.remote_control).get(Protocol.Companion)
        if companion:
            await companion.api.mediacontrol_command(command=MediaControlCommand.RewindEnd)
            self._playback_state = PlaybackState.NORMAL

    async def stop_fast_forward_rewind(self) -> bool:
        """Stop fast forward or rewind if running."""
        if self._playback_state == PlaybackState.NORMAL:
            return False
        if self._playback_state == PlaybackState.FAST_FORWARD:
            await self.fast_forward_companion_end()
        else:
            await self.rewind_companion_end()
        return True

    @async_handle_atvlib_errors
    async def next(self) -> StatusCodes:
        """Press key next."""
        assert self._atv is not None  # noqa: S101 — guaranteed by @async_handle_atvlib_errors
        await self.stop_fast_forward_rewind()
        if self._is_feature_available(FeatureName.Next):  # to prevent timeout errors
            await self._atv.remote_control.next()
        return StatusCodes.OK

    @async_handle_atvlib_errors
    async def previous(self) -> StatusCodes:
        """Press key previous."""
        assert self._atv is not None  # noqa: S101 — guaranteed by @async_handle_atvlib_errors
        await self.stop_fast_forward_rewind()
        if self._is_feature_available(FeatureName.Previous):
            await self._atv.remote_control.previous()
        return StatusCodes.OK

    @async_handle_atvlib_errors
    async def skip_forward(self) -> StatusCodes:
        """Skip forward a time interval.

        Skip interval is typically 15-30s, but is decided by the app.
        """
        assert self._atv is not None  # noqa: S101 — guaranteed by @async_handle_atvlib_errors
        await self.stop_fast_forward_rewind()
        if self._is_feature_available(FeatureName.SkipForward):
            await self._atv.remote_control.skip_forward()
        return StatusCodes.OK

    @async_handle_atvlib_errors
    async def skip_backward(self) -> StatusCodes:
        """Skip backwards a time interval.

        Skip interval is typically 15-30s, but is decided by the app.
        """
        assert self._atv is not None  # noqa: S101 — guaranteed by @async_handle_atvlib_errors
        await self.stop_fast_forward_rewind()
        if self._is_feature_available(FeatureName.SkipBackward):
            await self._atv.remote_control.skip_backward()
        return StatusCodes.OK

    @async_handle_atvlib_errors
    async def set_repeat(self, mode: str) -> StatusCodes:
        """Change repeat state."""
        assert self._atv is not None  # noqa: S101 — guaranteed by @async_handle_atvlib_errors
        if self._is_feature_available(FeatureName.Repeat):
            match mode:
                case "OFF":
                    repeat = RepeatState.Off
                case "ALL":
                    repeat = RepeatState.All
                case "ONE":
                    repeat = RepeatState.Track
                case _:
                    return StatusCodes.BAD_REQUEST
            await self._atv.remote_control.set_repeat(repeat)
            return StatusCodes.OK
        return StatusCodes.BAD_REQUEST

    @async_handle_atvlib_errors
    async def set_shuffle(self, mode: bool) -> StatusCodes:  # noqa: FBT001 — single-arg setter
        """Change shuffle mode to on or off."""
        assert self._atv is not None  # noqa: S101 — guaranteed by @async_handle_atvlib_errors
        if self._is_feature_available(FeatureName.Shuffle):
            await self._atv.remote_control.set_shuffle(ShuffleState.Albums if mode else ShuffleState.Off)
            return StatusCodes.OK
        return StatusCodes.SERVICE_UNAVAILABLE

    @async_handle_atvlib_errors
    async def volume_up(self) -> StatusCodes:
        """Press key volume up."""
        assert self._atv is not None  # noqa: S101 — guaranteed by @async_handle_atvlib_errors
        await self._atv.audio.volume_up()
        return StatusCodes.OK

    @async_handle_atvlib_errors
    async def volume_down(self) -> StatusCodes:
        """Press key volume down."""
        assert self._atv is not None  # noqa: S101 — guaranteed by @async_handle_atvlib_errors
        await self._atv.audio.volume_down()
        return StatusCodes.OK

    @async_handle_atvlib_errors
    async def volume_set(self, volume_level: float | None) -> StatusCodes:
        """Set volume level to all connected devices."""
        assert self._atv is not None  # noqa: S101 — guaranteed by @async_handle_atvlib_errors
        if volume_level is None:
            return StatusCodes.BAD_REQUEST
        audio_facade: FacadeAudio = cast("FacadeAudio", self._atv.audio)
        audio: MrpAudio | None = audio_facade.get(Protocol.MRP) if audio_facade else None
        if audio:
            tasks: list[Coroutine[Any, Any, Any]] = [audio.set_volume(volume_level)]
            # If global volume is set, apply volume to all connected devices
            if self._device.global_volume:
                output_devices = audio.output_devices
                output_devices_ids = [device.identifier for device in output_devices]
                current_output_id = (
                    self._atv.device_info.output_device_id
                    if self._atv.device_info is not None  # pyright: ignore[reportUnnecessaryComparison]
                    else None
                )
                for device_id in output_devices_ids:
                    if device_id == current_output_id:
                        continue
                    tasks.append(audio.protocol.send(messages.set_volume(device_id, volume_level / 100.0)))
            async with asyncio.timeout(5):
                await asyncio.gather(*tasks)
            return StatusCodes.OK
        return StatusCodes.SERVICE_UNAVAILABLE

    @async_handle_atvlib_errors
    async def cursor_up(self) -> StatusCodes:
        """Press key up."""
        assert self._atv is not None  # noqa: S101 — guaranteed by @async_handle_atvlib_errors
        await self._atv.remote_control.up()
        return StatusCodes.OK

    @async_handle_atvlib_errors
    async def cursor_down(self) -> StatusCodes:
        """Press key down."""
        assert self._atv is not None  # noqa: S101 — guaranteed by @async_handle_atvlib_errors
        await self._atv.remote_control.down()
        return StatusCodes.OK

    @async_handle_atvlib_errors
    async def cursor_left(self) -> StatusCodes:
        """Press key left."""
        assert self._atv is not None  # noqa: S101 — guaranteed by @async_handle_atvlib_errors
        await self._atv.remote_control.left()
        return StatusCodes.OK

    @async_handle_atvlib_errors
    async def cursor_right(self) -> StatusCodes:
        """Press key right."""
        assert self._atv is not None  # noqa: S101 — guaranteed by @async_handle_atvlib_errors
        await self._atv.remote_control.right()
        return StatusCodes.OK

    @async_handle_atvlib_errors
    async def cursor_select(self) -> StatusCodes:
        """Press key select."""
        assert self._atv is not None  # noqa: S101 — guaranteed by @async_handle_atvlib_errors
        await self._atv.remote_control.select()
        return StatusCodes.OK

    @async_handle_atvlib_errors
    async def context_menu(self) -> StatusCodes:
        """Press and hold select key for one second to bring up context menu in most apps."""
        assert self._atv is not None  # noqa: S101 — guaranteed by @async_handle_atvlib_errors
        await self._atv.remote_control.select(InputAction.Hold)
        return StatusCodes.OK

    @async_handle_atvlib_errors
    async def home(self) -> StatusCodes:
        """Press key home."""
        assert self._atv is not None  # noqa: S101 — guaranteed by @async_handle_atvlib_errors
        await self._atv.remote_control.home()
        return StatusCodes.OK

    @async_handle_atvlib_errors
    async def control_center(self) -> StatusCodes:
        """Show control center: press and hold home key for one second."""
        assert self._atv is not None  # noqa: S101 — guaranteed by @async_handle_atvlib_errors
        await self._atv.remote_control.control_center()
        return StatusCodes.OK

    @async_handle_atvlib_errors
    async def menu(self) -> StatusCodes:
        """Press key menu."""
        assert self._atv is not None  # noqa: S101 — guaranteed by @async_handle_atvlib_errors
        await self._atv.remote_control.menu()
        return StatusCodes.OK

    @async_handle_atvlib_errors
    async def top_menu(self) -> StatusCodes:
        """Go to top menu: press and hold menu key for one second."""
        assert self._atv is not None  # noqa: S101 — guaranteed by @async_handle_atvlib_errors
        await self._atv.remote_control.menu(InputAction.Hold)
        return StatusCodes.OK

    @async_handle_atvlib_errors
    async def channel_up(self) -> StatusCodes:
        """Select next channel."""
        assert self._atv is not None  # noqa: S101 — guaranteed by @async_handle_atvlib_errors
        if self._is_feature_available(FeatureName.ChannelUp):
            await self._atv.remote_control.channel_up()
            return StatusCodes.OK
        return StatusCodes.SERVICE_UNAVAILABLE

    @async_handle_atvlib_errors
    async def channel_down(self) -> StatusCodes:
        """Select previous channel."""
        assert self._atv is not None  # noqa: S101 — guaranteed by @async_handle_atvlib_errors
        if self._is_feature_available(FeatureName.ChannelDown):
            await self._atv.remote_control.channel_down()
            return StatusCodes.OK
        return StatusCodes.SERVICE_UNAVAILABLE

    @async_handle_atvlib_errors
    async def screensaver(self) -> StatusCodes:
        """Start screensaver."""
        assert self._atv is not None  # noqa: S101 — guaranteed by @async_handle_atvlib_errors
        try:
            if self._is_feature_available(FeatureName.Screensaver):
                await self._atv.remote_control.screensaver()
        except pyatv.exceptions.ProtocolError:
            # workaround: command succeeds and screensaver is started, but always returns
            # ProtocolError: Command _hidC failed
            pass
        return StatusCodes.OK

    @async_handle_atvlib_errors
    async def launch_app(self, app_name: str) -> StatusCodes:
        """Launch an app based on bundle ID or URL."""
        assert self._atv is not None  # noqa: S101 — guaranteed by @async_handle_atvlib_errors
        try:
            # Launch app by name
            await self._atv.apps.launch_app(self._app_list[app_name])
            return StatusCodes.OK
        except KeyError:
            # If app_name is not an app name handle it as app deep link url
            try:
                await self._atv.apps.launch_app(app_name)
                return StatusCodes.OK
            except pyatv.exceptions.NotSupportedError:
                _LOG.warning("[%s] Launch app is not supported", self.log_id)
            except pyatv.exceptions.ProtocolError:
                _LOG.warning("[%s] Launch app: protocol error", self.log_id)
            return StatusCodes.SERVICE_UNAVAILABLE

    @async_handle_atvlib_errors
    async def app_switcher(self) -> StatusCodes:
        """Press the TV/Control Center button two times to open the App Switcher."""
        assert self._atv is not None  # noqa: S101 — guaranteed by @async_handle_atvlib_errors
        await self._atv.remote_control.home(InputAction.DoubleTap)
        return StatusCodes.OK

    @async_handle_atvlib_errors
    async def toggle_guide(self) -> StatusCodes:
        """Toggle the EPG."""
        assert self._atv is not None  # noqa: S101 — guaranteed by @async_handle_atvlib_errors
        await self._atv.remote_control.guide()
        return StatusCodes.OK

    @async_handle_atvlib_errors
    async def set_output_device(self, device_name: str) -> StatusCodes:
        """Set output device selection."""
        assert self._atv is not None  # noqa: S101 — guaranteed by @async_handle_atvlib_errors
        device_entry = self._output_devices.get(device_name, None)
        if device_entry is None:
            _LOG.warning(
                "[%s] Output device not found in the list %s (list : %s)",
                self.log_id,
                device_name,
                self.output_devices_combinations,
            )
            return StatusCodes.BAD_REQUEST
        output_devices = self._atv.audio.output_devices
        if len(device_entry) == 0 and len(output_devices) == 0:
            return StatusCodes.OK
        device_ids = [device.identifier for device in output_devices]

        _LOG.debug("[%s] Removing output devices: %s", self.log_id, device_ids)
        # pyatv mistypes the signature as `*devices: List[str]`; runtime expects each id unpacked.
        await self._atv.audio.remove_output_devices(*device_ids)  # pyright: ignore[reportArgumentType]
        if len(device_entry) == 0:
            return StatusCodes.OK

        # Add current AppleTV device to the list unless it is already there
        new_output_devices = list(device_entry)
        device_info = self._atv.device_info
        current_device_id = (
            device_info.output_device_id if device_info is not None else None  # pyright: ignore[reportUnnecessaryComparison]
        )
        if current_device_id is not None and current_device_id not in new_output_devices:
            new_output_devices.append(current_device_id)

        _LOG.debug("[%s] Setting output devices: %s", self.log_id, new_output_devices)
        # pyatv mistypes the signature as `*devices: List[str]`; runtime expects each id unpacked.
        await self._atv.audio.set_output_devices(*new_output_devices)  # pyright: ignore[reportArgumentType]
        return StatusCodes.OK

    @async_handle_atvlib_errors
    async def set_media_position(self, media_position: int) -> StatusCodes:
        """Set media position."""
        assert self._atv is not None  # noqa: S101 — guaranteed by @async_handle_atvlib_errors
        await self._atv.remote_control.set_position(media_position)
        return StatusCodes.OK

    @async_handle_atvlib_errors
    async def swipe(self, start_x: int, start_y: int, end_x: int, end_y: int, duration_ms: int) -> StatusCodes:
        """Generate a swipe gesture."""
        assert self._atv is not None  # noqa: S101 — guaranteed by @async_handle_atvlib_errors
        touch_facade: FacadeTouchGestures = cast("FacadeTouchGestures", self._atv.touch)
        if touch_facade.get(Protocol.Companion):
            await touch_facade.swipe(start_x, start_y, end_x, end_y, duration_ms)
            return StatusCodes.OK
        msg = "Touch gestures not supported"
        raise pyatv.exceptions.CommandError(msg)

    @async_handle_atvlib_errors
    async def send_hid_key(self, use_page: int, usage: int) -> StatusCodes:
        """Send a short HID key press.

        :param use_page: HID usage page (1 Generic Desktop, 7 Keyboard, 12 Consumer)
        :param usage: HID key usage
        """
        assert self._atv is not None  # noqa: S101 — guaranteed by @async_handle_atvlib_errors
        if self._atv:
            main_instance = getattr(self._atv.remote_control, "main_instance", None)
            if isinstance(main_instance, MrpRemoteControl):
                protocol: MrpProtocol = main_instance.protocol
                send_hid_event = cast("Any", messages.send_hid_event)
                await protocol.send(send_hid_event(use_page, usage, True))  # noqa: FBT003
                await protocol.send(send_hid_event(use_page, usage, False))  # noqa: FBT003
                return StatusCodes.OK
        _LOG.warning("[%s] send HID key not supported (%d, %d)", self.log_id, use_page, usage)
        return StatusCodes.SERVICE_UNAVAILABLE

    def reset_media_data(self, attributes: dict[MediaAttr, Any]) -> None:
        """Reset media metadata."""
        attributes[MediaAttr.MEDIA_POSITION] = 0
        attributes[MediaAttr.MEDIA_DURATION] = 0
        attributes[MediaAttr.MEDIA_IMAGE_URL] = ""
        attributes[MediaAttr.MEDIA_TITLE] = ""
        attributes[MediaAttr.MEDIA_ARTIST] = ""
        attributes[MediaAttr.MEDIA_ALBUM] = ""
        attributes[MediaAttr.MEDIA_TYPE] = ""
        attributes[MediaAttr.REPEAT] = RepeatMode.OFF
        attributes[MediaAttr.SHUFFLE] = False
        attributes[MediaAttr.SOURCE] = ""
        self._media_position = None
        self._media_duration = None
        self._media_image_url = None
        self._media_title = None
        self._media_artist = None
        self._media_album = None
        self._media_content_type = MediaContentType.VIDEO
        self._repeat = RepeatMode.OFF
        self._shuffle = None
        self._source = None

    @debounce(1)
    async def deferred_state_update(self) -> None:
        """Defer state update."""
        attribute_state = self.media_state
        if attribute_state and attribute_state not in [
            MediaState.PLAYING,
            MediaState.PAUSED,
            MediaState.BUFFERING,
        ]:
            # if nothing is playing: clear the playing information
            attributes: dict[MediaAttr, Any] = {}
            self.reset_media_data(attributes)
            self.events.emit(EVENTS.UPDATE, self.device_config.identifier, attributes)
