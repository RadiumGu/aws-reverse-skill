#!/usr/bin/env python3
"""rewrite_cfn.py — Rules engine to clean hardcoded values from former2 CFN output.

Phase 1 rules (always-on / default preset):
  R1. Account ID       — 12-digit account IDs → ${AWS::AccountId}
  R2. Region           — source region code → ${AWS::Region}
  R3. AMI ID           — ImageId value → !Ref AmiId + Parameters.AmiId
  R4. Availability Zone — hardcoded AZ → !Select [0, !GetAZs '']
  R5. DeletionPolicy   — Retain on RDS / S3 / DynamoDB / EFS

Phase 2 rules (opt-in via --preset or --rules):
  R6.  KMS CMK ARN                    → !Ref KmsKeyArn + Parameters.KmsKeyArn
  R7.  VPC Peering / TGW attachment   → flagged as review decision (no auto-rewrite)
  R8.  Route53 Hosted Zone ID         → !Ref HostedZoneId + Parameters.HostedZoneId
  R9.  IAM Principal ARN account ID   → ${AWS::AccountId} (and flag external accounts)
  R10. S3 Bucket Name                 → !Sub '${BucketPrefix}-<orig>' + Parameters.BucketPrefix

Usage:
    python scripts/rewrite_cfn.py \\
        --input cfn-filtered.yml --output cleaned.yml \\
        --account-id 123456789012 --source-region ap-northeast-1 \\
        --preset cross-account
"""

import argparse
import copy
import json
import re
import sys
from io import StringIO
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq, TaggedScalar


# ---------------------------------------------------------------------------
# Constants & rule registry
# ---------------------------------------------------------------------------

PHASE1_RULES: frozenset[str] = frozenset(
    {"account_id", "region", "ami_id", "az", "deletion_policy"}
)
PHASE2_RULES: frozenset[str] = frozenset(
    {"kms", "peering", "route53", "iam_principal", "s3_bucket_name"}
)
ALL_RULES: frozenset[str] = PHASE1_RULES | PHASE2_RULES

#: Known AWS region codes (commercial partitions).
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
    ]
)

_AZ_PATTERN: re.Pattern[str] = re.compile(
    r"^("
    + "|".join(re.escape(r) for r in sorted(KNOWN_REGIONS, key=len, reverse=True))
    + r")[a-f]$"
)
_AMI_PATTERN: re.Pattern[str] = re.compile(r"^ami-[0-9a-f]{8,17}$", re.IGNORECASE)
_KMS_ARN_PATTERN: re.Pattern[str] = re.compile(
    r"arn:aws[a-z\-]*:kms:[a-z0-9\-]+:\d{12}:key/[0-9a-f\-]{36}"
)
# Route53 hosted zone IDs — 14-32 alphanumeric, begin with Z
_HOSTED_ZONE_PATTERN: re.Pattern[str] = re.compile(r"^Z[0-9A-Z]{4,31}$")
# Account ID regex — detect 12-digit numbers embedded in strings
_ACCOUNT_ID_PATTERN: re.Pattern[str] = re.compile(r"\b\d{12}\b")

RETAIN_TYPES: frozenset[str] = frozenset(
    [
        "AWS::RDS::DBInstance",
        "AWS::RDS::DBCluster",
        "AWS::S3::Bucket",
        "AWS::DynamoDB::Table",
        "AWS::EFS::FileSystem",
    ]
)

#: Peering-like resource types flagged for human review.
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

# Pre-parsed YAML template for the !Select [0, !GetAZs ''] expression.
_yaml_rt = YAML()
_AZ_SELECT_TEMPLATE: CommentedSeq = _yaml_rt.load("az: !Select [0, !GetAZs '']\n")["az"]


# ---------------------------------------------------------------------------
# Pure rule helpers
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
    """Return True if *value* matches the AMI ID pattern."""
    return bool(_AMI_PATTERN.match(value))


def is_az(value: str) -> bool:
    """Return True if *value* matches a known AZ pattern."""
    return bool(_AZ_PATTERN.match(value))


def is_kms_arn(value: str) -> bool:
    """Return True if *value* matches a KMS CMK ARN pattern."""
    return bool(_KMS_ARN_PATTERN.search(value))


def is_hosted_zone_id(value: str) -> bool:
    """Return True if *value* matches a Route53 hosted zone ID pattern."""
    return bool(_HOSTED_ZONE_PATTERN.match(value))


def find_external_account_ids(value: str, source_account_id: str) -> list[str]:
    """Return any 12-digit account IDs in *value* that are not *source_account_id*.

    Used by R9 to flag cross-account IAM trust / principal references for
    human review.
    """
    matches = _ACCOUNT_ID_PATTERN.findall(value)
    return [m for m in matches if m != source_account_id]


