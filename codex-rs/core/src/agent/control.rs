use crate::agent::AgentStatus;
use crate::agent::registry::AgentMetadata;
use crate::agent::registry::AgentRegistry;
use crate::agent::registry::SpawnPublicationDecision;
use crate::agent::registry::SpawnPublicationKey;
use crate::agent::role::DEFAULT_ROLE_NAME;
use crate::agent::role::resolve_role_config;
use crate::agent::status::is_final;
use crate::agent_communication::AgentCommunicationContext;
use crate::agent_communication::AgentCommunicationKind;
use crate::codex_thread::CodexThread;
use crate::codex_thread::ThreadConfigSnapshot;
use crate::config::Config;
use crate::config::RolloutBudgetConfig;
use crate::context::world_state::SubagentContext;
use crate::context::world_state::SubagentContextBuilder;
use crate::context::world_state::SubagentContextRow;
use crate::environment_selection::TurnEnvironmentSnapshot;
use crate::rollout_budget::RolloutBudget;
use crate::session::emit_subagent_session_started;
use crate::session_prefix::format_inter_agent_completion_message;
use crate::session_prefix::format_subagent_notification_message;
use crate::state_db;
use crate::thread_manager::RemoveThreadIfSameResult;
use crate::thread_manager::ResumeThreadWithHistoryOptions;
use crate::thread_manager::ThreadManagerState;
use crate::thread_rollout_truncation::truncate_rollout_to_last_n_fork_turns;
use codex_protocol::AgentPath;
use codex_protocol::SessionId;
use codex_protocol::ThreadId;
use codex_protocol::error::CodexErr;
use codex_protocol::error::CodexErrorDetails;
use codex_protocol::error::Result as CodexResult;
use codex_protocol::models::ContentItem;
use codex_protocol::models::MessagePhase;
use codex_protocol::openai_models::ReasoningEffort;
use codex_protocol::protocol::EventMsg;
use codex_protocol::protocol::InitialHistory;
use codex_protocol::protocol::InterAgentCommunication;
use codex_protocol::protocol::MultiAgentVersion;
use codex_protocol::protocol::Op;
use codex_protocol::protocol::ResumedHistory;
use codex_protocol::protocol::RolloutItem;
use codex_protocol::protocol::SessionSource;
use codex_protocol::protocol::SubAgentSource;
use codex_protocol::protocol::ThreadHistoryMode;
use codex_protocol::protocol::ThreadSource;
use codex_protocol::protocol::TurnEnvironmentSelection;
use codex_protocol::user_input::UserInput;
use codex_state::DirectionalThreadSpawnEdgeStatus;
use codex_thread_store::LoadThreadHistoryParams;
use codex_thread_store::ReadThreadParams;
use serde::Deserialize;
use serde::Serialize;
use std::collections::HashMap;
use std::collections::HashSet;
use std::collections::VecDeque;
use std::sync::Arc;
use std::sync::Weak;
#[cfg(test)]
use std::sync::atomic::AtomicBool;
#[cfg(test)]
use std::sync::atomic::Ordering;
use tokio::sync::watch;
use tracing::warn;

pub(crate) use self::execution::AgentExecutionGuard;
use self::execution::AgentExecutionLimiter;
use self::residency::V2Residency;

pub(crate) const SUBAGENT_IDENTITY_SOURCE_THREAD_CONFIG_SNAPSHOT: &str = "thread_config_snapshot";
const INSPECT_AGENT_TREE_STATE_DB_UNAVAILABLE_MESSAGE: &str = concat!(
    "inspect_agent_tree cannot include stale descendants because this session has no configured ",
    "state_db. Retry with scope=\"live\" for live-only inspection. For a completed sidecar, use ",
    "$subagent-session-tail with the child thread id (`inspect_subagent_tail.py --child-thread-id ",
    "<child-thread-id>`), or with parent thread id plus the exact agent_path if the child id is ",
    "unavailable."
);
mod execution;
mod legacy;
mod lifecycle;
mod residency;
mod spawn;

#[cfg(test)]
use self::spawn::UnpublishedSpawnReconciliation;
#[cfg(test)]
use self::spawn::unpublished_spawn_reconciliation;

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum SpawnAgentForkMode {
    FullHistory,
    LastNTurns(usize),
}

#[derive(Clone, Debug, Default)]
pub(crate) struct SpawnAgentOptions {
    pub(crate) fork_parent_spawn_call_id: Option<String>,
    pub(crate) fork_mode: Option<SpawnAgentForkMode>,
    pub(crate) parent_thread_id: Option<ThreadId>,
    pub(crate) environments: Option<Vec<TurnEnvironmentSelection>>,
    /// The call that owns the parent-visible publication decision for a tool spawn.
    ///
    /// Direct control-plane callers leave this unset. Tool runtime dispatch creates the matching
    /// record before the handler begins, so cancellation and child publication share one winner.
    pub(crate) spawn_call_id: Option<String>,
}

#[derive(Clone, Debug)]
pub(crate) struct LiveAgent {
    pub(crate) thread_id: ThreadId,
    pub(crate) metadata: AgentMetadata,
    pub(crate) status: AgentStatus,
}

/// Internal inventory snapshot for a spawned sub-agent.
///
/// `status` is the live agent state, while `effective_*` and `identity_source`
/// are resolved from the config snapshot used to reconstruct the agent record.
#[derive(Debug, Clone)]
pub(crate) struct SubAgentInventoryInfo {
    pub(crate) nickname: Option<String>,
    pub(crate) role: Option<String>,
    pub(crate) status: AgentStatus,
    pub(crate) effective_model: Option<String>,
    pub(crate) effective_reasoning_effort: Option<ReasoningEffort>,
    pub(crate) effective_model_provider_id: String,
    pub(crate) identity_source: String,
}

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
pub(crate) struct ListedAgent {
    pub(crate) agent_name: String,
    pub(crate) agent_status: AgentStatus,
    pub(crate) has_active_subagents: bool,
    pub(crate) active_subagent_count: usize,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub(crate) enum AgentTreeScope {
    Live,
    Stale,
    All,
}

#[derive(Clone, Copy, Debug, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub(crate) enum AgentSessionState {
    Live,
    Stale,
}

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
pub(crate) struct AgentTreeSummary {
    pub(crate) total_agents: usize,
    pub(crate) live_agents: usize,
    pub(crate) stale_agents: usize,
    pub(crate) pending_init_agents: usize,
    pub(crate) running_agents: usize,
    pub(crate) interrupted_agents: usize,
    pub(crate) completed_agents: usize,
    pub(crate) errored_agents: usize,
    pub(crate) shutdown_agents: usize,
    pub(crate) not_found_agents: usize,
}

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
pub(crate) struct AgentTreeNode {
    pub(crate) agent_name: String,
    pub(crate) depth: usize,
    pub(crate) session_state: AgentSessionState,
    pub(crate) agent_status: Option<AgentStatus>,
    pub(crate) nickname: Option<String>,
    pub(crate) role: Option<String>,
    /// Effective configuration read from the live thread config snapshot.
    ///
    /// These fields are intentionally absent for stale threads: persisted agent metadata does
    /// not contain a configuration snapshot, and a tree receipt must not infer one.
    pub(crate) effective_model: Option<String>,
    pub(crate) effective_reasoning_effort: Option<ReasoningEffort>,
    pub(crate) direct_child_count: usize,
    pub(crate) descendant_count: usize,
}

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
pub struct AgentTreeInspection {
    pub(crate) root_agent_name: String,
    pub(crate) scope_applied: AgentTreeScope,
    pub(crate) agent_roots_applied: Vec<String>,
    pub(crate) max_depth_applied: usize,
    pub(crate) max_agents_applied: usize,
    pub(crate) truncated: bool,
    pub(crate) summary: AgentTreeSummary,
    pub(crate) agents: Vec<AgentTreeNode>,
}

