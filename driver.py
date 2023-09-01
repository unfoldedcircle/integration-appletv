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
    """"Remove the configuration file"""
    global config
    config = []

    if os.path.exists(dataPath + '/config.json'):
        os.remove(dataPath + '/config.json')

async def storeCofig():
    global config
    f = None
    try:
        f= open(dataPath + '/config.json', 'w+', encoding='utf-8')
    except OSError:
        LOG.error('Cannot write the config file')
        return

    json.dump(config, f, ensure_ascii=False)

    f.close()

async def loadConfig():
    """"Load the config into the config global variable"""
    global config
    f = None
    try:
        f = open(dataPath + '/config.json', 'r', encoding='utf-8')
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

    if not config:
        return False

    return True
        
async def discoverAppleTVs():
    """"Discover Apple TVs on the network using pyatv.scan"""
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
        await api.driverSetupError(websocket)
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

    await api.acknowledgeCommand(websocket, id)
    await api.driverSetupProgress(websocket)

    # We pair with companion second
    if "pin_companion" in data:
        LOG.debug('User has entered the Companion PIN')
        await pairingAppleTv.enterPin(data['pin_companion'])

        res = await pairingAppleTv.finishPairing()
        if res is None:
            await api.driverSetupError(websocket)
        else:
            c = {
                'protocol': res.protocol.name.lower(),
                'credentials': res.credentials
            }
            pairingAppleTv.addCredentials(c)

            configuredAppleTvs[pairingAppleTv.identifier] = pairingAppleTv

            config.append({
                'identifier' : pairingAppleTv.identifier,
                'name': pairingAppleTv.name,
                'credentials': pairingAppleTv.getCredentials() 
            })
            await storeCofig()

            addAvailableAppleTv(pairingAppleTv.identifier, pairingAppleTv.name)

            await api.driverSetupComplete(websocket)
    
    # We pair with airplay first
    elif "pin_airplay" in data:
        LOG.debug('User has entered the Airplay PIN')
        await pairingAppleTv.enterPin(data['pin_airplay'])
        
        res = await pairingAppleTv.finishPairing()
        if res is None:
            await api.driverSetupError(websocket)
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
        pairingAppleTv.pairingAtv = await pairingAppleTv.findAtv(choice)
        
        if pairingAppleTv.pairingAtv is None:
            LOG.error('Cannot find the chosen AppleTV')
            await api.driverSetupError(websocket)
            return
        
        await pairingAppleTv.init(choice, name = pairingAppleTv.pairingAtv.name)

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
        await api.driverSetupError(websocket)

# When the core connects, we just set the device state
@api.events.on(uc.uc.EVENTS.CONNECT)
async def event_handler():
    await api.setDeviceState(uc.uc.DEVICE_STATES.CONNECTED)

# When the core disconnects, we just set the device state
@api.events.on(uc.uc.EVENTS.DISCONNECT)
async def event_handler():
    for entityId in configuredAppleTvs:
        LOG.debug('Client disconnected, disconnecting all Apple TVs')
        appleTv = configuredAppleTvs[entityId]
        await appleTv.disconnect()
        appleTv.events.remove_all_listeners()

    await api.setDeviceState(uc.uc.DEVICE_STATES.DISCONNECTED)

# On standby, we disconnect every Apple TV objects
@api.events.on(uc.uc.EVENTS.ENTER_STANDBY)
async def event_handler():
    global configuredAppleTvs

    for appleTv in configuredAppleTvs:
        await configuredAppleTvs[appleTv].disconnect()

# On exit standby we wait a bit then connect all Apple TV objects
@api.events.on(uc.uc.EVENTS.EXIT_STANDBY)
async def event_handler():
    global configuredAppleTvs

    await asyncio.sleep(2)

    for appleTv in configuredAppleTvs:
        await configuredAppleTvs[appleTv].connect()

# When the core subscribes to entities, we set these to UNAVAILABLE state
# then we hook up to the signals of the object and then connect
@api.events.on(uc.uc.EVENTS.SUBSCRIBE_ENTITIES)
async def event_handler(entityIds):
    global configuredAppleTvs

    for entityId in entityIds:
        if entityId in configuredAppleTvs:
            LOG.debug('We have a match, start listening to events')

            api.configuredEntities.updateEntityAttributes(entityId, {
                entities.media_player.ATTRIBUTES.STATE: entities.media_player.STATES.UNAVAILABLE
            })

            appleTv = configuredAppleTvs[entityId]

            @appleTv.events.on(tv.EVENTS.CONNECTED)
            async def _onConnected(identifier):
                await handleConnected(identifier)

            @appleTv.events.on(tv.EVENTS.DISCONNECTED)
            async def _onDisconnected(identifier):
                await handleDisconnected(identifier)
            
            @appleTv.events.on(tv.EVENTS.ERROR)
            async def _onDisconnected(identifier, message):
                await handleConnectionError(identifier, message)

            @appleTv.events.on(tv.EVENTS.UPDATE)
            async def onUpdate(update):
                await handleAppleTvUpdate(entityId, update)

            await appleTv.connect()

