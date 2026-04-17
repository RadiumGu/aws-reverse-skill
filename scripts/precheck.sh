#!/usr/bin/env bash
# precheck.sh — Pre-deployment validation for cleaned CFN template.
#
# Usage:
#   bash scripts/precheck.sh <template-file>
#
# Returns:
#   0 if all checks pass, non-zero on first failure.

set -euo pipefail

TEMPLATE="${1:-}"

if [[ -z "$TEMPLATE" ]]; then
    echo "Usage: bash scripts/precheck.sh <template-file>" >&2
    exit 1
fi

if [[ ! -f "$TEMPLATE" ]]; then
    echo "ERROR: template file not found: $TEMPLATE" >&2
    exit 1
fi

echo "==> Precheck: $TEMPLATE"
echo ""

# ---------------------------------------------------------------------------
# Step 1: cfn-lint
# ---------------------------------------------------------------------------
echo "--- [1/2] cfn-lint ---"
if command -v cfn-lint &>/dev/null; then
    cfn-lint "$TEMPLATE"
    echo "    cfn-lint: PASSED"
else
    echo "    WARNING: cfn-lint not found — skipping (run: pip install cfn-lint)"
fi
echo ""

# ---------------------------------------------------------------------------
# Step 2: aws cloudformation validate-template
# ---------------------------------------------------------------------------
echo "--- [2/2] aws cloudformation validate-template ---"
if command -v aws &>/dev/null; then
    aws cloudformation validate-template \
        --template-body "file://${TEMPLATE}" \
        --output text
    echo "    validate-template: PASSED"
else
    echo "    WARNING: aws CLI not found — skipping validate-template"
fi
echo ""

echo "==> Precheck complete: $TEMPLATE"
