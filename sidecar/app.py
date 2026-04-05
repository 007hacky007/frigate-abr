"""ABR sidecar service for Frigate - FastAPI application."""

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

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
                if ffpath == "default":
                    return "/usr/lib/ffmpeg/7.1/bin/ffmpeg"
                # If it contains a slash, it's already an absolute path
                if "/" in ffpath:
                    return ffpath
                return f"/usr/lib/ffmpeg/{ffpath}/bin/ffmpeg"
            except Exception:
                pass
    return "/usr/lib/ffmpeg/7.1/bin/ffmpeg"


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
    gpu = config.get("gpu", 0)
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
        await setup_live_variants(tiers, GO2RTC_API)
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


@app.get("/abr/vod_abr/{camera_name}/start/{start_ts}/end/{end_ts}")
async def vod_abr_mapped(
    camera_name: str, start_ts: float, end_ts: float, quality: str = "original"
):
    """Serve VOD manifest for a specific quality tier.

    Fetches segment info from Frigate's VOD API, transcodes segments on-demand,
    and returns a modified manifest pointing to transcoded files.
    """
    if quality == "original":
        return await _proxy_frigate_vod(camera_name, start_ts, end_ts)

    if not transcoder:
        raise HTTPException(503, "ABR transcoder not initialized")

    tier = _find_tier(quality)
    if not tier:
        raise HTTPException(400, f"Unknown quality tier: {quality}")

    # Fetch segment info from Frigate's VOD API
    vod_data = await _fetch_frigate_vod(camera_name, start_ts, end_ts)
    if not vod_data:
        raise HTTPException(404, "No recordings found")

    sequences = vod_data.get("sequences", [])
    if not sequences or not sequences[0].get("clips"):
        raise HTTPException(404, "No clips in VOD response")

    clips = sequences[0]["clips"]
    new_clips = []
    new_durations = []

    for i, clip in enumerate(clips):
        recording_path = clip.get("path")
        if not recording_path:
            continue

        # Extract clipFrom and duration from Frigate's VOD response.
        # These define the exact portion of the recording file to transcode.
        clip_from_ms = clip.get("clipFrom")
        duration_ms = vod_data["durations"][i] if i < len(vod_data.get("durations", [])) else None

        if not duration_ms:
            continue

        # Transcode this segment (with clip boundaries)
        transcoded_path = await transcoder.get_or_transcode(
            recording_path, tier, clip_from_ms=clip_from_ms, duration_ms=duration_ms
        )
        if not transcoded_path:
            logger.warning("Skipping segment %s - transcode failed", recording_path)
            continue

        # Build new clip pointing to transcoded file.
        # No clipFrom needed - we already trimmed during transcoding.
        new_clip = {
            "type": "source",
            "path": transcoded_path,
            "keyFrameDurations": [duration_ms],
        }
        new_durations.append(duration_ms)
        new_clips.append(new_clip)

    if not new_clips:
        raise HTTPException(404, "No segments could be transcoded")

    return {
        "cache": vod_data.get("cache", False),
        "discontinuity": vod_data.get("discontinuity", False),
        "consistentSequenceMediaInfo": True,
        "durations": new_durations,
        "segment_duration": max(new_durations),
        "sequences": [{"clips": new_clips}],
    }


@app.post("/abr/live/setup")
async def live_setup():
    """Manually trigger registration of live ABR variants in go2rtc."""
    if not tiers:
        raise HTTPException(503, "ABR not initialized")

    results = await setup_live_variants(tiers, GO2RTC_API)
    return {"cameras": results}


def _find_tier(quality: str) -> QualityTier | None:
    for t in tiers:
        if t.name == quality:
            return t
    return None


def _tier_bandwidth(tier: QualityTier) -> int:
    """Convert tier bitrate to bits/sec for HLS BANDWIDTH tag."""
    b = tier.bitrate.strip().lower()
    if b.endswith("k"):
        return int(float(b[:-1]) * 1000)
    if b.endswith("m"):
        return int(float(b[:-1]) * 1000000)
    return int(b)


async def _fetch_frigate_vod(
    camera_name: str, start_ts: float, end_ts: float
) -> dict | None:
    """Fetch VOD manifest data from Frigate's internal API (port 5001).

    Note: Frigate's internal API does NOT use the /api/ prefix.
    The /api/ prefix is only used by nginx's external routing.
    """
    # Use int timestamps to avoid .0 suffix in URL
    url = f"{FRIGATE_API}/vod/{camera_name}/start/{int(start_ts)}/end/{int(end_ts)}"
    # Frigate's API requires auth headers even on the internal port.
    # nginx normally sets these via auth subrequest. We pass admin
    # credentials since this is a same-container internal call.
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
        return resp.json()
    except httpx.HTTPError:
        logger.exception("Failed to fetch Frigate VOD for %s at %s", camera_name, url)
        return None


async def _proxy_frigate_vod(
    camera_name: str, start_ts: float, end_ts: float
):
    """Proxy VOD request directly to Frigate."""
    data = await _fetch_frigate_vod(camera_name, start_ts, end_ts)
    if not data:
        raise HTTPException(404, "No recordings found")
    return data
