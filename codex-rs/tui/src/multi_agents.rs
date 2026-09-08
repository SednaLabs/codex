//! Helpers for rendering and navigating multi-agent state in the TUI.
//!
//! This module owns the shared presentation contracts for multi-agent history rows, `/agent` picker
//! entries, and the fast-switch keyboard shortcuts. Higher-level coordination, such as deciding
//! which thread becomes active or when a thread closes, stays in [`crate::app::App`].

use crate::history_cell::PlainHistoryCell;
use crate::render::line_utils::prefix_lines;
use crate::status::format_tokens_compact;
use crate::text_formatting::truncate_text;
use chrono::Utc;
use codex_app_server_protocol::AgentNotificationContent;
use codex_app_server_protocol::AgentNotificationSummary;
use codex_app_server_protocol::AskForApproval;
use codex_app_server_protocol::CollabAgentState;
use codex_app_server_protocol::CollabAgentStatus;
use codex_app_server_protocol::CollabAgentTool;
use codex_app_server_protocol::CollabAgentToolCallStatus;
use codex_app_server_protocol::SandboxPolicy;
use codex_app_server_protocol::SubAgentActivityKind;
use codex_app_server_protocol::ThreadItem;
use codex_protocol::ThreadId;
use codex_protocol::config_types::ApprovalsReviewer;
use codex_protocol::openai_models::ReasoningEffort as ReasoningEffortConfig;
use codex_protocol::protocol::AgentStatus;
use codex_protocol::protocol::TokenUsage;
use crossterm::event::KeyCode;
use crossterm::event::KeyEvent;
#[cfg(target_os = "macos")]
use crossterm::event::KeyEventKind;
#[cfg(target_os = "macos")]
use crossterm::event::KeyModifiers;
use ratatui::style::Stylize;
use ratatui::text::Line;
use ratatui::text::Span;
use std::collections::HashSet;

const COLLAB_PROMPT_PREVIEW_GRAPHEMES: usize = 160;
const COLLAB_AGENT_ERROR_PREVIEW_GRAPHEMES: usize = 160;
const COLLAB_AGENT_RESPONSE_PREVIEW_GRAPHEMES: usize = 240;
#[cfg_attr(debug_assertions, allow(dead_code))]
const AGENT_PICKER_TASK_PREVIEW_GRAPHEMES: usize = 48;
pub(crate) const SUBAGENT_LABEL: &str = "Subagent";

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(crate) struct AgentPickerThreadEntry {
    /// Human-friendly nickname shown in picker rows and footer labels.
    pub(crate) agent_nickname: Option<String>,
    /// Agent type shown in brackets when present, for example `worker`.
    pub(crate) agent_role: Option<String>,
    /// Canonical v2 agent path, when the thread was observed through v2 activity.
    pub(crate) agent_path: Option<String>,
    /// Effective model selected for this child thread, when known.
    pub(crate) model: Option<String>,
    /// Effective reasoning effort selected for this child thread, when known.
    pub(crate) reasoning_effort: Option<ReasoningEffortConfig>,
    /// Provider selected for this child thread, when known.
    pub(crate) model_provider: Option<String>,
    /// Canonical task/path preview used by picker search, when known.
    pub(crate) task_name: Option<String>,
    /// Whether the latest liveness refresh says the agent thread is actively working.
    pub(crate) is_running: bool,
    /// Whether the thread has emitted a close event and should render dimmed.
    pub(crate) is_closed: bool,
    /// Unix timestamp (seconds) when the thread was created, if known.
    pub(crate) created_at: Option<i64>,
    /// Unix timestamp (seconds) when the thread was last updated, if known.
    pub(crate) updated_at: Option<i64>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct SubAgentActivityDisplay {
    pub(crate) thread_id: ThreadId,
    pub(crate) agent_path: String,
    pub(crate) model: Option<String>,
    pub(crate) reasoning_effort: Option<ReasoningEffortConfig>,
    pub(crate) is_running_hint: bool,
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub(crate) struct AgentMetadata {
    /// Human-friendly nickname shown in rendered tool-call rows.
    pub(crate) agent_nickname: Option<String>,
    /// Agent type shown in brackets when present, for example `worker`.
    pub(crate) agent_role: Option<String>,
    /// Canonical v2 agent path used when no nickname is available.
    pub(crate) agent_path: Option<String>,
    /// Effective model selected for this child, when known.
    pub(crate) model: Option<String>,
    /// Effective reasoning effort selected for this child, when known.
    pub(crate) reasoning_effort: Option<ReasoningEffortConfig>,
}

#[derive(Clone, Copy)]
struct AgentLabel<'a> {
    thread_id: Option<ThreadId>,
    nickname: Option<&'a str>,
    role: Option<&'a str>,
    path: Option<&'a str>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct SpawnRequestSummary {
    /// Model explicitly supplied to `spawn_agent`, when any.
    pub(crate) model: Option<String>,
    /// Reasoning effort explicitly supplied to `spawn_agent`, when any.
    pub(crate) reasoning_effort: Option<ReasoningEffortConfig>,
}

#[cfg_attr(debug_assertions, allow(dead_code))]
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(crate) struct AgentPickerThreadUsage {
    pub(crate) token_usage: TokenUsage,
    pub(crate) model_context_window: Option<i64>,
    pub(crate) model: Option<String>,
    pub(crate) reasoning_effort: Option<ReasoningEffortConfig>,
    pub(crate) task_name: Option<String>,
    pub(crate) approval_policy: Option<AskForApproval>,
    pub(crate) approvals_reviewer: Option<ApprovalsReviewer>,
    pub(crate) sandbox_policy: Option<SandboxPolicy>,
}

pub(crate) fn agent_picker_status_dot_spans(is_closed: bool) -> Vec<Span<'static>> {
    let dot = if is_closed {
        "•".into()
    } else {
        "•".green()
    };
    vec![dot, " ".into()]
}

pub(crate) fn format_agent_picker_item_name(
    agent_nickname: Option<&str>,
    agent_role: Option<&str>,
    is_primary: bool,
) -> String {
    if is_primary {
        return "Main [default]".to_string();
    }

    let agent_nickname = agent_nickname
        .map(str::trim)
        .filter(|nickname| !nickname.is_empty());
    let agent_role = agent_role.map(str::trim).filter(|role| !role.is_empty());
    match (agent_nickname, agent_role) {
        (Some(agent_nickname), Some(agent_role)) => {
            format!("{SUBAGENT_LABEL}: {agent_nickname} [{agent_role}]")
        }
        (Some(agent_nickname), None) => format!("{SUBAGENT_LABEL}: {agent_nickname}"),
        (None, Some(agent_role)) => format!("{SUBAGENT_LABEL} [{agent_role}]"),
        (None, None) => SUBAGENT_LABEL.to_string(),
    }
}

#[cfg_attr(debug_assertions, allow(dead_code))]
pub(crate) fn format_agent_picker_item_description(
    thread_id: ThreadId,
    entry: &AgentPickerThreadEntry,
    usage: &AgentPickerThreadUsage,
) -> String {
    format_agent_picker_item_description_at(thread_id, entry, usage, Utc::now().timestamp())
}

#[cfg_attr(debug_assertions, allow(dead_code))]
fn format_agent_picker_item_description_at(
    thread_id: ThreadId,
    entry: &AgentPickerThreadEntry,
    usage: &AgentPickerThreadUsage,
    now_ts: i64,
) -> String {
    let uuid = thread_id.to_string();
    let mut parts: Vec<String> = Vec::new();

    let model = usage
        .model
        .as_deref()
        .or(entry.model.as_deref())
        .map(str::trim)
        .filter(|model| !model.is_empty());
    if model.is_some() || usage.reasoning_effort.is_some() {
        let mut model_parts = Vec::new();
        if let Some(model) = model {
            model_parts.push(model.to_string());
        }
        if let Some(reasoning_effort) = &usage.reasoning_effort {
            model_parts.push(reasoning_effort.to_string());
        }
        parts.push(model_parts.join(" "));
    }

    if let Some(task_name) = usage
        .task_name
        .as_deref()
        .or(entry.task_name.as_deref())
        .map(str::trim)
        .filter(|task_name| !task_name.is_empty())
    {
        let task_preview = truncate_text(task_name, AGENT_PICKER_TASK_PREVIEW_GRAPHEMES);
        if !task_preview.is_empty() {
            parts.push(format!("task: {task_preview}"));
        }
    }

    parts.push(uuid);
    if let Some(path) = entry
        .agent_path
        .as_deref()
        .map(str::trim)
        .filter(|path| !path.is_empty())
    {
        parts.push(format!("path: {path}"));
    }
    if entry.is_running {
        parts.push("live active open".to_string());
    } else {
        parts.push("closed stale inactive finished".to_string());
    }
    if usage.token_usage.total_tokens > 0 {
        parts.push(format!(
            "{} used",
            format_tokens_compact(usage.token_usage.total_tokens)
        ));
        if let Some(context_window) = usage.model_context_window {
            parts.push(format!(
                "{}% left",
                usage
                    .token_usage
                    .percent_of_context_window_remaining(context_window)
            ));
        }
    }
    if let Some(age) = format_agent_picker_age(entry.updated_at, entry.created_at, now_ts) {
        parts.push(age);
    }
    parts.join(" • ")
}

