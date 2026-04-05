"""Tests for the ABR transcoder module.

Run with: python3 -m pytest tests/test_transcoder.py -v
"""

import sys
from pathlib import Path

# Add sidecar to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sidecar.transcoder import (
    ABRTranscoder,
    HWACCEL_TEMPLATES,
    QualityTier,
    _parse_bitrate_kbps,
)


class TestParseBitrate:
    def test_kbps(self):
        assert _parse_bitrate_kbps("2000k") == 2000

    def test_mbps(self):
        assert _parse_bitrate_kbps("4M") == 4000

    def test_plain_number(self):
        assert _parse_bitrate_kbps("1500") == 1500

    def test_with_whitespace(self):
        assert _parse_bitrate_kbps("  2000k  ") == 2000

    def test_fractional_mbps(self):
        assert _parse_bitrate_kbps("2.5M") == 2500


class TestHwaccelTemplates:
    """Verify all hwaccel templates have required keys and valid format strings."""

    def test_all_templates_have_required_keys(self):
        for name, template in HWACCEL_TEMPLATES.items():
            assert "decode" in template, f"{name} missing 'decode'"
            assert "scale" in template, f"{name} missing 'scale'"
            assert "encode" in template, f"{name} missing 'encode'"

    def test_default_template_exists(self):
        assert "default" in HWACCEL_TEMPLATES

    def test_nvidia_template_exists(self):
        assert "preset-nvidia" in HWACCEL_TEMPLATES

    def test_vaapi_template_exists(self):
        assert "preset-vaapi" in HWACCEL_TEMPLATES

    def test_templates_format_without_error(self):
        params = {
            "gpu": "0",
            "w": "1280",
            "h": "720",
            "bitrate": "2000k",
            "maxrate": "2000k",
            "bufsize": "4000k",
        }
        for name, template in HWACCEL_TEMPLATES.items():
            # Should not raise KeyError or ValueError
            template["decode"].format(**params)
            template["scale"].format(**params)
            template["encode"].format(**params)


class TestCachePath:
    def test_deterministic(self, tmp_path):
        t = ABRTranscoder("/usr/bin/ffmpeg", "default", 0, str(tmp_path))
        tier = QualityTier("720p", 1280, 720, "2000k")
        p1 = t.cache_path_for("/recordings/a.mp4", tier)
        p2 = t.cache_path_for("/recordings/a.mp4", tier)
        assert p1 == p2

    def test_different_tiers_different_paths(self, tmp_path):
        t = ABRTranscoder("/usr/bin/ffmpeg", "default", 0, str(tmp_path))
        tier_720 = QualityTier("720p", 1280, 720, "2000k")
        tier_480 = QualityTier("480p", 854, 480, "800k")
        p1 = t.cache_path_for("/recordings/a.mp4", tier_720)
        p2 = t.cache_path_for("/recordings/a.mp4", tier_480)
        assert p1 != p2

    def test_different_clips_different_paths(self, tmp_path):
        t = ABRTranscoder("/usr/bin/ffmpeg", "default", 0, str(tmp_path))
        tier = QualityTier("720p", 1280, 720, "2000k")
        p1 = t.cache_path_for("/recordings/a.mp4", tier, clip_from_ms=0, duration_ms=10000)
        p2 = t.cache_path_for("/recordings/a.mp4", tier, clip_from_ms=5000, duration_ms=10000)
        assert p1 != p2

    def test_path_is_mp4(self, tmp_path):
        t = ABRTranscoder("/usr/bin/ffmpeg", "default", 0, str(tmp_path))
        tier = QualityTier("720p", 1280, 720, "2000k")
        p = t.cache_path_for("/recordings/a.mp4", tier)
        assert str(p).endswith(".mp4")

    def test_path_contains_tier_name(self, tmp_path):
        t = ABRTranscoder("/usr/bin/ffmpeg", "default", 0, str(tmp_path))
        tier = QualityTier("720p", 1280, 720, "2000k")
        p = t.cache_path_for("/recordings/a.mp4", tier)
        assert "720p" in str(p)


class TestBuildCmd:
    def setup_method(self):
        self.transcoder = ABRTranscoder("/usr/bin/ffmpeg", "default", 0, "/tmp/cache")
        self.tier = QualityTier("720p", 1280, 720, "2000k")

    def test_basic_cmd_structure(self):
        cmd = self.transcoder._build_cmd("/input.mp4", "/output.mp4", self.tier)
        assert cmd[0] == "/usr/bin/ffmpeg"
        assert "-i" in cmd
        assert "/input.mp4" in cmd
        assert "/output.mp4" in cmd

    def test_contains_bitrate(self):
        cmd = self.transcoder._build_cmd("/in.mp4", "/out.mp4", self.tier)
        assert "2000k" in cmd

    def test_contains_scale(self):
        cmd = self.transcoder._build_cmd("/in.mp4", "/out.mp4", self.tier)
        cmd_str = " ".join(cmd)
        assert "1280" in cmd_str
        assert "720" in cmd_str

    def test_contains_audio_mapping(self):
        cmd = self.transcoder._build_cmd("/in.mp4", "/out.mp4", self.tier)
        assert "0:a:0?" in cmd

    def test_clip_from_adds_ss(self):
        cmd = self.transcoder._build_cmd("/in.mp4", "/out.mp4", self.tier, clip_from_ms=5000)
        assert "-ss" in cmd
        ss_idx = cmd.index("-ss")
        assert cmd[ss_idx + 1] == "5.000"

    def test_duration_adds_t(self):
        cmd = self.transcoder._build_cmd("/in.mp4", "/out.mp4", self.tier, duration_ms=10000)
        assert "-t" in cmd
        t_idx = cmd.index("-t")
        assert cmd[t_idx + 1] == "10.000"

    def test_no_clip_from_no_ss(self):
        cmd = self.transcoder._build_cmd("/in.mp4", "/out.mp4", self.tier)
        assert "-ss" not in cmd

    def test_nvidia_preset(self):
        t = ABRTranscoder("/usr/bin/ffmpeg", "preset-nvidia", 0, "/tmp/cache")
        cmd = t._build_cmd("/in.mp4", "/out.mp4", self.tier)
        cmd_str = " ".join(cmd)
        assert "h264_nvenc" in cmd_str
        assert "cuda" in cmd_str

    def test_vaapi_preset(self):
        t = ABRTranscoder("/usr/bin/ffmpeg", "preset-vaapi", "/dev/dri/renderD128", "/tmp/cache")
        cmd = t._build_cmd("/in.mp4", "/out.mp4", self.tier)
        cmd_str = " ".join(cmd)
        assert "h264_vaapi" in cmd_str
        assert "vaapi" in cmd_str

    def test_unknown_preset_falls_back_to_default(self):
        t = ABRTranscoder("/usr/bin/ffmpeg", "nonexistent-preset", 0, "/tmp/cache")
        cmd = t._build_cmd("/in.mp4", "/out.mp4", self.tier)
        cmd_str = " ".join(cmd)
        assert "libx264" in cmd_str

    def test_output_format_mp4(self):
        cmd = self.transcoder._build_cmd("/in.mp4", "/out.mp4", self.tier)
        assert "-f" in cmd
        f_idx = cmd.index("-f")
        assert cmd[f_idx + 1] == "mp4"

    def test_faststart(self):
        cmd = self.transcoder._build_cmd("/in.mp4", "/out.mp4", self.tier)
        assert "+faststart" in cmd
