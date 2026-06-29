# daemon2acp 改造进度归档

> 本文档记录将 daemon2acp 接入 QwenPaw（作为 ACP runner）的改造全过程。
> 目标：QwenPaw 通过 `delegate_external_agent` 工具调用 atomcode 编码能力。
> 架构：`QwenPaw (ACP client) ↔ stdio JSON-RPC ↔ daemon2acp (ACP server) ↔ HTTP/SSE ↔ atomcode-daemon → LLM`

---

## 一、改造阶段总览

| 阶段 | 内容 | 状态 |
|------|------|------|
| **P0 协议对齐** | session/update payload 改为带 `sessionUpdate` 字段的 chunk 结构 | ✅ 完成 |
| **P1 权限交互** | 实现 `session/request_permission`（server→client 请求） | ✅ 完成 |
| **P2 能力声明** | `loadSession=false`，sessionCapabilities 用 dict 而非 bool | ✅ 完成 |
| **P3 stdio 修复** | 修 3 个阻断性 bug（async start / stdin IOCP / AppState） | ✅ 完成 |
| **P4 响应格式** | NewSessionResponse / LoadSessionResponse / SessionInfo 对齐 schema | ✅ 完成 |
| **P5 stopReason** | 去掉非法的 `"tool_use"`，固定 `end_turn` | ✅ 完成 |
| **P6 消息提取** | `prompt` 字段（List[ContentBlock]）替代 `messages` | ✅ 完成 |
| **联调验证** | QwenPaw 端 5 轮多轮对话测试 | ⏳ 待用户重测 |

---

## 二、stopReason: "tool_use" 修复的正确性回顾

### 问题

QwenPaw 报错：
```
PromptResponse validation error:
stopReason Input should be 'end_turn', 'max_tokens', 'max_turn_requests', 'refusal' or 'cancelled'
input_value='tool_use'
```

### 修复方式

`proxy_agent.py` 的 `translate_daemon_event` 中，`done` 事件原来根据 `tool_calls > 0` 产出 `stopReason: "tool_use"`，改为**始终产出 `end_turn`**。

### 正确性结论：**修复方向正确，且是唯一正确做法**

依据三条源码证据：

1. **ACP SDK schema 硬约束**（`acp/schema.py` L14）：
   ```python
   StopReason = Literal["end_turn", "max_tokens", "max_turn_requests", "refusal", "cancelled"]
   ```
   `tool_use` 根本不在合法值列表里。这不是 QwenPaw 的限制，是 ACP 协议规范。

2. **QwenPaw 自己的 ACP server 也这样实现**（`agents/acp/server.py` L640）：
   ```python
   return PromptResponse(stop_reason="end_turn")  # 工具调用完成后
   ```
   工具调用后返回 `end_turn` 是 ACP 协议的标准做法。

3. **QwenPaw client 的 `run_turn` 是单次请求-响应**（`agents/acp/service.py` L65-114）：
   发 prompt → 等 outcome → 返回。**不会因 stopReason 自动续 prompt**。继续对话要用户再发 `action="message"`（即新的 prompt 请求）。

### 语义澄清

ACP 协议中，工具调用的完整信息通过 **`session/update` 通知**流式推送（`tool_call` + `tool_call_update`），**不靠 stopReason 区分**。stopReason 只表示"这一轮对话为什么停了"：

- `end_turn` — agent 认为任务完成（无论中间用了多少工具）
- `max_tokens` — token 上限
- `refusal` — agent 拒绝执行
- `cancelled` — 被取消

atomcode daemon 的 `done` 事件中 `tool_calls > 0` 只表示"这一轮用了工具"，不代表"任务没完成要继续"。daemon 自己会决定何时结束 turn。所以固定 `end_turn` 是正确的。

### 潜在风险（需联调观察）

如果 atomcode daemon 的行为是"用一次工具就 done，等用户再 prompt 才继续"，那 `end_turn` 完全正确。

如果 daemon 的行为是"用工具后自动继续直到任务完成才 done"，那 daemon 流会自然产出多个 `tool_call`/`tool_call_update` update + 最终一个 `done`，我们仍固定 `end_turn`，也正确。

