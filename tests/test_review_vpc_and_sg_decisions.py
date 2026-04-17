"""Tests for generate_review.py — VPC resource mapping + SG CIDR decision sections."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from generate_review import (  # noqa: E402
    build_decisions_section,
    build_review_markdown,
    load_raw,
    load_template,
)
from rewrite_cfn import (  # noqa: E402
    load_yaml as load_yaml_rewrite,
    rewrite_template,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _run_rewrite(fixture_name: str, rules, source_vpc_cidrs=("10.0.0.0/16",)):
    template = load_yaml_rewrite(FIXTURES / fixture_name)
    decisions: list[dict] = []
    rewrite_template(
        template,
        rules=set(rules),
        review_decisions=decisions,
        source_vpc_cidrs=list(source_vpc_cidrs),
    )
    return template, decisions


def test_review_md_contains_vpc_resource_mapping_section(tmp_path: Path):
    """build_review_markdown renders a 🔧 VPC 资源映射 block when R11 Parameters exist."""
    template, decisions = _run_rewrite(
        "sample_cfn_vpc.yml", rules={"vpc_resource_ids"}
    )
    md = build_review_markdown(
        template,
        raw_resources=[],
        external_decisions=decisions,
        stack_name="vpc-fixture",
        preset="cross-account",
    )

    assert "🔧 VPC 资源映射" in md
    # Three R11 parameters should appear in the table.
    assert "| TargetVpcId |" in md
    assert "| TargetSubnetIds |" in md
    assert "| TargetSecurityGroupIds |" in md
    # Original source IDs appear so the admin knows what to map.
    assert "vpc-0abcdef1234567890" in md


def test_review_md_contains_sg_cidr_audit_section(tmp_path: Path):
    """build_review_markdown renders a 🌐 SG CIDR 审阅 block with all R12 categories."""
    template, decisions = _run_rewrite(
        "sample_cfn_sg_cidr.yml",
        rules={"sg_cidr", "vpc_resource_ids"},
    )
    md = build_review_markdown(
        template,
        raw_resources=[],
        external_decisions=decisions,
        stack_name="sg-fixture",
        preset="cross-account",
    )

    assert "🌐 SG CIDR 审阅" in md
    # Classifications surfaced
    assert "rfc1918-external" in md
    assert "public" in md
    # Source CIDRs rendered in the table
    assert "192.168.10.0/24" in md
    assert "203.0.113.5/32" in md
    assert "0.0.0.0/0" in md


def test_review_md_groups_sg_decisions_by_resource():
    """SG CIDR decisions are grouped under one heading per resource_logical_id."""
    decisions = [
        {
            "id": "D.sg-cidr-rfc1918.AppSG.Ingress.443.192.168.10.0_24",
            "kind": "sg-cidr-rfc1918",
            "resource_logical_id": "AppSG",
            "direction": "Ingress",
            "port": "443",
            "cidr": "192.168.10.0/24",
            "classification": "rfc1918-external",
            "suggested_action": "Ask admin for CIDR",
        },
        {
            "id": "D.sg-cidr-public-check.AppSG.Ingress.22.203.0.113.5_32",
            "kind": "sg-cidr-public-check",
            "resource_logical_id": "AppSG",
            "direction": "Ingress",
            "port": "22",
            "cidr": "203.0.113.5/32",
            "classification": "public",
            "suggested_action": "Confirm still valid",
        },
        {
            "id": "D.sg-cidr-public-check.WebSG.Ingress.all.0.0.0.0_0",
            "kind": "sg-cidr-public-check",
            "resource_logical_id": "WebSG",
            "direction": "Ingress",
            "port": "all",
            "cidr": "0.0.0.0/0",
            "classification": "public",
            "suggested_action": "Confirm still valid",
        },
    ]
    md = build_decisions_section(decisions)

    # Two per-resource subheadings
    assert "Security Group `AppSG`" in md
    assert "Security Group `WebSG`" in md
    # AppSG subheading precedes WebSG in document order
    assert md.index("Security Group `AppSG`") < md.index("Security Group `WebSG`")
    # Both AppSG rows are inside the AppSG block, not the WebSG block
    app_start = md.index("Security Group `AppSG`")
    web_start = md.index("Security Group `WebSG`")
    app_block = md[app_start:web_start]
    assert "192.168.10.0/24" in app_block
    assert "203.0.113.5/32" in app_block
    # WebSG block should not contain AppSG's specific CIDRs
    web_block = md[web_start:]
    assert "192.168.10.0/24" not in web_block
