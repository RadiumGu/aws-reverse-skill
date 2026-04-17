# PRD: aws-reverse-skill

**版本**: v1.1（CFN 聚焦版）
**日期**: 2026-04-17
**作者**: 编程猫（合并 小乖乖 v0.1-v0.4 + CFN 一次性部署定位）
**状态**: 定稿，进入 Phase 1 实施
**宿主（Host Agent）**: Claude Code / Kiro / OpenClaw 编程猫（AgentSkills 规范）

---

## 1. 背景与问题

金融、政府等合规敏感客户，以及需要快速搭建 DR / 测试环境的团队，在 AWS 上普遍面临：

- 现有生产环境 **没有 IaC**，全靠手动控制台搭建
- 需要在 **另一个 Region 或账号** 快速构建对等环境，缺标准化起点
- **不接受第三方 SaaS 工具**（如 former2.com 公网版）处理 AWS 凭证
- 浏览器端 Former2 UI 对大账号选择费劲，无法编排到 Agent 流程
- 一次性部署完就交付，没有持续迭代的 IaC 开发团队

### 1.1 定位

> **本 Skill 解决「从现有环境一次性生成可部署 CloudFormation 模板」问题。**

**不做**：完整 DR 方案、数据同步、流量切换、长期 IaC 工程化演进。输出产物可作为这些方案的起点，但不对它们负责。

### 1.2 为什么只做 CFN 不做 CDK

一次性部署场景下 CDK 的所有优势（语义化 API / 类型检查 / 多环境复用 / 抽象）*一个都用不上*，反而引入工具链复杂度：

| 维度 | CFN | CDK |
|------|-----|-----|
| 客户 onboarding | 🟢 `aws cloudformation deploy` 一条命令 | 🔴 Node.js + npm + cdk + bootstrap |
| 工具链依赖 | 🟢 仅 aws cli | 🔴 Node.js + cdk CLI + TS 编译 |
| former2 CLI 原生输出 | 🟢 100% 准确 | 🔴 不支持（需下游转换） |
| Bootstrap | 🟢 零准备 | 🔴 每 account+region 要 bootstrap |
| 合规审计 | 🟢 YAML 直接可审 | 🔴 还要审 bootstrap IAM Role |
| 多环境迭代 | 🔴 参数化有限 | 🟢 代码抽象 |

**本 skill 客户画像 = 一次性部署 + 运维主导 + 合规严格 → CFN 最优解。**

CDK 输出路径放 Phase 4 作为可选，默认不做。

---

## 2. 目标

提供 AgentSkill，用自然语言完成：

> "扫描 ap-northeast-1 生产环境的 Lambda + IAM，按 tag `Environment=prod` 筛选，生成可部署到 us-east-1 的 CloudFormation"

> "把源账号 VPC `vpc-xxx` 和里面资源，生成一份可部署到目标账号的 CFN 模板"

### 2.1 引擎选型

| 候选 | 选用 | 理由 |
|------|------|------|
| **Former2 npm CLI**（`iann0036/former2`） | ✅ **主路径** | npm 装好即用；无需 EC2；覆盖 ~130 AWS 服务；原生 CFN 输出；自带 `filter` 子命令 |
| **Former2 on EC2**（`aws-samples/ec2-former2`）+ SSM 端口转发 | ✅ **合规模式** | 为拒绝本地运行第三方代码的金融/政府客户保留；EC2 零公网暴露 |
| CloudFormation IaC Generator | ❌ | 覆盖窄（EKS/TGW/Network Firewall 差）；3 次/天/账号硬限制 |
| 自研 mapping | ❌ | 400+ 资源规则维护成本过高 |

---

## 3. 用户角色

| 角色 | 描述 |
|------|------|
| AWS 解决方案架构师 | 帮客户做环境 IaC 化/复制 |
| 云运维工程师 | 快速为 DR/测试环境搭基础设施骨架 |
| 金融/政府客户技术负责人 | 合规敏感，不接受第三方工具和数据出境 |
| 普通开发者 | 一次性把现有资源导成 CFN 交付运维 |

---

## 4. 两种运行模式

### 4.1 标准模式（默认推荐）

Former2 CLI 在 **本地笔记本** 运行，调 AWS API。

