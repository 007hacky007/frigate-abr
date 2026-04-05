# frigate-abr

Adaptive bitrate streaming overlay for [Frigate NVR](https://github.com/blakeblackshear/frigate). Adds multi-quality streaming for both live and recorded footage with GPU-accelerated transcoding - **zero Frigate source modifications required**.

## What it does

- **Live streams**: Registers lower-resolution stream variants in go2rtc (e.g. `camera_abr_720p`). go2rtc only transcodes when a viewer connects, so idle variants cost nothing.
- **Recordings**: Transcodes recording segments on-demand via ffmpeg with GPU acceleration, caches the results, and serves them as a multi-bitrate HLS master playlist. hls.js handles automatic quality switching.
- **Quality selector**: A gear icon injected into every video player lets you pick: Original, 1080p, 720p, or 480p.

Two installation methods: **Docker image** (recommended - just swap one line) or **volume mounts** (no rebuild needed).

## Quality tiers

| Tier | Resolution | Bitrate | Use case |
|------|-----------|---------|----------|
| Original | Native (e.g. 4K) | Passthrough | LAN / fast connections |
| 1080p | 1920x1080 | 4000k | Broadband |
| 720p | 1280x720 | 2000k | Mobile / moderate |
| 480p | 854x480 | 800k | Slow connections |

Tiers are configurable in `config.yml`.

## Installation

### Option A: Docker image (recommended)

Just replace your Frigate image with the frigate-abr image. Everything is baked in - no extra volumes, no cloning, no internet needed at startup.

**1. Change your docker-compose.yml:**

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
- `latest` - latest build from main branch
- `frigate-0.17.1` - pinned to specific Frigate version
- `1.0.0` - specific frigate-abr release

To build locally instead of pulling:

```bash
git clone https://github.com/007hacky007/frigate-abr.git
cd frigate-abr
docker build -t frigate-abr .

# Or pin to a different Frigate version:
docker build --build-arg FRIGATE_VERSION=0.17.1 -t frigate-abr .
```

---

### Option B: Volume mounts (no rebuild)

If you prefer not to use a custom image, you can inject the overlay into the stock Frigate container via volume mounts.

**1. Clone the repo onto the machine running Frigate:**

```bash
cd /path/to/your/frigate-setup   # where your docker-compose.yml lives
git clone https://github.com/007hacky007/frigate-abr.git
```

**2. Add volumes to your Frigate service**

Edit your `docker-compose.yml` and add these volumes to the `frigate` service:

```yaml
services:
  frigate:
    # ... your existing config ...
    volumes:
      # ... your existing volumes ...

      # ABR sidecar code + config
      - ./frigate-abr/sidecar:/opt/frigate-abr/sidecar:ro
      - ./frigate-abr/config.yml:/opt/frigate-abr/config.yml:ro

      # S6 services (nginx patcher + sidecar process)
      - ./frigate-abr/overlay/s6/abr-patch:/etc/s6-overlay/s6-rc.d/abr-patch:ro
      - ./frigate-abr/overlay/s6/abr-sidecar:/etc/s6-overlay/s6-rc.d/abr-sidecar:ro

      # Register services in s6 pipeline
      - ./frigate-abr/overlay/s6/user-contents/abr-patch-pipeline:/etc/s6-overlay/s6-rc.d/user/contents.d/abr-patch-pipeline:ro
      - ./frigate-abr/overlay/s6/user-contents/abr-sidecar-pipeline:/etc/s6-overlay/s6-rc.d/user/contents.d/abr-sidecar-pipeline:ro

      # Make nginx wait for the patch to apply
      - ./frigate-abr/overlay/s6/nginx-deps/abr-patch:/etc/s6-overlay/s6-rc.d/nginx/dependencies.d/abr-patch:ro

      # Frontend quality selector (JS/CSS)
      - ./frigate-abr/overlay/web/abr:/opt/frigate/web/abr:ro

      # Transcoding cache
      - abr_cache:/tmp/cache/abr

volumes:
  abr_cache:
```

Alternatively, copy `docker-compose.override.yml` from this repo next to your `docker-compose.yml` and adjust the paths. Docker Compose merges override files automatically.

### 3. Configure hardware acceleration

Edit `config.yml`. The sidecar auto-detects your hwaccel preset from Frigate's config, but you can override it:

```yaml
# hwaccel: preset-nvidia
# gpu: 0
```

Common `hwaccel` values (must match what you use in your Frigate config):

| Hardware | Value |
|----------|-------|
| NVIDIA | `preset-nvidia` |
| Intel iGPU (VAAPI) | `preset-vaapi` |
| Intel iGPU (QSV) | `preset-intel-qsv-h264` |
| AMD (VAAPI) | `preset-vaapi` |
| Rockchip | `preset-rkmpp` |
| Raspberry Pi | `preset-rpi-64-h264` |
| CPU only | `default` |

### 4. Restart Frigate

```bash
docker compose down && docker compose up -d
```

### 5. Verify

```bash
# Check logs for successful startup
docker compose logs frigate | grep "\[ABR\]"

# Expected output:
# [ABR] nginx.conf patched successfully.
# [ABR] Starting ABR sidecar service...

# Check sidecar health
curl http://localhost:5000/abr/health
# {"status":"ok"}

# Check config and stats
curl http://localhost:5000/abr/config
curl http://localhost:5000/abr/stats
```

## Usage

1. Open Frigate's web UI.
2. A **gear icon** appears in the top-right corner of each video player.
3. Click it to select quality: Original, 1080p, 720p, or 480p.
4. For **live view** - switching quality reconnects to a lower-res go2rtc stream.
5. For **recordings** - hls.js automatically adapts quality based on bandwidth, or you can force a specific tier.

## Configuration reference

`config.yml`:

```yaml
enabled: true

tiers:
  - name: "1080p"
    width: 1920
    height: 1080
    bitrate: "4000k"
  - name: "720p"
    width: 1280
    height: 720
    bitrate: "2000k"
  - name: "480p"
    width: 854
    height: 480
    bitrate: "800k"

cache:
  path: /tmp/cache/abr
  max_size_gb: 10.0    # LRU eviction when exceeded
  ttl_hours: 24         # Cached segments expire after this

max_concurrent_transcodes: 2   # Limits simultaneous GPU transcodes

# Auto-detected from Frigate config. Override if needed:
# hwaccel: preset-nvidia
# ffmpeg_path: /usr/lib/ffmpeg/7.1/bin/ffmpeg
# gpu: 0
```

## API endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /abr/health` | Health check |
| `GET /abr/config` | Returns tiers, cache stats, enabled state |
| `GET /abr/stats` | Active transcodes, cache size, hwaccel info |
| `GET /abr/vod/{camera}/start/{ts}/end/{ts}/master.m3u8` | ABR master playlist for recordings |
| `POST /abr/live/setup` | Manually re-register go2rtc stream variants |

## How it works internally

1. **S6 oneshot** (`abr-patch`) runs after Frigate starts but before nginx. It patches `nginx.conf` with:
   - An upstream block for the sidecar (port 8090)
   - Location blocks for `/abr/` and `/abr_cache/`
   - A `sub_filter` directive to inject `inject.js` and `inject.css`
2. **S6 longrun** (`abr-sidecar`) starts a FastAPI service that:
   - Registers go2rtc stream variants for live ABR
   - Serves ABR master playlists for recordings
   - Transcodes segments on-demand with ffmpeg + GPU
   - Caches transcoded segments with TTL and LRU eviction
3. **Frontend overlay** (`inject.js`) monkey-patches hls.js and WebSocket to rewrite URLs based on the selected quality.

## Frigate update compatibility

The overlay does not modify any Frigate source files. On Frigate update:

- The nginx patch re-applies on every container start (idempotent).
- If Frigate changes `nginx.conf` structure significantly, the sed patterns in `abr-patch/run` may need updating. The patch logs clearly when it fails.
- If the frontend DOM changes, the MutationObserver-based quality selector injection may need adjustment.
- go2rtc's REST API has been stable across versions.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| No gear icon on video players | Check `docker compose logs frigate \| grep ABR` for patch errors. Verify the `sub_filter` line was added to nginx.conf. |
| Gear icon visible but quality switch has no effect | Check `curl localhost:5000/abr/stats` - the sidecar may not have started. Check logs for Python errors. |
| Transcoding is slow or failing | Verify `hwaccel` in `config.yml` matches your GPU. Run `docker compose logs frigate \| grep ffmpeg` for errors. |
| pip install fails at startup | The container needs internet access on first boot to install Python dependencies (`fastapi`, `uvicorn`, `httpx`, `pyyaml`). They are cached after the first run. |
| Cache growing too large | Lower `cache.max_size_gb` or `cache.ttl_hours` in `config.yml`. |

## License

MIT
