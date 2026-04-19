#!/usr/bin/env python3
"""audit.py — Pipeline audit log for aws-reverse-skill.

Implements the ``out/audit-<timestamp>.json`` artefact referenced in SKILL.md
Step 11. Each pipeline stage (scan / filter / rewrite / precheck / review /
deploy) records one ``AuditEntry`` describing its input and output file
digests plus contextual metadata. The resulting JSON is a replayable,
tamper-evident record: re-running the stage over the same inputs must
produce identical SHA-256 hashes.

Entry schema::

    {
        "step": "scan",
        "timestamp": "2026-04-20T03:30:00Z",
        "operator": "operator@example.com",
        "input_sha256":  {"raw.json": "abc..."},
        "output_sha256": {"cfn-full.yml": "def..."},
        "notes": "optional free-form string"
    }

The ``AuditLog`` object owns an append-only list of entries and can be
serialised via :meth:`AuditLog.write` to a JSON file. :meth:`AuditLog.load`
reconstructs an existing log so that resumed sessions can append to the
same artefact.

This module has no third-party dependencies — stdlib only.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


#: Pipeline stages recognised as first-class ``step`` values. Unknown steps
#: are accepted for forward compatibility but callers should prefer one of
#: these when possible.
KNOWN_STEPS: frozenset[str] = frozenset({
    "scan",
    "filter",
    "rewrite",
    "precheck",
    "review",
    "apply_review",
    "deploy",
})


def _utc_now_iso() -> str:
    """Return the current UTC time formatted as ``YYYY-MM-DDTHH:MM:SSZ``."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_operator() -> str:
    """Resolve the operator identity from environment, falling back to OS user.

    Precedence: ``AWS_REVERSE_OPERATOR`` → ``USER`` → ``getpass.getuser()`` →
    ``"unknown"``.
    """
    for env_var in ("AWS_REVERSE_OPERATOR", "USER"):
        value = os.environ.get(env_var)
        if value:
            return value
    try:
        return getpass.getuser()
    except Exception:  # pragma: no cover — extremely rare
        return "unknown"


def sha256_file(path: Path | str) -> str:
    """Compute the SHA-256 hex digest of a file in fixed-size chunks.

    Args:
        path: File path (missing files raise :class:`FileNotFoundError`).

    Returns:
        Lower-case hex digest string (64 chars).
    """
    hasher = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def sha256_many(paths: Iterable[Path | str]) -> dict[str, str]:
    """Return ``{filename: sha256}`` for each path, preserving insertion order.

    Uses the file's ``name`` (basename) as the dict key — callers that need
    to disambiguate same-named files across directories should pre-process
    the mapping themselves.
    """
    result: dict[str, str] = {}
    for path in paths:
        p = Path(path)
        result[p.name] = sha256_file(p)
    return result


@dataclass
class AuditEntry:
    """One line in the audit log."""

    step: str
    timestamp: str
    operator: str
    input_sha256: dict[str, str] = field(default_factory=dict)
    output_sha256: dict[str, str] = field(default_factory=dict)
    notes: str = ""


