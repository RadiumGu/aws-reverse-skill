#!/usr/bin/env bash
# deploy-former2.sh — Deploy the Former2 EC2 compliance-mode stack.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="${SCRIPT_DIR}/../references/former2-cfn.yaml"

VPC_ID=""
SUBNET_ID=""
STACK_NAME=""
REGION=""
INSTANCE_TYPE="t4g.medium"
PROFILE=""
CREATE_ENDPOINTS="Yes"
DRY_RUN=0

usage() {
    cat <<EOF
Usage: bash scripts/deploy-former2.sh --vpc-id <vpc> --subnet-id <subnet> \\
    --stack-name <name> --region <region> \\
    [--instance-type t4g.medium] [--profile <name>] \\
    [--create-endpoints Yes|No] [--dry-run] [--help]

Required:
  --vpc-id             Target VPC id
  --subnet-id          Private subnet id
  --stack-name         CloudFormation stack name
  --region             AWS region

Optional:
  --instance-type      EC2 instance type (default: t4g.medium)
  --profile            AWS CLI profile
  --create-endpoints   Yes (default) to create ssm/ssmmessages/ec2messages/logs/s3 endpoints
  --dry-run            Print the aws cli commands without executing
  --help               Show this help

Template: ${TEMPLATE}
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --vpc-id) VPC_ID="$2"; shift 2;;
        --subnet-id) SUBNET_ID="$2"; shift 2;;
        --stack-name) STACK_NAME="$2"; shift 2;;
        --region) REGION="$2"; shift 2;;
        --instance-type) INSTANCE_TYPE="$2"; shift 2;;
        --profile) PROFILE="$2"; shift 2;;
        --create-endpoints) CREATE_ENDPOINTS="$2"; shift 2;;
        --dry-run) DRY_RUN=1; shift;;
        --help|-h) usage; exit 0;;
        *) echo "ERROR: unknown arg: $1" >&2; usage >&2; exit 2;;
    esac
done

for v in VPC_ID SUBNET_ID STACK_NAME REGION; do
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

run aws cloudformation deploy \
    --template-file "$TEMPLATE" \
    --stack-name "$STACK_NAME" \
    --region "$REGION" \
    --capabilities CAPABILITY_IAM \
    --parameter-overrides \
        VpcId="$VPC_ID" \
        SubnetId="$SUBNET_ID" \
        InstanceType="$INSTANCE_TYPE" \
        CreateEndpoints="$CREATE_ENDPOINTS" \
    "${PROFILE_ARGS[@]}"

run aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --region "$REGION" \
    --query 'Stacks[0].Outputs' \
    --output table \
    "${PROFILE_ARGS[@]}"
