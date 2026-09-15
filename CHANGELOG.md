# Changelog

Notable changes to this overlay. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the versions are the
overlay's own, independent of the Frigate base each image wraps.

Release images are tagged `vX.Y.Z` (plus `-tensorrt` and `-rocm`) and never move.
`latest` and `frigate-<version>` keep tracking the newest build of master, so pin
a `v` tag when you want to stay on a known build.

## [Unreleased]

## [1.0.0] - 2026-09-15

First tagged release, built on Frigate 0.18.0. Earlier history is in the git log.

### Fixed

- ABR recording playback on Safari and other WebKit browsers, which showed a
  spinner that never resolved or a picture frozen after a few frames while audio
  kept playing. Recordings are variable frame rate, and a reordering encoder
  derives each B-frame's decode timestamp from a constant frame duration, so a
  long frame interval produced a timestamp the mpegts muxer had to repair by
  bumping it one tick past the previous one. WebKit stops decoding at those
  samples. Chrome does not, which is why the tiers looked fine everywhere else.

### Added

- Cached segments record the `CACHE_VERSION` that produced them, and the sidecar
  drops a cache left behind by an incompatible build on its first start.
- `cache.clear_on_start` (default `false`) starts every container with an empty
  cache.
- `AGENTS.md`, covering the cache version rule and the variable frame rate traps.

### Changed

- Every encode template disables B-frames. At the tier bitrate caps this costs no
  measurable quality, but the same picture takes roughly 10 to 18 percent more
  bits, so segments now sit closer to their cap.

[Unreleased]: https://github.com/007hacky007/frigate-abr/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/007hacky007/frigate-abr/releases/tag/v1.0.0
