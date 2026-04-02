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
from datetime import UTC, datetime
from typing import Any

import config
import pyatv

# TODO begin To be removed when https://github.com/postlund/pyatv/issues/2656 is resolved
import pyatv.auth.hap_pairing
import pyatv.const
import pyatv.protocols.companion.api

# TODO end
import setup_flow
import tv
import ucapi
import ucapi.api as uc

from i18n import _a
from ucapi import media_player

from media_player import AppleTVMediaPlayer

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
        atv_id = entity_id
        if atv_id in _configured_atvs:
            atv = _configured_atvs[atv_id]
            _LOG.info("Add '%s' to configured devices and connect", atv.name)
            if atv.is_on is None:
                state = media_player.States.UNAVAILABLE
            else:
                state = media_player.States.ON if atv.is_on else media_player.States.OFF
            api.configured_entities.update_attributes(entity_id, {media_player.Attributes.STATE: state})
            await atv.connect()
            continue

        device = config.devices.get(atv_id)
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
        atv = _configured_atvs[identifier]
        if atv_state := atv.state:
            state = _atv_state_to_media_player_state(atv_state)

    api.configured_entities.update_attributes(identifier, {media_player.Attributes.STATE: state})
    await api.set_device_state(ucapi.DeviceStates.CONNECTED)  # just to make sure the device state is set


async def on_atv_disconnected(identifier: str) -> None:
    """Handle ATV disconnection."""
    _LOG.debug("Apple TV disconnected: %s", identifier)
    api.configured_entities.update_attributes(
        identifier, {media_player.Attributes.STATE: media_player.States.UNAVAILABLE}
    )


async def on_atv_connection_error(identifier: str, message) -> None:
    """Set entities of ATV to state UNAVAILABLE if ATV connection error occurred."""
    _LOG.error(message)
    api.configured_entities.update_attributes(
        identifier, {media_player.Attributes.STATE: media_player.States.UNAVAILABLE}
    )
    await api.set_device_state(ucapi.DeviceStates.ERROR)


def _atv_state_to_media_player_state(
    device_state: pyatv.const.PowerState | pyatv.const.DeviceState,
) -> media_player.States:
    match device_state:
        case pyatv.const.PowerState.On:
            state = media_player.States.ON
        case pyatv.const.PowerState.Off:
            state = media_player.States.OFF
        case pyatv.const.DeviceState.Idle:
            state = media_player.States.ON
        case pyatv.const.DeviceState.Loading:
            state = media_player.States.BUFFERING
        case pyatv.const.DeviceState.Paused:
            state = media_player.States.PAUSED
        case pyatv.const.DeviceState.Playing:
            state = media_player.States.PLAYING
        case pyatv.const.DeviceState.Seeking:
            state = media_player.States.PLAYING
        case pyatv.const.DeviceState.Stopped:
            state = media_player.States.ON
        case _:
            state = media_player.States.UNKNOWN
    return state


# pylint: disable=too-many-branches,too-many-statements
async def on_atv_update(entity_id: str, update: dict[str, Any] | None) -> None:
    """
    Update attributes of configured media-player entity if ATV properties changed.

    :param entity_id: ATV media-player entity identifier
    :param update: dictionary containing the updated properties or None
    """
    attributes = {}

    # FIXME temporary workaround until ucapi has been refactored:
    #       there's shouldn't be separate lists for available and configured entities
    if api.configured_entities.contains(entity_id):
        target_entity = api.configured_entities.get(entity_id)
    else:
        target_entity = api.available_entities.get(entity_id)
    if target_entity is None:
        return

    if "state" in update:
        state = _atv_state_to_media_player_state(update["state"])
        attributes[media_player.Attributes.STATE] = state
    else:
        state = None

    # not playing anymore, clear the playback information
    reset_playback_info = state and state not in [
        media_player.States.PLAYING,
        media_player.States.PAUSED,
        media_player.States.BUFFERING,
    ]

    if reset_playback_info:
        attributes[media_player.Attributes.MEDIA_IMAGE_URL] = ""
        attributes[media_player.Attributes.MEDIA_ALBUM] = ""
        attributes[media_player.Attributes.MEDIA_ARTIST] = ""
        attributes[media_player.Attributes.MEDIA_TITLE] = ""
        attributes[media_player.Attributes.MEDIA_TYPE] = ""
        attributes[media_player.Attributes.SOURCE] = ""
        attributes[media_player.Attributes.MEDIA_DURATION] = None
        attributes[media_player.Attributes.MEDIA_POSITION] = None
        attributes[media_player.Attributes.REPEAT] = "OFF"
        attributes[media_player.Attributes.SHUFFLE] = False
    else:
        # updates initiated by the poller always include the data, even if it hasn't changed
        if (
            "position" in update
            and target_entity.attributes.get(media_player.Attributes.MEDIA_POSITION, 0) != update["position"]
        ):
            attributes[media_player.Attributes.MEDIA_POSITION] = update["position"]
            attributes[media_player.Attributes.MEDIA_POSITION_UPDATED_AT] = datetime.now(tz=UTC).isoformat()
        if (
            media_player.Attributes.MEDIA_DURATION in update
            and target_entity.attributes.get(media_player.Attributes.MEDIA_DURATION, 0)
            != update[media_player.Attributes.MEDIA_DURATION]
        ):
            attributes[media_player.Attributes.MEDIA_DURATION] = update["total_time"]
        if "source" in update:
            source = _replace_bad_chars(update["source"])
            if target_entity.attributes.get(media_player.Attributes.SOURCE, "") != source:
                attributes[media_player.Attributes.SOURCE] = source
        # end poller update handling

        if "artwork" in update:
            attributes[media_player.Attributes.MEDIA_IMAGE_URL] = update["artwork"]
        if "title" in update:
            attributes[media_player.Attributes.MEDIA_TITLE] = _replace_bad_chars(update["title"])
        if "artist" in update:
            attributes[media_player.Attributes.MEDIA_ARTIST] = _replace_bad_chars(update["artist"])
        if "album" in update:
            attributes[media_player.Attributes.MEDIA_ALBUM] = _replace_bad_chars(update["album"])
        if "media_type" in update:
            if update["media_type"] == pyatv.const.MediaType.Music:
                media_type = media_player.MediaType.MUSIC
            elif update["media_type"] == pyatv.const.MediaType.TV:
                media_type = media_player.MediaType.TVSHOW
            elif update["media_type"] == pyatv.const.MediaType.Video:
                media_type = media_player.MediaType.VIDEO
            else:
                media_type = ""

            attributes[media_player.Attributes.MEDIA_TYPE] = media_type
        if ENABLE_REPEAT_FEAT and "repeat" in update:
            attributes[media_player.Attributes.REPEAT] = update["repeat"]
        if ENABLE_SHUFFLE_FEAT and "shuffle" in update:
            attributes[media_player.Attributes.SHUFFLE] = update["shuffle"]

    # always update if available
    if "sourceList" in update:
        if media_player.Attributes.SOURCE_LIST in target_entity.attributes:
            if len(target_entity.attributes[media_player.Attributes.SOURCE_LIST]) != len(update["sourceList"]):
                attributes[media_player.Attributes.SOURCE_LIST] = update["sourceList"]
        else:
            attributes[media_player.Attributes.SOURCE_LIST] = update["sourceList"]
    if (
        "sound_mode" in update
        and target_entity.attributes.get(media_player.Attributes.SOUND_MODE, "") != update["sound_mode"]
    ):
        attributes[media_player.Attributes.SOUND_MODE] = update["sound_mode"]
    if "sound_mode_list" in update:
        if media_player.Attributes.SOUND_MODE_LIST in target_entity.attributes:
            if len(target_entity.attributes[media_player.Attributes.SOUND_MODE_LIST]) != len(update["sound_mode_list"]):
                attributes[media_player.Attributes.SOUND_MODE_LIST] = update["sound_mode_list"]
        else:
            attributes[media_player.Attributes.SOUND_MODE_LIST] = update["sound_mode_list"]
    if "volume" in update:
        attributes[media_player.Attributes.VOLUME] = update["volume"]

    if attributes:
        if api.configured_entities.contains(entity_id):
            api.configured_entities.update_attributes(entity_id, attributes)
        else:
            api.available_entities.update_attributes(entity_id, attributes)


