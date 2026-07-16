#!/usr/bin/env python3
"""
This module implements a Remote Two integration driver for Apple TV devices.

:copyright: (c) 2023-2024 by Unfolded Circle ApS.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import asyncio
from collections.abc import Coroutine
import logging
import os
import sys
from typing import Any, cast

from typing_extensions import override
import ucapi
from ucapi import Entity, media_player
import ucapi.api as uc

import config
from entities import AppleTVEntity
from i18n import _a
from media_player import AppleTVMediaPlayer
from remote import AppleTVRemote
import selector
import sensor
import setup_flow
import tv

_LOG = logging.getLogger("driver")  # avoid having __main__ in log messages
if sys.platform == "win32":
    windows_policy = cast("Any", asyncio).WindowsSelectorEventLoopPolicy()
    asyncio.set_event_loop_policy(windows_policy)  # pyright: ignore[reportDeprecated]

_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_LOOP)

# Global variables
api = uc.IntegrationAPI(_LOOP)
_configured_atvs: dict[str, tv.AppleTv] = {}
_background_tasks: set[asyncio.Task[Any]] = set()


def _handle_background_task_done(task: asyncio.Task[Any]) -> None:
    """Retrieve and log background task exceptions before discarding the task."""
    try:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            _LOG.exception("Background task failed", exc_info=exc)
    finally:
        _background_tasks.discard(task)


def _spawn_task(coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
    """Schedule a fire-and-forget coroutine and keep a strong reference until done."""
    task = _LOOP.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_handle_background_task_done)
    return task


@api.listens_to(ucapi.Events.CONNECT)
async def on_r2_connect_cmd() -> None:
    """Connect all configured ATVs when the Remote Two sends the connect command."""
    _LOG.debug("Client connect command: connecting device(s)")
    await api.set_device_state(ucapi.DeviceStates.CONNECTED)  # just to make sure the device state is set
    for atv in _configured_atvs.values():
        # start background task
        await atv.connect()


@api.listens_to(ucapi.Events.DISCONNECT)
async def on_r2_disconnect_cmd() -> None:
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
    # force an entity change event with the current state for all subscribed entities
    for entity_id in entity_ids:
        configured_entity: Entity | None = api.configured_entities.get(entity_id)
        if not isinstance(configured_entity, AppleTVEntity):
            continue

        device_id = configured_entity.atv_id
        if device_id in _configured_atvs:
            device = _configured_atvs[device_id]
            # make sure the device is connected when subscribing to an entity
            await device.connect()
            # Set all current attributes in the configured entity to make sure it is up to date once the Remote sends a
            # `get_entity_states` request. Note: `update_attributes` will also trigger an entity_change event.
            # Better to have too many `entity_change` events than old data!
            configured_entity.update_attributes(device.attributes, force=True)
            continue

        device = config.devices.get(device_id) if config.devices is not None else None
        if device:
            _add_configured_atv(device, connect=True)
        else:
            _LOG.error("Failed to subscribe entity %s: no Apple TV instance found", entity_id)


@api.listens_to(ucapi.Events.UNSUBSCRIBE_ENTITIES)
async def on_unsubscribe_entities(entity_ids: list[str]) -> None:
    """On unsubscribe, we disconnect the objects and remove listeners for events."""
    _LOG.debug("Unsubscribe entities event: %s", entity_ids)
    for entity_id in entity_ids:
        entity: Entity | None = api.configured_entities.get(entity_id)
        device_id = (
            entity.atv_id
            if entity and isinstance(entity, AppleTVEntity)
            else config.base_entity_id_from_entity_id(entity_id)
        )
        # only unsubscribe the device once all its entities are gone
        if device_id in _configured_atvs and not _get_entities(device_id):
            device = _configured_atvs.pop(device_id)
            _LOG.info("Removed '%s' from configured devices and disconnect", device.name)
            await device.disconnect()
            device.events.remove_all_listeners()


async def on_atv_connected(device_id: str) -> None:
    """Handle ATV connection."""
    _LOG.debug("Apple TV connected: %s", device_id)
    await api.set_device_state(ucapi.DeviceStates.CONNECTED)  # just to make sure the device state is set

    if device_id in _configured_atvs:
        atv = _configured_atvs[device_id]
        state = atv.media_state
        # make sure to not send an outdated state, not sure if media_state is immediately available
        if state == media_player.States.UNAVAILABLE:
            state = media_player.States.UNKNOWN
        on_atv_update(device_id, {media_player.Attributes.STATE: state})


def on_atv_disconnected(device_id: str) -> None:
    """Handle ATV disconnection. Set all entities to the UNAVAILABLE state."""
    _LOG.debug("[%s] Apple TV disconnected", device_id)
    _mark_entities_unavailable(device_id, force=True)


def on_atv_connection_error(device_id: str, message: Any) -> None:
    """Set entities of ATV to state UNAVAILABLE if ATV connection error occurred."""
    _LOG.error("[%s] Apple TV connection error: %s", device_id, message)
    _mark_entities_unavailable(device_id, force=False)


def _mark_entities_unavailable(device_id: str, *, force: bool) -> None:
    """Set all entities of a device to state UNAVAILABLE."""
    for entity in _get_entities(device_id, include_all=True):
        # The STATE attribute is common for all entities, just use the media player state :-)
        entity.update_attributes(
            {ucapi.media_player.Attributes.STATE: ucapi.media_player.States.UNAVAILABLE.value}, force=force
        )


def _get_entities(device_id: str, *, include_all: bool = False) -> list[AppleTVEntity]:
    """
    Return all associated entities of the given device.

    :param device_id: the device identifier
    :param include_all: includes both configured and available entities
    :return: list of ``AppleTVEntity``
    """
    entities: list[AppleTVEntity] = []
    for entity_entry in api.configured_entities.get_all():
        entity: Entity | None = api.configured_entities.get(entity_entry.get("entity_id", ""))
        if not isinstance(entity, AppleTVEntity):
            continue
        if entity.atv_id != device_id:
            continue
        entities.append(entity)
    if not include_all:
        return entities
    for entity_entry in api.available_entities.get_all():
        entity: Entity | None = api.available_entities.get(entity_entry.get("entity_id", ""))
        if not isinstance(entity, AppleTVEntity):
            continue
        if entity.atv_id != device_id:
            continue
        entities.append(entity)
    return entities


def on_atv_update(device_id: str, update: dict[str, Any]) -> None:
    """
    Update attributes of all entities if ATV properties changed.

    :param device_id: ATV media-player entity identifier
    :param update: dictionary containing the updated properties.
    """
    for entity in _get_entities(device_id, include_all=True):
        entity.update_attributes(update)


def _add_configured_atv(device_config: config.AtvDevice, *, connect: bool = True) -> None:
    # the device should not yet be configured, but better be safe
    if device_config.identifier in _configured_atvs:
        atv = _configured_atvs[device_config.identifier]
        _LOG.debug(
            "Updating existing ATV device: %s (%s) %s",
            device_config.name,
            device_config.identifier,
            device_config.address or "",
        )

        async def reconnect_with_new_config() -> None:
            # Disconnect first so a stale connection to the old address/identifier isn't left
            # dangling, then push the new config in and reconnect (which forces re-resolution
            # of the device since the cached, resolved Apple TV config is dropped).
            await atv.disconnect()
            atv.update_config(device_config)
            if connect:
                await atv.connect()

        _spawn_task(reconnect_with_new_config())
        _register_available_entities(device_config, atv)
        return

    _LOG.debug(
        "Adding new ATV device: %s (%s) %s",
        device_config.name,
        device_config.identifier,
        device_config.address or "",
    )
    atv = tv.AppleTv(device_config, loop=_LOOP)
    atv.events.on(tv.EVENTS.CONNECTED, on_atv_connected)
    atv.events.on(tv.EVENTS.DISCONNECTED, on_atv_disconnected)
    atv.events.on(tv.EVENTS.ERROR, on_atv_connection_error)
    atv.events.on(tv.EVENTS.UPDATE, on_atv_update)
    _configured_atvs[device_config.identifier] = atv

    if connect:
        # start background task
        _spawn_task(atv.connect())

    _register_available_entities(device_config, atv)


def _register_available_entities(device_config: config.AtvDevice, device: tv.AppleTv) -> bool:
    """
    Add a new ATV device to the available entities.

    :param device_config: ATV device configuration
    :param device: ATV device instance
    :return: True if at least one device entity was added, False if all entities were already in storage.
    """
    media_player_entity = AppleTVMediaPlayer(device_config, device, api)
    entities: list[Entity] = [
        media_player_entity,
        AppleTVRemote(device_config, device, api, mp_entity=media_player_entity),
        selector.AppSelect(device_config, device, api),
        sensor.AppSensor(device_config, device, api),
        selector.AudioOutputSelect(device_config, device, api),
        sensor.AudioOutputSensor(device_config, device, api),
    ]

    added = False
    for entity in entities:
        if api.available_entities.contains(entity.id):
            api.available_entities.remove(entity.id)
        added |= api.available_entities.add(entity)
    return added


def on_device_added(device: config.AtvDevice) -> None:
    """Handle a newly added device in the configuration."""
    _LOG.debug("New device added: %s", device)
    _add_configured_atv(device, connect=True)


def on_device_updated(device: config.AtvDevice) -> None:
    """
    Handle a reconfigured device in the configuration.

    Pushes the updated address / mac_address / credentials to the running `tv.AppleTv`
    instance (if any) and reconnects it, so a driver restart is not required for a
    reconfiguration (e.g. changed IP or MAC address) to take effect.
    """
    _LOG.debug("Device configuration updated: %s", device)
    _add_configured_atv(device, connect=True)


def on_device_removed(device: config.AtvDevice | None) -> None:
    """Handle a removed device in the configuration."""
    if device is None:
        _LOG.debug("Configuration cleared, disconnecting & removing all configured ATV instances")
        for atv in _configured_atvs.values():
            _spawn_task(atv.disconnect())
            atv.events.remove_all_listeners()
        _configured_atvs.clear()
        api.configured_entities.clear()
        api.available_entities.clear()
    elif device.identifier in _configured_atvs:
        _LOG.debug("Disconnecting from removed ATV %s", device.identifier)
        atv = _configured_atvs.pop(device.identifier)
        _spawn_task(atv.disconnect())
        atv.events.remove_all_listeners()
        for entity in _get_entities(atv.identifier, include_all=True):
            api.configured_entities.remove(entity.entity_id)
            api.available_entities.remove(entity.entity_id)


class JournaldFormatter(logging.Formatter):
    """Formatter for journald. Prefixes messages with priority level."""

    @override
    def format(self, record: logging.LogRecord) -> str:
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


async def main() -> None:
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
    logging.getLogger("remote").setLevel(level)

    # logging.getLogger("pyatv").setLevel(logging.DEBUG)

    # load paired devices
    config.devices = config.Devices(api.config_dir_path, on_device_added, on_device_removed, on_device_updated)
    devices = config.get_devices()
    # best effort migration (if required): network might not be available during startup
    await devices.migrate()

    # Check for devices changes and update its mac address and ip address if necessary
    await devices.handle_devices_change()
    # and register them as available devices.
    # Note: device will be moved to configured devices with the subscribe_events request!
    # This will also start the device connection.
    for device_config in devices.all():
        _add_configured_atv(device_config, connect=True)

    await api.init("driver.json", setup_flow.driver_setup_handler)
    # temporary hack to change driver.json language texts until supported by the wrapper lib
    # Attention: keep in sync with `custom_config.py`!
    api._driver_info["description"] = _a("Control your Apple TV with Remote Two/3.")  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001
    api._driver_info["setup_data_schema"] = setup_flow.setup_data_schema()  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001


if __name__ == "__main__":
    _LOOP.run_until_complete(main())
    _LOOP.run_forever()
