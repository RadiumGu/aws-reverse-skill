#!/usr/bin/env bash
# teardown.sh — Delete the Former2 compliance-mode stack.
set -euo pipefail

STACK_NAME=""
REGION=""
PROFILE=""
FORCE=0
DRY_RUN=0

usage() {
    cat <<EOF
Usage: bash scripts/teardown.sh --stack-name <name> --region <region> \\
    [--profile <name>] [--force] [--dry-run] [--help]

Required:
  --stack-name         CloudFormation stack name to delete
  --region             AWS region

Optional:
  --profile            AWS CLI profile
  --force              Skip y/N confirmation
  --dry-run            Print the aws cli commands without executing
  --help               Show this help
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --stack-name) STACK_NAME="$2"; shift 2;;
        --region) REGION="$2"; shift 2;;
        --profile) PROFILE="$2"; shift 2;;
        --force) FORCE=1; shift;;
        --dry-run) DRY_RUN=1; shift;;
        --help|-h) usage; exit 0;;
        *) echo "ERROR: unknown arg: $1" >&2; usage >&2; exit 2;;
    esac
done

for v in STACK_NAME REGION; do
    if [[ -z "${!v}" ]]; then
        echo "ERROR: --${v,,} is required" | tr '_' '-' >&2
        usage >&2
        exit 2
    fi
done

if [[ "$FORCE" -ne 1 && "$DRY_RUN" -ne 1 ]]; then
    printf 'About to delete stack %q in %q. Continue? [y/N] ' "$STACK_NAME" "$REGION"
    read -r reply
    if [[ ! "$reply" =~ ^[Yy]$ ]]; then
        echo "Aborted." >&2
        exit 1
    fi
fi

PROFILE_ARGS=()
if [[ -n "$PROFILE" ]]; then
    PROFILE_ARGS=(--profile "$PROFILE")
fi

run() {
    if [[ "$DRY_RUN" -eq 1 ]]; then
        printf 'DRY-RUN:'; printf ' %q' "$@"; printf '\n'
    else
        "$@"
    fi
}

run aws cloudformation delete-stack \
    --stack-name "$STACK_NAME" \
    --region "$REGION" \
    "${PROFILE_ARGS[@]}"

run aws cloudformation wait stack-delete-complete \
    --stack-name "$STACK_NAME" \
    --region "$REGION" \
    "${PROFILE_ARGS[@]}"

echo "Stack ${STACK_NAME} deleted."