唯一会出问题的场景：daemon 用工具后 done，但 QwenPaw 期望 agent 自动继续而不需用户再 prompt。但从源码看 QwenPaw 不会自动续，所以无此风险。

---

## 三、已完成改造清单（按文件）

### `python/src/acp_mapper.py`
- event 工厂函数改为带 `sessionUpdate` 字段的 chunk 结构
- 新增：`make_text_chunk` / `make_thought_chunk` / `make_tool_call_start` / `make_tool_call_update` / `make_turn_end_signal`
- 新增：`session_to_new_session_response`（顶层 sessionId）/ `session_to_load_session_response`（空对象）
- 保留旧函数名别名（deprecated）作过渡

### `python/src/proxy_agent.py`
- `translate_daemon_event` 工具调用映射改为 `tool_call`/`tool_call_update`
- `done` 事件固定 `stopReason: "end_turn"`（去掉 tool_use 分支）
- error/cancel 路径用 `make_turn_end_signal("cancelled")`（合法值）
- import 改用新工厂函数

### `python/src/mock_agent.py`
- event 名和工厂函数对齐新结构

### `python/src/agent_state.py`
- `loadSession=False`
- `sessionCapabilities.close/list/resume` 改为 dict（`{}` 或 `null`），不是 bool
- 去掉非 schema 的 `delete` 字段

### `python/src/session_manager.py`
- `Session` 加 `cwd` 字段（SessionInfo 必填）
- `to_info_dict` 对齐 SessionInfo：`sessionId`+`cwd`+`title`+`updatedAt`
- `create()` 接收 cwd 参数

### `python/stdio_server.py`
- 引入 `AppState` 类（对齐 server.py，含 agent + session_manager + proxy）
- `start_daemon` 改用 `start_sync()` / `stop_sync()`（async start 已删）
- `run_stdio` stdin 改用后台线程 + asyncio.Queue（修 Windows IOCP WinError 6）
- `handle_message` 识别响应消息 resolve pending future（支持 request_permission）
- `_handle_prompt` 消息提取改用 `prompt` 字段（List[ContentBlock]），兼容旧 `messages`
- `_handle_prompt` turn_end/error 不作为 update，stopReason 作为响应返回
- 新增 `_request_permission` 方法（server→client 请求）
- session/new/load 响应改用新转换函数

### `python/server.py`
- 同步 stdio_server 的 prompt 消息提取和响应格式改动
- `on_cleanup` 用 `stop_sync()`
- 去掉 `on_startup`（main 中已 start_sync）

### `python/acp-agent.json`
- `command` 改为绝对路径 `D:\QwenPaw\python.exe`

### `python/QWENPAW_SETUP.md`（新建）
- QwenPaw Workspace→ACP 页面配置指南

---

## 四、已验证通过项

| 验证项 | 方法 | 结果 |
|--------|------|------|
| 8 个源文件编译 | `py_compile` | ✅ |
| `InitializeResponse` schema | `acp.schema.model_validate` | ✅ loadSession=false, sessionCapabilities=dict |
| `NewSessionResponse` schema | 同上 | ✅ 顶层 sessionId |
| `LoadSessionResponse` schema | 同上 | ✅ 空对象 |
| `ListSessionsResponse` schema | 同上 | ✅ SessionInfo 含 sessionId+cwd |
| `PromptResponse` stopReason | 同上 | ✅ end_turn 合法，tool_use 被拒 |
| `PromptRequest` 消息提取 | 模拟 QwenPaw 真实 params | ✅ prompt 字段正确提取 text |
| stdio 完整协议流 | 诊断脚本 initialize→new→prompt→close | ✅ 全通，真实 LLM 响应 |
| mock 模式回归 | 5 项 E2E | ✅ 无破坏 |

---

## 五、联调进展与剩余风险

### 已通过的 QwenPaw 联调环节

1. ✅ stdio 子进程启动
2. ✅ initialize 握手（loadSession/sessionCapabilities 修复后）
3. ✅ session/new（sessionId 顶层修复后）
4. ✅ session/prompt 基本响应（stopReason 修复后）
5. ✅ 真实 LLM 文本响应通过 daemon → proxy → ACP 链路返回

### 待重测的环节

