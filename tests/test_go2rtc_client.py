"""Tests for the go2rtc ABR variant client.

Run with: python3 -m pytest tests/test_go2rtc_client.py -v
"""

import sys
from pathlib import Path

# Add sidecar to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sidecar.go2rtc_client import make_variant_name, make_variant_source
from sidecar.transcoder import QualityTier


TIER = QualityTier(name="720p", width=1280, height=720, bitrate="1200k")


class TestVariantSource:
    def test_transcodes_video_to_tier_resolution(self):
        src = make_variant_source("front_door", TIER)
        assert src.startswith("ffmpeg:front_door")
        assert "#video=h264" in src
        assert "#width=1280" in src
        assert "#height=720" in src

    def test_includes_audio_directive(self):
        # Without an #audio directive go2rtc's ffmpeg source produces a
        # video-only stream, so live playback has no sound. Regression guard
        # for the "no audio on live transcoded stream" bug.
        src = make_variant_source("front_door", TIER)
        assert "#audio=" in src

    def test_source_references_base_camera_not_variant(self):
        # The transcode input must be the original camera stream, not the
        # variant's own name (which would be a self-referential loop).
        src = make_variant_source("front_door", TIER)
        assert make_variant_name("front_door", TIER) not in src
