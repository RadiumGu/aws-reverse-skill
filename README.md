# aws-reverse-skill

Reverse-engineer existing AWS environments into deployable CloudFormation templates.

**Phase 1 MVP — Standard Mode (former2 CLI)**

---

## What It Does

```
AWS Account  →  former2 scan  →  select.py filter  →  rewrite_cfn.py clean  →  cfn-lint check  →  deploy
```

1. **Scan** — `former2` CLI enumerates ~130 AWS service types via AWS API
2. **Filter** — `select.py` filters by service / tag / regex; strips default VPC/SG/NACL
3. **Rewrite** — `rewrite_cfn.py` removes hardcoded Account IDs, region codes, AMI IDs, and AZs
4. **Validate** — `cfn-lint` + `aws cloudformation validate-template`
5. **Deploy** — standard `aws cloudformation deploy` one-liner

---

## Quick Start

```bash
# 1. Install
bash install.sh

# 2. Scan
node scripts/scan.js --region ap-northeast-1 --profile prod \
  --out-raw out/raw.json --out-cfn out/cfn-full.yml

# 3. Filter (by tag)
python scripts/select.py \
  --input out/raw.json --tag Environment=prod \
  --exclude-default --emit-regex-filter > out/id-filter.txt

npx former2 filter \
  --input out/cfn-full.yml --output out/cfn-filtered.yml \
  --search-filter "$(cat out/id-filter.txt)"

# 4. Rewrite
python scripts/rewrite_cfn.py \
  --input out/cfn-filtered.yml --output out/cleaned.yml \
  --account-id 123456789012 --source-region ap-northeast-1

# 5. Validate
bash scripts/precheck.sh out/cleaned.yml

# 6. Deploy
aws cloudformation deploy \
  --template-file out/cleaned.yml \
  --stack-name myapp-prod --region us-east-1 \
  --capabilities CAPABILITY_NAMED_IAM
```

---

## Rewrite Rules

| Rule | Before | After |
|------|--------|-------|
| Account ID | `arn:aws:iam::123456789012:role/x` | `!Sub 'arn:aws:iam::${AWS::AccountId}:role/x'` |
| Region | `ap-northeast-1` | `!Sub '${AWS::Region}'` |
| AMI ID | `ami-0abc1234` | `!Ref AmiId` + Parameter |
| AZ | `ap-northeast-1a` | `!Select [0, !GetAZs '']` |
| DeletionPolicy | *(missing)* on RDS/S3 | `DeletionPolicy: Retain` |

---

## Filter Dimensions

```bash
# By service (OR)
python scripts/select.py --input raw.json --service Lambda,IAM

# By tag (AND)
python scripts/select.py --input raw.json --tag Environment=prod --tag Team=backend

# By regex on PhysicalId
python scripts/select.py --input raw.json --regex '^myapp-'

# Exclude default VPC/SG/NACL
python scripts/select.py --input raw.json --exclude-default
```

---

## Requirements

- Node.js >= 18
- Python >= 3.10
- AWS CLI configured (`aws configure`)
- `npm install` (former2)
- `pip install -r requirements.txt` (ruamel.yaml, cfn-lint, pytest)

---

## Tests

```bash
pytest tests/ -v      # 35 tests, all should pass
```

---

## Workflows

- `workflows/export-by-tag.md` — full walkthrough with tag-based filtering

## References

- `references/rewrite-rules.md` — rule documentation
- `references/review-checklist.md` — pre-deploy checklist

---

## Scope (Phase 1)

**Does**: standard-mode scan → filter → rewrite → validate → deploy command  
**Doesn't**: compliance/EC2 mode, LLM rewriting, CDK output, KMS/IAM cross-account (Phase 3+)
