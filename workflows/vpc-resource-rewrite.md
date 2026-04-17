# Workflow: VPC Resource Rewrite (Hotfix A)

> **场景**: 跨 VPC / 跨账号部署时，源环境的 VPC ID / Subnet ID / Security Group ID 在目标环境不存在
> **规则**: R11 (VPC 资源 Parameter 化) + R12 (SG CIDR 分类)
> **版本**: aws-reverse-skill v4.0 (Hotfix A)

---

## 一、何时用

- 跨账号部署：源账号的 `vpc-xxx` / `subnet-xxx` / `sg-xxx` 在目标账号不存在
- 同账号跨 VPC：从测试 VPC 复制到生产 VPC，内部资源 ID 不同
- 源和目标 VPC CIDR 段不同（如源 `10.0.0.0/16` → 目标 `192.168.0.0/16`）

*不需要*：同 VPC 内复制（直接用源 ID 即可，不走 R11/R12）

---

## 二、典型对话

```
大乖乖:
我要把 ap-northeast-1 的 banking VPC 里的 ALB + ASG + RDS 复制到 us-east-1
的另一个账号 222222222222，目标 VPC CIDR 是 192.168.0.0/16，跟源不一样。

编程猫:
好的。这是跨账号跨 VPC 跨 region 场景，R11/R12 要全上。流程：

1. 先 scan + filter 得到 cleaned.yml
2. 用 --preset cross-account（含 R11 + R12）处理资源 ID 和 SG CIDR
3. 生成 review.md，会有两个新区块让你填：
   - 🔧 VPC 资源映射：目标 VPC / Subnet / SG / VPC CIDR 的 ID/值
   - 🌐 SG CIDR 审阅：RFC1918 外段 / 公网段的确认
4. 你填完 review.md
5. apply_review 生成最终 cleaned-final.yml + deploy-params.json
6. 部署
```

---

## 三、完整命令示例

### Step 1 — Rewrite with R11/R12

```bash
python3 scripts/rewrite_cfn.py \
  --input out/cfn-filtered.yml \
  --output out/cleaned.yml \
  --account-id 111111111111 \
  --source-region ap-northeast-1 \
  --target-region us-east-1 \
  --source-vpc-cidr 10.0.0.0/16 \
  --preset cross-account \
  --review-decisions out/decisions.json
```

*输出*：
- `out/cleaned.yml`：
  - 所有 `vpc-xxx` → `!Ref TargetVpcId`
  - 所有 `subnet-xxx` / Subnets list → `!Split [',', !Ref TargetSubnetIds]`
  - 所有 `sg-xxx` / SecurityGroupIds list → `!Split [',', !Ref TargetSecurityGroupIds]`
  - SG 规则里源 VPC 内 CIDR → `!Ref TargetVpcCidr`
  - RFC1918 外 / 公网 CIDR → 保留（记入决策）
- `out/decisions.json`：所有 R11/R12 产生的决策项

### Step 2 — Precheck (R11/R12 结果)

```bash
python3 scripts/precheck.py \
  --template out/cleaned.yml \
  --target-region us-east-1 \
  --target-profile target-prod \
  --output out/precheck-report.md
```

### Step 3 — Generate Review

```bash
python3 scripts/generate_review.py \
  --cleaned out/cleaned.yml \
  --decisions out/decisions.json \
  --precheck-report out/precheck-report.md \
  --output out/review.md \
  --stack-name banking-dr \
  --preset cross-account \
  --source "ap-northeast-1 / 111111111111" \
  --target "us-east-1 / 222222222222"
```

*review.md 新增两个区块*：

```markdown
## ⚠️ 需人工决策

### 🔧 VPC 资源映射（R11 Parameter 填写）

| Parameter 名 | 类型 | 源值 | 目标值（请填写） | 说明 |
|-------------|------|------|-----------------|------|
| TargetVpcId | AWS::EC2::VPC::Id | vpc-0abc1234 | **<填 vpc-xxxxxxxx>** | 目标账号的 VPC |
| TargetSubnetIds | CommaDelimitedList | subnet-aaa,subnet-bbb | **<填 subnet-xxx,subnet-yyy>** | 至少 2 个 private subnet（跨 AZ） |
| TargetSecurityGroupIds | CommaDelimitedList | sg-111,sg-222 | **<填 sg-xxx,sg-yyy>** | EC2/ALB 用的 SG |
| TargetVpcCidr | String | 10.0.0.0/16 | **<填 192.168.0.0/16>** | 目标 VPC CIDR，R12 替换用 |

### 🌐 SG CIDR 审阅（R12 分类结果）

Resource: AppServerSG
| 方向 | 端口 | 源 CIDR | 类别 | 处理 / 目标值 |
|------|------|---------|------|-------------|
| Ingress | 5432 | 192.168.10.0/24 | rfc1918-external | **<填目标 VPC Peering 对端段或 SKIP>** |
| Ingress | 443 | 203.0.113.5/32 | public | **<确认办公网段，保留或修改>** |
| Ingress | 22 | 0.0.0.0/0 | public | **<强烈建议缩小，或 SKIP>** |
```

### Step 4 — 管理员编辑 review.md

填完上面表格，保存。*不需要改 YAML，只填表格值*。

### Step 5 — Apply Review

```bash
python3 scripts/apply_review.py out/review.md \
  --cleaned out/cleaned.yml \
  --out-final out/cleaned-final.yml \
  --out-params out/deploy-params.json \
  --out-manual out/manual-tasks.md \
  --diff out/review.diff
```

