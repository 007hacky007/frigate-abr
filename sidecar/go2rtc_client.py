"""Client for go2rtc REST API to register ABR stream variants."""

import logging

import httpx

from .transcoder import QualityTier

logger = logging.getLogger(__name__)

GO2RTC_API = "http://127.0.0.1:1984"

ABR_VARIANT_PREFIX = "_abr_"


def make_variant_name(camera: str, tier: QualityTier) -> str:
    """Build the go2rtc stream name for an ABR variant."""
    return f"{camera}{ABR_VARIANT_PREFIX}{tier.name}"


def is_variant_stream(name: str) -> bool:
    """Check if a stream name is an ABR variant we created."""
    return ABR_VARIANT_PREFIX in name


async def get_streams(
    client: httpx.AsyncClient, base_url: str = GO2RTC_API
) -> dict[str, dict]:
    """Fetch all currently registered go2rtc streams."""
    resp = await client.get(f"{base_url}/api/streams")
    resp.raise_for_status()
    return resp.json()


async def register_variant(
    client: httpx.AsyncClient,
    camera: str,
    tier: QualityTier,
    base_url: str = GO2RTC_API,
) -> bool:
    """Register a quality variant stream in go2rtc using ffmpeg transcoding."""
    variant_name = make_variant_name(camera, tier)
    source = f"ffmpeg:{camera}#video=h264#width={tier.width}#height={tier.height}"

    try:
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


async def setup_live_variants(
    client: httpx.AsyncClient,
    tiers: list[QualityTier],
    base_url: str = GO2RTC_API,
) -> dict[str, list[str]]:
    """Register variant streams for all cameras in go2rtc.

    Returns dict mapping camera -> list of registered variant names.
    """
    streams = await get_streams(client, base_url)

    cameras = [
        name
        for name in streams
        if name != "birdseye" and not is_variant_stream(name)
    ]

    results: dict[str, list[str]] = {}
    for camera in cameras:
        variants = []
        for tier in tiers:
            ok = await register_variant(client, camera, tier, base_url)
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
