use super::*;
use codex_protocol::openai_models::ModelPreset;
use codex_protocol::openai_models::ModelServiceTier;
use codex_protocol::openai_models::ReasoningEffort;
use codex_protocol::openai_models::ReasoningEffortPreset;
use codex_protocol::protocol::MultiAgentVersion;
use codex_tools::JsonSchemaPrimitiveType;
use codex_tools::JsonSchemaType;
use pretty_assertions::assert_eq;
use serde_json::json;

fn model_preset(id: &str, show_in_picker: bool) -> ModelPreset {
    ModelPreset {
        id: id.to_string(),
        model: format!("{id}-model"),
        display_name: format!("{id} display"),
        description: format!("{id} description"),
        default_reasoning_effort: ReasoningEffort::Medium,
        supported_reasoning_efforts: vec![ReasoningEffortPreset {
            effort: ReasoningEffort::Medium,
            description: "Balanced".to_string(),
        }],
        supports_personality: false,
        additional_speed_tiers: Vec::new(),
        service_tiers: vec![ModelServiceTier {
            id: "priority".to_string(),
            name: "Fast".to_string(),
            description: "1.5x speed, increased usage".to_string(),
        }],
        default_service_tier: None,
        is_default: false,
        upgrade: None,
        show_in_picker,
        multi_agent_version: Some(MultiAgentVersion::V2),
        availability_nux: None,
        supported_in_api: true,
        input_modalities: Vec::new(),
    }
}

#[test]
fn spawn_agent_tool_v2_requires_task_name_and_lists_visible_models() {
    let mut incompatible = model_preset("incompatible", /*show_in_picker*/ true);
    incompatible.multi_agent_version = Some(MultiAgentVersion::V1);
    let mut luna = model_preset("luna", /*show_in_picker*/ true);
    luna.model = "gpt-5.6-luna".to_string();
    luna.multi_agent_version = Some(MultiAgentVersion::V1);
    let mut unspecified = model_preset("unspecified", /*show_in_picker*/ true);
    unspecified.multi_agent_version = None;
    let tool = create_spawn_agent_tool_v2(SpawnAgentToolOptions {
        available_models: vec![
            model_preset("visible", /*show_in_picker*/ true),
            model_preset("hidden", /*show_in_picker*/ false),
            unspecified,
            luna,
            incompatible,
        ],
        agent_type_description: "role help".to_string(),
        expose_agent_type: true,
        hide_agent_type_model_reasoning: false,
        expose_spawn_agent_model_overrides: true,
        multi_agent_version: MultiAgentVersion::V2,
        usage_hint_text: None,
    });

    let ToolSpec::Function(ResponsesApiTool {
        description,
        parameters,
        output_schema,
        ..
    }) = tool
    else {
        panic!("spawn_agent should be a function tool");
    };
    assert_eq!(
        parameters.schema_type,
        Some(JsonSchemaType::Single(JsonSchemaPrimitiveType::Object))
    );
    let properties = parameters
        .properties
        .as_ref()
        .expect("spawn_agent should use object params");
    assert!(description.contains("Spawns an agent to work on the specified task."));
    assert!(description.contains("The spawned agent will have the same tools as you"));
    assert!(!description.contains("max_concurrent_threads_per_session"));
    assert!(description.contains(SPAWN_AGENT_INHERITED_MODEL_GUIDANCE));
    assert!(
        description
            .contains("Available model overrides (optional; inherited parent model is preferred):")
    );
    assert!(description.contains(
        "- `visible-model`: visible description Reasoning efforts: medium (default). Service tiers: priority."
    ));
    assert!(description.contains(
        "- `unspecified-model`: unspecified description Reasoning efforts: medium (default). Service tiers: priority."
    ));
    assert!(description.contains(
        "- `gpt-5.6-luna`: luna description Reasoning efforts: medium (default). Service tiers: priority."
    ));
    assert!(!description.contains("hidden-model"));
    assert!(!description.contains("incompatible-model"));
    assert!(properties.contains_key("task_name"));
    assert!(properties.contains_key("message"));
    assert_eq!(
        properties
            .get("message")
            .and_then(|schema| schema.encrypted),
        Some(true)
    );
    assert!(properties.contains_key("fork_turns"));
    assert!(!properties.contains_key("spawn_approval"));
    assert!(!properties.contains_key("items"));
    assert!(!properties.contains_key("fork_context"));
    assert_eq!(
        properties
            .get("model")
            .and_then(|schema| schema.description.as_deref()),
        Some(SPAWN_AGENT_MODEL_OVERRIDE_DESCRIPTION)
    );
    assert_eq!(
        properties
            .get("expected_model")
            .and_then(|schema| schema.description.as_deref()),
        Some(SPAWN_AGENT_MODEL_ASSERTION_DESCRIPTION)
    );
    assert_eq!(
        properties
            .get("reasoning_effort")
            .and_then(|schema| schema.description.as_deref()),
        Some("Reasoning effort override for the new agent. Omit to inherit the parent effort.")
    );
    assert_eq!(
        properties
            .get("expected_reasoning_effort")
            .and_then(|schema| schema.description.as_deref()),
        Some(SPAWN_AGENT_REASONING_EFFORT_ASSERTION_DESCRIPTION)
    );
    assert_eq!(
        properties
            .get("service_tier")
            .and_then(|schema| schema.description.as_deref()),
        Some(SPAWN_AGENT_SERVICE_TIER_OVERRIDE_DESCRIPTION)
    );
    assert_eq!(
        parameters.required.as_ref(),
        Some(&vec!["task_name".to_string(), "message".to_string()])
    );
    let output_schema = output_schema.expect("spawn_agent output schema");
    assert_eq!(
        output_schema["required"],
        json!([
            "task_name",
            "effective_model",
            "effective_reasoning_effort",
            "agent_id"
        ])
    );
    assert_eq!(
        output_schema["properties"]
            .as_object()
            .expect("spawn result properties")
            .keys()
            .cloned()
            .collect::<std::collections::BTreeSet<_>>(),
        [
            "agent_id",
            "effective_model",
            "effective_reasoning_effort",
            "nickname",
            "requested_model",
            "requested_model_honored",
            "requested_reasoning_effort",
            "task_name",
        ]
        .into_iter()
        .map(str::to_string)
        .collect()
    );
    assert_eq!(
        output_schema["properties"]["effective_reasoning_effort"]["type"],
        json!(["string", "null"])
    );
}

