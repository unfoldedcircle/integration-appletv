# Apple TV integration for Remote Two Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

_Changes in the next release_

### Fixed
- Fast-forward and rewind commands.
- Automatically migrate old configuration file at startup and configuration flow, otherwise the device must be paired again.

---

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
