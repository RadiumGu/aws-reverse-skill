"""Tests for scripts/arn_rewriter.py — find_arns / rewrite_arn / rewrite_arns_in_text."""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from arn_rewriter import (  # noqa: E402
    find_arns,
    rewrite_arn,
    rewrite_arns_in_text,
)

PARAM_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")


def test_find_arns_single_in_string():
    text = "Invoke arn:aws:lambda:us-east-1:111111111111:function:foo when ready."
    results = find_arns(text)
    assert len(results) == 1
    arn = results[0]
    assert arn["arn"] == "arn:aws:lambda:us-east-1:111111111111:function:foo"
    assert arn["partition"] == "aws"
    assert arn["service"] == "lambda"
    assert arn["region"] == "us-east-1"
    assert arn["account"] == "111111111111"
    assert arn["resource"] == "function:foo"
    assert arn["is_known_service"] is True
    assert text[arn["start"] : arn["end"]] == arn["arn"]


def test_find_arns_multiple_in_string():
    text = (
        "first=arn:aws:lambda:us-east-1:111111111111:function:foo "
        "second=arn:aws:s3:::my-bucket/key"
    )
    results = find_arns(text)
    assert len(results) == 2
    services = [r["service"] for r in results]
    assert services == ["lambda", "s3"]
    # Global s3 ARN → empty region + empty account.
    assert results[1]["region"] == ""
    assert results[1]["account"] == ""
    assert results[1]["resource"] == "my-bucket/key"


def test_find_arns_ignores_unknown_service():
    """ARNs with a non-whitelisted service are still returned, but flagged."""
    text = "arn:aws:notaservice:us-east-1:111111111111:thing/abc"
    results = find_arns(text)
    assert len(results) == 1
    assert results[0]["service"] == "notaservice"
    assert results[0]["is_known_service"] is False


def test_find_arns_handles_china_partition():
    text = "arn:aws-cn:s3:::my-cn-bucket/data"
    results = find_arns(text)
    assert len(results) == 1
    assert results[0]["partition"] == "aws-cn"
    assert results[0]["service"] == "s3"
    assert results[0]["resource"] == "my-cn-bucket/data"


def test_find_arns_handles_govcloud_partition():
    text = "arn:aws-us-gov:lambda:us-gov-west-1:222222222222:function:bar"
    results = find_arns(text)
    assert len(results) == 1
    assert results[0]["partition"] == "aws-us-gov"
    assert results[0]["account"] == "222222222222"
    assert results[0]["region"] == "us-gov-west-1"


def test_rewrite_arn_same_account_same_region_sub_both():
    result = rewrite_arn(
        "arn:aws:lambda:us-east-1:111111111111:function:foo",
        source_account="111111111111",
        source_region="us-east-1",
    )
    assert result["action"] == "sub-both"
    assert "${AWS::AccountId}" in result["new_arn"]
    assert "${AWS::Region}" in result["new_arn"]
    assert result["parameter_name"] is None


def test_rewrite_arn_same_account_different_region_sub_account_only():
    result = rewrite_arn(
        "arn:aws:lambda:eu-west-1:111111111111:function:foo",
        source_account="111111111111",
        source_region="us-east-1",
    )
    assert result["action"] == "sub-account"
    assert "${AWS::AccountId}" in result["new_arn"]
    assert "${AWS::Region}" not in result["new_arn"]
    assert "eu-west-1" in result["new_arn"]


def test_rewrite_arn_different_account_uses_parameter():
    result = rewrite_arn(
        "arn:aws:lambda:us-east-1:999888777666:function:ext",
        source_account="111111111111",
        source_region="us-east-1",
    )
    assert result["action"] == "parameter"
    assert result["parameter_name"] is not None
    assert PARAM_NAME_RE.match(result["parameter_name"])
    assert result["new_arn"] == "${" + result["parameter_name"] + "}"


def test_rewrite_arn_global_arn_no_region_sub():
    # Global IAM ARN (empty region) owned by source account → sub-account only.
    result = rewrite_arn(
        "arn:aws:iam::111111111111:role/MyRole",
        source_account="111111111111",
        source_region="us-east-1",
    )
    assert result["action"] == "sub-account"
    assert "${AWS::Region}" not in result["new_arn"]
    assert "${AWS::AccountId}" in result["new_arn"]
    # Region segment should remain empty between the two colons.
    assert "arn:aws:iam::${AWS::AccountId}:role/MyRole" == result["new_arn"]


def test_rewrite_arns_in_text_returns_parameters_needed():
    text = (
        "source=arn:aws:lambda:us-east-1:111111111111:function:foo "
        "external=arn:aws:sns:us-east-1:999888777666:topic:alerts "
        "unknown=arn:aws:notaservice:us-east-1:111111111111:thing/abc"
    )
    result = rewrite_arns_in_text(
        text,
        source_account="111111111111",
        source_region="us-east-1",
    )
    actions = [r["action"] for r in result["rewrites"]]
    assert actions == ["sub-both", "parameter", "unchanged"]
    # One external-account ARN → exactly one Parameter needed.
    assert len(result["parameters_needed"]) == 1
    only_param = next(iter(result["parameters_needed"]))
    assert only_param.startswith("External")
    assert PARAM_NAME_RE.match(only_param)
    # Rewritten text contains the Sub placeholder and Parameter reference.
    assert "${AWS::AccountId}" in result["new_text"]
    assert "${" + only_param + "}" in result["new_text"]
    # Unknown-service ARN must still appear verbatim.
    assert "arn:aws:notaservice:us-east-1:111111111111:thing/abc" in result["new_text"]


def test_rewrite_arn_deterministic_parameter_name():
    arn = "arn:aws:sns:us-east-1:999888777666:topic:alerts"
    first = rewrite_arn(arn, source_account="111111111111", source_region="us-east-1")
    second = rewrite_arn(arn, source_account="111111111111", source_region="us-east-1")
    assert first["parameter_name"] == second["parameter_name"]
    # Different external ARN → different parameter name.
    other = rewrite_arn(
        "arn:aws:sns:us-east-1:999888777666:topic:other",
        source_account="111111111111",
        source_region="us-east-1",
    )
    assert other["parameter_name"] != first["parameter_name"]


def test_rewrite_arn_unknown_service_unchanged():
    arn = "arn:aws:notaservice:us-east-1:111111111111:thing/abc"
    result = rewrite_arn(arn, source_account="111111111111", source_region="us-east-1")
    assert result["action"] == "unchanged"
    assert result["new_arn"] == arn
    assert result["parameter_name"] is None


def test_rewrite_arn_source_region_different_account():
    """Same region, different account → sub-region path."""
    result = rewrite_arn(
        "arn:aws:sns:us-east-1:222222222222:topic:foo",
        source_account="111111111111",
        source_region="us-east-1",
    )
    # Different account — parameter path wins over region-only.
    assert result["action"] == "parameter"
    # When account is empty (not external), region-only path activates.
    result2 = rewrite_arn(
        "arn:aws:s3:us-east-1::accesspoint/foo",
        source_account="111111111111",
        source_region="us-east-1",
    )
    assert result2["action"] == "sub-region"
    assert "${AWS::Region}" in result2["new_arn"]
    assert "${AWS::AccountId}" not in result2["new_arn"]
