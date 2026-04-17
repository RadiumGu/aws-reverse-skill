"""Tests for R15 — region-locked resource review decisions."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from rewrite_cfn import (  # noqa: E402
    dump_yaml,
    load_yaml,
    load_presets,
    resolve_preset_rules,
    rewrite_template,
)


FIXTURES = Path(__file__).parent / "fixtures"


def _run(template, rules=("region_lock",), target_region="ap-northeast-1"):
    decisions: list[dict] = []
    rewrite_template(
        template,
        account_id="111111111111",
        source_region="ap-northeast-1",
        rules=set(rules),
        review_decisions=decisions,
        target_region=target_region,
    )
    return decisions


def test_r15_emits_decision_for_cloudfront_in_non_useast1():
    """With target=ap-northeast-1, R15 emits region-constraint decisions."""
    template = load_yaml(FIXTURES / "sample_cfn_region_locked.yml")
    decisions = _run(template, target_region="ap-northeast-1")
    rc = [d for d in decisions if d.get("kind") == "region-constraint"]
    assert rc, "expected region-constraint decisions for non-us-east-1 target"

    by_resource = {d["resource_logical_id"]: d for d in rc}
    assert "MyDist" in by_resource
    assert "MyCert" in by_resource
    assert "MyCloudFrontWebACL" in by_resource
    # REGIONAL WebACL must NOT show up.
    assert "MyRegionalWebACL" not in by_resource

    cf = by_resource["MyDist"]
    assert cf["required_region"] == "us-east-1"
    assert cf["target_region"] == "ap-northeast-1"
    assert cf["is_violation"] is True
    assert cf["resource_type"] == "AWS::CloudFront::Distribution"
    assert "us-east-1" in cf["suggested_action"]


def test_r15_no_decision_when_target_is_useast1():
    """Target region us-east-1 matches required region → no violation decisions."""
    template = load_yaml(FIXTURES / "sample_cfn_region_locked.yml")
    decisions = _run(template, target_region="us-east-1")
    rc = [d for d in decisions if d.get("kind") == "region-constraint"]
    assert rc == [], f"expected no region-constraint decisions, got {rc}"


def test_r15_respects_preset_inclusion():
    """The cross-account / cross-region / full presets all include region_lock."""
    presets = load_presets()
    assert "region_lock" in resolve_preset_rules("cross-account", presets)
    assert "region_lock" in resolve_preset_rules("cross-region", presets)
    assert "region_lock" in resolve_preset_rules("full", presets)
    # cleanup-only must not include it.
    assert "region_lock" not in resolve_preset_rules("cleanup-only", presets)


def test_r15_does_not_mutate_template():
    """R15 is advisory only — Resources tree remains byte-identical."""
    template = load_yaml(FIXTURES / "sample_cfn_region_locked.yml")
    before = dump_yaml(template)
    template2 = load_yaml(FIXTURES / "sample_cfn_region_locked.yml")
    _run(template2, rules=("region_lock",), target_region="ap-northeast-1")
    after = dump_yaml(template2)
    # Parameters may be absent in both; Resources must not change.
    assert "Resources:" in before and "Resources:" in after
    # Extract Resources section from both dumps and compare.
    import re
    def _resources(s: str) -> str:
        m = re.search(r"(?ms)^Resources:\s*\n(.*)", s)
        return m.group(1) if m else ""
    assert _resources(before) == _resources(after)


def test_r15_disabled_when_rule_not_in_set():
    """When the rule set does not include region_lock, no decisions emit."""
    template = load_yaml(FIXTURES / "sample_cfn_region_locked.yml")
    decisions = _run(
        template,
        rules=("account_id", "region"),
        target_region="ap-northeast-1",
    )
    rc = [d for d in decisions if d.get("kind") == "region-constraint"]
    assert rc == []
