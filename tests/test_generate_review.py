"""Tests for scripts/generate_review.py — section builders + decision detection."""

import json
import sys
from pathlib import Path

import pytest
from ruamel.yaml import YAML

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from generate_review import (  # type: ignore  # noqa: E402
    build_decisions_section,
    build_parameters_section,
    build_precheck_section,
    build_resources_section,
    build_review_markdown,
    detect_decisions,
    load_raw,
    load_template,
    parse_existing_review,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _yaml_from_str(text: str):
    return YAML().load(text)


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------


def test_parameters_section_emits_markdown_table_with_targets():
    """Parameters section emits a markdown table with a `<请填>` placeholder."""
    template = _yaml_from_str(
        """
        Parameters:
          AmiId:
            Type: AWS::SSM::Parameter::Value<AWS::EC2::Image::Id>
            Default: /aws/service/ami-amazon-linux-latest/al2023
            Description: AMI for the stack
          BucketPrefix:
            Type: String
            Default: myapp
            Description: S3 prefix
        Resources: {}
        """
    )
    md, names = build_parameters_section(template)

    assert "AmiId" in names and "BucketPrefix" in names
    assert "| AmiId |" in md
    assert "**<请填>**" in md
    assert "AMI for the stack" in md


def test_resources_section_checkbox_state_preserves_previous_unchecked():
    """Logical ids in previous_unchecked render with ``[ ]``; others ``[x]``."""
    template = _yaml_from_str(
        """
        Resources:
          Foo:
            Type: AWS::Lambda::Function
          Bar:
            Type: AWS::S3::Bucket
        """
    )
    md, names = build_resources_section(template, previous_unchecked={"Bar"})

    assert "Foo" in names and "Bar" in names
    assert "- [x] Lambda::Function / Foo" in md
    assert "- [ ] S3::Bucket / Bar" in md


def test_decisions_detect_peering_data_iam_kms(tmp_path: Path):
    """detect_decisions catches peering / data migration / IAM trust / KMS cross-account.

    Uses a *different* source_account so the fixture's KMS ARN
    (account 123456789012) registers as cross-account.
    """
    template = load_template(FIXTURES / "sample_cfn_phase2.yml")
    raw_resources = load_raw(FIXTURES / "sample_raw_phase2.json")

    decisions = detect_decisions(
        template, raw_resources, source_account="000000000000"
    )
    ids = [d["id"] for d in decisions]

    assert any(i.startswith("D.peering.") for i in ids)
    assert any(i.startswith("D.data-migration.") for i in ids)
    assert any(i.startswith("D.iam-trust.") for i in ids)
    assert any(i.startswith("D.kms-cross-account.") for i in ids)


def test_precheck_section_extracts_failures_and_warnings():
    """build_precheck_section extracts 🔴 + 🟡 blocks out of the report markdown."""
    precheck_md = """# Precheck Report — cleaned.yml

## 🔴 Failures
- **ami-availability**: AMI ami-0abc not found in us-east-1

## 🟡 Warnings
- **s3-bucket-names**: myapp-bucket already exists

## 📊 Summary
"""
    out = build_precheck_section(precheck_md)
    assert "🔴 预检失败项" in out
    assert "ami-availability" in out
    assert "s3-bucket-names" in out


def test_build_review_markdown_contains_all_four_sections(tmp_path: Path):
    """build_review_markdown stitches the 4 review blocks together."""
    template = load_template(FIXTURES / "sample_cfn_phase2.yml")
    raw = load_raw(FIXTURES / "sample_raw_phase2.json")
    md = build_review_markdown(
        template,
        raw,
        precheck_md=None,
        stack_name="myapp-prod",
        preset="cross-account",
        source="123456789012/ap-northeast-1",
        target="999988887777/ap-northeast-1",
        source_account="123456789012",
    )

    assert "🟢 Parameters" in md
    assert "🟡 Resources" in md
    assert "⚠️ 需人工决策" in md
    assert "🔴 预检失败项" in md
    # Stack name appears in the header
    assert "myapp-prod" in md


def test_parse_existing_review_preserves_filled_values_and_unchecks(tmp_path: Path):
    """--update mode parser recovers filled Parameters + unchecked resources."""
    review_path = tmp_path / "review.md"
    review_path.write_text(
        """# Deployment Review — test

## 🟢 Parameters（目标值必填）

| 参数名 | 类型 | 源值/默认 | 目标值 | 说明 |
|--------|------|-----------|--------|------|
| AmiId | String | `` | ami-newvalue | AMI |
| BucketPrefix | String | `myapp` | **<请填>** | Prefix |

## 🟡 Resources（勾选要部署的资源，默认全选）

- [x] Lambda::Function / Foo
- [ ] S3::Bucket / Bar

## ⚠️ 需人工决策
_无_
""",
        encoding="utf-8",
    )

    prev = parse_existing_review(review_path)

    assert prev["param_values"] == {"AmiId": "ami-newvalue"}
    assert prev["unchecked"] == {"Bar"}
