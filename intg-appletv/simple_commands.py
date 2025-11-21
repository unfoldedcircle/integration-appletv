"""
Apple TV command definitions.

:copyright: (c) 2023-2024 by Unfolded Circle ApS.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

from enum import Enum


class SimpleCommands(str, Enum):
    """Additional simple commands of the Apple TV not covered by media-player features."""

    TOP_MENU = "TOP_MENU"
    """Go to home screen."""
    APP_SWITCHER = "APP_SWITCHER"
    """Show running applications."""
    SCREENSAVER = "SCREENSAVER"
    """Run screensaver."""
    SKIP_FORWARD = "SKIP_FORWARD"
    """Skip forward a time interval."""
    SKIP_BACKWARD = "SKIP_BACKWARD"
    """Skip forward a time interval."""
    FAST_FORWARD_BEGIN = "FAST_FORWARD_BEGIN"
    """Fast forward using Companion protocol."""
    REWIND_BEGIN = "REWIND_BEGIN"
    """Rewind using Companion protocol."""
    SWIPE_LEFT = "SWIPE_LEFT"
    """Swipe left using Companion protocol."""
    SWIPE_RIGHT = "SWIPE_RIGHT"
    """Swipe right using Companion protocol."""
    SWIPE_UP = "SWIPE_UP"
    """Swipe up using Companion protocol."""
    SWIPE_DOWN = "SWIPE_DOWN"
    """Swipe down using Companion protocol."""
    PLAY = "PLAY"
    """Send play command. App specific! Some treat it as play_pause."""
    PAUSE = "PAUSE"
    """Send pause command. App specific! Some treat it as play_pause."""
    PLAY_PAUSE_KEY = "PLAY_PAUSE_KEY"
    """Alternative play/pause command by sending a HID key press."""