# ---------------------------------------------------------------------------
# YAML tree walker
# ---------------------------------------------------------------------------


class _Context:
    """Mutable accumulator passed through the recursive walk."""

    def __init__(self, rules: set[str], account_id: str, source_region: str) -> None:
        self.rules = rules
        self.account_id = account_id
        self.source_region = source_region
        self.ami_ids: list[str] = []
        self.kms_arns: list[str] = []
        self.hosted_zone_ids: list[str] = []
        self.s3_buckets: list[str] = []
        self.review_decisions: list[dict[str, Any]] = []


def _record_review(ctx: _Context, decision: dict[str, Any]) -> None:
    """Append a unique review decision to *ctx*."""
    key = (decision.get("id"), decision.get("resource"))
    for existing in ctx.review_decisions:
        if (existing.get("id"), existing.get("resource")) == key:
            return
    ctx.review_decisions.append(decision)


def _walk(
    node: Any,
    ctx: _Context,
    key_hint: str = "",
    logical_id: str | None = None,
    resource_type: str | None = None,
) -> Any:
    """Recursive walk over ruamel.yaml tree applying string-level rules."""
    if isinstance(node, TaggedScalar):
        return node

    if isinstance(node, str):
        # Rule 3: AMI ID on ImageId key
        if "ami_id" in ctx.rules and key_hint == "ImageId" and is_ami_id(node):
            if node not in ctx.ami_ids:
                ctx.ami_ids.append(node)
            return TaggedScalar(value="AmiId", tag="!Ref")

        # Rule 4: AZ
        if "az" in ctx.rules and is_az(node):
            return copy.deepcopy(_AZ_SELECT_TEMPLATE)

        # Rule 8: Route53 Hosted Zone ID
        if (
            "route53" in ctx.rules
            and key_hint in ("HostedZoneId", "HostedZone")
            and is_hosted_zone_id(node)
        ):
            if node not in ctx.hosted_zone_ids:
                ctx.hosted_zone_ids.append(node)
            return TaggedScalar(value="HostedZoneId", tag="!Ref")

        # Rule 6: KMS ARN
        if "kms" in ctx.rules and is_kms_arn(node):
            match = _KMS_ARN_PATTERN.search(node)
            if match:
                arn = match.group(0)
                if arn not in ctx.kms_arns:
                    ctx.kms_arns.append(arn)
                # Full-value ARN → !Ref, else leave string (unsupported mid-string)
                if node == arn:
                    return TaggedScalar(value="KmsKeyArn", tag="!Ref")

        # Rule 9: IAM external account ID flag (done before account rewrite)
        if "iam_principal" in ctx.rules and key_hint in ("AWS", "Principal"):
            externals = find_external_account_ids(node, ctx.account_id)
            for ext in externals:
                _record_review(
                    ctx,
                    {
                        "id": f"D.iam-trust.{ext}",
                        "category": "iam-trust",
                        "resource": logical_id or "unknown",
                        "detail": f"IAM principal references external account {ext}",
                        "hint": f"Confirm trust policy for external principal {ext}",
                    },
                )

        # Rule 1: Account ID
        if "account_id" in ctx.rules:
            changed, new_val = rewrite_account_id(node, ctx.account_id)
            if changed:
                return TaggedScalar(value=new_val, tag="!Sub")

        # Rule 2: Region (exact match only)
        if "region" in ctx.rules:
            changed, new_val = rewrite_region(node, ctx.source_region)
            if changed:
                return TaggedScalar(value=new_val, tag="!Sub")

        return node

    if isinstance(node, CommentedMap):
        for key in list(node.keys()):
            new_rtype = resource_type
            new_lid = logical_id
            if resource_type is None and isinstance(node.get(key), CommentedMap):
                t = node[key].get("Type")
                if isinstance(t, str) and t.startswith("AWS::"):
                    new_rtype = t
                    new_lid = str(key)
            node[key] = _walk(
                node[key],
                ctx,
                key_hint=str(key),
                logical_id=new_lid,
                resource_type=new_rtype,
            )
        return node

    if isinstance(node, (CommentedSeq, list)):
        for i, item in enumerate(node):
            node[i] = _walk(
                item,
                ctx,
                key_hint=key_hint,
                logical_id=logical_id,
                resource_type=resource_type,
            )
        return node

    return node


# ---------------------------------------------------------------------------
# Template-level transformations
# ---------------------------------------------------------------------------