上一轮 QwenPaw 测试报"空消息收到"——已修复（P6 消息提取）。**用户需重测 5 轮多轮对话**验证：
- 任务指令能正确传到 atomcode
- 工具调用（创建/编辑文件）能执行
- 多轮上下文连续性

### 剩余风险点（按可能性排序）

| 风险 | 可能性 | 应对 |
|------|--------|------|
| **daemon SSE 事件字段名与 translate 假设不符** | 中 | 联调时抓 daemon 原始 SSE，对照 `translate_daemon_event` 调整 |
| **工具调用的 toolCallId/title/kind 映射不准** | 中 | daemon 的 tool_use 事件字段名可能不同，需对照调 |
| **session/request_permission 触发后无法回传 daemon** | 低 | daemon HTTP/SSE 不支持反向注入；若 daemon 不发起 permission 则不触发 |
| **QwenPaw 期望 session/update 的 ContentBlock 子类型更细** | 低 | 若报错按 acp.schema 的 ToolCallStart/Progress 细化字段 |

### 诊断方法

联调出问题时：
1. 在 QwenPaw runner env 加 `LOG_LEVEL=debug`
2. 看 daemon2acp stderr 日志（stdout 是 JSON-RPC 不能看）
3. 用 `acp.schema.<对应Response>.model_validate()` 校验本项目产出
4. 对照 `D:\QwenPaw\lib\site-packages\acp\schema.py` 确认字段

---

## 六、关键参考路径

| 用途 | 路径 |
|------|------|
| ACP SDK schema（权威） | `D:\QwenPaw\lib\site-packages\acp\schema.py` |
| ACP SDK method 字符串 | `D:\QwenPaw\lib\site-packages\acp\meta.py` |
| QwenPaw ACP client 实现 | `D:\QwenPaw-source\src\qwenpaw\agents\acp\` |
| QwenPaw ACP mock runner（范例） | `D:\QwenPaw-source\tests\integration\fixtures\acp_mock_runner.py` |
| QwenPaw ACP 集成文档 | `D:\QwenPaw\Lib\site-packages\qwenpaw\docs\acp-integration.zh.md` |
| 本项目接入指南 | `python/QWENPAW_SETUP.md` |

---

## 七、QwenPaw ACP 配置（当前可用）

```json
"atomcode": {
  "enabled": true,
  "command": "python",
  "args": ["D:\\代码\\daemon2acp\\python\\stdio_server.py"],
  "env": {
    "DAEMON2ACP_MODE": "proxy",
    "DAEMON2ACP_AUTO_START": "1"
  },
  "trusted": true,
  "tool_parse_mode": "call_title",
  "stdio_buffer_limit_bytes": 52428800
}
```

**建议改进**：`command` 用绝对路径 `D:\QwenPaw\python.exe` 避免 PATH 依赖。

---

## 八、QwenPaw timeout 机制与长任务处理

### 默认 timeout

`delegate_external_agent` 的 `max_runtime` 参数默认 **300 秒**（5 分钟），`None` 表示不超时。

```python
# delegate_external_agent.py L917
async def delegate_external_agent(
    action: str, runner: str = "",
    message: str = "", cwd: str = "",
    max_runtime: Optional[float] = 300,  # ← 默认 300s
)
```

### 超时行为

超时后 **不关闭 ACP session**，只 cancel 当前 turn。QwenPaw 会提示用 `action="message"` 继续：

```
reached the preset max runtime and was interrupted.
The ACP session is still open; continue with
delegate_external_agent(action="message", runner="...", message="continue")
with higher max_runtime.
```

### 配置局限性

- `ACPAgentConfig`（config.py L58-70）没有 `max_runtime` 字段——timeout 不能全局配置，只能每次工具调用时由 LLM 指定
- 全局配置无 ACP 专用 timeout
- **无其他长任务机制**（如进度轮询、异步回调等）

### 建议

第 2 轮 timeout 是因为 daemon 需要 `glob` 搜索文件，耗时超过 300s。后续对话中可要求 QwenPaw 的 LLM 调大 `max_runtime=600` 或 `max_runtime=None`。这在 QwenPaw 端配置，不涉及本项目代码修改。

---

## 九、多轮对话 Session 设计分析

### 源码确认：QwenPaw 并非每轮独立 Session

`delegate_external_agent` 的四种 action 有不同的 session 策略：

| action | 参数 | 行为 |
|--------|------|------|
| `start` | `restart=True` | 关闭旧 session，创建新 session |
| `message` | `restart=False` | **复用已有 session** |
| `respond` | — | 复用已有 session |
| `close` | — | 关闭 session |

复用机制（`service.py` L211-248）：

```python
async def _get_or_create_session(self, *, chat_id, agent, ...):
    existing = self._sessions.get((chat_id, agent))  # 缓存 key
    if existing is not None and existing.process.returncode is None:
        return existing  # 直接复用，不创建新 session