# On unsubscribe, we disconnect the objects and remove listeners for events
@api.events.on(uc.uc.EVENTS.UNSUBSCRIBE_ENTITIES)
async def event_handler(entityIds):
    global configuredAppleTvs

    for entityId in entityIds:
        if entityId in configuredAppleTvs:
            LOG.debug('We have a match, stop listening to events')
            appleTv = configuredAppleTvs[entityId]
            await appleTv.disconnect()
            appleTv.events.remove_all_listeners()

# We handle commands here
@api.events.on(uc.uc.EVENTS.ENTITY_COMMAND)
async def event_handler(websocket, id, entityId, entityType, cmdId, params):
    global configuredAppleTvs
 
    appleTv = configuredAppleTvs[entityId]

    # If the device is not on we send SERVICE_UNAVAILABLE
    if appleTv.isOn is False:
        await api.acknowledgeCommand(websocket, id, uc.uc.STATUS_CODES.SERVICE_UNAVAILABLE)
        return

    configuredEntity = api.configuredEntities.getEntity(entityId)

    # If the entity is OFF, we send the turnOn command regardless of the actual command
    if configuredEntity.attributes[entities.media_player.ATTRIBUTES.STATE] == entities.media_player.STATES.OFF:
        LOG.debug('Apple TV is off, sending turn on command')
        res = await appleTv.turnOn()
        await api.acknowledgeCommand(websocket, id, uc.uc.STATUS_CODES.OK if res is True else uc.uc.STATUS_CODES.SERVER_ERROR)
        return

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
    elif cmdId == entities.media_player.COMMANDS.CURSOR_UP:
        res = await appleTv.cursorUp()
        await api.acknowledgeCommand(websocket, id, uc.uc.STATUS_CODES.OK if res is True else uc.uc.STATUS_CODES.SERVER_ERROR)
    elif cmdId == entities.media_player.COMMANDS.CURSOR_DOWN:
        res = await appleTv.cursorDown()
        await api.acknowledgeCommand(websocket, id, uc.uc.STATUS_CODES.OK if res is True else uc.uc.STATUS_CODES.SERVER_ERROR)
    elif cmdId == entities.media_player.COMMANDS.CURSOR_LEFT:
        res = await appleTv.cursorLeft()
        await api.acknowledgeCommand(websocket, id, uc.uc.STATUS_CODES.OK if res is True else uc.uc.STATUS_CODES.SERVER_ERROR)
    elif cmdId == entities.media_player.COMMANDS.CURSOR_RIGHT:
        res = await appleTv.cursorRight()
        await api.acknowledgeCommand(websocket, id, uc.uc.STATUS_CODES.OK if res is True else uc.uc.STATUS_CODES.SERVER_ERROR)
    elif cmdId == entities.media_player.COMMANDS.CURSOR_ENTER:
        res = await appleTv.cursorEnter()
        await api.acknowledgeCommand(websocket, id, uc.uc.STATUS_CODES.OK if res is True else uc.uc.STATUS_CODES.SERVER_ERROR)
    elif cmdId == entities.media_player.COMMANDS.HOME:
        res = await appleTv.home()
        await api.acknowledgeCommand(websocket, id, uc.uc.STATUS_CODES.OK if res is True else uc.uc.STATUS_CODES.SERVER_ERROR)
        
        # we wait a bit to get a push update, because music can play in the background
        await asyncio.sleep(1)
        if configuredEntity.attributes[entities.media_player.ATTRIBUTES.STATE] != entities.media_player.STATES.PLAYING:
            # if nothing is playing we clear the playing information
            attributes = {}
            attributes[entities.media_player.ATTRIBUTES.MEDIA_IMAGE_URL] = ""
            attributes[entities.media_player.ATTRIBUTES.MEDIA_ALBUM] = ""
            attributes[entities.media_player.ATTRIBUTES.MEDIA_ARTIST] = ""
            attributes[entities.media_player.ATTRIBUTES.MEDIA_TITLE] = ""
            attributes[entities.media_player.ATTRIBUTES.MEDIA_TYPE] = ""
            attributes[entities.media_player.ATTRIBUTES.SOURCE] = ""
            attributes[entities.media_player.ATTRIBUTES.MEDIA_DURATION] = 0
            api.configuredEntities.updateEntityAttributes(entityId, attributes)
    elif cmdId == entities.media_player.COMMANDS.BACK:
        res = await appleTv.menu()
        await api.acknowledgeCommand(websocket, id, uc.uc.STATUS_CODES.OK if res is True else uc.uc.STATUS_CODES.SERVER_ERROR)
    elif cmdId == entities.media_player.COMMANDS.CHANNEL_DOWN:
        res = await appleTv.channelDown()
        await api.acknowledgeCommand(websocket, id, uc.uc.STATUS_CODES.OK if res is True else uc.uc.STATUS_CODES.SERVER_ERROR)
    elif cmdId == entities.media_player.COMMANDS.CHANNEL_UP:
        res = await appleTv.channelUp()
        await api.acknowledgeCommand(websocket, id, uc.uc.STATUS_CODES.OK if res is True else uc.uc.STATUS_CODES.SERVER_ERROR)
    elif cmdId == entities.media_player.COMMANDS.SELECT_SOURCE:
        res = await appleTv.launchApp(params["source"])
        await api.acknowledgeCommand(websocket, id, uc.uc.STATUS_CODES.OK if res is True else uc.uc.STATUS_CODES.SERVER_ERROR)


