//! ACP 映射器 — HTTP 请求/响应 ↔ ACP 方法/类型的转换

use crate::agent::AgentCapabilities;
use crate::session::{SessionInfo, SessionId};
use serde::{Deserialize, Serialize};
use serde_json::Value;

// ============================================================
// ACP 协议类型定义（精简版，核心交互所需）
// ============================================================

/// ACP 协议版本
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ProtocolVersion(pub String);

impl ProtocolVersion {
    pub const V1: ProtocolVersion = ProtocolVersion("1".to_string());
}

/// JSON-RPC 2.0 消息信封
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(untagged)]
pub enum JsonRpcMessage {
    Request(JsonRpcRequest),
    Response(JsonRpcResponse),
    Notification(JsonRpcNotification),
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct JsonRpcRequest {
    pub jsonrpc: String,
    pub id: Value,
    pub method: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub params: Option<Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct JsonRpcResponse {
    pub jsonrpc: String,
    pub id: Value,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub result: Option<Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error: Option<JsonRpcError>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct JsonRpcNotification {
    pub jsonrpc: String,
    pub method: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub params: Option<Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct JsonRpcError {
    pub code: i32,
    pub message: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub data: Option<Value>,
}

// ============================================================
// Initialize
// ============================================================

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct InitializeRequest {
    pub protocol_version: String,
    #[serde(default)]
    pub client_capabilities: Value,
    #[serde(default)]
    pub client_info: Option<Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct InitializeResponse {
    pub protocol_version: String,
    pub agent_info: Value,
    pub agent_capabilities: Value,
    #[serde(default)]
    pub auth_methods: Vec<Value>,
}

// ============================================================
// Session
// ============================================================

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct NewSessionRequest {
    #[serde(default)]
    pub title: Option<String>,
    #[serde(default)]
    pub additional_directories: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct NewSessionResponse {
    pub session: SessionInfoData,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SessionInfoData {
    pub id: SessionId,
    pub title: String,
    pub created_at: String,
    pub updated_at: String,
    pub mode: String,
    pub message_count: usize,
}

impl From<SessionInfo> for SessionInfoData {
    fn from(info: SessionInfo) -> Self {
        Self {
            id: info.id,
            title: info.title,
            created_at: info.created_at.to_rfc3339(),
            updated_at: info.updated_at.to_rfc3339(),
            mode: format!("{:?}", info.mode).to_lowercase(),
            message_count: info.message_count,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ListSessionsRequest {
    #[serde(default)]
    pub cursor: Option<String>,
    #[serde(default)]
    pub limit: Option<u32>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ListSessionsResponse {
    pub sessions: Vec<SessionInfoData>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub next_cursor: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct LoadSessionRequest {
    pub session_id: SessionId,
}

// ============================================================
// Prompt
// ============================================================

/// HTTP Chat 请求（简化版）
#[derive(Debug, Clone, Deserialize)]
pub struct ChatRequest {
    pub session_id: Option<String>,
    pub message: String,
}

/// SSE 事件类型（对应 ACP session/update 中的 contentBlock）
#[derive(Debug, Clone, Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum StreamEvent {
    /// 文本内容
    Text {
        content: String,
    },
    /// 推理内容
    Reasoning {
        content: String,
    },
    /// 工具调用
    ToolUse {
        id: String,
        name: String,
        input: Value,
    },
    /// 工具结果
    ToolResult {
        id: String,
        content: Value,
    },
    /// 错误
    Error {
        message: String,
    },
    /// Prompt 结束
    TurnEnd {
        stop_reason: String,
    },
}

// ============================================================
// ACP 方法映射器
// ============================================================

/// ACP 方法映射器 — 将 HTTP 请求/响应转换为 ACP 概念
pub struct AcpMapper;

impl AcpMapper {
    /// 构建 ACP InitializeResponse
    pub fn build_initialize_response(capabilities: &AgentCapabilities) -> InitializeResponse {
        InitializeResponse {
            protocol_version: "1".to_string(),
            agent_info: serde_json::json!({
                "name": "daemon2acp",
                "version": env!("CARGO_PKG_VERSION"),
                "title": "AtomCode ACP Agent"
            }),
            agent_capabilities: serde_json::json!({
                "loadSession": capabilities.load_session,
                "promptCapabilities": {
                    "image": capabilities.prompt_capabilities.image,
                    "audio": capabilities.prompt_capabilities.audio,
                    "embeddedContext": capabilities.prompt_capabilities.embedded_context
                },
                "sessionCapabilities": {
                    "list": capabilities.session_capabilities.list,
                    "delete": capabilities.session_capabilities.delete,
                    "close": capabilities.session_capabilities.close
                }
            }),
            auth_methods: vec![],
        }
    }

    /// 解析 ACP 方法请求体为强类型
    pub fn parse_request(method: &str, params: Option<&Value>) -> anyhow::Result<Value> {
        match method {
            "initialize" => Ok(serde_json::to_value(InitializeRequest {
                protocol_version: params
                    .and_then(|p| p.get("protocolVersion"))
                    .and_then(|v| v.as_str())
                    .unwrap_or("1")
                    .to_string(),
                client_capabilities: params
                    .and_then(|p| p.get("clientCapabilities"))
                    .cloned()
                    .unwrap_or_default(),
                client_info: params.and_then(|p| p.get("clientInfo")).cloned(),
            })?),
            "session/new" => Ok(serde_json::to_value(NewSessionRequest {
                title: params.and_then(|p| p.get("title")).and_then(|v| v.as_str()).map(String::from),
                additional_directories: params
                    .and_then(|p| p.get("additionalDirectories"))
                    .and_then(|v| v.as_array())
                    .map(|arr| arr.iter().filter_map(|v| v.as_str().map(String::from)).collect())
                    .unwrap_or_default(),
            })?),
            _ => Ok(params.cloned().unwrap_or(Value::Null)),
        }
    }

    /// 将 ChatRequest 转换为 ACP session/prompt 请求参数
    pub fn chat_to_prompt_params(session_id: &str, message: &str) -> Value {
        serde_json::json!({
            "sessionId": session_id,
            "messages": [{
                "role": "user",
                "content": [{ "type": "text", "text": message }]
            }]
        })
    }
}