def apply_deletion_policy(resources: CommentedMap) -> None:
    """Add ``DeletionPolicy: Retain`` to critical resource types."""
    if not isinstance(resources, CommentedMap):
        return
    for _lid, resource in resources.items():
        if not isinstance(resource, CommentedMap):
            continue
        if resource.get("Type") in RETAIN_TYPES and "DeletionPolicy" not in resource:
            resource["DeletionPolicy"] = "Retain"


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


def add_ami_parameter(template: CommentedMap, ami_ids: list[str]) -> None:
    """Add an ``AmiId`` parameter referencing SSM public path."""
    if not ami_ids:
        return
    params = _ensure_parameters(template)
    params["AmiId"] = CommentedMap(
        [
            ("Type", "AWS::SSM::Parameter::Value<AWS::EC2::Image::Id>"),
            ("Default", _AMI_SSM_DEFAULT),
            ("Description", f"AMI ID (original: {ami_ids[0]})"),
        ]
    )


def add_kms_parameter(template: CommentedMap, kms_arns: list[str]) -> None:
    """Add a ``KmsKeyArn`` parameter if any KMS ARNs were rewritten."""
    if not kms_arns:
        return
    params = _ensure_parameters(template)
    params["KmsKeyArn"] = CommentedMap(
        [
            ("Type", "String"),
            ("Description", f"Target-account KMS CMK ARN (original: {kms_arns[0]})"),
            ("AllowedPattern", r"^arn:aws[a-z\-]*:kms:[a-z0-9\-]+:\d{12}:key/.+$"),
        ]
    )


def add_hosted_zone_parameter(
    template: CommentedMap, hosted_zone_ids: list[str]
) -> None:
    """Add ``HostedZoneId`` parameter if any Route53 zone IDs were rewritten."""
    if not hosted_zone_ids:
        return
    params = _ensure_parameters(template)
    params["HostedZoneId"] = CommentedMap(
        [
            ("Type", "AWS::Route53::HostedZone::Id"),
            ("Description", f"Route53 hosted zone ID (original: {hosted_zone_ids[0]})"),
        ]
    )


def add_bucket_prefix_parameter(template: CommentedMap, buckets: list[str]) -> None:
    """Add ``BucketPrefix`` parameter if any S3 buckets were rewritten."""
    if not buckets:
        return
    params = _ensure_parameters(template)
    params["BucketPrefix"] = CommentedMap(
        [
            ("Type", "String"),
            (
                "Description",
                "Prefix prepended to S3 bucket names (avoids global collisions).",
            ),
            ("Default", "myapp"),
        ]
    )


def apply_s3_bucket_name_rule(resources: CommentedMap, ctx: _Context) -> None:
    """R10 — wrap S3 bucket names with ``!Sub '${BucketPrefix}-<name>'``."""
    if "s3_bucket_name" not in ctx.rules:
        return
    if not isinstance(resources, CommentedMap):
        return
    for _lid, resource in resources.items():
        if not isinstance(resource, CommentedMap):
            continue
        if resource.get("Type") != "AWS::S3::Bucket":
            continue
        props = resource.get("Properties")
        if not isinstance(props, CommentedMap):
            continue
        name = props.get("BucketName")
        if isinstance(name, str) and not name.startswith("${"):
            ctx.s3_buckets.append(name)
            props["BucketName"] = TaggedScalar(
                value=f"${{BucketPrefix}}-{name}", tag="!Sub"
            )


def scan_peering_resources(resources: CommentedMap, ctx: _Context) -> None:
    """R7 — record peering / TGW attachment resources for review."""
    if "peering" not in ctx.rules:
        return
    if not isinstance(resources, CommentedMap):
        return
    for logical_id, resource in resources.items():
        if not isinstance(resource, CommentedMap):
            continue
        rtype = resource.get("Type")
        if rtype in PEERING_TYPES:
            _record_review(
                ctx,
                {
                    "id": f"D.peering.{logical_id}",
                    "category": "peering",
                    "resource": str(logical_id),
                    "type": rtype,
                    "detail": (
                        f"{rtype} requires cross-account handshake — "
                        "fill target PeeringConnectionId/AttachmentId manually."
                    ),
                    "hint": "Run the target-account accept-peering / attach command.",
                },
            )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def rewrite_template(
    template: CommentedMap,
    account_id: str = "",
    source_region: str = "",
    rules: set[str] | frozenset[str] | None = None,
    review_decisions: list[dict[str, Any]] | None = None,
) -> CommentedMap:
    """Apply rewrite rules to *template* in place.

    Args:
        template: Parsed CFN template.
        account_id: Source AWS account ID to replace (12 digits).
        source_region: Source AWS region code to replace.
        rules: Optional set of rule names to enable. Defaults to Phase 1 rules
            only (backward compatible). Pass ``ALL_RULES`` or a preset to enable
            Phase 2 rules (KMS / Route53 / peering / IAM principal / S3 name).
        review_decisions: Optional list to populate with flagged review items
            (Peering, cross-account IAM). Callers can inspect for generate_review.

    Returns:
        The same *template* object, mutated in place.
    """
    active_rules = set(rules) if rules is not None else set(PHASE1_RULES)

    ctx = _Context(active_rules, account_id, source_region)
    resources = template.get("Resources")
    if resources:
        _walk(resources, ctx)
        if "deletion_policy" in active_rules:
            apply_deletion_policy(resources)
        apply_s3_bucket_name_rule(resources, ctx)
        scan_peering_resources(resources, ctx)

    # Add parameters for any captured rewrites.
    add_ami_parameter(template, ctx.ami_ids)
    add_kms_parameter(template, ctx.kms_arns)
    add_hosted_zone_parameter(template, ctx.hosted_zone_ids)
    add_bucket_prefix_parameter(template, ctx.s3_buckets)

    if review_decisions is not None:
        review_decisions.extend(ctx.review_decisions)
    return template


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------


