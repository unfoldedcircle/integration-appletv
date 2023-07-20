import asyncio
import base64
import logging
import random

from enum import IntEnum

from pyee import AsyncIOEventEmitter

import pyatv
import pyatv.const

from pyatv.interface import PushListener
from pyatv.interface import DeviceListener
from pyatv.interface import AudioListener
from pyatv.interface import KeyboardListener

LOG = logging.getLogger(__name__)
LOG.setLevel(logging.DEBUG)

class EVENTS(IntEnum):
    CONNECTING = 0
    CONNECTED = 1
    DISCONNECTED = 2
    PAIRED = 3
    POLLING_STARTED = 4
    POLLING_STOPPED = 5
    ERROR = 6
    UPDATE = 7
    VOLUME_CHANGED = 8

class AppleTv(object):
    def __init__(self, loop):
        self._loop = loop
        self._atvObjDiscovered = None
        self._atvObj = None
        self.events = AsyncIOEventEmitter(self._loop)
        self.identifier = None
        self.name = ""
        self._credentialsSetup = False
        self._credentials = []
        self._pairingProcess = None
        self._connected = False
        self._credentialsBackOff = 0
        self._polling = False
        self._listener = None
        self._prevUpdateHash = None
        self._appList = {}

        @self.events.on(EVENTS.CONNECTED)
        async def _onConnected(identifier):
            await self._startPolling()
                
        @self.events.on(EVENTS.DISCONNECTED)
        async def _onDisconnected(identifier):
            await self._stopPolling()

    class PushListener(PushListener, DeviceListener, AudioListener, KeyboardListener):
        def __init__(self, loop, identifier):
            self._loop = loop
            self._identifier = identifier
            self.events = AsyncIOEventEmitter(self._loop)
            LOG.debug("Push listener initialised")

        def playstatus_update(self, updater, playstatus):
            LOG.debug("Push update")
            LOG.debug(str(playstatus))
            self.events.emit(EVENTS.UPDATE, playstatus)
            
        def playstatus_error(self, updater, exception):
            LOG.debug(str(exception))
            self.events.emit(EVENTS.ERROR, self._identifier)

        def connection_lost(self, exception):
            LOG.warning("Lost connection:", str(exception))
            LOG.debug("Reconnecting")
            self.events.emit(EVENTS.DISCONNECTED, self._identifier)

        def connection_closed(self):
            LOG.debug("Connection closed!")

        def volume_update(self, old_level, new_level):
            self.events.emit(EVENTS.VOLUME_CHANGED, new_level)

        def outputdevices_update(self, old_devices, new_devices):
            # print('Output devices changed from {0:s} to {1:s}'.format(old_devices, new_devices))
            LOG.debug("Output changed, TODO")
            # TODO: implement me

        def focusstate_update(self, old_state, new_state):
            # print('Focus state changed from {0:s} to {1:s}'.format(old_state, new_state))
            LOG.debug("Focus state changed, TODO")
            # TODO: implement me


    async def init(self, identifier, credentials = []):
        self.identifier = identifier
        self._credentials = credentials
        self._credentialsSetup = True;

        atvs = await pyatv.scan(self._loop, identifier=identifier)
        if not atvs:
            return False
        else:
            self._atvObjDiscovered = atvs[0]
            self._atvObj = atvs[0]
            self.name = self._atvObj.name
            return True


    def addCredentials(self, credentials):
        self._credentials.append(credentials)
        self._credentialsSetup = True;


    def getCredentials(self):
        return self._credentials


    def getConnected(self):
        return self._connected
    

    def getPolling(self):
        return self._polling


    async def startPairing(self, protocol, name):
        LOG.debug('Pairing started')
        self._pairingProcess = await pyatv.pair(self._atvObj, protocol, self._loop, name=name)
        await self._pairingProcess.begin()

        if self._pairingProcess.device_provides_pin:
            LOG.debug('Device provides PIN')
            return 0
        else:
            LOG.debug('We provide PIN')
            pin = random.randint(1000,9999)
            self._pairingProcess.pin(pin)
            return pin


    async def enterPin(self, pin):
        LOG.debug('Entering PIN')
        self._pairingProcess.pin(pin)


    async def finishPairing(self):
        LOG.debug('Pairing finished')
        res = None

        await self._pairingProcess.finish()

        if self._pairingProcess.has_paired:
            LOG.debug('Paired with device!')
            res = self._pairingProcess.service
        else:
            LOG.warning('Did not pair with device')
            self.events.emit(EVENTS.ERROR, self.identifier, 'Could not pair with device')

        await self._pairingProcess.close()
        self._pairingProcess = None

        return res


    async def connect(self):
        LOG.debug('Connecting...')
        self.events.emit(EVENTS.CONNECTING, self.identifier)

        if self._connected == True:
            return
        
        if self._atvObj is None:
            LOG.debug('No Apple TV object was created, trying to create one now')
            await asyncio.sleep(2)
            await self.init(self.identifier, self._credentials)
            await asyncio.sleep(4)
            await self.connect()
            return

        if self.identifier == "":
            LOG.warning('No identifier found, aborting connect')
            self.events.emit(EVENTS.ERROR, self.identifier, 'No identifier found, aborting connect')
            return

        if self._credentialsSetup is False:
            LOG.warning('Credentials not setup yet, retrying nr %d', self._credentialsBackOff)
            if self._credentialsBackOff == 15:
                self._credentialsBackOff = 0

            self._credentialsBackOff += 1
            await asyncio.sleep(2 * self._credentialsBackOff)
            await self.connect()
            return
        
        if not self._credentials:
            LOG.warning('No credentials found, retyring connect nr %d', self._credentialsBackOff)
            if self._credentialsBackOff == 15:
                self._credentialsBackOff = 0

            self._credentialsBackOff += 1
            await asyncio.sleep(2 * self._credentialsBackOff)
            await self.connect()
            return

        for credential in self._credentials:
            protocol = None
            if credential['protocol'] == 'companion':
                protocol = pyatv.const.Protocol.Companion
            elif credential['protocol'] == 'airplay':
                protocol = pyatv.const.Protocol.AirPlay

            res = self._atvObj.set_credentials(protocol, credential['credentials'])
            if res == False:
                LOG.error('Failed to set credentials')
                self.events.emit(EVENTS.ERROR, self.identifier, 'Failed to set credentials')
            else:
                LOG.debug('Credentials set for %s', protocol)

        connTry = 0

        while connTry != 5:
            try:
                LOG.debug('Trying to connect to the Apple TV')
                self._atvObj = await pyatv.connect(self._atvObj, self._loop)
                print(self._atvObj)
                connTry = 5
            except Exception:
                if connTry == 5:
                    LOG.error('Error connecting')
                    self.events.emit(EVENTS.ERROR, self.identifier, 'Failed to connect')
                    return
                LOG.debug('Trying to connect again ... %d', connTry)
                await asyncio.sleep(2 * connTry)
                connTry += 1

        self._listener = self.PushListener(self._loop, self.identifier)

        @self._listener.events.on(EVENTS.UPDATE)
        async def _onUpdateEvent(data):
            await self._processUpdate(data)

        @self._listener.events.on(EVENTS.VOLUME_CHANGED)
        async def _onVolumeChangedEvent(volume):
            self.events.emit(EVENTS.VOLUME_CHANGED, volume)
        
        @self._listener.events.on(EVENTS.ERROR)
        async def _onErrorEvent(data):
            LOG.error("An error happened while getting a push update.")

        @self._listener.events.on(EVENTS.DISCONNECTED)
        async def _onDisconnected(identifier):
            await self.disconnect(True)
            LOG.warning('Apple TV disconnected for some reason: %s. Reconnecting...', identifier)
            await asyncio.sleep(2)
            await self.connect()

        self._atvObj.push_updater.listener = self._listener
        self._atvObj.push_updater.start()
        self._atvObj.listener = self._listener
        self._atvObj.audio.listener = self._listener
        self._atvObj.keyboard.listener = self._listener

        self._connected = True
        self._credentialsBackOff = 0
        self.events.emit(EVENTS.CONNECTED, self.identifier)
        LOG.debug("Connected")


    async def disconnect(self, skipClose = False):
        LOG.debug('Disconnect')
        if self._atvObj is not None:
            if skipClose is False:
                self._atvObj.close()
            self._atvObj = self._atvObjDiscovered
            self._listener.events.remove_all_listeners()
            self._listener = None
            self._connected = False
            self._credentialsBackOff = 0
            self.events.emit(EVENTS.DISCONNECTED, self.identifier)


    async def _startPolling(self):
        if self._atvObj is None:
            LOG.warning('Polling not started, AppleTv object is None')
            self.events.emit(EVENTS.ERROR, 'Polling not started, AppleTv object is None')
            return
        
        self._polling = self._loop.create_task(self._pollWorker())
        self.events.emit(EVENTS.POLLING_STARTED)
        LOG.debug('Polling started')


    async def _stopPolling(self):
        if self._polling is not None:
            self._polling = None
            LOG.debug('Polling stopped')
            self.events.emit(EVENTS.POLLING_STOPPED)
        else:
            LOG.debug('Polling was already stopped')


    async def _processUpdate(self, data):
        update = {}

        if self._atvObj.power.power_state is pyatv.const.PowerState.On:
            update['state'] = data.device_state

        update['position'] = data.position

        # image operations are expensive, so we only do it when the hash changed
        # if data.hash != self._prevUpdateHash:
        try:
            artwork = await self._atvObj.metadata.artwork(width=400, height=None)
            artwork_encoded = 'data:image/png;base64,' + base64.b64encode(artwork.bytes).decode('utf-8')
            update['artwork'] = artwork_encoded
        except Exception:
            LOG.warning('Error while updating the artwork')

        update['total_time'] = data.total_time
        update['title'] = data.title

        if data.artist is not None:
            update['artist'] = data.artist
        else:
            update['artist'] = ""
        
        if data.album is not None:
            update['album'] = data.album
        else:
            update['album'] = ""

        if data.media_type is not None:
            update['media_type'] = data.media_type

        # TODO: data.genre
        # TODO: data.repeat: All, Off, Track
        # TODO: data.shuffle

        self._prevUpdateHash = data.hash
        self.events.emit(EVENTS.UPDATE, update)


    async def _pollWorker(self): 
        while True and self._connected is True:
            update = {}
            
            if self._atvObj.power.power_state is pyatv.const.PowerState.Off:
                update['state'] = self._atvObj.power.power_state
                update['artist'] = ""
                update['album'] = ""
                update['artwork'] = ""
                update['media_type'] = ""
            
            if self._atvObj.power.power_state is not pyatv.const.PowerState.Off:
                data = await self._atvObj.metadata.playing()
                update['state'] = data.device_state                
                update['sourceList'] = []

                try:
                    appList = await self._atvObj.apps.app_list()
                    for app in appList:
                        self._appList[app.name] = app.identifier
                        update['sourceList'].append(app.name)
                except Exception:
                    LOG.debug('Error while getting app list')

                try:
                    update['source'] = self._atvObj.metadata.app.name
                except Exception:
                    LOG.debug('Error while getting current app')
                    pass

            self.events.emit(EVENTS.UPDATE, update)
            await asyncio.sleep(2)


    async def _retry(fn, retries=5):
        i = 0
        while True:
            try:
                return await fn()
            except:
                if i == retries:
                    LOG.debug('Retry limit reached for %s', fn)
                    raise
                await asyncio.sleep(2)
                i += 1
                

    async def _commandWrapper(self, fn):
        if self._connected is False:
            return False
        
        try:
            await fn()
            return True
        except Exception:
            return False
        

    async def turnOn(self):
        return await self._commandWrapper(self._atvObj.power.turn_on)
    
    async def turnOff(self):
        return await self._commandWrapper(self._atvObj.power.turn_off)
    
    async def playPause(self):
        return await self._commandWrapper(self._atvObj.remote_control.play_pause)
    
    async def next(self):
        return await self._commandWrapper(self._atvObj.remote_control.next)
    
    async def previous(self):
        return await self._commandWrapper(self._atvObj.remote_control.previous)
    
    async def volumeUp(self):
        return await self._commandWrapper(self._atvObj.audio.volume_up)
    
    async def volumeDown(self):
        return await self._commandWrapper(self._atvObj.audio.volume_down)
    
    async def cursorUp(self):
        return await self._commandWrapper(self._atvObj.remote_control.up)
    
    async def cursorDown(self):
        return await self._commandWrapper(self._atvObj.remote_control.down)
    
    async def cursorLeft(self):
        return await self._commandWrapper(self._atvObj.remote_control.left)
    
    async def cursorRight(self):
        return await self._commandWrapper(self._atvObj.remote_control.right)
    
    async def cursorEnter(self):
        return await self._commandWrapper(self._atvObj.remote_control.select)
    
    async def home(self):
        return await self._commandWrapper(self._atvObj.remote_control.home)
    
    async def menu(self):
        return await self._commandWrapper(self._atvObj.remote_control.menu)
    
    async def channelUp(self):
        return await self._commandWrapper(self._atvObj.remote_control.channel_up)
    
    async def channelDown(self):
        return await self._commandWrapper(self._atvObj.remote_control.channel_down)
    
    async def launchApp(self, appName):
        await self._atvObj.apps.launch_app(self._appList[appName])
        return True