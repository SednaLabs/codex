//! Shared in-process app-server client facade for CLI surfaces.
//!
//! This crate wraps [`codex_app_server::in_process`] behind a single async API
//! used by surfaces like TUI and exec. It centralizes:
//!
//! - Runtime startup and initialize-capabilities handshake.
//! - Typed caller-provided startup identity (`SessionSource` + client name).
//! - Typed and raw request/notification dispatch.
//! - Server request resolution and rejection.
//! - Event consumption with backpressure signaling ([`InProcessServerEvent::Lagged`]).
//! - Bounded graceful shutdown with abort fallback.
//!
//! The facade interposes a worker task between the caller and the underlying
//! [`InProcessClientHandle`](codex_app_server::in_process::InProcessClientHandle),
//! bridging async `mpsc` channels on both sides. Queues are bounded so overload
//! surfaces as channel-full errors rather than unbounded memory growth.

mod path;
mod remote;

use std::error::Error;
use std::fmt;
use std::io::Error as IoError;
use std::io::ErrorKind;
use std::io::Result as IoResult;
use std::sync::Arc;
use std::time::Duration;

pub use codex_app_server::app_server_control_socket_path;
pub use codex_app_server::in_process::DEFAULT_IN_PROCESS_CHANNEL_CAPACITY;
pub use codex_app_server::in_process::InProcessServerEvent;
use codex_app_server::in_process::InProcessStartArgs;
use codex_app_server::in_process::LogDbLayer;
pub use codex_app_server::in_process::StateDbHandle;
use codex_app_server_protocol::ClientInfo;
use codex_app_server_protocol::ClientNotification;
use codex_app_server_protocol::ClientRequest;
use codex_app_server_protocol::ConfigWarningNotification;
use codex_app_server_protocol::InitializeCapabilities;
use codex_app_server_protocol::InitializeParams;
use codex_app_server_protocol::JSONRPCErrorError;
use codex_app_server_protocol::RequestId;
use codex_app_server_protocol::Result as JsonRpcResult;
use codex_app_server_protocol::ServerNotification;
use codex_app_server_protocol::ServerRequest;
use codex_arg0::Arg0DispatchPaths;
use codex_config::CloudConfigBundleLoader;
use codex_config::LoaderOverrides;
use codex_config::NoopThreadConfigLoader;
use codex_config::RemoteThreadConfigLoader;
use codex_config::ThreadConfigLoader;
use codex_core::config::Config;
pub use codex_core::otel_init::build_provider as build_otel_provider;
pub use codex_exec_server::EnvironmentManager;
pub use codex_exec_server::ExecServerRuntimePaths;
use codex_feedback::CodexFeedback;
use codex_protocol::protocol::SessionSource;
use codex_utils_absolute_path::AbsolutePathBuf;
use serde::de::DeserializeOwned;
use tokio::sync::mpsc;
use tokio::sync::oneshot;
use tokio::time::timeout;
use toml::Value as TomlValue;
use tracing::warn;

pub use crate::path::AppServerPath;
pub use crate::remote::RemoteAppServerClient;
pub use crate::remote::RemoteAppServerConnectArgs;
pub use crate::remote::RemoteAppServerEndpoint;

/// Transitional access to core-only embedded app-server types.
///
/// New TUI behavior should prefer the app-server protocol methods. This
/// module exists so clients can remove a direct `codex-core` dependency
/// while legacy startup/config paths are migrated to RPCs.
pub mod legacy_core {
    pub mod config {
        pub use codex_core::config::*;

        pub mod edit {
            pub use codex_core::config::edit::*;
        }
    }
}

const SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(5);
// Covers the embedded drain, its analytics flush, and final task join.
const IN_PROCESS_SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(45);

/// Raw app-server request result for typed in-process requests.
///
/// Even on the in-process path, successful responses still travel back through
/// the same JSON-RPC result envelope used by socket/stdio transports because
/// `MessageProcessor` continues to produce that shape internally.
pub type RequestResult = std::result::Result<JsonRpcResult, JSONRPCErrorError>;

#[derive(Debug, Clone)]
pub enum AppServerEvent {
    Lagged { skipped: usize },
    ServerNotification(ServerNotification),
    ServerRequest(ServerRequest),
    Disconnected { message: String },
}

impl From<InProcessServerEvent> for AppServerEvent {
    fn from(value: InProcessServerEvent) -> Self {
        match value {
            InProcessServerEvent::Lagged { skipped } => Self::Lagged { skipped },
            InProcessServerEvent::ServerNotification(notification) => {
                Self::ServerNotification(notification)
            }
            InProcessServerEvent::ServerRequest(request) => Self::ServerRequest(request),
        }
    }
}

fn event_requires_delivery(event: &InProcessServerEvent) -> bool {
    // These transcript and terminal events must remain lossless. Dropping
    // streamed assistant text or the authoritative completed item can leave
    // the TUI with permanently corrupted markdown, while dropping completion
    // notifications can leave surfaces waiting forever.
    match event {
        InProcessServerEvent::ServerNotification(notification) => {
            server_notification_requires_delivery(notification)
        }
        _ => false,
    }
}

/// Returns `true` for notifications that must survive backpressure.
///
/// Transcript events (`AgentMessageDelta`, `PlanDelta`, reasoning deltas) and
/// the authoritative `ItemCompleted` / `TurnCompleted` form the lossless tier
/// of the event stream. Dropping any of these corrupts the visible assistant
/// output or leaves surfaces waiting for a completion signal that already
/// fired. Everything else (`CommandExecutionOutputDelta`, progress, etc.) is
/// best-effort and may be dropped with only cosmetic impact.
///
/// Both the in-process and remote transports delegate to this function so the
/// classification stays in sync.
pub(crate) fn server_notification_requires_delivery(notification: &ServerNotification) -> bool {
    // This classifier intentionally follows the current downstream schema.
    // Upstream now has `ThreadQueueChanged` and an async delivery field on
    // `ItemCompleted`; those variants are absent downstream at this base, so
    // all current ItemCompleted notifications stay in the required tier.
    // Reconcile this predicate as part of the future protocol sync rather
    // than importing an upstream-only type into this bounded facade lane.
    matches!(
        notification,
        ServerNotification::TurnCompleted(_)
            | ServerNotification::ThreadSettingsUpdated(_)
            | ServerNotification::ItemCompleted(_)
            | ServerNotification::ExternalAgentConfigImportCompleted(_)
            | ServerNotification::AgentMessageDelta(_)
            | ServerNotification::PlanDelta(_)
            | ServerNotification::ReasoningSummaryTextDelta(_)
            | ServerNotification::ReasoningTextDelta(_)
    )
}

/// Outcome of attempting to forward a single event to the consumer channel.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ForwardEventResult {
    /// The event was delivered (or intentionally dropped); the stream is healthy.
    Continue,
    /// The consumer channel is closed; the caller should stop producing events.
    DisableStream,
}

/// Forwards a single in-process event to the consumer, respecting the
/// lossless/best-effort split.
///
/// Lossless events (transcript deltas, item/turn completions) block until the
/// consumer drains capacity. Best-effort events use `try_send` and increment
/// `skipped_events` on failure; a dropped lag marker contributes its contained
/// count so loss accounting remains cumulative across both bounded queues.
/// When a lag marker needs to be flushed before a lossless event, the flush
/// itself blocks so the marker is never lost.
///
async fn forward_in_process_event(
    event_tx: &mpsc::Sender<InProcessServerEvent>,
    skipped_events: &mut usize,
    event: InProcessServerEvent,
) -> ForwardEventResult {
    if *skipped_events > 0 {
        if event_requires_delivery(&event)
            || matches!(&event, InProcessServerEvent::ServerRequest(_))
        {
            // Surface lag before the lossless event, but do not let the lag marker itself cause
            // us to drop the transcript/completion notification the caller is blocked on.
            if event_tx
                .send(InProcessServerEvent::Lagged {
                    skipped: *skipped_events,
                })
                .await
                .is_err()
            {
                return ForwardEventResult::DisableStream;
            }
            *skipped_events = 0;
        } else {
            match event_tx.try_send(InProcessServerEvent::Lagged {
                skipped: *skipped_events,
            }) {
                Ok(()) => {
                    *skipped_events = 0;
                }
                Err(mpsc::error::TrySendError::Full(_)) => {
                    *skipped_events = skipped_events.saturating_add(skipped_event_count(&event));
                    warn!("dropping in-process app-server event because consumer queue is full");
                    return ForwardEventResult::Continue;
                }
                Err(mpsc::error::TrySendError::Closed(_)) => {
                    return ForwardEventResult::DisableStream;
                }
            }
        }
    }

    if event_requires_delivery(&event) || matches!(&event, InProcessServerEvent::ServerRequest(_)) {
        // Block until the consumer catches up for transcript/completion notifications; this
        // preserves the visible assistant output even when the queue is otherwise saturated.
        if event_tx.send(event).await.is_err() {
            return ForwardEventResult::DisableStream;
        }
        return ForwardEventResult::Continue;
    }

    match event_tx.try_send(event) {
        Ok(()) => ForwardEventResult::Continue,
        Err(mpsc::error::TrySendError::Full(event)) => {
            *skipped_events = skipped_events.saturating_add(skipped_event_count(&event));
            warn!("dropping in-process app-server event because consumer queue is full");
            debug_assert!(
                !matches!(event, InProcessServerEvent::ServerRequest(_)),
                "server requests must use the lossless path"
            );
            ForwardEventResult::Continue
        }
        Err(mpsc::error::TrySendError::Closed(_)) => ForwardEventResult::DisableStream,
    }
}

fn skipped_event_count(event: &InProcessServerEvent) -> usize {
    match event {
        InProcessServerEvent::Lagged { skipped } => *skipped,
        _ => 1,
    }
}

#[derive(Debug, Clone, Copy)]
enum FacadeTerminalReason {
    ConsumerClosed,
}

async fn reject_unsupported_in_process_server_request(
    request_sender: &codex_app_server::in_process::InProcessClientSender,
    request: &ServerRequest,
) -> IoResult<()> {
    timeout(
        SHUTDOWN_TIMEOUT,
        request_sender.fail_server_request_when_ready(
            request.id().clone(),
            JSONRPCErrorError {
                code: -32000,
                message:
                    "chatgpt auth token refresh is not supported for in-process app-server clients"
                        .to_string(),
                data: None,
            },
        ),
    )
    .await
    .map_err(|_| {
        IoError::new(
            ErrorKind::TimedOut,
            "timed out waiting to reject unsupported in-process server request",
        )
    })?
}

/// Layered error for [`InProcessAppServerClient::request_typed`].
///
/// This keeps transport failures, server-side JSON-RPC failures, and response
/// decode failures distinct so callers can decide whether to retry, surface a
/// server error, or treat the response as an internal request/response mismatch.
#[derive(Debug)]
pub enum TypedRequestError {
    Transport {
        method: String,
        source: IoError,
    },
    Server {
        method: String,
        source: JSONRPCErrorError,
    },
    Deserialize {
        method: String,
        source: serde_json::Error,
    },
}

impl fmt::Display for TypedRequestError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Transport { method, source } => {
                write!(f, "{method} transport error: {source}")
            }
            Self::Server { method, source } => {
                write!(
                    f,
                    "{method} failed: {} (code {})",
                    source.message, source.code
                )?;
                if let Some(data) = source.data.as_ref() {
                    write!(f, ", data: {data}")?;
                }
                Ok(())
            }
            Self::Deserialize { method, source } => {
                write!(f, "{method} response decode error: {source}")
            }
        }
    }
}

impl Error for TypedRequestError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::Transport { source, .. } => Some(source),
            Self::Server { .. } => None,
            Self::Deserialize { source, .. } => Some(source),
        }
    }
}

