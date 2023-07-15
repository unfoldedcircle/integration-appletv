import asyncio
import base64
import logging
import random

from enum import IntEnum

from pyee import AsyncIOEventEmitter

import pyatv
import pyatv.const

from pyatv.interface import PushListener

LOG = logging.getLogger(__name__)
LOG.setLevel(logging.DEBUG)

class EVENTS(IntEnum):
    CONNECTED = 0,
    DISCONNECTED = 1,
    PAIRED = 2,
    POLLING_STARTED = 3,
    POLLING_STOPPED = 4,
    ERROR = 5,
    UPDATE = 6,

class AppleTv(object):
    def __init__(self, loop):
        self._loop = loop
        self._atvObjDiscovered = None
        self._atvObj = None
        self.events = AsyncIOEventEmitter(self._loop)
        self.identifier = None
        self.name = ""
        self._credentials = []
        self._pairingProcess = None
        self._connected = False
        self._polling = False

        @self.events.on(EVENTS.CONNECTED)
        async def _onConnected():
            await self._startPolling()
                
        @self.events.on(EVENTS.DISCONNECTED)
        async def _onDisconnected():
            await self._stopPolling()


    async def init(self, identifier, credentials = []):
        atvs = await pyatv.scan(self._loop, identifier=identifier)
        if not atvs:
            return False
        else:
            self._atvObjDiscovered = atvs[0]
            self._atvObj = atvs[0]
            self.identifier = identifier
            self._credentials = credentials
            self.name = self._atvObj.name
            return True


    def addCredentials(self, credentials):
        self._credentials.append(credentials)


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
            self.events.emit(EVENTS.ERROR, 'Could not pair with device')

        await self._pairingProcess.close()
        self._pairingProcess = None

        return res


    async def connect(self):
        LOG.debug('Connecting...')

        if self._connected == True:
            return
        
        if self.identifier == "":
            LOG.warning('No identifier found, aborting connect')
            self.events.emit(EVENTS.ERROR, 'No identifier found, aborting connect')
            return

        if not self._credentials:
            LOG.warning('No credentials were found, aborting connect')
            self.events.emit(EVENTS.ERROR, 'No credentials were found, aborting connect')
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
                self.events.emit(EVENTS.ERROR, 'Failed to set credentials')
            else:
                LOG.debug('Credentials set for %s', protocol)

        try:
            self._atvObj = await pyatv.connect(self._atvObj, self._loop)
        except:
            LOG.error('Error connecting')
            self.events.emit(EVENTS.ERROR, 'Failed to connect')
            return

        self._connected = True
        self.events.emit(EVENTS.CONNECTED)
        LOG.debug("Connected")


    async def disconnect(self):
        LOG.debug('Disconnect')
        if self._atvObj is not None:
            self._atvObj.close()
            self._atvObj = self._atvObjDiscovered
            self._connected = False
            self.events.emit(EVENTS.DISCONNECTED)


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
            self._polling.cancel()
            self._polling = None
            LOG.debug('Polling stopped')
            self.events.emit(EVENTS.POLLING_STOPPED)
        else:
            LOG.debug('Polling was already stopped')


    async def _pollWorker(self): 
        prevHash = None
        while True:
            update = {}      
            state = ""
            try:
                playing = await self._atvObj.metadata.playing()
            except:
                LOG.error('Error while getting metadata')
            
            if self._atvObj.power.power_state is pyatv.const.PowerState.On:
                state = playing.device_state
            elif self._atvObj.power.power_state is pyatv.const.PowerState.Off:
                state = self._atvObj.power.power_state

            update['state'] = state
            update['position'] = playing.position

            if playing.hash != prevHash:
                try:
                    artwork = await self._atvObj.metadata.artwork(width=480, height=None)
                    artwork_encoded = 'data:image/png;base64,' + base64.b64encode(artwork.bytes).decode('utf-8')
                    # update['artwork'] = artwork_encoded
                except:
                    LOG.error('Error while updating the artwork')

                update['total_time'] = playing.total_time
                update['title'] = playing.title
                update['artist'] = playing.artist
                update['album'] = playing.album
            
            prevHash = playing.hash
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
        try:
            await fn()
            return True
        except:
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