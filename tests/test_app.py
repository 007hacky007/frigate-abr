"""Tests for the sidecar FastAPI app.

Run with: python3 -m pytest tests/test_app.py -v
"""

import asyncio
import sys
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
