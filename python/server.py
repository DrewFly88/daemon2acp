"""daemon2acp HTTP 服务 — aiohttp 实现

提供两种交互方式：
1. REST/SSE 端点（兼容现有 atomcode-daemon HTTP API 风格）
2. ACP JSON-RPC 2.0 端点（/acp，供 ACP 兼容客户端直接调用）
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from aiohttp import web, WSMsgType

from src.agent_state import AgentState
from src.session_manager import SessionManager, SessionMode
from src.acp_mapper import (
    build_initialize_response,
    chat_request_to_acp_prompt_params,
    make_jsonrpc_error,
    make_jsonrpc_response,
    session_to_acp,
    session_to_new_session_response,
    session_to_load_session_response,
    sessions_to_acp_list,
)
from src.mock_agent import run_mock_agent
from src.proxy_agent import DaemonProxy, run_proxy_agent
from src.daemon_launcher import DaemonLauncher


logger = logging.getLogger("daemon2acp")

# 运行模式：mock（模拟）或 proxy（转发到 atomcode-daemon）
RUN_MODE = os.environ.get("DAEMON2ACP_MODE", "mock")  # "mock" | "proxy"
DAEMON_URL = os.environ.get("DAEMON2ACP_DAEMON_URL", "http://127.0.0.1:13456")
# proxy 模式下是否自动拉起 atomcode daemon 子进程
DAEMON_AUTO_START = os.environ.get("DAEMON2ACP_AUTO_START", "1") not in ("0", "false", "no")
# daemon 子进程监听地址（默认与 daemon2acp 不同端口，避免冲突）
DAEMON_HOST = os.environ.get("DAEMON2ACP_DAEMON_HOST", "127.0.0.1")
DAEMON_PORT = int(os.environ.get("DAEMON2ACP_DAEMON_PORT", "13457"))


# ============================================================
# 应用状态
# ============================================================

class AppState:
    """应用全局状态"""

    def __init__(self) -> None:
        self.agent = AgentState()
        self.session_manager = SessionManager()
        # 转发代理（仅 proxy 模式使用）
        self.proxy: DaemonProxy | None = None
        # daemon 子进程管理器（仅 proxy + auto-start 模式使用）
        self.launcher: DaemonLauncher | None = None

        if RUN_MODE == "proxy":
            self.proxy = DaemonProxy(DAEMON_URL)
            if DAEMON_AUTO_START:
                self.launcher = DaemonLauncher(
                    host=DAEMON_HOST,
                    port=DAEMON_PORT,
                )
                logger.info(
                    "proxy mode: will auto-start atomcode daemon at %s:%s",
                    DAEMON_HOST, DAEMON_PORT,
                )
            else:
                logger.info(
                    "proxy mode: connecting to existing atomcode-daemon at %s",
                    DAEMON_URL,
                )
        else:
            logger.info("mock mode: using simulated agent responses")


# ============================================================
# 中间件
# ============================================================

async def cors_middleware(request: web.Request, handler) -> web.StreamResponse:
    """CORS 中间件 — 允许本地前端访问

    aiohttp 3.x 中间件签名: (request, handler) -> response
    必须用 @web.middleware 装饰以告知框架这是新式中间件
    """
    if request.method == "OPTIONS":
        response = web.Response(status=204)
    else:
        response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return response

# 用装饰器标记为新式中间件
cors_middleware = web.middleware(cors_middleware)


# ============================================================
# REST / SSE 端点
# ============================================================

async def health_handler(request: web.Request) -> web.Response:
    """GET /health — 健康检查"""
    state: AppState = request.app["state"]
    mode = "proxy" if state.proxy else "mock"
    result = {
        "status": "ok",
        "service": "daemon2acp",
        "version": state.agent.agent_info.version,
        "protocolVersion": "1",
        "mode": mode,
    }
    # proxy 模式下检查 daemon 连通性
    if state.proxy is not None:
        try:
            daemon_health = await state.proxy.health()
            result["daemon"] = daemon_health
        except Exception as e:
            result["daemon"] = {"status": "unreachable", "error": str(e)}
    return web.json_response(result)


async def chat_handler(request: web.Request) -> web.StreamResponse:
    """POST /chat — 发送消息，SSE 流式返回

    Body: {"sessionId": "...", "message": "..."}
    """
    state: AppState = request.app["state"]
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response(
            {"error": "invalid JSON body"}, status=400,
        )

    message = body.get("message", "").strip()
    if not message:
        return web.json_response(
            {"error": "message is required"}, status=400,
        )

    # 获取或创建会话
    session_id = body.get("sessionId")
    session = None
    if session_id:
        session = await state.session_manager.get(session_id)
    if session is None:
        session = await state.session_manager.create()
        session_id = session.id

    logger.info("chat: session=%s, message=%.50s", session_id, message)

    # 创建取消事件并标记运行中
    cancel_event = asyncio.Event()
    await state.session_manager.set_running(session_id, cancel_event)

    # 记录用户消息
    await state.session_manager.append_message(session_id, {
        "role": "user",
        "content": [{"type": "text", "text": message}],
    })

    # 构建 SSE 流响应
    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
    await response.prepare(request)

    try:
        # 根据运行模式选择 agent 执行器
        if state.proxy is not None:
            agent_stream = run_proxy_agent(
                state.proxy, session_id, message, cancel_event,
                model=body.get("model"),
                working_dir=session.cwd or None,
            )
        else:
            agent_stream = run_mock_agent(session_id, message, cancel_event)

        async for evt in agent_stream:
            event_type = evt["event"]
            data = evt["data"]
            sse_data = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
            await response.write(sse_data.encode("utf-8"))

            # 终止事件触发消息记录
            if event_type == "turn_end":
                mode_label = "proxy" if state.proxy else "mock"
                await state.session_manager.append_message(session_id, {
                    "role": "assistant",
                    "content": [{"type": "text", "text": f"[{mode_label} response]"}],
                    "stopReason": data.get("stopReason"),
                })
    except asyncio.CancelledError:
        logger.info("chat stream cancelled: session=%s", session_id)
    finally:
        await state.session_manager.clear_running(session_id)

    await response.write_eof()
    return response


async def list_sessions_handler(request: web.Request) -> web.Response:
    """GET /sessions — 列出所有会话"""
    state: AppState = request.app["state"]
    sessions = await state.session_manager.list()
    return web.json_response(sessions_to_acp_list(sessions))


async def get_session_handler(request: web.Request) -> web.Response:
    """GET /sessions/{id} — 获取单个会话"""
    state: AppState = request.app["state"]
    session_id = request.match_info["id"]
    session = await state.session_manager.get(session_id)
    if session is None:
        return web.json_response(
            {"error": "session not found"}, status=404,
        )
    return web.json_response({"session": session_to_acp(session)})


async def delete_session_handler(request: web.Request) -> web.Response:
    """DELETE /sessions/{id} — 删除会话"""
    state: AppState = request.app["state"]
    session_id = request.match_info["id"]

    # 先取消正在执行的任务
    await state.session_manager.request_cancel(session_id)

    deleted = await state.session_manager.delete(session_id)
    if not deleted:
        return web.json_response(
            {"error": "session not found"}, status=404,
        )
    return web.json_response({"deleted": True, "id": session_id})


async def cancel_session_handler(request: web.Request) -> web.Response:
    """POST /sessions/{id}/cancel — 取消正在执行的 prompt"""
    state: AppState = request.app["state"]
    session_id = request.match_info["id"]
    cancelled = await state.session_manager.request_cancel(session_id)
    if not cancelled:
        return web.json_response(
            {"error": "session not found or not running"}, status=404,
        )
    return web.json_response({"cancelled": True, "id": session_id})


# ============================================================
# ACP JSON-RPC 端点
# ============================================================

async def acp_rpc_handler(request: web.Request) -> web.Response:
    """POST /acp — 接受 ACP JSON-RPC 2.0 消息

    支持请求-响应和通知两种模式。
    """
    state: AppState = request.app["state"]
    try:
        msg = await request.json()
    except json.JSONDecodeError:
        return web.json_response(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
            status=400,
        )

    # JSON-RPC 消息可以是单条或批量
    if isinstance(msg, list):
        responses = []
        for m in msg:
            resp = await _handle_single_acp_message(state, m)
            if resp is not None:
                responses.append(resp)
        if not responses:
            # 全是通知，无需响应
            return web.Response(status=204)
        return web.json_response(responses)

    resp = await _handle_single_acp_message(state, msg)
    if resp is None:
        # 通知，无需响应
        return web.Response(status=204)
    return web.json_response(resp)


async def _handle_single_acp_message(state: AppState, msg: dict[str, Any]) -> dict[str, Any] | None:
    """处理单条 JSON-RPC 消息，返回响应 dict（通知则返回 None）"""
    msg_type = _detect_message_type(msg)

    if msg_type == "request":
        return await _handle_acp_request(state, msg)
    elif msg_type == "notification":
        await _handle_acp_notification(state, msg)
        return None
    elif msg_type == "response":
        # Agent 通常不接收响应（除非作为 Client 角色），忽略
        return None
    return make_jsonrpc_error(msg.get("id"), -32600, "Invalid Request")


def _detect_message_type(msg: dict[str, Any]) -> str:
    """根据 JSON-RPC 字段判断消息类型"""
    has_id = "id" in msg
    has_method = "method" in msg
    has_result = "result" in msg
    has_error = "error" in msg

    if has_method and has_id:
        return "request"
    elif has_method and not has_id:
        return "notification"
    elif has_id and (has_result or has_error):
        return "response"
    return "invalid"


async def _handle_acp_request(state: AppState, req: dict[str, Any]) -> dict[str, Any]:
    """处理 ACP 请求 — 返回 JSON-RPC 响应"""
    req_id = req.get("id")
    method = req.get("method", "")
    params = req.get("params") or {}

    try:
        if method == "initialize":
            return make_jsonrpc_response(req_id, build_initialize_response(state.agent))

        elif method == "session/new":
            title = params.get("title")
            cwd = params.get("cwd", "")
            session = await state.session_manager.create(title, cwd=cwd)
            # 在 daemon 端创建 session 映射
            if state.proxy is not None and session.id:
                try:
                    await state.proxy.get_or_create_daemon_session(session.id)
                except Exception as e:
                    logger.warning("failed to create daemon session: %s", e)
            return make_jsonrpc_response(req_id, session_to_new_session_response(session))

        elif method == "session/list":
            sessions = await state.session_manager.list()
            return make_jsonrpc_response(req_id, sessions_to_acp_list(sessions))

        elif method == "session/load":
            session_id = params.get("sessionId", "")
            session = await state.session_manager.get(session_id)
            if session is None:
                return make_jsonrpc_error(req_id, -32000, f"session not found: {session_id}")
            return make_jsonrpc_response(req_id, session_to_load_session_response(session))

        elif method == "session/delete":
            session_id = params.get("sessionId", "")
            await state.session_manager.request_cancel(session_id)
            deleted = await state.session_manager.delete(session_id)
            if state.proxy is not None:
                state.proxy.remove_session(session_id)
            return make_jsonrpc_response(req_id, {"deleted": deleted})

        elif method == "session/close":
            session_id = params.get("sessionId", "")
            await state.session_manager.request_cancel(session_id)
            await state.session_manager.clear_running(session_id)
            if state.proxy is not None:
                state.proxy.remove_session(session_id)
            return make_jsonrpc_response(req_id, {})

        elif method == "session/set_mode":
            session_id = params.get("sessionId", "")
            mode_str = params.get("mode", "normal")
            try:
                mode = SessionMode(mode_str)
            except ValueError:
                return make_jsonrpc_error(req_id, -32602, f"invalid mode: {mode_str}")
            await state.session_manager.set_mode(session_id, mode)
            return make_jsonrpc_response(req_id, {})

        elif method == "session/prompt":
            # 同步处理简化版 — 真实实现应返回流式响应
            # ACP 中 session/prompt 是请求-响应，期间通过 session/update 通知推送
            # 在 HTTP 单次请求模式下，我们直接返回最终结果（不做流式）
            # 流式版本需要 WebSocket 或 Streamable HTTP transport
            session_id = params.get("sessionId", "")
            # ACP SDK 用 `prompt` 字段（List[ContentBlock]），兼容旧 `messages` 格式
            prompt_blocks = params.get("prompt")
            if prompt_blocks is None:
                messages = params.get("messages", [])
                if messages:
                    prompt_blocks = messages[0].get("content", [])
            if prompt_blocks is None:
                prompt_blocks = []

            user_msg = ""
            for block in prompt_blocks:
                if isinstance(block, dict) and block.get("type") == "text":
                    user_msg += block.get("text", "")

            cancel_event = asyncio.Event()
            await state.session_manager.set_running(session_id, cancel_event)

            # 取 session cwd 透传给 daemon，确保 project_hash 一致
            sess_obj = await state.session_manager.get(session_id)
            sess_cwd = sess_obj.cwd if sess_obj else None

            # 根据运行模式选择 agent 执行器
            if state.proxy is not None:
                agent_stream = run_proxy_agent(
                    state.proxy, session_id, user_msg, cancel_event,
                    working_dir=sess_cwd,
                )
            else:
                agent_stream = run_mock_agent(session_id, user_msg, cancel_event)

            final_stop_reason = "end_turn"
            final_error: dict[str, Any] | None = None
            try:
                async for evt in agent_stream:
                    if evt["event"] == "turn_end":
                        final_stop_reason = evt["data"].get("stopReason", "end_turn")
                    elif evt["event"] == "error":
                        final_error = evt["data"]
                    # HTTP 单次请求模式不做流式 update 推送
            finally:
                await state.session_manager.clear_running(session_id)

            if final_error is not None:
                return make_jsonrpc_error(
                    req_id, -32000,
                    final_error.get("message", "agent error"),
                    data={"code": final_error.get("code", "internal_error")},
                )

            return make_jsonrpc_response(req_id, {
                "stopReason": final_stop_reason,
                "tokenUsage": {"input": 0, "output": 0},
            })

        else:
            return make_jsonrpc_error(req_id, -32601, f"method not found: {method}")

    except KeyError as e:
        return make_jsonrpc_error(req_id, -32000, str(e))
    except Exception as e:
        logger.exception("acp request failed: %s", method)
        return make_jsonrpc_error(req_id, -32603, f"internal error: {e}")


async def _handle_acp_notification(state: AppState, notif: dict[str, Any]) -> None:
    """处理 ACP 通知（无响应）"""
    method = notif.get("method", "")
    params = notif.get("params") or {}

    if method == "session/cancel":
        session_id = params.get("sessionId", "")
        logger.info("session/cancel: %s", session_id)
        await state.session_manager.request_cancel(session_id)
    else:
        logger.warning("unhandled ACP notification: %s", method)


# ============================================================
# 应用构建与入口
# ============================================================

async def _on_startup(app: web.Application) -> None:
    """应用启动钩子 — 自动拉起 atomcode daemon 子进程"""
    state: AppState = app["state"]
    if state.launcher is not None:
        logger.info("auto-starting atomcode daemon...")
        try:
            await state.launcher.start(timeout=15.0)
            # 更新 proxy 的 base_url 指向实际 daemon 地址
            if state.proxy is not None:
                state.proxy.base_url = state.launcher.base_url
            logger.info("atomcode daemon started successfully")
        except Exception as e:
            logger.error("failed to start atomcode daemon: %s", e)
            logger.info("falling back to mock mode for this session")
            state.proxy = None
            state.launcher = None


async def _on_cleanup(app: web.Application) -> None:
    """应用关闭钩子 — 优雅停止 atomcode daemon 子进程"""
    state: AppState = app["state"]
    if state.launcher is not None:
        logger.info("stopping atomcode daemon ...")
        state.launcher.stop_sync()
    if state.proxy is not None:
        await state.proxy.close()


def create_app() -> web.Application:
    """构建 aiohttp 应用"""
    state = AppState()
    app = web.Application()
    app["state"] = state

    # 生命周期钩子（仅清理；启动在 main() 中用 start_sync 完成）
    app.on_cleanup.append(_on_cleanup)

    # REST / SSE 路由
    app.router.add_get("/health", health_handler)
    app.router.add_post("/chat", chat_handler)
    app.router.add_get("/sessions", list_sessions_handler)
    app.router.add_get("/sessions/{id}", get_session_handler)
    app.router.add_delete("/sessions/{id}", delete_session_handler)
    app.router.add_post("/sessions/{id}/cancel", cancel_session_handler)

    # ACP JSON-RPC 端点
    app.router.add_post("/acp", acp_rpc_handler)

    # CORS 中间件
    app.middlewares.append(cors_middleware)

    return app


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "13456"))

    mode_label = RUN_MODE
    if RUN_MODE == "proxy" and DAEMON_AUTO_START:
        mode_label = f"proxy (auto-start daemon on {DAEMON_HOST}:{DAEMON_PORT})"

    app = create_app()
    state: AppState = app["state"]

    # 在事件循环之前同步启动 daemon（避免 asyncio 子进程生命周期冲突）
    if state.launcher is not None:
        try:
            state.launcher.start_sync(timeout=15.0)
            if state.proxy is not None:
                state.proxy.base_url = state.launcher.base_url
            logger.info("atomcode daemon started successfully")
        except Exception as e:
            logger.error("failed to start atomcode daemon: %s", e)
            logger.info("falling back to mock mode")
            state.proxy = None
            state.launcher = None

    logger.info("daemon2acp starting on http://%s:%s  mode=%s", host, port, mode_label)
    web.run_app(app, host=host, port=port, print=None)


if __name__ == "__main__":
    main()