#[cfg_attr(debug_assertions, allow(dead_code))]
pub(crate) fn format_agent_picker_item_selected_description(
    thread_id: ThreadId,
    entry: &AgentPickerThreadEntry,
    usage: &AgentPickerThreadUsage,
) -> String {
    let mut description = format_agent_picker_item_description(thread_id, entry, usage);
    if let Some(policy_details) = format_agent_picker_policy_details(
        usage.approval_policy,
        usage.approvals_reviewer,
        usage.sandbox_policy.as_ref(),
    ) {
        description.push_str(" • ");
        description.push_str(&policy_details);
    }
    description
}

#[cfg_attr(debug_assertions, allow(dead_code))]
fn format_agent_picker_policy_details(
    approval_policy: Option<AskForApproval>,
    approvals_reviewer: Option<ApprovalsReviewer>,
    sandbox_policy: Option<&SandboxPolicy>,
) -> Option<String> {
    let mut parts = Vec::new();
    if let Some(approval_policy) = approval_policy {
        parts.push(format!("approval: {}", approval_policy.to_core()));
    }
    if let Some(sandbox_policy) = sandbox_policy {
        parts.push(format!("sandbox: {}", sandbox_policy.to_core()));
    }
    if let Some(approvals_reviewer) = approvals_reviewer {
        parts.push(format!("reviewer: {approvals_reviewer}"));
    }
    (!parts.is_empty()).then(|| parts.join(" • "))
}

#[cfg_attr(debug_assertions, allow(dead_code))]
fn format_agent_picker_age(
    updated_at: Option<i64>,
    created_at: Option<i64>,
    now_ts: i64,
) -> Option<String> {
    let timestamp = updated_at.or(created_at)?;
    let age_secs = now_ts.saturating_sub(timestamp).max(0);
    let label = if age_secs < 60 {
        format!("{age_secs}s ago")
    } else if age_secs < 60 * 60 {
        format!("{}m ago", age_secs / 60)
    } else if age_secs < 60 * 60 * 24 {
        format!("{}h ago", age_secs / (60 * 60))
    } else {
        format!("{}d ago", age_secs / (60 * 60 * 24))
    };
    Some(label)
}

pub(crate) fn previous_agent_shortcut() -> crate::key_hint::KeyBinding {
    crate::key_hint::alt(KeyCode::Left)
}

pub(crate) fn next_agent_shortcut() -> crate::key_hint::KeyBinding {
    crate::key_hint::alt(KeyCode::Right)
}

/// Matches the canonical "previous agent" binding plus platform-specific fallbacks that keep agent
/// navigation working when enhanced key reporting is unavailable.
pub(crate) fn previous_agent_shortcut_matches(
    key_event: KeyEvent,
    allow_word_motion_fallback: bool,
) -> bool {
    previous_agent_shortcut().is_press(key_event)
        || previous_agent_word_motion_fallback(key_event, allow_word_motion_fallback)
}

/// Matches the canonical "next agent" binding plus platform-specific fallbacks that keep agent
/// navigation working when enhanced key reporting is unavailable.
pub(crate) fn next_agent_shortcut_matches(
    key_event: KeyEvent,
    allow_word_motion_fallback: bool,
) -> bool {
    next_agent_shortcut().is_press(key_event)
        || next_agent_word_motion_fallback(key_event, allow_word_motion_fallback)
}

#[cfg(target_os = "macos")]
fn previous_agent_word_motion_fallback(
    key_event: KeyEvent,
    allow_word_motion_fallback: bool,
) -> bool {
    // Some terminals, especially on macOS, send Option+b/f as word-motion keys instead of
    // Option+arrow events unless enhanced keyboard reporting is enabled. Callers should only
    // enable this fallback when the composer is empty so draft editing retains the expected
    // word-wise motion behavior.
    allow_word_motion_fallback
        && matches!(
            key_event,
            KeyEvent {
                code: KeyCode::Char('b'),
                modifiers: KeyModifiers::ALT,
                kind: KeyEventKind::Press | KeyEventKind::Repeat,
                ..
            }
        )
}

#[cfg(not(target_os = "macos"))]
fn previous_agent_word_motion_fallback(
    _key_event: KeyEvent,
    _allow_word_motion_fallback: bool,
) -> bool {
    false
}

#[cfg(target_os = "macos")]
fn next_agent_word_motion_fallback(key_event: KeyEvent, allow_word_motion_fallback: bool) -> bool {
    // Some terminals, especially on macOS, send Option+b/f as word-motion keys instead of
    // Option+arrow events unless enhanced keyboard reporting is enabled. Callers should only
    // enable this fallback when the composer is empty so draft editing retains the expected
    // word-wise motion behavior.
    allow_word_motion_fallback
        && matches!(
            key_event,
            KeyEvent {
                code: KeyCode::Char('f'),
                modifiers: KeyModifiers::ALT,
                kind: KeyEventKind::Press | KeyEventKind::Repeat,
                ..
            }
        )
}

#[cfg(not(target_os = "macos"))]
fn next_agent_word_motion_fallback(
    _key_event: KeyEvent,
    _allow_word_motion_fallback: bool,
) -> bool {
    false
}

pub(crate) fn spawn_request_summary(item: &ThreadItem) -> Option<SpawnRequestSummary> {
    match item {
        ThreadItem::CollabAgentToolCall {
            tool: CollabAgentTool::SpawnAgent,
            status,
            model,
            reasoning_effort,
            requested_model,
            requested_reasoning_effort,
            ..
        } => {
            let has_explicit_request =
                requested_model.is_some() || requested_reasoning_effort.is_some();
            let (model, reasoning_effort) = if has_explicit_request {
                (requested_model.clone(), requested_reasoning_effort.clone())
            } else if matches!(status, CollabAgentToolCallStatus::InProgress) {
                // Older in-progress payloads only exposed their request through
                // the established legacy aliases.
                (model.clone(), reasoning_effort.clone())
            } else {
                // Historic terminal payloads used the legacy pair for an
                // effective snapshot. With no explicit requested provenance,
                // treating it as a request would be a false assertion.
                return None;
            };
            (model.is_some() || reasoning_effort.is_some()).then_some(SpawnRequestSummary {
                model,
                reasoning_effort,
            })
        }
        _ => None,
    }
}

pub(crate) fn tool_call_history_cell(
    item: &ThreadItem,
    cached_spawn_request: Option<&SpawnRequestSummary>,
    mut agent_metadata: impl FnMut(ThreadId) -> AgentMetadata,
) -> Option<PlainHistoryCell> {
    let ThreadItem::CollabAgentToolCall {
        tool,
        status,
        sender_thread_id: _,
        receiver_thread_ids,
        prompt,
        model,
        reasoning_effort,
        requested_model,
        requested_reasoning_effort,
        effective_model,
        effective_reasoning_effort,
        agents_states,
        wake_notifications,
        completion_reason,
        ..
    } = item
    else {
        return None;
    };

    let first_receiver = receiver_thread_ids
        .first()
        .and_then(|id| parse_thread_id(id));
    let prompt = prompt.as_deref().unwrap_or_default();
    let terminal_legacy_aliases_are_observed_effective_identity = matches!(
        (tool, status),
        (
            CollabAgentTool::SpawnAgent,
            CollabAgentToolCallStatus::Completed | CollabAgentToolCallStatus::Failed
        )
    ) && requested_model.is_none()
        && requested_reasoning_effort.is_none()
        && effective_model.is_none()
        && effective_reasoning_effort.is_none();
    let (effective_model, effective_reasoning_effort) =
        if terminal_legacy_aliases_are_observed_effective_identity {
            // Pre-additive terminal records used the established aliases for the
            // observed snapshot. Only use that fallback when all explicit identity
            // fields are absent: a current terminal item's unknown effective
            // identity must remain unknown even if it has request provenance.
            (model.as_deref(), reasoning_effort.as_ref())
        } else {
            (
                effective_model.as_deref(),
                effective_reasoning_effort.as_ref(),
            )
        };

    match tool {
        CollabAgentTool::SpawnAgent => {
            if matches!(status, CollabAgentToolCallStatus::InProgress) {
                let fallback_spawn_request = spawn_request_summary(item);
                let spawn_request = cached_spawn_request.or(fallback_spawn_request.as_ref());
                return Some(spawn_begin(prompt, spawn_request));
            }
            let fallback_spawn_request = spawn_request_summary(item);
            let spawn_request = cached_spawn_request.or(fallback_spawn_request.as_ref());
            Some(spawn_end(
                first_receiver,
                prompt,
                spawn_request,
                effective_model,
                effective_reasoning_effort,
                &mut agent_metadata,
            ))
        }
        CollabAgentTool::SendInput => {
            if matches!(status, CollabAgentToolCallStatus::InProgress) {
                return None;
            }
            first_receiver.map(|receiver_thread_id| {
                interaction_end(receiver_thread_id, prompt, &mut agent_metadata)
            })
        }
        CollabAgentTool::ResumeAgent => first_receiver.map(|receiver_thread_id| {
            if matches!(status, CollabAgentToolCallStatus::InProgress) {
                resume_begin(receiver_thread_id, &mut agent_metadata)
            } else {
                let state = first_agent_state(receiver_thread_ids, agents_states);
                resume_end(
                    receiver_thread_id,
                    state,
                    "Agent resume failed",
                    &mut agent_metadata,
                )
            }
        }),
        CollabAgentTool::Wait => {
            if matches!(status, CollabAgentToolCallStatus::InProgress) {
                Some(waiting_begin(receiver_thread_ids, &mut agent_metadata))
            } else {
                Some(waiting_end(
                    receiver_thread_ids,
                    agents_states,
                    wake_notifications.as_deref().unwrap_or_default(),
                    *completion_reason,
                    &mut agent_metadata,
                ))
            }
        }
        CollabAgentTool::CloseAgent => {
            if matches!(status, CollabAgentToolCallStatus::InProgress) {
                return None;
            }
            first_receiver
                .map(|receiver_thread_id| close_end(receiver_thread_id, &mut agent_metadata))
        }
    }
}

