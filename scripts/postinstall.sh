#!/usr/bin/env bash
# postinstall.sh — Patch former2 ACM crash (data.Certificates may be undefined).
# See: BUG-03 in test-runs/20260420/test-report.md
set -euo pipefail

TARGET="node_modules/former2/services/ec2.js"
if [ ! -f "$TARGET" ]; then
    echo "postinstall: former2 not found, skipping patch"
    exit 0
fi

# Patch all unguarded .Certificates.forEach calls
sed -i 's/data\.Certificates\.forEach/(data.Certificates || []).forEach/g' "$TARGET"
sed -i 's/obj\.data\.Certificates\.forEach/(obj.data.Certificates || []).forEach/g' "$TARGET"

echo "postinstall: patched former2 ACM Certificates guard in $TARGET"
