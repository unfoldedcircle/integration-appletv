"""
This module holds global objects for the Apple TV integration.

:copyright: (c) 2023-2024 by Unfolded Circle ApS.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import asyncio
import sys

import tv
import ucapi.api as uc

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_LOOP)

api = uc.IntegrationAPI(_LOOP)
_configured_atvs: dict[str, tv.AppleTv] = {}
