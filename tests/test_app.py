"""Tests for the sidecar FastAPI app.

Run with: python3 -m pytest tests/test_app.py -v
"""

import asyncio
import sys
from unittest import mock

import pytest
from pathlib import Path

# Add sidecar to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sidecar import app as app_module
from sidecar.transcoder import QualityTier


def _fake_vod_response(num_clips: int) -> dict:
    return {
        "sequences": [{"clips": [{"path": f"/clip{i}.mp4"} for i in range(num_clips)]}],
        "durations": [9700 + i for i in range(num_clips)],
    }


class TestVodPlaylist:
    def _get_playlist(self, monkeypatch, quality="1080p"):
        monkeypatch.setattr(app_module, "transcoder", object())
        monkeypatch.setattr(
            app_module,
            "tiers",
            [QualityTier(name="1080p", width=1920, height=1080, bitrate="2500k")],
        )

        async def fake_vod(camera_name, start_ts, end_ts):
            return _fake_vod_response(3)

        monkeypatch.setattr(app_module, "_fetch_frigate_vod", fake_vod)
        resp = asyncio.run(
            app_module.vod_abr_playlist("vchod", 1787392800, 1787396400, quality)
        )
        return resp.body.decode()

    def test_segment_uris_are_relative(self, monkeypatch):
        """Segment URIs must be relative so they resolve against the playlist
        URL. Root-absolute URIs (/abr/hls/...) break behind path-prefix
        reverse proxies like Home Assistant ingress (frigate-proxy), where
        the playlist lives under /api/hassio_ingress/<token>/."""
        body = self._get_playlist(monkeypatch)
        segment_lines = [
            line for line in body.splitlines() if not line.startswith("#") and line
        ]
        assert segment_lines, "playlist should contain segment URIs"
        for line in segment_lines:
            assert not line.startswith("/"), (
                f"segment URI must be relative, got: {line}"
            )

    def test_segment_uris_resolve_to_segment_endpoint(self, monkeypatch):
        body = self._get_playlist(monkeypatch)
        assert "segment/0.ts?quality=1080p" in body
        assert "segment/2.ts?quality=1080p" in body

    def test_playlist_structure(self, monkeypatch):
        body = self._get_playlist(monkeypatch)
        assert body.startswith("#EXTM3U")
        assert "#EXT-X-PLAYLIST-TYPE:VOD" in body
        assert body.rstrip().endswith("#EXT-X-ENDLIST")

    def test_playlist_has_no_per_segment_discontinuity(self, monkeypatch):
        """Segments are served with continuous timestamps (the playlist
        offset is applied at serve time), so the playlist must not mark
        every segment boundary as a discontinuity."""
        body = self._get_playlist(monkeypatch)
        assert "#EXT-X-DISCONTINUITY" not in body


from sidecar.transcoder import ABRTranscoder  # noqa: E402
from tests.ts_helpers import FFMPEG, first_pts, make_ts, needs_ffmpeg  # noqa: E402


@needs_ffmpeg
class TestVodSegmentTimestamps:
    def test_segment_pts_offset_by_preceding_durations(self, monkeypatch, tmp_path):
        """Segment N must start where segments 0..N-1 end on the playlist
        timeline, so hls.js sees one continuous stream instead of a
        timestamp reset at every segment."""
        src = make_ts(tmp_path / "clip.ts")
        transcoder = ABRTranscoder(
            ffmpeg_path=FFMPEG, hwaccel_preset="default", gpu=0,
            cache_dir=str(tmp_path / "cache"),
        )

        async def fake_get_or_transcode(recording_path, tier, clip_from_ms=None, duration_ms=None):
            return str(src)

        monkeypatch.setattr(transcoder, "get_or_transcode", fake_get_or_transcode)
        monkeypatch.setattr(app_module, "transcoder", transcoder)
        monkeypatch.setattr(
            app_module, "tiers",
            [QualityTier(name="480p", width=854, height=480, bitrate="500k")],
        )

        async def fake_vod(camera_name, start_ts, end_ts):
            return {
                "sequences": [{"clips": [{"path": "/a.mp4"}, {"path": "/b.mp4"}, {"path": "/c.mp4"}]}],
                "durations": [1000, 1500, 1000],
            }

        monkeypatch.setattr(app_module, "_fetch_frigate_vod", fake_vod)

        resp = asyncio.run(app_module.vod_abr_segment("cam", 1, 2, 2, "480p"))
        out = tmp_path / "served.ts"
        out.write_bytes(resp.body if hasattr(resp, "body") else Path(resp.path).read_bytes())

        assert first_pts(out) == pytest.approx(first_pts(src) + 2.5, abs=0.001)


