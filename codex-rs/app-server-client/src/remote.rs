/*
This module implements the remote app-server client transport.

It owns the remote connection lifecycle, including the initialize/initialized
handshake, JSON-RPC request/response routing, server-request resolution, and
notification streaming. Remote connections always carry WebSocket frames, over
either TCP WebSocket URLs or local Unix sockets. The rest of the crate uses the
same `AppServerEvent` surface for both in-process and remote transports, so
callers such as the TUI can switch between them without changing their
higher-level session logic.
*/

use std::collections::HashMap;
use std::collections::VecDeque;
use std::io::Error as IoError;
use std::io::ErrorKind;
use std::io::Result as IoResult;
use std::time::Duration;

use crate::AppServerEvent;
use crate::RequestResult;
use crate::SHUTDOWN_TIMEOUT;
use crate::TypedRequestError;
use crate::server_notification_requires_delivery;
use codex_app_server_protocol::ClientInfo;
use codex_app_server_protocol::ClientNotification;
use codex_app_server_protocol::ClientRequest;
use codex_app_server_protocol::InitializeCapabilities;
use codex_app_server_protocol::InitializeParams;
use codex_app_server_protocol::JSONRPCError;
use codex_app_server_protocol::JSONRPCErrorError;
use codex_app_server_protocol::JSONRPCMessage;
use codex_app_server_protocol::JSONRPCNotification;
use codex_app_server_protocol::JSONRPCRequest;
use codex_app_server_protocol::JSONRPCResponse;
use codex_app_server_protocol::RequestId;
use codex_app_server_protocol::Result as JsonRpcResult;
use codex_app_server_protocol::ServerNotification;
use codex_app_server_protocol::ServerRequest;
use codex_uds::UnixStream;
use codex_utils_absolute_path::AbsolutePathBuf;
use codex_utils_rustls_provider::ensure_rustls_crypto_provider;
use futures::SinkExt;
use futures::StreamExt;
use serde::de::DeserializeOwned;
use tokio::io::AsyncRead;
use tokio::io::AsyncWrite;
use tokio::net::TcpStream;
use tokio::sync::mpsc;
use tokio::sync::oneshot;
use tokio::time::timeout;
use tokio_tungstenite::MaybeTlsStream;
use tokio_tungstenite::WebSocketStream;
use tokio_tungstenite::client_async_with_config;
use tokio_tungstenite::connect_async_with_config;
use tokio_tungstenite::tungstenite::Error as TungsteniteError;
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::error::ProtocolError;
use tokio_tungstenite::tungstenite::http::HeaderValue;
use tokio_tungstenite::tungstenite::http::header::AUTHORIZATION;
use tokio_tungstenite::tungstenite::protocol::WebSocketConfig;
use tracing::warn;
use url::Url;

const CONNECT_TIMEOUT: Duration = Duration::from_secs(10);
const INITIALIZE_TIMEOUT: Duration = Duration::from_secs(10);
const REMOTE_APP_SERVER_MAX_WEBSOCKET_MESSAGE_SIZE: usize = 128 << 20;
// Inbound request IDs are retained for the entire connection lifetime so a
// peer cannot recycle an ID after its response has been written.
const MAX_INBOUND_SERVER_REQUEST_IDS: usize = 512;
const MAX_INBOUND_SERVER_REQUEST_ID_STRING_BYTES: usize = 16 * 1024;
const INBOUND_SERVER_REQUEST_ID_BYTES: usize = 9 * 1024 * 1024;
const INBOUND_SERVER_REQUEST_ID_ENTRY_OVERHEAD_BYTES: usize = 256;
// Tungstenite still needs an HTTP request URI for the WebSocket handshake;
// the bytes travel over the Unix socket, not TCP.
const UDS_WEBSOCKET_HANDSHAKE_URL: &str = "ws://localhost/rpc";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum InboundServerRequestState {
    Pending,
    Responded,
    Rejected,
}

#[derive(Debug)]
struct InboundServerRequestLedger {
    entries: HashMap<RequestId, InboundServerRequestState>,
    retained_string_id_bytes: usize,
    max_entries: usize,
}

