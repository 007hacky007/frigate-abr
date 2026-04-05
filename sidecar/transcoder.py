"""GPU-accelerated on-demand transcoding for ABR streaming."""

import asyncio
import hashlib
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class QualityTier:
    name: str
    width: int
    height: int
    bitrate: str


# ffmpeg command templates per hwaccel backend.
# {gpu} is the GPU device index/path.
# {w}, {h}, {bitrate}, {maxrate}, {bufsize} are tier parameters.
HWACCEL_TEMPLATES = {
    "preset-nvidia": {
        "decode": "-hwaccel cuda -hwaccel_device {gpu} -hwaccel_output_format cuda",
        "scale": "-vf scale_cuda=w={w}:h={h}:force_original_aspect_ratio=decrease:force_divisible_by=2",
        "encode": "-c:v h264_nvenc -preset:v p4 -profile:v high -b:v {bitrate} -maxrate {maxrate} -bufsize {bufsize} -g 50",
    },
    "preset-nvidia-h264": {
        "decode": "-hwaccel cuda -hwaccel_device {gpu} -hwaccel_output_format cuda",
        "scale": "-vf scale_cuda=w={w}:h={h}:force_original_aspect_ratio=decrease:force_divisible_by=2",
        "encode": "-c:v h264_nvenc -preset:v p4 -profile:v high -b:v {bitrate} -maxrate {maxrate} -bufsize {bufsize} -g 50",
    },
    "preset-nvidia-h265": {
        "decode": "-hwaccel cuda -hwaccel_device {gpu} -hwaccel_output_format cuda",
        "scale": "-vf scale_cuda=w={w}:h={h}:force_original_aspect_ratio=decrease:force_divisible_by=2",
        "encode": "-c:v h264_nvenc -preset:v p4 -profile:v high -b:v {bitrate} -maxrate {maxrate} -bufsize {bufsize} -g 50",
    },
    "preset-vaapi": {
        "decode": "-hwaccel vaapi -hwaccel_device {gpu} -hwaccel_output_format vaapi -extra_hw_frames 16",
        "scale": "-vf hwdownload,format=nv12,scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2,format=nv12,hwupload",
        "encode": "-c:v h264_vaapi -profile:v high -b:v {bitrate} -maxrate {maxrate} -bufsize {bufsize} -g 50 -bf 0 -sei:v 0",
    },
    "preset-intel-qsv-h264": {
        "decode": "-hwaccel qsv -qsv_device {gpu} -hwaccel_output_format qsv",
        "scale": "-vf vpp_qsv=w={w}:h={h}",
        "encode": "-c:v h264_qsv -profile:v high -b:v {bitrate} -maxrate {maxrate} -bufsize {bufsize} -g 50 -async_depth:v 1",
    },
    "preset-intel-qsv-h265": {
        "decode": "-hwaccel qsv -qsv_device {gpu} -hwaccel_output_format qsv",
        "scale": "-vf vpp_qsv=w={w}:h={h}",
        "encode": "-c:v h264_qsv -profile:v high -b:v {bitrate} -maxrate {maxrate} -bufsize {bufsize} -g 50 -async_depth:v 1",
    },
    "preset-rkmpp": {
        "decode": "-hwaccel rkmpp -hwaccel_output_format drm_prime",
        "scale": "-vf scale_rkrga=w={w}:h={h}:format=yuv420p:force_original_aspect_ratio=0",
        "encode": "-c:v h264_rkmpp -profile:v high -b:v {bitrate} -maxrate {maxrate} -bufsize {bufsize} -g 50",
    },
    "preset-rk-h264": {
        "decode": "-hwaccel rkmpp -hwaccel_output_format drm_prime",
        "scale": "-vf scale_rkrga=w={w}:h={h}:format=yuv420p:force_original_aspect_ratio=0",
        "encode": "-c:v h264_rkmpp -profile:v high -b:v {bitrate} -maxrate {maxrate} -bufsize {bufsize} -g 50",
    },
    "preset-rk-h265": {
        "decode": "-hwaccel rkmpp -hwaccel_output_format drm_prime",
        "scale": "-vf scale_rkrga=w={w}:h={h}:format=yuv420p:force_original_aspect_ratio=0",
        "encode": "-c:v h264_rkmpp -profile:v high -b:v {bitrate} -maxrate {maxrate} -bufsize {bufsize} -g 50",
    },
    "preset-rpi-64-h264": {
        "decode": "",
        "scale": "-vf scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2",
        "encode": "-c:v h264_v4l2m2m -b:v {bitrate} -g 50",
    },
    "preset-rpi-64-h265": {
        "decode": "",
        "scale": "-vf scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2",
        "encode": "-c:v h264_v4l2m2m -b:v {bitrate} -g 50",
    },
    "preset-jetson-h264": {
        "decode": "",
        "scale": "-vf scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2",
        "encode": "-c:v h264_nvmpi -profile high -b:v {bitrate} -g 50",
    },
    "preset-jetson-h265": {
        "decode": "",
        "scale": "-vf scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2",
        "encode": "-c:v h264_nvmpi -profile high -b:v {bitrate} -g 50",
    },
}

