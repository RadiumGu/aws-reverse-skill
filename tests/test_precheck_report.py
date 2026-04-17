"""Tests for scripts/precheck.py — report rendering + AWS-side checks with mocks."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from precheck import (  # type: ignore  # noqa: E402
    FAIL,
    PASS,
    WARN,
    CheckResult,
    PrecheckReport,
    check_ami_availability,
    check_scope,
    find_ami_ids,
    find_kms_arns,
    find_s3_bucket_names,
    load_template,
    render_markdown,
    run_precheck,
)

FIXTURES = Path(__file__).parent / "fixtures"


class _FakeEc2:
    """Minimal boto3-like EC2 client returning preconfigured DescribeImages."""

    def __init__(self, available_amis: set[str]):
        self.available = available_amis

    def describe_images(self, ImageIds):  # noqa: N803 — boto3 style kwarg
        return {
            "Images": [{"ImageId": a} for a in ImageIds if a in self.available]
        }


def _factory(client_map: dict[str, object]):
    def factory(service: str, region: str = ""):
        if service in client_map:
            return client_map[service]
        raise RuntimeError(f"no client for {service}")

    return factory


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_render_markdown_highlights_failures_and_warnings():
    """render_markdown emits 🔴 Failures and 🟡 Warnings sections when present."""
    report = PrecheckReport(template="cleaned.yml", target_region="us-east-1")
    report.add("cfn-lint", PASS)
    report.add("ami-availability", FAIL, detail="missing AMI", hint="copy it")
    report.add("s3-bucket-names", WARN, detail="name taken")

    md = render_markdown(report)

    assert "# Precheck Report" in md
    assert "🔴 Failures" in md
    assert "🟡 Warnings" in md
    assert "missing AMI" in md
    assert "name taken" in md
    assert "| ami-availability | FAIL" in md


# ---------------------------------------------------------------------------
# Extractors
# ---------------------------------------------------------------------------


def test_find_resource_helpers_return_expected_values():
    """find_ami_ids / find_kms_arns / find_s3_bucket_names work on the fixture."""
    template = load_template(FIXTURES / "sample_cfn_phase2.yml")

    arns = find_kms_arns(template)
    assert (
        "arn:aws:kms:ap-northeast-1:123456789012:key/"
        "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    ) in arns

    buckets = find_s3_bucket_names(template)
    assert "myapp-encrypted-bucket" in buckets
    assert "myapp-global-data-bucket" in buckets

    dirty = load_template(FIXTURES / "sample_cfn_dirty.yml")
    ami_ids = find_ami_ids(dirty)
    assert "ami-0abcdef1234567890" in ami_ids


# ---------------------------------------------------------------------------
# AMI + scope checks with injected factory
# ---------------------------------------------------------------------------


def test_check_ami_availability_fail_when_missing():
    """check_ami_availability marks FAIL when the AMI is not in the region."""
    template = load_template(FIXTURES / "sample_cfn_dirty.yml")
    report = PrecheckReport(template="cleaned.yml", target_region="us-east-1")

    factory = _factory({"ec2": _FakeEc2(available_amis=set())})
    check_ami_availability(template, "us-east-1", report, factory)

    statuses = {r.status for r in report.results if r.name == "ami-availability"}
    assert FAIL in statuses


def test_run_precheck_scope_pass(tmp_path: Path):
    """run_precheck scope check PASSes when raw resources all map to template."""
    template_path = tmp_path / "cleaned.yml"
    template_path.write_text(
        """Resources:
  Foo:
    Type: AWS::Lambda::Function
    Properties:
      FunctionName: myapp-fn
""",
        encoding="utf-8",
    )
    raw_path = tmp_path / "raw.json"
    raw_path.write_text(
        '{"resources": [{"Type": "AWS::Lambda::Function", '
        '"PhysicalId": "myapp-fn"}]}',
        encoding="utf-8",
    )

    report = run_precheck(
        template_path,
        raw_path=raw_path,
        deep=False,
        client_factory=_factory({}),
    )
    scope = next(r for r in report.results if r.name == "scope")
    assert scope.status == PASS
