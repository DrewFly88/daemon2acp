"""会话管理器 — 管理 ACP session 生命周期与状态"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


class SessionMode(str, Enum):
    """ACP 会话模式"""
    NORMAL = "normal"
    PLAN = "plan"
    ARCHITECT = "architect"


@dataclass
class Session:
    """单个会话的内部状态"""
    id: str
    title: str
    created_at: datetime
    updated_at: datetime
    # ACP SessionInfo 必填 cwd（绝对路径）
    cwd: str = ""
    mode: SessionMode = SessionMode.NORMAL
    is_running: bool = False
    messages: list[dict[str, Any]] = field(default_factory=list)
    # 取消事件 — 用于 session/cancel
    cancel_event: asyncio.Event | None = None

    def to_info_dict(self) -> dict[str, Any]:
        """转换为 ACP SessionInfo 格式

        对应 acp.schema.SessionInfo：
            sessionId (必填), cwd (必填), title (可选), updatedAt (可选)
        本项目内部用的 id/createdAt/mode/messageCount 不在 ACP schema 内，不发送。
        """
        return {
            "sessionId": self.id,
            "cwd": self.cwd,
            "title": self.title,
            "updatedAt": self.updated_at.isoformat(),
        }


class SessionManager:
    """会话管理器 — 线程安全的会话存储"""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = asyncio.Lock()

    async def create(self, title: str | None = None, cwd: str = "") -> Session:
        """创建新会话

        cwd: ACP NewSessionRequest 的 cwd 字段（绝对路径），存入 SessionInfo
        """
        sid = str(uuid4())
        now = datetime.now(timezone.utc)
        title = title or f"Session {sid[:8]}"
        session = Session(
            id=sid,
            title=title,
            created_at=now,
            updated_at=now,
            cwd=cwd,
        )
        async with self._lock:
            self._sessions[sid] = session
        return session

    async def list(self) -> list[Session]:
        """列出所有会话（按 updated_at 倒序）"""
        async with self._lock:
            sessions = list(self._sessions.values())
        sessions.sort(key=lambda s: s.updated_at, reverse=True)
        return sessions

    async def get(self, session_id: str) -> Session | None:
        async with self._lock:
            return self._sessions.get(session_id)

    async def delete(self, session_id: str) -> bool:
        async with self._lock:
            return self._sessions.pop(session_id, None) is not None

    async def set_mode(self, session_id: str, mode: SessionMode) -> None:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise KeyError(f"session not found: {session_id}")
            session.mode = mode
            session.updated_at = datetime.now(timezone.utc)

    async def set_running(self, session_id: str, cancel_event: asyncio.Event) -> None:
        """标记会话为运行中，并保存取消事件"""
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise KeyError(f"session not found: {session_id}")
            session.is_running = True
            session.cancel_event = cancel_event

    async def clear_running(self, session_id: str) -> None:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return
            session.is_running = False
            session.cancel_event = None
            session.updated_at = datetime.now(timezone.utc)

    async def append_message(self, session_id: str, message: dict[str, Any]) -> None:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise KeyError(f"session not found: {session_id}")
            session.messages.append(message)
            session.updated_at = datetime.now(timezone.utc)

    async def request_cancel(self, session_id: str) -> bool:
        """请求取消正在执行的 prompt — 返回是否成功发送了取消信号"""
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.cancel_event is None:
                return False
            session.cancel_event.set()
            return True
