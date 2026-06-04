"""
Apple TV device discovery.

:copyright: (c) 2023-2024 by Unfolded Circle ApS.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

from asyncio import AbstractEventLoop
import logging

import pyatv
from pyatv.const import DeviceModel

_LOG = logging.getLogger(__name__)


async def apple_tvs(
    loop: AbstractEventLoop, identifier: str | set[str] | None = None, hosts: list[str] | None = None
) -> list[pyatv.interface.BaseConfig]:
    """Discover Apple TVs on the network using pyatv.scan."""
    if hosts:
        _LOG.info("Connecting to %s", hosts)
    else:
        _LOG.info("Starting Apple TV device discovery")

    # extra safety, if anything goes wrong here the reconnection logic is dead
    try:
        atvs = await pyatv.scan(loop, identifier=identifier, hosts=hosts)
        # We only support Apple TV devices. Attention: HomePods are reported as TvOS!
        # https://github.com/unfoldedcircle/feature-and-bug-tracker/issues/173
        supported_models = {
            # DeviceModel.Gen2,  # too old, doesn't support companion protocol. Additional work required.
            # DeviceModel.Gen3,  # "
            DeviceModel.Gen4,
            DeviceModel.Gen4K,
            DeviceModel.AppleTV4KGen2,
            DeviceModel.AppleTV4KGen3,
        }
        return [tv for tv in atvs if tv.device_info is not None and tv.device_info.model in supported_models]  # pyright: ignore[reportUnnecessaryComparison]
    except Exception as ex:  # noqa: BLE001 — broad catch retained for resilience
        _LOG.error("Failed to start discovery: %s", ex)
        return []
