use codex_exec_server_protocol::EXEC_CLOSED_METHOD;
use codex_exec_server_protocol::EXEC_METHOD;
use codex_exec_server_protocol::EXEC_OUTPUT_DELTA_METHOD;
use codex_exec_server_protocol::JSONRPCError;
use codex_exec_server_protocol::JSONRPCErrorError;
use codex_exec_server_protocol::JSONRPCMessage;
use codex_exec_server_protocol::JSONRPCNotification;
use codex_exec_server_protocol::JSONRPCRequest;
use codex_exec_server_protocol::JSONRPCResponse;
use codex_exec_server_protocol::RequestId;
use codex_protocol::protocol::W3cTraceContext;

use super::MAX_PROCESS_ID_BYTES;
use super::MAX_REQUEST_ID_BYTES;
use super::MAX_TRACE_CONTEXT_BYTES_PER_MAP;
use super::MAX_TRACE_CONTEXT_ENTRIES;
use super::MAX_TRACEPARENT_BYTES;
use super::MAX_TRACESTATE_BYTES;
use super::NoiseTraceContext;

fn trace_context() -> W3cTraceContext {
    W3cTraceContext {
        traceparent: Some("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01".to_string()),
        tracestate: Some("vendor=value".to_string()),
    }
}

#[test]
fn oversized_peer_carriers_cannot_inflate_retained_trace_state() {
    let oversized_request_id = "r".repeat(MAX_REQUEST_ID_BYTES + 1);
    let oversized_process_id = "p".repeat(MAX_PROCESS_ID_BYTES + 1);
    let oversized_traceparent = "t".repeat(MAX_TRACEPARENT_BYTES + 1);
    let oversized_tracestate = "s".repeat(MAX_TRACESTATE_BYTES + 1);
    let mut context = NoiseTraceContext::default();

    context.observe_request(&JSONRPCMessage::Request(JSONRPCRequest {
        id: RequestId::String(oversized_request_id),
        method: EXEC_METHOD.to_string(),
        params: Some(serde_json::json!({"processId": oversized_process_id})),
        trace: Some(trace_context()),
    }));
    context.observe_request(&JSONRPCMessage::Request(JSONRPCRequest {
        id: RequestId::Integer(1),
        method: EXEC_METHOD.to_string(),
        params: Some(serde_json::json!({"processId": "process-traceparent"})),
        trace: Some(W3cTraceContext {
            traceparent: Some(oversized_traceparent),
            tracestate: None,
        }),
    }));
    context.observe_request(&JSONRPCMessage::Request(JSONRPCRequest {
        id: RequestId::Integer(2),
        method: EXEC_METHOD.to_string(),
        params: Some(serde_json::json!({"processId": "process-tracestate"})),
        trace: Some(W3cTraceContext {
            traceparent: None,
            tracestate: Some(oversized_tracestate),
        }),
    }));

    assert_eq!(context.state_sizes(), (0, 0));
    assert_eq!(context.retained_bytes(), 0);

    for index in 0..(MAX_TRACE_CONTEXT_ENTRIES * 2) {
        context.observe_request(&JSONRPCMessage::Request(JSONRPCRequest {
            id: RequestId::String(format!(
                "{index:04}{}",
                "r".repeat(MAX_REQUEST_ID_BYTES - 4)
            )),
            method: EXEC_METHOD.to_string(),
            params: Some(serde_json::json!({
                "processId": format!("{index:04}{}", "p".repeat(MAX_PROCESS_ID_BYTES - 4)),
            })),
            trace: Some(W3cTraceContext {
                traceparent: Some("t".repeat(MAX_TRACEPARENT_BYTES)),
                tracestate: Some("s".repeat(MAX_TRACESTATE_BYTES)),
            }),
        }));
    }

    assert!(context.state_sizes().0 <= MAX_TRACE_CONTEXT_ENTRIES);
    assert!(context.state_sizes().1 <= MAX_TRACE_CONTEXT_ENTRIES);
    assert!(context.retained_bytes() <= MAX_TRACE_CONTEXT_BYTES_PER_MAP * 2);
}