#[derive(Clone, Debug)]
struct AgentTreeRecord {
    agent_name: String,
    session_state: AgentSessionState,
    agent_status: Option<AgentStatus>,
    nickname: Option<String>,
    role: Option<String>,
    effective_model: Option<String>,
    effective_reasoning_effort: Option<ReasoningEffort>,
}

/// Control-plane handle for multi-agent operations.
/// `AgentControl` is held by each session (via `SessionServices`). It provides capability to
/// spawn new agents and the inter-agent communication layer.
/// An `AgentControl` instance is intended to be created at most once per root thread/session
/// tree. That same `AgentControl` is then shared with every sub-agent spawned from that root,
/// which keeps the registry scoped to that root thread rather than the entire `ThreadManager`.
#[derive(Clone, Default)]
pub(crate) struct AgentControl {
    /// ID shared by the whole agent control session. This means every sub-agents from a common
    /// root share the same session ID.
    session_id: SessionId,
    /// Weak handle back to the global thread registry/state.
    /// This is `Weak` to avoid reference cycles and shadow persistence of the form
    /// `ThreadManagerState -> CodexThread -> Session -> SessionServices -> ThreadManagerState`.
    manager: Weak<ThreadManagerState>,
    state: Arc<AgentRegistry>,
    v2_residency: Arc<V2Residency>,
    agent_execution_limiter: Arc<AgentExecutionLimiter>,
    /// Session-scoped state shared by the root thread and every cloned sub-agent control handle.
    rollout_budget: Arc<RolloutBudget>,
    #[cfg(test)]
    spawn_test_hooks: Arc<SpawnTestHooks>,
    #[cfg(test)]
    hide_next_agent_config_snapshot: Arc<tokio::sync::Mutex<bool>>,
}

/// Deterministic race and failure controls for the post-creation spawn boundary.
///
/// These live on `AgentControl`, rather than the manager, because every spawned child shares the
/// same control and publication registry as its parent. Production builds have no hook surface.
#[cfg(test)]
#[derive(Default)]
struct SpawnTestHooks {
    after_new_thread: std::sync::Mutex<Option<AfterNewThreadTestHook>>,
    after_initial_delivery: std::sync::Mutex<Option<AfterNewThreadTestHook>>,
    fail_unpublished_shutdown_once: AtomicBool,
    retained_unpublished_cleanup: std::sync::Mutex<Option<RetainedUnpublishedCleanupTestHook>>,
}

#[cfg(test)]
struct AfterNewThreadTestHook {
    observed_child: tokio::sync::oneshot::Sender<ThreadId>,
    resume_spawn: Arc<tokio::sync::Notify>,
}

#[cfg(test)]
struct RetainedUnpublishedCleanupTestHook {
    observed_child: tokio::sync::oneshot::Sender<ThreadId>,
    resume_cleanup: Arc<tokio::sync::Notify>,
}

impl AgentControl {
    /// Construct a new `AgentControl` that can spawn/message agents via the given manager state.
    pub(crate) fn new(
        manager: Weak<ThreadManagerState>,
        rollout_budget: Option<RolloutBudgetConfig>,
    ) -> Self {
        let control = Self {
            manager,
            ..Default::default()
        };
        if let Some(rollout_budget) = rollout_budget {
            control.rollout_budget.configure(rollout_budget);
        }
        control
    }

    pub(crate) fn with_session_id(mut self, session_id: SessionId, max_threads: usize) -> Self {
        self.session_id = session_id;
        self.agent_execution_limiter.initialize(max_threads);
        self
    }

    pub(crate) fn session_id(&self) -> SessionId {
        self.session_id
    }

    pub(crate) fn rollout_budget(&self) -> &RolloutBudget {
        self.rollout_budget.as_ref()
    }

    pub(crate) fn begin_tool_spawn_publication(&self, parent_thread_id: ThreadId, call_id: &str) {
        self.state
            .begin_spawn_publication(SpawnPublicationKey::new(parent_thread_id, call_id));
    }

    pub(crate) fn cancel_tool_spawn_publication(
        &self,
        parent_thread_id: ThreadId,
        call_id: &str,
    ) -> SpawnPublicationDecision {
        self.state
            .cancel_spawn_publication(SpawnPublicationKey::new(parent_thread_id, call_id))
    }

    pub(crate) fn finish_tool_spawn_publication(&self, parent_thread_id: ThreadId, call_id: &str) {
        self.state
            .finish_spawn_publication(&SpawnPublicationKey::new(parent_thread_id, call_id));
    }

    #[cfg(test)]
    pub(crate) fn tool_spawn_publication_decision_for_test(
        &self,
        parent_thread_id: ThreadId,
        call_id: &str,
    ) -> SpawnPublicationDecision {
        self.state
            .spawn_publication_decision(&SpawnPublicationKey::new(parent_thread_id, call_id))
    }

    #[cfg(test)]
    pub(crate) fn v2_resident_count_for_test(&self) -> usize {
        self.v2_residency.resident_count()
    }

    #[cfg(test)]
    pub(crate) fn spawn_capacity_and_path_are_available_for_test(
        &self,
        max_threads: usize,
        agent_path: &AgentPath,
    ) -> bool {
        let Ok(mut reservation) = self.state.reserve_spawn_slot(Some(max_threads)) else {
            return false;
        };
        reservation.reserve_agent_path(agent_path).is_ok()
    }

