# Workflow: In-Account Mode (Mode B)

**Use case**: Keep the Former2 scanner — and the AWS credentials it
consumes — inside the target AWS account. The laptop only reaches AWS
through an SSM port-forward tunnel; no Former2 binary or long-lived key
ever lands on the laptop.

Typical drivers:

- Regulated boundary (FSI / GovCloud) — credentials must not leave the
  account.
- No-egress laptop policy — the operator cannot reach `*.amazonaws.com`
  directly.
- Audit requirement — every scan must be an auditable in-VPC asset with
  CloudTrail history.

If none apply, use **Mode A** (see [export-by-tag.md](export-by-tag.md)
or [cross-account-deploy.md](cross-account-deploy.md)). Mode A is faster
and has zero in-account footprint.

Maps to PRD v1.2 §4.2, §5 F1-F8, §7.2.

---

## Example Dialogue

```
Operator: I need to reverse the pre-prod VPC, but compliance won't let
          me run former2 locally. Credentials can't leave the account.
          Use in-account mode.

Skill:    Switching to Mode B. I need seven inputs before Step B2:
          source region, target region, CLI profile, source VPC id,
          source private subnet id, stack name, and whether to create
          VPC endpoints.

Operator: ap-northeast-1 for both, profile=compliance, vpc-0abc...,
          subnet-0def..., stack=former2-preprod, endpoints=No (we already
          have them).

Skill:    Confirmed. Running bash scripts/deploy-former2.sh ...
          Stack outputs: InstanceId=i-0123456789abcdef0.

          Open a second terminal and start the SSM tunnel with
            bash scripts/ssm-portforward.sh \
              --instance-id i-0123456789abcdef0 \
              --region ap-northeast-1 --profile compliance

          I'll wait. Ping me once http://localhost:8080 responds.
```

---

## Step-by-Step

### Step B1 — Confirm inputs

```
Source region:            ap-northeast-1
Target region:            ap-northeast-1
AWS CLI profile:          compliance
Source VPC id:            vpc-0abcdef0123456789
Source private subnet id: subnet-0abcdef0123456789
Stack name:               former2-preprod
Create VPC endpoints:     No   (VPC already has ssm/ssmmessages/ec2messages/logs/s3)
```

Pre-flight:

```bash
aws sts get-caller-identity --profile compliance
aws ec2 describe-vpcs --vpc-ids vpc-0abcdef0123456789 --profile compliance \
  --query 'Vpcs[0].{Dns:EnableDnsHostnames,Support:EnableDnsSupport}'
session-manager-plugin --version
```

Both DNS flags must be `true`; `session-manager-plugin` must print a
version string.

### Step B2 — Deploy the Former2 EC2 stack (idempotent)

```bash
bash scripts/deploy-former2.sh \
  --vpc-id vpc-0abcdef0123456789 \
  --subnet-id subnet-0abcdef0123456789 \
  --stack-name former2-preprod \
  --region ap-northeast-1 \
  --create-endpoints No \
  --profile compliance
```

Expected (abridged):

```
Successfully created/updated stack - former2-preprod
InstanceId          | i-0123456789abcdef0
PortForwardCommand  | aws ssm start-session --region ...
TearDownCommand     | bash scripts/teardown.sh --stack-name ...
```

If the stack already exists with `CREATE_COMPLETE` / `UPDATE_COMPLETE`,
`aws cloudformation deploy` prints `No changes to deploy` and the script
still returns the outputs. Template:
[../references/former2-cfn.yaml](../references/former2-cfn.yaml).

### Step B3 — Start the SSM port-forward tunnel (foreground)

```bash
bash scripts/ssm-portforward.sh \
  --instance-id i-0123456789abcdef0 \
  --region ap-northeast-1 \
  --profile compliance
```

Expected:

```
Starting session with SessionId: compliance-0a1b2c3d4e5f
Port 8080 opened for sessionId compliance-0a1b2c3d4e5f.
Waiting for connections...
```

Leave this terminal open for the entire scan. Ctrl+C tears the tunnel
down. Open a second terminal for Steps B4 and B6.

### Step B4 — Fetch IMDS credentials on the EC2 instance

In a second terminal:

```bash
aws ssm start-session --target i-0123456789abcdef0 \
  --region ap-northeast-1 --profile compliance
```

Inside the session:

```bash
bash /home/ec2-user/get-iam-creds.sh
```

Expected (example):

