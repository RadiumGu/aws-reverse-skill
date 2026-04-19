"""Tests for scripts/audit.py — AuditLog, AuditEntry, record_stage."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from audit import (
    AuditEntry,
    AuditLog,
    KNOWN_STEPS,
    record_stage,
    sha256_file,
    sha256_many,
)


# ---------------------------------------------------------------------------
# sha256 helpers
# ---------------------------------------------------------------------------


def test_sha256_file_deterministic(tmp_path):
    f = tmp_path / "x.txt"
    f.write_bytes(b"hello")
    assert sha256_file(f) == sha256_file(f)
    # sha256("hello") well-known
    assert sha256_file(f) == (
        "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    )


def test_sha256_file_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        sha256_file(tmp_path / "does-not-exist")


def test_sha256_many_basename_keys(tmp_path):
    (tmp_path / "a.txt").write_text("A")
    (tmp_path / "b.txt").write_text("B")
    result = sha256_many([tmp_path / "a.txt", tmp_path / "b.txt"])
    assert set(result) == {"a.txt", "b.txt"}
    assert all(len(v) == 64 for v in result.values())


# ---------------------------------------------------------------------------
# KNOWN_STEPS sanity
# ---------------------------------------------------------------------------


def test_known_steps_contains_pipeline_stages():
    for s in ("scan", "filter", "rewrite", "precheck", "review", "apply_review", "deploy"):
        assert s in KNOWN_STEPS


# ---------------------------------------------------------------------------
# AuditLog.new / record / write / load round-trip
# ---------------------------------------------------------------------------


def test_audit_log_new_timestamp_and_operator(monkeypatch):
    monkeypatch.setenv("AWS_REVERSE_OPERATOR", "alice@example.com")
    log = AuditLog.new()
    assert log.operator == "alice@example.com"
    assert log.started_at.endswith("Z")
    assert len(log.run_id) >= 15
    assert log.entries == []


def test_audit_log_record_hashes_inputs_and_outputs(tmp_path):
    inp = tmp_path / "raw.json"
    inp.write_text('{"resources": []}', encoding="utf-8")
    out = tmp_path / "cfn-full.yml"
    out.write_text("Resources: {}\n", encoding="utf-8")

    log = AuditLog.new(operator="test-op")
    entry = log.record(step="scan", inputs=[inp], outputs=[out], notes="unit test")

    assert isinstance(entry, AuditEntry)
    assert entry.step == "scan"
    assert entry.operator == "test-op"
    assert entry.input_sha256 == {"raw.json": sha256_file(inp)}
    assert entry.output_sha256 == {"cfn-full.yml": sha256_file(out)}
    assert entry.notes == "unit test"


def test_audit_log_record_skips_missing_files(tmp_path):
    real = tmp_path / "real.json"
    real.write_text("x", encoding="utf-8")

    log = AuditLog.new(operator="t")
    entry = log.record(
        step="filter",
        inputs=[real, tmp_path / "missing.json"],
        outputs=[tmp_path / "nope.json"],
    )
    assert "real.json" in entry.input_sha256
    assert "missing.json" not in entry.input_sha256
    assert entry.output_sha256 == {}
    assert "skipped missing" in entry.notes


def test_audit_log_write_and_load_round_trip(tmp_path):
    src = tmp_path / "raw.json"
    src.write_text("alpha", encoding="utf-8")

    log = AuditLog.new(operator="rt")
    log.record(step="scan", inputs=[src], outputs=[])
    log.record(step="filter", inputs=[src], outputs=[], notes="second entry")

    out_path = tmp_path / "audit.json"
    log.write(out_path)
    assert out_path.exists()

    data = json.loads(out_path.read_text(encoding="utf-8"))
    assert data["operator"] == "rt"
    assert len(data["entries"]) == 2
    assert data["entries"][0]["step"] == "scan"
    assert data["entries"][1]["notes"] == "second entry"

    restored = AuditLog.load(out_path)
    assert restored.run_id == log.run_id
    assert len(restored.entries) == 2
    assert restored.entries[1].notes == "second entry"


def test_audit_log_default_path_uses_run_id(tmp_path):
    log = AuditLog.new(operator="x")
    expected = tmp_path / f"audit-{log.run_id}.json"
    assert log.default_path(tmp_path) == expected


# ---------------------------------------------------------------------------
# record_stage env-var hook
# ---------------------------------------------------------------------------


def test_record_stage_no_env_noop(tmp_path, monkeypatch):
    monkeypatch.delenv("AWS_REVERSE_AUDIT_LOG", raising=False)
    result = record_stage(step="scan", inputs=[], outputs=[])
    assert result is None


def test_record_stage_creates_and_appends(tmp_path, monkeypatch):
    src = tmp_path / "in.txt"
    src.write_text("data", encoding="utf-8")
    log_path = tmp_path / "audit.json"
    monkeypatch.setenv("AWS_REVERSE_AUDIT_LOG", str(log_path))

    # First call creates the file
    result1 = record_stage(step="scan", inputs=[src], outputs=[])
    assert result1 == log_path
    assert log_path.exists()

    # Second call appends
    result2 = record_stage(step="filter", inputs=[src], outputs=[], notes="note")
    assert result2 == log_path

    data = json.loads(log_path.read_text(encoding="utf-8"))
    assert len(data["entries"]) == 2
    steps = [e["step"] for e in data["entries"]]
    assert steps == ["scan", "filter"]


def test_record_stage_errors_are_swallowed(tmp_path, monkeypatch, capsys):
    # Point to a path under a regular file → mkdir will fail
    parent_file = tmp_path / "not-a-dir"
    parent_file.write_text("x", encoding="utf-8")
    monkeypatch.setenv("AWS_REVERSE_AUDIT_LOG", str(parent_file / "nested" / "audit.json"))

    # Must not raise, returns None on error
    result = record_stage(step="scan", inputs=[], outputs=[])
    assert result is None
    captured = capsys.readouterr()
    assert "audit" in captured.err.lower()
