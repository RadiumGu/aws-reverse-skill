# Workflow: Cleanup-Only (In-Place Hardening)

**Use case**: You already own the stack; you only want to harden it against
accidental deletion by adding `DeletionPolicy: Retain` on stateful resources
**without** rewriting account IDs, regions, AMIs, or AZs. The `cleanup-only`
preset is the shortest path.

Typical reason: a former2 export of an existing prod account where the plan is
to re-deploy into the same account/region via CFN (so ARNs / AZs / AMIs must
stay stable), but you want to enforce retention policy.

---

## Prerequisites

```bash
bash install.sh
aws sts get-caller-identity --profile prod
```

---

## Step 1 — Scan

```bash
mkdir -p out
node scripts/scan.js \
  --region ap-northeast-1 \
  --profile prod \
  --out-raw out/raw.json \
  --out-cfn out/cfn-full.yml
```

---

## Step 2 — Preview and filter

```bash
python3 scripts/preview.py --input out/raw.json --group-by service

# Drop the default VPC/SG/NACL so they aren't re-declared accidentally
python3 scripts/select.py \
  --input out/raw.json \
  --exclude-default \
  --dry-run

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

## Step 3 — Rewrite with `cleanup-only`

```bash
python3 scripts/rewrite_cfn.py \
  --input out/cfn-filtered.yml \
  --output out/cleaned.yml \
  --preset cleanup-only
```

`cleanup-only` applies **only** R5 (DeletionPolicy: Retain on
RDS / S3 / DynamoDB / EFS). No account, region, AMI, AZ rewriting — the
template stays byte-for-byte compatible with the source environment except for
deletion semantics.

Confirm the diff is minimal:

```bash
diff out/cfn-filtered.yml out/cleaned.yml | head -40
```

---

## Step 4 — Precheck

```bash
python3 scripts/precheck.py \
  --template out/cleaned.yml \
  --raw out/raw.json \
  --target-region ap-northeast-1 \
  --output out/precheck-report.md
```

Since we did not rewrite anything, the scope check mainly verifies that every
raw resource is still represented.

---

## Step 5 — Generate review (short form)

For an in-place hardening the review is compact — no parameters to fill, no
cross-account decisions:

```bash
python3 scripts/generate_review.py \
  --cleaned out/cleaned.yml \
  --raw out/raw.json \
  --precheck-report out/precheck-report.md \
  --output out/review.md \
  --preset cleanup-only
```

Typical admin edits:
- Uncheck any scratch resources that shouldn't be part of the baseline.
- Sign off on the 🔴 precheck failures (often `cfn-lint` warnings about
  already-existing stateful names — acceptable for `--import` deploys).

---

## Step 6 — Apply review

```bash
python3 scripts/apply_review.py out/review.md \
  --cleaned out/cleaned.yml \
  --out-final out/cleaned-final.yml \
  --out-params out/deploy-params.json \
  --out-manual out/manual-tasks.md \
  --diff out/review.diff
```

`deploy-params.json` will be an empty list `[]` for this workflow — there are
no parameters to override.

---

## Step 7 — Deploy via CFN import (recommended)

Because the resources already exist, use CloudFormation **resource import**
instead of a fresh deploy to avoid creating duplicates:

```bash
aws cloudformation create-change-set \
  --stack-name myapp-hardened \
  --change-set-name import-initial \
  --change-set-type IMPORT \
  --resources-to-import file://out/import-mapping.json \
  --template-body file://out/cleaned-final.yml \
  --capabilities CAPABILITY_NAMED_IAM
```

Build `import-mapping.json` from `raw.json` (one entry per resource with its
existing physical ID). A simple generator:

```bash
python3 - <<'PY'
import json, pathlib
raw = json.loads(pathlib.Path("out/raw.json").read_text())
mapping = [
    {
      "ResourceType": r["Type"],
      "LogicalResourceId": r["PhysicalId"].replace("-", "").title(),
      "ResourceIdentifier": {"BucketName": r["PhysicalId"]} if r["Type"].endswith("::Bucket")
                             else {"InstanceId": r["PhysicalId"]}
    }
    for r in raw["resources"]
    if r["Type"] in {"AWS::S3::Bucket", "AWS::RDS::DBInstance", "AWS::DynamoDB::Table"}
]
pathlib.Path("out/import-mapping.json").write_text(json.dumps(mapping, indent=2))
PY
```

Then `aws cloudformation execute-change-set` to complete the import.

---

## Why not just run `cloudformation deploy`?

Running `deploy` on `cleaned-final.yml` for an account that already has the
resources will fail — CFN can't take over resources it did not create. The
import flow is what gives you retroactive IaC coverage plus `DeletionPolicy:
Retain` protection going forward.

Once imported, subsequent changes go through the normal
`cloudformation deploy` path.
