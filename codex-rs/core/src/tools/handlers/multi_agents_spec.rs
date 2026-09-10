use super::multi_agents_common::MAX_SPAWN_AGENT_MODEL_OVERRIDES;
use super::multi_agents_common::model_supports_multi_agent_backend;
use codex_protocol::openai_models::ModelPreset;
use codex_protocol::protocol::MultiAgentVersion;
use codex_tools::JsonSchema;
use codex_tools::ResponsesApiNamespace;
use codex_tools::ResponsesApiNamespaceTool;
use codex_tools::ResponsesApiTool;
use codex_tools::ToolSpec;
use serde_json::Value;
use serde_json::json;
use std::collections::BTreeMap;

use crate::tools::tool_runtime_capabilities::ToolRuntimeCapabilities;
use crate::tools::tool_runtime_capabilities::registered_tool_runtime_capabilities;

pub const MULTI_AGENT_V1_NAMESPACE: &str = "multi_agent_v1";
const MULTI_AGENT_V1_NAMESPACE_DESCRIPTION: &str = "Tools for spawning and managing sub-agents.";

const SPAWN_AGENT_INHERITED_MODEL_GUIDANCE: &str = "Spawned agents inherit your current model by default. Omit `model` to use that preferred default; set `model` only when an explicit override is needed.";
const SPAWN_AGENT_TYPE_OVERRIDE_DESCRIPTION_V1: &str = "Agent type override for the new agent. Omit to inherit the parent agent type with a full-history fork; otherwise, `default` is used.";
const SPAWN_AGENT_MODEL_OVERRIDE_DESCRIPTION: &str =
    "Model override for the new agent. Omit unless an explicit override is needed.";
const SPAWN_AGENT_MODEL_ASSERTION_DESCRIPTION: &str = "Optional exact model assertion. The spawn is rejected before child creation and prompt delivery if the model selected after role and profile resolution differs.";
const SPAWN_AGENT_REASONING_EFFORT_ASSERTION_DESCRIPTION: &str = "Optional exact reasoning effort assertion. The spawn is rejected before child creation and prompt delivery if the reasoning effort selected after model, role, and profile resolution differs.";
const SPAWN_AGENT_SERVICE_TIER_OVERRIDE_DESCRIPTION: &str =
    "Service tier override for the new agent. Omit unless explicitly requested.";
const MAX_REASONING_EFFORT_CHARS_IN_SPAWN_AGENT_DESCRIPTION: usize = 64;

#[derive(Debug, Clone)]
pub struct SpawnAgentToolOptions {
    pub available_models: Vec<ModelPreset>,
    pub agent_type_description: String,
    pub expose_agent_type: bool,
    pub hide_agent_type_model_reasoning: bool,
    pub expose_spawn_agent_model_overrides: bool,
    pub multi_agent_version: MultiAgentVersion,
    pub usage_hint_text: Option<String>,
}

