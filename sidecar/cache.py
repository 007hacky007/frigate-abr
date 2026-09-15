"""Cache manager for transcoded ABR segments."""

import asyncio
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Bump this whenever a release changes what a cached segment contains - encoder
# arguments, container, timestamp handling, anything a player could notice. The
# version is part of every cache key and the sidecar wipes the cache directory
# when it does not match the one recorded there, so a segment produced by an
# incompatible build is never served. Forgetting to bump it means users keep
# being served the old bytes until the TTL expires them.
CACHE_VERSION = 1

# Records the version the cache directory was written by.
VERSION_FILE = "cache_version"


class ABRCacheManager:
    def __init__(
        self,
        cache_dir: str,
        max_size_gb: float = 10.0,
        ttl_hours: int = 24,
        clear_on_start: bool = False,
    ):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_size_bytes = int(max_size_gb * 1024 * 1024 * 1024)
        self.ttl_seconds = ttl_hours * 3600
        self.clear_on_start = clear_on_start
        self._task: asyncio.Task | None = None

    def purge_if_stale(self) -> int:
        """Drop cached segments an incompatible build left behind.

        Called once at startup, before anything is served. A cache directory
        with no version marker predates versioning, so nothing about its
        contents can be assumed and it goes too. Returns files removed.
        """
        marker = self.cache_dir / VERSION_FILE
        try:
            recorded = marker.read_text().strip()
        except OSError:
            recorded = ""

        removed = 0
        if self.clear_on_start:
            removed = self.cleanup_all()
            logger.info("Cleared %d cached segments (clear_on_start)", removed)
        elif recorded != str(CACHE_VERSION):
            removed = self.cleanup_all()
            if removed:
                logger.info(
                    "Cleared %d cached segments written by cache version %s (now %d)",
                    removed, recorded or "unknown", CACHE_VERSION,
                )

        try:
            marker.write_text(f"{CACHE_VERSION}\n")
        except OSError:
            logger.warning("Could not record cache version in %s", marker)
        return removed

    def start(self) -> None:
        """Start the background cleanup loop."""
        self._task = asyncio.create_task(self._cleanup_loop())

    async def stop(self) -> None:
        """Stop the background cleanup loop."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def get_cache_size_bytes(self) -> int:
        """Return total size of cached files in bytes."""
        total = 0
        for f in self.cache_dir.iterdir():
            if f.is_file() and f.suffix in (".mp4", ".ts"):
                total += f.stat().st_size
        return total

    def get_cache_size_gb(self) -> float:
        return self.get_cache_size_bytes() / (1024 * 1024 * 1024)

    def get_file_count(self) -> int:
        return sum(1 for f in self.cache_dir.iterdir() if f.is_file() and f.suffix in (".mp4", ".ts"))

    async def _cleanup_loop(self) -> None:
        """Periodic cleanup every 10 minutes."""
        while True:
            try:
                await asyncio.sleep(600)
                self._evict_expired()
                self._evict_by_size()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Cache cleanup error")

    def _evict_expired(self) -> None:
        """Remove segments older than TTL based on last access time."""
        now = time.time()
        evicted = 0
        for f in self.cache_dir.iterdir():
            if not f.is_file() or f.suffix not in (".mp4", ".ts"):
                continue
            try:
                # Use mtime (touched on access by transcoder)
                age = now - f.stat().st_mtime
                if age > self.ttl_seconds:
                    f.unlink()
                    evicted += 1
            except OSError:
                pass
        if evicted:
            logger.info("Cache TTL eviction: removed %d files", evicted)

    def _evict_by_size(self) -> None:
        """LRU eviction when cache exceeds max size."""
        files = []
        total_size = 0
        for f in self.cache_dir.iterdir():
            if not f.is_file() or f.suffix not in (".mp4", ".ts"):
                continue
            try:
                st = f.stat()
                files.append((f, st.st_mtime, st.st_size))
                total_size += st.st_size
            except OSError:
                pass

        if total_size <= self.max_size_bytes:
            return

        # Sort by mtime ascending (oldest first = least recently used)
        files.sort(key=lambda x: x[1])

        evicted = 0
        for f, _, size in files:
            if total_size <= self.max_size_bytes:
                break
            try:
                f.unlink()
                total_size -= size
                evicted += 1
            except OSError:
                pass

        if evicted:
            logger.info(
                "Cache LRU eviction: removed %d files, size now %.1f GB",
                evicted,
                total_size / (1024 * 1024 * 1024),
            )

    def cleanup_all(self) -> int:
        """Remove all cached files. Returns count removed."""
        count = 0
        for f in self.cache_dir.iterdir():
            if f.is_file() and f.suffix in (".mp4", ".ts"):
                f.unlink(missing_ok=True)
                count += 1
        return count
