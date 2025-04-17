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

import config
import discover
import pyatv
import tv
from config import AtvDevice, AtvProtocol
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
    RECONFIGURE = 6


_setup_step = SetupSteps.INIT
_cfg_add_device: bool = False
_manual_address: bool = False
_discovered_atvs: list[pyatv.interface.BaseConfig] = None
_pairing_apple_tv: tv.AppleTv | None = None
_reconfigured_device: AtvDevice | None = None
# TODO #12 externalize language texts
# pylint: disable=line-too-long
_user_input_discovery = RequestUserInput(
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
                        "en": (
                            "Leave blank to use auto-discovery and click _Next_."
                            "The device must be on the same network as the remote."
                        ),
                        "de": (
                            "Leer lassen, um automatische Erkennung zu verwenden und auf _Weiter_ klicken."
                            "Das Gerät muss sich im gleichen Netzwerk wie die Fernbedienung befinden."
                        ),
                        "fr": (
                            "Laissez le champ vide pour utiliser la découverte automatique et cliquez sur _Suivant_."
                            "L'appareil doit être sur le même réseau que la télécommande"
                        ),
                    }
                }
            },
        },
        {
            "field": {"text": {"value": ""}},
            "id": "address",
            "label": {
                "en": "IP address (same network only)",
                "de": "IP-Adresse (nur im gleichen Netzwerk)",
                "fr": "Adresse IP (seulement dans le même réseau)",
            },
        },
    ],
)


async def driver_setup_handler(msg: SetupDriver) -> SetupAction:  # pylint: disable=too-many-return-statements
    """
    Dispatch driver setup requests to corresponding handlers.

    Either start the setup process or handle the selected Apple TV device.

    :param msg: the setup driver request object, either DriverSetupRequest or UserDataResponse
    :return: the setup action on how to continue
    """
    global _setup_step
    global _cfg_add_device
    global _pairing_apple_tv

    if isinstance(msg, DriverSetupRequest):
        _setup_step = SetupSteps.INIT
        _cfg_add_device = False
        return await _handle_driver_setup(msg)

    if isinstance(msg, UserDataResponse):
        _LOG.debug("%s", msg)
        if _setup_step == SetupSteps.CONFIGURATION_MODE and "action" in msg.input_values:
            return await _handle_configuration_mode(msg)
        if _setup_step == SetupSteps.DISCOVER and "address" in msg.input_values:
            return await _handle_discovery(msg)
        if _setup_step == SetupSteps.DEVICE_CHOICE and "choice" in msg.input_values:
            return await _handle_device_choice(msg)
        if _setup_step == SetupSteps.PAIRING_AIRPLAY and "pin_airplay" in msg.input_values:
            return await _handle_user_data_airplay_pin(msg)
        if _setup_step == SetupSteps.PAIRING_COMPANION and "pin_companion" in msg.input_values:
            return await _handle_user_data_companion_pin(msg)
        if _setup_step == SetupSteps.RECONFIGURE:
            return await _handle_device_reconfigure(msg)
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
    global _setup_step

    reconfigure = msg.reconfigure
    _LOG.debug("Starting driver setup, reconfigure=%s", reconfigure)

    if reconfigure:
        _setup_step = SetupSteps.CONFIGURATION_MODE

        # make sure configuration is up-to-date
        if config.devices.migration_required():
            await config.devices.migrate()

        # check after devices change and update configuration if necessary
        await config.devices.handle_devices_change()

        # get all configured devices for the user to choose from
        dropdown_devices = []
        for device in config.devices.all():
            dropdown_devices.append({"id": device.identifier, "label": {"en": f"{device.name} ({device.identifier})"}})

        # TODO #12 externalize language texts
        # build user actions, based on available devices
        dropdown_actions = [
            {
                "id": "add",
                "label": {
                    "en": "Add a new device",
                    "de": "Neues Gerät hinzufügen",
                    "fr": "Ajouter un nouvel appareil",
                },
            },
        ]

        # add remove, reconfigure & reset actions if there's at least one configured device
        if dropdown_devices:
            dropdown_actions.append(
                {
                    "id": "remove",
                    "label": {
                        "en": "Delete selected device",
                        "de": "Selektiertes Gerät löschen",
                        "fr": "Supprimer l'appareil sélectionné",
                    },
                },
            )
            dropdown_actions.append(
                {
                    "id": "configure",
                    "label": {
                        "en": "Configure selected device",
                        "de": "Selektiertes Gerät konfigurieren",
                        "fr": "Configurer l'appareil sélectionné",
                    },
                },
            )
            dropdown_actions.append(
                {
                    "id": "reset",
                    "label": {
                        "en": "Reset configuration and reconfigure",
                        "de": "Konfiguration zurücksetzen und neu konfigurieren",
                        "fr": "Réinitialiser la configuration et reconfigurer",
                    },
                },
            )
        else:
            # dummy entry if no devices are available
            dropdown_devices.append({"id": "", "label": {"en": "---"}})

        return RequestUserInput(
            {"en": "Configuration mode", "de": "Konfigurations-Modus"},
            [
                {
                    "field": {"dropdown": {"value": dropdown_devices[0]["id"], "items": dropdown_devices}},
                    "id": "choice",
                    "label": {
                        "en": "Configured devices",
                        "de": "Konfigurierte Geräte",
                        "fr": "Appareils configurés",
                    },
                },
                {
                    "field": {"dropdown": {"value": dropdown_actions[0]["id"], "items": dropdown_actions}},
                    "id": "action",
                    "label": {
                        "en": "Action",
                        "de": "Aktion",
                        "fr": "Appareils configurés",
                    },
                },
            ],
        )

    # Initial setup, make sure we have a clean configuration
    config.devices.clear()  # triggers device instance removal
    _setup_step = SetupSteps.DISCOVER
    return _user_input_discovery