#[test]
fn spawn_agent_tool_v1_keeps_legacy_fork_context_field() {
    let tool = create_spawn_agent_tool_v1(SpawnAgentToolOptions {
        available_models: Vec::new(),
        agent_type_description: "role help".to_string(),
        expose_agent_type: true,
        hide_agent_type_model_reasoning: false,
        expose_spawn_agent_model_overrides: true,
        multi_agent_version: MultiAgentVersion::V1,
        usage_hint_text: None,
    });

    let ToolSpec::Namespace(namespace) = tool else {
        panic!("spawn_agent v1 should be a namespace tool");
    };
    assert_eq!(namespace.name, MULTI_AGENT_V1_NAMESPACE);
    let Some(ResponsesApiNamespaceTool::Function(ResponsesApiTool { parameters, .. })) =
        namespace.tools.first()
    else {
        panic!("spawn_agent should be a namespace function tool");
    };
    assert_eq!(
        parameters.schema_type.clone(),
        Some(JsonSchemaType::Single(JsonSchemaPrimitiveType::Object))
    );
    let properties = parameters
        .properties
        .as_ref()
        .expect("spawn_agent should use object params");

    assert!(properties.contains_key("fork_context"));
    assert!(!properties.contains_key("fork_turns"));
    assert_eq!(
        properties.get("agent_type"),
        Some(&JsonSchema::string(Some(format!(
            "{SPAWN_AGENT_TYPE_OVERRIDE_DESCRIPTION_V1}\nrole help"
        ))))
    );
    assert_eq!(
        properties
            .get("message")
            .and_then(|schema| schema.encrypted),
        None
    );
    assert_eq!(
        properties
            .get("model")
            .and_then(|schema| schema.description.as_deref()),
        Some(SPAWN_AGENT_MODEL_OVERRIDE_DESCRIPTION)
    );
    assert_eq!(
        properties
            .get("service_tier")
            .and_then(|schema| schema.description.as_deref()),
        Some(SPAWN_AGENT_SERVICE_TIER_OVERRIDE_DESCRIPTION)
    );
}

#[test]
fn spawn_agent_tool_caps_visible_model_summaries() {
    let tool = create_spawn_agent_tool_v2(SpawnAgentToolOptions {
        available_models: vec![
            model_preset("first", /*show_in_picker*/ true),
            model_preset("second", /*show_in_picker*/ true),
            model_preset("third", /*show_in_picker*/ true),
            model_preset("fourth", /*show_in_picker*/ true),
            model_preset("fifth", /*show_in_picker*/ true),
            model_preset("sixth", /*show_in_picker*/ true),
        ],
        agent_type_description: "role help".to_string(),
        expose_agent_type: true,
        hide_agent_type_model_reasoning: false,
        expose_spawn_agent_model_overrides: true,
        multi_agent_version: MultiAgentVersion::V2,
        usage_hint_text: None,
    });

    let ToolSpec::Function(ResponsesApiTool { description, .. }) = tool else {
        panic!("spawn_agent should be a function tool");
    };

    for model in ["first", "second", "third", "fourth", "fifth"] {
        assert!(
            description.contains(&format!("`{model}-model`")),
            "expected {model} model summary in spawn_agent description: {description:?}"
        );
    }
    assert!(!description.contains("`sixth-model`"));
}

