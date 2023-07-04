import asyncio
import logging
import json
import os

import ucapi.api as uc
import ucapi.entities as entities

import pyatv
import pyatv.const

from pyatv.interface import PushListener

import tv

LOG = logging.getLogger(__name__)
LOOP = asyncio.get_event_loop()
LOG.setLevel(logging.DEBUG)

# Global variables
dataPath = None
api = uc.IntegrationAPI(LOOP)
config = []
configuredAppleTvs = {}
pairingAppleTv = None
    
async def clearConfig():
    global config
    config = []

    if os.path.exists(dataPath + '/config.json'):
        os.remove(dataPath + '/config.json')

async def storeCofig():
    global config
    f = None
    try:
        f= open(dataPath + '/config.json', 'w+')
    except OSError:
        LOG.error('Cannot write the config file')
        return

    json.dump(config, f, ensure_ascii=False)

    f.close()

async def loadConfig():
    global config
    f = None
    try:
        f = open(dataPath + '/config.json', 'r')
    except OSError:
        LOG.error('Cannot open the config file')
    
    if f is None:
        return False

    try:
        data = json.load(f)
        f.close()
    except ValueError:
        LOG.error('Empty config file')
        return False

    config = data

    return True
        
async def discoverAppleTVs():
    atvs = await pyatv.scan(LOOP)
    res = []

    for tv in atvs:
        # We only support TvOS
        if tv.device_info.operating_system == pyatv.const.OperatingSystem.TvOS:
            res.append(tv)

    return res


