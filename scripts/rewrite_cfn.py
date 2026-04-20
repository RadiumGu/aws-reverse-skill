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

Phase 3 rules (opt-in — Hotfix A Wave A1):
  R11. VPC / Subnet / Security Group resource IDs → Parameters
  R12. Security Group CidrIp / CidrIpv6 classification
  R13. Nested ARN string scan
  R14. JSON-embedded ARN scan (Step Functions / IAM policies)
  R15. Region-locked resource detection (advisory only)

Usage:
    python scripts/rewrite_cfn.py \\
        --input cfn-filtered.yml --output cleaned.yml \\
        --account-id 123456789012 --source-region ap-northeast-1 \\
        --preset cross-account
"""

import argparse
import copy
import json
import sys
from io import StringIO
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq, TaggedScalar

sys.path.insert(0, str(Path(__file__).parent))
from cidr_analyzer import classify_cidr  # noqa: E402
from arn_rewriter import (  # noqa: E402
    find_arns,
    rewrite_arn,
    rewrite_arns_in_text,
)
from region_lock import find_region_locked  # noqa: E402

# ---------------------------------------------------------------------------
# Import everything from rule sub-modules
# ---------------------------------------------------------------------------

from rules.common import (  # noqa: E402
    PHASE1_RULES,
    PHASE2_RULES,
    PHASE3_RULES,
    ALL_RULES,
    ALL_RULES_EXTENDED,
    KNOWN_REGIONS,
    CHINA_REGIONS,
    GOVCLOUD_REGIONS,
    RETAIN_TYPES,
    PEERING_TYPES,
    _AMI_SSM_DEFAULT,
    _Context,
    _record_review,
    _ensure_parameters,
    partition_for_region,
    rewrite_account_id,
    rewrite_region,
    is_ami_id,
    is_az,
    is_kms_arn,
    is_hosted_zone_id,
    is_vpc_id,
    is_subnet_id,
    is_sg_id,
    find_external_account_ids,
    _VPC_ID_KEYS,
    _SUBNET_SINGLE_KEYS,
    _SUBNET_LIST_KEYS,
    _SG_SINGLE_KEYS,
    _SG_LIST_KEYS,
    _AZ_PATTERN,
    _AMI_PATTERN,
    _KMS_ARN_PATTERN,
    _HOSTED_ZONE_PATTERN,
    _ACCOUNT_ID_PATTERN,
    _VPC_ID_PATTERN,
    _SUBNET_ID_PATTERN,
    _SG_ID_PATTERN,
)
from rules.identity import (  # noqa: E402
    _record_cross_account_decision,
    _parse_service_from_arn,
    _ingest_rewrite_result,
    add_external_arn_parameters,
)
from rules.data import (  # noqa: E402
    apply_deletion_policy,
    add_ami_parameter,
    add_kms_parameter,
    add_hosted_zone_parameter,
    add_bucket_prefix_parameter,
    apply_s3_bucket_name_rule,
    scan_peering_resources,
)
from rules.network import (  # noqa: E402
    _rewrite_vpc_scalar,
    _rewrite_subnet_scalar,
    _rewrite_sg_scalar,
    _rewrite_subnet_list,
    _rewrite_sg_list,
    apply_vpc_resource_ids_rule,
    add_vpc_resource_parameters,
    _format_sg_port,
    _record_sg_cidr_decision,
    _process_sg_rule_list,
    apply_sg_cidr_rule,
    add_sg_cidr_parameter,
    _emit_vpc_resource_mapping_decisions,
)
from rules.nested_arn import (  # noqa: E402
    _text_to_yaml_scalar,
    _apply_r13_to_scalar,
    _walk_r13,
    apply_nested_arn_string_rule,
    _walk_json_for_arns,
    _rewrite_step_functions_definition_string,
    _rewrite_policy_document,
    apply_nested_arn_json_rule,
)
from rules.region_lock import apply_region_lock_rule  # noqa: E402


# ---------------------------------------------------------------------------
# Pre-parsed YAML expression templates (used by _walk)
# ---------------------------------------------------------------------------

_yaml_rt = YAML()
_AZ_SELECT_TEMPLATE: CommentedSeq = _yaml_rt.load("az: !Select [0, !GetAZs '']\n")["az"]


# ---------------------------------------------------------------------------
# YAML tree walker
# ---------------------------------------------------------------------------


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
                if node == arn:
                    return TaggedScalar(value="KmsKeyArn", tag="!Ref")

        # Rule 9: IAM external account ID flag
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
# Public API
# ---------------------------------------------------------------------------


def rewrite_template(
    template: CommentedMap,
    account_id: str = "",
    source_region: str = "",
    rules: set[str] | frozenset[str] | None = None,
    review_decisions: list[dict[str, Any]] | None = None,
    source_vpc_cidrs: list[str] | None = None,
    target_region: str = "",
) -> CommentedMap:
    """Apply rewrite rules to *template* in place."""
    active_rules = set(rules) if rules is not None else set(PHASE1_RULES)

    ctx = _Context(
        active_rules, account_id, source_region, source_vpc_cidrs=source_vpc_cidrs
    )
    ctx.target_region = target_region or ""
    resources = template.get("Resources")
    if resources:
        _walk(resources, ctx)
        if "deletion_policy" in active_rules:
            apply_deletion_policy(resources)
        apply_s3_bucket_name_rule(resources, ctx)
        scan_peering_resources(resources, ctx)
        apply_vpc_resource_ids_rule(resources, ctx)
        apply_sg_cidr_rule(resources, ctx)
        apply_nested_arn_string_rule(resources, ctx)
        apply_nested_arn_json_rule(resources, ctx)
        apply_region_lock_rule(template, ctx)

    add_ami_parameter(template, ctx.ami_ids)
    add_kms_parameter(template, ctx.kms_arns)
    add_hosted_zone_parameter(template, ctx.hosted_zone_ids)
    add_bucket_prefix_parameter(template, ctx.s3_buckets)
    add_vpc_resource_parameters(template, ctx)
    add_sg_cidr_parameter(template, ctx)
    add_external_arn_parameters(template, ctx)
    if "vpc_resource_ids" in active_rules:
        _emit_vpc_resource_mapping_decisions(ctx)

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
    parser.add_argument(
        "--raw",
        default="",
        help=(
            "Optional raw.json. Source VPC CIDRs for R12 are extracted from "
            "AWS::EC2::VPC entries when --source-vpc-cidr is not given."
        ),
    )
    parser.add_argument(
        "--source-vpc-cidr",
        default="",
        help=(
            "Comma-separated source VPC CIDRs used by R12 (e.g. "
            "10.0.0.0/16,10.1.0.0/16). Overrides --raw detection."
        ),
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

    source_vpc_cidrs: list[str] = []
    if args.source_vpc_cidr:
        source_vpc_cidrs = [
            c.strip() for c in args.source_vpc_cidr.split(",") if c.strip()
        ]
    elif args.raw:
        raw_path = Path(args.raw)
        if raw_path.exists():
            try:
                raw_data = json.loads(raw_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                raw_data = None
            raw_items: list[dict[str, Any]] = []
            if isinstance(raw_data, list):
                raw_items = raw_data
            elif isinstance(raw_data, dict):
                raw_items = raw_data.get("resources", []) or []
            for entry in raw_items:
                if not isinstance(entry, dict):
                    continue
                if entry.get("Type") == "AWS::EC2::VPC":
                    cidr = entry.get("CidrBlock") or entry.get("Cidr")
                    if isinstance(cidr, str):
                        source_vpc_cidrs.append(cidr)

    template = load_yaml(input_path)
    decisions: list[dict[str, Any]] = []
    rewrite_template(
        template,
        account_id=args.account_id,
        source_region=args.source_region,
        rules=rules,
        review_decisions=decisions,
        source_vpc_cidrs=source_vpc_cidrs,
        target_region=args.target_region,
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

    from audit import record_stage
    audit_outputs = [args.output] if args.output else []
    if args.review_decisions and decisions:
        audit_outputs.append(args.review_decisions)
    record_stage(
        step="rewrite",
        inputs=[str(input_path)] + ([args.raw] if args.raw else []),
        outputs=audit_outputs,
        notes=f"preset={args.preset or '-'} rules={','.join(sorted(rules)) if rules else 'default'}",
    )


if __name__ == "__main__":
    main()
