"""
Remote entity functions.

:copyright: (c) 2023-2024 by Unfolded Circle ApS.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import logging
from typing import Any, Type

import tv
import ucapi.remote
from config import AppleTVEntity, AtvDevice, create_entity_id
from media_player import AppleTVMediaPlayer, SimpleCommands
from ucapi import EntityTypes, Remote, StatusCodes, media_player
from ucapi.remote import Attributes, Commands, Features
from ucapi.ui import Buttons

_LOG = logging.getLogger(__name__)


REMOTE_BUTTONS_MAPPING: list[dict[str, Any]] = [
    {"button": Buttons.BACK, "short_press": {"cmd_id": media_player.Commands.BACK}},
    {"button": Buttons.DPAD_DOWN, "short_press": {"cmd_id": media_player.Commands.CURSOR_DOWN}},
    {"button": Buttons.DPAD_LEFT, "short_press": {"cmd_id": media_player.Commands.CURSOR_LEFT}},
    {"button": Buttons.DPAD_RIGHT, "short_press": {"cmd_id": media_player.Commands.CURSOR_RIGHT}},
    {"button": Buttons.DPAD_MIDDLE, "short_press": {"cmd_id": media_player.Commands.CURSOR_ENTER}},
    {"button": Buttons.DPAD_UP, "short_press": {"cmd_id": media_player.Commands.CURSOR_UP}},
    {"button": Buttons.VOLUME_UP, "short_press": {"cmd_id": media_player.Commands.VOLUME_UP}},
    {"button": Buttons.VOLUME_DOWN, "short_press": {"cmd_id": media_player.Commands.VOLUME_DOWN}},
    {"button": Buttons.MUTE, "short_press": {"cmd_id": media_player.Commands.MUTE_TOGGLE}},
    {"button": Buttons.POWER, "short_press": {"cmd_id": media_player.Commands.TOGGLE}},
    {"button": Buttons.PREV, "short_press": {"cmd_id": media_player.Commands.PREVIOUS}},
    {"button": Buttons.PLAY, "short_press": {"cmd_id": media_player.Commands.PLAY_PAUSE}},
    {"button": Buttons.NEXT, "short_press": {"cmd_id": media_player.Commands.NEXT}},
    {"button": "STOP", "short_press": {"cmd_id": media_player.Commands.STOP}},
    {"button": "MENU", "short_press": {"cmd_id": media_player.Commands.CONTEXT_MENU}},
]


def _main_ui_page() -> dict[str, Any]:
    return {
        "page_id": "apple_tv_commands_main",
        "name": "Main",
        "grid": {"width": 1, "height": 5},
        "items": [
            {
                "command": {"cmd_id": SimpleCommands.TOP_MENU.value},
                "location": {"x": 0, "y": 0},
                "size": {"height": 1, "width": 1},
                "text": "Top Menu",
                "type": "text",
            },
            {
                "command": {"cmd_id": SimpleCommands.APP_SWITCHER.value},
                "location": {"x": 0, "y": 1},
                "size": {"height": 1, "width": 1},
                "text": "App Switcher",
                "type": "text",
            },
            {
                "command": {"cmd_id": media_player.Commands.CONTEXT_MENU.value},
                "location": {"x": 0, "y": 2},
                "size": {"height": 1, "width": 1},
                "text": "Context Menu",
                "type": "text",
            },
            {
                "command": {"cmd_id": SimpleCommands.SCREENSAVER.value},
                "location": {"x": 0, "y": 3},
                "size": {"height": 1, "width": 1},
                "text": "Screensaver",
                "type": "text",
            },
            {
                "command": {"cmd_id": media_player.Commands.GUIDE.value},
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
        simple_commands = [
            Buttons.BACK.value,
            Buttons.HOME.value,
            Buttons.VOLUME_UP.value,
            Buttons.VOLUME_DOWN.value,
            Buttons.MUTE.value,
            Buttons.DPAD_UP.value,
            Buttons.DPAD_DOWN.value,
            Buttons.DPAD_LEFT.value,
            Buttons.DPAD_RIGHT.value,
            Buttons.DPAD_MIDDLE.value,
            Buttons.CHANNEL_UP.value,
            Buttons.CHANNEL_DOWN.value,
            Buttons.PREV.value,
            Buttons.PLAY.value,
            Buttons.NEXT.value,
            Buttons.POWER.value,
            "MENU",
            "STOP",
            "GUIDE",
            *[c.value for c in SimpleCommands],
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

    @staticmethod
    def filter_attributes(attributes: dict[str, Any], attribute_type: Type[Attributes]) -> dict[str, Any]:
        """Filter attributes for the remote entity."""
        valid_keys = {e.value for e in attribute_type}
        return {k: v for k, v in attributes.items() if k in valid_keys}

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

        if cmd_id == Commands.SEND_CMD:
            inner = self._validate_inner_command(cmd_id, params.get("command", ""))
            if isinstance(inner, StatusCodes):
                return inner
            success = True
            for _ in range(repeat):
                if await self._media_player.command(inner, params) != StatusCodes.OK:
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
