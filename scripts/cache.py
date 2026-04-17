#!/usr/bin/env python3
"""cache.py — On-disk cache for former2 raw.json scans.

Cache key = sha256(account + region + services + former2_version).
Location  = ~/.cache/aws-reverse-skill/<key>.json (override via AWS_REVERSE_SKILL_CACHE).
TTL       = 24 hours by default; tunable per call.

Used by scan.js (via python bridge) and by any Python helper that wants to
short-circuit a full scan while iterating on filters.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_TTL_SECONDS = 24 * 3600


def cache_dir() -> Path:
    """Return the cache directory path (honours ``AWS_REVERSE_SKILL_CACHE`` env)."""
    override = os.environ.get("AWS_REVERSE_SKILL_CACHE")
    if override:
        return Path(override)
    home = Path(os.environ.get("HOME", str(Path.home())))
    return home / ".cache" / "aws-reverse-skill"


def make_key(
    account: str,
    region: str,
    services: str,
    former2_version: str = "",
) -> str:
    """Compute a deterministic cache key for the scan invocation."""
    parts = "|".join(
        [
            (account or "").strip(),
            (region or "").strip(),
            ",".join(sorted((services or "").split(","))).strip(),
            (former2_version or "").strip(),
        ]
    )
    return hashlib.sha256(parts.encode("utf-8")).hexdigest()[:32]


def cache_path(key: str, directory: Path | None = None) -> Path:
    """Return the on-disk path for *key*."""
    directory = directory or cache_dir()
    return directory / f"{key}.json"


@dataclass
class CacheEntry:
    key: str
    path: Path
    written_at: float
    ttl_seconds: int
    hit: bool


def read_cache(
    key: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    directory: Path | None = None,
) -> tuple[bool, dict[str, Any] | None, CacheEntry | None]:
    """Read cached payload if fresh.

    Returns (hit, payload, entry). *payload* is ``None`` on miss / expiry.
    """
    path = cache_path(key, directory)
    if not path.exists():
        return False, None, None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False, None, None
    written = raw.get("_cache_written_at", 0)
    if time.time() - written > ttl_seconds:
        return False, None, CacheEntry(
            key=key,
            path=path,
            written_at=written,
            ttl_seconds=ttl_seconds,
            hit=False,
        )
    payload = raw.get("payload")
    return True, payload, CacheEntry(
        key=key,
        path=path,
        written_at=written,
        ttl_seconds=ttl_seconds,
        hit=True,
    )


def write_cache(
    key: str,
    payload: Any,
    directory: Path | None = None,
) -> CacheEntry:
    """Persist *payload* under *key*."""
    directory = directory or cache_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = cache_path(key, directory)
    now = time.time()
    path.write_text(
        json.dumps(
            {"_cache_written_at": now, "key": key, "payload": payload},
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return CacheEntry(
        key=key, path=path, written_at=now, ttl_seconds=DEFAULT_TTL_SECONDS, hit=True
    )


def purge_cache(directory: Path | None = None) -> int:
    """Remove all cache entries. Returns count removed."""
    directory = directory or cache_dir()
    if not directory.exists():
        return 0
    count = 0
    for f in directory.glob("*.json"):
        f.unlink()
        count += 1
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description="aws-reverse-skill cache control")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("dir", help="Print cache directory path")
    purge = sub.add_parser("purge", help="Remove all cache files")
    purge.set_defaults(cmd="purge")

    info = sub.add_parser("info", help="List cache entries")
    info.set_defaults(cmd="info")

    args = parser.parse_args()

    if args.cmd == "dir":
        print(cache_dir())
        return
    if args.cmd == "purge":
        n = purge_cache()
        print(f"Purged {n} cache entries", file=sys.stderr)
        return
    if args.cmd == "info":
        d = cache_dir()
        if not d.exists():
            print("<cache empty>")
            return
        for f in sorted(d.glob("*.json")):
            size = f.stat().st_size
            print(f"{f.name}  {size} bytes")
        return


if __name__ == "__main__":
    main()
