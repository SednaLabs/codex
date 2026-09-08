use super::*;
use app_test_support::create_fake_parented_rollout_with_source;
use app_test_support::create_fake_rollout;
use app_test_support::rollout_path;
use codex_app_server_protocol::ClientNotification;
use codex_app_server_protocol::ClientRequest;
use codex_app_server_protocol::JSONRPCError;
use codex_app_server_protocol::JSONRPCErrorError;
use codex_app_server_protocol::JSONRPCMessage;
use codex_app_server_protocol::JSONRPCResponse;
use codex_app_server_protocol::SessionSource;
use codex_app_server_protocol::Thread;
use codex_app_server_protocol::ThreadStatus;
use codex_protocol::AgentPath;
use codex_state::SqliteConfig;
use futures::SinkExt;
use futures::StreamExt;
use pretty_assertions::assert_eq;
use std::collections::HashSet;
use std::collections::VecDeque;
use std::sync::Mutex;
use tokio::net::TcpListener;
use tokio::task::JoinHandle;
use tokio_tungstenite::accept_async;
use tokio_tungstenite::tungstenite::Message;

#[derive(Clone, Debug)]
struct RecordedRequest {
    method: String,
    params: Option<serde_json::Value>,
}

type RecordedRequests = Arc<Mutex<Vec<RecordedRequest>>>;

#[derive(Clone)]
enum ScriptedLineageResponse {
    Page(serde_json::Value),
    Error(String),
}

#[derive(Clone)]
enum ScriptedThreadReadResponse {
    Thread(serde_json::Value),
    Error(String),
}

fn take_recorded_requests(requests: &RecordedRequests) -> Vec<RecordedRequest> {
    std::mem::take(&mut *requests.lock().expect("request recorder lock"))
}

/// Returns and resets `(thread/list, thread/loaded/list, thread/read)` request counts.
fn take_backfill_counts(requests: &RecordedRequests) -> (usize, usize, usize) {
    let requests = take_recorded_requests(requests);
    (
        requests
            .iter()
            .filter(|request| request.method == "thread/list")
            .count(),
        requests
            .iter()
            .filter(|request| request.method == "thread/loaded/list")
            .count(),
        requests
            .iter()
            .filter(|request| request.method == "thread/read")
            .count(),
    )
}

fn take_backfill_counts_and_lineage_cursors(
    requests: &RecordedRequests,
) -> ((usize, usize, usize), Vec<Option<String>>) {
    let requests = take_recorded_requests(requests);
    let counts = (
        requests
            .iter()
            .filter(|request| request.method == "thread/list")
            .count(),
        requests
            .iter()
            .filter(|request| request.method == "thread/loaded/list")
            .count(),
        requests
            .iter()
            .filter(|request| request.method == "thread/read")
            .count(),
    );
    let cursors = requests
        .iter()
        .filter(|request| request.method == "thread/list")
        .map(|request| {
            request
                .params
                .as_ref()
                .and_then(|params| params.get("cursor"))
                .and_then(serde_json::Value::as_str)
                .map(str::to_string)
        })
        .collect();
    (counts, cursors)
}

/// Starts an embedded app server behind a loopback WebSocket proxy that records JSON-RPC methods.
async fn start_recording_app_server(
    config: &Config,
    blocked_thread_read_id: Option<ThreadId>,
) -> Result<(AppServerSession, RecordedRequests, JoinHandle<Result<()>>)> {
    start_recording_app_server_with_lineage(
        config,
        blocked_thread_read_id,
        /*lineage_responses*/ None,
        /*thread_read_responses*/ None,
    )
    .await
}

async fn start_recording_app_server_with_lineage(
    config: &Config,
    blocked_thread_read_id: Option<ThreadId>,
    lineage_responses: Option<Arc<Mutex<VecDeque<ScriptedLineageResponse>>>>,
    thread_read_responses: Option<Arc<Mutex<VecDeque<ScriptedThreadReadResponse>>>>,
) -> Result<(AppServerSession, RecordedRequests, JoinHandle<Result<()>>)> {
    start_recording_app_server_with_lineage_and_state(
        config,
        blocked_thread_read_id,
        lineage_responses,
        thread_read_responses,
        /*loaded_list_responses*/ None,
        /*resume_error_thread_id*/ None,
        /*with_state_db*/ true,
    )
    .await
}

async fn start_recording_app_server_with_lineage_and_state(
    config: &Config,
    blocked_thread_read_id: Option<ThreadId>,
    lineage_responses: Option<Arc<Mutex<VecDeque<ScriptedLineageResponse>>>>,
    thread_read_responses: Option<Arc<Mutex<VecDeque<ScriptedThreadReadResponse>>>>,
    loaded_list_responses: Option<Arc<Mutex<VecDeque<serde_json::Value>>>>,
    resume_error_thread_id: Option<ThreadId>,
    with_state_db: bool,
) -> Result<(AppServerSession, RecordedRequests, JoinHandle<Result<()>>)> {
    let state_db = if with_state_db {
        crate::init_state_db_for_app_server_target(config, &crate::AppServerTarget::Embedded)
            .await?
    } else {
        None
    };
    let embedded = crate::start_embedded_app_server(
        codex_arg0::Arg0DispatchPaths::default(),
        config.clone(),
        Vec::new(),
        codex_config::LoaderOverrides::default(),
        /*strict_config*/ false,
        codex_config::CloudConfigBundleLoader::default(),
        codex_feedback::CodexFeedback::new(),
        /*log_db*/ None,
        state_db,
        Arc::new(codex_exec_server::EnvironmentManager::default_for_tests()),
    )
    .await?;
    let codex_home = config.codex_home.display().to_string();
    let requests = Arc::new(Mutex::new(Vec::new()));
    let request_sink = Arc::clone(&requests);
    let listener = TcpListener::bind("127.0.0.1:0").await?;
    let websocket_url = format!("ws://{}", listener.local_addr()?);
    let blocked_thread_read_id = blocked_thread_read_id.map(|thread_id| thread_id.to_string());
    let resume_error_thread_id = resume_error_thread_id.map(|thread_id| thread_id.to_string());
    let proxy = tokio::spawn(async move {
        let (stream, _) = listener.accept().await?;
        let mut websocket = accept_async(stream).await?;
        while let Some(frame) = websocket.next().await {
            let Message::Text(text) = frame? else {
                continue;
            };
            let message = serde_json::from_str::<JSONRPCMessage>(&text)?;
            match message {
                JSONRPCMessage::Request(request) if request.method == "initialize" => {
                    websocket
                        .send(Message::Text(
                            serde_json::to_string(&JSONRPCMessage::Response(JSONRPCResponse {
                                id: request.id,
                                result: serde_json::json!({
                                    "userAgent": "codex-tui-test",
                                    "codexHome": codex_home,
                                }),
                            }))?
                            .into(),
                        ))
                        .await?;
                }
                JSONRPCMessage::Request(request) => {
                    request_sink
                        .lock()
                        .expect("request recorder lock")
                        .push(RecordedRequest {
                            method: request.method.clone(),
                            params: request.params.clone(),
                        });
                    if request.method == "thread/list"
                        && let Some(lineage_responses) = &lineage_responses
                    {
                        let response = lineage_responses
                            .lock()
                            .expect("lineage response lock")
                            .pop_front()
                            .expect("scripted thread/list response");
                        let message = match response {
                            ScriptedLineageResponse::Page(result) => {
                                JSONRPCMessage::Response(JSONRPCResponse {
                                    id: request.id.clone(),
                                    result,
                                })
                            }
                            ScriptedLineageResponse::Error(message) => {
                                JSONRPCMessage::Error(JSONRPCError {
                                    id: request.id.clone(),
                                    error: JSONRPCErrorError {
                                        code: -32603,
                                        message,
                                        data: None,
                                    },
                                })
                            }
                        };
                        websocket
                            .send(Message::Text(serde_json::to_string(&message)?.into()))
                            .await?;
                        continue;
                    }
                    if request.method == "thread/read"
                        && let Some(thread_read_responses) = &thread_read_responses
                    {
                        let response = thread_read_responses
                            .lock()
                            .expect("thread/read response lock")
                            .pop_front()
                            .expect("scripted thread/read response");
                        let message = match response {
                            ScriptedThreadReadResponse::Thread(thread) => {
                                JSONRPCMessage::Response(JSONRPCResponse {
                                    id: request.id.clone(),
                                    result: serde_json::json!({ "thread": thread }),
                                })
                            }
                            ScriptedThreadReadResponse::Error(message) => {
                                JSONRPCMessage::Error(JSONRPCError {
                                    id: request.id.clone(),
                                    error: JSONRPCErrorError {
                                        code: -32603,
                                        message,
                                        data: None,
                                    },
                                })
                            }
                        };
                        websocket
                            .send(Message::Text(serde_json::to_string(&message)?.into()))
                            .await?;
                        continue;
                    }
                    if request.method == "thread/resume"
                        && resume_error_thread_id
                            .as_deref()
                            .is_some_and(|resume_error_thread_id| {
                                request
                                    .params
                                    .as_ref()
                                    .and_then(|params| params.get("threadId"))
                                    .and_then(serde_json::Value::as_str)
                                    == Some(resume_error_thread_id)
                            })
                    {
                        let message = JSONRPCMessage::Error(JSONRPCError {
                            id: request.id.clone(),
                            error: JSONRPCErrorError {
                                code: -32603,
                                message: "scripted resume failure".to_string(),
                                data: None,
                            },
                        });
                        websocket
                            .send(Message::Text(serde_json::to_string(&message)?.into()))
                            .await?;
                        continue;
                    }
                    if request.method == "thread/loaded/list"
                        && let Some(loaded_list_responses) = &loaded_list_responses
                    {
                        let result = loaded_list_responses
                            .lock()
                            .expect("loaded-list response lock")
                            .pop_front()
                            .expect("scripted thread/loaded/list response");
                        websocket
                            .send(Message::Text(
                                serde_json::to_string(&JSONRPCMessage::Response(
                                    JSONRPCResponse {
                                        id: request.id.clone(),
                                        result,
                                    },
                                ))?
                                .into(),
                            ))
                            .await?;
                        continue;
                    }
                    if request.method == "thread/read"
                        && blocked_thread_read_id
                            .as_deref()
                            .is_some_and(|blocked_thread_read_id| {
                                request
                                    .params
                                    .as_ref()
                                    .and_then(|params| params.get("threadId"))
                                    .and_then(serde_json::Value::as_str)
                                    == Some(blocked_thread_read_id)
                            })
                    {
                        std::future::pending::<()>().await;
                    }
                    let request_id = request.id.clone();
                    let request =
                        serde_json::from_value::<ClientRequest>(serde_json::to_value(request)?)?;
                    let response = match embedded.request(request).await? {
                        Ok(result) => JSONRPCMessage::Response(JSONRPCResponse {
                            id: request_id,
                            result,
                        }),
                        Err(error) => JSONRPCMessage::Error(JSONRPCError {
                            id: request_id,
                            error,
                        }),
                    };
                    websocket
                        .send(Message::Text(serde_json::to_string(&response)?.into()))
                        .await?;
                }
                JSONRPCMessage::Notification(notification)
                    if notification.method == "initialized" => {}
                JSONRPCMessage::Notification(notification) => {
                    embedded
                        .notify(serde_json::from_value::<ClientNotification>(
                            serde_json::to_value(notification)?,
                        )?)
                        .await?;
                }
                JSONRPCMessage::Response(_) | JSONRPCMessage::Error(_) => {}
            }
        }
        embedded.shutdown().await?;
        Ok(())
    });
    let app_server = crate::connect_remote_app_server(crate::RemoteAppServerEndpoint::WebSocket {
        websocket_url,
        auth_token: None,
    })
    .await?;

    Ok((
        AppServerSession::new(
            app_server,
            crate::app_server_session::ThreadParamsMode::Embedded,
        ),
        requests,
        proxy,
    ))
}

