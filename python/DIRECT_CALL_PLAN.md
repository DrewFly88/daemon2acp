# 直接调用模式规划 — daemon2acp 进程内调用 atomcode-core

> 当前进度：转发模式（proxy）已完成并通过端到端验证。本文档规划下一步：去掉 atomcode-daemon 依赖，daemon2acp 自己跑 AgentLoop。

---

## 1 两种模式对比

| | 转发模式（已实现 ✅） | 直接调用模式（本文档） |
|---|---|---|
| **架构** | `Client → daemon2acp → HTTP → atomcode-daemon → core → LLM` | `Client → daemon2acp → core → LLM` |
| **进程数** | 2（atomcode-daemon + daemon2acp） | 1（daemon2acp 自身） |
| **Python 依赖** | aiohttp（HTTP 客户端） | PyO3 binding / cdylib / FFI |
| **延迟** | 多一跳 HTTP（~1ms 本地） | 无额外跳 |
| **开发难度** | 低（已实现） | 高（需 Rust → Python 绑定） |
| **独立性** | 依赖 atomcode-daemon 存活 | 自包含 |
| **配置一致性** | 两边各读一份 config.toml | 一份配置 |

---

## 2 技术路径

有三种方式让 Python 调用 Rust 的 `atomcode-core`：

### 路径 A：PyO3 binding（推荐）