- `npm install -g former2` 一条命令
- 凭证走本地 AWS CLI profile
- 无需部署任何 AWS 资源
- 端到端 3-10 分钟

**适合**：个人开发、中小团队、非强合规

### 4.2 合规模式（金融/政府）

Former2 部署到 **客户账号内 EC2**，通过 **SSM port forwarding** 访问。

- EC2 在 private subnet，零 ingress SG
- 出站 VPC Interface Endpoint（ssm / ssmmessages / s3），无 NAT
- IMDS 临时凭证，不落本地
- SSM 会话 CloudTrail 全量审计
- 一键 `teardown` 清理

**两种模式共用同一套筛选 + 清洗 + 预检逻辑**，只在 Former2 运行位置上分叉。

---

## 5. 核心功能需求

### F1: 环境准备

**标准模式**
- `install.sh` 装 former2 npm + Python 依赖
- 验证 AWS CLI + 凭证

**合规模式**
- 部署 `aws-samples/ec2-former2` 安全强化 fork：
  - EC2 private subnet，无 public IP / EIP
  - Security Group 无 ingress
  - VPC Endpoint（ssm / ssmmessages / ec2messages / s3）
  - IAM Role: `AmazonSSMManagedInstanceCore` + 只读审计权限
- 参数：VPC ID / Subnet ID / Instance Type（默认 Graviton `t4g.medium`）

### F2: 资源扫描

**标准模式**
```bash
former2 generate --region <r> --services <list> \
  --output-raw-data raw.json \
  --output-cloudformation cfn-full.yml
```
- 支持 `--region` / `--services` / `--exclude-services` / `--profile`
- 输出 `raw.json`（完整 resource metadata）+ 全量 `cfn-full.yml`

**合规模式**
- 通过 SSM port forwarding 访问 Former2 UI（`http://localhost:8080`）
- 用户浏览器勾选 → 导出 CFN YAML
- `scripts/fetch-export.sh` 通过 SSM + S3 临时 bucket 回传

### F3: 资源筛选（Skill 侧，两模式共用）

`scripts/select.py` 加载 raw.json 输出筛选后 physical id 清单，喂给 former2 的 `filter` 子命令或 `--search-filter`。

支持维度：
- `--service Lambda,IAM`（OR）
- `--tag Environment=prod`（可多次，AND）
- `--regex '^myapp-'`（匹配 physical id）
- `--exclude-default`（去默认 VPC/SG/NACL）
- `--physical-ids-file list.txt`（白名单）
- `--exclude-service CloudWatch,KMS`

核心 `apply_filters()` 为 pure function，便于单测。

### F4: CFN 模板清洗（轻量规则引擎，不走 LLM）

`scripts/rewrite_cfn.py` 处理跨账号/跨 region 常见硬编码，纯 Python 规则替换，**不依赖 LLM**：

| 处理项 | 处理方式 |
|--------|----------|
| 硬编码 Account ID | 替换为 `!Sub '${AWS::AccountId}'` |
| 硬编码 Region | 替换为 `!Sub '${AWS::Region}'` |
| 硬编码 AMI ID | 转 `Parameters.AmiId` + `SSM Parameter Store` 默认值 |
| 硬编码 AZ | 转 `!Select [0, !GetAZs '']` |
| IAM Role ARN 跨账号引用 | 抽成 Parameters，部署时传值 |
| KMS CMK 引用 | 抽成 Parameters，部署时传目标账号 CMK ARN |
| 默认资源（VPC/SG/NACL） | 按用户选择移除或保留 |
| DeletionPolicy | 关键资源（RDS/S3 with data）自动加 `Retain` |

> **为什么不走 LLM**：规则固定、可测试、确定性强，LLM 反而引入不确定性。复杂场景（跨账号 IAM principal 重写）作为 Phase 3 可选。

### F5: 预检与校验

部署前执行：
- `cfn-lint` 语法校验
- `aws cloudformation validate-template` API 校验
- 目标账号配额预检（VPC / EIP / IAM Role / Lambda 并发数）
- AMI 在目标 region 可用性
- KMS CMK 在目标账号存在性
- **Scope 检查**：对照 raw.json vs 清洗后 cfn.yml，报告遗漏资源

输出差异报告，人工确认再部署。

