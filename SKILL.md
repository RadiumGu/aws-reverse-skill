# aws-reverse-skill

**Version**: 1.0 (Phase 1 MVP — Standard Mode)  
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

---

## Standard Mode Flow (7 Steps)

### Step 1 — Confirm Parameters

Ask the user to confirm:

```
Source region?        (e.g. ap-northeast-1)
AWS CLI profile?      (e.g. default / prod)
Services to scan?     (e.g. Lambda,IAM  — or "all")
Filter by tag?        (e.g. Environment=prod)
Target region?        (e.g. us-east-1)
Source account ID?    (12-digit, for rewrite rules)
Stack name?           (e.g. myapp-prod)
```

### Step 2 — Scan

```bash
node scripts/scan.js \
  --region <source-region> \
  --services <services> \
  --profile <profile> \
  --out-raw out/raw.json \
  --out-cfn out/cfn-full.yml
```

Creates `out/raw.json` (resource metadata) and `out/cfn-full.yml` (full CFN).

### Step 3 — Filter

```bash
python scripts/select.py \
  --input out/raw.json \
  --output out/filtered.json \
  --service <services> \
  --tag <tag-filter> \
  --regex <regex> \
  --exclude-default \
  --emit-regex-filter > out/id-filter.txt
```

Then use former2 filter to produce scoped CFN:

```bash
npx former2 filter \
  --input out/cfn-full.yml \
  --output out/cfn-filtered.yml \
  --search-filter "$(cat out/id-filter.txt)"
```

### Step 4 — Rewrite

```bash
python scripts/rewrite_cfn.py \
  --input out/cfn-filtered.yml \
  --output out/cleaned.yml \
  --account-id <source-account-id> \
  --source-region <source-region>
```

Replaces:
- Hardcoded account IDs → `!Sub '${AWS::AccountId}'`
- Hardcoded region → `!Sub '${AWS::Region}'`
- AMI IDs → `!Ref AmiId` + Parameter with SSM default
- Hardcoded AZs → `!Select [0, !GetAZs '']`
- Adds `DeletionPolicy: Retain` to RDS / S3 / DynamoDB / EFS

### Step 5 — Precheck

```bash
bash scripts/precheck.sh out/cleaned.yml
```

Runs cfn-lint + `aws cloudformation validate-template`. Non-zero exit = failure.

### Step 6 — Review & Deploy

Show the user the deployment command:

```bash
aws cloudformation deploy \
  --template-file out/cleaned.yml \
  --stack-name <stack-name> \
  --region <target-region> \
  --profile <target-profile> \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides AmiId=<ami-id-in-target-region>
```

Point to `references/review-checklist.md` for pre-deploy manual checks.

### Step 7 — Audit Log

Record to `out/audit-<timestamp>.log`:
- Source region / account / profile
- Services scanned / filter conditions
- Rewrite rules applied
- Resource count before and after filter
- Output file paths

---

## Error Handling

| Situation | Response |
|-----------|----------|
| `former2: command not found` | "Run `npm install` in the skill directory first." |
| AWS credentials not configured | "Configure AWS CLI: `aws configure` or set `AWS_PROFILE`." |
| `cfn-lint` finds errors | Show cfn-lint output; ask user to review before deploying. |
| `validate-template` fails | Show error; template may need manual adjustment. |
| Account ID not provided | Skip Rule 1 (account rewrite); warn user to check ARNs manually. |
| No resources after filter | Confirm filter criteria with the user before proceeding. |

---

## Scope Limitations (Phase 1)

- **Standard mode only** — no EC2/SSM compliance mode (Phase 3)
- **No LLM rewriting** — deterministic rules only
- **No CDK output** — CFN YAML only
- **No quota pre-check** — manual review required (references/review-checklist.md)
- **KMS / cross-account IAM** — not auto-rewritten (Phase 3)

---

## File Layout

```
aws-reverse-skill/
├── SKILL.md              ← this file
├── scripts/
│   ├── scan.js           ← Step 2: former2 wrapper
│   ├── select.py         ← Step 3: resource filter
│   ├── rewrite_cfn.py    ← Step 4: rules engine
│   └── precheck.sh       ← Step 5: lint + validate
├── references/
│   ├── rewrite-rules.md  ← Rule documentation
│   └── review-checklist.md ← Pre-deploy checklist
├── config/
│   ├── default-filters.json
│   └── default-rewrites.json
├── workflows/
│   └── export-by-tag.md  ← Example workflow script
└── tests/                ← Unit tests (pytest)
```