def keyUpdateHelper(key, value, attributes, configuredEntity):
    if value is None:
        return attributes

    if key in configuredEntity.attributes:
        if configuredEntity.attributes[key] != value:
            attributes[key] = value
    else:
        attributes[key] = value

    return attributes


async def handleConnected(identifier):
    LOG.debug('Apple TV connected: %s', identifier)
    configuredEntity = api.configuredEntities.getEntity(identifier)

    if configuredEntity.attributes[entities.media_player.ATTRIBUTES.STATE] == entities.media_player.STATES.UNAVAILABLE:
        api.configuredEntities.updateEntityAttributes(identifier, {
            entities.media_player.ATTRIBUTES.STATE: entities.media_player.STATES.STANDBY
        })


async def handleDisconnected(identifier):
    LOG.debug('Apple TV disconnected: %s', identifier)
    api.configuredEntities.updateEntityAttributes(identifier, {
        entities.media_player.ATTRIBUTES.STATE: entities.media_player.STATES.UNAVAILABLE
    })


async def handleConnectionError(identifier, message):
    LOG.error(message)
    api.configuredEntities.updateEntityAttributes(identifier, {
        entities.media_player.ATTRIBUTES.STATE: entities.media_player.STATES.UNAVAILABLE
    })
    await api.setDeviceState(uc.uc.DEVICE_STATES.ERROR)