fn process_start_request_with_id(id: i64, trace: W3cTraceContext) -> JSONRPCMessage {
    process_start_request_for_process(id, "process-1", trace)
}

fn process_start_request_for_process(
    id: i64,
    process_id: &str,
    trace: W3cTraceContext,
) -> JSONRPCMessage {
    JSONRPCMessage::Request(JSONRPCRequest {
        id: RequestId::Integer(id),
        method: EXEC_METHOD.to_string(),
        params: Some(serde_json::json!({"processId": process_id})),
        trace: Some(trace),
    })
}

fn process_start_request(trace: W3cTraceContext) -> JSONRPCMessage {
    process_start_request_with_id(7, trace)
}

fn process_notification() -> JSONRPCMessage {
    JSONRPCMessage::Notification(JSONRPCNotification {
        method: EXEC_OUTPUT_DELTA_METHOD.to_string(),
        params: Some(serde_json::json!({"processId": "process-1"})),
    })
}

#[test]
fn correlates_response_and_terminal_notification_with_request_trace() {
    let trace = trace_context();
    let mut context = NoiseTraceContext::default();
    context.observe_request(&process_start_request(trace.clone()));

    let response = JSONRPCMessage::Response(JSONRPCResponse {
        id: RequestId::Integer(7),
        result: serde_json::Value::Null,
    });
    assert_eq!(context.return_trace(&response), Some(trace.clone()));
    assert_eq!(context.return_trace(&response), None);

    let closed = JSONRPCMessage::Notification(JSONRPCNotification {
        method: EXEC_CLOSED_METHOD.to_string(),
        params: Some(serde_json::json!({"processId": "process-1"})),
    });
    assert_eq!(context.return_trace(&closed), Some(trace));
    assert_eq!(context.return_trace(&closed), None);
}

#[test]
fn closed_process_before_start_response_cannot_be_resurrected() {
    let trace = trace_context();
    let mut context = NoiseTraceContext::default();
    context.observe_request(&process_start_request(trace.clone()));

    // Output which races the start response still uses the provisional trace.
    assert_eq!(
        context.return_trace(&process_notification()),
        Some(trace.clone())
    );

    let closed = JSONRPCMessage::Notification(JSONRPCNotification {
        method: EXEC_CLOSED_METHOD.to_string(),
        params: Some(serde_json::json!({"processId": "process-1"})),
    });
    assert_eq!(context.return_trace(&closed), Some(trace.clone()));

    // The response itself is still correlated to its request, but it must not
    // promote the already-closed process mapping.
    assert_eq!(
        context.return_trace(&JSONRPCMessage::Response(JSONRPCResponse {
            id: RequestId::Integer(7),
            result: serde_json::Value::Null,
        })),
        Some(trace)
    );
    assert_eq!(context.return_trace(&process_notification()), None);
}

#[test]
fn closed_process_tombstone_survives_duplicate_start_responses() {
    let first_trace = trace_context();
    let second_trace = W3cTraceContext {
        traceparent: Some("00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-01".to_string()),
        tracestate: None,
    };
    let mut context = NoiseTraceContext::default();
    context.observe_request(&process_start_request_with_id(1, first_trace.clone()));
    context.observe_request(&process_start_request_with_id(2, second_trace.clone()));

    let closed = JSONRPCMessage::Notification(JSONRPCNotification {
        method: EXEC_CLOSED_METHOD.to_string(),
        params: Some(serde_json::json!({"processId": "process-1"})),
    });
    assert_eq!(context.return_trace(&closed), Some(first_trace.clone()));

    // Both late responses remain request-correlated, but neither may restore
    // a process mapping after the terminal notification.
    assert_eq!(
        context.return_trace(&JSONRPCMessage::Response(JSONRPCResponse {
            id: RequestId::Integer(1),
            result: serde_json::Value::Null,
        })),
        Some(first_trace)
    );
    assert_eq!(
        context.return_trace(&JSONRPCMessage::Response(JSONRPCResponse {
            id: RequestId::Integer(2),
            result: serde_json::Value::Null,
        })),
        Some(second_trace)
    );
    assert_eq!(context.return_trace(&process_notification()), None);
}

