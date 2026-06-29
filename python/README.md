# daemon2acp

将 AtomCode Daemon 的 HTTP/SSE API 映射到 **ACP (Agent Client Protocol) v1** 协议，让任何支持 ACP 的编辑器/IDE 都能驱动 AtomCode。

## 它做了什么

```
┌──────────────┐     ACP (JSON-RPC)     ┌──────────────┐     HTTP/SSE      ┌──────────────────┐
│  ACP 客户端   │ ───────────────────▶ │  daemon2acp  │ ──────────────▶ │ atomcode-daemon  │
│ (Zed/VSCode/) │ ◀─────────────────── │  (本项目)     │ ◀────────────── │ (AtomCode 后端)   │
└──────────────┘     事件格式转换       └──────────────┘     现有 API       └──────────────────┘
```

daemon2acp 是一个协议翻译层：把 ACP 客户端的标准请求翻译成 AtomCode Daemon 已有的 HTTP 调用，并把返回的 SSE 事件流转换为 ACP 的 `session/update` 通知格式。

**当前状态**：mock 模式和 proxy 模式均已通过端到端验证，proxy 模式已与 AtomCode Daemon v4.25.6 联调成功，能执行真实的 AI 对话、工具调用和多轮上下文连续对话。已作为外部 ACP runner 接入 QwenPaw 验证可用。

## 快速启动

### 安装依赖

```bash
pip install aiohttp pydantic
```

> 需要 Python 3.10+

### 启动服务

**HTTP 模式** — 提供 REST + ACP JSON-RPC 端点：

```bash
# 模拟模式（默认，无需安装 AtomCode，用于开发测试）
python server.py

# 转发模式 — 自动拉起 atomcode daemon 子进程
DAEMON2ACP_MODE=proxy python server.py

# 转发模式 — 连接已运行的 atomcode daemon
DAEMON2ACP_MODE=proxy DAEMON2ACP_AUTO_START=0 DAEMON2ACP_DAEMON_URL=http://127.0.0.1:13456 python server.py
```

> Windows 用户用 `set VAR=value` 设置环境变量，或直接在命令前加 `set VAR=value && `

**stdio 模式** — 供 ACP 客户端（Zed、VS Code 等）直接启动：

```bash
# 模拟模式
python stdio_server.py

# 转发模式
DAEMON2ACP_MODE=proxy python stdio_server.py
```

## 运行模式

| 模式 | 说明 | 依赖 | 状态 |
|------|------|------|------|
| **mock** | 模拟 AI 响应，用于开发测试 | 无 | ✅ 已验证 |
| **proxy** | 转发到 atomcode-daemon，可自动拉起子进程 | 需要安装 AtomCode | ✅ 已验证（与 daemon v4.25.6 联调通过） |
| **direct** | 进程内调用 atomcode-core | PyO3 binding | ⏳ 规划中 |

通过环境变量 `DAEMON2ACP_MODE` 切换，默认 `mock`。

## 在 ACP 客户端中使用

ACP 客户端（Zed、VS Code、JetBrains、QwenPaw 等）通过 **stdio** 启动 Agent 子进程。你只需配置启动命令。

### QwenPaw（已验证）

QwenPaw 通过内置工具 `delegate_external_agent` 调用外部 ACP runner。本项目就是把 AtomCode daemon 包装成 ACP server，被 QwenPaw 当作 runner 调用。完整接入步骤见 [QWENPAW_SETUP.md](QWENPAW_SETUP.md)，核心配置：

| 字段 | 值 |
|------|-----|
| `command` | `D:\QwenPaw\python.exe` |
| `args` | `D:\代码\daemon2acp\python\stdio_server.py`（每行一个参数） |
| `env.DAEMON2ACP_MODE` | `proxy` |
| `env.DAEMON2ACP_AUTO_START` | `1` |

### Zed

编辑 `settings.json`：