impl Default for SpawnAgentToolOptions {
    fn default() -> Self {
        Self {
            available_models: Vec::new(),
            agent_type_description: String::new(),
            expose_agent_type: true,
            hide_agent_type_model_reasoning: false,
            expose_spawn_agent_model_overrides: false,
            multi_agent_version: MultiAgentVersion::Disabled,
            usage_hint_text: None,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct WaitAgentTimeoutOptions {
    pub default_timeout_ms: i64,
    pub min_timeout_ms: i64,
    pub max_timeout_ms: i64,
}

impl Default for WaitAgentTimeoutOptions {
    fn default() -> Self {
        Self {
            default_timeout_ms: super::multi_agents_common::DEFAULT_WAIT_TIMEOUT_MS,
            min_timeout_ms: super::multi_agents_common::MIN_WAIT_TIMEOUT_MS,
            max_timeout_ms: super::multi_agents_common::MAX_WAIT_TIMEOUT_MS,
        }
    }
}

pub fn create_spawn_agent_tool_v1(options: SpawnAgentToolOptions) -> ToolSpec {
    let available_models_description = (!options.hide_agent_type_model_reasoning).then(|| {
        spawn_agent_models_description(&options.available_models, options.multi_agent_version)
    });
    let inherited_model_guidance =
        (!options.hide_agent_type_model_reasoning).then_some(SPAWN_AGENT_INHERITED_MODEL_GUIDANCE);
    let return_value_description =
        "Returns the spawned agent id plus the user-facing nickname when available.";
    let mut properties = spawn_agent_common_properties_v1(&options.agent_type_description);
    if !options.expose_agent_type {
        properties.remove("agent_type");
    }
    if options.hide_agent_type_model_reasoning {
        hide_spawn_agent_metadata_options(&mut properties);
    }

    ToolSpec::Namespace(ResponsesApiNamespace {
        name: MULTI_AGENT_V1_NAMESPACE.to_string(),
        description: MULTI_AGENT_V1_NAMESPACE_DESCRIPTION.to_string(),
        tools: vec![ResponsesApiNamespaceTool::Function(ResponsesApiTool {
            name: "spawn_agent".to_string(),
            description: spawn_agent_tool_description(
                available_models_description.as_deref(),
                inherited_model_guidance,
                return_value_description,
                options.usage_hint_text,
            ),
            strict: false,
            defer_loading: None,
            parameters: JsonSchema::object(properties, /*required*/ None, Some(false.into())),
            output_schema: Some(spawn_agent_output_schema_v1()),
        })],
    })
}

pub fn create_spawn_agent_tool_v2(options: SpawnAgentToolOptions) -> ToolSpec {
    let available_models_description = options.expose_spawn_agent_model_overrides.then(|| {
        spawn_agent_models_description(&options.available_models, options.multi_agent_version)
    });
    let inherited_model_guidance = (options.expose_spawn_agent_model_overrides
        && !options.hide_agent_type_model_reasoning)
        .then_some(SPAWN_AGENT_INHERITED_MODEL_GUIDANCE);
    let mut properties = spawn_agent_common_properties_v2(&options.agent_type_description);
    if !options.expose_agent_type {
        properties.remove("agent_type");
    }
    if options.hide_agent_type_model_reasoning {
        properties.remove("service_tier");
    }
    if !options.expose_spawn_agent_model_overrides {
        properties.remove("model");
        properties.remove("reasoning_effort");
    }
    properties.insert(
        "task_name".to_string(),
        JsonSchema::string(Some(
            "Task name for the new agent. Use lowercase letters, digits, and underscores."
                .to_string(),
        )),
    );

    ToolSpec::Function(ResponsesApiTool {
        name: "spawn_agent".to_string(),
        description: spawn_agent_tool_description_v2(
            available_models_description.as_deref(),
            inherited_model_guidance,
            options.usage_hint_text,
        ),
        strict: false,
        defer_loading: None,
        parameters: JsonSchema::object(
            properties,
            Some(vec!["task_name".to_string(), "message".to_string()]),
            Some(false.into()),
        ),
        output_schema: Some(spawn_agent_output_schema_v2(
            options.hide_agent_type_model_reasoning,
        )),
    })
}

pub fn create_send_input_tool_v1() -> ToolSpec {
    let properties = BTreeMap::from([
        (
            "target".to_string(),
            JsonSchema::string(Some("Agent id to message (from spawn_agent).".to_string())),
        ),
        (
            "message".to_string(),
            JsonSchema::string(Some(
                "Legacy plain-text message to send to the agent. Use either message or items."
                    .to_string(),
            )),
        ),
        ("items".to_string(), create_collab_input_items_schema()),
        (
            "interrupt".to_string(),
            JsonSchema::boolean(Some(
                "True interrupts the current task and handles this message immediately; false or omitted queues it."
                    .to_string(),
            )),
        ),
    ]);

    ToolSpec::Namespace(ResponsesApiNamespace {
        name: MULTI_AGENT_V1_NAMESPACE.to_string(),
        description: MULTI_AGENT_V1_NAMESPACE_DESCRIPTION.to_string(),
        tools: vec![ResponsesApiNamespaceTool::Function(ResponsesApiTool {
            name: "send_input".to_string(),
            description: "Send a message to an existing agent. Use interrupt=true to redirect work immediately. You should reuse the agent by send_input if you believe your assigned task is highly dependent on the context of a previous task."
                .to_string(),
            strict: false,
            defer_loading: None,
            parameters: JsonSchema::object(properties, Some(vec!["target".to_string()]), Some(false.into())),
            output_schema: Some(send_input_output_schema()),
        })],
    })
}

pub fn create_send_message_tool() -> ToolSpec {
    let properties = BTreeMap::from([
        (
            "target".to_string(),
            JsonSchema::string(Some(
                "Relative or canonical task name to message (from spawn_agent).".to_string(),
            )),
        ),
        (
            "message".to_string(),
            JsonSchema::string(Some(
                "Message text to queue on the target agent.".to_string(),
            ))
            .with_encrypted(),
        ),
        (
            "interrupt".to_string(),
            JsonSchema::boolean(Some(
                "When true, stop the agent's current task and handle this immediately. When false (default), queue this message."
                    .to_string(),
            )),
        ),
    ]);

    ToolSpec::Function(ResponsesApiTool {
        name: "send_message".to_string(),
        description: "Send a text message to an existing agent. Use `interrupt=true` to stop the current task before queueing the message. Does not trigger a new turn. The receipt's effective_* identity fields describe the recipient agent named by target, never the sending agent."
            .to_string(),
        strict: false,
        defer_loading: None,
        parameters: JsonSchema::object(
            properties,
            Some(vec!["target".to_string(), "message".to_string()]),
            Some(false.into()),
        ),
        output_schema: Some(send_message_output_schema()),
    })
}

pub fn create_followup_task_tool() -> ToolSpec {
    let properties = BTreeMap::from([
        (
            "target".to_string(),
            JsonSchema::string(Some(
                "Agent id or canonical task name to send a follow-up task to (from spawn_agent)."
                    .to_string(),
            )),
        ),
        (
            "message".to_string(),
            JsonSchema::string(Some(
                "Message text to send to the target agent.".to_string(),
            ))
            .with_encrypted(),
        ),
        (
            "expected_model".to_string(),
            JsonSchema::string(Some(
                "Optional exact model assertion. The follow-up is rejected without sending the task if the target agent uses a different model."
                    .to_string(),
            )),
        ),
    ]);

    ToolSpec::Function(ResponsesApiTool {
        name: "followup_task".to_string(),
        description: "Send a follow-up task to an existing non-root target agent and trigger a turn if it is idle. If the target is already running, deliver the task promptly at message boundaries while sampling, or after the pending tool call completes. The receipt's effective_* identity fields describe the recipient agent named by target, never the sending agent."
            .to_string(),
        strict: false,
        defer_loading: None,
        parameters: JsonSchema::object(properties, Some(vec!["target".to_string(), "message".to_string()]), Some(false.into())),
        output_schema: Some(followup_task_output_schema()),
    })
}

pub fn create_resume_agent_tool() -> ToolSpec {
    let properties = BTreeMap::from([(
        "id".to_string(),
        JsonSchema::string(Some("Agent id to resume.".to_string())),
    )]);

    ToolSpec::Namespace(ResponsesApiNamespace {
        name: MULTI_AGENT_V1_NAMESPACE.to_string(),
        description: MULTI_AGENT_V1_NAMESPACE_DESCRIPTION.to_string(),
        tools: vec![ResponsesApiNamespaceTool::Function(ResponsesApiTool {
            name: "resume_agent".to_string(),
            description:
                "Resume a previously closed agent by id so it can receive send_input and wait_agent calls."
                    .to_string(),
            strict: false,
            defer_loading: None,
            parameters: JsonSchema::object(properties, Some(vec!["id".to_string()]), Some(false.into())),
            output_schema: Some(resume_agent_output_schema()),
        })],
    })
}

pub fn create_wait_agent_tool_v1(options: WaitAgentTimeoutOptions) -> ToolSpec {
    ToolSpec::Namespace(ResponsesApiNamespace {
        name: MULTI_AGENT_V1_NAMESPACE.to_string(),
        description: MULTI_AGENT_V1_NAMESPACE_DESCRIPTION.to_string(),
        tools: vec![ResponsesApiNamespaceTool::Function(ResponsesApiTool {
            name: "wait_agent".to_string(),
            description: "Wait for agents to reach a final status. Completed statuses may include the agent's final message. Returns empty status when timed out. Once the agent reaches a final status, a notification message will be received containing the same completed status."
                .to_string(),
            strict: false,
            defer_loading: None,
            parameters: wait_agent_tool_parameters_v1(options),
            output_schema: Some(wait_output_schema_v1()),
        })],
    })
}

pub fn create_wait_agent_tool_v2(options: WaitAgentTimeoutOptions) -> ToolSpec {
    create_wait_agent_tool_v2_with_capabilities(options, registered_tool_runtime_capabilities())
}

fn create_wait_agent_tool_v2_with_capabilities(
    options: WaitAgentTimeoutOptions,
    capabilities: ToolRuntimeCapabilities,
) -> ToolSpec {
    ToolSpec::Function(ResponsesApiTool {
        name: "wait_agent".to_string(),
        description: wait_agent_v2_description(capabilities.wait_agent.is_some()),
        strict: false,
        defer_loading: None,
        parameters: wait_agent_tool_parameters_v2(options, capabilities),
        output_schema: Some(wait_output_schema_v2(capabilities)),
    })
}

pub fn create_list_agents_tool() -> ToolSpec {
    create_list_agents_tool_with_capabilities(registered_tool_runtime_capabilities())
}

fn create_list_agents_tool_with_capabilities(capabilities: ToolRuntimeCapabilities) -> ToolSpec {
    let properties = BTreeMap::from([(
        "path_prefix".to_string(),
        JsonSchema::string(Some(
            "Task-path prefix filter without a trailing slash. Omit to list all live agents."
                .to_string(),
        )),
    )]);

    ToolSpec::Function(ResponsesApiTool {
        name: "list_agents".to_string(),
        description:
            "List live agents in the current root thread tree. Optionally filter by task-path prefix."
                .to_string(),
        strict: false,
        defer_loading: None,
        parameters: JsonSchema::object(properties, /*required*/ None, Some(false.into())),
        output_schema: Some(list_agents_output_schema(capabilities)),
    })
}

pub fn create_inspect_agent_tree_tool() -> ToolSpec {
    let properties = BTreeMap::from([
        (
            "target".to_string(),
            JsonSchema::string(Some(
                "Optional task-path root to inspect. Accepts the same relative or absolute task-path syntax."
                    .to_string(),
            )),
        ),
        (
            "agent_roots".to_string(),
            JsonSchema::array(
                JsonSchema::string(/*description*/ None),
                Some(
                    "Optional task-path roots to keep in the returned tree. Matching rows include the named agent and its descendants."
                        .to_string(),
                ),
            ),
        ),
        (
            "scope".to_string(),
            JsonSchema::string(Some(
                "Optional inspection scope. Use `live` for active sessions only, `stale` for persisted closed descendants only, or `all` to combine both."
                    .to_string(),
            )),
        ),
        (
            "max_depth".to_string(),
            JsonSchema::number(Some(
                "Optional maximum descendant depth to return, counted from the inspected subtree root."
                    .to_string(),
            )),
        ),
        (
            "max_agents".to_string(),
            JsonSchema::number(Some(
                "Optional maximum number of rows to return after tree ordering is applied."
                    .to_string(),
            )),
        ),
    ]);

    ToolSpec::Function(ResponsesApiTool {
        name: "inspect_agent_tree".to_string(),
        description: "Inspect a compact nested agent tree for the current subtree or a target task path. Returns tree rows, live-or-stale session state, optional branch-filter context, and summary counts without dumping full transcripts."
            .to_string(),
        strict: false,
        defer_loading: None,
        parameters: JsonSchema::object(properties, /*required*/ None, Some(false.into())),
        output_schema: Some(inspect_agent_tree_output_schema()),
    })
}

pub fn create_close_agent_tool_v1() -> ToolSpec {
    let properties = BTreeMap::from([(
        "target".to_string(),
        JsonSchema::string(Some("Agent id to close (from spawn_agent).".to_string())),
    )]);

    ToolSpec::Namespace(ResponsesApiNamespace {
        name: MULTI_AGENT_V1_NAMESPACE.to_string(),
        description: MULTI_AGENT_V1_NAMESPACE_DESCRIPTION.to_string(),
        tools: vec![ResponsesApiNamespaceTool::Function(ResponsesApiTool {
            name: "close_agent".to_string(),
            description: "Close an agent and any open descendants when they are no longer needed, and return the target agent's previous status before shutdown was requested. Completed agents remain open and count toward the concurrency limit until closed. Don't keep agents open for too long if they are not needed anymore.".to_string(),
            strict: false,
            defer_loading: None,
            parameters: JsonSchema::object(properties, Some(vec!["target".to_string()]), Some(false.into())),
            output_schema: Some(agent_previous_status_output_schema(
                "The agent status observed before shutdown was requested.",
            )),
        })],
    })
}

pub fn create_interrupt_agent_tool_v2() -> ToolSpec {
    let properties = BTreeMap::from([(
        "target".to_string(),
        JsonSchema::string(Some(
            "Agent id or canonical task name to interrupt (from spawn_agent).".to_string(),
        )),
    )]);

    ToolSpec::Function(ResponsesApiTool {
        name: "interrupt_agent".to_string(),
        description: "Interrupt an agent's current turn, if any, and return its previous status. The agent remains available for messages and follow-up tasks.".to_string(),
        strict: false,
        defer_loading: None,
        parameters: JsonSchema::object(properties, Some(vec!["target".to_string()]), Some(false.into())),
        output_schema: Some(agent_previous_status_output_schema(
            "The agent status observed before the interrupt request was handled.",
        )),
    })
}

fn agent_status_output_schema() -> Value {
    json!({
        "oneOf": [
            {
                "type": "string",
                "enum": ["pending_init", "running", "interrupted", "shutdown", "not_found"]
            },
            {
                "type": "object",
                "properties": {
                    "completed": {
                        "type": ["string", "null"]
                    }
                },
                "required": ["completed"],
                "additionalProperties": false
            },
            {
                "type": "object",
                "properties": {
                    "errored": {
                        "type": "string"
                    }
                },
                "required": ["errored"],
                "additionalProperties": false
            }
        ]
    })
}

fn spawn_agent_output_schema_v1() -> Value {
    json!({
        "type": "object",
        "properties": {
            "agent_id": {
                "type": "string",
                "description": "Thread identifier for the spawned agent."
            },
            "nickname": {
                "type": ["string", "null"],
                "description": "User-facing nickname for the spawned agent when available."
            }
        },
        "required": ["agent_id", "nickname"],
        "additionalProperties": false
    })
}

fn spawn_agent_output_schema_v2(hide_agent_metadata: bool) -> Value {
    let mut required = vec![
        "task_name".to_string(),
        "effective_model".to_string(),
        "effective_reasoning_effort".to_string(),
    ];
    let mut properties = serde_json::Map::from_iter([
        (
            "task_name".to_string(),
            json!({
                "type": "string",
                "description": "Canonical task name for the spawned agent."
            }),
        ),
        (
            "requested_model".to_string(),
            json!({
                "type": "string",
                "description": "Model explicitly requested for the spawned agent."
            }),
        ),
        (
            "requested_reasoning_effort".to_string(),
            json!({
                "type": "string",
                "description": "Reasoning effort explicitly requested for the spawned agent."
            }),
        ),
        (
            "effective_model".to_string(),
            json!({
                "type": "string",
                "description": "Model selected after role and profile resolution."
            }),
        ),
        (
            "requested_model_honored".to_string(),
            json!({
                "type": "boolean",
                "description": "Whether the requested model was selected."
            }),
        ),
        (
            "effective_reasoning_effort".to_string(),
            json!({
                "type": ["string", "null"],
                "description": "Reasoning effort selected after role and profile resolution, when configured."
            }),
        ),
    ]);
    if !hide_agent_metadata {
        required.push("agent_id".to_string());
        properties.insert(
            "agent_id".to_string(),
            json!({
                "type": "string",
                "description": "Thread identifier for the spawned agent."
            }),
        );
        properties.insert(
            "nickname".to_string(),
            json!({
                "type": "string",
                "description": "User-facing nickname for the spawned agent when available."
            }),
        );
    }

    json!({
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": false
    })
}

fn followup_task_output_schema() -> Value {
    json!({
        "type": "object",
        "properties": {
            "task_name": {
                "type": "string",
                "description": "Canonical task name of the agent receiving the follow-up."
            },
            "recipient_task_name": {
                "type": "string",
                "description": "Explicit canonical task name of the recipient; the effective_* fields below describe this recipient, not the sending agent."
            },
            "effective_identity_scope": {
                "type": "string",
                "enum": ["recipient"],
                "description": "Identity scope for the effective_* fields in this receipt."
            },
            "effective_model": {
                "type": "string",
                "description": "Effective model retained by the recipient agent for the follow-up turn."
            },
            "effective_model_provider_id": {
                "type": "string",
                "description": "Effective model provider retained by the recipient agent for the follow-up turn."
            },
            "effective_reasoning_effort": {
                "type": ["string", "null"],
                "description": "Effective reasoning effort retained by the recipient agent for the follow-up turn, when configured."
            },
            "effective_service_tier": {
                "type": ["string", "null"],
                "description": "Effective service tier retained by the recipient agent for the follow-up turn, when configured."
            }
        },
        "required": [
            "task_name",
            "recipient_task_name",
            "effective_identity_scope",
            "effective_model",
            "effective_model_provider_id",
            "effective_reasoning_effort",
            "effective_service_tier"
        ],
        "additionalProperties": false
    })
}

fn send_input_output_schema() -> Value {
    json!({
        "type": "object",
        "properties": {
            "submission_id": {
                "type": "string",
                "description": "Identifier for the queued input submission."
            }
        },
        "required": ["submission_id"],
        "additionalProperties": false
    })
}

fn send_message_output_schema() -> Value {
    json!({
        "type": "object",
        "properties": {
            "task_name": {
                "type": "string",
                "description": "Canonical target task name that accepted the handoff."
            },
            "recipient_task_name": {
                "type": "string",
                "description": "Explicit canonical task name of the recipient; the effective_* fields below describe this recipient, not the sending agent."
            },
            "effective_identity_scope": {
                "type": "string",
                "enum": ["recipient"],
                "description": "Identity scope for the effective_* fields in this receipt."
            },
            "handoff_state": {
                "type": "string",
                "enum": ["queued"],
                "description": "The runtime accepted the message for queue-only delivery; it does not represent a model acknowledgement or completion."
            },
            "effective_model": {
                "type": ["string", "null"],
                "description": "Effective model of the recipient when its runtime is already loaded. Null for a cold or evicted target so queue-only delivery does not activate it."
            },
            "effective_model_provider_id": {
                "type": ["string", "null"],
                "description": "Effective model provider of the recipient when its runtime is already loaded. Null for a cold or evicted target so queue-only delivery does not activate it."
            },
            "effective_reasoning_effort": {
                "type": ["string", "null"],
                "description": "Effective reasoning effort of the recipient when configured."
            },
            "effective_service_tier": {
                "type": ["string", "null"],
                "description": "Effective service tier of the recipient when configured."
            }
        },
        "required": [
            "task_name",
            "recipient_task_name",
            "effective_identity_scope",
            "handoff_state",
            "effective_model",
            "effective_model_provider_id",
            "effective_reasoning_effort",
            "effective_service_tier"
        ],
        "additionalProperties": false
    })
}

fn list_agents_output_schema(capabilities: ToolRuntimeCapabilities) -> Value {
    let include_active_descendants = capabilities
        .subagent_inventory
        .is_some_and(|capability| capability.include_active_descendants);
    let mut agent_properties = serde_json::Map::from_iter([
        (
            "agent_name".to_string(),
            json!({
                "type": "string",
                "description": "Canonical task name for the agent when available, otherwise the agent id."
            }),
        ),
        (
            "agent_status".to_string(),
            json!({
                "description": "Last known status of the agent.",
                "allOf": [agent_status_output_schema()]
            }),
        ),
    ]);
    let mut agent_required = vec!["agent_name".to_string(), "agent_status".to_string()];
    if include_active_descendants {
        agent_properties.insert(
            "has_active_subagents".to_string(),
            json!({
                "type": "boolean",
                "description": "Whether this agent has active live descendants."
            }),
        );
        agent_properties.insert(
            "active_subagent_count".to_string(),
            json!({
                "type": "number",
                "description": "Number of active live descendants below this agent."
            }),
        );
        agent_required.push("has_active_subagents".to_string());
        agent_required.push("active_subagent_count".to_string());
    }

    json!({
        "type": "object",
        "properties": {
            "agents": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": agent_properties,
                    "required": agent_required,
                    "additionalProperties": false
                },
                "description": "Live agents visible in the current root thread tree."
            }
        },
        "required": ["agents"],
        "additionalProperties": false
    })
}

fn inspect_agent_tree_output_schema() -> Value {
    json!({
        "type": "object",
        "properties": {
            "root_agent_name": {
                "type": "string",
                "description": "Canonical task name or agent id for the inspected subtree root."
            },
            "scope_applied": {
                "type": "string",
                "description": "Inspection scope actually applied to the result."
            },
            "agent_roots_applied": {
                "type": ["array", "null"],
                "items": { "type": "string" },
                "description": "Branch filters applied to the inspected tree when present."
            },
            "max_depth_applied": {
                "type": "number",
                "description": "Maximum descendant depth applied to this inspection."
            },
            "max_agents_applied": {
                "type": "number",
                "description": "Maximum row count applied to this inspection."
            },
            "truncated": {
                "type": "boolean",
                "description": "Whether the returned tree rows were truncated."
            },
            "summary": {
                "type": "object",
                "properties": {
                    "total_agents": { "type": "number" },
                    "live_agents": { "type": "number" },
                    "stale_agents": { "type": "number" },
                    "pending_init_agents": { "type": "number" },
                    "running_agents": { "type": "number" },
                    "interrupted_agents": { "type": "number" },
                    "completed_agents": { "type": "number" },
                    "errored_agents": { "type": "number" },
                    "shutdown_agents": { "type": "number" },
                    "not_found_agents": { "type": "number" }
                },
                "required": [
                    "total_agents",
                    "live_agents",
                    "stale_agents",
                    "pending_init_agents",
                    "running_agents",
                    "interrupted_agents",
                    "completed_agents",
                    "errored_agents",
                    "shutdown_agents",
                    "not_found_agents"
                ],
                "additionalProperties": false
            },
            "agents": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "agent_name": { "type": "string" },
                        "depth": { "type": "number" },
                        "session_state": { "type": "string" },
                        "agent_status": { "type": "string" },
                        "nickname": { "type": ["string", "null"] },
                        "role": { "type": ["string", "null"] },
                        "effective_model": {
                            "type": ["string", "null"],
                            "description": "Effective model from the live thread configuration snapshot. Null for stale threads; this is configuration evidence, not provider-usage proof."
                        },
                        "effective_reasoning_effort": {
                            "type": ["string", "null"],
                            "description": "Effective reasoning effort from the live thread configuration snapshot. Null for stale threads; this is configuration evidence, not provider-usage proof."
                        },
                        "direct_child_count": { "type": "number" },
                        "descendant_count": { "type": "number" }
                    },
                    "required": [
                        "agent_name",
                        "depth",
                        "session_state",
                        "agent_status",
                        "nickname",
                        "role",
                        "effective_model",
                        "effective_reasoning_effort",
                        "direct_child_count",
                        "descendant_count"
                    ],
                    "additionalProperties": false
                }
            }
        },
        "required": [
            "root_agent_name",
            "scope_applied",
            "agent_roots_applied",
            "max_depth_applied",
            "max_agents_applied",
            "truncated",
            "summary",
            "agents"
        ],
        "additionalProperties": false
    })
}

