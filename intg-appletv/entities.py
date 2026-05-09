"""
Common entity interface for Apple TV integration.

:copyright: (c) 2026 by Unfolded Circle ApS.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import json
import logging
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Type

from ucapi import IntegrationAPI, media_player
from utils import filter_attributes, truncate_dict

_LOG = logging.getLogger(__name__)


class AppleTVEntity(ABC):
    """Abstract AppleTV entity."""

    def __init__(self, api: IntegrationAPI):
        """Initialize the AppleTVEntity."""
        self._api: IntegrationAPI = api

    @property
    @abstractmethod
    def atv_id(self) -> str:
        """Return the ATV device identifier."""

    @property
    @abstractmethod
    def attribute_enum(self) -> Type[Enum]:
        """Return the attribute enum of the concrete entity."""

    @abstractmethod
    def state_from_media_player_state(self, state: media_player.States) -> Type[Enum]:
        """Map media-player state to target entity state."""

    def update_attributes(self, update: dict[str, Any], *, force: bool = False) -> None:
        """
        Update the configured entity attributes from the given ATV update.

        - If the entity is not configured, the updated attributes are ignored.
        - Only changed attributes are updated (and will trigger an ``entity_changed`` event), unless the ``force``
          parameter is True.

        **Attention:**
         - The update dictionary can be modified in place!
         - The ``force`` parameter will apply all ``update`` keys belonging to the entity's attribute
           without further filtering.

        :param update: dictionary containing the updated properties.
        :param force: if True, update attributes even if they haven't changed.
        """
        if force:
            if media_player.Attributes.STATE in update:
                state = self.state_from_media_player_state(update[media_player.Attributes.STATE])
                update[media_player.Attributes.STATE] = state
            attributes = filter_attributes(update, self.attribute_enum)

        else:
            attributes = self.filter_changed_attributes(update)

        if attributes:
            if _LOG.isEnabledFor(logging.DEBUG):
                # pylint: disable=E1101
                _LOG.debug("Updating attributes for entity %s : %s", self.id, json.dumps(truncate_dict(attributes)))
            # pylint: disable=E1101
            self._api.configured_entities.update_attributes(self.id, attributes)

    @abstractmethod
    def filter_changed_attributes(self, update: dict[str, Any]) -> dict[str, Any]:
        """
        Filter the given attributes from an ATV update and return only the changed values.

        **Attention:** the update dictionary can be modified in place!

        :param update: dictionary containing the updated properties.
        :return: dictionary containing only the changed attributes.
        """