```json
{
  "agent": {
    "profiles": {
      "atomcode": {
        "name": "AtomCode",
        "command": "python",
        "args": ["/path/to/daemon2acp/python/stdio_server.py"],
        "env": {
          "DAEMON2ACP_MODE": "proxy",
          "DAEMON2ACP_AUTO_START": "1"
        }
      }
    }
  }
}
```

### VS Code（Cline / Roo Code 等 ACP 扩展）

在扩展设置中配置 Agent 启动命令：

```json
{
  "cline.acpAgentCommand": "python /path/to/daemon2acp/python/stdio_server.py",
  "cline.acpAgentEnv": {
    "DAEMON2ACP_MODE": "proxy"
  }
}
```

### JetBrains（Junie 等）

在 Agent 设置中填入：

- **Command**: `python /path/to/daemon2acp/python/stdio_server.py`
- **Transport**: stdio
- **Environment**: `DAEMON2ACP_MODE=proxy`

### 通用配置

任何 ACP 客户端只需两项：

| 配置项 | 值 |
|--------|-----|
| 启动命令 | `python /path/to/daemon2acp/python/stdio_server.py` |
| 传输协议 | `stdio`（JSON-RPC over stdin/stdout，每行一条消息） |

stderr 输出日志，stdout 专用于 JSON-RPC 通信，互不干扰。

> 完整接入指南见 [ACP_CLIENT_GUIDE.md](ACP_CLIENT_GUIDE.md)

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DAEMON2ACP_MODE` | `mock` | 运行模式：`mock` / `proxy` |
| `DAEMON2ACP_AUTO_START` | `1` | proxy 模式下是否自动拉起 atomcode daemon 子进程 |
| `DAEMON2ACP_DAEMON_URL` | `http://127.0.0.1:13457` | 已有 daemon 的地址（不自动拉起时使用） |
| `DAEMON2ACP_DAEMON_HOST` | `127.0.0.1` | 自动拉起的 daemon 监听地址（注意：atomcode daemon 不支持 `--host`，始终绑定 127.0.0.1） |
| `DAEMON2ACP_DAEMON_PORT` | `13457` | 自动拉起的 daemon 监听端口 |
| `ATOMCODE_BIN` | 自动查找 | atomcode 可执行文件路径（找不到时手动指定） |
| `HOST` | `127.0.0.1` | HTTP 服务监听地址（仅 `server.py`） |
| `PORT` | `13456` | HTTP 服务监听端口（仅 `server.py`） |
| `LOG_LEVEL` | `INFO` | 日志级别：`DEBUG` / `INFO` / `WARNING` / `ERROR` |

## 自动拉起 AtomCode Daemon

proxy 模式下，daemon2acp 会自动拉起 `atomcode daemon` 子进程，无需手动启动：

```
python server.py (DAEMON2ACP_MODE=proxy)
    │
    ├─ 查找 atomcode 可执行文件
    │   PATH → 常见安装路径 → $ATOMCODE_BIN
    │
    ├─ 启动子进程: atomcode daemon --port 13457
    │   （注意：atomcode daemon 不支持 --host，始终绑定 127.0.0.1）
    │
    ├─ 轮询 /health 等待就绪（最多 15 秒）
    │
    └─ 就绪后开始转发请求
         Client → daemon2acp:13456 → atomcode-daemon:13457 → LLM
```

关闭 daemon2acp 时自动优雅停止子进程（`POST /shutdown` → `SIGTERM` → `SIGKILL`）。

## HTTP 端点

`server.py` 提供以下端点：

| 端点 | 方法 | 说明 | ACP 映射 |
|------|------|------|----------|
| `/health` | GET | 健康检查（proxy 模式含 daemon 连通性） | — |
| `/chat` | POST | 发送消息，SSE 流式返回 | `session/prompt` + `session/update` |
| `/sessions` | GET | 列出所有会话 | `session/list` |
| `/sessions/{id}` | GET | 获取单个会话 | `session/load` |
| `/sessions/{id}` | DELETE | 删除会话 | `session/delete` |
| `/sessions/{id}/cancel` | POST | 取消正在执行的 prompt | `session/cancel` |
| `/acp` | POST | ACP JSON-RPC 2.0 通用入口 | 直通 |

