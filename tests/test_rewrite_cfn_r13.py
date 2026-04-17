"""Tests for R13 — recursive ARN rewrite across every string scalar."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from rewrite_cfn import (  # noqa: E402
    dump_yaml,
    load_yaml,
    rewrite_template,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _run(template, rules=("nested_arn_string",), account="111111111111",
         source_region="ap-northeast-1"):
    decisions: list[dict] = []
    rewrite_template(
        template,
        account_id=account,
        source_region=source_region,
        rules=set(rules),
        review_decisions=decisions,
    )
    return dump_yaml(template), decisions


def test_r13_same_account_same_region_uses_sub():
    """Same-account/same-region ARN scalars become !Sub with AWS pseudo params."""
    template = load_yaml(FIXTURES / "sample_cfn_nested_arn.yml")
    body, decisions = _run(template)

    # The Lambda.Role scalar (arn:aws:iam::111111111111:role/lambda-role) should
    # be wrapped in !Sub with ${AWS::AccountId} (IAM is region-less).
    assert "${AWS::AccountId}" in body
    # Original same-account account id must no longer appear as a bare literal
    # in the rewritten Lambda.Role ARN.
    assert "arn:aws:iam::111111111111:role/lambda-role" not in body
    # No cross-account decision for same-account.
    same_account_decisions = [
        d for d in decisions
        if d.get("kind") == "cross-account-arn" and "111111111111" in d.get("arn", "")
    ]
    assert same_account_decisions == []


def test_r13_cross_account_creates_parameter():
    """Cross-account ARNs become ${ExternalXxxArn...} and surface a Parameter."""
    template = load_yaml(FIXTURES / "sample_cfn_nested_arn.yml")
    body, decisions = _run(template)

    params = template.get("Parameters", {}) or {}
    external = [p for p in params if p.startswith("External") and p.endswith(
        ""  # trailing 8-hex hash — just verify External*Arn prefix
    ) and "Arn" in p]
    assert external, f"expected External*Arn parameter, got {list(params.keys())}"

    cross_account_decisions = [
        d for d in decisions if d.get("kind") == "cross-account-arn"
    ]
    assert cross_account_decisions
    arns = {d["arn"] for d in cross_account_decisions}
    # Partner SQS queue ARN is cross-account
    assert any("999999999999:partner-queue" in a for a in arns)

    # The rewritten body should reference the parameter (via ${ExternalXxxArn}).
    assert "${External" in body


def test_r13_lambda_env_vars_rewritten():
    """Lambda Environment.Variables.* ARN scalars are rewritten."""
    template = load_yaml(FIXTURES / "sample_cfn_nested_arn.yml")
    body, decisions = _run(template)

    # Same-account DB_TABLE_ARN → !Sub with pseudo params; original literal gone.
    assert (
        "arn:aws:dynamodb:ap-northeast-1:111111111111:table/foo" not in body
    )
    assert "${AWS::Region}" in body or "${AWS::AccountId}" in body

    # Cross-account PARTNER_QUEUE_ARN → external parameter emits decision.
    partner_decisions = [
        d for d in decisions
        if d.get("kind") == "cross-account-arn"
        and "partner-queue" in d.get("arn", "")
    ]
    assert partner_decisions
    d = partner_decisions[0]
    assert "Environment" in d["location"] or "Variables" in d["location"]


def test_r13_eventbridge_target_arn_rewritten():
    """EventBridge Rule.Targets.Arn / RoleArn are rewritten by R13."""
    template = load_yaml(FIXTURES / "sample_cfn_nested_arn.yml")
    body, decisions = _run(template)

    # Same-account Target.Arn → !Sub with pseudo params (literal gone).
    assert (
        "arn:aws:lambda:ap-northeast-1:111111111111:function:another-local"
        not in body
    )
    # Still references the Lambda as a !Sub expression containing pseudo params.
    assert "function:another-local" in body
    assert "${AWS::AccountId}" in body
    assert "${AWS::Region}" in body