*输出*：
- `out/cleaned-final.yml`：最终可部署 CFN
- `out/deploy-params.json`：`{"TargetVpcId":"vpc-xxx","TargetSubnetIds":"subnet-x,subnet-y","TargetSecurityGroupIds":"sg-x,sg-y","TargetVpcCidr":"192.168.0.0/16","AmiId":"ami-xxx",...}`
- `out/manual-tasks.md`：需要人工处理的项（RFC1918 外 CIDR 需自己改目标 YAML 或 SKIP）

### Step 6 — Deploy

```bash
aws cloudformation deploy \
  --template-file out/cleaned-final.yml \
  --stack-name banking-dr \
  --region us-east-1 \
  --profile target-prod \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides $(python3 -c "
import json
p = json.load(open('out/deploy-params.json'))
print(' '.join(f'{k}={v}' for k,v in p.items()))
")
```

---

## 四、常见问题排障

### Q1. R11 没有生效，VPC/Subnet ID 没变

*检查*：
- 是否用了 `--preset cross-account` 或 `--preset full`？（默认预设 / cleanup-only / cross-region 不含 R11）
- 或显式 `--rules R11`

### Q2. R12 没生效，SG CIDR 全部保留

*原因*：没传 `--source-vpc-cidr`（没源 VPC CIDR 无法分类，R12 降级为 no-op + warning）

*解法*：
```bash
--source-vpc-cidr 10.0.0.0/16
# 或多个：
--source-vpc-cidr 10.0.0.0/16,10.1.0.0/16
```

也可以通过 `--raw out/raw.json` 让 skill 自动从 raw.json 读源 VPC CIDR。

### Q3. TargetSubnetIds 格式怎么填

逗号分隔，*无空格*：
```
subnet-0aaa1111,subnet-0bbb2222,subnet-0ccc3333
```

CFN 的 `CommaDelimitedList` 类型自动 split。

### Q4. review.md 里有些 SG CIDR 类别判错了

R12 的分类基于 `--source-vpc-cidr` 范围比对。如果你知道某个 "RFC1918-external" 其实是合法的 VPC Peering 对端段，在 review.md 决策表里填入目标对端 CIDR 即可（apply_review 不会自动改 YAML，但会生成 manual-tasks 提醒）。

### Q5. deployment_advice 提示用 PrefixList 怎么办

*情况*：多个 SG 共享一批公网 CIDR（典型：办公网白名单、SaaS 合作伙伴 IP）

*建议做法*（非强制）：
```bash
# 1. 创建 Customer-Managed Prefix List
aws ec2 create-managed-prefix-list \
  --prefix-list-name OfficeIPs \
  --address-family IPv4 \
  --max-entries 50 \
  --entries Cidr=203.0.113.0/24,Description=HQ Cidr=198.51.100.0/24,Description=Branch

# 2. SG 规则改用 SourcePrefixListId
```

这一步在 skill 之外手工完成，skill 只提建议。

### Q6. 跨账号 SG 引用（SourceSecurityGroupId 指向源账号 SG）

R11 会把 `SourceSecurityGroupId: sg-xxx` 替换为 `!Ref TargetSecurityGroupIds` 的第一个元素，这可能不准。*手工审阅*：
- 如果源 SG 在目标账号有对等 SG，review 填对应 ID
- 如果跨账号 SG 引用（`SourceSecurityGroupId + SourceSecurityGroupOwnerId`），需要双账号 Peering 或手工处理

---

## 五、R11/R12 规则速查

| 属性 | R11/R12 处理 |
|------|------------|
| `VpcId: vpc-xxx` | → `!Ref TargetVpcId` |
| `SubnetId: subnet-xxx` | → `!Select [0, !Split [',', !Ref TargetSubnetIds]]` |
| `SubnetIds: [subnet-a, subnet-b]` | → `!Split [',', !Ref TargetSubnetIds]` |
| `Subnets: [...]` (ALB) | → 同上 |
| `SecurityGroupIds: [sg-x, sg-y]` | → `!Split [',', !Ref TargetSecurityGroupIds]` |
| `VpcSecurityGroupIds: [...]` (RDS) | → 同上 |
| `GroupId: sg-xxx` | → `!Select [0, !Split [',', !Ref TargetSecurityGroupIds]]` |
| `SourceSecurityGroupId: sg-xxx` | → `!Select [0, !Split [',', !Ref TargetSecurityGroupIds]]`（需 review 审） |
| `CidrIp: 10.0.1.0/24`（VPC 内） | → `!Ref TargetVpcCidr` |
| `CidrIp: 192.168.10.0/24`（RFC1918 外） | 保留 + review decision |
| `CidrIp: 203.0.113.0/24`（公网） | 保留 + review decision |
| `CidrIp: 0.0.0.0/0` | 保留 + review decision（建议审阅） |
| `CidrIpv6: ::/0` | 保留 + review decision |

---

## 六、与其它 workflow 关系

| workflow | 关系 |
|---------|------|
| `export-by-tag.md` | 基础筛选流程，R11/R12 在 rewrite 阶段启用 |
| `cross-account-deploy.md` | R11/R12 是此场景核心能力 |
| `cross-region-dr.md` | R11/R12 + R3 (AMI) 是此场景核心 |
| `in-account-mode.md` | 合规模式的 rewrite 阶段同样走 R11/R12 |

---

## 七、参考

- `doc/PRD.md` §5 F4 规则引擎
- `doc/CROSS-ENV-ISSUES.md` §2.1-2.5（VPC/SG/ELB/TG/ASG 问题详解）
- `doc/PHASE4-PLAN.md` Hotfix A
- `scripts/cidr_analyzer.py` / `rewrite_cfn.py` R11/R12 实现
- AWS 文档：
  - [Customer-Managed Prefix List](https://docs.aws.amazon.com/vpc/latest/userguide/working-with-managed-prefix-lists.html)
  - [VPC Peering CIDR 规划](https://docs.aws.amazon.com/vpc/latest/peering/vpc-peering-overview.html)
