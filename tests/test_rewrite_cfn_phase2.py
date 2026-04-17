"""Tests for Phase 2 rewrite rules (R6-R10) — KMS / Peering / Route53 / IAM / S3.

Uses tests/fixtures/sample_cfn_phase2.yml which exercises all five new rules.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from rewrite_cfn import (  # type: ignore  # noqa: E402
    ALL_RULES,
    PHASE1_RULES,
    PHASE2_RULES,
    dump_yaml,
    find_external_account_ids,
    is_hosted_zone_id,
    is_kms_arn,
    load_yaml,
    rewrite_template,
)

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# R6: KMS CMK ARN → !Ref KmsKeyArn + Parameter
# ---------------------------------------------------------------------------


def test_r6_kms_arn_rewritten():
    """Standalone KMS CMK ARN values are replaced with !Ref KmsKeyArn."""
    template = load_yaml(FIXTURES / "sample_cfn_phase2.yml")
    rewrite_template(template, rules={"kms"})
    output = dump_yaml(template)

    assert "!Ref KmsKeyArn" in output
    assert "KmsKeyArn" in template["Parameters"]
    # Helper still recognises the ARN pattern.
    assert is_kms_arn(
        "arn:aws:kms:ap-northeast-1:123456789012:key/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    )


# ---------------------------------------------------------------------------
# R7: Peering / TGW attachment flagged as review decision
# ---------------------------------------------------------------------------


def test_r7_peering_flagged_not_rewritten():
    """Peering and TGW attachments produce D.peering review entries."""
    template = load_yaml(FIXTURES / "sample_cfn_phase2.yml")
    decisions: list[dict] = []
    rewrite_template(template, rules={"peering"}, review_decisions=decisions)

    ids = [d["id"] for d in decisions]
    categories = {d.get("category") for d in decisions}
    assert "peering" in categories
    assert "D.peering.MyappPeering" in ids
    assert "D.peering.MyappTgwAttachment" in ids
    # Template itself should be unchanged for this rule (no auto-rewrite).
    assert template["Resources"]["MyappPeering"]["Type"] == "AWS::EC2::VPCPeeringConnection"


# ---------------------------------------------------------------------------
# R8: Route53 hosted zone id
# ---------------------------------------------------------------------------


def test_r8_hosted_zone_id_rewritten():
    """Route53 zone IDs under HostedZoneId key become !Ref HostedZoneId."""
    template = load_yaml(FIXTURES / "sample_cfn_phase2.yml")
    rewrite_template(template, rules={"route53"})
    output = dump_yaml(template)

    assert "!Ref HostedZoneId" in output
    assert "HostedZoneId" in template["Parameters"]
    assert is_hosted_zone_id("Z1234567890ABCDEFGHIJ")
    assert not is_hosted_zone_id("Zshort")


# ---------------------------------------------------------------------------
# R9: IAM principal — external account flagged
# ---------------------------------------------------------------------------


def test_r9_iam_external_account_flagged():
    """IAM trust with external account ID produces a D.iam-trust review entry."""
    template = load_yaml(FIXTURES / "sample_cfn_phase2.yml")
    decisions: list[dict] = []
    rewrite_template(
        template,
        account_id="123456789012",
        rules={"iam_principal"},
        review_decisions=decisions,
    )

    cats = {d.get("category") for d in decisions}
    assert "iam-trust" in cats
    iam_entries = [d for d in decisions if d.get("category") == "iam-trust"]
    assert iam_entries
    assert any("999988887777" in d.get("detail", "") for d in iam_entries)

    # Pure helper
    assert find_external_account_ids(
        "arn:aws:iam::999988887777:root", "123456789012"
    ) == ["999988887777"]
    assert find_external_account_ids(
        "arn:aws:iam::123456789012:root", "123456789012"
    ) == []


# ---------------------------------------------------------------------------
# R10: S3 bucket name → !Sub '${BucketPrefix}-<orig>'
# ---------------------------------------------------------------------------


def test_r10_s3_bucket_name_prefixed():
    """Literal BucketName is wrapped with BucketPrefix parameter via !Sub."""
    template = load_yaml(FIXTURES / "sample_cfn_phase2.yml")
    rewrite_template(template, rules={"s3_bucket_name"})
    output = dump_yaml(template)

    assert "BucketPrefix" in template["Parameters"]
    assert "${BucketPrefix}-myapp-encrypted-bucket" in output
    assert "${BucketPrefix}-myapp-global-data-bucket" in output


def test_phase2_rule_sets_include_new_rules():
    """Rule set constants contain the Phase 2 rule names."""
    assert {"kms", "peering", "route53", "iam_principal", "s3_bucket_name"} <= PHASE2_RULES
    assert PHASE1_RULES.isdisjoint(PHASE2_RULES)
    assert ALL_RULES == PHASE1_RULES | PHASE2_RULES
