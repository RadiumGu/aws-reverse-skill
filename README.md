# aws-reverse-skill

Reverse-engineer existing AWS environments into deployable CloudFormation templates.

**Version 3** — Phase 3 ships **Mode A (Standard)** + **Mode B (In-Account,
compliance-friendly)** with an 11-step review loop, deployment-advice
generator, audit log, and multi-partition (commercial / China / GovCloud)
support.

> **New here?** Start with the [Quick Start guide](docs/quick-start.md) — scan → filter → rewrite → deploy in 5 minutes.

See [SKILL.md](SKILL.md) for the full skill specification consumed by Claude
Code / Kiro / OpenClaw agents.

---

## What It Does

```
AWS Account
   │
   ▼
[1] Scan         former2 (local CLI — Mode A)  or  Former2 UI on EC2 (Mode B)
   │
   ▼
[2] Preview      preview.py — inspect raw.json by service / tag / region
   │
   ▼
[3] Filter       select.py — by service / tag / regex, strip default VPC/SG
   │
   ▼
[4] Rewrite      rewrite_cfn.py — R1-R15 rules with cross-account / cross-region presets
   │
   ▼
[5] Precheck     precheck.py --deep — AMI / KMS / S3 / quotas / validate-template
   │
   ▼
[6] Review loop  generate_review.py → admin edits review.md → apply_review.py
   │             (loop on precheck failures)
   ▼
[7] Deploy       aws cloudformation deploy + audit archive
```

---

## Two Modes

| Mode | Use when | Scanner lives on |
|------|----------|------------------|
| **A — Standard** (default) | Everyday reverse-engineering, bootstrap IaC, cross-account / cross-region migration | Your laptop (`npm` former2) |
| **B — In-Account** | Credentials must not leave the account; VPC-only / no-egress laptop; audit requires an in-VPC scanner | EC2 in target VPC + SSM port-forward |

Mode B reuses Mode A steps 4-11 verbatim — only the scan step differs.

---

## Quick Start — Mode A (Standard)

```bash
# 1. Install (locks node + python deps to exact versions)
bash install.sh

# 2. Scan (cached for 24h under ~/.cache/aws-reverse-skill/)
node scripts/scan.js \
  --region ap-northeast-1 --profile prod \
  --out-raw out/raw.json --out-cfn out/cfn-full.yml

# 3. Preview — what did former2 actually find?
python3 scripts/preview.py --input out/raw.json --group-by service --sample 3

# 4. Filter (iterate with --dry-run, then commit)
python3 scripts/select.py \
  --input out/raw.json --tag Environment=prod \
  --exclude-default --dry-run

python3 scripts/select.py \
  --input out/raw.json --output out/filtered.json \
  --tag Environment=prod --exclude-default \
  --emit-regex-filter > out/id-filter.txt

npx former2 filter \
  --input out/cfn-full.yml --output out/cfn-filtered.yml \
  --search-filter "$(cat out/id-filter.txt)"

# 5. Rewrite with a preset
python3 scripts/rewrite_cfn.py \
  --input out/cfn-filtered.yml --output out/cleaned.yml \
  --account-id 123456789012 --source-region ap-northeast-1 \
  --preset cross-account \
  --review-decisions out/review-decisions.json

# 6. Precheck (add --deep once target creds are available)
python3 scripts/precheck.py \
  --template out/cleaned.yml --raw out/raw.json \
  --target-region us-east-1 --target-account 999999999999 \
  --output out/precheck-report.md --deep

# 7. Generate review.md + deployment-advice.md for admin sign-off
python3 scripts/generate_review.py \
  --cleaned out/cleaned.yml --raw out/raw.json \
  --precheck-report out/precheck-report.md \
  --output out/review.md --stack-name myapp-prod \
  --preset cross-account \
  --source "123456789012/ap-northeast-1" \
  --target "999999999999/us-east-1" \
  --source-account 123456789012

python3 scripts/deployment_advice.py \
  --input out/raw.json --output out/deployment-advice.md \
  --stack-name myapp-prod

# 8. *** Admin edits out/review.md *** (parameters + checkboxes + decisions)

# 9. Apply the admin's edits
python3 scripts/apply_review.py out/review.md \
  --cleaned out/cleaned.yml \
  --out-final out/cleaned-final.yml \
  --out-params out/deploy-params.json \
  --out-manual out/manual-tasks.md \
  --diff out/review.diff

# 10. Re-precheck — loop back to step 7 with --update if failures remain
python3 scripts/precheck.py \
  --template out/cleaned-final.yml --raw out/raw.json \
  --target-region us-east-1 \
  --output out/precheck-final.md --deep

# 11. Deploy + archive
aws cloudformation deploy \
  --template-file out/cleaned-final.yml --stack-name myapp-prod \
  --region us-east-1 --profile target --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides $(python3 -c "
import json
print(' '.join(f\"{p['ParameterKey']}={p['ParameterValue']}\" for p in json.load(open('out/deploy-params.json'))))")
```

