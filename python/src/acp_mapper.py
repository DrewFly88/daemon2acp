"""ACP 类型映射器 — HTTP 请求/响应 ↔ ACP JSON-RPC 类型的双向转换"""

from __future__ import annotations

from typing import Any

from .agent_state import AgentState
from .session_manager import Session


# ============================================================
# JSON-RPC 2.0 消息类型
# ============================================================

def make_jsonrpc_response(req_id: Any, result: Any) -> dict[str, Any]:
    """构建成功的 JSON-RPC 响应"""
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def make_jsonrpc_error(req_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    """构建 JSON-RPC 错误响应"""
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


# ============================================================
# ACP initialize
# ============================================================

def build_initialize_response(state: AgentState) -> dict[str, Any]:
    return state.build_initialize_response()


# ============================================================
# Session 类型转换
# 对照 acp SDK schema：
#   NewSessionResponse: 顶层 sessionId（必填）+ configOptions/models/modes（可选）
#   LoadSessionResponse: configOptions/models/modes（可选，无 session 字段）
#   ListSessionsResponse: sessions: [SessionInfo] + nextCursor
#   SessionInfo: sessionId + cwd（必填）+ title/updatedAt（可选）
# ============================================================

def session_to_acp(session: Session) -> dict[str, Any]:
    """内部 Session → ACP SessionInfo

    用于 ListSessionsResponse.sessions 和 session/load 的 session_info_update。
    """
    return session.to_info_dict()


def session_to_new_session_response(session: Session) -> dict[str, Any]:
    """内部 Session → ACP NewSessionResponse

    对照 acp.schema.NewSessionResponse：
        顶层 sessionId（必填），可选 configOptions/models/modes
    """
    return {
        "sessionId": session.id,
    }


def session_to_load_session_response(session: Session) -> dict[str, Any]:
    """内部 Session → ACP LoadSessionResponse

    对照 acp.schema.LoadSessionResponse：
        可选 configOptions/models/modes（本项目都不支持，返回空对象）
    """
    return {}


def sessions_to_acp_list(sessions: list[Session]) -> dict[str, Any]:
    """内部 Session 列表 → ACP ListSessionsResponse"""
    return {
        "sessions": [session_to_acp(s) for s in sessions],
        # 简化版不做分页
        "nextCursor": None,
    }


# ============================================================
# Prompt 参数转换
# ============================================================

def chat_request_to_acp_prompt_params(session_id: str, message: str) -> dict[str, Any]:
    """将 HTTP chat 请求转换为 ACP session/prompt 参数"""
    return {
        "sessionId": session_id,
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": message}],
            }
        ],
    }


# ============================================================
# ACP session/update notification 的 update payload
#
# 结构对照 ACP SDK schema.py（acp v1）：
#   每种 update 是带 `sessionUpdate` 类型字段的 chunk，不是裸 contentBlock。
#
#   {"sessionUpdate": "<type>", ...payload}
#
# 主要类型：
#   agent_message_chunk  — 智能体文本（流式）
#   agent_thought_chunk  — 内部推理
#   tool_call            — 工具调用开始（ToolCallStart）
#   tool_call_update     — 工具调用进度/完成（ToolCallProgress）
#   plan                 — 计划更新
#   current_mode_update  — 当前模式
#   available_commands_update — 可用命令
#   usage_update         — 用量统计
# ============================================================

def make_text_chunk(text: str) -> dict[str, Any]:
    """ACP agent_message_chunk — 智能体文本增量

    对应 acp.schema.AgentMessageChunk
    """
    return {
        "sessionUpdate": "agent_message_chunk",
        "content": {"type": "text", "text": text},
    }


def make_thought_chunk(text: str) -> dict[str, Any]:
    """ACP agent_thought_chunk — 推理增量

    对应 acp.schema.AgentThoughtChunk
    """
    return {
        "sessionUpdate": "agent_thought_chunk",
        "content": {"type": "text", "text": text},
    }


def make_tool_call_start(
    tool_call_id: str,
    title: str,
    kind: str = "other",
    raw_input: Any = None,
) -> dict[str, Any]:
    """ACP tool_call — 工具调用开始

    对应 acp.schema.ToolCallStart（继承 ToolCall）
    必填字段：toolCallId, title, kind, status, content, locations, rawInput
    """
    return {
        "sessionUpdate": "tool_call",
        "toolCallId": tool_call_id,
        "title": title,
        "kind": kind,
        "status": "pending",
        "content": [],
        "locations": [],
        "rawInput": raw_input if raw_input is not None else {},
    }


def make_tool_call_update(
    tool_call_id: str,
    content: Any,
    *,
    status: str = "completed",
    is_error: bool = False,
) -> dict[str, Any]:
    """ACP tool_call_update — 工具调用进度/完成

    对应 acp.schema.ToolCallProgress（继承 ToolCallUpdate）
    content 应为 contentBlock 列表；若传入字符串则自动包装为单个 text block。
    """
    if isinstance(content, str):
        blocks = [{"type": "text", "text": content}]
    elif isinstance(content, list):
        blocks = content
    else:
        blocks = [{"type": "text", "text": str(content)}]

    payload: dict[str, Any] = {
        "sessionUpdate": "tool_call_update",
        "toolCallId": tool_call_id,
        "status": "error" if is_error else status,
        "content": blocks,
    }
    return payload


def make_error_event(message: str, code: str = "internal_error") -> dict[str, Any]:
    """ACP error — 用于错误通知（非 sessionUpdate 类型，独立 error 事件）

    注意：ACP 协议中错误通常通过 session/prompt 的 error 响应或专用 error 通知传递，
    此处保留为内部错误信号，由上层决定如何包装。
    """
    return {"type": "error", "code": code, "message": message}


# ============================================================
# turn_end 不是 session/update 通知
# ACP 协议中 session/prompt 是请求-响应：
#   流式阶段 → session/update 通知（chunk）
#   结束 → session/prompt 响应 result: {"stopReason": "...", "tokenUsage": {...}}
# 此函数仅作为内部信号，由上层 consume 后转为响应，不应作为 update 发送。
# ============================================================

def make_turn_end_signal(stop_reason: str) -> dict[str, Any]:
    """turn 结束信号（内部用，不作为 session/update 发送）

    上层应将其 stopReason 作为 session/prompt 的响应 result 返回。
    """
    return {"type": "turn_end", "stopReason": stop_reason}


# ============================================================
# 向后兼容别名（已废弃，保留过渡期）
# ============================================================

def make_text_event(content: str) -> dict[str, Any]:
    """Deprecated: use make_text_chunk"""
    return make_text_chunk(content)


def make_reasoning_event(content: str) -> dict[str, Any]:
    """Deprecated: use make_thought_chunk"""
    return make_thought_chunk(content)


def make_tool_use_event(call_id: str, name: str, input: Any) -> dict[str, Any]:
    """Deprecated: use make_tool_call_start"""
    return make_tool_call_start(call_id, name, "other", input)


def make_tool_result_event(call_id: str, content: Any, is_error: bool = False) -> dict[str, Any]:
    """Deprecated: use make_tool_call_update"""
    return make_tool_call_update(call_id, content, is_error=is_error)


def make_turn_end_event(stop_reason: str) -> dict[str, Any]:
    """Deprecated: use make_turn_end_signal"""
    return make_turn_end_signal(stop_reason)