fn scripted_lineage_thread(
    config: &Config,
    thread_id: ThreadId,
    parent_thread_id: ThreadId,
    depth: i32,
) -> Thread {
    Thread {
        id: thread_id.to_string(),
        extra: None,
        session_id: thread_id.to_string(),
        forked_from_id: None,
        parent_thread_id: Some(parent_thread_id.to_string()),
        preview: String::new(),
        ephemeral: false,
        is_pinned: false,
        history_mode: Default::default(),
        model_provider: config.model_provider_id.clone(),
        model: config.model.clone(),
        reasoning_effort: None,
        created_at: i64::from(depth),
        updated_at: i64::from(depth),
        recency_at: Some(i64::from(depth)),
        status: ThreadStatus::Idle,
        path: None,
        cwd: config.cwd.clone(),
        cli_version: "0.0.0".to_string(),
        source: SessionSource::SubAgent(SubAgentSource::ThreadSpawn {
            parent_thread_id,
            depth,
            agent_path: None,
            agent_nickname: Some(format!("child-{depth}")),
            agent_role: Some("worker".to_string()),
        }),
        can_accept_direct_input: Some(true),
        thread_source: None,
        agent_nickname: Some(format!("child-{depth}")),
        agent_role: Some("worker".to_string()),
        git_info: None,
        name: None,
        turns: Vec::new(),
    }
}

fn scripted_lineage_page(threads: Vec<Thread>, next_cursor: Option<String>) -> serde_json::Value {
    serde_json::json!({
        "data": threads,
        "ancestorFilterApplied": true,
        "nextCursor": next_cursor,
    })
}

fn scripted_relation_limited_lineage_page(
    threads: Vec<Thread>,
    next_cursor: Option<String>,
) -> serde_json::Value {
    serde_json::json!({
        "data": threads,
        "ancestorFilterApplied": true,
        "relationLimitReached": true,
        "nextCursor": next_cursor,
    })
}

fn scripted_unacknowledged_lineage_page(
    threads: Vec<Thread>,
    next_cursor: Option<String>,
) -> serde_json::Value {
    serde_json::json!({
        "data": threads,
        "nextCursor": next_cursor,
    })
}

fn configure_backfill_primary(app: &mut App, primary_thread_id: ThreadId) {
    app.primary_thread_id = Some(primary_thread_id);
    app.agent_navigation.upsert(
        primary_thread_id,
        /*agent_nickname*/ None,
        /*agent_role*/ None,
        /*is_closed*/ false,
        /*created_at*/ None,
        /*updated_at*/ None,
    );
}

#[tokio::test]
async fn replay_only_model_persistence_does_not_write_config() -> Result<()> {
    let (mut app, mut app_event_rx, mut op_rx) = make_test_app_with_channels().await;
    let thread_id = ThreadId::new();
    let mut channel = ThreadEventChannel::new(/*capacity*/ 1);
    channel.mark_replay_only();
    app.thread_event_channels.insert(thread_id, channel);
    app.active_thread_id = Some(thread_id);
    app.chat_widget.handle_thread_session(test_thread_session(
        thread_id,
        test_path_buf("/tmp/project"),
    ));

    let (mut app_server, requests, proxy) =
        start_recording_app_server(&app.config, /*blocked_thread_read_id*/ None).await?;
    let mut tui = crate::tui::test_support::make_test_tui()?;

    // Observe only events produced by the actions below, not session setup.
    while app_event_rx.try_recv().is_ok() {}
    while op_rx.try_recv().is_ok() {}

    let live_target = ThreadId::new();
    app.thread_event_channels
        .insert(live_target, ThreadEventChannel::new(/*capacity*/ 1));
    assert!(
        !app.reject_replay_only_mutation(&AppEvent::SetThreadGoalStatus {
            thread_id: live_target,
            status: codex_app_server_protocol::ThreadGoalStatus::Complete,
        })
    );
    assert!(app_event_rx.try_recv().is_err());
    app.active_thread_id = Some(live_target);
    assert!(
        app.reject_replay_only_mutation(&AppEvent::SetThreadGoalStatus {
            thread_id,
            status: codex_app_server_protocol::ThreadGoalStatus::Complete,
        })
    );
    assert!(app_event_rx.try_recv().is_err());
    app.active_thread_id = Some(thread_id);
    assert!(
        app.reject_replay_only_mutation(&AppEvent::SetThreadGoalStatus {
            thread_id,
            status: codex_app_server_protocol::ThreadGoalStatus::Complete,
        })
    );
    assert_matches!(app_event_rx.try_recv(), Ok(AppEvent::InsertHistoryCell(_)));

    app.refresh_plugin_mentions_after_config_write();
    assert_matches!(app_event_rx.try_recv(), Ok(AppEvent::RefreshPluginMentions));
    assert!(op_rx.try_recv().is_err());

    app.thread_event_channels
        .insert(thread_id, ThreadEventChannel::new(/*capacity*/ 1));
    app.refresh_plugin_mentions_after_config_write();
    assert_matches!(app_event_rx.try_recv(), Ok(AppEvent::RefreshPluginMentions));
    assert_matches!(op_rx.try_recv(), Ok(AppCommand::ReloadUserConfig));

    let mut replay_channel = ThreadEventChannel::new(/*capacity*/ 1);
    replay_channel.mark_replay_only();
    app.thread_event_channels.insert(thread_id, replay_channel);

    app.pending_plugin_enabled_writes
        .insert("plugin.test".to_string(), Some(true));
    app.pending_hook_enabled_writes
        .insert("hook.test".to_string(), Some(true));
    app.handle_event(
        &mut tui,
        &mut app_server,
        AppEvent::PluginEnabledSet {
            cwd: test_path_buf("/tmp/project"),
            plugin_id: "plugin.test".to_string(),
            enabled: false,
            result: Err("replay-only test".to_string()),
        },
    )
    .await?;
    app.handle_event(
        &mut tui,
        &mut app_server,
        AppEvent::HookEnabledSet {
            key: "hook.test".to_string(),
            enabled: false,
            result: Err("replay-only test".to_string()),
        },
    )
    .await?;
    assert!(requests.lock().expect("request recorder lock").is_empty());

    app.handle_event(
        &mut tui,
        &mut app_server,
        AppEvent::PersistModelSelection {
            model: "gpt-5.4".to_string(),
            effort: None,
        },
    )
    .await?;

    assert!(
        !requests
            .lock()
            .expect("request recorder lock")
            .iter()
            .any(|request| request.method == "config/batchWrite")
    );

    app.thread_event_channels
        .insert(thread_id, ThreadEventChannel::new(/*capacity*/ 1));
    app.handle_event(
        &mut tui,
        &mut app_server,
        AppEvent::PersistModelSelection {
            model: "gpt-5.4".to_string(),
            effort: None,
        },
    )
    .await?;
    assert!(
        requests
            .lock()
            .expect("request recorder lock")
            .iter()
            .any(|request| request.method == "config/batchWrite")
    );

    app_server.shutdown().await?;
    proxy.await??;
    Ok(())
}

