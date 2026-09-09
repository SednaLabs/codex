use std::sync::Arc;
use std::sync::Weak;

use codex_analytics::AnalyticsEventsClient;
use codex_extension_api::ExtensionData;
use codex_extension_api::ExtensionRegistryBuilder;
use codex_extension_api::ThreadStartInput;
use codex_extension_api::TurnStartInput;
use codex_goal_extension::GoalObjectiveUpdate;
use codex_goal_extension::GoalService;
use codex_goal_extension::GoalSetRequest;
use codex_goal_extension::GoalTokenBudgetUpdate;
use codex_goal_extension::install_with_backend;
use codex_protocol::ThreadId;
use codex_protocol::config_types::CollaborationMode;
use codex_protocol::config_types::ModeKind;
use codex_protocol::config_types::Settings;
use codex_protocol::protocol::CodexErrorInfo;
use codex_protocol::protocol::SessionSource;
use codex_protocol::protocol::ThreadGoalStatus;
use codex_protocol::protocol::TokenUsage;
use codex_utils_absolute_path::test_support::PathExt;
use tempfile::TempDir;

#[tokio::test]
async fn record_only_error_observation_keeps_goal_active() -> anyhow::Result<()> {
    let tempdir = TempDir::new()?;
    let runtime = codex_state::StateRuntime::init(
        codex_state::SqliteConfig::new_for_testing(tempdir.keep().as_path().abs()),
        "diagnostic-provider".to_string(),
    )
    .await?;
    let thread_id = ThreadId::from_string("11111111-1111-4111-8111-111111111111")
        .map_err(anyhow::Error::msg)?;
    let metadata = codex_state::ThreadMetadataBuilder::new(
        thread_id,
        runtime
            .sqlite()
            .home()
            .join(format!("rollout-{thread_id}.jsonl")),
        chrono::Utc::now(),
        SessionSource::Cli,
    )
    .build("diagnostic-provider");
    runtime.upsert_thread(&metadata).await?;

    let goal_service = Arc::new(GoalService::new());
    let mut builder = ExtensionRegistryBuilder::<()>::new();
    install_with_backend(
        &mut builder,
        Arc::clone(&runtime),
        AnalyticsEventsClient::disabled(),
        None,
        Weak::new(),
        Arc::clone(&goal_service),
        |_| true,
    );
    let registry = builder.build();
    let session_store = ExtensionData::new("session-1");
    let thread_store = ExtensionData::new(thread_id.to_string());
    let session_source = SessionSource::Cli;
    for contributor in registry.thread_lifecycle_contributors() {
        contributor
            .on_thread_start(ThreadStartInput {
                config: &(),
                session_source: &session_source,
                persistent_thread_state_available: true,
                environments: &[],
                mcp_resource_client: None,
                session_store: &session_store,
                thread_store: &thread_store,
            })
            .await;
    }

    let outcome = goal_service
        .set_thread_goal(
            runtime.as_ref(),
            GoalSetRequest {
                thread_id,
                objective: GoalObjectiveUpdate::Set("diagnostic continuation"),
                status: Some(ThreadGoalStatus::Active),
                token_budget: GoalTokenBudgetUpdate::Keep,
            },
        )
        .await?;
    outcome.apply_runtime_effects(&goal_service).await;

    let turn_store = ExtensionData::new("turn-1");
    let collaboration_mode = CollaborationMode {
        mode: ModeKind::Default,
        settings: Settings {
            model: "gpt-test".to_string(),
            reasoning_effort: None,
            developer_instructions: None,
        },
    };
    for contributor in registry.turn_lifecycle_contributors() {
        contributor
            .on_turn_start(TurnStartInput {
                turn_id: "turn-1",
                collaboration_mode: &collaboration_mode,
                token_usage_at_turn_start: &TokenUsage::default(),
                session_store: &session_store,
                thread_store: &thread_store,
                turn_store: &turn_store,
            })
            .await;
    }

    let observed_error = CodexErrorInfo::UsageLimitExceeded;
    assert!(matches!(observed_error, CodexErrorInfo::UsageLimitExceeded));

    let goal = goal_service
        .get_thread_goal(runtime.as_ref(), thread_id)
        .await?
        .ok_or_else(|| anyhow::anyhow!("goal should exist"))?;
    assert_eq!(ThreadGoalStatus::Active, goal.status);

    Ok(())
}
