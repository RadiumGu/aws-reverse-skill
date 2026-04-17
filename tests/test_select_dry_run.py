"""Tests for select.py summarize_filtered — backs the --dry-run CLI path."""

import importlib.util
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"_aws_reverse_{name}", _SCRIPTS_DIR / f"{name}.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_select = _load("select")
summarize_filtered = _select.summarize_filtered
apply_filters = _select.apply_filters


def test_summarize_reports_before_after_counts(sample_resources):
    """Summary reports both total_before and total_after with by_service counts."""
    filtered = apply_filters(sample_resources, {"services": ["Lambda"]})
    summary = summarize_filtered(filtered, sample_resources)

    assert summary["total_before"] == len(sample_resources)
    assert summary["total_after"] == len(filtered)
    assert summary["by_service"] == {"Lambda": 3}
    assert len(summary["sample"]) <= 10


def test_summarize_empty_filter_result(sample_resources):
    """When filter yields 0 resources, summary still returns proper structure."""
    filtered = apply_filters(sample_resources, {"services": ["EKS"]})
    summary = summarize_filtered(filtered, sample_resources)

    assert summary["total_after"] == 0
    assert summary["by_service"] == {}
    assert summary["sample"] == []
