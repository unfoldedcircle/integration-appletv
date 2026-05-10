"""
Common entity interface for Apple TV integration.

:copyright: (c) 2026 by Unfolded Circle ApS.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import json
import logging
from abc import ABC, abstractmethod
from enum import Enum, StrEnum
from typing import Any, Type

from ucapi import IntegrationAPI, media_player
from utils import truncate_dict

_LOG = logging.getLogger(__name__)


class AppleTVSelects(StrEnum):
    """Apple TV select values."""

    SELECT_APP = "select_app"
    SELECT_AUDIO_OUTPUT = "select_audio_output"


class AppleTVSensors(StrEnum):
    """Apple TV sensor values."""

    SENSOR_APP = "sensor_app"
    SENSOR_AUDIO_OUTPUT = "sensor_audio_output"


class AppleTVEntity(ABC):
    """Abstract AppleTV entity."""

    def __init__(self, api: IntegrationAPI):
        """Initialize the AppleTVEntity."""
        self._api: IntegrationAPI = api

    @property
    @abstractmethod
    def atv_id(self) -> str:
        """Return the ATV device identifier."""

    @abstractmethod
    def state_from_media_player_state(self, state: media_player.States) -> Type[Enum]:
        """Map media-player state to target entity state."""

    def update_attributes(self, update: dict[str, Any], *, force: bool = False) -> None:
        """
        Update the entity attributes from the given ATV update.

        - Updates configured and available entities.
        - Only changed attributes are updated (and will trigger an ``entity_changed`` event), unless the ``force``
          parameter is True.

        :param update: Dictionary containing the updated properties.
        :param force: If True, update attributes even if they haven't changed.
        """
        attributes = self.filter_attributes(update, force=force)

        if attributes:
            # pylint: disable=E1101
            entity_id = self.id
            if _LOG.isEnabledFor(logging.DEBUG):
                _LOG.debug("Updating attributes for entity %s : %s", entity_id, json.dumps(truncate_dict(attributes)))
            if self._api.configured_entities.contains(entity_id):
                self._api.configured_entities.update_attributes(entity_id, attributes)
            else:
                self._api.available_entities.update_attributes(entity_id, attributes)

    @abstractmethod
    def filter_attributes(self, update: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
        """
        Filter the given attributes from an ATV update and return only the related entity values.

        :param update: dictionary containing the updated properties.
        :param force: If True, update attributes even if they haven't changed since the last update.
        :return: dictionary containing only the changed attributes.
        """
