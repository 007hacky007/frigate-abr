"""GPU-accelerated on-demand transcoding for ABR streaming."""

import asyncio
import hashlib
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .cache import CACHE_VERSION

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
#
# Every encoder runs with -bf 0. Frigate's recordings are variable frame rate -
# a camera that nominally sends 16 fps drifts by a few hundred ms between
# frames - and an encoder that reorders frames derives each B-frame's DTS from
# a constant frame duration. On a long frame interval that DTS lands before the
# previous one, and the mpegts muxer bumps the non-monotonic value to prev + 1
# tick (11 us). Safari's decoder stops at the first run of those samples: the
# picture freezes while audio keeps playing. Chrome decodes them anyway, so the
# breakage is WebKit-only. Without reordering, DTS is just PTS and the problem
# cannot arise.
HWACCEL_TEMPLATES = {
    "preset-nvidia": {
        "decode": "-hwaccel cuda -hwaccel_device {gpu} -hwaccel_output_format cuda",
        "scale": "-vf scale_cuda=w={w}:h={h}:force_original_aspect_ratio=decrease:force_divisible_by=2",
        "encode": "-c:v h264_nvenc -bf 0 -preset:v p4 -profile:v high -b:v {bitrate} -maxrate {maxrate} -bufsize {bufsize} -g 50",
    },
    "preset-nvidia-h264": {
        "decode": "-hwaccel cuda -hwaccel_device {gpu} -hwaccel_output_format cuda",
        "scale": "-vf scale_cuda=w={w}:h={h}:force_original_aspect_ratio=decrease:force_divisible_by=2",
        "encode": "-c:v h264_nvenc -bf 0 -preset:v p4 -profile:v high -b:v {bitrate} -maxrate {maxrate} -bufsize {bufsize} -g 50",
    },
    "preset-nvidia-h265": {
        "decode": "-hwaccel cuda -hwaccel_device {gpu} -hwaccel_output_format cuda",
        "scale": "-vf scale_cuda=w={w}:h={h}:force_original_aspect_ratio=decrease:force_divisible_by=2",
        "encode": "-c:v h264_nvenc -bf 0 -preset:v p4 -profile:v high -b:v {bitrate} -maxrate {maxrate} -bufsize {bufsize} -g 50",
    },
    "preset-vaapi": {
        "decode": "-hwaccel qsv -qsv_device {gpu} -hwaccel_output_format qsv -extra_hw_frames 32",
        "scale": "-vf vpp_qsv=w={w}:h={h}",
        "encode": "-c:v h264_qsv -bf 0 -b:v {bitrate} -maxrate {maxrate} -bufsize {bufsize} -g 50 -async_depth:v 1",
    },
    "preset-intel-qsv-h264": {
        "decode": "-hwaccel qsv -qsv_device {gpu} -hwaccel_output_format qsv -extra_hw_frames 32",
        "scale": "-vf vpp_qsv=w={w}:h={h}",
        "encode": "-c:v h264_qsv -bf 0 -b:v {bitrate} -maxrate {maxrate} -bufsize {bufsize} -g 50 -async_depth:v 1",
    },
    "preset-intel-qsv-h265": {
        "decode": "-hwaccel qsv -qsv_device {gpu} -hwaccel_output_format qsv -extra_hw_frames 32",
        "scale": "-vf vpp_qsv=w={w}:h={h}",
        "encode": "-c:v h264_qsv -bf 0 -b:v {bitrate} -maxrate {maxrate} -bufsize {bufsize} -g 50 -async_depth:v 1",
    },
    "preset-rkmpp": {
        "decode": "-hwaccel rkmpp -hwaccel_output_format drm_prime",
        "scale": "-vf scale_rkrga=w={w}:h={h}:format=yuv420p:force_original_aspect_ratio=0",
        "encode": "-c:v h264_rkmpp -bf 0 -profile:v high -b:v {bitrate} -maxrate {maxrate} -bufsize {bufsize} -g 50",
    },
    "preset-rk-h264": {
        "decode": "-hwaccel rkmpp -hwaccel_output_format drm_prime",
        "scale": "-vf scale_rkrga=w={w}:h={h}:format=yuv420p:force_original_aspect_ratio=0",
        "encode": "-c:v h264_rkmpp -bf 0 -profile:v high -b:v {bitrate} -maxrate {maxrate} -bufsize {bufsize} -g 50",
    },
    "preset-rk-h265": {
        "decode": "-hwaccel rkmpp -hwaccel_output_format drm_prime",
        "scale": "-vf scale_rkrga=w={w}:h={h}:format=yuv420p:force_original_aspect_ratio=0",
        "encode": "-c:v h264_rkmpp -bf 0 -profile:v high -b:v {bitrate} -maxrate {maxrate} -bufsize {bufsize} -g 50",
    },
    "preset-rpi-64-h264": {
        "decode": "",
        "scale": "-vf scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2",
        "encode": "-c:v h264_v4l2m2m -bf 0 -b:v {bitrate} -g 50",
    },
    "preset-rpi-64-h265": {
        "decode": "",
        "scale": "-vf scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2",
        "encode": "-c:v h264_v4l2m2m -bf 0 -b:v {bitrate} -g 50",
    },
    "preset-jetson-h264": {
        "decode": "",
        "scale": "-vf scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2",
        "encode": "-c:v h264_nvmpi -bf 0 -profile high -b:v {bitrate} -g 50",
    },
    "preset-jetson-h265": {
        "decode": "",
        "scale": "-vf scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2",
        "encode": "-c:v h264_nvmpi -bf 0 -profile high -b:v {bitrate} -g 50",
    },
}