#[test]
fn unrelated_request_process_ids_do_not_poison_notification_correlation() {
    let trace = trace_context();
    let mut context = NoiseTraceContext::default();
    context.observe_request(&JSONRPCMessage::Request(JSONRPCRequest {
        id: RequestId::Integer(1),
        method: "process/read".to_string(),
        params: Some(serde_json::json!({"processId": "process-1"})),
        trace: Some(trace.clone()),
    }));

    assert_eq!(context.return_trace(&process_notification()), None);
    assert_eq!(
        context.return_trace(&JSONRPCMessage::Response(JSONRPCResponse {
            id: RequestId::Integer(1),
            result: serde_json::Value::Null,
        })),
        Some(trace)
    );
}

#[test]
fn failed_process_start_discards_provisional_notification_correlation() {
    let trace = trace_context();
    let mut context = NoiseTraceContext::default();
    context.observe_request(&process_start_request(trace.clone()));

    // Notifications can race the start response and use the provisional
    // carrier, but a rejected start must not leave it behind.
    assert_eq!(
        context.return_trace(&process_notification()),
        Some(trace.clone())
    );
    assert_eq!(
        context.return_trace(&JSONRPCMessage::Error(JSONRPCError {
            id: RequestId::Integer(7),
            error: JSONRPCErrorError {
                code: -1,
                message: "rejected".to_string(),
                data: None,
            },
        })),
        Some(trace)
    );
    assert_eq!(context.return_trace(&process_notification()), None);
}

#[test]
fn duplicate_process_starts_preserve_first_trace_and_reassign_after_failure() {
    let first_trace = trace_context();
    let second_trace = W3cTraceContext {
        traceparent: Some("00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-01".to_string()),
        tracestate: None,
    };
    let mut context = NoiseTraceContext::default();
    context.observe_request(&process_start_request_with_id(1, first_trace.clone()));
    context.observe_request(&process_start_request_with_id(2, second_trace.clone()));

    // The early notification uses the first provisional carrier.
    assert_eq!(
        context.return_trace(&process_notification()),
        Some(first_trace.clone())
    );

    // Once the first start fails, the outstanding duplicate becomes the
    // provisional owner instead of being lost.
    assert_eq!(
        context.return_trace(&JSONRPCMessage::Error(JSONRPCError {
            id: RequestId::Integer(1),
            error: JSONRPCErrorError {
                code: -1,
                message: "rejected".to_string(),
                data: None,
            },
        })),
        Some(first_trace.clone())
    );
    assert_eq!(
        context.return_trace(&process_notification()),
        Some(second_trace.clone())
    );

    // A successful duplicate promotes the mapping, and a later duplicate
    // response cannot replace it.
    assert_eq!(
        context.return_trace(&JSONRPCMessage::Response(JSONRPCResponse {
            id: RequestId::Integer(2),
            result: serde_json::Value::Null,
        })),
        Some(second_trace.clone())
    );
    context.observe_request(&process_start_request_with_id(3, first_trace));
    assert_eq!(
        context.return_trace(&JSONRPCMessage::Response(JSONRPCResponse {
            id: RequestId::Integer(3),
            result: serde_json::Value::Null,
        })),
        Some(trace_context())
    );
    assert_eq!(
        context.return_trace(&process_notification()),
        Some(second_trace)
    );
}

