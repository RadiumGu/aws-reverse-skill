#!/usr/bin/env python3
"""apply_review.py — Parse admin-edited ``review.md`` back into CFN artifacts.

Outputs:
    - deploy-params.json   : CFN --parameter-overrides compatible JSON
    - cleaned-final.yml    : template with unchecked resources removed
    - manual-tasks.md      : decisions that cannot be applied automatically
    - review.diff          : textual diff cleaned.yml vs cleaned-final.yml

Usage:
    python scripts/apply_review.py out/review.md \\
        --cleaned out/cleaned.yml \\
        --out-final out/cleaned-final.yml \\
        --out-params out/deploy-params.json \\
        --out-manual out/manual-tasks.md \\
        --diff out/review.diff
"""

import argparse
import difflib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap


# ---------------------------------------------------------------------------
# Review parser
# ---------------------------------------------------------------------------

_PARAM_ROW_RE = re.compile(
    r"^\|\s*([A-Za-z][\w]*)\s*\|[^|]*\|[^|]*\|\s*(.*?)\s*\|[^|]*\|\s*$"
)
_RESOURCE_ROW_RE = re.compile(r"^-\s*\[\s*([ xX])\s*\]\s*([^/]+?)\s*/\s*(\S+)\s*$")
_DECISION_HDR_RE = re.compile(r"^###\s+(D\.[\w\-.]+)\s*(?:—|-)?\s*(.*)")


@dataclass
class ParsedReview:
    parameters: dict[str, str]
    resource_states: dict[str, bool]  # logical_id → True if checked
    decisions: list[dict[str, Any]]


def parse_review(md: str) -> ParsedReview:
    """Parse a review.md text into :class:`ParsedReview`."""
    params: dict[str, str] = {}
    resource_states: dict[str, bool] = {}
    decisions: list[dict[str, Any]] = []

    section = ""
    current_decision: dict[str, Any] | None = None
    code_block_open = False
    code_lang = ""

    lines = md.splitlines()
    for raw_line in lines:
        line = raw_line

        # Track section
        if line.startswith("## "):
            # Close any open decision
            if current_decision is not None:
                decisions.append(current_decision)
                current_decision = None
            if "Parameters" in line:
                section = "params"
            elif "Resources" in line:
                section = "resources"
            elif "人工决策" in line or "Decisions" in line or "需人工决策" in line:
                section = "decisions"
            elif "预检失败" in line or "Failures" in line:
                section = "failures"
            else:
                section = "other"
            continue

        # Parameter rows
        if section == "params":
            m = _PARAM_ROW_RE.match(line)
            if m:
                name, value = m.group(1), m.group(2).strip()
                # Skip header separator + template row
                if name.lower() in ("参数名", "name"):
                    continue
                if value.startswith("-") and value.replace("-", "") == "":
                    continue
                if value and value != "**<请填>**" and "<请填>" not in value:
                    # Strip backticks / ** formatting
                    stripped = value.strip("`* ")
                    if stripped:
                        params[name] = stripped

        # Resource rows
        if section == "resources":
            m = _RESOURCE_ROW_RE.match(line)
            if m:
                checked = m.group(1).lower() == "x"
                logical_id = m.group(3).strip()
                resource_states[logical_id] = checked

        # Decisions
        if section == "decisions":
            hdr = _DECISION_HDR_RE.match(line)
            if hdr:
                if current_decision is not None:
                    decisions.append(current_decision)
                current_decision = {
                    "id": hdr.group(1),
                    "title": hdr.group(2).strip(),
                    "body": [],
                    "fields": [],
                }
                code_block_open = False
                continue
            if current_decision is not None:
                if line.startswith("```"):
                    code_block_open = not code_block_open
                    continue
                if code_block_open:
                    current_decision["fields"].append(line)
                else:
                    current_decision["body"].append(line)

    if current_decision is not None:
        decisions.append(current_decision)

    return ParsedReview(
        parameters=params,
        resource_states=resource_states,
        decisions=decisions,
    )


# ---------------------------------------------------------------------------
# YAML helpers
# ---------------------------------------------------------------------------


def _make_yaml() -> YAML:
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.default_flow_style = False
    yaml.width = 120
    return yaml


def load_template(path: Path) -> CommentedMap:
    yaml = _make_yaml()
    with path.open(encoding="utf-8") as fh:
        data = yaml.load(fh)
    if data is None:
        raise ValueError(f"Empty YAML: {path}")
    return data


def dump_template(template: CommentedMap, path: Path) -> None:
    yaml = _make_yaml()
    with path.open("w", encoding="utf-8") as fh:
        yaml.dump(template, fh)


# ---------------------------------------------------------------------------
# Apply operations
# ---------------------------------------------------------------------------


def apply_resource_removal(
    template: CommentedMap, states: dict[str, bool]
) -> list[str]:
    """Remove unchecked resources. Returns list of removed logical ids."""
    resources = template.get("Resources")
    if not isinstance(resources, CommentedMap):
        return []
    removed: list[str] = []
    for logical_id in list(resources.keys()):
        if states.get(str(logical_id), True) is False:
            removed.append(str(logical_id))
            del resources[logical_id]
    return removed


