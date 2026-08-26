"""Helpers for tests that need real MPEG-TS media (require ffmpeg/ffprobe)."""

import shutil
import subprocess
from pathlib import Path

import pytest

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

needs_ffmpeg = pytest.mark.skipif(
    not (FFMPEG and FFPROBE), reason="ffmpeg/ffprobe not installed"
)


def make_ts(path: Path, seconds: float = 1.0) -> Path:
    """Write a tiny H.264 + AAC MPEG-TS file, the shape of a cached segment."""
    subprocess.run(
        [
            FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=64x64:rate=25",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
            "-t", str(seconds),
            "-c:v", "libx264", "-preset", "ultrafast",
            "-c:a", "aac",
            "-f", "mpegts", str(path),
        ],
        check=True,
    )
    return path


def first_pts(path: Path, stream: str = "v:0") -> float:
    """First packet PTS (seconds) of the given stream, via ffprobe."""
    out = subprocess.run(
        [
            FFPROBE, "-v", "error", "-select_streams", stream,
            "-show_entries", "packet=pts_time", "-of", "csv=p=0", str(path),
        ],
        capture_output=True, text=True, check=True,
    ).stdout
    return float(out.splitlines()[0].rstrip(","))
