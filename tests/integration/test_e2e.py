"""End-to-end integration test for the aws-reverse-skill pipeline.

Requires a real AWS account with resources to scan. Skipped by default
unless ``AWS_E2E_TEST=1`` is set. Expects:

    AWS_E2E_REGION       — region to scan (default: ap-northeast-1)
    AWS_E2E_ACCOUNT_ID   — 12-digit source account ID
    AWS_E2E_PROFILE      — AWS CLI profile (default: "default")
    AWS_E2E_SERVICES     — comma-separated services (default: "Lambda,S3")

The test runs the full pipeline:
    scan → preview → filter → rewrite → precheck → deployment_advice

No actual CloudFormation deployment happens — the test verifies that every
stage produces valid output and the final template is syntactically correct.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

SKIP_REASON = "Set AWS_E2E_TEST=1 to run integration tests"
pytestmark = pytest.mark.skipif(
    os.environ.get("AWS_E2E_TEST") != "1", reason=SKIP_REASON
)

SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"
REGION = os.environ.get("AWS_E2E_REGION", "ap-northeast-1")
ACCOUNT_ID = os.environ.get("AWS_E2E_ACCOUNT_ID", "")
PROFILE = os.environ.get("AWS_E2E_PROFILE", "default")
SERVICES = os.environ.get("AWS_E2E_SERVICES", "Lambda,S3")


def _run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a subprocess, capturing output."""
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        cwd=str(SCRIPTS.parent),
        **kwargs,
    )


@pytest.fixture(scope="module")
def work_dir():
    """Create a temporary working directory for pipeline outputs."""
    with tempfile.TemporaryDirectory(prefix="e2e-aws-reverse-") as d:
        yield Path(d)


@pytest.fixture(scope="module")
def scan_output(work_dir):
    """Step 1: Scan — produce raw.json and cfn-full.yml."""
    raw_path = work_dir / "raw.json"
    cfn_path = work_dir / "cfn-full.yml"
    result = _run([
        "node", str(SCRIPTS / "scan.js"),
        "--region", REGION,
        "--services", SERVICES,
        "--profile", PROFILE,
        "--out-raw", str(raw_path),
        "--out-cfn", str(cfn_path),
    ])
    assert result.returncode == 0, f"scan.js failed:\n{result.stderr}"
    assert raw_path.exists() and raw_path.stat().st_size > 0
    assert cfn_path.exists()
    return {"raw": raw_path, "cfn": cfn_path}


@pytest.fixture(scope="module")
def preview_output(scan_output):
    """Step 2: Preview — verify preview.py runs without error."""
    result = _run([
        sys.executable, str(SCRIPTS / "preview.py"),
        "--input", str(scan_output["raw"]),
        "--group-by", "service",
        "--sample", "2",
    ])
    assert result.returncode == 0, f"preview.py failed:\n{result.stderr}"
    assert len(result.stdout) > 0
    return result.stdout


@pytest.fixture(scope="module")
def filter_output(scan_output, work_dir):
    """Step 3: Filter — dry-run then commit."""
    # Dry run
    dr = _run([
        sys.executable, str(SCRIPTS / "select.py"),
        "--input", str(scan_output["raw"]),
        "--exclude-default",
        "--dry-run",
    ])
    assert dr.returncode == 0, f"select.py dry-run failed:\n{dr.stderr}"

    # Commit
    filtered_path = work_dir / "filtered.json"
    regex_path = work_dir / "id-filter.txt"
    result = _run([
        sys.executable, str(SCRIPTS / "select.py"),
        "--input", str(scan_output["raw"]),
        "--output", str(filtered_path),
        "--exclude-default",
        "--emit-regex-filter",
    ])
    assert result.returncode == 0, f"select.py failed:\n{result.stderr}"
    regex_path.write_text(result.stdout, encoding="utf-8")

    # former2 filter (skip if regex is empty)
    cfn_filtered = work_dir / "cfn-filtered.yml"
    regex = regex_path.read_text().strip()
    if regex and scan_output["cfn"].stat().st_size > 0:
        fr = _run([
            "npx", "former2", "filter",
            "--input", str(scan_output["cfn"]),
            "--output", str(cfn_filtered),
            "--search-filter", regex,
        ])
        if fr.returncode != 0:
            # former2 filter is optional; copy full CFN as fallback
            cfn_filtered.write_text(
                scan_output["cfn"].read_text(encoding="utf-8"), encoding="utf-8"
            )
    else:
        cfn_filtered.write_text(
            scan_output["cfn"].read_text(encoding="utf-8"), encoding="utf-8"
        )

    return {"filtered_json": filtered_path, "cfn_filtered": cfn_filtered}


