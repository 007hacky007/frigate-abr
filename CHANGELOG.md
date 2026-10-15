# Changelog

Notable changes to this overlay. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the versions are the
overlay's own, independent of the Frigate base each image wraps.

Release images are tagged `vX.Y.Z` (plus `-tensorrt` and `-rocm`) and never move.
`latest` and `frigate-<version>` keep tracking the newest build of master, so pin
a `v` tag when you want to stay on a known build.

## [Unreleased]

## [1.0.4] - 2026-10-15

### Fixed

- Restore the recording quality selector on iPhone and iPad. hls.js uses a
  blob-backed source element on iOS, which the overlay mistook for a preview.
- Keep the recording quality selector available while the video loads, so a
  slower connection can switch to a lower quality before playback starts.

## [1.0.3] - 2026-10-14

### Changed

- Update the Frigate base from 0.18.0 to 0.18.1 for all image variants,
  including upstream security fixes and recording and live playback fixes.

## [1.0.2] - 2026-10-03

### Fixed

- The quality gear no longer appears on preview clips or plain-file videos.

### Changed

- On desktop, the quality gear appears while hovering the player, focusing the
  selector, or keeping its menu open. It stays visible on touch devices.

## [1.0.1] - 2026-09-22

### Fixed

- The recording player on Safari, including every browser on iOS, offered playback
  speeds only up to 2x while other browsers go up to 16x. Frigate hard-codes the
  shorter list for Safari, a leftover from when Safari played recordings through
  native HLS; it now plays through hls.js like everything else, so the overlay
  serves the full 0.5x to 16x list, in the player and in the default playback
  rate setting. Browsers keep Frigate's JS bundles cached for a year, so one that
  already has them needs its website data cleared once to pick up the change.

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

[Unreleased]: https://github.com/007hacky007/frigate-abr/compare/v1.0.4...HEAD
[1.0.4]: https://github.com/007hacky007/frigate-abr/releases/tag/v1.0.4
[1.0.3]: https://github.com/007hacky007/frigate-abr/releases/tag/v1.0.3
[1.0.2]: https://github.com/007hacky007/frigate-abr/releases/tag/v1.0.2
[1.0.1]: https://github.com/007hacky007/frigate-abr/releases/tag/v1.0.1
[1.0.0]: https://github.com/007hacky007/frigate-abr/releases/tag/v1.0.0
