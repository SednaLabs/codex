//! Shared materialization policy for collaboration-tool lifecycle snapshots.
//!
//! Legacy rollout events and canonical item lifecycle records both describe one
//! collaboration call with a start snapshot followed by a terminal snapshot.
//! The public `ThreadItem` contract uses its original model/effort pair as
//! requested-identity aliases at spawn start and observed-effective aliases at
//! terminal spawn. It also retains explicit requested provenance and observed
//! terminal snapshots through additive fields. This module keeps those spawn
//! identities distinct while merging without turning a request or later thread
//! metadata into an observed terminal selection.

use crate::protocol::v2::CollabAgentTool;
use crate::protocol::v2::CollabAgentToolCallStatus;
use crate::protocol::v2::ThreadItem;

/// Merges a terminal collaboration-tool snapshot with its matching prior start
/// snapshot.
///
/// An explicit terminal value always wins. Wait and Resume terminal snapshots
/// retain receiver IDs already recorded by their start when the terminal omits
/// them; Spawn never inherits receivers because a missing terminal child is
/// meaningful.
pub(crate) fn merge_collab_agent_lifecycle(
    previous: &ThreadItem,
    incoming: ThreadItem,
) -> ThreadItem {
    let ThreadItem::CollabAgentToolCall {
        id: previous_id,
        tool: previous_tool,
        status: previous_status,
        receiver_thread_ids: previous_receiver_thread_ids,
        prompt: previous_prompt,
        model: previous_model,
        reasoning_effort: previous_reasoning_effort,
        requested_model: previous_requested_model,
        requested_reasoning_effort: previous_requested_reasoning_effort,
        effective_model: previous_effective_model,
        effective_reasoning_effort: previous_effective_reasoning_effort,
        agents_states: previous_agents_states,
        ..
    } = previous
    else {
        return incoming;
    };
    let ThreadItem::CollabAgentToolCall {
        id: incoming_id,
        tool: incoming_tool,
        status: incoming_status,
        ..
    } = &incoming
    else {
        return incoming;
    };

    if previous_id != incoming_id || previous_tool != incoming_tool {
        return incoming;
    }

    if incoming_status == &CollabAgentToolCallStatus::InProgress {
        if previous_status != &CollabAgentToolCallStatus::InProgress {
            // Replays can deliver duplicate lifecycle records out of order. Keep
            // terminal snapshots authoritative, but recover requested spawn
            // provenance from the late start when the terminal did not retain it.
            let mut terminal = previous.clone();
            if let (
                ThreadItem::CollabAgentToolCall {
                    prompt: terminal_prompt,
                    agents_states: terminal_agents_states,
                    wake_notifications: None,
                    ..
                },
                ThreadItem::CollabAgentToolCall {
                    prompt: started_prompt,
                    agents_states: started_agents_states,
                    wake_notifications: None,
                    ..
                },
            ) = (&mut terminal, &incoming)
            {
                if terminal_prompt.is_none() {
                    terminal_prompt.clone_from(started_prompt);
                }
                for (thread_id, state) in started_agents_states {
                    terminal_agents_states
                        .entry(thread_id.clone())
                        .or_insert_with(|| state.clone());
                }
            }
            if let (
                ThreadItem::CollabAgentToolCall {
                    tool: CollabAgentTool::SpawnAgent,
                    requested_model: terminal_requested_model,
                    requested_reasoning_effort: terminal_requested_reasoning_effort,
                    model: _,
                    reasoning_effort: _,
                    ..
                },
                ThreadItem::CollabAgentToolCall {
                    requested_model: started_requested_model,
                    requested_reasoning_effort: started_requested_reasoning_effort,
                    model: started_model,
                    reasoning_effort: started_reasoning_effort,
                    ..
                },
            ) = (&mut terminal, &incoming)
            {
                if terminal_requested_model.is_none() {
                    *terminal_requested_model = started_requested_model
                        .clone()
                        .or_else(|| started_model.clone());
                }
                if terminal_requested_reasoning_effort.is_none() {
                    *terminal_requested_reasoning_effort = started_requested_reasoning_effort
                        .clone()
                        .or_else(|| started_reasoning_effort.clone());
                }
                // A late start can recover only the terminal request provenance.
                // Its legacy aliases remain the terminal's observed effective
                // snapshot, including an explicit absence when that effect is
                // unknown.
            }
            if let (
                ThreadItem::CollabAgentToolCall {
                    tool: CollabAgentTool::Wait | CollabAgentTool::ResumeAgent,
                    receiver_thread_ids: terminal_receiver_thread_ids,
                    ..
                },
                ThreadItem::CollabAgentToolCall {
                    receiver_thread_ids: started_receiver_thread_ids,
                    ..
                },
            ) = (&mut terminal, &incoming)
            {
                *terminal_receiver_thread_ids = merge_receiver_thread_ids(
                    started_receiver_thread_ids,
                    terminal_receiver_thread_ids,
                );
            }
            return terminal;
        }
        return incoming;
    }

    let spawn_identity = spawn_lifecycle_identity(previous, &incoming);
    let mut incoming = incoming;
    let ThreadItem::CollabAgentToolCall {
        tool,
        receiver_thread_ids,
        prompt,
        model,
        reasoning_effort,
        requested_model,
        requested_reasoning_effort,
        effective_model: item_effective_model,
        effective_reasoning_effort: item_effective_reasoning_effort,
        agents_states,
        ..
    } = &mut incoming
    else {
        unreachable!("matching collab lifecycle item must remain a collab call");
    };

    if prompt.is_none() {
        prompt.clone_from(previous_prompt);
    }

    for (thread_id, state) in previous_agents_states {
        agents_states
            .entry(thread_id.clone())
            .or_insert_with(|| state.clone());
    }

    match tool {
        CollabAgentTool::SpawnAgent => {
            if let Some(SpawnLifecycleIdentity {
                requested_model: lifecycle_requested_model,
                requested_reasoning_effort: lifecycle_requested_reasoning_effort,
                effective_model: lifecycle_effective_model,
                effective_reasoning_effort: lifecycle_effective_reasoning_effort,
            }) = spawn_identity
            {
                // Do not promote a requested start identity into a missing terminal
                // effective identity. Terminal aliases and additive effective fields
                // both contain only the observed terminal snapshot; requested*
                // retains the independent request provenance.
                *model = lifecycle_effective_model.clone();
                *reasoning_effort = lifecycle_effective_reasoning_effort.clone();
                *requested_model = lifecycle_requested_model;
                *requested_reasoning_effort = lifecycle_requested_reasoning_effort;
                *item_effective_model = lifecycle_effective_model;
                *item_effective_reasoning_effort = lifecycle_effective_reasoning_effort;
            } else if previous_status != &CollabAgentToolCallStatus::InProgress {
                // Legacy SpawnEnd compatibility records can omit both additive identity
                // pairs. An explicit incoming value wins, while an omitted value must not
                // erase identity already observed in the matching terminal snapshot. This
                // makes stale terminal compatibility replays idempotent without promoting
                // requested identity into effective identity.
                if model.is_none() {
                    model.clone_from(previous_model);
                }
                if reasoning_effort.is_none() {
                    reasoning_effort.clone_from(previous_reasoning_effort);
                }
                if requested_model.is_none() {
                    requested_model.clone_from(previous_requested_model);
                }
                if requested_reasoning_effort.is_none() {
                    requested_reasoning_effort.clone_from(previous_requested_reasoning_effort);
                }
                if item_effective_model.is_none() {
                    item_effective_model.clone_from(previous_effective_model);
                }
                if item_effective_reasoning_effort.is_none() {
                    item_effective_reasoning_effort.clone_from(previous_effective_reasoning_effort);
                }
            }
        }
        CollabAgentTool::Wait | CollabAgentTool::ResumeAgent => {
            *receiver_thread_ids =
                merge_receiver_thread_ids(previous_receiver_thread_ids, receiver_thread_ids);
        }
        CollabAgentTool::SendInput | CollabAgentTool::CloseAgent => {}
    }

    incoming
}

