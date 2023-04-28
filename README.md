# AppleTV integration for Remote Two

Using [pyatv](https://github.com/postlund/pyatv) and [uc-integration-api](https://github.com/aitatoi/integration-python-library)

The driver discovers AppleTVs on the network and pairs them using AirPlay protocol. A media player entity is exposed to the core.

Supported versions:
- TvOS 16+

Supported attributes:
- State (on, off, playing, paused, unknown)
- Title
- Album
- Artist
- Artwork
- Media duration
- Media position

Supported commands:
- Turn on
- Turn off
- Play/pause