#[derive(Clone)]
pub struct InProcessClientStartArgs {
    /// Resolved argv0 dispatch paths used by command execution internals.
    pub arg0_paths: Arg0DispatchPaths,
    /// Shared config used to initialize app-server runtime.
    pub config: Arc<Config>,
    /// CLI config overrides that are already parsed into TOML values.
    pub cli_overrides: Vec<(String, TomlValue)>,
    /// Loader override knobs used by config API paths.
    pub loader_overrides: LoaderOverrides,
    /// Whether config API paths should reject unknown config fields.
    pub strict_config: bool,
    /// Preloaded cloud config bundle provider.
    pub cloud_config_bundle: CloudConfigBundleLoader,
    /// Feedback sink used by app-server/core telemetry and logs.
    pub feedback: CodexFeedback,
    /// SQLite tracing layer used to flush recently emitted logs before feedback upload.
    pub log_db: Option<LogDbLayer>,
    /// Process-wide SQLite state handle shared with the embedded app-server.
    pub state_db: Option<StateDbHandle>,
    /// Environment manager used by core execution and filesystem operations.
    pub environment_manager: Arc<EnvironmentManager>,
    /// Startup warnings emitted after initialize succeeds.
    pub config_warnings: Vec<ConfigWarningNotification>,
    /// Session source recorded in app-server thread metadata.
    pub session_source: SessionSource,
    /// Whether auth loading should honor the `CODEX_API_KEY` environment variable.
    pub enable_codex_api_key_env: bool,
    /// Client name reported during initialize.
    pub client_name: String,
    /// Client version reported during initialize.
    pub client_version: String,
    /// Whether experimental APIs are requested at initialize time.
    pub experimental_api: bool,
    /// Whether MCP servers may send `openai/form` elicitation requests.
    pub mcp_server_openai_form_elicitation: bool,
    /// Notification methods this client opts out of receiving.
    pub opt_out_notification_methods: Vec<String>,
    /// Queue capacity for command/event channels (clamped to at least 1).
    pub channel_capacity: usize,
}

fn configured_thread_config_loader(config: &Config) -> Arc<dyn ThreadConfigLoader> {
    match config.experimental_thread_config_endpoint.as_deref() {
        Some(endpoint) => Arc::new(RemoteThreadConfigLoader::new(endpoint)),
        None => Arc::new(NoopThreadConfigLoader),
    }
}

impl InProcessClientStartArgs {
    /// Builds initialize params from caller-provided metadata.
    pub fn initialize_params(&self) -> InitializeParams {
        let capabilities = InitializeCapabilities {
            experimental_api: self.experimental_api,
            request_attestation: false,
            opt_out_notification_methods: if self.opt_out_notification_methods.is_empty() {
                None
            } else {
                Some(self.opt_out_notification_methods.clone())
            },
            mcp_server_openai_form_elicitation: self.mcp_server_openai_form_elicitation,
        };

        InitializeParams {
            client_info: ClientInfo {
                name: self.client_name.clone(),
                title: None,
                version: self.client_version.clone(),
            },
            capabilities: Some(capabilities),
        }
    }

    fn into_runtime_start_args(self) -> InProcessStartArgs {
        let initialize = self.initialize_params();
        let thread_config_loader = configured_thread_config_loader(&self.config);
        InProcessStartArgs {
            arg0_paths: self.arg0_paths,
            config: self.config,
            cli_overrides: self.cli_overrides,
            loader_overrides: self.loader_overrides,
            strict_config: self.strict_config,
            cloud_config_bundle: self.cloud_config_bundle,
            thread_config_loader,
            feedback: self.feedback,
            log_db: self.log_db,
            state_db: self.state_db,
            environment_manager: self.environment_manager,
            config_warnings: self.config_warnings,
            session_source: self.session_source,
            enable_codex_api_key_env: self.enable_codex_api_key_env,
            initialize,
            channel_capacity: self.channel_capacity,
        }
    }
}

/// Internal command sent from public facade methods to the worker task.
///
/// Each variant carries a oneshot sender so the caller can `await` the
/// result without holding a mutable reference to the client.
enum ClientCommand {
    Request {
        request: Box<ClientRequest>,
        response_tx: oneshot::Sender<IoResult<RequestResult>>,
    },
    Notify {
        notification: ClientNotification,
        response_tx: oneshot::Sender<IoResult<()>>,
    },
    ResolveServerRequest {
        request_id: RequestId,
        result: JsonRpcResult,
        response_tx: oneshot::Sender<IoResult<()>>,
    },
    RejectServerRequest {
        request_id: RequestId,
        error: JSONRPCErrorError,
        response_tx: oneshot::Sender<IoResult<()>>,
    },
    Shutdown {
        response_tx: oneshot::Sender<IoResult<()>>,
    },
}

/// Async facade over the in-process app-server runtime.
///
/// This type owns a worker task that bridges between:
/// - caller-facing async `mpsc` channels used by TUI/exec
/// - [`codex_app_server::in_process::InProcessClientHandle`], which speaks to
///   the embedded `MessageProcessor`
///
/// The facade intentionally preserves the server's request/notification/event
/// model instead of exposing direct core runtime handles. That keeps in-process
/// callers aligned with app-server behavior while still avoiding a process
/// boundary.
pub struct InProcessAppServerClient {
    command_tx: mpsc::Sender<ClientCommand>,
    event_rx: mpsc::Receiver<InProcessServerEvent>,
    worker_handle: tokio::task::JoinHandle<()>,
}

#[derive(Clone)]
pub struct InProcessAppServerRequestHandle {
    command_tx: mpsc::Sender<ClientCommand>,
}

#[derive(Clone)]
pub enum AppServerRequestHandle {
    InProcess(InProcessAppServerRequestHandle),
    Remote(crate::remote::RemoteAppServerRequestHandle),
}

pub enum AppServerClient {
    InProcess(InProcessAppServerClient),
    Remote(RemoteAppServerClient),
}

impl InProcessAppServerClient {
    /// Starts the in-process runtime and facade worker task.
    ///
    /// The returned client is ready for requests and event consumption.
    /// Required notifications and ordinary server requests retain FIFO custody
    /// after admission, even while the consumer queue is saturated.
    pub async fn start(args: InProcessClientStartArgs) -> IoResult<Self> {
        let channel_capacity = args.channel_capacity.max(1);
        let mut handle =
            codex_app_server::in_process::start(args.into_runtime_start_args()).await?;
        let request_sender = handle.sender();
        let (command_tx, mut command_rx) = mpsc::channel::<ClientCommand>(channel_capacity);
        let (event_tx, event_rx) = mpsc::channel::<InProcessServerEvent>(channel_capacity);

        // Keep delivery to the public event receiver in a dedicated bounded
        // sequencer. A stalled lossless event must not prevent the facade
        // worker from accepting cancellation, resolution, or shutdown
        // commands, and the single sender preserves source FIFO.
        let (event_forward_tx, mut event_forward_rx) =
            mpsc::channel::<InProcessServerEvent>(channel_capacity);
        let (event_terminal_tx, mut event_terminal_rx) = mpsc::channel::<FacadeTerminalReason>(1);
        let event_tx_forward = event_tx;
        let event_terminal_tx_forward = event_terminal_tx.clone();
        let mut event_forward_handle = tokio::spawn(async move {
            let mut skipped_events = 0usize;
            loop {
                let event = tokio::select! {
                    _ = event_tx_forward.closed() => {
                        let _ = event_terminal_tx_forward
                            .try_send(FacadeTerminalReason::ConsumerClosed);
                        break;
                    }
                    event = event_forward_rx.recv() => {
                        let Some(event) = event else { break; };
                        event
                    }
                };
                match forward_in_process_event(&event_tx_forward, &mut skipped_events, event).await
                {
                    ForwardEventResult::Continue => {}
                    ForwardEventResult::DisableStream => {
                        let _ = event_terminal_tx_forward
                            .try_send(FacadeTerminalReason::ConsumerClosed);
                        break;
                    }
                }
            }
        });
        let (auth_rejection_tx, mut auth_rejection_rx) = mpsc::channel::<IoResult<()>>(1);

        let worker_handle = tokio::spawn(async move {
            // Keep the terminal sender alive so a normal forwarder exit does
            // not look like a terminal reason.
            let _event_terminal_tx = event_terminal_tx;
            let mut pending_event = None;
            let mut auth_rejection_pending = false;
            let mut auth_rejection_handle = None;
            loop {
                tokio::select! {
                    terminal_reason = event_terminal_rx.recv() => {
                        if matches!(terminal_reason, Some(FacadeTerminalReason::ConsumerClosed)) {
                            let _ = handle.shutdown().await;
                            break;
                        }
                    }
                    rejection = auth_rejection_rx.recv(), if auth_rejection_pending => {
                        auth_rejection_pending = false;
                        if let Some(auth_rejection_handle) = auth_rejection_handle.take() {
                            let _ = auth_rejection_handle.await;
                        }
                        match rejection {
                            Some(Ok(())) => {}
                            Some(Err(error)) => {
                                warn!("terminating in-process facade after auth rejection failed: {error}");
                                let _ = handle.shutdown().await;
                                break;
                            }
                            None => {
                                warn!("terminating in-process facade after auth rejection task closed");
                                let _ = handle.shutdown().await;
                                break;
                            }
                        }
                    }
                    permit = event_forward_tx.reserve(), if pending_event.is_some() => {
                        match permit {
                            Ok(permit) => {
                                let Some(event) = pending_event.take() else {
                                    break;
                                };
                                permit.send(event);
                            }
                            Err(_) => {
                                let _ = handle.shutdown().await;
                                break;
                            }
                        }
                    }
                    command = command_rx.recv() => {
                        match command {
                            Some(ClientCommand::Request { request, response_tx }) => {
                                let request_sender = request_sender.clone();
                                // Request waits happen on a detached task so
                                // this loop can keep draining runtime events
                                // while the request is blocked on client input.
                                tokio::spawn(async move {
                                    let result = request_sender.request(*request).await;
                                    let _ = response_tx.send(result);
                                });
                            }
                            Some(ClientCommand::Notify {
                                notification,
                                response_tx,
                            }) => {
                                let result = request_sender.notify(notification);
                                let _ = response_tx.send(result);
                            }
                            Some(ClientCommand::ResolveServerRequest {
                                request_id,
                                result,
                                response_tx,
                            }) => {
                                let send_result =
                                    request_sender.respond_to_server_request(request_id, result);
                                let _ = response_tx.send(send_result);
                            }
                            Some(ClientCommand::RejectServerRequest {
                                request_id,
                                error,
                                response_tx,
                            }) => {
                                let send_result = request_sender.fail_server_request(request_id, error);
                                let _ = response_tx.send(send_result);
                            }
                            Some(ClientCommand::Shutdown { response_tx }) => {
                                let shutdown_result = handle.shutdown().await;
                                let _ = response_tx.send(shutdown_result);
                                break;
                            }
                            None => {
                                let _ = handle.shutdown().await;
                                break;
                            }
                        }
                    }
                    event = handle.next_event(), if pending_event.is_none() && !auth_rejection_pending => {
                        let Some(event) = event else {
                            break;
                        };
                        if matches!(
                            &event,
                            InProcessServerEvent::ServerRequest(
                                ServerRequest::ChatgptAuthTokensRefresh { .. }
                            )
                        ) {
                            let InProcessServerEvent::ServerRequest(request) = event else {
                                unreachable!("matched in-process server request above");
                            };
                            let rejection_sender = request_sender.clone();
                            let rejection_tx = auth_rejection_tx.clone();
                            auth_rejection_handle = Some(tokio::spawn(async move {
                                let result = reject_unsupported_in_process_server_request(
                                    &rejection_sender,
                                    &request,
                                )
                                .await;
                                let _ = rejection_tx.send(result).await;
                            }));
                            auth_rejection_pending = true;
                            continue;
                        }

                        match event_forward_tx.try_send(event) {
                            Ok(()) => {}
                            Err(mpsc::error::TrySendError::Full(event)) => {
                                // One ordered head may wait behind the bounded
                                // queue. This includes server requests, which
                                // must never be rejected for saturation.
                                pending_event = Some(event);
                            }
                            Err(mpsc::error::TrySendError::Closed(_)) => {
                                let _ = handle.shutdown().await;
                                break;
                            }
                        }
                    }
                }
            }

            drop(event_forward_tx);
            if let Some(mut auth_rejection_handle) = auth_rejection_handle
                && timeout(SHUTDOWN_TIMEOUT, &mut auth_rejection_handle)
                    .await
                    .is_err()
            {
                auth_rejection_handle.abort();
                let _ = auth_rejection_handle.await;
            }
            if let Err(_elapsed) = timeout(SHUTDOWN_TIMEOUT, &mut event_forward_handle).await {
                event_forward_handle.abort();
                let _ = event_forward_handle.await;
            }
        });

        Ok(Self {
            command_tx,
            event_rx,
            worker_handle,
        })
    }

    pub fn request_handle(&self) -> InProcessAppServerRequestHandle {
        InProcessAppServerRequestHandle {
            command_tx: self.command_tx.clone(),
        }
    }

