"""Tests for scripts/apply_review.py — parse review.md + apply to cleaned.yml."""

import json
import sys
from pathlib import Path

import pytest
from ruamel.yaml import YAML

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from apply_review import (  # type: ignore  # noqa: E402
    apply_decisions_to_template,
    apply_resource_removal,
    apply_review,
    build_deploy_params,
    build_diff,
    build_manual_tasks_md,
    load_template,
    parse_review,
)

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# parse_review
# ---------------------------------------------------------------------------


def test_parse_review_extracts_filled_parameters():
    """Parameter rows with real target values land in parsed.parameters."""
    md = """## 🟢 Parameters（目标值必填）

| 参数名 | 类型 | 源值/默认 | 目标值 | 说明 |
|--------|------|-----------|--------|------|
| AmiId | String | `` | ami-newvalue | AMI |
| KmsKeyArn | String | `` | **<请填>** | CMK |
| BucketPrefix | String | `myapp` | dr-myapp | Prefix |
"""
    parsed = parse_review(md)

    assert parsed.parameters == {
        "AmiId": "ami-newvalue",
        "BucketPrefix": "dr-myapp",
    }


def test_parse_review_checkbox_states():
    """Resource checkbox lines populate resource_states dict with bool values."""
    md = """## 🟡 Resources（勾选要部署的资源，默认全选）

- [x] Lambda::Function / Foo
- [ ] S3::Bucket / Bar
- [X] IAM::Role / Baz
"""
    parsed = parse_review(md)

    assert parsed.resource_states == {"Foo": True, "Bar": False, "Baz": True}


def test_parse_review_decision_blocks():
    """Decision blocks produce entries with id, title, body, and fields."""
    md = """## ⚠️ 需人工决策

### D.peering.MyappPeering — 跨账号 Peering: MyappPeering

资源类型 AWS::EC2::VPCPeeringConnection。

```
PeeringConnectionId: pcx-123abc
```

### D.iam-trust.Role1 — 外部账号 999988887777

```
TrustedAccountId: keep
```
"""
    parsed = parse_review(md)
    ids = [d["id"] for d in parsed.decisions]

    assert "D.peering.MyappPeering" in ids
    assert "D.iam-trust.Role1" in ids
    peering = next(d for d in parsed.decisions if d["id"] == "D.peering.MyappPeering")
    assert any("PeeringConnectionId" in f for f in peering["fields"])


# ---------------------------------------------------------------------------
# apply_resource_removal + build_deploy_params
# ---------------------------------------------------------------------------


def test_apply_resource_removal_strips_unchecked_logical_ids():
    """Resources whose state is False are deleted from the template."""
    yaml = YAML()
    template = yaml.load(
        """
        Resources:
          Foo:
            Type: AWS::Lambda::Function
          Bar:
            Type: AWS::S3::Bucket
          Baz:
            Type: AWS::IAM::Role
        """
    )
    removed = apply_resource_removal(
        template, {"Foo": True, "Bar": False, "Baz": False}
    )

    assert removed == ["Bar", "Baz"]
    assert "Foo" in template["Resources"]
    assert "Bar" not in template["Resources"]


def test_build_deploy_params_formats_cfn_overrides():
    """build_deploy_params produces CFN parameter-overrides JSON shape."""
    out = build_deploy_params({"AmiId": "ami-x", "Prefix": "myapp"})
    keys = [p["ParameterKey"] for p in out]
    vals = [p["ParameterValue"] for p in out]

    assert keys == ["AmiId", "Prefix"]
    assert "ami-x" in vals and "myapp" in vals


# ---------------------------------------------------------------------------
# End-to-end apply_review + diff + manual-tasks
# ---------------------------------------------------------------------------


def test_apply_review_end_to_end(tmp_path: Path):
    """apply_review writes cleaned-final.yml + params.json + manual + diff."""
    cleaned = tmp_path / "cleaned.yml"
    cleaned.write_text(
        """Parameters:
  AmiId:
    Type: String
    Default: ''
Resources:
  MyappFn:
    Type: AWS::Lambda::Function
    Properties:
      FunctionName: foo
  MyappBucket:
    Type: AWS::S3::Bucket
    Properties:
      BucketName: foo-bucket
""",
        encoding="utf-8",
    )
    review_md = """# Review

## 🟢 Parameters
| 参数名 | 类型 | 源值/默认 | 目标值 | 说明 |
|--------|------|-----------|--------|------|
| AmiId | String | `` | ami-newami | AMI |

## 🟡 Resources
- [x] Lambda::Function / MyappFn
- [ ] S3::Bucket / MyappBucket

## ⚠️ 需人工决策
_无_

## 🔴 预检失败项
_precheck pass_
"""
    out_final = tmp_path / "cleaned-final.yml"
    out_params = tmp_path / "params.json"
    out_manual = tmp_path / "manual.md"
    diff_path = tmp_path / "review.diff"

    summary = apply_review(
        review_md,
        cleaned,
        out_final,
        out_params,
        out_manual,
        diff_path,
    )

    assert summary["parameters"] == {"AmiId": "ami-newami"}
    assert "MyappBucket" in summary["removed"]
    assert "MyappBucket" not in out_final.read_text()
    params_payload = json.loads(out_params.read_text())
    assert params_payload[0]["ParameterKey"] == "AmiId"
    assert "MyappBucket" in diff_path.read_text()


def test_apply_review_peering_decision_applied_to_template():
    """apply_decisions_to_template injects PeeringConnectionId when admin filled it."""
    yaml = YAML()
    template = yaml.load(
        """
        Resources:
          MyappPeering:
            Type: AWS::EC2::VPCPeeringConnection
            Properties:
              VpcId: vpc-123
        """
    )
    decisions = [
        {
            "id": "D.peering.MyappPeering",
            "title": "Peering",
            "body": [],
            "fields": ["PeeringConnectionId: pcx-target"],
        }
    ]
    applied, manual = apply_decisions_to_template(template, decisions)

    assert len(applied) == 1
    assert template["Resources"]["MyappPeering"]["Properties"]["PeeringConnectionId"] == "pcx-target"
    assert manual == []


def test_apply_review_empty_review_returns_noop(tmp_path: Path):
    """Empty review.md yields empty parameters / no resources removed."""
    cleaned = tmp_path / "cleaned.yml"
    cleaned.write_text(
        "Resources:\n  Foo:\n    Type: AWS::Lambda::Function\n", encoding="utf-8"
    )
    summary = apply_review(
        "",
        cleaned,
        tmp_path / "final.yml",
        tmp_path / "p.json",
        tmp_path / "m.md",
        tmp_path / "d.diff",
    )

    assert summary["parameters"] == {}
    assert summary["removed"] == []


def test_apply_review_raises_on_invalid_yaml(tmp_path: Path):
    """apply_review raises ValueError when cleaned.yml is empty (invalid)."""
    empty = tmp_path / "empty.yml"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(ValueError):
        apply_review(
            "## 🟡 Resources\n- [x] Lambda / Foo\n",
            empty,
            tmp_path / "final.yml",
            tmp_path / "p.json",
            tmp_path / "m.md",
        )


def test_build_manual_tasks_md_empty_vs_full():
    """build_manual_tasks_md degrades gracefully when no manual items exist."""
    assert "无" in build_manual_tasks_md([])

    out = build_manual_tasks_md(
        [
            {
                "id": "D.iam-trust.Role",
                "title": "IAM trust",
                "body": ["Cross-account trust detected"],
                "fields": ["TrustedAccountId: keep"],
            }
        ]
    )
    assert "D.iam-trust.Role" in out
    assert "TrustedAccountId: keep" in out
