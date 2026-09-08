//! Session, resume, fork, and subagent selection lifecycle for the TUI app.
//!
//! This module owns the high-level transitions between app-server threads: starting fresh sessions,
//! resuming/forking saved sessions, replacing ChatWidget instances, and maintaining the agent picker
//! cache used for multi-agent navigation.

use super::*;
use crate::app_server_session::source_agent_path;
use crate::app_server_session::thread_blocks_direct_input;
use crate::multi_agents::AgentPickerThreadUsage;
use crate::multi_agents::format_agent_picker_item_description;
use crate::multi_agents::format_agent_picker_item_selected_description;
use codex_app_server_protocol::Thread;
use codex_app_server_protocol::ThreadLoadedListParams;
use codex_config::types::ResumeCwdMode;
use codex_protocol::protocol::TokenUsage as ProtocolTokenUsage;
use std::collections::HashSet;
use std::collections::VecDeque;

pub(super) const SUBAGENT_BACKFILL_PAGE_SIZE: u32 = 100;
const SUBAGENT_BACKFILL_REFRESHES_PER_ATTEMPT: usize = 100;

pub(super) struct LoadedSubagentBackfillProgress {
    primary_thread_id: ThreadId,
    next_cursor: Option<String>,
    accumulator: LoadedSubagentAccumulator,
    seen_cursors: HashSet<String>,
    pending_refresh_thread_ids: VecDeque<ThreadId>,
    retained_thread_ids: HashSet<ThreadId>,
    compatibility: Option<LoadedSubagentCompatibilityProgress>,
    ancestor_filter_applied_to_all_pages: bool,
    loaded_fallback: Option<LoadedSubagentFallbackProgress>,
    listing_complete: bool,
    truncated: bool,
}

struct LoadedSubagentCompatibilityProgress {
    accumulator: LoadedSubagentAccumulator,
    retained_thread_ids: HashSet<ThreadId>,
}

impl LoadedSubagentCompatibilityProgress {
    fn new(
        primary_thread_id: ThreadId,
        authoritative_thread_ids: impl IntoIterator<Item = ThreadId>,
    ) -> Self {
        let mut accumulator = LoadedSubagentAccumulator::new(primary_thread_id);
        accumulator.seed_accepted(authoritative_thread_ids);
        Self {
            accumulator,
            retained_thread_ids: HashSet::new(),
        }
    }

    fn ingest(&mut self, threads: Vec<Thread>) -> (Vec<LoadedSubagentThread>, bool) {
        let mut truncated = false;
        let retained = threads
            .into_iter()
            .filter(|thread| {
                let Ok(thread_id) = ThreadId::from_string(&thread.id) else {
                    return false;
                };
                if self.retained_thread_ids.contains(&thread_id) {
                    return false;
                }
                if self.retained_thread_ids.len() >= codex_state::MAX_THREAD_RELATION_DESCENDANTS {
                    // Unacknowledged rows are untrusted compatibility metadata, so this raw cap is
                    // intentionally fail-closed: later valid descendants may be omitted, and the
                    // caller must surface truncation rather than widen memory or CPU consumption.
                    truncated = true;
                    return false;
                }
                self.retained_thread_ids.insert(thread_id)
            })
            .collect();
        (self.accumulator.ingest(retained), truncated)
    }
}

struct LoadedSubagentFallbackProgress {
    next_cursor: Option<String>,
    seen_cursors: HashSet<String>,
    seen_thread_ids: HashSet<ThreadId>,
    pending_thread_ids: VecDeque<ThreadId>,
    accumulator: LoadedSubagentAccumulator,
    listing_complete: bool,
}

impl LoadedSubagentFallbackProgress {
    fn new(primary_thread_id: ThreadId) -> Self {
        Self {
            next_cursor: None,
            seen_cursors: HashSet::new(),
            seen_thread_ids: HashSet::new(),
            pending_thread_ids: VecDeque::new(),
            accumulator: LoadedSubagentAccumulator::new(primary_thread_id),
            listing_complete: false,
        }
    }
}

impl LoadedSubagentBackfillProgress {
    pub(crate) fn new(primary_thread_id: ThreadId) -> Self {
        Self {
            primary_thread_id,
            next_cursor: None,
            accumulator: LoadedSubagentAccumulator::new(primary_thread_id),
            seen_cursors: HashSet::new(),
            pending_refresh_thread_ids: VecDeque::new(),
            retained_thread_ids: HashSet::new(),
            compatibility: None,
            ancestor_filter_applied_to_all_pages: true,
            loaded_fallback: None,
            listing_complete: false,
            truncated: false,
        }
    }

    fn retain_threads(&mut self, threads: Vec<Thread>) -> Vec<Thread> {
        threads
            .into_iter()
            .filter(|thread| {
                let Ok(thread_id) = ThreadId::from_string(&thread.id) else {
                    return false;
                };
                self.retain_thread_id(thread_id)
            })
            .collect()
    }

    fn retain_thread_id(&mut self, thread_id: ThreadId) -> bool {
        if thread_id == self.primary_thread_id || self.retained_thread_ids.contains(&thread_id) {
            return false;
        }
        if self.retained_thread_ids.len() >= MAX_RETAINED_SUBAGENT_LINEAGE {
            self.truncated = true;
            return false;
        }
        self.retained_thread_ids.insert(thread_id)
    }

    fn retain_loaded_threads(
        &mut self,
        threads: Vec<LoadedSubagentThread>,
    ) -> Vec<LoadedSubagentThread> {
        threads
            .into_iter()
            .filter(|thread| self.retain_thread_id(thread.thread_id))
            .collect()
    }

    #[cfg(test)]
    pub(crate) fn retained_thread_count(&self) -> usize {
        self.retained_thread_ids.len()
    }

    #[cfg(test)]
    pub(crate) fn compatibility_retained_thread_count(&self) -> usize {
        self.compatibility
            .as_ref()
            .map_or(0, |compatibility| compatibility.retained_thread_ids.len())
    }

    #[cfg(test)]
    pub(crate) fn fallback_retained_thread_count(&self) -> usize {
        self.loaded_fallback
            .as_ref()
            .map_or(0, |fallback| fallback.seen_thread_ids.len())
    }

    #[cfg(test)]
    pub(crate) fn is_truncated(&self) -> bool {
        self.truncated
    }

    #[cfg(test)]
    pub(crate) fn seed_relation_cursors_to_limit(&mut self) {
        self.seen_cursors = (0..MAX_RETAINED_SUBAGENT_LINEAGE)
            .map(|index| format!("cursor-{index}"))
            .collect();
    }

    #[cfg(test)]
    pub(crate) fn retained_relation_cursor_count(&self) -> usize {
        self.seen_cursors.len()
    }
}

#[derive(Clone, Copy)]
pub(super) enum ThreadAttachPresentation {
    SessionLineage,
    PromptEdit,
}

/// Reports whether a loaded-thread backfill completed and which descendants already had their
/// liveness metadata refreshed, allowing the picker to skip duplicate `thread/read` requests.
#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub(super) enum LoadedSubagentBackfillStatus {
    #[default]
    Incomplete,
    Complete,
    Paused,
    RetryableError,
    CursorCycle,
    Truncated,
}

#[derive(Default)]
pub(super) struct LoadedSubagentBackfill {
    pub(super) completed: bool,
    pub(super) status: LoadedSubagentBackfillStatus,
    pub(super) refreshed_thread_ids: HashSet<ThreadId>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) enum ThreadLivenessRefreshOutcome {
    Refreshed,
    TerminalPruned,
    RetryableError,
    CapacityRejected,
}

fn agent_picker_subtitle(
    lineage_truncated: bool,
    picker_has_more: bool,
    backfill_status: &LoadedSubagentBackfillStatus,
) -> String {
    let base = AgentNavigationState::picker_subtitle();
    let backfill_incomplete = !matches!(
        backfill_status,
        LoadedSubagentBackfillStatus::Complete | LoadedSubagentBackfillStatus::Truncated
    );
    if lineage_truncated && backfill_incomplete {
        return format!(
            "Additional rows were omitted at the {MAX_RETAINED_SUBAGENT_LINEAGE}-agent lineage safety limit. {base} Retained rows still need refresh; reopen to continue or retry."
        );
    }
    if lineage_truncated {
        return format!(
            "Additional rows were omitted at the {MAX_RETAINED_SUBAGENT_LINEAGE}-agent lineage safety limit. {base}"
        );
    }
    if picker_has_more {
        return format!(
            "{base} Showing a bounded slice; reopen to continue through retained agents."
        );
    }
    if backfill_incomplete {
        return format!("{base} Lineage refresh is incomplete; reopen to retry.");
    }
    base
}