# CPU fallback
HWACCEL_TEMPLATES["default"] = {
    "decode": "",
    "scale": "-vf scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2",
    "encode": "-c:v libx264 -preset:v fast -profile:v high -level:v 4.1 -b:v {bitrate} -maxrate {maxrate} -bufsize {bufsize} -g 50",
}


def _parse_bitrate_kbps(bitrate: str) -> int:
    """Parse bitrate string like '2000k' or '4M' to kbps integer."""
    b = bitrate.strip().lower()
    if b.endswith("k"):
        return int(b[:-1])
    if b.endswith("m"):
        return int(float(b[:-1]) * 1000)
    return int(b)


TRANSCODE_TIMEOUT_SECONDS = 300


class ABRTranscoder:
    def __init__(
        self,
        ffmpeg_path: str,
        hwaccel_preset: str,
        gpu: int | str,
        cache_dir: str,
        max_concurrent: int = 2,
    ):
        self.ffmpeg_path = ffmpeg_path
        self.hwaccel_preset = hwaccel_preset
        self.gpu = str(gpu)
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self._active_count = 0
        self._count_lock = asyncio.Lock()
        # Per-segment locks to prevent duplicate concurrent transcodes of the same file
        self._segment_locks: dict[str, asyncio.Lock] = {}
        self._segment_locks_lock = asyncio.Lock()

    @property
    def active_count(self) -> int:
        return self._active_count

    async def _get_segment_lock(self, key: str) -> asyncio.Lock:
        """Get or create a per-segment lock."""
        async with self._segment_locks_lock:
            if key not in self._segment_locks:
                self._segment_locks[key] = asyncio.Lock()
            return self._segment_locks[key]

    async def _release_segment_lock(self, key: str) -> None:
        """Remove a per-segment lock if no longer held."""
        async with self._segment_locks_lock:
            lock = self._segment_locks.get(key)
            if lock and not lock.locked():
                del self._segment_locks[key]

    def cache_path_for(
        self,
        recording_path: str,
        tier: QualityTier,
        clip_from_ms: int | None = None,
        duration_ms: int | None = None,
    ) -> Path:
        """Deterministic cache path for a recording+tier+clip combination."""
        key = f"{recording_path}:{tier.name}:{clip_from_ms}:{duration_ms}"
        h = hashlib.sha256(key.encode()).hexdigest()[:16]
        basename = Path(recording_path).stem
        return self.cache_dir / f"{basename}_{tier.name}_{h}.mp4"

    def is_cached(
        self,
        recording_path: str,
        tier: QualityTier,
        clip_from_ms: int | None = None,
        duration_ms: int | None = None,
    ) -> bool:
        p = self.cache_path_for(recording_path, tier, clip_from_ms, duration_ms)
        return p.exists() and p.stat().st_size > 0

    async def get_or_transcode(
        self,
        recording_path: str,
        tier: QualityTier,
        clip_from_ms: int | None = None,
        duration_ms: int | None = None,
    ) -> str | None:
        """Return path to transcoded segment. Transcodes on-demand if not cached.

        Args:
            recording_path: Path to the original recording MP4 file.
            tier: Target quality tier.
            clip_from_ms: Start offset in milliseconds (from Frigate's clipFrom).
            duration_ms: Duration in milliseconds to transcode.
        """
        cached = self.cache_path_for(recording_path, tier, clip_from_ms, duration_ms)
        cache_key = str(cached)

        if cached.exists() and cached.stat().st_size > 0:
            cached.touch()
            return str(cached)

        if not os.path.exists(recording_path):
            logger.error("Recording not found: %s", recording_path)
            return None

        # Per-segment lock prevents duplicate transcodes of the same segment
        segment_lock = await self._get_segment_lock(cache_key)

        async with segment_lock:
            # Check again after acquiring segment lock
            if cached.exists() and cached.stat().st_size > 0:
                cached.touch()
                await self._release_segment_lock(cache_key)
                return str(cached)

            # Global semaphore limits total concurrent GPU transcodes
            async with self.semaphore:
                async with self._count_lock:
                    self._active_count += 1
                try:
                    success = await self._transcode(
                        recording_path, str(cached), tier, clip_from_ms, duration_ms
                    )
                finally:
                    async with self._count_lock:
                        self._active_count -= 1

                if success:
                    await self._release_segment_lock(cache_key)
                    return str(cached)
                cached.unlink(missing_ok=True)

        await self._release_segment_lock(cache_key)
        return None

    def _build_cmd(
        self,
        input_path: str,
        output_path: str,
        tier: QualityTier,
        clip_from_ms: int | None = None,
        duration_ms: int | None = None,
    ) -> list[str]:
        """Build ffmpeg command for transcoding a segment."""
        preset = self.hwaccel_preset
        template = HWACCEL_TEMPLATES.get(preset, HWACCEL_TEMPLATES["default"])

        kbps = _parse_bitrate_kbps(tier.bitrate)
        params = {
            "gpu": self.gpu,
            "w": str(tier.width),
            "h": str(tier.height),
            "bitrate": tier.bitrate,
            "maxrate": tier.bitrate,
            "bufsize": f"{kbps * 2}k",
        }

        parts = [self.ffmpeg_path, "-hide_banner", "-loglevel", "warning", "-y"]

        # Seek to clip start (before input for fast seek)
        if clip_from_ms is not None and clip_from_ms > 0:
            ss_seconds = clip_from_ms / 1000.0
            parts.extend(["-ss", f"{ss_seconds:.3f}"])

        # Decode args
        decode = template["decode"].format(**params)
        if decode:
            parts.extend(decode.split())

        # Input
        parts.extend(["-i", input_path])

        # Duration limit (after input)
        if duration_ms is not None and duration_ms > 0:
            t_seconds = duration_ms / 1000.0
            parts.extend(["-t", f"{t_seconds:.3f}"])

        # Scale/filter
        scale = template["scale"].format(**params)
        if scale:
            parts.extend(scale.split())

        # Encode
        encode = template["encode"].format(**params)
        parts.extend(encode.split())

        # Map video and optional audio explicitly
        parts.extend(["-map", "0:v:0", "-map", "0:a:0?"])
        # Audio: transcode to AAC if present
        parts.extend(["-c:a", "aac", "-b:a", "128k", "-ac", "2"])

        # Output format
        parts.extend(["-movflags", "+faststart", "-f", "mp4", output_path])

        return parts

    async def _transcode(
        self,
        input_path: str,
        output_path: str,
        tier: QualityTier,
        clip_from_ms: int | None = None,
        duration_ms: int | None = None,
    ) -> bool:
        """Run ffmpeg to transcode a recording segment."""
        tmp_path = output_path + ".tmp"
        cmd = self._build_cmd(input_path, tmp_path, tier, clip_from_ms, duration_ms)

        logger.info(
            "Transcoding %s -> %s (%s, clip_from=%s, duration=%s)",
            input_path,
            tier.name,
            tier.bitrate,
            clip_from_ms,
            duration_ms,
        )
        logger.debug("ffmpeg cmd: %s", " ".join(cmd))

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=TRANSCODE_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                logger.error("Transcoding timed out for %s", input_path)
                proc.kill()
                await proc.wait()
                Path(tmp_path).unlink(missing_ok=True)
                return False

            if proc.returncode != 0:
                logger.error(
                    "Transcoding failed (exit %d) for %s: %s",
                    proc.returncode,
                    input_path,
                    stderr.decode(errors="replace")[-500:],
                )
                Path(tmp_path).unlink(missing_ok=True)
                return False

            shutil.move(tmp_path, output_path)
            logger.info("Transcoded: %s -> %s", input_path, output_path)
            return True

        except Exception:
            logger.exception("Transcoding exception for %s", input_path)
            Path(tmp_path).unlink(missing_ok=True)
            return False
