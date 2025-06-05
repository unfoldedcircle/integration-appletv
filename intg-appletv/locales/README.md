# Internationalization (i18n)

This directory contains translation files for the Apple TV integration. The integration uses the Python [gettext](https://docs.python.org/3.11/library/gettext.html)
module for internationalization.

‼️Only the English .po file may be edited and committed to Git!
- All other languages are translated by Crowdin.
- Updated texts are pushed back as pull requests.

Translations included in the build:
- en_US (default)
- de_DE
- fr_FR

See [Crowdin](https://crowdin.com/project/uc-integration-apple-tv) for current translation progress.

How to enable additional translations:
1. Edit [Makefile](Makefile) and add new language(s) to: `LOCALES = de_DE fr_FR en_US`
2. Edit [i18n.py](../i18n.py) and add new language(s): `AVAILABLE_LANGUAGES = ["en_US", "de_DE", "fr_FR"]`

See [/docs/i18n.md](../../docs/i18n.md) for more information.
