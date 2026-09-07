use super::*;
use crate::protocol::v2::CollabAgentState;
use crate::protocol::v2::CollabAgentStatus;
use std::collections::HashMap;

#[test]
fn keeps_requested_and_effective_spawn_identities_separate_before_materializing_terminal_state() {
    let started = spawn_item(
        "spawn-1",
        CollabAgentToolCallStatus::InProgress,
        Vec::new(),
        /*model*/ None,
        /*reasoning_effort*/ None,
        Some("gpt-requested"),
        Some(codex_protocol::openai_models::ReasoningEffort::High),
        HashMap::new(),
    );
    let terminal = spawn_item(
        "spawn-1",
        CollabAgentToolCallStatus::Completed,
        vec!["child-1".to_string()],
        Some("gpt-effective"),
        Some(codex_protocol::openai_models::ReasoningEffort::Medium),
        /*requested_model*/ None,
        /*requested_reasoning_effort*/ None,
        [(
            "child-1".to_string(),
            CollabAgentState {
                status: CollabAgentStatus::Running,
                message: None,
            },
        )]
        .into_iter()
        .collect(),
    );

    assert_eq!(
        spawn_lifecycle_identity(&started, &terminal),
        Some(SpawnLifecycleIdentity {
            requested_model: Some("gpt-requested".to_string()),
            requested_reasoning_effort: Some(codex_protocol::openai_models::ReasoningEffort::High),
            effective_model: Some("gpt-effective".to_string()),
            effective_reasoning_effort: Some(
                codex_protocol::openai_models::ReasoningEffort::Medium,
            ),
        })
    );
    assert_eq!(
        merge_collab_agent_lifecycle(&started, terminal),
        spawn_item(
            "spawn-1",
            CollabAgentToolCallStatus::Completed,
            vec!["child-1".to_string()],
            Some("gpt-effective"),
            Some(codex_protocol::openai_models::ReasoningEffort::Medium),
            Some("gpt-requested"),
            Some(codex_protocol::openai_models::ReasoningEffort::High),
            [(
                "child-1".to_string(),
                CollabAgentState {
                    status: CollabAgentStatus::Running,
                    message: None,
                },
            )]
            .into_iter()
            .collect(),
        )
    );
}

