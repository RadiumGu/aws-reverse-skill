"""Tests for scripts/deployment_advice.py — rule triggers + renderers."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from deployment_advice import (  # type: ignore  # noqa: E402
    generate_advice,
    render_checklist,
    render_json,
    render_markdown,
    rule_ami,
    rule_iam_trust,
    rule_kms_cross,
    rule_rds,
    rule_s3,
    rule_vpc_peering,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load_raw():
    data = json.loads((FIXTURES / "sample_raw_phase2.json").read_text())
    return data["resources"]


def test_rule_rds_triggers_on_db_instance():
    """RDS rule emits red-priority advice when a DBInstance is scanned."""
    advice = rule_rds(_load_raw())

    assert len(advice) == 1
    assert advice[0].priority == "red"
    assert "DMS" in " ".join(advice[0].options)
    assert "myapp-db-prod" in advice[0].resources


def test_rule_s3_triggers_and_includes_cli():
    """S3 rule emits a replication command template."""
    advice = rule_s3(_load_raw())

    assert advice
    assert advice[0].layer == "data"
    assert any("aws s3 sync" in c for c in advice[0].commands)


def test_rule_ami_requires_image_id():
    """rule_ami only fires when an EC2 instance has an ImageId value."""
    no_image = [
        {"Type": "AWS::EC2::Instance", "PhysicalId": "i-1", "ImageId": ""},
    ]
    assert rule_ami(no_image) == []

    with_image = [
        {"Type": "AWS::EC2::Instance", "PhysicalId": "i-1",
         "ImageId": "ami-0abc"},
    ]
    advice = rule_ami(with_image)
    assert advice and "ami-0abc" in advice[0].resources


def test_rule_vpc_peering_triggers():
    """Peering rule fires once per detected VPCPeeringConnection."""
    advice = rule_vpc_peering(_load_raw())

    assert advice
    assert advice[0].layer == "network"
    assert "pcx-0abc123def" in advice[0].resources


def test_rule_iam_trust_only_flags_with_iam_arn():
    """rule_iam_trust fires only when a trust policy references an IAM ARN."""
    # Role with an internal service principal should NOT fire.
    service_role = [
        {
            "Type": "AWS::IAM::Role",
            "PhysicalId": "lambda-exec",
            "AssumeRolePolicyDocument": {
                "Statement": [{"Principal": {"Service": "lambda.amazonaws.com"}}]
            },
        }
    ]
    assert rule_iam_trust(service_role) == []

    # Role with external account principal triggers the rule.
    cross = _load_raw()
    advice = rule_iam_trust(cross)
    assert advice
    assert "myapp-cross-account-role" in advice[0].resources


def test_rule_kms_cross_triggers_on_any_cmk():
    """KMS rule fires whenever a CMK resource is present."""
    advice = rule_kms_cross(_load_raw())

    assert advice
    assert advice[0].layer == "identity"
    assert "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" in advice[0].resources


def test_generate_advice_renderers_agree_on_coverage():
    """md / checklist / json renderers all reference the same advice count."""
    advice = generate_advice(_load_raw())
    md = render_markdown(advice, "unit-test")
    checklist = render_checklist(advice)
    payload = json.loads(render_json(advice))

    assert len(payload) == len(advice)
    assert "unit-test" in md
    assert "- [ ]" in checklist