impl App {
    pub(super) async fn open_agent_picker(&mut self, app_server: &mut AppServerSession) {
        // A previously completed, truncated backfill already refreshed every retained row during
        // its bounded recovery. Reopening the picker must not issue an identical burst of reads;
        // a subsequent lifecycle event will invalidate this cache when new lineage is observed.
        let retained_lineage_refreshes_complete = self
            .subagent_backfill_progress
            .as_ref()
            .is_some_and(|progress| {
                progress.listing_complete
                    && progress.truncated
                    && progress.pending_refresh_thread_ids.is_empty()
                    && progress.loaded_fallback.is_none()
            });
        let backfill = self.backfill_loaded_subagent_threads(app_server).await;
        let lineage_truncated = backfill.status == LoadedSubagentBackfillStatus::Truncated
            || self
                .subagent_backfill_progress
                .as_ref()
                .is_some_and(|progress| progress.truncated);
        let untracked_channel_ids = self
            .thread_event_channels
            .keys()
            .filter(|thread_id| self.agent_navigation.get(thread_id).is_none())
            .take(AGENT_PICKER_ROWS_PER_OPEN)
            .copied()
            .collect::<Vec<_>>();
        if !retained_lineage_refreshes_complete {
            for thread_id in untracked_channel_ids {
                self.refresh_agent_picker_thread_liveness(app_server, thread_id)
                    .await;
            }
        }
        let (picker_thread_ids, picker_has_more) = self.agent_navigation.next_picker_thread_ids(
            self.primary_thread_id,
            self.active_thread_id,
            AGENT_PICKER_ROWS_PER_OPEN,
        );
        // V2 subagents are identified by canonical paths observed from activity events or loaded
        // thread metadata. A buffered active turn is positive liveness evidence; a completed
        // snapshot is terminal evidence. An empty store does not clear a successful spawn hint.
        let path_backed_thread_ids: Vec<_> = picker_thread_ids
            .iter()
            .copied()
            .filter(|thread_id| {
                Some(*thread_id) != self.primary_thread_id
                    && self.agent_navigation.get(thread_id).is_some_and(|entry| {
                        entry
                            .agent_path
                            .as_deref()
                            .is_some_and(|agent_path| !agent_path.trim().is_empty())
                    })
            })
            .collect();
        for thread_id in path_backed_thread_ids.iter().copied() {
            if let Some(channel) = self.thread_event_channels.get(&thread_id)
                && channel.attachment() == ThreadEventAttachment::Live
            {
                let (has_active_turn, has_terminal_snapshot) = {
                    let store = channel.store.lock().await;
                    (
                        store.active_turn_id().is_some(),
                        store
                            .turns
                            .last()
                            .is_some_and(|turn| !matches!(turn.status, TurnStatus::InProgress)),
                    )
                };
                if has_active_turn {
                    self.agent_navigation.mark_running(thread_id);
                } else if has_terminal_snapshot {
                    self.agent_navigation.mark_stopped(thread_id);
                }
            } else if !retained_lineage_refreshes_complete
                && !backfill.refreshed_thread_ids.contains(&thread_id)
            {
                self.refresh_agent_picker_thread_liveness(app_server, thread_id)
                    .await;
            }
        }
        let path_backed_threads = path_backed_thread_ids
            .iter()
            .filter_map(|thread_id| {
                self.agent_navigation
                    .get(thread_id)
                    .map(|entry| (*thread_id, entry))
            })
            .collect::<Vec<_>>();
        if !path_backed_threads.is_empty() {
            let running_threads: Vec<_> = path_backed_threads
                .into_iter()
                .filter_map(|(thread_id, entry)| {
                    if !entry.is_running || entry.is_closed {
                        return None;
                    }
                    Some((thread_id, entry.agent_path.as_deref()?.trim().to_string()))
                })
                .collect();
            let mut entries = Vec::new();
            for (thread_id, agent_path) in running_threads {
                let preview = if let Some(channel) = self.thread_event_channels.get(&thread_id) {
                    let store = channel.store.lock().await;
                    super::agent_status_feed::AgentStatusThreadPreview::from_store(
                        agent_path, &store,
                    )
                } else {
                    super::agent_status_feed::AgentStatusThreadPreview::empty(agent_path)
                };
                entries.push(preview);
            }

            self.chat_widget
                .add_to_history(super::agent_status_feed::AgentStatusHistoryCell::new(
                    entries,
                ));
        }

        if !retained_lineage_refreshes_complete {
            for thread_id in picker_thread_ids.iter().copied() {
                if path_backed_thread_ids.contains(&thread_id)
                    || self.side_threads.contains_key(&thread_id)
                    || backfill.refreshed_thread_ids.contains(&thread_id)
                {
                    continue;
                }
                self.refresh_agent_picker_thread_liveness(app_server, thread_id)
                    .await;
            }
        }

        let has_non_primary_agent_thread = self
            .agent_navigation
            .has_non_primary_thread(self.primary_thread_id);
        if !self.config.features.enabled(Feature::Collab) && !has_non_primary_agent_thread {
            self.chat_widget.open_multi_agent_enable_prompt();
            return;
        }

        if self.agent_navigation.is_empty() {
            self.chat_widget
                .add_info_message("No agents available yet.".to_string(), /*hint*/ None);
            return;
        }

        let mut initial_selected_idx = None;
        let mut items = Vec::new();
        for (idx, thread_id) in picker_thread_ids.into_iter().enumerate() {
            let Some(entry) = self.agent_navigation.get(&thread_id) else {
                continue;
            };
            if self.active_thread_id == Some(thread_id) {
                initial_selected_idx = Some(idx);
            }
            let id = thread_id;
            let is_primary = self.primary_thread_id == Some(thread_id);
            let name = entry
                .agent_path
                .as_deref()
                .map(str::trim)
                .filter(|agent_path| !is_primary && !agent_path.is_empty())
                .map(ToOwned::to_owned)
                .unwrap_or_else(|| {
                    format_agent_picker_item_name(
                        entry.agent_nickname.as_deref(),
                        entry.agent_role.as_deref(),
                        is_primary,
                    )
                });
            let usage = self.agent_picker_thread_usage(thread_id, entry).await;
            let description = format_agent_picker_item_description(thread_id, entry, &usage);
            let selected_description =
                format_agent_picker_item_selected_description(thread_id, entry, &usage);
            let status_terms = if entry.is_running {
                "live active open"
            } else {
                "closed stale inactive finished"
            };
            let search_value =
                format!("{name} {description} {selected_description} {status_terms}");
            items.push(SelectionItem {
                name,
                name_prefix_spans: agent_picker_status_dot_spans(entry.is_closed),
                description: Some(description),
                selected_description: Some(selected_description),
                is_current: self.active_thread_id == Some(thread_id),
                hidden_when_unfiltered: !is_primary
                    && self.active_thread_id != Some(thread_id)
                    && !entry.is_running,
                actions: vec![Box::new(move |tx| {
                    tx.send(AppEvent::SelectAgentThread(id));
                })],
                dismiss_on_select: true,
                search_value: Some(search_value),
                ..Default::default()
            });
        }

        self.chat_widget.show_selection_view(SelectionViewParams {
            title: Some("Subagents".to_string()),
            subtitle: Some(agent_picker_subtitle(
                lineage_truncated,
                picker_has_more,
                &backfill.status,
            )),
            footer_hint: Some(standard_popup_hint_line()),
            is_searchable: true,
            search_placeholder: Some("Search agents or type 'closed'".to_string()),
            items,
            initial_selected_idx,
            ..Default::default()
        });
    }

    async fn agent_picker_thread_usage(
        &self,
        thread_id: ThreadId,
        entry: &crate::multi_agents::AgentPickerThreadEntry,
    ) -> AgentPickerThreadUsage {
        let mut usage = AgentPickerThreadUsage {
            model: entry.model.clone(),
            reasoning_effort: entry.reasoning_effort.clone(),
            task_name: entry.task_name.clone().or_else(|| entry.agent_path.clone()),
            ..Default::default()
        };

        if let Some(channel) = self.thread_event_channels.get(&thread_id) {
            let store = channel.store.lock().await;
            if let Some(session) = &store.session {
                if usage.model.is_none() && !session.model.trim().is_empty() {
                    usage.model = Some(session.model.clone());
                }
                if usage.reasoning_effort.is_none() {
                    usage.reasoning_effort = session.reasoning_effort.clone();
                }
                usage.approval_policy = Some(session.approval_policy);
                usage.approvals_reviewer = Some(session.approvals_reviewer);
                usage.sandbox_policy = session
                    .permission_profile
                    .to_legacy_sandbox_policy(session.cwd.as_path())
                    .ok()
                    .map(Into::into);
            }
        }

        if self.active_thread_id == Some(thread_id) {
            let token_usage = self.chat_widget.token_usage();
            usage.token_usage = ProtocolTokenUsage {
                input_tokens: token_usage.input_tokens,
                cached_input_tokens: token_usage.cached_input_tokens,
                cache_write_input_tokens: token_usage.cache_write_input_tokens,
                output_tokens: token_usage.output_tokens,
                reasoning_output_tokens: token_usage.reasoning_output_tokens,
                total_tokens: token_usage.total_tokens,
            };
        }

        usage
    }

    pub(super) fn is_terminal_thread_read_error(err: &color_eyre::Report) -> bool {
        err.chain()
            .any(|cause| cause.to_string().contains("thread not loaded:"))
    }

    pub(super) fn closed_state_for_thread_read_error(
        err: &color_eyre::Report,
        existing_is_closed: Option<bool>,
    ) -> bool {
        Self::is_terminal_thread_read_error(err) || existing_is_closed.unwrap_or(false)
    }

