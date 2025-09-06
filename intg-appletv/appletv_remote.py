# pylint: disable=C0302
"""
Remote entity functions.

:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""
import logging
from typing import Any

import helpers
import ucapi.remote
from command_handlers import media_player_cmd_handler
from config import create_entity_id
from simple_commands import SimpleCommands
from ucapi import EntityTypes, MediaPlayer, Remote, StatusCodes, media_player
from ucapi.remote import Attributes, Commands, Features
from ucapi.ui import Buttons

_LOG = logging.getLogger("appletv_remote")


# pylint: disable=R0903
class AppleTvRemote(Remote):
    """Representation of a Apple TV Remote entity."""

    def __init__(self, entity_id: str, name: str, m_player: MediaPlayer):
        """Initialize the class."""
        self._media_player = m_player
        super().__init__(
            create_entity_id(entity_id, EntityTypes.REMOTE),
            f"{name} Remote",
            [Features.SEND_CMD, Features.ON_OFF],
            attributes={
                Attributes.STATE: ucapi.remote.States.UNAVAILABLE,
            },
            simple_commands=[
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
                # pylint: disable=R0801
                "MENU",
                "STOP",
                "GUIDE",
                SimpleCommands.TOP_MENU.value,
                SimpleCommands.APP_SWITCHER.value,
                SimpleCommands.SCREENSAVER.value,
                SimpleCommands.SKIP_FORWARD.value,
                SimpleCommands.SKIP_BACKWARD.value,
                SimpleCommands.FAST_FORWARD_BEGIN.value,
                SimpleCommands.REWIND_BEGIN.value,
                SimpleCommands.SWIPE_LEFT.value,
                SimpleCommands.SWIPE_RIGHT.value,
                SimpleCommands.SWIPE_UP.value,
                SimpleCommands.SWIPE_DOWN.value,
                SimpleCommands.PLAY.value,
                SimpleCommands.PAUSE.value,
                SimpleCommands.PLAY_PAUSE_KEY.value,
            ],
            button_mapping=REMOTE_BUTTONS_MAPPING,
            ui_pages=[AppleTvRemote._get_main_page()],
        )

    async def command(self, cmd_id: str, params: dict[str, Any] | None = None) -> StatusCodes:
        """
        Remote entity command handler.

        Called by the integration-API if a command is sent to a configured remote entity.

        :param cmd_id: command
        :param params: optional command parameters
        :return: status code of the command request
        """
        # pylint: disable=R0911,R0912
        # Import here to avoid circular import
        match cmd_id:
            case Commands.ON:
                return await media_player_cmd_handler(self._media_player, media_player.Commands.ON, None)
            case Commands.OFF:
                return await media_player_cmd_handler(self._media_player, media_player.Commands.OFF, None)

        if cmd_id.startswith("remote."):
            _LOG.error("Command %s is not allowed.", cmd_id)
            return StatusCodes.BAD_REQUEST

        if params is None:
            return StatusCodes.BAD_REQUEST

        if params:
            repeat = self._get_int_param("repeat", params, 1)
        else:
            repeat = 1

        if cmd_id == Commands.SEND_CMD:
            command_or_status = self._get_command_or_status_code(cmd_id, params.get("command", ""))
            if isinstance(command_or_status, StatusCodes):
                return command_or_status

            success = True
            for _ in range(0, repeat):
                success |= await media_player_cmd_handler(self._media_player, command_or_status, None) == StatusCodes.OK

        if cmd_id == Commands.SEND_CMD_SEQUENCE:
            success = True
            for command in params.get("sequence", []):
                for _ in range(0, repeat):
                    command_or_status = self._get_command_or_status_code(cmd_id, command)
                    if isinstance(command_or_status, StatusCodes):
                        success = False
                    else:
                        res = await media_player_cmd_handler(self._media_player, command_or_status, None)
                        if res != StatusCodes.OK:
                            success = False
            if success:
                return StatusCodes.OK
            return StatusCodes.BAD_REQUEST
        return StatusCodes.BAD_REQUEST

    @staticmethod
    def state_from_media_player_state(
        media_player_state: media_player.States,
    ) -> ucapi.remote.States:
        """
        Convert UC API media player state to UC API remote state.

        :param media_player_state: UC API media player state
        :return: UC API remote state
        """
        match media_player_state:
            case media_player.States.ON:
                return ucapi.remote.States.ON
            case media_player.States.OFF:
                return ucapi.remote.States.OFF
            case media_player.States.BUFFERING:
                return ucapi.remote.States.ON
            case media_player.States.PAUSED:
                return ucapi.remote.States.ON
            case media_player.States.PLAYING:
                return ucapi.remote.States.ON
            case _:
                return ucapi.remote.States.UNKNOWN

    def filter_changed_attributes(self, update: dict[str, Any]) -> dict[str, Any]:
        """
        Filter the given attributes and return only the changed values.

        :param update: dictionary with attributes.
        :return: filtered entity attributes containing changed attributes only.
        """
        attributes = {}

        if Attributes.STATE in update:
            state = AppleTvRemote.state_from_media_player_state(update[Attributes.STATE])
            attributes = helpers.key_update_helper(Attributes.STATE, state, attributes, self.attributes)

        return attributes

    @staticmethod
    def _get_command_or_status_code(cmd_id: str, command: str) -> str | StatusCodes:
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
        return default

    @staticmethod
    def get_media_player_command(cmd_id: str) -> str:
        """Map remote command to media player command."""
        # pylint: disable=R0911
        match cmd_id:
            case Buttons.BACK:
                return media_player.Commands.BACK
            case Buttons.HOME:
                return media_player.Commands.HOME
            case Buttons.VOLUME_UP:
                return media_player.Commands.VOLUME_UP
            case Buttons.VOLUME_DOWN:
                return media_player.Commands.VOLUME_DOWN
            case Buttons.MUTE:
                return media_player.Commands.MUTE_TOGGLE
            case Buttons.DPAD_UP:
                return media_player.Commands.CURSOR_UP
            case Buttons.DPAD_DOWN:
                return media_player.Commands.CURSOR_DOWN
            case Buttons.DPAD_LEFT:
                return media_player.Commands.CURSOR_LEFT
            case Buttons.DPAD_RIGHT:
                return media_player.Commands.CURSOR_RIGHT
            case Buttons.DPAD_MIDDLE:
                return media_player.Commands.CURSOR_ENTER
            case Buttons.CHANNEL_UP:
                return media_player.Commands.CHANNEL_UP
            case Buttons.CHANNEL_DOWN:
                return media_player.Commands.CHANNEL_DOWN
            case Buttons.PREV:
                return media_player.Commands.PREVIOUS
            case Buttons.PLAY:
                return media_player.Commands.PLAY_PAUSE
            case Buttons.NEXT:
                return media_player.Commands.NEXT
            # pylint: disable=R0801
            case "MENU":
                return media_player.Commands.MENU
            # pylint: disable=R0801
            case "STOP":
                return media_player.Commands.STOP
            case _:
                return cmd_id

    @staticmethod
    def _get_main_page():
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


REMOTE_BUTTONS_MAPPING: list[dict[str, Any]] = [
    {"button": Buttons.BACK, "short_press": {"cmd_id": media_player.Commands.BACK}},
    {
        "button": Buttons.DPAD_DOWN,
        "short_press": {"cmd_id": media_player.Commands.CURSOR_DOWN},
    },
    {
        "button": Buttons.DPAD_LEFT,
        "short_press": {"cmd_id": media_player.Commands.CURSOR_LEFT},
    },
    {
        "button": Buttons.DPAD_RIGHT,
        "short_press": {"cmd_id": media_player.Commands.CURSOR_RIGHT},
    },
    {
        "button": Buttons.DPAD_MIDDLE,
        "short_press": {"cmd_id": media_player.Commands.CURSOR_ENTER},
    },
    {
        "button": Buttons.DPAD_UP,
        "short_press": {"cmd_id": media_player.Commands.CURSOR_UP},
    },
    {
        "button": Buttons.VOLUME_UP,
        "short_press": {"cmd_id": media_player.Commands.VOLUME_UP},
    },
    {
        "button": Buttons.VOLUME_DOWN,
        "short_press": {"cmd_id": media_player.Commands.VOLUME_DOWN},
    },
    {
        "button": Buttons.MUTE,
        "short_press": {"cmd_id": media_player.Commands.MUTE_TOGGLE},
    },
    {"button": Buttons.POWER, "short_press": {"cmd_id": media_player.Commands.TOGGLE}},
    {"button": Buttons.PREV, "short_press": {"cmd_id": media_player.Commands.PREVIOUS}},
    {
        "button": Buttons.PLAY,
        "short_press": {"cmd_id": media_player.Commands.PLAY_PAUSE},
    },
    {"button": Buttons.NEXT, "short_press": {"cmd_id": media_player.Commands.NEXT}},
    {"button": "STOP", "short_press": {"cmd_id": media_player.Commands.STOP}},
    {"button": "MENU", "short_press": {"cmd_id": media_player.Commands.CONTEXT_MENU}},
]
