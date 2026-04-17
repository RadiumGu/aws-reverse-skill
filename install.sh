#!/usr/bin/env bash
# install.sh — Install aws-reverse-skill dependencies
set -euo pipefail

echo "==> Checking Node.js..."
if ! command -v node &>/dev/null; then
    echo "ERROR: Node.js not found. Install Node.js >= 18 first." >&2
    exit 1
fi
echo "    Node.js $(node --version)"

echo "==> Installing former2 CLI..."
npm install
echo "    former2 installed (use: npx former2)"

echo "==> Checking Python 3..."
if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 not found." >&2
    exit 1
fi
echo "    $(python3 --version)"

echo "==> Installing Python dependencies..."
pip3 install -r requirements.txt --quiet

echo "==> Checking AWS CLI..."
if command -v aws &>/dev/null; then
    echo "    AWS CLI $(aws --version 2>&1 | head -1)"
else
    echo "    WARNING: aws CLI not found — needed for precheck.sh validate-template"
fi

echo ""
echo "Installation complete. Verify with:"
echo "  npx former2 --help"
echo "  python3 scripts/select.py --help"
echo "  python3 scripts/rewrite_cfn.py --help"
