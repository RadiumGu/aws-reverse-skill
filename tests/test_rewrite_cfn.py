"""Tests for scripts/rewrite_cfn.py — pure rule functions and template rewrite."""

import sys
from pathlib import Path

import pytest

# Make scripts/ importable without installing the package.
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from rewrite_cfn import (
    is_ami_id,
    is_az,
    load_yaml,
    dump_yaml,
    rewrite_account_id,
    rewrite_region,
    rewrite_template,
)


# ---------------------------------------------------------------------------
# Test 1: rewrite_account_id — pure function
# ---------------------------------------------------------------------------

def test_rewrite_account_id_in_arn():
    """Account ID embedded in an ARN is replaced with ${AWS::AccountId}."""
    value = "arn:aws:iam::123456789012:role/my-role"
    changed, new_val = rewrite_account_id(value, "123456789012")

    assert changed is True
    assert "${AWS::AccountId}" in new_val
    assert "123456789012" not in new_val


def test_rewrite_account_id_bare_value():
    """A bare account ID string is replaced with ${AWS::AccountId}."""
    changed, new_val = rewrite_account_id("123456789012", "123456789012")

    assert changed is True
    assert new_val == "${AWS::AccountId}"


def test_rewrite_account_id_no_match():
    """A string that does not contain the account ID is returned unchanged."""
    changed, new_val = rewrite_account_id("some-other-value", "123456789012")

    assert changed is False
    assert new_val == "some-other-value"


def test_rewrite_account_id_empty_account():
    """Empty account_id skips the rule and returns the original value."""
    changed, new_val = rewrite_account_id("123456789012", "")

    assert changed is False


def test_rewrite_account_id_multiple_occurrences():
    """All occurrences of the account ID are replaced."""
    value = "arn:aws:sts::123456789012:assumed-role/r/123456789012"
    changed, new_val = rewrite_account_id(value, "123456789012")

    assert changed is True
    assert "123456789012" not in new_val
    assert new_val.count("${AWS::AccountId}") == 2


# ---------------------------------------------------------------------------
# Test 2: rewrite_region — pure function
# ---------------------------------------------------------------------------

def test_rewrite_region_exact_match():
    """Exact region string is replaced with ${AWS::Region}."""
    changed, new_val = rewrite_region("ap-northeast-1", "ap-northeast-1")

    assert changed is True
    assert new_val == "${AWS::Region}"


def test_rewrite_region_no_partial_match():
    """Region code embedded in a longer string is NOT replaced (exact-only)."""
    changed, new_val = rewrite_region("bucket-ap-northeast-1-data", "ap-northeast-1")

    assert changed is False
    assert new_val == "bucket-ap-northeast-1-data"


def test_rewrite_region_empty_source():
    """Empty source_region disables the rule."""
    changed, new_val = rewrite_region("ap-northeast-1", "")

    assert changed is False


def test_rewrite_region_unknown_region():
    """A string that is not a known region code is not replaced."""
    changed, new_val = rewrite_region("fake-region-99", "fake-region-99")

    assert changed is False


# ---------------------------------------------------------------------------
# Test 3: is_ami_id helper
# ---------------------------------------------------------------------------

def test_is_ami_id_valid():
    assert is_ami_id("ami-0abcdef1234567890") is True
    assert is_ami_id("ami-12345678") is True


def test_is_ami_id_invalid():
    assert is_ami_id("i-0abcdef123") is False  # EC2 instance id
    assert is_ami_id("sg-0abcdef") is False
    assert is_ami_id("ami-") is False
    assert is_ami_id("ap-northeast-1") is False


# ---------------------------------------------------------------------------
# Test 4: is_az helper
# ---------------------------------------------------------------------------

def test_is_az_valid():
    assert is_az("ap-northeast-1a") is True
    assert is_az("us-east-1b") is True
    assert is_az("eu-west-2c") is True


def test_is_az_invalid():
    assert is_az("ap-northeast-1") is False   # region only, no suffix
    assert is_az("ap-northeast-1z") is False  # 'z' is not a valid AZ letter
    assert is_az("us-east-1") is False


# ---------------------------------------------------------------------------
# Test 5: rewrite_template — account ID rule via fixture
# ---------------------------------------------------------------------------