```json
{
  "Code": "Success",
  "AccessKeyId": "ASIA...EXAMPLE",
  "SecretAccessKey": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
  "Token": "IQoJb3JpZ2luX2VjE...EXAMPLE",
  "Expiration": "2026-04-17T15:12:34Z"
}
```

Paste `AccessKeyId`, `SecretAccessKey`, and `Token` into the Former2 UI →
**Credentials** panel. These are instance-role STS credentials that
expire at the printed timestamp (default 6 h). Do not save them to disk.

### Step B5 — Scan and export in the Former2 UI

Open `http://localhost:8080` on the laptop (served via the Step B3
tunnel).

1. **Credentials** panel — paste the three fields. Region
   `ap-northeast-1`.
2. **Parameters** — toggle services to scan (defaults are fine).
3. **Scan Account** — roughly one minute per 1000 resources.
4. **Generate → CloudFormation** — inspect, untick out-of-scope items.
5. **Download Template** → save as
   `/home/ec2-user/former2-exports/latest.yml` (UserData pre-creates
   this directory).

Verify inside the SSM session:

```bash
ls -lh /home/ec2-user/former2-exports/
# -rw-r--r-- 1 ec2-user ec2-user 128K Apr 17 09:42 latest.yml
```

Former2's UI is the scanner; the skill does not automate this step.

### Step B6 — Retrieve the exported file

```bash
bash scripts/fetch-export.sh \
  --instance-id i-0123456789abcdef0 \
  --region ap-northeast-1 \
  --remote-path /home/ec2-user/former2-exports/latest.yml \
  --local out/cfn-from-former2.yml \
  --profile compliance
```

Expected (abridged):

```
make_bucket: skill-former2-transfer-926093770964-ap-northeast-1
download: s3://skill-former2-transfer-.../exports/20260417T094231Z-7c3f1e2a.yml to out/cfn-from-former2.yml
delete: s3://skill-former2-transfer-.../exports/20260417T094231Z-7c3f1e2a.yml
Fetched export to out/cfn-from-former2.yml
```

The transfer bucket has SSE-AES256, full public-access block, and a
1-day lifecycle rule. Objects are deleted immediately after download;
the bucket is retained for later fetches.

### Step B7 — Reuse Mode A Steps 4-11

`out/cfn-from-former2.yml` now replaces `out/cfn-full.yml` from Mode A.
Every downstream script is identical:

| Mode A step | Command (unchanged) |
|-------------|---------------------|
| Step 4 — select + former2 filter | `python3 scripts/select.py --input out/cfn-from-former2.yml --input-type yaml ... --emit-regex-filter` → `npx former2 filter --input out/cfn-from-former2.yml --output out/cfn-filtered.yml --search-filter ...` |
| Step 5 — rewrite | `python3 scripts/rewrite_cfn.py --input out/cfn-filtered.yml --output out/cleaned.yml --preset <preset>` |
| Step 6 — deep precheck | `python3 scripts/precheck.py --template out/cleaned.yml --deep --output out/precheck-report.md` |
| Step 7 — review + advice | `python3 scripts/generate_review.py ... --output out/review.md` + `python3 scripts/deployment_advice.py ... --output out/deployment-advice.md` |
| Step 8 — admin edits `out/review.md` (skill pauses) | — |
| Step 9 — apply | `python3 scripts/apply_review.py out/review.md --out-final out/cleaned-final.yml --out-params out/deploy-params.json --diff out/review.diff` |
| Step 10 — re-precheck (loop) | `python3 scripts/precheck.py --template out/cleaned-final.yml --deep --output out/precheck-final.md` |
| Step 11 — deploy + archive | `AWS_PROFILE=compliance aws cloudformation deploy --template-file out/cleaned-final.yml ...` |

Archive `review.md`, `review.diff`, `manual-tasks.md`,
`deployment-advice.md`, and `precheck-final.md` with the change ticket,
exactly as Mode A does. See [cross-account-deploy.md](cross-account-deploy.md)
for a worked example of the review loop.

### Step B8 — Optional teardown

```bash
bash scripts/teardown.sh \
  --stack-name former2-preprod \
  --region ap-northeast-1 \
  --profile compliance
```

Expected:

```
About to delete stack "former2-preprod" in "ap-northeast-1". Continue? [y/N] y
Stack former2-preprod deleted.
```

Teardown is deliberately manual — run only when no rescan is planned.
The transfer S3 bucket is retained (objects self-expire after 24 h);
delete it by hand if policy forbids residual buckets.

