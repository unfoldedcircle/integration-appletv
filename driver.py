import asyncio
import logging
import random

import uc_integration_api.api as uc

import pyatv
import pyatv.const

LOG = logging.getLogger(__name__)
LOOP = asyncio.get_event_loop()

connectedAtv = None

async def discoverAppleTVs():
    atvs = await pyatv.scan(LOOP)

    res = []

    for tv in atvs:
        if tv.device_info.operating_system == pyatv.const.OperatingSystem.TvOS:
            res.append(tv)

    return res

# TODO save to file and add load method
async def storeCredentials(tv, service):
    identifier = tv.identifier
    protocol = service.protocol
    credentials = service.credentials
    print(identifier)
    print(protocol)
    print(credentials)

async def restoreCredentials(identifier, protocol, credentials):
    atvs = pyatv.scan(LOOP, identifier=identifier)
    atv = atvs[0]
    atv.set_credentials(protocol, credentials)
    return atv

async def connectToAppleTv(atv):
    connectedAtv = await pyatv.connect(atv, LOOP)

async def disconnectFromAppleTv(atv):
    connectedAtv.close()

async def main():
    api = uc.IntegrationAPI()
    await api.init(LOOP, 'driver.json')

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

        pairing = None

        if "choice" in data:
            choice = data['choice']
            LOG.debug('Chosen Apple TV: ' + choice)
            
            atvs = await pyatv.scan(LOOP, identifier=choice)

            if not atvs:
                LOG.error('Cannot find the chosen AppleTV')
                await api.driverSetupError(websocket, 'There was an error during the setup process')
                return

            pairing = await pyatv.pair(atvs[0], pyatv.const.Protocol.Companion, LOOP)
            await pairing.begin()

            if pairing.device_provides_pin:
                await api.requestDriverSetupUserInput(websocket, 'Please enter the PIN from your Apple TV', [
                    { 
                    'field': { 
                        'number': { 'max': 9999, 'min': 0 }
                    },
                    'id': 'pin',
                    'label': { 'en': 'Apple TV PIN' }
                    }
                ])
            else:
                pin = random.randint(1000,9999)
                pairing.pin(pin)
                await api.uc.requestDriverSetupUserConfirmation(websocket, 'Please enter the following PIN on your Apple TV:' + pin)

                # await pairing.finish()

                # if pairing.has_paired:
                #     print("Paired with device!")
                #     storeCredentials(atvs[0], pairing.service)
                #     await uc.driverSetupComplete(websocket)
                # else:
                #     LOG.warning('Did not pair with device!')
                #     await api.driverSetupError(websocket, 'Unable to pair with Apple TV')

                # await pairing.close()

        elif "pin" in data:
            print("we got pin")
            print(data['pin'])
            pairing.pin(data['pin'])

            await pairing.finish()

            if pairing.has_paired:
                LOG.debug("Paired with device!")
                storeCredentials(atvs[0], pairing.service)
                await uc.driverSetupComplete(websocket)
            else:
                LOG.warning('Did not pair with device!')
                await api.driverSetupError(websocket, 'Unable to pair with Apple TV')

            await pairing.close()

        else:
            LOG.error('No choice was received')
            await api.driverSetupError(websocket, 'No Apple TV was selected')

    @api.events.on(uc.uc.EVENTS.CONNECT)
    async def event_handler():
        await api.setDeviceState(uc.uc.DEVICE_STATES.CONNECTED)

    @api.events.on(uc.uc.EVENTS.DISCONNECT)
    async def event_handler():
        await api.setDeviceState(uc.uc.DEVICE_STATES.DISCONNECTED)

if __name__ == "__main__":
    LOOP.run_until_complete(main())
    LOOP.run_forever()