fn resume_agent_output_schema() -> Value {
    json!({
        "type": "object",
        "properties": {
            "status": agent_status_output_schema()
        },
        "required": ["status"],
        "additionalProperties": false
    })
}

fn wait_output_schema_v1() -> Value {
    json!({
        "type": "object",
        "properties": {
            "status": {
                "type": "object",
                "description": "Final statuses keyed by agent id.",
                "additionalProperties": agent_status_output_schema()
            },
            "timed_out": {
                "type": "boolean",
                "description": "Whether the wait call returned due to timeout before any agent reached a final status."
            }
        },
        "required": ["status", "timed_out"],
        "additionalProperties": false
    })
}

fn wait_output_schema_v2(capabilities: ToolRuntimeCapabilities) -> Value {
    let wait_capability = capabilities.wait_agent;
    let include_pending_ids = wait_capability.is_some_and(|capability| capability.pending_ids);
    let include_completion_reason =
        wait_capability.is_some_and(|capability| capability.completion_reason);
    let mut properties = serde_json::Map::from_iter([
        (
            "message".to_string(),
            json!({
                "type": "string",
                "description": "Brief wait summary without the agent's final content."
            }),
        ),
        (
            "requested_ids".to_string(),
            json!({
                "type": "array",
                "items": {
                    "type": "string"
                },
                "description": "Agent ids requested by the wait call."
            }),
        ),
        (
            "timed_out".to_string(),
            json!({
                "type": "boolean",
                "description": "Whether the wait call returned because it hit the timeout."
            }),
        ),
    ]);
    let mut required = vec![
        "message".to_string(),
        "requested_ids".to_string(),
        "timed_out".to_string(),
    ];
    if include_pending_ids {
        properties.insert(
            "pending_ids".to_string(),
            json!({
                "type": "array",
                "items": {
                    "type": "string"
                },
                "description": "Requested agent ids that were still non-terminal when the wait call returned."
            }),
        );
        required.push("pending_ids".to_string());
    }
    if include_completion_reason {
        properties.insert(
            "completion_reason".to_string(),
            json!({
                "type": "string",
                "enum": ["terminal", "mailbox", "timeout", "subscription_loss"],
                "description": "Why the wait call returned."
            }),
        );
        required.push("completion_reason".to_string());
    }

    json!({
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": false
    })
}