#[test]
fn preserves_legacy_spawn_request_provenance_without_inventing_an_effective_identity() {
    let legacy_started = collab_item(
        "spawn-legacy",
        CollabAgentTool::SpawnAgent,
        CollabAgentToolCallStatus::InProgress,
        Vec::new(),
        Some("gpt-requested"),
        Some(codex_protocol::openai_models::ReasoningEffort::High),
        HashMap::new(),
    );
    let terminal_without_effective_identity = collab_item(
        "spawn-legacy",
        CollabAgentTool::SpawnAgent,
        CollabAgentToolCallStatus::Failed,
        Vec::new(),
        /*model*/ None,
        /*reasoning_effort*/ None,
        HashMap::new(),
    );

    let ThreadItem::CollabAgentToolCall {
        model,
        reasoning_effort,
        requested_model,
        requested_reasoning_effort,
        effective_model,
        effective_reasoning_effort,
        ..
    } = merge_collab_agent_lifecycle(&legacy_started, terminal_without_effective_identity)
    else {
        unreachable!("collab test helper must create a collab item");
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
fn terminal_spawn_compatibility_records_preserve_only_missing_requested_provenance() {
    let canonical_terminal = spawn_item(
        "spawn-compatibility",
        CollabAgentToolCallStatus::Completed,
        vec!["child-canonical".to_string()],
        Some("gpt-canonical-effective"),
        Some(codex_protocol::openai_models::ReasoningEffort::Medium),
        Some("gpt-canonical-requested"),
        Some(codex_protocol::openai_models::ReasoningEffort::High),
        HashMap::new(),
    );
    let legacy_terminal = spawn_item(
        "spawn-compatibility",
        CollabAgentToolCallStatus::Failed,
        Vec::new(),
        Some("gpt-legacy-effective"),
        Some(codex_protocol::openai_models::ReasoningEffort::Low),
        /*requested_model*/ None,
        /*requested_reasoning_effort*/ None,
        HashMap::new(),
    );

    assert_eq!(
        merge_collab_agent_lifecycle(&canonical_terminal, legacy_terminal),
        spawn_item(
            "spawn-compatibility",
            CollabAgentToolCallStatus::Failed,
            Vec::new(),
            Some("gpt-legacy-effective"),
            Some(codex_protocol::openai_models::ReasoningEffort::Low),
            Some("gpt-canonical-requested"),
            Some(codex_protocol::openai_models::ReasoningEffort::High),
            HashMap::new(),
        )
    );

    let incoming_requested_terminal = spawn_item(
        "spawn-compatibility",
        CollabAgentToolCallStatus::Completed,
        Vec::new(),
        Some("gpt-incoming-effective"),
        Some(codex_protocol::openai_models::ReasoningEffort::Low),
        Some("gpt-incoming-requested"),
        Some(codex_protocol::openai_models::ReasoningEffort::Medium),
        HashMap::new(),
    );

    assert_eq!(
        merge_collab_agent_lifecycle(&canonical_terminal, incoming_requested_terminal.clone(),),
        incoming_requested_terminal
    );
}

#[test]
fn terminal_spawn_compatibility_replay_does_not_erase_known_effective_identity() {
    let canonical_terminal = spawn_item(
        "spawn-stale-compatibility",
        CollabAgentToolCallStatus::Completed,
        vec!["child-canonical".to_string()],
        Some("gpt-known-effective"),
        Some(codex_protocol::openai_models::ReasoningEffort::Medium),
        Some("gpt-requested"),
        Some(codex_protocol::openai_models::ReasoningEffort::High),
        HashMap::new(),
    );
    let legacy_terminal_without_identity = spawn_item(
        "spawn-stale-compatibility",
        CollabAgentToolCallStatus::Failed,
        Vec::new(),
        /*model*/ None,
        /*reasoning_effort*/ None,
        /*requested_model*/ None,
        /*requested_reasoning_effort*/ None,
        HashMap::new(),
    );

    assert_eq!(
        merge_collab_agent_lifecycle(&canonical_terminal, legacy_terminal_without_identity),
        spawn_item(
            "spawn-stale-compatibility",
            CollabAgentToolCallStatus::Failed,
            Vec::new(),
            Some("gpt-known-effective"),
            Some(codex_protocol::openai_models::ReasoningEffort::Medium),
            Some("gpt-requested"),
            Some(codex_protocol::openai_models::ReasoningEffort::High),
            HashMap::new(),
        )
    );
}

#[test]
fn collab_spawn_identity_is_phase_compatible_camel_case_and_old_payloads_parse() {
    let item = spawn_item(
        "spawn-1",
        CollabAgentToolCallStatus::Completed,
        vec!["child-1".to_string()],
        Some("gpt-effective"),
        Some(codex_protocol::openai_models::ReasoningEffort::Medium),
        Some("gpt-requested"),
        Some(codex_protocol::openai_models::ReasoningEffort::High),
        HashMap::new(),
    );

    let serialized = serde_json::to_value(item).expect("serialize collab item");
    assert_eq!(serialized["model"], "gpt-effective");
    assert_eq!(serialized["reasoningEffort"], "medium");
    assert_eq!(serialized["requestedModel"], "gpt-requested");
    assert_eq!(serialized["requestedReasoningEffort"], "high");
    assert_eq!(serialized["effectiveModel"], "gpt-effective");
    assert_eq!(serialized["effectiveReasoningEffort"], "medium");
    assert!(serialized.get("requested_model").is_none());
    assert!(serialized.get("requested_reasoning_effort").is_none());

    let legacy: ThreadItem = serde_json::from_value(serde_json::json!({
        "type": "collabAgentToolCall",
        "id": "spawn-legacy",
        "tool": "spawnAgent",
        "status": "inProgress",
        "senderThreadId": "parent",
        "receiverThreadIds": [],
        "prompt": "inspect",
        "model": "gpt-requested",
        "reasoningEffort": "high",
        "agentsStates": {},
    }))
    .expect("pre-additive payload remains valid");
    let ThreadItem::CollabAgentToolCall {
        model,
        requested_model,
        requested_reasoning_effort,
        effective_model,
        effective_reasoning_effort,
        ..
    } = legacy
    else {
        unreachable!("legacy payload must decode as a collab item");
    };
    assert_eq!(model.as_deref(), Some("gpt-requested"));
    assert_eq!(requested_model, None);
    assert_eq!(requested_reasoning_effort, None);
    assert_eq!(effective_model, None);
    assert_eq!(effective_reasoning_effort, None);
}

#[test]
fn unknown_terminal_collab_spawn_serializes_all_identity_fields_as_null() {
    let unknown_terminal = spawn_item(
        "spawn-unknown-terminal",
        CollabAgentToolCallStatus::Failed,
        Vec::new(),
        /*model*/ None,
        /*reasoning_effort*/ None,
        /*requested_model*/ None,
        /*requested_reasoning_effort*/ None,
        HashMap::new(),
    );

    let serialized = serde_json::to_value(unknown_terminal).expect("serialize collab item");
    for field in [
        "requestedModel",
        "requestedReasoningEffort",
        "effectiveModel",
        "effectiveReasoningEffort",
    ] {
        assert_eq!(
            serialized.get(field),
            Some(&serde_json::Value::Null),
            "unknown terminal identity must serialize {field} as null"
        );
    }
}

#[test]
fn preserves_wait_and_resume_receivers_only_when_terminal_snapshot_omits_them() {
    for tool in [CollabAgentTool::Wait, CollabAgentTool::ResumeAgent] {
        let started = collab_item(
            "call-1",
            tool.clone(),
            CollabAgentToolCallStatus::InProgress,
            vec!["child-1".to_string(), "child-2".to_string()],
            /*model*/ None,
            /*reasoning_effort*/ None,
            HashMap::new(),
        );
        let terminal = collab_item(
            "call-1",
            tool.clone(),
            CollabAgentToolCallStatus::Completed,
            Vec::new(),
            /*model*/ None,
            /*reasoning_effort*/ None,
            [(
                "child-1".to_string(),
                CollabAgentState {
                    status: CollabAgentStatus::Completed,
                    message: Some("finished".to_string()),
                },
            )]
            .into_iter()
            .collect(),
        );

        for merged in [
            merge_collab_agent_lifecycle(&started, terminal.clone()),
            merge_collab_agent_lifecycle(&terminal, started.clone()),
        ] {
            let ThreadItem::CollabAgentToolCall {
                receiver_thread_ids,
                agents_states,
                ..
            } = merged
            else {
                unreachable!("collab test helper must create a collab item");
            };
            assert_eq!(
                receiver_thread_ids,
                vec!["child-1".to_string(), "child-2".to_string()]
            );
            assert_eq!(
                agents_states,
                [(
                    "child-1".to_string(),
                    CollabAgentState {
                        status: CollabAgentStatus::Completed,
                        message: Some("finished".to_string()),
                    },
                )]
                .into_iter()
                .collect()
            );
        }
    }
}

#[test]
fn late_starts_fill_missing_terminal_context_without_overwriting_terminal_state() {
    for tool in [
        CollabAgentTool::SpawnAgent,
        CollabAgentTool::Wait,
        CollabAgentTool::ResumeAgent,
        CollabAgentTool::SendInput,
        CollabAgentTool::CloseAgent,
    ] {
        let started = collab_item(
            "call-1",
            tool.clone(),
            CollabAgentToolCallStatus::InProgress,
            Vec::new(),
            /*model*/ None,
            /*reasoning_effort*/ None,
            [
                (
                    "child-1".to_string(),
                    CollabAgentState {
                        status: CollabAgentStatus::Running,
                        message: None,
                    },
                ),
                (
                    "child-2".to_string(),
                    CollabAgentState {
                        status: CollabAgentStatus::Running,
                        message: None,
                    },
                ),
            ]
            .into_iter()
            .collect(),
        );
        let mut terminal = collab_item(
            "call-1",
            tool,
            CollabAgentToolCallStatus::Completed,
            Vec::new(),
            /*model*/ None,
            /*reasoning_effort*/ None,
            [(
                "child-1".to_string(),
                CollabAgentState {
                    status: CollabAgentStatus::Completed,
                    message: Some("finished".to_string()),
                },
            )]
            .into_iter()
            .collect(),
        );
        let ThreadItem::CollabAgentToolCall { prompt, .. } = &mut terminal else {
            unreachable!("collab test helper must create a collab item");
        };
        *prompt = None;

        let ThreadItem::CollabAgentToolCall {
            prompt,
            agents_states,
            ..
        } = merge_collab_agent_lifecycle(&terminal, started)
        else {
            unreachable!("collab test helper must create a collab item");
        };
        assert_eq!(prompt.as_deref(), Some("inspect"));
        assert_eq!(
            agents_states,
            [
                (
                    "child-1".to_string(),
                    CollabAgentState {
                        status: CollabAgentStatus::Completed,
                        message: Some("finished".to_string()),
                    },
                ),
                (
                    "child-2".to_string(),
                    CollabAgentState {
                        status: CollabAgentStatus::Running,
                        message: None,
                    },
                ),
            ]
            .into_iter()
            .collect()
        );
    }
}

#[test]
fn never_copies_a_prior_receiver_into_a_terminal_spawn() {
    let started = collab_item(
        "spawn-1",
        CollabAgentTool::SpawnAgent,
        CollabAgentToolCallStatus::InProgress,
        vec!["stale-child".to_string()],
        Some("gpt-requested"),
        Some(codex_protocol::openai_models::ReasoningEffort::High),
        HashMap::new(),
    );
    let terminal = collab_item(
        "spawn-1",
        CollabAgentTool::SpawnAgent,
        CollabAgentToolCallStatus::Failed,
        Vec::new(),
        /*model*/ None,
        /*reasoning_effort*/ None,
        HashMap::new(),
    );

    assert_eq!(
        merge_collab_agent_lifecycle(&started, terminal),
        spawn_item(
            "spawn-1",
            CollabAgentToolCallStatus::Failed,
            Vec::new(),
            /*model*/ None,
            /*reasoning_effort*/ None,
            Some("gpt-requested"),
            Some(codex_protocol::openai_models::ReasoningEffort::High),
            HashMap::new(),
        )
    );
}

fn collab_item(
    id: &str,
    tool: CollabAgentTool,
    status: CollabAgentToolCallStatus,
    receiver_thread_ids: Vec<String>,
    model: Option<&str>,
    reasoning_effort: Option<codex_protocol::openai_models::ReasoningEffort>,
    agents_states: HashMap<String, CollabAgentState>,
) -> ThreadItem {
    ThreadItem::CollabAgentToolCall {
        id: id.to_string(),
        tool,
        status,
        sender_thread_id: "parent".to_string(),
        receiver_thread_ids,
        prompt: Some("inspect".to_string()),
        model: model.map(str::to_string),
        reasoning_effort,
        requested_model: None,
        requested_reasoning_effort: None,
        effective_model: None,
        effective_reasoning_effort: None,
        agents_states,

        wake_notifications: None,
        completion_reason: None,
    }
}

#[allow(clippy::too_many_arguments)]
fn spawn_item(
    id: &str,
    status: CollabAgentToolCallStatus,
    receiver_thread_ids: Vec<String>,
    model: Option<&str>,
    reasoning_effort: Option<codex_protocol::openai_models::ReasoningEffort>,
    requested_model: Option<&str>,
    requested_reasoning_effort: Option<codex_protocol::openai_models::ReasoningEffort>,
    agents_states: HashMap<String, CollabAgentState>,
) -> ThreadItem {
    let (legacy_model, legacy_reasoning_effort) =
        if matches!(&status, CollabAgentToolCallStatus::InProgress) {
            (requested_model, requested_reasoning_effort.clone())
        } else {
            (model, reasoning_effort.clone())
        };
    let mut item = collab_item(
        id,
        CollabAgentTool::SpawnAgent,
        status,
        receiver_thread_ids,
        legacy_model,
        legacy_reasoning_effort,
        agents_states,
    );
    let ThreadItem::CollabAgentToolCall {
        requested_model: item_requested_model,
        requested_reasoning_effort: item_requested_reasoning_effort,
        effective_model: item_effective_model,
        effective_reasoning_effort: item_effective_reasoning_effort,
        ..
    } = &mut item
    else {
        unreachable!("collab test helper must create a collab item");
    };
    *item_requested_model = requested_model.map(str::to_string);
    *item_requested_reasoning_effort = requested_reasoning_effort;
    *item_effective_model = model.map(str::to_string);
    *item_effective_reasoning_effort = reasoning_effort;
    item
}
