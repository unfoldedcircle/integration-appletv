"""
Constants used for Apple TV integration.

:copyright: (c) 2026 by Unfolded Circle ApS.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

from enum import Enum, StrEnum
from typing import Any, Type


class AppleTVSelects(StrEnum):
    """Apple TV select values."""

    SELECT_APP = "select_app"


class AppleTVSensors(StrEnum):
    """Apple TV sensor values."""

    SENSOR_APP = "sensor_app"


def filter_attributes(attributes, attribute_type: Type[Enum]) -> dict[str, Any]:
    """Filter attributes based on an Enum class."""
    valid_keys = {e.value for e in attribute_type}
    return {k: v for k, v in attributes.items() if k in valid_keys}


def key_update_helper(input_attributes, key: str, value: str | None, attributes):
    """Return modified attributes only."""
    if value is None:
        return attributes

    if key in input_attributes:
        if input_attributes[key] != value:
            attributes[key] = value
    else:
        attributes[key] = value

    return attributes


def truncate_dict(data: dict[str, Any], max_len: int = 150) -> dict[str, Any]:
    """Truncate dictionary to max length."""
    return {k: (v[:max_len] + "..." if isinstance(v, str) and len(v) > max_len else v) for k, v in data.items()}
