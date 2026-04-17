# aws-reverse-skill

> 从现有 AWS 环境一次性生成可部署 CloudFormation 模板的 AgentSkill。

**方案总览请看 [`doc/PRD.md`](./PRD.md)（v1.1 定稿）**

---

## 一句话定位

用自然语言把已有 AWS 资源扫一遍、筛一筛、清一清，输出可 `aws cloudformation deploy` 的 YAML。*不做 CDK，不做 LLM，不做持续迭代场景。*

---

## 技术路线

```
former2 CLI 扫描 ──► select.py 筛选 ──► former2 filter 出 CFN
                                            │
                                            ▼
                              rewrite_cfn.py 规则引擎清洗
                                            │
                                            ▼
                                  cfn-lint + validate-template
                                            │
                                            ▼
                              aws cloudformation deploy
```

*不经过 LLM*：清洗用 pure Python 规则（替换 Account ID / Region / AMI / AZ、加 DeletionPolicy），确定性、可单测、合规简单。

---

## 两种模式

| 模式 | Former2 运行位置 | 适用 |
|------|------------------|------|
| *标准* | 本地 `npm install -g former2` | 默认推荐，3-10 分钟跑完 |
| *合规* | 客户账号内 EC2 + SSM 端口转发 | 金融/政府，凭证不出境，EC2 零公网 |

两模式共用筛选 + 清洗 + 预检逻辑。

---

## 实施阶段

- *Phase 1 MVP*（1 人日）：标准模式 scan + select（service/tag/regex） + rewrite（5 条核心规则） + cfn-lint + tests
- *Phase 2*：preview、规则预设、workflow 文档（1-2 人日）
- *Phase 3*：合规模式 EC2 + SSM（1-2 人日）
- *Phase 4+*（可选）：LLM 重构、CDK 输出（按需）

详见 [`PHASE1-TASKS.md`](./PHASE1-TASKS.md)。

---

## 相关文档

- [`PRD.md`](./PRD.md) — 产品需求 v1.1
- [`ARCHITECTURE.md`](./ARCHITECTURE.md) — 分层架构 + 五要素权衡
- [`PHASE1-TASKS.md`](./PHASE1-TASKS.md) — MVP 任务清单