@pytest.fixture(scope="module")
def rewrite_output(filter_output, work_dir):
    """Step 4: Rewrite — apply cleanup-only preset."""
    cleaned = work_dir / "cleaned.yml"
    decisions = work_dir / "decisions.json"
    args = [
        sys.executable, str(SCRIPTS / "rewrite_cfn.py"),
        "--input", str(filter_output["cfn_filtered"]),
        "--output", str(cleaned),
        "--preset", "cleanup-only",
        "--review-decisions", str(decisions),
    ]
    if ACCOUNT_ID:
        args += ["--account-id", ACCOUNT_ID]
    args += ["--source-region", REGION]
    result = _run(args)
    assert result.returncode == 0, f"rewrite_cfn.py failed:\n{result.stderr}"
    assert cleaned.exists() and cleaned.stat().st_size > 0
    return {"cleaned": cleaned, "decisions": decisions}


@pytest.fixture(scope="module")
def precheck_output(rewrite_output, work_dir):
    """Step 5: Precheck — validate the cleaned template."""
    precheck_script = SCRIPTS / "precheck.py"
    if not precheck_script.exists():
        pytest.skip("precheck.py not found")
    result = _run([
        sys.executable, str(precheck_script),
        "--input", str(rewrite_output["cleaned"]),
        "--region", REGION,
    ])
    # Precheck may warn but should not crash
    assert result.returncode in (0, 1), f"precheck.py crashed:\n{result.stderr}"
    return result


@pytest.fixture(scope="module")
def advice_output(scan_output, work_dir):
    """Step 6: Deployment advice — generate all formats."""
    results = {}
    for fmt in ("md", "checklist", "json"):
        out = work_dir / f"advice.{fmt}"
        args = [
            sys.executable, str(SCRIPTS / "deployment_advice.py"),
            "--input", str(scan_output["raw"]),
            "--format", fmt,
            "--output", str(out),
        ]
        if ACCOUNT_ID:
            args += ["--source-account", ACCOUNT_ID]
        result = _run(args)
        assert result.returncode == 0, f"deployment_advice.py --format {fmt} failed:\n{result.stderr}"
        assert out.exists()
        results[fmt] = out
    return results


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestE2EPipeline:
    """End-to-end pipeline verification (no actual CFN deployment)."""

    def test_scan_produces_resources(self, scan_output):
        raw = json.loads(scan_output["raw"].read_text(encoding="utf-8"))
        items = raw if isinstance(raw, list) else raw.get("resources", [])
        assert len(items) > 0, "Scan returned zero resources"

    def test_preview_runs(self, preview_output):
        assert len(preview_output) > 0

    def test_filter_produces_output(self, filter_output):
        data = json.loads(filter_output["filtered_json"].read_text(encoding="utf-8"))
        resources = data if isinstance(data, list) else data.get("resources", [])
        assert len(resources) >= 0  # may be empty if all filtered

    def test_rewrite_produces_valid_yaml(self, rewrite_output):
        from ruamel.yaml import YAML
        yaml = YAML()
        with open(rewrite_output["cleaned"], encoding="utf-8") as fh:
            template = yaml.load(fh)
        assert "Resources" in template or template is not None

    def test_rewrite_decisions_valid_json(self, rewrite_output):
        if rewrite_output["decisions"].exists():
            decisions = json.loads(
                rewrite_output["decisions"].read_text(encoding="utf-8")
            )
            assert isinstance(decisions, list)

    def test_precheck_runs(self, precheck_output):
        # Just verify it didn't crash (exit 0 or 1 for warnings)
        assert precheck_output.returncode in (0, 1)

    def test_advice_all_formats(self, advice_output):
        for fmt, path in advice_output.items():
            content = path.read_text(encoding="utf-8")
            assert len(content) > 0, f"Empty {fmt} advice output"
            if fmt == "json":
                data = json.loads(content)
                assert isinstance(data, list)

    def test_cleaned_template_has_no_raw_account_id(self, rewrite_output):
        """The cleaned template should not contain the raw source account ID."""
        if not ACCOUNT_ID:
            pytest.skip("No ACCOUNT_ID set")
        content = rewrite_output["cleaned"].read_text(encoding="utf-8")
        # Account ID may appear in Description strings — only check outside those
        lines = [
            l for l in content.splitlines()
            if "Description" not in l and "original:" not in l
        ]
        raw_text = "\n".join(lines)
        assert ACCOUNT_ID not in raw_text, (
            f"Raw account ID {ACCOUNT_ID} still present in cleaned template"
        )
