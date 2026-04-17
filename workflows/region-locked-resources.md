# Region-Locked Resources — Hotfix C Walkthrough

> **Applies to:** R15 (CloudFront / WAFv2 CLOUDFRONT / ACM-for-CloudFront /
> Lambda@Edge) detection and review/advice integration.
> **Rule:** `region_lock` — auto-included in `cross-account`, `cross-region`,
> and `full` presets. Advisory only — no YAML mutation.

---

## 1. When to apply

Some AWS services are globally scoped or pinned to `us-east-1`. A template
that is perfectly portable elsewhere still fails to deploy (or succeeds but
silently breaks traffic) when these resources land in the wrong region.

The Hotfix-C detector flags four types:

| Resource type | Constraint | Typical break |
|---|---|---|
| `AWS::CloudFront::Distribution` | Global — but ACM cert + WAFv2 must be in `us-east-1` | Distribution rejects `AcmCertificateArn` / `WebACLId` that live elsewhere |
| `AWS::WAFv2::WebACL` (`Scope: CLOUDFRONT`) | Must be `us-east-1` | CloudFormation fails in any other region |
| `AWS::CertificateManager::Certificate` referenced by CloudFront | Must be `us-east-1` | Distribution deploy fails |
| `AWS::Lambda::Function` used as Lambda@Edge | Must be `us-east-1` | `LambdaFunctionARN` must be from `us-east-1`; CloudFront associate fails |

Trigger the detector whenever the source scan was captured in a non-`us-east-1`
region *and* the target region also is not `us-east-1`.

---

## 2. Quick usage

```bash
# Preset cross-account (default for cross-region + cross-account) includes R15.
python scripts/rewrite_cfn.py \
    --input  out/cfn-filtered.yml \
    --output out/cleaned.yml \
    --preset cross-account \
    --account-id 111111111111 \
    --source-region ap-northeast-1 \
    --target-region ap-south-1 \
    --review-decisions out/review-decisions.json

# The review.md call picks up the region-constraint decisions automatically.
python scripts/generate_review.py \
    --cleaned out/cleaned.yml \
    --decisions out/review-decisions.json \
    --output out/review.md \
    --stack-name my-stack \
    --source ap-northeast-1 --target ap-south-1

# deployment-advice.md surfaces concrete CLI templates for each type.
python scripts/deployment_advice.py \
    --input out/raw.json \
    --target-region ap-south-1 \
    --format md --output out/deployment-advice.md
```

When `--target-region us-east-1` is passed the detector still runs but every
entry is marked `is_violation: false`, so `review.md` shows ✅ OK rows and
`deployment-advice.md` emits nothing from the region-lock rule.

---

## 3. Example — CloudFront + ACM + WAFv2 split-stack pattern

For a template that already contains a CloudFront Distribution *and* its
ACM cert *and* its CLOUDFRONT-scoped WebACL, the recommended layout is:

```
┌─ stack-edge (us-east-1) ──────────┐      ┌─ stack-app (ap-south-1) ─────┐
│ - MyCert (ACM)                    │      │ - ALB / EC2 / …              │
│ - MyCloudFrontWebACL (CLOUDFRONT) │─ARN─▶│ - MyDist (CloudFront)        │
│ - MyEdgeFunction (Lambda@Edge)    │      │     ViewerCertificate.Acm...  │
│ Outputs: CertArn, WebACLArn       │      │     WebACLId                 │
└───────────────────────────────────┘      └──────────────────────────────┘
```

Deploy the edge stack *first*, export its ARNs, and consume them via
Parameters / `Fn::ImportValue` / SSM Parameter Store in the app stack.

Minimal edge stack skeleton:

```yaml
AWSTemplateFormatVersion: '2010-09-09'
Description: Edge stack — deployed to us-east-1 ONLY.
Resources:
  MyCert:
    Type: AWS::CertificateManager::Certificate
    Properties:
      DomainName: app.example.com
      ValidationMethod: DNS
  MyCloudFrontWebACL:
    Type: AWS::WAFv2::WebACL
    Properties:
      Name: cloudfront-webacl
      Scope: CLOUDFRONT
      DefaultAction: {Allow: {}}
      VisibilityConfig:
        SampledRequestsEnabled: true
        CloudWatchMetricsEnabled: true
        MetricName: cloudfront-webacl
Outputs:
  CertArn:
    Value: !Ref MyCert
    Export: {Name: edge-cert-arn}
  WebACLArn:
    Value: !GetAtt MyCloudFrontWebACL.Arn
    Export: {Name: edge-webacl-arn}
```

App stack consumes the exports:

```yaml
Parameters:
  EdgeCertArn: {Type: String}  # or !ImportValue edge-cert-arn
  EdgeWebACLArn: {Type: String}
Resources:
  MyDist:
    Type: AWS::CloudFront::Distribution
    Properties:
      DistributionConfig:
        ViewerCertificate:
          AcmCertificateArn: !Ref EdgeCertArn
        WebACLId: !Ref EdgeWebACLArn
        # … origins / behaviours …
```

Deploy order:

```bash
aws cloudformation deploy --region us-east-1  --stack-name stack-edge ...
aws cloudformation deploy --region ap-south-1 --stack-name stack-app  \
    --parameter-overrides EdgeCertArn=$(aws cloudformation describe-stacks \
      --region us-east-1 --stack-name stack-edge \
      --query 'Stacks[0].Outputs[?OutputKey==`CertArn`].OutputValue' --output text)
```

---

## 4. Troubleshooting Q&A

**Q1. Why does my `REGIONAL`-scope WebACL not show up in the review?**
R15 only flags `Scope: CLOUDFRONT`. Regional WAFv2 WebACLs (used by ALB / API
Gateway) can live in any region — they are not region-locked.

**Q2. My Lambda is not Lambda@Edge but R15 flagged it.**
Detection is heuristic: function names containing `edge` / `cf-`, a
`Lambda-Edge` tag, or a `Description` mentioning `Lambda@Edge` trigger the
flag. Rename the function or remove the marker; alternatively confirm from
the CloudFront Distribution that `LambdaFunctionAssociations` does not
reference it.

**Q3. I passed `--target-region us-east-1`; do I still need a split stack?**
No — when target is `us-east-1` R15 still surfaces the resources for visibility
but marks every row `✅ OK`. A single-stack deploy works.

**Q4. Can R15 rewrite the CFN to auto-split for me?**
No — R15 is intentionally advisory only. Splitting requires human decisions
(which resources cross which boundary, how outputs flow between stacks) that
are unsafe to automate. R15 surfaces the list; the operator owns the split.
