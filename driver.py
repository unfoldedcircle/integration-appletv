import asyncio
import base64
import logging
import random
import json

import ucapi.api as uc
import ucapi.entities as entities

import pyatv
import pyatv.const

from pyatv.interface import PushListener

LOG = logging.getLogger(__name__)
LOOP = asyncio.get_event_loop()
LOG.setLevel(logging.DEBUG)

# Global variables
api = uc.IntegrationAPI(LOOP)
pairingAtv = None
pairingProcess = None
connectedAtv = None
isConnected = False
pollingTask = None

async def storeCredentials(tv, service):
    f = None
    data = {
        'identifier': tv.identifier,
        'protocol': service.protocol.name.lower(),
        'credentials': service.credentials
    }

    try:
        f= open('credentials.json', 'w+')
    except OSError:
        LOG.error('Cannot write the credentials file')
        return

    json.dump(data, f, ensure_ascii=False)

async def restoreCredentials():
    f = None

    try:
        f = open('credentials.json', 'r')
    except OSError:
        LOG.error('Cannot open the credentials file')
    
    if f is None:
        return None

    data = json.load(f)
    identifier = data['identifier']
    credentials = data['credentials']

    if data['protocol'] == 'companion':
        protocol = pyatv.const.Protocol.Companion
    elif data['protocol'] == 'airplay':
        protocol = pyatv.const.Protocol.AirPlay

    atvs = await pyatv.scan(LOOP, identifier=identifier)

    if not atvs:
        return None

    atv = atvs[0]
    atv.set_credentials(protocol, credentials)
    return atv

async def discoverAppleTVs():
    atvs = await pyatv.scan(LOOP)
    res = []

    for tv in atvs:
        # We only support TvOS
        if tv.device_info.operating_system == pyatv.const.OperatingSystem.TvOS:
            res.append(tv)

    return res

async def connectToAppleTv(atv):
    global connectedAtv
    connectedAtv = await pyatv.connect(atv, LOOP)

async def disconnectFromAppleTv():
    global connectedAtv
    global isConnected
    connectedAtv.close()
    isConnected = False

async def finishPairing(websocket):
    global pairingProcess

    await pairingProcess.finish()

    if pairingProcess.has_paired:
        LOG.debug("Paired with device!")
        await storeCredentials(pairingAtv, pairingProcess.service)
        await api.driverSetupComplete(websocket)
    else:
        LOG.warning('Did not pair with device!')
        await api.driverSetupError(websocket, 'Unable to pair with Apple TV')

    await pairingProcess.close()
    pairingProcess = None

async def connect():
    global isConnected

    if isConnected is True:
        return

    tv = await restoreCredentials()

    if tv is None:
        LOG.error('Cannot find AppleTV to connect to')
        await api.setDeviceState(uc.uc.DEVICE_STATES.ERROR)
        return

    await connectToAppleTv(tv)

    entity = entities.media_player.MediaPlayer(tv.identifier, tv.name, [
            entities.media_player.FEATURES.ON_OFF,
            # entities.media_player.FEATURES.VOLUME,
            entities.media_player.FEATURES.MUTE_TOGGLE,
            entities.media_player.FEATURES.PLAY_PAUSE,
            entities.media_player.FEATURES.NEXT,
            entities.media_player.FEATURES.PREVIOUS,
            entities.media_player.FEATURES.MEDIA_DURATION,
            entities.media_player.FEATURES.MEDIA_POSITION,
            entities.media_player.FEATURES.MEDIA_TITLE,
            entities.media_player.FEATURES.MEDIA_ARTIST,
            entities.media_player.FEATURES.MEDIA_ALBUM,
            entities.media_player.FEATURES.MEDIA_IMAGE_URL                                       
        ], {
            entities.media_player.ATTRIBUTES.STATE: entities.media_player.STATES.OFF,
            # entities.media_player.ATTRIBUTES.VOLUME: 0,
            # entities.media_player.ATTRIBUTES.MUTED: False,
            entities.media_player.ATTRIBUTES.MEDIA_DURATION: 0,
			entities.media_player.ATTRIBUTES.MEDIA_POSITION: 0,
			entities.media_player.ATTRIBUTES.MEDIA_IMAGE_URL: "",
			entities.media_player.ATTRIBUTES.MEDIA_TITLE: "",
			entities.media_player.ATTRIBUTES.MEDIA_ARTIST: "",
			entities.media_player.ATTRIBUTES.MEDIA_ALBUM: ""
        })
    api.availableEntities.addEntity(entity)

    isConnected = True

