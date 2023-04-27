import asyncio
import logging
import random
import json

import uc_integration_api.api as uc

import pyatv
import pyatv.const

LOG = logging.getLogger(__name__)
LOOP = asyncio.get_event_loop()
LOG.setLevel(logging.DEBUG)

api = uc.IntegrationAPI(LOOP)
pairingAtv = None
pairingProcess = None
connectedAtv = None

async def discoverAppleTVs():
    atvs = await pyatv.scan(LOOP)
    res = []

    for tv in atvs:
        # We only support TvOS
        if tv.device_info.operating_system == pyatv.const.OperatingSystem.TvOS:
            res.append(tv)

    return res

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

async def connectToAppleTv(atv):
    global connectedAtv
    connectedAtv = await pyatv.connect(atv, LOOP)

async def disconnectFromAppleTv():
    global connectedAtv
    connectedAtv.close()

# DRIVER SETUP
@api.events.on(uc.uc.EVENTS.SETUP_DRIVER)
async def setup_driver_event_handler(websocket, id, data):
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
async def setup_driver_user_data_vent_handler(websocket, id, data):
    await api.acknowledgeCommand(websocket, id)
    await api.driverSetupProgress(websocket)

    global pairingProcess
    global pairingAtv

    # TODO add timeout for inputs

    if "pin" in data:
        LOG.debug('User has entered the PIN')
        pairingProcess.pin(data['pin'])

        await pairingProcess.finish()

        if pairingProcess.has_paired:
            LOG.debug("Paired with device!")
            await storeCredentials(pairingAtv, pairingProcess.service)
            await api.driverSetupComplete(websocket)
        else:
            LOG.warning('Did not pair with device!')
            await api.driverSetupError(websocket, 'Unable to pair with Apple TV')

        await pairingProcess.close()

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
        pairingProcess = await pyatv.pair(pairingAtv, pyatv.const.Protocol.Companion, LOOP)
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

            await pairingProcess.finish()

            if pairingProcess.has_paired:
                print("Paired with device!")
                await storeCredentials(pairingAtv, pairingProcess.service)
                await api.driverSetupComplete(websocket)
            else:
                LOG.warning('Did not pair with device!')
                await api.driverSetupError(websocket, 'Unable to pair with Apple TV')

            await pairingProcess.close()

    else:
        LOG.error('No choice was received')
        await api.driverSetupError(websocket, 'No Apple TV was selected')

@api.events.on(uc.uc.EVENTS.CONNECT)
async def event_handler():
    tv = await restoreCredentials()

    if tv is None:
        LOG.error('Cannot find AppleTV to connect to')
        await api.setDeviceState(uc.uc.DEVICE_STATES.ERROR)
        return

    await connectToAppleTv(tv)
    await api.setDeviceState(uc.uc.DEVICE_STATES.CONNECTED)

@api.events.on(uc.uc.EVENTS.DISCONNECT)
async def event_handler():
    disconnectFromAppleTv()
    await api.setDeviceState(uc.uc.DEVICE_STATES.DISCONNECTED)

#TODO add suspend/resume

async def main():
    await api.init('driver.json')

if __name__ == "__main__":
    LOOP.run_until_complete(main())
    LOOP.run_forever()