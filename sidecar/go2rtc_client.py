"""Client for go2rtc REST API to register ABR stream variants."""

import logging
import re

import httpx

from .transcoder import QualityTier, _parse_bitrate_kbps

logger = logging.getLogger(__name__)

GO2RTC_API = "http://127.0.0.1:1984"

ABR_VARIANT_PREFIX = "_abr_"


def make_variant_name(camera: str, tier: QualityTier) -> str:
    """Build the go2rtc stream name for an ABR variant."""
    return f"{camera}{ABR_VARIANT_PREFIX}{tier.name}"


def is_variant_stream(name: str) -> bool:
    """Check if a stream name is an ABR variant we created."""
    return ABR_VARIANT_PREFIX in name


def make_variant_source(
    camera: str, tier: QualityTier, enforce_bitrate: bool = True
) -> str:
    """Build the go2rtc ffmpeg source string for an ABR variant.

    Transcodes the camera's video to H264 at the tier resolution and copies
    the source audio through unchanged. The ``#audio=copy`` directive is
    required: once an ffmpeg source specifies any ``#video=`` transcode
    option, go2rtc produces a video-only stream unless audio is requested
    too, which leaves the live transcoded stream silent. Copying (rather
    than re-encoding) preserves the original audio codec and lets go2rtc
    transcode it per-consumer (opus for WebRTC, AAC for MSE) exactly as it
    already does for the original stream.

    With enforce_bitrate (the default), the tier's bitrate is applied to the
    encoder via go2rtc's ``#raw=`` parameter, capping the video track only;
    copied audio rides on top of the cap. Invalid bitrate strings are skipped
    with a warning rather than injected.
    """
    source = (
        f"ffmpeg:{camera}#video=h264"
        f"#width={tier.width}#height={tier.height}#audio=copy"
    )
    if enforce_bitrate:
        if is_valid_bitrate(tier.bitrate):
            kbps = _parse_bitrate_kbps(tier.bitrate)
            source += f"#raw=-b:v {kbps}k -maxrate {kbps}k -bufsize {2 * kbps}k"
        else:
            logger.warning(
                "Tier %s has invalid bitrate %r; live variant registered "
                "without bitrate enforcement",
                tier.name,
                tier.bitrate,
            )
    return source


# Bitrate strings we are willing to inject into the go2rtc source. Anything
# else (units like "kbps", spaces, ffmpeg flags, go2rtc {template} braces)
# would end up inside the ffmpeg command line and can kill the variant.
_BITRATE_RE = re.compile(r"^\d+(\.\d+)?[kKmM]?$")


def is_valid_bitrate(value) -> bool:
    """True if value is a bitrate string safe to pass to ffmpeg (-b:v)."""
    return isinstance(value, str) and bool(_BITRATE_RE.match(value.strip()))


def find_missing_variants(
    streams: dict[str, dict], tiers: list[QualityTier]
) -> list[tuple[str, QualityTier]]:
    """Return (camera, tier) pairs whose variant stream is not registered.

    go2rtc loses API-registered streams whenever it restarts (crash, or
    Frigate rewriting its config), so the reconcile loop compares the live
    stream list against what should exist and re-registers the difference.
    New cameras appearing after startup are covered by the same comparison.
    """
    missing = []
    for name in streams:
        if name == "birdseye" or is_variant_stream(name):
            continue
        for tier in tiers:
            if make_variant_name(name, tier) not in streams:
                missing.append((name, tier))
    return missing


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
    enforce_bitrate: bool = True,
) -> bool:
    """Register a quality variant stream in go2rtc using ffmpeg transcoding."""
    variant_name = make_variant_name(camera, tier)
    source = make_variant_source(camera, tier, enforce_bitrate=enforce_bitrate)

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
    enforce_bitrate: bool = True,
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
            ok = await register_variant(
                client, camera, tier, base_url, enforce_bitrate=enforce_bitrate
            )
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


async def reconcile_live_variants(
    client: httpx.AsyncClient,
    tiers: list[QualityTier],
    base_url: str = GO2RTC_API,
    enforce_bitrate: bool = True,
) -> int:
    """Re-register any variant stream missing from go2rtc. Returns count."""
    streams = await get_streams(client, base_url)
    missing = find_missing_variants(streams, tiers)
    registered = 0
    for camera, tier in missing:
        if await register_variant(
            client, camera, tier, base_url, enforce_bitrate=enforce_bitrate
        ):
            registered += 1
    if registered:
        logger.info(
            "Reconcile: re-registered %d missing live variant(s)", registered
        )
    return registered
