# QwenPaw 接入指南

本文档说明如何将 daemon2acp 作为外部 ACP runner 接入 QwenPaw，让 QwenPaw 通过 `delegate_external_agent` 工具调用 AtomCode 的编码能力。

## 架构

```
QwenPaw (ACP client)
   ↕ stdio JSON-RPC
daemon2acp (ACP server / 外部 runner)
   ↕ HTTP/SSE
atomcode-daemon → atomcode-core → LLM
```

QwenPaw 通过内置工具 `delegate_external_agent` 调用外部 ACP runner。本项目就是把 AtomCode daemon 包装成 ACP server，被 QwenPaw 当作 runner 调用。

## 前置条件

1. **AtomCode** 已安装（`/path/to/atomcode` 或 PATH 中可找到 `atomcode`）
2. **Python** 可用（推荐使用与 QwenPaw 同一环境的 Python，已有 aiohttp/pydantic 依赖）
3. **daemon2acp** 项目已克隆到本地

## 配置步骤

### 1. 在 QwenPaw 控制台打开 ACP 配置页

进入 **Workspace → ACP** 页面，添加自定义 runner。

### 2. 填写 runner 配置

| 字段 | 值 |
|------|-----|
| `enabled` | `true` |
| `command` | `/path/to/python`（推荐用 QwenPaw 环境的 Python） |
| `args` | 每行一个参数：<br>`/path/to/daemon2acp/python/stdio_server.py` |
| `env` | 见下表 |
| `trusted` | `true`（可选，信任此 runner） |
| `tool_parse_mode` | 保持默认 |
| `stdio_buffer_limit_bytes` | 保持默认 |

**注意**：`args` 的每个参数必须**单独占一行**。本项目只需一个参数（脚本路径），所以填一行即可。

### 3. 环境变量

| 变量 | 值 | 说明 |
|------|-----|------|
| `DAEMON2ACP_MODE` | `proxy` | 必填。使用 proxy 模式连接 atomcode daemon |
| `DAEMON2ACP_AUTO_START` | `1` | 必填。自动拉起 atomcode daemon 子进程 |
| `DAEMON2ACP_DAEMON_PORT` | `13459` | 可选。daemon 监听端口（默认 13456） |
| `LOG_LEVEL` | `info` | 可选。日志级别，输出到 stderr |
| `ATOMCODE_BIN` | `/path/to/atomcode` | 可选。atomcode 可执行文件绝对路径（PATH 找不到时设） |

### 4. 启用工具

在工具栏中启用 `delegate_external_agent` 工具。

## 使用方式

配置完成后，在对话中明确指定与此 runner 协作：

```
请用 daemon2acp 分析当前工作目录结构
```

或直接调用工具：

```python
# 启动新会话
delegate_external_agent(action="start", runner="daemon2acp", cwd="<工作目录>", message="...")

# 续接会话
delegate_external_agent(action="message", runner="daemon2acp", message="...")

# 关闭会话
delegate_external_agent(action="close", runner="daemon2acp")
```

## 支持的 ACP 方法

本项目实现的 ACP JSON-RPC 方法（method 字符串对照 `acp` SDK `meta.py`）：

| method | 说明 |
|--------|------|
| `initialize` | 握手，返回 agentInfo 和 capabilities |
| `session/new` | 创建新会话 |
| `session/load` | 加载会话（loadSession=false，仅返回内存中存在的） |
| `session/list` | 列出活跃会话 |
| `session/close` | 关闭会话 |
| `session/delete` | 删除会话 |
| `session/set_mode` | 设置会话模式 |
| `session/prompt` | 发送消息，流式返回响应 |
| `session/cancel` | 取消进行中的 prompt（通知） |
| `session/request_permission` | **server→client 请求**，权限交互 |

## session/update 类型

本项目发送的 `session/update` 通知 payload 结构对照 `acp` SDK `schema.py`：

| sessionUpdate 值 | 触发时机 |
|-----------------|---------|
| `agent_message_chunk` | AI 文本增量 |
| `agent_thought_chunk` | AI 推理增量 |
| `tool_call` | 工具调用开始 |
| `tool_call_update` | 工具调用完成 |

`session/prompt` 的最终响应（非 update）：

```json
{"stopReason": "end_turn" | "tool_use", "tokenUsage": {"input": 0, "output": 0}}
```

## 权限处理

当 atomcode daemon 发起工具权限请求时，daemon2acp 会向 QwenPaw 发 `session/request_permission` 请求：

1. daemon2acp 暂停当前 prompt 流
2. 向 QwenPaw 发送权限请求（含 toolCall 详情和 options）
3. QwenPaw 展示给用户选择
4. 用户选择 option 后，QwenPaw 回响应（含 `outcome.optionId`）
5. daemon2acp 收到响应，记录结果

**注意**：将权限结果反馈回 atomcode daemon 需要 daemon 协议支持反向注入，当前 daemon HTTP/SSE 不支持此能力。若 daemon 不发起 permission 事件，此路径不会被触发。

## 故障排查

### daemon 启动失败

检查 `ATOMCODE_BIN` 环境变量是否指向正确的 atomcode 可执行文件。stderr 日志会显示：

```
starting atomcode daemon: /path/to/atomcode daemon --port 13459 ...
```

若报 `atomcode not found`，设置 `ATOMCODE_BIN` 为绝对路径。

### JSON-RPC 通信异常

stderr 日志是唯一诊断手段（stdout 只用于 JSON-RPC）。设 `LOG_LEVEL=debug` 查看详细日志。

### daemon 子进程残留

正常关闭时 daemon2acp 会优雅停止 daemon。异常退出可能导致残留进程占用端口。手动清理：

```bash
netstat -ano | findstr :13459
taskkill /F /PID <pid>
```

## Windows 注意事项

- `command` 填 `.exe` 路径时**不需要** `cmd /c` 包装（`cmd /c` 仅用于 `.cmd`/`.bat`）
- 脚本路径含中文无影响，Python 处理 Unicode 路径正常
- daemon 子进程通过端口健康检查判断存活（atomcode daemon 会 fork，父进程立即退出）
