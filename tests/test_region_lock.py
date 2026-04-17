"""Tests for region_lock module — region-locked resource detection."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from region_lock import (  # noqa: E402
    REGION_LOCKED_RESOURCES,
    find_region_locked,
)
from rewrite_cfn import load_yaml  # noqa: E402


FIXTURES = Path(__file__).parent / "fixtures"


def _load() -> dict:
    return load_yaml(FIXTURES / "sample_cfn_region_locked.yml")


def test_cloudfront_distribution_detected():
    """CloudFront::Distribution is always flagged when target != us-east-1."""
    template = _load()
    results = find_region_locked(template, "ap-northeast-1")
    cf = [r for r in results if r["resource_type"] == "AWS::CloudFront::Distribution"]
    assert len(cf) == 1
    entry = cf[0]
    assert entry["resource_logical_id"] == "MyDist"
    assert entry["required_region"] == "us-east-1"
    assert entry["target_region"] == "ap-northeast-1"
    assert entry["is_violation"] is True
    assert (
        "DistributionConfig.ViewerCertificate.AcmCertificateArn"
        in entry["affected_properties"]
    )


def test_wafv2_cloudfront_scope_detected():
    """WAFv2::WebACL with Scope=CLOUDFRONT is flagged."""
    template = _load()
    results = find_region_locked(template, "ap-northeast-1")
    waf = [
        r
        for r in results
        if r["resource_type"] == "AWS::WAFv2::WebACL"
        and r["resource_logical_id"] == "MyCloudFrontWebACL"
    ]
    assert len(waf) == 1
    assert waf[0]["required_region"] == "us-east-1"
    assert waf[0]["is_violation"] is True


def test_wafv2_regional_scope_not_detected():
    """WAFv2::WebACL with Scope=REGIONAL must NOT be flagged."""
    template = _load()
    results = find_region_locked(template, "ap-northeast-1")
    assert not any(
        r["resource_logical_id"] == "MyRegionalWebACL" for r in results
    ), "REGIONAL-scope WebACL should not appear in region-lock results"


def test_acm_cert_used_by_cloudfront_detected():
    """ACM certs referenced (via !Ref) by a CloudFront Distribution are flagged."""
    template = _load()
    results = find_region_locked(template, "ap-northeast-1")
    certs = [
        r
        for r in results
        if r["resource_type"] == "AWS::CertificateManager::Certificate"
    ]
    assert len(certs) == 1
    assert certs[0]["resource_logical_id"] == "MyCert"
    assert certs[0]["is_violation"] is True


def test_non_region_locked_resource_ignored():
    """Regular Lambda / unrelated types must not appear in the decisions."""
    template = _load()
    results = find_region_locked(template, "ap-northeast-1")
    logical_ids = {r["resource_logical_id"] for r in results}
    assert "MyRegularFunction" not in logical_ids
    assert "MyRegionalWebACL" not in logical_ids


def test_no_violation_when_target_matches_required_region():
    """When target == us-east-1, entries are emitted but flagged is_violation=False."""
    template = _load()
    results = find_region_locked(template, "us-east-1")
    assert results, "expected region-locked entries even when target matches"
    assert all(r["is_violation"] is False for r in results)


def test_edge_lambda_detected_via_description():
    """Lambda@Edge detection keys off FunctionName / Description heuristics."""
    template = _load()
    results = find_region_locked(template, "ap-northeast-1")
    edge = [
        r
        for r in results
        if r["resource_type"] == "AWS::Lambda::Function"
        and r["resource_logical_id"] == "MyEdgeFunction"
    ]
    assert len(edge) == 1
    assert edge[0]["required_region"] == "us-east-1"


def test_registry_includes_all_four_types():
    """REGION_LOCKED_RESOURCES exposes the four expected entries."""
    assert "AWS::CloudFront::Distribution" in REGION_LOCKED_RESOURCES
    assert "AWS::WAFv2::WebACL" in REGION_LOCKED_RESOURCES
    assert "AWS::CertificateManager::Certificate" in REGION_LOCKED_RESOURCES
    assert "AWS::Lambda::Function" in REGION_LOCKED_RESOURCES
    for meta in REGION_LOCKED_RESOURCES.values():
        assert meta["required_region"] == "us-east-1"
        assert meta.get("note")


def test_find_region_locked_empty_template():
    """An empty or malformed template returns []."""
    assert find_region_locked({}, "ap-northeast-1") == []
    assert find_region_locked({"Resources": None}, "ap-northeast-1") == []
    assert find_region_locked({"Resources": {}}, "ap-northeast-1") == []
