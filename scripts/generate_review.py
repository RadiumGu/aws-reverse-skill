#!/usr/bin/env python3
"""generate_review.py — Produce ``review.md`` for admin deployment sign-off.

Input:
    - cleaned.yml  (post-rewrite template)
    - raw.json     (original scan)
    - precheck-report.md (optional — pulled into 🔴 Failures block)

Output:
    - review.md with 4 sections:
        🟢 Parameters         (CFN Parameters → fill target values)
        🟡 Resources          (checkbox list to deselect)
        ⚠️ 需人工决策          (Peering / data / IAM / KMS)
        🔴 预检失败项          (lifted from precheck-report.md)

Update mode (``--update``): re-generate review.md while preserving already
filled Parameter values + existing checkbox states from the previous file.
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap


# ---------------------------------------------------------------------------
# YAML
# ---------------------------------------------------------------------------


def load_template(path: Path) -> CommentedMap:
    yaml = YAML()
    yaml.preserve_quotes = True
    with path.open(encoding="utf-8") as fh:
        data = yaml.load(fh)
    if data is None:
        raise ValueError(f"Empty or invalid YAML: {path}")
    return data


def load_raw(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("resources", [])
    return []


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------


def build_parameters_section(
    template: CommentedMap, filled_values: dict[str, str] | None = None
) -> tuple[str, list[str]]:
    """Return markdown for the 🟢 Parameters section + list of param names."""
    filled = filled_values or {}
    params = template.get("Parameters")
    if not isinstance(params, CommentedMap) or not params:
        return (
            "## 🟢 Parameters（目标值必填）\n\n"
            "_当前模板无 Parameters_\n",
            [],
        )

    lines = ["## 🟢 Parameters（目标值必填）", ""]
    lines.append("| 参数名 | 类型 | 源值/默认 | 目标值 | 说明 |")
    lines.append("|--------|------|-----------|--------|------|")

    names: list[str] = []
    for name, spec in params.items():
        if not isinstance(spec, CommentedMap):
            continue
        names.append(str(name))
        ptype = spec.get("Type", "String")
        default = spec.get("Default", "")
        desc = spec.get("Description", "")
        target = filled.get(str(name), "**<请填>**")
        lines.append(
            f"| {name} | {ptype} | `{default}` | {target} | {desc} |"
        )
    lines.append("")
    return "\n".join(lines), names


def build_resources_section(
    template: CommentedMap,
    previous_unchecked: set[str] | None = None,
) -> tuple[str, list[str]]:
    """Return markdown for the 🟡 Resources section + list of logical ids."""
    unchecked = previous_unchecked or set()
    resources = template.get("Resources")
    if not isinstance(resources, CommentedMap) or not resources:
        return "## 🟡 Resources（勾选要部署的资源，默认全选）\n\n_无资源_\n", []

    lines = ["## 🟡 Resources（勾选要部署的资源，默认全选）", ""]
    names: list[str] = []
    for logical_id, res in resources.items():
        if not isinstance(res, CommentedMap):
            continue
        rtype = res.get("Type", "")
        names.append(str(logical_id))
        mark = " " if str(logical_id) in unchecked else "x"
        short_type = rtype.replace("AWS::", "")
        lines.append(f"- [{mark}] {short_type} / {logical_id}")
    lines.append("")
    return "\n".join(lines), names


# ---------------------------------------------------------------------------
# Decision detection
# ---------------------------------------------------------------------------


_PEERING_TYPES = {
    "AWS::EC2::VPCPeeringConnection",
    "AWS::EC2::TransitGatewayAttachment",
    "AWS::EC2::TransitGatewayVpcAttachment",
}
_DATA_TYPES = {
    "AWS::RDS::DBInstance",
    "AWS::RDS::DBCluster",
    "AWS::DynamoDB::Table",
    "AWS::S3::Bucket",
    "AWS::EFS::FileSystem",
}

_EXTERNAL_ACCOUNT_RE = re.compile(r"arn:aws[a-z\-]*:iam::(\d{12}):")
_KMS_ARN_RE = re.compile(r"arn:aws[a-z\-]*:kms:[a-z0-9\-]+:(\d{12}):key/")


def detect_decisions(
    template: CommentedMap,
    raw_resources: list[dict[str, Any]],
    source_account: str = "",
) -> list[dict[str, Any]]:
    """Scan template + raw.json for items that need human decision."""
    decisions: list[dict[str, Any]] = []
    resources = template.get("Resources") or CommentedMap()

    # Peering / TGW from cleaned template
    if isinstance(resources, CommentedMap):
        for lid, res in resources.items():
            if not isinstance(res, CommentedMap):
                continue
            rtype = res.get("Type", "")
            if rtype in _PEERING_TYPES:
                decisions.append(
                    {
                        "id": f"D.peering.{lid}",
                        "title": f"跨账号 Peering/TGW: {lid}",
                        "detail": (
                            f"资源类型 {rtype}，需目标账号握手或 RAM 共享。"
                        ),
                        "fields": ["PeeringConnectionId: <pcx-xxx 或 SKIP>"],
                    }
                )

        # Cross-account IAM principal in trust policy
        for lid, res in resources.items():
            if not isinstance(res, CommentedMap):
                continue
            if res.get("Type") != "AWS::IAM::Role":
                continue
            props = res.get("Properties", {})
            if not isinstance(props, CommentedMap):
                continue
            trust = props.get("AssumeRolePolicyDocument", {})
            as_text = json.dumps(_to_plain(trust))
            for m in _EXTERNAL_ACCOUNT_RE.finditer(as_text):
                ext = m.group(1)
                if ext == source_account:
                    continue
                decisions.append(
                    {
                        "id": f"D.iam-trust.{lid}",
                        "title": f"IAM trust policy 含外部账号 {ext}",
                        "detail": (
                            f"Role `{lid}` trust policy 引用外部账号 {ext}。"
                            "确认目标环境 trust 是否仍然需要该外部账号。"
                        ),
                        "fields": [
                            f"TrustedAccountId: {ext} (keep/replace/remove)"
                        ],
                    }
                )

        # KMS cross-account references
        for lid, res in resources.items():
            if not isinstance(res, CommentedMap):
                continue
            as_text = json.dumps(_to_plain(res))
            for m in _KMS_ARN_RE.finditer(as_text):
                acct = m.group(1)
                if acct == source_account:
                    continue
                decisions.append(
                    {
                        "id": f"D.kms-cross-account.{lid}",
                        "title": f"KMS CMK 跨账号 (key owner: {acct})",
                        "detail": (
                            f"资源 `{lid}` 引用跨账号 KMS CMK，账号 {acct}。"
                            "目标账号需新建 CMK 或授权跨账号 grant。"
                        ),
                        "fields": [
                            "Action: [ ] new-cmk  [ ] cross-account-grant  [ ] skip"
                        ],
                    }
                )

    # Data-layer migration from raw scan
    data_hits: dict[str, list[str]] = {}
    for r in raw_resources:
        rtype = r.get("Type", "")
        if rtype in _DATA_TYPES:
            data_hits.setdefault(rtype, []).append(r.get("PhysicalId", ""))
    for rtype, ids in data_hits.items():
        decisions.append(
            {
                "id": f"D.data-migration.{rtype}",
                "title": f"数据迁移: {rtype} ({len(ids)} 项)",
                "detail": (
                    f"资源类型 {rtype} 不会自动迁移数据。"
                    f"样例 ID: {', '.join(ids[:3])}"
                ),
                "fields": [
                    "[ ] AWS DMS 持续同步",
                    "[ ] Snapshot + Copy + Restore",
                    "[ ] 放弃数据，空库启动",
                ],
            }
        )

    return decisions


def _to_plain(node: Any) -> Any:
    """Convert ruamel structures to plain Python for JSON dumping."""
    # dict-like (CommentedMap and plain dict)
    if isinstance(node, dict):
        return {str(k): _to_plain(v) for k, v in node.items()}
    # list-like (CommentedSeq and plain list/tuple)
    if isinstance(node, (list, tuple)):
        return [_to_plain(v) for v in node]
    # ruamel TaggedScalar (!Ref, !Sub, etc.) -> use string form
    tag = getattr(node, "tag", None)
    if tag is not None and not isinstance(node, (str, int, float, bool)):
        try:
            tag_name = getattr(tag, "value", str(tag))
            return f"{tag_name} {node}" if tag_name else str(node)
        except Exception:
            return str(node)
    if isinstance(node, (str, int, float, bool)) or node is None:
        return node
    # fallback: stringify anything else (datetime, ruamel quoted scalars, ...)
    return str(node)


def build_decisions_section(decisions: list[dict[str, Any]]) -> str:
    if not decisions:
        return "## ⚠️ 需人工决策（skill 无法自动处理）\n\n_无_\n"
    lines = ["## ⚠️ 需人工决策（skill 无法自动处理）", ""]
    for i, d in enumerate(decisions, 1):
        lines.append(f"### {d['id']} — {d['title']}")
        lines.append("")
        lines.append(d.get("detail", ""))
        lines.append("")
        for field in d.get("fields", []):
            lines.append(f"```")
            lines.append(field)
            lines.append(f"```")
        lines.append("")
    return "\n".join(lines)


def build_precheck_section(precheck_md: str | None) -> str:
    """Extract the Failures / Warnings tables from a precheck report.

    If *precheck_md* is None or empty, emit a placeholder.
    """
    if not precheck_md:
        return "## 🔴 预检失败项（需修复）\n\n_未提供 precheck-report.md_\n"

    failures: list[str] = []
    warnings: list[str] = []
    current: list[str] | None = None
    for line in precheck_md.splitlines():
        if line.startswith("## 🔴") or line.startswith("## Failures"):
            current = failures
            continue
        if line.startswith("## 🟡") or line.startswith("## Warnings"):
            current = warnings
            continue
        if line.startswith("##"):
            current = None
            continue
        if current is not None and line.strip():
            current.append(line)

    if not failures and not warnings:
        return "## 🔴 预检失败项（需修复）\n\n_precheck 全部通过_\n"

    lines = ["## 🔴 预检失败项（需修复）", ""]
    if failures:
        lines.extend(failures)
        lines.append("")
    if warnings:
        lines.append("### 🟡 Warnings")
        lines.extend(warnings)
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Parse existing review.md for --update mode
# ---------------------------------------------------------------------------


_PARAM_ROW_RE = re.compile(
    r"^\|\s*([A-Za-z0-9_]+)\s*\|[^|]*\|[^|]*\|\s*(.*?)\s*\|[^|]*\|\s*$"
)


def parse_existing_review(md_path: Path) -> dict[str, Any]:
    """Extract filled parameter values and checkbox states from an existing review.md."""
    if not md_path.exists():
        return {"param_values": {}, "unchecked": set()}
    text = md_path.read_text(encoding="utf-8")
    param_values: dict[str, str] = {}
    unchecked: set[str] = set()
    in_params = False
    for line in text.splitlines():
        if line.startswith("## 🟢 Parameters"):
            in_params = True
            continue
        if in_params and line.startswith("##"):
            in_params = False
        if in_params:
            m = _PARAM_ROW_RE.match(line)
            if m:
                name, value = m.group(1), m.group(2).strip()
                if (
                    value
                    and value != "**<请填>**"
                    and name not in ("参数名",)
                    and "----" not in value
                ):
                    param_values[name] = value

        m2 = re.match(r"^\-\s*\[\s*\]\s*\S+\s*/\s*(\S+)\s*$", line)
        if m2:
            unchecked.add(m2.group(1))

    return {"param_values": param_values, "unchecked": unchecked}


# ---------------------------------------------------------------------------
# Main assembly
# ---------------------------------------------------------------------------


def build_review_markdown(
    template: CommentedMap,
    raw_resources: list[dict[str, Any]],
    precheck_md: str | None = None,
    stack_name: str = "",
    preset: str = "",
    source: str = "",
    target: str = "",
    source_account: str = "",
    filled_values: dict[str, str] | None = None,
    previous_unchecked: set[str] | None = None,
) -> str:
    header = [
        f"# Deployment Review — {stack_name or 'unnamed-stack'}",
        f"> 规则预设: `{preset or 'default'}`  ｜ 源: `{source or '?'}`  ｜ 目标: `{target or '?'}`",
        "> 修改完成后运行: `python scripts/apply_review.py out/review.md ...`",
        "",
    ]

    params_md, _ = build_parameters_section(template, filled_values)
    resources_md, _ = build_resources_section(template, previous_unchecked)
    decisions = detect_decisions(template, raw_resources, source_account)
    decisions_md = build_decisions_section(decisions)
    precheck_section = build_precheck_section(precheck_md)

    freeform = "## 📝 Free-form notes（自由备注，不影响生成）\n\n```\n\n```\n"

    return "\n".join(
        [
            "\n".join(header),
            params_md,
            resources_md,
            decisions_md,
            precheck_section,
            freeform,
        ]
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate review.md for admin sign-off."
    )
    parser.add_argument("--cleaned", required=True, help="Path to cleaned.yml")
    parser.add_argument("--raw", default="", help="Path to raw.json (optional)")
    parser.add_argument(
        "--precheck-report",
        default="",
        help="Path to precheck-report.md (optional)",
    )
    parser.add_argument("--output", required=True, help="Output review.md path")
    parser.add_argument("--stack-name", default="")
    parser.add_argument("--preset", default="")
    parser.add_argument("--source", default="", help="Source account/region description")
    parser.add_argument("--target", default="", help="Target account/region description")
    parser.add_argument("--source-account", default="", help="Source 12-digit account ID")
    parser.add_argument(
        "--update",
        action="store_true",
        help="Preserve existing parameter values + checkbox states from --output file.",
    )
    args = parser.parse_args()

    template = load_template(Path(args.cleaned))
    raw_resources = load_raw(Path(args.raw)) if args.raw else []
    precheck_md = (
        Path(args.precheck_report).read_text(encoding="utf-8")
        if args.precheck_report and Path(args.precheck_report).exists()
        else None
    )

    filled: dict[str, str] = {}
    unchecked: set[str] = set()
    if args.update:
        prev = parse_existing_review(Path(args.output))
        filled = prev["param_values"]
        unchecked = prev["unchecked"]

    md = build_review_markdown(
        template,
        raw_resources,
        precheck_md=precheck_md,
        stack_name=args.stack_name,
        preset=args.preset,
        source=args.source,
        target=args.target,
        source_account=args.source_account,
        filled_values=filled,
        previous_unchecked=unchecked,
    )
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(md, encoding="utf-8")
    print(f"Wrote review.md to {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
