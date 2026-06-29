//! 会话管理 — 管理 ACP session 的生命周期和状态

use std::collections::HashMap;
use std::sync::Arc;
use tokio::sync::RwLock;
use uuid::Uuid;
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

/// ACP Session ID（UUID v4）
pub type SessionId = String;

/// 会话模式
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub enum SessionMode {
    #[default]
    Normal,
    Plan,
    Architect,
}

/// ACP 会话信息
#[derive(Debug, Clone, Serialize)]
pub struct SessionInfo {
    pub id: SessionId,
    pub title: String,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
    pub mode: SessionMode,
    pub message_count: usize,
}

/// 内部会话状态
#[derive(Debug)]
pub struct Session {
    pub id: SessionId,
    pub title: String,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
    pub mode: SessionMode,
    /// 当前是否在 prompt 执行中
    pub is_running: bool,
    /// 消息历史（简化版，后续可对接 atomcode-core 的消息类型）
    pub messages: Vec<serde_json::Value>,
    /// 取消令牌 — 用于 session/cancel
    pub cancel_token: Option<tokio::sync::watch::Sender<bool>>,
}

/// 会话管理器
#[derive(Debug, Clone)]
pub struct SessionManager {
    sessions: Arc<RwLock<HashMap<SessionId, Session>>>,
}

impl SessionManager {
    pub fn new() -> Self {
        Self {
            sessions: Arc::new(RwLock::new(HashMap::new())),
        }
    }

    /// 创建新会话
    pub async fn create(&self, title: Option<String>) -> SessionInfo {
        let id = Uuid::new_v4().to_string();
        let now = Utc::now();
        let title = title.unwrap_or_else(|| format!("Session {}", &id[..8]));

        let session = Session {
            id: id.clone(),
            title: title.clone(),
            created_at: now,
            updated_at: now,
            mode: SessionMode::Normal,
            is_running: false,
            messages: Vec::new(),
            cancel_token: None,
        };

        let info = SessionInfo {
            id: id.clone(),
            title: title.clone(),
            created_at: now,
            updated_at: now,
            mode: SessionMode::Normal,
            message_count: 0,
        };

        self.sessions.write().await.insert(id, session);
        info
    }

    /// 获取会话列表
    pub async fn list(&self) -> Vec<SessionInfo> {
        let sessions = self.sessions.read().await;
        let mut list: Vec<SessionInfo> = sessions
            .values()
            .map(|s| SessionInfo {
                id: s.id.clone(),
                title: s.title.clone(),
                created_at: s.created_at,
                updated_at: s.updated_at,
                mode: s.mode.clone(),
                message_count: s.messages.len(),
            })
            .collect();
        list.sort_by(|a, b| b.updated_at.cmp(&a.updated_at));
        list
    }

    /// 获取单个会话
    pub async fn get(&self, id: &str) -> Option<SessionInfo> {
        let sessions = self.sessions.read().await;
        sessions.get(id).map(|s| SessionInfo {
            id: s.id.clone(),
            title: s.title.clone(),
            created_at: s.created_at,
            updated_at: s.updated_at,
            mode: s.mode.clone(),
            message_count: s.messages.len(),
        })
    }

    /// 删除会话
    pub async fn delete(&self, id: &str) -> bool {
        self.sessions.write().await.remove(id).is_some()
    }

    /// 设置会话模式
    pub async fn set_mode(&self, id: &str, mode: SessionMode) -> anyhow::Result<()> {
        let mut sessions = self.sessions.write().await;
        let session = sessions.get_mut(id).ok_or_else(|| anyhow::anyhow!("session not found"))?;
        session.mode = mode;
        session.updated_at = Utc::now();
        Ok(())
    }

    /// 获取会话的取消令牌 sender
    pub async fn get_cancel_sender(&self, id: &str) -> Option<tokio::sync::watch::Sender<bool>> {
        self.sessions.read().await.get(id)?.cancel_token.clone()
    }

    /// 设置取消令牌
    pub async fn set_cancel_token(&self, id: &str, tx: tokio::sync::watch::Sender<bool>) {
        let mut sessions = self.sessions.write().await;
        if let Some(session) = sessions.get_mut(id) {
            session.cancel_token = Some(tx);
            session.is_running = true;
        }
    }

    /// 清除取消令牌（prompt 结束）
    pub async fn clear_cancel_token(&self, id: &str) {
        let mut sessions = self.sessions.write().await;
        if let Some(session) = sessions.get_mut(id) {
            session.cancel_token = None;
            session.is_running = false;
            session.updated_at = Utc::now();
        }
    }
}