def load_presets(path: Path | None = None) -> dict[str, Any]:
    """Load rewrite-presets.json — returns {name: {"rules": [...], ...}}."""
    if path is None:
        path = Path(__file__).parent.parent / "config" / "rewrite-presets.json"
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    return data.get("presets", {})


def resolve_preset_rules(preset_name: str, presets: dict[str, Any]) -> set[str]:
    """Resolve preset name → set of rule names. Raises KeyError if unknown."""
    if preset_name not in presets:
        raise KeyError(f"Unknown preset: {preset_name!r}")
    return set(presets[preset_name].get("rules", []))


# ---------------------------------------------------------------------------
# YAML I/O helpers
# ---------------------------------------------------------------------------


def _make_yaml() -> YAML:
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.default_flow_style = False
    yaml.width = 120
    return yaml


def load_yaml(path: Path) -> CommentedMap:
    """Load a YAML file using ruamel.yaml round-trip."""
    yaml = _make_yaml()
    with path.open(encoding="utf-8") as fh:
        data = yaml.load(fh)
    if data is None:
        print(f"ERROR: empty or invalid YAML: {path}", file=sys.stderr)
        sys.exit(1)
    return data


def dump_yaml(data: CommentedMap, path: Path | None = None) -> str:
    """Dump *data* to a YAML string (optionally writing *path*)."""
    yaml = _make_yaml()
    stream = StringIO()
    yaml.dump(data, stream)
    result = stream.getvalue()
    if path is not None:
        path.write_text(result, encoding="utf-8")
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Rewrite hardcoded values in former2 CFN YAML output. "
            "Phase 1 rules (R1-R5) always on; Phase 2 rules (R6-R10) opt-in "
            "via --preset or --rules."
        )
    )
    parser.add_argument("--input", required=True, help="Input CFN YAML path")
    parser.add_argument("--output", help="Output YAML path (default: stdout)")
    parser.add_argument(
        "--account-id", default="", help="Source AWS account ID (12 digits)"
    )
    parser.add_argument(
        "--source-region", default="", help="Source AWS region code"
    )
    parser.add_argument(
        "--target-region", default="", help="Target region (informational)"
    )
    parser.add_argument(
        "--preset",
        default="",
        help=(
            "Rule preset name (cross-account / cross-region / cleanup-only / full). "
            "Overrides --rules."
        ),
    )
    parser.add_argument(
        "--rules",
        default="",
        help="Comma-separated rule names (overrides Phase 1 defaults).",
    )
    parser.add_argument(
        "--review-decisions",
        default="",
        help="Optional path to write detected review decisions (JSON).",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"ERROR: input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    rules: set[str] | None = None
    if args.preset:
        presets = load_presets()
        try:
            rules = resolve_preset_rules(args.preset, presets)
        except KeyError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(2)
    elif args.rules:
        rules = {r.strip() for r in args.rules.split(",") if r.strip()}

    template = load_yaml(input_path)
    decisions: list[dict[str, Any]] = []
    rewrite_template(
        template,
        account_id=args.account_id,
        source_region=args.source_region,
        rules=rules,
        review_decisions=decisions,
    )

    if args.output:
        out_path = Path(args.output)
        dump_yaml(template, out_path)
        print(f"Wrote cleaned template to {args.output}", file=sys.stderr)
    else:
        print(dump_yaml(template))

    if args.review_decisions and decisions:
        Path(args.review_decisions).write_text(
            json.dumps(decisions, indent=2), encoding="utf-8"
        )
        print(
            f"Wrote {len(decisions)} review decisions to {args.review_decisions}",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
