"""
Internationalization support for the Apple TV integration.

This module provides functions for translating strings in the integration.
It uses gettext for internationalization and proper pluralization support.
This implementation is compatible with Crowdin for translation management.

:copyright: (c) 2025 by Unfolded Circle ApS.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

from gettext import NullTranslations, translation
from pathlib import Path
from typing import Any

# Define the available languages
AVAILABLE_LANGUAGES = ["en_US", "de_DE", "es_ES", "fr_FR", "nl_NL", "sv_SE"]
DEFAULT_LANGUAGE = "en_US"

# Path to the locales directory (relative to the package)
LOCALE_DIR = Path(__file__).parent / "locales"

# Cache for translators to avoid creating them multiple times
_translators: dict[str, NullTranslations] = {}

_current_language = DEFAULT_LANGUAGE


def setup_i18n(locale_dir: str | None = None) -> None:
    """
    Set up the internationalization system.

    This function should be called at the start of the application to ensure
    the locales directory exists and is properly set up.

    :param locale_dir: Optional path to the ``locales`` directory. If not provided,
                      the default locales directory will be used.
    """
    global LOCALE_DIR

    if locale_dir:
        LOCALE_DIR = Path(locale_dir)

    # Ensure the locales directory exists
    LOCALE_DIR.mkdir(parents=True, exist_ok=True)

    # Create language directories if they don't exist
    for lang in AVAILABLE_LANGUAGES:
        lang_dir = LOCALE_DIR / lang / "LC_MESSAGES"
        lang_dir.mkdir(parents=True, exist_ok=True)


def set_language(language: str) -> None:
    """
    Set the current language for translations.

    :param language: The language code to use (e.g., 'en', 'de', 'fr')
    """
    global _current_language

    _current_language = language if language in AVAILABLE_LANGUAGES else DEFAULT_LANGUAGE


def get_translator(language: str | None = None) -> NullTranslations:
    """
    Get a translator for the specified language.

    :param language: The language code to use. If not provided, the current language will be used.
    :return: A translator object for the specified language
    """
    lang = language or _current_language

    if lang not in _translators:
        try:
            _translators[lang] = translation("intg-appletv", localedir=str(LOCALE_DIR), languages=[lang], fallback=True)
        except (FileNotFoundError, OSError):
            # Fallback to NullTranslations if the translation file doesn't exist
            _translators[lang] = NullTranslations()

    return _translators[lang]


def gettext(message: str) -> str:
    """
    Translate a message using the current language.

    :param message: The message to translate
    :return: The translated message
    """
    translator = get_translator()
    return translator.gettext(message)


def ngettext(singular: str, plural: str, n: int) -> str:
    """
    Translate a singular/plural message using the current language.

    This function handles proper pluralization based on the count and language rules.

    :param singular: The singular form of the message
    :param plural: The plural form of the message
    :param n: The count that determines whether to use singular or plural
    :return: The translated message with proper pluralization
    """
    translator = get_translator()
    return translator.ngettext(singular, plural, n)


def i18all(message: str) -> dict[str, str]:
    """
    Create a translation dictionary for all available languages.

    This is a helper function to create dictionaries in the format expected by the Core-API for setup-flow messages.
    All language texts must be included as key value pairs, and not just a translation of the message. Example:

    .. code-block:: json

        {
          "label": {
            "en": "Good morning",
            "fr": "Bonjour",
            "de": "Guten Morgen"
          }
        }

    This might change in the future and can be easily adapted when necessary with the defined shorthand functions:
    just replace the custom ``_a`` and ``_am`` functions with the common ``_`` translation function.

    :param message: message to translate
    :return: A dictionary with language codes as keys and translated messages as values
    """
    result = {}
    for lang in AVAILABLE_LANGUAGES:
        translator = get_translator(lang)
        result[lang] = translator.gettext(message)
    return result


def i18all_format(message: str, **kwargs: Any) -> dict[str, str]:
    """
    Create a translation dictionary for all available languages with a formatting operation.

    The formatting operation is applied on every language text with the ``str.format()`` function.
    This is a helper function to create dictionaries in the format expected by the Core-API for setup-flow messages.
    All language texts must be included as key value pairs, and not just a translation of the message. Example:

    .. code-block:: json

        {
          "label": {
            "en": "Good morning $NAME",
            "fr": "Bonjour $NAME",
            "de": "Guten Morgen $NAME"
          }
        }

    The above JSON structure can be built in Python the following way:

    .. code-block:: python

        msg = {
          "label": i18all_format("Good morning {name}", name="Jane")
        }

    :param kwargs: map arguments to the formatting operation
    :param message: message to translate
    :return: A dictionary with language codes as keys and translated messages as values
    """
    result = {}
    for lang in AVAILABLE_LANGUAGES:
        translator = get_translator(lang)
        result[lang] = translator.gettext(message).format_map(kwargs)
    return result


def i18all_multi(*args: str) -> dict[str, str]:
    """
    Create a translation dictionary for all available languages with multiple messages.

    All messages will be concatenated into a single string. This can be used for splitting up longer paragraphs or text
    blocks for translation.

    This is a helper function to create dictionaries in the format expected by the Core-API for setup-flow messages.

    :param args: One or more messages to translate
    :return: A dictionary with language codes as keys and translated messages as values
    """
    result = {}
    for lang in AVAILABLE_LANGUAGES:
        translator = get_translator(lang)
        translated_messages = [translator.gettext(message) for message in args]
        result[lang] = "".join(translated_messages)

    return result


def echo(message: str) -> str:
    """
    Marker function for gettext text extraction, the message is returned unchanged.

    This is only used to extract the translation strings for the i18n module and in combination with the ``_am``
    function.
    :param message: message in the default language for xgettext extraction, returned unchanged.
    :return:
    """
    return message


# Define shorthand functions for easier use
_ = gettext
__ = echo
_n = ngettext
_a = i18all
_af = i18all_format
_am = i18all_multi

# Expose the underscore-prefixed shorthand aliases as part of the public API.
__all__ = [
    "AVAILABLE_LANGUAGES",
    "DEFAULT_LANGUAGE",
    "_",
    "__",
    "_a",
    "_af",
    "_am",
    "_n",
    "echo",
    "get_translator",
    "gettext",
    "i18all",
    "i18all_format",
    "i18all_multi",
    "ngettext",
    "set_language",
    "setup_i18n",
]

# Initialize the i18n system
setup_i18n()
