"""Cache manager for transcoded ABR segments."""

import asyncio
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)


class ABRCacheManager:
    def __init__(self, cache_dir: str, max_size_gb: float = 10.0, ttl_hours: int = 24):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_size_bytes = int(max_size_gb * 1024 * 1024 * 1024)
        self.ttl_seconds = ttl_hours * 3600
        self._task: asyncio.Task | None = None

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
            if f.is_file() and f.suffix == ".mp4":
                total += f.stat().st_size
        return total

    def get_cache_size_gb(self) -> float:
        return self.get_cache_size_bytes() / (1024 * 1024 * 1024)

    def get_file_count(self) -> int:
        return sum(1 for f in self.cache_dir.iterdir() if f.is_file() and f.suffix == ".mp4")

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
            if not f.is_file() or f.suffix != ".mp4":
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
            if not f.is_file() or f.suffix != ".mp4":
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
            if f.is_file() and f.suffix == ".mp4":
                f.unlink(missing_ok=True)
                count += 1
        return count
