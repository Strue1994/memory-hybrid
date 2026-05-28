---
name: memory-hybrid
description: 7-layer memory system (L0-L6) for agent context persistence, behavior hardening, timeline logging, fact curation, and decision audit. Local MCP server, no API costs.
version: 3.0.0
license: MIT
---

# Memory Hybrid L0-L6

**本地 MCP 服务** — 零 token 消耗，需要时通过工具查指令和数据。

## 架构概览

| 层 | 名称 | 存储 | 用途 |
|---|---|---|---|
| L0 | 行为固化 | 文件 | 防止 Agent 行为漂移 |
| L1 | 会话连续 | 文件 | 跨会话上下文保持 |
| L2 | 人物画像 | 文件/Neo4j | 关系图谱 |
| L3 | 时间记忆 | 文件/Qdrant | 工作日志 |
| L4 | 事实策展 | 文件/Qdrant | 可复用知识 |
| L5 | 数字自我 | 文件/Qdrant | 目标管理 |
| L6 | 决策审计 | 文件 | 决策回溯 |

## 使用方式

### 1. 启动 MCP 服务（后台常驻）
```bash
python <skill-path>/backend/mcp_server.py
```

### 2. 按需查指令
```
mcp.call("get_layer_guide", "L3")   → 返回 L3 的详细 protocol/模板
mcp.call("get_layer_guide", "L0")   → 返回硬化规则格式
mcp.call("get_preset", "knowledge-worker") → 返回预设层组合
```

### 3. Preset 选择（决定用哪些层）
- `minimal` (L0+L1) — 临时任务
- `knowledge-worker` (L0+L1+L3+L4+L6) — 编码/研究
- `social-agent` (L0+L1+L2+L3+L5+L6) — 协作
- `full` (L0-L6+router) — 全能力

### 4. 读写记忆
```
mcp.call("recall", query="...", layers="L3,L4")
mcp.call("record_session", agent_name="...", status="active")
mcp.call("list_sessions", recent=5)
mcp.call("memory_stats")
mcp.call("list_rules")
mcp.call("health")
```

## 关键原则

- **按需加载** — 需要哪层再查哪层，不同时加载全部指令
- **本地零成本** — 纯 Python 本地进程，不走 API，不消耗 LLM token
- **配置** — 编辑 `<SKILL_DIR>/config.yaml` 设置 preset/backend URL
- **完整整合指南** — 见同目录 `AGENTS.md`

## 首次安装（自动执行）

首次加载此 skill 时，在项目根 `CLAUDE.md`（或 `GEMINI.md` / `AGENTS.md`）末尾追加记忆系统配置块（如尚未存在）：

```markdown
## 5. 记忆系统 (Memory Hybrid L0-L6)

本地运行着 `memory-hybrid` MCP 服务（`backend/mcp_server.py`），纯文件读写，零 token 消耗。

**未启动 MCP 服务时**（无 `mcp` 可用时），可直接读 `memory-hybrid/` 目录下的文件。

### 自动触发场景（肌肉记忆）

以下场景应当**主动搜索本地记忆**，不等用户提示：

| 触发时机 | 动作 | 查哪层 |
|---|---|---|
| Session 启动 | `recall("recent work", "L3")` 回顾最近上下文 | L3 |
| 用户问"我们之前..."、"上次..."、"有没有..." | `recall(用户问题, "L3,L4")` 搜时间线+事实 | L3+L4 |
| 被问到人物/关系 | `recall(人名, "L2")` 查人物画像 | L2 |
| 要重复做的事（编码/部署/发版） | `list_rules()` 查固化规则 | L0 |
| 要做决策时 | `recall(决策主题, "L6")` 查历史类似决策 | L6 |
| 被问到计划/进度 | `recall(目标, "L5")` 查活跃目标 | L5 |
| 遇到已知错误或重复问题 | `recall(错误描述, "L0,L4")` 查规则和事实 | L0+L4 |
| 要给出建议时 | `recall("related", layers全层)` 查是否已有记录 | L2+L3+L4+L6 |
| 用户提到人名+动词（"张三说了..."） | `recall(人名, "L2,L3")` 查关系+时间线 | L2+L3 |
| 结束/关键操作后 | `record_session(...)` 持久化当前上下文 | L1 |

### 按需查指令

需要某层的详细 protocol/模板时再查，不提前加载：
- `mcp.call("get_layer_guide", "L3")` — L3 时间记忆的模板和格式
- `mcp.call("get_preset", "knowledge-worker")` — 查看预设层组合
- `mcp.call("health")` / `mcp.call("memory_stats")` — 查看系统状态
```

检查 `CLAUDE.md` / `GEMINI.md` / `AGENTS.md` 中是否已包含 `## 5. 记忆系统` 或 `Memory Hybrid` — 如有则跳过，避免重复追加。