### F6: 部署引导

生成一键部署命令：
```bash
aws cloudformation deploy \
  --template-file out/cleaned.yml \
  --stack-name <name> \
  --region <target-region> \
  --profile <target-profile> \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides AmiId=ami-xxx KmsKeyArn=arn:...
```

配 checklist：
- [ ] 目标账号/region 权限确认
- [ ] AMI 跨 region 可用（必要时 AMI Copy）
- [ ] ECR 镜像同步（含容器工作负载时）
- [ ] Secrets Manager / SSM 密钥同步
- [ ] KMS CMK 准备

### F7: 合规与审计

- **标准模式**：凭证走本地 CLI profile，模板不出本机
- **合规模式**：凭证不离客户 EC2；SSM 会话 CloudTrail 审计；EC2 零公网暴露
- Skill 强制落盘审计日志：扫描参数 / 筛选条件 / 清洗规则 diff / 时间戳 / 资源清单
- **LLM 边界**：本版本 F4 不用 LLM，合规审查简单；如未来启用 LLM 重构，由宿主代理配置客户自有 Bedrock

### F8: 资源清理

- `scripts/teardown.sh` 删除合规模式 EC2 栈
- 本地临时文件按参数保留或清理

---

## 6. 非功能需求

| 需求 | 描述 |
|------|------|
| 安全合规 | 凭证与模板不外传；合规模式 EC2 零公网 |
| 确定性 | 清洗规则 pure function，同输入同输出；无 LLM 不确定性 |
| 可测试性 | 筛选 + 清洗全部可单测（fixture JSON + snapshot） |
| 可维护性 | former2 当黑盒；自有代码 ≤ 500 行 Python + ≤ 200 行 Node |
| 可扩展性 | 筛选维度、清洗规则可插件化 |
| 可审计 | 扫描 + 筛选 + 清洗 + 部署全链路日志 |
| 资源清理 | 一键 teardown |

---

## 7. 技术架构

### 7.1 标准模式

```
┌─────────────────────────────────────────────┐
│  本地笔记本                                   │
│                                             │
│  ┌─────────────────────────────────────┐   │
│  │  宿主代理（Claude Code / Kiro / OC） │   │
│  └────────────────┬────────────────────┘   │
│                   │ 加载 SKILL.md           │
│                   ▼                         │
│  ┌─────────────────────────────────────┐   │
│  │   aws-reverse-skill                  │   │
│  │   scan.js ──► select.py ──► filter  │   │
│  │                          └──►rewrite │   │
│  │                          └──►cfn-lint│   │
│  └────────────────┬────────────────────┘   │
└───────────────────┼──────────────────────────┘
                    │ AWS CLI profile
                    ▼
           ┌────────────────────┐
           │  客户 AWS 账号      │
           │  (Describe/List)    │
           └────────────────────┘
```

### 7.2 合规模式

```
┌─────────────────────────────────────────────┐
│  本地笔记本                                   │
│  ┌──────────┐     ┌──────────────────────┐ │
│  │ 浏览器    │──── │ aws-reverse-skill    │ │
│  │localhost │     │ (SSM + fetch + clean)│ │
│  │  :8080   │     └──────┬───────────────┘ │
│  └────┬─────┘            │                  │
│       │ SSM Port FW      │ AWS CLI          │
└───────┼──────────────────┼──────────────────┘
        │                  │
        ▼                  ▼
┌─────────────────────────────────────────────┐
│  客户 AWS 账号                               │
│  ┌── Private Subnet（零 ingress）─────────┐ │
│  │  EC2: Former2 Web（Nginx :80 local）   │ │
│  └──────┬─────────────────────────────────┘ │
│         ▼ (出站 443)                        │
│  ┌── VPC Endpoints ───────────────────────┐ │
│  │ ssm / ssmmessages / ec2messages / s3   │ │
│  └────────────────────────────────────────┘ │
└─────────────────────────────────────────────┘

产物：Former2 UI 导出 CFN ──► S3 临时 bucket ──► 本地
                                               │
                                               ▼
                               select ──► rewrite ──► 预检 ──► 部署
```

---

## 8. Skill 产物结构

