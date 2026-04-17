"""Tests for scripts/fetch-export.sh — arg validation + dry-run rendering."""

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "fetch-export.sh"


def _run(args):
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
    )


def test_missing_required_args_exits_nonzero():
    result = _run([])
    assert result.returncode != 0
    assert "required" in (result.stderr + result.stdout).lower()


def test_help_exits_zero():
    result = _run(["--help"])
    assert result.returncode == 0
    assert "--instance-id" in result.stdout


def test_dry_run_prints_expected_aws_commands():
    result = _run([
        "--instance-id", "i-abc",
        "--region", "us-east-1",
        "--dry-run",
    ])
    assert result.returncode == 0, result.stderr
    combined = result.stdout + result.stderr
    assert "aws sts get-caller-identity" in combined
    assert "aws s3 mb" in combined
    assert "aws ssm send-command" in combined
    assert "aws s3 cp" in combined
    assert "aws s3 rm" in combined


def test_dry_run_bucket_override_is_used():
    result = _run([
        "--instance-id", "i-abc",
        "--region", "us-east-1",
        "--bucket", "my-custom-bucket",
        "--dry-run",
    ])
    assert result.returncode == 0, result.stderr
    combined = result.stdout + result.stderr
    assert "my-custom-bucket" in combined
    # sts get-caller-identity should be skipped when bucket is overridden
    assert "aws sts get-caller-identity" not in combined
