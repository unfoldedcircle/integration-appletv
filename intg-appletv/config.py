"""
Configuration handling of the integration driver.

:copyright: (c) 2023-2024 by Unfolded Circle ApS.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import asyncio
import dataclasses
import json
import logging
import os
from asyncio import Lock
from dataclasses import dataclass
from enum import Enum
from typing import Iterator

import discover
import pyatv
from ucapi import EntityTypes

_LOG = logging.getLogger(__name__)

_CFG_FILENAME = "config.json"


class AtvProtocol(str, Enum):
    """Apple TV protocols."""

    AIRPLAY = "airplay"
    COMPANION = "companion"


@dataclass
class AtvDevice:
    """Apple TV device configuration."""

    identifier: str
    """Unique identifier of the device."""
    name: str
    """Friendly name of the device."""
    credentials: list[dict[str, str]]
    """Credentials for different protocols."""
    address: str | None = None
    """Optional IP address of device. Disables IP discovery by identifier."""
    mac_address: str | None = None
    """Actual identifier of the device, which can change over time."""
    global_volume: bool | None = True
    """Change volume on all connected devices."""


class _EnhancedJSONEncoder(json.JSONEncoder):
    """Python dataclass json encoder."""

    def default(self, o):
        if dataclasses.is_dataclass(o):
            return dataclasses.asdict(o)
        return super().default(o)


def create_entity_id(entity_id: str, entity_type: EntityTypes) -> str:
    """Create a unique entity identifier for the given entity and entity type."""
    return (
        base_entity_id_from_entity_id(entity_id)
        if entity_type == EntityTypes.MEDIA_PLAYER
        else f"{entity_type.value}.{entity_id}"
    )


def base_entity_id_from_entity_id(entity_id: str) -> str | None:
    """
    Return the base entity id of an entity_id.

    :param entity_id: the entity identifier
    :return: unprefixed entity identifier
    """
    return entity_id.split(".", 1)[1]


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
        self._config_lock = Lock()

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
            if item.identifier == atv_id:
                return True
        return False

    def add_or_update(self, atv: AtvDevice) -> None:
        """
        Add a new configured Apple TV device and persist configuration.

        The device is updated if it already exists in the configuration.
        """
        # duplicate check
        if not self.update(atv):
            self._config.append(atv)
            self.store()
            if self._add_handler is not None:
                self._add_handler(atv)

    def get(self, atv_id: str) -> AtvDevice | None:
        """Get device configuration for given identifier."""
        for item in self._config:
            if item.identifier == atv_id:
                # return a copy
                return dataclasses.replace(item)
        return None

    def update(self, atv: AtvDevice) -> bool:
        """Update a configured Apple TV device and persist configuration."""
        for item in self._config:
            if item.identifier == atv.identifier:
                item.address = atv.address
                item.name = atv.name
                item.address = atv.address
                item.global_volume = atv.global_volume if atv.global_volume else True
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
        except OSError as err:
            _LOG.error("Cannot write the config file: %s", err)

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
                # not using AtvDevice(**item) to be able to migrate old configuration files with missing attributes
                atv = AtvDevice(
                    item.get("identifier"),
                    item.get("name", ""),
                    item.get("credentials"),
                    item.get("address"),
                    item.get("mac_address"),
                    item.get("global_volume", True),
                )
                self._config.append(atv)
            return True
        except OSError as err:
            _LOG.error("Cannot open the config file: %s", err)
        except (AttributeError, ValueError, TypeError) as err:
            _LOG.error("Empty or invalid config file: %s", err)

        return False

    def migration_required(self) -> bool:
        """Check if configuration migration is required."""
        for item in self._config:
            if not item.name or not item.mac_address:
                return True
        return False

    async def migrate(self) -> bool:
        """Migrate configuration if required."""
        result = True
        for item in self._config:
            if not item.mac_address:
                _LOG.info(
                    "Migrating configuration: storing device identifier %s as mac address in order to update it later",
                    item.identifier,
                )
                item.mac_address = item.identifier
                if not self.store():
                    result = False
            if not item.name:
                _LOG.info("Migrating configuration: scanning for device %s to update device name", item.identifier)
                search_hosts = [item.address] if item.address else None
                discovered_atvs = await discover.apple_tvs(
                    asyncio.get_event_loop(), identifier=item.identifier, hosts=search_hosts
                )
                if discovered_atvs:
                    item.name = discovered_atvs[0].name
                    _LOG.info("Updating device configuration %s with name: %s", item.identifier, item.name)
                    if not self.store():
                        result = False
                else:
                    result = False
                    _LOG.warning(
                        "Could not migrate device configuration %s: device not found on network", item.identifier
                    )
        return result

    def get_discovered_device(
        self, configured_device: AtvDevice, discovered_atvs: list[pyatv.interface.BaseConfig]
    ) -> pyatv.interface.BaseConfig | None:
        """Return the discovered AppleTV corresponding to the configured device."""
        found_atv: pyatv.interface.BaseConfig | None = None
        try:
            found_atv = next(atv for atv in discovered_atvs if atv.identifier == configured_device.mac_address)
        except StopIteration:
            pass
        # Fallback to device name if not found
        if found_atv is None:
            try:
                # Second check : 2 devices shouldn't have the same name otherwise skip
                found_atvs = [atv for atv in discovered_atvs if atv.name == configured_device.name]
                if len(found_atvs) > 1:
                    _LOG.debug("Multiple devices have the same name : %s", configured_device.name)
                    return None
                found_atv = found_atvs[0] if found_atvs else None
            except StopIteration:
                pass
        return found_atv

    async def handle_devices_change(self) -> bool:
        """Check after changed devices (mac and ip address)."""
        if self._config_lock.locked():
            _LOG.debug("Check device change already in progress")
            return False

        # Only one instance of devices change
        await self._config_lock.acquire()
        identifiers = set(map(lambda device: device.mac_address, self._config))
        # Scan should be quick if the devices are connected when submitting their identifiers
        discovered_atvs = await discover.apple_tvs(asyncio.get_event_loop(), identifier=identifiers)
        result = False
        find_all = False
        for item in self._config:
            found_atv = self.get_discovered_device(item, discovered_atvs)

            # If the configured device has not been found, discover all devices (longer) and check after them
            if found_atv is None and not find_all:
                discovered_atvs = await discover.apple_tvs(asyncio.get_event_loop())
                find_all = True
                found_atv = self.get_discovered_device(item, discovered_atvs)

            if found_atv is None:
                _LOG.debug(
                    "Check device change : %s (mac=%s, ip=%s) could not be found on network.",
                    item.name,
                    item.mac_address,
                    item.address,
                )
                continue
            if (
                found_atv.identifier == item.mac_address
                and found_atv.name == item.name
                and (item.address is None or item.address == found_atv.address)
            ):
                continue

            # Name, or mac address or IP address (only for manual configuration) changed
            _LOG.debug(
                "Check device change: %s (mac=%s, ip=%s) changed, now identified as %s (mac=%s, ip=%s)",
                item.name,
                item.mac_address,
                item.address,
                found_atv.name,
                found_atv.identifier,
                str(found_atv.address),
            )
            item.name = found_atv.name
            item.mac_address = found_atv.identifier
            if item.address and item.address != found_atv.address:
                item.address = str(found_atv.address)
            result = True

        if result:
            _LOG.debug("Configuration updated")
            self.store()
        self._config_lock.release()
        return result


devices: Devices | None = None
