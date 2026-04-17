#!/usr/bin/env bash
# fetch-export.sh — Retrieve a Former2 CFN export file from an EC2 instance via SSM + S3.
set -euo pipefail

INSTANCE_ID=""
REGION=""
PROFILE=""
REMOTE_PATH="/home/ec2-user/former2-exports/latest"
LOCAL="out/cfn-from-former2.yml"
BUCKET_OVERRIDE=""
DRY_RUN=0

usage() {
    cat <<EOF
Usage: bash scripts/fetch-export.sh --instance-id <id> --region <region> \\
    [--profile <name>] [--remote-path <path>] [--local <file>] \\
    [--bucket <name>] [--dry-run] [--help]

Required:
  --instance-id        Source EC2 instance id
  --region             AWS region

Optional:
  --profile            AWS CLI profile
  --remote-path        Remote file path on EC2 (default: ${REMOTE_PATH})
  --local              Local output file (default: ${LOCAL})
  --bucket             Override transfer bucket name
  --dry-run            Print the aws cli commands without executing
  --help               Show this help
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --instance-id) INSTANCE_ID="$2"; shift 2;;
        --region) REGION="$2"; shift 2;;
        --profile) PROFILE="$2"; shift 2;;
        --remote-path) REMOTE_PATH="$2"; shift 2;;
        --local) LOCAL="$2"; shift 2;;
        --bucket) BUCKET_OVERRIDE="$2"; shift 2;;
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

capture() {
    if [[ "$DRY_RUN" -eq 1 ]]; then
        {
            printf 'DRY-RUN:'
            printf ' %q' "$@"
            printf '\n'
        } >&2
        echo "DRYRUN_VALUE"
    else
        "$@"
    fi
}

if [[ -n "$BUCKET_OVERRIDE" ]]; then
    BUCKET="$BUCKET_OVERRIDE"
else
    ACCOUNT_ID="$(capture aws sts get-caller-identity --query Account --output text "${PROFILE_ARGS[@]}")"
    BUCKET="skill-former2-transfer-${ACCOUNT_ID}-${REGION}"
fi

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RANDHEX="$(od -An -N4 -tx1 /dev/urandom 2>/dev/null | tr -d ' \n' || echo "$$$(date +%s)")"
KEY="exports/${TIMESTAMP}-${RANDHEX}.yml"

# Ensure bucket exists (create with encryption + block public + lifecycle if missing).
if [[ "$DRY_RUN" -eq 1 ]] || ! aws s3api head-bucket --bucket "$BUCKET" --region "$REGION" "${PROFILE_ARGS[@]}" >/dev/null 2>&1; then
    run aws s3api head-bucket --bucket "$BUCKET" --region "$REGION" "${PROFILE_ARGS[@]}" || true
    run aws s3 mb "s3://${BUCKET}" --region "$REGION" "${PROFILE_ARGS[@]}"
    run aws s3api put-bucket-encryption \
        --bucket "$BUCKET" \
        --region "$REGION" \
        --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}' \
        "${PROFILE_ARGS[@]}"
    run aws s3api put-public-access-block \
        --bucket "$BUCKET" \
        --region "$REGION" \
        --public-access-block-configuration 'BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true' \
        "${PROFILE_ARGS[@]}"
    run aws s3api put-bucket-lifecycle-configuration \
        --bucket "$BUCKET" \
        --region "$REGION" \
        --lifecycle-configuration '{"Rules":[{"ID":"expire-1d","Status":"Enabled","Filter":{"Prefix":"exports/"},"Expiration":{"Days":1}}]}' \
        "${PROFILE_ARGS[@]}"
fi

# Tell the EC2 instance to upload the remote file to S3.
CMD="aws s3 cp ${REMOTE_PATH} s3://${BUCKET}/${KEY} --region ${REGION}"
SEND_OUT="$(capture aws ssm send-command \
    --instance-ids "$INSTANCE_ID" \
    --region "$REGION" \
    --document-name AWS-RunShellScript \
    --parameters "commands=[\"${CMD}\"]" \
    --query 'Command.CommandId' \
    --output text \
    "${PROFILE_ARGS[@]}")"

run aws ssm wait command-executed \
    --command-id "$SEND_OUT" \
    --instance-id "$INSTANCE_ID" \
    --region "$REGION" \
    "${PROFILE_ARGS[@]}"

# Download and delete the transient object locally.
mkdir -p "$(dirname "$LOCAL")"
run aws s3 cp "s3://${BUCKET}/${KEY}" "$LOCAL" --region "$REGION" "${PROFILE_ARGS[@]}"
run aws s3 rm "s3://${BUCKET}/${KEY}" --region "$REGION" "${PROFILE_ARGS[@]}"

echo "Fetched export to ${LOCAL} (bucket: ${BUCKET})"