    #[cfg(test)]
    pub(crate) fn pause_spawn_after_new_thread_for_test(
        &self,
    ) -> (
        tokio::sync::oneshot::Receiver<ThreadId>,
        Arc<tokio::sync::Notify>,
    ) {
        let (observed_child, child_created) = tokio::sync::oneshot::channel();
        let resume_spawn = Arc::new(tokio::sync::Notify::new());
        *self
            .spawn_test_hooks
            .after_new_thread
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner) = Some(AfterNewThreadTestHook {
            observed_child,
            resume_spawn: Arc::clone(&resume_spawn),
        });
        (child_created, resume_spawn)
    }

    #[cfg(test)]
    pub(crate) fn pause_spawn_after_initial_delivery_for_test(
        &self,
    ) -> (
        tokio::sync::oneshot::Receiver<ThreadId>,
        Arc<tokio::sync::Notify>,
    ) {
        let (observed_child, initial_delivery_finished) = tokio::sync::oneshot::channel();
        let resume_spawn = Arc::new(tokio::sync::Notify::new());
        *self
            .spawn_test_hooks
            .after_initial_delivery
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner) = Some(AfterNewThreadTestHook {
            observed_child,
            resume_spawn: Arc::clone(&resume_spawn),
        });
        (initial_delivery_finished, resume_spawn)
    }

    #[cfg(test)]
    pub(crate) fn fail_next_unpublished_spawn_shutdown_for_test(&self) {
        self.spawn_test_hooks
            .fail_unpublished_shutdown_once
            .store(true, Ordering::Release);
    }

    #[cfg(test)]
    pub(crate) fn pause_retained_unpublished_spawn_cleanup_for_test(
        &self,
    ) -> (
        tokio::sync::oneshot::Receiver<ThreadId>,
        Arc<tokio::sync::Notify>,
    ) {
        let (observed_child, cleanup_retained) = tokio::sync::oneshot::channel();
        let resume_cleanup = Arc::new(tokio::sync::Notify::new());
        *self
            .spawn_test_hooks
            .retained_unpublished_cleanup
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner) =
            Some(RetainedUnpublishedCleanupTestHook {
                observed_child,
                resume_cleanup: Arc::clone(&resume_cleanup),
            });
        (cleanup_retained, resume_cleanup)
    }

    #[cfg(test)]
    pub(crate) async fn await_after_new_thread_test_hook(&self, child_thread_id: ThreadId) {
        let hook = self
            .spawn_test_hooks
            .after_new_thread
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .take();
        if let Some(hook) = hook {
            let _ = hook.observed_child.send(child_thread_id);
            hook.resume_spawn.notified().await;
        }
    }

    #[cfg(test)]
    pub(crate) async fn await_after_initial_delivery_test_hook(&self, child_thread_id: ThreadId) {
        let hook = self
            .spawn_test_hooks
            .after_initial_delivery
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .take();
        if let Some(hook) = hook {
            let _ = hook.observed_child.send(child_thread_id);
            hook.resume_spawn.notified().await;
        }
    }

    #[cfg(test)]
    pub(crate) async fn await_retained_unpublished_spawn_cleanup_test_hook(
        &self,
        child_thread_id: ThreadId,
    ) {
        let hook = self
            .spawn_test_hooks
            .retained_unpublished_cleanup
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .take();
        if let Some(hook) = hook {
            let _ = hook.observed_child.send(child_thread_id);
            hook.resume_cleanup.notified().await;
        }
    }

    #[cfg(test)]
    pub(crate) fn take_unpublished_shutdown_failure_for_test(&self) -> bool {
        self.spawn_test_hooks
            .fail_unpublished_shutdown_once
            .swap(false, Ordering::AcqRel)
    }

    #[cfg(test)]
    pub(crate) async fn hide_next_agent_config_snapshot(&self) {
        *self.hide_next_agent_config_snapshot.lock().await = true;
    }

    /// Send rich user input items to an existing agent thread.
    pub(crate) async fn send_input(
        &self,
        agent_id: ThreadId,
        input: Vec<UserInput>,
    ) -> CodexResult<String> {
        let state = self.upgrade()?;
        self.ensure_execution_capacity_for_turn_start(agent_id, /*starts_turn*/ true)
            .await?;
        self.send_input_after_capacity_check(agent_id, &state, input)
            .await
    }

    async fn send_input_after_capacity_check(
        &self,
        agent_id: ThreadId,
        state: &Arc<ThreadManagerState>,
        input: Vec<UserInput>,
    ) -> CodexResult<String> {
        let (thread, result) = state.send_op_with_thread(agent_id, input.into()).await;
        self.handle_thread_request_result(agent_id, state, thread.as_ref(), result)
            .await
    }

    pub(crate) async fn send_inter_agent_communication(
        &self,
        agent_id: ThreadId,
        communication: InterAgentCommunication,
        agent_communication_context: AgentCommunicationContext,
    ) -> CodexResult<String> {
        let state = self.upgrade()?;
        self.ensure_execution_capacity_for_turn_start(agent_id, communication.trigger_turn)
            .await?;
        self.send_inter_agent_communication_after_capacity_check(
            agent_id,
            &state,
            communication,
            agent_communication_context,
        )
        .await
    }

    async fn send_inter_agent_communication_after_capacity_check(
        &self,
        agent_id: ThreadId,
        state: &Arc<ThreadManagerState>,
        communication: InterAgentCommunication,
        context: AgentCommunicationContext,
    ) -> CodexResult<String> {
        self.submit_inter_agent_communication(agent_id, state, communication, context)
            .await
    }

    async fn submit_inter_agent_communication(
        &self,
        agent_id: ThreadId,
        state: &Arc<ThreadManagerState>,
        communication: InterAgentCommunication,
        context: AgentCommunicationContext,
    ) -> CodexResult<String> {
        if self.uses_v2_lifecycle(state, agent_id).await {
            return self
                .prepare_v2_agent_delivery(agent_id)
                .await?
                .send_after_capacity_check(communication, context, /*interrupt*/ false)
                .await;
        }
        let communication_for_log =
            crate::agent_communication::logging_enabled().then(|| communication.clone());
        let (thread, result) = state
            .send_op_with_thread(agent_id, Op::InterAgentCommunication { communication })
            .await;
        let result = self
            .handle_thread_request_result(agent_id, state, thread.as_ref(), result)
            .await;
        if let (Some(communication), Ok(communication_id)) =
            (communication_for_log, result.as_ref())
        {
            crate::agent_communication::emit_agent_communication_send(
                communication_id,
                &context,
                &communication,
                agent_id,
            );
        }
        result
    }

    /// Interrupt the current task for an existing agent thread.
    pub(crate) async fn interrupt_agent(&self, agent_id: ThreadId) -> CodexResult<String> {
        let state = self.upgrade()?;
        let (thread, result) = state.send_op_with_thread(agent_id, Op::Interrupt).await;
        self.handle_thread_request_result(agent_id, &state, thread.as_ref(), result)
            .await
    }

    pub(crate) async fn submit_to_current_thread(
        &self,
        expected_thread: &CodexThread,
        op: Op,
    ) -> CodexResult<String> {
        let state = self.upgrade()?;
        state
            .send_op_to_expected_thread_with_trace(
                expected_thread.session.thread_id(),
                expected_thread,
                op,
                /*trace*/ None,
            )
            .await
    }

    async fn handle_thread_request_result(
        &self,
        agent_id: ThreadId,
        state: &Arc<ThreadManagerState>,
        expected_thread: Option<&Arc<CodexThread>>,
        result: CodexResult<String>,
    ) -> CodexResult<String> {
        if result
            .as_ref()
            .is_err_and(|err| matches!(err.details(), CodexErrorDetails::InternalAgentDied))
            && let Some(expected_thread) = expected_thread
        {
            let registry = Arc::clone(&self.state);
            let removal = state
                .remove_thread_if_same(&agent_id, expected_thread, || {
                    if registry
                        .cold_status(agent_id, Some(expected_thread))
                        .is_none()
                    {
                        registry.release_spawned_thread(agent_id);
                    }
                })
                .await;
            if removal == RemoveThreadIfSameResult::Removed {
                self.forget_v2_residency(agent_id);
            }
        }
        result
    }

    /// Fetch the last known status for `agent_id`, returning `NotFound` when unavailable.
    pub(crate) async fn get_status(&self, agent_id: ThreadId) -> AgentStatus {
        let Ok(state) = self.upgrade() else {
            return self
                .state
                .cold_status(agent_id, /*live_thread*/ None)
                .unwrap_or(AgentStatus::NotFound);
        };
        let thread = match state.get_thread(agent_id).await {
            Ok(thread) => thread,
            Err(_) => {
                let cold_status = self.state.cold_status(agent_id, /*live_thread*/ None);
                match state.get_thread(agent_id).await {
                    Ok(thread) => thread,
                    Err(_) => return cold_status.unwrap_or(AgentStatus::NotFound),
                }
            }
        };
        if let Some(status) = self.state.cold_status(agent_id, Some(&thread)) {
            return status;
        }
        let status = thread.agent_status().await;
        self.state
            .cold_status(agent_id, Some(&thread))
            .unwrap_or(status)
    }

    pub(crate) fn register_session_root(
        &self,
        current_thread_id: ThreadId,
        current_parent_thread_id: Option<ThreadId>,
    ) {
        if current_parent_thread_id.is_none() {
            self.state.register_root_thread(current_thread_id);
        }
    }

    pub(crate) fn get_agent_metadata(&self, agent_id: ThreadId) -> Option<AgentMetadata> {
        self.state.agent_metadata_for_thread(agent_id)
    }

    pub(crate) fn ensure_agent_known(&self, agent_id: ThreadId) -> CodexResult<AgentMetadata> {
        self.state
            .agent_metadata_for_thread(agent_id)
            .ok_or_else(|| CodexErr::ThreadNotFound(agent_id))
    }

    pub(crate) async fn get_live_agent_inventory_info(
        &self,
        thread_id: ThreadId,
    ) -> Option<SubAgentInventoryInfo> {
        let state = self.upgrade().ok()?;
        let thread = state.get_thread(thread_id).await.ok()?;
        let snapshot = thread.config_snapshot().await;
        let SessionSource::SubAgent(SubAgentSource::ThreadSpawn {
            agent_nickname,
            agent_role,
            ..
        }) = snapshot.session_source
        else {
            return None;
        };

        Some(SubAgentInventoryInfo {
            nickname: agent_nickname,
            role: agent_role,
            status: thread.agent_status().await,
            effective_model: Some(snapshot.model),
            effective_reasoning_effort: snapshot.reasoning_effort,
            effective_model_provider_id: snapshot.model_provider_id,
            identity_source: SUBAGENT_IDENTITY_SOURCE_THREAD_CONFIG_SNAPSHOT.to_string(),
        })
    }

    pub(crate) async fn get_agent_config_snapshot(
        &self,
        agent_id: ThreadId,
    ) -> Option<ThreadConfigSnapshot> {
        #[cfg(test)]
        if std::mem::take(&mut *self.hide_next_agent_config_snapshot.lock().await) {
            return None;
        }
        let Ok(state) = self.upgrade() else {
            return None;
        };
        let Ok(thread) = state.get_thread(agent_id).await else {
            return None;
        };
        Some(thread.config_snapshot().await)
    }

    pub(crate) async fn resolve_agent_reference(
        &self,
        _current_thread_id: ThreadId,
        current_session_source: &SessionSource,
        agent_reference: &str,
    ) -> CodexResult<ThreadId> {
        let current_agent_path = current_session_source
            .get_agent_path()
            .unwrap_or_else(AgentPath::root);
        let agent_path = current_agent_path
            .resolve(agent_reference)
            .map_err(CodexErr::UnsupportedOperation)?;
        if let Some(thread_id) = self.state.agent_id_for_path(&agent_path) {
            return Ok(thread_id);
        }
        Err(CodexErr::UnsupportedOperation(format!(
            "live agent path `{}` not found",
            agent_path.as_str()
        )))
    }

    pub(crate) fn agent_id_for_path(&self, agent_path: &AgentPath) -> Option<ThreadId> {
        self.state.agent_id_for_path(agent_path)
    }

    /// Subscribe to status updates for `agent_id`, yielding the latest value and changes.
    pub(crate) async fn subscribe_status(
        &self,
        agent_id: ThreadId,
    ) -> CodexResult<watch::Receiver<AgentStatus>> {
        let state = self.upgrade()?;
        let thread = match state.get_thread(agent_id).await {
            Ok(thread) => thread,
            Err(err) => {
                let cold_status = self.state.cold_status(agent_id, /*live_thread*/ None);
                match state.get_thread(agent_id).await {
                    Ok(thread) => thread,
                    Err(_) => {
                        return cold_status
                            .map(|status| watch::channel(status).1)
                            .ok_or(err);
                    }
                }
            }
        };
        if let Some(status) = self.state.cold_status(agent_id, Some(&thread)) {
            return Ok(watch::channel(status).1);
        }
        let status = thread.subscribe_status();
        Ok(self
            .state
            .cold_status(agent_id, Some(&thread))
            .map(|status| watch::channel(status).1)
            .unwrap_or(status))
    }

    pub(crate) async fn format_environment_context_subagents(
        &self,
        parent_thread_id: ThreadId,
    ) -> SubagentContext {
        let Ok(agents) = self.open_thread_spawn_children(parent_thread_id).await else {
            return SubagentContext::default();
        };

        let mut builder = SubagentContextBuilder::default();
        let mut agents = agents.into_iter();
        while let Some((thread_id, metadata)) = agents.next() {
            if !builder.has_row_capacity() {
                builder.note_omitted(1 + agents.len());
                break;
            }
            let row = if let Some(agent_path) = metadata.agent_path.as_ref() {
                SubagentContextRow::new(agent_path.name(), metadata.agent_nickname.as_deref())
            } else {
                let reference = thread_id.to_string();
                SubagentContextRow::new(reference.as_str(), metadata.agent_nickname.as_deref())
            };
            if !builder.push(row) {
                builder.note_omitted(1 + agents.len());
                break;
            }
        }
        builder.finish()
    }

    pub(crate) async fn list_agents(
        &self,
        current_session_source: &SessionSource,
        path_prefix: Option<&str>,
    ) -> CodexResult<Vec<ListedAgent>> {
        let state = self.upgrade()?;
        let live_children_by_parent = self.live_thread_spawn_children().await?;
        let resolved_prefix = path_prefix
            .map(|prefix| {
                current_session_source
                    .get_agent_path()
                    .unwrap_or_else(AgentPath::root)
                    .resolve(prefix)
                    .map_err(CodexErr::UnsupportedOperation)
            })
            .transpose()?;

        let mut live_agents = self.state.live_agents();
        live_agents.sort_by(|left, right| {
            left.agent_path
                .as_deref()
                .unwrap_or_default()
                .cmp(right.agent_path.as_deref().unwrap_or_default())
                .then_with(|| {
                    left.agent_id
                        .map(|id| id.to_string())
                        .unwrap_or_default()
                        .cmp(&right.agent_id.map(|id| id.to_string()).unwrap_or_default())
                })
        });

        let root_path = AgentPath::root();
        let mut listed_rows = Vec::with_capacity(live_agents.len().saturating_add(1));
        let mut status_by_thread_id = HashMap::<ThreadId, AgentStatus>::new();
        if let Some(root_thread_id) = self.state.agent_id_for_path(&root_path)
            && let Ok(root_thread) = state.get_thread(root_thread_id).await
        {
            let root_status = root_thread.agent_status().await;
            status_by_thread_id.insert(root_thread_id, root_status.clone());
            if resolved_prefix
                .as_ref()
                .is_none_or(|prefix| agent_matches_prefix(Some(&root_path), prefix))
            {
                listed_rows.push((root_thread_id, root_path.to_string(), root_status));
            }
        }

        for metadata in live_agents {
            let Some(thread_id) = metadata.agent_id else {
                continue;
            };
            let agent_status = self.get_status(thread_id).await;
            if agent_status == AgentStatus::NotFound {
                continue;
            }
            status_by_thread_id.insert(thread_id, agent_status.clone());
            if resolved_prefix
                .as_ref()
                .is_some_and(|prefix| !agent_matches_prefix(metadata.agent_path.as_ref(), prefix))
            {
                continue;
            }
            let agent_name = metadata
                .agent_path
                .as_ref()
                .map(ToString::to_string)
                .unwrap_or_else(|| thread_id.to_string());
            listed_rows.push((thread_id, agent_name, agent_status));
        }

        let mut active_descendant_counts = HashMap::<ThreadId, usize>::new();
        let agents = listed_rows
            .into_iter()
            .map(|(thread_id, agent_name, agent_status)| {
                let active_subagent_count = compute_active_live_descendant_count(
                    thread_id,
                    &live_children_by_parent,
                    &status_by_thread_id,
                    &mut active_descendant_counts,
                );
                ListedAgent {
                    agent_name,
                    agent_status,
                    has_active_subagents: active_subagent_count > 0,
                    active_subagent_count,
                }
            })
            .collect::<Vec<_>>();

        Ok(agents)
    }

    #[allow(clippy::too_many_arguments)]
    pub(crate) async fn inspect_agent_tree(
        &self,
        current_thread_id: ThreadId,
        current_session_source: &SessionSource,
        target: Option<&str>,
        agent_roots: Option<&[String]>,
        scope: AgentTreeScope,
        max_depth: usize,
        max_agents: usize,
    ) -> CodexResult<AgentTreeInspection> {
        let state = self.upgrade()?;
        let current_thread = state.get_thread(current_thread_id).await?;
        let state_db_ctx = current_thread.state_db();
        let root_live_thread_id = self
            .state
            .agent_id_for_path(&AgentPath::root())
            .unwrap_or(current_thread_id);
        let target_path = target
            .map(|reference| {
                current_session_source
                    .get_agent_path()
                    .unwrap_or_else(AgentPath::root)
                    .resolve(reference)
                    .map_err(CodexErr::UnsupportedOperation)
            })
            .transpose()?;
        let filter_base_path = target_path.clone().unwrap_or_else(|| {
            current_session_source
                .get_agent_path()
                .unwrap_or_else(AgentPath::root)
        });

        if !matches!(scope, AgentTreeScope::Live) && state_db_ctx.is_none() {
            return Err(inspect_agent_tree_state_db_unavailable());
        }

        let (tree_root_thread_id, tree_root_session_state) = match target_path.as_ref() {
            Some(target_path) => {
                if let Some(thread_id) = self.state.agent_id_for_path(target_path) {
                    (thread_id, AgentSessionState::Live)
                } else {
                    let Some(state_db_ctx) = state_db_ctx.as_ref() else {
                        return Err(CodexErr::UnsupportedOperation(format!(
                            "agent path `{}` not found in the live tree",
                            target_path.as_str()
                        )));
                    };
                    let thread_id = if target_path.is_root() {
                        Some(root_live_thread_id)
                    } else {
                        state_db_ctx
                            .find_thread_spawn_descendant_by_path(
                                root_live_thread_id,
                                target_path.as_str(),
                            )
                            .await
                            .map_err(|err| {
                                CodexErr::Fatal(format!(
                                    "failed to inspect persisted agent path `{}`: {err}",
                                    target_path.as_str()
                                ))
                            })?
                    }
                    .ok_or_else(|| {
                        CodexErr::UnsupportedOperation(format!(
                            "agent path `{}` not found",
                            target_path.as_str()
                        ))
                    })?;
                    (thread_id, AgentSessionState::Stale)
                }
            }
            None => (current_thread_id, AgentSessionState::Live),
        };
        let tree_root_name = match tree_root_session_state {
            AgentSessionState::Live => self
                .state
                .agent_metadata_for_thread(tree_root_thread_id)
                .and_then(|metadata| metadata.agent_path.map(|agent_path| agent_path.to_string()))
                .unwrap_or_else(|| tree_root_thread_id.to_string()),
            AgentSessionState::Stale => {
                let Some(state_db_ctx) = state_db_ctx.as_ref() else {
                    return Err(inspect_agent_tree_state_db_unavailable());
                };
                state_db_ctx
                    .get_thread(tree_root_thread_id)
                    .await
                    .map_err(|err| {
                        CodexErr::Fatal(format!(
                            "failed to inspect stale agent metadata for {tree_root_thread_id}: {err}"
                        ))
                    })?
                    .and_then(|metadata| metadata.agent_path)
                    .unwrap_or_else(|| tree_root_thread_id.to_string())
            }
        };
        let agent_roots_applied = agent_roots
            .map(|references| {
                references
                    .iter()
                    .map(|reference| {
                        filter_base_path
                            .resolve(reference)
                            .map_err(CodexErr::UnsupportedOperation)
                    })
                    .collect::<CodexResult<Vec<_>>>()
            })
            .transpose()?
            .unwrap_or_default();
        for agent_root in &agent_roots_applied {
            if !agent_name_is_same_or_descendant_of(agent_root.as_str(), tree_root_name.as_str()) {
                return Err(CodexErr::UnsupportedOperation(format!(
                    "agent_roots entry `{}` is outside inspected subtree `{}`",
                    agent_root.as_str(),
                    tree_root_name
                )));
            }
        }

        let live_children_by_parent = if matches!(scope, AgentTreeScope::Stale) {
            None
        } else {
            Some(self.live_thread_spawn_children().await?)
        };
        let mut queue = VecDeque::from([(tree_root_thread_id, tree_root_session_state, 0usize)]);
        let mut depth_by_thread_id = HashMap::<ThreadId, usize>::new();
        let mut tree_children = HashMap::<ThreadId, Vec<ThreadId>>::new();
        let mut tree_records = HashMap::<ThreadId, AgentTreeRecord>::new();

        while let Some((thread_id, session_state, depth)) = queue.pop_front() {
            if tree_records.contains_key(&thread_id) {
                continue;
            }

            let record = self
                .load_agent_tree_record(&state, state_db_ctx.as_ref(), thread_id, session_state)
                .await?;
            depth_by_thread_id.insert(thread_id, depth);

            let child_states = self
                .tree_child_session_states(
                    live_children_by_parent.as_ref(),
                    state_db_ctx.as_ref(),
                    thread_id,
                    scope,
                )
                .await?;
            let mut child_ids = child_states.keys().copied().collect::<Vec<_>>();
            child_ids.sort_by_key(std::string::ToString::to_string);
            tree_children.insert(thread_id, child_ids.clone());
            tree_records.insert(thread_id, record);

            for child_id in child_ids {
                if let Some(child_state) = child_states.get(&child_id).copied() {
                    queue.push_back((child_id, child_state, depth.saturating_add(1)));
                }
            }
        }

        for child_ids in tree_children.values_mut() {
            child_ids.sort_by(|left, right| {
                let left_name = tree_records
                    .get(left)
                    .map(|record| record.agent_name.as_str())
                    .unwrap_or_default();
                let right_name = tree_records
                    .get(right)
                    .map(|record| record.agent_name.as_str())
                    .unwrap_or_default();
                left_name
                    .cmp(right_name)
                    .then_with(|| left.to_string().cmp(&right.to_string()))
            });
        }

        let mut descendant_counts = HashMap::<ThreadId, usize>::new();
        compute_descendant_counts(tree_root_thread_id, &tree_children, &mut descendant_counts);

        let mut ordered_thread_ids = Vec::with_capacity(tree_records.len());
        let mut stack = vec![tree_root_thread_id];
        while let Some(thread_id) = stack.pop() {
            ordered_thread_ids.push(thread_id);
            if let Some(children) = tree_children.get(&thread_id) {
                for child_id in children.iter().rev().copied() {
                    stack.push(child_id);
                }
            }
        }

        let filtered_thread_ids = ordered_thread_ids
            .into_iter()
            .filter(|thread_id| {
                agent_roots_applied.is_empty()
                    || tree_records.get(thread_id).is_some_and(|record| {
                        agent_roots_applied.iter().any(|agent_root| {
                            agent_name_is_same_or_descendant_of(
                                record.agent_name.as_str(),
                                agent_root.as_str(),
                            )
                        })
                    })
            })
            .collect::<Vec<_>>();

        let mut summary = AgentTreeSummary {
            total_agents: filtered_thread_ids.len(),
            live_agents: 0,
            stale_agents: 0,
            pending_init_agents: 0,
            running_agents: 0,
            interrupted_agents: 0,
            completed_agents: 0,
            errored_agents: 0,
            shutdown_agents: 0,
            not_found_agents: 0,
        };

        for thread_id in &filtered_thread_ids {
            let Some(record) = tree_records.get(thread_id) else {
                continue;
            };
            match record.session_state {
                AgentSessionState::Live => summary.live_agents += 1,
                AgentSessionState::Stale => summary.stale_agents += 1,
            }
            match record.agent_status.as_ref() {
                Some(AgentStatus::PendingInit) => summary.pending_init_agents += 1,
                Some(AgentStatus::Running) => summary.running_agents += 1,
                Some(AgentStatus::Interrupted) => summary.interrupted_agents += 1,
                Some(AgentStatus::Completed { .. }) => summary.completed_agents += 1,
                Some(AgentStatus::Errored { .. }) => summary.errored_agents += 1,
                Some(AgentStatus::Shutdown) => summary.shutdown_agents += 1,
                Some(AgentStatus::NotFound) => summary.not_found_agents += 1,
                None => {}
            }
        }

        let filtered_count = filtered_thread_ids.len();
        let within_depth = filtered_thread_ids
            .into_iter()
            .filter(|thread_id| {
                depth_by_thread_id
                    .get(thread_id)
                    .copied()
                    .unwrap_or_default()
                    <= max_depth
            })
            .collect::<Vec<_>>();
        let within_depth_count = within_depth.len();
        let truncated = filtered_count > within_depth_count || within_depth_count > max_agents;
        let agents = within_depth
            .into_iter()
            .take(max_agents)
            .filter_map(|thread_id| {
                let record = tree_records.get(&thread_id)?;
                Some(AgentTreeNode {
                    agent_name: record.agent_name.clone(),
                    depth: depth_by_thread_id
                        .get(&thread_id)
                        .copied()
                        .unwrap_or_default(),
                    session_state: record.session_state,
                    agent_status: record.agent_status.clone(),
                    nickname: record.nickname.clone(),
                    role: record.role.clone(),
                    effective_model: record.effective_model.clone(),
                    effective_reasoning_effort: record.effective_reasoning_effort.clone(),
                    direct_child_count: tree_children.get(&thread_id).map_or(0, Vec::len),
                    descendant_count: descendant_counts.get(&thread_id).copied().unwrap_or(0),
                })
            })
            .collect::<Vec<_>>();
        let root_agent_name = tree_records
            .get(&tree_root_thread_id)
            .map(|record| record.agent_name.clone())
            .unwrap_or_else(|| tree_root_thread_id.to_string());

        Ok(AgentTreeInspection {
            root_agent_name,
            scope_applied: scope,
            agent_roots_applied: agent_roots_applied
                .into_iter()
                .map(|agent_root| agent_root.to_string())
                .collect(),
            max_depth_applied: max_depth,
            max_agents_applied: max_agents,
            truncated,
            summary,
            agents,
        })
    }

    /// Starts a detached watcher for sub-agents spawned from another thread.
    ///
    /// This is only enabled for `SubAgentSource::ThreadSpawn`, where a parent thread exists and
    /// can receive completion notifications.
    fn maybe_start_completion_watcher(
        &self,
        child_thread_id: ThreadId,
        session_source: Option<SessionSource>,
        child_reference: String,
        child_agent_path: Option<AgentPath>,
    ) {
        let Some(SessionSource::SubAgent(SubAgentSource::ThreadSpawn {
            parent_thread_id, ..
        })) = session_source
        else {
            return;
        };
        let control = self.clone();
        tokio::spawn(async move {
            let status = match control.subscribe_status(child_thread_id).await {
                Ok(mut status_rx) => {
                    let mut status = status_rx.borrow().clone();
                    while !is_final(&status) {
                        if status_rx.changed().await.is_err() {
                            status = control.get_status(child_thread_id).await;
                            break;
                        }
                        status = status_rx.borrow().clone();
                    }
                    status
                }
                Err(_) => control.get_status(child_thread_id).await,
            };
            if !is_final(&status) {
                return;
            }

            let Ok(state) = control.upgrade() else {
                return;
            };
            let child_thread = state.get_thread(child_thread_id).await.ok();
            let child_uses_multi_agent_v2 = match child_thread.as_ref() {
                Some(child_thread) => {
                    child_thread.multi_agent_version() == Some(MultiAgentVersion::V2)
                }
                None => true,
            };
            if child_agent_path.is_some() && child_uses_multi_agent_v2 {
                let Some(child_agent_path) = child_agent_path.clone() else {
                    return;
                };
                let Some(parent_agent_path) = child_agent_path
                    .as_str()
                    .rsplit_once('/')
                    .and_then(|(parent, _)| AgentPath::try_from(parent).ok())
                else {
                    return;
                };
                let Some(message) = format_inter_agent_completion_message(
                    parent_agent_path.clone(),
                    child_agent_path.clone(),
                    &status,
                ) else {
                    return;
                };
                let communication = InterAgentCommunication::new(
                    child_agent_path,
                    parent_agent_path,
                    Vec::new(),
                    message,
                    /*trigger_turn*/ false,
                );
                let context =
                    AgentCommunicationContext::new(AgentCommunicationKind::Result, child_thread_id);
                let _ = control
                    .send_inter_agent_communication(parent_thread_id, communication, context)
                    .await;
                return;
            }
            let message = format_subagent_notification_message(child_reference.as_str(), &status);
            let Ok(parent_thread) = state.get_thread(parent_thread_id).await else {
                return;
            };
            parent_thread
                .inject_user_message_without_turn(message)
                .await;
        });
    }

    fn prepare_agent_metadata(
        &self,
        reservation: &mut crate::agent::registry::SpawnReservation,
        config: &Config,
        agent_path: Option<AgentPath>,
        agent_role: Option<String>,
        preferred_agent_nickname: Option<String>,
    ) -> CodexResult<AgentMetadata> {
        if let Some(agent_path) = agent_path.as_ref() {
            reservation.reserve_agent_path(agent_path)?;
        }
        let candidate_names = spawn::agent_nickname_candidates(config, agent_role.as_deref());
        let candidate_name_refs: Vec<&str> = candidate_names.iter().map(String::as_str).collect();
        let agent_nickname = Some(reservation.reserve_agent_nickname_with_preference(
            &candidate_name_refs,
            preferred_agent_nickname.as_deref(),
        )?);
        Ok(AgentMetadata {
            agent_id: None,
            agent_path,
            agent_nickname,
            agent_role,
            ..Default::default()
        })
    }

    #[allow(clippy::too_many_arguments)]
    fn prepare_thread_spawn(
        &self,
        reservation: &mut crate::agent::registry::SpawnReservation,
        config: &Config,
        parent_thread_id: ThreadId,
        depth: i32,
        agent_path: Option<AgentPath>,
        agent_role: Option<String>,
        preferred_agent_nickname: Option<String>,
    ) -> CodexResult<(SessionSource, AgentMetadata)> {
        if depth == 1 {
            self.state.register_root_thread(parent_thread_id);
        }
        let agent_metadata = self.prepare_agent_metadata(
            reservation,
            config,
            agent_path,
            agent_role,
            preferred_agent_nickname,
        )?;
        let session_source = SessionSource::SubAgent(SubAgentSource::ThreadSpawn {
            parent_thread_id,
            depth,
            agent_path: agent_metadata.agent_path.clone(),
            agent_nickname: agent_metadata.agent_nickname.clone(),
            agent_role: agent_metadata.agent_role.clone(),
        });
        Ok((session_source, agent_metadata))
    }

    fn upgrade(&self) -> CodexResult<Arc<ThreadManagerState>> {
        self.manager
            .upgrade()
            .ok_or_else(|| CodexErr::UnsupportedOperation("thread manager dropped".to_string()))
    }

    async fn inherited_environments_for_source(
        &self,
        state: &Arc<ThreadManagerState>,
        session_source: Option<&SessionSource>,
    ) -> Option<TurnEnvironmentSnapshot> {
        let Some(SessionSource::SubAgent(SubAgentSource::ThreadSpawn {
            parent_thread_id, ..
        })) = session_source
        else {
            return None;
        };

        let parent_thread = state.get_thread(*parent_thread_id).await.ok()?;
        Some(
            parent_thread
                .session
                .services
                .turn_environments
                .snapshot()
                .await,
        )
    }

    async fn inherited_exec_policy_for_source(
        &self,
        state: &Arc<ThreadManagerState>,
        session_source: Option<&SessionSource>,
        child_config: &Config,
    ) -> Option<Arc<crate::exec_policy::ExecPolicyManager>> {
        let Some(SessionSource::SubAgent(SubAgentSource::ThreadSpawn {
            parent_thread_id, ..
        })) = session_source
        else {
            return None;
        };

        let parent_thread = state.get_thread(*parent_thread_id).await.ok()?;
        let parent_config = parent_thread.session.get_config().await;
        if !crate::exec_policy::child_uses_parent_exec_policy(&parent_config, child_config) {
            return None;
        }

        Some(Arc::clone(&parent_thread.session.services.exec_policy))
    }

    async fn open_thread_spawn_children(
        &self,
        parent_thread_id: ThreadId,
    ) -> CodexResult<Vec<(ThreadId, AgentMetadata)>> {
        let mut children_by_parent = self.live_thread_spawn_children().await?;
        Ok(children_by_parent
            .remove(&parent_thread_id)
            .unwrap_or_default())
    }

    async fn live_thread_spawn_children(
        &self,
    ) -> CodexResult<HashMap<ThreadId, Vec<(ThreadId, AgentMetadata)>>> {
        let state = self.upgrade()?;
        let mut children_by_parent = HashMap::<ThreadId, Vec<(ThreadId, AgentMetadata)>>::new();

        for (parent_thread_id, child_thread_id) in state.list_live_thread_spawn_edges().await {
            children_by_parent
                .entry(parent_thread_id)
                .or_default()
                .push((
                    child_thread_id,
                    self.state
                        .agent_metadata_for_thread(child_thread_id)
                        .unwrap_or(AgentMetadata {
                            agent_id: Some(child_thread_id),
                            ..Default::default()
                        }),
                ));
        }

        for children in children_by_parent.values_mut() {
            children.sort_by(|left, right| {
                left.1
                    .agent_path
                    .as_deref()
                    .unwrap_or_default()
                    .cmp(right.1.agent_path.as_deref().unwrap_or_default())
                    .then_with(|| left.0.to_string().cmp(&right.0.to_string()))
            });
        }

        Ok(children_by_parent)
    }

    async fn persist_thread_spawn_edge_for_source(
        &self,
        child_thread: &crate::CodexThread,
        child_thread_id: ThreadId,
        session_source: Option<&SessionSource>,
    ) {
        let Some(parent_thread_id) = session_source.and_then(SessionSource::parent_thread_id)
        else {
            return;
        };
        if child_thread.config_snapshot().await.ephemeral {
            return;
        }
        let Ok(state) = self.upgrade() else {
            return;
        };
        let Some(agent_graph_store) = state.agent_graph_store() else {
            return;
        };
        if let Err(err) = agent_graph_store
            .upsert_thread_spawn_edge(
                parent_thread_id,
                child_thread_id,
                codex_agent_graph_store::ThreadSpawnEdgeStatus::Open,
            )
            .await
        {
            warn!("failed to persist thread-spawn edge: {err}");
        }
    }

    #[allow(dead_code)]
    #[cfg(test)]
    /// Enumerate persisted descendants and filter them by the desired spawn-edge status.
    pub(crate) async fn list_persisted_subagent_descendants(
        &self,
        root_thread_id: ThreadId,
        status: DirectionalThreadSpawnEdgeStatus,
    ) -> CodexResult<Vec<ThreadId>> {
        let state = self.upgrade()?;
        let thread = state.get_thread(root_thread_id).await?;
        let Some(state_db_ctx) = thread.state_db() else {
            return Ok(Vec::new());
        };
        state_db_ctx
            .list_thread_spawn_descendants_with_status(root_thread_id, status)
            .await
            .map_err(|err| {
                CodexErr::Fatal(format!(
                    "failed to list persisted thread-spawn descendants for {root_thread_id}: {err}"
                ))
            })
    }

    async fn live_thread_spawn_descendants(
        &self,
        root_thread_id: ThreadId,
    ) -> CodexResult<Vec<ThreadId>> {
        let mut children_by_parent = self.live_thread_spawn_children().await?;
        let mut descendants = Vec::new();
        let mut stack = children_by_parent
            .remove(&root_thread_id)
            .unwrap_or_default()
            .into_iter()
            .map(|(child_thread_id, _)| child_thread_id)
            .rev()
            .collect::<Vec<_>>();

        while let Some(thread_id) = stack.pop() {
            descendants.push(thread_id);
            if let Some(children) = children_by_parent.remove(&thread_id) {
                for (child_thread_id, _) in children.into_iter().rev() {
                    stack.push(child_thread_id);
                }
            }
        }

        Ok(descendants)
    }

    pub(crate) async fn list_live_agent_subtree_thread_ids(
        &self,
        agent_id: ThreadId,
    ) -> CodexResult<Vec<ThreadId>> {
        let mut thread_ids = vec![agent_id];
        thread_ids.extend(self.live_thread_spawn_descendants(agent_id).await?);
        Ok(thread_ids)
    }

    async fn load_agent_tree_record(
        &self,
        state: &Arc<ThreadManagerState>,
        state_db_ctx: Option<&state_db::StateDbHandle>,
        thread_id: ThreadId,
        session_state: AgentSessionState,
    ) -> CodexResult<AgentTreeRecord> {
        match session_state {
            AgentSessionState::Live => {
                let thread = state.get_thread(thread_id).await?;
                let config_snapshot = thread.config_snapshot().await;
                let metadata =
                    self.state
                        .agent_metadata_for_thread(thread_id)
                        .unwrap_or(AgentMetadata {
                            agent_id: Some(thread_id),
                            ..Default::default()
                        });
                Ok(AgentTreeRecord {
                    agent_name: metadata
                        .agent_path
                        .as_ref()
                        .map(ToString::to_string)
                        .unwrap_or_else(|| thread_id.to_string()),
                    session_state,
                    agent_status: Some(thread.agent_status().await),
                    nickname: metadata.agent_nickname,
                    role: metadata.agent_role,
                    effective_model: Some(config_snapshot.model),
                    effective_reasoning_effort: config_snapshot.reasoning_effort,
                })
            }
            AgentSessionState::Stale => {
                let Some(state_db_ctx) = state_db_ctx else {
                    return Err(inspect_agent_tree_state_db_unavailable());
                };
                let metadata = state_db_ctx
                    .get_thread(thread_id)
                    .await
                    .map_err(|err| {
                        CodexErr::Fatal(format!(
                            "failed to inspect stale agent metadata for {thread_id}: {err}"
                        ))
                    })?
                    .ok_or_else(|| {
                        CodexErr::UnsupportedOperation(format!(
                            "stale agent metadata for {thread_id} is unavailable"
                        ))
                    })?;

                Ok(AgentTreeRecord {
                    agent_name: metadata.agent_path.unwrap_or_else(|| thread_id.to_string()),
                    session_state,
                    agent_status: None,
                    nickname: metadata.agent_nickname,
                    role: metadata.agent_role,
                    effective_model: None,
                    effective_reasoning_effort: None,
                })
            }
        }
    }

    async fn tree_child_session_states(
        &self,
        live_children_by_parent: Option<&HashMap<ThreadId, Vec<(ThreadId, AgentMetadata)>>>,
        state_db_ctx: Option<&state_db::StateDbHandle>,
        parent_thread_id: ThreadId,
        scope: AgentTreeScope,
    ) -> CodexResult<HashMap<ThreadId, AgentSessionState>> {
        let mut child_states = HashMap::<ThreadId, AgentSessionState>::new();

        if !matches!(scope, AgentTreeScope::Stale)
            && let Some(live_children_by_parent) = live_children_by_parent
            && let Some(children) = live_children_by_parent.get(&parent_thread_id)
        {
            for (child_thread_id, _) in children {
                child_states.insert(*child_thread_id, AgentSessionState::Live);
            }
        }

        if !matches!(scope, AgentTreeScope::Live) {
            let Some(state_db_ctx) = state_db_ctx else {
                return Err(inspect_agent_tree_state_db_unavailable());
            };
            let closed_children = state_db_ctx
                .list_thread_spawn_children_with_status(
                    parent_thread_id,
                    DirectionalThreadSpawnEdgeStatus::Closed,
                )
                .await
                .map_err(|err| {
                    CodexErr::Fatal(format!(
                        "failed to inspect stale child agents for {parent_thread_id}: {err}"
                    ))
                })?;
            for child_thread_id in closed_children {
                child_states
                    .entry(child_thread_id)
                    .or_insert(AgentSessionState::Stale);
            }
        }

        Ok(child_states)
    }
}