fn merge_receiver_thread_ids(started: &[String], terminal: &[String]) -> Vec<String> {
    let mut merged = started.to_vec();
    for receiver_thread_id in terminal {
        if !merged.contains(receiver_thread_id) {
            merged.push(receiver_thread_id.clone());
        }
    }
    merged
}

/// The identities observed across a spawn lifecycle boundary.
///
/// Start records capture what the caller asked for. A terminal record carries
/// an observed effective selection through both the established terminal aliases
/// and the explicit effective fields. Keeping the pair separate prevents a
/// terminal effective value from being reclassified as a requested override
/// during replay.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct SpawnLifecycleIdentity {
    pub(crate) requested_model: Option<String>,
    pub(crate) requested_reasoning_effort: Option<codex_protocol::openai_models::ReasoningEffort>,
    pub(crate) effective_model: Option<String>,
    pub(crate) effective_reasoning_effort: Option<codex_protocol::openai_models::ReasoningEffort>,
}

pub(crate) fn spawn_lifecycle_identity(
    previous: &ThreadItem,
    terminal: &ThreadItem,
) -> Option<SpawnLifecycleIdentity> {
    let ThreadItem::CollabAgentToolCall {
        id: previous_id,
        tool: CollabAgentTool::SpawnAgent,
        status: previous_status,
        model: previous_model,
        reasoning_effort: previous_reasoning_effort,
        requested_model: previous_requested_model,
        requested_reasoning_effort: previous_requested_reasoning_effort,
        ..
    } = previous
    else {
        return None;
    };
    let ThreadItem::CollabAgentToolCall {
        id: terminal_id,
        tool: CollabAgentTool::SpawnAgent,
        status: terminal_status,
        requested_model: terminal_requested_model,
        requested_reasoning_effort: terminal_requested_reasoning_effort,
        effective_model: terminal_effective_model,
        effective_reasoning_effort: terminal_effective_reasoning_effort,
        ..
    } = terminal
    else {
        return None;
    };
    if previous_id != terminal_id
        || previous_status != &CollabAgentToolCallStatus::InProgress
        || terminal_status == &CollabAgentToolCallStatus::InProgress
    {
        return None;
    }

    Some(SpawnLifecycleIdentity {
        // Canonical records written before requested* existed stored the start
        // identity in model/reasoning_effort. That narrow start-only fallback
        // preserves replay compatibility without ever inventing an effective
        // terminal identity from a request.
        requested_model: terminal_requested_model
            .clone()
            .or_else(|| previous_requested_model.clone())
            .or_else(|| previous_model.clone()),
        requested_reasoning_effort: terminal_requested_reasoning_effort
            .clone()
            .or_else(|| previous_requested_reasoning_effort.clone())
            .or_else(|| previous_reasoning_effort.clone()),
        effective_model: terminal_effective_model.clone(),
        effective_reasoning_effort: terminal_effective_reasoning_effort.clone(),
    })
}

#[cfg(test)]
#[path = "collab_agent_lifecycle_tests.rs"]
mod tests;
