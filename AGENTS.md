# Working on frigate-abr

A Docker overlay that adds on-demand ABR transcoding to Frigate NVR. The sidecar
(FastAPI, `sidecar/`) transcodes recording segments on request and serves its own
HLS playlist; `overlay/` patches nginx and injects the quality selector into
Frigate's UI. Nothing in Frigate itself is modified.

## Bump CACHE_VERSION when cached segments change

`CACHE_VERSION` in `sidecar/cache.py` must be bumped in any release that changes
what a cached segment contains: encoder arguments, container, timestamp handling,
anything a player could notice. The version is part of every cache key, and the
sidecar wipes the cache directory at startup when the recorded version no longer
matches. Skip the bump and users keep being served the old bytes until the TTL
expires them, which is how a fixed bug appears to survive its own fix.

Segments are cheap to recreate (a second or two each), so bump when in doubt.

## User-visible changes go in the changelog

Anything a user could notice - a fix, a new config key, changed behaviour - gets an
entry under `## [Unreleased]` in `CHANGELOG.md`, in the same commit as the change.

A release is cut by tagging `vX.Y.Z` on master. CI then builds the release images
and publishes the GitHub release with that version's changelog section as its
notes, so the notes cannot drift from the changelog. A version with no entries
fails the release job on purpose.

## Everything degrades gracefully

If the sidecar is unreachable or ABR is disabled, Frigate must work normally:
`inject.js` does nothing without a config, and `overlay/s6/abr-patch/run` always
exits 0 so a failed nginx patch can never stop nginx from starting.

## Tests

Sidecar dependencies are not in the system python, so use a venv:

```
python3 -m venv .venv && .venv/bin/pip install -r sidecar/requirements.txt pytest
.venv/bin/python -m pytest tests/ -q
node tests/test_inject.mjs
```

Tests that need real media are marked `needs_ffmpeg` and skip without ffmpeg and
ffprobe on PATH. CI does not run the tests, so run them before committing.

## Media gotchas

Recordings are variable frame rate: a camera that nominally sends 16 fps drifts by
a few hundred ms between frames. Anything that assumes a constant frame duration
breaks on them, quietly and only on some players. Encoders run with `-bf 0` for
exactly this reason (see the FAQ in README.md), and Safari is the browser that
punishes the resulting timestamp damage, so test playback changes on WebKit rather
than Chrome alone.
