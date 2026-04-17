# aws-reverse-skill

**Version**: 3.0 (Phase 3 — Mode A Standard + Mode B In-Account)
**Host**: Claude Code / Kiro / OpenClaw (AgentSkill spec)

---

## Trigger Keywords

Activate this skill when the user says any of:

### Mode A — Standard (default)

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

### Mode B — In-Account Deployment

- "in-account mode"
- "deploy former2 inside the account"
- "run former2 on EC2"
- "SSM port forward"
- "VPC-only workflow"
- "in-account scan"

---

## Mode Decision Tree

```
Does the user need the scan tooling to live inside the target AWS account
(e.g. credentials must not leave the account, ops/audit requires the scanner
to be an auditable in-VPC asset, or the account is in a regulated boundary)?

  ├── No  → Mode A — Standard (local npm CLI, 11 steps)
  │           Default for everyday reverse-engineering and IaC bootstrap.
  │
  └── Yes → Mode B — In-Account Deployment (EC2 + SSM, 8 steps)
              Choose only when the team prefers or requires keeping the
              scanner on an account-owned EC2 instance for operational or
              auditing reasons. Mode B reuses Mode A steps 4-11 verbatim.
```

Default to Mode A. Switch to Mode B only when the user explicitly requests
it or states a constraint (compliance boundary, no-egress laptop policy,
audit requirement) that Mode A cannot satisfy.

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

## Mode A — Standard Flow (11 Steps, with Review Loop)

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

## Mode B — In-Account Deployment Flow (8 Steps)

Mode B runs the scanner on an EC2 instance inside the target AWS account.
The operator reaches the Former2 UI through an SSM port-forward tunnel, so
no AWS credentials ever leave the account and the laptop never opens an
inbound or direct outbound AWS API path. Once the CFN YAML has been
exported off the EC2 instance, the rest of the pipeline is identical to
Mode A (select → rewrite → precheck → review loop → apply → deploy).

The full walkthrough, including troubleshooting and operational checklist,
is in [workflows/in-account-mode.md](workflows/in-account-mode.md).

### Step B1 — Confirm parameters

```
Source region?             e.g. ap-northeast-1
Target region?             e.g. ap-northeast-1 (may equal source)
AWS CLI profile?           e.g. compliance / target
Source VPC id?             vpc-xxxxxxxxxxxxxxxxx (must have DNS hostnames on)
Source private subnet id?  subnet-xxxxxxxxxxxxxxxxx (no route to IGW)
Stack name?                e.g. former2-reverse
Instance type?             t4g.medium (default)
Create VPC endpoints?      Yes (default) / No (VPC already has them)
```

Verify `aws sts get-caller-identity` and `session-manager-plugin --version`
return cleanly before continuing.

### Step B2 — Deploy the Former2 EC2 stack (idempotent)

```bash
bash scripts/deploy-former2.sh \
  --vpc-id vpc-xxxxxxxx \
  --subnet-id subnet-xxxxxxxx \
  --stack-name former2-reverse \
  --region ap-northeast-1 \
  --profile compliance
```

Skip if `aws cloudformation describe-stacks --stack-name former2-reverse`
already returns `CREATE_COMPLETE` / `UPDATE_COMPLETE`. The stack's
`Outputs.InstanceId` is the target for every later step. If the VPC already
has ssm / ssmmessages / ec2messages / logs / s3 endpoints, add
`--create-endpoints No` to avoid duplicate endpoints.

Template: [references/former2-cfn.yaml](references/former2-cfn.yaml).

### Step B3 — Start the SSM port-forward tunnel

```bash
bash scripts/ssm-portforward.sh \
  --instance-id i-0123456789abcdef0 \
  --region ap-northeast-1 \
  --local-port 8080 \
  --remote-port 80 \
  --profile compliance
```

This script runs in the foreground. Keep the terminal open for the full
scan; Ctrl+C tears the tunnel down. Open a second terminal for Steps B4
and B6.

