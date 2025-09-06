#!/usr/bin/env python3
"""
This module implements a Remote Two integration driver for Apple TV devices.

:copyright: (c) 2023-2024 by Unfolded Circle ApS.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import asyncio
import logging
import os
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
from appletv_remote import AppleTvRemote
from command_handlers import media_player_cmd_handler
from globals import _LOOP, _configured_atvs, api
from i18n import _a
from simple_commands import SimpleCommands
from ucapi import MediaPlayer, media_player

_LOG = logging.getLogger("driver")  # avoid having __main__ in log messages

# Experimental features, don't seem to work / supported (yet) with ATV4
ENABLE_REPEAT_FEAT = False
ENABLE_SHUFFLE_FEAT = False


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
        atv_id = config.base_entity_id_from_entity_id(entity_id)
        if atv_id in _configured_atvs:
            atv = _configured_atvs[atv_id]
            _LOG.info("Add '%s' to configured devices and connect", atv.name)
            if atv.is_on is None:
                state = media_player.States.UNAVAILABLE
            else:
                state = media_player.States.ON if atv.is_on else media_player.States.OFF

            if isinstance(atv, AppleTvRemote):
                state = AppleTvRemote.state_from_media_player_state(state)
                api.configured_entities.update_attributes(entity_id, {ucapi.remote.Attributes.STATE: state})
            else:
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
        if target_entity.attributes.get(media_player.Attributes.STATE, None) != state:
            attributes[media_player.Attributes.STATE] = state

    # updates initiated by the poller always include the data, even if it hasn't changed
    if (
        "position" in update
        and target_entity.attributes.get(media_player.Attributes.MEDIA_POSITION, 0) != update["position"]
    ):
        attributes[media_player.Attributes.MEDIA_POSITION] = update["position"]
        attributes["media_position_updated_at"] = datetime.now(tz=UTC).isoformat()
    if (
        "total_time" in update
        and target_entity.attributes.get(media_player.Attributes.MEDIA_DURATION, 0) != update["total_time"]
    ):
        attributes[media_player.Attributes.MEDIA_DURATION] = update["total_time"]
    if "source" in update and target_entity.attributes.get(media_player.Attributes.SOURCE, "") != update["source"]:
        attributes[media_player.Attributes.SOURCE] = update["source"]
    # end poller update handling

    if "artwork" in update:
        attributes[media_player.Attributes.MEDIA_IMAGE_URL] = update["artwork"]
    if "title" in update:
        attributes[media_player.Attributes.MEDIA_TITLE] = update["title"]
    if "artist" in update:
        attributes[media_player.Attributes.MEDIA_ARTIST] = update["artist"]
    if "album" in update:
        attributes[media_player.Attributes.MEDIA_ALBUM] = update["album"]
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

    if "volume" in update:
        attributes[media_player.Attributes.VOLUME] = update["volume"]

    if ENABLE_REPEAT_FEAT and "repeat" in update:
        attributes[media_player.Attributes.REPEAT] = update["repeat"]

    if ENABLE_SHUFFLE_FEAT and "shuffle" in update:
        attributes[media_player.Attributes.SHUFFLE] = update["shuffle"]

    if media_player.Attributes.STATE in attributes:
        if attributes[media_player.Attributes.STATE] == media_player.States.OFF:
            attributes[media_player.Attributes.MEDIA_IMAGE_URL] = ""
            attributes[media_player.Attributes.MEDIA_ALBUM] = ""
            attributes[media_player.Attributes.MEDIA_ARTIST] = ""
            attributes[media_player.Attributes.MEDIA_TITLE] = ""
            attributes[media_player.Attributes.MEDIA_TYPE] = ""
            attributes[media_player.Attributes.SOURCE] = ""
            attributes[media_player.Attributes.MEDIA_DURATION] = 0

    if attributes:
        if api.configured_entities.contains(entity_id):
            api.configured_entities.update_attributes(entity_id, attributes)
        else:
            api.available_entities.update_attributes(entity_id, attributes)

    for identifier in _entities_from_apple_tv(entity_id):
        configured_entity = api.configured_entities.get(identifier)
        if configured_entity is None:
            api.available_entities.update_attributes(identifier, attributes)
            continue

        if isinstance(configured_entity, AppleTvRemote):
            attributes = configured_entity.filter_changed_attributes(update)

        if attributes:
            api.configured_entities.update_attributes(identifier, attributes)


def _entities_from_apple_tv(entity_id: str) -> list[str]:
    """
    Return all associated entity identifiers of the given Apple TV.

    :param entity_id: the Apple TV identifier
    :return: list of entity identifiers
    """
    return [config.base_entity_id_from_entity_id(entity_id), f"remote.{entity_id}"]


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
    entity_id = identifier
    # plain and simple for now: only one media_player per ATV device
    features = [
        media_player.Features.ON_OFF,
        media_player.Features.VOLUME,
        media_player.Features.VOLUME_UP_DOWN,
        media_player.Features.MUTE_TOGGLE,
        media_player.Features.PLAY_PAUSE,
        media_player.Features.STOP,
        media_player.Features.NEXT,
        media_player.Features.PREVIOUS,
        media_player.Features.MEDIA_DURATION,
        media_player.Features.MEDIA_POSITION,
        media_player.Features.MEDIA_TITLE,
        media_player.Features.MEDIA_ARTIST,
        media_player.Features.MEDIA_ALBUM,
        media_player.Features.MEDIA_IMAGE_URL,
        media_player.Features.MEDIA_TYPE,
        media_player.Features.HOME,
        media_player.Features.CHANNEL_SWITCHER,
        media_player.Features.DPAD,
        media_player.Features.SELECT_SOURCE,
        media_player.Features.CONTEXT_MENU,
        media_player.Features.MENU,
        media_player.Features.REWIND,
        media_player.Features.FAST_FORWARD,
        media_player.Features.SELECT_SOUND_MODE,
        media_player.Features.SEEK,
        media_player.Features.GUIDE,
    ]
    if ENABLE_REPEAT_FEAT:
        features.append(media_player.Features.REPEAT)
    if ENABLE_SHUFFLE_FEAT:
        features.append(media_player.Features.SHUFFLE)

    entity = MediaPlayer(
        entity_id,
        name,
        features,
        {
            media_player.Attributes.STATE: media_player.States.UNAVAILABLE,
            media_player.Attributes.VOLUME: 0,
            # TODO(#34) is there a way to find out if the device is muted?
            # media_player.Attributes.MUTED: False,
            media_player.Attributes.MEDIA_DURATION: 0,
            media_player.Attributes.MEDIA_POSITION: 0,
            media_player.Attributes.MEDIA_IMAGE_URL: "",
            media_player.Attributes.MEDIA_TITLE: "",
            media_player.Attributes.MEDIA_ARTIST: "",
            media_player.Attributes.MEDIA_ALBUM: "",
        },
        device_class=media_player.DeviceClasses.TV,
        options={
            media_player.Options.SIMPLE_COMMANDS: [
                SimpleCommands.TOP_MENU.value,
                SimpleCommands.APP_SWITCHER.value,
                SimpleCommands.SCREENSAVER.value,
                SimpleCommands.SKIP_FORWARD.value,
                SimpleCommands.SKIP_BACKWARD.value,
                SimpleCommands.FAST_FORWARD_BEGIN.value,
                SimpleCommands.REWIND_BEGIN.value,
                SimpleCommands.SWIPE_LEFT.value,
                SimpleCommands.SWIPE_RIGHT.value,
                SimpleCommands.SWIPE_UP.value,
                SimpleCommands.SWIPE_DOWN.value,
                SimpleCommands.PLAY.value,
                SimpleCommands.PAUSE.value,
                SimpleCommands.PLAY_PAUSE_KEY.value,
            ]
        },
        cmd_handler=media_player_cmd_handler,
    )

    entities = [
        entity,
        AppleTvRemote(entity_id, name, entity),
    ]

    added: bool = False
    for entity in entities:
        if api.available_entities.contains(entity.id):
            api.available_entities.remove(entity.id)
        added |= api.available_entities.add(entity)

    return added


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


# TODO be removed when https://github.com/postlund/pyatv/issues/2656 is resolved
async def pyatv_patched_system_info(self):
    """Send system information to device."""
    creds = pyatv.auth.hap_pairing.parse_credentials(self.core.service.credentials)
    info = self.core.settings.info

    # Bunch of semi-random values here...
    # pylint: disable=W0212
    await self._send_command(
        "_systemInfo",
        {
            "_bf": 0,
            "_cf": 512,
            "_clFl": 128,
            "_i": os.urandom(6).hex(),  # TODO: Figure out what to put here => "cafecafecafe" don't work anymore
            "_idsID": creds.client_id,
            # Not really device id here, but better then anything...
            "_pubID": info.device_id,
            "_sf": 256,  # Status flags?
            "_sv": "170.18",  # Software Version (I guess?)
            "model": info.model,
            "name": info.name,
        },
    )


async def main():
    """Start the Remote Two integration driver."""
    logging.basicConfig()

    level = os.getenv("UC_LOG_LEVEL", "DEBUG").upper()
    logging.getLogger("tv").setLevel(level)
    logging.getLogger("driver").setLevel(level)
    logging.getLogger("config").setLevel(level)
    logging.getLogger("discover").setLevel(level)
    logging.getLogger("setup_flow").setLevel(level)

    # TODO be removed when https://github.com/postlund/pyatv/issues/2656 is resolved
    pyatv.protocols.companion.api.CompanionAPI.system_info = pyatv_patched_system_info

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
