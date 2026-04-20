# Quick Start — aws-reverse-skill

Get from **existing AWS resources → deployable CloudFormation template** in 5 minutes.

This guide uses **Mode A** (local CLI) with the **cleanup-only** preset — the
simplest path. For cross-account, cross-region, or in-account (Mode B)
workflows, see the full [SKILL.md](../SKILL.md).

---

## Prerequisites

| Tool | Install |
|------|---------|
| Node.js ≥ 18 | `nvm install 18` |
| Python 3.10+ | System or `pyenv` |
| AWS CLI v2 | `brew install awscli` / apt |
| Configured profile | `aws configure --profile source` |

```bash
cd /path/to/aws-reverse-skill
npm ci
pip install -r requirements-lock.txt
```

---

## Step 1 — Scan

```bash
node scripts/scan.js \
  --region ap-northeast-1 \
  --services Lambda,S3,DynamoDB \
  --profile source \
  --out-raw out/raw.json \
  --out-cfn out/cfn-full.yml
```

> 💡 Results are cached for 24h. Re-run is instant.

---

## Step 2 — Filter

Preview what was found, then filter:

```bash
# Preview
python3 scripts/preview.py --input out/raw.json --group-by service --sample 3

# Filter (dry-run first)
python3 scripts/select.py \
  --input out/raw.json \
  --exclude-default \
  --dry-run

# Commit
python3 scripts/select.py \
  --input out/raw.json \
  --output out/filtered.json \
  --exclude-default \
  --emit-regex-filter > out/id-filter.txt

npx former2 filter \
  --input out/cfn-full.yml \
  --output out/cfn-filtered.yml \
  --search-filter "$(cat out/id-filter.txt)"
```

---

## Step 3 — Rewrite (cleanup-only preset)

```bash
python3 scripts/rewrite_cfn.py \
  --input out/cfn-filtered.yml \
  --output out/cleaned.yml \
  --account-id 123456789012 \
  --source-region ap-northeast-1 \
  --preset cleanup-only \
  --review-decisions out/decisions.json
```

This replaces hardcoded account IDs, regions, AMIs, and AZs with
CloudFormation parameters and pseudo-references.

---

## Step 4 — Precheck & Deploy

```bash
# Validate the template
python3 scripts/precheck.py --template out/cleaned.yml --region ap-northeast-1

# Deploy
aws cloudformation deploy \
  --template-file out/cleaned.yml \
  --stack-name my-app \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides AmiId=/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64
```

---

## What's Next?

| Goal | Preset | Extra flags |
|------|--------|-------------|
| Same account, different region | `cross-region` | `--target-region us-east-1` |
| Different account | `cross-account` | `--target-region ... --source-vpc-cidr 10.0.0.0/16` |
| Full rewrite (all rules) | `full` | All flags above |
| Admin review loop | Any | See SKILL.md Steps 6-9 |

For the complete 11-step workflow with review loop and deployment advice,
see [SKILL.md](../SKILL.md).
