"""Tests for R12 — Security Group CIDR classification."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from rewrite_cfn import (  # noqa: E402
    dump_yaml,
    load_yaml,
    rewrite_template,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _run(template, source_vpc_cidrs=("10.0.0.0/16",)):
    decisions: list[dict] = []
    rewrite_template(
        template,
        rules={"sg_cidr"},
        review_decisions=decisions,
        source_vpc_cidrs=list(source_vpc_cidrs),
    )
    return dump_yaml(template), decisions


def test_r12_vpc_internal_cidr_replaced_with_parameter_ref():
    """A CidrIp inside the source VPC becomes !Ref TargetVpcCidr."""
    template = load_yaml(FIXTURES / "sample_cfn_sg_cidr.yml")
    body, decisions = _run(template)

    assert "!Ref TargetVpcCidr" in body
    # The vpc-internal literal in AppSG must be gone; same for standalone ingress.
    assert "10.0.1.0/24" not in body
    assert "10.0.2.0/24" not in body
    # Parameter created
    assert "TargetVpcCidr" in template["Parameters"]
    assert template["Parameters"]["TargetVpcCidr"]["Type"] == "String"
    # No decision emitted for vpc-internal category
    vpc_internal = [d for d in decisions if d.get("classification") == "vpc-internal"]
    assert vpc_internal == []


def test_r12_rfc1918_external_left_literal_but_decision_emitted():
    """An RFC1918 CIDR outside the source VPC stays literal + emits D.sg-cidr-rfc1918."""
    template = load_yaml(FIXTURES / "sample_cfn_sg_cidr.yml")
    body, decisions = _run(template)

    assert "192.168.10.0/24" in body  # literal preserved

    rfc_decisions = [d for d in decisions if d.get("kind") == "sg-cidr-rfc1918"]
    assert len(rfc_decisions) == 1
    d = rfc_decisions[0]
    assert d["cidr"] == "192.168.10.0/24"
    assert d["direction"] == "Ingress"
    assert d["port"] == "443"
    assert d["resource_logical_id"] == "AppSG"
    assert d["classification"] == "rfc1918-external"
    assert "suggested_action" in d and d["suggested_action"]


def test_r12_public_cidr_emits_informational_decision():
    """Public (non-RFC1918) CIDRs stay literal + emit sg-cidr-public-check decisions."""
    template = load_yaml(FIXTURES / "sample_cfn_sg_cidr.yml")
    body, decisions = _run(template)

    # public literals preserved
    assert "203.0.113.5/32" in body
    assert "0.0.0.0/0" in body
    # IPv6 ::/0 preserved (still classified as public)
    assert "::/0" in body

    public_decisions = [d for d in decisions if d.get("kind") == "sg-cidr-public-check"]
    cidrs = {d["cidr"] for d in public_decisions}
    assert "203.0.113.5/32" in cidrs
    assert "0.0.0.0/0" in cidrs
    # IPv6 route is also flagged
    assert "::/0" in cidrs
    # At least one public decision has Egress direction (the bulk-egress rule)
    assert any(d["direction"] == "Egress" for d in public_decisions)


def test_r12_no_source_vpc_cidrs_is_noop():
    """Without source VPC CIDRs R12 leaves every CidrIp literal and emits no decisions."""
    template = load_yaml(FIXTURES / "sample_cfn_sg_cidr.yml")
    decisions: list[dict] = []
    rewrite_template(
        template,
        rules={"sg_cidr"},
        review_decisions=decisions,
        source_vpc_cidrs=[],
    )
    body = dump_yaml(template)

    # No rewrite → all literals still there
    assert "10.0.1.0/24" in body
    assert "10.0.2.0/24" in body
    assert "192.168.10.0/24" in body
    assert "203.0.113.5/32" in body
    # No TargetVpcCidr parameter
    params = template.get("Parameters", {})
    assert "TargetVpcCidr" not in params
    # No SG CIDR decisions
    sg_decisions = [d for d in decisions if str(d.get("kind", "")).startswith("sg-cidr")]
    assert sg_decisions == []
