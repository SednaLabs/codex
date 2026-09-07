//! Root of the `codex-core` library.

// Prevent accidental direct writes to stdout/stderr in library code. All
// user-visible output must go through the appropriate abstraction (e.g.,
// the TUI or the tracing stack).
#![deny(clippy::print_stdout, clippy::print_stderr)]
// The combined downstream turn path exceeds rustc's default query-depth limit.
#![recursion_limit = "256"]

mod apply_patch;
mod apps;
mod audio_preparation;
pub use codex_login as auth;
mod capacity_retry;
mod client;
mod client_common;
mod realtime_context;
mod realtime_conversation;
mod realtime_prompt;
mod responses_metadata;
mod responses_retry;
#[allow(dead_code, unused_imports)]
pub(crate) mod session;
pub use responses_metadata::CodexResponsesMetadata;
pub use session::SteerInputError;
mod codex_thread;
mod compact_model_fallback;
mod compact_remote;
mod compact_remote_v2;
mod compact_token_budget;
mod config_lock;
pub use codex_thread::BackgroundTerminalInfo;
pub use codex_thread::CodexThread;
pub use codex_thread::CodexThreadSettingsOverrides;
pub use codex_thread::ThreadConfigSnapshot;
pub use codex_thread::TryStartTurnIfIdleError;
pub use codex_thread::TryStartTurnIfIdleRejectionReason;
pub use agent::goal_notifications::{
    GoalNotificationBinding, GoalNotificationClassification, GoalNotificationInput,
    GoalNotificationPhase, GoalNotificationProjection, GoalNotificationSnapshot,
};
pub use codex_thread::automatic_turn_context_fingerprint;
pub use session::turn_context::TurnContext;
mod agent;
mod agent_communication;
mod attestation;
mod codex_delegate;
mod command_canonicalization;
pub mod config;
pub mod connectors;
pub mod context;
mod context_manager;
mod current_time;
#[allow(dead_code)]
mod custom_prompts;
mod elicitation;
mod environment_selection;
pub use codex_utils_path::env;
pub mod exec;
pub mod exec_env;
mod exec_policy;
#[cfg(test)]
mod git_info_tests;
mod guardian;
mod hook_runtime;
mod image_preparation;
mod installation_id;
pub(crate) mod mcp;
mod mcp_openai_file;
mod mcp_skill_dependencies;
mod mcp_tool_approval_templates;
mod mcp_tool_exposure;
// This PR #700 foundation is intentionally dormant and crate-private. Its
// inline tests cover the module until consumer wiring lands in a separate follow-up.
#[allow(dead_code)]
pub(crate) mod model_request_admission;
mod network_policy_decision;
mod original_image_detail;
pub use codex_mcp::CodexAppsToolsCache;
pub use codex_mcp::SandboxState;
mod mcp_tool_call;
pub(crate) mod mention_syntax;
pub(crate) mod utils;
pub use mcp::McpManager;
pub use mention_syntax::PLUGIN_TEXT_MENTION_SIGIL;
pub use mention_syntax::TOOL_MENTION_SIGIL;
pub use utils::path_utils;
pub(crate) mod plugins;
#[doc(hidden)]
pub(crate) mod prompt_debug;
#[doc(hidden)]
pub use prompt_debug::build_prompt_input;
pub(crate) mod mentions {
    pub(crate) use crate::plugins::build_connector_slug_counts;
    pub(crate) use crate::plugins::build_skill_name_counts;
    pub(crate) use crate::plugins::collect_explicit_app_ids;
    pub(crate) use crate::plugins::collect_explicit_plugin_mentions;
    pub(crate) use crate::plugins::collect_tool_mentions_from_messages;
}
mod sandbox_tags;
pub mod sandboxing;
#[allow(dead_code)]
mod session_prefix;
mod session_startup_prewarm;
pub mod skills;
pub(crate) use skills::SkillInjections;
#[allow(unused_imports)]
pub(crate) use skills::SkillLoadOutcome;
pub(crate) use skills::SkillMetadata;
pub(crate) use skills::SkillsService;
pub(crate) use skills::build_available_skills;
pub(crate) use skills::build_skill_injections;
pub(crate) use skills::build_skill_name_counts;
pub(crate) use skills::collect_explicit_skill_mentions;
pub(crate) use skills::default_skill_metadata_budget;
pub(crate) use skills::injection;
pub(crate) use skills::maybe_emit_implicit_skill_invocation;
pub(crate) use skills::skills_load_input_from_config;
mod stream_events_utils;
pub mod test_support;
#[allow(dead_code)]
mod unified_exec;
pub mod windows_sandbox;
pub use client::X_RESPONSESAPI_INCLUDE_TIMING_METRICS_HEADER;
pub use codex_model_provider_info::DEFAULT_LMSTUDIO_PORT;
pub use codex_model_provider_info::DEFAULT_OLLAMA_PORT;
pub use codex_model_provider_info::LMSTUDIO_OSS_PROVIDER_ID;
pub use codex_model_provider_info::ModelProviderInfo;
pub use codex_model_provider_info::OLLAMA_OSS_PROVIDER_ID;
pub use codex_model_provider_info::OPENAI_PROVIDER_ID;
pub use codex_model_provider_info::WireApi;
pub use codex_model_provider_info::built_in_model_providers;
pub use codex_model_provider_info::create_oss_provider_with_base_url;
pub use codex_protocol::config_types::ModelProviderAuthInfo;
mod event_mapping;
pub use codex_prompts as review_prompts;
mod thread_manager;
pub(crate) mod web_search;
pub(crate) mod windows_sandbox_read_grants;
pub use thread_manager::ForkSnapshot;
pub use thread_manager::NewThread;
pub use thread_manager::StartThreadOptions;
pub use thread_manager::ThreadManager;
pub use thread_manager::ThreadShutdownReport;
pub use thread_manager::V2ThreadUnloadResult;
pub use thread_manager::build_models_manager;
pub use thread_manager::local_agent_graph_store_from_state_db;
pub use thread_manager::thread_store_from_config;
pub use tools::handlers::WaitForEnvironmentToolConfig;
pub use web_search::web_search_action_detail;
pub use windows_sandbox_read_grants::grant_read_root_non_elevated;
#[deprecated(note = "use ThreadManager")]
pub type ConversationManager = ThreadManager;
#[deprecated(note = "use NewThread")]
pub type NewConversation = NewThread;
#[deprecated(note = "use CodexThread")]
pub type CodexConversation = CodexThread;
// Re-export common auth types for workspace consumers.
pub use auth::AuthManager;
pub use auth::CodexAuth;
pub(crate) mod agents_md;
mod agents_md_manager;
pub use agents_md::DEFAULT_AGENTS_MD_FILENAME;
pub use agents_md::LOCAL_AGENTS_MD_FILENAME;
pub use agents_md::LoadedAgentsMd;
mod rollout;
mod rollout_budget;
pub(crate) mod safety;
mod session_rollout_init_error;
pub mod shell;
pub(crate) mod shell_snapshot;
pub mod spawn;
pub(crate) mod state_db_bridge;
pub use codex_rollout::state_db;
pub use state_db_bridge::StateDbHandle;
pub use state_db_bridge::init_state_db;
mod thread_rollout_truncation;
pub use thread_rollout_truncation::truncate_rollout_after_turn_id;
pub use thread_rollout_truncation::truncate_rollout_before_turn_id;
mod tools;
pub(crate) mod turn_diff_tracker;
mod turn_metadata;
pub use turn_metadata::detached_memory_responses_metadata;
mod turn_timing;
pub use rollout::ARCHIVED_SESSIONS_SUBDIR;
pub use rollout::Cursor;
pub use rollout::INTERACTIVE_SESSION_SOURCES;
pub use rollout::RolloutRecorder;
pub use rollout::RolloutRecorderParams;
pub use rollout::SESSIONS_SUBDIR;
pub use rollout::SessionMeta;
pub use rollout::SortDirection;
pub use rollout::ThreadItem;
pub use rollout::ThreadSortKey;
pub use rollout::ThreadsPage;
pub use rollout::append_thread_name;
pub use rollout::find_archived_thread_path_by_id_str;
#[deprecated(note = "use find_thread_path_by_id_str")]
pub use rollout::find_conversation_path_by_id_str;
pub use rollout::find_thread_meta_by_name_str;
pub use rollout::find_thread_name_by_id;
pub use rollout::find_thread_names_by_ids;
pub use rollout::find_thread_path_by_id_str;
pub use rollout::parse_cursor;
pub use rollout::read_head_for_summary;
pub use rollout::read_session_meta_line;
pub use rollout::rollout_date_parts;
mod function_tool;
mod state;
mod tasks;
mod user_shell_command;
pub mod util;
#[allow(unused_imports)]
pub(crate) use codex_protocol::protocol;
#[allow(unused_imports)]
pub(crate) use codex_shell_command::is_safe_command;

pub use attestation::AttestationContext;
pub use attestation::AttestationProvider;
pub use attestation::GenerateAttestationFuture;
pub use client::ModelClient;
pub use client::ModelClientSession;
pub use client::X_CODEX_INSTALLATION_ID_HEADER;
pub use client::X_CODEX_TURN_METADATA_HEADER;
pub use client_common::Prompt;
pub use client_common::ResponseEvent;
pub use client_common::ResponseStream;
pub use codex_prompts::REVIEW_PROMPT;
pub use compact::content_items_to_text;
pub use current_time::SleepFuture;
pub use current_time::TimeFuture;
pub use current_time::TimeProvider;
pub use event_mapping::parse_turn_item;
pub use exec_policy::ExecPolicyError;
pub use exec_policy::check_execpolicy_for_warnings;
pub use exec_policy::format_exec_policy_error_with_source;
pub use exec_policy::load_exec_policy;
pub use installation_id::resolve_installation_id;
pub mod compact;
mod memory_usage;
pub mod otel_init;
