# Workflow: Export All Non-Default Resources

**Use case**: Reverse-engineer an entire account (or region) into CloudFormation —
skip AWS default VPC/SG/NACL, keep everything else. Useful when you inherit an
account and need an IaC baseline before refactoring.

---

## Prerequisites

```bash
bash install.sh
aws sts get-caller-identity --profile source
```

---

## Step 1 — Scan

```bash
mkdir -p out

node scripts/scan.js \
  --region ap-northeast-1 \
  --profile source \
  --out-raw out/raw.json \
  --out-cfn out/cfn-full.yml
```

If you re-run this while iterating on filters, the scan result is cached in
`~/.cache/aws-reverse-skill/`. Use `python3 scripts/cache.py info` to inspect
or `python3 scripts/cache.py purge` to reset.

---

## Step 2 — Preview what was found

Group by AWS service so you can sanity-check the scan before filtering:

```bash
python3 scripts/preview.py --input out/raw.json --group-by service --sample 3
```

Then peek at tags if you plan to filter by environment later:

```bash
python3 scripts/preview.py --input out/raw.json \
  --group-by tag --tag-key Environment --sample 2 --with-tags
```

---

## Step 3 — Dry-run the filter

Iterate without writing files. We want everything except AWS defaults:

```bash
python3 scripts/select.py \
  --input out/raw.json \
  --exclude-default \
  --dry-run
```

When the summary looks right, execute for real:

```bash
python3 scripts/select.py \
  --input out/raw.json \
  --output out/filtered.json \
  --exclude-default \
  --emit-regex-filter > out/id-filter.txt
```

---

## Step 4 — Scope former2 output

```bash
npx former2 filter \
  --input out/cfn-full.yml \
  --output out/cfn-filtered.yml \
  --search-filter "$(cat out/id-filter.txt)"
```

---

## Step 5 — Rewrite with the `full` preset

```bash
python3 scripts/rewrite_cfn.py \
  --input out/cfn-filtered.yml \
  --output out/cleaned.yml \
  --account-id $(aws sts get-caller-identity --profile source --query Account --output text) \
  --source-region ap-northeast-1 \
  --preset full \
  --review-decisions out/review-decisions.json
```

`--preset full` turns on every rewrite rule (R1-R10). Flagged items (Peering,
cross-account IAM trust) land in `out/review-decisions.json` for the review
step.

---

## Step 6 — Deep precheck

```bash
python3 scripts/precheck.py \
  --template out/cleaned.yml \
  --raw out/raw.json \
  --target-region ap-northeast-1 \
  --output out/precheck-report.md
```

`--deep` enables live AWS-API checks (AMI / KMS / S3 / quotas). Skip it for a
local-only lint if you don't have target-account credentials yet.

---

## Step 7 — Generate review.md + deployment advice

```bash
python3 scripts/generate_review.py \
  --cleaned out/cleaned.yml \
  --raw out/raw.json \
  --precheck-report out/precheck-report.md \
  --output out/review.md \
  --preset full

python3 scripts/deployment_advice.py \
  --input out/raw.json \
  --output out/deployment-advice.md \
  --stack-name full-export
```

---

## Step 8 — Admin edits review.md

The skill stops here and waits for the admin to:
1. Fill in every `**<请填>**` parameter value.
2. Uncheck resources they don't want to deploy.
3. Pick options in the ⚠️ decision blocks (peering IDs, IAM trust decisions…).
4. Clear the 🔴 precheck failures or mark them as accepted risk.

---

## Step 9 — Apply the edited review

```bash
python3 scripts/apply_review.py out/review.md \
  --cleaned out/cleaned.yml \
  --out-final out/cleaned-final.yml \
  --out-params out/deploy-params.json \
  --out-manual out/manual-tasks.md \
  --diff out/review.diff
```

---

## Step 10 — Re-run precheck against the final template

```bash
python3 scripts/precheck.py \
  --template out/cleaned-final.yml \
  --raw out/raw.json \
  --output out/precheck-final.md
```

If failures remain, regenerate review with `--update` to preserve filled
parameter values and loop back to Step 8.

---

## Step 11 — Deploy

```bash
aws cloudformation deploy \
  --template-file out/cleaned-final.yml \
  --stack-name full-export \
  --region ap-northeast-1 \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides $(python3 -c "import json; print(' '.join(f\"{p['ParameterKey']}={p['ParameterValue']}\" for p in json.load(open('out/deploy-params.json'))))")
```

Archive `out/review.md`, `out/review.diff`, `out/manual-tasks.md`, and
`out/deployment-advice.md` with the deployment ticket for audit.