    pub(super) fn can_fallback_from_include_turns_error(err: &color_eyre::Report) -> bool {
        err.chain().any(|cause| {
            let message = cause.to_string();
            message.contains("includeTurns is unavailable before first user message")
                || message.contains("ephemeral threads do not support includeTurns")
        })
    }

    /// Updates cached picker metadata and then mirrors any visible-label change into the footer.
    ///
    /// These two writes stay paired so the picker rows and contextual footer continue to describe
    /// the same displayed thread after nickname or role updates. Returns `false` when a new thread
    /// would exceed the navigation cap, in which case no ChatWidget metadata is added.
    pub(super) fn upsert_agent_picker_thread(
        &mut self,
        thread_id: ThreadId,
        agent_nickname: Option<String>,
        agent_role: Option<String>,
        is_closed: bool,
    ) -> bool {
        if !self.agent_navigation.upsert(
            thread_id,
            agent_nickname.clone(),
            agent_role.clone(),
            is_closed,
            /*created_at*/ None,
            /*updated_at*/ None,
        ) {
            return false;
        }
        self.chat_widget
            .set_collab_agent_metadata(thread_id, agent_nickname, agent_role);
        self.sync_agent_picker_identity(thread_id);
        self.sync_active_agent_label();
        true
    }

    fn protected_agent_picker_threads(&self) -> Vec<ThreadId> {
        [self.primary_thread_id, self.active_thread_id]
            .into_iter()
            .flatten()
            .collect()
    }

    pub(super) fn upsert_agent_picker_thread_retaining(
        &mut self,
        thread_id: ThreadId,
        agent_nickname: Option<String>,
        agent_role: Option<String>,
        is_closed: bool,
    ) -> AgentNavigationUpdate {
        let update = self.agent_navigation.upsert_retaining(
            thread_id,
            AgentPickerThreadRetention {
                agent_nickname: agent_nickname.clone(),
                agent_role: agent_role.clone(),
                is_closed,
                created_at: None,
                updated_at: None,
            },
            &self.protected_agent_picker_threads(),
        );
        if let Some(evicted) = update.evicted() {
            self.chat_widget.remove_collab_agent_metadata(evicted);
        }
        if update.accepted() {
            self.chat_widget
                .set_collab_agent_metadata(thread_id, agent_nickname, agent_role);
            self.sync_agent_picker_identity(thread_id);
            self.sync_active_agent_label();
        }
        update
    }

    /// Removes a picker thread and its mirrored ChatWidget identity as one cache operation.
    pub(super) fn remove_agent_picker_thread(&mut self, thread_id: ThreadId) {
        self.agent_navigation.remove(thread_id);
        self.chat_widget.remove_collab_agent_metadata(thread_id);
        self.sync_active_agent_label();
    }

    pub(super) fn sync_agent_picker_identity(&mut self, thread_id: ThreadId) {
        let Some(entry) = self.agent_navigation.get(&thread_id).cloned() else {
            return;
        };
        self.chat_widget.set_collab_agent_identity(
            thread_id,
            crate::multi_agents::AgentMetadata {
                agent_nickname: entry.agent_nickname,
                agent_role: entry.agent_role,
                agent_path: entry.agent_path,
                model: entry.model,
                reasoning_effort: entry.reasoning_effort,
            },
        );
    }

    /// Persists the app-server's authoritative ownership flag and updates the active composer.
    pub(super) fn mark_primary_thread_parent_owned(&mut self, thread_id: ThreadId) {
        self.agent_navigation.mark_parent_owned(thread_id);
        self.chat_widget.set_parent_owned_thread();
    }

    /// Marks a cached picker thread closed and recomputes the contextual footer label.
    ///
    /// Closing a thread is not the same as removing it: users can still inspect finished agent
    /// transcripts, and the stable next/previous traversal order should not collapse around them.
    pub(super) fn mark_agent_picker_thread_closed(&mut self, thread_id: ThreadId) {
        self.agent_navigation.mark_closed(thread_id);
        self.sync_active_agent_label();
    }

    pub(super) async fn refresh_agent_picker_thread_liveness(
        &mut self,
        app_server: &mut AppServerSession,
        thread_id: ThreadId,
    ) -> ThreadLivenessRefreshOutcome {
        let existing_entry = self.agent_navigation.get(&thread_id).cloned();
        let has_replay_channel = self.thread_event_channels.contains_key(&thread_id);
        match app_server
            .thread_read(thread_id, /*include_turns*/ false)
            .await
        {
            Ok(thread) => {
                let is_parent_owned = thread_blocks_direct_input(&thread);
                let agent_path = source_agent_path(&thread.source);
                let is_running = matches!(
                    thread.status,
                    codex_app_server_protocol::ThreadStatus::Active { .. }
                );
                let is_closed = matches!(
                    thread.status,
                    codex_app_server_protocol::ThreadStatus::NotLoaded
                );
                let retain_at_capacity = is_running
                    || self.primary_thread_id == Some(thread_id)
                    || self.active_thread_id == Some(thread_id);
                let accepted = if retain_at_capacity {
                    self.upsert_agent_picker_thread_retaining(
                        thread_id,
                        thread.agent_nickname.or_else(|| {
                            existing_entry
                                .as_ref()
                                .and_then(|entry| entry.agent_nickname.clone())
                        }),
                        thread.agent_role.or_else(|| {
                            existing_entry
                                .as_ref()
                                .and_then(|entry| entry.agent_role.clone())
                        }),
                        is_closed,
                    )
                    .accepted()
                } else {
                    self.upsert_agent_picker_thread(
                        thread_id,
                        thread.agent_nickname.or_else(|| {
                            existing_entry
                                .as_ref()
                                .and_then(|entry| entry.agent_nickname.clone())
                        }),
                        thread.agent_role.or_else(|| {
                            existing_entry
                                .as_ref()
                                .and_then(|entry| entry.agent_role.clone())
                        }),
                        is_closed,
                    )
                };
                if !accepted {
                    return ThreadLivenessRefreshOutcome::CapacityRejected;
                }
                if is_parent_owned {
                    self.agent_navigation.mark_parent_owned(thread_id);
                }
                self.agent_navigation.set_agent_path(thread_id, agent_path);
                self.agent_navigation.update_identity(
                    thread_id,
                    thread.model.clone(),
                    thread.reasoning_effort.clone(),
                    Some(thread.model_provider.clone()),
                    thread.name.clone(),
                );
                self.agent_navigation.set_timestamps(
                    thread_id,
                    Some(thread.created_at),
                    Some(thread.updated_at),
                );
                self.sync_agent_picker_identity(thread_id);
                if is_running {
                    self.agent_navigation.mark_running(thread_id);
                } else {
                    self.agent_navigation
                        .set_running(thread_id, /*is_running*/ false);
                }
                ThreadLivenessRefreshOutcome::Refreshed
            }
            Err(err) => {
                // Keep the primary/current row available for picker rendering even when a
                // terminal read race means its metadata is no longer persisted. Removing that
                // protected row would hide a valid lineage-truncation notice (and could leave the
                // picker with no view at all) while the user is still on the session.
                if Self::is_terminal_thread_read_error(&err)
                    && !has_replay_channel
                    && self.primary_thread_id != Some(thread_id)
                    && self.active_thread_id != Some(thread_id)
                {
                    self.remove_agent_picker_thread(thread_id);
                    return ThreadLivenessRefreshOutcome::TerminalPruned;
                }
                let is_closed = Self::closed_state_for_thread_read_error(
                    &err,
                    existing_entry.as_ref().map(|entry| entry.is_closed),
                );
                if let Some(entry) = existing_entry {
                    let accepted = self.upsert_agent_picker_thread(
                        thread_id,
                        entry.agent_nickname,
                        entry.agent_role,
                        is_closed,
                    );
                    if !accepted {
                        return ThreadLivenessRefreshOutcome::CapacityRejected;
                    }
                } else {
                    let accepted = self.upsert_agent_picker_thread(
                        thread_id, /*agent_nickname*/ None, /*agent_role*/ None,
                        is_closed,
                    );
                    if !accepted {
                        return ThreadLivenessRefreshOutcome::CapacityRejected;
                    }
                }
                self.agent_navigation
                    .set_running(thread_id, /*is_running*/ false);
                ThreadLivenessRefreshOutcome::RetryableError
            }
        }
    }

