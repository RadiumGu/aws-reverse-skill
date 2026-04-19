"""Tests for China (aws-cn) and GovCloud (aws-us-gov) partition support.

Covers:
  * KNOWN_REGIONS membership for cn-north-1, cn-northwest-1, us-gov-west-1,
    us-gov-east-1.
  * partition_for_region() returns the correct partition per region.
  * rewrite_region substitutes ${AWS::Region} for China/GovCloud regions.
  * arn_rewriter handles aws-cn and aws-us-gov ARNs symmetrically.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from arn_rewriter import rewrite_arn, rewrite_arns_in_text
from rewrite_cfn import (
    CHINA_REGIONS,
    GOVCLOUD_REGIONS,
    KNOWN_REGIONS,
    partition_for_region,
    rewrite_region,
)


# ---------------------------------------------------------------------------
# KNOWN_REGIONS membership
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "region",
    ["cn-north-1", "cn-northwest-1", "us-gov-west-1", "us-gov-east-1"],
)
def test_known_regions_includes_partition_regions(region):
    assert region in KNOWN_REGIONS


def test_china_and_govcloud_sets_disjoint_from_commercial():
    commercial = KNOWN_REGIONS - CHINA_REGIONS - GOVCLOUD_REGIONS
    assert "us-east-1" in commercial
    assert not CHINA_REGIONS & GOVCLOUD_REGIONS


# ---------------------------------------------------------------------------
# partition_for_region
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "region,expected",
    [
        ("us-east-1", "aws"),
        ("ap-northeast-1", "aws"),
        ("cn-north-1", "aws-cn"),
        ("cn-northwest-1", "aws-cn"),
        ("us-gov-west-1", "aws-us-gov"),
        ("us-gov-east-1", "aws-us-gov"),
        ("totally-unknown-region", "aws"),
    ],
)
def test_partition_for_region(region, expected):
    assert partition_for_region(region) == expected


# ---------------------------------------------------------------------------
# rewrite_region for partition regions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "region",
    ["cn-north-1", "cn-northwest-1", "us-gov-west-1", "us-gov-east-1"],
)
def test_rewrite_region_substitutes_partition_region(region):
    changed, new_val = rewrite_region(region, region)
    assert changed is True
    assert new_val == "${AWS::Region}"


# ---------------------------------------------------------------------------
# ARN rewriter — aws-cn
# ---------------------------------------------------------------------------


def test_rewrite_arn_china_partition_sub_both():
    arn = "arn:aws-cn:lambda:cn-north-1:123456789012:function:my-fn"
    result = rewrite_arn(arn, source_account="123456789012", source_region="cn-north-1")
    assert result["action"] == "sub-both"
    assert "${AWS::AccountId}" in result["new_arn"]
    assert "${AWS::Region}" in result["new_arn"]
    assert result["new_arn"].startswith("arn:aws-cn:")


def test_rewrite_arn_china_partition_sub_account_only():
    arn = "arn:aws-cn:iam::123456789012:role/my-role"
    result = rewrite_arn(arn, source_account="123456789012", source_region="cn-north-1")
    assert result["action"] == "sub-account"
    assert "${AWS::AccountId}" in result["new_arn"]
    assert result["new_arn"].startswith("arn:aws-cn:iam::")


# ---------------------------------------------------------------------------
# ARN rewriter — aws-us-gov
# ---------------------------------------------------------------------------


def test_rewrite_arn_govcloud_partition_sub_both():
    arn = "arn:aws-us-gov:s3:us-gov-west-1:123456789012:bucket/my-bucket"
    result = rewrite_arn(
        arn, source_account="123456789012", source_region="us-gov-west-1"
    )
    assert result["action"] == "sub-both"
    assert result["new_arn"].startswith("arn:aws-us-gov:")
    assert "${AWS::AccountId}" in result["new_arn"]
    assert "${AWS::Region}" in result["new_arn"]


def test_rewrite_arns_in_text_govcloud_mixed():
    text = (
        "Source: arn:aws-us-gov:lambda:us-gov-west-1:111111111111:function:src "
        "External: arn:aws-us-gov:sns:us-gov-west-1:999999999999:topic"
    )
    result = rewrite_arns_in_text(
        text, source_account="111111111111", source_region="us-gov-west-1"
    )
    actions = {r["action"] for r in result["rewrites"]}
    assert "sub-both" in actions
    assert "parameter" in actions
    assert "${AWS::AccountId}" in result["new_text"]
    assert len(result["parameters_needed"]) == 1
