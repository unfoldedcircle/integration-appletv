"""
Setup flow for Apple TV Remote integration.

:copyright: (c) 2023 by Unfolded Circle ApS.
:license: MPL-2.0, see LICENSE for more details.
"""

import asyncio
import logging
from enum import IntEnum

import config
import discover
import tv
import ucapi
from config import AtvDevice
from ucapi import (
    AbortDriverSetup,
    DriverSetupRequest,
    IntegrationSetupError,
    RequestUserInput,
    RequestUserConfirmation,
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
# _discovered_android_tvs: list[dict[str, str]] = []
# _pairing_android_tv: tv.AndroidTv | None = None
pairing_apple_tv = None


async def driver_setup_handler(msg: SetupDriver) -> SetupAction:
    """
    Dispatch driver setup requests to corresponding handlers.

    Either start the setup process or handle the selected Apple TV device.

    :param msg: the setup driver request object, either DriverSetupRequest or UserDataResponse
    :return: the setup action on how to continue
    """
    global _setup_step
    global _pairing_android_tv

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
        if _pairing_android_tv is not None:
            _pairing_android_tv.disconnect()
            _pairing_android_tv = None
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
    # if _pairing_android_tv:
    #     _pairing_android_tv.disconnect()
    #     _pairing_android_tv = None
    config.devices.clear()

    tvs = await discover.apple_tvs(LOOP)
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
    global pairing_apple_tv
    global _setup_step

    choice = msg.input_values["choice"]
    # name = ""
    _LOG.debug("Chosen Apple TV: %s", choice)

    # Create a new AppleTv object
    pairing_apple_tv = tv.AppleTv(LOOP)
    pairing_apple_tv.pairing_atv = await pairing_apple_tv.find_atv(choice)

    if pairing_apple_tv.pairing_atv is None:
        _LOG.error("Cannot find the chosen AppleTV")
        return SetupError(error_type=IntegrationSetupError.NOT_FOUND)

    await pairing_apple_tv.init(choice, name=pairing_apple_tv.pairing_atv.name)

    _LOG.debug("Pairing process begin")
    # Hook up to signals
    # TODO error conditions in start_pairing?
    res = await pairing_apple_tv.start_pairing(pyatv.const.Protocol.AirPlay, "Remote Two Airplay")

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

    else:
        _LOG.debug("We provide PIN")
        # FIXME handle finish_pairing() in next step!
        await pairing_apple_tv.finish_pairing()
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
    await pairing_apple_tv.enter_pin(msg.input_values["pin"])

    res = await pairing_apple_tv.finish_pairing()
    if res is None:
        return SetupError()

    # Store credentials
    c = {"protocol": res.protocol.name.lower(), "credentials": res.credentials}
    pairing_apple_tv.add_credentials(c)

    # Start new pairing process
    res = await pairing_apple_tv.start_pairing(pyatv.const.Protocol.Companion, "Remote Two Companion")

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

    else:
        _LOG.debug("We provide PIN")
        # FIXME handle finish_pairing() in next step!
        await pairing_apple_tv.finish_pairing()
        return RequestUserConfirmation("Please enter the following PIN on your Apple TV:" + res)

    # global _pairing_android_tv
    #
    # _LOG.info("User has entered the PIN")
    #
    # if _pairing_android_tv is None:
    #     _LOG.error("Can't handle pairing pin: no device instance! Aborting setup")
    #     return SetupError()
    #
    # res = await _pairing_android_tv.finish_pairing(msg.input_values["pin"])
    # _pairing_android_tv.disconnect()
    #
    # if res != ucapi.StatusCodes.OK:
    #     _pairing_android_tv = None
    #     if res == ucapi.StatusCodes.UNAUTHORIZED:
    #         return SetupError(error_type=IntegrationSetupError.AUTHORIZATION_ERROR)
    #     return SetupError(error_type=IntegrationSetupError.CONNECTION_REFUSED)
    #
    # device = AtvDevice(_pairing_android_tv.identifier, _pairing_android_tv.name, _pairing_android_tv.address)
    # config.devices.add(device)  # triggers AndroidTv instance creation
    # config.devices.store()
    #
    # # ATV device connection will be triggered with subscribe_entities request
    #
    # _pairing_android_tv = None
    # await asyncio.sleep(1)
    #
    # _LOG.info("Setup successfully completed for %s", device.name)
    # return SetupComplete()
