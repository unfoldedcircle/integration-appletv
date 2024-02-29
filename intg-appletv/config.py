"""
Configuration handling of the integration driver.

:copyright: (c) 2023 by Unfolded Circle ApS.
:license: MPL-2.0, see LICENSE for more details.
"""

import dataclasses
import json
import logging
import os
from dataclasses import dataclass
from typing import Iterator

_LOG = logging.getLogger(__name__)

_CFG_FILENAME = "config.json"


@dataclass
class AtvDevice:
    """Apple TV device configuration."""

    identifier: str
    name: str
    credentials: str


class _EnhancedJSONEncoder(json.JSONEncoder):
    """Python dataclass json encoder."""

    def default(self, o):
        if dataclasses.is_dataclass(o):
            return dataclasses.asdict(o)
        return super().default(o)


class Devices:
    """Integration driver configuration class. Manages all configured Apple TV devices."""

    def __init__(self, data_path: str, add_handler, remove_handler):
        """
        Create a configuration instance for the given configuration path.

        :param data_path: configuration path for the configuration file and client device certificates.
        """
        self._data_path: str = data_path
        self._cfg_file_path: str = os.path.join(data_path, _CFG_FILENAME)
        self._config: list[AtvDevice] = []
        self._add_handler = add_handler
        self._remove_handler = remove_handler
        self.load()

    @property
    def data_path(self) -> str:
        """Return the configuration path."""
        return self._data_path

    def all(self) -> Iterator[AtvDevice]:
        """Get an iterator for all device configurations."""
        return iter(self._config)

    def contains(self, atv_id: str) -> bool:
        """Check if there's a device with the given device identifier."""
        for item in self._config:
            if item.id == atv_id:
                return True
        return False

    def add(self, atv: AtvDevice) -> None:
        """Add a new configured Apple TV device."""
        # TODO duplicate check
        self._config.append(atv)
        if self._add_handler is not None:
            self._add_handler(atv)

    def get(self, atv_id: str) -> AtvDevice | None:
        """Get device configuration for given identifier."""
        for item in self._config:
            if item.id == atv_id:
                # return a copy
                return dataclasses.replace(item)
        return None

    def update(self, atv: AtvDevice) -> bool:
        """Update a configured Apple TV device and persist configuration."""
        for item in self._config:
            if item.id == atv.id:
                item.address = atv.address
                item.name = atv.name
                return self.store()
        return False

    def remove(self, atv_id: str) -> bool:
        """Remove the given device configuration."""
        atv = self.get(atv_id)
        if atv is None:
            return False
        try:
            self._config.remove(atv)
            if self._remove_handler is not None:
                self._remove_handler(atv)
            return True
        except ValueError:
            pass
        return False

    def clear(self) -> None:
        """Remove the configuration file."""
        self._config = []

        if os.path.exists(self._cfg_file_path):
            os.remove(self._cfg_file_path)

        if self._remove_handler is not None:
            self._remove_handler(None)

    def store(self) -> bool:
        """
        Store the configuration file.

        :return: True if the configuration could be saved.
        """
        try:
            with open(self._cfg_file_path, "w+", encoding="utf-8") as f:
                json.dump(self._config, f, ensure_ascii=False, cls=_EnhancedJSONEncoder)
            return True
        except OSError:
            _LOG.error("Cannot write the config file")

        return False

    def load(self) -> bool:
        """
        Load the config into the config global variable.

        :return: True if the configuration could be loaded.
        """
        try:
            with open(self._cfg_file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for item in data:
                self._config.append(AtvDevice(**item))
            return True
        except OSError:
            _LOG.error("Cannot open the config file")
        except (ValueError, TypeError) as err:
            _LOG.error("Empty or invalid config file: %s", err)

        return False


devices: Devices | None = None