async def _handle_configuration_mode(msg: UserDataResponse) -> RequestUserInput | SetupComplete | SetupError:
    """
    Process user data response from the configuration mode screen.

    User input data:

    - ``choice`` contains identifier of selected device
    - ``action`` contains the selected action identifier

    :param msg: user input data from the configuration mode screen.
    :return: the setup action on how to continue
    """
    global _setup_step
    global _cfg_add_device
    global _reconfigured_device

    action = msg.input_values["action"]

    # workaround for web-configurator not picking up first response
    await asyncio.sleep(1)

    match action:
        case "add":
            _cfg_add_device = True
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

            # Found mac address/idenfitier of selected AppleTV upon detection
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
                    "label": {
                        "en": "Manual mac address (below)",
                        "de": "Manuelle MAC-Adresse (unten)",
                        "fr": "Adresse Mac manuelle (ci-dessous)",
                    },
                }
            )

            _setup_step = SetupSteps.RECONFIGURE
            _reconfigured_device = selected_device
            mac_address = selected_device.mac_address if selected_device.mac_address else ""
            address = selected_device.address if selected_device.address else ""

            return RequestUserInput(
                {
                    "en": "Configure your Apple TV (configured mac address " + mac_address + ")",
                    "de": "Konfiguriere dein Apple TV (konfigurierte MAC-Adresse " + mac_address + ")",
                    "fr": "Configurez votre Apple TV (addresse mac configurée " + mac_address + ")",
                },
                [
                    {
                        "field": {"dropdown": {"value": found_selected_device_id, "items": dropdown_items}},
                        "id": "mac_address",
                        "label": {
                            "en": "Mac address",
                            "de": "Mac-Adresse",
                            "fr": "Adresse Mac",
                        },
                    },
                    {
                        "field": {"text": {"value": mac_address}},
                        "id": "manual_mac_address",
                        "label": {
                            "en": "Manual mac address",
                            "de": "Manuelle MAC-Adresse",
                            "fr": "Adresse Mac manuelle",
                        },
                    },
                    {
                        "field": {"text": {"value": address}},
                        "id": "address",
                        "label": {
                            "en": "IP address (optional)",
                            "de": "IP-Adresse (optional)",
                            "fr": "Adresse IP (optionnelle)",
                        },
                    },
                ],
            )

        case "reset":
            config.devices.clear()  # triggers device instance removal
        case _:
            _LOG.error("Invalid configuration action: %s", action)
            return SetupError(error_type=IntegrationSetupError.OTHER)

    _setup_step = SetupSteps.DISCOVER
    return _user_input_discovery


async def _handle_discovery(msg: UserDataResponse) -> RequestUserInput | SetupError:
    """
    Process user data response from the first setup process screen.

    If ``address`` field is set by the user: try connecting to device and retrieve device information.
    Otherwise, start Apple TV discovery and present the found devices to the user to choose from.

    :param msg: response data from the requested user data
    :return: the setup action on how to continue
    """
    global _pairing_apple_tv
    global _setup_step
    global _manual_address
    global _discovered_atvs

    # clear all configured devices and any previous pairing attempt
    if _pairing_apple_tv:
        await _pairing_apple_tv.disconnect()
        _pairing_apple_tv = None

    search_hosts: list[str] | None = None
    dropdown_items = []
    address = msg.input_values["address"]

    if address:
        _LOG.debug("Starting manual driver setup for: %s", address)
        _manual_address = True
        # Connect to specific device and retrieve name
        search_hosts = [address]
    else:
        _LOG.debug("Starting driver setup with Apple TV discovery")
        _manual_address = False

    _discovered_atvs = await discover.apple_tvs(asyncio.get_event_loop(), hosts=search_hosts)

    for device in _discovered_atvs:
        _LOG.info(
            "Found: %s, %s (%s)",
            device.device_info,
            device.name,
            device.address,
        )
        # if we are adding a new device: make sure it's not already configured
        if _cfg_add_device and config.devices.contains(device.identifier):
            _LOG.info("Skipping found device %s: already configured", device.identifier)
            continue

        label = f"{device.name} ({device.address})"
        dropdown_items.append({"id": device.identifier, "label": {"en": label}})

    if not dropdown_items:
        _LOG.warning("No Apple TVs found")
        return SetupError(error_type=IntegrationSetupError.NOT_FOUND)

    _setup_step = SetupSteps.DEVICE_CHOICE
    # TODO #12 externalize language texts
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


