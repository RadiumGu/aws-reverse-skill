"""Tests for scripts/deploy-former2.sh — arg validation + dry-run command rendering."""

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "deploy-former2.sh"


def _run(args, input_text=None):
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        input=input_text,
    )


def test_missing_required_args_exits_nonzero():
    result = _run([])
    assert result.returncode != 0
    assert "required" in (result.stderr + result.stdout).lower()


def test_help_exits_zero():
    result = _run(["--help"])
    assert result.returncode == 0
    assert "--vpc-id" in result.stdout


def test_dry_run_prints_deploy_command_with_required_args():
    result = _run([
        "--vpc-id", "vpc-123",
        "--subnet-id", "subnet-abc",
        "--stack-name", "mystack",
        "--region", "us-east-1",
        "--dry-run",
    ])
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "aws cloudformation deploy" in out
    assert "--stack-name mystack" in out
    assert "--parameter-overrides" in out
    assert "VpcId=vpc-123" in out
    assert "SubnetId=subnet-abc" in out
