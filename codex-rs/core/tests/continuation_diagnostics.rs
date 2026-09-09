use anyhow::Result;
use codex_features::Feature;
use codex_model_provider_info::ModelProviderInfo;
use codex_model_provider_info::WireApi;
use codex_protocol::protocol::EventMsg;
use codex_protocol::protocol::Op;
use codex_protocol::user_input::UserInput;
use core_test_support::responses::ev_assistant_message;
use core_test_support::responses::ev_completed;
use core_test_support::responses::ev_function_call_with_namespace;
use core_test_support::responses::ev_response_created;
use core_test_support::responses::sse;
use core_test_support::test_codex::test_codex;
use core_test_support::wait_for_event;
use serde_json::json;
use wiremock::Mock;
use wiremock::MockServer;
use wiremock::ResponseTemplate;
use wiremock::matchers::body_string_contains;
use wiremock::matchers::method;
use wiremock::matchers::path;

const FIRST_PROMPT: &str = "diagnostic first turn";
const SECOND_PROMPT: &str = "diagnostic continuation turn";
const CHILD_TASK: &str = "diagnostic worker task";
const MULTI_AGENT_V2_NAMESPACE: &str = "agents";

fn has_function_call_output(request: &wiremock::Request, call_id: &str) -> bool {
    serde_json::from_slice::<serde_json::Value>(&request.body).is_ok_and(|body| {
        body.get("input")
            .and_then(serde_json::Value::as_array)
            .is_some_and(|items| {
                items.iter().any(|item| {
                    item.get("type").and_then(serde_json::Value::as_str)
                        == Some("function_call_output")
                        && item.get("call_id").and_then(serde_json::Value::as_str) == Some(call_id)
                })
            })
    })
}

async fn submit_user_turn(codex: &codex_core::CodexThread, prompt: &str) -> Result<()> {
    codex
        .submit(Op::UserInput {
            items: vec![UserInput::Text {
                text: prompt.to_string(),
                text_elements: Vec::new(),
            }],
            final_output_json_schema: None,
            responsesapi_client_metadata: None,
            additional_context: Default::default(),
            thread_settings: Default::default(),
        })
        .await?;
    Ok(())
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn synthetic_limit_error_does_not_prevent_later_v2_spawn_attempt() -> Result<()> {
    let server = MockServer::start().await;

    Mock::given(method("POST"))
        .and(path("/v1/responses"))
        .and(body_string_contains(FIRST_PROMPT))
        .respond_with(
            ResponseTemplate::new(429)
                .insert_header("content-type", "application/json")
                .set_body_json(json!({
                    "error": {
                        "type": "usage_limit_reached",
                        "message": "synthetic limit",
                        "plan_type": null,
                        "resets_at": null
                    }
                })),
        )
        .expect(1)
        .mount(&server)
        .await;

    let spawn_args = serde_json::to_string(&json!({
        "message": CHILD_TASK,
        "task_name": "diagnostic-worker",
    }))?;
    Mock::given(method("POST"))
        .and(path("/v1/responses"))
        .and(body_string_contains(SECOND_PROMPT))
        .respond_with(
            ResponseTemplate::new(200)
                .insert_header("content-type", "text/event-stream")
                .set_body_raw(
                    sse(vec![
                        ev_response_created("spawn-response"),
                        ev_function_call_with_namespace(
                            "spawn-call",
                            MULTI_AGENT_V2_NAMESPACE,
                            "spawn_agent",
                            &spawn_args,
                        ),
                        ev_completed("spawn-response"),
                    ]),
                    "text/event-stream",
                ),
        )
        .expect(1)
        .mount(&server)
        .await;

    Mock::given(method("POST"))
        .and(path("/v1/responses"))
        .and(body_string_contains(CHILD_TASK))
        .respond_with(
            ResponseTemplate::new(200)
                .insert_header("content-type", "text/event-stream")
                .set_body_raw(
                    sse(vec![
                        ev_response_created("child-response"),
                        ev_assistant_message("child-message", "diagnostic child completed"),
                        ev_completed("child-response"),
                    ]),
                    "text/event-stream",
                ),
        )
        .expect(1)
        .mount(&server)
        .await;

    Mock::given(method("POST"))
        .and(path("/v1/responses"))
        .respond_with(move |request: &wiremock::Request| {
            if has_function_call_output(request, "spawn-call") {
                ResponseTemplate::new(200)
                    .insert_header("content-type", "text/event-stream")
                    .set_body_raw(
                        sse(vec![
                            ev_response_created("spawn-followup-response"),
                            ev_assistant_message("spawn-followup-message", "spawn observed"),
                            ev_completed("spawn-followup-response"),
                        ]),
                        "text/event-stream",
                    )
            } else {
                ResponseTemplate::new(500)
            }
        })
        .mount(&server)
        .await;

    let provider = ModelProviderInfo {
        name: "diagnostic-provider".into(),
        base_url: Some(format!("{}/v1", server.uri())),
        env_key: Some("PATH".into()),
        env_key_instructions: None,
        experimental_bearer_token: None,
        auth: None,
        aws: None,
        wire_api: WireApi::Responses,
        query_params: None,
        http_headers: None,
        env_http_headers: None,
        request_max_retries: Some(0),
        stream_max_retries: Some(0),
        stream_idle_timeout_ms: Some(2_000),
        websocket_connect_timeout_ms: None,
        requires_openai_auth: false,
        supports_websockets: false,
        supports_standalone_web_search: false,
    };

    let test = test_codex()
        .with_model("koffing")
        .with_config(move |config| {
            config.model_provider = provider;
            config
                .features
                .enable(Feature::Collab)
                .expect("test config should allow feature update");
            config
                .features
                .enable(Feature::MultiAgentV2)
                .expect("test config should allow feature update");
        })
        .build(&server)
        .await?;

    submit_user_turn(&test.codex, FIRST_PROMPT).await?;
    wait_for_event(&test.codex, |event| matches!(event, EventMsg::Error(_))).await;
    wait_for_event(&test.codex, |event| {
        matches!(event, EventMsg::TurnComplete(_))
    })
    .await;

    submit_user_turn(&test.codex, SECOND_PROMPT).await?;
    wait_for_event(&test.codex, |event| {
        matches!(event, EventMsg::TurnComplete(_))
    })
    .await;

    server.verify().await;
    Ok(())
}
