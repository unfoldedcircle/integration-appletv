#!/usr/bin/env python3
"""
This module implements a Remote Two integration driver for Apple TV devices.

:copyright: (c) 2023-2024 by Unfolded Circle ApS.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import asyncio
import logging
import os
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


# DRIVER SETUP
# @api.events.on(uc.uc.EVENTS.SETUP_DRIVER)
# async def on_setup_driver(websocket, req_id, _data):
#     _LOG.debug("Starting driver setup")
#     config.devices.clear()
#     await api.acknowledge_command(websocket, req_id)
#     await api.driver_setup_progress(websocket)
#
#     _LOG.debug("Starting Apple TV discovery")
#     tvs = await discover.apple_tvs(_LOOP)
#     dropdown_items = []
#
#     for device in tvs:
#         tv_data = {"id": device.identifier, "label": {"en": device.name + " TvOS " + str(device.device_info.version)}}
#
#         dropdown_items.append(tv_data)
#
#     if not dropdown_items:
#         _LOG.warning("No Apple TVs found")
#         await api.driver_setup_error(websocket)
#         return
#
#     await api.request_driver_setup_user_input(
#         websocket,
#         "Please choose your Apple TV",
#         [
#             {
#                 "field": {"dropdown": {"value": dropdown_items[0]["id"], "items": dropdown_items}},
#                 "id": "choice",
#                 "label": {"en": "Choose your Apple TV"},
#             }
#         ],
#     )


# @api.events.on(uc.uc.EVENTS.SETUP_DRIVER_USER_DATA)
# async def on_setup_driver_user_data(websocket, req_id, data):
#     global pairing_apple_tv
#
#     await api.acknowledge_command(websocket, req_id)
#     await api.driver_setup_progress(websocket)
#
#     # We pair with companion second
#     if "pin_companion" in data:
#         _LOG.debug("User has entered the Companion PIN")
#         await pairing_apple_tv.enter_pin(data["pin_companion"])
#
#         res = await pairing_apple_tv.finish_pairing()
#         if res is None:
#             await api.driver_setup_error(websocket)
#         else:
#             c = {"protocol": res.protocol.name.lower(), "credentials": res.credentials}
#             pairing_apple_tv.add_credentials(c)
#
#             configuredAppleTvs[pairing_apple_tv.identifier] = pairing_apple_tv
#
#             config.devices.add(
#                 config.AtvDevice(
#                     identifier=pairing_apple_tv.identifier,
#                     name=pairing_apple_tv.name,
#                     credentials=pairing_apple_tv.get_credentials(),
#                 )
#             )
#             config.devices.store()
#
#             add_available_apple_tv(pairing_apple_tv.identifier, pairing_apple_tv.name)
#
#             await api.driver_setup_complete(websocket)
#
#     # We pair with airplay first
#     elif "pin_airplay" in data:
#         _LOG.debug("User has entered the Airplay PIN")
#         await pairing_apple_tv.enter_pin(data["pin_airplay"])
#
#         res = await pairing_apple_tv.finish_pairing()
#         if res is None:
#             await api.driver_setup_error(websocket)
#         else:
#             # Store credentials
#             c = {"protocol": res.protocol.name.lower(), "credentials": res.credentials}
#             pairing_apple_tv.add_credentials(c)
#
#             # Start new pairing process
#             res = await pairing_apple_tv.start_pairing(pyatv.const.Protocol.Companion, "Remote Two Companion")
#
#             if res == 0:
#                 _LOG.debug("Device provides PIN")
#                 await api.request_driver_setup_user_input(
#                     websocket,
#                     "Please enter the PIN from your Apple TV",
#                     [
#                         {
#                             "field": {"number": {"max": 9999, "min": 0, "value": 0000}},
#                             "id": "pin_companion",
#                             "label": {"en": "Apple TV PIN"},
#                         }
#                     ],
#                 )
#
#             else:
#                 _LOG.debug("We provide PIN")
#                 await api.request_driver_setup_user_confirmation(
#                     websocket, "Please enter the following PIN on your Apple TV:" + res
#                 )
#                 await pairing_apple_tv.finish_pairing()
#
#     elif "choice" in data:
#         choice = data["choice"]
#         _LOG.debug("Chosen Apple TV: %s", choice)
#
#         # Create a new AppleTv object
#         pairing_apple_tv = tv.AppleTv(_LOOP)
#         pairing_apple_tv.pairing_atv = await pairing_apple_tv.find_atv(choice)
#
#         if pairing_apple_tv.pairing_atv is None:
#             _LOG.error("Cannot find the chosen AppleTV")
#             await api.driver_setup_error(websocket)
#             return
#
#         await pairing_apple_tv.init(choice, name=pairing_apple_tv.pairing_atv.name)
#
#         _LOG.debug("Pairing process begin")
#         # Hook up to signals
#         res = await pairing_apple_tv.start_pairing(pyatv.const.Protocol.AirPlay, "Remote Two Airplay")
#
#         if res == 0:
#             _LOG.debug("Device provides PIN")
#             await api.request_driver_setup_user_input(
#                 websocket,
#                 "Please enter the PIN from your Apple TV",
#                 [
#                     {
#                         "field": {"number": {"max": 9999, "min": 0, "value": 0000}},
#                         "id": "pin_airplay",
#                         "label": {"en": "Apple TV PIN"},
#                     }
#                 ],
#             )
#
#         else:
#             _LOG.debug("We provide PIN")
#             await api.request_driver_setup_user_confirmation(
#                 websocket, "Please enter the following PIN on your Apple TV:" + res
#             )
#             await pairing_apple_tv.finish_pairing()
#
#     else:
#         _LOG.error("No choice was received")
#         await api.driver_setup_error(websocket)


@api.listens_to(ucapi.Events.CONNECT)
async def on_r2_connect_cmd() -> None:
    """Connect all configured ATVs when the Remote Two sends the connect command."""
    # FIXME connect all configured ATVs !!!
    await api.set_device_state(ucapi.DeviceStates.CONNECTED)


@api.listens_to(ucapi.Events.DISCONNECT)
async def on_r2_disconnect_cmd():
    """Disconnect all configured ATVs when the Remote Two sends the disconnect command."""
    _LOG.debug("Client disconnected, disconnecting all Apple TVs")
    for device in _configured_atvs.values():
        await device.disconnect()
        # TODO still required?
        device.events.remove_all_listeners()

    await api.set_device_state(ucapi.DeviceStates.DISCONNECTED)


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
        # TODO add atv_id -> list(entities_id) mapping. Right now the atv_id == entity_id!
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
    # TODO add entity_id --> atv_id mapping. Right now the atv_id == entity_id!
    for entity_id in entity_ids:
        if entity_id in _configured_atvs:
            _LOG.debug("We have a match, stop listening to events")
            device = _configured_atvs[entity_id]
            await device.disconnect()
            device.events.remove_all_listeners()


async def media_player_cmd_handler(
    entity: MediaPlayer, cmd_id: str, params: dict[str, Any] | None
) -> ucapi.StatusCodes:
    """
    Media-player entity command handler.

    Called by the integration-API if a command is sent to a configured media-player entity.

    :param entity: media-player entity
    :param cmd_id: command
    :param params: optional command parameters
    :return:
    """
    _LOG.info("Got %s command request: %s %s", entity.id, cmd_id, params)

    # TODO map from device id to entities (see Denon integration)
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

    res = ucapi.StatusCodes.NOT_IMPLEMENTED

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
            res = await device.cursor_enter()
        case media_player.Commands.HOME:
            res = await device.home()

            # we wait a bit to get a push update, because music can play in the background
            await asyncio.sleep(1)
            if configured_entity.attributes[media_player.Attributes.STATE] != media_player.States.PLAYING:
                # if nothing is playing we clear the playing information
                attributes = {}
                attributes[media_player.Attributes.MEDIA_IMAGE_URL] = ""
                attributes[media_player.Attributes.MEDIA_ALBUM] = ""
                attributes[media_player.Attributes.MEDIA_ARTIST] = ""
                attributes[media_player.Attributes.MEDIA_TITLE] = ""
                attributes[media_player.Attributes.MEDIA_TYPE] = ""
                attributes[media_player.Attributes.SOURCE] = ""
                attributes[media_player.Attributes.MEDIA_DURATION] = 0
                api.configured_entities.update_attributes(entity.id, attributes)
        case media_player.Commands.BACK:
            res = await device.menu()
        case media_player.Commands.CHANNEL_DOWN:
            res = await device.channel_down()
        case media_player.Commands.CHANNEL_UP:
            res = await device.channel_up()
        case media_player.Commands.SELECT_SOURCE:
            res = await device.launch_app(params["source"])

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
    configured_entity = api.configured_entities.get(identifier)

    if configured_entity.attributes[media_player.Attributes.STATE] == media_player.States.UNAVAILABLE:
        api.configured_entities.update_attributes(
            identifier, {media_player.Attributes.STATE: media_player.States.STANDBY}
        )


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


# TODO refactor & simply on_atv_update, then remove pylint exceptions
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
        atv.disconnect()
    else:
        _LOG.debug("Adding new ATV device: %s (%s)", device.identifier, device.name)
        atv = tv.AppleTv(_LOOP)
        atv.init(device.identifier, device.credentials, device.name)
        atv.events.on(tv.EVENTS.CONNECTED, on_atv_connected)
        atv.events.on(tv.EVENTS.DISCONNECTED, on_atv_disconnected)
        atv.events.on(tv.EVENTS.ERROR, on_atv_connection_error)
        atv.events.on(tv.EVENTS.UPDATE, on_atv_update)

        _configured_atvs[device.identifier] = atv

        # await atv.connect()

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
    # TODO map entity IDs from device identifier
    entity_id = identifier
    # plain and simple for now: only one media_player per ATV device
    entity = MediaPlayer(
        entity_id,
        name,
        [
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
        ],
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
        # TODO
    else:
        _LOG.debug("Device removed: %s", device)
        # TODO


async def main():
    """Start the Remote Two integration driver."""
    logging.basicConfig()

    level = os.getenv("UC_LOG_LEVEL", "DEBUG").upper()
    logging.getLogger("tv").setLevel(level)
    logging.getLogger("driver").setLevel(level)
    logging.getLogger("discover").setLevel(level)
    logging.getLogger("setup_flow").setLevel(level)

    config.devices = config.Devices(api.config_dir_path, on_device_added, on_device_removed)
    for device in config.devices.all():
        _add_configured_atv(device, connect=False)

    await api.init("driver.json", setup_flow.driver_setup_handler)


if __name__ == "__main__":
    _LOOP.run_until_complete(main())
    _LOOP.run_forever()
