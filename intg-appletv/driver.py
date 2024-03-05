#!/usr/bin/env python3
"""
This module implements a Remote Two integration driver for Apple TV devices.

:copyright: (c) 2023-2024 by Unfolded Circle ApS.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import asyncio
import logging
import os
from enum import Enum
from typing import Any

import config
import pyatv
import pyatv.const
import setup_flow
import tv
import ucapi
import ucapi.api as uc
from ucapi import MediaPlayer, media_player

_LOG = logging.getLogger("driver")  # avoid having __main__ in log messages
_LOOP = asyncio.get_event_loop()

# Global variables
api = uc.IntegrationAPI(_LOOP)
_configured_atvs: dict[str, tv.AppleTv] = {}

# Experimental features, don't seem to work / supported (yet) with ATV4
ENABLE_REPEAT_FEAT = False
ENABLE_SHUFFLE_FEAT = False


class SimpleCommands(str, Enum):
    """Additional simple commands of the Apple TV not covered by media-player features."""

    TOP_MENU = "TOP_MENU"
    """Go to home screen."""
    APP_SWITCHER = "APP_SWITCHER"
    """Show running applications."""
    SCREENSAVER = "SCREENSAVER"
    """Run screensaver."""


@api.listens_to(ucapi.Events.CONNECT)
async def on_r2_connect_cmd() -> None:
    """Connect all configured ATVs when the Remote Two sends the connect command."""
    await api.set_device_state(ucapi.DeviceStates.CONNECTED)
    for atv in _configured_atvs.values():
        # start background task
        _LOOP.create_task(atv.connect())


@api.listens_to(ucapi.Events.DISCONNECT)
async def on_r2_disconnect_cmd():
    """Disconnect all configured ATVs when the Remote Two sends the disconnect command."""
    _LOG.debug("Client disconnected, disconnecting all Apple TVs")
    for atv in _configured_atvs.values():
        _LOOP.create_task(atv.disconnect())


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
        # TODO #11 add atv_id -> list(entities_id) mapping. Right now the atv_id == entity_id!
        atv_id = entity_id
        if atv_id in _configured_atvs:
            _LOG.debug("We have a match, start listening to events")
            atv = _configured_atvs[atv_id]
            if atv.is_on is None:
                state = media_player.States.UNAVAILABLE
            else:
                state = media_player.States.ON if atv.is_on else media_player.States.OFF
            api.configured_entities.update_attributes(entity_id, {media_player.Attributes.STATE: state})
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
    # TODO #11 add entity_id --> atv_id mapping. Right now the atv_id == entity_id!
    for entity_id in entity_ids:
        if entity_id in _configured_atvs:
            _LOG.debug("We have a match, stop listening to events")
            device = _configured_atvs[entity_id]
            await device.disconnect()
            device.events.remove_all_listeners()


# pylint: disable=too-many-statements
async def media_player_cmd_handler(
    entity: MediaPlayer, cmd_id: str, params: dict[str, Any] | None
) -> ucapi.StatusCodes:
    """
    Media-player entity command handler.

    Called by the integration-API if a command is sent to a configured media-player entity.

    :param entity: media-player entity
    :param cmd_id: command
    :param params: optional command parameters
    :return: status code of the command. StatusCodes.OK if the command succeeded.
    """
    _LOG.info("Got %s command request: %s %s", entity.id, cmd_id, params)

    # TODO #11 map from device id to entities (see Denon integration)
    # atv_id = _tv_from_entity_id(entity.id)
    # if atv_id is None:
    #     return ucapi.StatusCodes.NOT_FOUND
    atv_id = entity.id

    device = _configured_atvs[atv_id]

    # If the device is not on we send SERVICE_UNAVAILABLE
    if device.is_on is False:
        return ucapi.StatusCodes.SERVICE_UNAVAILABLE

    configured_entity = api.configured_entities.get(entity.id)

    if configured_entity is None:
        _LOG.warning("No Apple TV device found for entity: %s", entity.id)
        return ucapi.StatusCodes.SERVICE_UNAVAILABLE

    # If the entity is OFF, we send the turnOn command regardless of the actual command
    if configured_entity.attributes[media_player.Attributes.STATE] == media_player.States.OFF:
        _LOG.debug("Apple TV is off, sending turn on command")
        return await device.turn_on()

    res = ucapi.StatusCodes.BAD_REQUEST

    match cmd_id:
        case media_player.Commands.PLAY_PAUSE:
            res = await device.play_pause()
        case media_player.Commands.NEXT:
            res = await device.next()
        case media_player.Commands.PREVIOUS:
            res = await device.previous()
        case media_player.Commands.VOLUME_UP:
            res = await device.volume_up()
        case media_player.Commands.VOLUME_DOWN:
            res = await device.volume_down()
        case media_player.Commands.ON:
            res = await device.turn_on()
        case media_player.Commands.OFF:
            res = await device.turn_off()
        case media_player.Commands.CURSOR_UP:
            res = await device.cursor_up()
        case media_player.Commands.CURSOR_DOWN:
            res = await device.cursor_down()
        case media_player.Commands.CURSOR_LEFT:
            res = await device.cursor_left()
        case media_player.Commands.CURSOR_RIGHT:
            res = await device.cursor_right()
        case media_player.Commands.CURSOR_ENTER:
            res = await device.cursor_select()
        case media_player.Commands.REWIND:
            res = await device.skip_backward()
        case media_player.Commands.FAST_FORWARD:
            res = await device.skip_forward()

        case media_player.Commands.REPEAT:
            mode = params.get("repeat")
            if mode is None:
                res = ucapi.StatusCodes.BAD_REQUEST
            else:
                res = await device.set_repeat(mode)
        case media_player.Commands.SHUFFLE:
            mode = params.get("shuffle")
            if mode is None:
                res = ucapi.StatusCodes.BAD_REQUEST
            else:
                res = await device.set_shuffle(mode == "true")
        case media_player.Commands.CONTEXT_MENU:
            res = await device.context_menu()
        case media_player.Commands.SETTINGS:
            res = await device.control_center()

        case media_player.Commands.HOME:
            res = await device.home()

            # we wait a bit to get a push update, because music can play in the background
            await asyncio.sleep(1)
            if configured_entity.attributes[media_player.Attributes.STATE] != media_player.States.PLAYING:
                # if nothing is playing we clear the playing information
                attributes = {
                    media_player.Attributes.MEDIA_IMAGE_URL: "",
                    media_player.Attributes.MEDIA_ALBUM: "",
                    media_player.Attributes.MEDIA_ARTIST: "",
                    media_player.Attributes.MEDIA_TITLE: "",
                    media_player.Attributes.MEDIA_TYPE: "",
                    media_player.Attributes.SOURCE: "",
                    media_player.Attributes.MEDIA_DURATION: 0,
                }
                api.configured_entities.update_attributes(entity.id, attributes)
        case media_player.Commands.BACK:
            res = await device.menu()
        case media_player.Commands.CHANNEL_DOWN:
            res = await device.channel_down()
        case media_player.Commands.CHANNEL_UP:
            res = await device.channel_up()
        case media_player.Commands.SELECT_SOURCE:
            res = await device.launch_app(params["source"])
        # --- simple commands ---
        case SimpleCommands.TOP_MENU:
            res = await device.top_menu()
        case SimpleCommands.APP_SWITCHER:
            res = await device.app_switcher()
        case SimpleCommands.SCREENSAVER:
            res = await device.screensaver()

    return res


def _key_update_helper(key, value, attributes, configured_entity):
    if value is None:
        return attributes

    if key in configured_entity.attributes:
        if configured_entity.attributes[key] != value:
            attributes[key] = value
    else:
        attributes[key] = value

    return attributes


async def on_atv_connected(identifier: str) -> None:
    """Handle ATV connection."""
    _LOG.debug("Apple TV connected: %s", identifier)
    # TODO is this the correct state?
    api.configured_entities.update_attributes(identifier, {media_player.Attributes.STATE: media_player.States.STANDBY})
    # TODO #11 when multiple devices are supported, the device state logic isn't that simple anymore!
    await api.set_device_state(ucapi.DeviceStates.CONNECTED)


async def on_atv_disconnected(identifier: str) -> None:
    """Handle ATV disconnection."""
    _LOG.debug("Apple TV disconnected: %s", identifier)
    api.configured_entities.update_attributes(
        identifier, {media_player.Attributes.STATE: media_player.States.UNAVAILABLE}
    )
    # TODO #11 when multiple devices are supported, the device state logic isn't that simple anymore!
    await api.set_device_state(ucapi.DeviceStates.DISCONNECTED)


async def on_atv_connection_error(identifier: str, message) -> None:
    """Set entities of ATV to state UNAVAILABLE if ATV connection error occurred."""
    _LOG.error(message)
    api.configured_entities.update_attributes(
        identifier, {media_player.Attributes.STATE: media_player.States.UNAVAILABLE}
    )
    await api.set_device_state(ucapi.DeviceStates.ERROR)


# TODO refactor & simplify on_atv_update
# pylint: disable=too-many-branches,too-many-statements
async def on_atv_update(entity_id: str, update: dict[str, Any] | None) -> None:
    """
    Update attributes of configured media-player entity if ATV properties changed.

    :param entity_id: ATV media-player entity identifier
    :param update: dictionary containing the updated properties or None
    """
    attributes = {}

    configured_entity = api.configured_entities.get(entity_id)
    if configured_entity is None:
        return

    if "state" in update:
        match update["state"]:
            case pyatv.const.PowerState.On:
                state = media_player.States.ON
            case pyatv.const.DeviceState.Playing:
                state = media_player.States.PLAYING
            case pyatv.const.DeviceState.Playing:
                state = media_player.States.PLAYING
            case pyatv.const.DeviceState.Paused:
                state = media_player.States.PAUSED
            case pyatv.const.DeviceState.Idle:
                state = media_player.States.PAUSED
            case pyatv.const.PowerState.Off:
                state = media_player.States.OFF
            case _:
                state = media_player.States.UNKNOWN

        attributes = _key_update_helper(media_player.Attributes.STATE, state, attributes, configured_entity)

    if "position" in update:
        attributes = _key_update_helper(
            media_player.Attributes.MEDIA_POSITION, update["position"], attributes, configured_entity
        )
    if "artwork" in update:
        attributes[media_player.Attributes.MEDIA_IMAGE_URL] = update["artwork"]
    if "total_time" in update:
        attributes = _key_update_helper(
            media_player.Attributes.MEDIA_DURATION, update["total_time"], attributes, configured_entity
        )
    if "title" in update:
        attributes = _key_update_helper(
            media_player.Attributes.MEDIA_TITLE, update["title"], attributes, configured_entity
        )
    if "artist" in update:
        attributes = _key_update_helper(
            media_player.Attributes.MEDIA_ARTIST, update["artist"], attributes, configured_entity
        )
    if "album" in update:
        attributes = _key_update_helper(
            media_player.Attributes.MEDIA_ALBUM, update["album"], attributes, configured_entity
        )
    if "source" in update:
        attributes = _key_update_helper(media_player.Attributes.SOURCE, update["source"], attributes, configured_entity)
    if "sourceList" in update:
        if media_player.Attributes.SOURCE_LIST in configured_entity.attributes:
            if len(configured_entity.attributes[media_player.Attributes.SOURCE_LIST]) != len(update["sourceList"]):
                attributes[media_player.Attributes.SOURCE_LIST] = update["sourceList"]
        else:
            attributes[media_player.Attributes.SOURCE_LIST] = update["sourceList"]
    if "media_type" in update:
        media_type = ""

        if update["media_type"] == pyatv.const.MediaType.Music:
            media_type = media_player.MediaType.MUSIC
        elif update["media_type"] == pyatv.const.MediaType.TV:
            media_type = media_player.MediaType.TVSHOW
        elif update["media_type"] == pyatv.const.MediaType.Video:
            media_type = media_player.MediaType.VIDEO
        elif update["media_type"] == pyatv.const.MediaType.Unknown:
            media_type = ""

        attributes = _key_update_helper(media_player.Attributes.MEDIA_TYPE, media_type, attributes, configured_entity)

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
        api.configured_entities.update_attributes(entity_id, attributes)


def _add_configured_atv(device: config.AtvDevice, connect: bool = True) -> None:
    # the device should not yet be configured, but better be safe
    if device.identifier in _configured_atvs:
        atv = _configured_atvs[device.identifier]
        _LOOP.create_task(atv.disconnect())
    else:
        _LOG.debug(
            "Adding new ATV device: %s (%s) %s",
            device.identifier,
            device.name,
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
    # TODO #11 map entity IDs from device identifier
    entity_id = identifier
    # plain and simple for now: only one media_player per ATV device
    features = [
        media_player.Features.ON_OFF,
        media_player.Features.VOLUME,
        media_player.Features.VOLUME_UP_DOWN,
        # media_player.Features.MUTE_TOGGLE,
        media_player.Features.PLAY_PAUSE,
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
        media_player.Features.SETTINGS,
        media_player.Features.MENU,
        media_player.Features.REWIND,
        media_player.Features.FAST_FORWARD,
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
            ]
        },
        cmd_handler=media_player_cmd_handler,
    )

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
            # TODO #11 map entity IDs from device identifier
            entity_id = atv.identifier
            api.configured_entities.remove(entity_id)
            api.available_entities.remove(entity_id)


async def main():
    """Start the Remote Two integration driver."""
    logging.basicConfig()

    level = os.getenv("UC_LOG_LEVEL", "DEBUG").upper()
    logging.getLogger("tv").setLevel(level)
    logging.getLogger("driver").setLevel(level)
    logging.getLogger("discover").setLevel(level)
    logging.getLogger("setup_flow").setLevel(level)

    # logging.getLogger("pyatv").setLevel(logging.DEBUG)

    config.devices = config.Devices(api.config_dir_path, on_device_added, on_device_removed)
    for device in config.devices.all():
        _add_configured_atv(device, connect=False)

    await api.init("driver.json", setup_flow.driver_setup_handler)


if __name__ == "__main__":
    _LOOP.run_until_complete(main())
    _LOOP.run_forever()
