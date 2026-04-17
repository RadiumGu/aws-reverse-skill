#!/usr/bin/env bash
# ssm-portforward.sh — Start an SSM port forwarding session to a Former2 EC2 instance.
set -euo pipefail

INSTANCE_ID=""
LOCAL_PORT="8080"
REMOTE_PORT="80"
REGION=""
PROFILE=""
DRY_RUN=0

usage() {
    cat <<EOF
Usage: bash scripts/ssm-portforward.sh --instance-id <id> --region <region> \\
    [--local-port 8080] [--remote-port 80] [--profile <name>] [--dry-run] [--help]

Required:
  --instance-id        Target EC2 instance id
  --region             AWS region

Optional:
  --local-port         Local port (default: 8080)
  --remote-port        Remote port (default: 80)
  --profile            AWS CLI profile
  --dry-run            Print the aws cli command without executing
  --help               Show this help
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --instance-id) INSTANCE_ID="$2"; shift 2;;
        --local-port) LOCAL_PORT="$2"; shift 2;;
        --remote-port) REMOTE_PORT="$2"; shift 2;;
        --region) REGION="$2"; shift 2;;
        --profile) PROFILE="$2"; shift 2;;
        --dry-run) DRY_RUN=1; shift;;
        --help|-h) usage; exit 0;;
        *) echo "ERROR: unknown arg: $1" >&2; usage >&2; exit 2;;
    esac
done

for v in INSTANCE_ID REGION; do
    if [[ -z "${!v}" ]]; then
        echo "ERROR: --${v,,} is required" | tr '_' '-' >&2
        usage >&2
        exit 2
    fi
done

if [[ "$DRY_RUN" -ne 1 ]]; then
    if ! command -v session-manager-plugin >/dev/null 2>&1; then
        cat >&2 <<EOF
ERROR: session-manager-plugin is not installed.
Install it from:
  https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html
EOF
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

run aws ssm start-session \
    --target "$INSTANCE_ID" \
    --region "$REGION" \
    --document-name AWS-StartPortForwardingSession \
    --parameters "portNumber=${REMOTE_PORT},localPortNumber=${LOCAL_PORT}" \
    "${PROFILE_ARGS[@]}"
