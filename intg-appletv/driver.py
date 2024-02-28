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
import tv
import ucapi
import ucapi.api as uc
from ucapi import MediaPlayer, media_player

LOG = logging.getLogger(__name__)
LOOP = asyncio.get_event_loop()

# Global variables
api = uc.IntegrationAPI(LOOP)
configuredAppleTvs = {}
pairing_apple_tv = None


# DRIVER SETUP
# @api.events.on(uc.uc.EVENTS.SETUP_DRIVER)
# async def on_setup_driver(websocket, req_id, _data):
#     LOG.debug("Starting driver setup")
#     config.devices.clear()
#     await api.acknowledge_command(websocket, req_id)
#     await api.driver_setup_progress(websocket)
#
#     LOG.debug("Starting Apple TV discovery")
#     tvs = await discover.apple_tvs(LOOP)
#     dropdown_items = []
#
#     for device in tvs:
#         tv_data = {"id": device.identifier, "label": {"en": device.name + " TvOS " + str(device.device_info.version)}}
#
#         dropdown_items.append(tv_data)
#
#     if not dropdown_items:
#         LOG.warning("No Apple TVs found")
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
#         LOG.debug("User has entered the Companion PIN")
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
#         LOG.debug("User has entered the Airplay PIN")
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
#                 LOG.debug("Device provides PIN")
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
#                 LOG.debug("We provide PIN")
#                 await api.request_driver_setup_user_confirmation(
#                     websocket, "Please enter the following PIN on your Apple TV:" + res
#                 )
#                 await pairing_apple_tv.finish_pairing()
#
#     elif "choice" in data:
#         choice = data["choice"]
#         LOG.debug("Chosen Apple TV: %s", choice)
#
#         # Create a new AppleTv object
#         pairing_apple_tv = tv.AppleTv(LOOP)
#         pairing_apple_tv.pairing_atv = await pairing_apple_tv.find_atv(choice)
#
#         if pairing_apple_tv.pairing_atv is None:
#             LOG.error("Cannot find the chosen AppleTV")
#             await api.driver_setup_error(websocket)
#             return
#
#         await pairing_apple_tv.init(choice, name=pairing_apple_tv.pairing_atv.name)
#
#         LOG.debug("Pairing process begin")
#         # Hook up to signals
#         res = await pairing_apple_tv.start_pairing(pyatv.const.Protocol.AirPlay, "Remote Two Airplay")
#
#         if res == 0:
#             LOG.debug("Device provides PIN")
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
#             LOG.debug("We provide PIN")
#             await api.request_driver_setup_user_confirmation(
#                 websocket, "Please enter the following PIN on your Apple TV:" + res
#             )
#             await pairing_apple_tv.finish_pairing()
#
#     else:
#         LOG.error("No choice was received")
#         await api.driver_setup_error(websocket)


@api.listens_to(ucapi.Events.CONNECT)
async def on_r2_connect_cmd() -> None:
    """Connect all configured ATVs when the Remote Two sends the connect command."""
    # FIXME connect all configured ATVs !!!
    await api.set_device_state(ucapi.DeviceStates.CONNECTED)


@api.listens_to(ucapi.Events.DISCONNECT)
async def on_r2_disconnect_cmd():
    """Disconnect all configured ATVs when the Remote Two sends the disconnect command."""
    for device in configuredAppleTvs.values():
        LOG.debug("Client disconnected, disconnecting all Apple TVs")
        await device.disconnect()
        device.events.remove_all_listeners()

    await api.set_device_state(ucapi.DeviceStates.DISCONNECTED)


@api.listens_to(ucapi.Events.ENTER_STANDBY)
async def on_r2_enter_standby() -> None:
    """
    Enter standby notification from Remote Two.

    Disconnect every ATV instances.
    """
    for device in configuredAppleTvs.values():
        await device.disconnect()