def test_template_account_id_replaced(sample_cfn_dirty_path):
    """rewrite_template replaces the hardcoded account ID throughout the template."""
    template = load_yaml(sample_cfn_dirty_path)
    rewrite_template(template, account_id="123456789012", source_region="")
    output = dump_yaml(template)

    assert "123456789012" not in output
    assert "${AWS::AccountId}" in output


# ---------------------------------------------------------------------------
# Test 6: rewrite_template — region rule via fixture
# ---------------------------------------------------------------------------

def test_template_region_replaced(sample_cfn_dirty_path):
    """rewrite_template replaces the hardcoded source region (exact matches)."""
    template = load_yaml(sample_cfn_dirty_path)
    rewrite_template(template, account_id="", source_region="ap-northeast-1")
    output = dump_yaml(template)

    assert "${AWS::Region}" in output


# ---------------------------------------------------------------------------
# Test 7: rewrite_template — AMI rule via fixture
# ---------------------------------------------------------------------------

def test_template_ami_replaced(sample_cfn_dirty_path):
    """rewrite_template replaces AMI IDs with !Ref AmiId and adds the parameter."""
    template = load_yaml(sample_cfn_dirty_path)
    rewrite_template(template, account_id="", source_region="")
    output = dump_yaml(template)

    # ImageId properties in resources should now use !Ref AmiId
    assert "ImageId: !Ref AmiId" in output
    # Parameter section should be added
    assert "Parameters" in template
    assert "AmiId" in template["Parameters"]
    # The original AMI ID should only remain in the parameter Description comment
    # (intentional — helps humans know what was replaced)
    resources = template["Resources"]
    assert resources["MyappInstance"]["Properties"]["ImageId"].value == "AmiId"


# ---------------------------------------------------------------------------
# Test 8: rewrite_template — DeletionPolicy rule via fixture
# ---------------------------------------------------------------------------

def test_template_deletion_policy_added(sample_cfn_dirty_path):
    """rewrite_template adds DeletionPolicy: Retain to RDS and S3 resources."""
    template = load_yaml(sample_cfn_dirty_path)
    rewrite_template(template, account_id="", source_region="")

    resources = template["Resources"]

    # RDS DBInstance should have Retain
    assert resources["MyappDatabase"]["DeletionPolicy"] == "Retain"
    # S3 Bucket should have Retain
    assert resources["MyappDataBucket"]["DeletionPolicy"] == "Retain"
    # Lambda function should NOT have DeletionPolicy
    assert "DeletionPolicy" not in resources["MyappProcessorFunction"]


# ---------------------------------------------------------------------------
# Test 9: rewrite_template — AZ rule via fixture
# ---------------------------------------------------------------------------

def test_template_az_replaced(sample_cfn_dirty_path):
    """rewrite_template replaces hardcoded AZ strings with !Select [0, !GetAZs '']."""
    template = load_yaml(sample_cfn_dirty_path)
    rewrite_template(template, account_id="", source_region="")
    output = dump_yaml(template)

    assert "!Select" in output
    assert "!GetAZs" in output
    # Specific AZ strings should be gone
    assert "ap-northeast-1a" not in output
    assert "ap-northeast-1b" not in output
    assert "ap-northeast-1c" not in output


# ---------------------------------------------------------------------------
# Test 10: rewrite_template — full pipeline via fixture
# ---------------------------------------------------------------------------

def test_template_full_pipeline(sample_cfn_dirty_path):
    """All five rules applied together: no hardcoded values remain."""
    template = load_yaml(sample_cfn_dirty_path)
    rewrite_template(
        template,
        account_id="123456789012",
        source_region="ap-northeast-1",
    )
    output = dump_yaml(template)

    # Account ID must be gone from the output
    assert "123456789012" not in output
    # Raw AZ strings must be replaced
    assert "ap-northeast-1a" not in output
    # Rewritten tokens must be present
    assert "${AWS::AccountId}" in output
    assert "!Select" in output
    assert "Parameters" in template
    # AMI IDs replaced in resource ImageId properties (original only in Description)
    resources = template["Resources"]
    assert resources["MyappInstance"]["Properties"]["ImageId"].value == "AmiId"