pub(crate) fn sub_agent_activity_display(item: &ThreadItem) -> Option<SubAgentActivityDisplay> {
    let ThreadItem::SubAgentActivity {
        kind,
        agent_thread_id,
        agent_path,
        model,
        reasoning_effort,
        ..
    } = item
    else {
        return None;
    };
    let is_running_hint = match kind {
        SubAgentActivityKind::Started => true,
        SubAgentActivityKind::Interacted => return None,
        SubAgentActivityKind::Interrupted => false,
    };
    Some(SubAgentActivityDisplay {
        thread_id: parse_thread_id(agent_thread_id)?,
        agent_path: agent_path.clone(),
        model: model.clone(),
        reasoning_effort: reasoning_effort.clone(),
        is_running_hint,
    })
}

pub(crate) fn sub_agent_activity_history_cell(item: &ThreadItem) -> Option<PlainHistoryCell> {
    let ThreadItem::SubAgentActivity {
        kind,
        agent_path,
        model,
        reasoning_effort,
        ..
    } = item
    else {
        return None;
    };
    Some(collab_event(
        sub_agent_activity_title(
            *kind,
            agent_path,
            model.as_deref(),
            reasoning_effort.as_ref(),
        ),
        Vec::new(),
    ))
}

pub(crate) fn sub_agent_activity_summary(kind: SubAgentActivityKind, agent_path: &str) -> String {
    match kind {
        SubAgentActivityKind::Started => format!("Started `{agent_path}`"),
        SubAgentActivityKind::Interacted => format!("Interacted with `{agent_path}`"),
        SubAgentActivityKind::Interrupted => format!("Interrupted `{agent_path}`"),
    }
}

fn sub_agent_activity_title(
    kind: SubAgentActivityKind,
    agent_path: &str,
    model: Option<&str>,
    reasoning_effort: Option<&ReasoningEffortConfig>,
) -> Line<'static> {
    let (prefix, path) = match kind {
        SubAgentActivityKind::Started => ("Started ", agent_path),
        SubAgentActivityKind::Interacted => ("Interacted with ", agent_path),
        SubAgentActivityKind::Interrupted => ("Interrupted ", agent_path),
    };
    let mut spans = vec![
        Span::from(prefix).bold(),
        Span::from(format!("`{path}`")).cyan(),
    ];
    spans.extend(model_reasoning_spans(model, reasoning_effort));
    title_spans_line(spans)
}

fn spawn_begin(prompt: &str, spawn_request: Option<&SpawnRequestSummary>) -> PlainHistoryCell {
    let mut details = Vec::new();
    if let Some(line) = prompt_line(prompt) {
        details.push(line);
    }
    collab_event(
        title_with_primitive(
            "Spawning",
            "spawn_agent",
            /*agent*/ None,
            spawn_request,
        ),
        details,
    )
}

fn spawn_end(
    new_thread_id: Option<ThreadId>,
    prompt: &str,
    spawn_request: Option<&SpawnRequestSummary>,
    effective_model: Option<&str>,
    effective_reasoning_effort: Option<&ReasoningEffortConfig>,
    agent_metadata: &mut impl FnMut(ThreadId) -> AgentMetadata,
) -> PlainHistoryCell {
    let title = match new_thread_id {
        Some(thread_id) => {
            let metadata = agent_metadata(thread_id);
            title_with_primitive_spawn_details(
                "Spawned",
                "spawn_agent",
                Some(agent_label(thread_id, &metadata)),
                effective_model,
                effective_reasoning_effort,
                spawn_request,
            )
        }
        None => title_with_primitive_spawn_details(
            "Agent spawn failed",
            "spawn_agent",
            /*agent*/ None,
            effective_model,
            effective_reasoning_effort,
            spawn_request,
        ),
    };

    let mut details = Vec::new();
    if let Some(line) = prompt_line(prompt) {
        details.push(line);
    }
    collab_event(title, details)
}

fn interaction_end(
    receiver_thread_id: ThreadId,
    prompt: &str,
    agent_metadata: &mut impl FnMut(ThreadId) -> AgentMetadata,
) -> PlainHistoryCell {
    let title = title_with_agent(
        "Sent input to",
        agent_label(receiver_thread_id, &agent_metadata(receiver_thread_id)),
        /*spawn_request*/ None,
    );

    let mut details = Vec::new();
    if let Some(line) = prompt_line(prompt) {
        details.push(line);
    }
    collab_event(title, details)
}

fn waiting_begin(
    receiver_thread_ids: &[String],
    agent_metadata: &mut impl FnMut(ThreadId) -> AgentMetadata,
) -> PlainHistoryCell {
    let receiver_agents = receiver_thread_ids
        .iter()
        .filter_map(|thread_id| parse_thread_id(thread_id))
        .map(|thread_id| (thread_id, agent_metadata(thread_id)))
        .collect::<Vec<_>>();

    let title = match receiver_agents.as_slice() {
        [(thread_id, metadata)] => title_with_primitive_agent_details(
            "Waiting on",
            "wait_agent",
            agent_label(*thread_id, metadata),
            metadata.model.as_deref(),
            metadata.reasoning_effort.as_ref(),
        ),
        [] => title_with_primitive(
            "Waiting",
            "wait_agent",
            /*agent*/ None,
            /*spawn_request*/ None,
        ),
        _ => title_with_primitive_text(
            "Waiting on",
            "wait_agent",
            Some(format_agent_names(&receiver_agents)),
            /*spawn_request*/ None,
        ),
    };

    let details = if receiver_agents.len() > 1 {
        receiver_agents
            .iter()
            .map(|(thread_id, metadata)| {
                let mut spans = agent_label_spans(agent_label(*thread_id, metadata));
                spans.extend(model_reasoning_spans(
                    metadata.model.as_deref(),
                    metadata.reasoning_effort.as_ref(),
                ));
                spans.into()
            })
            .collect()
    } else {
        Vec::new()
    };

    collab_event(title, details)
}

fn waiting_end(
    receiver_thread_ids: &[String],
    agents_states: &std::collections::HashMap<String, CollabAgentState>,
    notifications: &[AgentNotificationSummary],
    completion_reason: Option<codex_protocol::protocol::CollabWaitingCompletionReason>,
    agent_metadata: &mut impl FnMut(ThreadId) -> AgentMetadata,
) -> PlainHistoryCell {
    let pending = pending_wait_thread_ids(receiver_thread_ids, agents_states);
    let title = if matches!(
        completion_reason,
        Some(codex_protocol::protocol::CollabWaitingCompletionReason::Mailbox)
    ) {
        "Mailbox update received"
    } else if pending.is_empty() {
        "Finished waiting"
    } else {
        "Mailbox update received"
    };
    let mut details =
        wait_complete_lines(receiver_thread_ids, agents_states, &pending, agent_metadata);
    details.extend(notification_lines(notifications));
    collab_event(title_text(title), details)
}

fn notification_lines(notifications: &[AgentNotificationSummary]) -> Vec<Line<'static>> {
    notifications
        .iter()
        .map(|notification| {
            let content = match &notification.content {
                AgentNotificationContent::PlaintextPreview { text, truncated }
                | AgentNotificationContent::SenderSummary { text, truncated } => {
                    format!(
                        ": {}{}",
                        sanitize_notification_preview(text),
                        if *truncated { "..." } else { "" }
                    )
                }
                AgentNotificationContent::EncryptedUnavailable => {
                    ": [encrypted content unavailable]".to_string()
                }
                AgentNotificationContent::Unavailable => ": [content unavailable]".to_string(),
            };
            Line::from(format!(
                "Notification from {}{}",
                notification.sender_agent_path, content
            ))
        })
        .collect()
}

fn sanitize_notification_preview(text: &str) -> String {
    text.chars()
        .map(|ch| match ch {
            '\n' | '\r' | '\t' => ' ',
            ch if ch.is_control() => '\u{FFFD}',
            ch => ch,
        })
        .collect()
}

fn close_end(
    receiver_thread_id: ThreadId,
    agent_metadata: &mut impl FnMut(ThreadId) -> AgentMetadata,
) -> PlainHistoryCell {
    collab_event(
        title_with_agent(
            "Closed",
            agent_label(receiver_thread_id, &agent_metadata(receiver_thread_id)),
            /*spawn_request*/ None,
        ),
        Vec::new(),
    )
}

