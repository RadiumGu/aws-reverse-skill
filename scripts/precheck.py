#!/usr/bin/env python3
"""precheck.py — Deep pre-deployment validation for cleaned CFN templates.

Runs a suite of checks and emits a markdown report. All AWS API calls go
through an injectable boto3 client factory (``client_factory``) so the whole
pipeline is test-friendly without hitting real AWS.

Checks (all non-fail-fast — collected into the report):
  1. cfn-lint (local subprocess, optional)
  2. aws cloudformation validate-template (API, optional)
  3. AMI availability in target region (EC2 DescribeImages)
  4. KMS CMK existence in target account (KMS DescribeKey)
  5. S3 bucket name global availability (S3 HeadBucket)
  6. Service quotas: VPC / EIP / Lambda concurrency (ServiceQuotas)
  7. Scope check — compare raw.json resources vs cleaned.yml Resources,
     report any dropped resources.

Usage:
    python scripts/precheck.py \\
        --template out/cleaned.yml \\
        --raw out/raw.json \\
        --target-region us-east-1 \\
        --output out/precheck-report.md
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"
SKIP = "SKIP"


@dataclass
class CheckResult:
    """One precheck result line."""

    name: str
    status: str  # PASS / FAIL / WARN / SKIP
    detail: str = ""
    hint: str = ""


@dataclass
class PrecheckReport:
    """Collection of check results."""

    template: str = ""
    target_region: str = ""
    results: list[CheckResult] = field(default_factory=list)

    def add(
        self, name: str, status: str, detail: str = "", hint: str = ""
    ) -> CheckResult:
        r = CheckResult(name=name, status=status, detail=detail, hint=hint)
        self.results.append(r)
        return r

    @property
    def failures(self) -> list[CheckResult]:
        return [r for r in self.results if r.status == FAIL]

    @property
    def warnings(self) -> list[CheckResult]:
        return [r for r in self.results if r.status == WARN]


# ---------------------------------------------------------------------------
# boto3 client factory (injectable for tests)
# ---------------------------------------------------------------------------


def default_client_factory(service: str, region: str = "") -> Any:
    """Create a boto3 client. Imported lazily so tests without boto3 pass."""
    import boto3  # type: ignore[import-untyped]

    if region:
        return boto3.client(service, region_name=region)
    return boto3.client(service)


ClientFactory = Callable[..., Any]


# ---------------------------------------------------------------------------
# YAML helpers
# ---------------------------------------------------------------------------


def load_template(path: Path) -> CommentedMap:
    yaml = YAML()
    yaml.preserve_quotes = True
    with path.open(encoding="utf-8") as fh:
        data = yaml.load(fh)
    if data is None:
        raise ValueError(f"Empty or invalid YAML: {path}")
    return data


def extract_resources_by_type(
    template: CommentedMap,
) -> dict[str, list[tuple[str, CommentedMap]]]:
    """Return ``{Type: [(logical_id, resource_map), ...]}``."""
    by_type: dict[str, list[tuple[str, CommentedMap]]] = {}
    resources = template.get("Resources")
    if not isinstance(resources, CommentedMap):
        return by_type
    for logical_id, res in resources.items():
        if not isinstance(res, CommentedMap):
            continue
        rtype = res.get("Type", "")
        by_type.setdefault(rtype, []).append((str(logical_id), res))
    return by_type


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def check_cfn_lint(template_path: Path, report: PrecheckReport) -> None:
    """Run cfn-lint if available."""
    if shutil.which("cfn-lint") is None:
        report.add("cfn-lint", SKIP, detail="cfn-lint not installed")
        return
    try:
        proc = subprocess.run(
            ["cfn-lint", str(template_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            report.add("cfn-lint", PASS)
        else:
            output = (proc.stdout + proc.stderr).strip()
            report.add(
                "cfn-lint",
                FAIL,
                detail=output[:1000] or "cfn-lint reported issues",
                hint="Fix lint errors before deploying.",
            )
    except OSError as exc:
        report.add("cfn-lint", SKIP, detail=f"could not run cfn-lint: {exc}")


def check_validate_template(
    template_path: Path,
    report: PrecheckReport,
    target_region: str,
    client_factory: ClientFactory,
) -> None:
    """Call aws cloudformation validate-template via boto3."""
    try:
        cfn = client_factory("cloudformation", region=target_region)
    except Exception as exc:  # pragma: no cover — factory failure
        report.add("validate-template", SKIP, detail=str(exc))
        return

    try:
        body = template_path.read_text(encoding="utf-8")
        cfn.validate_template(TemplateBody=body)
        report.add("validate-template", PASS)
    except Exception as exc:
        report.add(
            "validate-template",
            FAIL,
            detail=str(exc),
            hint="Template syntax rejected by CloudFormation.",
        )


def find_ami_ids(template: CommentedMap) -> list[str]:
    """Extract referenced AMI IDs from template parameters/resources."""
    found: list[str] = []
    params = template.get("Parameters", {})
    if isinstance(params, CommentedMap):
        for _name, p in params.items():
            if not isinstance(p, CommentedMap):
                continue
            desc = p.get("Description", "")
            if isinstance(desc, str):
                m = re.search(r"ami-[0-9a-f]{8,17}", desc)
                if m:
                    found.append(m.group(0))
    # Scan resources for ImageId hardcoded values
    resources = template.get("Resources", {})
    if isinstance(resources, CommentedMap):
        for _lid, res in resources.items():
            if not isinstance(res, CommentedMap):
                continue
            props = res.get("Properties")
            if isinstance(props, CommentedMap):
                img = props.get("ImageId")
                if isinstance(img, str) and img.startswith("ami-"):
                    found.append(img)
    return list(dict.fromkeys(found))


def check_ami_availability(
    template: CommentedMap,
    target_region: str,
    report: PrecheckReport,
    client_factory: ClientFactory,
) -> None:
    """Verify AMI IDs exist in target region."""
    ami_ids = find_ami_ids(template)
    if not ami_ids:
        report.add("ami-availability", SKIP, detail="no AMI references")
        return
    try:
        ec2 = client_factory("ec2", region=target_region)
    except Exception as exc:
        report.add("ami-availability", SKIP, detail=str(exc))
        return

    for ami_id in ami_ids:
        try:
            resp = ec2.describe_images(ImageIds=[ami_id])
            images = resp.get("Images", []) if isinstance(resp, dict) else []
            if images:
                report.add("ami-availability", PASS, detail=f"{ami_id} exists")
            else:
                report.add(
                    "ami-availability",
                    FAIL,
                    detail=f"AMI {ami_id} not found in {target_region}",
                    hint="Run aws ec2 copy-image or set AmiId Parameter.",
                )
        except Exception as exc:
            report.add(
                "ami-availability",
                FAIL,
                detail=f"{ami_id}: {exc}",
                hint="Run aws ec2 copy-image or set AmiId Parameter.",
            )


def find_kms_arns(template: CommentedMap) -> list[str]:
    """Extract KMS CMK ARNs referenced in the template."""
    pattern = re.compile(r"arn:aws[a-z\-]*:kms:[a-z0-9\-]+:\d{12}:key/[0-9a-f\-]{36}")
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, str):
            for m in pattern.finditer(node):
                found.add(m.group(0))
        elif isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(dict(template))
    return sorted(found)


def check_kms_existence(
    template: CommentedMap,
    target_account: str,
    target_region: str,
    report: PrecheckReport,
    client_factory: ClientFactory,
) -> None:
    """Verify KMS CMK ARNs exist in the target account."""
    arns = find_kms_arns(template)
    if not arns:
        report.add("kms-existence", SKIP, detail="no KMS ARN references")
        return
    try:
        kms = client_factory("kms", region=target_region)
    except Exception as exc:
        report.add("kms-existence", SKIP, detail=str(exc))
        return

    for arn in arns:
        try:
            kms.describe_key(KeyId=arn)
            report.add("kms-existence", PASS, detail=arn)
        except Exception as exc:
            report.add(
                "kms-existence",
                FAIL,
                detail=f"{arn}: {exc}",
                hint=(
                    "Create target-account CMK or share the source CMK "
                    "via key policy + grant."
                ),
            )


def find_s3_bucket_names(template: CommentedMap) -> list[str]:
    """Extract literal S3 bucket names (excluding !Sub wrapped)."""
    names: list[str] = []
    resources = template.get("Resources", {})
    if not isinstance(resources, CommentedMap):
        return names
    for _lid, res in resources.items():
        if not isinstance(res, CommentedMap) or res.get("Type") != "AWS::S3::Bucket":
            continue
        props = res.get("Properties")
        if isinstance(props, CommentedMap):
            name = props.get("BucketName")
            if isinstance(name, str) and not name.startswith("${"):
                names.append(name)
    return names


def check_s3_bucket_names(
    template: CommentedMap,
    report: PrecheckReport,
    client_factory: ClientFactory,
) -> None:
    """Check S3 bucket name availability in the global namespace."""
    names = find_s3_bucket_names(template)
    if not names:
        report.add("s3-bucket-names", SKIP, detail="no literal BucketName")
        return
    try:
        s3 = client_factory("s3")
    except Exception as exc:
        report.add("s3-bucket-names", SKIP, detail=str(exc))
        return

    for name in names:
        try:
            s3.head_bucket(Bucket=name)
            # If HeadBucket succeeds the bucket exists — might be ours, but flag WARN
            report.add(
                "s3-bucket-names",
                WARN,
                detail=f"{name} already exists globally",
                hint="Use BucketPrefix Parameter to avoid collision.",
            )
        except Exception as exc:
            msg = str(exc)
            if "404" in msg or "NoSuchBucket" in msg or "Not Found" in msg:
                report.add("s3-bucket-names", PASS, detail=f"{name} available")
            elif "403" in msg or "Forbidden" in msg:
                report.add(
                    "s3-bucket-names",
                    WARN,
                    detail=f"{name} taken by another account",
                    hint="Use BucketPrefix to make the name unique.",
                )
            else:
                report.add("s3-bucket-names", WARN, detail=f"{name}: {msg}")


def check_quotas(
    target_region: str,
    report: PrecheckReport,
    client_factory: ClientFactory,
) -> None:
    """Soft-check key Service Quotas (VPC / EIP / Lambda concurrency)."""
    try:
        sq = client_factory("service-quotas", region=target_region)
    except Exception as exc:
        report.add("quotas", SKIP, detail=str(exc))
        return

    targets = [
        ("vpc", "L-F678F1CE", "VPCs per region"),
        ("ec2", "L-0263D0A3", "Elastic IP addresses"),
        ("lambda", "L-B99A9384", "Concurrent executions"),
    ]
    for service_code, quota_code, label in targets:
        try:
            resp = sq.get_service_quota(ServiceCode=service_code, QuotaCode=quota_code)
            quota = resp.get("Quota", {}) if isinstance(resp, dict) else {}
            value = quota.get("Value", "?")
            report.add("quotas", PASS, detail=f"{label}: {value}")
        except Exception as exc:
            report.add("quotas", WARN, detail=f"{label}: {exc}")


def check_scope(
    template: CommentedMap,
    raw_path: Path | None,
    report: PrecheckReport,
) -> None:
    """Compare raw.json resources vs. cleaned template — report dropped IDs."""
    if raw_path is None or not raw_path.exists():
        report.add("scope", SKIP, detail="raw.json not provided")
        return
    try:
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        report.add("scope", WARN, detail=f"raw.json parse error: {exc}")
        return
    raw_resources = (
        raw.get("resources", []) if isinstance(raw, dict) else (raw or [])
    )
    raw_ids = {r.get("PhysicalId", "") for r in raw_resources if r.get("PhysicalId")}
    template_ids: set[str] = set()
    resources = template.get("Resources", {})
    if isinstance(resources, CommentedMap):
        for lid, res in resources.items():
            template_ids.add(str(lid))
            if isinstance(res, CommentedMap):
                props = res.get("Properties")
                if isinstance(props, CommentedMap):
                    for v in props.values():
                        if isinstance(v, str):
                            template_ids.add(v)
    missing = [pid for pid in raw_ids if pid and pid not in template_ids]
    if missing:
        report.add(
            "scope",
            WARN,
            detail=f"{len(missing)} raw.json resources missing from cleaned template",
            hint=(
                "Dropped IDs: " + ", ".join(missing[:10])
                + (" ..." if len(missing) > 10 else "")
            ),
        )
    else:
        report.add("scope", PASS, detail=f"{len(raw_ids)} raw IDs present")


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def render_markdown(report: PrecheckReport) -> str:
    """Render *report* as Markdown."""
    lines: list[str] = []
    lines.append(f"# Precheck Report — {Path(report.template).name}")
    lines.append("")
    lines.append(f"- Template: `{report.template}`")
    if report.target_region:
        lines.append(f"- Target region: `{report.target_region}`")
    lines.append("")
    status_counts: dict[str, int] = {PASS: 0, FAIL: 0, WARN: 0, SKIP: 0}
    for r in report.results:
        status_counts[r.status] = status_counts.get(r.status, 0) + 1
    lines.append(
        "**Summary**: "
        + " ｜ ".join(f"{k}: {v}" for k, v in status_counts.items())
    )
    lines.append("")
    lines.append("| Check | Status | Detail | Hint |")
    lines.append("|-------|--------|--------|------|")
    for r in report.results:
        detail = r.detail.replace("|", r"\|").replace("\n", " ")
        hint = r.hint.replace("|", r"\|").replace("\n", " ")
        lines.append(f"| {r.name} | {r.status} | {detail} | {hint} |")
    lines.append("")

    if report.failures:
        lines.append("## 🔴 Failures")
        for r in report.failures:
            lines.append(f"- **{r.name}**: {r.detail}")
            if r.hint:
                lines.append(f"  - 修复建议: {r.hint}")
        lines.append("")

    if report.warnings:
        lines.append("## 🟡 Warnings")
        for r in report.warnings:
            lines.append(f"- **{r.name}**: {r.detail}")
            if r.hint:
                lines.append(f"  - 提示: {r.hint}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def run_precheck(
    template_path: Path,
    target_region: str = "",
    target_account: str = "",
    raw_path: Path | None = None,
    client_factory: ClientFactory = default_client_factory,
    deep: bool = False,
) -> PrecheckReport:
    """Run all precheck steps and return a populated :class:`PrecheckReport`.

    Args:
        template_path: Path to cleaned CFN YAML.
        target_region: Target AWS region for AMI / quota / KMS / validate.
        target_account: Target AWS account (informational).
        raw_path: Optional path to raw.json for scope check.
        client_factory: Callable returning boto3 clients (mockable).
        deep: Enable AWS-API-dependent checks (AMI / KMS / S3 / quotas / validate).
    """
    report = PrecheckReport(template=str(template_path), target_region=target_region)
    template = load_template(template_path)

    check_cfn_lint(template_path, report)

    if deep:
        check_validate_template(
            template_path, report, target_region, client_factory
        )
        check_ami_availability(template, target_region, report, client_factory)
        check_kms_existence(
            template, target_account, target_region, report, client_factory
        )
        check_s3_bucket_names(template, report, client_factory)
        check_quotas(target_region, report, client_factory)

    check_scope(template, raw_path, report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Deep precheck for cleaned CFN template."
    )
    parser.add_argument("--template", required=True, help="Path to cleaned CFN YAML")
    parser.add_argument("--raw", default="", help="Optional raw.json for scope check")
    parser.add_argument(
        "--target-region", default="", help="Target region for AWS-side checks"
    )
    parser.add_argument(
        "--target-account", default="", help="Target account (informational)"
    )
    parser.add_argument(
        "--output",
        default="",
        help="Path to write markdown report (default: stdout)",
    )
    parser.add_argument(
        "--deep",
        action="store_true",
        help="Run AWS-API checks (AMI / KMS / S3 / quotas / validate-template).",
    )
    args = parser.parse_args()

    template_path = Path(args.template)
    if not template_path.exists():
        print(f"ERROR: template not found: {template_path}", file=sys.stderr)
        sys.exit(1)

    raw_path = Path(args.raw) if args.raw else None

    report = run_precheck(
        template_path,
        target_region=args.target_region,
        target_account=args.target_account,
        raw_path=raw_path,
        deep=args.deep,
    )

    markdown = render_markdown(report)
    if args.output:
        Path(args.output).write_text(markdown, encoding="utf-8")
        print(f"Wrote precheck report to {args.output}", file=sys.stderr)
    else:
        print(markdown)

    from audit import record_stage
    audit_inputs = [str(template_path)]
    if raw_path:
        audit_inputs.append(str(raw_path))
    record_stage(
        step="precheck",
        inputs=audit_inputs,
        outputs=[args.output] if args.output else [],
        notes=(
            f"failures={len(report.failures)} warnings={len(report.warnings)} "
            f"deep={args.deep}"
        ),
    )

    # Non-zero exit only on failures (WARN is tolerated)
    sys.exit(1 if report.failures else 0)


if __name__ == "__main__":
    main()
