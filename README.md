# daemon2acp

**AtomCode Daemon HTTP/SSE API → ACP (Agent Client Protocol) v1 协议映射**

让支持 ACP 协议的客户端（QwenPaw、Zed、VS Code 等）能驱动 AtomCode AI 编码能力。

## 架构

```
ACP 客户端 ──(JSON-RPC stdio)──→ daemon2acp ──(HTTP/SSE)──→ atomcode-daemon ──→ LLM
```

## 快速使用

```bash
# mock 模式（无需 AtomCode，用于测试）
cd python && python stdio_server.py

# proxy 模式（自动拉起 atomcode daemon）
cd python && DAEMON2ACP_MODE=proxy python stdio_server.py
```

## 核心目录

| 目录/文件 | 说明 |
|-----------|------|
| `python/` | Python 实现（主代码） |
| `python/stdio_server.py` | stdio 传输层入口（供 ACP 客户端启动） |
| `python/server.py` | HTTP 服务入口（aiohttp） |
| `python/src/` | 核心模块：代理转发、会话管理、事件映射 |
| `python/tests/` | 单元测试 + E2E 联调脚本 |
| `python/QWENPAW_SETUP.md` | QwenPaw 接入指南 |
| `python/ACP_CLIENT_GUIDE.md` | ACP 客户端接入指南 |
| `PROGRESS.md` | 完整改造进度与已解决问题归档 |
| `crates/` | Rust 参考实现（早期，当前以 Python 为准） |

## 环境要求

- Python 3.10+
- aiohttp, pydantic（`pip install aiohttp pydantic`）
- proxy 模式需要安装 [AtomCode](https://atomgit.com)

## 详细文档

详见 [python/README.md](python/README.md)。
