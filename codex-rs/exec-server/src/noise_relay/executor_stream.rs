//! One executor-side virtual stream after the Noise handshake.
//!
//! The environment loop owns reads and a per-stream task owns writes. They share
//! `NoiseTransport` because its send and receive nonces live in the same value;
//! the mutex is never held across `.await`.

use std::sync::Arc;
use std::sync::Mutex;

use codex_exec_server_protocol::CAPABILITY_ROOTS_DISCOVER_METHOD;
use codex_exec_server_protocol::ENVIRONMENT_INFO_METHOD;
use codex_exec_server_protocol::ENVIRONMENT_STATUS_METHOD;
use codex_exec_server_protocol::EXEC_CLOSED_METHOD;
use codex_exec_server_protocol::EXEC_EXITED_METHOD;
use codex_exec_server_protocol::EXEC_METHOD;
use codex_exec_server_protocol::EXEC_OUTPUT_DELTA_METHOD;
use codex_exec_server_protocol::EXEC_READ_METHOD;
use codex_exec_server_protocol::EXEC_SIGNAL_METHOD;
use codex_exec_server_protocol::EXEC_TERMINATE_METHOD;
use codex_exec_server_protocol::EXEC_WRITE_METHOD;
use codex_exec_server_protocol::FS_CANONICALIZE_METHOD;
use codex_exec_server_protocol::FS_CLOSE_METHOD;
use codex_exec_server_protocol::FS_COPY_METHOD;
use codex_exec_server_protocol::FS_CREATE_DIRECTORY_METHOD;
use codex_exec_server_protocol::FS_GET_METADATA_METHOD;
use codex_exec_server_protocol::FS_OPEN_METHOD;
use codex_exec_server_protocol::FS_READ_BLOCK_METHOD;
use codex_exec_server_protocol::FS_READ_DIRECTORY_METHOD;
use codex_exec_server_protocol::FS_READ_FILE_METHOD;
use codex_exec_server_protocol::FS_REMOVE_METHOD;
use codex_exec_server_protocol::FS_WALK_METHOD;
use codex_exec_server_protocol::FS_WRITE_FILE_METHOD;
use codex_exec_server_protocol::HTTP_REQUEST_BODY_DELTA_METHOD;
use codex_exec_server_protocol::HTTP_REQUEST_METHOD;
use codex_exec_server_protocol::INITIALIZE_METHOD;
use codex_exec_server_protocol::INITIALIZED_METHOD;
use codex_exec_server_protocol::JSONRPCMessage;
use codex_exec_server_protocol::NETWORK_POLICY_REQUEST_METHOD;
use tokio::sync::mpsc;
use tokio::sync::watch;
use tracing::Instrument;
use tracing::Span;
use tracing::warn;

use crate::ExecServerError;
use crate::connection::CHANNEL_CAPACITY;
use crate::connection::JsonRpcConnection;
use crate::connection::JsonRpcConnectionEvent;
use crate::connection::JsonRpcTransport;
use crate::noise_channel::NoiseTransport;
use crate::noise_relay::NOISE_RELAY_RESET_REASON;
use crate::noise_relay::message_framing::JsonRpcMessageDecoder;
use crate::noise_relay::message_framing::NOISE_RECORD_PLAINTEXT_LEN;
use crate::noise_relay::message_framing::frame_jsonrpc_message;
use crate::noise_relay::ordered_ciphertext::OrderedCiphertextFrames;
use crate::noise_relay::take_next_sequence;
use crate::noise_relay::trace_context::NoiseTraceContext;
use crate::relay::encode_relay_message_frame;
use crate::relay_proto::RelayData;
use crate::relay_proto::RelayMessageFrame;
use crate::server::ConnectionProcessor;
use crate::telemetry::ConnectionTransport;

/// Identifies one completed virtual-stream instance.
///
/// Stream IDs are supplied by the untrusted relay peer and may be reused. The
/// instance ID prevents a delayed writer notification from removing a newer
/// stream that happens to use the same routing ID.
pub(crate) struct ClosedNoiseVirtualStream {
    pub(crate) stream_id: String,
    pub(crate) instance_id: u64,
}

/// One authenticated JSON-RPC stream carried by the executor's physical relay.
///
/// Inbound delivery is intentionally nonblocking. An overloaded or abandoned
/// stream fails independently instead of stalling every stream multiplexed over
/// the same physical websocket.
pub(crate) struct NoiseVirtualStream {
    incoming_tx: mpsc::Sender<JsonRpcConnectionEvent>,
    disconnected_tx: watch::Sender<bool>,
    transport: Arc<Mutex<NoiseTransport>>,
    inbound_ciphertexts: OrderedCiphertextFrames,
    inbound_decoder: JsonRpcMessageDecoder,
    trace_context: Arc<Mutex<NoiseTraceContext>>,
    pub(crate) instance_id: u64,
}

