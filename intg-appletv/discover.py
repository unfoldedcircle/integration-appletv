"""
Apple TV device discovery.

:copyright: (c) 2023 by Unfolded Circle ApS.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import logging
from asyncio import AbstractEventLoop

import pyatv
import pyatv.const

_LOG = logging.getLogger(__name__)


async def apple_tvs(loop: AbstractEventLoop) -> list[dict]:
    """"Discover Apple TVs on the network using pyatv.scan"""
    _LOG.debug("Starting discovery")

    # extra safety, if anything goes wrong here the reconnection logic is dead
    try:
        atvs = await pyatv.scan(loop)
        res = []

        for tv in atvs:
            # We only support TvOS
            # TODO check for device model, TvOS is not sufficient!
            # https://github.com/unfoldedcircle/feature-and-bug-tracker/issues/173
            if tv.device_info.operating_system == pyatv.const.OperatingSystem.TvOS:
                res.append(tv)

        return res
    except Exception as ex:  # pylint: disable=broad-exception-caught
        _LOG.error("Failed to start discovery: %s", ex)
        return []
