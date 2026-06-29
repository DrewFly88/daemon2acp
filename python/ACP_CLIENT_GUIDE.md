# ACP 客户端接入指南

本文档说明如何在主流 ACP 客户端（Zed、VS Code 等）中使用 daemon2acp。

---

## 前置条件

1. Python 3.10+ 已安装
2. 依赖已安装：`pip install aiohttp pydantic`
3. AtomCode 已安装（proxy 模式需要，mock 模式不需要）

---

## 1. Zed 编辑器

Zed 原生支持 ACP，配置最简单。

### 步骤

1. 打开 Zed 设置（`Cmd+,` / `Ctrl+,`）
2. 搜索 `agent` 或编辑 `settings.json`
3. 添加以下配置：

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

4. 在 Zed 的 Agent Panel 中选择 "AtomCode" profile
5. 开始对话

---

## 2. VS Code

VS Code 通过扩展支持 ACP。有两种方式：

### 方式 A：使用 ACP 兼容扩展

安装支持 ACP 的扩展（如 [Cline](https://cline.bot/)、[Roo Code](https://roocode.com/) 等），在扩展设置中配置 Agent：

```json
{
  "cline.acpAgentCommand": "python /path/to/daemon2acp/python/stdio_server.py",
  "cline.acpAgentEnv": {
    "DAEMON2ACP_MODE": "proxy",
    "DAEMON2ACP_AUTO_START": "1"
  }
}
```

### 方式 B：使用 AtomCode 官方扩展 + HTTP 模式

AtomCode 官方 VS Code 扩展连接的是 `atomcode-daemon` 的 HTTP API。可以改为指向 daemon2acp：

```json
{
  "atomcode.daemonUrl": "http://127.0.0.1:13456"
}
```

然后启动 daemon2acp HTTP 服务：

```bash
DAEMON2ACP_MODE=proxy PORT=13456 python server.py
```

---

## 3. JetBrains IDE（IntelliJ / PyCharm / WebStorm 等）

JetBrains 通过 Junie 或第三方插件支持 ACP。

### Junie 配置

在 Junie 设置中添加自定义 Agent：

- **Command**: `python /path/to/daemon2acp/python/stdio_server.py`
- **Transport**: stdio
- **Environment variables**:
  - `DAEMON2ACP_MODE=proxy`
  - `DAEMON2ACP_AUTO_START=1`

---

## 4. 通用 ACP 客户端

任何 ACP 客户端都需要知道两件事：

| 配置项 | 值 |
|--------|-----|
| **启动命令** | `python /path/to/daemon2acp/python/stdio_server.py` |
| **传输协议** | `stdio`（JSON-RPC over stdin/stdout，每行一条消息） |
| **stderr** | 日志输出，客户端可忽略或转发 |

### 交互流程

```
Client                              Agent (daemon2acp)
  │                                      │
  │── initialize ──────────────────────▶│
  │◀── initialize response ─────────────│
  │                                      │
  │── session/new ─────────────────────▶│
  │◀── session/new response ────────────│
  │                                      │
  │── session/prompt ──────────────────▶│
  │                                      │
  │◀── session/update notification ─────│  (流式，多条)
  │◀── session/update notification ─────│
  │◀── session/update notification ─────│
  │                                      │
  │◀── session/prompt response ─────────│  (最终结果)
  │                                      │
  │── session/close ───────────────────▶│
  │◀── session/close response ──────────│
```

---

## 5. acp-agent.json 配置文件

项目根目录下的 `acp-agent.json` 是 ACP Agent 的标准声明文件：

```json
{
  "name": "daemon2acp",
  "title": "AtomCode ACP Agent",
  "version": "0.1.0",
  "description": "AtomCode coding agent via ACP protocol",
  "command": ["python", "/path/to/daemon2acp/python/stdio_server.py"],
  "transport": "stdio",
  "env": {
    "DAEMON2ACP_MODE": "proxy",
    "DAEMON2ACP_AUTO_START": "1",
    "LOG_LEVEL": "info"
  }
}
```

**注意**：你需要根据实际环境修改 `command` 中的路径。

---

## 6. 运行模式选择

| 场景 | 推荐模式 | 环境变量 |
|------|----------|----------|
| 开发/调试 | mock | `DAEMON2ACP_MODE=mock` |
| 日常使用（有 AtomCode） | proxy + auto-start | `DAEMON2ACP_MODE=proxy` |
| AtomCode daemon 已在运行 | proxy（不自动拉起） | `DAEMON2ACP_MODE=proxy`, `DAEMON2ACP_AUTO_START=0` |
| 未来：进程内调用 | direct | `DAEMON2ACP_MODE=direct`（需 PyO3 binding） |

---

## 7. 验证连接

### 手动测试 stdio 通信

```bash
# 启动 Agent，手动输入 JSON-RPC 消息
python stdio_server.py

# 在 stdin 输入（每行一条 JSON）：
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":1}}
# → 收到 initialize 响应

{"jsonrpc":"2.0","id":2,"method":"session/new","params":{}}
# → 收到 session 信息

{"jsonrpc":"2.0","id":3,"method":"session/prompt","params":{"sessionId":"<上一步的id>","messages":[{"role":"user","content":[{"type":"text","text":"hello"}]}]}}
# → 收到 session/update 通知流 + 最终 prompt 响应
```

### 用 ACP Rust SDK 示例客户端测试

```bash
# 安装 Rust
# 克隆 SDK
git clone https://github.com/agentclientprotocol/rust-sdk
cd rust-sdk

# 编译示例客户端
cargo build --example yolo_one_shot_client

# 用它连接 daemon2acp
cargo run --example yolo_one_shot_client -- \
    python /path/to/daemon2acp/python/stdio_server.py
```

---

## 8. 故障排查

| 问题 | 原因 | 解决 |
|------|------|------|
| 客户端报 "Agent exited" | Python 路径不对 | 修改 `command` 为绝对路径 |
| Agent 无响应 | 依赖未安装 | `pip install aiohttp pydantic` |
| proxy 模式报 "atomcode not found" | AtomCode 未安装或不在 PATH | 设置 `ATOMCODE_BIN` 指向 atomcode 可执行文件 |
| daemon 启动后秒退 | 子进程管道缓冲区满 | 已修复：stdout/stderr 设为 DEVNULL |
| daemon 报 "unexpected argument --host" | atomcode daemon 不支持 `--host` | 已修复：启动命令不再传 `--host` |
| daemon 启动超时 | 端口被占用 | 改 `DAEMON2ACP_DAEMON_PORT` 为其他端口 |
| 日志看不到 | stdout 被 ACP 协议占用 | 日志在 stderr，用 `LOG_LEVEL=DEBUG` 查看 |
