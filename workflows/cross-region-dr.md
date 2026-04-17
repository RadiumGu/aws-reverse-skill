# Workflow: Cross-Region Disaster Recovery

**Use case**: Replicate a production stack into a secondary region in the same
account for DR. The emphasis here is on **`deployment-advice.md`** — the
template boots an empty skeleton, but data, AMIs, certs, and EventBridge rules
still need deliberate cross-region handling.

---

## Prerequisites

```bash
bash install.sh
aws sts get-caller-identity --profile prod
```

Source region: `ap-northeast-1`. Target region (DR): `us-east-1`.

---

## Step 1 — Scan the source region

```bash
mkdir -p out

node scripts/scan.js \
  --region ap-northeast-1 \
  --profile prod \
  --out-raw out/raw.json \
  --out-cfn out/cfn-full.yml
```

---

## Step 2 — Preview + filter

```bash
python3 scripts/preview.py --input out/raw.json --group-by service
python3 scripts/preview.py --input out/raw.json \
  --group-by tag --tag-key Environment --sample 2

python3 scripts/select.py \
  --input out/raw.json \
  --tag Environment=prod \
  --exclude-default \
  --dry-run

python3 scripts/select.py \
  --input out/raw.json \
  --output out/filtered.json \
  --tag Environment=prod \
  --exclude-default \
  --emit-regex-filter > out/id-filter.txt

npx former2 filter \
  --input out/cfn-full.yml \
  --output out/cfn-filtered.yml \
  --search-filter "$(cat out/id-filter.txt)"
```

---

## Step 3 — Rewrite with the `cross-region` preset

```bash
python3 scripts/rewrite_cfn.py \
  --input out/cfn-filtered.yml \
  --output out/cleaned.yml \
  --source-region ap-northeast-1 \
  --target-region us-east-1 \
  --preset cross-region
```

`cross-region` activates `region` / `ami_id` / `az` / `deletion_policy` — no
account rewriting because we stay in the same account.

---

## Step 4 — Deployment advice (the reason for this workflow)

```bash
python3 scripts/deployment_advice.py \
  --input out/raw.json \
  --output out/deployment-advice.md \
  --stack-name myapp-prod-dr
```

Expect guidance for (among others):

| Layer | Resource | Advice |
|-------|----------|--------|
| 数据层 | RDS DB | DMS / Snapshot-Copy / Aurora Global |
| 数据层 | DynamoDB | Global Tables (⭐) |
| 数据层 | S3 | Cross-Region Replication (⭐) |
| 数据层 | EFS | EFS Replication / DataSync |
| 镜像/工件 | AMI | `aws ec2 copy-image` snippet |
| 镜像/工件 | ECR | `put-replication-configuration` |
| 集成 | ACM | `request-certificate --region us-east-1` + DNS validation |
| 可观测性 | CloudWatch Logs | subscription / S3 export |
| 身份 | KMS | create target-region CMK or use MRK |

Also emit a compact checklist to embed in the admin's change ticket:

```bash
python3 scripts/deployment_advice.py \
  --input out/raw.json \
  --format checklist \
  --output out/dr-checklist.md
```

And a machine-readable JSON for pipeline automation:

```bash
python3 scripts/deployment_advice.py \
  --input out/raw.json \
  --format json \
  --output out/dr-advice.json
```

---

## Step 5 — Precheck against the target region

```bash
AWS_DEFAULT_REGION=us-east-1 python3 scripts/precheck.py \
  --template out/cleaned.yml \
  --raw out/raw.json \
  --target-region us-east-1 \
  --output out/precheck-report.md \
  --deep
```

Typical findings:
- AMI referenced in `AmiId` Parameter Description does not exist in `us-east-1`
  → follow the `aws ec2 copy-image` command from `deployment-advice.md`.
- S3 bucket name already taken globally → flip to `BucketPrefix`-based naming
  (R10) by re-running rewrite with `--preset cross-account` or `full`.

---

## Step 6 — Review loop

```bash
python3 scripts/generate_review.py \
  --cleaned out/cleaned.yml \
  --raw out/raw.json \
  --precheck-report out/precheck-report.md \
  --output out/review.md \
  --stack-name myapp-prod-dr \
  --preset cross-region \
  --source "ap-northeast-1" \
  --target "us-east-1"
```

Admin fills parameters, unchecks resources that shouldn't be part of DR
(e.g. one-off scheduled jobs), confirms AMI IDs match the post-copy IDs in
`us-east-1`. Save the file, then:

```bash
python3 scripts/apply_review.py out/review.md \
  --cleaned out/cleaned.yml \
  --out-final out/cleaned-final.yml \
  --out-params out/deploy-params.json \
  --out-manual out/manual-tasks.md \
  --diff out/review.diff
```

---

## Step 7 — Execute the pre-deploy runbook

Walk through `deployment-advice.md` **before** the CloudFormation deploy:

```bash
# AMI copy (once per source AMI)
aws ec2 copy-image \
  --source-region ap-northeast-1 \
  --source-image-id ami-0abc123... \
  --region us-east-1 \
  --name myapp-prod-dr

# ECR replication
aws ecr put-replication-configuration --replication-configuration \
  '{"rules":[{"destinations":[{"region":"us-east-1","registryId":"<acct>"}]}]}'

# ACM in target region
aws acm request-certificate --domain-name api.example.com \
  --validation-method DNS --region us-east-1

# S3 CRR — enable on source buckets that should replicate
# (see deployment-advice.md §数据层 for the full command)
```

---

## Step 8 — Deploy in the target region

```bash
aws cloudformation deploy \
  --template-file out/cleaned-final.yml \
  --stack-name myapp-prod-dr \
  --region us-east-1 \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides $(python3 -c "
import json
print(' '.join(f\"{p['ParameterKey']}={p['ParameterValue']}\" for p in json.load(open('out/deploy-params.json'))))
")
```

---

## Step 9 — Post-deploy: wire up data replication

These tasks are from `deployment-advice.md` and need to run **after** the DR
stack exists (so target resources are available as replication targets):

- RDS: create DMS replication task source→target.
- DynamoDB: `aws dynamodb update-table --replica-updates ...`
- S3: enable versioning + CRR on each source bucket pointing to the DR bucket.
- Route53 (if health checks / failover records): add target-region records.

Archive `out/deployment-advice.md`, `out/dr-checklist.md`,
`out/review.diff`, and `out/manual-tasks.md` with the DR runbook.
