# 🚀 daemon2acp — AtomCode Daemon × ACP 协议迁移规划

> 将 atomcode-daemon 从专有 HTTP/SSE API 转换为符合 **Agent Client Protocol (ACP)** 标准的 Agent，使任何支持 ACP 的编辑器/IDE 都能驱动 AtomCode 的 AI 能力。

## 实现进展

| 阶段 | 内容 | 状态 |
|------|------|------|
| Python 实现（mock 模式） | HTTP + stdio 传输、6 个端点、ACP JSON-RPC | ✅ 已完成 |
| Python 实现（proxy 模式） | 转发到 atomcode-daemon、SSE 事件转换、自动拉起子进程 | ✅ 已完成（与 daemon v4.25.6 联调通过） |
| 多轮对话验证 | 上下文连续性、工具调用（文件创建/读取） | ✅ 已验证 |
| Rust 规划样板 | workspace 骨架、core/http crate | 📁 保留作为参考 |
| 直接调用模式 | 进程内调用 atomcode-core（PyO3 binding） | ⏳ 规划中（见 python/DIRECT_CALL_PLAN.md） |

> **当前推荐使用方式**：Python proxy 模式，一行命令启动，自动拉起 atomcode daemon。详见 [python/README.md](python/README.md)

---

## 目录

