# 架构细节：aws-reverse-skill

## 分层

```
Layer 4: Agent 对话层          ← SKILL.md（自然语言 ↔ 脚本参数）
Layer 3: 业务编排层            ← scripts/ 里的 Python/Node orchestrator
Layer 2: 筛选 + 清洗引擎       ← select.py + rewrite_cfn.py（我们自己写）
Layer 1: former2 引擎          ← npm 包（扫描 + CFN 输出，黑盒只用不改）
Layer 0: AWS API               ← boto3 / aws-sdk-js
```

*分层原则*：上层可换（SKILL 改对话、筛选/清洗加维度），下层稳定（former2 当黑盒）。former2 升级只需锁 package.json。

## 五要素权衡（遵循大乖乖架构偏好）

| 维度 | 设计 |
|------|------|
| 可维护性 | 🟢 former2 当黑盒不改；自有代码 ≤ 500 行 Python + ≤ 200 行 Node |
| 可测试性 | 🟢 筛选和清洗均为 pure function，fixture JSON/YAML 单测；无 LLM 不确定性 |
| 可部署性 | 🟢 skill 装到 ~/.openclaw/skills/，`npm i -g former2` 装依赖 |
| 可扩展性 | 🟡 former2 不支持的资源类型要补；筛选/清洗维度插件化 |
| 可用性 | 🟡 依赖 former2 CLI + AWS API；大账号扫描慢（后续加缓存） |

*核心取舍*：不追求"长期维护友好"或"完美 L2 CDK"，只做"80% 可用的 CFN YAML 起点，剩 20% 人工补"。和客户画像（一次性部署、运维主导）对齐。

## 为什么清洗走规则引擎不走 LLM

| 维度 | 规则引擎（选用） | LLM 方案（放弃） |
|------|:---:|:---:|
| 确定性 | 🟢 同输入同输出 | 🔴 同输入可能不同输出 |
| 可测试 | 🟢 snapshot test | 🔴 难稳定断言 |
| 合规审计 | 🟢 规则代码可审 | 🔴 模型+prompt 要整套审 |
| 覆盖度 | 🟡 只处理确定场景 | 🟢 能处理边缘 |
| 成本 | 🟢 0 token | 🔴 每次扫描都花钱 |
| 依赖 | 🟢 纯 Python | 🔴 Bedrock / MCP 依赖 |

一次性部署场景，*确定性 + 合规简单* 的价值远大于"能处理边缘"。边缘场景（跨账号 IAM principal 重写、Custom Resource）放人工。

## 与其它 skill 的协作

- *aws-api skill*：补 former2 不支持的资源；部署后冒烟
- *aws-cloudwatch skill*：部署后查健康度
- *aws-pricing skill*：部署前估算目标环境月成本
- *aws-iac skill*：cfn-lint + cfn-guard 深度校验
- *s3-vector-skill*：把扫描结果 + 生成模板入知识库

## 不做什么（scope out）

- ❌ CDK / Terraform / Pulumi 输出（一次性部署用不上 CDK 优势）
- ❌ LLM 重构（规则引擎已够）
- ❌ Resource Import（CFN 新建即可）
- ❌ 跨 Partition（China ↔ Global ARN 不兼容）
- ❌ 数据层迁移（用 AWS Backup / CRR）
- ❌ Web UI（对话就是 UI）
- ❌ 长期维护脚手架（客户自己后续怎么演进不是本 skill 责任）
