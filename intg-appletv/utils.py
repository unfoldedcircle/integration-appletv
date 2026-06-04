"""
Utility functions used for Apple TV integration.

:copyright: (c) 2026 by Unfolded Circle ApS.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

from enum import Enum
import re
from typing import Any

_BAD_WHITESPACE_RE = re.compile(r"[\f\n\r\t\v\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]")


def replace_bad_chars(value: str) -> str:
    """Normalize unusual whitespace characters in a string to a regular space."""
    if not value:
        return value
    return _BAD_WHITESPACE_RE.sub(" ", value)


def filter_attributes(attributes: dict[str, Any], attribute_type: type[Enum]) -> dict[str, Any]:
    """Filter attributes based on an Enum class."""
    valid_keys = {e.value for e in attribute_type}
    return {k: v for k, v in attributes.items() if k in valid_keys}


def truncate_dict(data: dict[str, Any], max_len: int = 150) -> dict[str, Any]:
    """Return a copy of the dictionary with truncated values to given max length for logging."""
    return {k: (v[:max_len] + "..." if isinstance(v, str) and len(v) > max_len else v) for k, v in data.items()}


def key_update_helper(
    key: str,
    value: Any,
    attributes: dict[str, Any],
    original_attributes: dict[str, Any],
) -> dict[str, Any]:
    """
    Update the given key in the ``attributes`` dictionary with the specified value if required.

    The function compares the value of the key in the original attributes and updates it in
    attributes only if it is different.

    If the value is None, the original attributes dictionary is returned unmodified.

    :param key: The key whose value needs to be updated.
    :param value: The new value to associate with the key. It can be a string, list, or None.
    :param attributes: The dictionary where the key-value pair might be updated.
    :param original_attributes: The reference dictionary to check the current value of the key.
    :return: The updated ``attributes`` dictionary.
    :rtype: dict
    """
    if value is None:
        return attributes

    if key in original_attributes:
        if original_attributes[key] != value:
            attributes[key] = value
    else:
        attributes[key] = value

    return attributes
