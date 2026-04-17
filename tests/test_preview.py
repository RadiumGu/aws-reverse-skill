"""Tests for scripts/preview.py — service / tag / region grouping + sampling."""

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"_aws_reverse_{name}", _SCRIPTS_DIR / f"{name}.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_preview = _load("preview")
build_preview = _preview.build_preview
format_preview_text = _preview.format_preview_text


@pytest.fixture()
def preview_resources():
    """Hand-crafted resources covering service / tag / region dimensions."""
    return [
        {"Type": "AWS::Lambda::Function", "PhysicalId": "fn-a",
         "Tags": {"Environment": "prod"}, "Region": "ap-northeast-1"},
        {"Type": "AWS::Lambda::Function", "PhysicalId": "fn-b",
         "Tags": {"Environment": "prod"}, "Region": "ap-northeast-1"},
        {"Type": "AWS::S3::Bucket", "PhysicalId": "bucket-a",
         "Tags": {"Environment": "dev"}, "Region": "us-east-1"},
        {"Type": "AWS::IAM::Role", "PhysicalId": "role-a",
         "Tags": {}, "Region": "ap-northeast-1"},
    ]


def test_preview_group_by_service(preview_resources):
    """group-by=service buckets resources under their AWS service prefix."""
    out = build_preview(preview_resources, group_by="service", sample=1)
    names = [g["name"] for g in out["groups"]]

    assert out["total"] == 4
    assert "Lambda" in names
    assert "S3" in names
    assert "IAM" in names
    lam = next(g for g in out["groups"] if g["name"] == "Lambda")
    assert lam["count"] == 2
    assert len(lam["sample"]) == 1


def test_preview_group_by_tag_with_sample(preview_resources):
    """group-by=tag honors tag_key and respects sample + with_tags."""
    out = build_preview(
        preview_resources,
        group_by="tag",
        tag_key="Environment",
        sample=2,
        with_tags=True,
    )
    names = {g["name"] for g in out["groups"]}

    assert names == {"prod", "dev", "<missing>"}
    prod = next(g for g in out["groups"] if g["name"] == "prod")
    assert prod["count"] == 2
    assert all("Tags" in s for s in prod["sample"])


def test_preview_group_by_region_sort_desc(preview_resources):
    """Groups are ordered by count desc; region grouping handles <unknown>."""
    out = build_preview(preview_resources, group_by="region")

    counts = [g["count"] for g in out["groups"]]
    assert counts == sorted(counts, reverse=True)
    # ap-northeast-1 has 3 resources, us-east-1 has 1
    first = out["groups"][0]
    assert first["name"] == "ap-northeast-1"
    assert first["count"] == 3


def test_preview_empty_input():
    """Empty resource list produces zero groups and zero total."""
    out = build_preview([], group_by="service")
    text = format_preview_text(out)

    assert out["total"] == 0
    assert out["groups"] == []
    assert "Total resources: 0" in text


def test_preview_group_by_tag_requires_tag_key(preview_resources):
    """build_preview raises ValueError if tag_key missing for tag grouping."""
    with pytest.raises(ValueError):
        build_preview(preview_resources, group_by="tag")