fn thread_spawn_parent_thread_id(session_source: &SessionSource) -> Option<ThreadId> {
    match session_source {
        SessionSource::SubAgent(SubAgentSource::ThreadSpawn {
            parent_thread_id, ..
        }) => Some(*parent_thread_id),
        _ => None,
    }
}

fn inspect_agent_tree_state_db_unavailable() -> CodexErr {
    CodexErr::UnsupportedOperation(INSPECT_AGENT_TREE_STATE_DB_UNAVAILABLE_MESSAGE.to_string())
}

fn agent_matches_prefix(agent_path: Option<&AgentPath>, prefix: &AgentPath) -> bool {
    if prefix.is_root() {
        return true;
    }

    agent_path.is_some_and(|agent_path| {
        agent_path == prefix
            || agent_path
                .as_str()
                .strip_prefix(prefix.as_str())
                .is_some_and(|suffix| suffix.starts_with('/'))
    })
}

fn compute_descendant_counts(
    thread_id: ThreadId,
    tree_children: &HashMap<ThreadId, Vec<ThreadId>>,
    descendant_counts: &mut HashMap<ThreadId, usize>,
) -> usize {
    compute_descendant_counts_inner(
        thread_id,
        tree_children,
        descendant_counts,
        &mut HashSet::new(),
    )
}

