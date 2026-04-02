"""
HID usage tables.

:copyright: (c) 2025 by Unfolded Circle ApS.
:license: MPL-2.0, see LICENSE for more details.
"""

from enum import Enum


class UsagePage(int, Enum):
    """HID usage pages.

    More information: https://www.usb.org/sites/default/files/hut1_6.pdf
    """

    GENERIC_DESKTOP = 0x01
    KEYBOARD = 0x07
    CONSUMER = 0x0C
