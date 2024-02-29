"""
Setup flow for Apple TV Remote integration.

:copyright: (c) 2023-2024 by Unfolded Circle ApS.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import asyncio
import logging
from enum import IntEnum

import config
import discover
import pyatv
import tv
from config import AtvDevice
from ucapi import (
    AbortDriverSetup,
    DriverSetupRequest,
    IntegrationSetupError,
    RequestUserConfirmation,
    RequestUserInput,
    SetupAction,
    SetupComplete,
    SetupDriver,
    SetupError,
    UserDataResponse,
)

_LOG = logging.getLogger(__name__)


class SetupSteps(IntEnum):
    """Enumeration of setup steps to keep track of user data responses."""

    INIT = 0
    DEVICE_CHOICE = 2
    PAIRING_AIRPLAY = 3
    PAIRING_COMPANION = 4


_setup_step = SetupSteps.INIT
# _discovered_atvs: list[dict[str, str]] = []
_pairing_apple_tv: tv.AppleTv | None = None


async def driver_setup_handler(msg: SetupDriver) -> SetupAction:
    """
    Dispatch driver setup requests to corresponding handlers.

    Either start the setup process or handle the selected Apple TV device.

    :param msg: the setup driver request object, either DriverSetupRequest or UserDataResponse
    :return: the setup action on how to continue
    """
    global _setup_step
    global _pairing_apple_tv

    if isinstance(msg, DriverSetupRequest):
        _setup_step = SetupSteps.INIT
        return await handle_driver_setup(msg)
    if isinstance(msg, UserDataResponse):
        _LOG.debug("UserDataResponse: %s", msg)
        if _setup_step == SetupSteps.DEVICE_CHOICE and "choice" in msg.input_values:
            return await handle_device_choice(msg)
        if _setup_step == SetupSteps.PAIRING_AIRPLAY and "pin_airplay" in msg.input_values:
            return await handle_user_data_airplay_pin(msg)
        if _setup_step == SetupSteps.PAIRING_COMPANION and "pin_companion" in msg.input_values:
            return await handle_user_data_companion_pin(msg)
        _LOG.error("No or invalid user response was received: %s", msg)
    elif isinstance(msg, AbortDriverSetup):
        _LOG.info("Setup was aborted with code: %s", msg.error)
        if _pairing_apple_tv is not None:
            _pairing_apple_tv.disconnect()
            _pairing_apple_tv = None
        _setup_step = SetupSteps.INIT

    # user confirmation not used in setup process
    # if isinstance(msg, UserConfirmationResponse):
    #     return handle_user_confirmation(msg)

    return SetupError()


async def handle_driver_setup(_msg: DriverSetupRequest) -> RequestUserInput | SetupError:
    """
    Start driver setup.

    Initiated by Remote Two to set up the driver.

    :param _msg: not used, we don't have any input fields in the first setup screen.
    :return: the setup action on how to continue
    """
    global _setup_step

    _LOG.debug("Starting driver setup with Apple TV discovery")

    # clear all configured devices and any previous pairing attempt
    # if _pairing_apple_tv:
    #     _pairing_apple_tv.disconnect()
    #     _pairing_apple_tv = None
    config.devices.clear()

    tvs = await discover.apple_tvs(asyncio.get_event_loop())
    dropdown_items = []

    for device in tvs:
        tv_data = {"id": device.identifier, "label": {"en": device.name + " TvOS " + str(device.device_info.version)}}

        dropdown_items.append(tv_data)

    if not dropdown_items:
        _LOG.warning("No Apple TVs found")
        return SetupError(error_type=IntegrationSetupError.NOT_FOUND)

    _setup_step = SetupSteps.DEVICE_CHOICE
    return RequestUserInput(
        {"en": "Please choose your Apple TV", "de": "Bitte wähle deinen Apple TV"},
        [
            {
                "field": {"dropdown": {"value": dropdown_items[0]["id"], "items": dropdown_items}},
                "id": "choice",
                "label": {"en": "Choose your Apple TV", "de": "Wähle deinen Apple TV"},
            }
        ],
    )


async def handle_device_choice(msg: UserDataResponse) -> RequestUserInput | SetupError:
    """
    Process user data device choice response in a setup process.

    Driver setup callback to provide requested user data during the setup process.

    :param msg: response data from the requested user data
    :return: the setup action on how to continue.
    """
    global _pairing_apple_tv

    choice = msg.input_values["choice"]
    # name = ""
    _LOG.debug("Chosen Apple TV: %s", choice)

    # Create a new AppleTv object
    _pairing_apple_tv = tv.AppleTv(asyncio.get_event_loop())
    _pairing_apple_tv.pairing_atv = await _pairing_apple_tv.find_atv(choice)

    if _pairing_apple_tv.pairing_atv is None:
        _LOG.error("Cannot find the chosen AppleTV")
        return SetupError(error_type=IntegrationSetupError.NOT_FOUND)

    await _pairing_apple_tv.init(choice, name=_pairing_apple_tv.pairing_atv.name)

    _LOG.debug("Pairing process begin")
    # Hook up to signals
    # TODO error conditions in start_pairing?
    res = await _pairing_apple_tv.start_pairing(pyatv.const.Protocol.AirPlay, "Remote Two Airplay")

    if res == 0:
        _LOG.debug("Device provides PIN")
        return RequestUserInput(
            "Please enter the PIN from your Apple TV",
            [
                {
                    "field": {"number": {"max": 9999, "min": 0, "value": 0000}},
                    "id": "pin_airplay",
                    "label": {"en": "Apple TV PIN"},
                }
            ],
        )

    _LOG.debug("We provide PIN")
    # FIXME handle finish_pairing() in next step!
    await _pairing_apple_tv.finish_pairing()
    return RequestUserConfirmation("Please enter the following PIN on your Apple TV:" + res)

    # # no better error code right now
    # return SetupError(error_type=IntegrationSetupError.CONNECTION_REFUSED)


async def handle_user_data_airplay_pin(msg: UserDataResponse) -> RequestUserInput | SetupError:
    """
    Process user data airplay pairing pin response in a setup process.

    Driver setup callback to provide requested user data during the setup process.

    :param msg: response data from the requested user data
    :return: the setup action on how to continue
    """
    _LOG.debug("User has entered the Airplay PIN")
    await _pairing_apple_tv.enter_pin(msg.input_values["pin"])

    res = await _pairing_apple_tv.finish_pairing()
    if res is None:
        return SetupError()

    # Store credentials
    c = {"protocol": res.protocol.name.lower(), "credentials": res.credentials}
    _pairing_apple_tv.add_credentials(c)

    # Start new pairing process
    res = await _pairing_apple_tv.start_pairing(pyatv.const.Protocol.Companion, "Remote Two Companion")

    if res == 0:
        _LOG.debug("Device provides PIN")
        return RequestUserInput(
            "Please enter the PIN from your Apple TV",
            [
                {
                    "field": {"number": {"max": 9999, "min": 0, "value": 0000}},
                    "id": "pin_companion",
                    "label": {"en": "Apple TV PIN"},
                }
            ],
        )

    _LOG.debug("We provide PIN")
    # FIXME handle finish_pairing() in next step!
    await _pairing_apple_tv.finish_pairing()
    return RequestUserConfirmation("Please enter the following PIN on your Apple TV:" + res)

    # global _pairing_apple_tv
    #
    # _LOG.info("User has entered the PIN")
    #
    # if _pairing_apple_tv is None:
    #     _LOG.error("Can't handle pairing pin: no device instance! Aborting setup")
    #     return SetupError()
    #
    # res = await _pairing_apple_tv.finish_pairing(msg.input_values["pin"])
    # _pairing_apple_tv.disconnect()
    #
    # if res != ucapi.StatusCodes.OK:
    #     _pairing_apple_tv = None
    #     if res == ucapi.StatusCodes.UNAUTHORIZED:
    #         return SetupError(error_type=IntegrationSetupError.AUTHORIZATION_ERROR)
    #     return SetupError(error_type=IntegrationSetupError.CONNECTION_REFUSED)
    #
    # device = AtvDevice(_pairing_apple_tv.identifier, _pairing_apple_tv.name, _pairing_apple_tv.address)
    # config.devices.add(device)  # triggers AndroidTv instance creation
    # config.devices.store()
    #
    # # ATV device connection will be triggered with subscribe_entities request
    #
    # _pairing_apple_tv = None
    # await asyncio.sleep(1)
    #
    # _LOG.info("Setup successfully completed for %s", device.name)
    # return SetupComplete()


async def handle_user_data_companion_pin(msg: UserDataResponse) -> SetupComplete | SetupError:
    """
    Process user data companion pairing pin response in a setup process.

    Driver setup callback to provide requested user data during the setup process.

    :param msg: response data from the requested user data
    :return: the setup action on how to continue: SetupComplete if a valid Apple TV device was chosen.
    """
    global _pairing_apple_tv

    _LOG.debug("User has entered the Companion PIN")

    if _pairing_apple_tv is None:
        _LOG.error("Can't handle pairing pin: no device instance! Aborting setup")
        return SetupError()

    await _pairing_apple_tv.enter_pin(msg.input_values["pin_companion"])

    res = await _pairing_apple_tv.finish_pairing()
    _pairing_apple_tv.disconnect()

    if res is None:
        _pairing_apple_tv = None
        return SetupError()

    c = {"protocol": res.protocol.name.lower(), "credentials": res.credentials}
    _pairing_apple_tv.add_credentials(c)

    device = AtvDevice(
        identifier=_pairing_apple_tv.identifier,
        name=_pairing_apple_tv.name,
        credentials=_pairing_apple_tv.get_credentials(),
    )
    config.devices.add(device)  # triggers ATV instance creation
    config.devices.store()

    # ATV device connection will be triggered with subscribe_entities request

    _pairing_apple_tv = None
    await asyncio.sleep(1)

    _LOG.info("Setup successfully completed for %s", device.name)

    return SetupComplete()
