"""Tests for scripts/cache.py — hit, expiry, and key stability."""

import importlib.util
import sys
import time
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"


def _load(name: str):
    mod_name = f"_aws_reverse_{name}"
    spec = importlib.util.spec_from_file_location(
        mod_name, _SCRIPTS_DIR / f"{name}.py"
    )
    mod = importlib.util.module_from_spec(spec)
    # Register before exec so dataclass() can look the module up by __module__.
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


_cache = _load("cache")
make_key = _cache.make_key
read_cache = _cache.read_cache
write_cache = _cache.write_cache


def test_cache_round_trip_hit(tmp_path: Path):
    """write_cache + read_cache returns the stored payload on a fresh entry."""
    key = make_key("111122223333", "ap-northeast-1", "Lambda,IAM")
    write_cache(key, {"resources": [{"PhysicalId": "foo"}]}, directory=tmp_path)
    hit, payload, entry = read_cache(key, directory=tmp_path)

    assert hit is True
    assert payload == {"resources": [{"PhysicalId": "foo"}]}
    assert entry is not None
    assert entry.hit is True


def test_cache_expires_after_ttl(tmp_path: Path):
    """A zero-TTL read treats any stored entry as expired (miss)."""
    key = make_key("111122223333", "us-east-1", "Lambda")
    write_cache(key, {"cached": True}, directory=tmp_path)
    # Force expiry by using ttl_seconds=0.
    time.sleep(0.01)
    hit, payload, entry = read_cache(key, ttl_seconds=0, directory=tmp_path)

    assert hit is False
    assert payload is None
    assert entry is not None
    assert entry.hit is False