#[tokio::test]
async fn active_replay_only_selection_retries_failed_resume_and_preserves_draft() -> Result<()> {
    let (mut app, mut app_event_rx, _op_rx) = make_test_app_with_channels().await;
    let thread_id = ThreadId::new();
    let mut replay_channel = ThreadEventChannel::new(/*capacity*/ 1);
    replay_channel.mark_replay_only();
    {
        let mut store = replay_channel.store.lock().await;
        store.set_session(
            test_thread_session(thread_id, test_path_buf("/tmp/replay-only")),
            vec![test_turn("stale-turn", TurnStatus::Completed, Vec::new())],
        );
    }
    app.thread_event_channels.insert(thread_id, replay_channel);
    app.agent_navigation.upsert(
        thread_id,
        Some("cached".to_string()),
        Some("worker".to_string()),
        /*is_closed*/ true,
        /*created_at*/ None,
        /*updated_at*/ None,
    );
    app.active_thread_id = Some(thread_id);
    let draft = "keep this draft while retrying".to_string();
    app.chat_widget
        .restore_user_message_to_composer(draft.clone().into());

    let mut fallback_thread =
        scripted_lineage_thread(&app.config, thread_id, ThreadId::new(), /*depth*/ 1);
    fallback_thread.turns = vec![test_turn(
        "authoritative-fallback-turn",
        TurnStatus::Completed,
        Vec::new(),
    )];
    let thread_read_responses = Arc::new(Mutex::new(VecDeque::from([
        ScriptedThreadReadResponse::Thread(serde_json::to_value(fallback_thread)?),
    ])));
    let (mut app_server, requests, proxy) = start_recording_app_server_with_lineage_and_state(
        &app.config,
        /*blocked_thread_read_id*/ None,
        /*lineage_responses*/ None,
        Some(thread_read_responses),
        /*loaded_list_responses*/ None,
        /*resume_error_thread_id*/ Some(thread_id),
        /*with_state_db*/ true,
    )
    .await?;
    let mut tui = crate::tui::test_support::make_test_tui()?;
    while app_event_rx.try_recv().is_ok() {}

    app.select_agent_thread(&mut tui, &mut app_server, thread_id)
        .await?;

    assert_eq!(app.active_thread_id, Some(thread_id));
    assert_eq!(
        app.thread_event_channels[&thread_id].attachment(),
        ThreadEventAttachment::ReplayOnly
    );
    assert_eq!(app.chat_widget.composer_text_with_pending(), draft);
    while app_event_rx.try_recv().is_ok() {}
    app.chat_widget
        .handle_key_event(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
    assert_eq!(
        app.chat_widget.composer_text_with_pending(),
        "keep this draft while retrying"
    );
    assert!(
        !std::iter::from_fn(|| app_event_rx.try_recv().ok())
            .any(|event| matches!(event, AppEvent::CodexOp(Op::UserTurn { .. })))
    );
    let recorded = take_recorded_requests(&requests);
    assert!(
        recorded
            .iter()
            .any(|request| request.method == "thread/resume")
    );
    assert!(
        recorded
            .iter()
            .any(|request| request.method == "thread/read")
    );

    app_server.shutdown().await?;
    proxy.await??;
    Ok(())
}

#[test]
fn lineage_backfill_resumes_failed_cursor_without_refetching_prefix() -> Result<()> {
    run_large_stack_app_test(|| async {
        let mut app = make_test_app().await;
        let primary_thread_id = ThreadId::new();
        let child_thread_id = ThreadId::new();
        let grandchild_thread_id = ThreadId::new();
        configure_backfill_primary(&mut app, primary_thread_id);
        let responses = Arc::new(Mutex::new(VecDeque::from([
            ScriptedLineageResponse::Page(scripted_lineage_page(
                vec![scripted_lineage_thread(
                    &app.config,
                    child_thread_id,
                    primary_thread_id,
                    /*depth*/ 1,
                )],
                Some("page-2".to_string()),
            )),
            ScriptedLineageResponse::Error("transient list failure".to_string()),
            ScriptedLineageResponse::Page(scripted_lineage_page(
                vec![scripted_lineage_thread(
                    &app.config,
                    grandchild_thread_id,
                    child_thread_id,
                    /*depth*/ 2,
                )],
                /*next_cursor*/ None,
            )),
        ])));
        let (mut app_server, requests, proxy) = start_recording_app_server_with_lineage(
            &app.config,
            /*blocked_thread_read_id*/ None,
            Some(Arc::clone(&responses)),
            /*thread_read_responses*/ None,
        )
        .await?;

        let first = app.backfill_loaded_subagent_threads(&mut app_server).await;
        assert_eq!(
            first.status,
            crate::app::session_lifecycle::LoadedSubagentBackfillStatus::RetryableError
        );
        assert!(app.agent_navigation.get(&child_thread_id).is_some());
        let second = app.backfill_loaded_subagent_threads(&mut app_server).await;
        assert!(second.completed);
        assert_eq!(
            second.status,
            crate::app::session_lifecycle::LoadedSubagentBackfillStatus::Complete
        );
        assert!(app.agent_navigation.get(&grandchild_thread_id).is_some());

        let cursors = take_recorded_requests(&requests)
            .into_iter()
            .filter(|request| request.method == "thread/list")
            .map(|request| {
                request
                    .params
                    .and_then(|params| params.get("cursor").cloned())
                    .and_then(|cursor| cursor.as_str().map(str::to_string))
            })
            .collect::<Vec<_>>();
        assert_eq!(
            cursors,
            vec![None, Some("page-2".to_string()), Some("page-2".to_string())]
        );
        app_server.shutdown().await?;
        proxy.await??;
        Ok(())
    })
}

#[test]
fn completed_lineage_backfill_admits_descendant_behind_filtered_connector() -> Result<()> {
    run_large_stack_app_test(|| async {
        let mut app = make_test_app().await;
        let primary_thread_id = ThreadId::new();
        let filtered_connector_id = ThreadId::new();
        let grandchild_thread_id = ThreadId::new();
        configure_backfill_primary(&mut app, primary_thread_id);
        let responses = Arc::new(Mutex::new(VecDeque::from([ScriptedLineageResponse::Page(
            scripted_lineage_page(
                vec![scripted_lineage_thread(
                    &app.config,
                    grandchild_thread_id,
                    filtered_connector_id,
                    /*depth*/ 2,
                )],
                /*next_cursor*/ None,
            ),
        )])));
        let (mut app_server, requests, proxy) = start_recording_app_server_with_lineage(
            &app.config,
            /*blocked_thread_read_id*/ None,
            Some(Arc::clone(&responses)),
            /*thread_read_responses*/ None,
        )
        .await?;

        let backfill = app.backfill_loaded_subagent_threads(&mut app_server).await;

        assert!(backfill.completed);
        assert_eq!(take_backfill_counts(&requests), (1, 0, 0));
        assert!(app.agent_navigation.get(&filtered_connector_id).is_none());
        assert!(app.agent_navigation.get(&grandchild_thread_id).is_some());

        app_server.shutdown().await?;
        proxy.await??;
        Ok(())
    })
}

#[test]
fn unacknowledged_lineage_completion_keeps_strict_local_validation() -> Result<()> {
    run_large_stack_app_test(|| async {
        let mut app = make_test_app().await;
        let primary_thread_id = ThreadId::new();
        let direct_child_thread_id = ThreadId::new();
        let locally_valid_grandchild_thread_id = ThreadId::new();
        let filtered_connector_id = ThreadId::new();
        let hidden_grandchild_thread_id = ThreadId::new();
        let unrelated_parent_id = ThreadId::new();
        let unrelated_child_thread_id = ThreadId::new();
        configure_backfill_primary(&mut app, primary_thread_id);
        let mut untrusted_hidden_grandchild = scripted_lineage_thread(
            &app.config,
            hidden_grandchild_thread_id,
            filtered_connector_id,
            /*depth*/ 2,
        );
        untrusted_hidden_grandchild.can_accept_direct_input = None;
        let mut untrusted_unrelated_child = scripted_lineage_thread(
            &app.config,
            unrelated_child_thread_id,
            unrelated_parent_id,
            /*depth*/ 1,
        );
        untrusted_unrelated_child.can_accept_direct_input = None;
        let responses = Arc::new(Mutex::new(VecDeque::from([
            ScriptedLineageResponse::Page(scripted_unacknowledged_lineage_page(
                vec![
                    scripted_lineage_thread(
                        &app.config,
                        direct_child_thread_id,
                        primary_thread_id,
                        /*depth*/ 1,
                    ),
                    untrusted_hidden_grandchild,
                ],
                Some("page-2".to_string()),
            )),
            ScriptedLineageResponse::Page(scripted_unacknowledged_lineage_page(
                vec![
                    scripted_lineage_thread(
                        &app.config,
                        locally_valid_grandchild_thread_id,
                        direct_child_thread_id,
                        /*depth*/ 2,
                    ),
                    untrusted_unrelated_child,
                ],
                /*next_cursor*/ None,
            )),
        ])));
        let (mut app_server, requests, proxy) = start_recording_app_server_with_lineage(
            &app.config,
            /*blocked_thread_read_id*/ None,
            Some(Arc::clone(&responses)),
            /*thread_read_responses*/ None,
        )
        .await?;

        let backfill = app.backfill_loaded_subagent_threads(&mut app_server).await;

        assert!(backfill.completed);
        assert_eq!(take_backfill_counts(&requests), (2, 0, 0));
        assert!(app.agent_navigation.get(&direct_child_thread_id).is_some());
        assert!(
            app.agent_navigation
                .get(&locally_valid_grandchild_thread_id)
                .is_some()
        );
        assert!(
            app.agent_navigation
                .get(&hidden_grandchild_thread_id)
                .is_none()
        );
        assert!(
            app.agent_navigation
                .get(&unrelated_child_thread_id)
                .is_none()
        );

        app_server.shutdown().await?;
        proxy.await??;
        Ok(())
    })
}

#[test]
fn unacknowledged_lineage_does_not_poison_authoritative_retention_budget() -> Result<()> {
    run_large_stack_app_test(|| async {
        let mut app = make_test_app().await;
        let primary_thread_id = ThreadId::new();
        let valid_child_thread_id = ThreadId::new();
        configure_backfill_primary(&mut app, primary_thread_id);

        let unrelated_thread_ids = (0..crate::app::loaded_threads::MAX_RETAINED_SUBAGENT_LINEAGE)
            .map(|_| ThreadId::new())
            .collect::<Vec<_>>();
        let mut rows = unrelated_thread_ids
            .iter()
            .map(|thread_id| {
                scripted_lineage_thread(&app.config, *thread_id, ThreadId::new(), /*depth*/ 1)
            })
            .collect::<Vec<_>>();
        rows.push(scripted_lineage_thread(
            &app.config,
            valid_child_thread_id,
            primary_thread_id,
            /*depth*/ 1,
        ));
        let mut pages = VecDeque::new();
        let page_count = rows
            .len()
            .div_ceil(crate::app::session_lifecycle::SUBAGENT_BACKFILL_PAGE_SIZE as usize);
        for (page_index, page) in rows
            .chunks(crate::app::session_lifecycle::SUBAGENT_BACKFILL_PAGE_SIZE as usize)
            .enumerate()
        {
            pages.push_back(ScriptedLineageResponse::Page(
                scripted_unacknowledged_lineage_page(
                    page.to_vec(),
                    Some(format!("page-{}", page_index + 1)),
                ),
            ));
        }
        assert_eq!(
            page_count,
            crate::app::loaded_threads::SUBAGENT_BACKFILL_PAGES_PER_ATTEMPT
        );
        pages.push_back(ScriptedLineageResponse::Page(
            scripted_unacknowledged_lineage_page(Vec::new(), /*next_cursor*/ None),
        ));
        let responses = Arc::new(Mutex::new(pages));
        let (mut app_server, requests, proxy) = start_recording_app_server_with_lineage(
            &app.config,
            /*blocked_thread_read_id*/ None,
            Some(Arc::clone(&responses)),
            /*thread_read_responses*/ None,
        )
        .await?;

        let first = app.backfill_loaded_subagent_threads(&mut app_server).await;
        assert_eq!(
            first.status,
            crate::app::session_lifecycle::LoadedSubagentBackfillStatus::Paused
        );
        assert_eq!(take_backfill_counts(&requests), (page_count, 0, 0));
        assert!(app.agent_navigation.get(&valid_child_thread_id).is_some());
        assert!(
            unrelated_thread_ids
                .iter()
                .all(|thread_id| app.agent_navigation.get(thread_id).is_none())
        );
        let progress = app
            .subagent_backfill_progress
            .as_ref()
            .expect("paused compatibility listing should retain bounded progress");
        assert_eq!(progress.retained_thread_count(), 1);
        assert_eq!(
            progress.compatibility_retained_thread_count(),
            codex_state::MAX_THREAD_RELATION_DESCENDANTS
        );

        let second = app.backfill_loaded_subagent_threads(&mut app_server).await;
        assert!(second.completed);
        assert_eq!(
            second.status,
            crate::app::session_lifecycle::LoadedSubagentBackfillStatus::Complete
        );
        // Compatibility admission is followed by one bounded authoritative liveness refresh.
        assert_eq!(take_backfill_counts(&requests), (1, 0, 1));
        assert!(app.subagent_backfill_progress.is_none());

        app_server.shutdown().await?;
        proxy.await??;
        Ok(())
    })
}

#[test]
fn unacknowledged_lineage_fails_closed_beyond_raw_compatibility_cap() -> Result<()> {
    run_large_stack_app_test(|| async {
        let mut app = make_test_app().await;
        let primary_thread_id = ThreadId::new();
        let valid_child_thread_id = ThreadId::new();
        configure_backfill_primary(&mut app, primary_thread_id);

        let unrelated_thread_ids = (0..codex_state::MAX_THREAD_RELATION_DESCENDANTS)
            .map(|_| ThreadId::new())
            .collect::<Vec<_>>();
        let mut rows = unrelated_thread_ids
            .iter()
            .map(|thread_id| {
                scripted_lineage_thread(&app.config, *thread_id, ThreadId::new(), /*depth*/ 1)
            })
            .collect::<Vec<_>>();
        rows.push(scripted_lineage_thread(
            &app.config,
            valid_child_thread_id,
            primary_thread_id,
            /*depth*/ 1,
        ));
        let page_size = crate::app::session_lifecycle::SUBAGENT_BACKFILL_PAGE_SIZE as usize;
        let page_count = rows.len().div_ceil(page_size);
        let pages = rows
            .chunks(page_size)
            .enumerate()
            .map(|(page_index, page)| {
                ScriptedLineageResponse::Page(scripted_unacknowledged_lineage_page(
                    page.to_vec(),
                    (page_index + 1 < page_count).then(|| format!("page-{}", page_index + 1)),
                ))
            })
            .collect::<VecDeque<_>>();
        assert_eq!(
            page_count,
            crate::app::loaded_threads::SUBAGENT_BACKFILL_PAGES_PER_ATTEMPT + 1
        );
        let responses = Arc::new(Mutex::new(pages));
        let (mut app_server, requests, proxy) = start_recording_app_server_with_lineage(
            &app.config,
            /*blocked_thread_read_id*/ None,
            Some(Arc::clone(&responses)),
            /*thread_read_responses*/ None,
        )
        .await?;

        let first = app.backfill_loaded_subagent_threads(&mut app_server).await;
        assert_eq!(
            first.status,
            crate::app::session_lifecycle::LoadedSubagentBackfillStatus::Paused
        );
        assert_eq!(
            take_backfill_counts(&requests),
            (
                crate::app::loaded_threads::SUBAGENT_BACKFILL_PAGES_PER_ATTEMPT,
                0,
                0,
            )
        );
        let progress = app
            .subagent_backfill_progress
            .as_ref()
            .expect("paused compatibility listing should retain bounded progress");
        assert_eq!(progress.retained_thread_count(), 0);
        assert_eq!(
            progress.compatibility_retained_thread_count(),
            codex_state::MAX_THREAD_RELATION_DESCENDANTS
        );

        let second = app.backfill_loaded_subagent_threads(&mut app_server).await;
        assert!(second.completed);
        assert_eq!(
            second.status,
            crate::app::session_lifecycle::LoadedSubagentBackfillStatus::Truncated
        );
        assert_eq!(take_backfill_counts(&requests), (1, 0, 0));
        assert!(app.agent_navigation.get(&valid_child_thread_id).is_none());
        assert!(
            unrelated_thread_ids
                .iter()
                .all(|thread_id| app.agent_navigation.get(thread_id).is_none())
        );
        assert_eq!(
            app.subagent_backfill_progress
                .as_ref()
                .map(
                    super::super::session_lifecycle::LoadedSubagentBackfillProgress::retained_thread_count,
                ),
            Some(0)
        );

        app_server.shutdown().await?;
        proxy.await??;
        Ok(())
    })
}

#[test]
fn no_state_lineage_fallback_recovers_only_loaded_direct_children() -> Result<()> {
    run_large_stack_app_test(|| async {
        let mut app = make_test_app().await;
        let codex_home = tempdir()?;
        app.config.codex_home = codex_home.path().to_path_buf().abs();
        app.config.sqlite = SqliteConfig::new_for_testing(codex_home.path().abs());
        let primary_thread_id = ThreadId::from_string(
            &create_fake_rollout(
                codex_home.path(),
                "2026-01-01T00-00-00",
                "2026-01-01T00:00:00Z",
                "Primary thread",
                Some(app.config.model_provider_id.as_str()),
                /*git_info*/ None,
            )
            .expect("create primary rollout"),
        )?;
        let direct_child_thread_id = ThreadId::from_string(
            &create_fake_parented_rollout_with_source(
                codex_home.path(),
                "2026-01-01T00-00-01",
                "2026-01-01T00:00:01Z",
                "Direct child",
                Some(app.config.model_provider_id.as_str()),
                /*git_info*/ None,
                RolloutSessionSource::SubAgent(SubAgentSource::ThreadSpawn {
                    parent_thread_id: primary_thread_id,
                    depth: 1,
                    agent_path: Some(
                        AgentPath::try_from("/root/worker").expect("valid agent path"),
                    ),
                    agent_nickname: Some("worker".to_string()),
                    agent_role: Some("worker".to_string()),
                }),
                primary_thread_id.into(),
                primary_thread_id,
            )
            .expect("create direct child rollout"),
        )?;
        let unrelated_thread_id = ThreadId::from_string(
            &create_fake_rollout(
                codex_home.path(),
                "2026-01-01T00-00-02",
                "2026-01-01T00:00:02Z",
                "Unrelated thread",
                Some(app.config.model_provider_id.as_str()),
                /*git_info*/ None,
            )
            .expect("create unrelated rollout"),
        )?;
        let (mut app_server, requests, proxy) = start_recording_app_server_with_lineage_and_state(
            &app.config,
            /*blocked_thread_read_id*/ None,
            /*lineage_responses*/ None,
            /*thread_read_responses*/ None,
            /*loaded_list_responses*/ None,
            /*resume_error_thread_id*/ None,
            /*with_state_db*/ false,
        )
        .await?;
        let primary = app_server
            .resume_thread(
                app.config.clone(),
                primary_thread_id,
                app.resume_model_settings(),
            )
            .await?;
        app.enqueue_primary_thread_session(primary.session, primary.turns)
            .await?;
        for thread_id in [direct_child_thread_id, unrelated_thread_id] {
            app_server
                .resume_thread(app.config.clone(), thread_id, app.resume_model_settings())
                .await?;
        }
        take_recorded_requests(&requests);

        let backfill = app.backfill_loaded_subagent_threads(&mut app_server).await;

        assert!(!backfill.completed);
        assert_eq!(
            backfill.status,
            crate::app::session_lifecycle::LoadedSubagentBackfillStatus::RetryableError
        );
        assert!(app.agent_navigation.get(&direct_child_thread_id).is_some());
        assert!(app.agent_navigation.get(&unrelated_thread_id).is_none());
        let recorded = take_recorded_requests(&requests);
        assert_eq!(
            recorded
                .iter()
                .filter(|request| request.method == "thread/list")
                .count(),
            1
        );
        let loaded_requests = recorded
            .iter()
            .filter(|request| request.method == "thread/loaded/list")
            .collect::<Vec<_>>();
        assert_eq!(loaded_requests.len(), 1);
        assert_eq!(
            loaded_requests[0]
                .params
                .as_ref()
                .and_then(|params| params.get("limit"))
                .and_then(serde_json::Value::as_u64),
            Some(u64::from(
                crate::app::session_lifecycle::SUBAGENT_BACKFILL_PAGE_SIZE,
            ))
        );
        assert_eq!(
            recorded
                .iter()
                .filter(|request| request.method == "thread/read")
                .count(),
            2
        );

        app_server.shutdown().await?;
        proxy.await??;
        Ok(())
    })
}

#[test]
fn loaded_fallback_skips_terminal_race_before_valid_child() -> Result<()> {
    run_large_stack_app_test(|| async {
        let mut app = make_test_app().await;
        let primary_thread_id = ThreadId::new();
        let stale_thread_id = ThreadId::new();
        let valid_child_thread_id = ThreadId::new();
        configure_backfill_primary(&mut app, primary_thread_id);
        let lineage_responses = Arc::new(Mutex::new(VecDeque::from([
            ScriptedLineageResponse::Error("state DB unavailable".to_string()),
        ])));
        let loaded_list_responses = Arc::new(Mutex::new(VecDeque::from([serde_json::json!({
            "data": [stale_thread_id.to_string(), valid_child_thread_id.to_string()],
            "nextCursor": null,
        })])));
        let thread_read_responses = Arc::new(Mutex::new(VecDeque::from([
            ScriptedThreadReadResponse::Error(format!("thread not loaded: {stale_thread_id}")),
            ScriptedThreadReadResponse::Thread(serde_json::to_value(scripted_lineage_thread(
                &app.config,
                valid_child_thread_id,
                primary_thread_id,
                /*depth*/ 1,
            ))?),
        ])));
        let (mut app_server, requests, proxy) = start_recording_app_server_with_lineage_and_state(
            &app.config,
            /*blocked_thread_read_id*/ None,
            Some(lineage_responses),
            Some(thread_read_responses),
            Some(loaded_list_responses),
            /*resume_error_thread_id*/ None,
            /*with_state_db*/ true,
        )
        .await?;

        let backfill = app.backfill_loaded_subagent_threads(&mut app_server).await;

        assert_eq!(
            backfill.status,
            crate::app::session_lifecycle::LoadedSubagentBackfillStatus::RetryableError
        );
        assert!(app.agent_navigation.get(&stale_thread_id).is_none());
        assert!(app.agent_navigation.get(&valid_child_thread_id).is_some());
        assert_eq!(take_backfill_counts(&requests), (1, 1, 2));

        app_server.shutdown().await?;
        proxy.await??;
        Ok(())
    })
}

#[test]
fn authoritative_retry_promotes_fallback_child_behind_hidden_connector() -> Result<()> {
    run_large_stack_app_test(|| async {
        let mut app = make_test_app().await;
        let primary_thread_id = ThreadId::new();
        let hidden_connector_id = ThreadId::new();
        let child_thread_id = ThreadId::new();
        configure_backfill_primary(&mut app, primary_thread_id);
        let child = scripted_lineage_thread(
            &app.config,
            child_thread_id,
            hidden_connector_id,
            /*depth*/ 2,
        );
        let lineage_responses = Arc::new(Mutex::new(VecDeque::from([
            ScriptedLineageResponse::Error("state DB unavailable".to_string()),
            ScriptedLineageResponse::Page(scripted_lineage_page(
                vec![child.clone()],
                /*next_cursor*/ None,
            )),
        ])));
        let loaded_list_responses = Arc::new(Mutex::new(VecDeque::from([serde_json::json!({
            "data": [child_thread_id.to_string()],
            "nextCursor": null,
        })])));
        let thread_read_responses = Arc::new(Mutex::new(VecDeque::from([
            ScriptedThreadReadResponse::Thread(serde_json::to_value(child)?),
        ])));
        let (mut app_server, requests, proxy) = start_recording_app_server_with_lineage_and_state(
            &app.config,
            /*blocked_thread_read_id*/ None,
            Some(lineage_responses),
            Some(thread_read_responses),
            Some(loaded_list_responses),
            /*resume_error_thread_id*/ None,
            /*with_state_db*/ true,
        )
        .await?;

        let first = app.backfill_loaded_subagent_threads(&mut app_server).await;
        assert_eq!(
            first.status,
            crate::app::session_lifecycle::LoadedSubagentBackfillStatus::RetryableError
        );
        assert!(app.agent_navigation.get(&child_thread_id).is_none());
        assert_eq!(
            app.subagent_backfill_progress
                .as_ref()
                .map(
                    super::super::session_lifecycle::LoadedSubagentBackfillProgress::retained_thread_count,
                ),
            Some(0)
        );
        assert_eq!(take_backfill_counts(&requests), (1, 1, 1));

        let second = app.backfill_loaded_subagent_threads(&mut app_server).await;
        assert!(second.completed);
        assert_eq!(
            second.status,
            crate::app::session_lifecycle::LoadedSubagentBackfillStatus::Complete
        );
        assert!(app.agent_navigation.get(&hidden_connector_id).is_none());
        assert!(app.agent_navigation.get(&child_thread_id).is_some());
        assert_eq!(take_backfill_counts(&requests), (1, 0, 0));

        app_server.shutdown().await?;
        proxy.await??;
        Ok(())
    })
}

#[test]
fn loaded_fallback_stops_paging_when_descendant_capacity_is_reached() -> Result<()> {
    run_large_stack_app_test(|| async {
        let mut app = make_test_app().await;
        let primary_thread_id = ThreadId::new();
        configure_backfill_primary(&mut app, primary_thread_id);
        let loaded_thread_ids = (0..codex_state::MAX_THREAD_RELATION_DESCENDANTS)
            .map(|_| ThreadId::new().to_string())
            .collect::<Vec<_>>();
        let lineage_responses = Arc::new(Mutex::new(VecDeque::from([
            ScriptedLineageResponse::Error("state DB unavailable".to_string()),
            ScriptedLineageResponse::Error("state DB unavailable".to_string()),
        ])));
        let loaded_list_responses = Arc::new(Mutex::new(VecDeque::from([serde_json::json!({
            "data": loaded_thread_ids,
            "nextCursor": "must-not-be-fetched",
        })])));
        let (mut app_server, requests, proxy) = start_recording_app_server_with_lineage_and_state(
            &app.config,
            /*blocked_thread_read_id*/ None,
            Some(lineage_responses),
            /*thread_read_responses*/ None,
            Some(Arc::clone(&loaded_list_responses)),
            /*resume_error_thread_id*/ None,
            /*with_state_db*/ true,
        )
        .await?;

        let first = app.backfill_loaded_subagent_threads(&mut app_server).await;
        assert_eq!(
            first.status,
            crate::app::session_lifecycle::LoadedSubagentBackfillStatus::Paused
        );
        let first_counts = take_backfill_counts(&requests);
        assert_eq!(first_counts.0, 1);
        assert_eq!(first_counts.1, 1);
        assert_eq!(
            app.subagent_backfill_progress
                .as_ref()
                .map(
                    super::super::session_lifecycle::LoadedSubagentBackfillProgress::retained_thread_count,
                ),
            Some(0)
        );
        assert_eq!(
            app.subagent_backfill_progress
                .as_ref()
                .map(
                    super::super::session_lifecycle::LoadedSubagentBackfillProgress::fallback_retained_thread_count,
                ),
            Some(crate::app::loaded_threads::MAX_RETAINED_SUBAGENT_LINEAGE)
        );
        assert!(app.subagent_backfill_progress.as_ref().is_some_and(
            super::super::session_lifecycle::LoadedSubagentBackfillProgress::is_truncated,
        ));

        let second = app.backfill_loaded_subagent_threads(&mut app_server).await;
        assert_eq!(
            second.status,
            crate::app::session_lifecycle::LoadedSubagentBackfillStatus::Paused
        );
        let second_counts = take_backfill_counts(&requests);
        assert_eq!(second_counts.0, 1);
        assert_eq!(second_counts.1, 0);
        assert!(
            loaded_list_responses
                .lock()
                .expect("loaded response lock")
                .is_empty()
        );

        app_server.shutdown().await?;
        proxy.await??;
        Ok(())
    })
}

#[test]
fn relation_failure_services_staged_prefix_before_retry() -> Result<()> {
    run_large_stack_app_test(|| async {
        let mut app = make_test_app().await;
        let primary_thread_id = ThreadId::new();
        let child_thread_id = ThreadId::new();
        configure_backfill_primary(&mut app, primary_thread_id);
        let mut prefix_child = scripted_lineage_thread(
            &app.config,
            child_thread_id,
            primary_thread_id,
            /*depth*/ 1,
        );
        prefix_child.can_accept_direct_input = None;
        let lineage_responses = Arc::new(Mutex::new(VecDeque::from([
            ScriptedLineageResponse::Page(scripted_lineage_page(
                vec![prefix_child],
                Some("page-2".to_string()),
            )),
            ScriptedLineageResponse::Error("persistent relation failure".to_string()),
            ScriptedLineageResponse::Error("persistent relation failure".to_string()),
        ])));
        let loaded_list_responses = Arc::new(Mutex::new(VecDeque::from([serde_json::json!({
            "data": [],
            "nextCursor": null,
        })])));
        let thread_read_responses = Arc::new(Mutex::new(VecDeque::from([
            ScriptedThreadReadResponse::Thread(serde_json::to_value(scripted_lineage_thread(
                &app.config,
                child_thread_id,
                primary_thread_id,
                /*depth*/ 1,
            ))?),
        ])));
        let (mut app_server, requests, proxy) = start_recording_app_server_with_lineage_and_state(
            &app.config,
            /*blocked_thread_read_id*/ None,
            Some(lineage_responses),
            Some(thread_read_responses),
            Some(loaded_list_responses),
            /*resume_error_thread_id*/ None,
            /*with_state_db*/ true,
        )
        .await?;

        let first = app.backfill_loaded_subagent_threads(&mut app_server).await;
        let second = app.backfill_loaded_subagent_threads(&mut app_server).await;

        assert_eq!(
            (first.status, second.status),
            (
                crate::app::session_lifecycle::LoadedSubagentBackfillStatus::RetryableError,
                crate::app::session_lifecycle::LoadedSubagentBackfillStatus::RetryableError,
            )
        );
        assert!(app.agent_navigation.get(&child_thread_id).is_some());
        assert!(app.subagent_backfill_progress.is_some());
        let recorded = take_recorded_requests(&requests);
        assert_eq!(
            recorded
                .iter()
                .filter(|request| request.method == "thread/list")
                .count(),
            3
        );
        assert_eq!(
            recorded
                .iter()
                .filter(|request| request.method == "thread/loaded/list")
                .count(),
            1
        );
        assert_eq!(
            recorded
                .iter()
                .filter(|request| request.method == "thread/read")
                .count(),
            1
        );
        let relation_cursors = recorded
            .iter()
            .filter(|request| request.method == "thread/list")
            .map(|request| {
                request
                    .params
                    .as_ref()
                    .and_then(|params| params.get("cursor"))
                    .and_then(serde_json::Value::as_str)
            })
            .collect::<Vec<_>>();
        assert_eq!(relation_cursors, vec![None, Some("page-2"), Some("page-2")]);

        app_server.shutdown().await?;
        proxy.await??;
        Ok(())
    })
}

#[test]
fn lineage_backfill_retries_authoritative_thread_read_without_relisting() -> Result<()> {
    run_large_stack_app_test(|| async {
        let mut app = make_test_app().await;
        let primary_thread_id = ThreadId::new();
        let child_thread_id = ThreadId::new();
        configure_backfill_primary(&mut app, primary_thread_id);
        let mut listed_child = scripted_lineage_thread(
            &app.config,
            child_thread_id,
            primary_thread_id,
            /*depth*/ 1,
        );
        listed_child.can_accept_direct_input = None;
        let mut authoritative_child = listed_child.clone();
        authoritative_child.can_accept_direct_input = Some(false);
        let lineage_responses =
            Arc::new(Mutex::new(VecDeque::from([ScriptedLineageResponse::Page(
                scripted_lineage_page(vec![listed_child], /*next_cursor*/ None),
            )])));
        let thread_read_responses = Arc::new(Mutex::new(VecDeque::from([
            ScriptedThreadReadResponse::Error("transient read failure".to_string()),
            ScriptedThreadReadResponse::Thread(serde_json::to_value(authoritative_child)?),
        ])));
        let (mut app_server, requests, proxy) = start_recording_app_server_with_lineage(
            &app.config,
            /*blocked_thread_read_id*/ None,
            Some(Arc::clone(&lineage_responses)),
            Some(Arc::clone(&thread_read_responses)),
        )
        .await?;

        let first = app.backfill_loaded_subagent_threads(&mut app_server).await;
        assert!(!first.completed);
        assert_eq!(
            first.status,
            crate::app::session_lifecycle::LoadedSubagentBackfillStatus::RetryableError
        );
        assert_eq!(take_backfill_counts(&requests), (1, 0, 1));
        assert!(app.agent_navigation.get(&child_thread_id).is_none());
        let second = app.backfill_loaded_subagent_threads(&mut app_server).await;
        assert!(second.completed);
        assert_eq!(take_backfill_counts(&requests), (0, 0, 1));
        assert_eq!(
            second.refreshed_thread_ids,
            HashSet::from([child_thread_id])
        );
        assert!(app.agent_navigation.is_parent_owned(child_thread_id));

        app_server.shutdown().await?;
        proxy.await??;
        Ok(())
    })
}

#[test]
fn lineage_backfill_cursor_cycle_does_not_finalize_pending_descendants() -> Result<()> {
    run_large_stack_app_test(|| async {
        let mut app = make_test_app().await;
        let primary_thread_id = ThreadId::new();
        let first_thread_id = ThreadId::new();
        let second_thread_id = ThreadId::new();
        configure_backfill_primary(&mut app, primary_thread_id);
        let pending_cycle = vec![
            scripted_lineage_thread(
                &app.config,
                first_thread_id,
                second_thread_id,
                /*depth*/ 2,
            ),
            scripted_lineage_thread(
                &app.config,
                second_thread_id,
                first_thread_id,
                /*depth*/ 3,
            ),
        ];
        let responses = Arc::new(Mutex::new(VecDeque::from([
            ScriptedLineageResponse::Page(scripted_lineage_page(
                pending_cycle.clone(),
                Some("cycle".to_string()),
            )),
            ScriptedLineageResponse::Page(scripted_lineage_page(
                Vec::new(),
                Some("cycle".to_string()),
            )),
            ScriptedLineageResponse::Page(scripted_lineage_page(
                pending_cycle,
                Some("cycle".to_string()),
            )),
            ScriptedLineageResponse::Page(scripted_lineage_page(
                Vec::new(),
                Some("cycle".to_string()),
            )),
        ])));
        let (mut app_server, requests, proxy) = start_recording_app_server_with_lineage(
            &app.config,
            /*blocked_thread_read_id*/ None,
            Some(Arc::clone(&responses)),
            /*thread_read_responses*/ None,
        )
        .await?;

        let first = app.backfill_loaded_subagent_threads(&mut app_server).await;
        let second = app.backfill_loaded_subagent_threads(&mut app_server).await;
        assert_eq!(
            first.status,
            crate::app::session_lifecycle::LoadedSubagentBackfillStatus::CursorCycle
        );
        assert_eq!(second.status, first.status);
        assert_eq!(take_backfill_counts(&requests).0, 4);
        assert!(app.agent_navigation.get(&first_thread_id).is_none());
        assert!(app.agent_navigation.get(&second_thread_id).is_none());
        assert!(app.subagent_backfill_progress.is_none());

        app_server.shutdown().await?;
        proxy.await??;
        Ok(())
    })
}

#[test]
fn lineage_backfill_persistent_cycle_refreshes_pathless_v2_child_per_open() -> Result<()> {
    run_large_stack_app_test(|| async {
        let mut app = make_test_app().await;
        let primary_thread_id = ThreadId::new();
        let child_thread_id = ThreadId::new();
        configure_backfill_primary(&mut app, primary_thread_id);
        let mut listed_child = scripted_lineage_thread(
            &app.config,
            child_thread_id,
            primary_thread_id,
            /*depth*/ 1,
        );
        listed_child.can_accept_direct_input = None;
        let mut authoritative_child = listed_child.clone();
        authoritative_child.can_accept_direct_input = Some(false);
        let responses = Arc::new(Mutex::new(VecDeque::from([
            ScriptedLineageResponse::Page(scripted_lineage_page(
                vec![listed_child.clone()],
                Some("cycle".to_string()),
            )),
            ScriptedLineageResponse::Page(scripted_lineage_page(
                Vec::new(),
                Some("cycle".to_string()),
            )),
            ScriptedLineageResponse::Page(scripted_lineage_page(
                vec![listed_child],
                Some("cycle".to_string()),
            )),
            ScriptedLineageResponse::Page(scripted_lineage_page(
                Vec::new(),
                Some("cycle".to_string()),
            )),
        ])));
        let thread_read_responses = Arc::new(Mutex::new(VecDeque::from([
            ScriptedThreadReadResponse::Thread(serde_json::to_value(authoritative_child.clone())?),
            ScriptedThreadReadResponse::Thread(serde_json::to_value(authoritative_child)?),
        ])));
        let (mut app_server, requests, proxy) = start_recording_app_server_with_lineage(
            &app.config,
            /*blocked_thread_read_id*/ None,
            Some(Arc::clone(&responses)),
            Some(Arc::clone(&thread_read_responses)),
        )
        .await?;

        let first = app.backfill_loaded_subagent_threads(&mut app_server).await;
        assert_eq!(
            first.status,
            crate::app::session_lifecycle::LoadedSubagentBackfillStatus::CursorCycle
        );
        assert_eq!(
            take_backfill_counts_and_lineage_cursors(&requests),
            ((2, 0, 1), vec![None, Some("cycle".to_string())])
        );
        assert!(first.refreshed_thread_ids.contains(&child_thread_id));
        assert!(app.agent_navigation.get(&child_thread_id).is_some());
        assert!(app.agent_navigation.is_parent_owned(child_thread_id));
        assert!(app.subagent_backfill_progress.is_none());

        let second = app.backfill_loaded_subagent_threads(&mut app_server).await;
        assert_eq!(second.status, first.status);
        assert_eq!(
            take_backfill_counts_and_lineage_cursors(&requests),
            ((2, 0, 1), vec![None, Some("cycle".to_string())])
        );
        assert!(second.refreshed_thread_ids.contains(&child_thread_id));
        assert!(app.agent_navigation.is_parent_owned(child_thread_id));
        assert!(app.subagent_backfill_progress.is_none());

        app_server.shutdown().await?;
        proxy.await??;
        Ok(())
    })
}

#[test]
fn lineage_backfill_recovers_after_cursor_cycle_and_finds_new_child() -> Result<()> {
    run_large_stack_app_test(|| async {
        let mut app = make_test_app().await;
        let primary_thread_id = ThreadId::new();
        let child_thread_id = ThreadId::new();
        configure_backfill_primary(&mut app, primary_thread_id);
        let responses = Arc::new(Mutex::new(VecDeque::from([
            ScriptedLineageResponse::Page(scripted_lineage_page(
                Vec::new(),
                Some("cycle".to_string()),
            )),
            ScriptedLineageResponse::Page(scripted_lineage_page(
                Vec::new(),
                Some("cycle".to_string()),
            )),
            ScriptedLineageResponse::Page(scripted_lineage_page(
                vec![scripted_lineage_thread(
                    &app.config,
                    child_thread_id,
                    primary_thread_id,
                    /*depth*/ 1,
                )],
                /*next_cursor*/ None,
            )),
        ])));
        let (mut app_server, requests, proxy) = start_recording_app_server_with_lineage(
            &app.config,
            /*blocked_thread_read_id*/ None,
            Some(Arc::clone(&responses)),
            /*thread_read_responses*/ None,
        )
        .await?;

        let first = app.backfill_loaded_subagent_threads(&mut app_server).await;
        assert_eq!(
            first.status,
            crate::app::session_lifecycle::LoadedSubagentBackfillStatus::CursorCycle
        );
        let second = app.backfill_loaded_subagent_threads(&mut app_server).await;
        assert!(second.completed);
        assert!(app.agent_navigation.get(&child_thread_id).is_some());
        assert_eq!(take_backfill_counts(&requests).0, 3);

        app_server.shutdown().await?;
        proxy.await??;
        Ok(())
    })
}

#[test]
fn lineage_backfill_advances_beyond_page_budget_across_opens() -> Result<()> {
    run_large_stack_app_test(|| async {
        const PAGE_COUNT: usize =
            crate::app::loaded_threads::SUBAGENT_BACKFILL_PAGES_PER_ATTEMPT + 2;
        let mut app = make_test_app().await;
        let primary_thread_id = ThreadId::new();
        let filtered_connector_id = ThreadId::new();
        configure_backfill_primary(&mut app, primary_thread_id);
        let mut pages = VecDeque::new();
        let descendant_thread_ids = (0..PAGE_COUNT).map(|_| ThreadId::new()).collect::<Vec<_>>();
        for (index, descendant_thread_id) in descendant_thread_ids.iter().copied().enumerate() {
            pages.push_back(ScriptedLineageResponse::Page(scripted_lineage_page(
                vec![scripted_lineage_thread(
                    &app.config,
                    descendant_thread_id,
                    if index == 0 {
                        filtered_connector_id
                    } else {
                        primary_thread_id
                    },
                    /*depth*/ 1,
                )],
                (index + 1 < PAGE_COUNT).then(|| format!("page-{}", index + 1)),
            )));
        }
        let responses = Arc::new(Mutex::new(pages));
        let (mut app_server, requests, proxy) = start_recording_app_server_with_lineage(
            &app.config,
            /*blocked_thread_read_id*/ None,
            Some(Arc::clone(&responses)),
            /*thread_read_responses*/ None,
        )
        .await?;

        let first = app.backfill_loaded_subagent_threads(&mut app_server).await;
        assert_eq!(
            first.status,
            crate::app::session_lifecycle::LoadedSubagentBackfillStatus::Paused
        );
        assert_eq!(take_backfill_counts(&requests).0, PAGE_COUNT - 2);
        assert!(
            app.agent_navigation
                .get(&descendant_thread_ids[0])
                .is_none(),
            "a paused partial listing must not finalize a descendant behind a filtered connector"
        );
        assert_eq!(
            descendant_thread_ids
                .iter()
                .filter(|thread_id| app.agent_navigation.get(thread_id).is_some())
                .count(),
            PAGE_COUNT - 3
        );
        let second = app.backfill_loaded_subagent_threads(&mut app_server).await;
        assert!(second.completed);
        assert_eq!(take_backfill_counts(&requests).0, 2);
        assert!(
            descendant_thread_ids
                .iter()
                .all(|thread_id| app.agent_navigation.get(thread_id).is_some())
        );
        assert!(responses.lock().expect("lineage response lock").is_empty());

        app_server.shutdown().await?;
        proxy.await??;
        Ok(())
    })
}

#[test]
fn empty_relation_limited_page_marks_backfill_and_picker_truncated() -> Result<()> {
    run_large_stack_app_test(|| async {
        let mut app = make_test_app().await;
        let primary_thread_id = ThreadId::new();
        configure_backfill_primary(&mut app, primary_thread_id);
        app.active_thread_id = Some(primary_thread_id);
        let responses = Arc::new(Mutex::new(VecDeque::from([ScriptedLineageResponse::Page(
            scripted_relation_limited_lineage_page(Vec::new(), /*next_cursor*/ None),
        )])));
        let (mut app_server, requests, proxy) = start_recording_app_server_with_lineage(
            &app.config,
            /*blocked_thread_read_id*/ None,
            Some(Arc::clone(&responses)),
            /*thread_read_responses*/ None,
        )
        .await?;

        app.open_agent_picker(&mut app_server).await;

        // Opening the picker performs one bounded refresh for the retained primary row.
        assert_eq!(take_backfill_counts(&requests), (1, 0, 1));
        assert!(app.subagent_backfill_progress.as_ref().is_some_and(
            super::super::session_lifecycle::LoadedSubagentBackfillProgress::is_truncated,
        ));
        assert!(
            render_bottom_popup(&app.chat_widget, /*width*/ 100)
                .contains("Additional rows were omitted")
        );

        app_server.shutdown().await?;
        proxy.await??;
        Ok(())
    })
}

#[test]
fn lineage_retention_cap_is_exact_and_idempotent_across_reopen() -> Result<()> {
    run_large_stack_app_test(|| async {
        const RETAINED_LIMIT: usize = crate::app::loaded_threads::MAX_RETAINED_SUBAGENT_LINEAGE;
        const PAGE_SIZE: usize =
            crate::app::session_lifecycle::SUBAGENT_BACKFILL_PAGE_SIZE as usize;
        let mut app = make_test_app().await;
        let primary_thread_id = ThreadId::new();
        configure_backfill_primary(&mut app, primary_thread_id);
        let descendant_thread_ids = (0..=RETAINED_LIMIT)
            .map(|_| ThreadId::new())
            .collect::<Vec<_>>();
        let mut pages = VecDeque::new();
        let page_count = descendant_thread_ids.len().div_ceil(PAGE_SIZE);
        for (page_index, thread_ids) in descendant_thread_ids.chunks(PAGE_SIZE).enumerate() {
            pages.push_back(ScriptedLineageResponse::Page(scripted_lineage_page(
                thread_ids
                    .iter()
                    .map(|thread_id| {
                        scripted_lineage_thread(
                            &app.config,
                            *thread_id,
                            primary_thread_id,
                            /*depth*/ 1,
                        )
                    })
                    .collect(),
                (page_index + 1 < page_count).then(|| format!("page-{}", page_index + 1)),
            )));
        }
        let responses = Arc::new(Mutex::new(pages));
        let (mut app_server, requests, proxy) = start_recording_app_server_with_lineage(
            &app.config,
            /*blocked_thread_read_id*/ None,
            Some(Arc::clone(&responses)),
            /*thread_read_responses*/ None,
        )
        .await?;

        let first = app.backfill_loaded_subagent_threads(&mut app_server).await;
        assert!(first.completed);
        assert_eq!(
            first.status,
            crate::app::session_lifecycle::LoadedSubagentBackfillStatus::Truncated
        );
        assert_eq!(take_backfill_counts(&requests), (page_count, 0, 0));
        assert_eq!(
            app.subagent_backfill_progress
                .as_ref()
                .map(
                    super::super::session_lifecycle::LoadedSubagentBackfillProgress::retained_thread_count,
                ),
            Some(RETAINED_LIMIT)
        );
        assert_eq!(
            descendant_thread_ids
                .iter()
                .filter(|thread_id| app.agent_navigation.get(thread_id).is_some())
                .count(),
            RETAINED_LIMIT
        );
        assert_eq!(
            app.agent_navigation.tracked_thread_ids().len(),
            codex_state::MAX_THREAD_RELATION_DESCENDANTS
        );
        assert!(
            app.agent_navigation
                .get(&descendant_thread_ids[RETAINED_LIMIT])
                .is_none()
        );

        let second = app.backfill_loaded_subagent_threads(&mut app_server).await;
        assert!(second.completed);
        assert_eq!(
            second.status,
            crate::app::session_lifecycle::LoadedSubagentBackfillStatus::Truncated
        );
        assert_eq!(take_backfill_counts(&requests), (0, 0, 0));
        assert_eq!(
            app.subagent_backfill_progress
                .as_ref()
                .map(
                    super::super::session_lifecycle::LoadedSubagentBackfillProgress::retained_thread_count,
                ),
            Some(RETAINED_LIMIT)
        );
        assert_eq!(
            descendant_thread_ids
                .iter()
                .filter(|thread_id| app.agent_navigation.get(thread_id).is_some())
                .count(),
            RETAINED_LIMIT
        );
        assert_eq!(
            app.agent_navigation.tracked_thread_ids().len(),
            codex_state::MAX_THREAD_RELATION_DESCENDANTS
        );
        app.open_agent_picker(&mut app_server).await;
        assert!(
            render_bottom_popup(&app.chat_widget, /*width*/ 100)
                .contains("Additional rows were omitted")
        );
        assert_eq!(take_backfill_counts(&requests), (0, 0, 0));
        assert!(responses.lock().expect("lineage response lock").is_empty());

        app_server.shutdown().await?;
        proxy.await??;
        Ok(())
    })
}

#[test]
fn unique_relation_cursor_over_cap_stops_pagination_across_reopen() -> Result<()> {
    run_large_stack_app_test(|| async {
        let mut app = make_test_app().await;
        let primary_thread_id = ThreadId::new();
        configure_backfill_primary(&mut app, primary_thread_id);
        let mut progress =
            crate::app::session_lifecycle::LoadedSubagentBackfillProgress::new(primary_thread_id);
        progress.seed_relation_cursors_to_limit();
        app.subagent_backfill_progress = Some(progress);
        let responses = Arc::new(Mutex::new(VecDeque::from([ScriptedLineageResponse::Page(
            scripted_lineage_page(Vec::new(), Some("over-limit".to_string())),
        )])));
        let (mut app_server, requests, proxy) = start_recording_app_server_with_lineage(
            &app.config,
            /*blocked_thread_read_id*/ None,
            Some(Arc::clone(&responses)),
            /*thread_read_responses*/ None,
        )
        .await?;

        let first = app.backfill_loaded_subagent_threads(&mut app_server).await;
        assert!(first.completed);
        assert_eq!(
            first.status,
            crate::app::session_lifecycle::LoadedSubagentBackfillStatus::Truncated
        );
        assert_eq!(take_backfill_counts(&requests), (1, 0, 0));
        assert_eq!(
            app.subagent_backfill_progress
                .as_ref()
                .map(
                    super::super::session_lifecycle::LoadedSubagentBackfillProgress::retained_relation_cursor_count,
                ),
            Some(crate::app::loaded_threads::MAX_RETAINED_SUBAGENT_LINEAGE)
        );

        let second = app.backfill_loaded_subagent_threads(&mut app_server).await;
        assert!(second.completed);
        assert_eq!(
            second.status,
            crate::app::session_lifecycle::LoadedSubagentBackfillStatus::Truncated
        );
        assert_eq!(take_backfill_counts(&requests), (0, 0, 0));
        assert_eq!(
            app.subagent_backfill_progress
                .as_ref()
                .map(
                    super::super::session_lifecycle::LoadedSubagentBackfillProgress::retained_relation_cursor_count,
                ),
            Some(crate::app::loaded_threads::MAX_RETAINED_SUBAGENT_LINEAGE)
        );
        assert!(responses.lock().expect("lineage response lock").is_empty());

        app_server.shutdown().await?;
        proxy.await??;
        Ok(())
    })
}

#[test]
fn reset_clears_paused_lineage_continuation() -> Result<()> {
    run_large_stack_app_test(|| async {
        const PAGE_COUNT: usize = crate::app::loaded_threads::SUBAGENT_BACKFILL_PAGES_PER_ATTEMPT;
        let mut app = make_test_app().await;
        let primary_thread_id = ThreadId::new();
        configure_backfill_primary(&mut app, primary_thread_id);
        let responses = Arc::new(Mutex::new(VecDeque::from_iter((0..PAGE_COUNT).map(
            |index| {
                ScriptedLineageResponse::Page(scripted_lineage_page(
                    Vec::new(),
                    Some(format!("page-{}", index + 1)),
                ))
            },
        ))));
        let (mut app_server, _requests, proxy) = start_recording_app_server_with_lineage(
            &app.config,
            /*blocked_thread_read_id*/ None,
            Some(Arc::clone(&responses)),
            /*thread_read_responses*/ None,
        )
        .await?;

        let result = app.backfill_loaded_subagent_threads(&mut app_server).await;
        assert_eq!(
            result.status,
            crate::app::session_lifecycle::LoadedSubagentBackfillStatus::Paused
        );
        assert!(app.subagent_backfill_progress.is_some());
        app.reset_thread_event_state();
        assert!(app.subagent_backfill_progress.is_none());

        app_server.shutdown().await?;
        proxy.await??;
        Ok(())
    })
}

#[test]
fn fresh_session_applies_requested_name() -> Result<()> {
    const TEST_STACK_SIZE_BYTES: usize = 8 * 1024 * 1024;

    std::thread::Builder::new()
        .name("tui-named-fresh-session".to_string())
        .stack_size(TEST_STACK_SIZE_BYTES)
        .spawn(|| {
            let runtime = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()?;
            runtime.block_on(async {
                let mut app = make_test_app().await;
                let codex_home = tempdir()?;
                app.config.codex_home = codex_home.path().to_path_buf().abs();
                app.config.sqlite = SqliteConfig::new_for_testing(codex_home.path().abs());
                let (mut app_server, requests, proxy) =
                    start_recording_app_server(&app.config, /*blocked_thread_read_id*/ None)
                        .await?;
                let mut tui = crate::tui::test_support::make_test_tui()?;

                app.start_fresh_session_with_summary_hint(
                    &mut tui,
                    &mut app_server,
                    /*session_start_source*/ None,
                    /*initial_user_message*/ None,
                    /*new_thread_name*/ Some("Add User".to_string()),
                )
                .await;

                let thread_id = app
                    .chat_widget
                    .thread_id()
                    .expect("fresh session should have a thread id");
                assert_eq!(app.chat_widget.thread_name(), Some("Add User".to_string()));
                assert!(
                    requests
                        .lock()
                        .expect("request recorder lock")
                        .iter()
                        .any(|request| request.method == "thread/name/set"),
                    "fresh session should be named through the app server"
                );
                let thread = app_server
                    .thread_read(thread_id, /*include_turns*/ false)
                    .await?;
                assert_eq!(thread.name.as_deref(), Some("Add User"));

                app_server.shutdown().await?;
                proxy.await??;
                Ok(())
            })
        })?
        .join()
        .expect("named fresh session test thread")
}

#[test]
fn session_lifecycle_avoids_redundant_subagent_metadata_reads() -> Result<()> {
    const TEST_STACK_SIZE_BYTES: usize = 8 * 1024 * 1024;

    std::thread::Builder::new()
        .name("tui-session-lifecycle-requests".to_string())
        .stack_size(TEST_STACK_SIZE_BYTES)
        .spawn(|| {
            let runtime = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()?;
            runtime.block_on(async {
                let mut app = make_test_app().await;
                let codex_home = tempdir()?;
                app.config.codex_home = codex_home.path().to_path_buf().abs();
                app.config.sqlite =
                    codex_state::SqliteConfig::new_for_testing(codex_home.path().abs());
                let root_timestamp = "2026-01-01T00-00-00";
                let root_thread_id = ThreadId::from_string(
                    &create_fake_rollout(
                        codex_home.path(),
                        root_timestamp,
                        "2026-01-01T00:00:00Z",
                        "Saved user message",
                        Some(app.config.model_provider_id.as_str()),
                        /*git_info*/ None,
                    )
                    .expect("create root rollout"),
                )?;
                let child_thread_id = ThreadId::from_string(
                    &create_fake_parented_rollout_with_source(
                        codex_home.path(),
                        "2026-01-01T00-00-01",
                        "2026-01-01T00:00:01Z",
                        "Saved child message",
                        Some(app.config.model_provider_id.as_str()),
                        /*git_info*/ None,
                        RolloutSessionSource::SubAgent(SubAgentSource::ThreadSpawn {
                            parent_thread_id: root_thread_id,
                            depth: 1,
                            agent_path: Some(
                                AgentPath::try_from("/root/worker").expect("valid agent path"),
                            ),
                            agent_nickname: Some("worker".to_string()),
                            agent_role: Some("worker".to_string()),
                        }),
                        root_thread_id.into(),
                        root_thread_id,
                    )
                    .expect("create child rollout"),
                )?;
                let root_rollout_path = rollout_path(
                    codex_home.path(),
                    root_timestamp,
                    &root_thread_id.to_string(),
                );
                let (mut app_server, requests, proxy) =
                    start_recording_app_server(&app.config, /*blocked_thread_read_id*/ None)
                        .await?;
                let root = app_server
                    .resume_thread(
                        app.config.clone(),
                        root_thread_id,
                        app.resume_model_settings(),
                    )
                    .await?;
                app.enqueue_primary_thread_session(root.session, root.turns)
                    .await?;
                app_server
                    .resume_thread(
                        app.config.clone(),
                        child_thread_id,
                        app.resume_model_settings(),
                    )
                    .await?;
                let mut tui = crate::tui::test_support::make_test_tui()?;
                take_backfill_counts(&requests);

                let control = Box::pin(app.handle_event(
                    &mut tui,
                    &mut app_server,
                    AppEvent::ForkCurrentSession,
                ))
                .await?;

                assert!(matches!(control, AppRunControl::Continue));
                assert_ne!(app.chat_widget.thread_id(), Some(root_thread_id));
                // Forking may read the source metadata once when the response includes its parent
                // id. It must not scan or backfill loaded threads for the newly created fork.
                assert!(matches!(
                    take_backfill_counts(&requests),
                    (0, 0, 0) | (0, 0, 1)
                ));

                app.start_fresh_session_with_summary_hint(
                    &mut tui,
                    &mut app_server,
                    /*session_start_source*/ None,
                    /*initial_user_message*/ None,
                    /*new_thread_name*/ None,
                )
                .await;

                assert_ne!(app.chat_widget.thread_id(), Some(root_thread_id));
                assert_eq!(take_backfill_counts(&requests), (0, 0, 0));
                take_backfill_counts(&requests);
                app.harness_overrides.cwd = Some(app.config.cwd.to_path_buf());

                let control = app
                    .resume_target_session(
                        &mut tui,
                        &mut app_server,
                        crate::resume_picker::SessionTarget {
                            path: Some(root_rollout_path),
                            thread_id: root_thread_id,
                        },
                    )
                    .await?;

                assert!(matches!(control, AppRunControl::Continue));
                assert_eq!(app.chat_widget.thread_id(), Some(root_thread_id));
                assert_eq!(take_backfill_counts(&requests), (1, 0, 0));
                assert_eq!(
                    app.agent_navigation.get(&child_thread_id),
                    Some(&AgentPickerThreadEntry {
                        agent_nickname: Some("worker".to_string()),
                        agent_role: Some("worker".to_string()),
                        agent_path: Some("/root/worker".to_string()),
                        is_running: false,
                        is_closed: false,
                        created_at: None,
                        updated_at: None,
                        ..AgentPickerThreadEntry::default()
                    })
                );

                Box::pin(app.open_agent_picker(&mut app_server)).await;

                // The picker refreshes the primary thread once. Discovered children were already
                // refreshed by the picker's initial backfill and must not be read a second time.
                assert_eq!(take_backfill_counts(&requests), (1, 0, 1));
                app_server.shutdown().await?;
                proxy.await??;
                Ok(())
            })
        })?
        .join()
        .expect("session lifecycle request test thread")
}

#[test]
fn open_agent_picker_bounds_metadata_to_primary_lineage() -> Result<()> {
    const TEST_STACK_SIZE_BYTES: usize = 8 * 1024 * 1024;
    const UNRELATED_THREAD_COUNT: usize = 128;

    std::thread::Builder::new()
        .name("tui-agent-picker-lineage-bound".to_string())
        .stack_size(TEST_STACK_SIZE_BYTES)
        .spawn(|| {
            let runtime = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()?;
            runtime.block_on(async {
                let mut app = make_test_app().await;
                let codex_home = tempdir()?;
                app.config.codex_home = codex_home.path().to_path_buf().abs();
                app.config.sqlite = SqliteConfig::new_for_testing(codex_home.path().abs());
                let root_thread_id = ThreadId::from_string(
                    &create_fake_rollout(
                        codex_home.path(),
                        "2026-01-01T00-00-00",
                        "2026-01-01T00:00:00Z",
                        "Primary thread",
                        Some(app.config.model_provider_id.as_str()),
                        /*git_info*/ None,
                    )
                    .expect("create root rollout"),
                )?;
                let child_thread_id = ThreadId::from_string(
                    &create_fake_parented_rollout_with_source(
                        codex_home.path(),
                        "2026-01-01T00-00-01",
                        "2026-01-01T00:00:01Z",
                        "Descendant thread",
                        Some(app.config.model_provider_id.as_str()),
                        /*git_info*/ None,
                        RolloutSessionSource::SubAgent(SubAgentSource::ThreadSpawn {
                            parent_thread_id: root_thread_id,
                            depth: 1,
                            agent_path: Some(
                                AgentPath::try_from("/root/worker").expect("valid agent path"),
                            ),
                            agent_nickname: Some("worker".to_string()),
                            agent_role: Some("worker".to_string()),
                        }),
                        root_thread_id.into(),
                        root_thread_id,
                    )
                    .expect("create child rollout"),
                )?;
                let mut unrelated_thread_ids = Vec::with_capacity(UNRELATED_THREAD_COUNT);
                for index in 0..UNRELATED_THREAD_COUNT {
                    let minute = index / 60;
                    let second = index % 60;
                    let rollout_timestamp = format!("2026-01-02T00-{minute:02}-{second:02}");
                    let created_at = format!("2026-01-02T00:{minute:02}:{second:02}Z");
                    unrelated_thread_ids.push(ThreadId::from_string(
                        &create_fake_rollout(
                            codex_home.path(),
                            &rollout_timestamp,
                            &created_at,
                            "Unrelated thread",
                            Some(app.config.model_provider_id.as_str()),
                            /*git_info*/ None,
                        )
                        .expect("create unrelated rollout"),
                    )?);
                }

                let blocked_thread_read_id = unrelated_thread_ids[0];
                let (mut app_server, requests, proxy) =
                    start_recording_app_server(&app.config, Some(blocked_thread_read_id)).await?;
                let root = app_server
                    .resume_thread(
                        app.config.clone(),
                        root_thread_id,
                        app.resume_model_settings(),
                    )
                    .await?;
                app.enqueue_primary_thread_session(root.session, root.turns)
                    .await?;
                app_server
                    .resume_thread(
                        app.config.clone(),
                        child_thread_id,
                        app.resume_model_settings(),
                    )
                    .await?;
                for thread_id in unrelated_thread_ids {
                    app_server
                        .resume_thread(app.config.clone(), thread_id, app.resume_model_settings())
                        .await?;
                }
                let mut tui = crate::tui::test_support::make_test_tui()?;
                take_recorded_requests(&requests);

                let picker_result = tokio::time::timeout(
                    Duration::from_secs(10),
                    Box::pin(app.handle_event(
                        &mut tui,
                        &mut app_server,
                        AppEvent::OpenAgentPicker,
                    )),
                )
                .await;
                let control = match picker_result {
                    Ok(result) => result?,
                    Err(_) => {
                        proxy.abort();
                        return Err(color_eyre::eyre::eyre!(
                            "agent picker waited for an unrelated thread/read"
                        ));
                    }
                };

                assert!(matches!(control, AppRunControl::Continue));
                assert_eq!(
                    app.agent_navigation.get(&child_thread_id),
                    Some(&AgentPickerThreadEntry {
                        agent_nickname: Some("worker".to_string()),
                        agent_role: Some("worker".to_string()),
                        agent_path: Some("/root/worker".to_string()),
                        is_running: false,
                        is_closed: false,
                        created_at: None,
                        updated_at: None,
                        ..AgentPickerThreadEntry::default()
                    })
                );

                let recorded = take_recorded_requests(&requests);
                let lineage_requests: Vec<_> = recorded
                    .iter()
                    .filter(|request| request.method == "thread/list")
                    .collect();
                assert_eq!(lineage_requests.len(), 1);
                assert_eq!(
                    lineage_requests[0]
                        .params
                        .as_ref()
                        .and_then(|params| params.get("ancestorThreadId"))
                        .and_then(serde_json::Value::as_str),
                    Some(root_thread_id.to_string().as_str())
                );
                assert_eq!(
                    lineage_requests[0]
                        .params
                        .as_ref()
                        .and_then(|params| params.get("limit"))
                        .and_then(serde_json::Value::as_u64),
                    Some(u64::from(
                        crate::app::session_lifecycle::SUBAGENT_BACKFILL_PAGE_SIZE,
                    ))
                );
                assert_eq!(
                    lineage_requests[0]
                        .params
                        .as_ref()
                        .and_then(|params| params.get("useStateDbOnly"))
                        .and_then(serde_json::Value::as_bool),
                    Some(true)
                );
                let read_thread_ids: Vec<_> = recorded
                    .iter()
                    .filter(|request| request.method == "thread/read")
                    .filter_map(|request| request.params.as_ref())
                    .filter_map(|params| params.get("threadId"))
                    .filter_map(serde_json::Value::as_str)
                    .map(str::to_string)
                    .collect();
                assert_eq!(read_thread_ids, vec![root_thread_id.to_string()]);
                assert!(!read_thread_ids.contains(&blocked_thread_read_id.to_string()));

                app_server.shutdown().await?;
                proxy.await??;
                Ok(())
            })
        })?
        .join()
        .expect("agent picker lineage bound test thread")
}