impl InboundServerRequestLedger {
    fn new(channel_capacity: usize) -> Self {
        let max_entries = channel_capacity
            .saturating_mul(4)
            .clamp(1, MAX_INBOUND_SERVER_REQUEST_IDS);
        Self {
            entries: HashMap::with_capacity(max_entries),
            retained_string_id_bytes: 0,
            max_entries,
        }
    }
    fn claim(&mut self, request_id: &RequestId) -> IoResult<()> {
        if self.entries.contains_key(request_id) {
            return Err(IoError::new(
                ErrorKind::InvalidData,
                "duplicate inbound server request ID on this connection",
            ));
        }
        if self.entries.len() >= self.max_entries {
            return Err(IoError::new(
                ErrorKind::InvalidData,
                "inbound server request ID ledger is exhausted",
            ));
        }
        if let RequestId::String(value) = request_id {
            if value.len() > MAX_INBOUND_SERVER_REQUEST_ID_STRING_BYTES {
                return Err(IoError::new(
                    ErrorKind::InvalidData,
                    format!(
                        "inbound server request ID is longer than {MAX_INBOUND_SERVER_REQUEST_ID_STRING_BYTES} bytes (received {} bytes)",
                        value.len()
                    ),
                ));
            }
            let retained = value
                .len()
                .saturating_add(INBOUND_SERVER_REQUEST_ID_ENTRY_OVERHEAD_BYTES);
            let Some(next) = self.retained_string_id_bytes.checked_add(retained) else {
                return Err(IoError::new(
                    ErrorKind::InvalidData,
                    "inbound server request ID byte ledger is exhausted",
                ));
            };
            if next > INBOUND_SERVER_REQUEST_ID_BYTES {
                return Err(IoError::new(
                    ErrorKind::InvalidData,
                    "inbound server request ID byte ledger is exhausted",
                ));
            }
            self.retained_string_id_bytes = next;
        }
        self.entries
            .insert(request_id.clone(), InboundServerRequestState::Pending);
        Ok(())
    }
    fn finish(&mut self, request_id: &RequestId, state: InboundServerRequestState) -> IoResult<()> {
        match self.entries.get_mut(request_id) {
            Some(slot @ InboundServerRequestState::Pending) => {
                *slot = state;
                Ok(())
            }
            Some(_) => Err(IoError::new(
                ErrorKind::InvalidData,
                "inbound server request is no longer pending",
            )),
            None => Err(IoError::new(
                ErrorKind::InvalidData,
                "unknown inbound server request ID",
            )),
        }
    }
    fn respond(&mut self, id: &RequestId) -> IoResult<()> {
        self.finish(id, InboundServerRequestState::Responded)
    }
    fn reject(&mut self, id: &RequestId) -> IoResult<()> {
        self.finish(id, InboundServerRequestState::Rejected)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RemoteAppServerEndpoint {
    WebSocket {
        websocket_url: String,
        auth_token: Option<String>,
    },
    UnixSocket {
        socket_path: AbsolutePathBuf,
    },
}

#[derive(Debug, Clone)]
pub struct RemoteAppServerConnectArgs {
    pub endpoint: RemoteAppServerEndpoint,
    pub client_name: String,
    pub client_version: String,
    pub experimental_api: bool,
    pub mcp_server_openai_form_elicitation: bool,
    pub opt_out_notification_methods: Vec<String>,
    pub channel_capacity: usize,
}
impl RemoteAppServerConnectArgs {
    pub(crate) fn initialize_params(&self) -> InitializeParams {
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
}

pub(crate) fn websocket_url_supports_auth_token(url: &Url) -> bool {
    match (url.scheme(), url.host()) {
        ("wss", Some(_)) => true,
        ("ws", Some(url::Host::Domain(domain))) => domain.eq_ignore_ascii_case("localhost"),
        ("ws", Some(url::Host::Ipv4(addr))) => addr.is_loopback(),
        ("ws", Some(url::Host::Ipv6(addr))) => addr.is_loopback(),
        _ => false,
    }
}

enum RemoteClientCommand {
    Request {
        request: Box<JSONRPCRequest>,
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
    #[cfg(test)]
    CloseStreamForTest {
        response_tx: oneshot::Sender<IoResult<()>>,
    },
}

pub struct RemoteAppServerClient {
    command_tx: mpsc::Sender<RemoteClientCommand>,
    event_rx: mpsc::Receiver<AppServerEvent>,
    pending_events: VecDeque<AppServerEvent>,
    server_version: Option<String>,
    codex_home: Option<String>,
    worker_handle: tokio::task::JoinHandle<()>,
    #[cfg(test)]
    pub(crate) _test_pending_required_event: std::sync::Arc<tokio::sync::Notify>,
    #[cfg(test)]
    pub(crate) _test_pending_lag: std::sync::Arc<tokio::sync::Notify>,
}

#[derive(Clone)]
pub struct RemoteAppServerRequestHandle {
    command_tx: mpsc::Sender<RemoteClientCommand>,
}

impl RemoteAppServerClient {
    pub async fn connect(args: RemoteAppServerConnectArgs) -> IoResult<Self> {
        let channel_capacity = args.channel_capacity.max(1);
        let initialize_params = args.initialize_params();
        match args.endpoint {
            RemoteAppServerEndpoint::WebSocket {
                websocket_url,
                auth_token,
            } => {
                let (endpoint, stream) =
                    connect_websocket_endpoint(websocket_url, auth_token).await?;
                Self::connect_with_stream(channel_capacity, endpoint, stream, initialize_params)
                    .await
            }
            RemoteAppServerEndpoint::UnixSocket { socket_path } => {
                let (endpoint, stream) = connect_unix_socket_endpoint(socket_path).await?;
                Self::connect_with_stream(channel_capacity, endpoint, stream, initialize_params)
                    .await
            }
        }
    }

    pub fn server_version(&self) -> Option<&str> {
        self.server_version.as_deref()
    }

    pub fn codex_home(&self) -> Option<&str> {
        self.codex_home.as_deref()
    }

    async fn connect_with_stream<S>(
        channel_capacity: usize,
        endpoint: String,
        stream: WebSocketStream<S>,
        initialize_params: InitializeParams,
    ) -> IoResult<Self>
    where
        S: AsyncRead + AsyncWrite + Unpin + Send + 'static,
    {
        let mut stream = stream;
        let mut inbound_server_request_ledger = InboundServerRequestLedger::new(channel_capacity);
        let (pending_events, server_version, codex_home) = initialize_remote_connection(
            &mut stream,
            &endpoint,
            initialize_params,
            INITIALIZE_TIMEOUT,
            &mut inbound_server_request_ledger,
        )
        .await?;

        let (command_tx, mut command_rx) = mpsc::channel::<RemoteClientCommand>(channel_capacity);
        let (event_tx, event_rx) = mpsc::channel::<AppServerEvent>(channel_capacity);
        #[cfg(test)]
        let test_pending_required_event = std::sync::Arc::new(tokio::sync::Notify::new());
        #[cfg(test)]
        let worker_test_pending_required_event =
            std::sync::Arc::clone(&test_pending_required_event);
        #[cfg(test)]
        let test_pending_lag = std::sync::Arc::new(tokio::sync::Notify::new());
        #[cfg(test)]
        let worker_test_pending_lag = std::sync::Arc::clone(&test_pending_lag);
        let worker_handle = tokio::spawn(async move {
            let mut pending_requests =
                HashMap::<RequestId, oneshot::Sender<IoResult<RequestResult>>>::new();
            let mut inbound_server_request_ledger = inbound_server_request_ledger;
            let mut terminal_state = None::<RemoteTerminalState>;
            let mut event_delivery_enabled = true;
            let mut skipped_events = 0usize;
            let mut pending_required_event = None::<AppServerEvent>;
            loop {
                tokio::select! {
                    permit = event_tx.reserve(), if event_delivery_enabled && (
                        skipped_events > 0
                            || pending_required_event.is_some()
                            || terminal_state
                                .as_ref()
                                .is_some_and(|state| state.disconnected_pending)
                    ) => {
                        match permit {
                            Ok(permit) => {
                                if skipped_events > 0 {
                                    permit.send(AppServerEvent::Lagged {
                                        skipped: std::mem::take(&mut skipped_events),
                                    });
                                } else if let Some(event) = pending_required_event.take() {
                                    permit.send(event);
                                } else if let Some(state) = terminal_state.as_mut() {
                                    permit.send(AppServerEvent::Disconnected {
                                        message: state.message.clone(),
                                    });
                                    state.disconnected_pending = false;
                                }
                            }
                            Err(_) => {
                                skipped_events = 0;
                                pending_required_event = None;
                                disable_remote_event_delivery(
                                    &mut terminal_state,
                                    &mut pending_requests,
                                );
                                event_delivery_enabled = false;
                            }
                        }
                    }
                    command = command_rx.recv() => {
                        let Some(command) = command else {
                            let _ = close_remote_stream(&mut stream, &endpoint).await;
                            break;
                        };
                        if let Some(state) = &terminal_state {
                            match command {
                                RemoteClientCommand::Request { response_tx, .. } => {
                                    let _ = response_tx.send(Err(state.error()));
                                    continue;
                                }
                                RemoteClientCommand::Notify { response_tx, .. }
                                | RemoteClientCommand::ResolveServerRequest { response_tx, .. }
                                | RemoteClientCommand::RejectServerRequest { response_tx, .. } => {
                                    let _ = response_tx.send(Err(state.error()));
                                    continue;
                                }
                                RemoteClientCommand::Shutdown { response_tx } => {
                                    let close_result =
                                        close_remote_stream(&mut stream, &endpoint).await;
                                    let _ = response_tx.send(close_result);
                                    break;
                                }
                                #[cfg(test)]
                                RemoteClientCommand::CloseStreamForTest { response_tx } => {
                                    let close_result =
                                        close_remote_stream(&mut stream, &endpoint).await;
                                    let _ = response_tx.send(close_result);
                                    continue;
                                }
                            }
                        }
                        match command {
                            RemoteClientCommand::Request { request, response_tx } => {
                                let request_id = request.id.clone();
                                if pending_requests.contains_key(&request_id) {
                                    let _ = response_tx.send(Err(IoError::new(
                                        ErrorKind::InvalidInput,
                                        format!("duplicate remote app-server request id `{request_id}`"),
                                    )));
                                    continue;
                                }
                                pending_requests.insert(request_id.clone(), response_tx);
                                if let Err(err) = write_jsonrpc_message(
                                    &mut stream,
                                    JSONRPCMessage::Request(*request),
                                    &endpoint,
                                )
                                .await
                                {
                                    let err_message = err.to_string();
                                    let message = format!(
                                        "remote app server at `{endpoint}` write failed: {err_message}"
                                    );
                                    enter_remote_terminal_state(
                                        &mut terminal_state,
                                        &mut pending_requests,
                                        ErrorKind::BrokenPipe,
                                        message,
                                        RemoteDisconnectedDelivery::Pending,
                                    );
                                }
                            }
                            RemoteClientCommand::Notify { notification, response_tx } => {
                                let result = write_jsonrpc_message(
                                    &mut stream,
                                    JSONRPCMessage::Notification(
                                        jsonrpc_notification_from_client_notification(notification),
                                    ),
                                    &endpoint,
                                )
                                .await;
                                finish_remote_control_write(
                                    result,
                                    response_tx,
                                    &endpoint,
                                    &mut terminal_state,
                                    &mut pending_requests,
                                );
                            }
                            RemoteClientCommand::ResolveServerRequest {
                                request_id,
                                result,
                                response_tx,
                            } => {
                                if let Err(err) = inbound_server_request_ledger.respond(&request_id) {
                                    let _ = response_tx.send(Err(err));
                                    continue;
                                }
                                let result = write_jsonrpc_message(
                                    &mut stream,
                                    JSONRPCMessage::Response(JSONRPCResponse {
                                        id: request_id,
                                        result,
                                    }),
                                    &endpoint,
                                )
                                .await;
                                finish_remote_control_write(
                                    result,
                                    response_tx,
                                    &endpoint,
                                    &mut terminal_state,
                                    &mut pending_requests,
                                );
                            }
                            RemoteClientCommand::RejectServerRequest {
                                request_id,
                                error,
                                response_tx,
                            } => {
                                if let Err(err) = inbound_server_request_ledger.reject(&request_id) {
                                    let _ = response_tx.send(Err(err));
                                    continue;
                                }
                                let result = write_jsonrpc_message(
                                    &mut stream,
                                    JSONRPCMessage::Error(JSONRPCError {
                                        error,
                                        id: request_id,
                                    }),
                                    &endpoint,
                                )
                                .await;
                                finish_remote_control_write(
                                    result,
                                    response_tx,
                                    &endpoint,
                                    &mut terminal_state,
                                    &mut pending_requests,
                                );
                            }
                            RemoteClientCommand::Shutdown { response_tx } => {
                                let close_result = close_remote_stream(&mut stream, &endpoint).await;
                                let _ = response_tx.send(close_result);
                                break;
                            }
                            #[cfg(test)]
                            RemoteClientCommand::CloseStreamForTest { response_tx } => {
                                let close_result = close_remote_stream(&mut stream, &endpoint).await;
                                let _ = response_tx.send(close_result);
                            }
                        }
                    }
                    message = stream.next(), if event_delivery_enabled
                        && pending_required_event.is_none()
                        && terminal_state.is_none() => {
                        match message {
                            Some(Ok(Message::Text(text))) => {
                                match serde_json::from_str::<JSONRPCMessage>(&text) {
                                    Ok(JSONRPCMessage::Response(response)) => {
                                        if let Some(response_tx) = pending_requests.remove(&response.id) {
                                            let _ = response_tx.send(Ok(Ok(response.result)));
                                        }
                                    }
                                    Ok(JSONRPCMessage::Error(error)) => {
                                        if let Some(response_tx) = pending_requests.remove(&error.id) {
                                            let _ = response_tx.send(Ok(Err(error.error)));
                                        }
                                    }
                                    Ok(JSONRPCMessage::Notification(notification)) => {
                                        if let Some(event) = app_server_event_from_notification(notification) {
                                            let delivery = try_deliver_event(
                                                &event_tx,
                                                &mut skipped_events,
                                                &mut pending_required_event,
                                                event,
                                            );
                                            match delivery {
                                                RemoteEventForwardResult::Forwarded => {}
                                                RemoteEventForwardResult::Pending => {
                                                    #[cfg(test)]
                                                    worker_test_pending_required_event.notify_one();
                                                }
                                                RemoteEventForwardResult::DroppedBestEffort => {
                                                    #[cfg(test)]
                                                    worker_test_pending_lag.notify_one();
                                                }
                                                RemoteEventForwardResult::Closed => {
                                                    disable_remote_event_delivery(
                                                        &mut terminal_state,
                                                        &mut pending_requests,
                                                    );
                                                    event_delivery_enabled = false;
                                                }
                                            }
                                        }
                                    }
                                    Ok(JSONRPCMessage::Request(request)) => {
                                        let request_id = request.id.clone();
                                        let method = request.method.clone();
                                        if let Err(err) = inbound_server_request_ledger.claim(&request_id) {
                                            let message = format!("remote app server at `{endpoint}` sent an invalid inbound server request: {err}");
                                            enter_remote_terminal_state(&mut terminal_state, &mut pending_requests, ErrorKind::InvalidData, message, RemoteDisconnectedDelivery::Pending);
                                            continue;
                                        }
                                        match ServerRequest::try_from(request) {
                                            Ok(request) => {
                                                let delivery = try_deliver_event(
                                                    &event_tx,
                                                    &mut skipped_events,
                                                    &mut pending_required_event,
                                                    AppServerEvent::ServerRequest(request),
                                                );
                                                match delivery {
                                                    RemoteEventForwardResult::Forwarded => {}
                                                    RemoteEventForwardResult::Pending => {
                                                        #[cfg(test)]
                                                        worker_test_pending_required_event
                                                            .notify_one();
                                                    }
                                                    RemoteEventForwardResult::DroppedBestEffort => {
                                                        unreachable!(
                                                            "remote server requests require delivery"
                                                        );
                                                    }
                                                    RemoteEventForwardResult::Closed => {
                                                        disable_remote_event_delivery(
                                                            &mut terminal_state,
                                                            &mut pending_requests,
                                                        );
                                                        event_delivery_enabled = false;
                                                    }
                                                }
                                            }
                                            Err(err) => {
                                                warn!(%err, method, "rejecting unknown remote app-server request");
                                                let _ = inbound_server_request_ledger.reject(&request_id);
                                                if let Err(reject_err) = write_jsonrpc_message(
                                                    &mut stream,
                                                    JSONRPCMessage::Error(JSONRPCError {
                                                        error: JSONRPCErrorError {
                                                            code: -32601,
                                                            message: format!(
                                                                "unsupported remote app-server request `{method}`"
                                                            ),
                                                            data: None,
                                                        },
                                                        id: request_id,
                                                    }),
                                                    &endpoint,
                                                )
                                                .await
                                                {
                                                    let err_message = reject_err.to_string();
                                                    let message = format!(
                                                        "remote app server at `{endpoint}` write failed: {err_message}"
                                                    );
                                                    enter_remote_terminal_state(
                                                        &mut terminal_state,
                                                        &mut pending_requests,
                                                        ErrorKind::BrokenPipe,
                                                        message,
                                                        RemoteDisconnectedDelivery::Pending,
                                                    );
                                                }
                                            }
                                        }
                                    }
                                    Err(err) => {
                                        let message = format!(
                                            "remote app server at `{endpoint}` sent invalid JSON-RPC: {err}"
                                        );
                                        enter_remote_terminal_state(
                                            &mut terminal_state,
                                            &mut pending_requests,
                                            ErrorKind::InvalidData,
                                            message,
                                            RemoteDisconnectedDelivery::Pending,
                                        );
                                    }
                                }
                            }
                            Some(Ok(Message::Close(frame))) => {
                                let reason = frame
                                    .as_ref()
                                    .map(|frame| frame.reason.to_string())
                                    .filter(|reason| !reason.is_empty())
                                    .unwrap_or_else(|| "connection closed".to_string());
                                let message = format!(
                                    "remote app server at `{endpoint}` disconnected: {reason}"
                                );
                                enter_remote_terminal_state(
                                    &mut terminal_state,
                                    &mut pending_requests,
                                    ErrorKind::ConnectionAborted,
                                    message,
                                    RemoteDisconnectedDelivery::Pending,
                                );
                            }
                            Some(Ok(Message::Binary(_)))
                            | Some(Ok(Message::Ping(_)))
                            | Some(Ok(Message::Pong(_)))
                            | Some(Ok(Message::Frame(_))) => {}
                            Some(Err(err)) => {
                                let message = format!(
                                    "remote app server at `{endpoint}` transport failed: {err}"
                                );
                                enter_remote_terminal_state(
                                    &mut terminal_state,
                                    &mut pending_requests,
                                    ErrorKind::InvalidData,
                                    message,
                                    RemoteDisconnectedDelivery::Pending,
                                );
                            }
                            None => {
                                let message = format!(
                                    "remote app server at `{endpoint}` closed the connection"
                                );
                                enter_remote_terminal_state(
                                    &mut terminal_state,
                                    &mut pending_requests,
                                    ErrorKind::UnexpectedEof,
                                    message,
                                    RemoteDisconnectedDelivery::Pending,
                                );
                            }
                        }
                    }
                }
            }

            let (err_kind, err_message) = terminal_state
                .map(|state| (state.error_kind, state.message))
                .unwrap_or_else(|| {
                    (
                        ErrorKind::BrokenPipe,
                        "remote app-server worker channel is closed".to_string(),
                    )
                });
            for (_, response_tx) in pending_requests {
                let _ = response_tx.send(Err(IoError::new(err_kind, err_message.clone())));
            }
        });

        Ok(Self {
            command_tx,
            event_rx,
            pending_events: pending_events.into(),
            server_version,
            codex_home,
            worker_handle,
            #[cfg(test)]
            _test_pending_required_event: test_pending_required_event,
            #[cfg(test)]
            _test_pending_lag: test_pending_lag,
        })
    }

    pub fn request_handle(&self) -> RemoteAppServerRequestHandle {
        RemoteAppServerRequestHandle {
            command_tx: self.command_tx.clone(),
        }
    }

    pub async fn request(&self, request: ClientRequest) -> IoResult<RequestResult> {
        self.request_handle().request(request).await
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

    pub async fn notify(&self, notification: ClientNotification) -> IoResult<()> {
        let (response_tx, response_rx) = oneshot::channel();
        self.command_tx
            .send(RemoteClientCommand::Notify {
                notification,
                response_tx,
            })
            .await
            .map_err(|_| {
                IoError::new(
                    ErrorKind::BrokenPipe,
                    "remote app-server worker channel is closed",
                )
            })?;
        response_rx.await.map_err(|_| {
            IoError::new(
                ErrorKind::BrokenPipe,
                "remote app-server notify channel is closed",
            )
        })?
    }

    pub async fn resolve_server_request(
        &self,
        request_id: RequestId,
        result: JsonRpcResult,
    ) -> IoResult<()> {
        let (response_tx, response_rx) = oneshot::channel();
        self.command_tx
            .send(RemoteClientCommand::ResolveServerRequest {
                request_id,
                result,
                response_tx,
            })
            .await
            .map_err(|_| {
                IoError::new(
                    ErrorKind::BrokenPipe,
                    "remote app-server worker channel is closed",
                )
            })?;
        response_rx.await.map_err(|_| {
            IoError::new(
                ErrorKind::BrokenPipe,
                "remote app-server resolve channel is closed",
            )
        })?
    }

    pub async fn reject_server_request(
        &self,
        request_id: RequestId,
        error: JSONRPCErrorError,
    ) -> IoResult<()> {
        let (response_tx, response_rx) = oneshot::channel();
        self.command_tx
            .send(RemoteClientCommand::RejectServerRequest {
                request_id,
                error,
                response_tx,
            })
            .await
            .map_err(|_| {
                IoError::new(
                    ErrorKind::BrokenPipe,
                    "remote app-server worker channel is closed",
                )
            })?;
        response_rx.await.map_err(|_| {
            IoError::new(
                ErrorKind::BrokenPipe,
                "remote app-server reject channel is closed",
            )
        })?
    }

    #[cfg(test)]
    pub(crate) async fn close_stream_for_test(&self) -> IoResult<()> {
        let (response_tx, response_rx) = oneshot::channel();
        self.command_tx
            .send(RemoteClientCommand::CloseStreamForTest { response_tx })
            .await
            .map_err(|_| {
                IoError::new(
                    ErrorKind::BrokenPipe,
                    "remote app-server worker channel is closed",
                )
            })?;
        response_rx.await.map_err(|_| {
            IoError::new(
                ErrorKind::BrokenPipe,
                "remote app-server test close channel is closed",
            )
        })?
    }

    pub async fn next_event(&mut self) -> Option<AppServerEvent> {
        if let Some(event) = self.pending_events.pop_front() {
            return Some(event);
        }
        self.event_rx.recv().await
    }

    pub async fn shutdown(self) -> IoResult<()> {
        let Self {
            command_tx,
            event_rx,
            worker_handle,
            ..
        } = self;
        let mut worker_handle = worker_handle;
        drop(event_rx);
        let (response_tx, response_rx) = oneshot::channel();
        if command_tx
            .send(RemoteClientCommand::Shutdown { response_tx })
            .await
            .is_ok()
            && let Ok(Ok(close_result)) = timeout(SHUTDOWN_TIMEOUT, response_rx).await
        {
            close_result?;
        }

        if let Err(_elapsed) = timeout(SHUTDOWN_TIMEOUT, &mut worker_handle).await {
            worker_handle.abort();
            let _ = worker_handle.await;
        }
        Ok(())
    }
}

impl RemoteAppServerRequestHandle {
    pub async fn request(&self, request: ClientRequest) -> IoResult<RequestResult> {
        self.request_json_rpc(jsonrpc_request_from_client_request(request))
            .await
    }

    pub async fn request_json_rpc(&self, request: JSONRPCRequest) -> IoResult<RequestResult> {
        let (response_tx, response_rx) = oneshot::channel();
        self.command_tx
            .send(RemoteClientCommand::Request {
                request: Box::new(request),
                response_tx,
            })
            .await
            .map_err(|_| {
                IoError::new(
                    ErrorKind::BrokenPipe,
                    "remote app-server worker channel is closed",
                )
            })?;
        response_rx.await.map_err(|_| {
            IoError::new(
                ErrorKind::BrokenPipe,
                "remote app-server request channel is closed",
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

async fn connect_websocket_endpoint(
    websocket_url: String,
    auth_token: Option<String>,
) -> IoResult<(String, WebSocketStream<MaybeTlsStream<TcpStream>>)> {
    let url = Url::parse(&websocket_url).map_err(|err| {
        IoError::new(
            ErrorKind::InvalidInput,
            format!("invalid websocket URL `{websocket_url}`: {err}"),
        )
    })?;
    if auth_token.is_some() && !websocket_url_supports_auth_token(&url) {
        return Err(IoError::new(
            ErrorKind::InvalidInput,
            format!(
                "remote auth tokens require `wss://` or loopback `ws://` URLs; got `{websocket_url}`"
            ),
        ));
    }

    let mut request = url.as_str().into_client_request().map_err(|err| {
        IoError::new(
            ErrorKind::InvalidInput,
            format!("invalid websocket URL `{websocket_url}`: {err}"),
        )
    })?;
    if let Some(auth_token) = auth_token.as_deref() {
        let header_value =
            HeaderValue::from_str(&format!("Bearer {auth_token}")).map_err(|err| {
                IoError::new(
                    ErrorKind::InvalidInput,
                    format!("invalid remote authorization header value: {err}"),
                )
            })?;
        request.headers_mut().insert(AUTHORIZATION, header_value);
    }

    ensure_rustls_crypto_provider();
    let websocket_config = remote_websocket_config();
    let stream = timeout(
        CONNECT_TIMEOUT,
        connect_async_with_config(
            request,
            Some(websocket_config),
            /*disable_nagle*/ false,
        ),
    )
    .await
    .map_err(|_| {
        IoError::new(
            ErrorKind::TimedOut,
            format!("timed out connecting to remote app server at `{websocket_url}`"),
        )
    })?
    .map(|(stream, _response)| stream)
    .map_err(|err| {
        IoError::other(format!(
            "failed to connect to remote app server at `{websocket_url}`: {err}"
        ))
    })?;

    Ok((websocket_url, stream))
}

async fn connect_unix_socket_endpoint(
    socket_path: AbsolutePathBuf,
) -> IoResult<(String, WebSocketStream<UnixStream>)> {
    let endpoint = format!("unix://{}", socket_path.display());
    let request = UDS_WEBSOCKET_HANDSHAKE_URL
        .into_client_request()
        .map_err(|err| {
            IoError::new(
                ErrorKind::InvalidInput,
                format!("invalid UDS websocket handshake URL: {err}"),
            )
        })?;
    let stream = timeout(CONNECT_TIMEOUT, UnixStream::connect(socket_path.as_path()))
        .await
        .map_err(|_| {
            IoError::new(
                ErrorKind::TimedOut,
                format!("timed out connecting to remote app server at `{endpoint}`"),
            )
        })?
        .map_err(|err| {
            IoError::other(format!(
                "failed to connect to remote app server at `{endpoint}`: {err}"
            ))
        })?;
    let websocket_config = remote_websocket_config();
    let stream = timeout(
        CONNECT_TIMEOUT,
        client_async_with_config(request, stream, Some(websocket_config)),
    )
    .await
    .map_err(|_| {
        IoError::new(
            ErrorKind::TimedOut,
            format!("timed out upgrading remote app server at `{endpoint}`"),
        )
    })?
    .map(|(stream, _response)| stream)
    .map_err(|err| {
        IoError::other(format!(
            "failed to upgrade remote app server at `{endpoint}`: {err}"
        ))
    })?;

    Ok((endpoint, stream))
}

fn remote_websocket_config() -> WebSocketConfig {
    WebSocketConfig::default()
        .max_frame_size(Some(REMOTE_APP_SERVER_MAX_WEBSOCKET_MESSAGE_SIZE))
        .max_message_size(Some(REMOTE_APP_SERVER_MAX_WEBSOCKET_MESSAGE_SIZE))
}

async fn initialize_remote_connection<S>(
    stream: &mut WebSocketStream<S>,
    endpoint: &str,
    params: InitializeParams,
    initialize_timeout: Duration,
    inbound_server_request_ledger: &mut InboundServerRequestLedger,
) -> IoResult<(Vec<AppServerEvent>, Option<String>, Option<String>)>
where
    S: AsyncRead + AsyncWrite + Unpin,
{
    let initialize_request_id = RequestId::String("initialize".to_string());
    let mut pending_events = Vec::new();
    let mut server_version = None;
    let mut codex_home = None;
    write_jsonrpc_message(
        stream,
        JSONRPCMessage::Request(jsonrpc_request_from_client_request(
            ClientRequest::Initialize {
                request_id: initialize_request_id.clone(),
                params,
            },
        )),
        endpoint,
    )
    .await?;

    timeout(initialize_timeout, async {
        loop {
            match stream.next().await {
                Some(Ok(Message::Text(text))) => {
                    let message = serde_json::from_str::<JSONRPCMessage>(&text).map_err(|err| {
                        IoError::other(format!(
                            "remote app server at `{endpoint}` sent invalid initialize response: {err}"
                        ))
                    })?;
                    match message {
                        JSONRPCMessage::Response(response) if response.id == initialize_request_id => {
                            server_version = response
                                .result
                                .get("userAgent")
                                .and_then(serde_json::Value::as_str)
                                .and_then(|user_agent| {
                                    let (_, rest) = user_agent.split_once('/')?;
                                    rest.split_whitespace().next().map(str::to_string)
                                });
                            codex_home = response
                                .result
                                .get("codexHome")
                                .and_then(serde_json::Value::as_str)
                                .filter(|codex_home| !codex_home.is_empty())
                                .map(str::to_string);
                            break Ok(());
                        }
                        JSONRPCMessage::Error(error) if error.id == initialize_request_id => {
                            break Err(IoError::other(format!(
                                "remote app server at `{endpoint}` rejected initialize: {}",
                                error.error.message
                            )));
                        }
                        JSONRPCMessage::Notification(notification) => {
                            if let Some(event) = app_server_event_from_notification(notification) {
                                pending_events.push(event);
                            }
                        }
                        JSONRPCMessage::Request(request) => {
                            let request_id = request.id.clone();
                            let method = request.method.clone();
                            inbound_server_request_ledger.claim(&request_id)?;
                            match ServerRequest::try_from(request) {
                                Ok(request) => {
                                    pending_events.push(AppServerEvent::ServerRequest(request));
                                }
                                Err(err) => {
                                    warn!(%err, method, "rejecting unknown remote app-server request during initialize");
                                    inbound_server_request_ledger.reject(&request_id)?;
                                    write_jsonrpc_message(
                                        stream,
                                        JSONRPCMessage::Error(JSONRPCError {
                                            error: JSONRPCErrorError {
                                                code: -32601,
                                                message: format!(
                                                    "unsupported remote app-server request `{method}`"
                                                ),
                                                data: None,
                                            },
                                            id: request_id,
                                        }),
                                        endpoint,
                                    )
                                    .await?;
                                }
                            }
                        }
                        JSONRPCMessage::Response(_) | JSONRPCMessage::Error(_) => {}
                    }
                }
                Some(Ok(Message::Binary(_)))
                | Some(Ok(Message::Ping(_)))
                | Some(Ok(Message::Pong(_)))
                | Some(Ok(Message::Frame(_))) => {}
                Some(Ok(Message::Close(frame))) => {
                    let reason = frame
                        .as_ref()
                        .map(|frame| frame.reason.to_string())
                        .filter(|reason| !reason.is_empty())
                        .unwrap_or_else(|| "connection closed during initialize".to_string());
                    break Err(IoError::new(
                        ErrorKind::ConnectionAborted,
                        format!(
                            "remote app server at `{endpoint}` closed during initialize: {reason}"
                        ),
                    ));
                }
                Some(Err(err)) => {
                    break Err(IoError::other(format!(
                        "remote app server at `{endpoint}` transport failed during initialize: {err}"
                    )));
                }
                None => {
                    break Err(IoError::new(
                        ErrorKind::UnexpectedEof,
                        format!("remote app server at `{endpoint}` closed during initialize"),
                    ));
                }
            }
        }
    })
    .await
    .map_err(|_| {
        IoError::new(
            ErrorKind::TimedOut,
            format!("timed out waiting for initialize response from `{endpoint}`"),
        )
    })??;

    write_jsonrpc_message(
        stream,
        JSONRPCMessage::Notification(jsonrpc_notification_from_client_notification(
            ClientNotification::Initialized,
        )),
        endpoint,
    )
    .await?;

    Ok((pending_events, server_version, codex_home))
}

fn app_server_event_from_notification(notification: JSONRPCNotification) -> Option<AppServerEvent> {
    match ServerNotification::try_from(notification) {
        Ok(notification) => Some(AppServerEvent::ServerNotification(notification)),
        Err(_) => None,
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum RemoteEventForwardResult {
    Forwarded,
    Pending,
    DroppedBestEffort,
    Closed,
}

struct RemoteTerminalState {
    error_kind: ErrorKind,
    message: String,
    disconnected_pending: bool,
}

enum RemoteDisconnectedDelivery {
    Pending,
    Disabled,
}

impl RemoteTerminalState {
    fn error(&self) -> IoError {
        IoError::new(self.error_kind, self.message.clone())
    }
}

fn enter_remote_terminal_state(
    terminal_state: &mut Option<RemoteTerminalState>,
    pending_requests: &mut HashMap<RequestId, oneshot::Sender<IoResult<RequestResult>>>,
    error_kind: ErrorKind,
    message: String,
    disconnected_delivery: RemoteDisconnectedDelivery,
) {
    if terminal_state.is_none() {
        for (_, response_tx) in pending_requests.drain() {
            let _ = response_tx.send(Err(IoError::new(error_kind, message.clone())));
        }
        *terminal_state = Some(RemoteTerminalState {
            error_kind,
            message,
            disconnected_pending: matches!(
                disconnected_delivery,
                RemoteDisconnectedDelivery::Pending
            ),
        });
    }
}

fn disable_remote_event_delivery(
    terminal_state: &mut Option<RemoteTerminalState>,
    pending_requests: &mut HashMap<RequestId, oneshot::Sender<IoResult<RequestResult>>>,
) {
    enter_remote_terminal_state(
        terminal_state,
        pending_requests,
        ErrorKind::BrokenPipe,
        "remote app-server event consumer channel is closed".to_string(),
        RemoteDisconnectedDelivery::Disabled,
    );
    if let Some(terminal_state) = terminal_state {
        terminal_state.disconnected_pending = false;
    }
}

fn finish_remote_control_write(
    result: IoResult<()>,
    response_tx: oneshot::Sender<IoResult<()>>,
    endpoint: &str,
    terminal_state: &mut Option<RemoteTerminalState>,
    pending_requests: &mut HashMap<RequestId, oneshot::Sender<IoResult<RequestResult>>>,
) {
    match result {
        Ok(()) => {
            let _ = response_tx.send(Ok(()));
        }
        Err(err) => {
            let message = format!("remote app server at `{endpoint}` write failed: {err}");
            let fallback_error = IoError::new(ErrorKind::BrokenPipe, message.clone());
            enter_remote_terminal_state(
                terminal_state,
                pending_requests,
                ErrorKind::BrokenPipe,
                message,
                RemoteDisconnectedDelivery::Pending,
            );
            let error = terminal_state
                .as_ref()
                .map(RemoteTerminalState::error)
                .unwrap_or(fallback_error);
            let _ = response_tx.send(Err(error));
        }
    }
}

/// Attempts to deliver one remote event without waiting for consumer capacity.
///
/// The worker retains at most one required event in `pending_event`. Best-effort
/// notification loss is represented by `skipped_events`; the worker's permit
/// branch emits that lag count before polling the WebSocket again.
fn try_deliver_event(
    event_tx: &mpsc::Sender<AppServerEvent>,
    skipped_events: &mut usize,
    pending_event: &mut Option<AppServerEvent>,
    event: AppServerEvent,
) -> RemoteEventForwardResult {
    // A lag marker is a FIFO barrier: events decoded after a dropped
    // best-effort notification must not overtake it, even if the consumer
    // drains a queue slot before the worker's permit branch runs.
    if *skipped_events > 0 {
        if remote_event_requires_delivery(&event) {
            debug_assert!(
                pending_event.is_none(),
                "worker must stop polling the WebSocket while required custody is occupied"
            );
            *pending_event = Some(event);
            return RemoteEventForwardResult::Pending;
        }
        *skipped_events = skipped_events.saturating_add(1);
        warn!("dropping remote app-server event because consumer queue is full");
        return RemoteEventForwardResult::DroppedBestEffort;
    }

    if remote_event_requires_delivery(&event) {
        match event_tx.try_send(event) {
            Ok(()) => RemoteEventForwardResult::Forwarded,
            Err(mpsc::error::TrySendError::Full(event)) => {
                debug_assert!(
                    pending_event.is_none(),
                    "worker must stop polling the WebSocket while required custody is occupied"
                );
                *pending_event = Some(event);
                RemoteEventForwardResult::Pending
            }
            Err(mpsc::error::TrySendError::Closed(_)) => RemoteEventForwardResult::Closed,
        }
    } else {
        match event_tx.try_send(event) {
            Ok(()) => RemoteEventForwardResult::Forwarded,
            Err(mpsc::error::TrySendError::Full(_)) => {
                *skipped_events = skipped_events.saturating_add(1);
                warn!("dropping remote app-server event because consumer queue is full");
                RemoteEventForwardResult::DroppedBestEffort
            }
            Err(mpsc::error::TrySendError::Closed(_)) => RemoteEventForwardResult::Closed,
        }
    }
}

fn remote_event_requires_delivery(event: &AppServerEvent) -> bool {
    match event {
        AppServerEvent::Lagged { .. } => false,
        AppServerEvent::ServerNotification(notification) => {
            server_notification_requires_delivery(notification)
        }
        AppServerEvent::ServerRequest(_) | AppServerEvent::Disconnected { .. } => true,
    }
}

fn jsonrpc_request_from_client_request(request: ClientRequest) -> JSONRPCRequest {
    let value = match serde_json::to_value(request) {
        Ok(value) => value,
        Err(err) => panic!("client request should serialize: {err}"),
    };
    match serde_json::from_value(value) {
        Ok(request) => request,
        Err(err) => panic!("client request should encode as JSON-RPC request: {err}"),
    }
}

fn jsonrpc_notification_from_client_notification(
    notification: ClientNotification,
) -> JSONRPCNotification {
    let value = match serde_json::to_value(notification) {
        Ok(value) => value,
        Err(err) => panic!("client notification should serialize: {err}"),
    };
    match serde_json::from_value(value) {
        Ok(notification) => notification,
        Err(err) => panic!("client notification should encode as JSON-RPC notification: {err}"),
    }
}

async fn write_jsonrpc_message<S>(
    stream: &mut WebSocketStream<S>,
    message: JSONRPCMessage,
    endpoint: &str,
) -> IoResult<()>
where
    S: AsyncRead + AsyncWrite + Unpin,
{
    let payload = serde_json::to_string(&message).map_err(IoError::other)?;
    stream
        .send(Message::Text(payload.into()))
        .await
        .map_err(|err| {
            IoError::other(format!(
                "failed to write websocket message to `{endpoint}`: {err}"
            ))
        })
}

async fn close_remote_stream<S>(stream: &mut WebSocketStream<S>, endpoint: &str) -> IoResult<()>
where
    S: AsyncRead + AsyncWrite + Unpin,
{
    stream.close(None).await.or_else(|err| {
        if websocket_close_error_is_already_closed(&err) {
            Ok(())
        } else {
            Err(IoError::other(format!(
                "failed to close websocket app server `{endpoint}`: {err}"
            )))
        }
    })
}

fn websocket_close_error_is_already_closed(err: &TungsteniteError) -> bool {
    match err {
        TungsteniteError::ConnectionClosed | TungsteniteError::AlreadyClosed => true,
        TungsteniteError::Protocol(ProtocolError::SendAfterClosing) => true,
        TungsteniteError::Io(err) => matches!(
            err.kind(),
            ErrorKind::BrokenPipe | ErrorKind::ConnectionReset | ErrorKind::NotConnected
        ),
        _ => false,
    }
}
#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn lag_barrier_prevents_required_event_overtake_after_consumer_drain() {
        let (event_tx, mut event_rx) = mpsc::channel(1);
        let mut skipped_events = 0;
        let mut pending_event = None;

        event_tx
            .try_send(AppServerEvent::Lagged { skipped: 1 })
            .expect("test queue should start full");
        assert_eq!(
            try_deliver_event(
                &event_tx,
                &mut skipped_events,
                &mut pending_event,
                AppServerEvent::Lagged { skipped: 1 },
            ),
            RemoteEventForwardResult::DroppedBestEffort
        );
        assert_eq!(skipped_events, 1);

        assert!(matches!(
            event_rx.recv().await,
            Some(AppServerEvent::Lagged { .. })
        ));
        assert_eq!(
            try_deliver_event(
                &event_tx,
                &mut skipped_events,
                &mut pending_event,
                AppServerEvent::Disconnected {
                    message: "required".to_string(),
                },
            ),
            RemoteEventForwardResult::Pending
        );

        event_tx
            .try_send(AppServerEvent::Lagged {
                skipped: std::mem::take(&mut skipped_events),
            })
            .expect("lag marker should be delivered first");
        assert!(matches!(
            event_rx.recv().await,
            Some(AppServerEvent::Lagged { skipped: 1 })
        ));
        event_tx
            .try_send(
                pending_event
                    .take()
                    .expect("required event should be retained"),
            )
            .expect("required event should follow lag marker");
        assert!(matches!(
            event_rx.recv().await,
            Some(AppServerEvent::Disconnected { message }) if message == "required"
        ));
    }

    #[test]
    fn close_error_after_close_frame_is_idempotent() {
        assert!(websocket_close_error_is_already_closed(
            &TungsteniteError::Protocol(ProtocolError::SendAfterClosing),
        ));
    }

    #[tokio::test]
    async fn shutdown_tolerates_worker_exit_after_command_is_queued() {
        let (command_tx, mut command_rx) = mpsc::channel(1);
        let (_event_tx, event_rx) = mpsc::channel::<AppServerEvent>(1);
        let worker_handle = tokio::spawn(async move {
            let _ = command_rx.recv().await;
        });
        let client = RemoteAppServerClient {
            command_tx,
            event_rx,
            pending_events: VecDeque::new(),
            server_version: None,
            codex_home: None,
            worker_handle,
            _test_pending_required_event: std::sync::Arc::new(tokio::sync::Notify::new()),
            _test_pending_lag: std::sync::Arc::new(tokio::sync::Notify::new()),
        };

        client
            .shutdown()
            .await
            .expect("shutdown should complete when worker exits first");
    }
}
