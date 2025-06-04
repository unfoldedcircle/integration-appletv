# Code Guidelines

- Code line length: 120
- Use double quotes as default (don't mix and match for simple quoting, checked with pylint).
- Configuration:
    - `.pylint.rc` for pylint
    - `setup.cfg` for flake8 and isort

## Tooling

Install all code linting tools:

```shell
pip3 install -r test-requirements.txt
```

### Verify

The following tests are run as GitHub action for each push on the main branch and for pull requests.
They can also be run anytime on a local developer machine:
```shell
python -m pylint intg-appletv
python -m flake8 intg-appletv --count --show-source --statistics
python -m isort intg-appletv/. --check --verbose 
python -m black intg-appletv --check --verbose --line-length 120
```

Linting integration in PyCharm/IntelliJ IDEA:
1. Install plugin [Pylint](https://plugins.jetbrains.com/plugin/11084-pylint)
2. Open Pylint window and run a scan: `Check Module` or `Check Current File`

### Format Code
```shell
python -m black intg-appletv --line-length 120
```

PyCharm/IntelliJ IDEA integration:
1. Go to `Preferences or Settings -> Tools -> Black`
2. Configure:
- Python interpreter
- Use Black formatter: `On code reformat` & optionally `On save`
- Arguments: `--line-length 120`

### Sort Imports

```shell
python -m isort intg-appletv/.
```

## Language Texts

Only end-user texts must be prepared for translation with the Python [gettext](https://docs.python.org/3.11/library/gettext.html)
module. Helper functions are defined in [i18n.py](../intg-appletv/i18n.py).

- Do not translate log messages.
- See the setup-flow code on how to use language texts.
- For more information, see [i18n.md](i18n.md).

For a pull request, a properly updated English reference language file [intg-appletv/locales/en_US/LC_MESSAGES/intg-appletv.po](../intg-appletv/locales/en_US/LC_MESSAGES/intg-appletv.po)
is appreciated, but not mandatory. We understand that it can be a challenge if gettext hasn't been used before and the
development machine is Windows :-)

Minimally required are wrapped language texts in the Python code. A pull request reviewer can easily update the language
files after merging the PR if the translations are properly prepared in the Python code.

## Third-Party Libraries

The use of third-party libraries is encouraged when specific functionality is not provided by Python's standard library.
However, since this integration is included in the Remote firmware, the following requirements must be met:

### License Requirements

- Verify that the library's license is compatible with the project's [Mozilla Public License 2.0](https://choosealicense.com/licenses/mpl-2.0/).
- Prohibited licenses:
  - GPLv3 and its variants.
- The library's license information must be retrievable with the `pip-licenses` tool.

### Maintenance and Security

- Libraries must be actively maintained with:
  - Regular updates and bug fixes.
  - Updated dependencies.
  - Remain compatible with the Python version used in the integration.
- Specify version requirements:
  - Use semantic versioning in requirements.txt
  - Pin library version for stability.

### Approval Process

- The Unfolded Circle core team must approve any external library.  
  Please reach out before opening a pull request with a GitHub issue or a direct message.
- Approval documentation required in:
  - GitHub issue or pull request.
  - Include justification for the library.
  - List alternatives considered.
- Non-approved libraries will result in declined pull requests.
