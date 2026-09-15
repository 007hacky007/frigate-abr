#!/usr/bin/env python3
"""Print the CHANGELOG.md section for one release.

The release job turns this into the GitHub release notes, so the two cannot
drift. A version with no entries is an error: better a failed release job than a
release published with empty notes.

Usage: changelog-section.py 1.2.3 [path/to/CHANGELOG.md]
"""

import re
import sys
from pathlib import Path

DEFAULT_CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"

# "## [1.2.3] - 2026-09-15", the Keep a Changelog release heading.
HEADING = re.compile(r"^## \[([^\]]+)\]")
# "[1.2.3]: https://..." link definitions, which trail the final section.
LINK_DEFINITION = re.compile(r"^\[[^\]]+\]:\s+\S+")


def section(text: str, version: str) -> str:
    """Return the body of one release section. Raises LookupError if empty."""
    body: list[str] = []
    collecting = False
    for line in text.splitlines():
        heading = HEADING.match(line)
        if heading:
            if collecting:
                break
            collecting = heading.group(1) == version
            continue
        if collecting and not LINK_DEFINITION.match(line):
            body.append(line)

    notes = "\n".join(body).strip()
    if not notes:
        raise LookupError(f"CHANGELOG.md has no entries for {version}")
    return notes


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    path = Path(argv[2]) if len(argv) > 2 else DEFAULT_CHANGELOG
    try:
        print(section(path.read_text(), argv[1]))
    except LookupError as e:
        print(e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
