"""
This module implements the Apple TV communication of the Remote Two integration driver.

:copyright: (c) 2023 by Unfolded Circle ApS.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import asyncio
import base64
import logging
import random
from enum import IntEnum

import pyatv
import pyatv.const
from pyee import AsyncIOEventEmitter

LOG = logging.getLogger(__name__)
LOG.setLevel(logging.DEBUG)

BACKOFF_MAX = 30
BACKOFF_SEC = 2
ARTWORK_WIDTH = 400
ARTWORK_HEIGHT = 400


class EVENTS(IntEnum):
    CONNECTING = 0
    CONNECTED = 1
    DISCONNECTED = 2
    PAIRED = 3
    ERROR = 4
    UPDATE = 5


class AppleTv:
    def __init__(self, loop):
        self._loop = loop
        self.events = AsyncIOEventEmitter(self._loop)
        self.is_on = False
        self._atv = None
        self.name = ""
        self.identifier = None
        self._credentials = []
        self._connect_task = None
        self._connection_attempts = 0
        self.pairing_atv = None
        self._pairing_process = None
        self._polling = None
        self._poll_interval = 2
        self._state = None
        self._app_list = {}

    def backoff(self):
        if self._connection_attempts * BACKOFF_SEC >= BACKOFF_MAX:
            return BACKOFF_MAX

        return self._connection_attempts * BACKOFF_SEC

    def playstatus_update(self, _updater, playstatus):
        """ "Callback for when a push update happens"""
        LOG.debug("Push update")
        LOG.debug(str(playstatus))
        _ = asyncio.ensure_future(self._process_update(playstatus))

    def playstatus_error(self, _updater, exception):
        """ "Callback for when a push update error happens"""
        LOG.debug(str(exception))

    def connection_lost(self, _exception):
        """ "Callback for when the device is disconnected"""
        LOG.exception("Lost connection")
        self.events.emit(EVENTS.DISCONNECTED, self.identifier)
        _ = asyncio.ensure_future(self._stop_polling())
        if self._atv:
            self._atv.close()
            self._atv = None
        self._start_connect_loop()

    def connection_closed(self):
        """ "Callback for when the user disconnects the device"""
        LOG.debug("Connection closed!")

    def volume_update(self, _old_level, new_level):
        """ "Callback for volume level change"""
        LOG.debug("Volume level: %d", new_level)
        update = {"volume": new_level}
        self.events.emit(EVENTS.UPDATE, update)

    def outputdevices_update(self, old_devices, new_devices):
        """ "Callback for output device change, like airplay speaker"""
        # print('Output devices changed from {0:s} to {1:s}'.format(old_devices, new_devices))
        # TODO: implement me

    def focusstate_update(self, old_state, new_state):
        """ "Callback when the focus is changed for text input"""
        # print('Focus state changed from {0:s} to {1:s}'.format(old_state, new_state))
        # TODO: implement me

    async def find_atv(self, identifier):
        """Find a specific Apple TV on the network by identifier"""
        atvs = await pyatv.scan(self._loop, identifier=identifier)
        if not atvs:
            return None

        return atvs[0]

    async def init(self, identifier, credentials=[], name=""):
        """Initialises the object and setting identifier, credentials and name"""
        self.identifier = identifier
        self._credentials = credentials
        self.name = name

    def add_credentials(self, credentials):
        self._credentials.append(credentials)

    def get_credentials(self):
        return self._credentials

    async def start_pairing(self, protocol, name):
        """ ""Starts the pairing process with the Apple TV"""
        LOG.debug("Pairing started")
        self._pairing_process = await pyatv.pair(self.pairing_atv, protocol, self._loop, name=name)
        await self._pairing_process.begin()

        if self._pairing_process.device_provides_pin:
            LOG.debug("Device provides PIN")
            return 0

        LOG.debug("We provide PIN")
        pin = random.randint(1000, 9999)
        self._pairing_process.pin(pin)
        return pin

    async def enter_pin(self, pin):
        LOG.debug("Entering PIN")
        self._pairing_process.pin(pin)

    async def finish_pairing(self):
        LOG.debug("Pairing finished")
        res = None

        await self._pairing_process.finish()

        if self._pairing_process.has_paired:
            LOG.debug("Paired with device!")
            res = self._pairing_process.service
        else:
            LOG.warning("Did not pair with device")
            self.events.emit(EVENTS.ERROR, self.identifier, "Could not pair with device")

        await self._pairing_process.close()
        self._pairing_process = None
        return res

    async def connect(self):
        if self.is_on is True:
            return
        self.is_on = True
        self.events.emit(EVENTS.CONNECTING, self.identifier)
        self._start_connect_loop()

    def _start_connect_loop(self):
        if not self._connect_task and self._atv is None and self.is_on:
            self._connect_task = asyncio.create_task(self._connect_loop())
        else:
            LOG.debug("Not starting connect loop (Atv: %s, isOn: %s)", self._atv is None, self.is_on)

    async def _connect_loop(self):
        LOG.debug("Starting connect loop")
        while self.is_on and self._atv is None:
            await self._connect_once()
            if self._atv is not None:
                break
            self._connection_attempts += 1
            backoff = self.backoff()
            LOG.debug("Trying to connect again in %ds", backoff)
            await asyncio.sleep(backoff)

        LOG.debug("Connect loop ended")
        self._connect_task = None

        # Add callback listener for various push updates
        self._atv.push_updater.listener = self
        self._atv.push_updater.start()
        self._atv.listener = self
        self._atv.audio.listener = self
        self._atv.keyboard.listener = self

        # Reset the backoff counter
        self._connection_attempts = 0

        await self._start_polling()

        if self._atv.features.in_state(pyatv.const.FeatureState.Available, pyatv.const.FeatureName.AppList):
            self._loop.create_task(self._update_app_list())

        self.events.emit(EVENTS.CONNECTED, self.identifier)
        LOG.debug("Connected")

    async def _connect_once(self):
        try:
            if conf := await self.find_atv(self.identifier):
                await self._connect(conf)
        except pyatv.exceptions.AuthenticationError:
            LOG.warning("Could not connect: auth error")
            await self.disconnect()
            return
        except asyncio.CancelledError:
            pass
        except Exception:
            LOG.warning("Could not connect")
            self._atv = None

    async def _connect(self, conf):
        # We try to connect with all the protocols.
        # If something is not ready yet, we try again afterward
        missing_protocols = []

        for credential in self._credentials:
            protocol = None
            if credential["protocol"] == "companion":
                protocol = pyatv.const.Protocol.Companion
            elif credential["protocol"] == "airplay":
                protocol = pyatv.const.Protocol.AirPlay

            if conf.get_service(protocol) is not None:
                LOG.debug("Setting credentials for protocol: %s", protocol)
                conf.set_credentials(protocol, credential["credentials"])
            else:
                missing_protocols.append(protocol.name)

        if missing_protocols:
            missing_protocols_str = ", ".join(missing_protocols)
            LOG.warning("Protocols %s not yet found for %s, trying later", missing_protocols_str, conf.name)

        LOG.debug("Connecting to device %s", conf.name)
        self._atv = await pyatv.connect(conf, self._loop)

    async def disconnect(self):
        LOG.debug("Disconnecting from device")
        self.is_on = False
        await self._stop_polling()

        try:
            if self._atv:
                self._atv.close()
                self._atv = None
            if self._connect_task:
                self._connect_task.cancel()
                self._connect_task = None
            self.events.emit(EVENTS.DISCONNECTED, self.identifier)
        except Exception:
            LOG.exception("An error occured while disconnecting")

    async def _start_polling(self):
        if self._atv is None:
            LOG.warning("Polling not started, AppleTv object is None")
            self.events.emit(EVENTS.ERROR, "Polling not started, AppleTv object is None")
            return

        await asyncio.sleep(2)
        self._polling = self._loop.create_task(self._poll_worker())
        LOG.debug("Polling started")

    async def _stop_polling(self):
        if self._polling:
            self._polling.cancel()
            self._polling = None
            LOG.debug("Polling stopped")
        else:
            LOG.debug("Polling was already stopped")

    async def _process_update(self, data):
        LOG.debug("Push update")

        update = {}

        # We only update device state (playing, paused, etc) if the power state is On
        # otherwise we'll set the state to Off in the polling method
        self._state = data.device_state
        update["state"] = data.device_state

        if update["state"] == pyatv.const.DeviceState.Playing:
            self._poll_interval = 2

        update["position"] = data.position

        # image operations are expensive, so we only do it when the hash changed
        if self._state == pyatv.const.DeviceState.Playing:
            try:
                artwork = await self._atv.metadata.artwork(width=ARTWORK_WIDTH, height=ARTWORK_HEIGHT)
                artwork_encoded = "data:image/png;base64," + base64.b64encode(artwork.bytes).decode("utf-8")
                update["artwork"] = artwork_encoded
            except:
                LOG.warning("Error while updating the artwork")

        update["total_time"] = data.total_time
        update["title"] = data.title

        if data.artist is not None:
            update["artist"] = data.artist
        else:
            update["artist"] = ""

        if data.album is not None:
            update["album"] = data.album
        else:
            update["album"] = ""

        if data.media_type is not None:
            update["media_type"] = data.media_type

        # TODO: data.genre
        # TODO: data.repeat: All, Off, Track
        # TODO: data.shuffle

        self.events.emit(EVENTS.UPDATE, update)

    async def _update_app_list(self):
        LOG.debug("Updating app list")
        update = {}

        try:
            update["sourceList"] = []
            app_list = await self._atv.apps.app_list()
            for app in app_list:
                self._app_list[app.name] = app.identifier
                update["sourceList"].append(app.name)
        except pyatv.exceptions.NotSupportedError:
            LOG.warning("App list is not supported")
        except pyatv.exceptions.ProtocolError:
            LOG.warning("App list: protocol error")

        self.events.emit(EVENTS.UPDATE, update)

    async def _poll_worker(self):
        while self._atv is not None:
            update = {}

            if self._is_feature_available(pyatv.const.FeatureName.PowerState) and (
                self._state
                not in (
                    pyatv.const.DeviceState.Playing,
                    pyatv.const.DeviceState.Paused,
                    pyatv.const.DeviceState.Idle,
                    pyatv.const.DeviceState.Stopped,
                    pyatv.const.DeviceState.Seeking,
                    pyatv.const.DeviceState.Loading,
                )
            ):
                if self._atv.power.power_state == pyatv.const.PowerState.Off:
                    update["state"] = self._atv.power.power_state
                    self._poll_interval = 10
                elif self._atv.power.power_state == pyatv.const.PowerState.On:
                    update["state"] = self._atv.power.power_state
                    self._poll_interval = 2

            if self._is_feature_available(pyatv.const.FeatureName.App):
                update["source"] = self._atv.metadata.app.name

            self.events.emit(EVENTS.UPDATE, update)
            await asyncio.sleep(self._poll_interval)

    def _is_feature_available(self, feature: pyatv.const.FeatureName) -> bool:
        if self._atv:
            return self._atv.features.in_state(pyatv.const.FeatureState.Available, feature)
        return False

    async def _command_wrapper(self, fn):
        try:
            await fn()
            return True
        except:
            return False

    async def turn_on(self):
        if self._atv is None:
            return False

        return await self._command_wrapper(self._atv.power.turn_on)

    async def turn_off(self):
        if self._atv is None:
            return False

        return await self._command_wrapper(self._atv.power.turn_off)

    async def play_pause(self):
        if self._atv is None:
            return False

        return await self._command_wrapper(self._atv.remote_control.play_pause)

    async def next(self):
        if self._atv is None:
            return False

        return await self._command_wrapper(self._atv.remote_control.next)

    async def previous(self):
        if self._atv is None:
            return False

        return await self._command_wrapper(self._atv.remote_control.previous)

    async def volume_up(self):
        if self._atv is None:
            return False

        return await self._command_wrapper(self._atv.audio.volume_up)

    async def volume_down(self):
        if self._atv is None:
            return False

        return await self._command_wrapper(self._atv.audio.volume_down)

    async def cursor_up(self):
        if self._atv is None:
            return False

        return await self._command_wrapper(self._atv.remote_control.up)

    async def cursor_down(self):
        if self._atv is None:
            return False

        return await self._command_wrapper(self._atv.remote_control.down)

    async def cursor_left(self):
        if self._atv is None:
            return False

        return await self._command_wrapper(self._atv.remote_control.left)

    async def cursor_right(self):
        if self._atv is None:
            return False

        return await self._command_wrapper(self._atv.remote_control.right)

    async def cursor_enter(self):
        if self._atv is None:
            return False

        return await self._command_wrapper(self._atv.remote_control.select)

    async def home(self):
        if self._atv is None:
            return False

        return await self._command_wrapper(self._atv.remote_control.home)

    async def menu(self):
        if self._atv is None:
            return False

        return await self._command_wrapper(self._atv.remote_control.menu)

    async def channel_up(self):
        if self._atv is None:
            return False

        return await self._command_wrapper(self._atv.remote_control.channel_up)

    async def channel_down(self):
        if self._atv is None:
            return False

        return await self._command_wrapper(self._atv.remote_control.channel_down)

    async def launch_app(self, app_name):
        if self._atv is None:
            return False

        await self._atv.apps.launch_app(self._app_list[app_name])
        return True
