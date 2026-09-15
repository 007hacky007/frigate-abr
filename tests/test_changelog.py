"""Tests for the release-notes extractor.

Run with: python3 -m pytest tests/test_changelog.py -v
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
_spec = importlib.util.spec_from_file_location(
    "changelog_section", ROOT / "scripts" / "changelog-section.py"
)
changelog_section = importlib.util.module_from_spec(_spec)
sys.modules["changelog_section"] = changelog_section
_spec.loader.exec_module(changelog_section)

section = changelog_section.section

SAMPLE = """# Changelog

Preamble that belongs to no release.

## [Unreleased]

## [1.1.0] - 2026-10-01

### Added

- A thing.

## [1.0.0] - 2026-09-15

### Fixed

- An older thing.

[Unreleased]: https://example.invalid/compare/v1.1.0...HEAD
[1.1.0]: https://example.invalid/releases/tag/v1.1.0
"""


class TestSection:
    def test_returns_the_requested_release(self):
        assert section(SAMPLE, "1.1.0") == "### Added\n\n- A thing."

    def test_stops_at_the_next_release(self):
        assert "older thing" not in section(SAMPLE, "1.1.0")

    def test_reads_the_last_release_without_the_link_definitions(self):
        body = section(SAMPLE, "1.0.0")
        assert body == "### Fixed\n\n- An older thing."

    def test_skips_the_preamble(self):
        assert "Preamble" not in section(SAMPLE, "1.0.0")

    def test_unknown_version_is_an_error(self):
        """The release job fails rather than publishing empty notes."""
        with pytest.raises(LookupError):
            section(SAMPLE, "9.9.9")

    def test_empty_section_is_an_error(self):
        with pytest.raises(LookupError):
            section(SAMPLE, "Unreleased")

    def test_real_changelog_has_notes_for_the_current_release(self):
        text = (ROOT / "CHANGELOG.md").read_text()
        assert "Safari" in section(text, "1.0.0")