```
aws-reverse-skill/
├── SKILL.md                       # 宿主代理入口
├── README.md                      # 用户文档
├── install.sh
├── package.json                   # former2 npm 依赖
├── doc/
│   ├── PRD.md                     # 本文件
│   ├── ARCHITECTURE.md
│   └── PHASE1-TASKS.md
├── references/
│   ├── former2-cfn.yaml           # 合规模式 EC2 模板（强化版）
│   ├── rewrite-rules.md           # F4 清洗规则清单
│   └── review-checklist.md        # F5 预检 checklist
├── config/
│   ├── default-filters.json       # 筛选预设
│   └── default-rewrites.json      # 清洗规则预设
├── scripts/
│   ├── scan.js                    # F2 标准扫描
│   ├── select.py                  # F3 筛选
│   ├── rewrite_cfn.py             # F4 清洗（规则引擎）
│   ├── precheck.sh                # F5 预检
│   ├── preview.py                 # 资源概览
│   ├── deploy-former2.sh          # F1 合规部署
│   ├── ssm-portforward.sh         # 合规连接
│   ├── fetch-export.sh            # 合规回传
│   └── teardown.sh                # F8 清理
├── tests/
│   ├── test_select_filter.py
│   ├── test_rewrite_cfn.py
│   └── fixtures/
│       ├── sample_raw.json
│       └── sample_cfn_dirty.yml
└── workflows/
    ├── export-all.md
    ├── export-by-service.md
    ├── export-by-tag.md
    └── cross-account-deploy.md
```

---

## 9. 用户交互流程

### 标准模式

```
用户："导 ap-northeast-1 生产环境的 Lambda + IAM 到 us-east-1 的 CFN"

宿主加载 skill 后：
 1. 确认源 region / profile / 服务范围
 2. scan.js → raw.json + cfn-full.yml（~127 资源）
 3. preview.py 打印摘要：Lambda 15、IAM 42...
 4. 对话式筛选：
    - "tag Environment=prod" → select.py --tag Environment=prod
    - 命中 12 Lambda + 8 IAM
 5. former2 filter --regex-filter ... → cfn-filtered.yml
 6. rewrite_cfn.py → cleaned.yml（替换 Account ID / Region / AMI 等）
 7. precheck.sh：cfn-lint + validate-template + 配额预检
 8. 输出部署命令 + checklist + 审计日志路径
```

### 合规模式

```
用户："源账号 vpc-0abc123 生成 CFN 部署到 us-east-1，合规模式"

 1. 确认源 VPC / 目标 region / account
 2. 检测 Former2 EC2，未部署则 deploy-former2.sh（~5 分钟）
 3. ssm-portforward.sh → http://localhost:8080
 4. 用户在浏览器 Former2 UI 扫描 + 导出 CFN YAML
 5. fetch-export.sh 通过 SSM + S3 回传
 6. select.py 筛选 → rewrite_cfn.py 清洗 → precheck.sh 预检
 7. 输出 cleaned.yml + checklist + 审计日志
 8. 可选 teardown.sh 清理 EC2
```

---

## 10. 已知限制

| 限制 | 说明 |
|------|------|
| Former2 扫描覆盖 | 新服务可能滞后；EKS 集群内 K8s 资源不在范围 |
| CLI 版 experimental | 官方标注 experimental，边缘资源类型可能有 bug |
| 跨 Partition | China ↔ Global 不支持（ARN / IAM principal 不同） |
| 数据层 | EC2 EBS、RDS 数据、S3 对象不处理；需 AWS Backup/CRR 独立方案 |
| Resource Import | 生成的是新建资源，不支持 import 现有资源到 CFN 栈 |
| F4 清洗规则 | 规则引擎只处理确定性场景，复杂 IAM principal 跨账号重写需人工 |
| 合规模式 EC2 成本 | `t4g.medium` ~$25/月；用完 teardown |
| 一次性定位 | 不追求"生成后持续演进"的代码质量；长期维护请客户自己手工重构 |

---

## 11. 成功指标