### 请求示例

```bash
# 健康检查
curl http://127.0.0.1:13456/health

# ACP 握手
curl -X POST http://127.0.0.1:13456/acp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'

# 创建会话
curl -X POST http://127.0.0.1:13456/acp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"session/new"}'

# 发送消息（SSE 流式返回）
curl -N -X POST http://127.0.0.1:13456/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"hello"}'
```

## 事件转换

proxy 模式下，atomcode-daemon 的 SSE 事件自动转换为 ACP 格式。

### atomcode-daemon 的实际 SSE 格式

daemon 的 `/chat` 端点返回的 SSE 流格式（每行一条 `data:`，无 `event:` 行）：

```
: ping
data: {"type":"text","content":"Hello"}
data: {"type":"tokens","prompt":10,"completion":20,"total":30}
data: {"type":"done","tokens":30,"tool_calls":0,"session_id":"..."}
: bye
```

### 事件映射表

proxy 模式下，daemon 的 SSE 事件通过 `acp_mapper.translate_daemon_event` 转换为 ACP `session/update` chunk 类型：

| daemon `type` | daemon 字段 | ACP `sessionUpdate` | 说明 |
|---------------|------------|---------------------|------|
| `text` | `content` | `agent_message_chunk` | AI 文本增量 |
| `reasoning` | `content` | `agent_thought_chunk` | 推理增量 |
| `tool_start` | `id` / `name` / `arguments` | `tool_call` | 工具调用开始 |
| `tool_call_update` | `id` / `arguments` | `tool_call_update` | 工具参数增量 |
| `tool_output` | `chunk` | `tool_call_update`（status=`running`） | 工具实时输出 |
| `tool_end` | `id` / `output` | `tool_call_update`（status=`completed`） | 工具结果 |
| `tokens` | `prompt` / `completion` / `total` | （内部记录，不上报） | token 使用统计 |
| `done` | `tokens` / `tool_calls` / `session_id` | `turn_end` 信号 | 轮结束；捕获 `session_id` 建立映射 |
| `error` | `message` | `error` | 错误 |

> **关于 `stopReason`**：ACP schema 的 `Literal` 不包含 `tool_use`，且 QwenPaw 自身实现也如此，因此本项目 `stopReason` 始终为 `end_turn`（即使本轮有工具调用）。工具调用通过 `tool_call` / `tool_call_update` chunk 上报，不靠 `stopReason` 表达。
>
> **关于 session 复用**：daemon `/chat` 的 `ChatRequest` 字段是 snake_case `session_id` / `working_dir`，daemon2acp 透传 session 的 `cwd` 作为 `working_dir`，并从 `done` 事件捕获 daemon 真实 session_id 建立映射，确保多轮对话复用同一 daemon session（详见 PROGRESS.md 第十节）。

## 项目结构

```
python/
├── pyproject.toml               # 依赖声明
├── server.py                    # HTTP 服务入口（aiohttp）
├── stdio_server.py              # stdio 传输层入口（供 ACP 客户端启动）
├── acp-agent.json               # ACP Agent 声明文件
├── src/
│   ├── agent_state.py           # Agent 状态与 ACP 能力声明
│   ├── session_manager.py       # 会话管理器（内存态，Session 含 cwd 字段）
│   ├── acp_mapper.py            # daemon SSE → ACP session/update chunk 转换
│   ├── mock_agent.py            # 模拟 AI 处理（mock 模式）
│   ├── proxy_agent.py           # 转发代理（proxy 模式，含 ACP↔daemon session 映射）
│   └── daemon_launcher.py       # atomcode daemon 子进程管理（启动/停止/健康检查）
├── tests/
│   ├── test_endpoints.py        # 端点单元测试（6 个，mock 模式，全部通过）
│   ├── e2e/                     # proxy 模式端到端联调脚本（需 AtomCode）
│   │   ├── auto_start_test.py   # 自动拉起 daemon
│   │   ├── launch_test.py       # launcher 启动/停止
│   │   ├── multi_turn_test.py   # 多轮对话上下文
│   │   ├── proxy_test.py        # proxy 转发
│   │   └── quick_test.py        # 快速联调
│   └── debug/                   # 调试脚本（SSE 解析、daemon 连通性等）
├── ACP_CLIENT_GUIDE.md          # ACP 客户端接入详细指南
├── QWENPAW_SETUP.md             # QwenPaw 接入指南（已验证的主用例）
└── DIRECT_CALL_PLAN.md          # 直接调用模式规划（未来）
```