fn agent_previous_status_output_schema(previous_status_description: &str) -> Value {
    json!({
        "type": "object",
        "properties": {
            "previous_status": {
                "description": previous_status_description,
                "allOf": [agent_status_output_schema()]
            }
        },
        "required": ["previous_status"],
        "additionalProperties": false
    })
}

fn create_collab_input_items_schema() -> JsonSchema {
    let properties = BTreeMap::from([
        (
            "type".to_string(),
            JsonSchema::string(Some(
                "Input item type: text, image, local_image, audio, local_audio, skill, or mention."
                    .to_string(),
            )),
        ),
        (
            "text".to_string(),
            JsonSchema::string(Some("Text content when type is text.".to_string())),
        ),
        (
            "image_url".to_string(),
            JsonSchema::string(Some("Image URL when type is image.".to_string())),
        ),
        (
            "audio_url".to_string(),
            JsonSchema::string(Some("Audio data URL when type is audio.".to_string())),
        ),
        (
            "path".to_string(),
            JsonSchema::string(Some(
                "Path when type is local_image/local_audio/skill, or structured mention target such as app://<connector-id> or plugin://<plugin-name>@<marketplace-name> when type is mention."
                    .to_string(),
            )),
        ),
        (
            "name".to_string(),
            JsonSchema::string(Some("Display name when type is skill or mention.".to_string())),
        ),
    ]);

    JsonSchema::array(JsonSchema::object(properties, /*required*/ None, Some(false.into())), Some(
            "Structured input items. Use this to pass explicit mentions (for example app:// connector paths)."
                .to_string(),
        ))
}

