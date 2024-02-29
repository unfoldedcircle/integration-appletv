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
    CONFIGURATION_MODE = 1
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
        _LOG.debug("%s", msg)
        if _setup_step == SetupSteps.CONFIGURATION_MODE and "address" in msg.input_values:
            return await handle_configuration_mode(msg)
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
            await _pairing_apple_tv.disconnect()
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
    Ask user to enter ip-address for manual configuration, otherwise auto-discovery is used.

    :param _msg: not used, we don't have any input fields in the first setup screen.
    :return: the setup action on how to continue
    """
    global _setup_step

    _LOG.debug("Starting driver setup")
    _setup_step = SetupSteps.CONFIGURATION_MODE
    # pylint: disable=line-too-long
    return RequestUserInput(
        {"en": "Setup mode", "de": "Setup Modus"},
        [
            {
                "id": "info",
                "label": {
                    "en": "Discover or connect to Apple TV device",
                    "de": "Suche oder Verbinde auf Apple TV Gerät",
                    "fr": "Découvrir ou connexion à l'appareil Apple TV",
                },
                "field": {
                    "label": {
                        "value": {
                            "en": "Leave blank to use auto-discovery and click _Next_.",
                            "de": "Leer lassen, um automatische Erkennung zu verwenden und auf _Weiter_ klicken.",
                            "fr": "Laissez le champ vide pour utiliser la découverte automatique et cliquez sur _Suivant_.",  # noqa: E501
                        }
                    }
                },
            },
            {
                "field": {"text": {"value": ""}},
                "id": "address",
                "label": {"en": "IP address", "de": "IP-Adresse", "fr": "Adresse IP"},
            },
        ],
    )


async def handle_configuration_mode(msg: UserDataResponse) -> RequestUserInput | SetupError:
    """
    Process user data response from the first setup process screen.

    If ``address`` field is set by the user: try connecting to device and retrieve device information.
    Otherwise, start Apple TV discovery and present the found devices to the user to choose from.

    :param msg: response data from the requested user data
    :return: the setup action on how to continue
    """
    # global _discovered_atvs
    global _pairing_apple_tv
    global _setup_step

    # clear all configured devices and any previous pairing attempt
    if _pairing_apple_tv:
        await _pairing_apple_tv.disconnect()
        _pairing_apple_tv = None
    # TODO allow multiple devices!
    config.devices.clear()  # triggers device instance removal

    search_hosts: list[str] | None = None
    dropdown_items = []
    address = msg.input_values["address"]

    if address:
        _LOG.debug("Starting manual driver setup for: %s", address)
        # Connect to specific device and retrieve name
        search_hosts = [address]
    else:
        _LOG.debug("Starting driver setup with Apple TV discovery")

    tvs = await discover.apple_tvs(asyncio.get_event_loop(), hosts=search_hosts)

    for device in tvs:
        _LOG.info("Found Apple TV: %s", device)
        tv_data = {"id": device.identifier, "label": {"en": device.name + " tvOS " + str(device.device_info.version)}}

        dropdown_items.append(tv_data)

    if not dropdown_items:
        _LOG.warning("No Apple TVs found")
        return SetupError(error_type=IntegrationSetupError.NOT_FOUND)

    _setup_step = SetupSteps.DEVICE_CHOICE
    # TODO externalize language texts
    return RequestUserInput(
        {"en": "Please choose your Apple TV", "de": "Bitte wähle deinen Apple TV", "fr": "Choisissez votre Apple TV"},
        [
            {
                "field": {"dropdown": {"value": dropdown_items[0]["id"], "items": dropdown_items}},
                "id": "choice",
                "label": {
                    "en": "Choose your Apple TV",
                    "de": "Wähle deinen Apple TV",
                    "fr": "Choisissez votre Apple TV",
                },
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
    global _setup_step

    choice = msg.input_values["choice"]
    # name = ""
    _LOG.debug("Chosen Apple TV: %s", choice)

    # Create a new AppleTv object
    # TODO refactor for manually entered IP --> this will most likely NOT work if in different subnet!
    # TODO exception handling?
    atvs = await pyatv.scan(asyncio.get_event_loop(), identifier=choice)
    if not atvs:
        _LOG.error("Cannot find the chosen Apple TV: %s", choice)
        return SetupError(error_type=IntegrationSetupError.NOT_FOUND)

    atv = atvs[0]
    _pairing_apple_tv = tv.AppleTv(choice, atv.name, loop=asyncio.get_event_loop(), pairing_atv=atv)

    _LOG.debug("Pairing process begin")
    # Hook up to signals
    # TODO error conditions in start_pairing?
    res = await _pairing_apple_tv.start_pairing(pyatv.const.Protocol.AirPlay, "Remote Two Airplay")
    if res is None:
        return SetupError()

    if res == 0:
        _LOG.debug("Device provides AirPlay-Code")
        _setup_step = SetupSteps.PAIRING_AIRPLAY
        return RequestUserInput(
            {
                "en": "Please enter the shown AirPlay-Code on your Apple TV",
                "de": "Bitte gib die angezeigte AirPlay-Code auf deinem Apple TV ein",
                "fr": "Veuillez entrer le code AirPlay affiché sur votre Apple TV",
            },
            [
                {
                    "field": {"number": {"max": 9999, "min": 0, "value": 0000}},
                    "id": "pin_airplay",
                    "label": {"en": "Apple TV AirPlay-Code"},
                }
            ],
        )

    _LOG.debug("We provide AirPlay-Code")
    return RequestUserConfirmation("Please enter the following PIN on your Apple TV: " + res)

    # # no better error code right now
    # return SetupError(error_type=IntegrationSetupError.CONNECTION_REFUSED)


async def handle_user_data_airplay_pin(msg: UserDataResponse) -> RequestUserInput | SetupError:
    """
    Process user data airplay pairing pin response in a setup process.

    Driver setup callback to provide requested user data during the setup process.

    :param msg: response data from the requested user data
    :return: the setup action on how to continue
    """
    global _setup_step

    _LOG.debug("User has entered the AirPlay PIN")

    if _pairing_apple_tv is None:
        _LOG.error("Pairing Apple TV device no longer available after entering AirPlay pin. Aborting setup")
        return SetupError()

    await _pairing_apple_tv.enter_pin(msg.input_values["pin_airplay"])

    res = await _pairing_apple_tv.finish_pairing()
    if res is None:
        return SetupError()

    # Store credentials
    c = {"protocol": res.protocol.name.lower(), "credentials": res.credentials}
    _pairing_apple_tv.add_credentials(c)

    # Start new pairing process
    res = await _pairing_apple_tv.start_pairing(pyatv.const.Protocol.Companion, "Remote Two Companion")
    if res is None:
        return SetupError()

    if res == 0:
        _LOG.debug("Device provides PIN")
        _setup_step = SetupSteps.PAIRING_COMPANION
        return RequestUserInput(
            {
                "en": "Please enter the shown companion PIN on your Apple TV",
                "de": "Bitte gib die angezeigte PIN auf deinem Apple TV ein",
                "fr": "Veuillez entrer le code PIN affiché sur votre Apple TV",
            },
            [
                {
                    "field": {"number": {"max": 9999, "min": 0, "value": 0000}},
                    "id": "pin_companion",
                    "label": {"en": "Apple TV Companion PIN"},
                }
            ],
        )

    _LOG.debug("We provide companion PIN")
    return RequestUserConfirmation("Please enter the following PIN on your Apple TV: " + res)


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
        _LOG.error("Pairing Apple TV device no longer available after entering companion pin. Aborting setup")
        return SetupError()

    await _pairing_apple_tv.enter_pin(msg.input_values["pin_companion"])

    res = await _pairing_apple_tv.finish_pairing()
    await _pairing_apple_tv.disconnect()

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