@api.listens_to(ucapi.Events.EXIT_STANDBY)
async def on_r2_exit_standby() -> None:
    """
    Exit standby notification from Remote Two.

    Connect all ATV instances.
    """
    for device in configuredAppleTvs.values():
        await device.connect()


@api.listens_to(ucapi.Events.SUBSCRIBE_ENTITIES)
async def on_subscribe_entities(entity_ids: list[str]) -> None:
    """
    Subscribe to given entities.

    :param entity_ids: entity identifiers.
    """
    for entity_id in entity_ids:
        if entity_id in configuredAppleTvs:
            LOG.debug("We have a match, start listening to events")

            api.configured_entities.update_attributes(
                entity_id, {media_player.Attributes.STATE: media_player.States.UNAVAILABLE}
            )

            device = configuredAppleTvs[entity_id]

            @device.events.on(tv.EVENTS.CONNECTED)
            async def _on_connected(identifier):
                await on_atv_connected(identifier)

            @device.events.on(tv.EVENTS.DISCONNECTED)
            async def _on_disconnected(identifier):
                await on_atv_disconnected(identifier)

            @device.events.on(tv.EVENTS.ERROR)
            async def _on_disconnected(identifier, message):
                await on_atv_connection_error(identifier, message)

            @device.events.on(tv.EVENTS.UPDATE)
            async def on_update(update):
                await on_atv_update(entity_id, update)

            await device.connect()


@api.listens_to(ucapi.Events.UNSUBSCRIBE_ENTITIES)
async def on_unsubscribe_entities(entity_ids: list[str]) -> None:
    """On unsubscribe, we disconnect the objects and remove listeners for events."""
    for entity_id in entity_ids:
        if entity_id in configuredAppleTvs:
            LOG.debug("We have a match, stop listening to events")
            device = configuredAppleTvs[entity_id]
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
    LOG.info("Got %s command request: %s %s", entity.id, cmd_id, params)

    # TODO map from device id to entities (see Denon integration)
    # atv_id = _tv_from_entity_id(entity.id)
    # if atv_id is None:
    #     return ucapi.StatusCodes.NOT_FOUND
    atv_id = entity.id

    device = configuredAppleTvs[atv_id]

    # If the device is not on we send SERVICE_UNAVAILABLE
    if device.is_on is False:
        return ucapi.StatusCodes.SERVICE_UNAVAILABLE

    configured_entity = api.configured_entities.get(entity.id)

    if configured_entity is None:
        LOG.warning("No Apple TV device found for entity: %s", entity.id)
        return ucapi.StatusCodes.SERVICE_UNAVAILABLE

    # If the entity is OFF, we send the turnOn command regardless of the actual command
    if configured_entity.attributes[media_player.Attributes.STATE] == media_player.States.OFF:
        LOG.debug("Apple TV is off, sending turn on command")
        return await device.turn_on()

    res = ucapi.StatusCodes.NOT_IMPLEMENTED

    if cmd_id == media_player.Commands.PLAY_PAUSE:
        res = await device.play_pause()
    elif cmd_id == media_player.Commands.NEXT:
        res = await device.next()
    elif cmd_id == media_player.Commands.PREVIOUS:
        res = await device.previous()
    elif cmd_id == media_player.Commands.VOLUME_UP:
        res = await device.volume_up()
    elif cmd_id == media_player.Commands.VOLUME_DOWN:
        res = await device.volume_down()
    elif cmd_id == media_player.Commands.ON:
        res = await device.turn_on()
    elif cmd_id == media_player.Commands.OFF:
        res = await device.turn_off()
    elif cmd_id == media_player.Commands.CURSOR_UP:
        res = await device.cursor_up()
    elif cmd_id == media_player.Commands.CURSOR_DOWN:
        res = await device.cursor_down()
    elif cmd_id == media_player.Commands.CURSOR_LEFT:
        res = await device.cursor_left()
    elif cmd_id == media_player.Commands.CURSOR_RIGHT:
        res = await device.cursor_right()
    elif cmd_id == media_player.Commands.CURSOR_ENTER:
        res = await device.cursor_enter()
    elif cmd_id == media_player.Commands.HOME:
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
    elif cmd_id == media_player.Commands.BACK:
        res = await device.menu()
    elif cmd_id == media_player.Commands.CHANNEL_DOWN:
        res = await device.channel_down()
    elif cmd_id == media_player.Commands.CHANNEL_UP:
        res = await device.channel_up()
    elif cmd_id == media_player.Commands.SELECT_SOURCE:
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
    LOG.debug("Apple TV connected: %s", identifier)
    configured_entity = api.configured_entities.get(identifier)

    if configured_entity.attributes[media_player.Attributes.STATE] == media_player.States.UNAVAILABLE:
        api.configured_entities.update_attributes(
            identifier, {media_player.Attributes.STATE: media_player.States.STANDBY}
        )


