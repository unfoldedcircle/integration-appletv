#!/usr/bin/env python3
"""
This module implements a Remote Two integration driver for Apple TV devices.

:copyright: (c) 2023-2024 by Unfolded Circle ApS.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import asyncio
import logging
import os
import re
import sys
from typing import Any

import config
import pyatv
import selector
import sensor
import setup_flow
import tv
import ucapi
import ucapi.api as uc
from config import AppleTVEntity
from const import filter_attributes, truncate_dict
from i18n import _a
from media_player import AppleTVMediaPlayer
from ucapi import Entity, media_player

_LOG = logging.getLogger("driver")  # avoid having __main__ in log messages
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_LOOP)

# Global variables
api = uc.IntegrationAPI(_LOOP)
_configured_atvs: dict[str, tv.AppleTv] = {}


@api.listens_to(ucapi.Events.CONNECT)
async def on_r2_connect_cmd() -> None:
    """Connect all configured ATVs when the Remote Two sends the connect command."""
    _LOG.debug("Client connect command: connecting device(s)")
    await api.set_device_state(ucapi.DeviceStates.CONNECTED)  # just to make sure the device state is set
    for atv in _configured_atvs.values():
        # start background task
        await atv.connect()


@api.listens_to(ucapi.Events.DISCONNECT)
async def on_r2_disconnect_cmd():
    """Disconnect all configured ATVs when the Remote Two sends the disconnect command."""
    _LOG.debug("Client disconnect command: disconnecting device(s)")
    for atv in _configured_atvs.values():
        await atv.disconnect()


@api.listens_to(ucapi.Events.ENTER_STANDBY)
async def on_r2_enter_standby() -> None:
    """
    Enter standby notification from Remote Two.

    Disconnect every ATV instances.
    """
    _LOG.debug("Enter standby event: disconnecting device(s)")
    for device in _configured_atvs.values():
        await device.disconnect()


@api.listens_to(ucapi.Events.EXIT_STANDBY)
async def on_r2_exit_standby() -> None:
    """
    Exit standby notification from Remote Two.

    Connect all ATV instances.
    """
    _LOG.debug("Exit standby event: connecting device(s)")
    for device in _configured_atvs.values():
        await device.connect()


@api.listens_to(ucapi.Events.SUBSCRIBE_ENTITIES)
async def on_subscribe_entities(entity_ids: list[str]) -> None:
    """
    Subscribe to given entities.

    :param entity_ids: entity identifiers.
    """
    _LOG.debug("Subscribe entities event: %s", entity_ids)

    for entity_id in entity_ids:
        entity: AppleTVEntity | None = api.configured_entities.get(entity_id)
        device_id = entity.deviceid
        if device_id in _configured_atvs:
            device = _configured_atvs[device_id]
            if isinstance(entity, media_player.MediaPlayer):
                api.configured_entities.update_attributes(
                    entity_id, filter_attributes(device.attributes, ucapi.media_player.Attributes)
                )
            elif isinstance(entity, selector.AppleTVSelect):
                api.configured_entities.update_attributes(entity_id, entity.update_attributes())
            elif isinstance(entity, sensor.AppleTVSensor):
                api.configured_entities.update_attributes(entity_id, entity.update_attributes())
            continue

        device = config.devices.get(device_id)
        if device:
            _add_configured_atv(device)
        else:
            _LOG.error("Failed to subscribe entity %s: no Apple TV instance found", entity_id)


@api.listens_to(ucapi.Events.UNSUBSCRIBE_ENTITIES)
async def on_unsubscribe_entities(entity_ids: list[str]) -> None:
    """On unsubscribe, we disconnect the objects and remove listeners for events."""
    _LOG.debug("Unsubscribe entities event: %s", entity_ids)
    for entity_id in entity_ids:
        if entity_id in _configured_atvs:
            device = _configured_atvs.pop(entity_id)
            _LOG.info("Removed '%s' from configured devices and disconnect", device.name)
            await device.disconnect()
            device.events.remove_all_listeners()


async def on_atv_connected(identifier: str) -> None:
    """Handle ATV connection."""
    _LOG.debug("Apple TV connected: %s", identifier)
    state = media_player.States.UNKNOWN
    if identifier in _configured_atvs:
        await on_atv_update(identifier, None)
        await api.set_device_state(ucapi.DeviceStates.CONNECTED)  # just to make sure the device state is set
        return

    api.configured_entities.update_attributes(identifier, {media_player.Attributes.STATE: state})
    await api.set_device_state(ucapi.DeviceStates.CONNECTED)  # just to make sure the device state is set


async def on_atv_disconnected(identifier: str) -> None:
    """Handle ATV disconnection."""
    _LOG.debug("Apple TV disconnected: %s", identifier)
    for configured_entity in _get_entities(identifier):
        if configured_entity.entity_type == ucapi.EntityTypes.MEDIA_PLAYER:
            api.configured_entities.update_attributes(
                configured_entity.id, {ucapi.media_player.Attributes.STATE: ucapi.media_player.States.UNAVAILABLE}
            )
        elif configured_entity.entity_type == ucapi.EntityTypes.SENSOR:
            api.configured_entities.update_attributes(
                configured_entity.id, {ucapi.sensor.Attributes.STATE: ucapi.sensor.States.UNAVAILABLE}
            )


async def on_atv_connection_error(identifier: str, message) -> None:
    """Set entities of ATV to state UNAVAILABLE if ATV connection error occurred."""
    _LOG.error(message)
    for configured_entity in _get_entities(identifier):
        if configured_entity.entity_type == ucapi.EntityTypes.MEDIA_PLAYER:
            api.configured_entities.update_attributes(
                configured_entity.id, {ucapi.media_player.Attributes.STATE: ucapi.media_player.States.UNAVAILABLE}
            )
        elif configured_entity.entity_type == ucapi.EntityTypes.SENSOR:
            api.configured_entities.update_attributes(
                configured_entity.id, {ucapi.sensor.Attributes.STATE: ucapi.sensor.States.UNAVAILABLE}
            )
    await api.set_device_state(ucapi.DeviceStates.ERROR)


def _get_entities(device_id: str, include_all=False) -> list[Entity]:
    """
    Return all associated entities of the given device.

    :param device_id: the device  identifier
    :param include_all: include both configured and available entities
    :return: list of entities
    """
    entities = []
    for entity_entry in api.configured_entities.get_all():
        entity: AppleTVEntity | None = api.configured_entities.get(entity_entry.get("entity_id", ""))
        if entity is None or entity.deviceid != device_id:
            continue
        entities.append(entity)
    if not include_all:
        return entities
    for entity_entry in api.available_entities.get_all():
        entity: AppleTVEntity | None = api.available_entities.get(entity_entry.get("entity_id", ""))
        if entity is None or entity.deviceid != device_id:
            continue
        entities.append(entity)
    return entities


# pylint: disable=too-many-branches,too-many-statements
async def on_atv_update(device_id: str, update: dict[str, Any] | None) -> None:
    """
    Update attributes of configured media-player entity if ATV properties changed.

    :param device_id: ATV media-player entity identifier
    :param update: dictionary containing the updated properties or None
    """
    if update is None:
        if device_id not in _configured_atvs:
            return
        device = _configured_atvs[device_id]
        update = device.attributes
    else:
        _LOG.info("[%s] Device update: %s", device_id, truncate_dict(update))

    # FIXME temporary workaround until ucapi has been refactored:
    #       there's shouldn't be separate lists for available and configured entities
    for configured_entity in _get_entities(device_id):
        attributes = {}
        if isinstance(configured_entity, media_player.MediaPlayer):
            attributes = filter_attributes(update, ucapi.media_player.Attributes)
        elif isinstance(configured_entity, selector.AppleTVSelect):
            attributes = configured_entity.update_attributes(update)
        elif isinstance(configured_entity, sensor.AppleTVSensor):
            attributes = configured_entity.update_attributes(update)

        if attributes:
            _LOG.debug("Updating attributes for entity %s : %s", configured_entity.id, truncate_dict(attributes))
            api.configured_entities.update_attributes(configured_entity.id, attributes)


def _replace_bad_chars(value: str) -> str:
    if not value:
        return value
    # Replace all whitespace characters except the normal space and non-breaking space (#72).
    return re.sub(r"[\f\n\r\t\v\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]", " ", value)


def _add_configured_atv(device_config: config.AtvDevice, connect: bool = True) -> None:
    # the device should not yet be configured, but better be safe
    if device_config.identifier in _configured_atvs:
        atv = _configured_atvs[device_config.identifier]
        _LOOP.create_task(atv.disconnect())
    else:
        _LOG.debug(
            "Adding new ATV device: %s (%s) %s",
            device_config.name,
            device_config.identifier,
            device_config.address if device_config.address else "",
        )
        atv = tv.AppleTv(device_config, loop=_LOOP)
        atv.events.on(tv.EVENTS.CONNECTED, on_atv_connected)
        atv.events.on(tv.EVENTS.DISCONNECTED, on_atv_disconnected)
        atv.events.on(tv.EVENTS.ERROR, on_atv_connection_error)
        atv.events.on(tv.EVENTS.UPDATE, on_atv_update)
        _configured_atvs[device_config.identifier] = atv

    async def start_connection():
        await atv.connect()

    if connect:
        # start background task
        _LOOP.create_task(start_connection())

    _register_available_entities(device_config, atv)


def _register_available_entities(device_config: config.AtvDevice, device: tv.AppleTv) -> bool:
    """
    Add a new ATV device to the available entities.

    :param identifier: ATV identifier
    :param name: Friendly name
    :return: True if added, False if the device was already in storage.
    """
    entities: list[AppleTVEntity] = [
        AppleTVMediaPlayer(config_device=device_config, device=device),
        selector.AppleTVAppSelect(config_device=device_config, device=device),
        sensor.AppleTVAppSensor(config_device=device_config, device=device),
    ]

    result = False
    for entity in entities:
        if api.available_entities.contains(entity.id):
            api.available_entities.remove(entity.id)
        result = api.available_entities.add(entity)
    return result


def on_device_added(device: config.AtvDevice) -> None:
    """Handle a newly added device in the configuration."""
    _LOG.debug("New device added: %s", device)
    _add_configured_atv(device, connect=False)


def on_device_removed(device: config.AtvDevice | None) -> None:
    """Handle a removed device in the configuration."""
    if device is None:
        _LOG.debug("Configuration cleared, disconnecting & removing all configured ATV instances")
        for atv in _configured_atvs.values():
            _LOOP.create_task(atv.disconnect())
            atv.events.remove_all_listeners()
        _configured_atvs.clear()
        api.configured_entities.clear()
        api.available_entities.clear()
    else:
        if device.identifier in _configured_atvs:
            _LOG.debug("Disconnecting from removed ATV %s", device.identifier)
            atv = _configured_atvs.pop(device.identifier)
            _LOOP.create_task(atv.disconnect())
            atv.events.remove_all_listeners()
            for entity in _get_entities(atv.identifier):
                api.configured_entities.remove(entity.id)
                api.available_entities.remove(entity.id)


class JournaldFormatter(logging.Formatter):
    """Formatter for journald. Prefixes messages with priority level."""

    def format(self, record):
        """Format the log record with journald priority prefix."""
        # mapping of logging levels to journald priority levels
        # https://www.freedesktop.org/software/systemd/man/latest/sd-daemon.html#syslog-compatible-log-levels
        # Note: DEBUG app messages are logged with priority 6 (info) and INFO with priority 5 (notice)
        # This is a workaround until the log subsystem on the Remote is updated to support debug levels.
        priority = {
            logging.DEBUG: "<6>",  # SD_INFO
            logging.INFO: "<5>",  # SD_NOTICE
            logging.WARNING: "<4>",
            logging.ERROR: "<3>",
            logging.CRITICAL: "<2>",
        }.get(record.levelno, "<6>")
        return f"{priority}{record.name}: {record.getMessage()}"


async def patched_pyatv_companion_connect(self):
    """Patch connect method for pyatv Companion protocol."""
    # pylint: disable=W0212
    if self._protocol:
        return
    self._connection = pyatv.protocols.companion.connection.CompanionConnection(
        self.core.loop,
        str(self.core.config.address),
        self.core.service.port,
        self.core.device_listener,
    )
    self._protocol = pyatv.protocols.companion.protocol.CompanionProtocol(
        self._connection, pyatv.auth.hap_srp.SRPAuthHandler(), self.core.service
    )
    self._protocol.listener = self
    await self._protocol.start()
    await self.system_info()
    await self._touch_start()
    await self._session_start()
    await self._send_command("TVRCSessionStart", {"ProtocolVersionKey": "1.2"})
    await self._text_input_start()
    await self.subscribe_event("_iMC")


async def main():
    """Start the Remote Two/3 integration driver."""
    if os.getenv("INVOCATION_ID"):
        # when running under systemd: timestamps are added by the journal
        # and we use a custom formatter for journald priority levels
        handler = logging.StreamHandler()
        handler.setFormatter(JournaldFormatter())
        logging.basicConfig(handlers=[handler])
    else:
        logging.basicConfig(
            format="%(asctime)s.%(msecs)03d %(levelname)-5s %(name)s.%(funcName)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    level = os.getenv("UC_LOG_LEVEL", "DEBUG").upper()
    logging.getLogger("tv").setLevel(level)
    logging.getLogger("driver").setLevel(level)
    logging.getLogger("config").setLevel(level)
    logging.getLogger("discover").setLevel(level)
    logging.getLogger("setup_flow").setLevel(level)
    logging.getLogger("media_player").setLevel(level)
    logging.getLogger("selector").setLevel(level)
    logging.getLogger("sensor").setLevel(level)

    # logging.getLogger("pyatv").setLevel(logging.DEBUG)

    # TODO patch for tvOS 26.5 : to be removed when https://github.com/postlund/pyatv/issues/2845 is fixed
    pyatv.protocols.companion.api.CompanionAPI.connect = patched_pyatv_companion_connect

    # load paired devices
    config.devices = config.Devices(api.config_dir_path, on_device_added, on_device_removed)
    # best effort migration (if required): network might not be available during startup
    await config.devices.migrate()

    # Check for devices changes and update its mac address and ip address if necessary
    await asyncio.create_task(config.devices.handle_devices_change())
    # and register them as available devices.
    # Note: device will be moved to configured devices with the subscribe_events request!
    # This will also start the device connection.
    for device_config in config.devices.all():
        _add_configured_atv(device_config, connect=True)

    await api.init("driver.json", setup_flow.driver_setup_handler)
    # temporary hack to change driver.json language texts until supported by the wrapper lib
    api._driver_info["description"] = _a("Control your Apple TV with Remote Two/3.")  # pylint: disable=W0212
    api._driver_info["setup_data_schema"] = setup_flow.setup_data_schema()  # pylint: disable=W0212


if __name__ == "__main__":
    _LOOP.run_until_complete(main())
    _LOOP.run_forever()
