"""Tests for the ABR cache manager.

Run with: python3 -m pytest tests/test_cache.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sidecar import cache as cache_mod
from sidecar.cache import CACHE_VERSION, VERSION_FILE, ABRCacheManager


def _seed(tmp_path: Path, count: int = 3) -> None:
    for i in range(count):
        (tmp_path / f"seg{i}_720p_abc.ts").write_bytes(b"x" * 16)


class TestPurgeIfStale:
    def test_wipes_cache_written_by_an_older_version(self, tmp_path):
        _seed(tmp_path)
        (tmp_path / VERSION_FILE).write_text(f"{CACHE_VERSION - 1}\n")
        m = ABRCacheManager(str(tmp_path))
        assert m.purge_if_stale() == 3
        assert m.get_file_count() == 0

    def test_wipes_unversioned_cache(self, tmp_path):
        """A cache directory from a build that predates versioning has no
        marker, and nothing about its contents can be assumed."""
        _seed(tmp_path)
        m = ABRCacheManager(str(tmp_path))
        assert m.purge_if_stale() == 3
        assert m.get_file_count() == 0

    def test_keeps_cache_when_version_matches(self, tmp_path):
        _seed(tmp_path)
        (tmp_path / VERSION_FILE).write_text(f"{CACHE_VERSION}\n")
        m = ABRCacheManager(str(tmp_path))
        assert m.purge_if_stale() == 0
        assert m.get_file_count() == 3

    def test_records_version_so_the_next_start_keeps_the_cache(self, tmp_path):
        m = ABRCacheManager(str(tmp_path))
        m.purge_if_stale()
        _seed(tmp_path)
        assert m.purge_if_stale() == 0
        assert m.get_file_count() == 3

    def test_clear_on_start_wipes_a_current_cache(self, tmp_path):
        _seed(tmp_path)
        (tmp_path / VERSION_FILE).write_text(f"{CACHE_VERSION}\n")
        m = ABRCacheManager(str(tmp_path), clear_on_start=True)
        assert m.purge_if_stale() == 3
        assert m.get_file_count() == 0

    def test_clear_on_start_defaults_off(self, tmp_path):
        _seed(tmp_path)
        (tmp_path / VERSION_FILE).write_text(f"{CACHE_VERSION}\n")
        assert ABRCacheManager(str(tmp_path)).purge_if_stale() == 0

    def test_marker_file_is_not_counted_as_a_cached_segment(self, tmp_path):
        m = ABRCacheManager(str(tmp_path))
        m.purge_if_stale()
        assert (tmp_path / VERSION_FILE).exists()
        assert m.get_file_count() == 0

    def test_bumping_the_version_invalidates_a_current_cache(self, tmp_path, monkeypatch):
        _seed(tmp_path)
        (tmp_path / VERSION_FILE).write_text(f"{CACHE_VERSION}\n")
        monkeypatch.setattr(cache_mod, "CACHE_VERSION", CACHE_VERSION + 1)
        assert ABRCacheManager(str(tmp_path)).purge_if_stale() == 3
