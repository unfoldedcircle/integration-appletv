"""
Helper functions.

:copyright: (c) 2023 by Unfolded Circle ApS.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

from typing import Any


def key_update_helper(key: str, value: str | None, attributes: dict, original_attributes: dict[str, Any]):
    """Update the attributes dictionary with the given key and value."""
    if value is None:
        return attributes

    if key in original_attributes:
        if original_attributes[key] != value:
            attributes[key] = value
    else:
        attributes[key] = value

    return attributes
