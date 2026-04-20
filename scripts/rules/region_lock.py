"""rules.region_lock — R15 region-locked resource detection (advisory only)."""

from __future__ import annotations

import sys
from pathlib import Path

from ruamel.yaml.comments import CommentedMap

sys.path.insert(0, str(Path(__file__).parent.parent))
from region_lock import find_region_locked  # noqa: E402

from .common import _Context, _record_review


def apply_region_lock_rule(
    template: CommentedMap, ctx: _Context
) -> None:
    """R15 — flag resources pinned to a specific region as review decisions."""
    if "region_lock" not in ctx.rules:
        return
    try:
        locked = find_region_locked(template, ctx.target_region)
    except Exception as exc:  # pragma: no cover
        ctx.nested_arn_warnings.append(f"R15 error: {exc}")
        return
    for entry in locked:
        if not entry.get("is_violation"):
            continue
        logical_id = entry["resource_logical_id"]
        rtype = entry["resource_type"]
        decision = {
            "id": f"D.region-constraint.{logical_id}",
            "kind": "region-constraint",
            "category": "region-constraint",
            "resource": logical_id,
            "resource_logical_id": logical_id,
            "resource_type": rtype,
            "required_region": entry["required_region"],
            "target_region": entry["target_region"],
            "affected_properties": list(entry.get("affected_properties", [])),
            "note": entry["note"],
            "is_violation": True,
            "suggested_action": (
                "Split this resource into a separate us-east-1 stack OR ensure "
                "the referenced resource (ACM cert / WAFv2 ACL) lives in "
                "us-east-1."
            ),
            "detail": (
                f"{rtype} `{logical_id}` requires region "
                f"{entry['required_region']}, but target region is "
                f"{entry['target_region']}."
            ),
        }
        _record_review(ctx, decision)