fn compute_descendant_counts_inner(
    thread_id: ThreadId,
    tree_children: &HashMap<ThreadId, Vec<ThreadId>>,
    descendant_counts: &mut HashMap<ThreadId, usize>,
    visiting: &mut HashSet<ThreadId>,
) -> usize {
    if let Some(count) = descendant_counts.get(&thread_id).copied() {
        return count;
    }
    if !visiting.insert(thread_id) {
        warn!(%thread_id, "cycle detected in agent descendant tree");
        return 0;
    }

    let count = tree_children.get(&thread_id).map_or(0, |children| {
        children
            .iter()
            .map(|child_thread_id| {
                if visiting.contains(child_thread_id) {
                    warn!(
                        parent_thread_id = %thread_id,
                        child_thread_id = %child_thread_id,
                        "cycle detected in agent descendant tree"
                    );
                    0
                } else {
                    1 + compute_descendant_counts_inner(
                        *child_thread_id,
                        tree_children,
                        descendant_counts,
                        visiting,
                    )
                }
            })
            .sum()
    });
    visiting.remove(&thread_id);
    descendant_counts.insert(thread_id, count);
    count
}

fn compute_active_live_descendant_count(
    thread_id: ThreadId,
    live_children_by_parent: &HashMap<ThreadId, Vec<(ThreadId, AgentMetadata)>>,
    status_by_thread_id: &HashMap<ThreadId, AgentStatus>,
    active_descendant_counts: &mut HashMap<ThreadId, usize>,
) -> usize {
    compute_active_live_descendant_count_inner(
        thread_id,
        live_children_by_parent,
        status_by_thread_id,
        active_descendant_counts,
        &mut HashSet::new(),
    )
}

