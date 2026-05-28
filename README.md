# Memory System — Hybrid (L0–L7)

将 [JQR-crbf/memory-system](https://github.com/JQR-crbf/memory-system) 的 **FastAPI+Qdrant+Neo4j** 后端能力，与可组合的 **OpenCode Skills** 架构融合，形成的 7 层混合记忆系统。

---

## 架构总览

```
┌─────────────────────────────────────────────────────────────┐
│                    Memory Router                              │
│            启发式分类器 / API 统一路由                          │
├─────────────────────────────────────────────────────────────┤
│  L6 决策审计     │  L5 数字自我     │  L4 事实策展             │
│  (纯文件)        │  (文件+Qdrant+API)│  (文件+Qdrant)          │
├──────────────────┼──────────────────┼─────────────────────────┤
│  L3 时间记忆     │  L2 人物关系     │  L1 会话连续  │  L0 固  │
│  (文件+Qdrant)   │  (文件+Neo4j)    │  (纯文件)     │  (文件)  │
├──────────────────┴──────────────────┴──────────────┴────────┤
│                     Backend Service                           │
│            FastAPI + Qdrant + Neo4j (可选，自动降级)          │
└─────────────────────────────────────────────────────────────┘
```

## 设计原则

### 1. 每层独立
每层就是一个 SKILL 段落。无后端时降级为纯文件模式，有后端时启用全部能力。

### 2. 双通道存储
```
写入 → 文件 (即时) + 后端 (异步)
读取 → 后端 (优先) → 文件 (降级)
```

### 3. 离线优先
所有核心功能可在无后端时正常工作。后端只是加速器，不是必需品。

### 4. 行为固化 > 知识存储
与原版 memory-system 的核心差异：新增 L0 行为固化层，确保 Agent 在不稳定环境中维持行为一致。

### 5. 生命周期先于扩层
V2 开始统一 memory lifecycle：`captured -> pending -> verified -> curated -> hardened -> decayed -> archived`。

### 6. Router 必须感知任务模式
V2 的 unified recall 不再只看 query，也看 `task_mode`，例如 `debug` 优先 L6/L3，`implement` 优先 L4/L3。

## 与原版 (JQR-crbf/memory-system) 的关键差异

| 维度 | 原版 | 此版本 |
|---|---|---|
| 架构 | 单体后端服务 | 可组合 Skills + 可选后端 |
| 离线能力 | 不可用 | 完整离线 |
| 行为固化 | 无 | L0 + 2-Strike + 3级固化 |
| 会话连续 | 无 | L1 + WAL 协议 |
| 人物关系 | Neo4j 仅存储 | Neo4j + 文件双通道 |
| 决策审计 | 无 | L6 + 状态机 |
| 生命周期 | 弱 | V2 增加 shared lifecycle |
| 路由 | query-aware | V2 task-aware |
| 部署 | Docker Compose | 同左 (新增降级路径) |

## V2 Phase 1

当前已补上的第一阶段能力：

- shared lifecycle metadata for L3/L4/L5/L6
- task-aware router (`general/debug/implement/plan/social`)
- L6 -> L0 hardening candidate promotion endpoint

## V2 Phase 2

当前已补上的第二阶段能力：

- file-backed hardening candidate review queue
- lifecycle decay/archive scanner script
- minimal benchmark/evaluation script

## V2 Phase 3

当前已补上的第三阶段能力：

- approve / reject candidate into `rules.yaml`
- lifecycle scanner 支持真实目录扫描
- benchmark 支持 JSON 用例配置

## V2 Phase 4

当前已补上的第四阶段能力：

- candidate 重复审批保护
- approve/reject 审计日志
- scanner 支持更真实的 timeline/facts/goals/decisions fixture
- benchmark 输出 usefulness / false helpfulness 指标

## V2 Phase 5

当前已补上的第五阶段能力：

- scanner 支持更贴近项目真实目录约定的 markdown/yaml/json
- rules 历史快照与回滚
- benchmark 覆盖 `plan` / `social` 等跨层场景

## V2 Phase 6

当前已补上的第六阶段能力：

- benchmark 报告持久化到 `tools/reports/`
- review/rules history 查询接口
- scanner 输出 summary 和 recommendations

## V2 Phase 7

当前已补上的第七阶段能力：

- benchmark 趋势汇总
- history 查询过滤
- scanner action plan
- 工具优先尝试真实 `memory-hybrid/` 目录

## V2 Phase 8

当前已补上的第八阶段能力：

- `memory-hybrid/` 目录自动初始化
- scanner 直接生成 remediation candidates
- benchmark 追加 recent-window 趋势
- history API 支持 `offset/limit/recent`

## V2 Phase 9

当前已补上的第九阶段能力：

- remediation candidate 与 hardening candidate 统一格式
- scanner 生成可审批候选
- benchmark 回归检测
- history API 支持 `since/until`

## V2 Phase 10

当前已补上的第十阶段能力：

- remediation candidate 可走同一 approve -> rules 流
- benchmark 阈值告警
- scanner `by_type` 聚合统计
- history API 支持关键词 `q` 过滤

## V2 Phase 11

当前已补上的第十一阶段能力：

- remediation candidate severity / priority
- benchmark Markdown 摘要报告
- 过滤结果附带 count 统计
- rules enable / disable

## V2 Phase 12

当前已补上的第十二阶段能力：

- rules enable/disable 审计
- candidate / rule 关联视图
- remediation candidate priority 排序
- benchmark Markdown 趋势解释

## V2 Phase 13

当前已补上的第十三阶段能力：

- candidate/rule/review 统一 timeline 视图
- scanner `by_severity` 聚合
- benchmark Markdown 历史对比表
- rules API 支持 `enabled` / `q` 过滤

## V2 Phase 14

当前已补上的第十四阶段能力：

- timeline 支持 `kind/candidate_id/rule_id` 过滤
- benchmark 报告保留策略
- rules disabled reason
- 高 severity remediation 自动审批建议

## V2 Phase 15

当前已补上的第十五阶段能力：

- timeline 支持 `since/until`
- benchmark 输出清理统计
- rule toggle 事件进入统一 timeline
- auto-approve remediation 专门查询

## V2 Phase 16

当前已补上的第十六阶段能力：

- timeline 过滤统计增强
- auto-approve severity 阈值配置
- benchmark 稳定/回归摘要
- rules history diff 视图

## Timeline Export

新增导出接口：

`GET /v1/layers/L0/timeline/export`

支持格式：
- `html` (默认)
- `markdown`
- `json`

HTML 导出现在包含：
- summary 区块
- filters 元信息
- 更可读的表格样式
- 页内目录
- severity 高亮
- kind 分组
- 可选批量 zip 导出
- 可选自定义视图列表

### Candidate Review Completion

现在 review queue 不只是 `pending`，还支持：

- approve: 写入 `hardening/rules.yaml`
- reject: 标记候选为 `rejected`

### Configurable Benchmark

基准用例文件：

`.opencode/skills/memory-hybrid/backend/tools/benchmark_cases.json`

### Review Queue

新端点会把 L6 promotion candidate 写到：

`memory-hybrid/hardening/candidates/<candidate-id>.yaml`

默认状态：`pending`

### Scanner / Benchmark

运行方式：

```bash
D:\Python\Python313\python.exe .opencode/skills/memory-hybrid/backend/tools/lifecycle_scan.py
D:\Python\Python313\python.exe .opencode/skills/memory-hybrid/backend/tools/benchmark_memory.py
```

## 使用

```yaml
# AGENTS.md
init:
  - skill(name="memory-hybrid")
  - set_var("MEMORY_PRESET", "knowledge-worker")
```

或加载特定层组合：
```yaml
init:
  - skill(name="memory-hybrid")
  - set_var("MEMORY_PRESET", "minimal")
```

## 文件结构

```
memory-hybrid/
├── SKILL.md            # 可加载的技能定义 (主入口)
├── README.md           # 本文档
├── AGENTS.md           # AGENTS.md 配置示例
├── config.yaml         # 配置文件模板
├── backend/
│   ├── docker-compose.yml
│   ├── Dockerfile
│   ├── .env.example
│   ├── requirements.txt
│   └── api/
│       ├── main.py           # FastAPI 应用
│       ├── config.py         # 环境变量配置
│       ├── qdrant_client.py  # Qdrant 向量客户端
│       ├── neo4j_client.py   # Neo4j 图客户端
│       └── router.py         # 查询分类器
```

## 预设速查

| Preset | Layers | 后端 | 场景 |
|---|---|---|---|
| `minimal` | L0+L1 | 不需要 | 临时任务 |
| `knowledge-worker` | L0+L1+L3+L4+L6 | Qdrant 推荐 | 编码/研究 |
| `social-agent` | L0+L1+L2+L3+L5+L6 | Qdrant+Neo4j | 协作 |
| `full` | L0-L7+Router | 必须 | 生产 |

## 部署后端

```bash
cd memory-hybrid/backend
cp .env.example .env   # 编辑配置
docker-compose up -d
curl http://localhost:8000/health
```