impl NoiseVirtualStream {
    pub(crate) fn disconnect(self, reason: Option<String>) {
        let _ = self.disconnected_tx.send(true);
        let _ = self
            .incoming_tx
            .try_send(JsonRpcConnectionEvent::Disconnected { reason });
    }

    /// Reorder and decrypt one inbound record, then queue complete JSON-RPC messages.
    /// This must stay nonblocking because all virtual streams share the read loop.
    pub(crate) fn receive_data(&mut self, data: RelayData) -> Result<(), ExecServerError> {
        for ciphertext in self.inbound_ciphertexts.push(data.seq, data.payload)? {
            let plaintext = {
                let mut transport = self
                    .transport
                    .lock()
                    .unwrap_or_else(std::sync::PoisonError::into_inner);
                transport.decrypt(&ciphertext).map_err(|error| {
                    ExecServerError::Protocol(format!("Noise relay decryption failed: {error}"))
                })?
            };
            for message in self.inbound_decoder.push(&plaintext)? {
                self.trace_context
                    .lock()
                    .unwrap_or_else(std::sync::PoisonError::into_inner)
                    .observe_request(&message);
                self.incoming_tx
                    .try_send(JsonRpcConnectionEvent::Message(message))
                    .map_err(|_| {
                        ExecServerError::Protocol(
                            "Noise virtual stream inbound queue is full or closed".to_string(),
                        )
                    })?;
            }
        }
        Ok(())
    }
}

/// Start JSON-RPC processing for a completed handshake.
///
/// The returned value is the read half; the spawned task owns outbound framing
/// and reports its instance ID on exit so stream-ID reuse is safe.
pub(crate) fn spawn_noise_virtual_stream(
    stream_id: String,
    instance_id: u64,
    processor: ConnectionProcessor,
    physical_outgoing_tx: mpsc::Sender<Vec<u8>>,
    closed_stream_tx: mpsc::Sender<ClosedNoiseVirtualStream>,
    transport: NoiseTransport,
) -> NoiseVirtualStream {
    let (json_outgoing_tx, mut json_outgoing_rx) = mpsc::channel(CHANNEL_CAPACITY);
    let (incoming_tx, incoming_rx) = mpsc::channel(CHANNEL_CAPACITY);
    let (disconnected_tx, disconnected_rx) = watch::channel(false);
    let transport = Arc::new(Mutex::new(transport));
    let writer_transport = Arc::clone(&transport);
    let trace_context = Arc::new(Mutex::new(NoiseTraceContext::default()));
    let writer_trace_context = Arc::clone(&trace_context);
    let processor_stream_id = stream_id.clone();
    let processor_closed_stream_tx = closed_stream_tx.clone();
    let writer_stream_id = stream_id;
    let writer_task = tokio::spawn(async move {
        let mut next_seq = 0u32;
        while let Some(message) = json_outgoing_rx.recv().await {
            let span = outbound_message_span(&message, &writer_trace_context);
            if let Err(error) = send_outbound_message(
                &physical_outgoing_tx,
                &writer_transport,
                &writer_stream_id,
                &mut next_seq,
                &message,
            )
            .instrument(span)
            .await
            {
                warn!("failed to send Noise virtual stream JSON-RPC payload: {error}");
                break;
            }
        }

        // The reset is best effort; the local close notification is not.
        let closed_stream = ClosedNoiseVirtualStream {
            stream_id: writer_stream_id.clone(),
            instance_id,
        };
        let reset =
            RelayMessageFrame::reset(writer_stream_id, NOISE_RELAY_RESET_REASON.to_string());
        let _ = physical_outgoing_tx.try_send(encode_relay_message_frame(&reset));
        let _ = closed_stream_tx.send(closed_stream).await;
    });

    let connection = JsonRpcConnection {
        outgoing_tx: json_outgoing_tx,
        incoming_rx,
        disconnected_rx,
        task_handles: vec![writer_task],
        transport: JsonRpcTransport::Plain,
    };
    tokio::spawn(async move {
        processor
            .run_connection(connection, ConnectionTransport::Relay)
            .await;
        let _ = processor_closed_stream_tx
            .send(ClosedNoiseVirtualStream {
                stream_id: processor_stream_id,
                instance_id,
            })
            .await;
    });

    NoiseVirtualStream {
        incoming_tx,
        disconnected_tx,
        transport,
        inbound_ciphertexts: OrderedCiphertextFrames::default(),
        inbound_decoder: JsonRpcMessageDecoder::default(),
        trace_context,
        instance_id,
    }
}

