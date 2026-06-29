//! daemon2acp-core — ACP Agent 核心库
//!
//! 提供会话管理、ACP 方法映射、Agent 状态等核心能力。
//! 是 HTTP / stdio 等传输层公用的核心库。

pub mod session;
pub mod agent;
pub mod acp_mapper;

// 重导出核心类型
pub use session::SessionManager;
pub use agent::AgentState;
pub use acp_mapper::AcpMapper;
