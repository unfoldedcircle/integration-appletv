"""
Sensor entity functions.

:copyright: (c) 2026 by Unfolded Circle ApS.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import logging
from typing import Any

import tv
import ucapi.media_player
from config import AppleTVEntity, AtvDevice, create_entity_id
from const import AppleTVSensors
from ucapi import EntityTypes, Sensor
from ucapi.media_player import States as MediaStates
from ucapi.sensor import Attributes, DeviceClasses, Options, States

_LOG = logging.getLogger(__name__)

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
class AppleTVSensor(AppleTVEntity, Sensor):
    """Representation of a Kodi Sensor entity."""

    ENTITY_NAME = "sensor"
    SENSOR_NAME: AppleTVSensors

    def __init__(
            self,
            entity_id: str,
            name: str | dict[str, str],
            config_device: AtvDevice,
            device: tv.AppleTv,
            options: dict[Options, Any] | None = None,
            device_class: DeviceClasses = DeviceClasses.CUSTOM,
    ):
        """Initialize the class."""
        self._device: tv.AppleTv = device
        features = []

        self._config_device = config_device
        self._state: States = States.UNAVAILABLE
        super().__init__(entity_id, name, features, self.all_attributes, device_class=device_class, options=options)

    @property
    def deviceid(self) -> str:
        """Return device identifier."""
        return self._device.identifier

    @property
    def state(self) -> States:
        """Return sensor state."""
        raise self._state

    @property
    def sensor_value(self) -> str:
        """Return sensor value."""
        raise NotImplementedError()

    @property
    def all_attributes(self) -> dict[str, Any]:
        """Return all attributes."""
        return {
            Attributes.VALUE: self.sensor_value,
            Attributes.STATE: SENSOR_STATE_MAPPING.get(self._device.media_state),
        }

    def update_attributes(self, update: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Return updated sensor value from full update if provided or sensor value if no udpate is provided."""
        attributes: dict[str, Any] = {}
        if update:
            if ucapi.media_player.Attributes.STATE in update:
                new_state = SENSOR_STATE_MAPPING.get(update[ucapi.media_player.Attributes.STATE])
                if new_state != self._state:
                    self._state = new_state
                    attributes[Attributes.STATE] = self._state
            if self.SENSOR_NAME in update:
                attributes[Attributes.VALUE] = update[self.SENSOR_NAME]
            return attributes
        return self.all_attributes


class AppSensor(AppleTVSensor):
    """Current audio stream sensor entity."""

    ENTITY_NAME = "app"
    SENSOR_NAME = AppleTVSensors.SENSOR_APP

    def __init__(self, config_device: AtvDevice, device: tv.AppleTv):
        """Initialize the class."""
        entity_id = f"{create_entity_id(config_device.identifier, EntityTypes.SENSOR)}.{self.ENTITY_NAME}"
        super().__init__(
            entity_id,
            {
                "en": f"{config_device.name} App",
            },
            config_device,
            device,
        )

    @property
    def sensor_value(self) -> str:
        """Return sensor value."""
        return self._device.app_name


class AudioOutputSensor(AppleTVSensor):
    """Current audio output sensor entity."""

    ENTITY_NAME = "audio_output"
    SENSOR_NAME = AppleTVSensors.SENSOR_AUDIO_OUTPUT

    def __init__(self, config_device: AtvDevice, device: tv.AppleTv):
        """Initialize the class."""
        entity_id = f"{create_entity_id(config_device.identifier, EntityTypes.SENSOR)}.{self.ENTITY_NAME}"
        super().__init__(
            entity_id,
            {
                "en": f"{config_device.name} Audio output",
            },
            config_device,
            device,
        )

    @property
    def sensor_value(self) -> str:
        """Return sensor value."""
        return self._device.output_devices
