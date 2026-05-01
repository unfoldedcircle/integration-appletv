"""
Select entity functions.

:copyright: (c) 2026 by Unfolded Circle ApS.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import logging
from typing import Any

import tv
from config import AppleTVEntity, AtvDevice, create_entity_id
from const import AppleTVSelects
from ucapi import EntityTypes, Select, StatusCodes
from ucapi.api_definitions import CommandHandler
from ucapi.select import Attributes, Commands, States

_LOG = logging.getLogger(__name__)


# pylint: disable=W1405,R0801
class AppleTVSelect(AppleTVEntity, Select):
    """Representation of a Apple TV select entity."""

    ENTITY_NAME = "select"
    SELECT_NAME: AppleTVSelects

    # pylint: disable=R0917
    def __init__(
            self,
            entity_id: str,
            name: str | dict[str, str],
            config_device: AtvDevice,
            device: tv.AppleTv,
            select_handler: CommandHandler,
    ):
        """Initialize the class."""
        # pylint: disable = R0801
        self._config_device = config_device
        self._device: tv.AppleTv = device
        self._state: States = States.ON
        self._select_handler: CommandHandler = select_handler
        super().__init__(identifier=entity_id, name=name, attributes=self.all_attributes)

    @property
    def deviceid(self) -> str:
        """Return device identifier."""
        return self._device.identifier

    @property
    def current_option(self) -> str:
        """Return select value."""
        raise NotImplementedError()

    @property
    def select_options(self) -> list[str]:
        """Return selection list."""
        raise NotImplementedError()

    @property
    def all_attributes(self) -> dict[str, Any]:
        """Return all attributes."""
        return {
            Attributes.CURRENT_OPTION: self.current_option,
            Attributes.OPTIONS: self.select_options,
            Attributes.STATE: States.ON,
        }

    def update_attributes(self, update: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Return updated selector value from full update if provided or sensor value if no udpate is provided."""
        if update:
            if self.SELECT_NAME in update:
                return update[self.SELECT_NAME]
            return None
        return self.all_attributes

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
            cycle = params.get("cycle", False)
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
            cycle = params.get("cycle", False)
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
    """Current audio stream selector entity."""

    ENTITY_NAME = "app"
    SELECT_NAME = AppleTVSelects.SELECT_APP

    def __init__(self, config_device: AtvDevice, device: tv.AppleTv):
        """Initialize the class."""
        # pylint: disable=W1405,R0801
        entity_id = f"{create_entity_id(config_device.identifier, EntityTypes.SELECT)}.{self.ENTITY_NAME}"
        super().__init__(
            entity_id,
            {
                "en": f"{config_device.name} App",
            },
            config_device,
            device,
            device.launch_app,
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

    def __init__(self, config_device: AtvDevice, device: tv.AppleTv):
        """Initialize the class."""
        # pylint: disable=W1405,R0801
        entity_id = f"{create_entity_id(config_device.identifier, EntityTypes.SELECT)}.{self.ENTITY_NAME}"
        super().__init__(
            entity_id,
            {
                "en": f"{config_device.name} Audio output",
            },
            config_device,
            device,
            device.set_output_device,
        )

    @property
    def current_option(self) -> str:
        """Return selector value."""
        return self._device.output_devices

    @property
    def select_options(self) -> list[str]:
        """Return selection list."""
        return self._device.output_devices_combinations
