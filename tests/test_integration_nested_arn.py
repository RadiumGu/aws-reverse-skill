"""Integration tests for Hotfix B Wave B3 — end-to-end nested-ARN flow.

Drives the full pipeline from the nested-ARN fixture through ``rewrite_cfn`` →
``generate_review`` and the new cross-account section in ``deployment_advice``.
"""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from deployment_advice import generate_advice, render_markdown  # noqa: E402
from generate_review import build_review_markdown  # noqa: E402
from rewrite_cfn import (  # noqa: E402
    dump_yaml,
    load_yaml,
    rewrite_template,
)

FIXTURES = Path(__file__).parent / "fixtures"
REPO_ROOT = Path(__file__).parent.parent


def _rewrite_cross_account_preset(
    template, decisions: list[dict]
) -> None:
    """Apply the cross-account preset rules to *template* in place."""
    rewrite_template(
        template,
        account_id="111111111111",
        source_region="ap-northeast-1",
        rules={
            "account_id", "region", "ami_id", "az", "deletion_policy",
            "kms", "iam_principal", "s3_bucket_name", "peering",
            "vpc_resource_ids", "sg_cidr",
            "nested_arn_string", "nested_arn_json",
        },
        review_decisions=decisions,
    )


def test_e2e_step_functions_same_and_cross_account(tmp_path: Path) -> None:
    """Cross-account preset on nested_arn fixture → same-acct !Sub + external Parameters."""
    template = load_yaml(FIXTURES / "sample_cfn_nested_arn.yml")
    decisions: list[dict] = []
    _rewrite_cross_account_preset(template, decisions)

    body = dump_yaml(template)

    # Same-account ARNs become !Sub expressions referencing CFN pseudo params.
    assert "${AWS::AccountId}" in body
    # Cross-account ARNs surface as External*Arn Parameters.
    params = template.get("Parameters") or {}
    external = [p for p in params if p.startswith("External") and "Arn" in p]
    assert external, f"expected External*Arn parameter, got {list(params.keys())}"

    # Literal cross-account ARNs must be gone from the Resources body.
    from ruamel.yaml.comments import CommentedMap

    resources_only = CommentedMap()
    resources_only["Resources"] = template["Resources"]
    resources_body = dump_yaml(resources_only)
    assert (
        "arn:aws:lambda:us-east-1:999999999999:function:external-task"
        not in resources_body
    )
    assert "arn:aws:sqs:us-east-1:999999999999:partner-queue" not in resources_body

    # Decisions JSON has at least 2 cross-account-arn entries covering the two
    # distinct external resources (Lambda + SQS). The fixture contains one more
    # cross-account IAM role reference, so the real count is ≥ 3.
    cross = [d for d in decisions if d.get("kind") == "cross-account-arn"]
    assert len(cross) >= 2
    arns = {d["arn"] for d in cross}
    assert any("999999999999:function:external-task" in a for a in arns)
    assert any("999999999999:partner-queue" in a for a in arns)


def test_e2e_review_cross_account_arn_section(tmp_path: Path) -> None:
    """generate_review consumes cross-account decisions → review.md contains the section."""
    template = load_yaml(FIXTURES / "sample_cfn_nested_arn.yml")
    decisions: list[dict] = []
    _rewrite_cross_account_preset(template, decisions)

    review_md = build_review_markdown(
        template,
        raw_resources=[],
        external_decisions=decisions,
        stack_name="integration-nested-arn",
        preset="cross-account",
        source_account="111111111111",
    )

    # The dedicated section header emitted by generate_review for R13/R14.
    assert "跨账号 ARN 引用" in review_md
    # Each cross-account ARN renders a Parameter row with a **<请填>** target.
    assert "ExternalLambdaArn" in review_md or "ExternalSqsArn" in review_md
    assert "**<请填>**" in review_md

    review_path = tmp_path / "review.md"
    review_path.write_text(review_md, encoding="utf-8")
    assert "跨账号 ARN 引用" in review_path.read_text(encoding="utf-8")


def test_e2e_deployment_advice_cross_account(tmp_path: Path) -> None:
    """Raw scan with Step Functions + Lambda referring to external account → md shows cross-account section."""
    raw_path = FIXTURES / "sample_raw_cross_account.json"
    raw = json.loads(raw_path.read_text(encoding="utf-8"))

    # Programmatic path first.
    advice = generate_advice(raw["resources"], source_account="111111111111")
    titles = " ".join(a.title for a in advice)
    assert "Step Functions 跨账号引用" in titles
    assert "Lambda Env cross-account ARN" in titles

    md = render_markdown(advice, stack_name="cross-account-integration")
    assert "跨账号集成" in md
    assert "arn:aws:lambda:us-east-1:999999999999:function:external-task" in md
    assert "arn:aws:sqs:us-east-1:999999999999:partner-queue" in md

    # CLI subprocess call — exercises the real --format md path.
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "deployment_advice.py"),
            "--input", str(raw_path),
            "--format", "md",
            "--source-account", "111111111111",
        ],
        capture_output=True, text=True, check=True,
    )
    assert "跨账号集成" in result.stdout
    assert "Step Functions 跨账号引用" in result.stdout
    assert "Lambda Env cross-account ARN" in result.stdout
