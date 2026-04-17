#!/usr/bin/env python3
"""rewrite_cfn.py — Rules engine to clean hardcoded values from former2 CFN output.

Applies five deterministic rules:
  1. Account ID  — 12-digit account IDs in any string → !Sub '${AWS::AccountId}'
  2. Region      — exact match of source region code → !Sub '${AWS::Region}'
  3. AMI ID      — ami-* value in ImageId key → !Ref AmiId + Parameters.AmiId
  4. AZ          — hardcoded AZ string → !Select [0, !GetAZs '']
  5. DeletionPolicy — add Retain to RDS / S3 / DynamoDB / EFS resources

Usage:
    python scripts/rewrite_cfn.py \\
        --input cfn-filtered.yml \\
        --output cleaned.yml \\
        --account-id 123456789012 \\
        --source-region ap-northeast-1
"""

import argparse
import copy
import re
import sys
from io import StringIO
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq, TaggedScalar


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Known AWS region codes (all commercial regions as of 2025).
KNOWN_REGIONS: frozenset[str] = frozenset(
    [
        "us-east-1",
        "us-east-2",
        "us-west-1",
        "us-west-2",
        "ap-northeast-1",
        "ap-northeast-2",
        "ap-northeast-3",
        "ap-southeast-1",
        "ap-southeast-2",
        "ap-southeast-3",
        "ap-southeast-4",
        "ap-south-1",
        "ap-south-2",
        "ap-east-1",
        "eu-west-1",
        "eu-west-2",
        "eu-west-3",
        "eu-central-1",
        "eu-central-2",
        "eu-north-1",
        "eu-south-1",
        "eu-south-2",
        "sa-east-1",
        "ca-central-1",
        "ca-west-1",
        "me-south-1",
        "me-central-1",
        "af-south-1",
        "il-central-1",
    ]
)

#: Regex matching an AZ string: known-region + letter suffix (a–f).
_AZ_PATTERN: re.Pattern[str] = re.compile(
    r"^("
    + "|".join(re.escape(r) for r in sorted(KNOWN_REGIONS, key=len, reverse=True))
    + r")[a-f]$"
)

#: Regex matching an AMI ID.
_AMI_PATTERN: re.Pattern[str] = re.compile(r"^ami-[0-9a-f]{8,17}$", re.IGNORECASE)

#: CFN resource types that should receive DeletionPolicy: Retain.
RETAIN_TYPES: frozenset[str] = frozenset(
    [
        "AWS::RDS::DBInstance",
        "AWS::RDS::DBCluster",
        "AWS::S3::Bucket",
        "AWS::DynamoDB::Table",
        "AWS::EFS::FileSystem",
    ]
)

#: SSM public path used as default AmiId parameter value.
_AMI_SSM_DEFAULT = (
    "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
)

# Pre-parsed YAML template for the !Select [0, !GetAZs ''] expression.
# We deepcopy this whenever we need to emit an AZ substitution.
_yaml_rt = YAML()
_AZ_SELECT_TEMPLATE: CommentedSeq = _yaml_rt.load(
    "az: !Select [0, !GetAZs '']\n"
)["az"]


# ---------------------------------------------------------------------------
# Pure rule functions (easily unit-testable)
# ---------------------------------------------------------------------------


def rewrite_account_id(value: str, account_id: str) -> tuple[bool, str]:
    """Replace occurrences of *account_id* in *value* with ``${AWS::AccountId}``.

    Args:
        value: String value to inspect.
        account_id: The 12-digit source account ID to replace.

    Returns:
        ``(changed, new_value)`` where *new_value* is the transformed string
        (still a plain str — the caller wraps it in a ``!Sub`` tagged scalar).
    """
    if not account_id or account_id not in value:
        return False, value
    return True, value.replace(account_id, "${AWS::AccountId}")


def rewrite_region(value: str, source_region: str) -> tuple[bool, str]:
    """Replace an exact *source_region* string value with ``${AWS::Region}``.

    Only replaces values that *exactly* equal the region code to avoid
    false-positive matches inside longer strings (e.g. resource names).

    Args:
        value: String value to inspect.
        source_region: The source AWS region code to replace.

    Returns:
        ``(changed, new_value)`` where *new_value* is ``'${AWS::Region}'``
        if changed, otherwise the original *value*.
    """
    if source_region and value == source_region and source_region in KNOWN_REGIONS:
        return True, "${AWS::Region}"
    return False, value