    /// Sends a typed client request and returns raw JSON-RPC result.
    ///
    /// Callers that expect a concrete response type should usually prefer
    /// [`request_typed`](Self::request_typed).
    pub async fn request(&self, request: ClientRequest) -> IoResult<RequestResult> {
        let (response_tx, response_rx) = oneshot::channel();
        self.command_tx
            .send(ClientCommand::Request {
                request: Box::new(request),
                response_tx,
            })
            .await
            .map_err(|_| {
                IoError::new(
                    ErrorKind::BrokenPipe,
                    "in-process app-server worker channel is closed",
                )
            })?;
        response_rx.await.map_err(|_| {
            IoError::new(
                ErrorKind::BrokenPipe,
                "in-process app-server request channel is closed",
            )
        })?
    }

    /// Sends a typed client request and decodes the successful response body.
    ///
    /// This still deserializes from a JSON value produced by app-server's
    /// JSON-RPC result envelope. Because the caller chooses `T`, `Deserialize`
    /// failures indicate an internal request/response mismatch at the call site
    /// (or an in-process bug), not transport skew from an external client.
    pub async fn request_typed<T>(&self, request: ClientRequest) -> Result<T, TypedRequestError>
    where
        T: DeserializeOwned,
    {
        let method = request.method_name();
        let response =
            self.request(request)
                .await
                .map_err(|source| TypedRequestError::Transport {
                    method: method.to_string(),
                    source,
                })?;
        let result = response.map_err(|source| TypedRequestError::Server {
            method: method.to_string(),
            source,
        })?;
        serde_json::from_value(result).map_err(|source| TypedRequestError::Deserialize {
            method: method.to_string(),
            source,
        })
    }

    /// Sends a typed client notification.
    pub async fn notify(&self, notification: ClientNotification) -> IoResult<()> {
        let (response_tx, response_rx) = oneshot::channel();
        self.command_tx
            .send(ClientCommand::Notify {
                notification,
                response_tx,
            })
            .await
            .map_err(|_| {
                IoError::new(
                    ErrorKind::BrokenPipe,
                    "in-process app-server worker channel is closed",
                )
            })?;
        response_rx.await.map_err(|_| {
            IoError::new(
                ErrorKind::BrokenPipe,
                "in-process app-server notify channel is closed",
            )
        })?
    }

    /// Resolves a pending server request.
    ///
    /// This should only be called with request IDs obtained from the current
    /// client's event stream.
    pub async fn resolve_server_request(
        &self,
        request_id: RequestId,
        result: JsonRpcResult,
    ) -> IoResult<()> {
        let (response_tx, response_rx) = oneshot::channel();
        self.command_tx
            .send(ClientCommand::ResolveServerRequest {
                request_id,
                result,
                response_tx,
            })
            .await
            .map_err(|_| {
                IoError::new(
                    ErrorKind::BrokenPipe,
                    "in-process app-server worker channel is closed",
                )
            })?;
        response_rx.await.map_err(|_| {
            IoError::new(
                ErrorKind::BrokenPipe,
                "in-process app-server resolve channel is closed",
            )
        })?
    }

    /// Rejects a pending server request with JSON-RPC error payload.
    pub async fn reject_server_request(
        &self,
        request_id: RequestId,
        error: JSONRPCErrorError,
    ) -> IoResult<()> {
        let (response_tx, response_rx) = oneshot::channel();
        self.command_tx
            .send(ClientCommand::RejectServerRequest {
                request_id,
                error,
                response_tx,
            })
            .await
            .map_err(|_| {
                IoError::new(
                    ErrorKind::BrokenPipe,
                    "in-process app-server worker channel is closed",
                )
            })?;
        response_rx.await.map_err(|_| {
            IoError::new(
                ErrorKind::BrokenPipe,
                "in-process app-server reject channel is closed",
            )
        })?
    }

    /// Returns the next in-process event, or `None` when worker exits.
    ///
    /// Callers that fall behind receive [`InProcessServerEvent::Lagged`]
    /// markers for best-effort loss. Required notifications and server
    /// requests remain ordered and retained within the bounded delivery
    /// sequencer.
    pub async fn next_event(&mut self) -> Option<InProcessServerEvent> {
        self.event_rx.recv().await
    }

    /// Shuts down worker and in-process runtime with bounded wait.
    ///
    /// If graceful shutdown exceeds timeout, the worker task is aborted to
    /// avoid leaking background tasks in embedding callers.
    pub async fn shutdown(self) -> IoResult<()> {
        let Self {
            command_tx,
            event_rx,
            worker_handle,
        } = self;
        let mut worker_handle = worker_handle;
        let (response_tx, response_rx) = oneshot::channel();
        if command_tx
            .send(ClientCommand::Shutdown { response_tx })
            .await
            .is_ok()
            && let Ok(command_result) = timeout(IN_PROCESS_SHUTDOWN_TIMEOUT, response_rx).await
        {
            command_result.map_err(|_| {
                IoError::new(
                    ErrorKind::BrokenPipe,
                    "in-process app-server shutdown channel is closed",
                )
            })??;
        }
        // Release the caller-facing receiver only after the shutdown response
        // has been delivered. This prevents the consumer-closed path from
        // winning the race against an explicit `ClientCommand::Shutdown`.
        drop(event_rx);

        if let Err(_elapsed) = timeout(IN_PROCESS_SHUTDOWN_TIMEOUT, &mut worker_handle).await {
            worker_handle.abort();
            let _ = worker_handle.await;
        }
        Ok(())
    }
}

impl InProcessAppServerRequestHandle {
    pub async fn request(&self, request: ClientRequest) -> IoResult<RequestResult> {
        let (response_tx, response_rx) = oneshot::channel();
        self.command_tx
            .send(ClientCommand::Request {
                request: Box::new(request),
                response_tx,
            })
            .await
            .map_err(|_| {
                IoError::new(
                    ErrorKind::BrokenPipe,
                    "in-process app-server worker channel is closed",
                )
            })?;
        response_rx.await.map_err(|_| {
            IoError::new(
                ErrorKind::BrokenPipe,
                "in-process app-server request channel is closed",
            )
        })?
    }

    pub async fn request_typed<T>(&self, request: ClientRequest) -> Result<T, TypedRequestError>
    where
        T: DeserializeOwned,
    {
        let method = request.method_name();
        let response =
            self.request(request)
                .await
                .map_err(|source| TypedRequestError::Transport {
                    method: method.to_string(),
                    source,
                })?;
        let result = response.map_err(|source| TypedRequestError::Server {
            method: method.to_string(),
            source,
        })?;
        serde_json::from_value(result).map_err(|source| TypedRequestError::Deserialize {
            method: method.to_string(),
            source,
        })
    }
}

impl AppServerRequestHandle {
    pub async fn request(&self, request: ClientRequest) -> IoResult<RequestResult> {
        match self {
            Self::InProcess(handle) => handle.request(request).await,
            Self::Remote(handle) => handle.request(request).await,
        }
    }

    pub async fn request_typed<T>(&self, request: ClientRequest) -> Result<T, TypedRequestError>
    where
        T: DeserializeOwned,
    {
        match self {
            Self::InProcess(handle) => handle.request_typed(request).await,
            Self::Remote(handle) => handle.request_typed(request).await,
        }
    }
}

impl AppServerClient {
    pub fn codex_home(&self, local_codex_home: &AbsolutePathBuf) -> Option<AppServerPath> {
        match self {
            Self::InProcess(_) => Some(AppServerPath::from_app_server(
                local_codex_home.display().to_string(),
            )),
            Self::Remote(client) => client.codex_home().map(AppServerPath::from_app_server),
        }
    }

    pub async fn request(&self, request: ClientRequest) -> IoResult<RequestResult> {
        match self {
            Self::InProcess(client) => client.request(request).await,
            Self::Remote(client) => client.request(request).await,
        }
    }

    pub async fn request_typed<T>(&self, request: ClientRequest) -> Result<T, TypedRequestError>
    where
        T: DeserializeOwned,
    {
        match self {
            Self::InProcess(client) => client.request_typed(request).await,
            Self::Remote(client) => client.request_typed(request).await,
        }
    }

    pub async fn notify(&self, notification: ClientNotification) -> IoResult<()> {
        match self {
            Self::InProcess(client) => client.notify(notification).await,
            Self::Remote(client) => client.notify(notification).await,
        }
    }

    pub async fn resolve_server_request(
        &self,
        request_id: RequestId,
        result: JsonRpcResult,
    ) -> IoResult<()> {
        match self {
            Self::InProcess(client) => client.resolve_server_request(request_id, result).await,
            Self::Remote(client) => client.resolve_server_request(request_id, result).await,
        }
    }

    pub async fn reject_server_request(
        &self,
        request_id: RequestId,
        error: JSONRPCErrorError,
    ) -> IoResult<()> {
        match self {
            Self::InProcess(client) => client.reject_server_request(request_id, error).await,
            Self::Remote(client) => client.reject_server_request(request_id, error).await,
        }
    }

    pub async fn next_event(&mut self) -> Option<AppServerEvent> {
        match self {
            Self::InProcess(client) => client.next_event().await.map(Into::into),
            Self::Remote(client) => client.next_event().await,
        }
    }

    pub async fn shutdown(self) -> IoResult<()> {
        match self {
            Self::InProcess(client) => client.shutdown().await,
            Self::Remote(client) => client.shutdown().await,
        }
    }

