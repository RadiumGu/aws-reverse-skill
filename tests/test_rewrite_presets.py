"""Tests for config/rewrite-presets.json + preset resolution in rewrite_cfn."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from rewrite_cfn import load_presets, resolve_preset_rules  # type: ignore  # noqa: E402


def test_preset_cleanup_only_has_deletion_policy_only():
    """cleanup-only preset enables exactly R5 (deletion_policy) and nothing else."""
    presets = load_presets()
    rules = resolve_preset_rules("cleanup-only", presets)

    assert rules == {"deletion_policy"}


def test_preset_cross_region_enables_r2_r3_r4_r5():
    """cross-region preset covers region/ami/az/deletion_policy."""
    presets = load_presets()
    rules = resolve_preset_rules("cross-region", presets)

    assert rules == {"region", "ami_id", "az", "deletion_policy"}
    assert "account_id" not in rules  # same account, do not rewrite


def test_preset_cross_account_enables_phase2_extensions():
    """cross-account preset enables Phase 1 + KMS + IAM + S3 + peering flags."""
    presets = load_presets()
    rules = resolve_preset_rules("cross-account", presets)

    assert {"account_id", "region", "ami_id", "az", "deletion_policy"} <= rules
    assert {"kms", "iam_principal", "s3_bucket_name", "peering"} <= rules
    # Route53 is only in `full` preset
    assert "route53" not in rules


def test_unknown_preset_raises():
    """Requesting an unknown preset name raises KeyError."""
    presets = load_presets()

    with pytest.raises(KeyError):
        resolve_preset_rules("totally-unknown-preset", presets)
