# Apple TV integration for Remote Two

Using [pyatv](https://github.com/postlund/pyatv) and [uc-integration-api](https://github.com/aitatoi/integration-python-library)

The driver discovers Apple TV devices on the network and pairs them using AirPlay and companion protocols.
A [media player entity](https://github.com/unfoldedcircle/core-api/blob/main/doc/entities/entity_media_player.md)
is exposed to the Remote Two.

Supported versions:
- Apple TV 4 and newer models with TvOS 16+

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
- Play/pause
- Directional pad navigation and select
- Context menu
- Home screen
- Control center
- Launch application
- App switcher
- Start screensaver
- Stream audio to one or multiple output devices

Please note that certain commands like channel up & down are app dependant and don't work with every app!

## Usage
### Setup

- Requires Python 3.11
- Install required libraries:  
  (using a [virtual environment](https://docs.python.org/3/library/venv.html) is highly recommended)
```shell
pip3 install -r requirements.txt
```

For running a separate integration driver on your network for Remote Two, the configuration in file
[driver.json](driver.json) needs to be changed:

- Set `driver_id` to a unique value, `uc_appletv_driver` is already used for the embedded driver in the firmware.
- Change `name` to easily identify the driver for discovery & setup  with Remote Two or the web-configurator.
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
sudo apt install qemu binfmt-support qemu-user-static
docker run --rm --privileged multiarch/qemu-user-static --reset -p yes
```

Run PyInstaller:
```shell
docker run --rm --name builder \
    --platform=aarch64 \
    --user=$(id -u):$(id -g) \
    -v "$PWD":/workspace \
    docker.io/unfoldedcircle/r2-pyinstaller:3.11.6  \
    bash -c \
      "python -m pip install -r requirements.txt && \
      pyinstaller --clean --onedir --name intg-appletv --collect-all zeroconf intg-appletv/driver.py"
```

### aarch64 Linux / Mac

On an aarch64 host platform, the build image can be run directly (and much faster):
```shell
docker run --rm --name builder \
    --user=$(id -u):$(id -g) \
    -v "$PWD":/workspace \
    docker.io/unfoldedcircle/r2-pyinstaller:3.11.6  \
    bash -c \
      "python -m pip install -r requirements.txt && \
      pyinstaller --clean --onedir --name intg-appletv --collect-all zeroconf intg-appletv/driver.py"
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