### Step B4 — Fetch IMDS credentials on the EC2 instance

Start an interactive SSM shell in a separate terminal:

```bash
aws ssm start-session \
  --target i-0123456789abcdef0 \
  --region ap-northeast-1 \
  --profile compliance
```

Inside the session run:

```bash
bash /home/ec2-user/get-iam-creds.sh
```

The script prints the instance-role temporary credentials
(`AccessKeyId` / `SecretAccessKey` / `Token` / `Expiration`) from IMDSv2.
Copy the three fields into the Former2 UI → **Credentials** panel. These
credentials never leave the SSM session transcript and the laptop, and
they expire with the instance role's session (typically 6 hours).

### Step B5 — Scan and export inside Former2 UI

On the laptop open `http://localhost:8080` (served by the tunnel from
Step B3). In the Former2 UI:

1. Paste credentials from Step B4 into the **Credentials** panel.
2. Pick source region and services, click **Scan**.
3. When scanning finishes, switch to the **Generate** tab and export as
   CloudFormation YAML.
4. Save the file inside the instance at `/home/ec2-user/former2-exports/`
   (create one if it does not exist — the UserData already provisioned it).
   Keep the filename predictable, e.g. `latest.yml`.

The skill does not automate this step — Former2's UI is the scanner.

### Step B6 — Retrieve the exported file

```bash
bash scripts/fetch-export.sh \
  --instance-id i-0123456789abcdef0 \
  --region ap-northeast-1 \
  --remote-path /home/ec2-user/former2-exports/latest.yml \
  --local out/cfn-from-former2.yml \
  --profile compliance
```

The script provisions a one-shot transfer bucket
(`skill-former2-transfer-<account>-<region>`) with SSE-AES256, public
access block, and a 1-day lifecycle expiration. The object is copied via
SSM `send-command` → S3 → local download, and removed from S3
immediately afterward.

### Step B7 — Reuse Mode A Steps 4-11

The file at `out/cfn-from-former2.yml` now plays the role of
`out/cfn-full.yml` in Mode A. From here, run **Mode A Steps 4-11**
verbatim:

- Step 4 — `select.py` (`--dry-run` → commit) + `npx former2 filter`
  (use `out/cfn-from-former2.yml` as the `--input`)
- Step 5 — `rewrite_cfn.py --preset`
- Step 6 — `precheck.py --deep`
- Step 7 — `generate_review.py` + `deployment_advice.py`
- Step 8 — admin edits `out/review.md`
- Step 9 — `apply_review.py`
- Step 10 — re-precheck; loop back to Step 8 until clean
- Step 11 — `aws cloudformation deploy` + audit archive

Mode A's Step 2 (local `scan.js`) and Step 3 (preview) are unnecessary
because Step B5 already produced a scoped template. If preview is still
useful, run `python3 scripts/preview.py --input out/cfn-from-former2.yml
--group-by service` (the preview script tolerates either raw-scan JSON or
exported YAML via `--input-type yaml`).

### Step B8 — Optional teardown

```bash
bash scripts/teardown.sh \
  --stack-name former2-reverse \
  --region ap-northeast-1 \
  --profile compliance
```

Teardown is deliberately manual: delete the stack only after the CFN
artifact is safely archived and the admin has decided no follow-up scan
is needed. The transfer S3 bucket is left in place (its lifecycle expires
objects at 1 day) — delete it manually if policy requires a zero-bucket
aftermath.

---

## Mode A to Mode B Mapping

