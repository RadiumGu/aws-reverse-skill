"""Integration tests for Hotfix A Wave A3 — end-to-end VPC rewrite + review + advice.

These drive the full pipeline from fixture → rewrite_cfn → generate_review /
deployment_advice and assert on the rendered artefacts, to prove R11/R12 and
the new PrefixList advice compose correctly.
"""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from deployment_advice import generate_advice, render_markdown  # noqa: E402
from generate_review import build_review_markdown  # noqa: E402
from rewrite_cfn import (  # noqa: E402
    dump_yaml,
    load_yaml,
    rewrite_template,
)

FIXTURES = Path(__file__).parent / "fixtures"
REPO_ROOT = Path(__file__).parent.parent


def test_e2e_vpc_resource_rewrite(tmp_path: Path) -> None:
    """Load sample_cfn_vpc.yml, apply cross-account preset → verify Parameters + decisions."""
    template = load_yaml(FIXTURES / "sample_cfn_vpc.yml")
    decisions: list[dict] = []
    rewrite_template(
        template,
        account_id="123456789012",
        source_region="ap-northeast-1",
        rules={
            "account_id", "region", "ami_id", "az", "deletion_policy",
            "kms", "iam_principal", "s3_bucket_name", "peering",
            "vpc_resource_ids", "sg_cidr",
        },
        review_decisions=decisions,
        source_vpc_cidrs=["10.0.0.0/16"],
    )

    body = dump_yaml(template)

    # R11 parameterization produced the expected CFN intrinsics.
    assert "!Ref TargetVpcId" in body
    assert "TargetSubnetIds" in body
    assert "TargetSecurityGroupIds" in body

    # Literal source IDs are gone from the Resources body (Parameter
    # descriptions are allowed to mention them for traceability).
    from ruamel.yaml.comments import CommentedMap

    resources_only = CommentedMap()
    resources_only["Resources"] = template["Resources"]
    resources_body = dump_yaml(resources_only)
    assert "vpc-0abcdef1234567890" not in resources_body
    assert "subnet-0abc111aaa2222aaa" not in resources_body
    assert "sg-0aaa111222333aaaa" not in resources_body

    # Review decisions now carry vpc-resource-mapping entries.
    kinds = {d.get("kind") or d.get("id", "").split(".")[1] for d in decisions}
    assert "vpc-resource-mapping" in kinds
    mapping_ids = {d["id"] for d in decisions if d.get("kind") == "vpc-resource-mapping"}
    assert "D.vpc-resource-mapping.TargetVpcId" in mapping_ids
    assert "D.vpc-resource-mapping.TargetSubnetIds" in mapping_ids
    assert "D.vpc-resource-mapping.TargetSecurityGroupIds" in mapping_ids


def test_e2e_sg_cidr_rewrite(tmp_path: Path) -> None:
    """SG CIDR fixture → rewrite → review.md contains both VPC-mapping and SG-CIDR sections."""
    template = load_yaml(FIXTURES / "sample_cfn_sg_cidr.yml")
    decisions: list[dict] = []
    rewrite_template(
        template,
        account_id="",
        source_region="ap-northeast-1",
        rules={"vpc_resource_ids", "sg_cidr"},
        review_decisions=decisions,
        source_vpc_cidrs=["10.0.0.0/16"],
    )

    # vpc-internal CIDR got rewritten to !Ref TargetVpcCidr; external ones kept.
    body = dump_yaml(template)
    assert "!Ref TargetVpcCidr" in body
    assert "192.168.10.0/24" in body
    assert "203.0.113.5/32" in body
    assert "0.0.0.0/0" in body

    review_md = build_review_markdown(
        template,
        raw_resources=[],
        external_decisions=decisions,
        stack_name="integration-sg-cidr",
        preset="cross-account",
    )

    review_path = tmp_path / "review.md"
    review_path.write_text(review_md, encoding="utf-8")

    # Both the R11 mapping table and the R12 audit block render in the same
    # review.md — this is the primary admin artefact for Hotfix A.
    assert "🔧 VPC 资源映射" in review_md
    assert "🌐 SG CIDR 审阅" in review_md
    # SG CIDR block surfaces the classifications captured by R12.
    assert "rfc1918-external" in review_md
    assert "public" in review_md


def test_e2e_deployment_advice_prefix_list(tmp_path: Path) -> None:
    """Synthesised raw.json with 2 SGs sharing a public CIDR → md output contains PrefixList."""
    raw = {
        "resources": [
            {
                "Type": "AWS::EC2::SecurityGroup",
                "PhysicalId": "sg-front",
                "SecurityGroupIngress": [
                    {
                        "IpProtocol": "tcp",
                        "FromPort": 443,
                        "ToPort": 443,
                        "CidrIp": "198.51.100.0/24",
                    },
                ],
            },
            {
                "Type": "AWS::EC2::SecurityGroup",
                "PhysicalId": "sg-back",
                "SecurityGroupIngress": [
                    {
                        "IpProtocol": "tcp",
                        "FromPort": 8080,
                        "ToPort": 8080,
                        "CidrIp": "198.51.100.0/24",
                    },
                ],
            },
        ]
    }

    raw_path = tmp_path / "raw.json"
    raw_path.write_text(json.dumps(raw), encoding="utf-8")

    # Programmatic assertion first (fast feedback).
    advice = generate_advice(raw["resources"])
    titles = " ".join(a.title for a in advice)
    assert "PrefixList" in titles

    md = render_markdown(advice, stack_name="prefix-list-integration")
    assert "PrefixList" in md
    assert "198.51.100.0/24" in md

    # CLI subprocess call — exercises the real --format md path.
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "deployment_advice.py"),
            "--input", str(raw_path),
            "--format", "md",
        ],
        capture_output=True, text=True, check=True,
    )
    assert "PrefixList" in result.stdout
    assert "create-managed-prefix-list" in result.stdout