def _replace_bad_chars(value: str) -> str:
    if not value:
        return value
    # Replace all whitespace characters except the normal space and non-breaking space (#72).
    return re.sub(r"[\f\n\r\t\v\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]", " ", value)


def _add_configured_atv(device: config.AtvDevice, connect: bool = True) -> None:
    # the device should not yet be configured, but better be safe
    if device.identifier in _configured_atvs:
        atv = _configured_atvs[device.identifier]
        _LOOP.create_task(atv.disconnect())
    else:
        _LOG.debug(
            "Adding new ATV device: %s (%s) %s",
            device.name,
            device.identifier,
            device.address if device.address else "",
        )
        atv = tv.AppleTv(device, loop=_LOOP)
        atv.events.on(tv.EVENTS.CONNECTED, on_atv_connected)
        atv.events.on(tv.EVENTS.DISCONNECTED, on_atv_disconnected)
        atv.events.on(tv.EVENTS.ERROR, on_atv_connection_error)
        atv.events.on(tv.EVENTS.UPDATE, on_atv_update)
        _configured_atvs[device.identifier] = atv

    async def start_connection():
        await atv.connect()

    if connect:
        # start background task
        _LOOP.create_task(start_connection())

    _register_available_entities(device.identifier, device.name)


def _register_available_entities(identifier: str, name: str) -> bool:
    """
    Add a new ATV device to the available entities.

    :param identifier: ATV identifier
    :param name: Friendly name
    :return: True if added, False if the device was already in storage.
    """
    device = _configured_atvs.get(identifier)
    if device is None:
        _LOG.error("Unknown device to register entities %s", identifier)
    entity = AppleTVMediaPlayer(config_device=device.device_config, device=device)

    if api.available_entities.contains(entity.id):
        api.available_entities.remove(entity.id)
    return api.available_entities.add(entity)


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
            entity_id = atv.identifier
            api.configured_entities.remove(entity_id)
            api.available_entities.remove(entity_id)


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

    # logging.getLogger("pyatv").setLevel(logging.DEBUG)

    # load paired devices
    config.devices = config.Devices(api.config_dir_path, on_device_added, on_device_removed)
    # best effort migration (if required): network might not be available during startup
    await config.devices.migrate()

    # Check for devices changes and update its mac address and ip address if necessary
    await asyncio.create_task(config.devices.handle_devices_change())
    # and register them as available devices.
    # Note: device will be moved to configured devices with the subscribe_events request!
    # This will also start the device connection.
    for device in config.devices.all():
        _register_available_entities(device.identifier, device.name)

    await api.init("driver.json", setup_flow.driver_setup_handler)
    # temporary hack to change driver.json language texts until supported by the wrapper lib
    api._driver_info["description"] = _a("Control your Apple TV with Remote Two/3.")  # pylint: disable=W0212
    api._driver_info["setup_data_schema"] = setup_flow.setup_data_schema()  # pylint: disable=W0212


if __name__ == "__main__":
    _LOOP.run_until_complete(main())
    _LOOP.run_forever()
