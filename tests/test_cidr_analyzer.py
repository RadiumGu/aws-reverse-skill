"""Tests for scripts/cidr_analyzer.py — classify_cidr() pure function."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from cidr_analyzer import classify_cidr  # noqa: E402


def test_vpc_internal_subset_of_single_source():
    """A /24 inside the source VPC /16 is classified as vpc-internal."""
    result = classify_cidr("10.0.1.0/24", ["10.0.0.0/16"])
    assert result["category"] == "vpc-internal"
    assert result["matched_vpc_cidr"] == "10.0.0.0/16"
    assert result["is_rfc1918"] is True
    assert "10.0.0.0/16" in result["notes"]


def test_vpc_internal_exact_match():
    """An exact source VPC CIDR is still classified as vpc-internal (self-subset)."""
    result = classify_cidr("10.0.0.0/16", ["10.0.0.0/16", "172.31.0.0/16"])
    assert result["category"] == "vpc-internal"
    assert result["matched_vpc_cidr"] == "10.0.0.0/16"


def test_rfc1918_external_not_in_source():
    """An RFC1918 range outside the source VPC set is rfc1918-external."""
    result = classify_cidr("192.168.50.0/24", ["10.0.0.0/16"])
    assert result["category"] == "rfc1918-external"
    assert result["matched_vpc_cidr"] is None
    assert result["is_rfc1918"] is True


def test_public_cidr_classified():
    """A plain public CIDR (not AWS first-octet, not RFC1918) is 'public'."""
    result = classify_cidr("8.8.8.0/24", ["10.0.0.0/16"])
    assert result["category"] == "public"
    assert result["matched_vpc_cidr"] is None
    assert result["is_rfc1918"] is False


def test_zero_zero_route_is_public():
    """The any-route 0.0.0.0/0 is always classified as public."""
    result = classify_cidr("0.0.0.0/0", ["10.0.0.0/16"])
    assert result["category"] == "public"
    assert result["matched_vpc_cidr"] is None
    assert result["is_rfc1918"] is False


def test_aws_public_range_heuristic():
    """A CIDR starting with 52.x (not RFC1918) is flagged as aws-public-range."""
    result = classify_cidr("52.95.0.0/20", [])
    assert result["category"] == "aws-public-range"
    assert result["is_rfc1918"] is False


def test_vpc_internal_prefers_first_matching_source():
    """With multiple source VPCs, the matching one is returned."""
    result = classify_cidr("172.31.5.0/24", ["10.0.0.0/16", "172.31.0.0/16"])
    assert result["category"] == "vpc-internal"
    assert result["matched_vpc_cidr"] == "172.31.0.0/16"