fn spawn_agent_common_properties_v1(agent_type_description: &str) -> BTreeMap<String, JsonSchema> {
    BTreeMap::from([
        (
            "message".to_string(),
            JsonSchema::string(Some(
                "Initial plain-text task for the new agent. Use either message or items."
                    .to_string(),
            )),
        ),
        ("items".to_string(), create_collab_input_items_schema()),
        (
            "agent_type".to_string(),
            JsonSchema::string(Some(format!(
                "{SPAWN_AGENT_TYPE_OVERRIDE_DESCRIPTION_V1}\n{agent_type_description}"
            ))),
        ),
        (
            "fork_context".to_string(),
            JsonSchema::boolean(Some(
                "True forks the current thread history into the new agent; false or omitted starts with only the initial prompt."
                    .to_string(),
            )),
        ),
        (
            "model".to_string(),
            JsonSchema::string(Some(
                SPAWN_AGENT_MODEL_OVERRIDE_DESCRIPTION.to_string(),
            )),
        ),
        (
            "reasoning_effort".to_string(),
            JsonSchema::string(Some(
                "Reasoning effort override for the new agent. Omit to inherit the parent effort."
                    .to_string(),
            )),
        ),
        (
            "service_tier".to_string(),
            JsonSchema::string(Some(
                SPAWN_AGENT_SERVICE_TIER_OVERRIDE_DESCRIPTION.to_string(),
            )),
        ),
    ])
}

