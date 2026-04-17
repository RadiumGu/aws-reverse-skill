# Workflow: Nested ARN Rewrite (Hotfix B)

> **场景**: CFN 模板内嵌字符串 / JSON 里藏着源环境的 ARN，跨账号部署时失效
> **规则**: R13 (字符串扫描) + R14 (JSON 嵌套扫描)
> **版本**: aws-reverse-skill v4.1 (Hotfix B)

---

## 一、何时用

当源环境的 CFN 模板包含以下任一：
- *Step Functions StateMachine* — Definition 里硬编码 Lambda / SQS / DynamoDB ARN
- *Lambda Function Environment Variables* — 变量值里藏 ARN（如 `DB_TABLE_ARN=arn:aws:dynamodb:...`）
- *EventBridge Rule* — Target Arn / RoleArn
- *IAM / S3 / SNS / SQS Policy Document* — Statement.Resource 或 Principal.AWS 含外部账号 ARN
- *API Gateway* — Integration.Uri 指向 Lambda

R13/R14 自动识别这些位置，同账号 ARN → `!Sub` 表达式，跨账号 ARN → Parameter。

---

## 二、工作流程

```
rewrite_cfn.py --preset cross-account (R13/R14 启用)
      ↓
  同账号 ARN: arn:aws:lambda:<src-region>:<src-account>:function:foo
      → !Sub arn:aws:lambda:${AWS::Region}:${AWS::AccountId}:function:foo
      
  跨账号 ARN: arn:aws:lambda:us-east-1:999999999999:function:external
      → Parameter ExternalLambdaArn<hash>
      → review 决策表让管理员填目标环境对应 ARN
```

---

## 三、示例：含 Step Functions 的跨账号部署

### 源 CFN (Definition 里有跨账号 Lambda)

```yaml
Resources:
  MyStateMachine:
    Type: AWS::StepFunctions::StateMachine
    Properties:
      DefinitionString: |
        {
          "StartAt": "Local",
          "States": {
            "Local": {
              "Type": "Task",
              "Resource": "arn:aws:lambda:ap-northeast-1:111111111111:function:local-task",
              "Next": "External"
            },
            "External": {
              "Type": "Task",
              "Resource": "arn:aws:lambda:us-east-1:999999999999:function:partner-task",
              "End": true
            }
          }
        }
```

### Step 1 — Rewrite

```bash
python3 scripts/rewrite_cfn.py \
  --input out/cfn-filtered.yml \
  --output out/cleaned.yml \
  --account-id 111111111111 \
  --source-region ap-northeast-1 \
  --target-region us-east-1 \
  --preset cross-account \
  --review-decisions out/decisions.json
```

### Step 2 — 结果

```yaml
Parameters:
  ExternalLambdaArn5ee44cd0:
    Type: String
    Description: "Target ARN for arn:aws:lambda:us-east-1:999999999999:function:partner-task"
  # ... 原有其它 Parameters

Resources:
  MyStateMachine:
    Type: AWS::StepFunctions::StateMachine
    Properties:
      DefinitionString: !Sub |
        {
          "StartAt": "Local",
          "States": {
            "Local": {
              "Type": "Task",
              "Resource": "arn:aws:lambda:${AWS::Region}:${AWS::AccountId}:function:local-task",
              "Next": "External"
            },
            "External": {
              "Type": "Task",
              "Resource": "${ExternalLambdaArn5ee44cd0}",
              "End": true
            }
          }
        }
```

同账号 Lambda ARN 用 `${AWS::Region}` / `${AWS::AccountId}`，跨账号引用抽成 Parameter。

### Step 3 — Review 决策

`review.md` 新增区块：

```markdown
### 🔗 跨账号 ARN 引用 (R13/R14)

| Parameter 名 | Service | 源 ARN | 位置 | 目标值（请填） |
|-------------|---------|--------|------|-------------|
| ExternalLambdaArn5ee44cd0 | lambda | arn:aws:lambda:us-east-1:999999999999:function:partner-task | StateMachine.DefinitionString | **<填目标 ARN>** |
| ExternalSqsArnc4862c75 | sqs | arn:aws:sqs:us-east-1:999999999999:partner-queue | Lambda.Environment.Variables.PARTNER_QUEUE_ARN | **<填目标 ARN>** |
```