1. [背景](#1-背景)
2. [核心架构](#2-核心架构)
3. [ACP 方法映射](#3-acp-方法映射)
4. [项目结构](#4-项目结构)
5. [实现阶段](#5-实现阶段)
6. [与现有 daemon 的兼容策略](#6-与现有-daemon-的兼容策略)
7. [关键依赖](#7-关键依赖)
8. [风险与应对](#8-风险与应对)
9. [交付物清单](#9-交付物清单)
10. [快速启动](#10-快速启动)

---

## 1 背景

| 项目 | 说明 |
|------|------|
| **AtomCode Daemon** | 现有 AtomCode 架构中的 HTTP + SSE API 服务，位于 `crates/atomcode-daemon/`，复用 `atomcode-core`（AgentLoop、工具、Provider、会话系统），供 VS Code 插件、AtomCode Air 等前端调用 |
| **ACP v1** | Agent Client Protocol — 编辑器与 AI 编码代理之间的标准化 JSON-RPC 2.0 协议。官方 Rust SDK：`agent-client-protocol` v1.0.0 + `agent-client-protocol-schema` v1.1.0 |
| **目标** | 将 atomcode-daemon 从专有的 HTTP API 转换为符合 ACP 标准的 Agent，使其能够被任何支持 ACP 的编辑器/客户端（Zed、VS Code 等）驱动 |

### 1.1 什么是 ACP？

ACP (Agent Client Protocol) 是 AI 编码代理与代码编辑器之间的标准化通信协议，遵循 JSON-RPC 2.0 规范：

- **Agent**（本程序）— 使用生成式 AI 自主修改代码的程序，处理来自 Client 的请求
- **Client**（编辑器/IDE）— 提供用户界面，管理环境，控制资源访问
- 主要传输方式：**stdio**（Client 将 Agent 作为子进程启动）、Streamable HTTP（草案中）
- 消息类型：请求-响应对（methods）、单向通知（notifications）

### 1.2 什么是 AtomCode Daemon？

现有 `atomcode-daemon` 是一个 HTTP + SSE API 服务：
- 复用 `atomcode-core` 的所有核心能力（AgentLoop、tool 系统、provider、session 管理）
- 默认绑定 `127.0.0.1:13456`
- 提供 `POST /chat`（SSE 流式对话）、`GET /history`（会话列表）等端点
- 被 VS Code 扩展和 AtomCode Air 桌面端使用

---

## 2 核心架构

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         ACP Agent（daemon2acp）                          │
│                                                                         │
│  ┌─────────────────────────────────────────────────────────────────────┐ │
│  │                    ACP 传输层                                        │ │
│  │  ┌──────────────┐  ┌──────────────────┐  ┌────────────────────────┐│ │
│  │  │ JSON-RPC     │  │ Builder          │  │ Stdio / HTTP transport││ │
│  │  │ 编码/解码      │  │ (role.acp.Agent) │  │                        ││ │
│  │  └──────────────┘  └──────────────────┘  └────────────────────────┘│ │
│  └─────────────────────────────────────────────────────────────────────┘ │
│                                   │                                      │
│  ┌─────────────────────────────────────────────────────────────────────┐ │
│  │                    ACP 方法路由层                                     │ │
│  │  ┌────────────┐ ┌───────────┐ ┌────────────┐ ┌──────────────────┐  │ │
│  │  │ initialize │ │ auth/     │ │ session/*  │ │ client → tools   │  │ │
│  │  │            │ │ logout    │ │ 路由       │ │ 委托（可选）       │  │ │
│  │  └────────────┘ └───────────┘ └────────────┘ └──────────────────┘  │ │
│  └─────────────────────────────────────────────────────────────────────┘ │
│                                   │                                      │
│  ┌─────────────────────────────────────────────────────────────────────┐ │
│  │                    atomcode-core（现有核心复用）                        │ │
│  │  ┌────────────┐ ┌────────────┐ ┌──────────┐ ┌────────────────────┐ │ │
│  │  │ AgentLoop  │ │ 会话系统    │ │ Provider │ │ Tool 系统          │ │ │
│  │  │ (agent/)   │ │(session/)  │ │(provider/│ │(tool/ + MCP)      │ │ │
│  │  └────────────┘ └────────────┘ └──────────┘ └────────────────────┘ │ │
│  └─────────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                       ACP Client（编辑器/IDE）                           │
│  Zed / VS Code / JetBrains / 自定义客户端                               │
└─────────────────────────────────────────────────────────────────────────┘
```

### 2.1 设计原则

1. **最大复用** — 不重写 atomcode-core，只添加 ACP 协议层适配
2. **渐进迁移** — V1 保留 Agent 侧工具执行，逐步过渡到 Client 侧委托
3. **标准兼容** — 严格遵循 ACP v1 规范，通过 JSON Schema 验证
4. **多传输支持** — stdio（主力） + HTTP（向后兼容）

---

## 3 ACP 方法映射

### 3.1 Agent 侧（本程序实现）

| ACP 方法 | 现有 daemon 等价点 | 实现说明 |
|----------|-------------------|----------|
| `initialize` | —（新增） | 协商协议版本、声明 `AgentCapabilities`、交换 `Implementation` 信息 |
| `authenticate` | —（新增） | 可选：基于 Token 的认证 |
| `logout` | —（新增） | 清除认证状态 |
| `session/new` | `POST /chat` + session 创建 | 通过 `atomcode_core::session::Manager::create()` 创建新会话，返回 ACP `SessionInfo` |
| `session/load` | —（新增） | 加载已有持久化会话，恢复上下文 |
| `session/list` | `GET /history` | 遍历 `Manager::list()`，映射为 `SessionInfo[]` |
| `session/delete` | —（新增） | 通过 `Manager::delete()` 删除指定会话 |
| `session/close` | —（新增） | 取消进行中的任务，释放会话资源 |
| `session/prompt` | `POST /chat`（核心） | 接收用户消息 → 启动 AgentLoop → 通过 `session/update` 通知流式返回结果 |
| `session/cancel` | —（新增） | 取消正在执行的 prompt turn |
| `session/set_mode` | —（新增） | 切换 Agent 工作模式（normal / plan / architect） |
| `session/set_config_option` | —（新增） | 设置会话级配置（模型选择、温度等） |

### 3.2 Client 侧（由编辑器/IDE 实现）

ACP 标准中，以下是 Client 提供的功能，Agent 可发起请求调用：

| ACP 方法 | AtomCode 现有等价功能 | 说明 |
|----------|----------------------|------|
| `fs/read_text_file` | `tool::Read` | 委托编辑器读取文件 |
| `fs/write_text_file` | `tool::Write` / `tool::Edit` | 委托编辑器写入/编辑文件 |
| `terminal/create` | `tool::Bash` | 委托编辑器创建终端执行命令 |
| `terminal/output` | `tool::Bash` 输出流 | 终端输出通知 |
| `session/request_permission` | `permission_decider` | 请求用户授权（如危险命令） |
| `session/update` | SSE `TurnEvent` 流 | 流式更新通知（文本增量、工具调用、错误等） |

### 3.3 工具执行策略

| 策略 | 优点 | 缺点 | 适用阶段 |
|------|------|------|----------|
| **Agent 侧执行**（默认） | 改动小，兼容现有 `tool::Bash`/`tool::Read`/`tool::Write`/`tool::Edit`/`tool::Grep` | 编辑器看不到工具调用细节 | V1 |
| **Client 侧委托**（ACP 标准） | 编辑器完全掌控文件系统，更安全 | 需要 Client 支持，改动量大 | V2 |

**V1 策略**：Agent 侧执行 + 通过 `session/update` 通知将工具调用信息同步给 Client。

---

## 4 项目结构

```
daemon2acp/                          # 项目根
│
├── Cargo.toml                       # workspace 定义
├── README.md                        # 项目说明
├── PLAN.md                          # 本规划文件
│
├── crates/
│   ├── daemon2acp-core/             # ACP 核心适配层
│   │   ├── Cargo.toml               # 依赖: agent-client-protocol, atomcode-core
│   │   └── src/
│   │       ├── lib.rs               # 库入口
│   │       ├── agent.rs             # ACP Agent 主入口（Builder 配置、连接生命周期）
│   │       ├── capabilities.rs      # AgentCapabilities 构建
│   │       ├── handlers/            # ACP 方法处理器
│   │       │   ├── mod.rs
│   │       │   ├── initialize.rs    # initialize 请求处理器
│   │       │   ├── authenticate.rs  # authenticate / logout 处理器
│   │       │   ├── session_new.rs   # session/new 处理器
│   │       │   ├── session_load.rs  # session/load 处理器
│   │       │   ├── session_list.rs  # session/list 处理器
│   │       │   ├── session_delete.rs# session/delete 处理器
│   │       │   ├── session_close.rs # session/close 处理器
│   │       │   ├── session_prompt.rs# session/prompt 处理器（最核心）
│   │       │   ├── session_cancel.rs# session/cancel 通知处理器
│   │       │   ├── session_mode.rs  # session/set_mode 处理器
│   │       │   └── session_config.rs# session/set_config_option 处理器
│   │       ├── bridge/              # atomcode-core ↔ ACP 桥接层
│   │       │   ├── mod.rs
│   │       │   ├── agent_loop.rs    # AgentLoop 包装为 ACP 兼容接口
│   │       │   ├── session_map.rs   # ACP SessionId ↔ atomcode session 映射
│   │       │   ├── event_adapter.rs # AgentEvent → session/update notification 转换
│   │       │   └── tool_adapter.rs  # Tool 执行适配（Agent 侧 / Client 侧）
│   │       └── state.rs             # 全局状态管理（活跃会话、认证状态等）
│   │
│   ├── daemon2acp-stdio/            # ACP stdio 传输二进制
│   │   ├── Cargo.toml
│   │   └── src/
│   │       └── main.rs              # 入口：Stdio 传输，Builder 模式启动 Agent
│   │
│   └── daemon2acp-http/             # ACP Streamable HTTP 传输二进制（可选）
│       ├── Cargo.toml
│       └── src/
│           └── main.rs              # HTTP 传输层入口
│
└── tests/
    ├── integration_test.rs          # 与 ACP 客户端联调测试
    └── protocol_compliance_test.rs  # ACP 协议合规性 JSON Schema 验证
```

### 4.1 核心类型设计

```rust
/// 全局 Agent 状态
pub struct AgentState {
    /// session 管理器（复用 atomcode-core）
    pub session_manager: Arc<Mutex<SessionManager>>,
    /// ACP session ID ↔ atomcode session ID 映射
    pub session_map: Arc<Mutex<HashMap<SessionId, String>>>,
    /// 活跃 prompt 任务（用于取消）
    pub active_tasks: Arc<Mutex<HashMap<SessionId, CancellationToken>>>,
    /// 认证状态
    pub auth_state: Arc<Mutex<AuthState>>,
    /// 配置
    pub config: Arc<Config>,
    /// llm provider 注册表
    pub providers: Arc<ProviderRegistry>,
}

/// 事件适配器：将 AgentEvent 转换为 session/update notification
pub enum AgentEvent {
    TextDelta { content: String },
    ReasoningDelta { content: String },
    ToolCallStart { tool_name: String, args: Value },
    ToolCallEnd { tool_name: String, result: Result<Value, String> },
    PermissionRequest { /* ... */ },
    Error { message: String },
    Stats { /* token usage */ },
}
```

---

## 5 实现阶段

### Phase 1：项目骨架与依赖注入（Week 1）

**目标**：建立 workspace 骨架，配置依赖，实现最小 ACP Agent 握手。

```
daemon2acp-stdio (stdio)     daemon2acp-http (HTTP，可选)
        │                            │
        └──────────┬─────────────────┘
                   │
        daemon2acp-core (lib)
              │
              ▼
   agent-client-protocol  (crates.io)
   agent-client-protocol-schema (crates.io)
   atomcode-core            (git dependency)
```

**任务清单**：

1. **创建 workspace** — `Cargo.toml`、`crates/` 目录结构
2. **添加依赖** — 配置 `agent-client-protocol` v1.0、`agent-client-protocol-schema` v1.1、`atomcode-core`（git）、tokio、serde、tracing 等
3. **实现 `capabilities.rs`** — 构建 `AgentCapabilities`：
   - `sessionCapabilities`：list、delete、close
   - `promptCapabilities`：text（必需）、image、audio、embeddedContext
   - `fs`：readTextFile、writeTextFile
   - `auth`：可选
   - `protocolVersion`：V1
4. **实现最小 `initialize` 处理器** — 接受 `InitializeRequest`，返回 `InitializeResponse`
5. **发布 `daemon2acp-stdio`** — 使用 `Stdio::new()` + `UntypedRole::builder()`，验证握手成功

**验证标准**：
```bash
# 启动 Agent
cargo run -p daemon2acp-stdio

# 用示例 Client 连接（来自 agent-client-protocol 仓库）
cargo run --example client -- target/debug/daemon2acp-stdio
# 输出：Initialize succeeded
```

---

### Phase 2：会话管理方法实现（Week 2）

**目标**：实现 ACP 会话的 CRUD 方法，与 atomcode-core 的 `session::Manager` 对接。

| 方法 | 输入 | 输出 | 实现关键 |
|------|------|------|----------|
| `session/new` | `NewSessionRequest` | `SessionInfo` | `Manager::create()`，返回 `sessionId`、`title`、`createdAt` |
| `session/list` | `ListSessionsRequest` | `SessionInfo[]` | 支持 `cursor` 分页 |
| `session/load` | `LoadSessionRequest` | `SessionInfo` | `Manager::load()`，恢复消息上下文 |
| `session/close` | `CloseSessionRequest` | — | 取消活跃任务，释放内存 |
| `session/delete` | `DeleteSessionRequest` | — | `Manager::delete()`，持久化删除 |
| `session/set_config_option` | `SetConfigOptionRequest` | — | 设置会话配置选项 |

**状态管理**：

```rust
struct SessionManagerState {
    /// ACP session_id → SessionHandle
    active_sessions: HashMap<SessionId, SessionHandle>,
    /// ACP session_id → CancellationToken
    active_tasks: HashMap<SessionId, CancellationToken>,
}

struct SessionHandle {
    atomcode_session_id: String,
    session: Session,            // atomcode_core::session::Session
    created_at: DateTime<Utc>,
    last_active: DateTime<Utc>,
    mode: SessionMode,
}
```

**验证标准**：
```bash
# 创建会话 → 列表会话 → 加载会话 → 关闭会话 → 删除会话
# 通过示例 Client 或手写测试脚本验证每个方法的响应
```

---

### Phase 3：核心 Prompt 流程实现（Week 3）

**目标**：实现 `session/prompt` — 这是整个项目最核心的流程。

```
session/prompt 请求
        │
        ▼
  ┌───────────────────────┐
  │  1. 解析请求内容       │── 提取 userMessage、附件等
  └───────────┬───────────┘
              │
              ▼
  ┌───────────────────────┐
  │  2. 构建 TurnContext   │── 从会话恢复历史消息 + 上下文注入
  └───────────┬───────────┘
              │
              ▼
  ┌───────────────────────┐
  │  3. 启动 AgentLoop    │── 复用 atomcode_core::agent::AgentLoop
  │     (异步任务)         │     通过 channel 通信
  └───────────┬───────────┘
              │
              ▼ AgentEvent 流
  ┌───────────────────────┐
  │  4. 事件 → 通知转换    │── 每个 AgentEvent → session/update notification
  │                       │     AgentEvent::TextDelta    → text contentBlock delta
  │                       │     AgentEvent::ReasoningDelta → reasoning contentBlock
  │                       │     AgentEvent::ToolCallBatch → tool_use contentBlock
  │                       │     AgentEvent::Permission    → request_permission 请求
  │                       │     AgentEvent::Error         → error notification
  └───────────┬───────────┘
              │
              ▼
  ┌───────────────────────┐
  │  5. 构建最终响应       │── AgentLoop 结束 → PromptResponse
  └───────────────────────┘
```

**关键设计点**：

1. **并发控制** — 一个 session 同一时间只能有一个 prompt 在执行
2. **取消机制** — `session/cancel` 通知通过 `CancellationToken` 传播
3. **权限请求** — `tool_use` 前的 `request_permission`：Agent 发出 Client 请求，等待响应后再继续
4. **令牌统计** — 在 `stopReason` 中包含 token 使用信息
5. **错误处理** — AgentLoop 内部的错误映射为 `session/update` 错误通知 + `PromptResponse` 中的 `stopReason: error`

**AgentEvent → ACP 通知映射表**：

| `AgentEvent`（atomcode-core） | ACP `session/update` | 类型 |
|-------------------------------|----------------------|------|
| `Text { content }` | `contentBlock { type: text, text: content }` | notification |
| `TextDelta { delta }` | `contentBlock { type: text, partial: true, text: delta }` | notification |
| `ToolCallBatch { calls }` | `contentBlock { type: tool_use, id, name, input }` | notification |
| `ToolResult { id, content }` | `contentBlock { type: tool_result, id, content }` | notification |
| `PermissionRequest { ... }` | `session/request_permission` | client request |
| `Error { msg }` | `error { code, message }` | notification |
| `Stats { tokens_in, tokens_out }` | `tokenUsage` | in PromptResponse |

**验证标准**：
```bash
# 创建会话 → 发送 prompt → 接收流式更新 → 收到 end_turn 响应
cargo test test_prompt_flow
```

---

### Phase 4：工具执行（Week 4）

**目标**：实现工具执行策略 — Agent 侧执行（V1）与 Client 侧委托（V2 基础）。

#### V1：Agent 侧执行

```rust
// Agent 内部执行工具，通过 session/update 通知同步结果
async fn execute_tool_agent_side(
    tool_call: ToolCall,
    tool_registry: &ToolRegistry,
) -> Result<AgentEvent, AgentError> {
    // 1. 发送 tool_use 通知
    send_notification(SessionUpdateNotification::content_block(
        ContentBlock::ToolUse { id, name, input }
    ));
    
    // 2. 执行工具（复用 atomcode_core::tool::Tool 系统）
    let result = tool_registry.execute(tool_call).await;
    
    // 3. 发送 tool_result 通知
    send_notification(SessionUpdateNotification::content_block(
        ContentBlock::ToolResult { id, content: result }
    ));
    
    Ok(result)
}
```

**支持的工具**（复用现有实现）：

| 工具 | 对应 atomcode-core | 是否需要 Client 委托 |
|------|-------------------|---------------------|
| bash | `tool::Bash::execute` | V2 可选 |
| read | `tool::Read::execute` | V2 可选 |
| write | `tool::Write::execute` | V2 可选 |
| edit | `tool::Edit::execute` | V2 可选 |
| grep | `tool::Grep::execute` | V2 可选 |
| directory list | `tool::Directory::execute` | V2 可选 |

#### V2 预留：Client 侧委托

```rust
// Agent 发出 Client 请求，等待编辑器执行
async fn execute_tool_client_side(
    tool_call: ToolCall,
    client_request: &impl ClientRequestSender,
) -> Result<AgentEvent, AgentError> {
    match tool_call.name.as_str() {
        "read" => {
            let result = client_request
                .send_request(ReadTextFileRequest { path })
                .await?;
            Ok(AgentEvent::ToolResult { id, content: result })
        }
        "bash" => {
            let terminal = client_request
                .send_request(CreateTerminalRequest { cmd })
                .await?;
            // 监听 terminal/output 通知
            // ...
        }
        // ...
    }
}
```

---

### Phase 5：传输层与二进制入口（Week 5）

**目标**：完善二进制入口，支持多种传输方式。

#### 5.1 `daemon2acp-stdio`

```rust
// crates/daemon2acp-stdio/src/main.rs
#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt::init();
    
    let agent_state = AgentState::load_default()?;
    let transport = agent_client_protocol::Stdio::new();
    
    UntypedRole::builder()
        .name("atomcode-daemon2acp")
        .on_receive_request(/* initialize */)
        .on_receive_request(/* session/new */)
        .on_receive_request(/* session/load */)
        .on_receive_request(/* session/prompt */)
        .on_receive_notification(/* session/cancel */)
        // ... 其他方法
        .connect_to(transport)
        .await?;
    
    Ok(())
}
```

- **输入**：stdin（JSON-RPC 消息，以 `\n` 分隔）
- **输出**：stdout（JSON-RPC 消息）、stderr（日志）
- **启动方式**：编辑器直接通过子进程启动

#### 5.2 `daemon2acp-http`（可选）

- 兼容现有 `atomcode-daemon` 的 CLI 参数：`--host`、`--port`、`--idle-timeout`
- 基于 Streamable HTTP transport（草案）
- 保留 CORS、idle-timeout、config 热重载等现有功能

**CLI 参数设计**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--transport` | `stdio` | 传输协议：`stdio`、`http` |
| `--host` | `127.0.0.1` | HTTP 监听地址 |
| `--port` | `13456` | HTTP 监听端口 |
| `--idle-timeout` | `1800` | 空闲超时（秒，0=禁用） |
| `--log-level` | `info` | 日志级别 |

---

### Phase 6：测试与合规性验证（Week 6）

**目标**：全面测试确保协议合规性和端到端功能。

#### 6.1 单元测试

- 每个 handler 的独立测试（mock AgentState）
- `session_map.rs` 中的映射逻辑测试
- `event_adapter.rs` 中 AgentEvent → ACP notification 的转换测试

#### 6.2 集成测试

```rust
// tests/integration_test.rs
#[tokio::test]
async fn test_full_session_lifecycle() {
    // 1. 通过 Channel transport 启动 Agent（无需真实 stdio）
    let (agent_tx, client_rx) = channel();
    let (client_tx, agent_rx) = channel();
    
    let agent_handle = tokio::spawn(run_agent(agent_rx, agent_tx));
    
    // 2. 模拟 Client 行为
    let client = TestClient::new(client_tx, client_rx);
    client.initialize().await.unwrap();
    let session = client.new_session().await.unwrap();
    let response = client.prompt(session.id, "Hello").await.unwrap();
    assert_eq!(response.stop_reason, "end_turn");
    
    client.close_session(session.id).await.unwrap();
}
```

#### 6.3 协议合规性测试

- 使用 ACP 官方 JSON Schema 验证所有发出的消息格式
- 验证 JSON-RPC 2.0 合规性（`jsonrpc` 字段、`id` 格式、错误对象结构等）
- 验证 `session/update` 通知中 `contentBlock` 的格式正确性

#### 6.4 与真实 Client 联调

```bash
# 启动 Agent
cargo run -p daemon2acp-stdio

# 使用官方示例 Client 连接
git clone https://github.com/agentclientprotocol/rust-sdk
cd rust-sdk
cargo run --example yolo_one_shot_client -- ../daemon2acp/target/debug/daemon2acp-stdio
```

---

## 6 与现有 daemon 的兼容策略

| 现有 `atomcode-daemon` 功能 | 处理方式 |
|----------------------------|----------|
| `POST /chat`（SSE 流） | → 替换为 `session/prompt` + `session/update` notifications |
| `GET /history` | → 替换为 `session/list` |
| `GET /tools` 等工具端点 | → 工具执行转为内部调度，通过 AgentCapabilities 声明 |
| `--host` / `--port` | → HTTP transport 保留；stdio transport 不需要 |
| `--idle-timeout` | → HTTP transport 保留 |
| `--no-telemetry` | → 保留，统一处理 |
| `config.toml` 热重载 | → 保留，通过 `session/set_config_option` 对外暴露部分配置 |
| 认证/鉴权 | → 使用 ACP 标准 `authenticate` 方法 + `AuthMethod` |
| CORS 保护 | → HTTP transport 保留 |
| Web UI | → 构建一个简单的 ACP Client Web 端，连接 daemon2acp-http |

**过渡方案**：`daemon2acp-http` 可在同一进程中同时暴露 ACP JSON-RPC 端点（`POST /acp`）和旧版 API，便于逐步迁移客户端。

---

## 7 关键依赖

```
daemon2acp
  ├── agent-client-protocol 1.0.0       # ACP Agent runtime (Builder 模式)
  ├── agent-client-protocol-schema 1.1.0 # ACP 协议类型定义
  ├── atomcode-core (git)               # AgentLoop, session, provider, tool
  ├── tokio { features = ["full"] }     # 异步运行时
  ├── serde / serde_json                # JSON 序列化
  ├── tracing / tracing-subscriber      # 日志
  ├── uuid { features = ["v4"] }        # sessionId 生成
  ├── chrono                            # 时间戳
  ├── async-trait                       # 异步 trait
  └── anyhow / thiserror                # 错误处理

[dev-dependencies]
  ├── tokio-util                        # 测试用 channel transport
  └── schemars                          # JSON Schema 验证
```

### Cargo.toml 示例

```toml
# 根 workspace
[workspace]
members = [
    "crates/daemon2acp-core",
    "crates/daemon2acp-stdio",
    "crates/daemon2acp-http",
]
resolver = "2"

[workspace.dependencies]
agent-client-protocol = "1.0"
agent-client-protocol-schema = "1.1"
tokio = { version = "1", features = ["full"] }
serde = { version = "1", features = ["derive"] }
serde_json = "1"
tracing = "0.1"
tracing-subscriber = "0.3"
uuid = { version = "1", features = ["v4"] }
anyhow = "1"
thiserror = "2"
```

---

## 8 风险与应对

| # | 风险 | 影响 | 可能性 | 应对措施 |
|---|------|------|--------|----------|
| 1 | `atomcode-core` 的 AgentLoop 事件模型与 ACP 不完全匹配 | 适配工作量大 | 中 | Phase 3 前置分析 AgentEvent 枚举，设计双向映射表 |
| 2 | ACP 协议仍在演进（v2 提案已出现） | 可能需要持续适配 | 中 | 基于 v1 稳定 API 开发，使用 feature flag 隔离 unstable 功能 |
| 3 | 现有 HTTP daemon 用户需要平滑过渡 | 兼容性风险 | 低 | 提供 HTTP transport，协议共存期支持双端点 |
| 4 | Client 侧工具委托需要编辑器支持 | 功能受限 | 高 | V1 优先 Agent 侧执行，V2 逐步增加 Client 委托 |
| 5 | Windows 平台 stdio 行为差异 | 传输层适配 | 低 | 复用已有 Windows 兼容逻辑 |
| 6 | `agent-client-protocol` crate 要求 `!Send` future | 架构约束 | 中 | 使用 `tokio::task::LocalSet` 和 `spawn_local` |
| 7 | `atomcode-core` 的 git 依赖无法稳定版本化 | 构建安全 | 低 | 锁定到特定 commit/tag |

---

## 9 交付物清单

| # | 产出 | 类型 | 说明 |
|---|------|------|------|
| 1 | `PLAN.md` | 文档 | 本项目规划文件 |
| 2 | `daemon2acp-core` | crate | ACP Agent 核心适配库 |
| 3 | `daemon2acp-stdio` | 二进制 | stdio 传输的 ACP Agent |
| 4 | `daemon2acp-http` | 二进制（可选） | HTTP 传输的 ACP Agent |
| 5 | 集成测试套件 | 测试 | 协议合规性 + 端到端测试 |
| 6 | `ARCHITECTURE.md` | 文档 | 架构设计说明 |
| 7 | `README.md` | 文档 | 使用说明 |
| 8 | VS Code ACP 配置示例 | 配置 | 编辑器端接入配置 |

---

## 10 快速启动

```bash
# 1. 创建 workspace
cargo init --workspace
mkdir crates

# 2. 创建 core crate
cargo new crates/daemon2acp-core --lib

# 3. 添加依赖
# (编辑 Cargo.toml 配置 workspace.dependencies)

# 4. 创建 stdio binary crate
cargo new crates/daemon2acp-stdio

# 5. 实现最小 ACP Agent
#    参考 crates/daemon2acp-core/src/agent.rs 模板

# 6. 运行
cargo run -p daemon2acp-stdio

# 7. 用 ACP 示例 Client 连接测试
git clone https://github.com/agentclientprotocol/rust-sdk
cd rust-sdk
RUST_LOG=info cargo run --example yolo_one_shot_client -- \
    ../daemon2acp/target/debug/daemon2acp-stdio
```

### 最小 Agent 代码模板

```rust
// crates/daemon2acp-core/src/agent.rs
use agent_client_protocol::{UntypedRole, on_receive_request};
use agent_client_protocol::schema::{
    ProtocolVersion,
    v1::{
        InitializeRequest, InitializeResponse,
        NewSessionRequest, NewSessionResponse,
        PromptRequest, PromptResponse,
        Implementation, AgentCapabilities,
    },
};
use crate::state::AgentState;

pub async fn run_stdio_agent(state: AgentState) -> anyhow::Result<()> {
    let transport = agent_client_protocol::Stdio::new();
    
    UntypedRole::builder()
        .name("atomcode-daemon2acp")
        .on_receive_request(
            async |req: InitializeRequest, responder, _cx| {
                let resp = InitializeResponse::new(ProtocolVersion::V1)
                    .agent_info(
                        Implementation::new("atomcode-daemon2acp", env!("CARGO_PKG_VERSION"))
                            .title("AtomCode ACP Agent")
                    )
                    .agent_capabilities(build_capabilities());
                responder.respond(resp)
            },
            on_receive_request!()
        )
        .on_receive_request(
            async |req: NewSessionRequest, responder, cx| {
                // 调用 atomcode session manager 创建会话
                let session_info = state.create_session(req).await?;
                responder.respond(NewSessionResponse::new(session_info))
            },
            on_receive_request!()
        )
        // ... 更多处理器
        .connect_to(transport)
        .await?;
    
    Ok(())
}
```

---

## 附录 A：参考资源

| 资源 | 链接 |
|------|------|
| ACP 协议文档 | https://agentclientprotocol.com/protocol/v1/overview |
| ACP Rust SDK | https://docs.rs/agent-client-protocol |
| ACP Schema 定义 | https://docs.rs/agent-client-protocol-schema |
| Rust SDK 源码 | https://github.com/agentclientprotocol/rust-sdk |
| AtomCode 项目 | https://gitcode.com/atomgit_atomcode/atomcode |
| ACP Agent 示例 | https://github.com/agentclientprotocol/rust-sdk/blob/main/src/agent-client-protocol/examples/agent.rs |

## 附录 B：术语表

| 术语 | 说明 |
|------|------|
| ACP | Agent Client Protocol — 编辑器与 AI 编码代理之间的标准化协议 |
| Agent | ACP 中提供 AI 能力的程序（本程序扮演的角色） |
| Client | ACP 中提供编辑器/IDE 界面的程序 |
| JSON-RPC 2.0 | 轻量级 JSON 远程调用协议，ACP 的消息格式基础 |
| Session | ACP 中的一次对话会话，包含消息历史和状态 |
| AgentLoop | atomcode-core 中的自动工具使用循环 |
| Turn | 一次完整的用户消息 → AI 响应循环 |
| SSE | Server-Sent Events — 服务器推送事件流 |
| Transport | ACP 传输层（stdio / HTTP / WebSocket） |
