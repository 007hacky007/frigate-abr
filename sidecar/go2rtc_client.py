"""Client for go2rtc REST API to register ABR stream variants."""

import logging
from dataclasses import dataclass

import httpx

from .transcoder import QualityTier

logger = logging.getLogger(__name__)

GO2RTC_API = "http://127.0.0.1:1984"


@dataclass
class StreamInfo:
    name: str
    producers: list[dict]


async def get_streams(base_url: str = GO2RTC_API) -> dict[str, StreamInfo]:
    """Fetch all currently registered go2rtc streams."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{base_url}/api/streams")
        resp.raise_for_status()
        data = resp.json()

    streams = {}
    for name, info in data.items():
        producers = info.get("producers", []) if isinstance(info, dict) else []
        streams[name] = StreamInfo(name=name, producers=producers)
    return streams


async def register_variant(
    camera: str,
    tier: QualityTier,
    base_url: str = GO2RTC_API,
) -> bool:
    """Register a quality variant stream in go2rtc using ffmpeg transcoding.

    Creates e.g. 'front_door_abr_720p' sourced from 'front_door' with ffmpeg
    scaling and re-encoding.
    """
    variant_name = make_variant_name(camera, tier)
    # go2rtc ffmpeg source syntax: transcode from the parent stream
    source = f"ffmpeg:{camera}#video=h264#width={tier.width}#height={tier.height}"

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.put(
                f"{base_url}/api/streams",
                params={"src": source, "name": variant_name},
            )
            resp.raise_for_status()
        logger.info("Registered go2rtc variant: %s -> %s", camera, variant_name)
        return True
    except httpx.HTTPError:
        logger.exception("Failed to register go2rtc variant: %s", variant_name)
        return False


async def remove_variant(
    camera: str,
    tier: QualityTier,
    base_url: str = GO2RTC_API,
) -> bool:
    """Remove a quality variant stream from go2rtc."""
    variant_name = make_variant_name(camera, tier)
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.delete(
                f"{base_url}/api/streams",
                params={"name": variant_name},
            )
            resp.raise_for_status()
        logger.info("Removed go2rtc variant: %s", variant_name)
        return True
    except httpx.HTTPError:
        logger.exception("Failed to remove go2rtc variant: %s", variant_name)
        return False


ABR_VARIANT_PREFIX = "_abr_"


def make_variant_name(camera: str, tier: QualityTier) -> str:
    """Build the go2rtc stream name for an ABR variant."""
    return f"{camera}{ABR_VARIANT_PREFIX}{tier.name}"


def is_variant_stream(name: str) -> bool:
    """Check if a stream name is an ABR variant we created."""
    return ABR_VARIANT_PREFIX in name


async def setup_live_variants(
    tiers: list[QualityTier],
    base_url: str = GO2RTC_API,
) -> dict[str, list[str]]:
    """Register variant streams for all cameras in go2rtc.

    Returns dict mapping camera -> list of registered variant names.
    """
    streams = await get_streams(base_url)

    # Filter to only original camera streams (not birdseye, not existing variants)
    cameras = [
        name
        for name in streams
        if name != "birdseye" and not is_variant_stream(name)
    ]

    results: dict[str, list[str]] = {}
    for camera in cameras:
        variants = []
        for tier in tiers:
            ok = await register_variant(camera, tier, base_url)
            if ok:
                variants.append(make_variant_name(camera, tier))
        results[camera] = variants

    total = sum(len(v) for v in results.values())
    logger.info(
        "Live ABR setup complete: %d variants for %d cameras",
        total,
        len(cameras),
    )
    return results