fn compute_active_live_descendant_count_inner(
    thread_id: ThreadId,
    live_children_by_parent: &HashMap<ThreadId, Vec<(ThreadId, AgentMetadata)>>,
    status_by_thread_id: &HashMap<ThreadId, AgentStatus>,
    active_descendant_counts: &mut HashMap<ThreadId, usize>,
    visiting: &mut HashSet<ThreadId>,
) -> usize {
    if let Some(count) = active_descendant_counts.get(&thread_id).copied() {
        return count;
    }
    if !visiting.insert(thread_id) {
        warn!(%thread_id, "cycle detected in live agent descendant tree");
        return 0;
    }

    let count = live_children_by_parent
        .get(&thread_id)
        .map_or(0, |children| {
            children
                .iter()
                .map(|(child_thread_id, _)| {
                    if visiting.contains(child_thread_id) {
                        warn!(
                            parent_thread_id = %thread_id,
                            child_thread_id = %child_thread_id,
                            "cycle detected in live agent descendant tree"
                        );
                        return 0;
                    }
                    let child_is_active = status_by_thread_id
                        .get(child_thread_id)
                        .is_some_and(|status| !is_final(status));
                    usize::from(child_is_active)
                        + compute_active_live_descendant_count_inner(
                            *child_thread_id,
                            live_children_by_parent,
                            status_by_thread_id,
                            active_descendant_counts,
                            visiting,
                        )
                })
                .sum()
        });
    visiting.remove(&thread_id);
    active_descendant_counts.insert(thread_id, count);
    count
}