---

## Operational Checklist

Before handing a Mode B session to an auditor, verify all five — each
must hold.

1. **No public IP on the instance**
   ```bash
   aws ec2 describe-instances --instance-ids i-0123456789abcdef0 \
     --region ap-northeast-1 --profile compliance \
     --query 'Reservations[0].Instances[0].{Public:PublicIpAddress,Private:PrivateIpAddress}'
   ```
   Expected: `Public: null`, `Private` is RFC1918.

2. **Empty ingress on the instance security group**
   ```bash
   SG=$(aws cloudformation describe-stack-resources --stack-name former2-preprod \
     --region ap-northeast-1 --profile compliance \
     --query "StackResources[?LogicalResourceId=='InstanceSecurityGroup'].PhysicalResourceId" --output text)
   aws ec2 describe-security-groups --group-ids "$SG" \
     --region ap-northeast-1 --profile compliance \
     --query 'SecurityGroups[0].IpPermissions'
   ```
   Expected: `[]`.

3. **IAM role carries only SSM + ReadOnlyAccess**
   ```bash
   ROLE=$(aws cloudformation describe-stack-resources --stack-name former2-preprod \
     --region ap-northeast-1 --profile compliance \
     --query "StackResources[?LogicalResourceId=='InstanceRole'].PhysicalResourceId" --output text)
   aws iam list-attached-role-policies --role-name "$ROLE" --profile compliance
   ```
   Expected exactly two managed policies:
   `arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore` and
   `arn:aws:iam::aws:policy/ReadOnlyAccess`. No inline policies.

4. **VPC endpoints present**
   ```bash
   aws ec2 describe-vpc-endpoints \
     --filters Name=vpc-id,Values=vpc-0abcdef0123456789 \
     --region ap-northeast-1 --profile compliance \
     --query 'VpcEndpoints[].{Service:ServiceName,State:State}' --output table
   ```
   Expected: `com.amazonaws.ap-northeast-1.{ssm,ssmmessages,ec2messages,logs,s3}`
   each in state `available`.

5. **Every SSM session recorded in CloudTrail**
   ```bash
   aws cloudtrail lookup-events \
     --lookup-attributes AttributeKey=ResourceName,AttributeValue=i-0123456789abcdef0 \
     --region ap-northeast-1 --profile compliance \
     --query 'Events[?EventName==`StartSession`].[EventTime,Username]' --output table
   ```
   Expected: one row per `start-session` / `ssm-portforward.sh` call,
   each tagged with the operator principal.

---

## Mode A vs Mode B — Comparison

| Dimension | Mode A — Standard | Mode B — In-Account |
|-----------|-------------------|----------------------|
| Deploy location | Laptop (`npx former2`) | EC2 in private subnet of target account |
| Credential lifecycle on laptop | Long-lived AWS profile / SSO session | None — only an SSM tunnel |
| Credentials the scanner uses | Laptop profile | Instance role via IMDSv2 (STS, ~6 h) |
| Network egress from laptop | `*.amazonaws.com` (TLS) | `ssmmessages.<region>.amazonaws.com` only |
| In-account footprint | 0 | 1× EC2 + 1× SG + 1× IAM role + optional VPC endpoints + 1× transfer S3 bucket |
| Cost per run | 0 | ≈ USD 0.10-0.30 (EC2 hours + endpoints + negligible S3) |
| Typical end-to-end time | 15-45 min | 45-90 min |
| Audit surface | API calls from laptop principal | API calls from instance role + CloudTrail `StartSession` per operator |
| Recommended use case | Day-to-day reverse-engineering, cross-region DR, cross-account migration | Regulated boundary, no-egress laptop, in-VPC audit requirement |

Engagements with mixed regulated / unregulated accounts: run Mode B only
for the regulated subset.

---

## Troubleshooting

### session-manager-plugin missing

```
ERROR: session-manager-plugin is not installed.
```

`ssm-portforward.sh` checks the plugin before starting the session.
Install from
<https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html>
and re-run. `awscli` does not bundle the plugin — installing `awscli`
alone is not enough.

### VPC already has SSM / S3 endpoints

Symptom: `deploy-former2.sh` fails with `Resource of type
'AWS::EC2::VPCEndpoint' with identifier '...' already exists`.

Re-run with `--create-endpoints No`. Confirm coverage first:

