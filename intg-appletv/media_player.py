"""
Media-player entity functions.

:copyright: (c) 2023 by Unfolded Circle ApS.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import asyncio
import logging
from enum import Enum, StrEnum
from typing import Any, Type

import tv
from config import AppleTVEntity, AtvDevice
from hid import UsagePage
from hid.consumer_control_code import ConsumerControlCode
from ucapi import MediaPlayer, StatusCodes, media_player
from ucapi.media_player import (
    Attributes,
    Commands,
    DeviceClasses,
    Features,
    Options,
)

_LOG = logging.getLogger(__name__)
# Experimental features, don't seem to work / supported (yet) with ATV4
ENABLE_REPEAT_FEAT = False
ENABLE_SHUFFLE_FEAT = False


class SimpleCommands(StrEnum):
    """Additional simple commands of the Apple TV not covered by media-player features."""

    TOP_MENU = "TOP_MENU"
    """Go to home screen."""
    APP_SWITCHER = "APP_SWITCHER"
    """Show running applications."""
    SCREENSAVER = "SCREENSAVER"
    """Run screensaver."""
    SKIP_FORWARD = "SKIP_FORWARD"
    """Skip forward a time interval."""
    SKIP_BACKWARD = "SKIP_BACKWARD"
    """Skip forward a time interval."""
    FAST_FORWARD_BEGIN = "FAST_FORWARD_BEGIN"
    """Fast forward using Companion protocol."""
    REWIND_BEGIN = "REWIND_BEGIN"
    """Rewind using Companion protocol."""
    SWIPE_LEFT = "SWIPE_LEFT"
    """Swipe left using Companion protocol."""
    SWIPE_RIGHT = "SWIPE_RIGHT"
    """Swipe right using Companion protocol."""
    SWIPE_UP = "SWIPE_UP"
    """Swipe up using Companion protocol."""
    SWIPE_DOWN = "SWIPE_DOWN"
    """Swipe down using Companion protocol."""
    PLAY = "PLAY"
    """Send play command. App specific! Some treat it as play_pause."""
    PAUSE = "PAUSE"
    """Send pause command. App specific! Some treat it as play_pause."""
    PLAY_PAUSE_KEY = "PLAY_PAUSE_KEY"
    """Alternative play/pause command by sending a HID key press."""


def filter_attributes(attributes, attribute_type: Type[Enum]) -> dict[str, Any]:
    """Filter attributes based on an Enum class."""
    valid_keys = {e.value for e in attribute_type}
    return {k: v for k, v in attributes.items() if k in valid_keys}


def _get_cmd_param(name: str, params: dict[str, Any] | None) -> str | bool | None:
    if params is None:
        return None
    return params.get(name)


class AppleTVMediaPlayer(AppleTVEntity, MediaPlayer):
    """Representation of a AppleTV Media Player entity."""

    def __init__(self, config_device: AtvDevice, device: tv.AppleTv):
        """Initialize the class."""
        # pylint: disable = R0801
        self._device = device
        # entity_id = create_entity_id(config_device.name, EntityTypes.MEDIA_PLAYER)
        entity_id = config_device.identifier
        features = [
            Features.ON_OFF,
            Features.VOLUME,
            Features.VOLUME_UP_DOWN,
            Features.MUTE_TOGGLE,
            Features.PLAY_PAUSE,
            Features.STOP,
            Features.NEXT,
            Features.PREVIOUS,
            Features.MEDIA_DURATION,
            Features.MEDIA_POSITION,
            Features.MEDIA_TITLE,
            Features.MEDIA_ARTIST,
            Features.MEDIA_ALBUM,
            Features.MEDIA_IMAGE_URL,
            Features.MEDIA_TYPE,
            Features.HOME,
            Features.CHANNEL_SWITCHER,
            Features.DPAD,
            Features.SELECT_SOURCE,
            Features.CONTEXT_MENU,
            Features.MENU,
            Features.REWIND,
            Features.FAST_FORWARD,
            Features.SELECT_SOUND_MODE,
            Features.SEEK,
            Features.GUIDE,
        ]
        if ENABLE_REPEAT_FEAT:
            features.append(Features.REPEAT)
        if ENABLE_SHUFFLE_FEAT:
            features.append(Features.SHUFFLE)

        attributes = filter_attributes(device.attributes, Attributes)
        options = {Options.SIMPLE_COMMANDS: list(SimpleCommands)}
        super().__init__(
            entity_id, config_device.name, features, attributes, device_class=DeviceClasses.TV, options=options
        )

    @property
    def deviceid(self) -> str:
        """Return the device identifier."""
        return self._device.identifier

    async def _playpause_in_screensaver(self) -> StatusCodes | None:
        """
        Mimic the original ATV remote behaviour (one can also call it a bunch of workarounds).

        Screensaver active: play/pause button exits screensaver. If a playback was paused, resume it.

        :param state: the media-player state
        :param device: the device
        :return: None if screensaver was not active, a StatusCode otherwise
        """
        # tvOS 18.4 will raise an exception https://github.com/postlund/pyatv/issues/2648
        # Screensaver state is no longer accessible
        # pylint: disable=W0718
        try:
            if self._device.media_state != media_player.States.PLAYING and await self._device.screensaver_active():
                _LOG.debug("Screensaver is running, sending menu command for play_pause to exit")
                await self._device.menu()
                if self._device.media_state == media_player.States.PAUSED:
                    # another awkwardness: the play_pause button doesn't work anymore after exiting the screensaver.
                    # One has to send a dpad select first to start playback. Afterward, play_pause works again...
                    await asyncio.sleep(1)  # delay required, otherwise the second button press is ignored
                    return await self._device.cursor_select()
                # Nothing was playing, only the screensaver was active
                return StatusCodes.OK
        except Exception:
            pass
        return None

    async def command(self, cmd_id: str, params: dict[str, Any] | None = None, *, websocket: Any) -> StatusCodes:
        """
        Media-player entity command handler.

        Called by the integration-API if a command is sent to a configured media-player entity.

        :param cmd_id: command
        :param params: optional command parameters
        :param websocket: optional websocket connection. Allows for directed event
                          callbacks instead of broadcasts.
        :return: status code of the command request
        """
        # pylint: disable=R0912,R0915
        _LOG.info("Got %s command request: %s %s", self.id, cmd_id, params if params else "")

        # If the entity is OFF (device is in standby), we turn it on regardless of the actual command
        if self._device.is_on is None or self._device.is_on is False:
            _LOG.debug("Device not connected, reconnect")
            await self._device.connect()

        state = self._device.media_state

        # TODO #15 implement proper fix for correct entity OFF state (it may not remain in OFF state if connection is
        #  established) + online check if we think it is in standby mode.
        if state == media_player.States.OFF and cmd_id != Commands.OFF:
            _LOG.debug("Device is off, sending turn on command")
            # quick & dirty workaround for #15: the entity state is not always correct!
            res = await self._device.turn_on()
            if res != StatusCodes.OK:
                return res

        # Only proceed if self._device connection is established
        if self._device.is_on is False:
            return StatusCodes.SERVICE_UNAVAILABLE

        res = StatusCodes.BAD_REQUEST

        match cmd_id:
            case Commands.PLAY_PAUSE:
                if res := await self._playpause_in_screensaver():
                    return res
                res = await self._device.play_pause()
            case SimpleCommands.PLAY_PAUSE_KEY:
                if res := await self._playpause_in_screensaver():
                    return res
                res = await self._device.send_hid_key(UsagePage.CONSUMER, ConsumerControlCode.PLAY_PAUSE)
            case Commands.STOP:
                res = await self._device.stop()
            case Commands.NEXT:
                res = await self._device.next()
            case Commands.PREVIOUS:
                res = await self._device.previous()
            case Commands.VOLUME_UP:
                res = await self._device.volume_up()
            case Commands.VOLUME_DOWN:
                res = await self._device.volume_down()
            case Commands.VOLUME:
                res = await self._device.volume_set(params.get("volume"))
            case Commands.MUTE_TOGGLE:
                res = await self._device.send_hid_key(UsagePage.CONSUMER, ConsumerControlCode.MUTE)
            case Commands.ON:
                res = await self._device.turn_on()
            case Commands.OFF:
                res = await self._device.turn_off()
            case Commands.CURSOR_UP:
                res = await self._device.cursor_up()
            case Commands.CURSOR_DOWN:
                res = await self._device.cursor_down()
            case Commands.CURSOR_LEFT:
                res = await self._device.cursor_left()
            case Commands.CURSOR_RIGHT:
                res = await self._device.cursor_right()
            case Commands.CURSOR_ENTER:
                res = await self._device.cursor_select()
            case Commands.REWIND:
                res = await self._device.rewind()
            case Commands.FAST_FORWARD:
                res = await self._device.fast_forward()
            case Commands.REPEAT:
                mode = _get_cmd_param("repeat", params)
                res = await self._device.set_repeat(mode) if mode else StatusCodes.BAD_REQUEST
            case Commands.SHUFFLE:
                mode = _get_cmd_param("shuffle", params)
                res = await self._device.set_shuffle(mode) if isinstance(mode, bool) else StatusCodes.BAD_REQUEST
            case Commands.CONTEXT_MENU:
                res = await self._device.context_menu()
            case Commands.MENU:
                res = await self._device.control_center()
            case Commands.HOME:
                res = await self._device.home()
                # Request a defer update because music can play in the background
                asyncio.create_task(self._device.deferred_state_update())
            case Commands.BACK:
                res = await self._device.menu()
            case Commands.CHANNEL_DOWN:
                res = await self._device.channel_down()
            case Commands.CHANNEL_UP:
                res = await self._device.channel_up()
            case Commands.SELECT_SOURCE:
                res = await self._device.launch_app(params["source"])
            case Commands.GUIDE:
                res = await self._device.toggle_guide()
            # --- simple commands ---
            case SimpleCommands.TOP_MENU:
                res = await self._device.top_menu()
            case SimpleCommands.APP_SWITCHER:
                res = await self._device.app_switcher()
            case SimpleCommands.SCREENSAVER:
                res = await self._device.screensaver()
            case SimpleCommands.SKIP_FORWARD:
                res = await self._device.skip_forward()
            case SimpleCommands.SKIP_BACKWARD:
                res = await self._device.skip_backward()
            case SimpleCommands.FAST_FORWARD_BEGIN:
                res = await self._device.fast_forward_companion()
            case SimpleCommands.REWIND_BEGIN:
                res = await self._device.rewind_companion()
            case Commands.SELECT_SOUND_MODE:
                mode = _get_cmd_param("mode", params)
                res = await self._device.set_output_device(mode)
            case Commands.SEEK:
                res = await self._device.set_media_position(params.get("media_position", 0))
            case SimpleCommands.SWIPE_LEFT:
                res = await self._device.swipe(1000, 500, 50, 500, 200)
            case SimpleCommands.SWIPE_RIGHT:
                res = await self._device.swipe(50, 500, 1000, 500, 200)
            case SimpleCommands.SWIPE_UP:
                res = await self._device.swipe(500, 1000, 500, 50, 200)
            case SimpleCommands.SWIPE_DOWN:
                res = await self._device.swipe(500, 50, 500, 1000, 200)
            case SimpleCommands.PLAY:
                res = await self._device.play()
            case SimpleCommands.PAUSE:
                res = await self._device.pause()

        return res
