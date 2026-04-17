"""conftest.py — Shared pytest fixtures for aws-reverse-skill tests."""

import json
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture()
def sample_resources() -> list[dict]:
    """Load the sample_raw.json fixture and return the resources list."""
    with (FIXTURES_DIR / "sample_raw.json").open(encoding="utf-8") as fh:
        data = json.load(fh)
    return data["resources"]


@pytest.fixture()
def sample_cfn_dirty_path() -> Path:
    """Return the path to the dirty CFN fixture."""
    return FIXTURES_DIR / "sample_cfn_dirty.yml"