fn spawn_agent_common_properties_v2(agent_type_description: &str) -> BTreeMap<String, JsonSchema> {
    BTreeMap::from([
        (
            "message".to_string(),
            JsonSchema::string(Some(
                "Initial plain-text task for the new agent.".to_string(),
            ))
            .with_encrypted(),
        ),
        (
            "agent_type".to_string(),
            JsonSchema::string(Some(format!(
                "Agent type override for the new agent. Omit unless explicitly asked. Set `fork_turns` to `none` or a positive integer when an explicit override is needed.\n{agent_type_description}"
            ))),
        ),
        (
            "fork_turns".to_string(),
            JsonSchema::string(Some(
                "Optional number of turns to fork. Defaults to `all`. Use `none`, `all`, or a positive integer string such as `3` to fork only the most recent turns."
                    .to_string(),
            )),
        ),
        (
            "model".to_string(),
            JsonSchema::string(Some(
                SPAWN_AGENT_MODEL_OVERRIDE_DESCRIPTION.to_string(),
            )),
        ),
        (
            "expected_model".to_string(),
            JsonSchema::string(Some(
                SPAWN_AGENT_MODEL_ASSERTION_DESCRIPTION.to_string(),
            )),
        ),
        (
            "reasoning_effort".to_string(),
            JsonSchema::string(Some(
                "Reasoning effort override for the new agent. Omit to inherit the parent effort."
                    .to_string(),
            )),
        ),
        (
            "expected_reasoning_effort".to_string(),
            JsonSchema::string(Some(
                SPAWN_AGENT_REASONING_EFFORT_ASSERTION_DESCRIPTION.to_string(),
            )),
        ),
        (
            "service_tier".to_string(),
            JsonSchema::string(Some(
                SPAWN_AGENT_SERVICE_TIER_OVERRIDE_DESCRIPTION.to_string(),
            )),
        ),
    ])
}

fn hide_spawn_agent_metadata_options(properties: &mut BTreeMap<String, JsonSchema>) {
    properties.remove("agent_type");
    properties.remove("model");
    properties.remove("reasoning_effort");
    properties.remove("service_tier");
}

