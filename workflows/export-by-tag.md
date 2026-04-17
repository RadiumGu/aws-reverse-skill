# Workflow: Export Resources by Tag

**Use case**: Export all production-tagged resources from source account/region and generate
a deployable CloudFormation template for the target region.

---

## Prerequisites

```bash
# Install dependencies
bash install.sh

# Verify AWS credentials
aws sts get-caller-identity --profile <your-profile>
```

---

## Step-by-Step

### 1. Scan the source environment

```bash
mkdir -p out

node scripts/scan.js \
  --region ap-northeast-1 \
  --profile prod \
  --out-raw out/raw.json \
  --out-cfn out/cfn-full.yml
```

Expected output:
- `out/raw.json` — resource metadata for all discovered resources
- `out/cfn-full.yml` — complete CloudFormation template (unfiltered)

---

### 2. Filter by tag

Select only resources tagged `Environment=prod`:

```bash
python scripts/select.py \
  --input out/raw.json \
  --output out/filtered.json \
  --tag Environment=prod \
  --exclude-default
```

Preview the count:
```bash
cat out/filtered.json | python3 -c "import json,sys; d=json.load(sys.stdin); print(f'{d[\"count\"]} resources selected')"
```

---

### 3. Generate the scoped regex for former2

```bash
python scripts/select.py \
  --input out/raw.json \
  --tag Environment=prod \
  --exclude-default \
  --emit-regex-filter > out/id-filter.txt

echo "Filter regex:"
cat out/id-filter.txt
```

---

### 4. Apply the filter with former2

```bash
npx former2 filter \
  --input out/cfn-full.yml \
  --output out/cfn-filtered.yml \
  --search-filter "$(cat out/id-filter.txt)"
```

---

### 5. Rewrite for the target environment

Replace hardcoded account ID, region, AMI IDs, and AZs:

```bash
python scripts/rewrite_cfn.py \
  --input out/cfn-filtered.yml \
  --output out/cleaned.yml \
  --account-id 123456789012 \
  --source-region ap-northeast-1 \
  --target-region us-east-1
```

---

### 6. Validate

```bash
bash scripts/precheck.sh out/cleaned.yml
```

Review any cfn-lint warnings. Check `references/review-checklist.md` for manual items.

---

### 7. Deploy

```bash
aws cloudformation deploy \
  --template-file out/cleaned.yml \
  --stack-name myapp-prod-us-east-1 \
  --region us-east-1 \
  --profile target-profile \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides AmiId=/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64
```

Monitor deployment:
```bash
aws cloudformation describe-stack-events \
  --stack-name myapp-prod-us-east-1 \
  --region us-east-1 \
  --query 'StackEvents[?ResourceStatus!=`UPDATE_COMPLETE`].[ResourceStatus,ResourceType,LogicalResourceId,ResourceStatusReason]' \
  --output table
```

---

## Example: Filter by tag + service

Export only prod-tagged Lambda functions:

```bash
python scripts/select.py \
  --input out/raw.json \
  --service Lambda \
  --tag Environment=prod \
  --emit-regex-filter > out/id-filter.txt

npx former2 filter \
  --input out/cfn-full.yml \
  --output out/cfn-lambda-only.yml \
  --search-filter "$(cat out/id-filter.txt)"

python scripts/rewrite_cfn.py \
  --input out/cfn-lambda-only.yml \
  --output out/cleaned-lambda.yml \
  --account-id 123456789012 \
  --source-region ap-northeast-1
```

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `former2: command not found` | Run `npm install` in the skill directory |
| `0 resources selected` | Check tag spelling; confirm tags with `aws resourcegroupstaggingapi get-resources` |
| cfn-lint errors on `!Sub` / `!Ref` | Usually safe to ignore W-level warnings after rewrite |
| `validate-template` size error | Template > 51,200 bytes; split into nested stacks |
