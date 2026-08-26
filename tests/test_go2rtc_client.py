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


# --- Live bitrate enforcement (#raw=-b:v ...) ---

from sidecar.go2rtc_client import find_missing_variants, is_valid_bitrate  # noqa: E402


class TestBitrateEnforcement:
    def test_raw_bitrate_args_appended(self):
        src = make_variant_source("front_door", TIER)
        assert "#raw=-b:v 1200k -maxrate 1200k -bufsize 2400k" in src

    def test_raw_comes_after_template_params(self):
        # go2rtc parses #-separated params; raw must not swallow width/height.
        src = make_variant_source("front_door", TIER)
        assert src.index("#raw=") > src.index("#audio=copy")

    def test_kill_switch_disables_raw(self):
        src = make_variant_source("front_door", TIER, enforce_bitrate=False)
        assert "#raw" not in src

    def test_invalid_bitrate_skips_raw_and_warns(self, caplog):
        bad = QualityTier(name="480p", width=854, height=480, bitrate="300kbps")
        with caplog.at_level("WARNING"):
            src = make_variant_source("cam", bad)
        assert "#raw" not in src
        assert any("bitrate" in r.message.lower() for r in caplog.records)

    def test_fractional_megabit_normalized(self):
        t = QualityTier(name="1080p", width=1920, height=1080, bitrate="2.5M")
        src = make_variant_source("cam", t)
        assert "#raw=-b:v 2500k -maxrate 2500k -bufsize 5000k" in src


class TestBitrateValidation:
    def test_accepts_common_forms(self):
        for v in ("300k", "1200K", "2M", "2.5M", "800000"):
            assert is_valid_bitrate(v), v

    def test_rejects_garbage(self):
        for v in ("300kbps", "fast", "", None, "-b:v 1k", "{input}", "1 200k"):
            assert not is_valid_bitrate(v), v


# --- Reconciliation: re-register variants lost to go2rtc restarts ---

class TestFindMissingVariants:
    TIERS = [
        QualityTier(name="720p", width=1280, height=720, bitrate="1200k"),
        QualityTier(name="480p", width=854, height=480, bitrate="300k"),
    ]

    def test_all_present_returns_empty(self):
        streams = {
            "cam1": {}, "cam1_abr_720p": {}, "cam1_abr_480p": {},
        }
        assert find_missing_variants(streams, self.TIERS) == []

    def test_lost_variant_is_reported(self):
        streams = {"cam1": {}, "cam1_abr_720p": {}}
        missing = find_missing_variants(streams, self.TIERS)
        assert [(c, t.name) for c, t in missing] == [("cam1", "480p")]

    def test_new_camera_gets_all_variants(self):
        streams = {"cam1": {}, "cam1_abr_720p": {}, "cam1_abr_480p": {}, "cam2": {}}
        missing = find_missing_variants(streams, self.TIERS)
        assert [(c, t.name) for c, t in missing] == [("cam2", "720p"), ("cam2", "480p")]

    def test_birdseye_and_variants_not_treated_as_cameras(self):
        streams = {"birdseye": {}, "cam1_abr_720p": {}}
        assert find_missing_variants(streams, self.TIERS) == []