fn spawn_agent_tool_description(
    available_models_description: Option<&str>,
    inherited_model_guidance: Option<&str>,
    return_value_description: &str,
    usage_hint_text: Option<String>,
) -> String {
    let agent_role_guidance = available_models_description.unwrap_or_default();
    let inherited_model_guidance = inherited_model_guidance.unwrap_or_default();

    let tool_description = format!(
        r#"
        {agent_role_guidance}
        Spawn a sub-agent for a well-scoped task. {return_value_description} {inherited_model_guidance}"#
    );

    if let Some(usage_hint_text) = usage_hint_text {
        return format!(
            r#"
        {tool_description}
{usage_hint_text}"#
        );
    }
    let default_usage_hint = default_spawn_agent_usage_hint(available_models_description);
    format!(
        r#"
        {tool_description}
{default_usage_hint}"#
    )
}

fn spawn_agent_tool_description_v2(
    available_models_description: Option<&str>,
    inherited_model_guidance: Option<&str>,
    usage_hint_text: Option<String>,
) -> String {
    let agent_role_guidance = available_models_description.unwrap_or_default();
    let inherited_model_guidance = inherited_model_guidance.unwrap_or_default();

    let tool_description = format!(
        r#"
        {agent_role_guidance}
        Spawns an agent to work on the specified task. If your current task is `/root/task1` and you spawn_agent with task_name "task_3" the agent will have canonical task name `/root/task1/task_3`.
You are then able to refer to this agent as `task_3` or `/root/task1/task_3` interchangeably. However an agent `/root/task2/task_3` would only be able to communicate with this agent via its canonical name `/root/task1/task_3`.
The spawned agent will have the same tools as you and the ability to spawn its own subagents.
{inherited_model_guidance}
The `fork_turns` field is optional. Optional number of turns to fork. Defaults to `all`. Use `none`, `all`, or a positive integer string such as `3` to fork only the most recent turns. Passing `fork_turns="none"` carries no surrounding context, while `fork_turns="all"` carries all surrounding context.
Only call this tool for a concrete, bounded subtask that can run independently alongside useful local work; otherwise continue locally.
It will be able to send you and other running agents messages, and its final answer will be provided to you when it finishes.
The new agent's canonical task name will be provided to it along with the message."#
    );

    if let Some(usage_hint_text) = usage_hint_text {
        return format!(
            r#"
        {tool_description}
{usage_hint_text}"#
        );
    }
    let default_usage_hint = default_spawn_agent_usage_hint(available_models_description);
    format!(
        r#"
        {tool_description}
{default_usage_hint}"#
    )
}

fn default_spawn_agent_usage_hint(available_models_description: Option<&str>) -> String {
    let agent_role_usage_hint = available_models_description
        .map(|_| {
            "Agent-role guidance below only helps choose which agent to use after spawning is already authorized; it never authorizes spawning by itself."
        })
        .unwrap_or_default();
    format!(
        r#"This spawn_agent tool provides access to sub-agents for bounded parallel work. Do not set the `model` field unless the user explicitly asks for a different model or there is a clear task-specific reason. You should follow the rules and guidelines below to use this tool.

Do not spawn sub-agents unless the user or applicable AGENTS.md/skill instructions explicitly ask for sub-agents, delegation, or parallel agent work.
Requests for depth, thoroughness, research, investigation, or detailed codebase analysis do not count as permission to spawn.
{agent_role_usage_hint}

### When to delegate vs. do the subtask yourself
- First, quickly analyze the overall user task and form a succinct high-level plan. Identify which tasks are immediate blockers on the critical path, and which tasks are sidecar tasks that are needed but can run in parallel without blocking the next local step. As part of that plan, explicitly decide what immediate task you should do locally right now. Do this planning step before delegating to agents so you do not hand off the immediate blocking task to a submodel and then waste time waiting on it.
- Use a subagent when a subtask is easy enough for it to handle and can run in parallel with your local work. Prefer delegating concrete, bounded sidecar tasks that materially advance the main task without blocking your immediate next local step.
- Do not delegate urgent blocking work when your immediate next step depends on that result. If the very next action is blocked on that task, the main rollout should usually do it locally to keep the critical path moving.
- Keep work local when the subtask is too difficult to delegate well and when it is tightly coupled, urgent, or likely to block your immediate next step.

### Designing delegated subtasks
- Subtasks must be concrete, well-defined, and self-contained.
- Delegated subtasks must materially advance the main task.
- Do not duplicate work between the main rollout and delegated subtasks.
- Avoid issuing multiple delegate calls on the same unresolved thread unless the new delegated task is genuinely different and necessary.
- Narrow the delegated ask to the concrete output you need next.
- For coding tasks, prefer delegating concrete code-change worker subtasks over read-only explorer analysis when the subagent can make a bounded patch in a clear write scope.
- When delegating coding work, instruct the submodel to edit files directly in its forked workspace and list the file paths it changed in the final answer.
- For code-edit subtasks, decompose work so each delegated task has a disjoint write set.

### After you delegate
- Call wait_agent very sparingly. Only call wait_agent when you need the result immediately for the next critical-path step and you are blocked until it returns.
- Call wait_agent directly when blocked on a child or mailbox event. Its V2 wait is event-driven, so do not poll list_agents or inspect_agent_tree while the awaited state is unchanged. If a mailbox wake is unrelated to the requested terminal condition, rearm the same target set and return_when contract rather than starting a new status-discovery loop.
- Do not redo delegated subagent tasks yourself; focus on integrating results or tackling non-overlapping work.
- While the subagent is running in the background, do meaningful non-overlapping work immediately.
- Do not repeatedly wait by reflex.
- When a delegated coding task returns, quickly review the uploaded changes, then integrate or refine them.

### Parallel delegation patterns
- Run multiple independent information-seeking subtasks in parallel when you have distinct questions that can be answered independently.
- Split implementation into disjoint codebase slices and spawn multiple agents for them in parallel when the write scopes do not overlap.
- Delegate verification only when it can run in parallel with ongoing implementation and is likely to catch a concrete risk before final integration.
- The key is to find opportunities to spawn multiple independent subtasks in parallel within the same round, while ensuring each subtask is well-defined, self-contained, and materially advances the main task."#
    )
}

