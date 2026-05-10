"""
Utility functions used for Apple TV integration.

:copyright: (c) 2026 by Unfolded Circle ApS.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

from enum import Enum
from typing import Any, Type


def filter_attributes(attributes, attribute_type: Type[Enum]) -> dict[str, Any]:
    """Filter attributes based on an Enum class."""
    valid_keys = {e.value for e in attribute_type}
    return {k: v for k, v in attributes.items() if k in valid_keys}


def truncate_dict(data: dict[str, Any], max_len: int = 150) -> dict[str, Any]:
    """Return a copy of the dictionary with truncated values to given max length for logging."""
    return {k: (v[:max_len] + "..." if isinstance(v, str) and len(v) > max_len else v) for k, v in data.items()}


def key_update_helper(key: str, value: str | list[Any] | None, attributes: dict, original_attributes: dict[str, Any]):
    """Update the attribute dictionary with the given key and value."""
    if value is None:
        return attributes

    if key in original_attributes:
        if original_attributes[key] != value:
            attributes[key] = value
    else:
        attributes[key] = value

    return attributes