def is_ami_id(value: str) -> bool:
    """Return True if *value* looks like an AMI ID (``ami-[0-9a-f]{8,17}``).

    Args:
        value: String value to check.

    Returns:
        True if the value matches the AMI ID pattern.
    """
    return bool(_AMI_PATTERN.match(value))


def is_az(value: str) -> bool:
    """Return True if *value* looks like an Availability Zone string.

    Args:
        value: String value to check.

    Returns:
        True if the value matches the known AZ pattern.
    """
    return bool(_AZ_PATTERN.match(value))


# ---------------------------------------------------------------------------
# YAML tree walker
# ---------------------------------------------------------------------------


def _walk(
    node: Any,
    account_id: str,
    source_region: str,
    ami_ids: list[str],
    key_hint: str = "",
) -> Any:
    """Recursively walk a ruamel.yaml node tree and apply all rewrite rules.

    Modifies the tree in-place where possible; returns the (possibly replaced)
    node for the caller to assign back.

    Args:
        node: Current YAML node (str, CommentedMap, CommentedSeq, TaggedScalar,
              or any scalar).
        account_id: Source account ID to replace (empty string = skip rule).
        source_region: Source region to replace (empty string = skip rule).
        ami_ids: Accumulator list — AMI IDs found during the walk are appended.
        key_hint: The parent key name, used for context-sensitive rules
                  (e.g. ``ImageId`` activates the AMI rule).

    Returns:
        The rewritten node (may be a new ``TaggedScalar`` or deepcopied sequence).
    """
    # ---- Tagged scalars (existing !Sub / !Ref / etc.) ----
    # Do not inspect the content of existing CFN intrinsic functions.
    if isinstance(node, TaggedScalar):
        return node

    # ---- Plain string scalars: apply all string-level rules ----
    if isinstance(node, str):
        # Rule 3: AMI — only trigger on ImageId key
        if key_hint == "ImageId" and is_ami_id(node):
            if node not in ami_ids:
                ami_ids.append(node)
            return TaggedScalar(value="AmiId", tag="!Ref")

        # Rule 4: AZ
        if is_az(node):
            return copy.deepcopy(_AZ_SELECT_TEMPLATE)

        # Rule 1: Account ID (may overlap with Rule 2 — account wins)
        changed, new_val = rewrite_account_id(node, account_id)
        if changed:
            return TaggedScalar(value=new_val, tag="!Sub")

        # Rule 2: Region (exact match only)
        changed, new_val = rewrite_region(node, source_region)
        if changed:
            return TaggedScalar(value=new_val, tag="!Sub")

        return node

    # ---- Mapping ----
    if isinstance(node, CommentedMap):
        for key in list(node.keys()):
            node[key] = _walk(
                node[key],
                account_id,
                source_region,
                ami_ids,
                key_hint=str(key),
            )
        return node

    # ---- Sequence ----
    if isinstance(node, (CommentedSeq, list)):
        for i, item in enumerate(node):
            node[i] = _walk(item, account_id, source_region, ami_ids, key_hint=key_hint)
        return node

    # Anything else (int, float, bool, None) — leave unchanged
    return node


# ---------------------------------------------------------------------------
# Template-level transformations
# ---------------------------------------------------------------------------


def apply_deletion_policy(resources: CommentedMap) -> None:
    """Add ``DeletionPolicy: Retain`` to critical resource types.

    Mutates *resources* in place. Only adds the policy if not already set.

    Args:
        resources: The ``Resources`` section of the CFN template.
    """
    if not isinstance(resources, CommentedMap):
        return
    for _logical_id, resource in resources.items():
        if not isinstance(resource, CommentedMap):
            continue
        if resource.get("Type") in RETAIN_TYPES:
            if "DeletionPolicy" not in resource:
                resource["DeletionPolicy"] = "Retain"


