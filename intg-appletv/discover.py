"""
Apple TV device discovery.

:copyright: (c) 2023-2024 by Unfolded Circle ApS.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import logging
from asyncio import AbstractEventLoop

import pyatv
import pyatv.const
from pyatv.const import DeviceModel

_LOG = logging.getLogger(__name__)


async def apple_tvs(loop: AbstractEventLoop, hosts: list[str] | None = None) -> list[pyatv.interface.BaseConfig]:
    """Discover Apple TVs on the network using pyatv.scan."""
    if hosts:
        _LOG.info("Connecting to %s", hosts)
    else:
        _LOG.info("Starting Apple TV device discovery")

    # extra safety, if anything goes wrong here the reconnection logic is dead
    try:
        atvs = await pyatv.scan(loop, hosts=hosts)
        res = []

        for tv in atvs:
            # We only support Apple TV devices. Attention: HomePods are reported as TvOS!
            # https://github.com/unfoldedcircle/feature-and-bug-tracker/issues/173
            if tv.device_info.model in [
                # DeviceModel.Gen2,  # too old, doesn't support companion protocol. Additional work required.
                # DeviceModel.Gen3,  # "
                DeviceModel.Gen4,
                DeviceModel.Gen4K,
                DeviceModel.AppleTV4KGen2,
                DeviceModel.AppleTV4KGen3,
            ]:
                res.append(tv)

        return res
    except Exception as ex:  # pylint: disable=broad-exception-caught
        _LOG.error("Failed to start discovery: %s", ex)
        return []
