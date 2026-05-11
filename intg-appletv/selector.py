"""
Select entity functions.

:copyright: (c) 2026 by Unfolded Circle ApS.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import logging
from abc import abstractmethod
from typing import Any

import tv
import ucapi
from config import AtvDevice, create_entity_id
from entities import AppleTVEntity, AppleTVSelects
from ucapi import EntityTypes, IntegrationAPI, Select, StatusCodes
from ucapi.api_definitions import CommandHandler
from ucapi.media_player import States as MediaStates
from ucapi.select import Attributes, Commands, States

_LOG = logging.getLogger(__name__)

# pylint should focus on the real Python issues! pylint: disable=R0801
_SELECTOR_STATE_MAPPING = {
    MediaStates.OFF: States.ON,
    MediaStates.ON: States.ON,
    MediaStates.STANDBY: States.ON,
    MediaStates.PLAYING: States.ON,
    MediaStates.PAUSED: States.ON,
    MediaStates.UNAVAILABLE: States.UNAVAILABLE,
    MediaStates.UNKNOWN: States.UNKNOWN,
}


class AppleTVSelect(Select, AppleTVEntity):
    """Representation of a Apple TV select entity."""

    ENTITY_NAME = "select"
    SELECT_NAME: AppleTVSelects

    def __init__(
        self,
        entity_id: str,
        name: str | dict[str, str],
        config_device: AtvDevice,
        device: tv.AppleTv,
        *,
        api: IntegrationAPI,
        select_handler: CommandHandler,
    ):
        """Initialize the class."""
        self._config_device = config_device
        self._device: tv.AppleTv = device
        self._select_handler: CommandHandler = select_handler
        super().__init__(identifier=entity_id, name=name, attributes=self.all_attributes)
        AppleTVEntity.__init__(self, entity_id, api)

    @property
    def atv_id(self) -> str:
        """Return device identifier."""
        return self._device.identifier

    @property
    @abstractmethod
    def current_option(self) -> str:
        """Return select value."""

    @property
    @abstractmethod
    def select_options(self) -> list[str]:
        """Return selection list."""

    @property
    def all_attributes(self) -> dict[str, Any]:
        """Return all attributes."""
        return {
            Attributes.CURRENT_OPTION: self.current_option,
            Attributes.OPTIONS: self.select_options,
            Attributes.STATE: States.ON,
        }

    def state_from_media_player_state(self, state: States) -> States:
        """Map media-player state to select state."""
        return _SELECTOR_STATE_MAPPING.get(state, States.UNKNOWN)

    def filter_attributes(self, update: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
        """
        Filter the given attributes from an ATV update and return only the related select-entity values.

        :param update: Dictionary containing the updated properties.
        :param force: If True, update attributes even if they haven't changed since the last update.
        :return: Dictionary containing only the changed attributes.
        """
        attributes: dict[str, Any] = {}
        if ucapi.media_player.Attributes.STATE in update:
            new_state = self.state_from_media_player_state(update[ucapi.media_player.Attributes.STATE])
            if force or new_state != self.attributes.get(Attributes.STATE):
                attributes[Attributes.STATE] = new_state
        # TODO untangle select-entity attribute updates.
        if self.SELECT_NAME in update:
            if Attributes.CURRENT_OPTION in update[self.SELECT_NAME]:
                if force or update[self.SELECT_NAME][Attributes.CURRENT_OPTION] != self.attributes.get(
                    Attributes.CURRENT_OPTION
                ):
                    attributes[Attributes.CURRENT_OPTION] = update[self.SELECT_NAME][Attributes.CURRENT_OPTION]
            if Attributes.OPTIONS in update[self.SELECT_NAME]:
                if force or update[self.SELECT_NAME][Attributes.OPTIONS] != self.attributes.get(Attributes.OPTIONS):
                    attributes[Attributes.OPTIONS] = update[self.SELECT_NAME][Attributes.OPTIONS]
        # make sure select-entity is available if data changes
        if attributes and Attributes.STATE not in update:
            attributes[Attributes.STATE] = States.ON
        return attributes

    async def command(self, cmd_id: str, params: dict[str, Any] | None = None, *, websocket: Any) -> StatusCodes:
        """Process selector command."""
        # pylint: disable=R0911
        if cmd_id == Commands.SELECT_OPTION and params:
            option = params.get("option", None)
            return await self._select_handler(option)
        options = self.select_options
        if cmd_id == Commands.SELECT_FIRST and len(options) > 0:
            return await self._select_handler(options[0])
        if cmd_id == Commands.SELECT_LAST and len(options) > 0:
            return await self._select_handler(options[len(options) - 1])
        if cmd_id == Commands.SELECT_NEXT and len(options) > 0:
            cycle = params.get("cycle", True) if params else True
            try:
                index = options.index(self.current_option) + 1
                if not cycle and index >= len(options):
                    return StatusCodes.OK
                if index >= len(options):
                    index = 0
                return await self._select_handler(options[index])
            except ValueError as ex:
                _LOG.warning(
                    "[%s] Invalid option %s in list %s %s",
                    self._config_device.address,
                    self.current_option,
                    options,
                    ex,
                )
                return StatusCodes.BAD_REQUEST
        if cmd_id == Commands.SELECT_PREVIOUS and len(options) > 0:
            cycle = params.get("cycle", True) if params else True
            try:
                index = options.index(self.current_option) - 1
                if not cycle and index < 0:
                    return StatusCodes.OK
                if index < 0:
                    index = len(options) - 1
                return await self._select_handler(options[index])
            except ValueError as ex:
                _LOG.warning(
                    "[%s] Invalid option %s in list %s %s",
                    self._config_device.address,
                    self.current_option,
                    options,
                    ex,
                )
                return StatusCodes.BAD_REQUEST
        return StatusCodes.BAD_REQUEST


class AppSelect(AppleTVSelect):
    """Representation of a AppleTV selector entity."""

    ENTITY_NAME = "app"
    SELECT_NAME = AppleTVSelects.SELECT_APP

    def __init__(
        self,
        config_device: AtvDevice,
        device: tv.AppleTv,
        api: IntegrationAPI,
    ):
        """Initialize the class."""
        entity_id = f"{create_entity_id(config_device.identifier, EntityTypes.SELECT)}.{self.ENTITY_NAME}"
        super().__init__(
            entity_id,
            {
                "en": f"{config_device.name} App",
            },
            config_device,
            device,
            api=api,
            select_handler=device.launch_app,
        )

    @property
    def current_option(self) -> str:
        """Return selector value."""
        return self._device.app_name

    @property
    def select_options(self) -> list[str]:
        """Return selection list."""
        return self._device.app_names


class AudioOutputSelect(AppleTVSelect):
    """Audio output selector entity."""

    ENTITY_NAME = "audio_output"
    SELECT_NAME = AppleTVSelects.SELECT_AUDIO_OUTPUT

    def __init__(self, config_device: AtvDevice, device: tv.AppleTv, api: IntegrationAPI):
        """Initialize the class."""
        entity_id = f"{create_entity_id(config_device.identifier, EntityTypes.SELECT)}.{self.ENTITY_NAME}"
        super().__init__(
            entity_id,
            {
                "en": f"{config_device.name} Audio output",
            },
            config_device,
            device,
            api=api,
            select_handler=device.set_output_device,
        )

    @property
    def current_option(self) -> str:
        """Return selector value."""
        return self._device.output_devices

    @property
    def select_options(self) -> list[str]:
        """Return selection list."""
        return self._device.output_devices_combinations
