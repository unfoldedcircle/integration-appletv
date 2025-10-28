# Apple TV integration for Remote Two/3

This integration is based on the great [pyatv](https://github.com/postlund/pyatv) library and uses our
[uc-integration-api](https://github.com/aitatoi/integration-python-library) to communicate with the Remote Two/3.
[Crowdin translations](https://crowdin.com/project/uc-integration-apple-tv).

The driver discovers Apple TV devices on the network and pairs them using AirPlay and companion protocols.
A [media player entity](https://github.com/unfoldedcircle/core-api/blob/main/doc/entities/entity_media_player.md)
and a [remote entity](https://github.com/unfoldedcircle/core-api/blob/main/doc/entities/entity_remote.md)
is exposed to the Remote Two/3.

‼️ Do not install this integration as a custom integration on the Remote, or it can interfere with the included version.  
Included integrations in the firmware cannot be updated manually. The integration can be run as an external integration
for testing and development.

Supported versions:
- Apple TV 4 and newer models with tvOS 16+

Supported attributes:
- State (on, off, playing, paused, unknown)
- Title
- Album
- Artist
- Artwork
- Media duration
- Media position
- Sound mode (list of output device(s) streamed to)

Supported commands:
- Turn on & off (device will be put into standby)
- Next / Previous
- Rewind / Fast-forward
- Volume up / down
- Mute toggle
- Play/pause
- Stop, play, pause
- Directional pad navigation and select
- Context menu
- Home screen
- Control center
- Launch application
- App switcher
- Start screensaver
- Stream audio to one or multiple output devices

Please note:
- Certain commands like channel up & down are app dependant and don't work with every app!
- Not every app provides media information and artwork.

## Requirements

Please also check the [pyatv troubleshooting](https://pyatv.dev/support/troubleshooting/) section for more information.

If you have trouble using this integration with your Apple TV, then please open an issue with us and not the 3rd party
[pyatv](https://github.com/postlund/pyatv) library! Unless of course you are a developer and can track down a specific
issue in the library with all the required information to reproduce it.

### Network

- The Apple TV device must be on the same network subnet as the Remote. Routed networks are not supported.
- [Zeroconf](https://en.m.wikipedia.org/wiki/Zero-configuration_networking) (multicast DNS) must be a allowed.  
  Check your WiFi access point and router that this traffic is not filtered out.
- When using DHCP: a static IP address reservation for the Apple TV device(s) is recommended.  
  This speeds up reconnection and helps to identify the device again if Apple changes the (not so) unique device identifiers. 

### Apple TV device

- Make sure you have  _"Allow Access"_ set to _"Anyone on the Same Network"_ for AirPlay on your Apple TV.
- When using multiple Apple TVs, each device should have a unique name for easier identification.  
  - The name can be set under Settings, General, About: Name  
- The name should not be changed anymore once the Apple TV is connected with this integration.  
  This helps to identify the device again if the device identifiers change after a tvOS update.
- Disabling automatic software updates is recommended, especially if you rely on controlling an Apple TV with an Unfolded Circle Remote.
  - tvOS updates might break certain functionality.
  - It can take time to fix these issues and release a new integration version.

## Usage

### Setup

- Requires Python 3.11
- Install required libraries:  
  (using a [virtual environment](https://docs.python.org/3/library/venv.html) is highly recommended)
```shell
pip3 install -r requirements.txt
```

- The integration is runnable without updating the language files or compiling the .po files!  
  If a language file is missing, the language key is used which in most cases is identical to the English language text.
- Optional: compile gettext translation files:
  - This requires `msgfmt` from the GNU gettext utilities.
  - See [docs/i18n.md](docs/i18n.md) for more information.
  - Helper Makefile:
  
```shell
cd intg-appletv/locales
make all
```

For running a separate integration driver on your network for Remote Two/3, the configuration in file
[driver.json](driver.json) needs to be changed:

- Set `driver_id` to a unique value, `uc_appletv_driver` is already used for the embedded driver in the firmware.
- Change `name` to easily identify the driver for discovery & setup  with Remote Two/3 or the web-configurator.
- Optionally add a `"port": 8090` field for the WebSocket server listening port.
    - Default port: `9090`
    - This is also overrideable with environment variable `UC_INTEGRATION_HTTP_PORT`

### Run

```shell
UC_CONFIG_HOME=./ python3 intg-appletv/driver.py
```

See available [environment variables](https://github.com/unfoldedcircle/integration-python-library#environment-variables)
in the Python integration library to control certain runtime features like listening interface and configuration directory.

The configuration file is loaded & saved from the path specified in the environment variable `UC_CONFIG_HOME`.
Otherwise, the `HOME` path is used or the working directory as fallback.

The client name prefix used for pairing can be set in ENV variable `UC_CLIENT_NAME`. The hostname is used by default.

## Build distribution binary

After some tests, turns out Python stuff on embedded is a nightmare. So we're better off creating a binary distribution
that has everything in it, including the Python runtime and all required modules and native libraries.

To do that, we use [PyInstaller](https://pyinstaller.org/), but it needs to run on the target architecture as
`PyInstaller` does not support cross compilation.

The `--onefile` option to create a one-file bundled executable should be avoided:
- Higher startup cost, since the wrapper binary must first extract the archive.
- Files are extracted to the /tmp directory on the device, which is an in-memory filesystem.  
  This will further reduce the available memory for the integration drivers!

### x86-64 Linux

On x86-64 Linux we need Qemu to emulate the aarch64 target platform:
```bash
sudo apt install qemu-system-arm binfmt-support qemu-user-static
docker run --rm --privileged multiarch/qemu-user-static --reset -p yes
```

Run PyInstaller:
```shell
docker run --rm --name builder \
    --platform=aarch64 \
    --user=$(id -u):$(id -g) \
    -v "$PWD":/workspace \
    docker.io/unfoldedcircle/r2-pyinstaller:3.11.13  \
    bash -c \
      "python -m pip install -r requirements.txt && \
      pyinstaller --clean --onedir --name intg-appletv \
        --add-data intg-appletv/locales:locales --collect-all zeroconf intg-appletv/driver.py"
```

### aarch64 Linux / Mac

On an aarch64 host platform, the build image can be run directly (and much faster):
```shell
docker run --rm --name builder \
    --user=$(id -u):$(id -g) \
    -v "$PWD":/workspace \
    docker.io/unfoldedcircle/r2-pyinstaller:3.11.13  \
    bash -c \
      "python -m pip install -r requirements.txt && \
      pyinstaller --clean --onedir --name intg-appletv \
        --add-data intg-appletv/locales:locales --collect-all zeroconf intg-appletv/driver.py"
```

## Versioning

We use [SemVer](http://semver.org/) for versioning. For the versions available, see the
[tags and releases in this repository](https://github.com/unfoldedcircle/integration-appletv/releases).

## Changelog

The major changes found in each new release are listed in the [changelog](CHANGELOG.md)
and under the GitHub [releases](https://github.com/unfoldedcircle/integration-appletv/releases).

## Contributions

Please read our [contribution guidelines](CONTRIBUTING.md) before opening a pull request.

## License

This project is licensed under the [**Mozilla Public License 2.0**](https://choosealicense.com/licenses/mpl-2.0/).
See the [LICENSE](LICENSE) file for details.