用 [PyO3](https://pyo3.rs) 将 atomcode-core 暴露为 Python 模块。

```
atomcode-core (Rust)
    │
    ▼ PyO3 #[pymodule]
atomcode_core_py (Python extension crate)
    │
    ▼ maturin build
atomcode_core_py.so / .pyd  ← Python importable
    │
    ▼
daemon2acp (Python) → import atomcode_core_py
```

**优点**：类型安全、零拷贝数据传递、性能最优
**缺点**：需要 Rust 编译链、每次 core 升级要重编 binding

**实现步骤**：

1. 在 `crates/` 下新增 `atomcode-core-py` crate
2. 添加 PyO3 依赖，用 `#[pymodule]` 暴露核心类型：
   - `AgentLoop` → `agent_core_py.AgentLoop`
   - `Session` → `agent_core_py.Session`
   - `TurnEvent` → `agent_core_py.TurnEvent`
   - `Config` → `agent_core_py.Config`
   - `Provider` → `agent_core_py.Provider`
3. 用 [maturin](https://www.maturin.rs) 构建 wheel：
   ```bash
   maturin develop --release  # 开发时
   maturin build --release    # 产出 .whl
   ```
4. Python 端替换 `proxy_agent.py`：
   ```python
   import atomcode_core_py

   async def run_direct_agent(session_id, message, cancel_event):
       loop = atomcode_core_py.AgentLoop(config)
       async for event in loop.run(message):
           yield translate_core_event(event)
   ```

**关键 PyO3 映射**：

| Rust 类型 | Python 类型 | 说明 |
|-----------|------------|------|
| `AgentLoop` | `agent_core_py.AgentLoop` | 主循环，需暴露 `run()` 为 async generator |
| `AgentEvent` | `agent_core_py.AgentEvent` | 枚举：TextDelta / ToolCall / ToolResult / Error |
| `Session` | `agent_core_py.Session` | 会话管理 |
| `Config` | `agent_core_py.Config` | 从 `config.toml` 加载 |
| `ToolRegistry` | `agent_core_py.ToolRegistry` | 工具注册表 |

**难点**：
- `AgentLoop` 是 async 的，PyO3 需要配合 `tokio` runtime
- `AgentEvent` 通过 channel 流出，需映射为 Python `AsyncIterator`
- `AgentLoop` 内部的 `AgentCommand` / `AgentEvent` channel 模型需要适配

### 路径 B：cdylib FFI

将 atomcode-core 编译为 C 动态库，Python 通过 `ctypes` / `cffi` 调用。

```
atomcode-core → [no_mangle extern "C"] → libatomcode_core.so / .dll
    │
    ▼ cffi / ctypes
daemon2acp (Python)
```

**优点**：不依赖 PyO3，cffi 性能也不错
**缺点**：需要手写 C ABI 层、数据序列化/反序列化开销、类型不安全

**不推荐**：手写 C ABI 工作量大，且 PyO3 已经是 Rust→Python 的标准方案。

### 路径 C：子进程 IPC

daemon2acp 启动 `atomcode -p "..."` 子进程，通过 stdout 捕获输出。

**优点**：零 Rust 编译需求、最简单
**缺点**：每个 prompt 启动一个进程（慢）、无法共享会话状态、无法流式获取工具调用中间过程

**适用场景**：快速验证、CI/CD 环境、不需要流式工具调用

---

## 3 推荐方案：路径 A（PyO3 binding）

### 3.1 项目结构

```
daemon2acp/
├── crates/
│   └── atomcode-core-py/          # ← 新增 PyO3 binding crate
│       ├── Cargo.toml
│       └── src/
│           └── lib.rs             # #[pymodule] 定义
├── python/
│   ├── src/
│   │   ├── direct_agent.py        # ← 新增：进程内调用实现
│   │   └── proxy_agent.py         # 已有：转发模式
│   └── ...
```

### 3.2 Cargo.toml

```toml
[package]
name = "atomcode-core-py"
version = "0.1.0"
edition = "2024"

[lib]
name = "atomcode_core_py"
crate-type = ["cdylib"]

[dependencies]
atomcode-core = { git = "https://github.com/atomgit-atomcode/atomcode" }
pyo3 = { version = "0.24", features = ["extension-module"] }
pyo3-asyncio = { version = "0.24", features = ["tokio-runtime"] }
tokio = { version = "1", features = ["full"] }
serde_json = "1"
```

### 3.3 Python 端 direct_agent.py

```python
"""直接调用模式 — 通过 PyO3 binding 进程内调用 atomcode-core"""

import asyncio
from typing import AsyncIterator

import atomcode_core_py  # PyO3 binding

from .acp_mapper import (
    make_error_event,
    make_text_event,
    make_tool_result_event,
    make_tool_use_event,
    make_turn_end_event,
)


def _translate_core_event(event: dict) -> list[dict]:
    """将 atomcode-core 事件转换为 ACP 事件"""
    results = []
    event_type = event.get("type", "")

    if event_type == "text":
        results.append({"event": "text", "data": make_text_event(event.get("text", ""))})
    elif event_type == "tool_use":
        results.append({
            "event": "tool_use",
            "data": make_tool_use_event(
                event.get("id", ""), event.get("name", ""), event.get("input", {})
            ),
        })
    elif event_type == "tool_result":
        results.append({
            "event": "tool_result",
            "data": make_tool_result_event(event.get("id", ""), event.get("content", "")),
        })
    elif event_type == "end_turn":
        results.append({"event": "turn_end", "data": make_turn_end_event(event.get("stopReason", "end_turn"))})
    elif event_type == "error":
        results.append({"event": "error", "data": make_error_event(event.get("message", ""))})

    return results


async def run_direct_agent(
    session_id: str,
    message: str,
    cancel_event: asyncio.Event,
    config_path: str | None = None,
) -> AsyncIterator[dict]:
    """直接调用模式 Agent 执行 — 进程内调用 atomcode-core

    签名与 run_mock_agent / run_proxy_agent 一致。
    """
    loop = atomcode_core_py.AgentLoop(config_path)
    events = await loop.run(message)

    for event in events:
        if cancel_event.is_set():
            yield {"event": "turn_end", "data": make_turn_end_event("cancelled")}
            return
        for acp_event in _translate_core_event(event):
            yield acp_event
```

### 3.4 server.py 集成

```python
# 在 server.py 中根据 DAEMON2ACP_MODE 环境变量选择模式
RUN_MODE = os.environ.get("DAEMON2ACP_MODE", "mock")  # "mock" | "proxy" | "direct"

if RUN_MODE == "direct":
    from src.direct_agent import run_direct_agent
    # chat_handler 中：
    agent_stream = run_direct_agent(session_id, message, cancel_event)
```

---

## 4 构建与发布

### 4.1 开发时

```bash
# 构建 PyO3 binding（开发模式，可编辑）
cd crates/atomcode-core-py
maturin develop

# 启动 daemon2acp（直接调用模式）
DAEMON2ACP_MODE=direct python server.py
```

### 4.2 发布 wheel

```bash
maturin build --release --interpreter python3.10
# 产出 target/wheels/atomcode_core_py-0.1.0-cp310-cp310-win_amd64.whl

pip install target/wheels/atomcode_core_py-0.1.0-cp310-cp310-win_amd64.whl
```

### 4.3 CI/CD

```yaml
# GitHub Actions 示例
- uses: actions/setup-python@v5
  with: { python-version: "3.10" }
- uses: dtolnay/rust-toolchain@stable
- run: pip install maturin
- run: maturin build --release --interpreter python3.10
- uses: actions/upload-artifact@v4
  with:
    name: wheel
    path: target/wheels/*.whl
```

---

## 5 实现阶段

| Phase | 内容 | 预计时间 |
|-------|------|----------|
| **Phase 1** | 搭建 `atomcode-core-py` crate 骨架，暴露 `AgentLoop::new()` + `Config::load()` | 1 周 |
| **Phase 2** | 暴露 `AgentLoop::run()` 为 async 方法，返回 `Vec<AgentEvent>` | 1 周 |
| **Phase 3** | 实现 `direct_agent.py`，替换 `proxy_agent.py`，端到端测试 | 3 天 |
| **Phase 4** | 流式支持 — `AgentLoop::run()` 返回 async generator 而非 Vec | 1 周 |
| **Phase 5** | wheel 发布 + CI/CD + 文档 | 3 天 |

---

## 6 风险

| 风险 | 影响 | 应对 |
|------|------|------|
| `atomcode-core` 的 `AgentLoop` 需要 tokio runtime | PyO3 async 兼容性 | 使用 `pyo3-asyncio` 的 `tokio-runtime` feature |
| `AgentLoop` 内部用 channel 通信 | 无法直接返回 Vec | Phase 4 改为 async generator，或 Phase 2 先 collect 再返回 |
| `atomcode-core` 是 git 依赖，无稳定版本 | 构建不可复现 | 锁定到特定 commit/tag |
| Windows 上 `.pyd` 文件兼容性 | 分发困难 | 用 maturin 构建 platform wheel，每个平台单独发布 |
| `AgentLoop` 不是 `Send` | PyO3 要求 Send | 用 `tokio::task::spawn_local` + `LocalSet` |

---

## 7 从转发模式迁移到直接调用

迁移步骤（零停机）：

1. **构建 binding** — `maturin develop` 安装到本地
2. **灰度切换** — 通过环境变量 `DAEMON2ACP_MODE=direct` 启动新实例
3. **验证** — 对比两种模式的输出一致性
4. **切换流量** — 客户端指向新实例
5. **下线 atomcode-daemon** — 不再需要独立进程

```bash
# 灰度验证脚本
DAEMON2ACP_MODE=proxy python server.py --port 13456   # 旧实例
DAEMON2ACP_MODE=direct python server.py --port 13457  # 新实例

# 对比同一 prompt 的输出
curl -N http://127.0.0.1:13456/chat -d '{"message":"test"}' > proxy_output.txt
curl -N http://127.0.0.1:13457/chat -d '{"message":"test"}' > direct_output.txt
diff proxy_output.txt direct_output.txt
```