| Mode A step | Mode B step | Notes |
|-------------|-------------|-------|
| Step 1 — confirm parameters | Step B1 — confirm parameters | Mode B adds VPC id, subnet id, stack name, endpoint toggle. |
| Step 2 — `scan.js` (local npm CLI) | Steps B2 + B3 + B4 + B5 | Mode B replaces the local CLI with an in-account EC2 scan via SSM tunnel + Former2 UI. |
| Step 3 — `preview.py` on raw.json | (Optional) `preview.py --input out/cfn-from-former2.yml` | Former2's UI already performs resource grouping; preview is optional in Mode B. |
| Step 4 — `select.py` + former2 filter | Step B7 (reuses Step 4) | Same scripts; input file is `out/cfn-from-former2.yml`. |
| Step 5 — `rewrite_cfn.py --preset` | Step B7 (reuses Step 5) | Identical. |
| Step 6 — `precheck.py --deep` | Step B7 (reuses Step 6) | Identical. |
| Step 7 — `generate_review.py` + `deployment_advice.py` | Step B7 (reuses Step 7) | Identical. |
| Step 8 — admin edits `review.md` | Step B7 (reuses Step 8) | Identical. |
| Step 9 — `apply_review.py` | Step B7 (reuses Step 9) | Identical. |
| Step 10 — re-precheck loop | Step B7 (reuses Step 10) | Identical. |
| Step 11 — `cloudformation deploy` | Step B7 (reuses Step 11) | Identical. |
| (no equivalent) | Step B8 — `teardown.sh` | Removes the Former2 EC2 stack; Mode A has nothing to tear down. |
| (no equivalent) | `fetch-export.sh` | Mode A never leaves the laptop, so no transfer step is needed. |

Fallback hierarchy: if an admin picked Mode B but a specific Mode A script
does not behave well with a Former2-exported YAML (e.g. the raw.json-only
`preview.py --group-by tag` path), fall back to running that script with
`--input out/cfn-from-former2.yml --input-type yaml` or skip that step and
carry on — the review loop will still catch issues.

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

## Scope Limitations (Phase 3)

- **No LLM rewriting** — deterministic rules only
- **No CDK output** — CFN YAML only
- **KMS cross-account grants** — flagged in review, not auto-rewritten
- **Data migration** — `deployment-advice.md` emits commands but does not run them
- **Peering handshake** — emitted as manual task; not executed
- **review.md schema is strict** — admins must keep the 4-block layout intact
- **Mode B scanner UI is manual** — Former2 upstream (iann0036) is used as-is;
  the UI click-through is not automated
- **Mode B runs one EC2 at a time** — no Organizations-level fan-out

---

## File Layout

```
aws-reverse-skill/
├── SKILL.md                   ← this file (v3)
├── scripts/
│   ├── scan.js                ← Mode A Step 2
│   ├── cache.py               ← scan cache helper
│   ├── preview.py             ← Mode A Step 3
│   ├── select.py              ← Step 4 (with --dry-run)
│   ├── rewrite_cfn.py         ← Step 5 (with --preset)
│   ├── precheck.py            ← Step 6 + Step 10 (deep AWS checks)
│   ├── precheck.sh            ← legacy lint wrapper
│   ├── generate_review.py     ← Step 7 (also --update for Step 10 loop)
│   ├── apply_review.py        ← Step 9
│   ├── deployment_advice.py   ← Step 7 companion
│   ├── deploy-former2.sh      ← Mode B Step B2
│   ├── ssm-portforward.sh     ← Mode B Step B3
│   ├── fetch-export.sh        ← Mode B Step B6
│   └── teardown.sh            ← Mode B Step B8
├── config/
│   ├── default-filters.json
│   ├── default-rewrites.json
│   └── rewrite-presets.json   ← 4 presets
├── references/
│   ├── rewrite-rules.md
│   ├── review-checklist.md
│   └── former2-cfn.yaml       ← Mode B EC2 stack template
├── workflows/
│   ├── export-by-tag.md
│   ├── export-all.md
│   ├── cross-account-deploy.md    ← showcases review loop
│   ├── cross-region-dr.md         ← showcases deployment-advice
│   ├── cleanup-default-only.md
│   └── in-account-mode.md         ← Mode B walkthrough
└── tests/                      ← 95 pytest cases
```