fn agent_name_is_same_or_descendant_of(agent_name: &str, parent_name: &str) -> bool {
    agent_name == parent_name
        || agent_name
            .strip_prefix(parent_name)
            .is_some_and(|suffix| suffix.starts_with('/'))
}

pub(crate) fn render_input_preview(input: &[UserInput]) -> String {
    input
        .iter()
        .map(|item| match item {
            UserInput::Text { text, .. } => text.clone(),
            UserInput::Image { .. } => "[image]".to_string(),
            UserInput::LocalImage { path, .. } => {
                format!("[local_image:{}]", path.display())
            }
            UserInput::Audio { .. } => "[audio]".to_string(),
            UserInput::LocalAudio { path } => {
                format!("[local_audio:{}]", path.display())
            }
            UserInput::Skill { name, path, .. } => {
                format!("[skill:${name}]({})", path.display())
            }
            UserInput::Mention { name, path, .. } => format!("[mention:${name}]({path})"),
            _ => "[input]".to_string(),
        })
        .collect::<Vec<_>>()
        .join("\n")
}

fn thread_spawn_depth(session_source: &SessionSource) -> Option<i32> {
    match session_source {
        SessionSource::SubAgent(SubAgentSource::ThreadSpawn { depth, .. }) => Some(*depth),
        _ => None,
    }
}
#[cfg(test)]
#[path = "control_tests.rs"]
mod tests;