    /// Materializes a live thread into local replay state when the picker knows about it but the
    /// TUI has not cached a local event channel yet.
    ///
    /// Resume-time backfill intentionally avoids creating empty placeholder channels, because those
    /// placeholders make stale `/agent` entries open blank transcripts. When a user later selects a
    /// still-live discovered thread, attach it on demand with a real resumed snapshot.
    pub(super) async fn attach_live_thread_for_selection(
        &mut self,
        app_server: &mut AppServerSession,
        thread_id: ThreadId,
    ) -> Result<bool> {
        if self
            .thread_event_channels
            .get(&thread_id)
            .is_some_and(|channel| channel.attachment() == ThreadEventAttachment::Live)
        {
            return Ok(true);
        }

        let (session, turns, live_attached, blocks_direct_input) = match app_server
            .resume_thread(self.config.clone(), thread_id, self.resume_model_settings())
            .await
        {
            Ok(started) => (
                started.session,
                started.turns,
                true,
                started.blocks_direct_input,
            ),
            Err(resume_err) => {
                tracing::warn!(
                    thread_id = %thread_id,
                    error = %resume_err,
                    "failed to resume live thread for selection; falling back to thread/read"
                );
                let (session, turns) = self
                    .read_thread_for_selection_replay(app_server, thread_id)
                    .await?;
                (session, turns, false, false)
            }
        };
        if !self
            .upsert_agent_picker_thread_retaining(
                thread_id, /*agent_nickname*/ None, /*agent_role*/ None,
                /*is_closed*/ false,
            )
            .accepted()
        {
            return Err(color_eyre::eyre::eyre!(
                "Agent thread {thread_id} could not be retained in the bounded picker cache."
            ));
        }
        if blocks_direct_input {
            self.agent_navigation.mark_parent_owned(thread_id);
        }
        let channel = self.ensure_thread_channel(thread_id);
        let mut store = channel.store.lock().await;
        store.set_session(session, turns);
        drop(store);
        if live_attached {
            channel.mark_live();
        } else {
            channel.mark_replay_only();
        }
        Ok(live_attached)
    }

    async fn read_thread_for_selection_replay(
        &mut self,
        app_server: &mut AppServerSession,
        thread_id: ThreadId,
    ) -> Result<(ThreadSessionState, Vec<Turn>)> {
        let (thread, turns) = match app_server
            .thread_read(thread_id, /*include_turns*/ true)
            .await
        {
            Ok(thread) => {
                let turns = thread.turns.clone();
                (thread, turns)
            }
            Err(err) if Self::can_fallback_from_include_turns_error(&err) => {
                let thread = app_server
                    .thread_read(thread_id, /*include_turns*/ false)
                    .await?;
                (thread, Vec::new())
            }
            Err(err) => return Err(err),
        };
        if turns.is_empty() {
            // A `thread/read` fallback without turns would create a blank local replay channel
            // with no live listener attached, which blocks later real re-attach.
            return Err(color_eyre::eyre::eyre!(
                "Agent thread {thread_id} is not yet available for replay or live attach."
            ));
        }
        let mut session = self.session_state_for_thread_read(thread_id, &thread).await;
        // `thread/read` can seed replay state, but it does not attach the app-server listener that
        // `thread/resume` establishes, so treat this path as replay-only.
        session.model.clear();
        Ok((session, turns))
    }

    /// Replaces the chat widget and re-seeds the new widget's collab metadata from the navigation
    /// cache.
    ///
    /// Thread switches reconstruct the `ChatWidget`, which loses the `collab_agent_metadata` map.
    /// This helper copies every known nickname/role from `AgentNavigationState` into the
    /// replacement widget so that replayed collab items render agent names immediately.
    pub(super) fn replace_chat_widget(&mut self, mut chat_widget: ChatWidget) {
        // Transfer the last-written terminal title to the replacement widget
        // so it knows what OSC title is currently displayed. Without this, the
        // new widget would redundantly clear and rewrite the same title, causing
        // a visible flicker in some terminals.
        let previous_terminal_title = self.chat_widget.last_terminal_title.take();
        if chat_widget.last_terminal_title.is_none() {
            chat_widget.last_terminal_title = previous_terminal_title;
        }
        chat_widget.remote_connection = self.chat_widget.remote_connection.clone();
        for (thread_id, entry) in self.agent_navigation.ordered_threads() {
            chat_widget.set_collab_agent_identity(
                thread_id,
                crate::multi_agents::AgentMetadata {
                    agent_nickname: entry.agent_nickname.clone(),
                    agent_role: entry.agent_role.clone(),
                    agent_path: entry.agent_path.clone(),
                    model: entry.model.clone(),
                    reasoning_effort: entry.reasoning_effort.clone(),
                },
            );
        }
        self.chat_widget = chat_widget;
        self.sync_active_agent_label();
    }

    pub(super) async fn select_agent_thread(
        &mut self,
        tui: &mut tui::Tui,
        app_server: &mut AppServerSession,
        thread_id: ThreadId,
    ) -> Result<()> {
        if self.active_thread_id == Some(thread_id) {
            if !self.should_attach_live_thread_for_selection(thread_id) {
                return Ok(());
            }

            let live_attached = self
                .attach_live_thread_for_selection(app_server, thread_id)
                .await?;
            self.store_active_thread_receiver().await;
            self.active_thread_id = None;
            let Some((receiver, snapshot)) = self.activate_thread_for_replay(thread_id).await
            else {
                return Err(color_eyre::eyre::eyre!(
                    "Agent thread {thread_id} could not be reactivated after attach."
                ));
            };
            let blocks_direct_input = self.agent_navigation.is_parent_owned(thread_id);

            self.active_thread_id = Some(thread_id);
            self.active_thread_rx = Some(receiver);

            let init = self.chatwidget_init_for_forked_or_resumed_thread(
                tui,
                self.config.clone(),
                /*initial_user_message*/ None,
            );
            self.replace_chat_widget(ChatWidget::new_with_app_event(init));
            self.chat_widget.set_replay_only_thread(!live_attached);
            if blocks_direct_input {
                self.chat_widget.set_parent_owned_thread();
            }

            self.reset_for_thread_switch(tui)?;
            self.replay_thread_snapshot(snapshot, live_attached);
            if !live_attached {
                self.chat_widget.add_info_message(
                    format!(
                        "Agent thread {thread_id} could not be resumed live. Replaying saved transcript."
                    ),
                    /*hint*/ None,
                );
            }
            self.drain_active_thread_events(tui).await?;
            self.refresh_pending_thread_approvals().await;
            return Ok(());
        }

        // A tracked side thread stays loaded until it is explicitly discarded and already has a
        // replay channel, so another liveness read cannot add anything before selection.
        let thread_available = self.side_threads.contains_key(&thread_id)
            && self.thread_event_channels.contains_key(&thread_id)
            || matches!(
                self.refresh_agent_picker_thread_liveness(app_server, thread_id)
                    .await,
                ThreadLivenessRefreshOutcome::Refreshed
                    | ThreadLivenessRefreshOutcome::RetryableError
            );
        if !thread_available {
            self.chat_widget
                .add_error_message(format!("Agent thread {thread_id} is no longer available."));
            return Ok(());
        }
        let mut is_replay_only = self
            .agent_navigation
            .get(&thread_id)
            .is_some_and(|entry| entry.is_closed);
        let mut attached_replay_only = false;
        if self.should_attach_live_thread_for_selection(thread_id) {
            match self
                .attach_live_thread_for_selection(app_server, thread_id)
                .await
            {
                Ok(live_attached) => {
                    let newly_replay_only = !live_attached;
                    attached_replay_only = newly_replay_only && !is_replay_only;
                    is_replay_only = newly_replay_only;
                }
                Err(err) => {
                    self.chat_widget.add_error_message(format!(
                        "Failed to attach to agent thread {thread_id}: {err}"
                    ));
                    return Ok(());
                }
            }
        } else if !self.thread_event_channels.contains_key(&thread_id) && is_replay_only {
            self.chat_widget
                .add_error_message(format!("Agent thread {thread_id} is no longer available."));
            return Ok(());
        }
        let previous_thread_id = self.active_thread_id;
        self.store_active_thread_receiver().await;
        self.active_thread_id = None;
        let Some((receiver, mut snapshot)) = self.activate_thread_for_replay(thread_id).await
        else {
            self.chat_widget
                .add_error_message(format!("Agent thread {thread_id} is already active."));
            if let Some(previous_thread_id) = previous_thread_id {
                self.activate_thread_channel(previous_thread_id).await;
            }
            return Ok(());
        };

        self.refresh_snapshot_session_if_needed(
            app_server,
            thread_id,
            is_replay_only,
            &mut snapshot,
        )
        .await;
        let blocks_direct_input = self.agent_navigation.is_parent_owned(thread_id);

        self.active_thread_id = Some(thread_id);
        self.active_thread_rx = Some(receiver);

        let init = self.chatwidget_init_for_forked_or_resumed_thread(
            tui,
            self.config.clone(),
            /*initial_user_message*/ None,
        );
        self.replace_chat_widget(ChatWidget::new_with_app_event(init));
        self.chat_widget.set_replay_only_thread(is_replay_only);
        if blocks_direct_input {
            self.chat_widget.set_parent_owned_thread();
        }

        self.reset_for_thread_switch(tui)?;
        self.replay_thread_snapshot(snapshot, !is_replay_only);
        if is_replay_only {
            let message = if attached_replay_only {
                format!(
                    "Agent thread {thread_id} could not be resumed live. Replaying saved transcript."
                )
            } else {
                format!("Agent thread {thread_id} is closed. Replaying saved transcript.")
            };
            self.chat_widget.add_info_message(message, /*hint*/ None);
        }
        self.drain_active_thread_events(tui).await?;
        self.refresh_pending_thread_approvals().await;

        Ok(())
    }