    pub fn request_handle(&self) -> AppServerRequestHandle {
        match self {
            Self::InProcess(client) => AppServerRequestHandle::InProcess(client.request_handle()),
            Self::Remote(client) => AppServerRequestHandle::Remote(client.request_handle()),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use codex_app_server_protocol::AccountUpdatedNotification;
    use codex_app_server_protocol::ConfigRequirementsReadResponse;
    use codex_app_server_protocol::GetAccountResponse;
    use codex_app_server_protocol::JSONRPCMessage;
    use codex_app_server_protocol::JSONRPCRequest;
    use codex_app_server_protocol::JSONRPCResponse;
    use codex_app_server_protocol::ServerNotification;
    use codex_app_server_protocol::SessionSource as ApiSessionSource;
    use codex_app_server_protocol::ThreadSettingsUpdateParams;
    use codex_app_server_protocol::ThreadSettingsUpdateResponse;
    use codex_app_server_protocol::ThreadStartParams;
    use codex_app_server_protocol::ThreadStartResponse;
    use codex_app_server_protocol::ToolRequestUserInputParams;
    use codex_app_server_protocol::ToolRequestUserInputQuestion;
    use codex_core::config::ConfigBuilder;
    use codex_core::init_state_db;
    use codex_protocol::config_types::Personality;
    use codex_uds::UnixListener;
    use codex_utils_absolute_path::AbsolutePathBuf;
    use futures::SinkExt;
    use futures::StreamExt;
    use pretty_assertions::assert_eq;
    use std::ops::Deref;
    use std::ops::DerefMut;
    use std::path::Path;
    use tempfile::TempDir;
    use tokio::net::TcpListener;
    use tokio::time::Duration;
    use tokio::time::timeout;
    use tokio_tungstenite::accept_async;
    use tokio_tungstenite::accept_hdr_async;
    use tokio_tungstenite::tungstenite::Message;
    use tokio_tungstenite::tungstenite::handshake::server::Request as WebSocketRequest;
    use tokio_tungstenite::tungstenite::handshake::server::Response as WebSocketResponse;
    use tokio_tungstenite::tungstenite::http::header::AUTHORIZATION;

    async fn build_test_config() -> Config {
        match ConfigBuilder::default().build().await {
            Ok(config) => config,
            Err(_) => Config::load_default_with_cli_overrides(Vec::new())
                .await
                .expect("default config should load"),
        }
    }

    async fn build_test_config_for_codex_home(codex_home: &Path) -> Config {
        match ConfigBuilder::default()
            .codex_home(codex_home.to_path_buf())
            .build()
            .await
        {
            Ok(config) => config,
            Err(_) => Config::load_default_with_cli_overrides_for_codex_home(
                codex_home.to_path_buf(),
                Vec::new(),
            )
            .await
            .expect("default config should load"),
        }
    }

    struct TestClient {
        _codex_home: TempDir,
        client: InProcessAppServerClient,
    }

    impl Deref for TestClient {
        type Target = InProcessAppServerClient;

        fn deref(&self) -> &Self::Target {
            &self.client
        }
    }

    impl DerefMut for TestClient {
        fn deref_mut(&mut self) -> &mut Self::Target {
            &mut self.client
        }
    }

    impl TestClient {
        async fn shutdown(self) -> IoResult<()> {
            self.client.shutdown().await
        }
    }

    async fn start_test_client_with_capacity(
        session_source: SessionSource,
        channel_capacity: usize,
    ) -> TestClient {
        let codex_home = TempDir::new().expect("temp dir");
        let config = Arc::new(build_test_config_for_codex_home(codex_home.path()).await);
        let state_db = init_state_db(config.as_ref())
            .await
            .expect("state db should initialize for in-process test");
        let client = InProcessAppServerClient::start(InProcessClientStartArgs {
            arg0_paths: Arg0DispatchPaths::default(),
            config,
            cli_overrides: Vec::new(),
            loader_overrides: LoaderOverrides::default(),
            strict_config: false,
            cloud_config_bundle: CloudConfigBundleLoader::default(),
            feedback: CodexFeedback::new(),
            log_db: None,
            state_db: Some(state_db),
            environment_manager: Arc::new(EnvironmentManager::default_for_tests()),
            config_warnings: Vec::new(),
            session_source,
            enable_codex_api_key_env: false,
            client_name: "codex-app-server-client-test".to_string(),
            client_version: "0.0.0-test".to_string(),
            experimental_api: true,
            mcp_server_openai_form_elicitation: false,
            opt_out_notification_methods: Vec::new(),
            channel_capacity,
        })
        .await
        .expect("in-process app-server client should start");

        TestClient {
            _codex_home: codex_home,
            client,
        }
    }

    async fn start_test_client(session_source: SessionSource) -> TestClient {
        start_test_client_with_capacity(session_source, DEFAULT_IN_PROCESS_CHANNEL_CAPACITY).await
    }

    async fn start_test_remote_server<F, Fut>(handler: F) -> String
    where
        F: FnOnce(tokio_tungstenite::WebSocketStream<tokio::net::TcpStream>) -> Fut
            + Send
            + 'static,
        Fut: std::future::Future<Output = ()> + Send + 'static,
    {
        start_test_remote_server_with_auth(/*expected_auth_token*/ None, handler).await
    }

    async fn start_test_remote_server_with_auth<F, Fut>(
        expected_auth_token: Option<String>,
        handler: F,
    ) -> String
    where
        F: FnOnce(tokio_tungstenite::WebSocketStream<tokio::net::TcpStream>) -> Fut
            + Send
            + 'static,
        Fut: std::future::Future<Output = ()> + Send + 'static,
    {
        let listener = TcpListener::bind("127.0.0.1:0")
            .await
            .expect("listener should bind");
        let addr = listener.local_addr().expect("listener address");
        tokio::spawn(async move {
            let (stream, _) = listener.accept().await.expect("accept should succeed");
            let websocket = accept_hdr_async(
                stream,
                move |request: &WebSocketRequest, response: WebSocketResponse| {
                    let provided_auth_token = request
                        .headers()
                        .get(AUTHORIZATION)
                        .and_then(|value| value.to_str().ok())
                        .map(str::to_owned);
                    let expected_auth_token = expected_auth_token
                        .as_ref()
                        .map(|token| format!("Bearer {token}"));
                    assert_eq!(provided_auth_token, expected_auth_token);
                    Ok(response)
                },
            )
            .await
            .expect("websocket upgrade should succeed");
            handler(websocket).await;
        });
        format!("ws://{addr}")
    }

    async fn expect_remote_initialize<S>(websocket: &mut tokio_tungstenite::WebSocketStream<S>)
    where
        S: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin,
    {
        let JSONRPCMessage::Request(request) = read_websocket_message(websocket).await else {
            panic!("expected initialize request");
        };
        assert_eq!(request.method, "initialize");
        write_websocket_message(
            websocket,
            JSONRPCMessage::Response(JSONRPCResponse {
                id: request.id,
                result: serde_json::json!({
                    "userAgent": "codex_cli_rs/9.8.7-test (Test OS; x86_64) rust",
                    "codexHome": "/server/.codex",
                }),
            }),
        )
        .await;

        let JSONRPCMessage::Notification(notification) = read_websocket_message(websocket).await
        else {
            panic!("expected initialized notification");
        };
        assert_eq!(notification.method, "initialized");
    }

    async fn read_websocket_message<S>(
        websocket: &mut tokio_tungstenite::WebSocketStream<S>,
    ) -> JSONRPCMessage
    where
        S: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin,
    {
        loop {
            let frame = websocket
                .next()
                .await
                .expect("frame should be available")
                .expect("frame should decode");
            match frame {
                Message::Text(text) => {
                    return serde_json::from_str::<JSONRPCMessage>(&text)
                        .expect("text frame should be valid JSON-RPC");
                }
                Message::Binary(_) | Message::Ping(_) | Message::Pong(_) | Message::Frame(_) => {
                    continue;
                }
                Message::Close(_) => panic!("unexpected close frame"),
            }
        }
    }

    async fn assert_no_second_websocket_response<S>(
        websocket: &mut tokio_tungstenite::WebSocketStream<S>,
    ) where
        S: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin,
    {
        let _ = timeout(Duration::from_secs(2), async {
            loop {
                let Some(frame) = websocket.next().await else {
                    return;
                };
                let frame = frame.expect("frame should decode");
                match frame {
                    Message::Text(text) => {
                        let message = serde_json::from_str::<JSONRPCMessage>(&text)
                            .expect("text frame should be valid JSON-RPC");
                        assert!(
                            !matches!(
                                message,
                                JSONRPCMessage::Response(_) | JSONRPCMessage::Error(_)
                            ),
                            "unexpected second JSON-RPC response: {message:?}"
                        );
                    }
                    Message::Close(_) => return,
                    Message::Binary(_)
                    | Message::Ping(_)
                    | Message::Pong(_)
                    | Message::Frame(_) => {
                        continue;
                    }
                }
            }
        })
        .await;
    }

    async fn write_websocket_message<S>(
        websocket: &mut tokio_tungstenite::WebSocketStream<S>,
        message: JSONRPCMessage,
    ) where
        S: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin,
    {
        websocket
            .send(Message::Text(
                serde_json::to_string(&message)
                    .expect("message should serialize")
                    .into(),
            ))
            .await
            .expect("message should send");
    }

    fn command_execution_output_delta_notification(delta: &str) -> ServerNotification {
        ServerNotification::CommandExecutionOutputDelta(
            codex_app_server_protocol::CommandExecutionOutputDeltaNotification {
                thread_id: "thread".to_string(),
                turn_id: "turn".to_string(),
                item_id: "item".to_string(),
                delta: delta.to_string(),
            },
        )
    }

    fn remote_thread_closed_notification(thread_id: &str) -> ServerNotification {
        ServerNotification::ThreadClosed(codex_app_server_protocol::ThreadClosedNotification {
            thread_id: thread_id.to_string(),
        })
    }

    fn remote_get_account_request(request_id: i64) -> ClientRequest {
        ClientRequest::GetAccount {
            request_id: RequestId::Integer(request_id),
            params: codex_app_server_protocol::GetAccountParams {
                refresh_token: false,
            },
        }
    }

    fn agent_message_delta_notification(delta: &str) -> ServerNotification {
        ServerNotification::AgentMessageDelta(
            codex_app_server_protocol::AgentMessageDeltaNotification {
                thread_id: "thread".to_string(),
                turn_id: "turn".to_string(),
                item_id: "item".to_string(),
                delta: delta.to_string(),
            },
        )
    }

    fn item_completed_notification(text: &str) -> ServerNotification {
        ServerNotification::ItemCompleted(codex_app_server_protocol::ItemCompletedNotification {
            thread_id: "thread".to_string(),
            turn_id: "turn".to_string(),
            completed_at_ms: 0,
            item: codex_app_server_protocol::ThreadItem::AgentMessage {
                id: "item".to_string(),
                text: text.to_string(),
                phase: None,
                memory_citation: None,
            },
        })
    }

    fn turn_completed_notification() -> ServerNotification {
        ServerNotification::TurnCompleted(codex_app_server_protocol::TurnCompletedNotification {
            final_model: None,
            model_snapshot: None,
            thread_id: "thread".to_string(),
            turn: codex_app_server_protocol::Turn {
                id: "turn".to_string(),
                items_view: codex_app_server_protocol::TurnItemsView::Full,
                items: Vec::new(),
                status: codex_app_server_protocol::TurnStatus::Completed,
                error: None,
                started_at: None,
                completed_at: Some(0),
                duration_ms: Some(1),
            },
        })
    }

    fn test_remote_connect_args(websocket_url: String) -> RemoteAppServerConnectArgs {
        RemoteAppServerConnectArgs {
            endpoint: RemoteAppServerEndpoint::WebSocket {
                websocket_url,
                auth_token: None,
            },
            client_name: "codex-app-server-client-test".to_string(),
            client_version: "0.0.0-test".to_string(),
            experimental_api: true,
            mcp_server_openai_form_elicitation: false,
            opt_out_notification_methods: Vec::new(),
            channel_capacity: 8,
        }
    }

    fn remote_user_input_request(request_id: RequestId) -> JSONRPCRequest {
        JSONRPCRequest {
            id: request_id,
            method: "item/tool/requestUserInput".to_string(),
            params: Some(
                serde_json::to_value(ToolRequestUserInputParams {
                    thread_id: "thread-1".to_string(),
                    turn_id: "turn-1".to_string(),
                    item_id: "call-1".to_string(),
                    questions: vec![ToolRequestUserInputQuestion {
                        id: "question-1".to_string(),
                        header: "Mode".to_string(),
                        question: "Pick one".to_string(),
                        is_other: false,
                        is_secret: false,
                        options: Some(vec![]),
                    }],
                    is_blocking: true,
                    auto_resolution_ms: None,
                })
                .expect("params should serialize"),
            ),
            trace: None,
        }
    }

    #[test]
    fn remote_initialize_params_forward_openai_form_capability() {
        let mut args = test_remote_connect_args("ws://localhost/rpc".to_string());
        args.mcp_server_openai_form_elicitation = true;

        assert!(
            args.initialize_params()
                .capabilities
                .expect("initialize capabilities")
                .mcp_server_openai_form_elicitation
        );
    }

    #[tokio::test]
    async fn typed_request_roundtrip_works() {
        let client = start_test_client(SessionSource::Exec).await;
        let _response: ConfigRequirementsReadResponse = client
            .request_typed(ClientRequest::ConfigRequirementsRead {
                request_id: RequestId::Integer(1),
                params: None,
            })
            .await
            .expect("typed request should succeed");
        client.shutdown().await.expect("shutdown should complete");
    }

    #[tokio::test]
    async fn typed_request_reports_json_rpc_errors() {
        let client = start_test_client(SessionSource::Exec).await;
        let err = client
            .request_typed::<ConfigRequirementsReadResponse>(ClientRequest::ThreadRead {
                request_id: RequestId::Integer(99),
                params: codex_app_server_protocol::ThreadReadParams {
                    thread_id: "missing-thread".to_string(),
                    include_turns: false,
                },
            })
            .await
            .expect_err("missing thread should return a JSON-RPC error");
        assert!(
            err.to_string().starts_with("thread/read failed:"),
            "expected method-qualified JSON-RPC failure message"
        );
        client.shutdown().await.expect("shutdown should complete");
    }

    #[tokio::test]
    async fn caller_provided_session_source_is_applied() {
        for (session_source, expected_source) in [
            (SessionSource::Exec, ApiSessionSource::Exec),
            (SessionSource::Cli, ApiSessionSource::Cli),
        ] {
            let client = start_test_client(session_source).await;
            let parsed: ThreadStartResponse = client
                .request_typed(ClientRequest::ThreadStart {
                    request_id: RequestId::Integer(2),
                    params: ThreadStartParams {
                        ephemeral: Some(true),
                        ..ThreadStartParams::default()
                    },
                })
                .await
                .expect("thread/start should succeed");
            assert_eq!(parsed.thread.source, expected_source);
            client.shutdown().await.expect("shutdown should complete");
        }
    }

    #[tokio::test]
    async fn threads_started_via_app_server_are_visible_through_typed_requests() {
        let client = start_test_client(SessionSource::Cli).await;

        let response: ThreadStartResponse = client
            .request_typed(ClientRequest::ThreadStart {
                request_id: RequestId::Integer(3),
                params: ThreadStartParams {
                    ephemeral: Some(true),
                    ..ThreadStartParams::default()
                },
            })
            .await
            .expect("thread/start should succeed");
        let read = client
            .request_typed::<codex_app_server_protocol::ThreadReadResponse>(
                ClientRequest::ThreadRead {
                    request_id: RequestId::Integer(4),
                    params: codex_app_server_protocol::ThreadReadParams {
                        thread_id: response.thread.id.clone(),
                        include_turns: false,
                    },
                },
            )
            .await
            .expect("thread/read should return the newly started thread");
        assert_eq!(read.thread.id, response.thread.id);

        client.shutdown().await.expect("shutdown should complete");
    }

    #[tokio::test]
    async fn tiny_channel_capacity_still_supports_request_roundtrip() {
        let client =
            start_test_client_with_capacity(SessionSource::Exec, /*channel_capacity*/ 1).await;
        let _response: ConfigRequirementsReadResponse = client
            .request_typed(ClientRequest::ConfigRequirementsRead {
                request_id: RequestId::Integer(1),
                params: None,
            })
            .await
            .expect("typed request should succeed");
        client.shutdown().await.expect("shutdown should complete");
    }

    #[tokio::test]
    async fn closed_event_consumer_closes_request_handle() {
        let client =
            start_test_client_with_capacity(SessionSource::Exec, /*channel_capacity*/ 1).await;
        let request_handle = client.request_handle();
        let worker_closed = request_handle.command_tx.clone();
        drop(client);

        timeout(Duration::from_secs(2), worker_closed.closed())
            .await
            .expect("dropping the event consumer should stop the facade worker");
        let error = request_handle
            .request(ClientRequest::ConfigRequirementsRead {
                request_id: RequestId::Integer(11),
                params: None,
            })
            .await
            .expect_err("requests should fail after the event consumer closes");
        assert_eq!(error.kind(), ErrorKind::BrokenPipe);
    }

    #[tokio::test]
    async fn unread_required_notifications_do_not_block_requests() {
        let mut client =
            start_test_client_with_capacity(SessionSource::Cli, /*channel_capacity*/ 1).await;
        let thread: ThreadStartResponse = client
            .request_typed(ClientRequest::ThreadStart {
                request_id: RequestId::Integer(1),
                params: ThreadStartParams {
                    ephemeral: Some(true),
                    personality: Some(Personality::None),
                    ..ThreadStartParams::default()
                },
            })
            .await
            .expect("thread/start should succeed");
        let request_handle = client.request_handle();

        timeout(Duration::from_secs(2), async {
            for (index, personality) in [
                Personality::Friendly,
                Personality::Pragmatic,
                Personality::Friendly,
                Personality::Pragmatic,
            ]
            .into_iter()
            .enumerate()
            {
                let _: ThreadSettingsUpdateResponse = request_handle
                    .request_typed(ClientRequest::ThreadSettingsUpdate {
                        request_id: RequestId::Integer((index + 2) as i64),
                        params: ThreadSettingsUpdateParams {
                            thread_id: thread.thread.id.clone(),
                            personality: Some(personality),
                            ..ThreadSettingsUpdateParams::default()
                        },
                    })
                    .await
                    .expect("thread/settings/update should succeed");
            }

            let _: ConfigRequirementsReadResponse = request_handle
                .request_typed(ClientRequest::ConfigRequirementsRead {
                    request_id: RequestId::Integer(10),
                    params: None,
                })
                .await
                .expect("configuration request should succeed behind unread events");
        })
        .await
        .expect("unread required notifications must not block app-server requests");

        let mut personalities = Vec::new();
        timeout(Duration::from_secs(2), async {
            while personalities.len() < 4 {
                if let Some(InProcessServerEvent::ServerNotification(
                    ServerNotification::ThreadSettingsUpdated(notification),
                )) = client.next_event().await
                {
                    personalities.push(notification.thread_settings.personality);
                }
            }
        })
        .await
        .expect("queued settings notifications should remain readable");
        assert_eq!(
            personalities,
            vec![
                Some(Personality::Friendly),
                Some(Personality::Pragmatic),
                Some(Personality::Friendly),
                Some(Personality::Pragmatic),
            ]
        );

        client.shutdown().await.expect("shutdown should complete");
    }

    #[tokio::test]
    async fn forward_in_process_event_preserves_transcript_notifications_under_backpressure() {
        let (event_tx, mut event_rx) = mpsc::channel(1);
        event_tx
            .send(InProcessServerEvent::ServerNotification(
                command_execution_output_delta_notification("stdout-1"),
            ))
            .await
            .expect("initial event should enqueue");

        let mut skipped_events = 0usize;
        let result = forward_in_process_event(
            &event_tx,
            &mut skipped_events,
            InProcessServerEvent::ServerNotification(command_execution_output_delta_notification(
                "stdout-2",
            )),
        )
        .await;
        assert_eq!(result, ForwardEventResult::Continue);
        assert_eq!(skipped_events, 1);

        let receive_task = tokio::spawn(async move {
            let mut events = Vec::new();
            for _ in 0..5 {
                events.push(
                    timeout(Duration::from_secs(2), event_rx.recv())
                        .await
                        .expect("event should arrive before timeout")
                        .expect("event stream should stay open"),
                );
            }
            events
        });

        for notification in [
            agent_message_delta_notification("hello"),
            item_completed_notification("hello"),
            turn_completed_notification(),
        ] {
            let result = forward_in_process_event(
                &event_tx,
                &mut skipped_events,
                InProcessServerEvent::ServerNotification(notification),
            )
            .await;
            assert_eq!(result, ForwardEventResult::Continue);
        }
        assert_eq!(skipped_events, 0);

        let events = receive_task
            .await
            .expect("receiver task should join successfully");
        assert!(matches!(
            &events[0],
            InProcessServerEvent::ServerNotification(
                ServerNotification::CommandExecutionOutputDelta(notification)
            ) if notification.delta == "stdout-1"
        ));
        assert!(matches!(
            &events[1],
            InProcessServerEvent::Lagged { skipped: 1 }
        ));
        assert!(matches!(
            &events[2],
            InProcessServerEvent::ServerNotification(ServerNotification::AgentMessageDelta(
                notification
            )) if notification.delta == "hello"
        ));
        assert!(matches!(
            &events[3],
            InProcessServerEvent::ServerNotification(ServerNotification::ItemCompleted(
                notification
            )) if matches!(
                &notification.item,
                codex_app_server_protocol::ThreadItem::AgentMessage { text, .. } if text == "hello"
            )
        ));
        assert!(matches!(
            &events[4],
            InProcessServerEvent::ServerNotification(ServerNotification::TurnCompleted(
                notification
            )) if notification.turn.status == codex_app_server_protocol::TurnStatus::Completed
        ));
    }

    #[tokio::test]
    async fn forward_in_process_event_preserves_nested_lag_counts() {
        let (event_tx, _event_rx) = mpsc::channel(1);
        event_tx
            .send(InProcessServerEvent::Lagged { skipped: 3 })
            .await
            .expect("initial lag marker should enqueue");

        // The lower delivery layer can report a loss marker of its own. If
        // the facade queue is also full, retain that marker's full count.
        let mut skipped_events = 4;
        let result = forward_in_process_event(
            &event_tx,
            &mut skipped_events,
            InProcessServerEvent::Lagged { skipped: 5 },
        )
        .await;
        assert_eq!(result, ForwardEventResult::Continue);
        assert_eq!(skipped_events, 9);

        // A marker dropped without an earlier accumulated count must retain
        // its own count as well.
        let mut skipped_events = 0;
        let result = forward_in_process_event(
            &event_tx,
            &mut skipped_events,
            InProcessServerEvent::Lagged { skipped: 7 },
        )
        .await;
        assert_eq!(result, ForwardEventResult::Continue);
        assert_eq!(skipped_events, 7);
    }

    #[tokio::test]
    async fn remote_typed_request_roundtrip_works() {
        let websocket_url = start_test_remote_server(|mut websocket| async move {
            expect_remote_initialize(&mut websocket).await;
            let JSONRPCMessage::Request(request) = read_websocket_message(&mut websocket).await
            else {
                panic!("expected account/read request");
            };
            assert_eq!(request.method, "account/read");
            write_websocket_message(
                &mut websocket,
                JSONRPCMessage::Response(JSONRPCResponse {
                    id: request.id,
                    result: serde_json::to_value(GetAccountResponse {
                        account: None,
                        requires_openai_auth: false,
                    })
                    .expect("response should serialize"),
                }),
            )
            .await;
            websocket.close(None).await.expect("close should succeed");
        })
        .await;
        let client = RemoteAppServerClient::connect(test_remote_connect_args(websocket_url))
            .await
            .expect("remote client should connect");

        assert_eq!(client.server_version(), Some("9.8.7-test"));
        assert_eq!(client.codex_home(), Some("/server/.codex"));
        let response: GetAccountResponse = client
            .request_typed(ClientRequest::GetAccount {
                request_id: RequestId::Integer(1),
                params: codex_app_server_protocol::GetAccountParams {
                    refresh_token: false,
                },
            })
            .await
            .expect("typed request should succeed");
        assert_eq!(response.account, None);

        client.shutdown().await.expect("shutdown should complete");
    }

    #[tokio::test]
    async fn remote_unix_socket_typed_request_roundtrip_works() {
        let socket_dir = TempDir::new().expect("socket dir");
        let socket_path = AbsolutePathBuf::from_absolute_path(socket_dir.path().join("codex.sock"))
            .expect("socket path should resolve");
        let mut listener = UnixListener::bind(socket_path.as_path())
            .await
            .expect("listener should bind");
        tokio::spawn(async move {
            let stream = listener.accept().await.expect("accept should succeed");
            let mut websocket = accept_async(stream)
                .await
                .expect("websocket upgrade should succeed");
            expect_remote_initialize(&mut websocket).await;
            let JSONRPCMessage::Request(request) = read_websocket_message(&mut websocket).await
            else {
                panic!("expected account/read request");
            };
            assert_eq!(request.method, "account/read");
            write_websocket_message(
                &mut websocket,
                JSONRPCMessage::Response(JSONRPCResponse {
                    id: request.id,
                    result: serde_json::to_value(GetAccountResponse {
                        account: None,
                        requires_openai_auth: false,
                    })
                    .expect("response should serialize"),
                }),
            )
            .await;
            websocket.close(None).await.expect("close should succeed");
        });
        let client = RemoteAppServerClient::connect(RemoteAppServerConnectArgs {
            endpoint: RemoteAppServerEndpoint::UnixSocket { socket_path },
            client_name: "codex-app-server-client-test".to_string(),
            client_version: "0.0.0-test".to_string(),
            experimental_api: true,
            mcp_server_openai_form_elicitation: false,
            opt_out_notification_methods: Vec::new(),
            channel_capacity: 8,
        })
        .await
        .expect("remote client should connect");

        let response: GetAccountResponse = client
            .request_typed(ClientRequest::GetAccount {
                request_id: RequestId::Integer(1),
                params: codex_app_server_protocol::GetAccountParams {
                    refresh_token: false,
                },
            })
            .await
            .expect("typed request should succeed");
        assert_eq!(response.account, None);

        client.shutdown().await.expect("shutdown should complete");
    }

    #[tokio::test]
    async fn remote_typed_request_accepts_large_single_frame_response() {
        let padding = "x".repeat((17 << 20) + 1024);
        let websocket_url = start_test_remote_server(move |mut websocket| async move {
            expect_remote_initialize(&mut websocket).await;
            let JSONRPCMessage::Request(request) = read_websocket_message(&mut websocket).await
            else {
                panic!("expected account/read request");
            };
            assert_eq!(request.method, "account/read");
            write_websocket_message(
                &mut websocket,
                JSONRPCMessage::Response(JSONRPCResponse {
                    id: request.id,
                    result: serde_json::json!({
                        "account": null,
                        "requiresOpenaiAuth": false,
                        "padding": padding,
                    }),
                }),
            )
            .await;
            websocket.close(None).await.expect("close should succeed");
        })
        .await;
        let client = RemoteAppServerClient::connect(test_remote_connect_args(websocket_url))
            .await
            .expect("remote client should connect");

        let response: GetAccountResponse = client
            .request_typed(ClientRequest::GetAccount {
                request_id: RequestId::Integer(1),
                params: codex_app_server_protocol::GetAccountParams {
                    refresh_token: false,
                },
            })
            .await
            .expect("large typed request should succeed");
        assert_eq!(
            response,
            GetAccountResponse {
                account: None,
                requires_openai_auth: false,
            }
        );

        client.shutdown().await.expect("shutdown should complete");
    }

    #[tokio::test]
    async fn remote_connect_includes_auth_header_when_configured() {
        let auth_token = "remote-bearer-token".to_string();
        let websocket_url = start_test_remote_server_with_auth(
            Some(auth_token.clone()),
            |mut websocket| async move {
                expect_remote_initialize(&mut websocket).await;
                websocket.close(None).await.expect("close should succeed");
            },
        )
        .await;
        let client = RemoteAppServerClient::connect(RemoteAppServerConnectArgs {
            endpoint: RemoteAppServerEndpoint::WebSocket {
                websocket_url,
                auth_token: Some(auth_token),
            },
            client_name: "codex-app-server-client-test".to_string(),
            client_version: "0.0.0-test".to_string(),
            experimental_api: true,
            mcp_server_openai_form_elicitation: false,
            opt_out_notification_methods: Vec::new(),
            channel_capacity: 8,
        })
        .await
        .expect("remote client should connect");

        client.shutdown().await.expect("shutdown should complete");
    }

    #[tokio::test]
    async fn remote_connect_rejects_non_loopback_ws_when_auth_configured() {
        let result = RemoteAppServerClient::connect(RemoteAppServerConnectArgs {
            endpoint: RemoteAppServerEndpoint::WebSocket {
                websocket_url: "ws://example.com:4500".to_string(),
                auth_token: Some("remote-bearer-token".to_string()),
            },
            client_name: "codex-app-server-client-test".to_string(),
            client_version: "0.0.0-test".to_string(),
            experimental_api: true,
            mcp_server_openai_form_elicitation: false,
            opt_out_notification_methods: Vec::new(),
            channel_capacity: 8,
        })
        .await;
        let err = match result {
            Ok(_) => panic!("non-loopback ws should be rejected before connect"),
            Err(err) => err,
        };
        assert_eq!(err.kind(), ErrorKind::InvalidInput);
        assert!(
            err.to_string()
                .contains("remote auth tokens require `wss://` or loopback `ws://` URLs")
        );
    }

    #[test]
    fn remote_auth_token_transport_policy_allows_wss_and_loopback_ws() {
        assert!(crate::remote::websocket_url_supports_auth_token(
            &url::Url::parse("wss://example.com:443").expect("wss URL should parse")
        ));
        assert!(crate::remote::websocket_url_supports_auth_token(
            &url::Url::parse("ws://127.0.0.1:4500").expect("loopback ws URL should parse")
        ));
        assert!(!crate::remote::websocket_url_supports_auth_token(
            &url::Url::parse("ws://example.com:4500").expect("non-loopback ws URL should parse")
        ));
    }

    #[tokio::test]
    async fn remote_duplicate_request_id_keeps_original_waiter() {
        let (first_request_seen_tx, first_request_seen_rx) = tokio::sync::oneshot::channel();
        let websocket_url = start_test_remote_server(|mut websocket| async move {
            expect_remote_initialize(&mut websocket).await;
            let JSONRPCMessage::Request(request) = read_websocket_message(&mut websocket).await
            else {
                panic!("expected account/read request");
            };
            assert_eq!(request.method, "account/read");
            first_request_seen_tx
                .send(request.id.clone())
                .expect("request id should send");
            assert!(
                timeout(
                    Duration::from_millis(100),
                    read_websocket_message(&mut websocket)
                )
                .await
                .is_err(),
                "duplicate request should not be forwarded to the server"
            );
            write_websocket_message(
                &mut websocket,
                JSONRPCMessage::Response(JSONRPCResponse {
                    id: request.id,
                    result: serde_json::to_value(GetAccountResponse {
                        account: None,
                        requires_openai_auth: false,
                    })
                    .expect("response should serialize"),
                }),
            )
            .await;
            let _ = websocket.next().await;
        })
        .await;
        let client = RemoteAppServerClient::connect(test_remote_connect_args(websocket_url))
            .await
            .expect("remote client should connect");
        let first_request_handle = client.request_handle();
        let second_request_handle = first_request_handle.clone();

        let first_request = tokio::spawn(async move {
            first_request_handle
                .request_typed::<GetAccountResponse>(ClientRequest::GetAccount {
                    request_id: RequestId::Integer(1),
                    params: codex_app_server_protocol::GetAccountParams {
                        refresh_token: false,
                    },
                })
                .await
        });

        let first_request_id = first_request_seen_rx
            .await
            .expect("server should observe the first request");
        assert_eq!(first_request_id, RequestId::Integer(1));

        let second_err = second_request_handle
            .request_typed::<GetAccountResponse>(ClientRequest::GetAccount {
                request_id: RequestId::Integer(1),
                params: codex_app_server_protocol::GetAccountParams {
                    refresh_token: false,
                },
            })
            .await
            .expect_err("duplicate request id should be rejected");
        assert_eq!(
            second_err.to_string(),
            "account/read transport error: duplicate remote app-server request id `1`"
        );

        let first_response = first_request
            .await
            .expect("first request task should join")
            .expect("first request should succeed");
        assert_eq!(
            first_response,
            GetAccountResponse {
                account: None,
                requires_openai_auth: false,
            }
        );

        client.shutdown().await.expect("shutdown should complete");
    }

    #[tokio::test]
    async fn remote_notifications_arrive_over_websocket() {
        let websocket_url = start_test_remote_server(|mut websocket| async move {
            expect_remote_initialize(&mut websocket).await;
            write_websocket_message(
                &mut websocket,
                JSONRPCMessage::Notification(
                    serde_json::from_value(
                        serde_json::to_value(ServerNotification::AccountUpdated(
                            AccountUpdatedNotification {
                                auth_mode: None,
                                plan_type: None,
                            },
                        ))
                        .expect("notification should serialize"),
                    )
                    .expect("notification should convert to JSON-RPC"),
                ),
            )
            .await;
        })
        .await;
        let mut client = RemoteAppServerClient::connect(test_remote_connect_args(websocket_url))
            .await
            .expect("remote client should connect");

        let event = client.next_event().await.expect("event should arrive");
        assert!(matches!(
            event,
            AppServerEvent::ServerNotification(ServerNotification::AccountUpdated(_))
        ));

        client.shutdown().await.expect("shutdown should complete");
    }

    #[tokio::test]
    async fn remote_backpressure_preserves_transcript_notifications() {
        let (done_tx, done_rx) = tokio::sync::oneshot::channel();
        let websocket_url = start_test_remote_server(|mut websocket| async move {
            expect_remote_initialize(&mut websocket).await;
            for notification in [
                command_execution_output_delta_notification("stdout-1"),
                command_execution_output_delta_notification("stdout-2"),
                agent_message_delta_notification("hello"),
                item_completed_notification("hello"),
                turn_completed_notification(),
            ] {
                write_websocket_message(
                    &mut websocket,
                    JSONRPCMessage::Notification(
                        serde_json::from_value(
                            serde_json::to_value(notification)
                                .expect("notification should serialize"),
                        )
                        .expect("notification should convert to JSON-RPC"),
                    ),
                )
                .await;
            }
            let _ = done_rx.await;
        })
        .await;
        let mut client = RemoteAppServerClient::connect(RemoteAppServerConnectArgs {
            channel_capacity: 1,
            ..test_remote_connect_args(websocket_url)
        })
        .await
        .expect("remote client should connect");

        let first_event = timeout(Duration::from_secs(2), client.next_event())
            .await
            .expect("first event should arrive before timeout")
            .expect("event stream should stay open");
        assert!(matches!(
            first_event,
            AppServerEvent::ServerNotification(ServerNotification::CommandExecutionOutputDelta(
                notification
            )) if notification.delta == "stdout-1"
        ));

        let mut remaining_events = Vec::new();
        for _ in 0..4 {
            remaining_events.push(
                timeout(Duration::from_secs(2), client.next_event())
                    .await
                    .expect("event should arrive before timeout")
                    .expect("event stream should stay open"),
            );
        }

        let mut transcript_event_names = Vec::new();
        for event in &remaining_events {
            match event {
                AppServerEvent::Lagged { skipped: 1 } => {}
                AppServerEvent::ServerNotification(
                    ServerNotification::CommandExecutionOutputDelta(notification),
                ) if notification.delta == "stdout-2" => {}
                AppServerEvent::ServerNotification(ServerNotification::AgentMessageDelta(
                    notification,
                )) if notification.delta == "hello" => {
                    transcript_event_names.push("agent_message_delta");
                }
                AppServerEvent::ServerNotification(ServerNotification::ItemCompleted(
                    notification,
                )) if matches!(
                    &notification.item,
                    codex_app_server_protocol::ThreadItem::AgentMessage { text, .. } if text == "hello"
                ) =>
                {
                    transcript_event_names.push("item_completed");
                }
                AppServerEvent::ServerNotification(ServerNotification::TurnCompleted(
                    notification,
                )) if notification.turn.status
                    == codex_app_server_protocol::TurnStatus::Completed =>
                {
                    transcript_event_names.push("turn_completed");
                }
                _ => panic!("unexpected remaining event: {event:?}"),
            }
        }
        assert_eq!(
            transcript_event_names,
            vec!["agent_message_delta", "item_completed", "turn_completed"]
        );

        done_tx
            .send(())
            .expect("server completion signal should send");
        client.shutdown().await.expect("shutdown should complete");
    }

    #[tokio::test]
    async fn remote_server_request_resolution_roundtrip_works() {
        let websocket_url = start_test_remote_server(|mut websocket| async move {
            expect_remote_initialize(&mut websocket).await;
            let request_id = RequestId::String("srv-1".to_string());
            let server_request = JSONRPCRequest {
                id: request_id.clone(),
                method: "item/tool/requestUserInput".to_string(),
                params: Some(
                    serde_json::to_value(ToolRequestUserInputParams {
                        thread_id: "thread-1".to_string(),
                        turn_id: "turn-1".to_string(),
                        item_id: "call-1".to_string(),
                        questions: vec![ToolRequestUserInputQuestion {
                            id: "question-1".to_string(),
                            header: "Mode".to_string(),
                            question: "Pick one".to_string(),
                            is_other: false,
                            is_secret: false,
                            options: Some(vec![]),
                        }],
                        is_blocking: true,
                        auto_resolution_ms: None,
                    })
                    .expect("params should serialize"),
                ),
                trace: None,
            };
            write_websocket_message(&mut websocket, JSONRPCMessage::Request(server_request)).await;

            let JSONRPCMessage::Response(response) = read_websocket_message(&mut websocket).await
            else {
                panic!("expected server request response");
            };
            assert_eq!(response.id, request_id);
        })
        .await;
        let mut client = RemoteAppServerClient::connect(test_remote_connect_args(websocket_url))
            .await
            .expect("remote client should connect");

        let AppServerEvent::ServerRequest(request) = client
            .next_event()
            .await
            .expect("request event should arrive")
        else {
            panic!("expected server request event");
        };
        client
            .resolve_server_request(request.id().clone(), serde_json::json!({}))
            .await
            .expect("server request should resolve");

        client.shutdown().await.expect("shutdown should complete");
    }

    #[tokio::test]
    async fn remote_live_server_request_id_reuse_terminates_without_second_event_or_response() {
        let websocket_url = start_test_remote_server(|mut websocket| async move {
            expect_remote_initialize(&mut websocket).await;
            let request_id = RequestId::String("live-reuse".to_string());
            write_websocket_message(
                &mut websocket,
                JSONRPCMessage::Request(remote_user_input_request(request_id.clone())),
            )
            .await;
            let JSONRPCMessage::Response(response) = read_websocket_message(&mut websocket).await
            else {
                panic!("expected first server request response");
            };
            assert_eq!(response.id, request_id);
            write_websocket_message(
                &mut websocket,
                JSONRPCMessage::Request(remote_user_input_request(request_id)),
            )
            .await;
            assert_no_second_websocket_response(&mut websocket).await;
        })
        .await;
        let mut client = RemoteAppServerClient::connect(test_remote_connect_args(websocket_url))
            .await
            .expect("remote client should connect");
        let AppServerEvent::ServerRequest(request) = client
            .next_event()
            .await
            .expect("first request event should arrive")
        else {
            panic!("expected first server request event");
        };
        client
            .resolve_server_request(request.id().clone(), serde_json::json!({}))
            .await
            .expect("first server request should resolve");
        let Some(AppServerEvent::Disconnected { message }) =
            timeout(Duration::from_secs(2), client.next_event())
                .await
                .expect("duplicate request should terminate promptly")
        else {
            panic!("duplicate request should produce a disconnect event");
        };
        assert!(message.contains("duplicate inbound server request ID"));
        client.shutdown().await.expect("shutdown should complete");
    }

    #[tokio::test]
    async fn remote_initialize_server_request_id_reuse_terminates_without_second_response() {
        let websocket_url = start_test_remote_server(|mut websocket| async move {
            let JSONRPCMessage::Request(initialize) = read_websocket_message(&mut websocket).await
            else {
                panic!("expected initialize request");
            };
            let request_id = RequestId::String("initialize-reuse".to_string());
            write_websocket_message(
                &mut websocket,
                JSONRPCMessage::Request(remote_user_input_request(request_id.clone())),
            )
            .await;
            write_websocket_message(
                &mut websocket,
                JSONRPCMessage::Response(JSONRPCResponse {
                    id: initialize.id,
                    result: serde_json::json!({}),
                }),
            )
            .await;
            let JSONRPCMessage::Notification(notification) =
                read_websocket_message(&mut websocket).await
            else {
                panic!("expected initialized notification");
            };
            assert_eq!(notification.method, "initialized");
            let JSONRPCMessage::Response(response) = read_websocket_message(&mut websocket).await
            else {
                panic!("expected initialize-time server request response");
            };
            assert_eq!(response.id, request_id);
            write_websocket_message(
                &mut websocket,
                JSONRPCMessage::Request(remote_user_input_request(request_id)),
            )
            .await;
            assert_no_second_websocket_response(&mut websocket).await;
        })
        .await;
        let mut client = RemoteAppServerClient::connect(test_remote_connect_args(websocket_url))
            .await
            .expect("remote client should connect");
        let AppServerEvent::ServerRequest(request) = client
            .next_event()
            .await
            .expect("initialize-time request event should arrive")
        else {
            panic!("expected initialize-time server request event");
        };
        client
            .resolve_server_request(request.id().clone(), serde_json::json!({}))
            .await
            .expect("initialize-time server request should resolve");
        let Some(AppServerEvent::Disconnected { message }) =
            timeout(Duration::from_secs(2), client.next_event())
                .await
                .expect("duplicate request should terminate promptly")
        else {
            panic!("duplicate request should produce a disconnect event");
        };
        assert!(message.contains("duplicate inbound server request ID"));
        client.shutdown().await.expect("shutdown should complete");
    }

    #[tokio::test]
    async fn remote_server_request_double_resolution_is_rejected_without_second_response() {
        let websocket_url = start_test_remote_server(|mut websocket| async move {
            expect_remote_initialize(&mut websocket).await;
            let request_id = RequestId::String("double-resolve".to_string());
            write_websocket_message(
                &mut websocket,
                JSONRPCMessage::Request(remote_user_input_request(request_id.clone())),
            )
            .await;
            let JSONRPCMessage::Response(response) = read_websocket_message(&mut websocket).await
            else {
                panic!("expected server request response");
            };
            assert_eq!(response.id, request_id);
            assert!(
                timeout(
                    Duration::from_millis(200),
                    read_websocket_message(&mut websocket)
                )
                .await
                .is_err(),
                "double resolution must not write a second response"
            );
        })
        .await;
        let mut client = RemoteAppServerClient::connect(test_remote_connect_args(websocket_url))
            .await
            .expect("remote client should connect");
        let AppServerEvent::ServerRequest(request) = client
            .next_event()
            .await
            .expect("request event should arrive")
        else {
            panic!("expected server request event");
        };
        let request_id = request.id().clone();
        client
            .resolve_server_request(request_id.clone(), serde_json::json!({}))
            .await
            .expect("first resolution should succeed");
        let error = client
            .resolve_server_request(request_id, serde_json::json!({}))
            .await
            .expect_err("double resolution should be rejected");
        assert_eq!(error.kind(), ErrorKind::InvalidData);
        client.shutdown().await.expect("shutdown should complete");
    }

    #[tokio::test]
    async fn remote_inbound_server_request_ledger_exhaustion_fails_closed() {
        let websocket_url = start_test_remote_server(|mut websocket| async move {
            expect_remote_initialize(&mut websocket).await;
            // Admit and resolve each request before sending the next one.  If all five
            // frames are sent in one burst, the worker can observe the exhausting
            // request and close the connection before the client has written the
            // response for the fourth admitted request, making this test race with a
            // transport-level broken pipe instead of exercising the ledger boundary.
            for index in 0..4 {
                let request_id = RequestId::String(format!("bounded-{index}"));
                write_websocket_message(
                    &mut websocket,
                    JSONRPCMessage::Request(remote_user_input_request(request_id.clone())),
                )
                .await;
                let JSONRPCMessage::Response(response) =
                    read_websocket_message(&mut websocket).await
                else {
                    panic!("expected response for admitted server request");
                };
                assert_eq!(response.id, request_id);
            }

            write_websocket_message(
                &mut websocket,
                JSONRPCMessage::Request(remote_user_input_request(RequestId::String(
                    "bounded-4".to_string(),
                ))),
            )
            .await;
            let _ = timeout(Duration::from_secs(2), websocket.next()).await;
        })
        .await;
        let mut client = RemoteAppServerClient::connect(RemoteAppServerConnectArgs {
            channel_capacity: 1,
            ..test_remote_connect_args(websocket_url)
        })
        .await
        .expect("remote client should connect");
        for _ in 0..4 {
            let AppServerEvent::ServerRequest(request) = client
                .next_event()
                .await
                .expect("bounded requests should arrive before exhaustion")
            else {
                panic!("expected server request event before exhaustion");
            };
            client
                .resolve_server_request(request.id().clone(), serde_json::json!({}))
                .await
                .expect("admitted request should resolve");
        }
        let Some(AppServerEvent::Disconnected { message }) =
            timeout(Duration::from_secs(2), client.next_event())
                .await
                .expect("ledger exhaustion should terminate promptly")
        else {
            panic!("ledger exhaustion should produce a disconnect event");
        };
        assert!(message.contains("ledger is exhausted"));
        client.shutdown().await.expect("shutdown should complete");
    }

    #[tokio::test]
    async fn remote_unsupported_server_request_id_reuse_terminates_without_second_response() {
        let websocket_url = start_test_remote_server(|mut websocket| async move {
            expect_remote_initialize(&mut websocket).await;
            let request_id = RequestId::String("unsupported-reuse".to_string());
            let request = JSONRPCRequest {
                id: request_id.clone(),
                method: "thread/unknown".to_string(),
                params: None,
                trace: None,
            };
            write_websocket_message(&mut websocket, JSONRPCMessage::Request(request.clone())).await;
            let JSONRPCMessage::Error(response) = read_websocket_message(&mut websocket).await
            else {
                panic!("expected unsupported request rejection");
            };
            assert_eq!(response.id, request_id);
            write_websocket_message(&mut websocket, JSONRPCMessage::Request(request)).await;
            assert_no_second_websocket_response(&mut websocket).await;
        })
        .await;
        let mut client = RemoteAppServerClient::connect(test_remote_connect_args(websocket_url))
            .await
            .expect("remote client should connect");
        let Some(AppServerEvent::Disconnected { message }) =
            timeout(Duration::from_secs(2), client.next_event())
                .await
                .expect("unsupported request reuse should terminate promptly")
        else {
            panic!("unsupported request reuse should produce a disconnect event");
        };
        assert!(message.contains("duplicate inbound server request ID"));
        client.shutdown().await.expect("shutdown should complete");
    }

    #[tokio::test]
    async fn remote_server_request_received_during_initialize_is_delivered() {
        let websocket_url = start_test_remote_server(|mut websocket| async move {
            let JSONRPCMessage::Request(request) = read_websocket_message(&mut websocket).await
            else {
                panic!("expected initialize request");
            };
            assert_eq!(request.method, "initialize");

            let request_id = RequestId::String("srv-init".to_string());
            write_websocket_message(
                &mut websocket,
                JSONRPCMessage::Request(JSONRPCRequest {
                    id: request_id.clone(),
                    method: "item/tool/requestUserInput".to_string(),
                    params: Some(
                        serde_json::to_value(ToolRequestUserInputParams {
                            thread_id: "thread-1".to_string(),
                            turn_id: "turn-1".to_string(),
                            item_id: "call-1".to_string(),
                            questions: vec![ToolRequestUserInputQuestion {
                                id: "question-1".to_string(),
                                header: "Mode".to_string(),
                                question: "Pick one".to_string(),
                                is_other: false,
                                is_secret: false,
                                options: Some(vec![]),
                            }],
                            is_blocking: true,
                            auto_resolution_ms: None,
                        })
                        .expect("params should serialize"),
                    ),
                    trace: None,
                }),
            )
            .await;
            write_websocket_message(
                &mut websocket,
                JSONRPCMessage::Response(JSONRPCResponse {
                    id: request.id,
                    result: serde_json::json!({}),
                }),
            )
            .await;

            let JSONRPCMessage::Notification(notification) =
                read_websocket_message(&mut websocket).await
            else {
                panic!("expected initialized notification");
            };
            assert_eq!(notification.method, "initialized");

            let JSONRPCMessage::Response(response) = read_websocket_message(&mut websocket).await
            else {
                panic!("expected server request response");
            };
            assert_eq!(response.id, request_id);
        })
        .await;
        let mut client = RemoteAppServerClient::connect(test_remote_connect_args(websocket_url))
            .await
            .expect("remote client should connect");

        let AppServerEvent::ServerRequest(request) = client
            .next_event()
            .await
            .expect("request event should arrive")
        else {
            panic!("expected server request event");
        };
        client
            .resolve_server_request(request.id().clone(), serde_json::json!({}))
            .await
            .expect("server request should resolve");

        client.shutdown().await.expect("shutdown should complete");
    }

    #[tokio::test]
    async fn remote_unknown_server_request_is_rejected() {
        let websocket_url = start_test_remote_server(|mut websocket| async move {
            expect_remote_initialize(&mut websocket).await;
            let request_id = RequestId::String("srv-unknown".to_string());
            write_websocket_message(
                &mut websocket,
                JSONRPCMessage::Request(JSONRPCRequest {
                    id: request_id.clone(),
                    method: "thread/unknown".to_string(),
                    params: None,
                    trace: None,
                }),
            )
            .await;

            let JSONRPCMessage::Error(response) = read_websocket_message(&mut websocket).await
            else {
                panic!("expected JSON-RPC error response");
            };
            assert_eq!(response.id, request_id);
            assert_eq!(response.error.code, -32601);
            assert_eq!(
                response.error.message,
                "unsupported remote app-server request `thread/unknown`"
            );
        })
        .await;
        let client = RemoteAppServerClient::connect(test_remote_connect_args(websocket_url))
            .await
            .expect("remote client should connect");

        client.shutdown().await.expect("shutdown should complete");
    }

    #[tokio::test]
    async fn remote_disconnect_surfaces_as_event() {
        let websocket_url = start_test_remote_server(|mut websocket| async move {
            expect_remote_initialize(&mut websocket).await;
            websocket.close(None).await.expect("close should succeed");
        })
        .await;
        let mut client = RemoteAppServerClient::connect(test_remote_connect_args(websocket_url))
            .await
            .expect("remote client should connect");

        let event = client
            .next_event()
            .await
            .expect("disconnect event should arrive");
        assert!(matches!(event, AppServerEvent::Disconnected { .. }));
    }

    #[tokio::test]
    async fn remote_pending_required_event_keeps_request_control_responsive() {
        let (done_tx, done_rx) = oneshot::channel();
        let websocket_url = start_test_remote_server(|mut websocket| async move {
            expect_remote_initialize(&mut websocket).await;
            for thread_id in ["queued", "pending"] {
                let notification = remote_thread_closed_notification(thread_id);
                write_websocket_message(
                    &mut websocket,
                    JSONRPCMessage::Notification(
                        serde_json::from_value(serde_json::to_value(notification).unwrap())
                            .unwrap(),
                    ),
                )
                .await;
            }
            let request = read_websocket_message(&mut websocket).await;
            let JSONRPCMessage::Request(request) = request else {
                panic!("expected account request");
            };
            write_websocket_message(
                &mut websocket,
                JSONRPCMessage::Response(JSONRPCResponse {
                    id: request.id,
                    result: serde_json::to_value(GetAccountResponse {
                        account: None,
                        requires_openai_auth: false,
                    })
                    .unwrap(),
                }),
            )
            .await;
            let _ = done_rx.await;
        })
        .await;
        let mut client = RemoteAppServerClient::connect(RemoteAppServerConnectArgs {
            channel_capacity: 1,
            ..test_remote_connect_args(websocket_url)
        })
        .await
        .unwrap();
        timeout(Duration::from_secs(1), client._test_pending_required_event.notified())
            .await
            .expect("second required event should enter custody");
        let request = timeout(
            Duration::from_secs(1),
            client.request_typed::<GetAccountResponse>(remote_get_account_request(92)),
        )
        .await
        .expect("request control should remain responsive")
        .unwrap();
        assert!(!request.requires_openai_auth);
        assert!(matches!(client.next_event().await, Some(AppServerEvent::ServerNotification(ServerNotification::ThreadClosed(n))) if n.thread_id == "queued"));
        assert!(matches!(client.next_event().await, Some(AppServerEvent::ServerNotification(ServerNotification::ThreadClosed(n))) if n.thread_id == "pending"));
        done_tx.send(()).unwrap();
        client.shutdown().await.unwrap();
    }

    #[tokio::test]
    async fn remote_shutdown_preserves_pending_required_event_order() {
        let websocket_url = start_test_remote_server(|mut websocket| async move {
            expect_remote_initialize(&mut websocket).await;
            for thread_id in ["queued", "pending"] {
                let notification = remote_thread_closed_notification(thread_id);
                write_websocket_message(
                    &mut websocket,
                    JSONRPCMessage::Notification(
                        serde_json::from_value(serde_json::to_value(notification).unwrap())
                            .unwrap(),
                    ),
                )
                .await;
            }
            let _ = websocket.next().await;
        })
        .await;
        let mut client = RemoteAppServerClient::connect(RemoteAppServerConnectArgs {
            channel_capacity: 1,
            ..test_remote_connect_args(websocket_url)
        })
        .await
        .unwrap();
        timeout(Duration::from_secs(1), client._test_pending_required_event.notified())
            .await
            .expect("second required event should enter custody");
        timeout(Duration::from_secs(1), client.shutdown())
            .await
            .expect("shutdown should not wait for pending event")
            .unwrap();
    }

    #[test]
    fn typed_request_error_exposes_sources() {
        let transport = TypedRequestError::Transport {
            method: "config/read".to_string(),
            source: IoError::new(ErrorKind::BrokenPipe, "closed"),
        };
        assert_eq!(std::error::Error::source(&transport).is_some(), true);

        let server = TypedRequestError::Server {
            method: "thread/read".to_string(),
            source: JSONRPCErrorError {
                code: -32603,
                data: Some(serde_json::json!({"detail": "config lock mismatch"})),
                message: "internal".to_string(),
            },
        };
        assert_eq!(std::error::Error::source(&server).is_some(), false);
        assert_eq!(
            server.to_string(),
            "thread/read failed: internal (code -32603), data: {\"detail\":\"config lock mismatch\"}"
        );

        let deserialize = TypedRequestError::Deserialize {
            method: "thread/start".to_string(),
            source: serde_json::from_str::<u32>("\"nope\"")
                .expect_err("invalid integer should return deserialize error"),
        };
        assert_eq!(std::error::Error::source(&deserialize).is_some(), true);
    }

    #[tokio::test]
    async fn next_event_surfaces_lagged_markers() {
        let (command_tx, _command_rx) = mpsc::channel(1);
        let (event_tx, event_rx) = mpsc::channel(1);
        let worker_handle = tokio::spawn(async {});
        event_tx
            .send(InProcessServerEvent::Lagged { skipped: 3 })
            .await
            .expect("lagged marker should enqueue");
        drop(event_tx);

        let mut client = InProcessAppServerClient {
            command_tx,
            event_rx,
            worker_handle,
        };

        let event = timeout(Duration::from_secs(2), client.next_event())
            .await
            .expect("lagged marker should arrive before timeout");
        assert!(matches!(
            event,
            Some(InProcessServerEvent::Lagged { skipped: 3 })
        ));

        client.shutdown().await.expect("shutdown should complete");
    }

    #[test]
    fn event_requires_delivery_marks_transcript_and_terminal_events() {
        assert!(event_requires_delivery(
            &InProcessServerEvent::ServerNotification(
                codex_app_server_protocol::ServerNotification::TurnCompleted(
                    codex_app_server_protocol::TurnCompletedNotification {
                        final_model: None,
                        model_snapshot: None,
                        thread_id: "thread".to_string(),
                        turn: codex_app_server_protocol::Turn {
                            id: "turn".to_string(),
                            items_view: codex_app_server_protocol::TurnItemsView::Full,
                            items: Vec::new(),
                            status: codex_app_server_protocol::TurnStatus::Completed,
                            error: None,
                            started_at: None,
                            completed_at: Some(0),
                            duration_ms: None,
                        },
                    }
                )
            )
        ));
        assert!(event_requires_delivery(
            &InProcessServerEvent::ServerNotification(
                codex_app_server_protocol::ServerNotification::AgentMessageDelta(
                    codex_app_server_protocol::AgentMessageDeltaNotification {
                        thread_id: "thread".to_string(),
                        turn_id: "turn".to_string(),
                        item_id: "item".to_string(),
                        delta: "hello".to_string(),
                    }
                )
            )
        ));
        assert!(event_requires_delivery(
            &InProcessServerEvent::ServerNotification(
                codex_app_server_protocol::ServerNotification::ItemCompleted(
                    codex_app_server_protocol::ItemCompletedNotification {
                        thread_id: "thread".to_string(),
                        turn_id: "turn".to_string(),
                        completed_at_ms: 0,
                        item: codex_app_server_protocol::ThreadItem::AgentMessage {
                            id: "item".to_string(),
                            text: "hello".to_string(),
                            phase: None,
                            memory_citation: None,
                        },
                    }
                )
            )
        ));
        assert!(event_requires_delivery(
            &InProcessServerEvent::ServerNotification(
                codex_app_server_protocol::ServerNotification::ExternalAgentConfigImportCompleted(
                    codex_app_server_protocol::ExternalAgentConfigImportCompletedNotification {
                        import_id: "import".to_string(),
                        item_type_results: Vec::new(),
                    },
                )
            )
        ));
        assert!(!event_requires_delivery(&InProcessServerEvent::Lagged {
            skipped: 1
        }));
        assert!(!event_requires_delivery(
            &InProcessServerEvent::ServerNotification(
                codex_app_server_protocol::ServerNotification::CommandExecutionOutputDelta(
                    codex_app_server_protocol::CommandExecutionOutputDeltaNotification {
                        thread_id: "thread".to_string(),
                        turn_id: "turn".to_string(),
                        item_id: "item".to_string(),
                        delta: "stdout".to_string(),
                    }
                )
            )
        ));
    }

    #[tokio::test]
    async fn runtime_start_args_forward_environment_manager_and_openai_form_capability() {
        let config = Arc::new(build_test_config().await);
        let environment_manager = Arc::new(
            EnvironmentManager::create_for_tests(
                Some("ws://127.0.0.1:8765".to_string()),
                Some(
                    ExecServerRuntimePaths::new(
                        std::env::current_exe().expect("current exe"),
                        /*codex_linux_sandbox_exe*/ None,
                    )
                    .expect("runtime paths"),
                ),
            )
            .await,
        );

        let runtime_args = InProcessClientStartArgs {
            arg0_paths: Arg0DispatchPaths::default(),
            config: config.clone(),
            cli_overrides: Vec::new(),
            loader_overrides: LoaderOverrides::default(),
            strict_config: false,
            cloud_config_bundle: CloudConfigBundleLoader::default(),
            feedback: CodexFeedback::new(),
            log_db: None,
            state_db: None,
            environment_manager: environment_manager.clone(),
            config_warnings: Vec::new(),
            session_source: SessionSource::Exec,
            enable_codex_api_key_env: false,
            client_name: "codex-app-server-client-test".to_string(),
            client_version: "0.0.0-test".to_string(),
            experimental_api: true,
            mcp_server_openai_form_elicitation: true,
            opt_out_notification_methods: Vec::new(),
            channel_capacity: DEFAULT_IN_PROCESS_CHANNEL_CAPACITY,
        }
        .into_runtime_start_args();

        assert_eq!(runtime_args.config, config);
        assert!(
            runtime_args
                .initialize
                .capabilities
                .expect("initialize capabilities")
                .mcp_server_openai_form_elicitation
        );
        assert!(Arc::ptr_eq(
            &runtime_args.environment_manager,
            &environment_manager
        ));
        assert!(
            runtime_args
                .environment_manager
                .default_environment()
                .expect("default environment")
                .is_remote()
        );
    }

    #[tokio::test]
    async fn runtime_start_args_use_remote_thread_config_loader_when_configured() {
        let mut config = build_test_config().await;
        config.experimental_thread_config_endpoint = Some("not-a-valid-endpoint".to_string());

        let runtime_args = InProcessClientStartArgs {
            arg0_paths: Arg0DispatchPaths::default(),
            config: Arc::new(config),
            cli_overrides: Vec::new(),
            loader_overrides: LoaderOverrides::default(),
            strict_config: false,
            cloud_config_bundle: CloudConfigBundleLoader::default(),
            feedback: CodexFeedback::new(),
            log_db: None,
            state_db: None,
            environment_manager: Arc::new(EnvironmentManager::default_for_tests()),
            config_warnings: Vec::new(),
            session_source: SessionSource::Exec,
            enable_codex_api_key_env: false,
            client_name: "codex-app-server-client-test".to_string(),
            client_version: "0.0.0-test".to_string(),
            experimental_api: true,
            mcp_server_openai_form_elicitation: false,
            opt_out_notification_methods: Vec::new(),
            channel_capacity: DEFAULT_IN_PROCESS_CHANNEL_CAPACITY,
        }
        .into_runtime_start_args();

        let err = runtime_args
            .thread_config_loader
            .load(Default::default())
            .await
            .expect_err("configured remote loader should try to connect");
        assert_eq!(
            err.code(),
            codex_config::ThreadConfigLoadErrorCode::RequestFailed
        );
    }

    #[tokio::test]
    async fn shutdown_completes_promptly_without_retained_managers() {
        let client = start_test_client(SessionSource::Cli).await;

        timeout(Duration::from_secs(1), client.shutdown())
            .await
            .expect("shutdown should not wait for the 5s fallback timeout")
            .expect("shutdown should complete");
    }

    #[tokio::test]
    async fn shutdown_delivers_response_before_releasing_event_receiver() {
        use std::sync::atomic::AtomicBool;
        use std::sync::atomic::Ordering;

        let (command_tx, mut command_rx) = mpsc::channel(1);
        let (event_tx, event_rx) = mpsc::channel(1);
        let response_sent_while_event_receiver_open = Arc::new(AtomicBool::new(false));
        let response_sent = Arc::clone(&response_sent_while_event_receiver_open);
        let worker_handle = tokio::spawn(async move {
            let Some(ClientCommand::Shutdown { response_tx }) = command_rx.recv().await else {
                panic!("expected shutdown command");
            };
            assert!(!event_tx.is_closed());
            response_tx
                .send(Ok(()))
                .expect("shutdown response receiver should be alive");
            response_sent.store(true, Ordering::Release);
            event_tx.closed().await;
        });
        let client = InProcessAppServerClient {
            command_tx,
            event_rx,
            worker_handle,
        };

        timeout(Duration::from_secs(1), client.shutdown())
            .await
            .expect("shutdown should complete")
            .expect("shutdown should deliver its response before releasing events");
        assert!(response_sent_while_event_receiver_open.load(Ordering::Acquire));
    }

    #[tokio::test(start_paused = true)]
    async fn shutdown_waits_for_in_process_drain() {
        use std::sync::atomic::AtomicBool;
        use std::sync::atomic::Ordering;

        let (command_tx, mut command_rx) = mpsc::channel(1);
        let (_event_tx, event_rx) = mpsc::channel(1);
        let completed = Arc::new(AtomicBool::new(false));
        let worker_completed = Arc::clone(&completed);
        let worker_handle = tokio::spawn(async move {
            let response_tx = match command_rx.recv().await {
                Some(ClientCommand::Shutdown { response_tx }) => response_tx,
                _ => panic!("expected shutdown command"),
            };
            tokio::time::sleep(Duration::from_secs(30)).await;
            worker_completed.store(true, Ordering::Release);
            let _ = response_tx.send(Ok(()));
        });
        let client = InProcessAppServerClient {
            command_tx,
            event_rx,
            worker_handle,
        };

        client.shutdown().await.expect("shutdown should complete");
        assert!(completed.load(Ordering::Acquire));
    }
}
