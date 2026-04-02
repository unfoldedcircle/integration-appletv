#!/usr/bin/env python
# coding: utf-8
"""
This module implements the Orange TV communication of the Remote Two integration driver.

:copyright: (c) Albaintor
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""
# pylint: skip-file
# flake8: noqa
import asyncio
import logging
import sys
from typing import Any

from config import AtvDevice
from rich import print_json
from tv import EVENTS, AppleTv
from ucapi.media_player import Attributes

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_LOOP)

# 192.168.1.123
# 192.168.0.8

# OLD 192.168.1.35


async def on_device_update(device_id: str, update: dict[str, Any] | None) -> None:
    try:
        print_json(data=update)
    except Exception:
        _LOG.info(update)


async def main():
    _LOG.debug("Start connection")
    client = AppleTv(
        device=AtvDevice(  # 192.168.1.129 192.168.1.132
            identifier="5E:EB:AD:13:C3:81",
            name="Salon Apple TV",
            credentials=
            [{"protocol": "airplay",
              "credentials": "9998ce7860090ff57f6d10b9e542562da1023e135cbee04c64f33023e476c251:521dace8dab820e0d592133ef1dc4c039f83ac00c4bee7bd27710c96e780e1db:35444542414431332d433338312d343944422d413532322d463039343736353642454331:62633038393330362d303439612d343436382d613735322d616532626533643338663363"},
             {"protocol": "companion",
              "credentials": "9998ce7860090ff57f6d10b9e542562da1023e135cbee04c64f33023e476c251:9d2aa299d2deb5b2916059979ed9653752548443bc1b2ef63e66729b86f365d9:35444542414431332d433338312d343944422d413532322d463039343736353642454331:61316130303161612d323161622d343636392d626638332d623738613432373039613037"}],
            address="192.168.1.63",
            global_volume=True
        )
    )
    client.events.on(EVENTS.UPDATE, on_device_update)
    await client.connect()
    await asyncio.sleep(150)


if __name__ == "__main__":
    _LOG = logging.getLogger(__name__)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    ch = logging.StreamHandler()
    ch.setFormatter(formatter)
    logging.basicConfig(handlers=[ch])
    logging.getLogger("tv").setLevel(logging.DEBUG)
    logging.getLogger("test").setLevel(logging.DEBUG)
    _LOOP.run_until_complete(main())
    _LOOP.run_forever()
