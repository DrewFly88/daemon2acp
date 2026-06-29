//! daemon2acp-http — HTTP ↔ ACP 映射服务
//!
//! 提供两种交互方式：
//! 1. REST / SSE 端点（兼容现有 atomcode-daemon 的 HTTP API 风格）
//! 2. ACP JSON-RPC 端点（供 ACP 兼容客户端直接调用）

use std::net::SocketAddr;
use std::convert::Infallible;

use axum::{
    Router,
    extract::{Path, State},
    http::{Method, StatusCode},
    response::{
        sse::{Event, Sse},
        Json,
    },
    routing::{get, post, delete},
};
use futures::stream::Stream;
use tokio::sync::watch;
use tokio_stream::StreamExt;
use tower_http::cors::{CorsLayer, Any};
use tracing::{info, warn};

use daemon2acp_core::agent::AgentState;
use daemon2acp_core::acp_mapper::{
    AcpMapper, ChatRequest, StreamEvent,
    JsonRpcMessage, JsonRpcRequest, JsonRpcNotification,
};

/// 应用共享状态
#[derive(Clone)]
struct AppState {
    agent: AgentState,
}

// ============================================================
// 简化的 AI 处理（模拟 — 后续接入 atomcode-core 的 AgentLoop）
// ============================================================

/// 处理 chat 消息并返回事件流
async fn process_chat(
    session_id: &str,
    message: &str,
    cancel_rx: watch::Receiver<bool>,
) -> impl Stream<Item = Result<Event, Infallible>> + Send + 'static {
    let sid = session_id.to_string();
    let msg = message.to_string();

    let stream = async_stream::stream! {
        // 1. 推理文本
        yield Ok(Event::default()
            .event("reasoning")
            .data(serde_json::to_string(&StreamEvent::Reasoning {
                content: format!("Thinking about: {}", msg),
            }).unwrap())
        );

        tokio::time::sleep(std::time::Duration::from_millis(300)).await;

        // 检查取消
        if *cancel_rx.borrow() {
            yield Ok(Event::default()
                .event("turn_end")
                .data(serde_json::to_string(&StreamEvent::TurnEnd {
                    stop_reason: "cancelled".to_string(),
                }).unwrap())
            );
            return;
        }

        // 2. 文本响应
        yield Ok(Event::default()
            .event("text")
            .data(serde_json::to_string(&StreamEvent::Text {
                content: format!("Echo: {} (session: {})", msg, sid),
            }).unwrap())
        );

        tokio::time::sleep(std::time::Duration::from_millis(200)).await;

        // 3. 模拟工具调用
        yield Ok(Event::default()
            .event("tool_use")
            .data(serde_json::to_string(&StreamEvent::ToolUse {
                id: "call_1".to_string(),
                name: "bash".to_string(),
                input: serde_json::json!({ "command": "echo hello" }),
            }).unwrap())
        );

        tokio::time::sleep(std::time::Duration::from_millis(200)).await;

        yield Ok(Event::default()
            .event("tool_result")
            .data(serde_json::to_string(&StreamEvent::ToolResult {
                id: "call_1".to_string(),
                content: serde_json::json!({ "stdout": "hello\n" }),
            }).unwrap())
        );

        // 4. 结束
        yield Ok(Event::default()
            .event("turn_end")
            .data(serde_json::to_string(&StreamEvent::TurnEnd {
                stop_reason: "end_turn".to_string(),
            }).unwrap())
        );
    };

    stream
}

// ============================================================
// REST 端点
// ============================================================

/// POST /chat — 发送消息，以 SSE 流返回（兼容 daemon 现有风格）
async fn chat_handler(
    State(state): State<AppState>,
    Json(req): Json<ChatRequest>,
) -> Result<Sse<impl Stream<Item = Result<Event, Infallible>>>, (StatusCode, Json<serde_json::Value>)> {
    // 获取或创建会话
    let session_id = match &req.session_id {
        Some(id) if state.agent.session_manager.get(id).await.is_some() => id.clone(),
        _ => {
            let info = state.agent.session_manager.create(None).await;
            info.id
        }
    };

    info!("chat: session={}, message={:.50}", session_id, req.message);

    // 创建取消令牌
    let (cancel_tx, cancel_rx) = watch::channel(false);
    state.agent.session_manager.set_cancel_token(&session_id, cancel_tx).await;

    let stream = process_chat(&session_id, &req.message, cancel_rx);

    // 设置 SSE 响应头
    let sse = Sse::new(stream)
        .keep_alive(axum::response::sse::KeepAlive::new()
            .interval(std::time::Duration::from_secs(15))
            .text("keep-alive"));

    // 流结束后清理 cancel token
    // （简化处理：在 stream 外部无法捕获结束事件，实际使用可通过 Drop 或 wrap stream 实现）
    // 这里让 cancel token 自然残留，后续可通过 session/close 清理

    Ok(sse)
}

