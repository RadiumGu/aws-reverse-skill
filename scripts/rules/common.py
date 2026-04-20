"""rules.common — Shared context and helpers used across all rule modules."""

from __future__ import annotations

import re
from typing import Any

from ruamel.yaml.comments import CommentedMap


# ---------------------------------------------------------------------------
# Constants & rule registry
# ---------------------------------------------------------------------------

PHASE1_RULES: frozenset[str] = frozenset(
    {"account_id", "region", "ami_id", "az", "deletion_policy"}
)
PHASE2_RULES: frozenset[str] = frozenset(
    {"kms", "peering", "route53", "iam_principal", "s3_bucket_name"}
)
PHASE3_RULES: frozenset[str] = frozenset(
    {
        "vpc_resource_ids",
        "sg_cidr",
        "nested_arn_string",
        "nested_arn_json",
        "region_lock",
    }
)
ALL_RULES: frozenset[str] = PHASE1_RULES | PHASE2_RULES
ALL_RULES_EXTENDED: frozenset[str] = PHASE1_RULES | PHASE2_RULES | PHASE3_RULES

#: Known AWS region codes — commercial + China (aws-cn) + GovCloud (aws-us-gov).
KNOWN_REGIONS: frozenset[str] = frozenset(
    [
        "us-east-1", "us-east-2", "us-west-1", "us-west-2",
        "ap-northeast-1", "ap-northeast-2", "ap-northeast-3",
        "ap-southeast-1", "ap-southeast-2", "ap-southeast-3", "ap-southeast-4",
        "ap-south-1", "ap-south-2", "ap-east-1",
        "eu-west-1", "eu-west-2", "eu-west-3",
        "eu-central-1", "eu-central-2", "eu-north-1",
        "eu-south-1", "eu-south-2",
        "sa-east-1", "ca-central-1", "ca-west-1",
        "me-south-1", "me-central-1", "af-south-1", "il-central-1",
        # China partition (aws-cn)
        "cn-north-1", "cn-northwest-1",
        # GovCloud partition (aws-us-gov)
        "us-gov-west-1", "us-gov-east-1",
    ]
)

CHINA_REGIONS: frozenset[str] = frozenset({"cn-north-1", "cn-northwest-1"})
GOVCLOUD_REGIONS: frozenset[str] = frozenset({"us-gov-west-1", "us-gov-east-1"})


def partition_for_region(region: str) -> str:
    """Return the AWS partition name for a region code."""
    if region in CHINA_REGIONS:
        return "aws-cn"
    if region in GOVCLOUD_REGIONS:
        return "aws-us-gov"
    return "aws"


# ---------------------------------------------------------------------------
# Compiled patterns
# ---------------------------------------------------------------------------

_AZ_PATTERN: re.Pattern[str] = re.compile(
    r"^("
    + "|".join(re.escape(r) for r in sorted(KNOWN_REGIONS, key=len, reverse=True))
    + r")[a-f]$"
)
_AMI_PATTERN: re.Pattern[str] = re.compile(r"^ami-[0-9a-f]{8,17}$", re.IGNORECASE)
_KMS_ARN_PATTERN: re.Pattern[str] = re.compile(
    r"arn:aws[a-z\-]*:kms:[a-z0-9\-]+:\d{12}:key/[0-9a-f\-]{36}"
)
_HOSTED_ZONE_PATTERN: re.Pattern[str] = re.compile(r"^Z[0-9A-Z]{4,31}$")
_ACCOUNT_ID_PATTERN: re.Pattern[str] = re.compile(r"\b\d{12}\b")

