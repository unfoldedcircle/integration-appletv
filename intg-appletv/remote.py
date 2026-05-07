"""
Remote entity functions.

:copyright: (c) 2023-2024 by Unfolded Circle ApS.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import logging
from typing import Any

import tv
import ucapi.remote
from config import AppleTVEntity, AtvDevice, create_entity_id
from media_player import AppleTVMediaPlayer, SimpleCommands
from ucapi import EntityTypes, Remote, StatusCodes, media_player
from ucapi.media_player import Commands as MediaPlayerCommands
from ucapi.remote import Attributes, Commands, Features
from ucapi.ui import Buttons

_LOG = logging.getLogger(__name__)


REMOTE_BUTTONS_MAPPING: list[dict[str, Any]] = [
    {"button": Buttons.BACK, "short_press": {"cmd_id": media_player.Commands.BACK.upper()}},
    {"button": Buttons.DPAD_DOWN, "short_press": {"cmd_id": media_player.Commands.CURSOR_DOWN.upper()}},
    {"button": Buttons.DPAD_LEFT, "short_press": {"cmd_id": media_player.Commands.CURSOR_LEFT.upper()}},
    {"button": Buttons.DPAD_RIGHT, "short_press": {"cmd_id": media_player.Commands.CURSOR_RIGHT.upper()}},
    {"button": Buttons.DPAD_MIDDLE, "short_press": {"cmd_id": media_player.Commands.CURSOR_ENTER.upper()}},
    {"button": Buttons.DPAD_UP, "short_press": {"cmd_id": media_player.Commands.CURSOR_UP.upper()}},
    {"button": Buttons.VOLUME_UP, "short_press": {"cmd_id": media_player.Commands.VOLUME_UP.upper()}},
    {"button": Buttons.VOLUME_DOWN, "short_press": {"cmd_id": media_player.Commands.VOLUME_DOWN.upper()}},
    {"button": Buttons.MUTE, "short_press": {"cmd_id": media_player.Commands.MUTE_TOGGLE.upper()}},
    {"button": Buttons.POWER, "short_press": {"cmd_id": media_player.Commands.TOGGLE.upper()}},
    {"button": Buttons.PREV, "short_press": {"cmd_id": SimpleCommands.REWIND_BEGIN}},
    {"button": Buttons.PLAY, "short_press": {"cmd_id": media_player.Commands.PLAY_PAUSE.upper()}},
    {"button": Buttons.NEXT, "short_press": {"cmd_id": SimpleCommands.FAST_FORWARD_BEGIN}},
    {"button": Buttons.STOP, "short_press": {"cmd_id": media_player.Commands.STOP.upper()}},
    {"button": Buttons.MENU, "short_press": {"cmd_id": media_player.Commands.CONTEXT_MENU.upper()}},
    {"button": Buttons.CHANNEL_UP, "short_press": {"cmd_id": media_player.Commands.CHANNEL_UP.upper()}},
    {"button": Buttons.CHANNEL_DOWN, "short_press": {"cmd_id": media_player.Commands.CHANNEL_DOWN.upper()}},
    {"button": Buttons.HOME, "short_press": {"cmd_id": media_player.Commands.HOME.upper()}},
]


def _main_ui_page() -> dict[str, Any]:
    return {
        "page_id": "apple_tv_commands_main",
        "name": "Main",
        "grid": {"width": 1, "height": 5},
        "items": [
            {
                "command": {"cmd_id": SimpleCommands.TOP_MENU},
                "location": {"x": 0, "y": 0},
                "size": {"height": 1, "width": 1},
                "text": "Top Menu",
                "type": "text",
            },
            {
                "command": {"cmd_id": SimpleCommands.APP_SWITCHER},
                "location": {"x": 0, "y": 1},
                "size": {"height": 1, "width": 1},
                "text": "App Switcher",
                "type": "text",
            },
            {
                "command": {"cmd_id": SimpleCommands.SCREENSAVER},
                "location": {"x": 0, "y": 2},
                "size": {"height": 1, "width": 1},
                "text": "Screensaver",
                "type": "text",
            },
            {
                "command": {"cmd_id": media_player.Commands.MENU.upper()},
                "location": {"x": 0, "y": 3},
                "size": {"height": 1, "width": 1},
                "text": "Control Center",
                "type": "text",
            },
            {
                "command": {"cmd_id": media_player.Commands.GUIDE.upper()},
                "location": {"x": 0, "y": 4},
                "size": {"height": 1, "width": 1},
                "text": "Guide",
                "type": "text",
            },
        ],
    }


def _state_from_media_player_state(state: media_player.States) -> ucapi.remote.States:
    """Map media-player state to remote state."""
    match state:
        case (
            media_player.States.ON
            | media_player.States.BUFFERING
            | media_player.States.PAUSED
            | media_player.States.PLAYING
            | media_player.States.STANDBY
        ):
            return ucapi.remote.States.ON
        case media_player.States.OFF:
            return ucapi.remote.States.OFF
        case media_player.States.UNAVAILABLE:
            return ucapi.remote.States.UNAVAILABLE
        case _:
            return ucapi.remote.States.UNKNOWN


def _key_update(key: str, value: Any, attributes: dict, original: dict) -> dict:
    if value is None:
        return attributes
    if key not in original or original[key] != value:
        attributes[key] = value
    return attributes


# pylint: disable=R0903
class AppleTVRemote(AppleTVEntity, Remote):
    """Representation of an Apple TV Remote entity."""

    def __init__(self, config_device: AtvDevice, device: tv.AppleTv, mp_entity: AppleTVMediaPlayer):
        """Initialize the class."""
        # pylint: disable=R0801
        self._device = device
        self._media_player = mp_entity
        entity_id = create_entity_id(config_device.identifier, EntityTypes.REMOTE)
        self._media_player_simple_commands = [c.value for c in SimpleCommands]
        simple_commands = [
            MediaPlayerCommands.BACK.upper(),
            MediaPlayerCommands.HOME.upper(),
            MediaPlayerCommands.VOLUME_UP.upper(),
            MediaPlayerCommands.VOLUME_DOWN.upper(),
            MediaPlayerCommands.MUTE.upper(),
            MediaPlayerCommands.CURSOR_UP.upper(),
            MediaPlayerCommands.CURSOR_DOWN.upper(),
            MediaPlayerCommands.CURSOR_LEFT.upper(),
            MediaPlayerCommands.CURSOR_RIGHT.upper(),
            MediaPlayerCommands.CURSOR_ENTER.upper(),
            MediaPlayerCommands.CHANNEL_UP.upper(),
            MediaPlayerCommands.CHANNEL_DOWN.upper(),
            MediaPlayerCommands.PREVIOUS.upper(),
            MediaPlayerCommands.PLAY_PAUSE.upper(),
            MediaPlayerCommands.NEXT.upper(),
            MediaPlayerCommands.MENU.upper(),
            MediaPlayerCommands.STOP.upper(),
            MediaPlayerCommands.GUIDE.upper(),
            MediaPlayerCommands.CONTEXT_MENU.upper(),
            *self._media_player_simple_commands,
        ]
        super().__init__(
            entity_id,
            f"{config_device.name} Remote",
            [Features.SEND_CMD, Features.ON_OFF, Features.TOGGLE],
            attributes={Attributes.STATE: _state_from_media_player_state(device.media_state)},
            simple_commands=simple_commands,
            button_mapping=REMOTE_BUTTONS_MAPPING,
            ui_pages=[_main_ui_page()],
        )

    @property
    def deviceid(self) -> str:
        """Return the device identifier."""
        return self._device.identifier

    def filter_changed_attributes(self, update: dict[str, Any]) -> dict[str, Any]:
        """Return only the changed attributes (state mapped to remote state)."""
        attributes: dict[str, Any] = {}
        if media_player.Attributes.STATE in update:
            state = _state_from_media_player_state(update[media_player.Attributes.STATE])
            _key_update(Attributes.STATE, state, attributes, self.attributes)
        return attributes

    async def command(self, cmd_id: str, params: dict[str, Any] | None = None, *, websocket: Any = None) -> StatusCodes:
        """Remote entity command handler."""
        # pylint: disable=R0911,R0912,W0613
        match cmd_id:
            case Commands.ON:
                return await self._media_player.command(media_player.Commands.ON, None)
            case Commands.OFF:
                return await self._media_player.command(media_player.Commands.OFF, None)
            case Commands.TOGGLE:
                return await self._media_player.command(media_player.Commands.TOGGLE, None)

        if cmd_id.startswith("remote."):
            _LOG.error("Command %s is not allowed.", cmd_id)
            return StatusCodes.BAD_REQUEST

        if params is None:
            return StatusCodes.BAD_REQUEST

        repeat = self._get_int_param("repeat", params, 1)
        # TODO temporary hack for hold-down buttons sending a repeat count.
        #      This will be addressed with the upcoming press-and-hold feature.
        if repeat < 1 or repeat == 4:
            repeat = 1
        elif repeat > 20:
            repeat = 20

        if cmd_id == Commands.SEND_CMD:
            inner = self._validate_inner_command(cmd_id, params.get("command", ""))
            if isinstance(inner, StatusCodes):
                return inner
            success = True
            for _ in range(repeat):
                properly_cased_command = inner if inner in self._media_player_simple_commands else inner.lower()
                if await self._media_player.command(properly_cased_command, params) != StatusCodes.OK:
                    success = False
            return StatusCodes.OK if success else StatusCodes.BAD_REQUEST

        if cmd_id == Commands.SEND_CMD_SEQUENCE:
            success = True
            for command in params.get("sequence", []):
                inner = self._validate_inner_command(cmd_id, command)
                if isinstance(inner, StatusCodes):
                    success = False
                    continue
                for _ in range(repeat):
                    if await self._media_player.command(inner, params) != StatusCodes.OK:
                        success = False
            return StatusCodes.OK if success else StatusCodes.BAD_REQUEST

        return StatusCodes.BAD_REQUEST

    @staticmethod
    def _validate_inner_command(cmd_id: str, command: str) -> str | StatusCodes:
        if not command:
            _LOG.error("Command parameter is missing for cmd_id %s", cmd_id)
            return StatusCodes.BAD_REQUEST
        if command.startswith("remote."):
            _LOG.error("Command %s is not allowed for cmd_id %s.", command, cmd_id)
            return StatusCodes.BAD_REQUEST
        return command

    @staticmethod
    def _get_int_param(param: str, params: dict[str, Any], default: int) -> int:
        try:
            value = params.get(param, default)
        except AttributeError:
            return default
        if isinstance(value, str) and len(value) > 0:
            return int(float(value))
        if isinstance(value, int):
            return value
        return default
