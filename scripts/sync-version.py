#!/usr/bin/env python3
"""Propagate the pinned Frigate version from FRIGATE_VERSION to everywhere it appears.

FRIGATE_VERSION at the repo root is the single source of truth. To bump the base:

    echo 0.17.3 > FRIGATE_VERSION
    python3 scripts/sync-version.py
    git commit -am "Bump Frigate base to 0.17.3"

The workflow reads FRIGATE_VERSION directly at build time, so it never needs
syncing. This script rewrites the two places that cannot read a file:

  * Dockerfile     the ARG FRIGATE_VERSION default, used by a bare `docker build .`
  * README.md      the generated tag tables between the GENERATED markers

The published-versions table is built from the tags that actually exist in the
registry, so it lists real pullable images rather than a hand-kept list. That
lookup needs network access; without it the existing table is left untouched.

Run with --check to verify without writing (CI does this; exit 1 means the tree
is out of sync with FRIGATE_VERSION).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT / "FRIGATE_VERSION"
DOCKERFILE = ROOT / "Dockerfile"
README = ROOT / "README.md"

IMAGE = "007hacky007/frigate-abr"
REGISTRY_HOST = "ghcr.io"

# (tag suffix, base-image suffix, description) for the images CI publishes.
VARIANTS = [
    ("", "", "Standard x86_64 (Intel/AMD)"),
    ("-tensorrt", "-tensorrt", "NVIDIA GPU with TensorRT"),
    ("-rocm", "-rocm", "AMD GPU with ROCm"),
]

TAG_RE = re.compile(r"^frigate-(\d+\.\d+\.\d+)(-[a-z0-9]+)?$")


def read_version() -> str:
    version = VERSION_FILE.read_text().strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        sys.exit(f"FRIGATE_VERSION must be a bare version like 0.17.2, got {version!r}")
    return version


def replace_block(text: str, name: str, body: str) -> str:
    """Swap the contents between <!-- BEGIN GENERATED name --> and its END marker."""
    begin, end = f"<!-- BEGIN GENERATED {name} -->", f"<!-- END GENERATED {name} -->"
    pattern = re.compile(
        re.escape(begin) + r"\n.*?\n" + re.escape(end), re.DOTALL
    )
    if not pattern.search(text):
        sys.exit(f"marker block {name!r} not found in README.md")
    return pattern.sub(f"{begin}\n{body.rstrip()}\n{end}", text)


def current_tags_table(version: str) -> str:
    rows = [
        "| Tag | Frigate base image | Use case |",
        "|-----|--------------------|----------|",
    ]
    for tag_suffix, base_suffix, description in VARIANTS:
        rows.append(
            f"| `latest{tag_suffix}` | `frigate:{version}{base_suffix}` | {description} |"
        )
    return "\n".join(rows)


def fetch_published_tags() -> list[str]:
    token_url = (
        f"https://{REGISTRY_HOST}/token"
        f"?scope=repository:{IMAGE}:pull&service={REGISTRY_HOST}"
    )
    with urllib.request.urlopen(token_url, timeout=20) as response:
        token = json.load(response)["token"]
    request = urllib.request.Request(
        f"https://{REGISTRY_HOST}/v2/{IMAGE}/tags/list",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response).get("tags") or []


def published_versions_table(version: str, tags: list[str]) -> str:
    published: dict[str, set[str]] = {}
    for tag in tags:
        match = TAG_RE.match(tag)
        if match:
            published.setdefault(match.group(1), set()).add(match.group(2) or "")
    published.setdefault(version, set()).add("")

    rows = [
        "| Frigate | Standard | NVIDIA (TensorRT) | AMD (ROCm) |",
        "|---------|----------|-------------------|------------|",
    ]
    for release in sorted(published, key=lambda v: tuple(int(p) for p in v.split(".")), reverse=True):
        label = f"**{release}** (current)" if release == version else release
        cells = []
        for tag_suffix, _, _ in VARIANTS:
            has_tag = tag_suffix in published[release]
            cells.append(f"`frigate-{release}{tag_suffix}`" if has_tag else "not built")
        rows.append(f"| {label} | " + " | ".join(cells) + " |")
    return "\n".join(rows)


def render(version: str) -> dict[Path, str]:
    """Return the intended content of every file this script owns."""
    dockerfile = re.sub(
        r"(?m)^ARG FRIGATE_VERSION=.*$",
        f"ARG FRIGATE_VERSION={version}",
        DOCKERFILE.read_text(),
    )

    readme = README.read_text()
    readme = replace_block(readme, "TAGS", current_tags_table(version))
    try:
        tags = fetch_published_tags()
    except (urllib.error.URLError, TimeoutError, OSError, KeyError, ValueError) as exc:
        print(f"note: registry lookup failed ({exc}); leaving the versions table as is")
    else:
        readme = replace_block(readme, "VERSIONS", published_versions_table(version, tags))

    return {DOCKERFILE: dockerfile, README: readme}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="report files that are out of sync and exit 1 instead of writing them",
    )
    args = parser.parse_args()

    version = read_version()
    stale = []
    for path, content in render(version).items():
        if path.read_text() == content:
            continue
        stale.append(path)
        if not args.check:
            path.write_text(content)

    names = ", ".join(p.relative_to(ROOT).as_posix() for p in stale)
    if args.check:
        if stale:
            print(f"out of sync with FRIGATE_VERSION ({version}): {names}")
            print("run: python3 scripts/sync-version.py")
            return 1
        print(f"in sync with FRIGATE_VERSION ({version})")
        return 0

    print(f"updated for {version}: {names}" if stale else f"already in sync ({version})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
