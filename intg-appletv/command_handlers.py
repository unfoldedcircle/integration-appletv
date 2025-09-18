"""
This module handles command requests.

:copyright: (c) 2023-2024 by Unfolded Circle ApS.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

# pylint: disable=too-many-statements
import asyncio
import logging
from typing import Any

import tv
import ucapi
from globals import _configured_atvs, api
from hid import UsagePage
from hid.consumer_control_code import ConsumerControlCode
from pyatv.const import PowerState
from simple_commands import SimpleCommands
from ucapi import MediaPlayer, media_player

_LOG = logging.getLogger("command_handlers")


async def media_player_cmd_handler(
    entity: MediaPlayer, cmd_id: str, params: dict[str, Any] | None
) -> ucapi.StatusCodes:
    """
    Media-player entity command handler.

    Called by the integration-API if a command is sent to a configured media-player entity.

    :param entity: media-player entity
    :param cmd_id: command
    :param params: optional command parameters
    :return: status code of the command. StatusCodes.OK if the command succeeded.
    """
    _LOG.info("Got %s command request: %s %s", entity.id, cmd_id, params if params else "")

    atv_id = entity.id
    device = _configured_atvs[atv_id]

    configured_entity = api.configured_entities.get(entity.id)

    if configured_entity is None:
        _LOG.warning("No device found for entity: %s", entity.id)
        return ucapi.StatusCodes.SERVICE_UNAVAILABLE

    # If the entity is OFF (device is in standby), we turn it on regardless of the actual command
    if device.is_on is None or device.is_on is False:
        _LOG.debug("Device not connected, reconnect")
        await device.connect()

    state = configured_entity.attributes[media_player.Attributes.STATE]

    # TODO #15 implement proper fix for correct entity OFF state (it may not remain in OFF state if connection is
    #  established) + online check if we think it is in standby mode.
    if state == media_player.States.OFF and not (cmd_id is media_player.Commands.OFF or media_player.Commands.TOGGLE):
        _LOG.debug("Device is off, sending turn on command")
        # quick & dirty workaround for #15: the entity state is not always correct!
        res = await device.turn_on()
        if res != ucapi.StatusCodes.OK:
            return res

    # TODO: This seems wrong, why not use self._atv.power.power_state == PowerState.On?
    # Only proceed if device connection is established
    if device.is_on is False:
        return ucapi.StatusCodes.SERVICE_UNAVAILABLE

    res = ucapi.StatusCodes.BAD_REQUEST

    match cmd_id:
        case media_player.Commands.PLAY_PAUSE:
            if res := await _playpause_in_screensaver(state, device):
                return res
            res = await device.play_pause()
        case SimpleCommands.PLAY_PAUSE_KEY:
            if res := await _playpause_in_screensaver(state, device):
                return res
            res = await device.send_hid_key(UsagePage.CONSUMER, ConsumerControlCode.PLAY_PAUSE)
        case media_player.Commands.STOP:
            res = await device.stop()
        case media_player.Commands.NEXT:
            res = await device.next()
        case media_player.Commands.PREVIOUS:
            res = await device.previous()
        case media_player.Commands.VOLUME_UP:
            res = await device.volume_up()
        case media_player.Commands.VOLUME_DOWN:
            res = await device.volume_down()
        case media_player.Commands.VOLUME:
            res = await device.volume_set(params.get("volume"))
        case media_player.Commands.MUTE_TOGGLE:
            res = await device.send_hid_key(UsagePage.CONSUMER, ConsumerControlCode.MUTE)
        case media_player.Commands.ON:
            res = await device.turn_on()
        case media_player.Commands.OFF:
            res = await device.turn_off()
        case media_player.Commands.TOGGLE:
            # pylint: disable=W0212
            if device._atv.power.power_state == PowerState.On:
                res = await device.turn_off()
            else:
                res = await device.turn_on()
        case media_player.Commands.CURSOR_UP:
            res = await device.cursor_up()
        case media_player.Commands.CURSOR_DOWN:
            res = await device.cursor_down()
        case media_player.Commands.CURSOR_LEFT:
            res = await device.cursor_left()
        case media_player.Commands.CURSOR_RIGHT:
            res = await device.cursor_right()
        case media_player.Commands.CURSOR_ENTER:
            res = await device.cursor_select()
        case media_player.Commands.REWIND:
            res = await device.rewind()
        case media_player.Commands.FAST_FORWARD:
            res = await device.fast_forward()
        case media_player.Commands.REPEAT:
            mode = _get_cmd_param("repeat", params)
            res = await device.set_repeat(mode) if mode else ucapi.StatusCodes.BAD_REQUEST
        case media_player.Commands.SHUFFLE:
            mode = _get_cmd_param("shuffle", params)
            res = await device.set_shuffle(mode) if isinstance(mode, bool) else ucapi.StatusCodes.BAD_REQUEST
        case media_player.Commands.CONTEXT_MENU:
            res = await device.context_menu()
        case media_player.Commands.MENU:
            res = await device.control_center()
        case media_player.Commands.HOME:
            res = await device.home()

            # we wait a bit to get a push update, because music can play in the background
            await asyncio.sleep(1)
            if configured_entity.attributes[media_player.Attributes.STATE] != media_player.States.PLAYING:
                # if nothing is playing: clear the playing information
                attributes = {
                    media_player.Attributes.MEDIA_IMAGE_URL: "",
                    media_player.Attributes.MEDIA_ALBUM: "",
                    media_player.Attributes.MEDIA_ARTIST: "",
                    media_player.Attributes.MEDIA_TITLE: "",
                    media_player.Attributes.MEDIA_TYPE: "",
                    media_player.Attributes.SOURCE: "",
                    media_player.Attributes.MEDIA_DURATION: 0,
                }
                api.configured_entities.update_attributes(entity.id, attributes)
        case media_player.Commands.BACK:
            res = await device.menu()
        case media_player.Commands.CHANNEL_DOWN:
            res = await device.channel_down()
        case media_player.Commands.CHANNEL_UP:
            res = await device.channel_up()
        case media_player.Commands.SELECT_SOURCE:
            res = await device.launch_app(params["source"])
        case media_player.Commands.GUIDE:
            res = await device.toggle_guide()
        # --- simple commands ---
        case SimpleCommands.TOP_MENU:
            res = await device.top_menu()
        case SimpleCommands.APP_SWITCHER:
            res = await device.app_switcher()
        case SimpleCommands.SCREENSAVER:
            res = await device.screensaver()
        case SimpleCommands.SKIP_FORWARD:
            res = await device.skip_forward()
        case SimpleCommands.SKIP_BACKWARD:
            res = await device.skip_backward()
        case SimpleCommands.FAST_FORWARD_BEGIN:
            res = await device.fast_forward_companion()
        case SimpleCommands.REWIND_BEGIN:
            res = await device.rewind_companion()
        case media_player.Commands.SELECT_SOUND_MODE:
            mode = _get_cmd_param("mode", params)
            res = await device.set_output_device(mode)
        case media_player.Commands.SEEK:
            res = await device.set_media_position(params.get("media_position", 0))
        case SimpleCommands.SWIPE_LEFT:
            res = await device.swipe(1000, 500, 50, 500, 200)
        case SimpleCommands.SWIPE_RIGHT:
            res = await device.swipe(50, 500, 1000, 500, 200)
        case SimpleCommands.SWIPE_UP:
            res = await device.swipe(500, 1000, 500, 50, 200)
        case SimpleCommands.SWIPE_DOWN:
            res = await device.swipe(500, 50, 500, 1000, 200)
        case SimpleCommands.PLAY:
            res = await device.play()
        case SimpleCommands.PAUSE:
            res = await device.pause()

    return res


async def _playpause_in_screensaver(state: media_player.States, device: tv.AppleTv) -> ucapi.StatusCodes | None:
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
        if state != media_player.States.PLAYING and await device.screensaver_active():
            _LOG.debug("Screensaver is running, sending menu command for play_pause to exit")
            await device.menu()
            if state == media_player.States.PAUSED:
                # another awkwardness: the play_pause button doesn't work anymore after exiting the screensaver.
                # One has to send a dpad select first to start playback. Afterward, play_pause works again...
                await asyncio.sleep(1)  # delay required, otherwise the second button press is ignored
                return await device.cursor_select()
            # Nothing was playing, only the screensaver was active
            return ucapi.StatusCodes.OK
    except Exception:
        pass
    return None


def _get_cmd_param(name: str, params: dict[str, Any] | None) -> str | bool | None:
    if params is None:
        return None
    return params.get(name)
