# aws-reverse-skill

**Version**: 2.0 (Phase 2 — Review Loop + Deployment Advice)
**Host**: Claude Code / Kiro / OpenClaw (AgentSkill spec)

---

## Trigger Keywords

Activate this skill when the user says any of:

- "导出 AWS 资源到 CFN"
- "生成 CloudFormation" / "生成 CFN"
- "IaC 化现有环境"
- "reverse engineer AWS"
- "former2 扫描"
- "把现有资源转成模板"
- "export AWS resources"
- "generate CloudFormation template"
- "replicate AWS environment"
- "跨账号部署" / "cross-account deploy"
- "跨 region DR" / "disaster recovery"

---

## What Phase 2 Adds

Phase 1 shipped a 7-step pipeline (scan → filter → rewrite → precheck →
deploy). Phase 2 wraps that pipeline in an **admin review loop** and an
**automatic deployment advice generator**, so non-Claude operators (compliance,
ops, DBA) can sign off on every change before CloudFormation touches the
target account.

New scripts:
- `scripts/preview.py` — group raw.json by service / tag / region for sanity-check
- `scripts/select.py --dry-run` — iterate filters without writing files
- `scripts/rewrite_cfn.py --preset` — pick one of 4 rule presets
- `scripts/precheck.py` — deep AWS-API precheck (AMI / KMS / S3 / quotas) with injectable boto3 client
- `scripts/generate_review.py` — produce `review.md` for admin sign-off
- `scripts/apply_review.py` — parse edited `review.md` back into CFN artifacts
- `scripts/deployment_advice.py` — `md` / `checklist` / `json` advice per PRD §10
- `scripts/cache.py` — 24h TTL cache for raw.json

---

## Standard Mode Flow (11 Steps, with Review Loop)

### Step 1 — Confirm parameters

```
Source region?        e.g. ap-northeast-1
AWS CLI profile?      e.g. source / prod
Services to scan?     e.g. Lambda,IAM — or "all"
Filter by tag?        e.g. Environment=prod
Target region?        e.g. us-east-1
Source account ID?    12-digit
Target account ID?    12-digit (same if in-place)
Stack name?           e.g. myapp-prod
Preset?               cleanup-only / cross-region / cross-account / full
```

### Step 2 — Scan (with cache)

```bash
node scripts/scan.js \
  --region <source-region> \
  --services <services> \
  --profile <profile> \
  --out-raw out/raw.json \
  --out-cfn out/cfn-full.yml
```

Re-runs within 24h hit `~/.cache/aws-reverse-skill/`.
Inspect via `python3 scripts/cache.py info`; purge via `python3 scripts/cache.py purge`.

### Step 3 — Preview

Show the admin what the scan returned before committing to filters:

```bash
python3 scripts/preview.py --input out/raw.json --group-by service --sample 3
python3 scripts/preview.py --input out/raw.json --group-by tag --tag-key Environment
```

### Step 4 — Filter with dry-run confirmation

Iterate first:

```bash
python3 scripts/select.py \
  --input out/raw.json \
  --tag <tag-filter> \
  --exclude-default \
  --dry-run
```

Commit once the summary looks right:

```bash
python3 scripts/select.py \
  --input out/raw.json \
  --output out/filtered.json \
  --tag <tag-filter> \
  --exclude-default \
  --emit-regex-filter > out/id-filter.txt

npx former2 filter \
  --input out/cfn-full.yml \
  --output out/cfn-filtered.yml \
  --search-filter "$(cat out/id-filter.txt)"
```

### Step 5 — Rewrite (pick a preset)

```bash
python3 scripts/rewrite_cfn.py \
  --input out/cfn-filtered.yml \
  --output out/cleaned.yml \
  --account-id <source-account-id> \
  --source-region <source-region> \
  --preset <cleanup-only|cross-region|cross-account|full> \
  --review-decisions out/review-decisions.json
```

| Preset | When to use | Rules enabled |
|--------|-------------|---------------|
| `cleanup-only` | In-place hardening, import into CFN | R5 only |
| `cross-region` | Same account, different region (DR) | R2 R3 R4 R5 |
| `cross-account` | Move to a different account | R1 R2 R3 R4 R5 R6 R9 R10 R7(flag) |
| `full` | Unknown / maximum coverage | All R1-R10 |

### Step 6 — Precheck

```bash
python3 scripts/precheck.py \
  --template out/cleaned.yml \
  --raw out/raw.json \
  --target-region <target-region> \
  --target-account <target-account-id> \
  --output out/precheck-report.md \
  --deep
```

`--deep` adds AWS-API checks (AMI availability, KMS CMK existence, S3 global
names, Service Quotas, validate-template). Omit `--deep` if target creds
aren't available yet.

### Step 7 — Generate review.md + deployment-advice.md

```bash
python3 scripts/generate_review.py \
  --cleaned out/cleaned.yml \
  --raw out/raw.json \
  --precheck-report out/precheck-report.md \
  --output out/review.md \
  --stack-name <stack-name> \
  --preset <preset> \
  --source "<acct>/<region>" \
  --target "<acct>/<region>" \
  --source-account <source-account-id>

python3 scripts/deployment_advice.py \
  --input out/raw.json \
  --output out/deployment-advice.md \
  --stack-name <stack-name>
```

`review.md` contains 4 blocks:
- 🟢 Parameters (target values to fill)
- 🟡 Resources (checkboxes to deselect)
- ⚠️ 需人工决策 (Peering / IAM trust / KMS / data migration)
- 🔴 预检失败项 (from precheck-report.md)

