# Workflow: Cross-Account Deployment (with Review Loop)

**Use case**: Copy production resources from Account A (source) to Account B
(target). This is the canonical showcase of the **review.md closed loop** — the
admin signs off on every parameter, resource, and decision before any AWS call
is made in the target account.

---

## Prerequisites

```bash
bash install.sh
aws sts get-caller-identity --profile source   # Account A
aws sts get-caller-identity --profile target   # Account B
```

---

## Step 1 — Scan source

```bash
mkdir -p out

node scripts/scan.js \
  --region ap-northeast-1 \
  --profile source \
  --out-raw out/raw.json \
  --out-cfn out/cfn-full.yml
```

---

## Step 2 — Preview + dry-run filter

```bash
python3 scripts/preview.py --input out/raw.json --group-by service --sample 3

python3 scripts/select.py \
  --input out/raw.json \
  --tag Project=myapp \
  --tag Environment=prod \
  --exclude-default \
  --dry-run
```

Tighten filters until the dry-run summary lists exactly what should move. Then
commit:

```bash
python3 scripts/select.py \
  --input out/raw.json \
  --output out/filtered.json \
  --tag Project=myapp --tag Environment=prod \
  --exclude-default \
  --emit-regex-filter > out/id-filter.txt

npx former2 filter \
  --input out/cfn-full.yml \
  --output out/cfn-filtered.yml \
  --search-filter "$(cat out/id-filter.txt)"
```

---

## Step 3 — Rewrite with the `cross-account` preset

```bash
python3 scripts/rewrite_cfn.py \
  --input out/cfn-filtered.yml \
  --output out/cleaned.yml \
  --account-id 123456789012 \
  --source-region ap-northeast-1 \
  --preset cross-account \
  --review-decisions out/review-decisions.json
```

The `cross-account` preset enables:
- R1/R2/R3/R4/R5 (Phase 1 baseline)
- R6 KMS ARN → `!Ref KmsKeyArn`
- R9 IAM principal external-account flag
- R10 S3 bucket name prefix
- R7 peering review flag (no auto-rewrite)

---

## Step 4 — Deep precheck against target

```bash
python3 scripts/precheck.py \
  --template out/cleaned.yml \
  --raw out/raw.json \
  --target-region ap-northeast-1 \
  --target-account 999988887777 \
  --output out/precheck-report.md \
  --deep
```

`--deep` will call AWS APIs (via the injected boto3 client) to check AMI / KMS
CMK / S3 bucket name / quotas. Use the `target` profile:

```bash
AWS_PROFILE=target python3 scripts/precheck.py ...  --deep
```

---

## Step 5 — Generate `review.md` and `deployment-advice.md`

```bash
python3 scripts/generate_review.py \
  --cleaned out/cleaned.yml \
  --raw out/raw.json \
  --precheck-report out/precheck-report.md \
  --output out/review.md \
  --stack-name myapp-prod-cross-account \
  --preset cross-account \
  --source "123456789012 / ap-northeast-1" \
  --target "999988887777 / ap-northeast-1" \
  --source-account 123456789012

python3 scripts/deployment_advice.py \
  --input out/raw.json \
  --output out/deployment-advice.md \
  --stack-name myapp-prod-cross-account
```

The review document has four sections:
- 🟢 Parameters (AmiId / KmsKeyArn / BucketPrefix …) — **admin fills target
  values**.
- 🟡 Resources (checkbox for each logical id) — **admin unchecks anything that
  shouldn't move**.
- ⚠️ 需人工决策 — cross-account IAM trust, Peering IDs, KMS cross-account refs.
- 🔴 预检失败项 — lifted from `precheck-report.md`.

---

## Step 6 — Admin review loop (the key step)

The skill **stops** and hands `out/review.md` to the admin or compliance
reviewer. A typical edit looks like:

```diff
-| KmsKeyArn | String | `` | **<请填>** | ... |
+| KmsKeyArn | String | `` | arn:aws:kms:ap-northeast-1:999988887777:key/cafe...feed | ... |

-- [x] IAM::Role / MyappExternalRole
+- [ ] IAM::Role / MyappExternalRole

  ### D.iam-trust.MyappExternalRole — IAM trust policy 含外部账号 999988887777
  ...
-  TrustedAccountId: 999988887777 (keep/replace/remove)
+  TrustedAccountId: keep
```

Save the file. The admin can pause here as long as needed — there is no state
kept in memory.

---

## Step 7 — Apply review + regenerate artifacts

```bash
python3 scripts/apply_review.py out/review.md \
  --cleaned out/cleaned.yml \
  --out-final out/cleaned-final.yml \
  --out-params out/deploy-params.json \
  --out-manual out/manual-tasks.md \
  --diff out/review.diff
```

Artifacts:
- `cleaned-final.yml` — unchecked resources removed, decisions applied.
- `deploy-params.json` — ready for `--parameter-overrides`.
- `manual-tasks.md` — everything the skill could not auto-apply (runbook).
- `review.diff` — unified diff for audit archiving.

---

## Step 8 — Re-precheck the final template

```bash
AWS_PROFILE=target python3 scripts/precheck.py \
  --template out/cleaned-final.yml \
  --target-region ap-northeast-1 \
  --target-account 999988887777 \
  --output out/precheck-final.md \
  --deep
```

If failures remain:

```bash
# Regenerate review.md keeping already-filled values
python3 scripts/generate_review.py \
  --cleaned out/cleaned-final.yml \
  --raw out/raw.json \
  --precheck-report out/precheck-final.md \
  --output out/review.md \
  --update
```

Then loop back to **Step 6**. Repeat until precheck is clean (or failures are
explicitly accepted in `manual-tasks.md`).

---

## Step 9 — Deploy in the target account

```bash
AWS_PROFILE=target aws cloudformation deploy \
  --template-file out/cleaned-final.yml \
  --stack-name myapp-prod \
  --region ap-northeast-1 \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides $(python3 -c "
import json
print(' '.join(f\"{p['ParameterKey']}={p['ParameterValue']}\" for p in json.load(open('out/deploy-params.json'))))
")
```

---

## Step 10 — Complete the runbook

`manual-tasks.md` lists the out-of-band steps. Typical ones for cross-account:
1. Target account accepts VPC Peering (`aws ec2 accept-vpc-peering-connection`).
2. Source account updates CMK key policy to grant target-account principal.
3. Data migration (RDS DMS / S3 CRR / DynamoDB Global Tables) per
   `deployment-advice.md`.
4. Update DNS / ACM / external callers to the target stack.

Archive `out/review.md`, `out/review.diff`, `out/manual-tasks.md`, and
`out/deployment-advice.md` with the change ticket.