#[test]
fn unfinished_peer_ids_cannot_grow_trace_state_without_bound() {
    let trace = trace_context();
    let mut context = NoiseTraceContext::default();
    for index in 0..(MAX_TRACE_CONTEXT_ENTRIES + 64) {
        context.observe_request(&JSONRPCMessage::Request(JSONRPCRequest {
            id: RequestId::Integer(index as i64),
            method: EXEC_METHOD.to_string(),
            params: Some(serde_json::json!({"processId": format!("process-{index}")})),
            trace: Some(trace.clone()),
        }));
    }

    assert_eq!(
        context.state_sizes(),
        (MAX_TRACE_CONTEXT_ENTRIES, MAX_TRACE_CONTEXT_ENTRIES)
    );
    let evicted_response = JSONRPCMessage::Response(JSONRPCResponse {
        id: RequestId::Integer(0),
        result: serde_json::Value::Null,
    });
    assert_eq!(context.return_trace(&evicted_response), None);
    let newest_response = JSONRPCMessage::Response(JSONRPCResponse {
        id: RequestId::Integer((MAX_TRACE_CONTEXT_ENTRIES + 63) as i64),
        result: serde_json::Value::Null,
    });
    assert_eq!(context.return_trace(&newest_response), Some(trace));
}

#[test]
fn evicting_closed_tombstone_fences_pending_request() {
    let trace = trace_context();
    let mut context = NoiseTraceContext::default();
    context.observe_request(&process_start_request(trace.clone()));

    let closed = JSONRPCMessage::Notification(JSONRPCNotification {
        method: EXEC_CLOSED_METHOD.to_string(),
        params: Some(serde_json::json!({"processId": "process-1"})),
    });
    assert_eq!(context.return_trace(&closed), Some(trace.clone()));

    // Completed starts leave established process mappings behind while their
    // requests are consumed. Once the process bound is exceeded, the closed
    // tombstone is evicted and its pending request must be evicted with it.
    for index in 0..MAX_TRACE_CONTEXT_ENTRIES {
        let request_id = index as i64 + 1000;
        let process_id = format!("process-fill-{index}");
        context.observe_request(&process_start_request_for_process(
            request_id,
            &process_id,
            trace.clone(),
        ));
        assert_eq!(
            context.return_trace(&JSONRPCMessage::Response(JSONRPCResponse {
                id: RequestId::Integer(request_id),
                result: serde_json::Value::Null,
            })),
            Some(trace.clone())
        );
    }

    // The late response remains request-correlated, but its evicted process
    // generation cannot recreate process-1.
    assert_eq!(
        context.return_trace(&JSONRPCMessage::Response(JSONRPCResponse {
            id: RequestId::Integer(7),
            result: serde_json::Value::Null,
        })),
        Some(trace)
    );
    assert_eq!(context.return_trace(&process_notification()), None);
}

#[test]
fn evicting_closed_tombstone_fences_duplicate_start_requests() {
    let first_trace = trace_context();
    let second_trace = W3cTraceContext {
        traceparent: Some("00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-01".to_string()),
        tracestate: None,
    };
    let mut context = NoiseTraceContext::default();
    context.observe_request(&process_start_request_with_id(1, first_trace.clone()));
    context.observe_request(&process_start_request_with_id(2, second_trace.clone()));

    let closed = JSONRPCMessage::Notification(JSONRPCNotification {
        method: EXEC_CLOSED_METHOD.to_string(),
        params: Some(serde_json::json!({"processId": "process-1"})),
    });
    assert_eq!(context.return_trace(&closed), Some(first_trace.clone()));

    // Fill the process bound so the closed tombstone is evicted while the
    // second start request remains retained in the request map.
    for index in 0..MAX_TRACE_CONTEXT_ENTRIES {
        let request_id = index as i64 + 1000;
        let process_id = format!("process-fill-{index}");
        context.observe_request(&process_start_request_for_process(
            request_id,
            &process_id,
            first_trace.clone(),
        ));
        assert_eq!(
            context.return_trace(&JSONRPCMessage::Response(JSONRPCResponse {
                id: RequestId::Integer(request_id),
                result: serde_json::Value::Null,
            })),
            Some(first_trace.clone())
        );
    }

    // The late duplicate response is still request-correlated, but its
    // evicted process generation can no longer recreate process-1.
    assert_eq!(
        context.return_trace(&JSONRPCMessage::Response(JSONRPCResponse {
            id: RequestId::Integer(2),
            result: serde_json::Value::Null,
        })),
        Some(second_trace)
    );
    assert_eq!(context.return_trace(&process_notification()), None);
}

