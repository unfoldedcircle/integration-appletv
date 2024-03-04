# Apple TV integration for Remote Two Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

_Changes in the next release_

### Added
- New media-player entity features ([feature-and-bug-tracker#56](https://github.com/unfoldedcircle/feature-and-bug-tracker/issues/56)):
- Manual setup option with IP address.
### Fixed
- Only discover Apple TV devices and no longer HomePods ([feature-and-bug-tracker#173](https://github.com/unfoldedcircle/feature-and-bug-tracker/issues/173)).
- Improved disconnect & reconnect handling. This should prevent the reported BlockedStateError issues.
### Changed
- Updated pyatv client library to 0.14.5

---