# CPU fallback
HWACCEL_TEMPLATES["default"] = {
    "decode": "",
    "scale": "-vf scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2",
    "encode": "-c:v libx264 -bf 0 -preset:v superfast -tune:v zerolatency -b:v {bitrate} -maxrate {maxrate} -bufsize {bufsize} -g 50",
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

    def _template(self) -> dict:
        """ffmpeg argument template for the configured hwaccel preset."""
        return HWACCEL_TEMPLATES.get(self.hwaccel_preset, HWACCEL_TEMPLATES["default"])

    def cache_path_for(
        self,
        recording_path: str,
        tier: QualityTier,
        clip_from_ms: int | None = None,
        duration_ms: int | None = None,
    ) -> Path:
        """Deterministic cache path for a recording+tier+clip combination.

        CACHE_VERSION is part of the key, so segments produced by a build
        that encoded them differently cannot be served even if they survived
        the startup purge (a cache directory restored from a backup, say).
        """
        key = f"{recording_path}:{tier.name}:{clip_from_ms}:{duration_ms}:v{CACHE_VERSION}"
        h = hashlib.sha256(key.encode()).hexdigest()[:16]
        basename = Path(recording_path).stem
        return self.cache_dir / f"{basename}_{tier.name}_{h}.ts"

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
        template = self._template()

        kbps = _parse_bitrate_kbps(tier.bitrate)
        params = {
            "gpu": self.gpu,
            "w": str(tier.width),
            "h": str(tier.height),
            "bitrate": tier.bitrate,
            "maxrate": tier.bitrate,
            "bufsize": f"{kbps * 2}k",
        }

        parts = [self.ffmpeg_path, "-hide_banner", "-loglevel", "warning", "-threads", "4", "-y"]

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

        # Output format: MPEG-TS for HLS segment compatibility.
        # Timestamps start at 0 so one cached file serves every playlist the
        # clip appears in; shift_timestamps() moves it to its playlist
        # position at serve time.
        parts.extend(["-avoid_negative_ts", "make_zero", "-f", "mpegts", output_path])

        return parts

    async def shift_timestamps(self, src_path: str, offset_seconds: float) -> bytes | None:
        """Return the cached segment remuxed so its timestamps start at
        ``offset_seconds`` on the playlist timeline.

        Cached segments are transcoded with timestamps starting at zero so one
        cache entry serves every playlist the clip appears in. Shifting them at
        serve time (stream copy, no re-encode, about 40 ms for a 1080p segment)
        gives hls.js one continuous timeline instead of a discontinuity per
        segment. The continuity-counter restart is signalled in the TS
        adaptation field instead. Returns None if ffmpeg fails.
        """
        cmd = [
            self.ffmpeg_path, "-hide_banner", "-loglevel", "error",
            "-i", src_path,
            "-c", "copy",
            "-output_ts_offset", f"{offset_seconds:.3f}",
            # Each cached segment was muxed on its own, so its continuity
            # counters restart at 0. Flag that at the TS level (adaptation
            # field discontinuity_indicator) so demuxers reading the playlist
            # as one stream do not report the boundary packet as corrupt.
            "-mpegts_flags", "initial_discontinuity",
            "-f", "mpegts", "pipe:1",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            data, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        except asyncio.TimeoutError:
            logger.error("Timestamp shift timed out for %s", src_path)
            proc.kill()
            await proc.wait()
            return None
        except Exception:
            logger.exception("Timestamp shift failed for %s", src_path)
            return None

        if proc.returncode != 0 or not data:
            logger.warning(
                "ffmpeg exit %d shifting %s: %s",
                proc.returncode, src_path, stderr.decode(errors="replace")[-300:],
            )
            return None
        return data

    async def _run_ffmpeg(self, cmd: list[str], output_path: str) -> bool:
        """Run an ffmpeg command. Returns True on success."""
        tmp_path = output_path + ".tmp"
        # Replace output path in cmd with tmp_path
        cmd = [tmp_path if x == output_path else x for x in cmd]

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
                logger.error("Transcoding timed out")
                proc.kill()
                await proc.wait()
                Path(tmp_path).unlink(missing_ok=True)
                return False

            if proc.returncode != 0:
                logger.warning(
                    "ffmpeg exit %d: %s",
                    proc.returncode,
                    stderr.decode(errors="replace")[-300:],
                )
                Path(tmp_path).unlink(missing_ok=True)
                return False

            shutil.move(tmp_path, output_path)
            return True

        except Exception:
            logger.exception("ffmpeg exception")
            Path(tmp_path).unlink(missing_ok=True)
            return False

    async def _transcode(
        self,
        input_path: str,
        output_path: str,
        tier: QualityTier,
        clip_from_ms: int | None = None,
        duration_ms: int | None = None,
    ) -> bool:
        """Run ffmpeg to transcode a recording segment with the configured
        hwaccel preset. There is no automatic CPU retry: a failed transcode
        returns False and the caller reports it, so set ``hwaccel: default``
        in config.yml on hosts where the GPU path does not work.
        """
        logger.info(
            "Transcoding %s -> %s (%s, clip_from=%s, duration=%s)",
            input_path,
            tier.name,
            tier.bitrate,
            clip_from_ms,
            duration_ms,
        )

        cmd = self._build_cmd(input_path, output_path, tier, clip_from_ms, duration_ms)
        logger.debug("ffmpeg cmd: %s", " ".join(cmd))
        success = await self._run_ffmpeg(cmd, output_path)

        if success:
            logger.info("Transcoded: %s -> %s", input_path, output_path)
        else:
            logger.error("Transcoding failed for %s", input_path)

        return success