    pub(super) fn should_attach_live_thread_for_selection(&self, thread_id: ThreadId) -> bool {
        self.thread_event_channels
            .get(&thread_id)
            .is_none_or(|channel| channel.attachment() == ThreadEventAttachment::ReplayOnly)
    }

    pub(super) fn reset_for_thread_switch(&mut self, tui: &mut tui::Tui) -> Result<()> {
        self.reset_transcript_state_after_clear();
        tui.clear_pending_history_lines();
        Self::clear_terminal_for_thread_switch(&mut tui.terminal)?;
        Ok(())
    }

    pub(super) fn clear_terminal_for_thread_switch<B>(
        terminal: &mut crate::custom_terminal::Terminal<B>,
    ) -> Result<()>
    where
        B: Backend + Write,
    {
        terminal.clear_scrollback_and_visible_screen_ansi()?;
        let mut area = terminal.viewport_area;
        if area.y > 0 {
            area.y = 0;
            terminal.set_viewport_area(area);
        }
        Ok(())
    }

    pub(super) fn reset_thread_event_state(&mut self) {
        self.abort_all_thread_event_listeners();
        self.thread_event_channels.clear();
        self.agent_navigation.clear();
        self.side_threads.clear();
        self.active_thread_id = None;
        self.active_thread_rx = None;
        self.primary_thread_id = None;
        self.last_subagent_backfill_attempt = None;
        self.subagent_backfill_progress = None;
        self.primary_session_configured = None;
        self.pending_primary_events.clear();
        self.pending_app_server_requests.clear();
        self.pending_startup_thread_start = false;
        self.chat_widget.set_pending_thread_approvals(Vec::new());
        self.sync_active_agent_label();
    }

    /// Attaches the session supplied during `App::new`, inserting its navigation row before
    /// persisting the app-server's parent-owned capability.
    pub(super) async fn attach_initial_started_thread(
        &mut self,
        started: AppServerStartedThread,
    ) -> Result<ThreadId> {
        let thread_id = started.session.thread_id;
        let blocks_direct_input = started.blocks_direct_input;
        self.enqueue_primary_thread_session(started.session, started.turns)
            .await?;
        if blocks_direct_input {
            self.mark_primary_thread_parent_owned(thread_id);
        }
        Ok(thread_id)
    }

    pub(super) async fn handle_startup_thread_started(
        &mut self,
        app_server: &mut AppServerSession,
        result: Result<AppServerStartedThread, String>,
    ) -> Result<()> {
        if !self.pending_startup_thread_start {
            if let Ok(started) = result {
                let thread_id = started.session.thread_id;
                if let Err(err) = app_server.thread_unsubscribe(thread_id).await {
                    tracing::warn!(
                        thread_id = %thread_id,
                        "failed to unsubscribe stale startup thread: {err}"
                    );
                }
                self.discard_thread_local_state(thread_id).await;
            }
            return Ok(());
        }

        self.pending_startup_thread_start = false;
        self.chat_widget
            .set_queue_submissions_until_session_configured(/*queue*/ false);
        match result {
            Ok(started) => {
                let blocks_direct_input = started.blocks_direct_input;
                let thread_id = started.session.thread_id;
                self.enqueue_primary_thread_session(started.session, started.turns)
                    .await?;
                if blocks_direct_input {
                    self.mark_primary_thread_parent_owned(thread_id);
                }
                self.chat_widget.maybe_send_next_queued_input();
            }
            Err(err) => {
                return Err(color_eyre::eyre::eyre!(
                    "Failed to start a fresh session through the app server: {err}"
                ));
            }
        }
        Ok(())
    }

    pub(super) async fn start_fresh_session_with_summary_hint(
        &mut self,
        tui: &mut tui::Tui,
        app_server: &mut AppServerSession,
        session_start_source: Option<ThreadStartSource>,
        initial_user_message: Option<crate::chatwidget::UserMessage>,
        new_thread_name: Option<String>,
    ) {
        // Start a fresh in-memory session while preserving resumability via persisted rollout
        // history. If an initial message is provided, `enqueue_primary_thread_session` suppresses it
        // until the new session is configured and any replayed turns have been rendered.
        self.refresh_in_memory_config_from_disk_best_effort("starting a new thread")
            .await;
        let model = self.chat_widget.current_model().to_string();
        let mut config = self.fresh_session_config();
        apply_managed_new_thread_defaults(
            &mut config,
            app_server.managed_new_thread_defaults(),
            &self.cli_kv_overrides,
            &self.harness_overrides,
        );
        let summary = session_summary(
            self.chat_widget.token_usage(),
            self.chat_widget.thread_id(),
            self.chat_widget.thread_name(),
            self.chat_widget.rollout_path().as_deref(),
        );
        self.shutdown_current_thread(app_server).await;
        let tracked_thread_ids: Vec<ThreadId> =
            self.thread_event_channels.keys().copied().collect();
        for thread_id in tracked_thread_ids {
            if let Err(err) = app_server.thread_unsubscribe(thread_id).await {
                tracing::warn!("failed to unsubscribe tracked thread {thread_id}: {err}");
            }
        }
        self.config = config.clone();
        match app_server
            .start_thread_with_session_start_source(&config, session_start_source)
            .await
        {
            Ok(mut started) => {
                let name_error = if let Some(name) = new_thread_name {
                    match app_server
                        .thread_set_name(started.session.thread_id, name.clone())
                        .await
                    {
                        Ok(()) => {
                            started.session.thread_name = Some(name);
                            None
                        }
                        Err(err) => Some(format!("Failed to name the new session: {err}")),
                    }
                } else {
                    None
                };
                if let Err(err) = self
                    .replace_chat_widget_with_app_server_thread(
                        tui,
                        started,
                        ThreadAttachPresentation::SessionLineage,
                        initial_user_message,
                    )
                    .await
                {
                    self.chat_widget.add_error_message(format!(
                        "Failed to attach to fresh app-server thread: {err}"
                    ));
                } else {
                    if let Some(err) = name_error {
                        self.chat_widget.add_error_message(err);
                    }
                    if let Some(summary) = summary {
                        let mut lines: Vec<Line<'static>> = Vec::new();
                        if let Some(usage_line) = summary.usage_line {
                            lines.push(usage_line.into());
                        }
                        if let Some(command) = summary.resume_hint {
                            let spans =
                                vec!["To continue this session, run ".into(), command.cyan()];
                            lines.push(spans.into());
                        }
                        self.chat_widget.add_plain_history_lines(lines);
                    }
                }
            }
            Err(err) => {
                self.chat_widget.add_error_message(format!(
                    "Failed to start a fresh session through the app server: {err}"
                ));
                self.config.model = Some(model);
            }
        }
        tui.frame_requester().schedule_frame();
    }

    pub(super) async fn replace_chat_widget_with_app_server_thread(
        &mut self,
        tui: &mut tui::Tui,
        started: AppServerStartedThread,
        presentation: ThreadAttachPresentation,
        initial_user_message: Option<crate::chatwidget::UserMessage>,
    ) -> Result<()> {
        // Initial messages are for freshly attached primary threads only. Thread switches and
        // resume/fork flows pass `None` so they cannot replay old history and then auto-submit a new
        // user turn by accident.
        self.reset_thread_event_state();
        let init = self.chatwidget_init_for_forked_or_resumed_thread(
            tui,
            self.config.clone(),
            initial_user_message,
        );
        self.replace_chat_widget(ChatWidget::new_with_app_event(init));
        let blocks_direct_input = started.blocks_direct_input;
        let thread_id = started.session.thread_id;
        self.enqueue_primary_thread_session_with_presentation(
            started.session,
            started.turns,
            presentation,
        )
        .await?;
        if blocks_direct_input {
            self.mark_primary_thread_parent_owned(thread_id);
        }
        Ok(())
    }

