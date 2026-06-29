//! Agent 状态 — 全局 Agent 运行时状态

use crate::session::SessionManager;
use std::sync::Arc;
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

/// Agent 信息（对应 ACP InitializeResponse.agentInfo）
#[derive(Debug, Clone, Serialize)]
pub struct AgentInfo {
    pub name: String,
    pub version: String,
    pub title: String,
}

impl Default for AgentInfo {
    fn default() -> Self {
        Self {
            name: "daemon2acp".to_string(),
            version: env!("CARGO_PKG_VERSION").to_string(),
            title: "AtomCode ACP Agent".to_string(),
        }
    }
}

/// ACP 能力声明
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AgentCapabilities {
    pub load_session: bool,
    pub prompt_capabilities: PromptCapabilities,
    pub session_capabilities: SessionCapabilities,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PromptCapabilities {
    pub image: bool,
    pub audio: bool,
    pub embedded_context: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SessionCapabilities {
    pub list: bool,
    pub delete: bool,
    pub close: bool,
}

impl Default for AgentCapabilities {
    fn default() -> Self {
        Self {
            load_session: true,
            prompt_capabilities: PromptCapabilities {
                image: false,
                audio: false,
                embedded_context: true,
            },
            session_capabilities: SessionCapabilities {
                list: true,
                delete: true,
                close: true,
            },
        }
    }
}

/// Agent 全局状态
#[derive(Clone)]
pub struct AgentState {
    pub session_manager: SessionManager,
    pub agent_info: AgentInfo,
    pub capabilities: AgentCapabilities,
}

impl AgentState {
    pub fn new() -> Self {
        Self {
            session_manager: SessionManager::new(),
            agent_info: AgentInfo::default(),
            capabilities: AgentCapabilities::default(),
        }
    }
}
