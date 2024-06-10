"""
This module implements the Apple TV communication of the Remote Two integration driver.

Uses the [pyatv](https://github.com/postlund/pyatv) library with concepts borrowed from the Home Assistant
[Apple TV integration](https://github.com/postlund/home-assistant/tree/dev/homeassistant/components/apple_tv)

:copyright: (c) 2023-2024 by Unfolded Circle ApS.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import asyncio
import base64
import logging
import random
from asyncio import AbstractEventLoop
from enum import IntEnum
from functools import wraps
from typing import (
    Any,
    Awaitable,
    Callable,
    Concatenate,
    Coroutine,
    ParamSpec,
    TypeVar,
    cast,
)

import pyatv
import pyatv.const
import ucapi
from config import AtvDevice, AtvProtocol
from pyatv.const import (
    DeviceState,
    FeatureName,
    FeatureState,
    InputAction,
    PowerState,
    Protocol,
    RepeatState,
    ShuffleState,
)
from pyatv.core.facade import FacadeRemoteControl
from pyatv.protocols.companion import CompanionAPI, MediaControlCommand, SystemStatus
from pyee import AsyncIOEventEmitter

_LOG = logging.getLogger(__name__)

BACKOFF_MAX = 30
BACKOFF_SEC = 2
ARTWORK_WIDTH = 400
ARTWORK_HEIGHT = 400


class EVENTS(IntEnum):
    """Internal driver events."""

    CONNECTING = 0
    CONNECTED = 1
    DISCONNECTED = 2
    PAIRED = 3
    ERROR = 4
    UPDATE = 5


_AppleTvT = TypeVar("_AppleTvT", bound="AppleTv")
_P = ParamSpec("_P")


# Adapted from Home Assistant `asyncLOG_errors` in
# https://github.com/home-assistant/core/blob/fd1f0b0efeb5231d3ee23d1cb2a10cdeff7c23f1/homeassistant/components/denonavr/media_player.py
def async_handle_atvlib_errors(
    func: Callable[Concatenate[_AppleTvT, _P], Awaitable[ucapi.StatusCodes | None]],
) -> Callable[Concatenate[_AppleTvT, _P], Coroutine[Any, Any, ucapi.StatusCodes | None]]:
    """
    Handle errors when calling commands in the AppleTv class.

    Decorator for the AppleTv class:
    - Check if device is connected.
    - Log errors occurred when calling an Apple TV library function.
    - Translate errors into UC status codes to return to the Remote.

    Taken from Home-Assistant
    """

    @wraps(func)
    async def wrapper(self: _AppleTvT, *args: _P.args, **kwargs: _P.kwargs) -> ucapi.StatusCodes:
        # pylint: disable=protected-access
        if self._atv is None:
            return ucapi.StatusCodes.SERVICE_UNAVAILABLE

        result = ucapi.StatusCodes.SERVER_ERROR
        try:
            await func(self, *args, **kwargs)
            return ucapi.StatusCodes.OK
        except (TimeoutError, pyatv.exceptions.OperationTimeoutError):
            result = ucapi.StatusCodes.TIMEOUT
            _LOG.warning(
                "[%s] Operation timed out: %s%s",
                self.log_id,
                func.__name__,
                args,
            )
        except (pyatv.exceptions.ConnectionFailedError, pyatv.exceptions.ConnectionLostError) as err:
            result = ucapi.StatusCodes.SERVICE_UNAVAILABLE
            _LOG.warning("[%s] ATV network error (%s%s): %s", self.log_id, func.__name__, args, err)
        except pyatv.exceptions.AuthenticationError as err:
            result = ucapi.StatusCodes.UNAUTHORIZED
            _LOG.warning("[%s] Authentication error (%s%s): %s", self.log_id, func.__name__, args, err)
        except (pyatv.exceptions.NoCredentialsError, pyatv.exceptions.InvalidCredentialsError) as err:
            result = ucapi.StatusCodes.UNAUTHORIZED
            _LOG.warning("[%s] Credential error (%s%s): %s", self.log_id, func.__name__, args, err)

        except pyatv.exceptions.CommandError as err:
            result = ucapi.StatusCodes.BAD_REQUEST
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
            result = ucapi.StatusCodes.SERVICE_UNAVAILABLE
            _LOG.error("[%s] Command is blocked (%s%s), reconnecting...", self.log_id, func.__name__, args)
            self._handle_disconnect()
        except Exception as err:  # pylint: disable=broad-exception-caught
            _LOG.exception("[%s] Error %s occurred in method %s%s", self.log_id, err, func.__name__, args)

        return result

    return wrapper


class AppleTv:
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
        self._state: DeviceState | None = None
        self._app_list: dict[str, str] = {}

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
        _LOG.exception("[%s] Lost connection", self.log_id)
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
        self.events.emit(EVENTS.DISCONNECTED, self._device.identifier)
        self._start_connect_loop()

    def volume_update(self, _old_level: float, new_level: float) -> None:
        """Volume level change callback."""
        _LOG.debug("[%s] Volume level: %d", self.log_id, new_level)
        update = {"volume": new_level}
        self.events.emit(EVENTS.UPDATE, self._device.identifier, update)

    def outputdevices_update(self, old_devices, new_devices) -> None:
        """Output device change callback handler, for example airplay speaker."""
        # print('Output devices changed from {0:s} to {1:s}'.format(old_devices, new_devices))
        # TODO check if this could be used to better handle volume control (if it's available or not)

    async def _find_atv(self) -> pyatv.interface.BaseConfig | None:
        """Find a specific Apple TV on the network by identifier."""
        hosts = [self._device.address] if self._device.address else None
        atvs = await pyatv.scan(self._loop, identifier=self._device.identifier, hosts=hosts)
        if not atvs:
            return None

        return atvs[0]

    def add_credentials(self, credentials: dict[AtvProtocol, str]) -> None:
        """Add credentials for a protocol."""
        self._device.credentials.append(credentials)

    def get_credentials(self) -> list[dict[AtvProtocol, str]]:
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

        self.events.emit(EVENTS.CONNECTED, self._device.identifier)
        _LOG.debug("[%s] Connected", self.log_id)

    async def _connect_once(self) -> None:
        try:
            if conf := await self._find_atv():
                await self._connect(conf)
        except pyatv.exceptions.AuthenticationError:
            _LOG.warning("[%s] Could not connect: auth error", self.log_id)
            await self.disconnect()
            return
        except asyncio.CancelledError:
            pass
        except Exception as err:  # pylint: disable=broad-exception-caught
            _LOG.warning("[%s] Could not connect: %s", self.log_id, err)
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
            self.events.emit(EVENTS.DISCONNECTED, self._device.identifier)
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

    async def _process_update(self, data: pyatv.interface.Playing) -> None:  # pylint: disable=too-many-branches
        _LOG.debug("[%s] Process update", self.log_id)

        update = {}

        # We only update device state (playing, paused, etc) if the power state is On
        # otherwise we'll set the state to Off in the polling method
        self._state = data.device_state
        update["state"] = data.device_state

        update["position"] = data.position if data.position else 0
        update["total_time"] = data.total_time if data.total_time else 0

        # image operations are expensive, so we only do it when the hash changed
        if self._state == DeviceState.Playing:
            try:
                artwork = await self._atv.metadata.artwork(width=ARTWORK_WIDTH, height=ARTWORK_HEIGHT)
                if artwork:
                    artwork_encoded = "data:image/png;base64," + base64.b64encode(artwork.bytes).decode("utf-8")
                    update["artwork"] = artwork_encoded
            except Exception as err:  # pylint: disable=broad-exception-caught
                _LOG.warning("[%s] Error while updating the artwork: %s", self.log_id, err)

        if data.title is not None:
            # TODO filter out non-printable characters, for example all emojis
            # workaround for Plex DVR
            if data.title.startswith("(null):"):
                title = data.title.removeprefix("(null):").strip()
            else:
                title = data.title
            update["title"] = title
        else:
            update["title"] = ""

        update["artist"] = data.artist if data.artist else ""
        update["album"] = data.album if data.album else ""

        if data.media_type is not None:
            update["media_type"] = data.media_type

        if data.repeat is not None:
            match data.repeat:
                case RepeatState.Off:
                    update["repeat"] = "OFF"
                case RepeatState.All:
                    update["repeat"] = "ALL"
                case RepeatState.Track:
                    update["repeat"] = "ONE"

        if data.shuffle is not None:
            update["shuffle"] = data.shuffle in (ShuffleState.Albums, ShuffleState.Songs)

        self.events.emit(EVENTS.UPDATE, self._device.identifier, update)

    async def _update_app_list(self) -> None:
        _LOG.debug("[%s] Updating app list", self.log_id)
        update = {}

        try:
            update["sourceList"] = []
            app_list = await self._atv.apps.app_list()
            for app in app_list:
                self._app_list[app.name] = app.identifier
                update["sourceList"].append(app.name)
        except pyatv.exceptions.NotSupportedError:
            _LOG.warning("[%s] App list is not supported", self.log_id)
        except pyatv.exceptions.ProtocolError:
            _LOG.warning("[%s] App list: protocol error", self.log_id)

        self.events.emit(EVENTS.UPDATE, self._device.identifier, update)

    async def _poll_worker(self) -> None:
        await asyncio.sleep(2)
        while self._atv is not None:
            update = {}

            # Push updates are not reliable for power events, and if the device is in standby it reports state idle!
            if self._is_feature_available(FeatureName.PowerState):
                # Off isn't sent with push updates with the current pyatv library
                # Care must be taken to not override certain states like playing and paused
                if self._atv.power.power_state == PowerState.Off:
                    # The Off state is important to wakeup the device in the command handler
                    update["state"] = self._atv.power.power_state
                elif self._atv.power.power_state == PowerState.On and self._state not in (
                    DeviceState.Playing,
                    DeviceState.Paused,
                    DeviceState.Stopped,
                    DeviceState.Seeking,
                    DeviceState.Loading,
                ):
                    update["state"] = self._atv.power.power_state

            if self._is_feature_available(FeatureName.App) and self._atv.metadata.app.name:
                update["source"] = self._atv.metadata.app.name
                if playing := await self._atv.metadata.playing():
                    update["position"] = playing.position if playing.position else 0
                    update["total_time"] = playing.total_time if playing.total_time else 0

            if update:
                self.events.emit(EVENTS.UPDATE, self._device.identifier, update)

            await asyncio.sleep(self._poll_interval)

    def _is_feature_available(self, feature: FeatureName) -> bool:
        if self._atv:
            return self._atv.features.in_state(FeatureState.Available, feature)
        return False

    async def _system_status(self) -> SystemStatus:
        try:
            # TODO check if there's a nicer way to get to the CompanionAPI
            # Screensaver state is only accessible in SystemStatus
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
    async def turn_on(self) -> ucapi.StatusCodes:
        """Turn device on."""
        await self._atv.power.turn_on()

    @async_handle_atvlib_errors
    async def turn_off(self) -> ucapi.StatusCodes:
        """Turn device off."""
        await self._atv.power.turn_off()

    @async_handle_atvlib_errors
    async def play_pause(self) -> ucapi.StatusCodes:
        """Toggle between play and pause."""
        await self._atv.remote_control.play_pause()

    @async_handle_atvlib_errors
    async def fast_forward(self) -> ucapi.StatusCodes:
        """Long press key right for fast-forward."""
        await self._atv.remote_control.right(InputAction.Hold)

    @async_handle_atvlib_errors
    async def rewind(self) -> ucapi.StatusCodes:
        """Long press key left for rewind."""
        await self._atv.remote_control.left(InputAction.Hold)

    @async_handle_atvlib_errors
    async def fast_forward_companion(self) -> ucapi.StatusCodes:
        """Fast-forward using companion protocol."""
        companion = cast(FacadeRemoteControl, self._atv.remote_control).get(Protocol.Companion)
        if companion:
            await companion.api.mediacontrol_command(command=MediaControlCommand.FastForwardBegin)
        else:
            await self._atv.remote_control.right(InputAction.Hold)

    @async_handle_atvlib_errors
    async def rewind_companion(self) -> ucapi.StatusCodes:
        """Rewind using companion protocol."""
        companion = cast(FacadeRemoteControl, self._atv.remote_control).get(Protocol.Companion)
        if companion:
            await companion.api.mediacontrol_command(command=MediaControlCommand.RewindBegin)
        else:
            await self._atv.remote_control.left(InputAction.Hold)

    @async_handle_atvlib_errors
    async def next(self) -> ucapi.StatusCodes:
        """Press key next."""
        if self._is_feature_available(FeatureName.Next):  # to prevent timeout errors
            await self._atv.remote_control.next()

    @async_handle_atvlib_errors
    async def previous(self) -> ucapi.StatusCodes:
        """Press key previous."""
        if self._is_feature_available(FeatureName.Previous):
            await self._atv.remote_control.previous()

    @async_handle_atvlib_errors
    async def skip_forward(self) -> ucapi.StatusCodes:
        """Skip forward a time interval.

        Skip interval is typically 15-30s, but is decided by the app.
        """
        if self._is_feature_available(FeatureName.SkipForward):
            await self._atv.remote_control.skip_forward()

    @async_handle_atvlib_errors
    async def skip_backward(self) -> ucapi.StatusCodes:
        """Skip backwards a time interval.

        Skip interval is typically 15-30s, but is decided by the app.
        """
        if self._is_feature_available(FeatureName.SkipBackward):
            await self._atv.remote_control.skip_backward()

    @async_handle_atvlib_errors
    async def set_repeat(self, mode: str) -> ucapi.StatusCodes:
        """Change repeat state."""
        match mode:
            case "OFF":
                repeat = RepeatState.Off
            case "ALL":
                repeat = RepeatState.All
            case "ONE":
                repeat = RepeatState.Track
            case _:
                return ucapi.StatusCodes.BAD_REQUEST
        if self._is_feature_available(FeatureName.Repeat):
            await self._atv.remote_control.set_repeat(repeat)

    @async_handle_atvlib_errors
    async def set_shuffle(self, mode: bool) -> ucapi.StatusCodes:
        """Change shuffle mode to on or off."""
        if self._is_feature_available(FeatureName.Shuffle):
            await self._atv.remote_control.set_shuffle(ShuffleState.Albums if mode else ShuffleState.Off)

    @async_handle_atvlib_errors
    async def volume_up(self) -> ucapi.StatusCodes:
        """Press key volume up."""
        await self._atv.audio.volume_up()

    @async_handle_atvlib_errors
    async def volume_down(self) -> ucapi.StatusCodes:
        """Press key volume down."""
        await self._atv.audio.volume_down()

    @async_handle_atvlib_errors
    async def cursor_up(self) -> ucapi.StatusCodes:
        """Press key up."""
        await self._atv.remote_control.up()

    @async_handle_atvlib_errors
    async def cursor_down(self) -> ucapi.StatusCodes:
        """Press key down."""
        await self._atv.remote_control.down()

    @async_handle_atvlib_errors
    async def cursor_left(self) -> ucapi.StatusCodes:
        """Press key left."""
        await self._atv.remote_control.left()

    @async_handle_atvlib_errors
    async def cursor_right(self) -> ucapi.StatusCodes:
        """Press key right."""
        await self._atv.remote_control.right()

    @async_handle_atvlib_errors
    async def cursor_select(self) -> ucapi.StatusCodes:
        """Press key select."""
        await self._atv.remote_control.select()

    @async_handle_atvlib_errors
    async def context_menu(self) -> ucapi.StatusCodes:
        """Press and hold select key for one second to bring up context menu in most apps."""
        await self._atv.remote_control.select(InputAction.Hold)

    @async_handle_atvlib_errors
    async def home(self) -> ucapi.StatusCodes:
        """Press key home."""
        await self._atv.remote_control.home()

    @async_handle_atvlib_errors
    async def control_center(self) -> ucapi.StatusCodes:
        """Show control center: press and hold home key for one second."""
        await self._atv.remote_control.home(InputAction.Hold)

    @async_handle_atvlib_errors
    async def menu(self) -> ucapi.StatusCodes:
        """Press key menu."""
        await self._atv.remote_control.menu()

    @async_handle_atvlib_errors
    async def top_menu(self) -> ucapi.StatusCodes:
        """Go to top menu: press and hold menu key for one second."""
        await self._atv.remote_control.menu(InputAction.Hold)

    @async_handle_atvlib_errors
    async def channel_up(self) -> ucapi.StatusCodes:
        """Select next channel."""
        if self._is_feature_available(FeatureName.ChannelUp):
            await self._atv.remote_control.channel_up()

    @async_handle_atvlib_errors
    async def channel_down(self) -> ucapi.StatusCodes:
        """Select previous channel."""
        if self._is_feature_available(FeatureName.ChannelDown):
            await self._atv.remote_control.channel_down()

    @async_handle_atvlib_errors
    async def screensaver(self) -> ucapi.StatusCodes:
        """Start screensaver."""
        try:
            if self._is_feature_available(FeatureName.Screensaver):
                await self._atv.remote_control.screensaver()
        except pyatv.exceptions.ProtocolError:
            # workaround: command succeeds and screensaver is started, but always returns
            # ProtocolError: Command _hidC failed
            pass

    @async_handle_atvlib_errors
    async def launch_app(self, app_name: str) -> ucapi.StatusCodes:
        """Launch an app based on bundle ID or URL."""
        await self._atv.apps.launch_app(self._app_list[app_name])

    @async_handle_atvlib_errors
    async def app_switcher(self) -> ucapi.StatusCodes:
        """Press the TV/Control Center button two times to open the App Switcher."""
        await self._atv.remote_control.home(InputAction.DoubleTap)
