"""Tests for scripts/ssm-portforward.sh — arg validation + dry-run rendering."""

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "ssm-portforward.sh"


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


def test_dry_run_prints_start_session_command():
    result = _run([
        "--instance-id", "i-123",
        "--region", "us-east-1",
        "--dry-run",
    ])
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "aws ssm start-session" in out
    assert "AWS-StartPortForwardingSession" in out
    assert "i-123" in out
    assert "portNumber=80" in out
    assert "localPortNumber=8080" in out