> 项目根还有 `PROGRESS.md`（完整改造进度与已解决问题归档，含 session 复用根因分析）和 `PLAN.md`。`crates/` 下的 Rust 代码为早期参考实现，当前以 `python/` 为准。

## 测试

```bash
# 端点单元测试（mock 模式，无需 AtomCode）
D:\QwenPaw\python.exe tests/test_endpoints.py

# proxy 模式联调（需 AtomCode 已安装，手动启动 daemon 后运行 tests/e2e/ 下的脚本）
# 详见 tests/e2e/README.md
```

### 已验证的测试场景

| 场景 | 模式 | 结果 |
|------|------|------|
| 6 个端点单元测试 | mock | ✅ 全部通过 |
| 健康检查 + ACP 握手 | proxy | ✅ 通过 |
| 单轮对话（"你好"） | proxy | ✅ 返回真实 AI 响应 |
| 多轮对话（创建文件 → 读回验证） | proxy | ✅ 上下文连续，AI 记住前轮操作 |
| session 复用（记住 42 → 询问） | proxy | ✅ 同一 daemon session，AI 直接答 **42**，无 recall 搜索 |
| QwenPaw `delegate_external_agent` 调用 | proxy | ✅ 多轮协作正常 |

## 故障排查

| 问题 | 原因 | 解决 |
|------|------|------|
| 客户端报 "Agent exited" | Python 路径不对 | 使用绝对路径，如 `python3 /full/path/stdio_server.py` |
| Agent 无响应 | 依赖未安装 | `pip install aiohttp pydantic` |
| proxy 模式报 "atomcode not found" | AtomCode 未安装或不在 PATH | 设置 `ATOMCODE_BIN` 指向 atomcode 可执行文件 |
| daemon 启动后秒退 | 子进程 stdout/stderr 管道缓冲区满 | 已修复：stdout/stderr 设为 DEVNULL |
| daemon 启动超时 | 端口被占用 | 改 `DAEMON2ACP_DAEMON_PORT` 为其他端口 |
| daemon 报 "unexpected argument --host" | atomcode daemon 不支持 `--host` | 已修复：启动命令不再传 `--host` |
| SSE 解析无输出 | daemon 用逐行 `data:` 格式，无 `event:` 行 | 已修复：使用 `readline()` 逐行解析 |
| 看不到日志 | stdout 被 ACP 协议占用 | 日志在 stderr，设置 `LOG_LEVEL=DEBUG` 查看 |

## 后续规划

- [x] 接入真实 AtomCode Daemon 测试转发模式
- [x] 多轮对话上下文连续性验证
- [x] session 复用修复（daemon `/chat` 字段名 snake_case 对齐 + `working_dir` 透传，详见 PROGRESS.md 第十节）
- [x] QwenPaw 作为外部 ACP runner 接入验证
- [ ] 实现直接调用模式（进程内调用 atomcode-core，见 [DIRECT_CALL_PLAN.md](DIRECT_CALL_PLAN.md)）
- [ ] WebSocket / Streamable HTTP transport 支持
- [ ] ACP v2 协议适配
