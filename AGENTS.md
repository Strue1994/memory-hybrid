# Memory System Hybrid — AGENTS.md Integration Reference

> 此文件供 OpenClaw、Hermes、OpenHuman 等 Agent 配置记忆系统时参考。
> 在 Agent 的 `AGENTS.md` 或系统提示中引入对应配置块即可。

---

## 通用配置

所有 Agent 在 `init:` 中加载技能：

```yaml
init:
  - skill(name="memory-hybrid")
  - set_var("MEMORY_PRESET", "knowledge-worker")
  - set_var("BACKEND_URL", "http://localhost:8000")   # 可选，无后端时纯文件模式
  - set_var("NEO4J_URI", "bolt://localhost:7687")     # 可选，无时 L2 降级纯文件
```

## Agent Profile 示例

```yaml
name: "dev-agent"
profile:
  role: "developer"
  preset: "knowledge-worker"
  layers: [L0, L1, L3, L4, L6]
```

## 层激活说明

| 层 | 用途 | 适用 Agent |
|---|---|---|
| `L0` | 行为固化 (Behavior Hardening) | 所有 Agent，始终开启 |
| `L1` | 会话连续 (Session Continuity) | 所有 Agent，始终开启 |
| `L2` | 人物画像 + 关系图谱 (Persona Graph) | 社交/协作型 Agent |
| `L3` | 时间记忆 (Temporal Memory) | 知识工作型 Agent |
| `L4` | 事实策展 (Fact Curation) | 知识工作型 Agent |
| `L5` | 数字自我 (Digital Self / Goals) | 目标驱动型 Agent |
| `L6` | 决策审计 (Decision Audit) | 所有 Agent |
| `Router` | 查询路由 (Query Classifier) | 全部能力时开启 |

---

## OpenClaw 集成

OpenClaw（oh-my-openagent 的 Discord/Telegram/webhook 模块）加载 memory-hybrid 后，记忆系统自动覆盖所有通道：

**在 OpenClaw Agent 提示中追加：**

```
skill(name="memory-hybrid")
```

**建议 preset:** `social-agent`（多人协作场景）或 `knowledge-worker`（单人开发场景）

**跨通道记忆共享：** OpenClaw 的多个通道（Discord、Telegram、Webhook）共用同一 `memory_root`，L3 时间记忆 + L2 人物关系自动聚合所有通道交互。

**OpenClaw 特有的记忆写入点：**
- 每次消息响应前 → L1 SESSION-STATE.md
- 每日交互摘要 → L3 timeline
- 用户画像更新 → L2 profiles/humans/
- 关键决策（如通道切换、命令执行）→ L6 decisions/

---

## Hermes 集成

Hermes（oh-my-openagent 的消息路由/事件总线 Agent）使用 memory-hybrid 作为事件存储和查询层：

**在 Hermes Agent 提示中追加：**

```
skill(name="memory-hybrid")
set_var("MEMORY_PRESET", "knowledge-worker")
```

**建议 preset:** `knowledge-worker`（L0+L1+L3+L4+L6）

**Hermes 特有的记忆写入点：**
- 事件路由决策 → L6 decisions/
- 路由模式学习 → L4 facts/curated/
- 每日路由统计 → L3 timeline

---

## OpenHuman 集成

OpenHuman（oh-my-openagent 的人类协作接口 Agent）使用 memory-hybrid 记录人类交互历史和协作模式：

**在 OpenHuman Agent 提示中追加：**

```
skill(name="memory-hybrid")
set_var("MEMORY_PRESET", "social-agent")
```

**建议 preset:** `social-agent`（需要 L2 人物关系 + L5 目标跟踪）

**OpenHuman 特有的记忆写入点：**
- 人类交互记录 → L2 profiles/humans/
- 协作模式提取 → L4 facts/curated/
- 任务进度跟踪 → L5 goals/active.yaml
- 交互决策审计 → L6 decisions/
- 每日协作摘要 → L3 timeline

---

## 文件结构公约

所有 Agent 统一使用如下目录结构（`memory_root` 可配置，默认 `memory-hybrid/`）：

```
memory-hybrid/
  sessions/
    SESSION-STATE.md              # 当前会话状态 (WAL)
    sessions-archive/             # 历史会话存档
  profiles/
    self.md                       # Agent 自画像
    humans/                       # 人类交互者画像
    agents/                       # 其他 Agent 画像
  timeline/YYYY/MM/YYYY-MM-DD.md  # 每日工作日志
  facts/
    curated/                      # 已策展事实
    pending/                      # 待验证事实
  goals/
    active.yaml                   # 活跃目标
    completed.yaml                # 已完成目标
    archived/                     # 归档目标
  decisions/YYYY/MM/              # 决策日志
  state-machines/                 # 状态机跟踪
  hardening/
    rules.yaml                    # 固化规则
    strikes.log                   # 2-Strike 审计日志
    selectors/                    # 行为选择器
    candidates/                   # 固化候选
    history/                      # 规则历史快照
    review-log.yaml               # 审批日志
```

---

## 后端 API

当 `BACKEND_URL` 配置时，Agent 可通过 HTTP 调用后端服务：

```python
# 示例：Agent 用 Python 调用记忆检索
import requests
BACKEND = "http://localhost:8000"

response = requests.get(f"{BACKEND}/v1/recall", params={
    "query": "FastAPI async 模式",
    "layers": "L3,L4",
    "top_k": 5
})
print(response.json())
```

所有端点列表见 [SKILL.md Backend 章节](SKILL.md)。