#[test]
fn spawn_agent_tool_caps_reasoning_effort_value_length() {
    let mut model = model_preset("visible", /*show_in_picker*/ true);
    let custom_effort = ReasoningEffort::Custom(
        "é".repeat(MAX_REASONING_EFFORT_CHARS_IN_SPAWN_AGENT_DESCRIPTION + 1),
    );
    model.default_reasoning_effort = custom_effort.clone();
    model.supported_reasoning_efforts = vec![ReasoningEffortPreset {
        effort: custom_effort,
        description: "Model-defined".to_string(),
    }];

    assert_eq!(
        spawn_agent_models_description(&[model], MultiAgentVersion::V2),
        format!(
            "Available model overrides (optional; inherited parent model is preferred):\n- `visible-model`: visible description Reasoning efforts: {} (default). Service tiers: priority.",
            "é".repeat(MAX_REASONING_EFFORT_CHARS_IN_SPAWN_AGENT_DESCRIPTION)
        )
    );
}

#[test]
fn spawn_agent_tool_keeps_model_controls_when_spawn_metadata_is_hidden() {
    let tool = create_spawn_agent_tool_v2(SpawnAgentToolOptions {
        available_models: vec![model_preset("visible", /*show_in_picker*/ true)],
        agent_type_description: "role help".to_string(),
        expose_agent_type: false,
        hide_agent_type_model_reasoning: true,
        expose_spawn_agent_model_overrides: true,
        multi_agent_version: MultiAgentVersion::V2,
        usage_hint_text: None,
    });

    let ToolSpec::Function(ResponsesApiTool {
        description,
        parameters,
        ..
    }) = tool
    else {
        panic!("spawn_agent should be a function tool");
    };
    let properties = parameters
        .properties
        .as_ref()
        .expect("spawn_agent should use object params");

    assert!(!properties.contains_key("agent_type"));
    assert!(properties.contains_key("model"));
    assert!(properties.contains_key("reasoning_effort"));
    assert!(!properties.contains_key("service_tier"));
    assert!(!description.contains(SPAWN_AGENT_INHERITED_MODEL_GUIDANCE));
    assert!(description.contains("Available model overrides"));
}

#[test]
fn spawn_agent_tool_hides_model_controls_without_override_exposure() {
    let tool = create_spawn_agent_tool_v2(SpawnAgentToolOptions {
        available_models: vec![model_preset("visible", /*show_in_picker*/ true)],
        agent_type_description: "role help".to_string(),
        expose_agent_type: false,
        hide_agent_type_model_reasoning: true,
        expose_spawn_agent_model_overrides: false,
        multi_agent_version: MultiAgentVersion::V2,
        usage_hint_text: None,
    });

    let ToolSpec::Function(ResponsesApiTool {
        description,
        parameters,
        output_schema,
        ..
    }) = tool
    else {
        panic!("spawn_agent should be a function tool");
    };
    let properties = parameters
        .properties
        .as_ref()
        .expect("spawn_agent should use object params");

    for property in ["agent_type", "model", "reasoning_effort", "service_tier"] {
        assert!(!properties.contains_key(property));
    }
    assert!(properties.contains_key("expected_model"));
    assert!(properties.contains_key("expected_reasoning_effort"));
    assert!(!description.contains(SPAWN_AGENT_INHERITED_MODEL_GUIDANCE));
    assert!(!description.contains("Available model overrides"));
    let output_schema = output_schema.expect("spawn_agent output schema");
    assert_eq!(
        output_schema["required"],
        json!(["task_name", "effective_model", "effective_reasoning_effort"])
    );
    let output_properties = output_schema["properties"]
        .as_object()
        .expect("spawn result properties");
    assert!(!output_properties.contains_key("agent_id"));
    assert!(!output_properties.contains_key("nickname"));
}