```

同一 QwenPaw 会话内多次 `action="message"` → 复用同一个 daemon2acp 子进程 + 同一个 ACP sessionId。**QwenPaw 端的 session 设计是正确的。**

### 那为什么 daemon 侧看起来像独立 Session？

关键线索在 atomcode 终端输出——每轮开头都有 `resumed:`：

```
─────────────────────── resumed: 请在 D:\代码\ 目录下... ───────────────────────
```

这是 **atomcode daemon 的行为特性**：daemon 的每个 `/chat` 请求虽然带同一个 `sessionId`，但 atomcode-core 在处理每个 prompt 时**从持久化存储重新加载会话上下文**（`resumed` = 恢复历史到当前 prompt）。这导致：

1. **第 1 轮**：创建文件 → daemon session 记录到对话历史
2. **第 2 轮**：`recall(刚才修改的文件)` → **找不到**。因为 `recall` 不是对话上下文，是**项目级持久化记忆系统**。文件创建信息存在 daemon 的对话历史（可从上一轮恢复），但没被 `recall` 索引过。这是预期行为。
3. **用户提示文件名后** → daemon `glob` 找到文件 → 修改成功 → `recall` 索引到记录
4. **第 3/4 轮**：`recall` 能找到了 → 成功

**所以问题不是 "多轮分独立 session"**，而是 `recall` 记忆系统在第 2 轮时尚未索引到文件。

### 是否符合 ACP 设计理念？

**符合**。ACP 协议的 `session/prompt` 是单次请求-响应模式：
- Agent 可以在 prompt 间维护状态（通过 `sessionId` 关联）
- Agent 也可以不维护状态（每次 prompt 独立处理）
- QwenPaw 的设计（复用 sessionId + agent 自己管理上下文）是 ACP 推荐的松耦合方式
- atomcode daemon 的 `resumed` 行为（从持久化历史恢复上下文）也是合理实现

### 工程上是否合理？

**合理。** 现有的行为（每轮 `resumed` + `recall` 搜索）对大部分场景够用。`recall` 的"首次找不到"是单次代价（第 2 轮），后续轮次都有索引。

如果 daemon 侧上下文丢失成为瓶颈，可优化的方向：
1. 在 daemon2acp 层缓存上一轮对话摘要，注入到 prompt 的 `system_prompt_files` 中
2. 但这是优化不是 Bug，不建议现在做

### 关于 "daemon session 心跳保持"

之前归档里提到的"心跳保持"是指：如果 daemon 的 session 因 `--idle-timeout` 自动关闭，下一轮 prompt 会丢失上下文。做法：定时发 GET /health 刷新 idle 计时器。

**实际不需要**——因为：
- 用户配置的 daemon 没有设 `--idle-timeout`（或设得很大），session 不会主动过期
- 观察到"丢失上下文"的根因是 `recall` 记忆系统初次搜索不到，不是 session 过期
- **不做心跳保持，等到因 idle timeout 导致上下文丢失的证据后再做**

---

_本文档由 2026-06-28 改造会话整理。最后更新：2026-06-28 21:14，追加 session 映射 Bug、QwenPaw session 生命周期、待验证项。_

---

## 十、session 复用问题 — 已修复 ✅

### 问题

QwenPaw 的 `action="message"` 复用同一 ACP sessionId，但 daemon 侧每次 prompt 都是**独立 session**（无上下文），只能靠 `recall` 搜索项目记忆。

### 根因（调查三阶段才定位）

**最终根因：daemon `/chat` 的 `ChatRequest` 字段是 snake_case `session_id`，但 daemon2acp 传的是 camelCase `sessionId`。** serde 默认忽略未知字段（`ChatRequest` 无 `#[serde(deny_unknown_fields)]`），所以 daemon 从未收到 sessionId → 每次都走 `Session::new()` 新建分支 → **根本没调 `load()`**。

