//! Shared argument parsing and dispatch for the v2 agent messaging tools.
//!
//! `send_message` accepts text items plus optional interruption, while `followup_task`
//! keeps the plain-text message path. Both share the same submission plumbing once the prompt is
//! assembled.

use super::*;
use crate::agent_communication::AgentCommunicationContext;
use crate::agent_communication::AgentCommunicationKind;
use crate::tools::context::FunctionToolOutput;
use codex_protocol::openai_models::ReasoningEffort;
use codex_protocol::protocol::InterAgentCommunication;
use codex_protocol::user_input::UserInput;
use futures::future::BoxFuture;
use serde::Serialize;
use serde_json::Value;

#[derive(Clone, Copy, PartialEq, Eq)]
pub(crate) enum MessageDeliveryMode {
    QueueOnly,
    TriggerTurn,
}

impl MessageDeliveryMode {
    /// Returns whether the produced communication should start a turn immediately.
    fn apply(self, communication: InterAgentCommunication) -> InterAgentCommunication {
        match self {
            Self::QueueOnly => InterAgentCommunication {
                trigger_turn: false,
                ..communication
            },
            Self::TriggerTurn => InterAgentCommunication {
                trigger_turn: true,
                ..communication
            },
        }
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
/// Input for the MultiAgentV2 `send_message` tool.
pub(crate) struct SendMessageArgs {
    pub(crate) target: String,
    /// The published V2 contract is an encrypted scalar. The wrapper keeps a
    /// raw JSON value so we can distinguish an omitted field from an explicit
    /// null before resolving the target agent.
    #[serde(default)]
    pub(crate) message: SendMessageField,
    /// Compatibility for payloads produced by the pre-fix `items` schema.
    #[serde(default)]
    pub(crate) items: SendMessageField,
    #[serde(default)]
    pub(crate) interrupt: bool,
}

#[derive(Debug, Deserialize)]
#[serde(untagged)]
pub(crate) enum SendMessagePayload {
    /// An opaque Responses encrypted scalar. It is forwarded without parsing.
    Scalar(String),
    /// The pre-fix structured representation, which only supported text items.
    TextItems(Vec<UserInput>),
}

impl SendMessageArgs {
    pub(crate) fn into_parts(self) -> Result<(String, String, bool), FunctionCallError> {
        let Self {
            target,
            message,
            items,
            interrupt,
        } = self;
        let message = match (message.0, items.0) {
            (Some(_), Some(_)) => Err(FunctionCallError::RespondToModel(
                "send_message accepts either message or legacy items, not both".to_string(),
            )),
            (Some(payload), None) => parse_send_message_payload("message", payload),
            (None, Some(payload)) => parse_send_message_payload("items", payload),
            (None, None) => Err(FunctionCallError::RespondToModel(
                "missing field `message`".to_string(),
            )),
        }?;
        Ok((target, message, interrupt))
    }
}

#[derive(Debug, Default)]
pub(crate) struct SendMessageField(Option<Value>);

impl<'de> Deserialize<'de> for SendMessageField {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        Value::deserialize(deserializer).map(|value| Self(Some(value)))
    }
}

fn parse_send_message_payload(
    field_name: &str,
    payload: Value,
) -> Result<String, FunctionCallError> {
    if payload.is_null() {
        return Err(FunctionCallError::RespondToModel(format!(
            "send_message field `{field_name}` can't be null"
        )));
    }
    let payload: SendMessagePayload = serde_json::from_value(payload).map_err(|err| {
        FunctionCallError::RespondToModel(format!(
            "invalid send_message {field_name} payload: {err}"
        ))
    })?;
    payload.into_message()
}

impl SendMessagePayload {
    fn into_message(self) -> Result<String, FunctionCallError> {
        match self {
            Self::Scalar(message) => message_content(message),
            Self::TextItems(items) => message_content_from_items("send_message", items),
        }
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
/// Input for the MultiAgentV2 `assign_task` tool.
pub(crate) struct AssignTaskArgs {
    pub(crate) target: String,
    pub(crate) message: String,
    pub(crate) expected_model: Option<String>,
}

#[derive(Debug, Serialize)]
struct FollowupTaskResult {
    task_name: String,
    recipient_task_name: String,
    effective_identity_scope: &'static str,
    effective_model: String,
    effective_model_provider_id: String,
    effective_reasoning_effort: Option<ReasoningEffort>,
    effective_service_tier: Option<String>,
}

#[derive(Debug, Serialize)]
struct SendMessageReceipt {
    task_name: String,
    recipient_task_name: String,
    effective_identity_scope: &'static str,
    handoff_state: &'static str,
    effective_model: Option<String>,
    effective_model_provider_id: Option<String>,
    effective_reasoning_effort: Option<ReasoningEffort>,
    effective_service_tier: Option<String>,
}

pub(super) fn message_content(message: String) -> Result<String, FunctionCallError> {
    if message.trim().is_empty() {
        return Err(FunctionCallError::RespondToModel(
            "Empty message can't be sent to an agent".to_string(),
        ));
    }
    Ok(message)
}

/// Handles the shared MultiAgentV2 message flow for both `send_message` and `followup_task`.
pub(crate) async fn handle_message_string_tool(
    invocation: ToolInvocation,
    mode: MessageDeliveryMode,
    target: String,
    message: String,
    expected_model: Option<String>,
) -> Result<FunctionToolOutput, FunctionCallError> {
    handle_message_submission(
        invocation,
        mode,
        target,
        message_content(message)?,
        /*interrupt*/ false,
        expected_model,
    )
    .await
}

fn message_content_from_items(
    tool_name: &str,
    items: Vec<UserInput>,
) -> Result<String, FunctionCallError> {
    if items.is_empty() {
        return Err(FunctionCallError::RespondToModel(
            "Items can't be empty".to_string(),
        ));
    }
    let mut text_segments = Vec::new();
    for item in items {
        match item {
            UserInput::Text { text, .. } if !text.trim().is_empty() => text_segments.push(text),
            UserInput::Text { .. } => {}
            UserInput::Image { .. }
            | UserInput::LocalImage { .. }
            | UserInput::Skill { .. }
            | UserInput::Mention { .. }
            | _ => {
                return Err(FunctionCallError::RespondToModel(format!(
                    "{tool_name} only supports text content in MultiAgentV2 for now"
                )));
            }
        }
    }

    message_content(text_segments.join("\n"))
}

pub(crate) fn handle_message_submission(
    invocation: ToolInvocation,
    mode: MessageDeliveryMode,
    target: String,
    message: String,
    interrupt: bool,
    expected_model: Option<String>,
) -> BoxFuture<'static, Result<FunctionToolOutput, FunctionCallError>> {
    Box::pin(handle_message_submission_inner(
        invocation,
        mode,
        target,
        message,
        interrupt,
        expected_model,
    ))
}

async fn handle_message_submission_inner(
    invocation: ToolInvocation,
    mode: MessageDeliveryMode,
    target: String,
    message: String,
    interrupt: bool,
    expected_model: Option<String>,
) -> Result<FunctionToolOutput, FunctionCallError> {
    let ToolInvocation {
        session,
        turn,
        payload,
        call_id,
        ..
    } = invocation;
    let _ = payload;
    let receiver_thread_id = resolve_agent_target(&session, &turn, &target).await?;
    let receiver_agent = session
        .services
        .agent_control
        .ensure_agent_known(receiver_thread_id)
        .map_err(|err| collab_agent_error(receiver_thread_id, err))?;
    if mode == MessageDeliveryMode::TriggerTurn
        && receiver_agent
            .agent_path
            .as_ref()
            .is_some_and(AgentPath::is_root)
    {
        return Err(FunctionCallError::RespondToModel(
            "Follow-up tasks can't target the root agent".to_string(),
        ));
    }
    let receiver_agent_path = receiver_agent.agent_path.clone().ok_or_else(|| {
        FunctionCallError::RespondToModel("target agent is missing an agent_path".to_string())
    })?;
    let delivery = match mode {
        MessageDeliveryMode::QueueOnly => {
            session
                .services
                .agent_control
                .prepare_v2_agent_delivery(receiver_thread_id)
                .await
        }
        MessageDeliveryMode::TriggerTurn => {
            let resume_config = build_agent_resume_config(turn.as_ref())?;
            session
                .services
                .agent_control
                .prepare_v2_agent_delivery_with_reload(resume_config, receiver_thread_id)
                .await
        }
    }
    .map_err(|err| collab_agent_error(receiver_thread_id, err))?;
    let receiver_config = match mode {
        MessageDeliveryMode::QueueOnly => {
            session
                .services
                .agent_control
                .get_agent_config_snapshot(receiver_thread_id)
                .await
        }
        MessageDeliveryMode::TriggerTurn => Some(
            delivery
                .config_snapshot()
                .await
                .map_err(|err| collab_agent_error(receiver_thread_id, err))?,
        ),
    };
    if let Some(expected_model) = expected_model
        && receiver_config
            .as_ref()
            .is_some_and(|config| config.model != expected_model)
    {
        let receiver_model = receiver_config
            .as_ref()
            .map(|config| config.model.as_str())
            .unwrap_or_default();
        return Err(FunctionCallError::RespondToModel(format!(
            "follow-up task was not sent: target {receiver_agent_path} uses model `{receiver_model}`, not expected model `{expected_model}`",
        )));
    }
    let author = turn
        .session_source
        .get_agent_path()
        .unwrap_or_else(AgentPath::root);
    let communication =
        communication_from_tool_message(author, receiver_agent_path.clone(), message);
    let kind = match mode {
        MessageDeliveryMode::QueueOnly => AgentCommunicationKind::Message,
        MessageDeliveryMode::TriggerTurn => AgentCommunicationKind::Followup,
    };
    let context = AgentCommunicationContext::new(kind, session.thread_id);
    let result = delivery
        .send(mode.apply(communication), context, interrupt)
        .await
        .map_err(|err| collab_agent_error(receiver_thread_id, err));
    result?;
    emit_sub_agent_activity(
        &session,
        &turn,
        SubAgentActivityItem {
            id: call_id,
            agent_thread_id: receiver_thread_id,
            agent_path: receiver_agent_path.clone(),
            model: None,
            reasoning_effort: None,
            kind: SubAgentActivityKind::Interacted,
        },
    )
    .await;

    let output = match mode {
        MessageDeliveryMode::QueueOnly => {
            let receipt = SendMessageReceipt {
                task_name: receiver_agent_path.to_string(),
                recipient_task_name: receiver_agent_path.to_string(),
                effective_identity_scope: "recipient",
                handoff_state: "queued",
                effective_model: receiver_config.as_ref().map(|config| config.model.clone()),
                effective_model_provider_id: receiver_config
                    .as_ref()
                    .map(|config| config.model_provider_id.clone()),
                effective_reasoning_effort: receiver_config
                    .as_ref()
                    .and_then(|config| config.reasoning_effort.clone()),
                effective_service_tier: receiver_config
                    .as_ref()
                    .and_then(|config| config.service_tier.clone()),
            };
            tool_output_json_text(&receipt, "send_message")
        }
        MessageDeliveryMode::TriggerTurn => {
            let receiver_config = receiver_config.ok_or_else(|| {
                FunctionCallError::RespondToModel(format!(
                    "agent with id {receiver_thread_id} has no runtime config snapshot"
                ))
            })?;
            tool_output_json_text(
                &FollowupTaskResult {
                    task_name: receiver_agent_path.to_string(),
                    recipient_task_name: receiver_agent_path.to_string(),
                    effective_identity_scope: "recipient",
                    effective_model: receiver_config.model,
                    effective_model_provider_id: receiver_config.model_provider_id,
                    effective_reasoning_effort: receiver_config.reasoning_effort,
                    effective_service_tier: receiver_config.service_tier,
                },
                "followup_task",
            )
        }
    };
    Ok(FunctionToolOutput::from_text(output, Some(true)))
}

pub(crate) async fn handle_message_items_tool(
    invocation: ToolInvocation,
    mode: MessageDeliveryMode,
    target: String,
    items: Vec<UserInput>,
    interrupt: bool,
) -> Result<FunctionToolOutput, FunctionCallError> {
    let tool_name = invocation.tool_name.clone();
    let prompt = message_content_from_items(tool_name.name.as_str(), items)?;
    handle_message_submission(
        invocation, mode, target, prompt, interrupt, /*expected_model*/ None,
    )
    .await
}
