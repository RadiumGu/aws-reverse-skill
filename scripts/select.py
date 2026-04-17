#!/usr/bin/env python3
"""select.py — Filter former2 raw.json resources by service, tag, and regex.

Usage:
    python scripts/select.py \\
        --input raw.json \\
        --output filtered.json \\
        --service Lambda,IAM \\
        --tag Environment=prod \\
        --regex '^myapp-' \\
        --exclude-default \\
        --emit-regex-filter
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


# Resource types considered "default" AWS resources (created automatically).
_DEFAULT_RESOURCE_TYPES = frozenset(
    [
        "AWS::EC2::VPC",
        "AWS::EC2::SecurityGroup",
        "AWS::EC2::NetworkAcl",
        "AWS::EC2::InternetGateway",
        "AWS::EC2::RouteTable",
        "AWS::EC2::Subnet",
    ]
)


def is_default_resource(resource: dict[str, Any]) -> bool:
    """Return True if the resource appears to be a default AWS-created resource.

    Args:
        resource: Resource dict from former2 raw.json.

    Returns:
        True if resource is a default AWS-managed resource.
    """
    rtype = resource.get("Type", "")
    tags = resource.get("Tags") or {}
    if not isinstance(tags, dict):
        tags = {}

    if rtype == "AWS::EC2::VPC":
        if resource.get("IsDefault", False):
            return True
        if tags.get("Name", "").lower() == "default":
            return True

    if rtype == "AWS::EC2::SecurityGroup":
        if resource.get("GroupName", "") == "default":
            return True
        if tags.get("Name", "").lower() == "default":
            return True

    if rtype == "AWS::EC2::NetworkAcl":
        if resource.get("IsDefault", False):
            return True

    return False


def apply_filters(
    resources: list[dict[str, Any]],
    filters: dict[str, Any],
) -> list[dict[str, Any]]:
    """Apply service/tag/regex filters to a list of former2 resources.

    Args:
        resources: List of resource dicts from former2 raw.json.
        filters: Dict with optional keys:
            - ``services``: list of AWS service names, e.g. ``['Lambda', 'IAM']``
              (OR logic — resource matches if it belongs to any listed service).
            - ``tags``: list of ``'KEY=VALUE'`` strings (AND logic — resource must
              match every tag filter).
            - ``regex``: regex pattern matched against ``PhysicalId``.
            - ``exclude_default``: bool — remove default VPC / SG / NACL.

    Returns:
        Filtered list of resources.
    """
    result = list(resources)

    # --- Service filter (OR) ---
    services = filters.get("services")
    if services:
        prefixes = tuple(f"AWS::{s}::" for s in services)
        result = [r for r in result if r.get("Type", "").startswith(prefixes)]

    # --- Tag filter (AND) ---
    tag_filters = filters.get("tags")
    if tag_filters:
        for tag_kv in tag_filters:
            if "=" not in tag_kv:
                continue
            tag_key, tag_val = tag_kv.split("=", 1)
            result = [
                r
                for r in result
                if isinstance(r.get("Tags"), dict)
                and r["Tags"].get(tag_key) == tag_val
            ]

    # --- Regex filter on PhysicalId ---
    regex_pattern = filters.get("regex")
    if regex_pattern:
        compiled = re.compile(regex_pattern)
        result = [r for r in result if compiled.search(r.get("PhysicalId", ""))]

    # --- Exclude default resources ---
    if filters.get("exclude_default"):
        result = [r for r in result if not is_default_resource(r)]

    return result


def build_regex_filter(physical_ids: list[str]) -> str:
    """Build a pipe-joined regex string for the former2 filter command.

    Args:
        physical_ids: List of resource physical IDs.

    Returns:
        Pipe-joined regex string suitable for ``former2 filter --search-filter``.
    """
    escaped = [re.escape(pid) for pid in physical_ids if pid]
    return "|".join(escaped)


def main() -> None:
    """CLI entry point for select.py."""
    parser = argparse.ArgumentParser(
        description=(
            "Filter former2 raw.json resources by service, tag, and regex. "
            "Outputs filtered resource list as JSON."
        )
    )
    parser.add_argument("--input", required=True, help="Path to former2 raw.json")
    parser.add_argument(
        "--output", help="Output path for filtered JSON (default: stdout)"
    )
    parser.add_argument(
        "--service",
        default="",
        metavar="SVC1,SVC2",
        help="Comma-separated AWS service names (e.g. Lambda,IAM). OR logic.",
    )
    parser.add_argument(
        "--tag",
        action="append",
        default=[],
        metavar="KEY=VAL",
        help="Tag filter KEY=VAL (AND logic). May be repeated.",
    )
    parser.add_argument(
        "--regex",
        default="",
        metavar="PATTERN",
        help="Regex matched against PhysicalId.",
    )
    parser.add_argument(
        "--exclude-default",
        action="store_true",
        help="Exclude default VPC / Security Group / NACL.",
    )
    parser.add_argument(
        "--emit-regex-filter",
        action="store_true",
        help=(
            "Instead of JSON output, print a pipe-joined regex string "
            "for use with 'former2 filter --search-filter'."
        ),
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"ERROR: input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    with input_path.open(encoding="utf-8") as fh:
        data = json.load(fh)

    # former2 raw.json wraps resources in {"resources": [...]}
    if isinstance(data, dict):
        resources: list[dict[str, Any]] = data.get("resources", [])
    elif isinstance(data, list):
        resources = data
    else:
        print("ERROR: unexpected JSON structure in input file", file=sys.stderr)
        sys.exit(1)

    filters: dict[str, Any] = {
        "services": (
            [s.strip() for s in args.service.split(",") if s.strip()]
            if args.service
            else None
        ),
        "tags": args.tag if args.tag else None,
        "regex": args.regex if args.regex else None,
        "exclude_default": args.exclude_default,
    }

    filtered = apply_filters(resources, filters)

    if args.emit_regex_filter:
        ids = [r.get("PhysicalId", "") for r in filtered]
        print(build_regex_filter(ids))
        return

    output = {"resources": filtered, "count": len(filtered)}
    output_json = json.dumps(output, indent=2, ensure_ascii=False)

    if args.output:
        Path(args.output).write_text(output_json, encoding="utf-8")
        print(
            f"Wrote {len(filtered)} resources to {args.output}",
            file=sys.stderr,
        )
    else:
        print(output_json)


if __name__ == "__main__":
    main()