To emit an end-to-end audit JSON for the whole pipeline, export
`AWS_REVERSE_OPERATOR` and `AWS_REVERSE_AUDIT_LOG` before running any script:

```bash
export AWS_REVERSE_OPERATOR=alice@example.com
export AWS_REVERSE_AUDIT_LOG=out/audit-$(date -u +%Y%m%dT%H%M%SZ).json
# ...run pipeline...
```

Every instrumented stage appends one entry with SHA-256 digests of its
inputs / outputs. The resulting JSON is replayable and audit-friendly.

---

## Quick Start — Mode B (In-Account)

```bash
# B1. Deploy the hardened Former2 EC2 stack
#     - SecurityAudit + Deny on secretsmanager/ssm/kms IAM policy
#     - Private subnet, zero ingress SG, IMDSv2 required
#     - VPC endpoints for ssm / ssmmessages / ec2messages / logs / s3
bash scripts/deploy-former2.sh \
  --vpc-id vpc-xxxxxxxx --subnet-id subnet-xxxxxxxx \
  --stack-name former2-reverse --region ap-northeast-1 --profile compliance

# B2. Start the SSM port-forward tunnel
bash scripts/ssm-portforward.sh \
  --instance-id i-0123456789abcdef0 --region ap-northeast-1 \
  --local-port 8080 --remote-port 80 --profile compliance

# B3. Open http://localhost:8080 in a browser and run the Former2 UI scan.
#     Save the exported CFN YAML on the EC2 instance to
#     /home/ec2-user/former2-exports/latest.

# B4. Fetch the export off the EC2 instance (auto-creates a hardened S3
#     transfer bucket with a principal-restricted bucket policy).
bash scripts/fetch-export.sh \
  --instance-id i-0123456789abcdef0 --region ap-northeast-1 \
  --local out/cfn-from-former2.yml --profile compliance

# B5 onwards — identical to Mode A steps 3-11, reusing out/cfn-from-former2.yml
#     in place of out/cfn-full.yml.

# B-teardown — delete the scanner stack once the export is safely archived.
bash scripts/teardown.sh --stack-name former2-reverse --region ap-northeast-1
```

Full walkthrough: [workflows/in-account-mode.md](workflows/in-account-mode.md).

---

## Rewrite Rules (R1–R15)

| # | Rule | Before | After |
|---|------|--------|-------|
| R1 | Account ID | `arn:aws:iam::123456789012:role/x` | `!Sub 'arn:aws:iam::${AWS::AccountId}:role/x'` |
| R2 | Region | `ap-northeast-1` | `!Sub '${AWS::Region}'` |
| R3 | AMI ID | `ami-0abc1234` | `!Ref AmiId` (SSM parameter) |
| R4 | AZ | `ap-northeast-1a` | `!Select [0, !GetAZs '']` |
| R5 | DeletionPolicy | *(missing)* on RDS / S3 / DynamoDB | `DeletionPolicy: Retain` |
| R6 | KMS key ARN | Fixed CMK ARN | Parameter + !Ref |
| R7 | Peering / TGW | Flagged for review | Commented block |
| R9 | Route53 / hosted zone | Inline ID | Parameter |
| R10 | Cross-account ARN | External acct 999... | `${ExternalXxxArn<hash>}` parameter |
| R11 | VPC / Subnet / SG IDs | Hardcoded `vpc-*` / `subnet-*` | `!Ref TargetVpcId` / `TargetSubnetIds` |
| R12 | Source VPC CIDR | `10.0.0.0/16` in SG rule | Parameter |
| R13 | String-embedded ARN | `message = "arn:aws:…"` | Rewritten inline |
| R14 | JSON-embedded ARN | Step Functions / Lambda env | Rewritten inline |
| R15 | Region-locked resource | CloudFront / WAFv2 / Lambda@Edge | Flagged |

**Presets** (`--preset`):

| Preset | Rules enabled | Use when |
|--------|---------------|----------|
| `cleanup-only` | R5 | In-place hardening, import into CFN |
| `cross-region` | R2 R3 R4 R5 | Same account, different region (DR) |
| `cross-account` | R1 R2 R3 R4 R5 R6 R9 R10 (+ R7 flag) | Move to a different account |
| `full` | All R1-R15 | Unknown / maximum coverage |