async def handleAppleTvUpdate(entityId, update):
    attributes = {}

    configuredEntity = api.configuredEntities.getEntity(entityId)

    if 'state' in update:
        state = entities.media_player.STATES.UNKNOWN

        if update['state'] == pyatv.const.PowerState.On:
            state = entities.media_player.STATES.ON
        elif update['state'] == pyatv.const.DeviceState.Playing:
            state = entities.media_player.STATES.PLAYING
        elif update['state'] == pyatv.const.DeviceState.Playing:
            state = entities.media_player.STATES.PLAYING
        elif update['state'] == pyatv.const.DeviceState.Paused:
            state = entities.media_player.STATES.PAUSED
        elif update['state'] == pyatv.const.DeviceState.Idle:
            state = entities.media_player.STATES.PAUSED
        elif update['state'] == pyatv.const.PowerState.Off:
            state = entities.media_player.STATES.OFF

        attributes = keyUpdateHelper(entities.media_player.ATTRIBUTES.STATE, state, attributes, configuredEntity)

    if 'position' in update:
        attributes = keyUpdateHelper(entities.media_player.ATTRIBUTES.MEDIA_POSITION, update['position'], attributes, configuredEntity)
    if 'artwork' in update:
        attributes[entities.media_player.ATTRIBUTES.MEDIA_IMAGE_URL] = update['artwork']
    if 'total_time' in update:
        attributes = keyUpdateHelper(entities.media_player.ATTRIBUTES.MEDIA_DURATION, update['total_time'], attributes, configuredEntity)
    if 'title' in update:
        attributes = keyUpdateHelper(entities.media_player.ATTRIBUTES.MEDIA_TITLE, update['title'], attributes, configuredEntity)
    if 'artist' in update:
        attributes = keyUpdateHelper(entities.media_player.ATTRIBUTES.MEDIA_ARTIST, update['artist'], attributes, configuredEntity)
    if 'album' in update:
        attributes = keyUpdateHelper(entities.media_player.ATTRIBUTES.MEDIA_ALBUM, update['album'], attributes, configuredEntity)
    if 'source' in update:
        attributes = keyUpdateHelper(entities.media_player.ATTRIBUTES.SOURCE, update['source'], attributes, configuredEntity)
    if 'sourceList' in update:
        if entities.media_player.ATTRIBUTES.SOURCE_LIST in configuredEntity.attributes:
            if len(configuredEntity.attributes[entities.media_player.ATTRIBUTES.SOURCE_LIST]) != len(update['sourceList']):
                attributes[entities.media_player.ATTRIBUTES.SOURCE_LIST] = update['sourceList']
        else:
            attributes[entities.media_player.ATTRIBUTES.SOURCE_LIST] = update['sourceList']
    if 'media_type' in update:
        mediaType = ""

        if update['media_type'] == pyatv.const.MediaType.Music:
            mediaType = entities.media_player.MEDIA_TYPE.MUSIC
        elif update['media_type'] == pyatv.const.MediaType.TV:
            mediaType = entities.media_player.MEDIA_TYPE.TVSHOW
        elif update['media_type'] == pyatv.const.MediaType.Video:
            mediaType = entities.media_player.MEDIA_TYPE.VIDEO
        elif update['media_type'] == pyatv.const.MediaType.Unknown:
            mediaType = ""

        attributes = keyUpdateHelper(entities.media_player.ATTRIBUTES.MEDIA_TYPE, mediaType, attributes, configuredEntity)

    if 'volume' in update:
        attributes[entities.media_player.ATTRIBUTES.VOLUME] = update['volume']

    if entities.media_player.ATTRIBUTES.STATE in attributes:
        if attributes[entities.media_player.ATTRIBUTES.STATE] == entities.media_player.STATES.OFF:
            attributes[entities.media_player.ATTRIBUTES.MEDIA_IMAGE_URL] = ""
            attributes[entities.media_player.ATTRIBUTES.MEDIA_ALBUM] = ""
            attributes[entities.media_player.ATTRIBUTES.MEDIA_ARTIST] = ""
            attributes[entities.media_player.ATTRIBUTES.MEDIA_TITLE] = ""
            attributes[entities.media_player.ATTRIBUTES.MEDIA_TYPE] = ""
            attributes[entities.media_player.ATTRIBUTES.SOURCE] = ""
            attributes[entities.media_player.ATTRIBUTES.MEDIA_DURATION] = 0

    if attributes:
        api.configuredEntities.updateEntityAttributes(entityId, attributes)


def addAvailableAppleTv(identifier, name):
    entity = entities.media_player.MediaPlayer(identifier, name, [
        entities.media_player.FEATURES.ON_OFF,
        entities.media_player.FEATURES.VOLUME,
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
        entities.media_player.FEATURES.MEDIA_IMAGE_URL,
        entities.media_player.FEATURES.MEDIA_TYPE,
        entities.media_player.FEATURES.HOME,
        entities.media_player.FEATURES.CHANNEL_SWITCHER,                                                                     
        entities.media_player.FEATURES.DPAD,
        entities.media_player.FEATURES.SELECT_SOURCE,
    ], {
        entities.media_player.ATTRIBUTES.STATE: entities.media_player.STATES.UNAVAILABLE,
        entities.media_player.ATTRIBUTES.VOLUME: 0,
        # entities.media_player.ATTRIBUTES.MUTED: False,
        entities.media_player.ATTRIBUTES.MEDIA_DURATION: 0,
        entities.media_player.ATTRIBUTES.MEDIA_POSITION: 0,
        entities.media_player.ATTRIBUTES.MEDIA_IMAGE_URL: "",
        entities.media_player.ATTRIBUTES.MEDIA_TITLE: "",
        entities.media_player.ATTRIBUTES.MEDIA_ARTIST: "",
        entities.media_player.ATTRIBUTES.MEDIA_ALBUM: ""
    }, deviceClass = entities.media_player.DEVICECLASSES.TV)

    api.availableEntities.addEntity(entity)


async def main():
    global dataPath
    global config
    global configuredAppleTvs

    dataPath = api.configDirPath

    # We load the config and create an AppleTv object for every entry
    # We also create an available entity entry for every config entry
    res = await loadConfig()
    if res is True:
        for item in config:
            # TODO: remove this after one verison update
            name = 'AppleTv'
            if 'name' in item:
                name = item['name']

            appleTv = tv.AppleTv(LOOP)
            await appleTv.init(item['identifier'], item['credentials'], name)
            configuredAppleTvs[appleTv.identifier] = appleTv

            addAvailableAppleTv(item['identifier'], name)
    else:  
        LOG.error("Cannot load config")

    await api.init('driver.json')

if __name__ == "__main__":
    LOOP.run_until_complete(main())
    LOOP.run_forever()