fn resume_begin(
    receiver_thread_id: ThreadId,
    agent_metadata: &mut impl FnMut(ThreadId) -> AgentMetadata,
) -> PlainHistoryCell {
    collab_event(
        title_with_agent(
            "Resuming",
            agent_label(receiver_thread_id, &agent_metadata(receiver_thread_id)),
            /*spawn_request*/ None,
        ),
        Vec::new(),
    )
}

fn resume_end(
    receiver_thread_id: ThreadId,
    status: Option<&CollabAgentState>,
    fallback_error: &str,
    agent_metadata: &mut impl FnMut(ThreadId) -> AgentMetadata,
) -> PlainHistoryCell {
    collab_event(
        title_with_agent(
            "Resumed",
            agent_label(receiver_thread_id, &agent_metadata(receiver_thread_id)),
            /*spawn_request*/ None,
        ),
        vec![status_summary_line(status, fallback_error)],
    )
}

#[cfg_attr(debug_assertions, allow(dead_code))]
pub(crate) fn subagent_notification(agent_id: &str, status: &AgentStatus) -> PlainHistoryCell {
    let mut spans = vec![Span::from("Subagent update ").bold()];
    if let Ok(thread_id) = ThreadId::from_string(agent_id) {
        spans.extend(agent_label_spans(AgentLabel {
            thread_id: Some(thread_id),
            nickname: None,
            role: None,
            path: None,
        }));
    } else {
        spans.push(Span::from(agent_id.to_string()).cyan());
    }

    collab_event(
        title_spans_line(spans),
        vec![agent_status_summary_line(status)],
    )
}

fn collab_event(title: Line<'static>, details: Vec<Line<'static>>) -> PlainHistoryCell {
    let mut lines: Vec<Line<'static>> = vec![title];
    if !details.is_empty() {
        lines.extend(prefix_lines(details, "  └ ".dim(), "    ".into()));
    }
    PlainHistoryCell::new(lines)
}

fn title_text(title: impl Into<String>) -> Line<'static> {
    title_spans_line(vec![Span::from(title.into()).bold()])
}

fn title_with_agent(
    prefix: &str,
    agent: AgentLabel<'_>,
    spawn_request: Option<&SpawnRequestSummary>,
) -> Line<'static> {
    let mut spans = vec![Span::from(format!("{prefix} ")).bold()];
    spans.extend(agent_label_spans(agent));
    spans.extend(spawn_request_spans(
        spawn_request,
        /*effective_model*/ None,
        /*effective_reasoning_effort*/ None,
    ));
    title_spans_line(spans)
}

fn title_with_primitive(
    action: &str,
    primitive: &str,
    agent: Option<AgentLabel<'_>>,
    spawn_request: Option<&SpawnRequestSummary>,
) -> Line<'static> {
    let mut spans = primitive_title_prefix(action, primitive);
    if let Some(agent) = agent {
        spans.push(Span::from(" · ").dim());
        spans.extend(agent_label_spans(agent));
    }
    spans.extend(spawn_request_spans(
        spawn_request,
        /*effective_model*/ None,
        /*effective_reasoning_effort*/ None,
    ));
    title_spans_line(spans)
}

fn title_with_primitive_agent_details(
    action: &str,
    primitive: &str,
    agent: AgentLabel<'_>,
    model: Option<&str>,
    reasoning_effort: Option<&ReasoningEffortConfig>,
) -> Line<'static> {
    let mut spans = primitive_title_prefix(action, primitive);
    spans.push(Span::from(" · ").dim());
    spans.extend(agent_label_spans(agent));
    spans.extend(model_reasoning_spans(model, reasoning_effort));
    title_spans_line(spans)
}

fn title_with_primitive_spawn_details(
    action: &str,
    primitive: &str,
    agent: Option<AgentLabel<'_>>,
    effective_model: Option<&str>,
    effective_reasoning_effort: Option<&ReasoningEffortConfig>,
    spawn_request: Option<&SpawnRequestSummary>,
) -> Line<'static> {
    let mut spans = primitive_title_prefix(action, primitive);
    if let Some(agent) = agent {
        spans.push(Span::from(" · ").dim());
        spans.extend(agent_label_spans(agent));
    }
    let request_matches_effective = spawn_request.is_some_and(|spawn_request| {
        spawn_request_matches_effective(spawn_request, effective_model, effective_reasoning_effort)
    });
    spans.extend(model_reasoning_spans_with_label(
        (!request_matches_effective).then_some("effective"),
        effective_model,
        effective_reasoning_effort,
    ));
    spans.extend(spawn_request_spans(
        spawn_request,
        effective_model,
        effective_reasoning_effort,
    ));
    title_spans_line(spans)
}

fn title_with_primitive_text(
    action: &str,
    primitive: &str,
    detail: Option<String>,
    spawn_request: Option<&SpawnRequestSummary>,
) -> Line<'static> {
    let mut spans = primitive_title_prefix(action, primitive);
    if let Some(detail) = detail.filter(|detail| !detail.is_empty()) {
        spans.push(Span::from(" · ").dim());
        spans.push(Span::from(detail).cyan());
    }
    spans.extend(spawn_request_spans(
        spawn_request,
        /*effective_model*/ None,
        /*effective_reasoning_effort*/ None,
    ));
    title_spans_line(spans)
}

fn primitive_title_prefix(action: &str, primitive: &str) -> Vec<Span<'static>> {
    vec![
        Span::from(action.to_string()).bold(),
        Span::from(" · primitive: ").dim(),
        Span::from(primitive.to_string()).cyan(),
    ]
}

fn title_spans_line(mut spans: Vec<Span<'static>>) -> Line<'static> {
    let mut title = Vec::with_capacity(spans.len() + 1);
    title.push(Span::from("• ").dim());
    title.append(&mut spans);
    title.into()
}

fn parse_thread_id(thread_id: &str) -> Option<ThreadId> {
    ThreadId::from_string(thread_id).ok()
}

fn agent_label(thread_id: ThreadId, metadata: &AgentMetadata) -> AgentLabel<'_> {
    AgentLabel {
        thread_id: Some(thread_id),
        nickname: metadata.agent_nickname.as_deref(),
        role: metadata.agent_role.as_deref(),
        path: metadata.agent_path.as_deref(),
    }
}

fn agent_label_spans(agent: AgentLabel<'_>) -> Vec<Span<'static>> {
    let mut spans = Vec::new();
    let nickname = agent
        .nickname
        .map(str::trim)
        .filter(|nickname| !nickname.is_empty());
    let role = agent.role.map(str::trim).filter(|role| !role.is_empty());

    if let Some(nickname) = nickname {
        spans.push(Span::from(nickname.to_string()).cyan().bold());
    } else if let Some(path) = agent.path.map(str::trim).filter(|path| !path.is_empty()) {
        spans.push(Span::from(path.to_string()).cyan());
    } else if let Some(thread_id) = agent.thread_id {
        let rendered = thread_id.to_string();
        let short_id = rendered.get(..8).unwrap_or(&rendered).to_string();
        spans.push(Span::from(short_id).cyan());
    } else {
        spans.push(Span::from("agent").cyan());
    }

    if let Some(role) = role {
        spans.push(Span::from(" ").dim());
        spans.push(Span::from(format!("[{role}]")));
    }

    spans
}

fn format_agent_names(agents: &[(ThreadId, AgentMetadata)]) -> String {
    agents
        .iter()
        .map(|(thread_id, metadata)| friendly_agent_name(*thread_id, metadata))
        .collect::<Vec<_>>()
        .join(", ")
}

fn friendly_agent_name(thread_id: ThreadId, metadata: &AgentMetadata) -> String {
    let name = metadata
        .agent_nickname
        .as_deref()
        .map(str::trim)
        .filter(|name| !name.is_empty())
        .or_else(|| {
            metadata
                .agent_path
                .as_deref()
                .map(str::trim)
                .filter(|path| !path.is_empty())
        })
        .map(ToOwned::to_owned)
        .unwrap_or_else(|| {
            let rendered = thread_id.to_string();
            rendered.get(..8).unwrap_or(&rendered).to_string()
        });
    match metadata
        .agent_role
        .as_deref()
        .map(str::trim)
        .filter(|role| !role.is_empty())
    {
        Some(role) => format!("{name} [{role}]"),
        None => name,
    }
}

fn model_reasoning_spans(
    model: Option<&str>,
    reasoning_effort: Option<&ReasoningEffortConfig>,
) -> Vec<Span<'static>> {
    model_reasoning_spans_with_label(/*label*/ None, model, reasoning_effort)
}

fn model_reasoning_spans_with_label(
    label: Option<&str>,
    model: Option<&str>,
    reasoning_effort: Option<&ReasoningEffortConfig>,
) -> Vec<Span<'static>> {
    let model = model.map(str::trim).filter(|model| !model.is_empty());
    if model.is_none() && reasoning_effort.is_none() {
        return Vec::new();
    }

    let has_model = model.is_some();
    let mut details = match label {
        Some(label) => format!(" ({label}: "),
        None => String::from(" ("),
    };
    if let Some(model) = model {
        details.push_str(model);
    }
    if let Some(reasoning_effort) = reasoning_effort {
        if has_model {
            details.push(' ');
        }
        details.push_str(&reasoning_effort.to_string());
    }
    details.push(')');
    vec![Span::from(details).magenta()]
}