---

## Multi-Partition Support

`aws`, `aws-cn`, `aws-us-gov` partitions are supported for both ARN rewriting
and region substitution. Supported partition regions:

| Partition | Regions |
|-----------|---------|
| `aws` | All commercial regions (us-east-1, ap-northeast-1, eu-central-1, …) |
| `aws-cn` | `cn-north-1`, `cn-northwest-1` |
| `aws-us-gov` | `us-gov-west-1`, `us-gov-east-1` |

Use `scripts.rewrite_cfn.partition_for_region(region)` to resolve a region
to its partition programmatically.

---

## Review Loop Quick Reference

```
Step 7  generate_review.py           → out/review.md       ← skill writes
Step 8  (admin edits review.md)      ← skill pauses here
Step 9  apply_review.py              → cleaned-final.yml   ← skill reads edits
Step 10 precheck.py --deep            → precheck-final.md
        ├── fail → generate_review.py --update → back to Step 8
        └── pass → Step 11
Step 11 deploy + archive
```

`review.md` contains four blocks:

- 🟢 **Parameters** — target values for operator to fill
- 🟡 **Resources** — checkboxes to deselect resources from the deploy
- ⚠️ **需人工决策** — peering / IAM trust / KMS / data migration
- 🔴 **预检失败项** — precheck failures that must be resolved

---

## Filter Dimensions

```bash
# By service (OR)
python3 scripts/select.py --input raw.json --service Lambda,IAM

# By tag (AND)
python3 scripts/select.py --input raw.json --tag Environment=prod --tag Team=backend

# By regex on PhysicalId
python3 scripts/select.py --input raw.json --regex '^myapp-'

# Exclude default VPC/SG/NACL
python3 scripts/select.py --input raw.json --exclude-default

# Iterate filters without writing output
python3 scripts/select.py --input raw.json --tag Environment=prod --dry-run
```

---

## Requirements

- Node.js >= 18
- Python >= 3.10
- AWS CLI v2 configured (`aws configure`)
- `npm ci` (installs former2 from `package-lock.json`)
- `pip install -r requirements-lock.txt` (ruamel.yaml, cfn-lint, pytest — exact versions)

Both dependency files are version-locked so pipeline results are
byte-for-byte reproducible across machines.

---

## Tests

```bash
pytest tests/ -v        # 190+ tests, all green
```

---

## Layout

```
scripts/
├── scan.js                 # Mode A: former2 wrapper
├── deploy-former2.sh       # Mode B: deploy EC2 scanner stack
├── ssm-portforward.sh      # Mode B: SSM tunnel
├── fetch-export.sh         # Mode B: pull CFN YAML off EC2 via hardened S3 bucket
├── teardown.sh             # Mode B: delete scanner stack
├── preview.py              # raw.json inspection
├── select.py               # service / tag / regex filter
├── rewrite_cfn.py          # R1-R15 rewrite engine + presets
├── arn_rewriter.py         # pure ARN scanner / rewriter (used by R10/R13/R14)
├── region_lock.py          # R15 (CloudFront / WAFv2 / Lambda@Edge)
├── cidr_analyzer.py        # R12 helper
├── precheck.py             # shallow + deep AWS-API precheck
├── precheck.sh             # legacy one-shot precheck
├── generate_review.py      # builds review.md for admin sign-off
├── apply_review.py         # parses edited review.md back into CFN artifacts
├── deployment_advice.py    # md / checklist / json advice
├── cache.py                # 24h TTL cache for raw.json
├── audit.py                # pipeline audit log (AWS_REVERSE_AUDIT_LOG)
└── ...

references/
├── former2-cfn.yaml        # Mode B EC2 stack (hardened)
├── rewrite-rules.md        # Rule reference
└── review-checklist.md     # Pre-deploy checklist

workflows/
├── export-by-tag.md        # Mode A walkthrough
└── in-account-mode.md      # Mode B walkthrough
```

---

## Scope

**Does**: Mode A + Mode B scan → filter → rewrite (R1-R15) → deep precheck →
review loop → deployment advice → deploy → audit archive. Multi-partition
(commercial / China / GovCloud). Cross-account / cross-region / in-place
cleanup. Hardened in-VPC scanner with zero-ingress EC2 and Deny-on-secrets
IAM role.

**Doesn't**: LLM-based rewrites, CDK output, data migration automation
(surfaces it in `manual-tasks.md` instead).
