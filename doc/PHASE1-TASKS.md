# Phase 1 MVP 任务清单（CFN 聚焦版）

> 执行目录：`/home/ubuntu/tech/aws-reverse-skill/`
> 预计：1 人日

## Task 1: 骨架 + 依赖

- [ ] 目录骨架：`scripts/` `tests/fixtures/` `config/` `workflows/` `references/`
- [ ] `package.json` 声明 `former2` 依赖 + `npm install former2`
- [ ] `install.sh`：npm install + pip 安装 pytest / cfn-lint
- [ ] 验证 `npx former2 --help`
- [ ] `.gitignore`（node_modules、__pycache__、out/、*.log）

## Task 2: scan.js

轻量 Node.js wrapper，spawn former2 CLI。

```
node scripts/scan.js --region <r> --services <list> \
  --profile <p> --out-raw raw.json --out-cfn cfn-full.yml
```

职责：参数解析 + child_process.spawn former2 + 错误码传递 + stderr 透传。

## Task 3: select.py

纯 Python，加载 raw.json 输出筛选结果（JSON + 可选 regex 字符串给 former2 filter）。

```
python scripts/select.py \
  --input raw.json \
  --output filtered.json \
  --service Lambda,IAM \
  --tag Environment=prod \
  --regex '^myapp-' \
  --exclude-default \
  --emit-regex-filter  # 输出可直接喂 former2 filter 的 regex 字符串
```

核心：`apply_filters(resources: list, filters: dict) -> list` 是 pure function。

筛选维度（Phase 1 只做 3 个）：
- `--service` OR 多选
- `--tag KEY=VAL` AND 多次
- `--regex` 匹配 physical id

## Task 4: rewrite_cfn.py

纯 Python 规则引擎，输入 CFN YAML 输出清洗后 YAML。

```
python scripts/rewrite_cfn.py \
  --input cfn-filtered.yml \
  --output cleaned.yml \
  --target-region us-east-1 \
  --target-account 123456789012 \
  --rules default  # 或指定 config/default-rewrites.json
```

*5 条核心规则*：
1. 硬编码 Account ID → `!Sub '${AWS::AccountId}'`（识别 12 位数字 in ARN）
2. 硬编码 Region → `!Sub '${AWS::Region}'`（识别已知 region 码）
3. 硬编码 AMI ID → 转 Parameter `AmiId`，默认值用 SSM Parameter Store 公有 path
4. 硬编码 AZ → `!Select [0, !GetAZs '']`
5. 关键资源（RDS/S3 with data）自动加 `DeletionPolicy: Retain`

用 `ruamel.yaml` 保留 CFN YAML 的 `!Sub` / `!Ref` 等短标签格式。

## Task 5: precheck.sh

```bash
bash scripts/precheck.sh cleaned.yml
  - cfn-lint cleaned.yml
  - aws cloudformation validate-template --template-body file://cleaned.yml
```

非零退出码即失败。

## Task 6: SKILL.md

AgentSkill 入口，定义：
- 触发关键词：`导出 AWS`、`reverse engineer AWS`、`生成 CFN`、`former2`、`IaC 化`
- 标准流程 7 步（见 PRD §9）
- 错误话术（扫描失败 / 权限不足 / cfn-lint fail）

## Task 7: 测试

```
tests/
├── conftest.py
├── fixtures/
│   ├── sample_raw.json       # 20-30 条 mock 资源
│   └── sample_cfn_dirty.yml  # 含硬编码 Account/Region/AMI/AZ 的脏 CFN
├── test_select_filter.py     # 3 测试：service/tag/regex
└── test_rewrite_cfn.py       # 3 测试：AccountId/Region/AMI 替换 snapshot
```

## Task 8: workflow 文档

`workflows/export-by-tag.md`：给宿主 Agent 照抄的对话脚本。

## 验收标准

1. `pytest tests/` 全绿，≥ 6 个测试
2. 端到端：跑 `node scripts/scan.js ...` → `select.py` → `former2 filter` → `rewrite_cfn.py` → `precheck.sh`，在 ap-northeast-1 的测试账号产出 `cleaned.yml` 并通过 cfn-lint
3. `git diff --stat` 改动集中在上述文件
4. SKILL.md 能被 OpenClaw 识别

## 不做（放 Phase 2+）

- ❌ 合规模式（EC2 + SSM）
- ❌ LLM / CDK 输出
- ❌ preview 交互
- ❌ 配额预检
- ❌ 多规则预设切换
- ❌ KMS/IAM 跨账号重写

## 交付分支

`feat/v1-mvp` → push GitHub（repo 名 `aws-reverse-skill`），不自动部署。