fn outbound_message_span(
    message: &JSONRPCMessage,
    trace_context: &Mutex<NoiseTraceContext>,
) -> Span {
    let (message_kind, method) = match message {
        JSONRPCMessage::Request(request) => ("request", protocol_method_label(&request.method)),
        JSONRPCMessage::Notification(notification) => {
            ("notification", protocol_method_label(&notification.method))
        }
        JSONRPCMessage::Response(_) => ("response", ""),
        JSONRPCMessage::Error(_) => ("error", ""),
    };
    let trace = trace_context
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
        .return_trace(message);
    let span = tracing::info_span!("exec_server.noise.executor_outbound", message_kind, method,);
    if let Some(trace) = trace.as_ref() {
        let _ = codex_otel::set_parent_from_w3c_trace_context(&span, trace);
    }
    span
}

fn protocol_method_label(method: &str) -> &'static str {
    match method {
        INITIALIZE_METHOD => INITIALIZE_METHOD,
        INITIALIZED_METHOD => INITIALIZED_METHOD,
        EXEC_METHOD => EXEC_METHOD,
        EXEC_READ_METHOD => EXEC_READ_METHOD,
        EXEC_WRITE_METHOD => EXEC_WRITE_METHOD,
        EXEC_SIGNAL_METHOD => EXEC_SIGNAL_METHOD,
        EXEC_TERMINATE_METHOD => EXEC_TERMINATE_METHOD,
        EXEC_OUTPUT_DELTA_METHOD => EXEC_OUTPUT_DELTA_METHOD,
        EXEC_EXITED_METHOD => EXEC_EXITED_METHOD,
        EXEC_CLOSED_METHOD => EXEC_CLOSED_METHOD,
        ENVIRONMENT_INFO_METHOD => ENVIRONMENT_INFO_METHOD,
        ENVIRONMENT_STATUS_METHOD => ENVIRONMENT_STATUS_METHOD,
        FS_READ_FILE_METHOD => FS_READ_FILE_METHOD,
        FS_OPEN_METHOD => FS_OPEN_METHOD,
        FS_READ_BLOCK_METHOD => FS_READ_BLOCK_METHOD,
        FS_CLOSE_METHOD => FS_CLOSE_METHOD,
        FS_WRITE_FILE_METHOD => FS_WRITE_FILE_METHOD,
        FS_CREATE_DIRECTORY_METHOD => FS_CREATE_DIRECTORY_METHOD,
        FS_GET_METADATA_METHOD => FS_GET_METADATA_METHOD,
        FS_CANONICALIZE_METHOD => FS_CANONICALIZE_METHOD,
        FS_READ_DIRECTORY_METHOD => FS_READ_DIRECTORY_METHOD,
        FS_WALK_METHOD => FS_WALK_METHOD,
        FS_REMOVE_METHOD => FS_REMOVE_METHOD,
        FS_COPY_METHOD => FS_COPY_METHOD,
        CAPABILITY_ROOTS_DISCOVER_METHOD => CAPABILITY_ROOTS_DISCOVER_METHOD,
        HTTP_REQUEST_METHOD => HTTP_REQUEST_METHOD,
        HTTP_REQUEST_BODY_DELTA_METHOD => HTTP_REQUEST_BODY_DELTA_METHOD,
        NETWORK_POLICY_REQUEST_METHOD => NETWORK_POLICY_REQUEST_METHOD,
        _ => "unknown",
    }
}

async fn send_outbound_message(
    physical_outgoing_tx: &mpsc::Sender<Vec<u8>>,
    transport: &Mutex<NoiseTransport>,
    stream_id: &str,
    next_seq: &mut u32,
    message: &JSONRPCMessage,
) -> Result<(), ExecServerError> {
    let framed = frame_jsonrpc_message(message)?;
    for plaintext_record in framed.chunks(NOISE_RECORD_PLAINTEXT_LEN) {
        let seq = take_next_sequence(next_seq)?;
        let ciphertext = transport
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .encrypt(plaintext_record)
            .map_err(|error| {
                ExecServerError::Protocol(format!(
                    "failed to encrypt Noise virtual stream payload: {error}"
                ))
            })?;
        let frame = RelayMessageFrame::data(stream_id.to_string(), seq, ciphertext);
        physical_outgoing_tx
            .send(encode_relay_message_frame(&frame))
            .await
            .map_err(|_| ExecServerError::Closed)?;
    }
    Ok(())
}

#[cfg(test)]
#[path = "executor_stream_tests.rs"]
mod tests;