def build_deploy_params(parameters: dict[str, str]) -> list[dict[str, str]]:
    """Return CloudFormation ``parameter-overrides`` compatible list."""
    return [
        {"ParameterKey": k, "ParameterValue": v} for k, v in parameters.items()
    ]


def apply_decisions_to_template(
    template: CommentedMap, decisions: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Apply simple decisions to the template; return (applied, manual)."""
    applied: list[dict[str, Any]] = []
    manual: list[dict[str, Any]] = []

    resources = template.get("Resources") or CommentedMap()

    for d in decisions:
        did = d.get("id", "")
        fields = d.get("fields", [])
        handled = False
        if did.startswith("D.peering."):
            logical_id = did[len("D.peering."):]
            # Look for "PeeringConnectionId: xxx"
            for raw in fields:
                m = re.match(
                    r"\s*PeeringConnectionId:\s*(\S+)\s*", raw
                )
                if m and isinstance(resources, CommentedMap):
                    val = m.group(1)
                    if val in ("<pcx-xxx", "<pcx-xxx>", "SKIP"):
                        manual.append({**d, "reason": "Admin selected SKIP"})
                        handled = True
                        break
                    res = resources.get(logical_id)
                    if isinstance(res, CommentedMap):
                        props = res.setdefault("Properties", CommentedMap())
                        if isinstance(props, CommentedMap):
                            props["PeeringConnectionId"] = val
                            applied.append({**d, "value": val})
                            handled = True
                            break
        if not handled:
            manual.append(d)
    return applied, manual


def build_manual_tasks_md(
    manual: list[dict[str, Any]], precheck_failures: list[str] | None = None
) -> str:
    if not manual and not precheck_failures:
        return "# Manual Tasks\n\n_无_\n"
    lines = ["# Manual Tasks", ""]
    if manual:
        lines.append("## 决策项（skill 未自动应用）")
        lines.append("")
        for d in manual:
            lines.append(f"### {d.get('id', '')} — {d.get('title', '')}")
            for body_line in d.get("body", []):
                if body_line.strip():
                    lines.append(body_line)
            for f in d.get("fields", []):
                lines.append(f"- 管理员填写: `{f}`")
            if "reason" in d:
                lines.append(f"- 备注: {d['reason']}")
            lines.append("")
    if precheck_failures:
        lines.append("## 预检失败项")
        lines.extend(precheck_failures)
        lines.append("")
    return "\n".join(lines)


def build_diff(original: str, modified: str, path_a: str, path_b: str) -> str:
    return "\n".join(
        difflib.unified_diff(
            original.splitlines(),
            modified.splitlines(),
            fromfile=path_a,
            tofile=path_b,
            lineterm="",
        )
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def apply_review(
    review_md: str,
    cleaned_path: Path,
    out_final: Path,
    out_params: Path,
    out_manual: Path,
    diff_path: Path | None = None,
) -> dict[str, Any]:
    """Apply a parsed review to the cleaned template and emit artifacts.

    Returns a summary dict useful for tests / SKILL.md.
    """
    parsed = parse_review(review_md)
    template = load_template(cleaned_path)
    original_text = cleaned_path.read_text(encoding="utf-8")

    removed = apply_resource_removal(template, parsed.resource_states)
    applied, manual = apply_decisions_to_template(template, parsed.decisions)

    out_final.parent.mkdir(parents=True, exist_ok=True)
    dump_template(template, out_final)

    params_payload = build_deploy_params(parsed.parameters)
    out_params.write_text(
        json.dumps(params_payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    manual_md = build_manual_tasks_md(manual)
    out_manual.write_text(manual_md, encoding="utf-8")

    diff_text = ""
    if diff_path is not None:
        modified_text = out_final.read_text(encoding="utf-8")
        diff_text = build_diff(
            original_text, modified_text, str(cleaned_path), str(out_final)
        )
        diff_path.write_text(diff_text, encoding="utf-8")

    return {
        "parameters": parsed.parameters,
        "removed": removed,
        "applied": applied,
        "manual": manual,
        "diff": diff_text,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply review.md to CFN artifacts.")
    parser.add_argument("review", help="Path to edited review.md")
    parser.add_argument("--cleaned", required=True, help="Path to cleaned.yml")
    parser.add_argument("--out-final", required=True, help="Output cleaned-final.yml")
    parser.add_argument(
        "--out-params", required=True, help="Output deploy-params.json"
    )
    parser.add_argument(
        "--out-manual", required=True, help="Output manual-tasks.md"
    )
    parser.add_argument(
        "--diff", default="", help="Optional review.diff output path"
    )
    args = parser.parse_args()

    review_path = Path(args.review)
    if not review_path.exists():
        print(f"ERROR: review file not found: {review_path}", file=sys.stderr)
        sys.exit(1)

    md = review_path.read_text(encoding="utf-8")
    summary = apply_review(
        md,
        Path(args.cleaned),
        Path(args.out_final),
        Path(args.out_params),
        Path(args.out_manual),
        Path(args.diff) if args.diff else None,
    )
    print(
        f"Applied: {len(summary['parameters'])} params, "
        f"removed {len(summary['removed'])} resources, "
        f"{len(summary['applied'])} auto-applied decisions, "
        f"{len(summary['manual'])} manual tasks.",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