async def _handle_device_choice(msg: UserDataResponse) -> RequestUserInput | SetupError:
    """
    Process user data device choice response in a setup process.

    Driver setup callback to provide requested user data during the setup process.

    :param msg: response data from the requested user data
    :return: the setup action on how to continue.
    """
    global _pairing_apple_tv
    global _setup_step

    choice = msg.input_values["choice"]

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
    _pairing_apple_tv = tv.AppleTv(
        AtvDevice(
            identifier=choice,
            name=atv.name,
            credentials=[],
            address=atv.address if _manual_address else None,
            mac_address=choice,
        ),
        loop=asyncio.get_event_loop(),
        pairing_atv=atv,
    )

    _LOG.debug("Pairing process begin")
    # Hook up to signals
    # TODO error conditions in start_pairing?
    name = os.getenv("UC_CLIENT_NAME", socket.gethostname().split(".", 1)[0])
    res = await _pairing_apple_tv.start_pairing(pyatv.const.Protocol.AirPlay, f"{name} Airplay")
    if res is None:
        return SetupError()

    if res == 0:
        _LOG.debug("Device provides AirPlay-Code")
        _setup_step = SetupSteps.PAIRING_AIRPLAY
        # TODO #12 externalize language texts
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


async def _handle_user_data_airplay_pin(msg: UserDataResponse) -> RequestUserInput | SetupError:
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
    c = {"protocol": AtvProtocol.AIRPLAY, "credentials": res.credentials}
    _pairing_apple_tv.add_credentials(c)

    # Start new pairing process
    name = os.getenv("UC_CLIENT_NAME", socket.gethostname().split(".", 1)[0])
    res = await _pairing_apple_tv.start_pairing(pyatv.const.Protocol.Companion, f"{name} Companion")
    if res is None:
        return SetupError()

    if res == 0:
        _LOG.debug("Device provides PIN")
        _setup_step = SetupSteps.PAIRING_COMPANION
        # TODO #12 externalize language texts
        return RequestUserInput(
            {
                "en": "Please enter the shown PIN on your Apple TV",
                "de": "Bitte gib die angezeigte PIN auf deinem Apple TV ein",
                "fr": "Veuillez entrer le code PIN affiché sur votre Apple TV",
            },
            [
                {
                    "field": {"number": {"max": 9999, "min": 0, "value": 0000}},
                    "id": "pin_companion",
                    "label": {"en": "Apple TV PIN"},
                }
            ],
        )

    _LOG.debug("We provide companion PIN")
    return RequestUserConfirmation("Please enter the following PIN on your Apple TV: " + res)


async def _handle_user_data_companion_pin(msg: UserDataResponse) -> SetupComplete | SetupError:
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

    c = {"protocol": AtvProtocol.COMPANION, "credentials": res.credentials}
    _pairing_apple_tv.add_credentials(c)

    device = AtvDevice(
        identifier=_pairing_apple_tv.identifier,
        name=_pairing_apple_tv.name,
        credentials=_pairing_apple_tv.get_credentials(),
        address=_pairing_apple_tv.address,
        mac_address=_pairing_apple_tv.identifier,
    )
    config.devices.add_or_update(device)  # triggers ATV instance creation

    # ATV device connection will be triggered with subscribe_entities request

    _pairing_apple_tv = None
    await asyncio.sleep(1)

    _LOG.info("Setup successfully completed for %s", device.name)

    return SetupComplete()


async def _handle_device_reconfigure(msg: UserDataResponse) -> SetupComplete | SetupError:
    """
    Process reconfiguration of a registered Apple TV device.

    :param msg: response data from the requested user data
    :return: the setup action on how to continue: SetupComplete after updating configuration
    """
    # flake8: noqa:F824
    # pylint: disable=W0602
    global _reconfigured_device

    if _reconfigured_device is None:
        return SetupError()

    mac_address = msg.input_values["mac_address"]
    manual_mac_address = msg.input_values["manual_mac_address"]

    if mac_address == "" and manual_mac_address == "":
        _LOG.error("Mac address is mandatory, no changes applied")
        return SetupError()
    address = msg.input_values["address"]
    if address == "":
        address = None
    if mac_address == "":
        mac_address = manual_mac_address

    _LOG.debug("User has changed configuration")
    _reconfigured_device.mac_address = mac_address
    _reconfigured_device.address = address

    config.devices.add_or_update(_reconfigured_device)  # triggers ATV instance update

    await asyncio.sleep(1)
    new_identifier = (
        _reconfigured_device.identifier
        if _reconfigured_device.mac_address is None
        else _reconfigured_device.mac_address
    )
    _LOG.info("Setup successfully completed for %s with new identifier : %s", _reconfigured_device.name, new_identifier)

    return SetupComplete()


def _discovered_atv_from_identifier(identifier: str) -> pyatv.interface.BaseConfig | None:
    """
    Get discovery information from identifier.

    :param identifier: ATV identifier
    :return: Device configuration if found, None otherwise
    """
    for atv in _discovered_atvs:
        if atv.identifier == identifier:
            return atv
    return None