fn spawn_agent_models_description(
    models: &[ModelPreset],
    multi_agent_version: MultiAgentVersion,
) -> String {
    let visible_models: Vec<&ModelPreset> = models
        .iter()
        .filter(|model| model.show_in_picker)
        .filter(|model| model_supports_multi_agent_backend(model, multi_agent_version))
        .take(MAX_SPAWN_AGENT_MODEL_OVERRIDES)
        .collect();
    if visible_models.is_empty() {
        return "No picker-visible model overrides are currently loaded.".to_string();
    }

    let model_descriptions = visible_models
        .into_iter()
        .map(|model| {
            let default_reasoning_effort = &model.default_reasoning_effort;
            let efforts = model
                .supported_reasoning_efforts
                .iter()
                .map(|preset| {
                    let effort = preset.effort.as_str();
                    let effort = match effort
                        .char_indices()
                        .nth(MAX_REASONING_EFFORT_CHARS_IN_SPAWN_AGENT_DESCRIPTION)
                    {
                        Some((index, _)) => &effort[..index],
                        None => effort,
                    };
                    if &preset.effort == default_reasoning_effort {
                        format!("{effort} (default)")
                    } else {
                        effort.to_string()
                    }
                })
                .collect::<Vec<_>>()
                .join(", ");
            let reasoning_efforts_suffix = if efforts.is_empty() {
                String::new()
            } else {
                format!(" Reasoning efforts: {efforts}.")
            };
            let service_tiers = model
                .service_tiers
                .iter()
                .map(|tier| tier.id.as_str())
                .collect::<Vec<_>>()
                .join(", ");
            let service_tiers_suffix = if service_tiers.is_empty() {
                String::new()
            } else {
                format!(" Service tiers: {service_tiers}.")
            };
            let model_slug = &model.model;
            let description = &model.description;
            format!(
                "- `{model_slug}`: {description}{reasoning_efforts_suffix}{service_tiers_suffix}"
            )
        })
        .collect::<Vec<_>>()
        .join("\n");
    format!(
        "Available model overrides (optional; inherited parent model is preferred):\n{model_descriptions}"
    )
}

fn wait_agent_tool_parameters_v1(options: WaitAgentTimeoutOptions) -> JsonSchema {
    let properties = BTreeMap::from([
        (
            "targets".to_string(),
            JsonSchema::array(
                JsonSchema::string(/*description*/ None),
                Some(
                    "Agent ids to wait on. Pass multiple ids to wait for whichever finishes first."
                        .to_string(),
                ),
            ),
        ),
        (
            "timeout_ms".to_string(),
            JsonSchema::number(Some(format!(
                "Timeout in milliseconds. Defaults to {}, min {}, max {}. Prefer longer waits (minutes) to avoid busy polling.",
                options.default_timeout_ms, options.min_timeout_ms, options.max_timeout_ms,
            ))),
        ),
    ]);

    JsonSchema::object(
        properties,
        Some(vec!["targets".to_string()]),
        Some(false.into()),
    )
}

fn wait_agent_tool_parameters_v2(
    options: WaitAgentTimeoutOptions,
    capabilities: ToolRuntimeCapabilities,
) -> JsonSchema {
    let mut properties = BTreeMap::from([
        (
            "targets".to_string(),
            JsonSchema::array(
                JsonSchema::string(/*description*/ None),
                Some(
                    "Optional agent ids or task-path references to wait on. Pass multiple targets to wait for whichever finishes first unless return_when=all. Omit to wait only for mailbox activity or timeout."
                        .to_string(),
                ),
            ),
        ),
        (
            "timeout_ms".to_string(),
            JsonSchema::number(Some(format!(
                "Optional timeout in milliseconds. Defaults to {}, min {}, max {}. Prefer longer waits to avoid busy polling.",
                options.default_timeout_ms, options.min_timeout_ms, options.max_timeout_ms,
            ))),
        ),
    ]);

    if capabilities
        .wait_agent
        .is_some_and(|capability| capability.return_when)
    {
        properties.insert(
            "return_when".to_string(),
            JsonSchema::string_enum(
                vec![
                    serde_json::Value::String("any".to_string()),
                    serde_json::Value::String("all".to_string()),
                ],
                Some("Whether the wait completes when any requested agent reaches terminal status or only after all requested agents are terminal.".to_string()),
            ),
        );
    }

    if capabilities
        .wait_agent
        .is_some_and(|capability| capability.native_event_wait)
    {
        properties.insert(
            "native_event_wait".to_string(),
            JsonSchema::boolean(Some(
                "Keep this invocation pending across internal lease expiries until a target event, mailbox input, cancellation, or subscription loss. When true, timeout_ms is an internal lease window rather than a user-visible timeout.".to_string(),
            )),
        );
    }

    JsonSchema::object(properties, /*required*/ None, Some(false.into()))
}

fn wait_agent_v2_description(include_runtime_capability: bool) -> String {
    if include_runtime_capability {
        "Use this for blocking coordination while awaiting sub-agent completion. Waits on the requested agents until the requested completion rule is satisfied, but may also wake early when the current agent receives new mailbox activity. Omit `targets` to wait only for mailbox activity or timeout. When `return_when` is `any`, completion requires any requested agent to reach terminal status. When `return_when` is `all`, completion requires all requested agents to reach terminal status. Does not return mailbox content; returns an explicit completion reason plus the still-pending targets when applicable. Prefer longer timeouts to avoid busy polling."
            .to_string()
    } else {
        "Use this for blocking coordination while awaiting sub-agent completion. Waits on the requested agents until an agent reaches terminal status or the timeout expires. Prefer longer timeouts to avoid busy polling."
            .to_string()
    }
}

#[cfg(test)]
#[path = "multi_agents_spec_tests.rs"]
mod tests;
