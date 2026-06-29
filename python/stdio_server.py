"""daemon2acp stdio 传输层 — ACP 客户端标准接入方式

ACP 客户端（Zed、VS Code 等）将 Agent 作为子进程启动，
通过 stdin/stdout 交换 JSON-RPC 2.0 消息（每行一条），
stderr 用于日志输出。

用法：
    # 直接运行（供 ACP 客户端启动）
    python stdio_server.py

    # 指定运行模式
    set DAEMON2ACP_MODE=proxy
    python stdio_server.py

协议流程：
    1. Client → Agent: initialize 请求（via stdin）
    2. Agent → Client: initialize 响应（via stdout）
    3. Client → Agent: session/new 请求
    4. Agent → Client: session/new 响应
    5. Client → Agent: session/prompt 请求
    6. Agent → Client: session/update 通知（流式，多条）
    7. Agent → Client: session/prompt 响应（最终结果）
    8. ... 更多 prompt 或 session/close
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Any

from src.agent_state import AgentState
from src.session_manager import SessionManager, SessionMode
from src.acp_mapper import (
    build_initialize_response,
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

logger = logging.getLogger("daemon2acp.stdio")


class AppState:
    """应用全局状态（对齐 server.py）"""

    def __init__(self) -> None:
        self.agent = AgentState()
        self.session_manager = SessionManager()
        # 转发代理（仅 proxy 模式使用）
        self.proxy: DaemonProxy | None = None

# 运行模式配置（与 server.py 一致）
RUN_MODE = os.environ.get("DAEMON2ACP_MODE", "mock")
DAEMON_URL = os.environ.get("DAEMON2ACP_DAEMON_URL", "http://127.0.0.1:13457")
DAEMON_AUTO_START = os.environ.get("DAEMON2ACP_AUTO_START", "1") not in ("0", "false", "no")
DAEMON_HOST = os.environ.get("DAEMON2ACP_DAEMON_HOST", "127.0.0.1")
DAEMON_PORT = int(os.environ.get("DAEMON2ACP_DAEMON_PORT", "13457"))


# ============================================================
# JSON-RPC 消息读写
# ============================================================

async def read_message(reader: asyncio.StreamReader) -> dict[str, Any] | None:
    """从 stdin 读取一行 JSON-RPC 消息"""
    line = await reader.readline()
    if not line:
        return None  # EOF
    text = line.decode("utf-8").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning("invalid JSON from stdin: %s (%s)", text[:100], e)
        return None


def write_message(writer: asyncio.StreamWriter, msg: dict[str, Any]) -> None:
    """向 stdout 写入一行 JSON-RPC 消息"""
    line = json.dumps(msg, ensure_ascii=False, separators=(",", ":"))
    writer.write((line + "\n").encode("utf-8"))
    writer.flush()


# ============================================================
# ACP 方法处理（与 server.py 逻辑一致，但输出方式不同）
# ============================================================

class StdioAgent:
    """stdio 传输层的 ACP Agent"""

    def __init__(self) -> None:
        # AppState 结构对齐 server.py：agent=AgentState() + session_manager
        self.state = AppState()
        self.launcher: DaemonLauncher | None = None
        self._initialized = False
        # server→client 请求的 pending future（如 session/request_permission）
        self._pending_requests: dict[Any, asyncio.Future] = {}
        self._next_request_id = 1000  # server 发出的请求 id 从 1000 起

        if RUN_MODE == "proxy":
            self.state.proxy = DaemonProxy(DAEMON_URL)
            if DAEMON_AUTO_START:
                self.launcher = DaemonLauncher(host=DAEMON_HOST, port=DAEMON_PORT)

    async def start_daemon(self) -> None:
        """启动 atomcode daemon 子进程（如果配置了自动拉起）

        用同步 start_sync() — daemon_launcher 已重构为 Popen + 端口健康检查，
        async start() 已删除（atomcode daemon fork 行为导致 asyncio 子进程管理失效）。
        """
        if self.launcher is not None:
            try:
                self.launcher.start_sync(timeout=15.0)
                if self.state.proxy is not None:
                    self.state.proxy.base_url = self.launcher.base_url
            except Exception as e:
                logger.error("failed to start daemon: %s, falling back to mock", e)
                self.state.proxy = None
                self.launcher = None

    async def stop_daemon(self) -> None:
        if self.launcher is not None:
            self.launcher.stop_sync()
        if self.state.proxy is not None:
            await self.state.proxy.close()

    async def handle_message(
        self,
        msg: dict[str, Any],
        writer: asyncio.StreamWriter,
    ) -> None:
        """处理一条 JSON-RPC 消息"""
        has_id = "id" in msg
        has_method = "method" in msg

        if has_method and has_id:
            # 请求 — 需要响应
            response = await self._handle_request(msg)
            write_message(writer, response)
        elif has_method:
            # 通知 — 无响应
            await self._handle_notification(msg)
        elif has_id:
            # 响应消息（Client → Agent 的响应，如 request_permission 的回复）
            # resolve pending future
            fut = self._pending_requests.pop(msg.get("id"), None)
            if fut is not None and not fut.done():
                if "error" in msg:
                    fut.set_exception(RuntimeError(
                        "client error: %s" % msg["error"].get("message", "unknown")
                    ))
                else:
                    fut.set_result(msg.get("result"))
            else:
                logger.warning("orphan response from client: id=%s", msg.get("id"))

    async def _handle_request(self, req: dict[str, Any]) -> dict[str, Any]:
        """处理 ACP 请求，返回 JSON-RPC 响应"""
        req_id = req.get("id")
        method = req.get("method", "")
        params = req.get("params") or {}

        try:
            # ---- initialize ----
            if method == "initialize":
                self._initialized = True
                return make_jsonrpc_response(
                    req_id, build_initialize_response(self.state.agent)
                )

            if not self._initialized:
                return make_jsonrpc_error(
                    req_id, -32002, "Server not initialized. Call 'initialize' first."
                )

            # ---- session/new ----
            # ACP NewSessionRequest 必填 cwd + mcpServers；取 cwd 存入 Session
            if method == "session/new":
                title = params.get("title")
                cwd = params.get("cwd", "")
                session = await self.state.session_manager.create(title, cwd=cwd)
                # 在 daemon 端也创建 session，建立 ACP UUID ↔ daemon session 映射
                # 确保后续 session/prompt 的 chat_stream 用 daemon session ID 传参
                if self.state.proxy is not None and session.id:
                    try:
                        await self.state.proxy.get_or_create_daemon_session(session.id)
                    except Exception as e:
                        logger.warning("failed to create daemon session: %s", e)
                return make_jsonrpc_response(
                    req_id, session_to_new_session_response(session)
                )

            # ---- session/list ----
            elif method == "session/list":
                sessions = await self.state.session_manager.list()
                return make_jsonrpc_response(
                    req_id, sessions_to_acp_list(sessions)
                )

            # ---- session/load ----
            elif method == "session/load":
                session_id = params.get("sessionId", "")
                session = await self.state.session_manager.get(session_id)
                if session is None:
                    return make_jsonrpc_error(req_id, -32000, f"session not found: {session_id}")
                return make_jsonrpc_response(
                    req_id, session_to_load_session_response(session)
                )

            # ---- session/delete ----
            elif method == "session/delete":
                session_id = params.get("sessionId", "")
                await self.state.session_manager.request_cancel(session_id)
                deleted = await self.state.session_manager.delete(session_id)
                # 清理 daemon session 映射
                if self.state.proxy is not None:
                    self.state.proxy.remove_session(session_id)
                return make_jsonrpc_response(req_id, {"deleted": deleted})

            # ---- session/close ----
            elif method == "session/close":
                session_id = params.get("sessionId", "")
                await self.state.session_manager.request_cancel(session_id)
                await self.state.session_manager.clear_running(session_id)
                # 清理 daemon session 映射
                if self.state.proxy is not None:
                    self.state.proxy.remove_session(session_id)
                return make_jsonrpc_response(req_id, {})

            # ---- session/set_mode ----
            elif method == "session/set_mode":
                session_id = params.get("sessionId", "")
                mode_str = params.get("mode", "normal")
                try:
                    mode = SessionMode(mode_str)
                except ValueError:
                    return make_jsonrpc_error(req_id, -32602, f"invalid mode: {mode_str}")
                await self.state.session_manager.set_mode(session_id, mode)
                return make_jsonrpc_response(req_id, {})

            # ---- session/prompt ----
            elif method == "session/prompt":
                return await self._handle_prompt(req_id, params)

            # ---- 未知方法 ----
            else:
                return make_jsonrpc_error(req_id, -32601, f"method not found: {method}")

        except KeyError as e:
            return make_jsonrpc_error(req_id, -32000, str(e))
        except Exception as e:
            logger.exception("request failed: %s", method)
            return make_jsonrpc_error(req_id, -32603, f"internal error: {e}")

    async def _handle_prompt(self, req_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        """处理 session/prompt — 流式发送 session/update 通知，最终返回响应

        ACP PromptRequest 结构（acp.schema）：
            sessionId (必填), prompt: List[ContentBlock] (必填), messageId (可选)
        注意：字段是 `prompt`（ContentBlock 列表），不是 `messages`。
        """
        session_id = params.get("sessionId", "")
        logger.info("_handle_prompt: acp_session_id=%s, proxy=%s, map_size=%d",
                     session_id[:8] if session_id else "(none)",
                     "yes" if self.state.proxy else "no",
                     len(self.state.proxy._daemon_session_map) if self.state.proxy else 0)
        # ACP SDK 用 `prompt` 字段（List[ContentBlock]），兼容旧 `messages` 格式
        prompt_blocks = params.get("prompt")
        if prompt_blocks is None:
            # 兼容旧格式 messages[0].content
            messages = params.get("messages", [])
            if messages:
                prompt_blocks = messages[0].get("content", [])
        if prompt_blocks is None:
            prompt_blocks = []

        user_msg = ""
        for block in prompt_blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                user_msg += block.get("text", "")
        if not user_msg:
            logger.warning("session/prompt received empty prompt: %s", json.dumps(params)[:300])

        cancel_event = asyncio.Event()
        await self.state.session_manager.set_running(session_id, cancel_event)

        # 取 session 的 cwd 透传给 daemon，确保 daemon 用正确 working_dir 算 project_hash
        # 否则 daemon 回退到自己的 cwd，可能算出不同 hash → load() 失败 → 新建 session
        session_obj = await self.state.session_manager.get(session_id)
        working_dir = session_obj.cwd if session_obj else None

        # 选择执行器
        if self.state.proxy is not None:
            agent_stream = run_proxy_agent(
                self.state.proxy, session_id, user_msg, cancel_event,
                working_dir=working_dir,
            )
        else:
            agent_stream = run_mock_agent(session_id, user_msg, cancel_event)

        # 注意：stdio 模式下，session/update 通知通过 stdout 发送
        # 我们需要拿到 writer 引用 — 通过实例变量传递
        writer = self._writer  # type: ignore

        final_stop_reason = "end_turn"
        final_error: dict[str, Any] | None = None
        try:
            async for evt in agent_stream:
                event_type = evt["event"]
                data = evt["data"]

                if event_type == "turn_end":
                    # turn_end 是内部信号，不作为 session/update 发送
                    # stopReason 通过 session/prompt 响应 result 返回
                    final_stop_reason = data.get("stopReason", "end_turn")
                elif event_type == "error":
                    # error 不作为 session/update 发送，通过响应 error 返回
                    final_error = data
                elif event_type == "permission":
                    # daemon 发起权限请求 → 向 client 发 session/request_permission
                    # 等待 client 响应（用户选择 option），结果记录到日志
                    # daemon 流的实际回传需要 daemon 协议支持，此处先完成 ACP 端交互
                    outcome = await self._request_permission(writer, session_id, data)
                    logger.info("permission outcome: %s", outcome)
                    # 注意：将 outcome 反馈给 daemon 需要 daemon 协议支持，
                    # 当前 daemon HTTP/SSE 不支持反向注入权限结果，
                    # 实际部署时若 daemon 不发起 permission，此路径不会被触发。
                else:
                    # 发送 session/update 通知（agent_message_chunk / tool_call / 等）
                    notification = {
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "sessionId": session_id,
                            "update": data,
                        },
                    }
                    write_message(writer, notification)
        finally:
            await self.state.session_manager.clear_running(session_id)

        # error 优先于 stopReason
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

    async def _request_permission(
        self,
        writer: asyncio.StreamWriter,
        session_id: str,
        permission_data: dict[str, Any],
    ) -> dict[str, Any] | None:
        """向 client 发 session/request_permission 请求并等待响应

        ACP 协议中 server 可向 client 发请求（双向 JSON-RPC）。
        permission_data 是 daemon 的权限事件，转成 ACP 请求格式：
            params: {sessionId, toolCall: {toolCallId, title, kind, status, content, locations, rawInput}, options}

        返回 client 的响应 result（含 outcome.optionId）。
        """
        # 构造 toolCall（daemon 的 permission 事件字段可能不完整，做兼容）
        tool_call = {
            "toolCallId": permission_data.get("toolCallId",
                            permission_data.get("id", "perm_unknown")),
            "title": permission_data.get("title",
                        permission_data.get("reason", "permission request")),
            "kind": permission_data.get("kind", "other"),
            "status": "pending",
            "content": permission_data.get("content", []),
            "locations": permission_data.get("locations", []),
            "rawInput": permission_data.get("rawInput",
                            permission_data.get("input", {})),
        }

        # 构造 options（若 daemon 已提供就用，否则给默认 allow/deny）
        raw_options = permission_data.get("options")
        if isinstance(raw_options, list) and raw_options:
            options = [
                {
                    "optionId": opt.get("optionId") or opt.get("id", "allow"),
                    "name": opt.get("name", opt.get("optionId", "allow")),
                    "kind": opt.get("kind", "allow_once"),
                }
                for opt in raw_options
                if isinstance(opt, dict)
            ]
        else:
            options = [
                {"optionId": "allow_once", "name": "Allow", "kind": "allow_once"},
                {"optionId": "reject_once", "name": "Deny", "kind": "reject_once"},
            ]

        # 分配 server 端请求 id
        req_id = self._next_request_id
        self._next_request_id += 1

        # 注册 pending future
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_requests[req_id] = fut

        # 发送请求
        request = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "session/request_permission",
            "params": {
                "sessionId": session_id,
                "toolCall": tool_call,
                "options": options,
            },
        }
        write_message(writer, request)
        logger.info("sent session/request_permission (id=%s) for tool %s",
                    req_id, tool_call["toolCallId"])

        # 等待响应（handle_message 会 resolve future）
        try:
            result = await asyncio.wait_for(fut, timeout=300.0)
            return result
        except asyncio.TimeoutError:
            self._pending_requests.pop(req_id, None)
            logger.warning("permission request %s timed out", req_id)
            return {"outcome": {"optionId": "reject_once"}}
        except Exception as e:
            self._pending_requests.pop(req_id, None)
            logger.error("permission request %s failed: %s", req_id, e)
            return None

    async def _handle_notification(self, notif: dict[str, Any]) -> None:
        """处理 ACP 通知（无响应）"""
        method = notif.get("method", "")
        params = notif.get("params") or {}

        if method == "session/cancel":
            session_id = params.get("sessionId", "")
            logger.info("session/cancel: %s", session_id)
            await self.state.session_manager.request_cancel(session_id)
        else:
            logger.warning("unhandled notification: %s", method)


# ============================================================
# 主循环
# ============================================================

async def run_stdio() -> None:
    """stdio 传输层主循环 — 读取 stdin，处理消息，写入 stdout

    Windows 兼容：用后台线程读 stdin 而非 asyncio connect_read_pipe。
    ProactorEventLoop 的 connect_read_pipe(sys.stdin) 在某些环境下
    IOCP 注册失败（WinError 6 句柄无效），导致主循环根本读不到 stdin。
    线程读 + asyncio.Queue 是跨平台稳定的方案。
    """
    agent = StdioAgent()

    # 启动 daemon（如果需要）
    await agent.start_daemon()

    # stdin 用线程读，推入 asyncio.Queue
    import threading
    msg_queue: asyncio.Queue = asyncio.Queue()
    main_loop = asyncio.get_event_loop()  # 在主线程捕获，传给子线程

    def stdin_reader():
        """后台线程：逐行读 stdin，解析 JSON，推入 queue；EOF 推 None"""
        while True:
            try:
                line = sys.stdin.readline()
            except Exception as e:
                logger.error("stdin read error: %s", e)
                main_loop.call_soon_threadsafe(msg_queue.put_nowait, None)
                return
            if not line:
                # EOF
                main_loop.call_soon_threadsafe(msg_queue.put_nowait, None)
                return
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError as e:
                logger.warning("invalid JSON on stdin: %s (line: %s)", e, line[:200])
                continue
            main_loop.call_soon_threadsafe(msg_queue.put_nowait, msg)

    reader_thread = threading.Thread(target=stdin_reader, daemon=True)
    reader_thread.start()

    # stdout 直接写 buffer（避开 ProactorEventLoop connect_write_pipe 限制）
    class _StdoutWriter:
        """包装 sys.stdout.buffer，提供 StreamWriter 兼容接口"""
        async def drain(self):
            sys.stdout.buffer.flush()
        def write(self, data):
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
        def flush(self):
            sys.stdout.buffer.flush()

    writer = _StdoutWriter()
    agent._writer = writer

    logger.info("daemon2acp stdio agent ready (mode=%s)", RUN_MODE)

    try:
        while True:
            msg = await msg_queue.get()
            if msg is None:
                break  # EOF — Client 关闭了连接
            await agent.handle_message(msg, writer)
    except asyncio.CancelledError:
        pass
    finally:
        await agent.stop_daemon()

    logger.info("daemon2acp stdio agent shutting down")


def main() -> None:
    # 日志只写 stderr（stdout 保留给 JSON-RPC 消息）
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        asyncio.run(run_stdio())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
