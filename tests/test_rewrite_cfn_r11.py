"""Tests for R11 — VPC / Subnet / Security Group ID parameterization."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from rewrite_cfn import (  # noqa: E402
    PHASE1_RULES,
    dump_yaml,
    load_yaml,
    rewrite_template,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _resources_only(template) -> str:
    """Dump only the Resources section so Parameter descriptions do not pollute
    the 'literal ID must be absent' assertions (the descriptions intentionally
    keep the original ID for human traceability — same convention as R3/AMI).
    """
    from ruamel.yaml.comments import CommentedMap  # local import to avoid top-level cost

    subset = CommentedMap()
    subset["Resources"] = template["Resources"]
    return dump_yaml(subset)


def test_r11_replaces_single_vpc_id_with_parameter():
    """A literal VpcId scalar becomes !Ref TargetVpcId + Parameter is added."""
    template = load_yaml(FIXTURES / "sample_cfn_vpc.yml")
    rewrite_template(template, rules={"vpc_resource_ids"})
    body = _resources_only(template)

    assert "!Ref TargetVpcId" in body
    assert "vpc-0abcdef1234567890" not in body
    assert "TargetVpcId" in template["Parameters"]
    assert template["Parameters"]["TargetVpcId"]["Type"] == "AWS::EC2::VPC::Id"


def test_r11_replaces_subnet_ids_list_with_split():
    """SubnetIds / Subnets list of literal subnet IDs becomes !Split expression."""
    template = load_yaml(FIXTURES / "sample_cfn_vpc.yml")
    rewrite_template(template, rules={"vpc_resource_ids"})
    body = _resources_only(template)

    # Both list sites (DBSubnetGroup.SubnetIds and ALB.Subnets) collapse
    assert "!Split [',', !Ref TargetSubnetIds]" in body
    # Single-subnet site (EC2 Instance.SubnetId) uses !Select
    assert "!Select [0, !Split [',', !Ref TargetSubnetIds]]" in body
    # Literal subnet IDs must be gone from the resources body
    assert "subnet-0abc111aaa2222aaa" not in body
    assert "subnet-0def222bbb3333bbb" not in body
    # Parameter declared
    assert "TargetSubnetIds" in template["Parameters"]
    assert (
        template["Parameters"]["TargetSubnetIds"]["Type"] == "CommaDelimitedList"
    )


def test_r11_not_applied_when_preset_excludes_r11():
    """Default Phase 1 rules do NOT rewrite literal VPC/Subnet/SG IDs."""
    template = load_yaml(FIXTURES / "sample_cfn_vpc.yml")
    rewrite_template(template, rules=set(PHASE1_RULES))
    output = dump_yaml(template)

    # R11 was not enabled, so literal IDs survive.
    assert "vpc-0abcdef1234567890" in output
    assert "subnet-0abc111aaa2222aaa" in output
    assert "sg-0aaa111222333aaaa" in output
    # Target* parameters must not be present.
    params = template.get("Parameters", {})
    assert "TargetVpcId" not in params
    assert "TargetSubnetIds" not in params
    assert "TargetSecurityGroupIds" not in params


def test_r11_replaces_security_group_ids_list():
    """SecurityGroupIds list of literal sg-… becomes !Split TargetSecurityGroupIds."""
    template = load_yaml(FIXTURES / "sample_cfn_vpc.yml")
    rewrite_template(template, rules={"vpc_resource_ids"})
    body = _resources_only(template)

    assert "!Split [',', !Ref TargetSecurityGroupIds]" in body
    assert "sg-0aaa111222333aaaa" not in body
    assert "sg-0bbb444555666bbbb" not in body
    assert "TargetSecurityGroupIds" in template["Parameters"]