# DRIVER SETUP
@api.events.on(uc.uc.EVENTS.SETUP_DRIVER)
async def event_handler(websocket, id, data):
    LOG.debug('Starting driver setup')
    await clearConfig()
    await api.acknowledgeCommand(websocket, id)
    await api.driverSetupProgress(websocket)

    LOG.debug('Starting Apple TV discovery')
    tvs = await discoverAppleTVs();
    dropdownItems = []

    for tv in tvs:
        tvData = {
            'id': tv.identifier,
            'label': {
                'en': tv.name + " TvOS " + str(tv.device_info.version)
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
    global configuredAppleTvs
    global pairingAppleTv
    global config

    if pairingAppleTv:
        @pairingAppleTv.events.on(tv.EVENTS.ERROR)
        async def onError(message):
            LOG.error(message)
            await api.driverSetupError(websocket, message)

    await api.acknowledgeCommand(websocket, id)
    await api.driverSetupProgress(websocket)

    # We pair with companion second
    if "pin_companion" in data:
        LOG.debug('User has entered the Companion PIN')
        await pairingAppleTv.enterPin(data['pin_companion'])

        res = await pairingAppleTv.finishPairing()
        if res is None:
            await api.driverSetupError(websocket, 'Unable to pair with Apple TV')
        else:
            c = {
                'protocol': res.protocol.name.lower(),
                'credentials': res.credentials
            }
            pairingAppleTv.addCredentials(c)

            configuredAppleTvs[pairingAppleTv.identifier] = pairingAppleTv
            await configuredAppleTvs[pairingAppleTv.identifier].connect()

            config.append({
                'identifier' : pairingAppleTv.identifier,
                'credentials': pairingAppleTv.getCredentials() 
            })
            await storeCofig()
            await api.driverSetupComplete(websocket)

            entity = entities.media_player.MediaPlayer(pairingAppleTv.identifier, pairingAppleTv.name, [
                entities.media_player.FEATURES.ON_OFF,
                # entities.media_player.FEATURES.VOLUME,
                entities.media_player.FEATURES.VOLUME_UP_DOWN,
                # entities.media_player.FEATURES.MUTE_TOGGLE,
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
    
    # We pair with airplay first
    elif "pin_airplay" in data:
        LOG.debug('User has entered the Airplay PIN')
        await pairingAppleTv.enterPin(data['pin_airplay'])
        
        res = await pairingAppleTv.finishPairing()
        if res is None:
            await api.driverSetupError(websocket, 'Unable to pair with Apple TV')
        else:
            # Store credentials
            c = {
                'protocol': res.protocol.name.lower(),
                'credentials': res.credentials
            }
            pairingAppleTv.addCredentials(c)

            # Start new pairing process
            res = await pairingAppleTv.startPairing(pyatv.const.Protocol.Companion, "Remote Two Companion")

            if res == 0:
                LOG.debug('Device provides PIN')
                await api.requestDriverSetupUserInput(websocket, 'Please enter the PIN from your Apple TV', [
                    { 
                    'field': { 
                        'number': { 'max': 9999, 'min': 0, 'value': 0000 }
                    },
                    'id': 'pin_companion',
                    'label': { 'en': 'Apple TV PIN' }
                    }
                ])
            
            else:
                LOG.debug('We provide PIN')
                await api.requestDriverSetupUserConfirmation(websocket, 'Please enter the following PIN on your Apple TV:' + res)
                await pairingAppleTv.finishPairing()

    elif "choice" in data:
        choice = data['choice']
        LOG.debug('Chosen Apple TV: ' + choice)

        # Create a new AppleTv object
        pairingAppleTv = tv.AppleTv(LOOP)
        res = await pairingAppleTv.init(choice)
        
        if res is False:
            LOG.error('Cannot find the chosen AppleTV')
            await api.driverSetupError(websocket, 'There was an error during the setup process')
            return

        LOG.debug('Pairing process begin')
        # Hook up to signals
        res = await pairingAppleTv.startPairing(pyatv.const.Protocol.AirPlay, "Remote Two Airplay")

        if res == 0:
            LOG.debug('Device provides PIN')
            await api.requestDriverSetupUserInput(websocket, 'Please enter the PIN from your Apple TV', [
                { 
                'field': { 
                    'number': { 'max': 9999, 'min': 0, 'value': 0000 }
                },
                'id': 'pin_airplay',
                'label': { 'en': 'Apple TV PIN' }
                }
            ])

        else:
            LOG.debug('We provide PIN')
            await api.requestDriverSetupUserConfirmation(websocket, 'Please enter the following PIN on your Apple TV:' + res)
            await pairingAppleTv.finishPairing()

    else:
        LOG.error('No choice was received')
        await api.driverSetupError(websocket, 'No Apple TV was selected')

@api.events.on(uc.uc.EVENTS.CONNECT)
async def event_handler():
    global configuredAppleTvs

    for appleTv in configuredAppleTvs:

        atv = configuredAppleTvs[appleTv]

        @atv.events.on(tv.EVENTS.ERROR)
        async def onError(message):
            LOG.error(message)
            await api.setDeviceState(uc.uc.DEVICE_STATES.ERROR)

        await configuredAppleTvs[appleTv].connect()
        await api.setDeviceState(uc.uc.DEVICE_STATES.CONNECTED)

@api.events.on(uc.uc.EVENTS.DISCONNECT)
async def event_handler():
    global configuredAppleTvs

    for appleTv in configuredAppleTvs:
        await configuredAppleTvs[appleTv].disconnect()
        await api.setDeviceState(uc.uc.DEVICE_STATES.DISCONNECTED)

@api.events.on(uc.uc.EVENTS.ENTER_STANDBY)
async def event_handler():
    global configuredAppleTvs

    for appleTv in configuredAppleTvs:
        await configuredAppleTvs[appleTv].disconnect()
        await api.setDeviceState(uc.uc.DEVICE_STATES.DISCONNECTED)

@api.events.on(uc.uc.EVENTS.EXIT_STANDBY)
async def event_handler():
    global configuredAppleTvs

    for appleTv in configuredAppleTvs:
        await configuredAppleTvs[appleTv].connect()
        await api.setDeviceState(uc.uc.DEVICE_STATES.CONNECTED)

@api.events.on(uc.uc.EVENTS.SUBSCRIBE_ENTITIES)
async def event_handler(entityIds):
    global configuredAppleTvs

    for entityId in entityIds:
        if entityId in configuredAppleTvs:
            LOG.debug('We have a match, start listening to events')
            appleTv = configuredAppleTvs[entityId]
            @appleTv.events.on(tv.EVENTS.UPDATE)
            async def onUpdate(update):
                await handleAppleTvUpdate(entityId, update)

@api.events.on(uc.uc.EVENTS.UNSUBSCRIBE_ENTITIES)
async def event_handler(entityIds):
    global configuredAppleTvs

    for entityId in entityIds:
        if entityId in configuredAppleTvs:
            LOG.debug('We have a match, stop listening to events')
            appleTv = configuredAppleTvs[entityId]
            appleTv.events.remove_all_listeners()

#TODO handle commands
@api.events.on(uc.uc.EVENTS.ENTITY_COMMAND)
async def event_handler(websocket, id, entityId, entityType, cmdId, params):
    global configuredAppleTvs

    appleTv = configuredAppleTvs[entityId]

    if cmdId == entities.media_player.COMMANDS.PLAY_PAUSE:
        res = await appleTv.playPause()
        await api.acknowledgeCommand(websocket, id, uc.uc.STATUS_CODES.OK if res is True else uc.uc.STATUS_CODES.SERVER_ERROR)
    elif cmdId == entities.media_player.COMMANDS.NEXT:
        res = await appleTv.next()
        await api.acknowledgeCommand(websocket, id, uc.uc.STATUS_CODES.OK if res is True else uc.uc.STATUS_CODES.SERVER_ERROR)
    elif cmdId == entities.media_player.COMMANDS.PREVIOUS:
        res = await appleTv.previous()
        await api.acknowledgeCommand(websocket, id, uc.uc.STATUS_CODES.OK if res is True else uc.uc.STATUS_CODES.SERVER_ERROR)
    elif cmdId == entities.media_player.COMMANDS.VOLUME_UP:
        res = await appleTv.volumeUp()
        await api.acknowledgeCommand(websocket, id, uc.uc.STATUS_CODES.OK if res is True else uc.uc.STATUS_CODES.SERVER_ERROR)
    elif cmdId == entities.media_player.COMMANDS.VOLUME_DOWN:
        res = await appleTv.volumeDown()
        await api.acknowledgeCommand(websocket, id, uc.uc.STATUS_CODES.OK if res is True else uc.uc.STATUS_CODES.SERVER_ERROR)
    elif cmdId == entities.media_player.COMMANDS.ON:
        res = await appleTv.turnOn()
        await api.acknowledgeCommand(websocket, id, uc.uc.STATUS_CODES.OK if res is True else uc.uc.STATUS_CODES.SERVER_ERROR)
    elif cmdId == entities.media_player.COMMANDS.OFF:
        res = await appleTv.turnOff()
        await api.acknowledgeCommand(websocket, id, uc.uc.STATUS_CODES.OK if res is True else uc.uc.STATUS_CODES.SERVER_ERROR)


async def handleAppleTvUpdate(entityId, update):
    attributes = {}

    if 'state' in update:
        state = entities.media_player.STATES.UNKNOWN

        if update['state'] is pyatv.const.PowerState.On:
            state = entities.media_player.STATES.ON
        elif update['state'] is pyatv.const.DeviceState.Playing:
            state = entities.media_player.STATES.PLAYING
        elif update['state'] == pyatv.const.DeviceState.Playing:
            state = entities.media_player.STATES.PLAYING
        elif update['state'] == pyatv.const.DeviceState.Paused:
            state = entities.media_player.STATES.PAUSED
        elif update['state'] == pyatv.const.DeviceState.Idle:
            state = entities.media_player.STATES.PAUSED
        elif update['state'] is pyatv.const.PowerState.Off:
            state = entities.media_player.STATES.OFF
        
        attributes[entities.media_player.ATTRIBUTES.STATE] = state
    if 'position' in update:
        attributes[entities.media_player.ATTRIBUTES.MEDIA_POSITION] = update['position']
    if 'artwork' in update:
        attributes[entities.media_player.ATTRIBUTES.MEDIA_IMAGE_URL] = update['artwork']
    if 'total_time' in update:
        attributes[entities.media_player.ATTRIBUTES.MEDIA_DURATION] = update['total_time']
    if 'title' in update:
        attributes[entities.media_player.ATTRIBUTES.MEDIA_TITLE] = update['title']
    if 'artist' in update:
        attributes[entities.media_player.ATTRIBUTES.MEDIA_ARTIST] = update['artist']
    if 'album' in update:
        attributes[entities.media_player.ATTRIBUTES.MEDIA_ALBUM] = update['album']

    api.configuredEntities.updateEntityAttributes(entityId, attributes)

async def main():
    global dataPath
    global config
    global configuredAppleTvs

    await api.init('driver.json')
    dataPath = api.configDirPath

    res = await loadConfig()
    if res is True:
        for item in config:
            appleTv = tv.AppleTv(LOOP)
            await appleTv.init(item['identifier'], item['credentials'])
            configuredAppleTvs[appleTv.identifier] = appleTv

            entity = entities.media_player.MediaPlayer(appleTv.identifier, appleTv.name, [
                entities.media_player.FEATURES.ON_OFF,
                # entities.media_player.FEATURES.VOLUME,
                entities.media_player.FEATURES.VOLUME_UP_DOWN,
                # entities.media_player.FEATURES.MUTE_TOGGLE,
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
    else:  
        LOG.error("Cannot load config")

if __name__ == "__main__":
    LOOP.run_until_complete(main())
    LOOP.run_forever()