### Step 8 — Admin reviews + edits `out/review.md`

**The skill stops here and waits for the admin.** Claude must:
1. Surface the review.md path and the deployment-advice.md path.
2. Summarize the 4 sections and highlight ⚠️ decisions.
3. Wait for the admin to return an edited review.md (pasted back, committed,
   or path re-confirmed).

Do not proceed to Step 9 until the admin confirms the review is edited. This
is the human checkpoint that the whole workflow is built around.

### Step 9 — Apply the edited review

```bash
python3 scripts/apply_review.py out/review.md \
  --cleaned out/cleaned.yml \
  --out-final out/cleaned-final.yml \
  --out-params out/deploy-params.json \
  --out-manual out/manual-tasks.md \
  --diff out/review.diff
```

Outputs:
- `cleaned-final.yml` — resources removed per unchecked boxes; decisions applied.
- `deploy-params.json` — CloudFormation `--parameter-overrides` payload.
- `manual-tasks.md` — steps the skill cannot auto-execute.
- `review.diff` — unified diff cleaned → cleaned-final for audit.

### Step 10 — Re-precheck (loop until clean)

```bash
python3 scripts/precheck.py \
  --template out/cleaned-final.yml \
  --raw out/raw.json \
  --target-region <target-region> \
  --output out/precheck-final.md \
  --deep
```

If failures remain, re-run `generate_review.py --update` to produce a new
review.md that preserves filled parameter values and checkbox state, then loop
back to **Step 8**. The `--update` mode keeps the admin's prior decisions.

### Step 11 — Deploy + audit archive

```bash
aws cloudformation deploy \
  --template-file out/cleaned-final.yml \
  --stack-name <stack-name> \
  --region <target-region> \
  --profile <target-profile> \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides $(python3 -c "
import json
print(' '.join(f\"{p['ParameterKey']}={p['ParameterValue']}\" for p in json.load(open('out/deploy-params.json'))))
")
```

Archive these with the change ticket:
- `out/review.md` (edited) + `out/review.diff`
- `out/manual-tasks.md`
- `out/deployment-advice.md`
- `out/precheck-final.md`
- `out/audit-<timestamp>.log` (step-by-step record)

Work through `manual-tasks.md` and the relevant sections of
`deployment-advice.md` to complete data migration, peering, AMI copy, etc.

---

## Review Loop Quick Reference

```
Step 7  generate_review.py           → out/review.md   ← skill writes
Step 8  (admin edits review.md)      ← skill pauses
Step 9  apply_review.py              → cleaned-final   ← skill reads admin edits
Step 10 precheck.py --deep            → precheck-final
        ├── fail → generate_review.py --update → back to Step 8
        └── pass → Step 11
Step 11 deploy + archive review + diff + manual-tasks
```

---

## Error Handling

| Situation | Response |
|-----------|----------|
| `former2: command not found` | "Run `npm install` in the skill directory first." |
| AWS credentials not configured | "Configure AWS CLI: `aws configure` or set `AWS_PROFILE`." |
| `cfn-lint` finds errors | Surface the output in `precheck-report.md`; will appear in review 🔴 block. |
| `validate-template` fails | Precheck marks FAIL; admin must fix before apply_review. |
| Account ID not provided | Skip R1; warn user to check ARNs manually. |
| No resources after filter | Re-run `select.py --dry-run` until summary is sensible. |
| Preset unknown | List `cleanup-only / cross-region / cross-account / full` and re-ask. |
| `review.md` parsing error in apply_review | Skill prints the offending line; admin fixes and re-runs. |
| `review.md` has unfilled `<请填>` | apply_review emits them as empty Parameter values — re-open review. |

---

## Scope Limitations (Phase 2)

- **Standard mode only** — no EC2/SSM compliance mode (Phase 3)
- **No LLM rewriting** — deterministic rules only
- **No CDK output** — CFN YAML only
- **KMS cross-account grants** — flagged in review, not auto-rewritten
- **Data migration** — `deployment-advice.md` emits commands but does not run them
- **Peering handshake** — emitted as manual task; not executed
- **review.md schema is strict** — admins must keep the 4-block layout intact

---

## File Layout

```
aws-reverse-skill/
├── SKILL.md                ← this file (v2)
├── scripts/
│   ├── scan.js             ← Step 2
│   ├── cache.py            ← scan cache helper
│   ├── preview.py          ← Step 3
│   ├── select.py           ← Step 4 (with --dry-run)
│   ├── rewrite_cfn.py      ← Step 5 (with --preset)
│   ├── precheck.py         ← Step 6 + Step 10 (deep AWS checks)
│   ├── precheck.sh         ← legacy lint wrapper
│   ├── generate_review.py  ← Step 7 (also --update for Step 10 loop)
│   ├── apply_review.py     ← Step 9
│   └── deployment_advice.py ← Step 7 companion
├── config/
│   ├── default-filters.json
│   ├── default-rewrites.json
│   └── rewrite-presets.json ← 4 presets
├── references/
│   ├── rewrite-rules.md
│   └── review-checklist.md
├── workflows/
│   ├── export-by-tag.md
│   ├── export-all.md            ← new
│   ├── cross-account-deploy.md  ← new — showcases review loop
│   ├── cross-region-dr.md       ← new — showcases deployment-advice
│   └── cleanup-default-only.md  ← new
└── tests/                   ← ≥70 pytest cases
```
