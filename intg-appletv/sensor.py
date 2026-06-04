"""
Sensor entity functions.

:copyright: (c) 2026 by Unfolded Circle ApS.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

from abc import abstractmethod
from typing import Any, cast

from typing_extensions import override
from ucapi import EntityTypes, IntegrationAPI
import ucapi.media_player
from ucapi.media_player import Attributes as MediaAttr, States as MediaStates
from ucapi.sensor import Attributes, DeviceClasses, Sensor, States

from config import AtvDevice, create_entity_id
from entities import AppleTVEntity
import tv

_SENSOR_STATE_MAPPING = {
    MediaStates.OFF: States.ON,
    MediaStates.ON: States.ON,
    MediaStates.STANDBY: States.ON,
    MediaStates.PLAYING: States.ON,
    MediaStates.PAUSED: States.ON,
    MediaStates.UNAVAILABLE: States.UNAVAILABLE,
    MediaStates.UNKNOWN: States.UNKNOWN,
}


class AppleTVSensor(Sensor, AppleTVEntity):
    """Representation of a AppleTV Sensor entity."""

    ENTITY_NAME = "sensor"
    _SENSOR_ATTRIBUTE: str
    """Update attribute name for sensor value."""

    def __init__(  # noqa: PLR0913
        self,
        entity_id: str,
        name: str | dict[str, str],
        config_device: AtvDevice,
        device: tv.AppleTv,
        *,
        api: IntegrationAPI,
        options: dict[str, Any] | None = None,
        device_class: DeviceClasses = DeviceClasses.CUSTOM,
    ):
        """Initialize the class."""
        self._device: tv.AppleTv = device
        features: list[Any] = []

        self._config_device = config_device
        base = cast("Any", super())
        base.__init__(entity_id, name, features, self.all_attributes, device_class=device_class, options=options)
        AppleTVEntity.__init__(self, entity_id, api)

    @property
    @override
    def atv_id(self) -> str:
        """Return device identifier."""
        return self._device.identifier

    @property
    @abstractmethod
    def sensor_value(self) -> str:
        """Return sensor value."""

    @property
    def all_attributes(self) -> dict[str, Any]:
        """Return all attributes."""
        return {
            Attributes.VALUE: self.sensor_value,
            Attributes.STATE: _SENSOR_STATE_MAPPING.get(self._device.media_state),
        }

    @override
    def state_from_media_player_state(self, state: MediaStates) -> States:
        """Map media-player state to sensor state."""
        return _SENSOR_STATE_MAPPING.get(state, States.UNKNOWN)

    @override
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
        if self._SENSOR_ATTRIBUTE in update and (
            force or update[self._SENSOR_ATTRIBUTE] != self.attributes.get(Attributes.VALUE)
        ):
            # make sure sensor-entity is available if data changes
            attributes.setdefault(Attributes.STATE, States.ON)
            attributes[Attributes.VALUE] = update[self._SENSOR_ATTRIBUTE]
        return attributes


class AppSensor(AppleTVSensor):
    """Current App sensor entity."""

    ENTITY_NAME = "app"
    _SENSOR_ATTRIBUTE = MediaAttr.SOURCE.value

    def __init__(
        self,
        config_device: AtvDevice,
        device: tv.AppleTv,
        api: IntegrationAPI,
    ):
        """Initialize the class."""
        entity_id = f"{create_entity_id(config_device.identifier, EntityTypes.SENSOR)}.{self.ENTITY_NAME}"
        super().__init__(
            entity_id,
            {
                "en": f"{config_device.name} App",
            },
            config_device,
            device,
            api=api,
        )

    @property
    @override
    def sensor_value(self) -> str:
        """Return sensor value."""
        return self._device.app_name


class AudioOutputSensor(AppleTVSensor):
    """Current audio output sensor entity."""

    ENTITY_NAME = "audio_output"
    _SENSOR_ATTRIBUTE = MediaAttr.SOUND_MODE.value

    def __init__(
        self,
        config_device: AtvDevice,
        device: tv.AppleTv,
        api: IntegrationAPI,
    ):
        """Initialize the class."""
        entity_id = f"{create_entity_id(config_device.identifier, EntityTypes.SENSOR)}.{self.ENTITY_NAME}"
        super().__init__(
            entity_id,
            {
                "en": f"{config_device.name} Audio output",
            },
            config_device,
            device,
            api=api,
        )

    @property
    @override
    def sensor_value(self) -> str:
        """Return sensor value."""
        return self._device.output_devices
