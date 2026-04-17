"""Tests for R14 — JSON-embedded ARN rewrite."""

import json
import sys
from pathlib import Path

from ruamel.yaml.comments import TaggedScalar

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from rewrite_cfn import (  # noqa: E402
    dump_yaml,
    load_yaml,
    rewrite_template,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _run(template, rules=("nested_arn_json",), account="111111111111",
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


def test_r14_step_functions_definition_string_rewritten():
    """DefinitionString JSON is parsed, ARNs rewritten, and serialised back."""
    from ruamel.yaml.comments import CommentedMap  # noqa: WPS433

    template = load_yaml(FIXTURES / "sample_cfn_nested_arn.yml")
    _, decisions = _run(template)

    # Dump Resources only so Parameter descriptions do not pollute the assertion.
    resources_subset = CommentedMap()
    resources_subset["Resources"] = template["Resources"]
    resources_body = dump_yaml(resources_subset)

    # Same-account Lambda ARN literal gone — replaced inside DefinitionString.
    assert (
        "arn:aws:lambda:ap-northeast-1:111111111111:function:local-task"
        not in resources_body
    )
    # Cross-account Lambda ARN gone — replaced by External*Arn placeholder.
    assert (
        "arn:aws:lambda:us-east-1:999999999999:function:external-task"
        not in resources_body
    )
    # The rewritten DefinitionString introduces CFN intrinsics → !Sub wrap.
    assert "${AWS::AccountId}" in resources_body or "${External" in resources_body

    cross_account = [d for d in decisions if d.get("kind") == "cross-account-arn"]
    assert cross_account
    assert any(
        "DefinitionString" in d.get("location", "") for d in cross_account
    )


def test_r14_iam_policy_document_statement_resource_rewritten():
    """IAM::Policy.PolicyDocument.Statement.Resource list ARNs rewritten."""
    template = load_yaml(FIXTURES / "sample_cfn_nested_arn.yml")
    body, decisions = _run(template)

    policy = template["Resources"]["MyappPolicy"]
    doc = policy["Properties"]["PolicyDocument"]
    # doc remains a dict (CommentedMap) since it was dict-form in fixture.
    resources = doc["Statement"][0]["Resource"]
    joined = "\n".join(
        r.value if isinstance(r, TaggedScalar) else str(r) for r in resources
    )
    # Same-account Lambda → ${AWS::AccountId} substitution.
    assert "${AWS::AccountId}" in joined
    # Cross-account Lambda → ${External...} placeholder.
    assert "${External" in joined
    # Literal cross-account ARN must be gone.
    assert "999999999999:function:external-task" not in joined

    # Decision emitted for cross-account ARN.
    iam_cross = [
        d for d in decisions
        if d.get("kind") == "cross-account-arn"
        and "MyappPolicy" in d.get("location", "")
    ]
    assert iam_cross


def test_r14_bucket_policy_principal_aws_rewritten():
    """S3::BucketPolicy.PolicyDocument.Statement.Principal.AWS list rewritten."""
    template = load_yaml(FIXTURES / "sample_cfn_nested_arn.yml")
    body, decisions = _run(template)

    bp = template["Resources"]["MyappBucketPolicy"]
    doc = bp["Properties"]["PolicyDocument"]
    principals = doc["Statement"][0]["Principal"]["AWS"]
    joined = "\n".join(
        p.value if isinstance(p, TaggedScalar) else str(p) for p in principals
    )
    # Same-account IAM role → ${AWS::AccountId}
    assert "${AWS::AccountId}" in joined
    # Cross-account IAM role → External placeholder
    assert "${External" in joined
    assert "999999999999:role/partner-role" not in joined

    bp_cross = [
        d for d in decisions
        if d.get("kind") == "cross-account-arn"
        and "MyappBucketPolicy" in d.get("location", "")
    ]
    assert bp_cross


def test_r14_malformed_json_skipped_gracefully():
    """Malformed DefinitionString JSON does not crash; skipped unchanged."""
    yaml_text = """
AWSTemplateFormatVersion: '2010-09-09'
Resources:
  BadStateMachine:
    Type: AWS::StepFunctions::StateMachine
    Properties:
      StateMachineName: bad
      RoleArn: arn:aws:iam::111111111111:role/x
      DefinitionString: "this is { not valid json"
"""
    from io import StringIO
    from ruamel.yaml import YAML
    yaml = YAML()
    yaml.preserve_quotes = True
    template = yaml.load(StringIO(yaml_text))
    decisions: list[dict] = []
    # Should not raise
    rewrite_template(
        template,
        account_id="111111111111",
        source_region="ap-northeast-1",
        rules={"nested_arn_json"},
        review_decisions=decisions,
    )
    defn = template["Resources"]["BadStateMachine"]["Properties"]["DefinitionString"]
    # DefinitionString preserved unchanged (still a plain string)
    assert "this is { not valid json" in (
        defn.value if isinstance(defn, TaggedScalar) else str(defn)
    )