def add_ami_parameter(template: CommentedMap, ami_ids: list[str]) -> None:
    """Add an ``AmiId`` parameter to the CFN template's ``Parameters`` section.

    Uses the SSM public path as the default value so cross-region deploys
    automatically resolve the correct AMI.

    Args:
        template: Full CFN template (top-level CommentedMap).
        ami_ids: List of original AMI IDs found during the walk (for the
                 description field).
    """
    if not ami_ids:
        return

    param_entry = CommentedMap(
        [
            ("Type", "AWS::SSM::Parameter::Value<AWS::EC2::Image::Id>"),
            ("Default", _AMI_SSM_DEFAULT),
            ("Description", f"AMI ID (original: {ami_ids[0]})"),
        ]
    )

    if "Parameters" not in template:
        # Insert Parameters before Resources for CFN readability.
        new_template = CommentedMap()
        for key in template:
            if key == "Resources":
                new_template["Parameters"] = CommentedMap([("AmiId", param_entry)])
            new_template[key] = template[key]
        template.clear()
        template.update(new_template)
    else:
        template["Parameters"]["AmiId"] = param_entry


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def rewrite_template(
    template: CommentedMap,
    account_id: str = "",
    source_region: str = "",
) -> CommentedMap:
    """Apply all five rewrite rules to a CFN template in-place.

    Args:
        template: Parsed CFN template (ruamel.yaml ``CommentedMap``).
        account_id: Source AWS account ID to replace (12 digits).
        source_region: Source AWS region code to replace.

    Returns:
        The same *template* object, mutated in place.
    """
    ami_ids: list[str] = []

    resources = template.get("Resources")
    if resources:
        # Rules 1–4: walk the Resources section
        _walk(resources, account_id, source_region, ami_ids)
        # Rule 5: DeletionPolicy
        apply_deletion_policy(resources)

    # Rule 3 follow-up: add AmiId parameter if any AMIs were found
    if ami_ids:
        add_ami_parameter(template, ami_ids)

    return template


# ---------------------------------------------------------------------------
# YAML I/O helpers
# ---------------------------------------------------------------------------


def _make_yaml() -> YAML:
    """Create a ruamel.yaml instance configured for CFN round-trip."""
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.default_flow_style = False
    yaml.width = 120
    return yaml


def load_yaml(path: Path) -> CommentedMap:
    """Load a YAML file using ruamel.yaml (round-trip mode).

    Args:
        path: Path to the YAML file.

    Returns:
        Parsed content as a ``CommentedMap``.

    Raises:
        SystemExit: If the file is empty or fails to parse.
    """
    yaml = _make_yaml()
    with path.open(encoding="utf-8") as fh:
        data = yaml.load(fh)
    if data is None:
        print(f"ERROR: empty or invalid YAML: {path}", file=sys.stderr)
        sys.exit(1)
    return data


def dump_yaml(data: CommentedMap, path: Path | None = None) -> str:
    """Dump *data* to a YAML string (and optionally to *path*).

    Args:
        data: YAML data to serialise.
        path: Optional output file path.

    Returns:
        YAML string.
    """
    yaml = _make_yaml()
    stream = StringIO()
    yaml.dump(data, stream)
    result = stream.getvalue()
    if path is not None:
        path.write_text(result, encoding="utf-8")
    return result


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entry point for rewrite_cfn.py."""
    parser = argparse.ArgumentParser(
        description=(
            "Rewrite hardcoded values in former2 CFN YAML output "
            "(Account ID / Region / AMI / AZ / DeletionPolicy)."
        )
    )
    parser.add_argument("--input", required=True, help="Input CFN YAML file path")
    parser.add_argument(
        "--output", help="Output YAML file path (default: stdout)"
    )
    parser.add_argument(
        "--account-id",
        default="",
        metavar="ACCOUNT_ID",
        help="Source AWS account ID (12 digits) to replace with ${AWS::AccountId}",
    )
    parser.add_argument(
        "--source-region",
        default="",
        metavar="REGION",
        help="Source AWS region code to replace with ${AWS::Region}",
    )
    parser.add_argument(
        "--target-region",
        default="",
        metavar="REGION",
        help="Target AWS region (informational only — not used in rewrite logic)",
    )
    parser.add_argument(
        "--rules",
        default="default",
        help="Rule set name (currently only 'default' is supported)",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"ERROR: input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    template = load_yaml(input_path)
    rewrite_template(template, account_id=args.account_id, source_region=args.source_region)

    if args.output:
        out_path = Path(args.output)
        dump_yaml(template, out_path)
        print(f"Wrote cleaned template to {args.output}", file=sys.stderr)
    else:
        print(dump_yaml(template))


if __name__ == "__main__":
    main()