#[test]
fn send_message_tool_requires_encrypted_scalar_message_and_interrupt_schema() {
    let ToolSpec::Function(ResponsesApiTool {
        parameters,
        output_schema,
        ..
    }) = create_send_message_tool()
    else {
        panic!("send_message should be a function tool");
    };
    assert_eq!(
        parameters.schema_type,
        Some(JsonSchemaType::Single(JsonSchemaPrimitiveType::Object))
    );
    let properties = parameters
        .properties
        .as_ref()
        .expect("send_message should use object params");
    assert!(properties.contains_key("target"));
    assert!(properties.contains_key("message"));
    assert_eq!(
        properties
            .get("message")
            .and_then(|schema| schema.encrypted),
        Some(true)
    );
    assert!(properties.contains_key("interrupt"));
    assert!(!properties.contains_key("items"));
    assert_eq!(
        properties
            .get("target")
            .and_then(|schema| schema.description.as_deref()),
        Some("Relative or canonical task name to message (from spawn_agent).")
    );
    assert_eq!(
        parameters.required.as_ref(),
        Some(&vec!["target".to_string(), "message".to_string()])
    );
    assert!(output_schema.is_some());
}

#[test]
fn followup_task_tool_requires_message_and_describes_model_receipt() {
    let ToolSpec::Function(ResponsesApiTool {
        name,
        description,
        parameters,
        output_schema,
        ..
    }) = create_followup_task_tool()
    else {
        panic!("followup_task should be a function tool");
    };
    assert_eq!(name, "followup_task");
    assert_eq!(
        description,
        "Send a follow-up task to an existing non-root target agent and trigger a turn if it is idle. If the target is already running, deliver the task promptly at message boundaries while sampling, or after the pending tool call completes."
    );
    assert_eq!(
        parameters.schema_type,
        Some(JsonSchemaType::Single(JsonSchemaPrimitiveType::Object))
    );
    let properties = parameters
        .properties
        .as_ref()
        .expect("followup_task should use object params");
    assert!(properties.contains_key("target"));
    assert!(properties.contains_key("message"));
    assert!(properties.contains_key("expected_model"));
    assert_eq!(
        properties
            .get("message")
            .and_then(|schema| schema.encrypted),
        Some(true)
    );
    assert!(!properties.contains_key("items"));
    assert_eq!(
        parameters.required.as_ref(),
        Some(&vec!["target".to_string(), "message".to_string()])
    );
    let output_schema = output_schema.expect("followup_task should describe its receipt");
    assert_eq!(
        output_schema,
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
    );
}

#[test]
fn wait_agent_tool_v2_uses_timeout_only_summary_output() {
    let ToolSpec::Function(ResponsesApiTool {
        description,
        parameters,
        output_schema,
        ..
    }) = create_wait_agent_tool_v2(WaitAgentTimeoutOptions {
        default_timeout_ms: 30_000,
        min_timeout_ms: 10_000,
        max_timeout_ms: 7_200_000,
    })
    else {
        panic!("wait_agent should be a function tool");
    };
    assert_eq!(
        parameters.schema_type,
        Some(JsonSchemaType::Single(JsonSchemaPrimitiveType::Object))
    );
    let properties = parameters
        .properties
        .as_ref()
        .expect("wait_agent should use object params");
    assert!(properties.contains_key("targets"));
    assert!(properties.contains_key("timeout_ms"));
    assert!(properties.contains_key("return_when"));
    assert!(properties.contains_key("native_event_wait"));
    let completion_reason = output_schema
        .as_ref()
        .expect("wait output schema")["properties"]["completion_reason"]["enum"]
        .as_array()
        .expect("completion reason enum");
    assert!(completion_reason.contains(&json!("subscription_loss")));
    assert!(description.contains("When `return_when` is `all`"));
    assert_eq!(
        properties
            .get("timeout_ms")
            .and_then(|schema| schema.description.as_deref()),
        Some(
            "Optional timeout in milliseconds. Defaults to 30000, min 10000, max 7200000. Prefer longer waits to avoid busy polling."
        )
    );
    assert_eq!(parameters.required.as_ref(), None);
    assert_eq!(
        output_schema.expect("wait output schema")["properties"]["message"]["description"],
        json!("Brief wait summary without the agent's final content.")
    );
}

#[test]
fn wait_agent_tool_v2_omits_runtime_fields_without_capability_provider() {
    let ToolSpec::Function(ResponsesApiTool {
        description,
        parameters,
        output_schema,
        ..
    }) = create_wait_agent_tool_v2_with_capabilities(
        WaitAgentTimeoutOptions::default(),
        ToolRuntimeCapabilities::upstream_default(),
    )
    else {
        panic!("wait_agent should be a function tool");
    };
    let properties = parameters
        .properties
        .as_ref()
        .expect("wait_agent should use object params");
    assert!(!properties.contains_key("return_when"));
    assert!(!properties.contains_key("native_event_wait"));
    assert!(!description.contains("return_when"));

    let output_schema = output_schema.expect("wait output schema");
    assert!(
        !output_schema["properties"]
            .as_object()
            .expect("properties should be object")
            .contains_key("pending_ids")
    );
    assert!(
        !output_schema["properties"]
            .as_object()
            .expect("properties should be object")
            .contains_key("completion_reason")
    );
    assert_eq!(
        output_schema["required"],
        json!(["message", "requested_ids", "timed_out"])
    );
}

