use crate::protocol::common::ServerNotification;
use crate::protocol::item_builders::build_command_execution_begin_item;
use crate::protocol::item_builders::build_command_execution_end_item;
use crate::protocol::item_builders::convert_patch_changes;
use crate::protocol::v2::AgentMessageDeltaNotification;
use crate::protocol::v2::CollabAgentState;
use crate::protocol::v2::CollabAgentTool;
use crate::protocol::v2::CollabAgentToolCallStatus;
use crate::protocol::v2::CommandExecutionOutputDeltaNotification;
use crate::protocol::v2::DynamicToolCallOutputContentItem;
use crate::protocol::v2::DynamicToolCallStatus;
use crate::protocol::v2::FileChangePatchUpdatedNotification;
use crate::protocol::v2::ItemCompletedNotification;
use crate::protocol::v2::ItemStartedNotification;
use crate::protocol::v2::PlanDeltaNotification;
use crate::protocol::v2::ReasoningSummaryPartAddedNotification;
use crate::protocol::v2::ReasoningSummaryTextDeltaNotification;
use crate::protocol::v2::ReasoningTextDeltaNotification;
use crate::protocol::v2::TerminalInteractionNotification;
use crate::protocol::v2::ThreadItem;
use codex_protocol::dynamic_tools::DynamicToolCallOutputContentItem as CoreDynamicToolCallOutputContentItem;
use codex_protocol::protocol::EventMsg;
use std::collections::HashMap;