    /// Fetches persisted descendants of the primary thread from the app server and registers them
    /// in the navigation cache and chat widget metadata.
    ///
    /// Called when opening the `/agent` picker and after resuming a thread so that the picker and
    /// keyboard navigation are pre-populated even if the TUI did not witness the original spawn
    /// events. Fresh and forked threads cannot have pre-existing descendants.
    ///
    /// The app server applies the ancestor filter before pagination, so unrelated loaded threads
    /// neither require metadata reads nor consume the page budget. All lineage pages are followed
    /// to avoid permanently hiding descendants beyond the first page. Pages are validated and
    /// applied incrementally through `LoadedSubagentAccumulator`; accepted rows are not retained
    /// for a final all-history walk. Each discovered subagent is registered via
    /// `upsert_agent_picker_thread`, which writes to both `AgentNavigationState` and the
    /// `ChatWidget` metadata map.
    pub(super) async fn backfill_loaded_subagent_threads(
        &mut self,
        app_server: &mut AppServerSession,
    ) -> LoadedSubagentBackfill {
        let Some(primary_thread_id) = self.primary_thread_id else {
            return LoadedSubagentBackfill::default();
        };

        let mut progress = self
            .subagent_backfill_progress
            .take()
            .filter(|progress| progress.primary_thread_id == primary_thread_id)
            .unwrap_or_else(|| LoadedSubagentBackfillProgress::new(primary_thread_id));
        let mut refreshed_thread_ids = HashSet::new();
        let mut had_cursor_cycle = false;
        let mut relation_list_failed = false;
        if !progress.listing_complete {
            let mut page_budget =
                LineagePageBudget::new(std::mem::take(&mut progress.seen_cursors));
            loop {
                let response = match app_server
                    .thread_list(ThreadListParams {
                        cursor: progress.next_cursor.clone(),
                        limit: Some(SUBAGENT_BACKFILL_PAGE_SIZE),
                        sort_key: None,
                        sort_direction: None,
                        model_providers: None,
                        source_kinds: Some(vec![ThreadSourceKind::SubAgentThreadSpawn]),
                        thread_sources: None,
                        archived: Some(false),
                        is_pinned: None,
                        cwd: None,
                        use_state_db_only: true,
                        search_term: None,
                        parent_thread_id: None,
                        ancestor_thread_id: Some(primary_thread_id.to_string()),
                    })
                    .await
                {
                    Ok(response) => response,
                    Err(err) => {
                        tracing::warn!(%err, "failed to list subagent lineage for backfill");
                        progress.seen_cursors = page_budget.into_seen_cursors();
                        progress.loaded_fallback.get_or_insert_with(|| {
                            LoadedSubagentFallbackProgress::new(primary_thread_id)
                        });
                        relation_list_failed = true;
                        break;
                    }
                };
                let ancestor_filter_applied = response.ancestor_filter_applied.unwrap_or(false);
                if !ancestor_filter_applied && progress.compatibility.is_none() {
                    progress.compatibility = Some(LoadedSubagentCompatibilityProgress::new(
                        primary_thread_id,
                        progress.retained_thread_ids.iter().copied(),
                    ));
                }
                progress.ancestor_filter_applied_to_all_pages &= ancestor_filter_applied;
                progress.truncated |= response.relation_limit_reached.unwrap_or(false);
                let loaded_threads = if progress.ancestor_filter_applied_to_all_pages {
                    let retained_threads = progress.retain_threads(response.data);
                    progress.accumulator.ingest(retained_threads)
                } else if let Some(compatibility) = progress.compatibility.as_mut() {
                    let (loaded_threads, compatibility_truncated) =
                        compatibility.ingest(response.data);
                    progress.truncated |= compatibility_truncated;
                    progress.retain_loaded_threads(loaded_threads)
                } else {
                    tracing::warn!(
                        primary_thread_id = %primary_thread_id,
                        "discarding an unacknowledged lineage page without compatibility state"
                    );
                    progress.truncated = true;
                    Vec::new()
                };
                let admission_truncated = self.stage_loaded_subagent_threads(
                    loaded_threads,
                    &mut progress.pending_refresh_thread_ids,
                    &mut refreshed_thread_ids,
                );
                progress.truncated |= admission_truncated;
                match page_budget.observe_page(response.next_cursor) {
                    LineagePageAdvance::Complete => {
                        if progress.ancestor_filter_applied_to_all_pages {
                            let admission_truncated = self.stage_loaded_subagent_threads(
                                progress.accumulator.finish(),
                                &mut progress.pending_refresh_thread_ids,
                                &mut refreshed_thread_ids,
                            );
                            progress.truncated |= admission_truncated;
                        } else {
                            // Completion of an unacknowledged listing cannot promote unresolved
                            // rows. Drop its compatibility graph instead of treating it as an
                            // authoritative complete descendant set.
                            progress.compatibility = None;
                        }
                        progress.listing_complete = true;
                        break;
                    }
                    LineagePageAdvance::Continue(next_cursor) => {
                        progress.next_cursor = Some(next_cursor);
                    }
                    LineagePageAdvance::Pause(next_cursor) => {
                        progress.next_cursor = Some(next_cursor);
                        progress.seen_cursors = page_budget.into_seen_cursors();
                        self.subagent_backfill_progress = Some(progress);
                        tracing::warn!(
                            primary_thread_id = %primary_thread_id,
                            "paused subagent lineage backfill at the per-attempt page budget"
                        );
                        self.sync_active_agent_label();
                        return LoadedSubagentBackfill {
                            status: LoadedSubagentBackfillStatus::Paused,
                            refreshed_thread_ids,
                            ..Default::default()
                        };
                    }
                    LineagePageAdvance::CursorCycle(next_cursor) => {
                        tracing::warn!(
                            %next_cursor,
                            primary_thread_id = %primary_thread_id,
                            "subagent lineage backfill detected a cursor cycle"
                        );
                        progress.next_cursor = None;
                        progress.seen_cursors.clear();
                        had_cursor_cycle = true;
                        break;
                    }
                    LineagePageAdvance::Truncated => {
                        progress.seen_cursors = page_budget.into_seen_cursors();
                        progress.next_cursor = None;
                        progress.listing_complete = true;
                        progress.truncated = true;
                        break;
                    }
                }
            }
        }

        let mut refreshes_attempted = 0;
        if let Some(mut fallback) = progress.loaded_fallback.take() {
            let mut had_fallback_cursor_cycle = false;
            if fallback.seen_thread_ids.len() >= MAX_RETAINED_SUBAGENT_LINEAGE
                && !fallback.listing_complete
            {
                fallback.next_cursor = None;
                fallback.seen_cursors.clear();
                fallback.listing_complete = true;
                progress.truncated = true;
            }
            if !fallback.listing_complete && fallback.pending_thread_ids.is_empty() {
                let mut page_budget =
                    LineagePageBudget::new(std::mem::take(&mut fallback.seen_cursors));
                loop {
                    let response = match app_server
                        .thread_loaded_list(ThreadLoadedListParams {
                            cursor: fallback.next_cursor.clone(),
                            limit: Some(SUBAGENT_BACKFILL_PAGE_SIZE),
                        })
                        .await
                    {
                        Ok(response) => response,
                        Err(err) => {
                            tracing::warn!(%err, "loaded-thread fallback failed during lineage backfill");
                            fallback.seen_cursors = page_budget.into_seen_cursors();
                            progress.loaded_fallback = Some(fallback);
                            self.subagent_backfill_progress = Some(progress);
                            return LoadedSubagentBackfill {
                                status: LoadedSubagentBackfillStatus::RetryableError,
                                refreshed_thread_ids,
                                ..Default::default()
                            };
                        }
                    };
                    let mut retention_exhausted = false;
                    for thread_id in response.data {
                        let Ok(thread_id) = ThreadId::from_string(&thread_id) else {
                            continue;
                        };
                        if thread_id != primary_thread_id
                            && !fallback.seen_thread_ids.contains(&thread_id)
                            && fallback.seen_thread_ids.len() >= MAX_RETAINED_SUBAGENT_LINEAGE
                        {
                            progress.truncated = true;
                            retention_exhausted = true;
                            break;
                        }
                        if thread_id != primary_thread_id
                            && fallback.seen_thread_ids.insert(thread_id)
                        {
                            fallback.pending_thread_ids.push_back(thread_id);
                        }
                    }
                    if retention_exhausted
                        || fallback.seen_thread_ids.len() >= MAX_RETAINED_SUBAGENT_LINEAGE
                            && response.next_cursor.is_some()
                    {
                        fallback.next_cursor = None;
                        fallback.seen_cursors.clear();
                        fallback.listing_complete = true;
                        progress.truncated = true;
                        break;
                    }
                    match page_budget.observe_page(response.next_cursor) {
                        LineagePageAdvance::Complete => {
                            fallback.listing_complete = true;
                            break;
                        }
                        LineagePageAdvance::Continue(next_cursor) => {
                            fallback.next_cursor = Some(next_cursor);
                        }
                        LineagePageAdvance::Pause(next_cursor) => {
                            fallback.next_cursor = Some(next_cursor);
                            fallback.seen_cursors = page_budget.into_seen_cursors();
                            break;
                        }
                        LineagePageAdvance::CursorCycle(next_cursor) => {
                            tracing::warn!(
                                %next_cursor,
                                primary_thread_id = %primary_thread_id,
                                "loaded-thread fallback detected a cursor cycle"
                            );
                            fallback.next_cursor = None;
                            fallback.seen_cursors.clear();
                            had_fallback_cursor_cycle = true;
                            break;
                        }
                        LineagePageAdvance::Truncated => {
                            fallback.seen_cursors = page_budget.into_seen_cursors();
                            fallback.next_cursor = None;
                            fallback.listing_complete = true;
                            progress.truncated = true;
                            break;
                        }
                    }
                }
            }

            while refreshes_attempted < SUBAGENT_BACKFILL_REFRESHES_PER_ATTEMPT {
                let Some(thread_id) = fallback.pending_thread_ids.pop_front() else {
                    break;
                };
                refreshes_attempted += 1;
                match app_server
                    .thread_read(thread_id, /*include_turns*/ false)
                    .await
                {
                    Ok(thread) => {
                        for loaded in fallback.accumulator.ingest(vec![thread]) {
                            let thread_id = loaded.thread_id;
                            let retained = progress.retained_thread_ids.contains(&thread_id)
                                || progress.retain_thread_id(thread_id);
                            if retained && self.apply_loaded_subagent_thread(loaded) {
                                refreshed_thread_ids.insert(thread_id);
                            } else {
                                progress.truncated = true;
                            }
                        }
                    }
                    Err(err) => {
                        if Self::is_terminal_thread_read_error(&err) {
                            let existing_entry = self.agent_navigation.get(&thread_id).cloned();
                            if self.thread_event_channels.contains_key(&thread_id) {
                                if let Some(entry) = existing_entry {
                                    self.upsert_agent_picker_thread(
                                        thread_id,
                                        entry.agent_nickname,
                                        entry.agent_role,
                                        /*is_closed*/ true,
                                    );
                                } else {
                                    self.upsert_agent_picker_thread(
                                        thread_id, /*agent_nickname*/ None,
                                        /*agent_role*/ None, /*is_closed*/ true,
                                    );
                                }
                                self.agent_navigation
                                    .set_running(thread_id, /*is_running*/ false);
                            } else {
                                self.remove_agent_picker_thread(thread_id);
                            }
                            continue;
                        }
                        tracing::warn!(
                            %err,
                            %thread_id,
                            "loaded-thread fallback metadata read failed"
                        );
                        fallback.pending_thread_ids.push_front(thread_id);
                        progress.loaded_fallback = Some(fallback);
                        self.subagent_backfill_progress = Some(progress);
                        return LoadedSubagentBackfill {
                            status: LoadedSubagentBackfillStatus::RetryableError,
                            refreshed_thread_ids,
                            ..Default::default()
                        };
                    }
                }
            }

            if !fallback.listing_complete || !fallback.pending_thread_ids.is_empty() {
                progress.loaded_fallback = Some(fallback);
                self.subagent_backfill_progress = Some(progress);
                return LoadedSubagentBackfill {
                    status: if had_fallback_cursor_cycle {
                        LoadedSubagentBackfillStatus::CursorCycle
                    } else {
                        LoadedSubagentBackfillStatus::Paused
                    },
                    refreshed_thread_ids,
                    ..Default::default()
                };
            }
            if relation_list_failed {
                progress.loaded_fallback = Some(fallback);
            }
        }

        while refreshes_attempted < SUBAGENT_BACKFILL_REFRESHES_PER_ATTEMPT {
            let Some(thread_id) = progress.pending_refresh_thread_ids.pop_front() else {
                break;
            };
            refreshes_attempted += 1;
            let had_existing_entry = self.agent_navigation.get(&thread_id).is_some();
            match self
                .refresh_agent_picker_thread_liveness(app_server, thread_id)
                .await
            {
                ThreadLivenessRefreshOutcome::Refreshed => {
                    refreshed_thread_ids.insert(thread_id);
                }
                ThreadLivenessRefreshOutcome::TerminalPruned => {}
                ThreadLivenessRefreshOutcome::CapacityRejected => {
                    progress.truncated = true;
                }
                ThreadLivenessRefreshOutcome::RetryableError => {
                    if !had_existing_entry {
                        self.remove_agent_picker_thread(thread_id);
                    }
                    progress.pending_refresh_thread_ids.push_front(thread_id);
                    self.subagent_backfill_progress = Some(progress);
                    return LoadedSubagentBackfill {
                        status: LoadedSubagentBackfillStatus::RetryableError,
                        refreshed_thread_ids,
                        ..Default::default()
                    };
                }
            }
        }
        if !progress.pending_refresh_thread_ids.is_empty() {
            self.subagent_backfill_progress = Some(progress);
            return LoadedSubagentBackfill {
                status: LoadedSubagentBackfillStatus::Paused,
                refreshed_thread_ids,
                ..Default::default()
            };
        }
        if relation_list_failed {
            self.subagent_backfill_progress = Some(progress);
            return LoadedSubagentBackfill {
                status: LoadedSubagentBackfillStatus::RetryableError,
                refreshed_thread_ids,
                ..Default::default()
            };
        }
        if had_cursor_cycle {
            self.sync_active_agent_label();
            if progress.truncated {
                self.subagent_backfill_progress = Some(progress);
            }
            return LoadedSubagentBackfill {
                status: LoadedSubagentBackfillStatus::CursorCycle,
                refreshed_thread_ids,
                ..Default::default()
            };
        }

        let tracked_thread_ids = self
            .agent_navigation
            .tracked_thread_ids_bounded(SUBAGENT_BACKFILL_PAGE_SIZE as usize);
        for thread_id in tracked_thread_ids {
            if refreshes_attempted >= SUBAGENT_BACKFILL_REFRESHES_PER_ATTEMPT {
                break;
            }
            if thread_id == primary_thread_id
                || progress.accumulator.contains_accepted(thread_id)
                || refreshed_thread_ids.contains(&thread_id)
                || self.side_threads.contains_key(&thread_id)
                || self
                    .thread_event_channels
                    .get(&thread_id)
                    .is_some_and(|channel| channel.attachment() == ThreadEventAttachment::Live)
            {
                continue;
            }
            refreshes_attempted += 1;
            match self
                .refresh_agent_picker_thread_liveness(app_server, thread_id)
                .await
            {
                ThreadLivenessRefreshOutcome::Refreshed => {
                    refreshed_thread_ids.insert(thread_id);
                }
                ThreadLivenessRefreshOutcome::TerminalPruned => {}
                ThreadLivenessRefreshOutcome::CapacityRejected => {
                    progress.truncated = true;
                }
                ThreadLivenessRefreshOutcome::RetryableError => {
                    self.subagent_backfill_progress = Some(progress);
                    return LoadedSubagentBackfill {
                        status: LoadedSubagentBackfillStatus::RetryableError,
                        refreshed_thread_ids,
                        ..Default::default()
                    };
                }
            }
        }

        self.sync_active_agent_label();

        let truncated = progress.truncated;
        if truncated {
            self.subagent_backfill_progress = Some(progress);
        }
        LoadedSubagentBackfill {
            completed: true,
            status: if truncated {
                LoadedSubagentBackfillStatus::Truncated
            } else {
                LoadedSubagentBackfillStatus::Complete
            },
            refreshed_thread_ids,
        }
    }