/// GET /sessions — 列出所有会话
async fn list_sessions_handler(
    State(state): State<AppState>,
) -> Json<serde_json::Value> {
    let sessions = state.agent.session_manager.list().await;
    let sessions_data: Vec<serde_json::Value> = sessions
        .into_iter()
        .map(|s| serde_json::json!({
            "id": s.id,
            "title": s.title,
            "createdAt": s.created_at.to_rfc3339(),
            "updatedAt": s.updated_at.to_rfc3339(),
            "mode": format!("{:?}", s.mode).to_lowercase(),
            "messageCount": s.message_count,
        }))
        .collect();

    Json(serde_json::json!({ "sessions": sessions_data }))
}

/// GET /sessions/:id — 获取单个会话
async fn get_session_handler(
    State(state): State<AppState>,
    Path(id): Path<String>,
) -> Result<Json<serde_json::Value>, (StatusCode, Json<serde_json::Value>)> {
    match state.agent.session_manager.get(&id).await {
        Some(s) => Ok(Json(serde_json::json!({
            "id": s.id,
            "title": s.title,
            "createdAt": s.created_at.to_rfc3339(),
            "updatedAt": s.updated_at.to_rfc3339(),
            "mode": format!("{:?}", s.mode).to_lowercase(),
            "messageCount": s.message_count,
        }))),
        None => Err((
            StatusCode::NOT_FOUND,
            Json(serde_json::json!({ "error": "session not found" })),
        )),
    }
}

/// DELETE /sessions/:id — 删除会话
async fn delete_session_handler(
    State(state): State<AppState>,
    Path(id): Path<String>,
) -> Result<Json<serde_json::Value>, (StatusCode, Json<serde_json::Value>)> {
    // 先取消正在执行的任务
    if let Some(tx) = state.agent.session_manager.get_cancel_sender(&id).await {
        let _ = tx.send(true);
    }

    if state.agent.session_manager.delete(&id).await {
        Ok(Json(serde_json::json!({ "deleted": true, "id": id })))
    } else {
        Err((
            StatusCode::NOT_FOUND,
            Json(serde_json::json!({ "error": "session not found" })),
        ))
    }
}

// ============================================================
// ACP JSON-RPC 端点
// ============================================================

/// POST /acp — 接受 ACP JSON-RPC 2.0 消息
async fn acp_rpc_handler(
    State(state): State<AppState>,
    Json(msg): Json<JsonRpcMessage>,
) -> Result<Json<serde_json::Value>, (StatusCode, Json<serde_json::Value>)> {
    match msg {
        JsonRpcMessage::Request(req) => {
            handle_acp_request(&state, req).await
        }
        JsonRpcMessage::Notification(notif) => {
            handle_acp_notification(&state, notif).await;
            Ok(Json(serde_json::json!({})))
        }
        JsonRpcMessage::Response(_) => {
            // Agent 通常不接收响应（除非是 Client 角色），这里忽略
            Ok(Json(serde_json::json!({})))
        }
    }
}

