#!/usr/bin/env python3
"""Check that the version is the same everywhere it appears.

    python scripts/check_version.py            # consistency only
    python scripts/check_version.py v0.9.1     # ... and matches this git tag (used by the release workflow)
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def find(path: str, pattern: str) -> str:
    match = re.search(pattern, (ROOT / path).read_text(encoding="utf-8"), re.MULTILINE)
    return match.group(1) if match else "<not found>"


versions = {
    "relay.py __version__": find("relay.py", r'^__version__ = "([^"]+)"'),
    "Dockerfile ARG VERSION": find("Dockerfile", r"^ARG VERSION=(\S+)"),
    "CHANGELOG newest entry": find("CHANGELOG.md", r"^## (\d+\.\d+\.\d+)"),
    "README version line": find("README.md", r"^\*\*Version (\d+\.\d+\.\d+)\*\*"),
    "Unraid template tag": find("unraid/my-signal-media-relay.xml", r"signal-media-relay:(\d+\.\d+\.\d+)</Repository>"),
}
if len(sys.argv) > 1:
    versions["git tag"] = sys.argv[1].removeprefix("refs/tags/").removeprefix("v")

width = max(map(len, versions))
for name, value in versions.items():
    print(f"{name:<{width}}  {value}")
if len(set(versions.values())) != 1:
    print("\nERROR: versions differ – update all of them together.")
    sys.exit(1)
print("\nOK: all versions match.")