async def polling():
    global api
    global connectedAtv
    prevHash = None
    while True:
        if api.configuredEntities.contains(connectedAtv.service.identifier):
            playing = await connectedAtv.metadata.playing()
            power = connectedAtv.power

            state = entities.media_player.STATES.UNKNOWN

            if power.power_state is pyatv.const.PowerState.On:
                state = entities.media_player.STATES.ON

                if playing.device_state == pyatv.const.DeviceState.Playing:
                    state = entities.media_player.STATES.PLAYING
                elif playing.device_state == pyatv.const.DeviceState.Paused:
                    state = entities.media_player.STATES.PAUSED
                elif playing.device_state == pyatv.const.DeviceState.Idle:
                    state = entities.media_player.STATES.PAUSED

            elif power.power_state is pyatv.const.PowerState.Off:
                state = entities.media_player.STATES.OFF

            attributes = {
                entities.media_player.ATTRIBUTES.STATE: state,
                entities.media_player.ATTRIBUTES.MEDIA_POSITION: playing.position,
            }
            
            # Update if content changed
            if playing.hash != prevHash:
                try:
                    artwork = await connectedAtv.metadata.artwork(width=480, height=None)
                    artwork_encoded = 'data:image/png;base64,' + base64.b64encode(artwork.bytes).decode('utf-8')
                    attributes[entities.media_player.ATTRIBUTES.MEDIA_IMAGE_URL] = artwork_encoded
                except:
                    LOG.error('OMG')
                
                attributes[entities.media_player.ATTRIBUTES.MEDIA_DURATION] = playing.total_time
                attributes[entities.media_player.ATTRIBUTES.MEDIA_TITLE] = playing.title
                attributes[entities.media_player.ATTRIBUTES.MEDIA_ARTIST] = playing.artist
                attributes[entities.media_player.ATTRIBUTES.MEDIA_ALBUM] = playing.album

            prevHash = playing.hash

            api.configuredEntities.updateEntityAttributes(
                    connectedAtv.service.identifier,
                    attributes
                )

        await asyncio.sleep(2)

def startPolling():
    global pollingTask

    if pollingTask is not None:
        return

    pollingTask = LOOP.create_task(polling())
    LOG.debug('Polling started')

def stopPolling():
    global pollingTask
    pollingTask.cancel()
    pollingTask = None
    LOG.debug('Polling stopped')

# DRIVER SETUP
@api.events.on(uc.uc.EVENTS.SETUP_DRIVER)
async def event_handler(websocket, id, data):
    LOG.debug('Starting driver setup')
    await api.acknowledgeCommand(websocket, id)
    await api.driverSetupProgress(websocket)

    LOG.debug('Starting Apple TV discovery')
    tvs = await discoverAppleTVs();
    dropdownItems = []

    for tv in tvs:
        tvData = {
            'id': tv.identifier,
            'label': {
                'en': tv.name + " TvOS " + tv.device_info.version
            }
        }

        dropdownItems.append(tvData)

    if not dropdownItems:
        LOG.warning('No Apple TVs found')
        await api.driverSetupError(websocket, 'No Apple TVs found')
        return

    await api.requestDriverSetupUserInput(websocket, 'Please choose your Apple TV', [
        { 
        'field': { 
            'dropdown': {
                'value': dropdownItems[0]['id'],
                'items': dropdownItems
            }
        },
        'id': 'choice',
        'label': { 'en': 'Choose your Apple TV' }
        }
    ])