    pub(super) fn apply_loaded_subagent_thread(&mut self, thread: LoadedSubagentThread) -> bool {
        let agent_path = thread.agent_path;
        let has_live_channel = self
            .thread_event_channels
            .get(&thread.thread_id)
            .is_some_and(|channel| channel.attachment() == ThreadEventAttachment::Live);
        let is_closed = !has_live_channel && thread.is_closed;
        let accepted = if thread.is_running || has_live_channel {
            self.upsert_agent_picker_thread_retaining(
                thread.thread_id,
                thread.agent_nickname,
                thread.agent_role,
                is_closed,
            )
            .accepted()
        } else {
            self.upsert_agent_picker_thread(
                thread.thread_id,
                thread.agent_nickname,
                thread.agent_role,
                is_closed,
            )
        };
        if !accepted {
            return false;
        }
        if thread.blocks_direct_input {
            self.agent_navigation.mark_parent_owned(thread.thread_id);
        }
        self.agent_navigation
            .set_agent_path(thread.thread_id, agent_path);
        if !has_live_channel {
            if thread.is_running {
                self.agent_navigation.mark_running(thread.thread_id);
            } else {
                self.agent_navigation
                    .set_running(thread.thread_id, /*is_running*/ false);
            }
        }
        true
    }

    fn stage_loaded_subagent_threads(
        &mut self,
        threads: impl IntoIterator<Item = LoadedSubagentThread>,
        pending_refresh_thread_ids: &mut VecDeque<ThreadId>,
        refreshed_thread_ids: &mut HashSet<ThreadId>,
    ) -> bool {
        let mut truncated = false;
        for thread in threads {
            let thread_id = thread.thread_id;
            let has_live_channel = self
                .thread_event_channels
                .get(&thread_id)
                .is_some_and(|channel| channel.attachment() == ThreadEventAttachment::Live);
            let requires_authoritative_refresh = !has_live_channel
                && !thread.has_authoritative_input_capability
                && thread
                    .agent_path
                    .as_deref()
                    .is_none_or(|agent_path| agent_path.trim().is_empty());
            if requires_authoritative_refresh {
                pending_refresh_thread_ids.push_back(thread_id);
            } else {
                let accepted = self.apply_loaded_subagent_thread(thread);
                truncated |= !accepted;
                if accepted && !has_live_channel {
                    refreshed_thread_ids.insert(thread_id);
                }
            }
        }
        truncated
    }

