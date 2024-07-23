# Apple TV integration for Remote Two Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

_Changes in the next release_

### Changed
- Create a one-folder bundle with PyInstaller instead a one-file bundle to save resources.
- Change archive format to the custom integration installation archive.
- Change default `driver_id` value in `driver.json` to create a compatible custom installation archive.

---

## v0.14.0 - 2024-07-09
### Added
- Stream to output devices through sound mode selection. Contributed by @albaintor, thanks! ([#20](https://github.com/unfoldedcircle/integration-appletv/pull/20))

### Fixed
- Simple commands FAST_FORWARD_BEGIN and REWIND_BEGIN remained stuck in some apps. Fixed by @albaintor, thanks! ([#22](https://github.com/unfoldedcircle/integration-appletv/pull/22))

## v0.13.9 - 2024-06-14
### Added
- Simple commands for skip forward and backward, alternative FF/RW commands with companion protocol. Contributed by @albaintor, thanks! ([#19](https://github.com/unfoldedcircle/integration-appletv/pull/19))

## v0.13.8 - 2024-04-01
### Changed
- Use unique device name prefix for pairing to easily identify paired devices on Apple TV ([feature-and-bug-tracker#362](https://github.com/unfoldedcircle/feature-and-bug-tracker/issues/362)).

## v0.13.7 - 2024-03-18
### Fixed
- Prevent device power-on in standby with power-off command ([feature-and-bug-tracker#349](https://github.com/unfoldedcircle/feature-and-bug-tracker/issues/349)).

## v0.13.6 - 2024-03-13
### Changed
- Update ucapi library: filter out base64 image data in message logs.

## v0.13.5 - 2024-03-13
### Changed
- Use menu feature instead of settings for control-center ([feature-and-bug-tracker#56](https://github.com/unfoldedcircle/feature-and-bug-tracker/issues/56)).
- Enhance setup instructions that the device must be in the same network.

## v0.13.4 - 2024-03-09
### Fixed
- Invalid driver metadata file for the Remote Two ([feature-and-bug-tracker#340](https://github.com/unfoldedcircle/feature-and-bug-tracker/issues/340)).
- Temporary workaround for standby check ([#15](https://github.com/unfoldedcircle/integration-appletv/issues/15)).

## v0.13.3 - 2024-03-08
### Fixed
- Shuffle command parameter handling
### Changed
- Feature check for not always available commands, which would just time out otherwise.

## v0.13.2 - 2024-03-08
### Fixed
- Fast-forward and rewind commands.
- Automatically migrate old configuration file at startup and beginning of configuration flow, otherwise the device must be paired again.

## v0.13.1 - 2024-03-07
### Changed
- Limit discovery to Apple TV 4 and newer models. Generations 2 and 3 are not supported and would fail during pairing.
- Delayed return of initial setup-flow screen as a workaround for the web-configurator.

## v0.13.0 - 2024-03-06
### Added
- New media-player entity features ([feature-and-bug-tracker#56](https://github.com/unfoldedcircle/feature-and-bug-tracker/issues/56)):
  - context menu, control center, app switcher, top menu, rewind, fast-forward.
- Multi-device support ([#11](https://github.com/aitatoi/integration-appletv/issues/11), [feature-and-bug-tracker#118](https://github.com/unfoldedcircle/feature-and-bug-tracker/issues/118)).
- Enhanced reconfiguration and manual setup option with IP address.
- Updated German and French translations ([#6](https://github.com/aitatoi/integration-appletv/issues/6)).
### Fixed
- Only discover Apple TV devices and no longer HomePods ([feature-and-bug-tracker#173](https://github.com/unfoldedcircle/feature-and-bug-tracker/issues/173)).
- Improved disconnect & reconnect handling. This should prevent the reported BlockedStateError issues ([feature-and-bug-tracker#300](https://github.com/unfoldedcircle/feature-and-bug-tracker/issues/300)).
- Automatically wake up Apple TV if a command is sent while it is in standby.
### Changed
- Play/Pause will exit screensaver and continue playing paused media (tested with Apple TV+, YouTube).
- Updated pyatv client library to latest 0.14.5 release for common bug fixes and improvements.
- Major rewrite to support more features and to release it as open source project.