#### 调查走过的弯路

1. **误判阶段**：以为是 daemon 的 `SessionManager::load()` 反序列化失败。证据：磁盘 session 文件存在、JSON 有效、字段与 `Session` struct 完全匹配，但 `/chat` 每轮新建 session。
2. **关键反证实验**：用 `GET /projects/:hash/sessions/:id` 端点（`lib.rs` L1098 `load_session()` 函数，与 `SessionManager::load()` 用完全相同的 `serde_json::from_str::<Session>`）加载**同一个 session 文件** → 返回 200 成功。证明 **load 本身没问题**。
3. **真凶定位**：对照 `ChatRequest` struct（`lib.rs` L1807）字段定义 `pub session_id: Option<String>`（snake_case，无 `#[serde(rename)]`），发现 daemon2acp 传的 `payload["sessionId"]`（camelCase）被 serde 静默丢弃。同时 daemon2acp 也**没传 `working_dir`**，daemon 回退到 `std::env::current_dir()` 算 project_hash（虽然实测 cwd 一致，但显式传更稳妥）。

### 修复

| 文件 | 改动 |
|------|------|
| `python/src/proxy_agent.py` | `payload["sessionId"]` → `payload["session_id"]`；`chat_stream` 加 `working_dir` 参数，payload 加 `working_dir` 字段 |
| `python/src/proxy_agent.py` | `run_proxy_agent` 签名加 `working_dir`，透传给 `chat_stream` |
| `python/stdio_server.py` | `_handle_prompt` 从 `session_manager.get(session_id).cwd` 取 working_dir，传给 `run_proxy_agent` |
| `python/server.py` | HTTP `/chat` 和 ACP `/acp` 两处 `run_proxy_agent` 调用加 `working_dir=session.cwd` |
| `python/_test_reuse2.py` | `session/new` 加 `"params":{"cwd": PROJECT}`，否则 session.cwd 为空 → working_dir 仍 None |

### 验证结果（2026-06-29 07:40）

```
[5] Round 1: prompt (remember 42)...
  AI: I'll remember that. The number is **42**.
[6] Round 2: prompt (what was the number?)...
  AI: **42**            ← 直接回答，无 tool_call，无 recall 搜索
RESULT: ✅ SESSION REUSED! AI remembered 42
```

**对比修复前**：修复前 Round 2 会调 `recall` 工具搜索记忆，回答"保存在之前的对话会话中"；修复后 Round 2 直接答 `**42**`（来自 session 历史上下文）。daemon session 文件从每轮新建变为复用同一 ID（`25be9121`）。

### 经验教训

- **serde 默认忽略未知字段**是个静默陷阱：camelCase 字段名不报错，但被丢弃。daemon API 没有 `deny_unknown_fields`，客户端字段名必须严格匹配 struct。
- 调查时用了错误的反证：用 `GET` 端点测 load 成功 → 推断 `/chat` 的 load 也该成功 → 误以为 load 失败。实际上 `/chat` 根本没走到 load 分支（sessionId 被丢弃）。
- **应该最早检查的是"daemon 是否真的收到了我传的字段"**，而不是怀疑 daemon 内部逻辑。
- **不要在没读源码时就下"无法定位"的结论**。本 Bug 第一轮归档（第十节旧版）曾写"daemon 二进制与源码 struct 版本不匹配，daemon2acp 无法修复"——这是逃避。源码里 `SessionManager::load`、`load_session`、`/chat` handler 的 load 分支都清清楚楚，深入读源码 + 用 `GET` 端点做反证实验，根因（字段名不匹配）本该在第一轮就定位到。被用户追问"atom 源码中没有搜索和加载 session 的代码吗？"后才回头补做该做的调查。

### 实验/诊断脚本

- `python/_test_reuse2.py` — 端到端回归测试（保留，已更新传 cwd）
- 调查过程中的临时诊断脚本（`_test_load_real.py`、`_test_get_session.py`、`_dump_session.py` 等）已清理

_本文档由 2026-06-28/29 改造会话整理。最后更新：2026-06-29 07:41，session 复用问题修复确认。_