    /// Returns the adjacent thread id for keyboard navigation, backfilling from the server if the
    /// local cache has no neighbor.
    ///
    /// Tries the fast path first: ask `AgentNavigationState` directly. If it returns `None` (no
    /// adjacent entry exists, typically because the cache was never populated with remote
    /// subagents), performs a full `backfill_loaded_subagent_threads` and retries. This ensures the
    /// first next/previous keypress in a resumed remote session discovers subagents on demand
    /// without requiring the user to wait for a proactive fetch.
    pub(super) async fn adjacent_thread_id_with_backfill(
        &mut self,
        app_server: &mut AppServerSession,
        direction: AgentNavigationDirection,
    ) -> Option<ThreadId> {
        let current_thread = self.current_displayed_thread_id();
        if let Some(thread_id) = self
            .agent_navigation
            .adjacent_thread_id(current_thread, direction)
        {
            return Some(thread_id);
        }

        let primary_thread_id = self.primary_thread_id?;
        if self.last_subagent_backfill_attempt == Some(primary_thread_id) {
            return None;
        }

        if self
            .backfill_loaded_subagent_threads(app_server)
            .await
            .completed
        {
            self.last_subagent_backfill_attempt = Some(primary_thread_id);
        }
        self.agent_navigation
            .adjacent_thread_id(self.current_displayed_thread_id(), direction)
    }

    pub(super) fn fresh_session_config(&self) -> Config {
        let mut config = self.config.clone();
        config.service_tier = self.chat_widget.configured_service_tier();
        config
    }
    pub(super) async fn resume_target_session(
        &mut self,
        tui: &mut tui::Tui,
        app_server: &mut AppServerSession,
        target_session: SessionTarget,
    ) -> Result<AppRunControl> {
        if self.ignore_same_thread_resume(&target_session) {
            tui.frame_requester().schedule_frame();
            return Ok(AppRunControl::Continue);
        }

        self.refresh_in_memory_config_from_disk_best_effort("resuming a thread")
            .await;
        let cwd_override = self
            .harness_overrides
            .cwd
            .as_deref()
            .or_else(|| app_server.remote_cwd_override());
        let resume_cwd_mode = crate::session_resume::effective_resume_cwd_mode(
            self.config.tui_resume_cwd,
            cwd_override,
        );
        let remembered_current_cwd = cwd_override.unwrap_or(self.launch_cwd.as_path());
        let current_cwd = if matches!(resume_cwd_mode, Some(ResumeCwdMode::Current)) {
            remembered_current_cwd.to_path_buf()
        } else {
            self.config.cwd.to_path_buf()
        };
        let uses_remote_workspace_or_environment = crate::uses_remote_workspace_or_environment(
            &self.app_server_target,
            &self.environment_manager,
        );
        if uses_remote_workspace_or_environment
            && self.harness_overrides.cwd.is_none()
            && app_server.remote_cwd_override().is_none()
            && matches!(resume_cwd_mode, Some(ResumeCwdMode::Current))
        {
            self.chat_widget.add_error_message(
                "`tui.resume_cwd = \"current\"` requires `--cd` when using a remote workspace"
                    .to_string(),
            );
            return Ok(AppRunControl::Continue);
        }
        let resume_cwd = if self.app_server_target.uses_remote_workspace() {
            current_cwd.clone()
        } else {
            let outcome = crate::session_resume::resolve_cwd_for_resume_or_fork(
                tui,
                &self.config,
                self.state_db.as_deref(),
                &target_session,
                CwdPromptAction::Resume,
                crate::session_resume::ResumeCwdContext {
                    current_cwd: &current_cwd,
                    remembered_current_cwd,
                    allow_remember_current: !uses_remote_workspace_or_environment
                        || cwd_override.is_some(),
                    mode: resume_cwd_mode,
                },
            )
            .await;
            match outcome {
                Err(err) => {
                    self.chat_widget.add_error_message(format!(
                        "Failed to determine working directory for resume: {err}"
                    ));
                    return Ok(AppRunControl::Continue);
                }
                Ok(crate::session_resume::ResolveCwdOutcome::Continue(Some(cwd))) => cwd,
                Ok(crate::session_resume::ResolveCwdOutcome::Continue(None)) => current_cwd.clone(),
                Ok(crate::session_resume::ResolveCwdOutcome::Exit) => {
                    return Ok(AppRunControl::Exit(ExitReason::UserRequested));
                }
            }
        };

        let (config_current_cwd, config_resume_cwd) =
            if self.app_server_target.uses_remote_workspace() {
                let local_config_cwd = self.config.cwd.to_path_buf();
                (local_config_cwd.clone(), local_config_cwd)
            } else {
                (current_cwd, resume_cwd)
            };
        let mut resume_config = match self
            .rebuild_config_for_resume_or_fallback(&config_current_cwd, config_resume_cwd)
            .await
        {
            Ok(cfg) => cfg,
            Err(err) => {
                self.chat_widget.add_error_message(format!(
                    "Failed to rebuild configuration for resume: {err}"
                ));
                return Ok(AppRunControl::Continue);
            }
        };
        self.apply_runtime_policy_overrides(&mut resume_config);

        let summary = session_summary(
            self.chat_widget.token_usage(),
            self.chat_widget.thread_id(),
            self.chat_widget.thread_name(),
            self.chat_widget.rollout_path().as_deref(),
        );
        match app_server
            .resume_thread(
                resume_config.clone(),
                target_session.thread_id,
                self.resume_model_settings(),
            )
            .await
        {
            Ok(resumed) => {
                let resumed_thread_id = resumed.session.thread_id;
                self.shutdown_current_thread(app_server).await;
                self.config = resume_config;
                tui.set_notification_settings(
                    self.config.tui_notifications.method,
                    self.config.tui_notifications.condition,
                );
                self.file_search
                    .update_search_dir(self.config.cwd.to_path_buf());
                match self
                    .replace_chat_widget_with_app_server_thread(
                        tui,
                        resumed,
                        ThreadAttachPresentation::SessionLineage,
                        /*initial_user_message*/ None,
                    )
                    .await
                {
                    Ok(()) => {
                        self.backfill_loaded_subagent_threads(app_server).await;
                        if let Some(summary) = summary {
                            let mut lines: Vec<Line<'static>> = Vec::new();
                            if let Some(usage_line) = summary.usage_line {
                                lines.push(usage_line.into());
                            }
                            if let Some(command) = summary.resume_hint {
                                let spans =
                                    vec!["To continue this session, run ".into(), command.cyan()];
                                lines.push(spans.into());
                            }
                            self.chat_widget.add_plain_history_lines(lines);
                        }
                        self.maybe_prompt_resume_paused_goal_after_resume(
                            app_server,
                            resumed_thread_id,
                        )
                        .await;
                    }
                    Err(err) => {
                        self.chat_widget.add_error_message(format!(
                            "Failed to attach to resumed app-server thread: {err}"
                        ));
                    }
                }
            }
            Err(err) => {
                let path_display = target_session.display_label();
                self.chat_widget.add_error_message(format!(
                    "Failed to resume session from {path_display}: {err}"
                ));
            }
        }

        Ok(AppRunControl::Continue)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn terminal_thread_read_error_detection_matches_not_loaded_errors() {
        let err = color_eyre::eyre::eyre!(
            "thread/read failed during TUI session lookup: thread/read failed: thread not loaded: thr_123"
        );

        assert!(App::is_terminal_thread_read_error(&err));
    }

    #[test]
    fn terminal_thread_read_error_detection_ignores_transient_failures() {
        let err = color_eyre::eyre::eyre!(
            "thread/read failed during TUI session lookup: thread/read transport error: broken pipe"
        );

        assert!(!App::is_terminal_thread_read_error(&err));
    }

    #[test]
    fn closed_state_for_thread_read_error_preserves_live_state_without_cache_on_transient_error() {
        let err = color_eyre::eyre::eyre!(
            "thread/read failed during TUI session lookup: thread/read transport error: broken pipe"
        );

        assert!(!App::closed_state_for_thread_read_error(
            &err, /*existing_is_closed*/ None
        ));
    }

    #[test]
    fn closed_state_for_thread_read_error_marks_terminal_uncached_threads_closed() {
        let err = color_eyre::eyre::eyre!(
            "thread/read failed during TUI session lookup: thread/read failed: thread not loaded: thr_123"
        );

        assert!(App::closed_state_for_thread_read_error(
            &err, /*existing_is_closed*/ None
        ));
    }

    #[test]
    fn include_turns_fallback_detection_handles_unmaterialized_and_ephemeral_threads() {
        let unmaterialized = color_eyre::eyre::eyre!(
            "thread/read failed during TUI session lookup: thread/read failed: thread thr_123 is not materialized yet; includeTurns is unavailable before first user message"
        );
        let ephemeral = color_eyre::eyre::eyre!(
            "thread/read failed during TUI session lookup: thread/read failed: ephemeral threads do not support includeTurns"
        );

        assert!(App::can_fallback_from_include_turns_error(&unmaterialized));
        assert!(App::can_fallback_from_include_turns_error(&ephemeral));
    }

    #[test]
    fn truncated_incomplete_picker_subtitle_reports_omission_and_retry() {
        let subtitle = agent_picker_subtitle(
            /*lineage_truncated*/ true,
            /*picker_has_more*/ false,
            &LoadedSubagentBackfillStatus::RetryableError,
        );

        assert_eq!(
            subtitle,
            format!(
                "Additional rows were omitted at the {MAX_RETAINED_SUBAGENT_LINEAGE}-agent lineage safety limit. {} Retained rows still need refresh; reopen to continue or retry.",
                AgentNavigationState::picker_subtitle()
            )
        );
    }
}