fn spawn_request_spans(
    spawn_request: Option<&SpawnRequestSummary>,
    effective_model: Option<&str>,
    effective_reasoning_effort: Option<&ReasoningEffortConfig>,
) -> Vec<Span<'static>> {
    let Some(spawn_request) = spawn_request else {
        return Vec::new();
    };

    if spawn_request_matches_effective(spawn_request, effective_model, effective_reasoning_effort) {
        return Vec::new();
    }

    let requested_model = spawn_request
        .model
        .as_deref()
        .map(str::trim)
        .filter(|model| !model.is_empty());
    let requested_reasoning_effort = spawn_request.reasoning_effort.as_ref();
    let details = match (requested_model, requested_reasoning_effort) {
        (Some(model), Some(reasoning_effort)) => {
            format!("(requested: {model} {reasoning_effort})")
        }
        (Some(model), None) => format!("(requested: {model})"),
        (None, Some(reasoning_effort)) => format!("(requested: {reasoning_effort})"),
        (None, None) => return Vec::new(),
    };

    vec![Span::from(" ").dim(), Span::from(details).magenta()]
}

fn spawn_request_matches_effective(
    spawn_request: &SpawnRequestSummary,
    effective_model: Option<&str>,
    effective_reasoning_effort: Option<&ReasoningEffortConfig>,
) -> bool {
    let requested_model = spawn_request
        .model
        .as_deref()
        .map(str::trim)
        .filter(|model| !model.is_empty());
    let requested_reasoning_effort = spawn_request.reasoning_effort.as_ref();
    let has_explicit_request = requested_model.is_some() || requested_reasoning_effort.is_some();
    let model_matches = requested_model.is_none_or(|requested_model| {
        Some(requested_model)
            == effective_model
                .map(str::trim)
                .filter(|model| !model.is_empty())
    });
    let reasoning_effort_matches =
        requested_reasoning_effort.is_none_or(|requested_reasoning_effort| {
            Some(requested_reasoning_effort) == effective_reasoning_effort
        });

    has_explicit_request && model_matches && reasoning_effort_matches
}

fn prompt_line(prompt: &str) -> Option<Line<'static>> {
    let trimmed = prompt.trim();
    if trimmed.is_empty() {
        None
    } else {
        Some(Line::from(Span::from(truncate_text(
            trimmed,
            COLLAB_PROMPT_PREVIEW_GRAPHEMES,
        ))))
    }
}

fn wait_complete_lines(
    receiver_thread_ids: &[String],
    agents_states: &std::collections::HashMap<String, CollabAgentState>,
    pending_thread_ids: &[String],
    agent_metadata: &mut impl FnMut(ThreadId) -> AgentMetadata,
) -> Vec<Line<'static>> {
    let mut seen = HashSet::new();
    let mut entries = receiver_thread_ids
        .iter()
        .filter_map(|thread_id| {
            let parsed_thread_id = parse_thread_id(thread_id)?;
            let status = agents_states.get(thread_id)?;
            seen.insert(parsed_thread_id);
            Some((parsed_thread_id, agent_metadata(parsed_thread_id), status))
        })
        .collect::<Vec<_>>();

    let mut extras = agents_states
        .iter()
        .filter_map(|(thread_id, status)| {
            let parsed_thread_id = parse_thread_id(thread_id)?;
            (!seen.contains(&parsed_thread_id))
                .then(|| (parsed_thread_id, agent_metadata(parsed_thread_id), status))
        })
        .collect::<Vec<_>>();
    extras.sort_by_key(|entry| entry.0.to_string());
    entries.extend(extras);

    let mut lines = if entries.is_empty() {
        vec![Line::from(Span::from("No agents completed yet"))]
    } else {
        entries
            .into_iter()
            .map(|(thread_id, metadata, status)| {
                let mut spans = agent_label_spans(agent_label(thread_id, &metadata));
                spans.push(Span::from(": ").dim());
                spans.extend(status_summary_spans(status));
                spans.into()
            })
            .collect()
    };

    if !pending_thread_ids.is_empty() {
        let pending = pending_thread_ids
            .iter()
            .map(|thread_id| {
                parse_thread_id(thread_id)
                    .map(|thread_id| friendly_agent_name(thread_id, &agent_metadata(thread_id)))
                    .unwrap_or_else(|| thread_id.clone())
            })
            .collect::<Vec<_>>()
            .join(", ");
        lines.push(Line::from(format!("Still pending: {pending}")));
    }

    lines
}

fn pending_wait_thread_ids(
    receiver_thread_ids: &[String],
    agents_states: &std::collections::HashMap<String, CollabAgentState>,
) -> Vec<String> {
    receiver_thread_ids
        .iter()
        .filter(|thread_id| {
            agents_states
                .get(*thread_id)
                .is_none_or(|status| !collab_agent_state_is_terminal(status))
        })
        .cloned()
        .collect()
}

fn collab_agent_state_is_terminal(status: &CollabAgentState) -> bool {
    matches!(
        status.status,
        CollabAgentStatus::Interrupted
            | CollabAgentStatus::Completed
            | CollabAgentStatus::Errored
            | CollabAgentStatus::Shutdown
            | CollabAgentStatus::NotFound
    )
}

fn first_agent_state<'a>(
    receiver_thread_ids: &[String],
    agents_states: &'a std::collections::HashMap<String, CollabAgentState>,
) -> Option<&'a CollabAgentState> {
    receiver_thread_ids
        .iter()
        .find_map(|thread_id| agents_states.get(thread_id))
        .or_else(|| {
            agents_states
                .iter()
                .min_by(|left, right| left.0.cmp(right.0))
                .map(|(_, status)| status)
        })
}

fn status_summary_line(status: Option<&CollabAgentState>, fallback_error: &str) -> Line<'static> {
    match status {
        Some(status) => status_summary_spans(status).into(),
        None => error_summary_spans(fallback_error).into(),
    }
}

fn agent_status_summary_line(status: &AgentStatus) -> Line<'static> {
    match status {
        AgentStatus::PendingInit => Span::from("Pending init").cyan().into(),
        AgentStatus::Running => Span::from("Running").cyan().bold().into(),
        // Allow `.yellow()`
        #[allow(clippy::disallowed_methods)]
        AgentStatus::Interrupted => Span::from("Interrupted").yellow().into(),
        AgentStatus::Completed(message) => {
            let mut spans = vec![Span::from("Completed").green()];
            if let Some(message) = message.as_ref() {
                let message_preview = truncate_text(
                    &message.split_whitespace().collect::<Vec<_>>().join(" "),
                    COLLAB_AGENT_RESPONSE_PREVIEW_GRAPHEMES,
                );
                if !message_preview.is_empty() {
                    spans.push(Span::from(format!(" - {message_preview}")).dim());
                }
            }
            spans.into()
        }
        AgentStatus::Errored(message) => error_summary_spans(message).into(),
        AgentStatus::Shutdown => Span::from("Shutdown").into(),
        AgentStatus::NotFound => Span::from("Not found").red().into(),
    }
}

fn status_summary_spans(status: &CollabAgentState) -> Vec<Span<'static>> {
    match status.status {
        CollabAgentStatus::PendingInit => vec![Span::from("Pending init").cyan()],
        CollabAgentStatus::Running => vec![Span::from("Running").cyan().bold()],
        // Allow `.yellow()`
        #[allow(clippy::disallowed_methods)]
        CollabAgentStatus::Interrupted => vec![Span::from("Interrupted").yellow()],
        CollabAgentStatus::Completed => {
            let mut spans = vec![Span::from("Completed").green()];
            if let Some(message) = status.message.as_ref() {
                let message_preview = truncate_text(
                    &message.split_whitespace().collect::<Vec<_>>().join(" "),
                    COLLAB_AGENT_RESPONSE_PREVIEW_GRAPHEMES,
                );
                if !message_preview.is_empty() {
                    spans.push(Span::from(" - ").dim());
                    spans.push(Span::from(message_preview));
                }
            }
            spans
        }
        CollabAgentStatus::Errored => {
            error_summary_spans(status.message.as_deref().unwrap_or("Agent errored"))
        }
        CollabAgentStatus::Shutdown => vec![Span::from("Shutdown")],
        CollabAgentStatus::NotFound => vec![Span::from("Not found").red()],
    }
}