_VPC_ID_PATTERN: re.Pattern[str] = re.compile(r"^vpc-[0-9a-f]{3,17}$", re.IGNORECASE)
_SUBNET_ID_PATTERN: re.Pattern[str] = re.compile(r"^subnet-[0-9a-f]{3,17}$", re.IGNORECASE)
_SG_ID_PATTERN: re.Pattern[str] = re.compile(r"^sg-[0-9a-f]{3,17}$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


class _Context:
    """Mutable accumulator passed through the recursive walk."""

    def __init__(
        self,
        rules: set[str],
        account_id: str,
        source_region: str,
        source_vpc_cidrs: list[str] | None = None,
    ) -> None:
        self.rules = rules
        self.account_id = account_id
        self.source_region = source_region
        self.source_vpc_cidrs: list[str] = list(source_vpc_cidrs or [])
        self.ami_ids: list[str] = []
        self.kms_arns: list[str] = []
        self.hosted_zone_ids: list[str] = []
        self.s3_buckets: list[str] = []
        self.vpc_ids: list[str] = []
        self.subnet_ids: list[str] = []
        self.security_group_ids: list[str] = []
        self.sg_vpc_cidr_used: bool = False
        self.sg_cidr_warnings: list[str] = []
        self.external_arn_parameters: dict[str, dict[str, str]] = {}
        self.nested_arn_warnings: list[str] = []
        self.target_region: str = ""
        self.review_decisions: list[dict[str, Any]] = []


def _record_review(ctx: _Context, decision: dict[str, Any]) -> None:
    """Append a unique review decision to *ctx*."""
    key = (decision.get("id"), decision.get("resource"))
    for existing in ctx.review_decisions:
        if (existing.get("id"), existing.get("resource")) == key:
            return
    ctx.review_decisions.append(decision)


# ---------------------------------------------------------------------------
# Shared constants for resource types
# ---------------------------------------------------------------------------

RETAIN_TYPES: frozenset[str] = frozenset(
    [
        "AWS::RDS::DBInstance",
        "AWS::RDS::DBCluster",
        "AWS::S3::Bucket",
        "AWS::DynamoDB::Table",
        "AWS::EFS::FileSystem",
    ]
)

PEERING_TYPES: frozenset[str] = frozenset(
    [
        "AWS::EC2::VPCPeeringConnection",
        "AWS::EC2::TransitGatewayAttachment",
        "AWS::EC2::TransitGatewayVpcAttachment",
    ]
)

_AMI_SSM_DEFAULT = (
    "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
)

# ---------------------------------------------------------------------------
# VPC key sets (R11)
# ---------------------------------------------------------------------------

_VPC_ID_KEYS: frozenset[str] = frozenset({"VpcId", "VPCId"})
_SUBNET_SINGLE_KEYS: frozenset[str] = frozenset({"SubnetId"})
_SUBNET_LIST_KEYS: frozenset[str] = frozenset(
    {"SubnetIds", "Subnets", "VPCZoneIdentifier"}
)
_SG_SINGLE_KEYS: frozenset[str] = frozenset(
    {"GroupId", "SourceSecurityGroupId", "DestinationSecurityGroupId"}
)
_SG_LIST_KEYS: frozenset[str] = frozenset(
    {"SecurityGroupIds", "VpcSecurityGroupIds", "SecurityGroups"}
)


# ---------------------------------------------------------------------------
# Pure matchers
# ---------------------------------------------------------------------------


def rewrite_account_id(value: str, account_id: str) -> tuple[bool, str]:
    """Replace occurrences of *account_id* in *value* with ``${AWS::AccountId}``."""
    if not account_id or account_id not in value:
        return False, value
    return True, value.replace(account_id, "${AWS::AccountId}")


def rewrite_region(value: str, source_region: str) -> tuple[bool, str]:
    """Replace an exact *source_region* string value with ``${AWS::Region}``."""
    if source_region and value == source_region and source_region in KNOWN_REGIONS:
        return True, "${AWS::Region}"
    return False, value


def is_ami_id(value: str) -> bool:
    return bool(_AMI_PATTERN.match(value))


def is_az(value: str) -> bool:
    return bool(_AZ_PATTERN.match(value))


def is_kms_arn(value: str) -> bool:
    return bool(_KMS_ARN_PATTERN.search(value))


def is_hosted_zone_id(value: str) -> bool:
    return bool(_HOSTED_ZONE_PATTERN.match(value))


def is_vpc_id(value: str) -> bool:
    return isinstance(value, str) and bool(_VPC_ID_PATTERN.match(value))


def is_subnet_id(value: str) -> bool:
    return isinstance(value, str) and bool(_SUBNET_ID_PATTERN.match(value))


def is_sg_id(value: str) -> bool:
    return isinstance(value, str) and bool(_SG_ID_PATTERN.match(value))


def find_external_account_ids(value: str, source_account_id: str) -> list[str]:
    """Return any 12-digit account IDs in *value* that are not *source_account_id*."""
    matches = _ACCOUNT_ID_PATTERN.findall(value)
    return [m for m in matches if m != source_account_id]


def _ensure_parameters(template: CommentedMap) -> CommentedMap:
    """Ensure a ``Parameters`` section exists (inserted before Resources)."""
    if "Parameters" in template:
        return template["Parameters"]
    new_template = CommentedMap()
    inserted = False
    for key in list(template.keys()):
        if key == "Resources" and not inserted:
            new_template["Parameters"] = CommentedMap()
            inserted = True
        new_template[key] = template[key]
    if not inserted:
        new_template["Parameters"] = CommentedMap()
    template.clear()
    template.update(new_template)
    return template["Parameters"]