/// 处理 ACP 请求
async fn handle_acp_request(
    state: &AppState,
    req: JsonRpcRequest,
) -> Result<Json<serde_json::Value>, (StatusCode, Json<serde_json::Value>)> {
    let result = match req.method.as_str() {
        "initialize" => {
            let resp = AcpMapper::build_initialize_response(&state.agent.capabilities);
            serde_json::json!(resp)
        }
        "session/new" => {
            let info = state.agent.session_manager.create(None).await;
            let data = daemon2acp_core::acp_mapper::SessionInfoData::from(info);
            serde_json::json!({ "session": data })
        }
        "session/list" => {
            let sessions = state.agent.session_manager.list().await;
            let sessions_data: Vec<daemon2acp_core::acp_mapper::SessionInfoData> =
                sessions.into_iter().map(Into::into).collect();
            serde_json::json!({ "sessions": sessions_data })
        }
        "session/load" => {
            let session_id = req.params
                .as_ref()
                .and_then(|p| p.get("sessionId"))
                .and_then(|v| v.as_str())
                .unwrap_or("");
            match state.agent.session_manager.get(session_id).await {
                Some(info) => {
                    let data = daemon2acp_core::acp_mapper::SessionInfoData::from(info);
                    serde_json::json!({ "session": data })
                }
                None => return Err((
                    StatusCode::NOT_FOUND,
                    Json(serde_json::json!({
                        "error": { "code": -32000, "message": "session not found" }
                    })),
                )),
            }
        }
        "session/delete" => {
            let session_id = req.params
                .as_ref()
                .and_then(|p| p.get("sessionId"))
                .and_then(|v| v.as_str())
                .unwrap_or("");
            let deleted = state.agent.session_manager.delete(session_id).await;
            serde_json::json!({ "deleted": deleted })
        }
        "session/close" => {
            let session_id = req.params
                .as_ref()
                .and_then(|p| p.get("sessionId"))
                .and_then(|v| v.as_str())
                .unwrap_or("");
            // 取消正在执行的任务
            if let Some(tx) = state.agent.session_manager.get_cancel_sender(session_id).await {
                let _ = tx.send(true);
            }
            serde_json::json!({})
        }
        _ => {
            return Err((
                StatusCode::BAD_REQUEST,
                Json(serde_json::json!({
                    "error": {
                        "code": -32601,
                        "message": format!("method not found: {}", req.method)
                    }
                })),
            ))
        }
    };

    // 构建 JSON-RPC 响应
    let response = serde_json::json!({
        "jsonrpc": "2.0",
        "id": req.id,
        "result": result,
    });

    Ok(Json(response))
}

/// 处理 ACP 通知（无响应）
async fn handle_acp_notification(
    state: &AppState,
    notif: daemon2acp_core::acp_mapper::JsonRpcNotification,
) {
    match notif.method.as_str() {
        "session/cancel" => {
            let session_id = notif.params
                .as_ref()
                .and_then(|p| p.get("sessionId"))
                .and_then(|v| v.as_str())
                .unwrap_or("");
            info!("session/cancel: {}", session_id);
            if let Some(tx) = state.agent.session_manager.get_cancel_sender(session_id).await {
                let _ = tx.send(true);
            }
        }
        _ => {
            warn!("unhandled ACP notification: {}", notif.method);
        }
    }
}

// ============================================================
// 健康检查
// ============================================================

/// GET /health — 健康检查
async fn health_handler() -> Json<serde_json::Value> {
    Json(serde_json::json!({
        "status": "ok",
        "service": "daemon2acp",
        "version": env!("CARGO_PKG_VERSION"),
    }))
}

// ============================================================
// 入口
// ============================================================

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "info".into())
        )
        .init();

    // 初始化 Agent 状态
    let state = AppState {
        agent: AgentState::new(),
    };

    info!("daemon2acp v{} starting...", env!("CARGO_PKG_VERSION"));

    // CORS 配置（允许本地前端访问）
    let cors = CorsLayer::new()
        .allow_methods([Method::GET, Method::POST, Method::DELETE, Method::OPTIONS])
        .allow_origin(Any)
        .allow_headers(Any);

    // 路由
    let app = Router::new()
        // REST API
        .route("/chat", post(chat_handler))
        .route("/sessions", get(list_sessions_handler))
        .route("/sessions/{id}", get(get_session_handler).delete(delete_session_handler))
        // ACP JSON-RPC 端点
        .route("/acp", post(acp_rpc_handler))
        // 健康检查
        .route("/health", get(health_handler))
        .layer(cors)
        .with_state(state);

    let addr = SocketAddr::from(([127, 0, 0, 1], 13456));
    info!("listening on http://{addr}");

    let listener = tokio::net::TcpListener::bind(addr).await.unwrap();
    axum::serve(listener, app).await.unwrap();
}