@dataclass
class AuditLog:
    """Append-only audit log for a single pipeline run.

    Typical usage::

        log = AuditLog.new()
        log.record(step="scan",
                   inputs=["raw.json"],
                   outputs=["out/cfn-full.yml"])
        ...
        log.write(Path("out") / f"audit-{log.run_id}.json")
    """

    run_id: str
    started_at: str
    operator: str
    entries: list[AuditEntry] = field(default_factory=list)

    @classmethod
    def new(cls, *, operator: str | None = None) -> "AuditLog":
        """Create a fresh log stamped with the current UTC timestamp.

        ``run_id`` is derived from the start time so it slots cleanly into the
        ``out/audit-<timestamp>.json`` filename.
        """
        now = _utc_now_iso()
        run_id = now.replace(":", "").replace("-", "")
        return cls(
            run_id=run_id,
            started_at=now,
            operator=operator or _resolve_operator(),
            entries=[],
        )

    def record(
        self,
        *,
        step: str,
        inputs: Iterable[Path | str] | None = None,
        outputs: Iterable[Path | str] | None = None,
        notes: str = "",
        operator: str | None = None,
    ) -> AuditEntry:
        """Append a new entry for a pipeline stage.

        Args:
            step: Pipeline stage name (see :data:`KNOWN_STEPS`).
            inputs: Paths read during this stage. Missing files are skipped
                and noted in the entry's ``notes`` field.
            outputs: Paths written during this stage. Missing files are
                skipped with a note.
            notes: Free-form string appended verbatim (alongside any
                auto-generated "skipped missing file" notes).
            operator: Override operator for this entry only (rare — defaults
                to log-wide operator).

        Returns:
            The :class:`AuditEntry` just appended (also stored on ``self``).
        """
        auto_notes: list[str] = []
        input_hashes = self._hash_existing(inputs or [], auto_notes, "input")
        output_hashes = self._hash_existing(outputs or [], auto_notes, "output")

        full_notes = notes
        if auto_notes:
            suffix = "; ".join(auto_notes)
            full_notes = f"{notes} [{suffix}]" if notes else f"[{suffix}]"

        entry = AuditEntry(
            step=step,
            timestamp=_utc_now_iso(),
            operator=operator or self.operator,
            input_sha256=input_hashes,
            output_sha256=output_hashes,
            notes=full_notes,
        )
        self.entries.append(entry)
        return entry

    @staticmethod
    def _hash_existing(
        paths: Iterable[Path | str],
        notes_out: list[str],
        label: str,
    ) -> dict[str, str]:
        """Hash each path that exists; record skipped ones in ``notes_out``."""
        result: dict[str, str] = {}
        for path in paths:
            p = Path(path)
            if p.exists() and p.is_file():
                result[p.name] = sha256_file(p)
            else:
                notes_out.append(f"skipped missing {label} {p}")
        return result

    def to_dict(self) -> dict:
        """Return a JSON-serialisable dict representation of the log."""
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "operator": self.operator,
            "entries": [asdict(e) for e in self.entries],
        }

    def write(self, path: Path | str) -> Path:
        """Serialise the log to *path* as pretty-printed JSON.

        Parent directories are created if missing. Returns the resolved
        :class:`pathlib.Path`.
        """
        out_path = Path(path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=2, sort_keys=False)
            fh.write("\n")
        return out_path

    @classmethod
    def load(cls, path: Path | str) -> "AuditLog":
        """Reconstruct a log from its JSON representation on disk."""
        with Path(path).open(encoding="utf-8") as fh:
            data = json.load(fh)
        return cls(
            run_id=data["run_id"],
            started_at=data["started_at"],
            operator=data["operator"],
            entries=[AuditEntry(**e) for e in data.get("entries", [])],
        )

    def default_path(self, out_dir: Path | str = "out") -> Path:
        """Return the conventional ``<out_dir>/audit-<run_id>.json`` path."""
        return Path(out_dir) / f"audit-{self.run_id}.json"


# ---------------------------------------------------------------------------
# CLI — useful for shell-driven pipelines that want to append an entry and
# rewrite the audit JSON atomically without loading the module.
# ---------------------------------------------------------------------------


def record_stage(
    step: str,
    *,
    inputs: Iterable[Path | str] | None = None,
    outputs: Iterable[Path | str] | None = None,
    notes: str = "",
) -> Path | None:
    """Append an entry to the audit log named by ``AWS_REVERSE_AUDIT_LOG``.

    Pipeline scripts call this from their ``main()`` at success. When the
    ``AWS_REVERSE_AUDIT_LOG`` environment variable is unset the call is a
    no-op — keeping the feature fully opt-in and preserving backward-
    compatible CLI behaviour.

    Args:
        step: Pipeline stage name (see :data:`KNOWN_STEPS`).
        inputs: Files consumed by the stage.
        outputs: Files produced by the stage.
        notes: Free-form audit note.

    Returns:
        Path of the audit log that was appended, or ``None`` if the feature
        is disabled / the append failed (errors are swallowed — audit logging
        must never break the pipeline).
    """
    log_file = os.environ.get("AWS_REVERSE_AUDIT_LOG")
    if not log_file:
        return None
    try:
        log_path = Path(log_file)
        if log_path.exists():
            log = AuditLog.load(log_path)
        else:
            log = AuditLog.new()
        log.record(step=step, inputs=inputs, outputs=outputs, notes=notes)
        log.write(log_path)
        return log_path
    except Exception as exc:  # noqa: BLE001 — audit must never break pipeline
        import sys
        print(f"[audit] WARN: failed to record '{step}': {exc}", file=sys.stderr)
        return None


def _cli(argv: list[str]) -> int:  # pragma: no cover — thin argparse wrapper
    import argparse

    parser = argparse.ArgumentParser(description="Append an audit log entry.")
    parser.add_argument("--log", required=True, help="Path to audit JSON file.")
    parser.add_argument("--step", required=True, help="Pipeline stage name.")
    parser.add_argument("--input", action="append", default=[], help="Input file (repeatable).")
    parser.add_argument("--output", action="append", default=[], help="Output file (repeatable).")
    parser.add_argument("--notes", default="", help="Free-form notes.")
    args = parser.parse_args(argv)

    log_path = Path(args.log)
    if log_path.exists():
        log = AuditLog.load(log_path)
    else:
        log = AuditLog.new()
    log.record(step=args.step, inputs=args.input, outputs=args.output, notes=args.notes)
    log.write(log_path)
    print(f"[audit] appended {args.step} → {log_path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys
    sys.exit(_cli(sys.argv[1:]))