### Step 4 — Apply + 部署

```bash
python3 scripts/apply_review.py out/review.md \
  --cleaned out/cleaned.yml \
  --out-final out/cleaned-final.yml \
  --out-params out/deploy-params.json \
  --out-manual out/manual-tasks.md

aws cloudformation deploy \
  --template-file out/cleaned-final.yml \
  --parameter-overrides $(python3 -c "
import json
p = json.load(open('out/deploy-params.json'))
print(' '.join(f'{k}={v}' for k,v in p.items()))
") \
  ...
```

---

## 四、R13/R14 覆盖范围对照

| 属性 | R13/R14 处理 |
|------|-------------|
| Step Functions `DefinitionString` (JSON 字符串) | R14 parse JSON → 改 ARN → serialize |
| IAM `PolicyDocument.Statement.Resource` | R14 walk dict |
| S3/SNS/SQS `BucketPolicy.PolicyDocument` | R14 walk dict |
| Lambda `Environment.Variables.*` | R13 scalar scan |
| EventBridge `Rule.Targets.Arn` | R13 scalar scan |
| EventSourceMapping `EventSourceArn` | R13 scalar scan |
| API Gateway `Integration.Uri` | R13 scalar scan |
| Statement `Principal.AWS` (list of ARNs) | R14 walk list |
| 普通字符串 Properties | R13 scalar scan |

---

## 五、常见排障

### Q1. R13/R14 没生效

*检查*：
- `--preset cross-account` 或 `--preset full`（其他预设不含 R13/R14）
- 或 `--rules R13,R14` 显式启用

### Q2. 同账号 ARN 没变成 !Sub

*检查 `--account-id` 参数*：如果没传源账号 ID，R13/R14 无法识别"同账号"，会跳过。

```bash
--account-id 111111111111 --source-region ap-northeast-1
```

### Q3. Step Functions DefinitionString 是 !Sub 表达式（不是纯字符串）怎么办

*R14 降级处理*：遇到 CFN 函数值（`!Sub`/`!Join`）会跳过 JSON parse，但 R13 仍会扫描 `!Sub` 内部的 scalar 字符串。覆盖率可能不完整，review.md 会提示。

### Q4. 产生了很多 Parameter 看起来一样

*原因*：跨账号 ARN 用 SHA256 前 8 字节生成 hash 后缀，同 ARN 同 hash。如看到 `ExternalLambdaArnabc12345` 和 `ExternalLambdaArnabc12345` 完全一样，是正常去重。

### Q5. 某个 ARN 没被识别

*原因*：`arn_rewriter` 有 service whitelist（KNOWN_SERVICES），不常见服务（如某些 AWS 新产品）可能不在列表里。

*解法*：
- 临时：手工在生成的 cleaned.yml 里改
- 长期：提 issue 扩展 KNOWN_SERVICES

### Q6. IAM Policy 里的 NotResource 会处理吗

*会*。R14 对 Policy Statement 里的 `Resource`、`NotResource`、`Principal.AWS`、`NotPrincipal.AWS` 都扫描。

---

## 六、和其它 workflow 关系

| workflow | 关系 |
|---------|------|
| `cross-account-deploy.md` | R13/R14 是跨账号场景的关键能力 |
| `vpc-resource-rewrite.md` | R13/R14 常和 R11/R12 一起用（cross-account 预设全含） |
| `cross-region-dr.md` | R13 帮跨 region 场景替换 region 硬编码 |

---

## 七、参考

- `doc/CROSS-ENV-ISSUES.md` §2.13 Step Functions / §2.7 Lambda / §2.14 EventBridge
- `doc/PHASE4-PLAN.md` Hotfix B
- `scripts/arn_rewriter.py` — find_arns / rewrite_arn / rewrite_arns_in_text
- `scripts/rewrite_cfn.py` — R13 scalar scan / R14 JSON walker
- AWS 文档：
  - [Step Functions IAM 跨账号](https://docs.aws.amazon.com/step-functions/latest/dg/concepts-access-cross-acct-resources.html)
  - [CFN !Sub](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/intrinsic-function-reference-sub.html)
