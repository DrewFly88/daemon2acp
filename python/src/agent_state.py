"""Agent 状态与能力声明 — 对应 ACP InitializeResponse 中的 agentInfo / agentCapabilities"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

__version__ = "0.1.0"


@dataclass
class PromptCapabilities:
    """ACP promptCapabilities — 描述 Agent 可接受的输入类型"""
    image: bool = False
    audio: bool = False
    embedded_context: bool = True


@dataclass
class SessionCapabilities:
    """ACP sessionCapabilities — 描述 Agent 支持的会话管理能力"""
    list: bool = True
    delete: bool = True
    close: bool = True
    resume: bool = False


@dataclass
class AgentCapabilities:
    """ACP agentCapabilities — 在 initialize 响应中声明

    loadSession=False：本项目 session 是内存的、无持久化，
    与 acp SDK mock runner 语义一致，避免 client 尝试 load 已不存在的 session。
    """
    load_session: bool = False
    prompt_capabilities: PromptCapabilities = field(default_factory=PromptCapabilities)
    session_capabilities: SessionCapabilities = field(default_factory=SessionCapabilities)

    def to_dict(self) -> dict[str, Any]:
        # ACP schema 要求 sessionCapabilities 的 close/list/resume 是字典对象
        # （SessionCloseCapabilities / SessionListCapabilities / SessionResumeCapabilities），
        # 不是布尔值。支持该能力时传空 dict {}，不支持时传 None（字段是 Optional）。
        return {
            "loadSession": self.load_session,
            "promptCapabilities": {
                "image": self.prompt_capabilities.image,
                "audio": self.prompt_capabilities.audio,
                "embeddedContext": self.prompt_capabilities.embedded_context,
            },
            "sessionCapabilities": {
                "close": {} if self.session_capabilities.close else None,
                "list": {} if self.session_capabilities.list else None,
                "resume": {} if self.session_capabilities.resume else None,
            },
        }


@dataclass
class AgentInfo:
    """ACP agentInfo — Agent 自描述信息"""
    name: str = "daemon2acp"
    version: str = __version__
    title: str = "AtomCode ACP Agent"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "title": self.title,
        }


@dataclass
class AgentState:
    """Agent 全局运行时状态"""

    agent_info: AgentInfo = field(default_factory=AgentInfo)
    capabilities: AgentCapabilities = field(default_factory=AgentCapabilities)
    # 是否启用认证
    auth_methods: list[dict[str, Any]] = field(default_factory=list)

    def build_initialize_response(self) -> dict[str, Any]:
        """构建 ACP initialize 响应体"""
        return {
            "protocolVersion": "1",
            "agentInfo": self.agent_info.to_dict(),
            "agentCapabilities": self.capabilities.to_dict(),
            "authMethods": self.auth_methods,
        }
