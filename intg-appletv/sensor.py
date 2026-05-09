"""
Sensor entity functions.

:copyright: (c) 2026 by Unfolded Circle ApS.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import logging
from abc import abstractmethod
from enum import Enum
from typing import Any, Type

import tv
import ucapi.media_player
from config import AtvDevice, create_entity_id
from entities import AppleTVEntity
from ucapi import EntityTypes, IntegrationAPI, Sensor
from ucapi.media_player import States as MediaStates
from ucapi.sensor import Attributes, DeviceClasses, Options, States
from utils import AppleTVSensors

_LOG = logging.getLogger(__name__)

# pylint: disable=R0801
SENSOR_STATE_MAPPING = {
    MediaStates.OFF: States.ON,
    MediaStates.ON: States.ON,
    MediaStates.STANDBY: States.ON,
    MediaStates.PLAYING: States.ON,
    MediaStates.PAUSED: States.ON,
    MediaStates.UNAVAILABLE: States.UNAVAILABLE,
    MediaStates.UNKNOWN: States.UNKNOWN,
}


# pylint: disable=R0917,R0801
class AppleTVSensor(Sensor, AppleTVEntity):
    """Representation of a AppleTV Sensor entity."""

    ENTITY_NAME = "sensor"
    SENSOR_NAME: AppleTVSensors

    def __init__(
        self,
        entity_id: str,
        name: str | dict[str, str],
        config_device: AtvDevice,
        device: tv.AppleTv,
        api: IntegrationAPI,
        *,
        options: dict[Options, Any] | None = None,
        device_class: DeviceClasses = DeviceClasses.CUSTOM,
    ):
        """Initialize the class."""
        self._device: tv.AppleTv = device
        features = []

        self._config_device = config_device
        self._state: States = States.UNAVAILABLE
        super().__init__(entity_id, name, features, self.all_attributes, device_class=device_class, options=options)
        AppleTVEntity.__init__(self, api)

    @property
    def atv_id(self) -> str:
        """Return device identifier."""
        return self._device.identifier

    @property
    def attribute_enum(self) -> Type[Enum]:
        """Return the sensor-entity attribute enum."""
        return Attributes

    @property
    def state(self) -> States:
        """Return sensor state."""
        return self._state

    @property
    @abstractmethod
    def sensor_value(self) -> str:
        """Return sensor value."""

    @property
    def all_attributes(self) -> dict[str, Any]:
        """Return all attributes."""
        return {
            Attributes.VALUE: self.sensor_value,
            Attributes.STATE: SENSOR_STATE_MAPPING.get(self._device.media_state),
        }

    def filter_changed_attributes(self, update: dict[str, Any]) -> dict[str, Any]:
        """Return only the changed attributes."""
        attributes: dict[str, Any] = {}
        if ucapi.media_player.Attributes.STATE in update:
            new_state = SENSOR_STATE_MAPPING.get(update[ucapi.media_player.Attributes.STATE], States.UNKNOWN)
            if new_state != self._state:
                self._state = new_state
                attributes[Attributes.STATE] = self._state
        if self.SENSOR_NAME in update:
            attributes[Attributes.VALUE] = update[self.SENSOR_NAME]
        return attributes


class AppSensor(AppleTVSensor):
    """Current App sensor entity."""

    ENTITY_NAME = "app"
    SENSOR_NAME = AppleTVSensors.SENSOR_APP

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
            api,
        )

    @property
    def sensor_value(self) -> str:
        """Return sensor value."""
        return self._device.app_name


class AudioOutputSensor(AppleTVSensor):
    """Current audio output sensor entity."""

    ENTITY_NAME = "audio_output"
    SENSOR_NAME = AppleTVSensors.SENSOR_AUDIO_OUTPUT

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
            api,
        )

    @property
    def sensor_value(self) -> str:
        """Return sensor value."""
        return self._device.output_devices