@api.events.on(uc.uc.EVENTS.SETUP_DRIVER_USER_DATA)
async def event_handler(websocket, id, data):
    await api.acknowledgeCommand(websocket, id)
    await api.driverSetupProgress(websocket)

    global pairingProcess
    global pairingAtv

    # TODO add timeout for inputs

    if "pin" in data:
        LOG.debug('User has entered the PIN')
        pairingProcess.pin(data['pin'])
        await finishPairing(websocket)

    elif "choice" in data:
        choice = data['choice']
        LOG.debug('Chosen Apple TV: ' + choice)
        
        atvs = await pyatv.scan(LOOP, identifier=choice)

        if not atvs:
            LOG.error('Cannot find the chosen AppleTV')
            await api.driverSetupError(websocket, 'There was an error during the setup process')
            return

        LOG.debug('Pairing process begin')
        pairingAtv = atvs[0]
        pairingProcess = await pyatv.pair(pairingAtv, pyatv.const.Protocol.AirPlay, LOOP)
        await pairingProcess.begin()

        if pairingProcess.device_provides_pin:
            LOG.debug('Device provides PIN')
            await api.requestDriverSetupUserInput(websocket, 'Please enter the PIN from your Apple TV', [
                { 
                'field': { 
                    'number': { 'max': 9999, 'min': 0, 'value': 0000 }
                },
                'id': 'pin',
                'label': { 'en': 'Apple TV PIN' }
                }
            ])
        else:
            LOG.debug('We provide PIN')
            pin = random.randint(1000,9999)
            pairingProcess.pin(pin)
            await api.requestDriverSetupUserConfirmation(websocket, 'Please enter the following PIN on your Apple TV:' + pin)
            await finishPairing(websocket)

    else:
        LOG.error('No choice was received')
        await api.driverSetupError(websocket, 'No Apple TV was selected')

@api.events.on(uc.uc.EVENTS.CONNECT)
async def event_handler():
    await connect()
    await api.setDeviceState(uc.uc.DEVICE_STATES.CONNECTED)
    startPolling()

@api.events.on(uc.uc.EVENTS.DISCONNECT)
async def event_handler():
    await disconnectFromAppleTv()
    stopPolling()
    await api.setDeviceState(uc.uc.DEVICE_STATES.DISCONNECTED)

@api.events.on(uc.uc.EVENTS.ENTER_STANDBY)
async def event_handler():
    await disconnectFromAppleTv()
    stopPolling()

@api.events.on(uc.uc.EVENTS.EXIT_STANDBY)
async def event_handler():
    global connectedAtv
    await connectToAppleTv(connectedAtv)
    startPolling()

@api.events.on(uc.uc.EVENTS.SUBSCRIBE_ENTITIES)
async def event_handler(entityIds):
    global connectedAtv

    if connectedAtv is None:
        await api.setDeviceState(uc.uc.DEVICE_STATES.ERROR)
        return

    # We only have one appleTv per driver for now
    for entityId in entityIds:
        if entityId == connectedAtv.service.identifier:
            LOG.debug('We have a match, start listening to events')

@api.events.on(uc.uc.EVENTS.UNSUBSCRIBE_ENTITIES)
async def event_handler(entityIds):
    global connectedAtv

    if connectedAtv is None:
        await api.setDeviceState(uc.uc.DEVICE_STATES.ERROR)
        return

    # We only have one appleTv per driver for now
    for entityId in entityIds:
        if entityId == connectedAtv.service.identifier:
            LOG.debug('We have a match, stop listening to events')

#TODO handle commands
@api.events.on(uc.uc.EVENTS.ENTITY_COMMAND)
async def event_handler(websocket, id, entityId, entityType, cmdId, params):
    global connectedAtv

    rc = connectedAtv.remote_control

    if cmdId == entities.media_player.COMMANDS.PLAY_PAUSE:
        await rc.play_pause()
        await api.acknowledgeCommand(websocket, id)

async def main():
    await connect()
    await api.init('driver.json')

if __name__ == "__main__":
    LOOP.run_until_complete(main())
    LOOP.run_forever()