#[test]
fn list_agents_tool_includes_path_prefix_and_agent_fields() {
    let ToolSpec::Function(ResponsesApiTool {
        parameters,
        output_schema,
        ..
    }) = create_list_agents_tool()
    else {
        panic!("list_agents should be a function tool");
    };
    assert_eq!(
        parameters.schema_type,
        Some(JsonSchemaType::Single(JsonSchemaPrimitiveType::Object))
    );
    let properties = parameters
        .properties
        .as_ref()
        .expect("list_agents should use object params");
    assert!(properties.contains_key("path_prefix"));
    assert_eq!(
        properties
            .get("path_prefix")
            .and_then(|schema| schema.description.as_deref()),
        Some("Task-path prefix filter without a trailing slash. Omit to list all live agents.")
    );
    assert_eq!(
        output_schema.expect("list_agents output schema")["properties"]["agents"]["items"]["required"],
        json!([
            "agent_name",
            "agent_status",
            "has_active_subagents",
            "active_subagent_count"
        ])
    );
}

#[test]
fn list_agents_tool_status_schema_includes_interrupted() {
    let ToolSpec::Function(ResponsesApiTool { output_schema, .. }) = create_list_agents_tool()
    else {
        panic!("list_agents should be a function tool");
    };

    assert_eq!(
        output_schema.expect("list_agents output schema")["properties"]["agents"]["items"]["properties"]
            ["agent_status"]["allOf"][0]["oneOf"][0]["enum"],
        json!([
            "pending_init",
            "running",
            "interrupted",
            "shutdown",
            "not_found"
        ])
    );
}

#[test]
fn list_agents_tool_omits_active_descendants_without_capability_provider() {
    let ToolSpec::Function(ResponsesApiTool { output_schema, .. }) =
        create_list_agents_tool_with_capabilities(ToolRuntimeCapabilities::upstream_default())
    else {
        panic!("list_agents should be a function tool");
    };

    let output_schema = output_schema.expect("list_agents output schema");
    let agent_properties = output_schema["properties"]["agents"]["items"]["properties"]
        .as_object()
        .expect("agent item properties should be object");
    assert!(!agent_properties.contains_key("has_active_subagents"));
    assert!(!agent_properties.contains_key("active_subagent_count"));
    assert_eq!(
        output_schema["properties"]["agents"]["items"]["required"],
        json!(["agent_name", "agent_status"])
    );
}

#[test]
fn inspect_agent_tree_tool_exposes_scope_and_compact_tree_fields() {
    let ToolSpec::Function(ResponsesApiTool {
        parameters,
        output_schema,
        ..
    }) = create_inspect_agent_tree_tool()
    else {
        panic!("inspect_agent_tree should be a function tool");
    };

    let properties = parameters
        .properties
        .as_ref()
        .expect("inspect_agent_tree should use object params");
    assert!(properties.contains_key("scope"));
    assert!(properties.contains_key("agent_roots"));
    let output_schema = output_schema.expect("inspect_agent_tree output schema");
    assert_eq!(
        output_schema["properties"]["agents"]["items"]["required"],
        json!([
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
        ])
    );
}

#[test]
fn send_message_tool_declares_non_acknowledgement_handoff_receipt() {
    let ToolSpec::Function(ResponsesApiTool { output_schema, .. }) = create_send_message_tool()
    else {
        panic!("send_message should be a function tool");
    };

    let output_schema = output_schema.expect("send_message output schema");
    assert_eq!(
        output_schema["required"],
        json!([
            "task_name",
            "recipient_task_name",
            "effective_identity_scope",
            "handoff_state",
            "effective_model",
            "effective_model_provider_id",
            "effective_reasoning_effort",
            "effective_service_tier"
        ])
    );
    assert_eq!(
        output_schema["properties"]["handoff_state"]["enum"],
        json!(["queued"])
    );
    assert_eq!(
        output_schema["properties"]["effective_model"]["type"],
        json!(["string", "null"])
    );
    assert_eq!(
        output_schema["properties"]["effective_model_provider_id"]["type"],
        json!(["string", "null"])
    );
    assert_eq!(
        output_schema["properties"]["effective_identity_scope"]["enum"],
        json!(["recipient"])
    );
}