async def on_atv_disconnected(identifier: str) -> None:
    """Handle ATV disconnection."""
    LOG.debug("Apple TV disconnected: %s", identifier)
    api.configured_entities.update_attributes(
        identifier, {media_player.Attributes.STATE: media_player.States.UNAVAILABLE}
    )


async def on_atv_connection_error(identifier: str, message) -> None:
    """Set entities of ATV to state UNAVAILABLE if ATV connection error occurred."""
    LOG.error(message)
    api.configured_entities.update_attributes(
        identifier, {media_player.Attributes.STATE: media_player.States.UNAVAILABLE}
    )
    await api.set_device_state(ucapi.DeviceStates.ERROR)


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
        state = media_player.States.UNKNOWN

        if update["state"] == pyatv.const.PowerState.On:
            state = media_player.States.ON
        elif update["state"] == pyatv.const.DeviceState.Playing:
            state = media_player.States.PLAYING
        elif update["state"] == pyatv.const.DeviceState.Playing:
            state = media_player.States.PLAYING
        elif update["state"] == pyatv.const.DeviceState.Paused:
            state = media_player.States.PAUSED
        elif update["state"] == pyatv.const.DeviceState.Idle:
            state = media_player.States.PAUSED
        elif update["state"] == pyatv.const.PowerState.Off:
            state = media_player.States.OFF

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


def add_available_apple_tv(identifier: str, name: str) -> bool:
    """
    Add a new ATV device to the available entities.

    :param identifier: ATV identifier
    :param name: Friendly name
    :return: True if added, False if the device was already in storage.
    """
    entity = MediaPlayer(
        identifier,
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

    return api.available_entities.add(entity)


def on_device_added(device: config.AtvDevice) -> None:
    """Handle a newly added device in the configuration."""
    LOG.debug("New device added: %s", device)
    # TODO


def on_device_removed(device: config.AtvDevice | None) -> None:
    """Handle a removed device in the configuration."""
    if device is None:
        LOG.debug("Configuration cleared, disconnecting & removing all configured ATV instances")
        # TODO
    else:
        LOG.debug("Device removed: %s", device)
        # TODO


async def main():
    """Start the Remote Two integration driver."""
    logging.basicConfig()

    level = os.getenv("UC_LOG_LEVEL", "DEBUG").upper()
    logging.getLogger("tv").setLevel(level)
    logging.getLogger("discover").setLevel(level)
    logging.getLogger("driver").setLevel(level)

    config.devices = config.Devices(api.config_dir_path, on_device_added, on_device_removed)
    for device in config.devices.all():
        # _add_configured_apple_tv(device, connect=False)
        apple_tv = tv.AppleTv(LOOP)
        await apple_tv.init(device.identifier, device.credentials, device.name)
        configuredAppleTvs[apple_tv.identifier] = apple_tv

        add_available_apple_tv(device.identifier, device.name)

    await api.init("driver.json")


if __name__ == "__main__":
    LOOP.run_until_complete(main())
    LOOP.run_forever()