class TestFfmpegPathDetection:
    """The sidecar must transcode with the same ffmpeg build Frigate uses.

    Frigate advertises its bundled default in DEFAULT_FFMPEG_VERSION. That
    default moved from 7.0 to 8.0 on x86_64 in Frigate 0.18 (and to "rpi" on
    arm64) while 7.0 stayed bundled, so a hard-coded probe list keeps working
    yet silently pins the sidecar to the older build.
    """

    def _detect(self, monkeypatch, present, env=None, config=None, frigate_cfg=None):
        present = set(present)
        monkeypatch.setattr(app_module.os.path, "exists", lambda p: p in present)
        if env is None:
            monkeypatch.delenv("DEFAULT_FFMPEG_VERSION", raising=False)
        else:
            monkeypatch.setenv("DEFAULT_FFMPEG_VERSION", env)
        if frigate_cfg is not None:
            monkeypatch.setattr(app_module.yaml, "safe_load", lambda f: frigate_cfg)
        with mock.patch("builtins.open", mock.mock_open(read_data="")):
            return app_module.detect_ffmpeg_path(config or {})

    def test_follows_bundled_default_over_older_bundled_version(self, monkeypatch):
        """A 0.18 image bundles 7.0 alongside its 8.0 default; take the 8.0."""
        assert self._detect(
            monkeypatch,
            present=[
                "/usr/lib/ffmpeg/8.0/bin/ffmpeg",
                "/usr/lib/ffmpeg/7.0/bin/ffmpeg",
                "/usr/lib/ffmpeg/5.0/bin/ffmpeg",
            ],
            env="8.0",
        ) == "/usr/lib/ffmpeg/8.0/bin/ffmpeg"

    def test_follows_non_numeric_bundled_default(self, monkeypatch):
        """arm64 images name their default build "rpi" rather than a version."""
        assert self._detect(
            monkeypatch,
            present=[
                "/usr/lib/ffmpeg/rpi/bin/ffmpeg",
                "/usr/lib/ffmpeg/8.0/bin/ffmpeg",
                "/usr/lib/ffmpeg/7.0/bin/ffmpeg",
            ],
            env="rpi",
        ) == "/usr/lib/ffmpeg/rpi/bin/ffmpeg"

    def test_older_base_still_resolves_to_its_default(self, monkeypatch):
        assert self._detect(
            monkeypatch,
            present=[
                "/usr/lib/ffmpeg/7.0/bin/ffmpeg",
                "/usr/lib/ffmpeg/5.0/bin/ffmpeg",
            ],
            env="7.0",
        ) == "/usr/lib/ffmpeg/7.0/bin/ffmpeg"

    def test_probe_list_covers_ffmpeg_8_without_the_env_var(self, monkeypatch):
        """Bases that advertise nothing must still not skip past ffmpeg 8."""
        assert self._detect(
            monkeypatch,
            present=[
                "/usr/lib/ffmpeg/8.0/bin/ffmpeg",
                "/usr/lib/ffmpeg/7.0/bin/ffmpeg",
            ],
            env=None,
        ) == "/usr/lib/ffmpeg/8.0/bin/ffmpeg"

    def test_unusable_bundled_default_falls_through_to_probe(self, monkeypatch):
        assert self._detect(
            monkeypatch,
            present=["/usr/lib/ffmpeg/7.0/bin/ffmpeg"],
            env="9.9",
        ) == "/usr/lib/ffmpeg/7.0/bin/ffmpeg"

    def test_frigate_config_version_wins_over_bundled_default(self, monkeypatch):
        assert self._detect(
            monkeypatch,
            present=[
                "/config/config.yml",
                "/usr/lib/ffmpeg/8.0/bin/ffmpeg",
                "/usr/lib/ffmpeg/7.0/bin/ffmpeg",
            ],
            env="8.0",
            frigate_cfg={"ffmpeg": {"path": "7.0"}},
        ) == "/usr/lib/ffmpeg/7.0/bin/ffmpeg"

    def test_frigate_config_version_no_longer_bundled_falls_back(self, monkeypatch):
        """Frigate drops bundled ffmpeg versions between releases (5.0 goes in
        0.19) and falls back to its default for an alias it no longer ships.
        Returning a path that does not exist would fail every transcode."""
        assert self._detect(
            monkeypatch,
            present=[
                "/config/config.yml",
                "/usr/lib/ffmpeg/8.0/bin/ffmpeg",
            ],
            env="8.0",
            frigate_cfg={"ffmpeg": {"path": "5.0"}},
        ) == "/usr/lib/ffmpeg/8.0/bin/ffmpeg"

    def test_custom_absolute_path_is_untouched(self, monkeypatch):
        assert self._detect(
            monkeypatch,
            present=["/config/config.yml", "/usr/lib/ffmpeg/8.0/bin/ffmpeg"],
            env="8.0",
            frigate_cfg={"ffmpeg": {"path": "/opt/custom/ffmpeg"}},
        ) == "/opt/custom/ffmpeg"

    def test_explicit_sidecar_config_wins(self, monkeypatch):
        assert self._detect(
            monkeypatch,
            present=["/usr/lib/ffmpeg/8.0/bin/ffmpeg"],
            env="8.0",
            config={"ffmpeg_path": "/somewhere/ffmpeg"},
        ) == "/somewhere/ffmpeg"

    def test_last_resort_when_nothing_is_bundled(self, monkeypatch):
        assert self._detect(monkeypatch, present=[], env=None) == "/usr/bin/ffmpeg"