```bash
aws ec2 describe-vpc-endpoints --filters Name=vpc-id,Values=vpc-... \
  --region ap-northeast-1 --profile compliance \
  --query 'VpcEndpoints[].ServiceName' --output text
```

All of `ssm`, `ssmmessages`, `ec2messages`, `logs`, `s3` must appear.

### Browser reports `http://localhost:8080` connection refused

Two causes:

1. **Tunnel terminated** — restart `ssm-portforward.sh`. Each Ctrl+C
   drops the tunnel; the script must run for the full Step B5.
2. **Instance nginx not ready** — UserData provisions nginx + Former2 at
   boot. Inside an SSM shell:
   ```bash
   sudo systemctl status nginx
   curl -sI http://127.0.0.1/
   ```
   Expect 60-120 s after stack `CREATE_COMPLETE` before nginx answers.

### Former2 UI reports `ExpiredToken` during scan

IMDSv2 credentials issued at the start of the scan have expired (default
6 h). Re-run `bash /home/ec2-user/get-iam-creds.sh` inside the SSM
shell, repaste the fresh `AccessKeyId` / `SecretAccessKey` / `Token`,
and click **Scan** again. Start heavy scans within an hour of fetch.

### `ssm send-command` returns `InvalidInstanceId`

`fetch-export.sh` fails with `InvalidInstanceId`. Checklist:

1. `aws ssm describe-instance-information --filters
    Key=InstanceIds,Values=i-... --region ap-northeast-1
    --profile compliance` — instance must list with
    `PingStatus: Online`. If missing, the SSM agent cannot reach
    `ssmmessages` / `ec2messages`; re-check Step B2 endpoints.
2. Instance role must have `AmazonSSMManagedInstanceCore` attached (the
   template provides it — do not detach).
3. Wait ≥ 2 min after instance launch for SSM registration.

### Teardown blocked by ENI still in use

`teardown.sh` errors with `The security group '...' has a dependent
object` or `ENI '...' is still in use`.

1. Detach any ad-hoc ENIs created post-deploy.
2. If the stack owns the endpoints (`CreateEndpoints: Yes`),
   CloudFormation must finish removing them first — re-run teardown
   after ~2 min.
3. If persistent, identify the blocking ENI:
   ```bash
   aws ec2 describe-network-interfaces \
     --filters Name=group-id,Values=<sg-id> \
     --query 'NetworkInterfaces[].[NetworkInterfaceId,Status,Description]'
   ```
   Delete manually, then re-run teardown.

### CloudTrail audit pointers

Three lookups auditors typically request (all with `--profile
compliance`):

- **Stack lifecycle** — `CreateStack` / `UpdateStack` / `DeleteStack`
  for `former2-preprod`:
  ```bash
  aws cloudtrail lookup-events \
    --lookup-attributes AttributeKey=ResourceName,AttributeValue=former2-preprod \
    --region ap-northeast-1
  ```
- **Session activity** — `StartSession` / `TerminateSession` filtered by
  instance id (see Operational Checklist item 5).
- **ReadOnlyAccess calls made by the instance** — filter CloudTrail on
  `userIdentity.sessionContext.sessionIssuer.userName = <InstanceRoleName>`
  to see every API call Former2 issued during the scan.

Session Manager can log session output to S3 or CloudWatch Logs for full
keystroke history — configure at the session document level if control
demands it.

---

## Related Files

- **PRD v1.2 §4.2** — compliance-mode specification.
- **PRD v1.2 §5** — F1 environment prep, F2 scan, F7 compliance & audit,
  F8 resource cleanup.
- **PRD v1.2 §7.2** — architecture diagram matched by the CFN template.
- [../references/former2-cfn.yaml](../references/former2-cfn.yaml) —
  security-hardened EC2 stack (zero-ingress SG, no public IP, IMDSv2,
  encrypted EBS, optional VPC endpoints).
- [../scripts/deploy-former2.sh](../scripts/deploy-former2.sh) — Step B2.
- [../scripts/ssm-portforward.sh](../scripts/ssm-portforward.sh) — Step B3.
- [../scripts/fetch-export.sh](../scripts/fetch-export.sh) — Step B6.
- [../scripts/teardown.sh](../scripts/teardown.sh) — Step B8.
- [../SKILL.md](../SKILL.md) — Mode A / Mode B decision tree, mapping
  table, and the 11-step Mode A flow reused by Step B7.
- [cross-account-deploy.md](cross-account-deploy.md) — worked example of
  the review loop inherited by Step B7.
