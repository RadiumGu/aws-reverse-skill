#!/usr/bin/env bash
# precheck.sh — Thin wrapper around precheck.py for shell invocation.
#
# Usage:
#   bash scripts/precheck.sh <template-file> [--deep] [--raw <raw.json>] \
#                            [--target-region <r>] [--output <path>]
#
# For scripted / tested use, invoke precheck.py directly:
#   python scripts/precheck.py --template cleaned.yml --deep --output out/precheck-report.md
#
# Exit codes:
#   0 — no failures (warnings allowed)
#   1 — missing args / missing template
#   2 — one or more failing checks

set -euo pipefail

TEMPLATE="${1:-}"

if [[ -z "$TEMPLATE" ]]; then
    echo "Usage: bash scripts/precheck.sh <template-file> [--deep] [--raw <raw.json>] [--target-region <r>] [--output <path>]" >&2
    exit 1
fi

if [[ ! -f "$TEMPLATE" ]]; then
    echo "ERROR: template file not found: $TEMPLATE" >&2
    exit 1
fi

shift
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

python3 "$SCRIPT_DIR/precheck.py" --template "$TEMPLATE" "$@" || {
    rc=$?
    echo "precheck.py exited with status ${rc}" >&2
    exit "$rc"
}
