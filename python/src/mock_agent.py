"""模拟 AI 处理 — 后续替换为 atomcode-core 桥接（通过子进程或 IPC）"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

from .acp_mapper import (
    make_error_event,
    make_text_chunk,
    make_thought_chunk,
    make_tool_call_start,
    make_tool_call_update,
    make_turn_end_signal,
)


async def run_mock_agent(
    session_id: str,
    message: str,
    cancel_event: asyncio.Event,
) -> AsyncIterator[dict]:
    """模拟 AgentLoop 执行 — 产生 ACP session/update 事件流

    后续接入 atomcode-core 时，替换此函数为：
    - 通过子进程启动 atomcode CLI headless 模式
    - 或通过 IPC 调用 atomcode-core 的 AgentLoop
    - 或直接调用 atomcode daemon 的 HTTP API（转发模式）
    """
    # 1. 推理内容 — agent_thought_chunk
    yield {"event": "agent_thought_chunk", "data": make_thought_chunk(f"思考用户请求: {message[:80]}")}
    await asyncio.sleep(0.3)

    # 检查取消
    if cancel_event.is_set():
        yield {"event": "turn_end", "data": make_turn_end_signal("cancelled")}
        return

    # 2. 文本响应 — agent_message_chunk
    yield {
        "event": "agent_message_chunk",
        "data": make_text_chunk(f"[mock] 收到消息: {message} (session: {session_id[:8]})"),
    }
    await asyncio.sleep(0.2)

    # 3. 模拟工具调用 — tool_call (start) + tool_call_update (completed)
    call_id = "call_1"
    yield {
        "event": "tool_call",
        "data": make_tool_call_start(call_id, "bash", "execute", {"command": "echo hello"}),
    }
    await asyncio.sleep(0.2)

    yield {
        "event": "tool_call_update",
        "data": make_tool_call_update(call_id, {"stdout": "hello\n", "exitCode": 0}),
    }

    # 4. 结束 — turn_end 是内部信号，不作为 session/update 发送
    yield {"event": "turn_end", "data": make_turn_end_signal("end_turn")}
