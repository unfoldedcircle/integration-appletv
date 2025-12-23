"""
Setup flow for Apple TV Remote integration.

:copyright: (c) 2023-2024 by Unfolded Circle ApS.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import asyncio
import logging
import os
import socket
from enum import IntEnum
from typing import Set

import config
import discover
import pyatv
import tv
from config import AtvDevice, AtvProtocol
from i18n import __, _a, _af, _am
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
    DISCOVER = 2
    DEVICE_CHOICE = 3
    PAIRING_AIRPLAY = 4
    PAIRING_COMPANION = 5
    PAIRING_DMAP = 6
    PAIRING_MRP = 7
    RECONFIGURE = 8


_SETUP_STEP = SetupSteps.INIT
_CFG_ADD_DEVICE: bool = False
_MANUAL_ADDRESS: bool = False
_DISCOVERED_ATVS: list[pyatv.interface.BaseConfig] | None = None
_PAIRING_APPLE_TV: tv.AppleTv | None = None
_RECONFIGURED_DEVICE: AtvDevice | None = None
_SUPPORTED_PROTOCOLS: Set[pyatv.const.Protocol] | None = None
_CURRENT_PROTOCOL: pyatv.const.Protocol | None = None

MAP_PROTOCOL_TO_SETUP_STEP = {
    pyatv.const.Protocol.AirPlay: SetupSteps.PAIRING_AIRPLAY,
    pyatv.const.Protocol.Companion: SetupSteps.PAIRING_COMPANION,
    pyatv.const.Protocol.DMAP: SetupSteps.PAIRING_DMAP,
    pyatv.const.Protocol.MRP: SetupSteps.PAIRING_MRP,
}

MAP_PROTOCOL_TO_ATV_PROTOCOL = {
    pyatv.const.Protocol.AirPlay: AtvProtocol.AIRPLAY,
    pyatv.const.Protocol.Companion: AtvProtocol.COMPANION,
    pyatv.const.Protocol.DMAP: AtvProtocol.DMAP,
    pyatv.const.Protocol.MRP: AtvProtocol.MRP,
}


def setup_data_schema():
    """
    Get the JSON setup data structure for the driver.json file.

    :return: ``setup_data_schema`` json object
    """
    # pylint: disable=line-too-long
    return {
        "title": _a("Integration setup"),
        "settings": [
            {
                "id": "info",
                "label": _a("Setup process"),
                "field": {
                    "label": {
                        "value": _am(
                            __("The integration will discover your Apple TV on your network."),
                            "\n",
                            __(
                                "Apple TV 4 and newer are supported and the device must be on the same network as the remote."
                            ),
                            "\n",
                            __(
                                "During the process, you need to enter multiple PINs that are shown on your Apple TV. Please make sure to set AirPlay access to _Anyone on the Same Network_ in Apple TV settings."
                            ),
                            "\n\n",
                            # Translators: Make sure to include the support article link as Markdown. See English text
                            __("Please see our support article for requirements, features and restrictions."),
                        )
                    }
                },
            }
        ],
    }


async def driver_setup_handler(msg: SetupDriver) -> SetupAction:  # pylint: disable=too-many-return-statements
    """
    Dispatch driver setup requests to corresponding handlers.

    Either start the setup process or handle the selected Apple TV device.

    :param msg: the setup driver request object, either DriverSetupRequest or UserDataResponse
    :return: the setup action on how to continue
    """
    global _SETUP_STEP
    global _CFG_ADD_DEVICE
    global _PAIRING_APPLE_TV

    if isinstance(msg, DriverSetupRequest):
        _SETUP_STEP = SetupSteps.INIT
        _CFG_ADD_DEVICE = False
        return await _handle_driver_setup(msg)

    if isinstance(msg, UserDataResponse):
        _LOG.debug("%s", msg)
        if _SETUP_STEP == SetupSteps.CONFIGURATION_MODE and "action" in msg.input_values:
            return await _handle_configuration_mode(msg)
        if _SETUP_STEP == SetupSteps.DISCOVER and "address" in msg.input_values:
            return await _handle_discovery(msg)
        if _SETUP_STEP == SetupSteps.DEVICE_CHOICE and "choice" in msg.input_values:
            return await _handle_device_choice(msg)
        if (
            _SETUP_STEP in MAP_PROTOCOL_TO_SETUP_STEP.values()
            and f"pin_{_CURRENT_PROTOCOL.name.lower()}" in msg.input_values
        ):
            return await _handle_user_data_pin(msg)
        if _SETUP_STEP == SetupSteps.RECONFIGURE:
            return await _handle_device_reconfigure(msg)
        _LOG.error("No or invalid user response was received: %s", msg)
    elif isinstance(msg, AbortDriverSetup):
        _LOG.info("Setup was aborted with code: %s", msg.error)
        if _PAIRING_APPLE_TV is not None:
            await _PAIRING_APPLE_TV.disconnect()
            _PAIRING_APPLE_TV = None
        _SETUP_STEP = SetupSteps.INIT

    # user confirmation not used in setup process
    # if isinstance(msg, UserConfirmationResponse):
    #     return handle_user_confirmation(msg)

    return SetupError()


async def _handle_driver_setup(msg: DriverSetupRequest) -> RequestUserInput | SetupError:
    """
    Start driver setup.

    Initiated by Remote Two to set up the driver. The reconfigure flag determines the setup flow:

    - Reconfigure is True: show the configured devices and ask user what action to perform (add, delete, reset).
    - Reconfigure is False: clear the existing configuration and show device discovery screen.
      Ask user to enter ip-address for manual configuration, otherwise auto-discovery is used.

    :param msg: driver setup request data, only `reconfigure` flag is of interest.
    :return: the setup action on how to continue
    """
    global _SETUP_STEP

    reconfigure = msg.reconfigure
    _LOG.debug("Starting driver setup, reconfigure=%s", reconfigure)

    if reconfigure:
        _SETUP_STEP = SetupSteps.CONFIGURATION_MODE

        # make sure configuration is up-to-date
        if config.devices.migration_required():
            await config.devices.migrate()

        # check after devices change and update configuration if necessary
        await config.devices.handle_devices_change()

        # get all configured devices for the user to choose from
        dropdown_devices = []
        for device in config.devices.all():
            dropdown_devices.append({"id": device.identifier, "label": {"en": f"{device.name} ({device.identifier})"}})

        # build user actions, based on available devices
        dropdown_actions = [
            {
                "id": "add",
                "label": _a("Add a new device"),
            },
        ]

        # add remove, reconfigure & reset actions if there's at least one configured device
        if dropdown_devices:
            dropdown_actions.append(
                {
                    "id": "remove",
                    "label": _a("Delete selected device"),
                },
            )
            dropdown_actions.append(
                {
                    "id": "configure",
                    "label": _a("Configure selected device"),
                },
            )
            dropdown_actions.append(
                {
                    "id": "reset",
                    "label": _a("Reset configuration and reconfigure"),
                },
            )
        else:
            # dummy entry if no devices are available
            dropdown_devices.append({"id": "", "label": {"en": "---"}})

        return RequestUserInput(
            _a("Configuration mode"),
            [
                {
                    "field": {"dropdown": {"value": dropdown_devices[0]["id"], "items": dropdown_devices}},
                    "id": "choice",
                    "label": _a("Configured devices"),
                },
                {
                    "field": {"dropdown": {"value": dropdown_actions[0]["id"], "items": dropdown_actions}},
                    "id": "action",
                    "label": _a("Action"),
                },
            ],
        )

    # Initial setup, make sure we have a clean configuration
    config.devices.clear()  # triggers device instance removal
    _SETUP_STEP = SetupSteps.DISCOVER
    return __user_input_discovery()


async def _handle_configuration_mode(msg: UserDataResponse) -> RequestUserInput | SetupComplete | SetupError:
    """
    Process user data response from the configuration mode screen.

    User input data:

    - ``choice`` contains identifier of selected device
    - ``action`` contains the selected action identifier

    :param msg: user input data from the configuration mode screen.
    :return: the setup action on how to continue
    """
    global _SETUP_STEP
    global _CFG_ADD_DEVICE
    global _RECONFIGURED_DEVICE

    action = msg.input_values["action"]

    # workaround for web-configurator not picking up first response
    await asyncio.sleep(1)

    match action:
        case "add":
            _CFG_ADD_DEVICE = True
        case "remove":
            choice = msg.input_values["choice"]
            if not config.devices.remove(choice):
                _LOG.warning("Could not remove device from configuration: %s", choice)
                return SetupError(error_type=IntegrationSetupError.OTHER)
            config.devices.store()
            return SetupComplete()
        case "configure":
            # Reconfigure device if the identifier has changed
            choice = msg.input_values["choice"]
            selected_device = config.devices.get(choice)
            if not selected_device:
                _LOG.warning("Can not configure device from configuration: %s", choice)
                return SetupError(error_type=IntegrationSetupError.OTHER)

            discovered_atvs = await discover.apple_tvs(asyncio.get_event_loop())
            dropdown_items = []

            # Found mac address/identifier of selected AppleTV upon detection
            found_selected_device_id = ""
            for discovered_atv in discovered_atvs:
                # List of detected AppleTVs : exclude already configured ones except the one the user selected
                if (
                    selected_device.identifier != discovered_atv.identifier
                    and selected_device.mac_address != discovered_atv.identifier
                    and config.devices.contains(discovered_atv.identifier)
                ):
                    _LOG.info("Skipping device %s: already configured", discovered_atv.identifier)
                    continue
                if discovered_atv.identifier in [selected_device.identifier, selected_device.mac_address]:
                    found_selected_device_id = discovered_atv.identifier
                label = f"{discovered_atv.name} ({discovered_atv.address})"
                dropdown_items.append(
                    {"id": discovered_atv.identifier, "label": {"en": label + " (" + discovered_atv.identifier + ")"}}
                )

            dropdown_items.append(
                {
                    "id": "",
                    "label": _a("Manual MAC address (below)"),
                }
            )

            _SETUP_STEP = SetupSteps.RECONFIGURE
            _RECONFIGURED_DEVICE = selected_device
            mac_address = selected_device.mac_address if selected_device.mac_address else ""
            address = selected_device.address if selected_device.address else ""

            return RequestUserInput(
                _af("Configure your Apple TV (configured mac address {mac_address})", mac_address=mac_address),
                [
                    {
                        "field": {"dropdown": {"value": found_selected_device_id, "items": dropdown_items}},
                        "id": "mac_address",
                        "label": _a("MAC address"),
                    },
                    {
                        "field": {"text": {"value": mac_address}},
                        "id": "manual_mac_address",
                        "label": _a("Manual MAC address"),
                    },
                    {
                        "field": {"text": {"value": address}},
                        "id": "address",
                        "label": _a("IP address (optional)"),
                    },
                    __global_volume(True),
                ],
            )

        case "reset":
            config.devices.clear()  # triggers device instance removal
        case _:
            _LOG.error("Invalid configuration action: %s", action)
            return SetupError(error_type=IntegrationSetupError.OTHER)

    _SETUP_STEP = SetupSteps.DISCOVER
    return __user_input_discovery()


async def _handle_discovery(msg: UserDataResponse) -> RequestUserInput | SetupError:
    """
    Process user data response from the first setup process screen.

    If ``address`` field is set by the user: try connecting to device and retrieve device information.
    Otherwise, start Apple TV discovery and present the found devices to the user to choose from.

    :param msg: response data from the requested user data
    :return: the setup action on how to continue
    """
    global _PAIRING_APPLE_TV
    global _SETUP_STEP
    global _MANUAL_ADDRESS
    global _DISCOVERED_ATVS

    # clear all configured devices and any previous pairing attempt
    if _PAIRING_APPLE_TV:
        await _PAIRING_APPLE_TV.disconnect()
        _PAIRING_APPLE_TV = None

    search_hosts: list[str] | None = None
    dropdown_items = []
    address = msg.input_values["address"]

    if address:
        _LOG.debug("Starting manual driver setup for: %s", address)
        _MANUAL_ADDRESS = True
        # Connect to specific device and retrieve name
        search_hosts = [address]
    else:
        _LOG.debug("Starting driver setup with Apple TV discovery")
        _MANUAL_ADDRESS = False

    _DISCOVERED_ATVS = await discover.apple_tvs(asyncio.get_event_loop(), hosts=search_hosts)

    for device in _DISCOVERED_ATVS:
        _LOG.info(
            "Found: %s, %s (%s)",
            device.device_info,
            device.name,
            device.address,
        )
        # if we are adding a new device: make sure it's not already configured
        if _CFG_ADD_DEVICE and config.devices.contains(device.identifier):
            _LOG.info("Skipping found device %s: already configured", device.identifier)
            continue

        label = f"{device.name} ({device.address})"
        dropdown_items.append({"id": device.identifier, "label": {"en": label}})

    if not dropdown_items:
        _LOG.warning("No Apple TVs found")
        return SetupError(error_type=IntegrationSetupError.NOT_FOUND)

    _SETUP_STEP = SetupSteps.DEVICE_CHOICE
    return RequestUserInput(
        _a("Please choose your Apple TV"),
        [
            {
                "field": {"dropdown": {"value": dropdown_items[0]["id"], "items": dropdown_items}},
                "id": "choice",
                "label": _a("Choose your Apple TV"),
            },
            __global_volume(True),
        ],
    )


async def _handle_device_choice(msg: UserDataResponse) -> RequestUserInput | RequestUserConfirmation | SetupError:
    """
    Process user data device choice response in a setup process.

    Driver setup callback to provide requested user data during the setup process.

    :param msg: response data from the requested user data
    :return: the setup action on how to continue.
    """
    global _PAIRING_APPLE_TV
    global _SUPPORTED_PROTOCOLS

    choice = msg.input_values["choice"]
    global_volume = msg.input_values.get("global_volume", "true") == "true"

    atv = _discovered_atv_from_identifier(choice)
    if atv is None:
        _LOG.error("Chosen Apple TV not found in discovery: %s", choice)
        return SetupError(error_type=IntegrationSetupError.NOT_FOUND)

    _LOG.debug("Chosen Apple TV: %s", choice)

    # Create a new AppleTv object
    # TODO exception handling?
    atvs = await pyatv.scan(asyncio.get_event_loop(), identifier=choice, hosts=[str(atv.address)])
    if not atvs:
        _LOG.error("Cannot connect the chosen Apple TV: %s", choice)
        return SetupError(error_type=IntegrationSetupError.NOT_FOUND)

    atv = atvs[0]
    _PAIRING_APPLE_TV = tv.AppleTv(
        AtvDevice(
            identifier=choice,
            name=atv.name,
            credentials=[],
            address=str(atv.address) if _MANUAL_ADDRESS else None,
            mac_address=choice,
            global_volume=global_volume,
        ),
        loop=asyncio.get_event_loop(),
        pairing_atv=atv,
    )

    _LOG.debug("Pairing process begin")
    # Hook up to signals
    # TODO error conditions in start_pairing?
    # Determine supported protocols
    _SUPPORTED_PROTOCOLS = set()
    for service in atv.services:
        if service.protocol in MAP_PROTOCOL_TO_SETUP_STEP:
            _SUPPORTED_PROTOCOLS.add(service.protocol)

    return await _create_next_pairing_step()


async def _create_next_pairing_step() -> RequestUserInput | RequestUserConfirmation | SetupError | SetupComplete:
    global _CURRENT_PROTOCOL, _PAIRING_APPLE_TV, _SUPPORTED_PROTOCOLS
    _CURRENT_PROTOCOL = _SUPPORTED_PROTOCOLS.pop()
    if _CURRENT_PROTOCOL is None:
        # no more protocols to pair means we're done
        device = AtvDevice(
            identifier=_PAIRING_APPLE_TV.identifier,
            name=_PAIRING_APPLE_TV.name,
            credentials=_PAIRING_APPLE_TV.get_credentials(),
            address=_PAIRING_APPLE_TV.address,
            mac_address=_PAIRING_APPLE_TV.identifier,
            global_volume=_PAIRING_APPLE_TV.device_config.global_volume,
        )
        config.devices.add_or_update(device)  # triggers ATV instance creation

        # ATV device connection will be triggered with subscribe_entities request

        _PAIRING_APPLE_TV = None
        _SUPPORTED_PROTOCOLS = None
        await asyncio.sleep(1)

        _LOG.info("Setup successfully completed for %s", device.name)

        return SetupComplete()

    name = os.getenv("UC_CLIENT_NAME", socket.gethostname().split(".", 1)[0])
    res = await _PAIRING_APPLE_TV.start_pairing(_CURRENT_PROTOCOL, f"{name} {_CURRENT_PROTOCOL.name}")
    if res is None:
        return SetupError()

    if res == 0:
        _LOG.debug("Device provides %s-Code", _CURRENT_PROTOCOL.name)
        _setup_step = MAP_PROTOCOL_TO_SETUP_STEP[_CURRENT_PROTOCOL]
        return RequestUserInput(
            _af("Please enter the shown {protocol}-Code on your Apple TV", protocol=_CURRENT_PROTOCOL.name),
            [
                {
                    "field": {"number": {"max": 9999, "min": 0, "value": 0000}},
                    "id": f"pin_{_CURRENT_PROTOCOL.name.lower()}",
                    "label": _af("Apple TV {protocol}-Code", protocol=_CURRENT_PROTOCOL.name),
                }
            ],
        )

    _LOG.debug("We provide %s-Code", _CURRENT_PROTOCOL.name)
    return RequestUserConfirmation(
        _af(
            "Please enter the following {protocol}-Code on your Apple TV: {pin}",
            protocol=_CURRENT_PROTOCOL.name,
            pin=res,
        )
    )


async def _handle_user_data_pin(
    msg: UserDataResponse,
) -> RequestUserInput | RequestUserConfirmation | SetupError:
    """
    Process user data airplay pairing pin response in a setup process.

    Driver setup callback to provide requested user data during the setup process.

    :param msg: response data from the requested user data
    :return: the setup action on how to continue
    """

    _LOG.debug("User has entered the %s PIN", _CURRENT_PROTOCOL.name)

    if _PAIRING_APPLE_TV is None:
        _LOG.error("Pairing Apple TV device no longer available after entering AirPlay pin. Aborting setup")
        return SetupError()

    pin = int(msg.input_values[f"pin_{_CURRENT_PROTOCOL.name.lower()}"])
    await _PAIRING_APPLE_TV.enter_pin(pin)

    res = await _PAIRING_APPLE_TV.finish_pairing()
    if res is None:
        return SetupError()

    # Store credentials
    _PAIRING_APPLE_TV.add_credentials({MAP_PROTOCOL_TO_ATV_PROTOCOL[_CURRENT_PROTOCOL]: res.credentials})

    # Start new pairing process
    return await _create_next_pairing_step()


async def _handle_device_reconfigure(msg: UserDataResponse) -> SetupComplete | SetupError:
    """
    Process reconfiguration of a registered Apple TV device.

    :param msg: response data from the requested user data
    :return: the setup action on how to continue: SetupComplete after updating configuration
    """
    # flake8: noqa:F824
    # pylint: disable=W0602
    global _RECONFIGURED_DEVICE

    if _RECONFIGURED_DEVICE is None:
        return SetupError()

    mac_address = msg.input_values["mac_address"]
    manual_mac_address = msg.input_values["manual_mac_address"]
    global_volume = msg.input_values.get("global_volume", "true") == "true"

    if mac_address == "" and manual_mac_address == "":
        _LOG.error("MAC address is mandatory, no changes applied")
        return SetupError()
    address = msg.input_values["address"]
    if address == "":
        address = None
    if mac_address == "":
        mac_address = manual_mac_address

    _LOG.debug("User has changed configuration")
    _RECONFIGURED_DEVICE.mac_address = mac_address
    _RECONFIGURED_DEVICE.address = address
    _RECONFIGURED_DEVICE.global_volume = global_volume

    config.devices.add_or_update(_RECONFIGURED_DEVICE)  # triggers ATV instance update

    await asyncio.sleep(1)
    new_identifier = (
        _RECONFIGURED_DEVICE.identifier
        if _RECONFIGURED_DEVICE.mac_address is None
        else _RECONFIGURED_DEVICE.mac_address
    )
    _LOG.info("Setup successfully completed for %s with new identifier : %s", _RECONFIGURED_DEVICE.name, new_identifier)

    return SetupComplete()


def _discovered_atv_from_identifier(identifier: str) -> pyatv.interface.BaseConfig | None:
    """
    Get discovery information from identifier.

    :param identifier: ATV identifier
    :return: Device configuration if found, None otherwise
    """
    if _DISCOVERED_ATVS is None:
        return None
    for atv in _DISCOVERED_ATVS:
        if atv.identifier == identifier:
            return atv
    return None


def __user_input_discovery():
    return RequestUserInput(
        _a("Setup mode"),
        [
            {
                "id": "info",
                "label": _a("Discover or connect to Apple TV device"),
                "field": {
                    "label": {
                        "value": _am(
                            # Translators: Markdown can be used for formatting
                            __("Leave blank to use auto-discovery and click _Next_."),
                            "\n\n",
                            __("The device must be on the same network as the remote."),
                        )
                    }
                },
            },
            {
                "id": "address",
                "label": _a("IP address (same network only)"),
                "field": {"text": {"value": ""}},
            },
        ],
    )


def __global_volume(enabled: bool):
    return {
        "id": "global_volume",
        "label": _a("Change volume on all connected devices"),
        "field": {"checkbox": {"value": enabled}},
    }
