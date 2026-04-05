# frigate-abr

Adaptive bitrate streaming overlay for [Frigate NVR](https://github.com/blakeblackshear/frigate). Adds multi-quality streaming for both live and recorded footage with GPU-accelerated transcoding - **zero Frigate source modifications required**.

## What it does

- **Live streams**: Registers lower-resolution stream variants in go2rtc (e.g. `camera_abr_720p`). go2rtc only transcodes when a viewer connects, so idle variants cost nothing.
- **Recordings**: Transcodes recording segments on-demand via Intel QSV/VAAPI, caches the results, and serves them as HLS. Each 10-second segment transcodes in ~1-2 seconds.
- **Quality selector**: A gear icon injected into every video player lets you pick quality. Switching reloads the page with the new setting.

## Quality tiers

| Tier | Resolution | Bitrate | Use case |
|------|-----------|---------|----------|
| Original | Native (e.g. 4K) | Passthrough | LAN / fast connections |
| 1080p | 1920x1080 | 2500k | Broadband |
| 720p | 1280x720 | 1200k | Mobile / moderate |
| 480p | 854x480 | 500k | Slow connections |

These are conservative defaults tuned for security camera footage (mostly static scenes). Configurable in `config.yml`.

## Installation

Replace your Frigate image with the frigate-abr image. Everything is baked in.

**1. Change your docker-compose.yml (or Portainer stack):**

```yaml
services:
  frigate:
    image: ghcr.io/007hacky007/frigate-abr:latest   # was: ghcr.io/blakeblackshear/frigate:stable
    # everything else stays exactly the same
```

**2. (Optional) Mount your own ABR config to customize tiers/cache:**

```yaml
    volumes:
      # ... your existing volumes ...
      - ./config-abr.yml:/opt/frigate-abr/config.yml:ro
```

**3. Restart:**

```bash
docker compose up -d
```

That's it. The image is based on Frigate `0.17.1` with the ABR overlay pre-installed.

Available tags:
- `latest` - latest build from master branch
- `frigate-0.17.1` - pinned to specific Frigate version

To build locally:

```bash
git clone https://github.com/007hacky007/frigate-abr.git
cd frigate-abr
docker build -t frigate-abr .

# Pin to a different Frigate version:
docker build --build-arg FRIGATE_VERSION=0.17.1 -t frigate-abr .
```

## Hardware acceleration

The sidecar auto-detects your hwaccel preset from Frigate's config. Override in `config.yml` if needed:

| Hardware | Value |
|----------|-------|
| NVIDIA | `preset-nvidia` |
| Intel iGPU (VAAPI) | `preset-vaapi` |
| Intel iGPU (QSV) | `preset-intel-qsv-h264` |
| AMD (VAAPI) | `preset-vaapi` |
| Rockchip | `preset-rkmpp` |
| Raspberry Pi | `preset-rpi-64-h264` |
| CPU only | `default` |

For Intel GPUs, VOD transcoding uses QSV (decode + scale + encode entirely on GPU). Live transcoding is handled by go2rtc.

## Usage

1. Open Frigate's web UI.
2. A **gear icon** appears in the top-right corner of each video player.
3. Click it to select quality: Original, 1080p, 720p, or 480p.
4. For **live view** - switching quality reconnects to a lower-res go2rtc stream.
5. For **recordings** - segments are transcoded on-demand and cached.

## Configuration reference

`config.yml`:

```yaml
enabled: true

tiers:
  - name: "1080p"
    width: 1920
    height: 1080
    bitrate: "2500k"
  - name: "720p"
    width: 1280
    height: 720
    bitrate: "1200k"
  - name: "480p"
    width: 854
    height: 480
    bitrate: "500k"

cache:
  path: /tmp/cache/abr
  max_size_gb: 10.0    # LRU eviction when exceeded
  ttl_hours: 24         # Cached segments expire after this

max_concurrent_transcodes: 2   # Limits simultaneous GPU transcodes

# Auto-detected from Frigate config. Override if needed:
# hwaccel: preset-nvidia
# gpu: 0
```

## API endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /abr/health` | Health check with version/commit |
| `GET /abr/config` | Returns tiers, cache stats, enabled state |
| `GET /abr/stats` | Active transcodes, cache size, hwaccel info |
| `GET /abr/debug/transcode` | Test single segment transcode with diagnostics |
| `POST /abr/live/setup` | Manually re-register go2rtc stream variants |

## Verify

```bash
# Check logs for successful startup
docker compose logs frigate | grep "\[ABR\]"

# Check sidecar health (should show version and commit)
curl http://localhost:5000/abr/health

# Check transcoding works
curl http://localhost:5000/abr/debug/transcode?camera=YOUR_CAMERA&quality=480p
```

## How it works

1. **S6 oneshot** (`abr-patch`) patches `nginx.conf` before nginx starts - adds upstream, location blocks, and `sub_filter` for JS injection.
2. **S6 longrun** (`abr-sidecar`) runs a FastAPI service that registers go2rtc stream variants, generates HLS playlists, transcodes segments on-demand, and manages the cache.
3. **Frontend overlay** (`inject.js`) intercepts XHR/WebSocket requests to rewrite URLs based on the selected quality.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| No gear icon on video players | Check `docker compose logs frigate \| grep ABR` for patch errors. |
| Grey/black screen on ABR quality (live) | **Firefox autoplay restriction.** Click the lock icon in address bar -> Permissions -> Autoplay -> Allow Audio and Video. Chrome works without this. |
| Transcoding fails | Run `curl localhost:5000/abr/debug/transcode?camera=YOUR_CAMERA&quality=480p` and check `ffmpeg_exit_code` and `ffmpeg_stderr`. |
| Cache growing too large | Lower `cache.max_size_gb` or `cache.ttl_hours` in `config.yml`. |

## Frigate update compatibility

The overlay does not modify any Frigate source files. On Frigate update, the nginx patch re-applies automatically (idempotent). If Frigate changes `nginx.conf` structure significantly, the sed patterns in `abr-patch/run` may need updating - the patch logs clearly when it fails.

## License

MIT
