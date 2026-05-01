"""
This module implements the Apple TV communication of the Remote Two integration driver.

Uses the [pyatv](https://github.com/postlund/pyatv) library with concepts borrowed from the Home Assistant
[Apple TV integration](https://github.com/postlund/home-assistant/tree/dev/homeassistant/components/apple_tv)

:copyright: (c) 2023-2024 by Unfolded Circle ApS.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import asyncio
import base64
import datetime
import hashlib
import itertools
import logging
import random
from asyncio import AbstractEventLoop, Task
from collections import OrderedDict
from enum import Enum, StrEnum
from functools import wraps
from typing import (
    Any,
    Awaitable,
    Callable,
    Concatenate,
    Coroutine,
    List,
    ParamSpec,
    TypeVar,
    cast,
)

import pyatv
import pyatv.const
from config import AtvDevice, AtvProtocol
from const import AppleTVSelects, AppleTVSensors
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
from pyatv.core.facade import FacadeAudio, FacadeRemoteControl, FacadeTouchGestures
from pyatv.interface import BaseConfig, OutputDevice, Playing
from pyatv.protocols.companion import (
    CompanionAPI,
    MediaControlCommand,
    SystemStatus,
)
from pyatv.protocols.mrp import (
    MrpAudio,
    MrpRemoteControl,
    messages,
)
from pyee.asyncio import AsyncIOEventEmitter
from ucapi import StatusCodes
from ucapi.media_player import Attributes as MediaAttr
from ucapi.media_player import MediaContentType, RepeatMode
from ucapi.media_player import States as MediaState
from ucapi.select import Attributes as SelectAttributes

_LOG = logging.getLogger(__name__)

BACKOFF_MAX = 30
BACKOFF_SEC = 2
ARTWORK_WIDTH = 400
ARTWORK_HEIGHT = 400
ERROR_OS_WAIT = 0.5


# pylint: disable=too-many-lines


class EVENTS(StrEnum):
    """Internal driver events."""

    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"
    PAIRED = "PAIRED"
    ERROR = "ERROR"
    UPDATE = "UPDATE"


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


def debounce(wait: float):
    """Debounce function with delay in seconds."""

    def decorator(func):
        task: Task | None = None

        @wraps(func)
        async def debounced(*args, **kwargs):
            nonlocal task

            async def call_func():
                """Call wrapped function."""
                await asyncio.sleep(wait)
                await func(*args, **kwargs)

            if task and not task.done():
                task.cancel()
            task = asyncio.create_task(call_func())
            return task

        return debounced

    return decorator


# Adapted from Home Assistant `asyncLOG_errors` in
# https://github.com/home-assistant/core/blob/fd1f0b0efeb5231d3ee23d1cb2a10cdeff7c23f1/homeassistant/components/denonavr/media_player.py
def async_handle_atvlib_errors(
    func: Callable[Concatenate[_AppleTvT, _P], Awaitable[StatusCodes | None]],
) -> Callable[Concatenate[_AppleTvT, _P], Coroutine[Any, Any, StatusCodes | None]]:
    """
    Handle errors when calling commands in the AppleTv class.

    Decorator for the AppleTv class:
    - Check if device is connected.
    - Log errors occurred when calling an Apple TV library function.
    - Translate errors into UC status codes to return to the Remote.

    Taken from Home-Assistant
    """

    @wraps(func)
    async def wrapper(self: _AppleTvT, *args: _P.args, **kwargs: _P.kwargs) -> StatusCodes:
        # pylint: disable=protected-access
        if self._atv is None:
            _LOG.debug("Command wrapper : not connected try reconnect")
            await self.connect()
            if self._atv is None:
                return StatusCodes.SERVICE_UNAVAILABLE

        result = StatusCodes.SERVER_ERROR
        try:
            await func(self, *args, **kwargs)
            return StatusCodes.OK
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
            self._handle_disconnect()
        except Exception as err:  # pylint: disable=broad-exception-caught
            _LOG.exception("[%s] Error %s occurred in method %s%s", self.log_id, err, func.__name__, args)

        return result

    return wrapper


ARTWORK_CACHE: dict[str, bytes] = {}
PLAYING_STATE_CACHE: dict[str, int] = {}


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
        self._is_on: bool = False
        self._atv: pyatv.interface.AppleTV | None = None
        if device.credentials is None:
            device.credentials = []
        self._device: AtvDevice = device
        self._connect_task = None
        self._connection_attempts: int = 0
        self._pairing_atv: pyatv.interface.BaseConfig | None = pairing_atv
        self._pairing_process: pyatv.interface.PairingHandler | None = None
        self._polling = None
        self._poll_interval: int = 10
        self._state: DeviceState | PowerState | None = None
        self._app_list: dict[str, str] = {}
        self._available_output_devices: dict[str, str] = {}
        self._output_devices: OrderedDict[str, list[str]] = OrderedDict[str, list[str]]()
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
        self._shuffle = False
        self._source: str | None = None

    @property
    def device_config(self) -> AtvDevice:
        """Return the device configuration."""
        return self._device

    @property
    def identifier(self) -> str:
        """Return the device identifier."""
        if not self._device.identifier:
            raise ValueError("Instance not initialized, no identifier available")
        return self._device.identifier

    @property
    def log_id(self) -> str:
        """Return a log identifier."""
        return self._device.name if self._device.name else self._device.identifier

    @property
    def name(self) -> str:
        """Return the device name."""
        return self._device.name

    @property
    def address(self) -> str | None:
        """Return the optional device address."""
        return self._device.address

    @property
    def is_on(self) -> bool | None:
        """Whether the Apple TV is on or off. Returns None if not connected."""
        if self._atv is None:
            return None
        return self._is_on

    @property
    def state(self) -> DeviceState | None:
        """Return the device state."""
        return self._state

    @property
    def media_state(self) -> MediaState:
        """Return the device state."""
        if self._state is None:
            return MediaState.OFF
        if isinstance(self._state, PowerState):
            if self._state == PowerState.Off:
                return MediaState.OFF
            return MediaState.ON
        return MEDIA_STATE_MAPPING.get(self._state, MediaState.UNKNOWN)

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
        """Return the current selection of output devices."""
        if self._atv is None or self._atv.audio is None:
            return ""
        device_names = []
        for device in self._atv.audio.output_devices:
            device_names.append(device.name)
        return ", ".join(sorted(device_names, key=str.casefold))

    @property
    def app_name(self) -> str:
        """Return current app name."""
        app_name = ""
        if self._atv and self._atv.metadata and self._atv.metadata.app:
            app_name = self._atv.metadata.app.name
        return app_name

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
            MediaAttr.MEDIA_IMAGE_URL: self._media_image_url if self._media_image_url else "",
            MediaAttr.MEDIA_TITLE: self._media_title if self._media_title else "",
            MediaAttr.MEDIA_ALBUM: self._media_album if self._media_album else "",
            MediaAttr.MEDIA_ARTIST: self._media_artist if self._media_artist else "",
            MediaAttr.MEDIA_POSITION: self._media_position if self._media_position else 0,
            MediaAttr.MEDIA_DURATION: self._media_duration if self._media_duration else 0,
            MediaAttr.MEDIA_POSITION_UPDATED_AT: (
                self.media_position_updated_at if self.media_position_updated_at else ""
            ),
            MediaAttr.SOURCE_LIST: self.app_names,
            MediaAttr.SOURCE: self.app_name,
            MediaAttr.SOUND_MODE_LIST: self.output_devices_combinations,
            MediaAttr.SOUND_MODE: self.output_devices,
            MediaAttr.SHUFFLE: self._shuffle,
            MediaAttr.REPEAT: self._repeat,
            # TODO when UC library udpated
            # MediaAttr.MEDIA_ID : self._media_id,
            AppleTVSelects.SELECT_APP: {
                SelectAttributes.CURRENT_OPTION: self.app_name,
                SelectAttributes.OPTIONS: self.app_names,
            },
            AppleTVSelects.SELECT_AUDIO_OUTPUT: {
                SelectAttributes.CURRENT_OPTION: self.output_devices,
                SelectAttributes.OPTIONS: self.output_devices_combinations,
            },
            AppleTVSensors.SENSOR_APP: self.app_name,
            AppleTVSensors.SENSOR_AUDIO_OUTPUT: self.output_devices
        }

    def _backoff(self) -> float:
        if self._connection_attempts * BACKOFF_SEC >= BACKOFF_MAX:
            return BACKOFF_MAX

        return self._connection_attempts * BACKOFF_SEC

    def playstatus_update(self, _updater, playstatus: pyatv.interface.Playing) -> None:
        """Play status push update callback handler."""
        _LOG.debug("[%s] Push update: %s", self.log_id, str(playstatus))
        _ = asyncio.ensure_future(self._process_update(playstatus))

    def playstatus_error(self, _updater, exception: Exception) -> None:
        """Play status push update error callback handler."""
        _LOG.warning("[%s] A %s error occurred: %s", self.log_id, exception.__class__, exception)
        data = pyatv.interface.Playing()
        _ = asyncio.ensure_future(self._process_update(data))
        # TODO restart push updates?

    def connection_lost(self, _exception) -> None:
        """
        Device was unexpectedly disconnected.

        This is a callback function from pyatv.interface.DeviceListener.
        """
        _LOG.exception("[%s] Lost connection %s", self.log_id, _exception)
        self._handle_disconnect()

    def connection_closed(self) -> None:
        """Device connection was closed.

        This is a callback function from pyatv.interface.DeviceListener.
        """
        _LOG.debug("[%s] Connection closed!", self.log_id)
        self._handle_disconnect()

    def _handle_disconnect(self):
        """Handle that the device disconnected and restart connect loop."""
        _ = asyncio.ensure_future(self._stop_polling())
        if self._atv:
            self._atv.close()
            self._atv = None
        self._start_connect_loop()

    def _volume_notify(self):
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

        update = {MediaAttr.VOLUME: volume_level}
        self.events.emit(EVENTS.UPDATE, self._device.identifier, update)

    def volume_update(self, _old_level: float, new_level: float) -> None:
        """Volume level change callback."""
        _LOG.debug("[%s] Volume level: %d", self.log_id, new_level)
        self._volume_level = new_level
        self._volume_notify()

    def volume_device_update(self, output_device: OutputDevice, old_level: float, new_level: float) -> None:
        """Output device volume was updated."""
        # Skip if volume does not concern an external device
        _LOG.debug("[%s] Volume level for device %s", self.log_id, output_device.identifier)
        if output_device.identifier == self._atv.device_info.output_device_id:
            return
        volume = round(new_level, 1)
        _LOG.debug("[%s] Volume level for device %s : %.2f", self.log_id, output_device.identifier, volume)
        self._output_devices_volume[output_device.identifier] = volume
        if self.device_config.global_volume:
            self._volume_notify()

    def outputdevices_update(self, old_devices: List[OutputDevice], new_devices: List[OutputDevice]) -> None:
        """Output device change callback handler, for example airplay speaker."""
        _LOG.debug("[%s] Changed output devices to %s", self.log_id, self.output_devices)
        # self.events.emit(EVENTS.UPDATE, self._device.identifier, {MediaAttr.SOUND_MODE: self.output_devices})

    async def _find_atv(self) -> pyatv.interface.BaseConfig | None:
        """Find a specific Apple TV on the network by identifier."""
        hosts = [self._device.address] if self._device.address else None
        identifier = self._device.mac_address
        _LOG.debug("Find AppleTV for identifier %s and hosts %s", identifier, hosts)
        atvs = await pyatv.scan(self._loop, identifier=identifier, hosts=hosts)
        if not atvs:
            return None
        _LOG.debug(f"Found {len(atvs)} AppleTV for identifier {identifier} and hosts {hosts} : %s", atvs[0])
        return atvs[0]

    def add_credentials(self, credentials: dict[AtvProtocol, str]) -> None:
        """Add credentials for a protocol."""
        self._device.credentials.append(credentials)

    def get_credentials(self) -> list[dict[str, str]]:
        """Return stored credentials."""
        return self._device.credentials

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
        pin = random.randint(1000, 9999)
        self._pairing_process.pin(pin)
        return pin

    async def enter_pin(self, pin: int) -> None:
        """Pin code used for pairing."""
        _LOG.debug("[%s] Entering PIN", self.log_id)
        self._pairing_process.pin(pin)

    async def finish_pairing(self) -> pyatv.interface.BaseService | None:
        """Finish the pairing process."""
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

    async def connect(self) -> None:
        """Establish connection to ATV."""
        if self._is_on is True:
            return
        self._is_on = True
        self._start_connect_loop()

    def _start_connect_loop(self) -> None:
        if not self._connect_task and self._atv is None and self._is_on:
            self.events.emit(EVENTS.CONNECTING, self._device.identifier)
            self._connect_task = asyncio.create_task(self._connect_loop())
        else:
            _LOG.debug(
                "[%s] Not starting connect loop (ATv: %s, isOn: %s)",
                self.log_id,
                self._atv is None,
                self._is_on,
            )

    async def _connect_loop(self) -> None:
        _LOG.debug("[%s] Starting connect loop", self.log_id)
        while self._is_on and self._atv is None:
            await self._connect_once()
            if self._atv is not None:
                break
            self._connection_attempts += 1
            backoff = self._backoff()
            _LOG.debug("[%s] Trying to connect again in %ds", self.log_id, backoff)
            await asyncio.sleep(backoff)

        _LOG.debug("[%s] Connect loop ended", self.log_id)
        self._connect_task = None

        # Add callback listener for various push updates
        self._atv.push_updater.listener = self
        self._atv.push_updater.start()
        self._atv.listener = self
        self._atv.audio.listener = self

        # Reset the backoff counter
        self._connection_attempts = 0

        await self._start_polling()

        if self._atv.features.in_state(FeatureState.Available, FeatureName.AppList):
            self._loop.create_task(self._update_app_list())

        self._loop.create_task(self._update_output_devices())

        self.events.emit(EVENTS.CONNECTED, self._device.identifier)
        _LOG.debug("[%s] Connected", self.log_id)

    async def _connect_once(self) -> None:
        try:
            # Reuse the latest AppleTV instance (Mac and IP) if defined to avoid a scan
            if self._apple_tv_conf is None:
                self._apple_tv_conf = await self._find_atv()
            if self._apple_tv_conf:
                await self._connect(self._apple_tv_conf)
        except pyatv.exceptions.AuthenticationError:
            _LOG.warning("[%s] Could not connect: auth error", self.log_id)
            await self.disconnect()
            return
        except asyncio.CancelledError:
            pass
        except Exception as err:  # pylint: disable=broad-exception-caught
            _LOG.warning("[%s] Could not connect: %s", self.log_id, err)
            # OSError(101, 'Network is unreachable') or 10065 for Windows
            # pylint: disable=E1101
            if err.__cause__ and isinstance(err.__cause__, OSError) and err.__cause__.errno in [101, 10065]:
                _LOG.warning("[%s] Network may not be ready yet %s : retry", self.log_id, err)
                await asyncio.sleep(ERROR_OS_WAIT)
                try:
                    if self._apple_tv_conf is None:
                        self._apple_tv_conf = await self._find_atv()
                    if self._apple_tv_conf:
                        await self._connect(self._apple_tv_conf)
                except Exception as err2:  # pylint: disable=broad-exception-caught
                    _LOG.warning("[%s] Could not connect: %s", self.log_id, err2)
                    self._atv = None
            else:
                # Reset AppleTV configuration in case this is the wrong conf (changed Mac or IP)
                self._apple_tv_conf = None
                self._atv = None

    async def _connect(self, conf: pyatv.interface.BaseConfig) -> None:
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

        self._atv = await pyatv.connect(conf, self._loop)

    async def disconnect(self) -> None:
        """Disconnect from ATV."""
        _LOG.debug("[%s] Disconnecting from device", self.log_id)
        self._is_on = False
        await self._stop_polling()

        try:
            if self._atv:
                self._atv.close()
            if self._connect_task:
                self._connect_task.cancel()
        except Exception as err:  # pylint: disable=broad-exception-caught
            _LOG.exception("[%s] An error occurred while disconnecting: %s", self.log_id, err)
        finally:
            self._atv = None
            self._connect_task = None

    async def _start_polling(self) -> None:
        if self._atv is None:
            _LOG.warning("[%s] Polling not started, AppleTv object is None", self.log_id)
            self.events.emit(EVENTS.ERROR, "Polling not started, AppleTv object is None")
            return

        self._polling = self._loop.create_task(self._poll_worker())
        _LOG.debug("[%s] Polling started", self.log_id)

    async def _stop_polling(self) -> None:
        if self._polling:
            self._polling.cancel()
            self._polling = None
            _LOG.debug("[%s] Polling stopped", self.log_id)
        else:
            _LOG.debug("[%s] Polling was already stopped", self.log_id)

    async def _analyze_updated_data(self, update: dict[str, Any], data: Playing):
        """Analyze and report updated data."""
        await self._process_artwork(update, data)

        if data.title is not None:
            # TODO filter out non-printable characters, for example all emojis
            # workaround for Plex DVR
            if data.title.startswith("(null):"):
                title = data.title.removeprefix("(null):").strip()
            else:
                title = data.title
            if self._media_title != title:
                self._media_title = title
                update[MediaAttr.MEDIA_TITLE] = self._media_title
        else:
            if self._media_title != "":
                self._media_title = ""
                update[MediaAttr.MEDIA_TITLE] = self._media_title

        if data.artist != self._media_artist:
            self._media_artist = data.artist if data.artist else ""
            update[MediaAttr.MEDIA_ARTIST] = self._media_artist
        if data.album != self._media_album:
            self._media_album = data.album if data.album else ""
            update[MediaAttr.MEDIA_ALBUM] = self._media_album

        if data.position is not None and data.position != self._media_position:
            self._media_position = data.position
            update[MediaAttr.MEDIA_POSITION] = self._media_position if self._media_position else 0
            self._media_position_updated_at = datetime.datetime.now(datetime.timezone.utc)
            update[MediaAttr.MEDIA_POSITION_UPDATED_AT] = self.media_position_updated_at
        if data.total_time is not None and data.total_time != self._media_duration:
            self._media_duration = data.total_time
            update[MediaAttr.MEDIA_DURATION] = self._media_duration if self._media_duration else 0

        if (
            data.media_type is not None
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

    async def _process_update(self, data: pyatv.interface.Playing) -> None:  # pylint: disable=too-many-branches
        _LOG.debug("[%s] Process update", self.log_id)

        update = {}
        power_state = await self._get_power_state()
        # off state is not included in metadata, don't override it
        current_state = self.media_state
        if power_state and power_state == PowerState.Off:
            self._state = PowerState.Off
        else:
            self._state = data.device_state

        if current_state != self.media_state:
            update[MediaAttr.STATE] = self.media_state

        reset_playback_info = self._state not in [
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

        if self._is_feature_available(FeatureName.App) and (source := self._atv.metadata.app.name):
            if source != self._source:
                self._source = source
                update[MediaAttr.SOURCE] = self._source
                update[AppleTVSelects.SELECT_APP] = {SelectAttributes.CURRENT_OPTION: self.app_name}
                update[AppleTVSensors.SENSOR_APP] = self.app_name

            self.events.emit(EVENTS.UPDATE, self._device.identifier, update)

    async def _update_app_list(self) -> None:
        _LOG.debug("[%s] Updating app list", self.log_id)
        update = {}

        try:
            update[MediaAttr.SOURCE_LIST] = []
            app_list = sorted(await self._atv.apps.app_list(), key=lambda item: item.name.lower())
            for app in app_list:
                self._app_list[app.name] = app.identifier
                update[MediaAttr.SOURCE_LIST].append(app.name)
            update[AppleTVSelects.SELECT_APP] = {SelectAttributes.OPTIONS: update[MediaAttr.SOURCE_LIST]}
        except pyatv.exceptions.NotSupportedError:
            _LOG.warning("[%s] App list is not supported", self.log_id)
        except pyatv.exceptions.ProtocolError:
            _LOG.warning("[%s] App list: protocol error", self.log_id)

        self.events.emit(EVENTS.UPDATE, self._device.identifier, update)

    async def _update_output_devices(self) -> None:
        _LOG.debug("[%s] Updating available output devices list", self.log_id)
        try:
            atvs = await pyatv.scan(self._loop)
            if self._atv is None:
                return
            current_output_devices = self._available_output_devices
            current_output_device = self.output_devices
            device_ids: list[str] = []
            self._available_output_devices = {}
            for atv in atvs:
                if atv.device_info.output_device_id == self._atv.device_info.output_device_id:
                    continue
                if atv.device_info.output_device_id not in device_ids:
                    device_ids.append(atv.device_info.output_device_id)
                    self._available_output_devices[atv.device_info.output_device_id] = atv.name
        except pyatv.exceptions.NotSupportedError:
            _LOG.warning("[%s] Output devices listing is not supported", self.log_id)
            return
        except pyatv.exceptions.ProtocolError:
            _LOG.warning("[%s] Output devices: protocol error", self.log_id)
            return
        update = {}
        if set(current_output_devices.keys()) != set(self._available_output_devices.keys()) and len(device_ids) > 0:
            # Build combinations of output devices. First device in the list is the current Apple TV
            # When selecting this entry, it will disable all output devices
            self._output_devices = OrderedDict()
            self._output_devices[self._device.name] = []
            self._build_output_devices_list(atvs, device_ids)
            update[MediaAttr.SOUND_MODE_LIST] = self.output_devices_combinations
            update[AppleTVSelects.SELECT_AUDIO_OUTPUT] = {
                SelectAttributes.CURRENT_OPTION: self.output_devices,
                SelectAttributes.OPTIONS: self.output_devices_combinations
            }

        if current_output_device != self.output_devices:
            update[MediaAttr.SOUND_MODE] = self.output_devices
            update[AppleTVSensors.SENSOR_AUDIO_OUTPUT] = self.output_devices
            update.setdefault(AppleTVSelects.SELECT_AUDIO_OUTPUT, {})
            update[AppleTVSelects.SELECT_AUDIO_OUTPUT][SelectAttributes.CURRENT_OPTION] = self.output_devices

        _LOG.debug("Updated sound mode list : %s", update)

        if update:
            self.events.emit(EVENTS.UPDATE, self._device.identifier, update)

    def _build_output_devices_list(self, atvs: list[BaseConfig], device_ids: list[str]):
        """Build possible combinations of output devices."""
        # Don't go beyond combinations of 5 devices
        max_len = min(len(device_ids), 4)
        for i in range(0, max_len):
            combinations = itertools.combinations(device_ids, i + 1)
            for combination in combinations:
                device_names: list[str] = []
                for device_id in combination:
                    for atv in atvs:
                        if atv.device_info.output_device_id == device_id:
                            device_names.append(atv.name)
                            break
                entry_name: str = ", ".join(sorted(device_names, key=str.casefold))
                self._output_devices[entry_name] = list[str](combination)

    async def _poll_worker(self) -> None:
        await asyncio.sleep(2)
        while self._atv is not None:
            update = {}
            current_state = self.media_state
            power_state = await self._get_power_state()
            if power_state and power_state == PowerState.Off:
                self._state = PowerState.Off

            if self._is_feature_available(FeatureName.App) and self._atv.metadata.app.name:
                update[MediaAttr.SOURCE] = self._atv.metadata.app.name
                update[AppleTVSelects.SELECT_APP] = {SelectAttributes.CURRENT_OPTION: self.app_name}
                update[AppleTVSensors.SENSOR_APP] = self.app_name

            if data := await self._atv.metadata.playing():
                await self._analyze_updated_data(update, data)

                # off state is not included in metadata, don't override it
                if power_state and power_state != PowerState.Off:
                    self._state = data.device_state
            else:
                # No playback data available, clear the artwork
                await self._process_artwork(update, None)

            if current_state != self.media_state:
                update[MediaAttr.STATE] = self.media_state

            if update:
                self.events.emit(EVENTS.UPDATE, self._device.identifier, update)

            await asyncio.sleep(self._poll_interval)

    async def _get_power_state(self) -> PowerState | None:
        # Push updates are not reliable for power events, and if the device is in standby it reports state idle!
        if self._is_feature_available(FeatureName.PowerState):
            # Off isn't sent with push updates with the current pyatv library
            # Care must be taken to not override certain states like playing and paused
            return self._atv.power.power_state
        return None

    async def _process_artwork(self, update: dict[Any, Any], data: pyatv.interface.Playing | None):
        current_media_image_url = self._media_image_url
        if self._state not in [DeviceState.Idle, DeviceState.Stopped]:
            try:
                if data:
                    playback_hash = hash((data.title, data.artist, data.album))
                    # Hash has changed, invalidate/update cache
                    if PLAYING_STATE_CACHE.get(self._device.identifier, None) != playback_hash:
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
                    artwork_hash = hashlib.md5(artwork.bytes).digest()
                    # Check hash of the artwork to avoid processing it again if it's unchanged
                    if ARTWORK_CACHE.get(self._device.identifier, None) == artwork_hash:
                        return
                    artwork_encoded = "data:image/png;base64," + base64.b64encode(artwork.bytes).decode("utf-8")
                    self._media_image_url = artwork_encoded
                    if self._media_image_url != current_media_image_url:
                        update[MediaAttr.MEDIA_IMAGE_URL] = self._media_image_url
                    ARTWORK_CACHE[self._device.identifier] = artwork_hash
            except Exception as err:  # pylint: disable=broad-exception-caught
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
            return self._atv.features.in_state(FeatureState.Available, feature)
        return False

    async def _system_status(self) -> SystemStatus:
        try:
            # TODO check if there's a nicer way to get to the CompanionAPI
            # Screensaver state is only accessible in SystemStatus
            # This call will raise an exception for tvOS >= 18.4 until it is resolved (or feature removed)
            # See https://github.com/postlund/pyatv/issues/2648
            if self._atv and isinstance(self._atv.apps.main_instance.api, CompanionAPI):
                system_status = await self._atv.apps.main_instance.api.fetch_attention_state()
                return system_status
        except Exception:  # pylint: disable=broad-exception-caught
            pass
        return SystemStatus.Unknown

    async def screensaver_active(self) -> bool:
        """Check if screensaver is active."""
        return await self._system_status() == SystemStatus.Screensaver

    @async_handle_atvlib_errors
    async def turn_on(self) -> StatusCodes:
        """Turn device on."""
        await self._atv.power.turn_on()

    @async_handle_atvlib_errors
    async def turn_off(self) -> StatusCodes:
        """Turn device off."""
        await self._atv.power.turn_off()

    @async_handle_atvlib_errors
    async def play_pause(self) -> StatusCodes:
        """Toggle between play and pause."""
        await self.stop_fast_forward_rewind()
        await self._atv.remote_control.play_pause()

    @async_handle_atvlib_errors
    async def play(self) -> StatusCodes:
        """Start playback."""
        await self.stop_fast_forward_rewind()
        await self._atv.remote_control.play()

    @async_handle_atvlib_errors
    async def pause(self) -> StatusCodes:
        """Pause playback."""
        await self.stop_fast_forward_rewind()
        await self._atv.remote_control.pause()

    @async_handle_atvlib_errors
    async def stop(self) -> StatusCodes:
        """Stop playback."""
        await self.stop_fast_forward_rewind()
        await self._atv.remote_control.stop()

    @async_handle_atvlib_errors
    async def fast_forward(self) -> StatusCodes:
        """Long press key right for fast-forward."""
        await self.stop_fast_forward_rewind()
        await self._atv.remote_control.right(InputAction.Hold)

    @async_handle_atvlib_errors
    async def rewind(self) -> StatusCodes:
        """Long press key left for rewind."""
        await self.stop_fast_forward_rewind()
        await self._atv.remote_control.left(InputAction.Hold)

    @async_handle_atvlib_errors
    async def fast_forward_companion(self) -> StatusCodes:
        """Fast-forward using companion protocol."""
        companion = cast(FacadeRemoteControl, self._atv.remote_control).get(Protocol.Companion)
        if companion:
            if self._playback_state == PlaybackState.REWIND:
                await self.stop_fast_forward_rewind()
            await companion.api.mediacontrol_command(command=MediaControlCommand.FastForwardBegin)
            self._playback_state = PlaybackState.FAST_FORWARD
        else:
            await self._atv.remote_control.right(InputAction.Hold)

    @async_handle_atvlib_errors
    async def rewind_companion(self) -> StatusCodes:
        """Rewind using companion protocol."""
        companion = cast(FacadeRemoteControl, self._atv.remote_control).get(Protocol.Companion)
        if companion:
            if self._playback_state == PlaybackState.FAST_FORWARD:
                await self.stop_fast_forward_rewind()
            await companion.api.mediacontrol_command(command=MediaControlCommand.RewindBegin)
            self._playback_state = PlaybackState.REWIND
        else:
            await self._atv.remote_control.left(InputAction.Hold)

    async def fast_forward_companion_end(self):
        """Fast-forward using companion protocol."""
        companion = cast(FacadeRemoteControl, self._atv.remote_control).get(Protocol.Companion)
        if companion:
            await companion.api.mediacontrol_command(command=MediaControlCommand.FastForwardEnd)
            self._playback_state = PlaybackState.NORMAL

    async def rewind_companion_end(self):
        """Rewind using companion protocol."""
        companion = cast(FacadeRemoteControl, self._atv.remote_control).get(Protocol.Companion)
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
        await self.stop_fast_forward_rewind()
        if self._is_feature_available(FeatureName.Next):  # to prevent timeout errors
            await self._atv.remote_control.next()

    @async_handle_atvlib_errors
    async def previous(self) -> StatusCodes:
        """Press key previous."""
        await self.stop_fast_forward_rewind()
        if self._is_feature_available(FeatureName.Previous):
            await self._atv.remote_control.previous()

    @async_handle_atvlib_errors
    async def skip_forward(self) -> StatusCodes:
        """Skip forward a time interval.

        Skip interval is typically 15-30s, but is decided by the app.
        """
        await self.stop_fast_forward_rewind()
        if self._is_feature_available(FeatureName.SkipForward):
            await self._atv.remote_control.skip_forward()

    @async_handle_atvlib_errors
    async def skip_backward(self) -> StatusCodes:
        """Skip backwards a time interval.

        Skip interval is typically 15-30s, but is decided by the app.
        """
        await self.stop_fast_forward_rewind()
        if self._is_feature_available(FeatureName.SkipBackward):
            await self._atv.remote_control.skip_backward()

    @async_handle_atvlib_errors
    async def set_repeat(self, mode: str) -> StatusCodes:
        """Change repeat state."""
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
        else:
            return StatusCodes.BAD_REQUEST

    @async_handle_atvlib_errors
    async def set_shuffle(self, mode: bool) -> StatusCodes:
        """Change shuffle mode to on or off."""
        if self._is_feature_available(FeatureName.Shuffle):
            await self._atv.remote_control.set_shuffle(ShuffleState.Albums if mode else ShuffleState.Off)

    @async_handle_atvlib_errors
    async def volume_up(self) -> StatusCodes:
        """Press key volume up."""
        await self._atv.audio.volume_up()

    @async_handle_atvlib_errors
    async def volume_down(self) -> StatusCodes:
        """Press key volume down."""
        await self._atv.audio.volume_down()

    @async_handle_atvlib_errors
    async def volume_set(self, volume_level: float | None) -> StatusCodes:
        """Set volume level to all connected devices."""
        if volume_level is None:
            return StatusCodes.BAD_REQUEST
        audio_facade: FacadeAudio = cast(FacadeAudio, self._atv.audio)
        audio: MrpAudio | None = audio_facade.get(Protocol.MRP) if audio_facade else None
        if audio:
            tasks: list[Coroutine] = [audio.set_volume(volume_level)]
            # If global volume is set, apply volume to all connected devices
            if self._device.global_volume:
                output_devices = audio.output_devices
                output_devices_ids = [device.identifier for device in output_devices]
                for device_id in output_devices_ids:
                    if device_id == self._atv.device_info.output_device_id:
                        continue
                    tasks.append(audio.protocol.send(messages.set_volume(device_id, volume_level / 100.0)))
            async with asyncio.timeout(5):
                await asyncio.gather(*tasks)

    @async_handle_atvlib_errors
    async def cursor_up(self) -> StatusCodes:
        """Press key up."""
        await self._atv.remote_control.up()

    @async_handle_atvlib_errors
    async def cursor_down(self) -> StatusCodes:
        """Press key down."""
        await self._atv.remote_control.down()

    @async_handle_atvlib_errors
    async def cursor_left(self) -> StatusCodes:
        """Press key left."""
        await self._atv.remote_control.left()

    @async_handle_atvlib_errors
    async def cursor_right(self) -> StatusCodes:
        """Press key right."""
        await self._atv.remote_control.right()

    @async_handle_atvlib_errors
    async def cursor_select(self) -> StatusCodes:
        """Press key select."""
        await self._atv.remote_control.select()

    @async_handle_atvlib_errors
    async def context_menu(self) -> StatusCodes:
        """Press and hold select key for one second to bring up context menu in most apps."""
        await self._atv.remote_control.select(InputAction.Hold)

    @async_handle_atvlib_errors
    async def home(self) -> StatusCodes:
        """Press key home."""
        await self._atv.remote_control.home()

    @async_handle_atvlib_errors
    async def control_center(self) -> StatusCodes:
        """Show control center: press and hold home key for one second."""
        await self._atv.remote_control.control_center()

    @async_handle_atvlib_errors
    async def menu(self) -> StatusCodes:
        """Press key menu."""
        await self._atv.remote_control.menu()

    @async_handle_atvlib_errors
    async def top_menu(self) -> StatusCodes:
        """Go to top menu: press and hold menu key for one second."""
        await self._atv.remote_control.menu(InputAction.Hold)

    @async_handle_atvlib_errors
    async def channel_up(self) -> StatusCodes:
        """Select next channel."""
        if self._is_feature_available(FeatureName.ChannelUp):
            await self._atv.remote_control.channel_up()

    @async_handle_atvlib_errors
    async def channel_down(self) -> StatusCodes:
        """Select previous channel."""
        if self._is_feature_available(FeatureName.ChannelDown):
            await self._atv.remote_control.channel_down()

    @async_handle_atvlib_errors
    async def screensaver(self) -> StatusCodes:
        """Start screensaver."""
        try:
            if self._is_feature_available(FeatureName.Screensaver):
                await self._atv.remote_control.screensaver()
        except pyatv.exceptions.ProtocolError:
            # workaround: command succeeds and screensaver is started, but always returns
            # ProtocolError: Command _hidC failed
            pass

    @async_handle_atvlib_errors
    async def launch_app(self, app_name: str) -> StatusCodes:
        """Launch an app based on bundle ID or URL."""
        try:
            # Launch app by name
            await self._atv.apps.launch_app(self._app_list[app_name])
        except KeyError:
            # If app_name is not an app name handle it as app deep link url
            try:
                await self._atv.apps.launch_app(app_name)
            except pyatv.exceptions.NotSupportedError:
                _LOG.warning("[%s] Launch app is not supported", self.log_id)
            except pyatv.exceptions.ProtocolError:
                _LOG.warning("[%s] Launch app: protocol error", self.log_id)

    @async_handle_atvlib_errors
    async def app_switcher(self) -> StatusCodes:
        """Press the TV/Control Center button two times to open the App Switcher."""
        await self._atv.remote_control.home(InputAction.DoubleTap)

    @async_handle_atvlib_errors
    async def toggle_guide(self) -> StatusCodes:
        """Toggle the EPG."""
        await self._atv.remote_control.guide()

    @async_handle_atvlib_errors
    async def set_output_device(self, device_name: str) -> StatusCodes:
        """Set output device selection."""
        if device_name is None:
            return StatusCodes.BAD_REQUEST
        device_entry = self._output_devices.get(device_name, [])
        if device_entry is None:
            _LOG.warning(
                "Output device not found in the list %s (list : %s)", device_name, self.output_devices_combinations
            )
            return StatusCodes.BAD_REQUEST
        output_devices = self._atv.audio.output_devices
        if len(device_entry) == 0 and len(output_devices) == 0:
            return StatusCodes.OK
        device_ids = []
        for device in output_devices:
            device_ids.append(device.identifier)

        _LOG.debug("Removing output devices %s", device_ids)
        await self._atv.audio.remove_output_devices(*device_ids)
        if len(device_entry) == 0:
            return StatusCodes.OK

        # Add current AppleTV device to the list unless it is already there
        new_output_devices = device_entry
        found_current_device = [
            device_id for device_id in new_output_devices if device_id == self._atv.device_info.output_device_id
        ]
        if len(found_current_device) == 0:
            new_output_devices.append(self._atv.device_info.output_device_id)

        _LOG.debug("Setting output devices %s", new_output_devices)
        await self._atv.audio.set_output_devices(*new_output_devices)

    @async_handle_atvlib_errors
    async def set_media_position(self, media_position: int) -> StatusCodes:
        """Set media position."""
        await self._atv.remote_control.set_position(media_position)

    @async_handle_atvlib_errors
    # pylint: disable=too-many-positional-arguments
    async def swipe(self, start_x: int, start_y: int, end_x: int, end_y: int, duration_ms: int) -> StatusCodes:
        """Generate a swipe gesture."""
        touch_facade: FacadeTouchGestures = cast(FacadeTouchGestures, self._atv.touch)
        if touch_facade.get(Protocol.Companion):
            await touch_facade.swipe(start_x, start_y, end_x, end_y, duration_ms)
        else:
            raise pyatv.exceptions.CommandError("Touch gestures not supported")

    @async_handle_atvlib_errors
    async def send_hid_key(self, use_page: int, usage: int) -> StatusCodes:
        """Send a short HID key press.

        :param use_page: HID usage page (1 Generic Desktop, 7 Keyboard, 12 Consumer)
        :param usage: HID key usage
        """
        if self._atv and isinstance(self._atv.remote_control.main_instance, MrpRemoteControl):
            await self._atv.remote_control.main_instance.protocol.send(messages.send_hid_event(use_page, usage, True))
            await self._atv.remote_control.main_instance.protocol.send(messages.send_hid_event(use_page, usage, False))
        else:
            _LOG.warning("[%s] send HID key not supported (%d, %d)", self.log_id, use_page, usage)

    def reset_media_data(self, attributes: dict[str, Any]):
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
        attributes[AppleTVSelects.SELECT_APP] = {
            SelectAttributes.CURRENT_OPTION: "",
        }
        attributes[AppleTVSensors.SENSOR_APP] = ""
        self._media_position = None
        self._media_duration = None
        self._media_image_url = None
        self._media_title = None
        self._media_artist = None
        self._media_album = None
        self._media_content_type = None
        self._repeat = RepeatMode.OFF
        self._shuffle = None
        self._source = None

    @debounce(1)
    async def deferred_state_update(self):
        """Defer state update."""
        attribute_state = self.media_state
        if attribute_state and attribute_state not in [
            MediaState.PLAYING,
            MediaState.PAUSED,
            MediaState.BUFFERING,
        ]:
            # if nothing is playing: clear the playing information
            attributes: dict[str, Any] = {}
            self.reset_media_data(attributes)
            self.events.emit(EVENTS.UPDATE, self.device_config.identifier, attributes)
