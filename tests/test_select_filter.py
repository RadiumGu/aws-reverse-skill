"""Tests for scripts/select.py — apply_filters() pure function."""

import importlib.util
import sys
from pathlib import Path

import pytest

# Load scripts/select.py directly to avoid collision with Python's built-in
# `select` module which would shadow our file when using sys.path tricks.
_SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"

def _load_script(name: str):
    """Load a script module from scripts/ by file path."""
    spec = importlib.util.spec_from_file_location(
        f"_aws_reverse_{name}", _SCRIPTS_DIR / f"{name}.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

_select_mod = _load_script("select")
apply_filters = _select_mod.apply_filters
build_regex_filter = _select_mod.build_regex_filter
is_default_resource = _select_mod.is_default_resource


# ---------------------------------------------------------------------------
# Test 1: Service filter (OR logic)
# ---------------------------------------------------------------------------

def test_service_filter_lambda_only(sample_resources):
    """Filtering by 'Lambda' returns only Lambda functions."""
    result = apply_filters(sample_resources, {"services": ["Lambda"]})

    assert len(result) == 3
    assert all(r["Type"] == "AWS::Lambda::Function" for r in result)


def test_service_filter_multi_service(sample_resources):
    """Filtering by Lambda AND IAM returns resources from both services."""
    result = apply_filters(sample_resources, {"services": ["Lambda", "IAM"]})

    types = {r["Type"] for r in result}
    assert "AWS::Lambda::Function" in types
    assert "AWS::IAM::Role" in types
    # No S3 or RDS etc.
    assert all(
        r["Type"].startswith("AWS::Lambda::") or r["Type"].startswith("AWS::IAM::")
        for r in result
    )


def test_service_filter_no_match(sample_resources):
    """Filtering by a service not present returns an empty list."""
    result = apply_filters(sample_resources, {"services": ["EKS"]})
    assert result == []


# ---------------------------------------------------------------------------
# Test 2: Tag filter (AND logic)
# ---------------------------------------------------------------------------

def test_tag_filter_single_tag(sample_resources):
    """Filtering by Environment=prod returns only prod-tagged resources."""
    result = apply_filters(sample_resources, {"tags": ["Environment=prod"]})

    assert len(result) > 0
    assert all(r["Tags"].get("Environment") == "prod" for r in result)
    # The dev Lambda should NOT be in the result
    physical_ids = [r["PhysicalId"] for r in result]
    assert "test-function" not in physical_ids


def test_tag_filter_multi_tag_and(sample_resources):
    """Multiple tag filters apply as AND — resource must match all tags."""
    result = apply_filters(
        sample_resources,
        {"tags": ["Environment=prod", "Project=myapp"]},
    )

    assert len(result) > 0
    for r in result:
        assert r["Tags"].get("Environment") == "prod"
        assert r["Tags"].get("Project") == "myapp"
    # The logs-bucket has Environment=prod but no Project=myapp tag
    physical_ids = [r["PhysicalId"] for r in result]
    assert "logs-bucket" not in physical_ids


def test_tag_filter_no_match(sample_resources):
    """A tag filter that matches nothing returns an empty list."""
    result = apply_filters(sample_resources, {"tags": ["Environment=staging"]})
    assert result == []


# ---------------------------------------------------------------------------
# Test 3: Regex filter on PhysicalId
# ---------------------------------------------------------------------------

def test_regex_filter_prefix(sample_resources):
    """Regex '^myapp-' matches all resources whose ID starts with 'myapp-'."""
    result = apply_filters(sample_resources, {"regex": "^myapp-"})

    assert len(result) > 0
    assert all(r["PhysicalId"].startswith("myapp-") for r in result)
    # test-function should NOT appear
    physical_ids = [r["PhysicalId"] for r in result]
    assert "test-function" not in physical_ids


def test_regex_filter_no_match(sample_resources):
    """A regex that matches nothing returns an empty list."""
    result = apply_filters(sample_resources, {"regex": "^zzz-nonexistent-"})
    assert result == []


def test_regex_filter_partial_match(sample_resources):
    """Regex can match a substring of the PhysicalId."""
    result = apply_filters(sample_resources, {"regex": "scheduler"})

    assert len(result) == 1
    assert result[0]["PhysicalId"] == "myapp-scheduler"


# ---------------------------------------------------------------------------
# Test 4: exclude_default flag
# ---------------------------------------------------------------------------

def test_exclude_default_removes_default_vpc(sample_resources):
    """exclude_default=True removes the default VPC."""
    result = apply_filters(sample_resources, {"exclude_default": True})

    physical_ids = [r["PhysicalId"] for r in result]
    assert "vpc-default0001" not in physical_ids


def test_exclude_default_removes_default_sg(sample_resources):
    """exclude_default=True removes the default Security Group."""
    result = apply_filters(sample_resources, {"exclude_default": True})

    physical_ids = [r["PhysicalId"] for r in result]
    assert "sg-default0001" not in physical_ids


def test_exclude_default_removes_default_nacl(sample_resources):
    """exclude_default=True removes the default NACL."""
    result = apply_filters(sample_resources, {"exclude_default": True})

    physical_ids = [r["PhysicalId"] for r in result]
    assert "acl-default0001" not in physical_ids


# ---------------------------------------------------------------------------
# Test 5: build_regex_filter helper
# ---------------------------------------------------------------------------

def test_build_regex_filter_escapes_dots():
    """build_regex_filter escapes dots in physical IDs."""
    result = build_regex_filter(["a.b.c", "foo"])
    assert r"a\.b\.c" in result
    assert "foo" in result


def test_build_regex_filter_empty():
    """build_regex_filter returns empty string for no IDs."""
    assert build_regex_filter([]) == ""


# ---------------------------------------------------------------------------
# Test 6: Combined filters
# ---------------------------------------------------------------------------

def test_combined_service_and_tag_filter(sample_resources):
    """Combining service + tag filters returns only matching resources."""
    result = apply_filters(
        sample_resources,
        {"services": ["Lambda"], "tags": ["Environment=prod"]},
    )

    assert len(result) == 2  # myapp-processor and myapp-scheduler
    assert all(r["Type"] == "AWS::Lambda::Function" for r in result)
    assert all(r["Tags"].get("Environment") == "prod" for r in result)


def test_no_filters_returns_all(sample_resources):
    """Passing no filters (all None/empty) returns all resources unchanged."""
    result = apply_filters(sample_resources, {})
    assert len(result) == len(sample_resources)
