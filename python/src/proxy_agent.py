"""转发代理 — 连接 atomcode-daemon HTTP API，将 SSE 事件转换为 ACP 格式

架构：
    ACP Client → daemon2acp → HTTP/SSE → atomcode-daemon → atomcode-core → LLM
                                              ↑
                                         事件格式转换
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

import aiohttp

from .acp_mapper import (
    make_error_event,
    make_text_chunk,
    make_thought_chunk,
    make_tool_call_start,
    make_tool_call_update,
    make_turn_end_signal,
)

logger = logging.getLogger("daemon2acp.proxy")


# ============================================================
# atomcode-daemon SSE 事件 → ACP session/update 事件映射
# ============================================================

# atomcode-daemon 的 TurnEvent 类型（来自 atomcode-core/src/turn/event.rs）
# 主要事件类型：
#   TextDelta      — AI 文本增量
#   ReasoningDelta — 推理增量
#   ToolCallBatch  — 工具调用批次（start）
#   ToolResult     — 工具执行结果
#   Stats          — token 使用统计
#   Error          — 错误
#   EndTurn        — turn 结束
#   Permission     — 权限请求

def translate_daemon_event(event_type: str, event_data: dict[str, Any]) -> list[dict]:
    """将 atomcode-daemon 的 SSE 事件转换为 ACP session/update 事件列表

    atomcode-daemon 实际事件格式（来自 /chat SSE 流）：
        {"type": "text",    "content": "..."}      — AI 文本增量
        {"type": "tokens",  "prompt": N, ...}      — token 使用统计
        {"type": "done",    "tokens": N, ...}      — turn 结束

    也兼容未来可能的事件（tool_use / tool_result / reasoning / error）。

    Returns:
        list of {"event": str, "data": dict} — ACP SSE 事件列表
    """
    results: list[dict] = []

    # ---- 文本增量 ----
    # daemon 格式: {"type": "text", "content": "..."}
    # ACP: agent_message_chunk
    if event_type in ("text", "TextDelta", "text_delta"):
        content = event_data.get("content", event_data.get("text", event_data.get("delta", "")))
        if content:
            results.append({"event": "agent_message_chunk", "data": make_text_chunk(content)})

    # ---- 推理增量 ----
    # ACP: agent_thought_chunk
    elif event_type in ("reasoning", "ReasoningDelta", "reasoning_delta"):
        content = event_data.get("content", event_data.get("text", event_data.get("delta", "")))
        if content:
            results.append({"event": "agent_thought_chunk", "data": make_thought_chunk(content)})

    # ---- 工具调用开始 ----
    # ACP: tool_call (ToolCallStart)
    # 必填: toolCallId, title, kind, status=pending, content=[], locations=[], rawInput
    elif event_type in ("tool_use", "ToolCallBatch", "tool_call_batch"):
        calls = event_data.get("calls", event_data.get("toolCalls", []))
        if not isinstance(calls, list):
            calls = [event_data]
        for call in calls:
            call_id = call.get("id", call.get("callId", "call_unknown"))
            name = call.get("name", call.get("toolName", "unknown"))
            input = call.get("input", call.get("args", {}))
            # kind 粗判：写操作 vs 读操作 vs 其他
            kind = "other"
            name_lower = name.lower()
            if any(k in name_lower for k in ("write", "edit", "create", "delete", "remove", "move", "rename")):
                kind = "execute"
            elif any(k in name_lower for k in ("read", "list", "search", "grep", "glob", "stat")):
                kind = "read_only"
            results.append({
                "event": "tool_call",
                "data": make_tool_call_start(call_id, name, kind, input),
            })

    # ---- 工具结果 ----
    # ACP: tool_call_update (ToolCallProgress, status=completed/error)
    elif event_type in ("tool_result", "ToolResult"):
        call_id = event_data.get("id", event_data.get("callId", event_data.get("toolCallId", "call_unknown")))
        content = event_data.get("content", event_data.get("result", ""))
        is_error = event_data.get("isError", False)
        results.append({
            "event": "tool_call_update",
            "data": make_tool_call_update(call_id, content, is_error=is_error),
        })

    # ---- Token 统计 ----
    # daemon 格式: {"type": "tokens", "prompt": N, "completion": N, "total": N}
    # ACP: usage_update（可选，暂不发送，仅记录日志）
    elif event_type in ("tokens", "stats", "Stats", "token_usage"):
        logger.debug("token stats: %s", event_data)

    # ---- Turn 结束 ----
    # daemon 格式: {"type": "done", "tokens": N, "tool_calls": N, "session_id": "..."}
    # 注意：turn_end 不是 session/update 通知，而是 session/prompt 的响应 result。
    # 此处产出内部信号，由上层 consume 后转为响应，不作为 update 发送。
    #
    # ACP PromptResponse.stopReason 允许的 Literal 值：
    #   end_turn, max_tokens, max_turn_requests, refusal, cancelled
    # 工具调用的信息已通过 session/update（tool_call / tool_call_update）推送，
    # stopReason 固定为 end_turn。
    elif event_type in ("done", "EndTurn", "end_turn"):
        results.append({"event": "turn_end", "data": make_turn_end_signal("end_turn")})

    # ---- 错误 ----
    elif event_type in ("error", "Error"):
        message = event_data.get("message", event_data.get("error", "unknown error"))
        results.append({"event": "error", "data": make_error_event(message)})

    # ---- 权限请求 ----
    # daemon 格式: {"type": "permission", ...}
    # 此处先标记，由上层（P1 实现）发起 session/request_permission 请求
    elif event_type in ("permission", "Permission"):
        results.append({"event": "permission", "data": event_data})

    # ---- 未知事件 ----
    else:
        logger.debug("untranslated daemon event: %s %s", event_type, event_data)

    return results


# ============================================================
# 转发代理客户端
# ============================================================

class DaemonProxy:
    """atomcode-daemon HTTP 客户端 — 转发请求并转换 SSE 事件

    维护 ACP session UUID → daemon session ID 的映射，
    确保多轮对话复用同一 daemon session（而非每轮新创建）。
    """

    def __init__(self, base_url: str = "http://127.0.0.1:13456") -> None:
        self.base_url = base_url.rstrip("/")
        self._session: aiohttp.ClientSession | None = None
        # ACP session UUID → daemon session ID 映射
        # daemon 不识别 ACP 的 UUID 格式，每次 chat 都传 UUID 会新建 session
        self._daemon_session_map: dict[str, str] = {}

    async def get_or_create_daemon_session(self, acp_session_id: str) -> str:
        """获取或创建 daemon 端 session，返回 daemon session ID

        首次调用时不预创建 daemon session，第一轮 /chat 让 daemon 自然创建。
        从 done 事件中捕获 daemon 返回的真实 session_id 后存入映射。
        后续同一 acp_session_id 直接返回缓存的 daemon session ID。

        注意：POST /sessions 创建的 session（空 session）与 daemon 的 load()
        路径存在兼容性问题（文件存在但 load() 返回 Err），因此放弃预创建策略。
        改用"第一轮无 sessionId → daemon 创建 → 捕获 done 中的 session_id"方案。
        """
        daemon_sid = self._daemon_session_map.get(acp_session_id)
        if daemon_sid is not None:
            logger.debug("session map HIT: ACP %s → daemon %s", acp_session_id[:8], daemon_sid)
            return daemon_sid

        # 第一次调用：不预创建，标记为 pending，返回 None 表示 No sessionId
        logger.debug("session map MISS for ACP %s: will capture from daemon done event",
                     acp_session_id[:8])
        self._daemon_session_map[acp_session_id] = "__pending__"
        return ""

    def resolve_daemon_session(self, acp_session_id: str, daemon_session_id: str) -> None:
        """从 done 事件中捕获 daemon 真实 session_id，更新映射"""
        if self._daemon_session_map.get(acp_session_id) == "__pending__":
            self._daemon_session_map[acp_session_id] = daemon_session_id
            logger.info("resolved daemon session: ACP %s → daemon %s",
                        acp_session_id[:8], daemon_session_id)

    def remove_session(self, acp_session_id: str) -> None:
        """清理 ACP session 映射"""
        self._daemon_session_map.pop(acp_session_id, None)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # force_close=True 防止连接复用导致 SSE 流中途断开
            connector = aiohttp.TCPConnector(force_close=True)
            self._session = aiohttp.ClientSession(
                base_url=self.base_url,
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=300, connect=10, sock_read=120),
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ---- 健康检查 ----

    async def health(self) -> dict[str, Any]:
        """GET /health"""
        session = await self._get_session()
        async with session.get("/health") as resp:
            return await resp.json()

    # ---- 会话管理 ----

    async def list_sessions(self) -> list[dict[str, Any]]:
        """GET /sessions"""
        session = await self._get_session()
        async with session.get("/sessions") as resp:
            return await resp.json()

    async def create_session(self) -> dict[str, Any]:
        """POST /sessions"""
        session = await self._get_session()
        async with session.post("/sessions") as resp:
            return await resp.json()

    async def delete_session(self, project_hash: str, session_id: str) -> dict[str, Any]:
        """DELETE /projects/:hash/sessions/:id"""
        session = await self._get_session()
        async with session.delete(f"/projects/{project_hash}/sessions/{session_id}") as resp:
            return await resp.json()

    # ---- 模型 ----

    async def list_models(self) -> list[dict[str, Any]]:
        """GET /models"""
        session = await self._get_session()
        async with session.get("/models") as resp:
            return await resp.json()

    # ---- Chat（核心） ----

    async def chat_stream(
        self,
        message: str,
        session_id: str | None = None,
        model: str | None = None,
        working_dir: str | None = None,
    ) -> AsyncIterator[dict]:
        """POST /chat — 发送消息，yield 转换后的 ACP 事件

        session_id 是 ACP session UUID；若已映射到 daemon session，
        则传 daemon 的 session ID 以保证多轮对话上下文连续性。

        working_dir: 项目工作目录（绝对路径）。daemon 用它算 project_hash
        并定位 session 文件。必须与创建该 session 时的 working_dir 一致，
        否则 daemon 的 SessionManager::load() 会去错误的 hash 目录查找。
        若不传，daemon 回退到 std::env::current_dir()。
        """
        # 使用 daemon session ID（若有映射），否则回退到 ACP UUID
        # __pending__ 表示第一次调用，不传 sessionId 让 daemon 自然创建
        daemon_session_id = None
        if session_id:
            mapped = self._daemon_session_map.get(session_id)
            if mapped and mapped != "__pending__":
                daemon_session_id = mapped
        effective_session = daemon_session_id or None  # None = 不传 sessionId

        if session_id and daemon_session_id:
            logger.debug("chat_stream: ACP %s → daemon session %s", session_id[:8], daemon_session_id)
        elif session_id:
            logger.debug("chat_stream: no daemon session for ACP %s yet, no sessionId sent", session_id[:8])

        session = await self._get_session()
        # 注意：daemon ChatRequest 字段是 snake_case（session_id / working_dir），
        # serde 默认忽略未知字段（无 deny_unknown_fields），所以传 camelCase
        # 的 sessionId 会被静默丢弃 → daemon 每次新建 session → 无多轮上下文。
        payload: dict[str, Any] = {"message": message}
        if effective_session:
            payload["session_id"] = effective_session
        if working_dir:
            payload["working_dir"] = working_dir
        if model:
            payload["model"] = model

        async with session.post("/chat", json=payload) as resp:
            if resp.status != 200:
                body = await resp.text()
                yield {"event": "error", "data": make_error_event(
                    f"daemon returned {resp.status}: {body[:200]}"
                )}
                yield {"event": "turn_end", "data": make_turn_end_signal("cancelled")}
                return

            # 解析 SSE 流
            # atomcode-daemon 的 SSE 格式：
            #   : ping                          ← SSE 注释（keep-alive）
            #   data: {"type":"text",...}       ← 事件数据，每行一条
            #   data: {"type":"done",...}       ← 结束事件
            #   : bye                           ← SSE 注释
            # 注意：没有 event: 行，类型在 JSON 的 type 字段中
            while True:
                line_bytes = await resp.content.readline()
                if not line_bytes:
                    break  # EOF
                line = line_bytes.decode("utf-8", errors="replace").rstrip("\n\r")
                if not line or line.startswith(":"):
                    # 空行或 SSE 注释（: ping / : bye），跳过
                    continue
                if not line.startswith("data:"):
                    # 非标准行，跳过
                    continue

                event_data_str = line[5:].strip()
                if not event_data_str:
                    continue

                # 解析 data JSON
                try:
                    event_data = json.loads(event_data_str)
                except json.JSONDecodeError:
                    logger.warning("non-JSON SSE data: %s", event_data_str[:100])
                    continue

                # daemon 的 SSE data 中 type 字段标识事件类型
                # 实际格式: { "type": "text", "content": "..." }
                #           { "type": "tokens", "prompt": N, ... }
                #           { "type": "done", "tokens": N, "session_id": "..." }
                effective_type = event_data.pop("type", None) or "unknown"

                # 从 done 事件捕获 daemon 真实 session_id，更新映射
                if effective_type in ("done", "EndTurn", "end_turn"):
                    done_sid = event_data.get("session_id", "")
                    if done_sid and session_id:
                        self.resolve_daemon_session(session_id, done_sid)

                # 转换为 ACP 事件
                for acp_event in translate_daemon_event(effective_type, event_data):
                    yield acp_event

                # daemon 的 "done" 事件表示 turn 结束，主动跳出循环
                if effective_type in ("done", "EndTurn", "end_turn"):
                    break

    # ---- 停止 ----

    async def chat_stop(self) -> None:
        """POST /chat/stop"""
        session = await self._get_session()
        async with session.post("/chat/stop") as resp:
            pass  # 无需处理响应

    # ---- 配置 ----

    async def get_config(self) -> dict[str, Any]:
        """GET /config"""
        session = await self._get_session()
        async with session.get("/config") as resp:
            return await resp.json()

    async def reload_config(self) -> None:
        """POST /config/reload"""
        session = await self._get_session()
        async with session.post("/config/reload") as resp:
            pass


# ============================================================
# 供 server.py 调用的高层接口
# ============================================================

async def run_proxy_agent(
    proxy: DaemonProxy,
    session_id: str,
    message: str,
    cancel_event: asyncio.Event,
    model: str | None = None,
    working_dir: str | None = None,
) -> AsyncIterator[dict]:
    """转发模式 Agent 执行 — 连接 atomcode-daemon 并转换 SSE 事件

    签名与 run_mock_agent 一致，方便在 server.py 中切换。
    working_dir: 透传给 daemon 的 /chat，用于算 project_hash 定位 session 文件。
    """
    # 启动一个后台任务来监听取消事件
    async def watch_cancel():
        while not cancel_event.is_set():
            await asyncio.sleep(0.1)
        await proxy.chat_stop()

    cancel_task = asyncio.create_task(watch_cancel())

    try:
        async for event in proxy.chat_stream(message, session_id, model, working_dir=working_dir):
            if cancel_event.is_set():
                yield {"event": "turn_end", "data": make_turn_end_signal("cancelled")}
                return
            yield event
    except aiohttp.ClientError as e:
        yield {"event": "error", "data": make_error_event(f"daemon connection error: {e}")}
        yield {"event": "turn_end", "data": make_turn_end_signal("cancelled")}
    finally:
        cancel_task.cancel()
        try:
            await cancel_task
        except asyncio.CancelledError:
            pass