#[test]
fn evicting_open_provisional_process_fences_late_start_response() {
    let trace = trace_context();
    let mut context = NoiseTraceContext::default();
    context.observe_request(&process_start_request(trace.clone()));

    // Evict process-1 while its start is still provisional. The request is
    // retained for response tracing, but its process association is fenced.
    for index in 0..MAX_TRACE_CONTEXT_ENTRIES {
        let request_id = index as i64 + 1000;
        let process_id = format!("process-fill-{index}");
        context.observe_request(&process_start_request_for_process(
            request_id,
            &process_id,
            trace.clone(),
        ));
        assert_eq!(
            context.return_trace(&JSONRPCMessage::Response(JSONRPCResponse {
                id: RequestId::Integer(request_id),
                result: serde_json::Value::Null,
            })),
            Some(trace.clone())
        );
    }

    let closed = JSONRPCMessage::Notification(JSONRPCNotification {
        method: EXEC_CLOSED_METHOD.to_string(),
        params: Some(serde_json::json!({"processId": "process-1"})),
    });
    assert_eq!(context.return_trace(&closed), None);
    assert_eq!(
        context.return_trace(&JSONRPCMessage::Response(JSONRPCResponse {
            id: RequestId::Integer(7),
            result: serde_json::Value::Null,
        })),
        Some(trace)
    );
    assert_eq!(context.return_trace(&process_notification()), None);
}

#[test]
fn closing_established_process_fences_retained_duplicate_start() {
    let first_trace = trace_context();
    let second_trace = W3cTraceContext {
        traceparent: Some("00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-01".to_string()),
        tracestate: None,
    };
    let mut context = NoiseTraceContext::default();
    context.observe_request(&process_start_request_with_id(1, first_trace.clone()));
    context.observe_request(&process_start_request_with_id(2, second_trace.clone()));

    assert_eq!(
        context.return_trace(&JSONRPCMessage::Response(JSONRPCResponse {
            id: RequestId::Integer(1),
            result: serde_json::Value::Null,
        })),
        Some(first_trace.clone())
    );
    let closed = JSONRPCMessage::Notification(JSONRPCNotification {
        method: EXEC_CLOSED_METHOD.to_string(),
        params: Some(serde_json::json!({"processId": "process-1"})),
    });
    assert_eq!(context.return_trace(&closed), Some(first_trace));
    assert_eq!(
        context.return_trace(&JSONRPCMessage::Response(JSONRPCResponse {
            id: RequestId::Integer(2),
            result: serde_json::Value::Null,
        })),
        Some(second_trace)
    );
    assert_eq!(context.return_trace(&process_notification()), None);
}

#[test]
fn replacing_closed_process_fences_previous_generation_requests() {
    let first_trace = trace_context();
    let second_trace = W3cTraceContext {
        traceparent: Some("00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-01".to_string()),
        tracestate: None,
    };
    let mut context = NoiseTraceContext::default();
    context.observe_request(&process_start_request_with_id(1, first_trace.clone()));

    let closed = JSONRPCMessage::Notification(JSONRPCNotification {
        method: EXEC_CLOSED_METHOD.to_string(),
        params: Some(serde_json::json!({"processId": "process-1"})),
    });
    assert_eq!(context.return_trace(&closed), Some(first_trace.clone()));

    // A new generation may reuse the process ID, but a failure in that
    // generation must not promote the old request that was already closed.
    context.observe_request(&process_start_request_with_id(2, second_trace.clone()));
    assert_eq!(
        context.return_trace(&JSONRPCMessage::Error(JSONRPCError {
            id: RequestId::Integer(2),
            error: JSONRPCErrorError {
                code: -1,
                message: "rejected".to_string(),
                data: None,
            },
        })),
        Some(second_trace)
    );
    assert_eq!(
        context.return_trace(&JSONRPCMessage::Response(JSONRPCResponse {
            id: RequestId::Integer(1),
            result: serde_json::Value::Null,
        })),
        Some(first_trace)
    );
    assert_eq!(context.return_trace(&process_notification()), None);
}