/// Build the v2 app-server notification that directly corresponds to a single core event.
///
/// This only covers the stateless event-to-notification projections that have a one-to-one
/// mapping. Callers remain responsible for any surrounding state checks or side effects before
/// invoking this helper. Returns `None` for core events that do not have a direct v2 item
/// notification mapping so new or unrelated [`EventMsg`] variants cannot panic the app-server
/// hot path.
pub fn item_event_to_server_notification(
    msg: EventMsg,
    thread_id: &str,
    turn_id: &str,
) -> Option<ServerNotification> {
    let thread_id = thread_id.to_string();
    let turn_id = turn_id.to_string();
    let notification = match msg {
        EventMsg::DynamicToolCallResponse(response) => {
            let status = if response.success {
                DynamicToolCallStatus::Completed
            } else {
                DynamicToolCallStatus::Failed
            };
            let duration_ms = i64::try_from(response.duration.as_millis()).ok();
            let item = ThreadItem::DynamicToolCall {
                id: response.call_id,
                namespace: response.namespace,
                tool: response.tool,
                arguments: response.arguments,
                status,
                content_items: Some(
                    response
                        .content_items
                        .into_iter()
                        .map(|item| match item {
                            CoreDynamicToolCallOutputContentItem::InputText { text } => {
                                DynamicToolCallOutputContentItem::InputText { text }
                            }
                            CoreDynamicToolCallOutputContentItem::InputImage {
                                image_url,
                                detail,
                            } => DynamicToolCallOutputContentItem::InputImage { image_url, detail },
                            CoreDynamicToolCallOutputContentItem::InputAudio { audio_url } => {
                                DynamicToolCallOutputContentItem::InputAudio { audio_url }
                            }
                        })
                        .collect(),
                ),
                success: Some(response.success),
                duration_ms,
            };
            ServerNotification::ItemCompleted(ItemCompletedNotification {
                thread_id,
                turn_id: response.turn_id,
                item,
                completed_at_ms: response.completed_at_ms,
            })
        }
        EventMsg::CollabAgentSpawnBegin(begin_event) => {
            let (requested_model, requested_reasoning_effort) =
                begin_event.canonical_requested_identity();
            let item = ThreadItem::CollabAgentToolCall {
                id: begin_event.call_id,
                tool: CollabAgentTool::SpawnAgent,
                status: CollabAgentToolCallStatus::InProgress,
                sender_thread_id: begin_event.sender_thread_id.to_string(),
                receiver_thread_ids: Vec::new(),
                prompt: Some(begin_event.prompt),
                model: requested_model.clone(),
                reasoning_effort: requested_reasoning_effort.clone(),
                requested_model,
                requested_reasoning_effort,
                effective_model: None,
                effective_reasoning_effort: None,
                agents_states: HashMap::new(),
                wake_notifications: None,
            };
            ServerNotification::ItemStarted(ItemStartedNotification {
                thread_id,
                turn_id,
                item,
                started_at_ms: begin_event.started_at_ms,
            })
        }
        EventMsg::CollabAgentSpawnEnd(end_event) => {
            let (effective_model, effective_reasoning_effort) =
                end_event.canonical_effective_identity();
            let has_receiver = end_event.new_thread_id.is_some();
            let status = match &end_event.status {
                codex_protocol::protocol::AgentStatus::Errored(_)
                | codex_protocol::protocol::AgentStatus::NotFound => {
                    CollabAgentToolCallStatus::Failed
                }
                _ if has_receiver => CollabAgentToolCallStatus::Completed,
                _ => CollabAgentToolCallStatus::Failed,
            };
            let (receiver_thread_ids, agents_states) = match end_event.new_thread_id {
                Some(id) => {
                    let receiver_id = id.to_string();
                    let received_status = CollabAgentState::from(end_event.status.clone());
                    (
                        vec![receiver_id.clone()],
                        [(receiver_id, received_status)].into_iter().collect(),
                    )
                }
                None => (Vec::new(), HashMap::new()),
            };
            let item = ThreadItem::CollabAgentToolCall {
                id: end_event.call_id,
                tool: CollabAgentTool::SpawnAgent,
                status,
                sender_thread_id: end_event.sender_thread_id.to_string(),
                receiver_thread_ids,
                prompt: Some(end_event.prompt),
                // Historic V1 terminal fields are the established observed aliases.
                // The event has no caller-request provenance, so requested* remains
                // absent rather than being fabricated from the observed snapshot.
                model: effective_model.clone(),
                reasoning_effort: effective_reasoning_effort.clone(),
                requested_model: None,
                requested_reasoning_effort: None,
                effective_model,
                effective_reasoning_effort,
                agents_states,
            };
            ServerNotification::ItemCompleted(ItemCompletedNotification {
                thread_id,
                turn_id,
                item,
                completed_at_ms: end_event.completed_at_ms,
            })
        }
        EventMsg::CollabAgentInteractionBegin(begin_event) => {
            let receiver_thread_ids = vec![begin_event.receiver_thread_id.to_string()];
            let item = ThreadItem::CollabAgentToolCall {
                id: begin_event.call_id,
                tool: CollabAgentTool::SendInput,
                status: CollabAgentToolCallStatus::InProgress,
                sender_thread_id: begin_event.sender_thread_id.to_string(),
                receiver_thread_ids,
                prompt: Some(begin_event.prompt),
                model: None,
                reasoning_effort: None,
                requested_model: None,
                requested_reasoning_effort: None,
                effective_model: None,
                effective_reasoning_effort: None,
                agents_states: HashMap::new(),
                wake_notifications: None,
            };
            ServerNotification::ItemStarted(ItemStartedNotification {
                thread_id,
                turn_id,
                item,
                started_at_ms: begin_event.started_at_ms,
            })
        }
        EventMsg::CollabAgentInteractionEnd(end_event) => {
            let status = match &end_event.status {
                codex_protocol::protocol::AgentStatus::Errored(_)
                | codex_protocol::protocol::AgentStatus::NotFound => {
                    CollabAgentToolCallStatus::Failed
                }
                _ => CollabAgentToolCallStatus::Completed,
            };
            let receiver_id = end_event.receiver_thread_id.to_string();
            let received_status = CollabAgentState::from(end_event.status);
            let item = ThreadItem::CollabAgentToolCall {
                id: end_event.call_id,
                tool: CollabAgentTool::SendInput,
                status,
                sender_thread_id: end_event.sender_thread_id.to_string(),
                receiver_thread_ids: vec![receiver_id.clone()],
                prompt: Some(end_event.prompt),
                model: None,
                reasoning_effort: None,
                requested_model: None,
                requested_reasoning_effort: None,
                effective_model: None,
                effective_reasoning_effort: None,
                agents_states: [(receiver_id, received_status)].into_iter().collect(),
                wake_notifications: None,
            };
            ServerNotification::ItemCompleted(ItemCompletedNotification {
                thread_id,
                turn_id,
                item,
                completed_at_ms: end_event.completed_at_ms,
            })
        }
        EventMsg::SubAgentActivity(activity) => {
            let item = ThreadItem::SubAgentActivity {
                id: activity.event_id,
                kind: activity.kind.into(),
                agent_thread_id: activity.agent_thread_id.to_string(),
                agent_path: String::from(activity.agent_path),
                model: activity.model,
                reasoning_effort: activity.reasoning_effort,
            };
            ServerNotification::ItemCompleted(ItemCompletedNotification {
                thread_id,
                turn_id,
                item,
                completed_at_ms: activity.occurred_at_ms,
            })
        }
        EventMsg::CollabWaitingBegin(begin_event) => {
            let receiver_thread_ids = begin_event
                .receiver_thread_ids
                .iter()
                .map(ToString::to_string)
                .collect();
            let item = ThreadItem::CollabAgentToolCall {
                id: begin_event.call_id,
                tool: CollabAgentTool::Wait,
                status: CollabAgentToolCallStatus::InProgress,
                sender_thread_id: begin_event.sender_thread_id.to_string(),
                receiver_thread_ids,
                prompt: None,
                model: None,
                reasoning_effort: None,
                requested_model: None,
                requested_reasoning_effort: None,
                effective_model: None,
                effective_reasoning_effort: None,
                agents_states: HashMap::new(),
                wake_notifications: None,
            };
            ServerNotification::ItemStarted(ItemStartedNotification {
                thread_id,
                turn_id,
                item,
                started_at_ms: begin_event.started_at_ms,
            })
        }
        EventMsg::CollabWaitingEnd(end_event) => {
            let status = if end_event.statuses.values().any(|status| {
                matches!(
                    status,
                    codex_protocol::protocol::AgentStatus::Errored(_)
                        | codex_protocol::protocol::AgentStatus::NotFound
                )
            }) {
                CollabAgentToolCallStatus::Failed
            } else {
                CollabAgentToolCallStatus::Completed
            };
            let mut receiver_thread_ids = end_event
                .statuses
                .keys()
                .map(ToString::to_string)
                .collect::<Vec<_>>();
            receiver_thread_ids.sort();
            let agents_states = end_event
                .statuses
                .iter()
                .map(|(id, status)| (id.to_string(), CollabAgentState::from(status.clone())))
                .collect();
            let item = ThreadItem::CollabAgentToolCall {
                id: end_event.call_id,
                tool: CollabAgentTool::Wait,
                status,
                sender_thread_id: end_event.sender_thread_id.to_string(),
                receiver_thread_ids,
                prompt: None,
                model: None,
                reasoning_effort: None,
                requested_model: None,
                requested_reasoning_effort: None,
                effective_model: None,
                effective_reasoning_effort: None,
                agents_states,
            };
            ServerNotification::ItemCompleted(ItemCompletedNotification {
                thread_id,
                turn_id,
                item,
                completed_at_ms: end_event.completed_at_ms,
            })
        }
        EventMsg::CollabCloseBegin(begin_event) => {
            let item = ThreadItem::CollabAgentToolCall {
                id: begin_event.call_id,
                tool: CollabAgentTool::CloseAgent,
                status: CollabAgentToolCallStatus::InProgress,
                sender_thread_id: begin_event.sender_thread_id.to_string(),
                receiver_thread_ids: vec![begin_event.receiver_thread_id.to_string()],
                prompt: None,
                model: None,
                reasoning_effort: None,
                requested_model: None,
                requested_reasoning_effort: None,
                effective_model: None,
                effective_reasoning_effort: None,
                agents_states: HashMap::new(),
                wake_notifications: None,
            };
            ServerNotification::ItemStarted(ItemStartedNotification {
                thread_id,
                turn_id,
                item,
                started_at_ms: begin_event.started_at_ms,
            })
        }
        EventMsg::CollabCloseEnd(end_event) => {
            let status = match &end_event.status {
                codex_protocol::protocol::AgentStatus::Errored(_)
                | codex_protocol::protocol::AgentStatus::NotFound => {
                    CollabAgentToolCallStatus::Failed
                }
                _ => CollabAgentToolCallStatus::Completed,
            };
            let receiver_id = end_event.receiver_thread_id.to_string();
            let agents_states = [(
                receiver_id.clone(),
                CollabAgentState::from(end_event.status),
            )]
            .into_iter()
            .collect();
            let item = ThreadItem::CollabAgentToolCall {
                id: end_event.call_id,
                tool: CollabAgentTool::CloseAgent,
                status,
                sender_thread_id: end_event.sender_thread_id.to_string(),
                receiver_thread_ids: vec![receiver_id],
                prompt: None,
                model: None,
                reasoning_effort: None,
                requested_model: None,
                requested_reasoning_effort: None,
                effective_model: None,
                effective_reasoning_effort: None,
                agents_states,
            };
            ServerNotification::ItemCompleted(ItemCompletedNotification {
                thread_id,
                turn_id,
                item,
                completed_at_ms: end_event.completed_at_ms,
            })
        }
        EventMsg::CollabResumeBegin(begin_event) => {
            let item = ThreadItem::CollabAgentToolCall {
                id: begin_event.call_id,
                tool: CollabAgentTool::ResumeAgent,
                status: CollabAgentToolCallStatus::InProgress,
                sender_thread_id: begin_event.sender_thread_id.to_string(),
                receiver_thread_ids: vec![begin_event.receiver_thread_id.to_string()],
                prompt: None,
                model: None,
                reasoning_effort: None,
                requested_model: None,
                requested_reasoning_effort: None,
                effective_model: None,
                effective_reasoning_effort: None,
                agents_states: HashMap::new(),
                wake_notifications: None,
            };
            ServerNotification::ItemStarted(ItemStartedNotification {
                thread_id,
                turn_id,
                item,
                started_at_ms: begin_event.started_at_ms,
            })
        }
        EventMsg::CollabResumeEnd(end_event) => {
            let status = match &end_event.status {
                codex_protocol::protocol::AgentStatus::Errored(_)
                | codex_protocol::protocol::AgentStatus::NotFound => {
                    CollabAgentToolCallStatus::Failed
                }
                _ => CollabAgentToolCallStatus::Completed,
            };
            let receiver_id = end_event.receiver_thread_id.to_string();
            let agents_states = [(
                receiver_id.clone(),
                CollabAgentState::from(end_event.status),
            )]
            .into_iter()
            .collect();
            let item = ThreadItem::CollabAgentToolCall {
                id: end_event.call_id,
                tool: CollabAgentTool::ResumeAgent,
                status,
                sender_thread_id: end_event.sender_thread_id.to_string(),
                receiver_thread_ids: vec![receiver_id],
                prompt: None,
                model: None,
                reasoning_effort: None,
                requested_model: None,
                requested_reasoning_effort: None,
                effective_model: None,
                effective_reasoning_effort: None,
                agents_states,
            };
            ServerNotification::ItemCompleted(ItemCompletedNotification {
                thread_id,
                turn_id,
                item,
                completed_at_ms: end_event.completed_at_ms,
            })
        }
        EventMsg::AgentMessageContentDelta(event) => {
            let codex_protocol::protocol::AgentMessageContentDeltaEvent { item_id, delta, .. } =
                event;
            ServerNotification::AgentMessageDelta(AgentMessageDeltaNotification {
                thread_id,
                turn_id,
                item_id,
                delta,
            })
        }
        EventMsg::PlanDelta(event) => ServerNotification::PlanDelta(PlanDeltaNotification {
            thread_id,
            turn_id,
            item_id: event.item_id,
            delta: event.delta,
        }),
        EventMsg::ReasoningContentDelta(event) => {
            ServerNotification::ReasoningSummaryTextDelta(ReasoningSummaryTextDeltaNotification {
                thread_id,
                turn_id,
                item_id: event.item_id,
                delta: event.delta,
                summary_index: event.summary_index,
            })
        }
        EventMsg::ReasoningRawContentDelta(event) => {
            ServerNotification::ReasoningTextDelta(ReasoningTextDeltaNotification {
                thread_id,
                turn_id,
                item_id: event.item_id,
                delta: event.delta,
                content_index: event.content_index,
            })
        }
        EventMsg::AgentReasoningSectionBreak(event) => {
            ServerNotification::ReasoningSummaryPartAdded(ReasoningSummaryPartAddedNotification {
                thread_id,
                turn_id,
                item_id: event.item_id,
                summary_index: event.summary_index,
            })
        }
        EventMsg::ItemStarted(item_started_event) => {
            ServerNotification::ItemStarted(ItemStartedNotification {
                thread_id,
                turn_id,
                item: item_started_event.item.into(),
                started_at_ms: item_started_event.started_at_ms,
            })
        }
        EventMsg::ItemCompleted(item_completed_event) => {
            ServerNotification::ItemCompleted(ItemCompletedNotification {
                thread_id,
                turn_id,
                item: item_completed_event.item.into(),
                completed_at_ms: item_completed_event.completed_at_ms,
            })
        }
        EventMsg::PatchApplyUpdated(event) => {
            ServerNotification::FileChangePatchUpdated(FileChangePatchUpdatedNotification {
                thread_id,
                turn_id,
                item_id: event.call_id,
                changes: convert_patch_changes(&event.changes),
            })
        }
        EventMsg::ExecCommandBegin(exec_command_begin_event) => {
            ServerNotification::ItemStarted(ItemStartedNotification {
                thread_id,
                turn_id,
                item: build_command_execution_begin_item(&exec_command_begin_event),
                started_at_ms: exec_command_begin_event.started_at_ms,
            })
        }
        EventMsg::ExecCommandOutputDelta(exec_command_output_delta_event) => {
            let item_id = exec_command_output_delta_event.call_id;
            let delta = String::from_utf8_lossy(&exec_command_output_delta_event.chunk).to_string();
            ServerNotification::CommandExecutionOutputDelta(
                CommandExecutionOutputDeltaNotification {
                    thread_id,
                    turn_id,
                    item_id,
                    delta,
                },
            )
        }
        EventMsg::TerminalInteraction(terminal_event) => {
            ServerNotification::TerminalInteraction(TerminalInteractionNotification {
                thread_id,
                turn_id,
                item_id: terminal_event.call_id,
                process_id: terminal_event.process_id,
                stdin: terminal_event.stdin,
                terminal_wait: terminal_event.terminal_wait.map(Into::into),
            })
        }
        EventMsg::ExecCommandEnd(exec_command_end_event) => {
            ServerNotification::ItemCompleted(ItemCompletedNotification {
                thread_id,
                turn_id,
                item: build_command_execution_end_item(&exec_command_end_event),
                completed_at_ms: exec_command_end_event.completed_at_ms,
            })
        }
        _ => return None,
    };
    Some(notification)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::protocol::v2::TerminalWaitInfo;
    use crate::protocol::v2::TerminalWaitPrimitive;
    use codex_protocol::ThreadId;
    use codex_protocol::items::CollabAgentTool as CoreCollabAgentTool;
    use codex_protocol::items::CollabAgentToolCallItem as CoreCollabAgentToolCallItem;
    use codex_protocol::items::CollabAgentToolCallStatus as CoreCollabAgentToolCallStatus;
    use codex_protocol::items::TurnItem as CoreTurnItem;
    use codex_protocol::protocol::AgentStatus;
    use codex_protocol::protocol::CollabAgentSpawnEndEvent;
    use codex_protocol::protocol::CollabResumeBeginEvent;
    use codex_protocol::protocol::CollabResumeEndEvent;
    use codex_protocol::protocol::ExecCommandOutputDeltaEvent;
    use codex_protocol::protocol::ExecOutputStream;
    use codex_protocol::protocol::HasLegacyEvent;
    use codex_protocol::protocol::ItemCompletedEvent;
    use codex_protocol::protocol::ItemStartedEvent;
    use codex_protocol::protocol::TerminalInteractionEvent;
    use codex_protocol::protocol::TerminalWaitInfo as CoreTerminalWaitInfo;
    use codex_protocol::protocol::TerminalWaitPrimitive as CoreTerminalWaitPrimitive;
    use pretty_assertions::assert_eq;

    fn assert_item_started_server_notification(
        notification: Option<ServerNotification>,
        expected: ItemStartedNotification,
    ) {
        match notification.expect("supported event should map to notification") {
            ServerNotification::ItemStarted(payload) => assert_eq!(payload, expected),
            other => panic!("expected item started notification, got {other:?}"),
        }
    }

    fn assert_item_completed_server_notification(
        notification: Option<ServerNotification>,
        expected: ItemCompletedNotification,
    ) {
        match notification.expect("supported event should map to notification") {
            ServerNotification::ItemCompleted(payload) => assert_eq!(payload, expected),
            other => panic!("expected item completed notification, got {other:?}"),
        }
    }

    fn assert_command_execution_output_delta_server_notification(
        notification: Option<ServerNotification>,
        expected: CommandExecutionOutputDeltaNotification,
    ) {
        match notification.expect("supported event should map to notification") {
            ServerNotification::CommandExecutionOutputDelta(payload) => {
                assert_eq!(payload, expected)
            }
            other => panic!("expected command execution output delta, got {other:?}"),
        }
    }

    fn assert_terminal_interaction_server_notification(
        notification: Option<ServerNotification>,
        expected: TerminalInteractionNotification,
    ) {
        match notification.expect("supported event should map to notification") {
            ServerNotification::TerminalInteraction(payload) => assert_eq!(payload, expected),
            other => panic!("expected terminal interaction notification, got {other:?}"),
        }
    }

    #[test]
    fn unsupported_event_returns_none_instead_of_panicking() {
        assert!(
            item_event_to_server_notification(EventMsg::ShutdownComplete, "thread-1", "turn-1",)
                .is_none()
        );
    }

    #[test]
    fn collab_resume_begin_maps_to_item_started_resume_agent() {
        let event = CollabResumeBeginEvent {
            call_id: "call-1".to_string(),
            started_at_ms: 123,
            sender_thread_id: ThreadId::new(),
            receiver_thread_id: ThreadId::new(),
            receiver_agent_nickname: None,
            receiver_agent_role: None,
        };

        let notification = item_event_to_server_notification(
            EventMsg::CollabResumeBegin(event.clone()),
            "thread-1",
            "turn-1",
        );
        assert_item_started_server_notification(
            notification,
            ItemStartedNotification {
                thread_id: "thread-1".to_string(),
                turn_id: "turn-1".to_string(),
                started_at_ms: event.started_at_ms,
                item: ThreadItem::CollabAgentToolCall {
                    id: event.call_id,
                    tool: CollabAgentTool::ResumeAgent,
                    status: CollabAgentToolCallStatus::InProgress,
                    sender_thread_id: event.sender_thread_id.to_string(),
                    receiver_thread_ids: vec![event.receiver_thread_id.to_string()],
                    prompt: None,
                    model: None,
                    reasoning_effort: None,
                    requested_model: None,
                    requested_reasoning_effort: None,
                    effective_model: None,
                    effective_reasoning_effort: None,
                    agents_states: HashMap::new(),
                    wake_notifications: None,
                },
            },
        );
    }

    #[test]
    fn collab_spawn_identity_is_phase_compatible_across_current_and_historic_protocol_conversions()
    {
        let sender_thread_id = ThreadId::new();
        let receiver_thread_id = ThreadId::new();
        let requested_model = "gpt-requested".to_string();
        let requested_reasoning_effort = codex_protocol::openai_models::ReasoningEffort::High;
        let effective_model = "gpt-effective".to_string();
        let effective_reasoning_effort = codex_protocol::openai_models::ReasoningEffort::Medium;

        let started = item_event_to_server_notification(
            EventMsg::ItemStarted(ItemStartedEvent {
                thread_id: sender_thread_id,
                turn_id: "turn-phase-compatible".into(),
                started_at_ms: 1,
                item: CoreTurnItem::CollabAgentToolCall(CoreCollabAgentToolCallItem {
                    id: "spawn-phase-compatible".into(),
                    tool: CoreCollabAgentTool::SpawnAgent,
                    status: CoreCollabAgentToolCallStatus::InProgress,
                    sender_thread_id,
                    receiver_thread_ids: Vec::new(),
                    receiver_agents: Vec::new(),
                    prompt: Some("inspect".into()),
                    model: None,
                    reasoning_effort: None,
                    requested_model: Some(requested_model.clone()),
                    requested_reasoning_effort: Some(requested_reasoning_effort.clone()),
                    agents_states: HashMap::new(),
                    wake_notifications: None,
                }),
            }),
            "thread-phase-compatible",
            "turn-phase-compatible",
        );
        let Some(ServerNotification::ItemStarted(ItemStartedNotification { item, .. })) = started
        else {
            panic!("current spawn start must map to item/started");
        };
        let ThreadItem::CollabAgentToolCall {
            model,
            reasoning_effort,
            requested_model: start_requested_model,
            requested_reasoning_effort: start_requested_reasoning_effort,
            effective_model: start_effective_model,
            effective_reasoning_effort: start_effective_reasoning_effort,
            ..
        } = item
        else {
            panic!("current spawn start must map to a collab item");
        };
        assert_eq!(model, Some(requested_model.clone()));
        assert_eq!(reasoning_effort, Some(requested_reasoning_effort.clone()));
        assert_eq!(start_requested_model, Some(requested_model.clone()));
        assert_eq!(
            start_requested_reasoning_effort,
            Some(requested_reasoning_effort.clone())
        );
        assert_eq!(start_effective_model, None);
        assert_eq!(start_effective_reasoning_effort, None);

        let completed = item_event_to_server_notification(
            EventMsg::ItemCompleted(ItemCompletedEvent {
                thread_id: sender_thread_id,
                turn_id: "turn-phase-compatible".into(),
                completed_at_ms: 2,
                item: CoreTurnItem::CollabAgentToolCall(CoreCollabAgentToolCallItem {
                    id: "spawn-phase-compatible".into(),
                    tool: CoreCollabAgentTool::SpawnAgent,
                    status: CoreCollabAgentToolCallStatus::Completed,
                    sender_thread_id,
                    receiver_thread_ids: vec![receiver_thread_id],
                    receiver_agents: Vec::new(),
                    prompt: Some("inspect".into()),
                    model: Some(effective_model.clone()),
                    reasoning_effort: Some(effective_reasoning_effort.clone()),
                    requested_model: Some(requested_model.clone()),
                    requested_reasoning_effort: Some(requested_reasoning_effort.clone()),
                    agents_states: [(receiver_thread_id, AgentStatus::Completed(None))]
                        .into_iter()
                        .collect(),
                    wake_notifications: None,
                }),
            }),
            "thread-phase-compatible",
            "turn-phase-compatible",
        );
        let Some(ServerNotification::ItemCompleted(ItemCompletedNotification { item, .. })) =
            completed
        else {
            panic!("current spawn terminal must map to item/completed");
        };
        let ThreadItem::CollabAgentToolCall {
            model,
            reasoning_effort,
            requested_model: terminal_requested_model,
            requested_reasoning_effort: terminal_requested_reasoning_effort,
            effective_model: terminal_effective_model,
            effective_reasoning_effort: terminal_effective_reasoning_effort,
            ..
        } = item
        else {
            panic!("current spawn terminal must map to a collab item");
        };
        assert_eq!(model, Some(effective_model.clone()));
        assert_eq!(reasoning_effort, Some(effective_reasoning_effort.clone()));
        assert_eq!(terminal_requested_model, Some(requested_model));
        assert_eq!(
            terminal_requested_reasoning_effort,
            Some(requested_reasoning_effort)
        );
        assert_eq!(terminal_effective_model, Some(effective_model));
        assert_eq!(
            terminal_effective_reasoning_effort,
            Some(effective_reasoning_effort)
        );

        let historic = item_event_to_server_notification(
            EventMsg::CollabAgentSpawnEnd(CollabAgentSpawnEndEvent {
                call_id: "spawn-historic-terminal".into(),
                completed_at_ms: 3,
                sender_thread_id,
                new_thread_id: Some(receiver_thread_id),
                new_agent_nickname: None,
                new_agent_role: None,
                prompt: "inspect".into(),
                model: "gpt-historic-observed".into(),
                reasoning_effort: codex_protocol::openai_models::ReasoningEffort::Low,
                effective_reasoning_effort_present: None,
                status: AgentStatus::Completed(None),
            }),
            "thread-phase-compatible",
            "turn-phase-compatible",
        );
        let Some(ServerNotification::ItemCompleted(ItemCompletedNotification { item, .. })) =
            historic
        else {
            panic!("historic spawn terminal must map to item/completed");
        };
        let ThreadItem::CollabAgentToolCall {
            model,
            reasoning_effort,
            requested_model,
            requested_reasoning_effort,
            effective_model,
            effective_reasoning_effort,
            ..
        } = item
        else {
            panic!("historic spawn terminal must map to a collab item");
        };
        assert_eq!(model.as_deref(), Some("gpt-historic-observed"));
        assert_eq!(
            reasoning_effort,
            Some(codex_protocol::openai_models::ReasoningEffort::Low)
        );
        assert_eq!(requested_model, None);
        assert_eq!(requested_reasoning_effort, None);
        assert_eq!(effective_model.as_deref(), Some("gpt-historic-observed"));
        assert_eq!(
            effective_reasoning_effort,
            Some(codex_protocol::openai_models::ReasoningEffort::Low)
        );

        let unknown_sender_thread_id = ThreadId::new();
        let unknown = item_event_to_server_notification(
            EventMsg::ItemCompleted(ItemCompletedEvent {
                thread_id: unknown_sender_thread_id,
                turn_id: "turn-unknown-terminal".into(),
                completed_at_ms: 4,
                item: CoreTurnItem::CollabAgentToolCall(CoreCollabAgentToolCallItem {
                    id: "spawn-unknown-terminal".into(),
                    tool: CoreCollabAgentTool::SpawnAgent,
                    status: CoreCollabAgentToolCallStatus::Failed,
                    sender_thread_id: unknown_sender_thread_id,
                    receiver_thread_ids: Vec::new(),
                    receiver_agents: Vec::new(),
                    prompt: Some("inspect".into()),
                    model: None,
                    reasoning_effort: None,
                    requested_model: Some("gpt-requested".into()),
                    requested_reasoning_effort: Some(
                        codex_protocol::openai_models::ReasoningEffort::High,
                    ),
                    agents_states: HashMap::new(),
                    wake_notifications: None,
                }),
            }),
            "thread-unknown-terminal",
            "turn-unknown-terminal",
        );
        let Some(ServerNotification::ItemCompleted(ItemCompletedNotification { item, .. })) =
            unknown
        else {
            panic!("unknown-effect spawn terminal must map to item/completed");
        };
        let ThreadItem::CollabAgentToolCall {
            model,
            reasoning_effort,
            requested_model,
            requested_reasoning_effort,
            effective_model,
            effective_reasoning_effort,
            ..
        } = item
        else {
            panic!("unknown-effect spawn terminal must map to a collab item");
        };
        assert_eq!(model, None);
        assert_eq!(reasoning_effort, None);
        assert_eq!(requested_model.as_deref(), Some("gpt-requested"));
        assert_eq!(
            requested_reasoning_effort,
            Some(codex_protocol::openai_models::ReasoningEffort::High)
        );
        assert_eq!(effective_model, None);
        assert_eq!(effective_reasoning_effort, None);
    }

    #[test]
    fn collab_resume_end_maps_to_item_completed_resume_agent() {
        let event = CollabResumeEndEvent {
            call_id: "call-2".to_string(),
            completed_at_ms: 456,
            sender_thread_id: ThreadId::new(),
            receiver_thread_id: ThreadId::new(),
            receiver_agent_nickname: None,
            receiver_agent_role: None,
            status: codex_protocol::protocol::AgentStatus::NotFound,
        };

        let receiver_id = event.receiver_thread_id.to_string();
        let notification = item_event_to_server_notification(
            EventMsg::CollabResumeEnd(event.clone()),
            "thread-2",
            "turn-2",
        );
        assert_item_completed_server_notification(
            notification,
            ItemCompletedNotification {
                thread_id: "thread-2".to_string(),
                turn_id: "turn-2".to_string(),
                completed_at_ms: event.completed_at_ms,
                item: ThreadItem::CollabAgentToolCall {
                    id: event.call_id,
                    tool: CollabAgentTool::ResumeAgent,
                    status: CollabAgentToolCallStatus::Failed,
                    sender_thread_id: event.sender_thread_id.to_string(),
                    receiver_thread_ids: vec![receiver_id.clone()],
                    prompt: None,
                    model: None,
                    reasoning_effort: None,
                    requested_model: None,
                    requested_reasoning_effort: None,
                    effective_model: None,
                    effective_reasoning_effort: None,
                    agents_states: [(
                        receiver_id,
                        CollabAgentState::from(codex_protocol::protocol::AgentStatus::NotFound),
                    )]
                    .into_iter()
                    .collect(),
                    wake_notifications: None,
                },
            },
        );
    }

    #[test]
    fn current_unknown_collab_spawn_end_round_trips_through_legacy_json_as_unknown_identity() {
        let sender_thread_id = ThreadId::new();
        let receiver_thread_id = ThreadId::new();
        let current_terminal = ItemCompletedEvent {
            thread_id: sender_thread_id,
            turn_id: "turn-unknown".into(),
            completed_at_ms: 456,
            item: CoreTurnItem::CollabAgentToolCall(CoreCollabAgentToolCallItem {
                id: "spawn-unknown".into(),
                tool: CoreCollabAgentTool::SpawnAgent,
                status: CoreCollabAgentToolCallStatus::Completed,
                sender_thread_id,
                receiver_thread_ids: vec![receiver_thread_id],
                receiver_agents: Vec::new(),
                prompt: Some("inspect".into()),
                model: None,
                reasoning_effort: None,
                requested_model: Some("gpt-requested".into()),
                requested_reasoning_effort: Some(
                    codex_protocol::openai_models::ReasoningEffort::High,
                ),
                agents_states: [(receiver_thread_id, AgentStatus::Completed(None))]
                    .into_iter()
                    .collect(),
                wake_notifications: None,
            }),
        };

        let legacy_event = current_terminal
            .as_legacy_events(/*show_raw_agent_reasoning*/ false)
            .into_iter()
            .next()
            .expect("completed spawn emits one legacy terminal event");
        assert!(matches!(
            &legacy_event,
            EventMsg::CollabAgentSpawnEnd(event)
                if event.model.is_empty()
                    && event.reasoning_effort
                        == codex_protocol::openai_models::ReasoningEffort::Medium
                    && event.effective_reasoning_effort_present == Some(false)
        ));
        let serialized = serde_json::to_value(&legacy_event).expect("serialize legacy event");
        assert_eq!(
            serialized["effective_reasoning_effort_present"],
            serde_json::Value::Bool(false)
        );
        let legacy_event: EventMsg =
            serde_json::from_value(serialized).expect("deserialize legacy event");

        assert_item_completed_server_notification(
            item_event_to_server_notification(legacy_event, "thread-unknown", "turn-unknown"),
            ItemCompletedNotification {
                thread_id: "thread-unknown".into(),
                turn_id: "turn-unknown".into(),
                completed_at_ms: 456,
                item: ThreadItem::CollabAgentToolCall {
                    id: "spawn-unknown".into(),
                    tool: CollabAgentTool::SpawnAgent,
                    status: CollabAgentToolCallStatus::Completed,
                    sender_thread_id: sender_thread_id.to_string(),
                    receiver_thread_ids: vec![receiver_thread_id.to_string()],
                    prompt: Some("inspect".into()),
                    model: None,
                    reasoning_effort: None,
                    requested_model: None,
                    requested_reasoning_effort: None,
                    effective_model: None,
                    effective_reasoning_effort: None,
                    agents_states: [(
                        receiver_thread_id.to_string(),
                        CollabAgentState::from(AgentStatus::Completed(None)),
                    )]
                    .into_iter()
                    .collect(),
                    wake_notifications: None,
                },
            },
        );
    }

    #[test]
    fn current_model_only_collab_spawn_end_round_trips_through_legacy_json_without_effort() {
        let sender_thread_id = ThreadId::new();
        let receiver_thread_id = ThreadId::new();
        let current_terminal = ItemCompletedEvent {
            thread_id: sender_thread_id,
            turn_id: "turn-model-only".into(),
            completed_at_ms: 456,
            item: CoreTurnItem::CollabAgentToolCall(CoreCollabAgentToolCallItem {
                id: "spawn-model-only".into(),
                tool: CoreCollabAgentTool::SpawnAgent,
                status: CoreCollabAgentToolCallStatus::Failed,
                sender_thread_id,
                receiver_thread_ids: vec![receiver_thread_id],
                receiver_agents: Vec::new(),
                prompt: Some("inspect".into()),
                model: Some("gpt-effective".into()),
                reasoning_effort: None,
                requested_model: Some("gpt-requested".into()),
                requested_reasoning_effort: Some(
                    codex_protocol::openai_models::ReasoningEffort::High,
                ),
                agents_states: [(
                    receiver_thread_id,
                    AgentStatus::Errored("spawn failed".into()),
                )]
                .into_iter()
                .collect(),
                wake_notifications: None,
            }),
        };

        let legacy_event = current_terminal
            .as_legacy_events(/*show_raw_agent_reasoning*/ false)
            .into_iter()
            .next()
            .expect("failed spawn emits one legacy terminal event");
        assert!(matches!(
            &legacy_event,
            EventMsg::CollabAgentSpawnEnd(event)
                if event.model == "gpt-effective"
                    && event.reasoning_effort
                        == codex_protocol::openai_models::ReasoningEffort::Medium
                    && event.effective_reasoning_effort_present == Some(false)
        ));
        let serialized = serde_json::to_value(&legacy_event).expect("serialize legacy event");
        assert_eq!(
            serialized["effective_reasoning_effort_present"],
            serde_json::Value::Bool(false)
        );
        let legacy_event: EventMsg =
            serde_json::from_value(serialized).expect("deserialize legacy event");

        assert_item_completed_server_notification(
            item_event_to_server_notification(legacy_event, "thread-model-only", "turn-model-only"),
            ItemCompletedNotification {
                thread_id: "thread-model-only".into(),
                turn_id: "turn-model-only".into(),
                completed_at_ms: 456,
                item: ThreadItem::CollabAgentToolCall {
                    id: "spawn-model-only".into(),
                    tool: CollabAgentTool::SpawnAgent,
                    status: CollabAgentToolCallStatus::Failed,
                    sender_thread_id: sender_thread_id.to_string(),
                    receiver_thread_ids: vec![receiver_thread_id.to_string()],
                    prompt: Some("inspect".into()),
                    model: Some("gpt-effective".into()),
                    reasoning_effort: None,
                    requested_model: None,
                    requested_reasoning_effort: None,
                    effective_model: Some("gpt-effective".into()),
                    effective_reasoning_effort: None,
                    agents_states: [(
                        receiver_thread_id.to_string(),
                        CollabAgentState::from(AgentStatus::Errored("spawn failed".into())),
                    )]
                    .into_iter()
                    .collect(),
                    wake_notifications: None,
                },
            },
        );
    }

    #[test]
    fn exec_command_output_delta_maps_to_command_execution_output_delta() {
        let notification = item_event_to_server_notification(
            EventMsg::ExecCommandOutputDelta(ExecCommandOutputDeltaEvent {
                call_id: "call-1".to_string(),
                stream: ExecOutputStream::Stdout,
                chunk: b"hello".to_vec(),
            }),
            "thread-1",
            "turn-1",
        );

        assert_command_execution_output_delta_server_notification(
            notification,
            CommandExecutionOutputDeltaNotification {
                thread_id: "thread-1".to_string(),
                turn_id: "turn-1".to_string(),
                item_id: "call-1".to_string(),
                delta: "hello".to_string(),
            },
        );
    }

    #[test]
    fn terminal_interaction_maps_terminal_wait_metadata() {
        let notification = item_event_to_server_notification(
            EventMsg::TerminalInteraction(TerminalInteractionEvent {
                call_id: "call-wait".to_string(),
                process_id: "123".to_string(),
                stdin: String::new(),
                terminal_wait: Some(CoreTerminalWaitInfo {
                    primitive: CoreTerminalWaitPrimitive::WriteStdinWaitUntilTerminal,
                    max_wait_ms: Some(10_000),
                    heartbeat_interval_ms: Some(1_000),
                }),
            }),
            "thread-1",
            "turn-1",
        );

        assert_terminal_interaction_server_notification(
            notification,
            TerminalInteractionNotification {
                thread_id: "thread-1".to_string(),
                turn_id: "turn-1".to_string(),
                item_id: "call-wait".to_string(),
                process_id: "123".to_string(),
                stdin: String::new(),
                terminal_wait: Some(TerminalWaitInfo {
                    primitive: TerminalWaitPrimitive::WriteStdinWaitUntilTerminal,
                    max_wait_ms: Some(10_000),
                    heartbeat_interval_ms: Some(1_000),
                }),
            },
        );
    }
}
