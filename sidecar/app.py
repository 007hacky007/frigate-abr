"""ABR sidecar service for Frigate - FastAPI application."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import yaml
from fastapi import FastAPI, HTTPException, Path as PathParam
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

CAMERA_NAME_RE = r"^[a-zA-Z0-9_-]+$"

from .cache import ABRCacheManager
from .go2rtc_client import setup_live_variants
from .transcoder import ABRTranscoder, QualityTier

logger = logging.getLogger(__name__)

# Frigate API (same container, accessed via loopback)
FRIGATE_API = "http://127.0.0.1:5001"
GO2RTC_API = "http://127.0.0.1:1984"

CONFIG_PATH = os.environ.get("ABR_CONFIG", "/opt/frigate-abr/config.yml")

# Shared httpx client (initialized in lifespan)
http_client: httpx.AsyncClient | None = None

# In-memory cache for VOD metadata (avoids re-fetching from Frigate for every segment)
# Key: "camera:start:end", Value: (timestamp, vod_data)
_vod_cache: dict[str, tuple[float, dict]] = {}
_VOD_CACHE_TTL = 300  # 5 minutes


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def parse_tiers(config: dict) -> list[QualityTier]:
    return [
        QualityTier(
            name=t["name"],
            width=t["width"],
            height=t["height"],
            bitrate=t["bitrate"],
        )
        for t in config.get("tiers", [])
    ]


def detect_ffmpeg_path(config: dict) -> str:
    """Determine ffmpeg binary path."""
    if "ffmpeg_path" in config:
        return config["ffmpeg_path"]
    # Try reading from Frigate config
    for path in ["/config/config.yml", "/config/config.yaml"]:
        if os.path.exists(path):
            try:
                with open(path) as f:
                    frigate_cfg = yaml.safe_load(f)
                ffpath = frigate_cfg.get("ffmpeg", {}).get("path", "default")
                if ffpath != "default":
                    if "/" in ffpath:
                        return ffpath
                    return f"/usr/lib/ffmpeg/{ffpath}/bin/ffmpeg"
            except Exception:
                pass
    # Auto-detect: find the first available ffmpeg binary
    for version in ["7.0", "7.1", "6.1", "6.0"]:
        candidate = f"/usr/lib/ffmpeg/{version}/bin/ffmpeg"
        if os.path.exists(candidate):
            return candidate
    # Last resort
    return "/usr/bin/ffmpeg"


def detect_gpu_device(hwaccel: str) -> str:
    """Auto-detect the GPU device path/index based on hwaccel preset."""
    if "nvidia" in hwaccel:
        return "0"
    # VAAPI/QSV/RKMPP use device paths
    for device in ["/dev/dri/renderD128", "/dev/dri/renderD129"]:
        if os.path.exists(device):
            return device
    return "/dev/dri/renderD128"


def detect_hwaccel(config: dict) -> str:
    """Determine hwaccel preset."""
    if "hwaccel" in config:
        return config["hwaccel"]
    # Try reading from Frigate config
    for path in ["/config/config.yml", "/config/config.yaml"]:
        if os.path.exists(path):
            try:
                with open(path) as f:
                    frigate_cfg = yaml.safe_load(f)
                return frigate_cfg.get("ffmpeg", {}).get("hwaccel_args", "default")
            except Exception:
                pass
    return "default"


# Globals initialized at startup
config: dict = {}
tiers: list[QualityTier] = []
transcoder: ABRTranscoder | None = None
cache_manager: ABRCacheManager | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global config, tiers, transcoder, cache_manager, http_client

    config = load_config()

    # Create shared httpx client
    http_client = httpx.AsyncClient(timeout=30.0)

    if not config.get("enabled", False):
        logger.warning("ABR is disabled in config")
        yield
        await http_client.aclose()
        return

    tiers = parse_tiers(config)
    if not tiers:
        logger.error("No quality tiers configured")
        yield
        await http_client.aclose()
        return

    ffmpeg_path = detect_ffmpeg_path(config)
    hwaccel = detect_hwaccel(config)
    gpu_cfg = config.get("gpu")
    # For VAAPI/QSV/RKMPP, the GPU must be a device path (e.g. /dev/dri/renderD128).
    # An integer like 0 is only valid for NVIDIA. Auto-detect if not a valid path.
    if gpu_cfg is not None and isinstance(gpu_cfg, str) and gpu_cfg.startswith("/"):
        gpu = gpu_cfg
    elif "nvidia" in hwaccel:
        gpu = str(gpu_cfg if gpu_cfg is not None else 0)
    else:
        gpu = detect_gpu_device(hwaccel)
    cache_cfg = config.get("cache", {})
    cache_dir = cache_cfg.get("path", "/tmp/cache/abr")

    logger.info("ABR sidecar starting: ffmpeg=%s hwaccel=%s gpu=%s", ffmpeg_path, hwaccel, gpu)
    logger.info("Tiers: %s", [t.name for t in tiers])

    transcoder = ABRTranscoder(
        ffmpeg_path=ffmpeg_path,
        hwaccel_preset=hwaccel,
        gpu=gpu,
        cache_dir=cache_dir,
        max_concurrent=config.get("max_concurrent_transcodes", 2),
    )

    cache_manager = ABRCacheManager(
        cache_dir=cache_dir,
        max_size_gb=cache_cfg.get("max_size_gb", 10.0),
        ttl_hours=cache_cfg.get("ttl_hours", 24),
    )
    cache_manager.start()

    # Register live stream variants in go2rtc
    try:
        await setup_live_variants(http_client, tiers, GO2RTC_API)
    except Exception:
        logger.exception("Failed to setup live ABR variants (go2rtc may not be ready)")

    yield

    # Shutdown
    if cache_manager:
        await cache_manager.stop()
    if http_client:
        await http_client.aclose()


app = FastAPI(title="Frigate ABR Sidecar", lifespan=lifespan)

# Serve frontend overlay files (fallback - nginx also serves these at /abr/static/)
overlay_web_dir = Path(__file__).parent.parent / "overlay" / "web" / "abr"
if overlay_web_dir.exists():
    app.mount("/abr/static", StaticFiles(directory=str(overlay_web_dir)), name="abr-static")


@app.get("/abr/health")
async def health():
    """Health check endpoint with version info."""
    return {
        "status": "ok",
        "version": os.environ.get("ABR_VERSION", "dev"),
        "commit": os.environ.get("ABR_COMMIT", "unknown"),
    }


@app.get("/abr/config")
async def get_config():
    """Return ABR configuration and status."""
    return {
        "enabled": config.get("enabled", False),
        "tiers": [
            {"name": t.name, "width": t.width, "height": t.height, "bitrate": t.bitrate}
            for t in tiers
        ],
        "cache": {
            "size_gb": cache_manager.get_cache_size_gb() if cache_manager else 0,
            "file_count": cache_manager.get_file_count() if cache_manager else 0,
            "max_size_gb": config.get("cache", {}).get("max_size_gb", 10.0),
            "ttl_hours": config.get("cache", {}).get("ttl_hours", 24),
        },
    }


@app.get("/abr/stats")
async def get_stats():
    """Return transcoding stats."""
    return {
        "active_transcodes": transcoder.active_count if transcoder else 0,
        "max_concurrent": config.get("max_concurrent_transcodes", 2),
        "cache_size_gb": cache_manager.get_cache_size_gb() if cache_manager else 0,
        "cache_files": cache_manager.get_file_count() if cache_manager else 0,
        "hwaccel": detect_hwaccel(config),
    }


@app.get("/abr/debug/transcode")
async def debug_transcode(camera: str = "vchod", quality: str = "480p"):
    """Debug endpoint: tries to transcode one segment and returns diagnostics."""
    import time as _time

    diag = {
        "ffmpeg_path": transcoder.ffmpeg_path if transcoder else None,
        "ffmpeg_exists": os.path.exists(transcoder.ffmpeg_path) if transcoder else False,
        "hwaccel_preset": transcoder.hwaccel_preset if transcoder else None,
        "gpu": transcoder.gpu if transcoder else None,
        "cache_dir": str(transcoder.cache_dir) if transcoder else None,
    }

    if not transcoder:
        diag["error"] = "Transcoder not initialized"
        return diag

    tier = _find_tier(quality)
    if not tier:
        diag["error"] = f"Unknown tier: {quality}"
        return diag

    # Get one recording segment from Frigate
    now = int(_time.time())
    vod_data = await _fetch_frigate_vod(camera, now - 3600, now)

    if not vod_data:
        diag["error"] = "Could not fetch VOD data from Frigate"
        return diag

    clips = vod_data.get("sequences", [{}])[0].get("clips", [])
    if not clips:
        diag["error"] = "No clips in VOD response"
        return diag

    clip = clips[0]
    recording_path = clip.get("path")
    clip_from_ms = clip.get("clipFrom")
    duration_ms = vod_data["durations"][0] if vod_data.get("durations") else None

    diag["recording_path"] = recording_path
    diag["recording_exists"] = os.path.exists(recording_path) if recording_path else False
    diag["clip_from_ms"] = clip_from_ms
    diag["duration_ms"] = duration_ms

    # Build the ffmpeg command without running it
    cache_path = transcoder.cache_path_for(recording_path, tier, clip_from_ms, duration_ms)
    tmp_path = str(cache_path) + ".debug"
    cmd = transcoder._build_cmd(recording_path, tmp_path, tier, clip_from_ms, duration_ms)
    diag["ffmpeg_cmd"] = " ".join(cmd)

    # Try running ffmpeg
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        diag["ffmpeg_exit_code"] = proc.returncode
        diag["ffmpeg_stderr"] = stderr.decode(errors="replace")[-1000:]
        if proc.returncode == 0 and os.path.exists(tmp_path):
            diag["output_size_bytes"] = os.path.getsize(tmp_path)
            os.unlink(tmp_path)
        elif os.path.exists(tmp_path):
            os.unlink(tmp_path)
    except asyncio.TimeoutError:
        diag["error"] = "ffmpeg timed out after 30s"
    except Exception as e:
        diag["error"] = str(e)

    return diag


@app.get("/abr/hls/{camera_name}/start/{start_ts}/end/{end_ts}/playlist.m3u8")
async def vod_abr_playlist(
    camera_name: str = PathParam(..., pattern=CAMERA_NAME_RE),
    start_ts: float = 0,
    end_ts: float = 0,
    quality: str = "480p",
):
    """Generate an HLS VOD playlist for a specific quality tier."""
    if not transcoder or not tiers:
        raise HTTPException(503, "ABR not initialized")

    tier = _find_tier(quality)
    if not tier:
        raise HTTPException(400, f"Unknown quality tier: {quality}")

    vod_data = await _fetch_frigate_vod(camera_name, start_ts, end_ts)
    if not vod_data:
        raise HTTPException(404, "No recordings found")

    sequences = vod_data.get("sequences", [])
    if not sequences or not sequences[0].get("clips"):
        raise HTTPException(404, "No clips in VOD response")

    clips = sequences[0]["clips"]
    durations = vod_data.get("durations", [])

    # Build HLS VOD playlist
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        "#EXT-X-TARGETDURATION:" + str(max(int(d / 1000) + 1 for d in durations) if durations else 10),
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:VOD",
    ]

    base_url = f"/abr/hls/{camera_name}/start/{int(start_ts)}/end/{int(end_ts)}"

    for i, clip in enumerate(clips):
        if i >= len(durations):
            break
        duration_s = durations[i] / 1000.0
        lines.append(f"#EXTINF:{duration_s:.3f},")
        lines.append(f"{base_url}/segment/{i}.ts?quality={quality}")

    lines.append("#EXT-X-ENDLIST")

    return PlainTextResponse(
        "\n".join(lines) + "\n",
        media_type="application/vnd.apple.mpegurl",
    )


@app.get("/abr/hls/{camera_name}/start/{start_ts}/end/{end_ts}/segment/{index}.ts")
async def vod_abr_segment(
    camera_name: str = PathParam(..., pattern=CAMERA_NAME_RE),
    start_ts: float = 0,
    end_ts: float = 0,
    index: int = 0,
    quality: str = "480p",
):
    """Transcode and serve a single recording segment as MPEG-TS.

    Called by hls.js when it needs a specific segment from the playlist.
    Transcodes on-demand and caches the result.
    """
    if not transcoder:
        raise HTTPException(503, "ABR transcoder not initialized")

    tier = _find_tier(quality)
    if not tier:
        raise HTTPException(400, f"Unknown quality tier: {quality}")

    # Get the clip info for this segment index
    vod_data = await _fetch_frigate_vod(camera_name, start_ts, end_ts)
    if not vod_data:
        raise HTTPException(404, "No recordings found")

    clips = vod_data.get("sequences", [{}])[0].get("clips", [])
    durations = vod_data.get("durations", [])

    if index >= len(clips) or index >= len(durations):
        raise HTTPException(404, f"Segment {index} not found")

    clip = clips[index]
    recording_path = clip.get("path")
    if not recording_path:
        raise HTTPException(404, "No recording path for segment")

    clip_from_ms = clip.get("clipFrom")
    duration_ms = durations[index]

    # Transcode this single segment (cached if already done)
    transcoded_path = await transcoder.get_or_transcode(
        recording_path, tier, clip_from_ms=clip_from_ms, duration_ms=duration_ms
    )
    if not transcoded_path:
        raise HTTPException(500, "Transcoding failed")

    # Serve the transcoded file
    from fastapi.responses import FileResponse
    return FileResponse(
        transcoded_path,
        media_type="video/mp2t",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.post("/abr/live/setup")
async def live_setup():
    """Manually trigger registration of live ABR variants in go2rtc."""
    if not tiers:
        raise HTTPException(503, "ABR not initialized")

    results = await setup_live_variants(http_client, tiers, GO2RTC_API)
    return {"cameras": results}


def _find_tier(quality: str) -> QualityTier | None:
    for t in tiers:
        if t.name == quality:
            return t
    return None


async def _fetch_frigate_vod(
    camera_name: str, start_ts: float, end_ts: float
) -> dict | None:
    """Fetch VOD manifest data from Frigate's internal API (port 5001).

    Results are cached in memory to avoid repeated API calls when serving
    individual segments from the same recording range.
    """
    import time as _time

    cache_key = f"{camera_name}:{int(start_ts)}:{int(end_ts)}"
    now = _time.time()

    # Check cache
    if cache_key in _vod_cache:
        cached_time, cached_data = _vod_cache[cache_key]
        if now - cached_time < _VOD_CACHE_TTL:
            return cached_data

    url = f"{FRIGATE_API}/vod/{camera_name}/start/{int(start_ts)}/end/{int(end_ts)}"
    headers = {"remote-user": "admin", "remote-role": "admin"}
    try:
        resp = await http_client.get(url, headers=headers)
        if resp.status_code == 404:
            logger.warning("Frigate VOD returned 404 for %s: %s", url, resp.text[:200])
            return None
        if resp.status_code >= 400:
            logger.error("Frigate VOD returned %d for %s: %s", resp.status_code, url, resp.text[:200])
            return None
        resp.raise_for_status()
        data = resp.json()
        # Cache the result
        _vod_cache[cache_key] = (now, data)
        # Evict old entries (copy keys first to avoid RuntimeError during iteration)
        to_delete = [k for k in _vod_cache if now - _vod_cache[k][0] > _VOD_CACHE_TTL]
        for k in to_delete:
            _vod_cache.pop(k, None)
        return data
    except httpx.HTTPError:
        logger.exception("Failed to fetch Frigate VOD for %s at %s", camera_name, url)
        return None


