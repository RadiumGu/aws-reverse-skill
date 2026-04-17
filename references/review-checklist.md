# Pre-Deployment Review Checklist

Run `scripts/precheck.sh cleaned.yml` first, then go through this checklist manually.

## Automated Checks (precheck.sh)

- [ ] `cfn-lint cleaned.yml` exits 0
- [ ] `aws cloudformation validate-template --template-body file://cleaned.yml` succeeds

## Cross-Region / Cross-Account

- [ ] No hardcoded account IDs remain (grep for 12-digit numbers)
- [ ] No hardcoded region codes remain (grep for `us-east-1`, `ap-northeast-1`, etc.)
- [ ] AMI IDs parameterized or updated for target region
- [ ] ECR image URIs updated if deploying containers

## IAM

- [ ] IAM Role trust policies reference correct principals for target account
- [ ] IAM policies use `${AWS::AccountId}` / `${AWS::Partition}` where needed
- [ ] No cross-account IAM role ARNs hardcoded

## Data Layer

- [ ] S3 buckets with data: confirm replication or data migration plan
- [ ] RDS: DeletionPolicy=Retain confirmed; snapshot/restore strategy defined
- [ ] DynamoDB: DeletionPolicy=Retain confirmed; export/import strategy defined
- [ ] Secrets Manager / SSM ParameterStore values copied to target account

## KMS

- [ ] KMS CMK ARN references updated to target account CMK
- [ ] S3 bucket encryption keys valid in target region

## Networking

- [ ] VPC/Subnet IDs are NOT hardcoded (they are account/region specific)
- [ ] Elastic IPs: quota confirmed in target region
- [ ] Security Group rule CIDRs reviewed

## Lambda / ECS / EKS

- [ ] Lambda function code S3 bucket accessible from target region
- [ ] ECS task definition image URIs updated
- [ ] EKS node AMI IDs updated for target region

## Deployment Command

```bash
aws cloudformation deploy \
  --template-file out/cleaned.yml \
  --stack-name <stack-name> \
  --region <target-region> \
  --profile <target-profile> \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides AmiId=ami-xxx
```