| 指标 | 目标值 |
|------|-------|
| 标准模式端到端完成时间（100 资源） | < 10 分钟 |
| 合规模式端到端完成时间（中小 VPC） | < 1 小时 |
| former2 扫描成功率 | > 95% |
| SSM 建连成功率（合规模式） | > 98% |
| 生成 CFN `cfn-lint` 通过率 | > 95% |
| 生成 CFN `validate-template` 通过率 | > 98% |
| 跨账号/region 一次性 `deploy` 成功率 | > 80%（剩 20% 人工调 IAM/KMS 依赖） |
| 人工调整工作量减少 | ≥ 70%（对比从零手写 CFN） |
| 金融客户合规评审通过 | 100% 0 凭证/模板泄露；0 公网暴露 |

---

## 12. 分阶段实施

| 阶段 | 范围 | 预计 |
|------|------|------|
| **Phase 1（MVP）** | 标准模式：scan + select（3 维度） + rewrite（核心规则） + cfn-lint + tests | **1 人日** |
| **Phase 2** | 完整交互：preview / 多规则预设 / workflow 文档 / 扩展清洗规则 | 1-2 人日 |
| **Phase 3** | 合规模式：EC2 + SSM + fetch + teardown | 1-2 人日 |
| **Phase 4（可选）** | LLM 重构 / CDK 输出（通过 cdk migrate 或 aws-iac-mcp） | 按需 |
| **Phase 5（可选）** | AMI 跨 region / Organizations 多账号 / Control Tower | 按需 |

### Phase 1 MVP 定义

**必做**：
- `scripts/scan.js`（调 former2 CLI）
- `scripts/select.py`（3 个筛选维度：service/tag/regex）
- `scripts/rewrite_cfn.py`（5 个核心规则：AccountId / Region / AMI / AZ / DeletionPolicy）
- `scripts/precheck.sh`（cfn-lint + validate-template）
- `tests/` 至少 6 个测试（select 3 个 + rewrite 3 个）
- `SKILL.md` 定义对话触发流程
- 1 个 workflow 文档：`export-by-tag.md`

**不做**：合规模式、LLM、CDK、交互式 preview、多格式并行、配额预检（Phase 2）

---

## 13. 交付后生命周期

**本 Skill 定位是「一次性生成」，不追求长期维护友好度。**

- 客户拿到 CFN YAML 后，如有后续改动：
  - 小改（< 20% 资源）→ 直接在 YAML 上改，用 `aws cloudformation deploy` 更新
  - 中改（20-50%）→ 建议 `cfn-lint` + 代码 review 后增量部署
  - 大改 / 长期演进 → 客户自己重构为 CDK / Terraform，本 skill 不负责
- **Drift Detection**：目标环境配 CloudFormation drift detection + 定期人工对账
- **降级路径**：如客户完全不维护，至少保留初始 CFN 模板作为 baseline

---

## 14. 参考资料

- [iann0036/former2](https://github.com/iann0036/former2)
- [former2 npm CLI](https://www.npmjs.com/package/former2)
- [aws-samples/ec2-former2](https://github.com/aws-samples/ec2-former2)
- [AWS Blog: Accelerate IaC with Former2](https://aws.amazon.com/blogs/opensource/accelerate-infrastructure-as-code-development-with-open-source-former2/)
- [DNAnexus DR 案例](https://aws.amazon.com/blogs/opensource/how-dnanexus-used-the-open-source-former2-project-to-create-infrastructure-as-code-templates-for-their-disaster-recovery-pipeline/)
- [AWS SSM Port Forwarding](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-sessions-start.html#sessions-start-port-forwarding)
- [CloudFormation Deploy CLI](https://docs.aws.amazon.com/cli/latest/reference/cloudformation/deploy/index.html)

---

## 修订历史

| 版本 | 日期 | 变更 |
|------|------|------|
| v0.1 | 2026-04-17 | 小乖乖初稿 |
| v0.2 | 2026-04-17 | 区分 IaC 化与 DR；强化合规边界 |
| v0.3 | 2026-04-17 | 引擎切换 Former2；采用 ec2-former2 + SSM |
| v0.4 | 2026-04-17 | 宿主定位澄清：AgentSkill 规范 |
| v1.0 | 2026-04-17 | 编程猫合并定稿：双运行模式 |
| **v1.1** | **2026-04-17** | **CFN 聚焦**：一次性部署定位下 CDK/LLM 路径不值得做，Phase 1 MVP 只输出 CFN YAML；F4 清洗改规则引擎而非 LLM；工作量砍半（1 人日出 MVP） |
