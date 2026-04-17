#!/usr/bin/env python3
"""preview.py — Quick overview of a former2 raw.json scan result.

Groups resources by service / tag / region, shows counts, and optionally
prints sample physical ids. Useful for deciding what to filter before
running ``select.py`` / ``rewrite_cfn.py``.

Usage:
    python scripts/preview.py --input out/raw.json --group-by service
    python scripts/preview.py --input out/raw.json --group-by tag --tag-key Environment
    python scripts/preview.py --input out/raw.json --group-by region --sample 3 --with-tags
"""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


def load_resources(path: Path) -> list[dict[str, Any]]:
    """Load raw.json and return the resources list."""
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("resources", [])
    raise ValueError(f"Unexpected JSON structure in {path}")


def service_of(resource: dict[str, Any]) -> str:
    """Return the AWS service (``Lambda``, ``IAM``, …) for a resource."""
    rtype = resource.get("Type", "")
    if rtype.startswith("AWS::"):
        parts = rtype.split("::")
        if len(parts) >= 2:
            return parts[1]
    return "Unknown"


def group_by_service(resources: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group resources by AWS service."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in resources:
        groups[service_of(r)].append(r)
    return dict(groups)


def group_by_tag(
    resources: list[dict[str, Any]], tag_key: str
) -> dict[str, list[dict[str, Any]]]:
    """Group resources by the value of tag *tag_key* (``<missing>`` if absent)."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in resources:
        tags = r.get("Tags") or {}
        if not isinstance(tags, dict):
            tags = {}
        val = tags.get(tag_key, "<missing>")
        groups[val].append(r)
    return dict(groups)


def group_by_region(
    resources: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Group resources by ``Region`` (``<unknown>`` if absent)."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in resources:
        region = r.get("Region", "<unknown>")
        groups[region].append(r)
    return dict(groups)


def build_preview(
    resources: list[dict[str, Any]],
    group_by: str = "service",
    tag_key: str = "",
    sample: int = 0,
    with_tags: bool = False,
) -> dict[str, Any]:
    """Build a preview dict summarising *resources*.

    Args:
        resources: Resource list from raw.json.
        group_by: One of ``service``, ``tag``, ``region``.
        tag_key: Required when ``group_by == 'tag'``.
        sample: Number of sample physical ids to include per group (0 = none).
        with_tags: Include tag dict in samples.

    Returns:
        Dict with ``total`` and ``groups`` (ordered by count desc).
    """
    if group_by == "service":
        groups = group_by_service(resources)
    elif group_by == "tag":
        if not tag_key:
            raise ValueError("group-by=tag requires --tag-key")
        groups = group_by_tag(resources, tag_key)
    elif group_by == "region":
        groups = group_by_region(resources)
    else:
        raise ValueError(f"Unknown group_by: {group_by}")

    ordered = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))

    group_out: list[dict[str, Any]] = []
    for name, items in ordered:
        entry: dict[str, Any] = {"name": name, "count": len(items)}
        if sample > 0:
            entry["sample"] = [
                {
                    "PhysicalId": r.get("PhysicalId", ""),
                    "Type": r.get("Type", ""),
                    **({"Tags": r.get("Tags", {})} if with_tags else {}),
                }
                for r in items[:sample]
            ]
        group_out.append(entry)

    return {
        "total": len(resources),
        "group_by": group_by,
        "groups": group_out,
    }


def format_preview_text(preview: dict[str, Any]) -> str:
    """Format a preview dict as human-readable text (for terminal / SKILL use)."""
    lines: list[str] = []
    lines.append(f"Total resources: {preview['total']}")
    lines.append(f"Grouped by: {preview['group_by']}")
    lines.append("")
    lines.append(f"{'Group':<30s} Count")
    lines.append("-" * 40)
    for g in preview["groups"]:
        lines.append(f"{g['name']:<30s} {g['count']}")
        for s in g.get("sample", []):
            pid = s.get("PhysicalId", "")
            rtype = s.get("Type", "")
            lines.append(f"    - {pid} ({rtype})")
            if "Tags" in s and s["Tags"]:
                lines.append(f"      tags: {json.dumps(s['Tags'], ensure_ascii=False)}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Preview former2 raw.json grouped by service / tag / region."
    )
    parser.add_argument("--input", required=True, help="Path to raw.json")
    parser.add_argument(
        "--group-by",
        choices=["service", "tag", "region"],
        default="service",
        help="Grouping dimension (default: service).",
    )
    parser.add_argument(
        "--tag-key",
        default="",
        help="Tag key when --group-by=tag (e.g. Environment).",
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=0,
        help="Number of sample resources to print per group.",
    )
    parser.add_argument(
        "--with-tags",
        action="store_true",
        help="Include Tags in samples.",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text).",
    )
    args = parser.parse_args()

    path = Path(args.input)
    if not path.exists():
        print(f"ERROR: input file not found: {path}", file=sys.stderr)
        sys.exit(1)

    resources = load_resources(path)

    try:
        preview = build_preview(
            resources,
            group_by=args.group_by,
            tag_key=args.tag_key,
            sample=args.sample,
            with_tags=args.with_tags,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)

    if args.format == "json":
        print(json.dumps(preview, indent=2, ensure_ascii=False))
    else:
        print(format_preview_text(preview))


if __name__ == "__main__":
    main()