fn error_summary_spans(error: &str) -> Vec<Span<'static>> {
    let mut spans = vec![Span::from("Error").red()];
    let error_preview = truncate_text(
        &error.split_whitespace().collect::<Vec<_>>().join(" "),
        COLLAB_AGENT_ERROR_PREVIEW_GRAPHEMES,
    );
    if !error_preview.is_empty() {
        spans.push(Span::from(" - ").dim());
        spans.push(Span::from(error_preview));
    }
    spans
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::history_cell::HistoryCell;
    #[cfg(target_os = "macos")]
    use crossterm::event::KeyEvent;
    #[cfg(target_os = "macos")]
    use crossterm::event::KeyModifiers;
    use insta::assert_snapshot;
    use pretty_assertions::assert_eq;
    use ratatui::style::Color;
    use ratatui::style::Modifier;
    use std::collections::HashMap;

    #[test]
    fn picker_description_falls_back_to_thread_id_without_usage() {
        let thread_id =
            ThreadId::from_string("00000000-0000-0000-0000-000000000111").expect("valid thread");

        assert_eq!(
            format_agent_picker_item_description(
                thread_id,
                &AgentPickerThreadEntry::default(),
                &AgentPickerThreadUsage::default(),
            ),
            "00000000-0000-0000-0000-000000000111 • closed stale inactive finished"
        );
    }

    #[test]
    fn picker_description_includes_compact_token_usage_when_present() {
        let thread_id =
            ThreadId::from_string("00000000-0000-0000-0000-000000000112").expect("valid thread");
        let usage = TokenUsage {
            input_tokens: 9_800,
            cached_input_tokens: 300,
            output_tokens: 2_200,
            total_tokens: 12_300,
            ..Default::default()
        };
        assert_eq!(
            format_agent_picker_item_description(
                thread_id,
                &AgentPickerThreadEntry::default(),
                &AgentPickerThreadUsage {
                    token_usage: usage,
                    ..AgentPickerThreadUsage::default()
                },
            ),
            "00000000-0000-0000-0000-000000000112 • closed stale inactive finished • 12.3K used"
        );
    }

    #[test]
    fn picker_description_includes_remaining_context_when_known() {
        let thread_id =
            ThreadId::from_string("00000000-0000-0000-0000-000000000113").expect("valid thread");
        let usage = TokenUsage {
            total_tokens: 12_300,
            ..Default::default()
        };

        assert_eq!(
            format_agent_picker_item_description_at(
                thread_id,
                &AgentPickerThreadEntry::default(),
                &AgentPickerThreadUsage {
                    token_usage: usage,
                    model_context_window: Some(24_000),
                    ..AgentPickerThreadUsage::default()
                },
                /*now_ts*/ 1_000,
            ),
            "00000000-0000-0000-0000-000000000113 • closed stale inactive finished • 12.3K used • 98% left"
        );
    }

    #[test]
    fn picker_description_includes_compact_age_when_known() {
        let thread_id =
            ThreadId::from_string("00000000-0000-0000-0000-000000000113").expect("valid thread");
        let usage = TokenUsage {
            total_tokens: 12_300,
            ..Default::default()
        };

        let snapshot = [
            format_agent_picker_item_description_at(
                thread_id,
                &AgentPickerThreadEntry {
                    created_at: Some(900),
                    updated_at: Some(958),
                    ..AgentPickerThreadEntry::default()
                },
                &AgentPickerThreadUsage {
                    token_usage: usage.clone(),
                    ..AgentPickerThreadUsage::default()
                },
                /*now_ts*/ 1_000,
            ),
            format_agent_picker_item_description_at(
                thread_id,
                &AgentPickerThreadEntry {
                    created_at: Some(300),
                    updated_at: Some(400),
                    ..AgentPickerThreadEntry::default()
                },
                &AgentPickerThreadUsage {
                    token_usage: usage,
                    ..AgentPickerThreadUsage::default()
                },
                /*now_ts*/ 1_000,
            ),
            format_agent_picker_item_description_at(
                thread_id,
                &AgentPickerThreadEntry {
                    created_at: Some(1_000 - 3 * 60 * 60),
                    ..AgentPickerThreadEntry::default()
                },
                &AgentPickerThreadUsage::default(),
                /*now_ts*/ 1_000,
            ),
        ]
        .join("\n");

        assert_snapshot!("agent_picker_item_description_age", snapshot);
    }

    #[test]
    fn picker_description_includes_model_effort_and_task_when_available() {
        let thread_id =
            ThreadId::from_string("00000000-0000-0000-0000-000000000114").expect("valid thread");
        let usage = TokenUsage {
            total_tokens: 120,
            ..Default::default()
        };

        assert_eq!(
            format_agent_picker_item_description(
                thread_id,
                &AgentPickerThreadEntry::default(),
                &AgentPickerThreadUsage {
                    token_usage: usage,
                    model: Some("gpt-5.4-mini".to_string()),
                    reasoning_effort: Some(ReasoningEffortConfig::Medium),
                    task_name: Some("Investigate /agent picker metadata display".to_string()),
                    ..AgentPickerThreadUsage::default()
                },
            ),
            "gpt-5.4-mini medium • task: Investigate /agent picker metadata display • 00000000-0000-0000-0000-000000000114 • closed stale inactive finished • 120 used"
        );
    }

    #[test]
    fn picker_description_omits_blank_metadata_fields() {
        let thread_id =
            ThreadId::from_string("00000000-0000-0000-0000-000000000115").expect("valid thread");

        assert_eq!(
            format_agent_picker_item_description(
                thread_id,
                &AgentPickerThreadEntry::default(),
                &AgentPickerThreadUsage {
                    model: Some("   ".to_string()),
                    task_name: Some("   ".to_string()),
                    ..AgentPickerThreadUsage::default()
                },
            ),
            "00000000-0000-0000-0000-000000000115 • closed stale inactive finished"
        );
    }

    #[test]
    fn picker_selected_description_includes_permission_details_when_available() {
        let thread_id = ThreadId::from_string("00000000-0000-0000-0000-000000000114").unwrap();
        let description = format_agent_picker_item_selected_description(
            thread_id,
            &AgentPickerThreadEntry::default(),
            &AgentPickerThreadUsage {
                token_usage: TokenUsage {
                    input_tokens: 100,
                    output_tokens: 20,
                    total_tokens: 120,
                    ..Default::default()
                },
                model: Some("gpt-5.4-mini".to_string()),
                reasoning_effort: Some(ReasoningEffortConfig::Medium),
                task_name: Some("Investigate /agent picker metadata display".to_string()),
                approval_policy: Some(AskForApproval::OnRequest),
                approvals_reviewer: Some(ApprovalsReviewer::AutoReview),
                sandbox_policy: Some(SandboxPolicy::WorkspaceWrite {
                    writable_roots: Vec::new(),
                    network_access: false,
                    exclude_tmpdir_env_var: false,
                    exclude_slash_tmp: false,
                }),
                ..AgentPickerThreadUsage::default()
            },
        );
        assert_snapshot!("agent_picker_item_selected_description", description);
    }

    #[test]
    fn interacted_sub_agent_activity_does_not_change_liveness() {
        let item = ThreadItem::SubAgentActivity {
            id: "activity-1".to_string(),
            kind: SubAgentActivityKind::Interacted,
            agent_thread_id: ThreadId::new().to_string(),
            agent_path: "/root/child".to_string(),
            model: None,
            reasoning_effort: None,
        };

        assert_eq!(sub_agent_activity_display(&item), None);
    }

    #[test]
    fn sub_agent_activity_history_includes_effective_identity() {
        let item = ThreadItem::SubAgentActivity {
            id: "activity-identity".to_string(),
            kind: SubAgentActivityKind::Started,
            agent_thread_id: ThreadId::new().to_string(),
            agent_path: "/root/reviewer".to_string(),
            model: Some("gpt-5.4".to_string()),
            reasoning_effort: Some(ReasoningEffortConfig::High),
        };

        let rendered =
            cell_to_text(&sub_agent_activity_history_cell(&item).expect("activity cell"));
        assert!(rendered.contains("/root/reviewer"));
        assert!(rendered.contains("gpt-5.4 high"));
    }

    #[test]
    fn collab_events_snapshot() {
        let sender_thread_id = ThreadId::from_string("00000000-0000-0000-0000-000000000001")
            .expect("valid sender thread id");
        let robie_id = ThreadId::from_string("00000000-0000-0000-0000-000000000002")
            .expect("valid robie thread id");
        let bob_id = ThreadId::from_string("00000000-0000-0000-0000-000000000003")
            .expect("valid bob thread id");

        let spawn = tool_call_history_cell(
            &ThreadItem::CollabAgentToolCall {
                id: "call-spawn".to_string(),
                tool: CollabAgentTool::SpawnAgent,
                status: CollabAgentToolCallStatus::Completed,
                sender_thread_id: sender_thread_id.to_string(),
                receiver_thread_ids: vec![robie_id.to_string()],
                prompt: Some("Compute 11! and reply with just the integer result.".to_string()),
                model: Some("gpt-5".to_string()),
                reasoning_effort: Some(ReasoningEffortConfig::High),
                requested_model: Some("gpt-5".to_string()),
                requested_reasoning_effort: Some(ReasoningEffortConfig::High),
                effective_model: Some("gpt-5".to_string()),
                effective_reasoning_effort: Some(ReasoningEffortConfig::High),
                agents_states: HashMap::from([(
                    robie_id.to_string(),
                    agent_state(CollabAgentStatus::PendingInit, /*message*/ None),
                )]),

                wake_notifications: None,
                completion_reason: None,
            },
            /*cached_spawn_request*/ None,
            |thread_id| metadata_for(thread_id, robie_id, bob_id),
        )
        .expect("spawn item renders");

        let send = tool_call_history_cell(
            &ThreadItem::CollabAgentToolCall {
                id: "call-send".to_string(),
                tool: CollabAgentTool::SendInput,
                status: CollabAgentToolCallStatus::Completed,
                sender_thread_id: sender_thread_id.to_string(),
                receiver_thread_ids: vec![robie_id.to_string()],
                prompt: Some("Please continue and return the answer only.".to_string()),
                model: None,
                reasoning_effort: None,
                requested_model: None,
                requested_reasoning_effort: None,
                effective_model: None,
                effective_reasoning_effort: None,
                agents_states: HashMap::from([(
                    robie_id.to_string(),
                    agent_state(CollabAgentStatus::Running, /*message*/ None),
                )]),

                wake_notifications: None,
                completion_reason: None,
            },
            /*cached_spawn_request*/ None,
            |thread_id| metadata_for(thread_id, robie_id, bob_id),
        )
        .expect("send-input item renders");

        let waiting = tool_call_history_cell(
            &ThreadItem::CollabAgentToolCall {
                id: "call-wait".to_string(),
                tool: CollabAgentTool::Wait,
                status: CollabAgentToolCallStatus::InProgress,
                sender_thread_id: sender_thread_id.to_string(),
                receiver_thread_ids: vec![robie_id.to_string()],
                prompt: None,
                model: None,
                reasoning_effort: None,
                requested_model: None,
                requested_reasoning_effort: None,
                effective_model: None,
                effective_reasoning_effort: None,
                agents_states: HashMap::new(),

                wake_notifications: None,
                completion_reason: None,
            },
            /*cached_spawn_request*/ None,
            |thread_id| metadata_for(thread_id, robie_id, bob_id),
        )
        .expect("wait begin item renders");

        let finished = tool_call_history_cell(
            &ThreadItem::CollabAgentToolCall {
                id: "call-wait".to_string(),
                tool: CollabAgentTool::Wait,
                status: CollabAgentToolCallStatus::Completed,
                sender_thread_id: sender_thread_id.to_string(),
                receiver_thread_ids: vec![robie_id.to_string(), bob_id.to_string()],
                prompt: None,
                model: None,
                reasoning_effort: None,
                requested_model: None,
                requested_reasoning_effort: None,
                effective_model: None,
                effective_reasoning_effort: None,
                agents_states: HashMap::from([
                    (
                        robie_id.to_string(),
                        agent_state(CollabAgentStatus::Completed, Some("39916800")),
                    ),
                    (
                        bob_id.to_string(),
                        agent_state(CollabAgentStatus::Errored, Some("tool timeout")),
                    ),
                ]),

                wake_notifications: None,
                completion_reason: None,
            },
            /*cached_spawn_request*/ None,
            |thread_id| metadata_for(thread_id, robie_id, bob_id),
        )
        .expect("wait end item renders");

        let close = tool_call_history_cell(
            &ThreadItem::CollabAgentToolCall {
                id: "call-close".to_string(),
                tool: CollabAgentTool::CloseAgent,
                status: CollabAgentToolCallStatus::Completed,
                sender_thread_id: sender_thread_id.to_string(),
                receiver_thread_ids: vec![robie_id.to_string()],
                prompt: None,
                model: None,
                reasoning_effort: None,
                requested_model: None,
                requested_reasoning_effort: None,
                effective_model: None,
                effective_reasoning_effort: None,
                agents_states: HashMap::from([(
                    robie_id.to_string(),
                    agent_state(CollabAgentStatus::Completed, Some("39916800")),
                )]),

                wake_notifications: None,
                completion_reason: None,
            },
            /*cached_spawn_request*/ None,
            |thread_id| metadata_for(thread_id, robie_id, bob_id),
        )
        .expect("close item renders");

        let snapshot = [spawn, send, waiting, finished, close]
            .iter()
            .map(cell_to_text)
            .collect::<Vec<_>>()
            .join("\n\n");
        assert_snapshot!("collab_agent_transcript", snapshot);
    }

    #[test]
    fn in_progress_spawn_keeps_partial_requested_identity() {
        let model_only = spawn_item(
            CollabAgentToolCallStatus::InProgress,
            Some("gpt-legacy"),
            Some(ReasoningEffortConfig::High),
            Some("gpt-requested"),
            /*requested_reasoning_effort*/ None,
        );
        assert_eq!(
            spawn_request_summary(&model_only),
            Some(SpawnRequestSummary {
                model: Some("gpt-requested".to_string()),
                reasoning_effort: None,
            })
        );
        let rendered = cell_to_text(
            &tool_call_history_cell(
                &model_only,
                /*cached_spawn_request*/ None,
                |_| AgentMetadata::default(),
            )
            .expect("model-only spawn renders"),
        );
        assert!(rendered.contains("requested: gpt-requested"));
        assert!(
            !rendered.contains("high"),
            "a model-only request must not inherit the legacy reasoning effort: {rendered}"
        );

        let effort_only = spawn_item(
            CollabAgentToolCallStatus::InProgress,
            Some("gpt-legacy"),
            Some(ReasoningEffortConfig::Low),
            /*requested_model*/ None,
            Some(ReasoningEffortConfig::High),
        );
        assert_eq!(
            spawn_request_summary(&effort_only),
            Some(SpawnRequestSummary {
                model: None,
                reasoning_effort: Some(ReasoningEffortConfig::High),
            })
        );
        let rendered = cell_to_text(
            &tool_call_history_cell(
                &effort_only,
                /*cached_spawn_request*/ None,
                |_| AgentMetadata::default(),
            )
            .expect("effort-only spawn renders"),
        );
        assert!(rendered.contains("requested: high"));
        assert!(
            !rendered.contains("gpt-legacy"),
            "an effort-only request must not inherit the legacy model: {rendered}"
        );
    }

    #[test]
    fn partial_spawn_request_matches_only_its_explicit_effective_field() {
        let model_only = SpawnRequestSummary {
            model: Some("gpt-requested".to_string()),
            reasoning_effort: None,
        };
        assert!(spawn_request_matches_effective(
            &model_only,
            Some("gpt-requested"),
            Some(&ReasoningEffortConfig::High),
        ));
        assert!(!spawn_request_matches_effective(
            &model_only,
            Some("gpt-effective"),
            Some(&ReasoningEffortConfig::High),
        ));

        let effort_only = SpawnRequestSummary {
            model: None,
            reasoning_effort: Some(ReasoningEffortConfig::High),
        };
        assert!(spawn_request_matches_effective(
            &effort_only,
            Some("gpt-effective"),
            Some(&ReasoningEffortConfig::High),
        ));
        assert!(!spawn_request_matches_effective(
            &effort_only,
            Some("gpt-effective"),
            Some(&ReasoningEffortConfig::Low),
        ));
    }

    #[test]
    fn spawn_request_summary_preserves_full_none_and_legacy_cases() {
        let both = spawn_item(
            CollabAgentToolCallStatus::InProgress,
            Some("gpt-legacy"),
            Some(ReasoningEffortConfig::Low),
            Some("gpt-requested"),
            Some(ReasoningEffortConfig::Ultra),
        );
        assert_eq!(
            spawn_request_summary(&both),
            Some(SpawnRequestSummary {
                model: Some("gpt-requested".to_string()),
                reasoning_effort: Some(ReasoningEffortConfig::Ultra),
            })
        );

        let no_identity = spawn_item(
            CollabAgentToolCallStatus::InProgress,
            /*model*/ None,
            /*reasoning_effort*/ None,
            /*requested_model*/ None,
            /*requested_reasoning_effort*/ None,
        );
        assert_eq!(spawn_request_summary(&no_identity), None);
        let rendered = cell_to_text(
            &tool_call_history_cell(
                &no_identity,
                /*cached_spawn_request*/ None,
                |_| AgentMetadata::default(),
            )
            .expect("empty spawn renders"),
        );
        assert!(!rendered.contains("requested:"));

        let legacy = spawn_item(
            CollabAgentToolCallStatus::InProgress,
            Some("gpt-legacy"),
            Some(ReasoningEffortConfig::Medium),
            /*requested_model*/ None,
            /*requested_reasoning_effort*/ None,
        );
        assert_eq!(
            spawn_request_summary(&legacy),
            Some(SpawnRequestSummary {
                model: Some("gpt-legacy".to_string()),
                reasoning_effort: Some(ReasoningEffortConfig::Medium),
            })
        );
    }

    #[test]
    fn collab_wait_mailbox_snapshot() {
        let robie_id = ThreadId::from_string("00000000-0000-0000-0000-000000000002")
            .expect("valid robie thread id");
        let bob_id = ThreadId::from_string("00000000-0000-0000-0000-000000000003")
            .expect("valid bob thread id");
        let receiver_thread_ids = vec![robie_id.to_string(), bob_id.to_string()];
        let mut agent_metadata = |thread_id| metadata_for(thread_id, robie_id, bob_id);

        let mut statuses = HashMap::new();
        statuses.insert(
            robie_id.to_string(),
            agent_state(CollabAgentStatus::Completed, Some("39916800")),
        );
        let waiting = waiting_begin(&receiver_thread_ids, &mut agent_metadata);
        let finished = waiting_end(
            &receiver_thread_ids,
            &statuses,
            &[],
            /*completion_reason*/ None,
            &mut agent_metadata,
        );

        let snapshot = [waiting, finished]
            .iter()
            .map(cell_to_text)
            .collect::<Vec<_>>()
            .join("\n\n");
        assert_snapshot!("collab_wait_mailbox", snapshot);
    }

    #[test]
    fn collab_close_end_omits_resume_targets() {
        let receiver_thread_id = ThreadId::from_string("00000000-0000-0000-0000-000000000002")
            .expect("valid receiver thread id");

        let mut agent_metadata = |thread_id| {
            if thread_id == receiver_thread_id {
                AgentMetadata {
                    agent_nickname: Some("Robie".to_string()),
                    agent_role: Some("explorer".to_string()),
                    ..AgentMetadata::default()
                }
            } else {
                AgentMetadata::default()
            }
        };
        let close = close_end(receiver_thread_id, &mut agent_metadata);
        let rendered = cell_to_text(&close);

        assert!(
            rendered.contains("Closed Robie [explorer]"),
            "expected rendered close message to identify the closed agent, got: {rendered}"
        );
        assert!(
            !rendered.contains("Resume subagent:"),
            "expected rendered close message to omit subagent resume target, got: {rendered}"
        );
        assert!(
            !rendered.contains("Return to parent:"),
            "expected rendered close message to omit parent resume target, got: {rendered}"
        );
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn agent_shortcut_matches_option_arrow_word_motion_fallbacks_only_when_allowed() {
        assert!(previous_agent_shortcut_matches(
            KeyEvent::new(KeyCode::Left, KeyModifiers::ALT),
            /*allow_word_motion_fallback*/ false,
        ));
        assert!(next_agent_shortcut_matches(
            KeyEvent::new(KeyCode::Right, KeyModifiers::ALT),
            /*allow_word_motion_fallback*/ false,
        ));
        assert!(previous_agent_shortcut_matches(
            KeyEvent::new(KeyCode::Char('b'), KeyModifiers::ALT),
            /*allow_word_motion_fallback*/ true,
        ));
        assert!(next_agent_shortcut_matches(
            KeyEvent::new(KeyCode::Char('f'), KeyModifiers::ALT),
            /*allow_word_motion_fallback*/ true,
        ));
        assert!(!previous_agent_shortcut_matches(
            KeyEvent::new(KeyCode::Char('b'), KeyModifiers::ALT),
            /*allow_word_motion_fallback*/ false,
        ));
        assert!(!next_agent_shortcut_matches(
            KeyEvent::new(KeyCode::Char('f'), KeyModifiers::ALT),
            /*allow_word_motion_fallback*/ false,
        ));
    }

    #[cfg(not(target_os = "macos"))]
    #[test]
    fn agent_shortcut_matches_option_arrows_only() {
        assert!(previous_agent_shortcut_matches(
            KeyEvent::new(KeyCode::Left, crossterm::event::KeyModifiers::ALT,),
            /*allow_word_motion_fallback*/ false
        ));
        assert!(next_agent_shortcut_matches(
            KeyEvent::new(KeyCode::Right, crossterm::event::KeyModifiers::ALT,),
            /*allow_word_motion_fallback*/ false
        ));
        assert!(!previous_agent_shortcut_matches(
            KeyEvent::new(KeyCode::Char('b'), crossterm::event::KeyModifiers::ALT,),
            /*allow_word_motion_fallback*/ false
        ));
        assert!(!next_agent_shortcut_matches(
            KeyEvent::new(KeyCode::Char('f'), crossterm::event::KeyModifiers::ALT,),
            /*allow_word_motion_fallback*/ false
        ));
    }

    #[test]
    fn title_styles_nickname_and_role() {
        let sender_thread_id = ThreadId::from_string("00000000-0000-0000-0000-000000000001")
            .expect("valid sender thread id");
        let robie_id = ThreadId::from_string("00000000-0000-0000-0000-000000000002")
            .expect("valid robie thread id");
        let cell = tool_call_history_cell(
            &ThreadItem::CollabAgentToolCall {
                id: "call-spawn".to_string(),
                tool: CollabAgentTool::SpawnAgent,
                status: CollabAgentToolCallStatus::Completed,
                sender_thread_id: sender_thread_id.to_string(),
                receiver_thread_ids: vec![robie_id.to_string()],
                prompt: Some(String::new()),
                model: Some("gpt-5".to_string()),
                reasoning_effort: Some(ReasoningEffortConfig::High),
                requested_model: Some("gpt-5".to_string()),
                requested_reasoning_effort: Some(ReasoningEffortConfig::High),
                effective_model: Some("gpt-5".to_string()),
                effective_reasoning_effort: Some(ReasoningEffortConfig::High),
                agents_states: HashMap::from([(
                    robie_id.to_string(),
                    agent_state(CollabAgentStatus::PendingInit, /*message*/ None),
                )]),

                wake_notifications: None,
                completion_reason: None,
            },
            /*cached_spawn_request*/ None,
            |thread_id| metadata_for(thread_id, robie_id, ThreadId::new()),
        )
        .expect("spawn item renders");

        let lines = cell.display_lines(/*width*/ 200);
        let title = &lines[0];
        assert_eq!(title.spans[3].content.as_ref(), "spawn_agent");
        assert_eq!(title.spans[3].style.fg, Some(Color::Cyan));
        assert_eq!(title.spans[5].content.as_ref(), "Robie");
        assert_eq!(title.spans[5].style.fg, Some(Color::Cyan));
        assert!(title.spans[5].style.add_modifier.contains(Modifier::BOLD));
        assert_eq!(title.spans[7].content.as_ref(), "[explorer]");
        assert_eq!(title.spans[7].style.fg, None);
        assert!(!title.spans[7].style.add_modifier.contains(Modifier::DIM));
        assert_eq!(title.spans[8].content.as_ref(), " (gpt-5 high)");
        assert_eq!(title.spans[8].style.fg, Some(Color::Magenta));
    }

    #[test]
    fn collab_resume_interrupted_snapshot() {
        let sender_thread_id = ThreadId::from_string("00000000-0000-0000-0000-000000000001")
            .expect("valid sender thread id");
        let robie_id = ThreadId::from_string("00000000-0000-0000-0000-000000000002")
            .expect("valid robie thread id");

        let cell = tool_call_history_cell(
            &ThreadItem::CollabAgentToolCall {
                id: "call-resume".to_string(),
                tool: CollabAgentTool::ResumeAgent,
                status: CollabAgentToolCallStatus::Completed,
                sender_thread_id: sender_thread_id.to_string(),
                receiver_thread_ids: vec![robie_id.to_string()],
                prompt: None,
                model: None,
                reasoning_effort: None,
                requested_model: None,
                requested_reasoning_effort: None,
                effective_model: None,
                effective_reasoning_effort: None,
                agents_states: HashMap::from([(
                    robie_id.to_string(),
                    agent_state(CollabAgentStatus::Interrupted, /*message*/ None),
                )]),

                wake_notifications: None,
                completion_reason: None,
            },
            /*cached_spawn_request*/ None,
            |thread_id| metadata_for(thread_id, robie_id, ThreadId::new()),
        )
        .expect("resume item renders");

        assert_snapshot!("collab_resume_interrupted", cell_to_text(&cell));
    }

    fn agent_state(status: CollabAgentStatus, message: Option<&str>) -> CollabAgentState {
        CollabAgentState {
            status,
            message: message.map(str::to_string),
        }
    }

    fn spawn_item(
        status: CollabAgentToolCallStatus,
        model: Option<&str>,
        reasoning_effort: Option<ReasoningEffortConfig>,
        requested_model: Option<&str>,
        requested_reasoning_effort: Option<ReasoningEffortConfig>,
    ) -> ThreadItem {
        ThreadItem::CollabAgentToolCall {
            id: "call-spawn".to_string(),
            tool: CollabAgentTool::SpawnAgent,
            status,
            sender_thread_id: ThreadId::new().to_string(),
            receiver_thread_ids: Vec::new(),
            prompt: None,
            model: model.map(str::to_string),
            reasoning_effort,
            requested_model: requested_model.map(str::to_string),
            requested_reasoning_effort,
            effective_model: None,
            effective_reasoning_effort: None,
            agents_states: HashMap::new(),

            wake_notifications: None,
            completion_reason: None,
        }
    }

    fn metadata_for(thread_id: ThreadId, robie_id: ThreadId, bob_id: ThreadId) -> AgentMetadata {
        if thread_id == robie_id {
            AgentMetadata {
                agent_nickname: Some("Robie".to_string()),
                agent_role: Some("explorer".to_string()),
                ..AgentMetadata::default()
            }
        } else if thread_id == bob_id {
            AgentMetadata {
                agent_nickname: Some("Bob".to_string()),
                agent_role: Some("worker".to_string()),
                ..AgentMetadata::default()
            }
        } else {
            AgentMetadata::default()
        }
    }

    fn cell_to_text(cell: &PlainHistoryCell) -> String {
        cell.display_lines(/*width*/ 200)
            .iter()
            .map(line_to_text)
            .collect::<Vec<_>>()
            .join("\n")
    }

    fn line_to_text(line: &Line<'static>) -> String {
        line.spans
            .iter()
            .map(|span| span.content.as_ref())
            .collect::<Vec<_>>()
            .join("")
    }
}
