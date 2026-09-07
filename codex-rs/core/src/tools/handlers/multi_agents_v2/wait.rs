use super::*;
use crate::agent::agent_resolver::resolve_agent_targets;
use crate::agent::status::is_final;
use crate::session::input_queue::InputQueueActivity;
use crate::session::session::Session;
use crate::session::turn_context::TurnContext;
use crate::tools::context::FunctionToolOutput;
use crate::tools::handlers::multi_agents_spec::WaitAgentTimeoutOptions;
use crate::tools::handlers::multi_agents_spec::create_wait_agent_tool_v2;
use crate::tools::tool_runtime_capabilities::ToolRuntimeCapabilities;
use crate::tools::tool_runtime_capabilities::registered_tool_runtime_capabilities;
use codex_protocol::ThreadId;
use codex_protocol::error::CodexErrorDetails;
use codex_protocol::protocol::CollabAgentRef;
use codex_protocol::protocol::CollabWaitingCompletionReason;
use codex_tools::ToolSpec;
use futures::StreamExt;
use futures::future;
use futures::stream::FuturesUnordered;
use serde_json::json;
use std::collections::HashMap;
use std::collections::HashSet;
use std::time::Duration;
use tokio::sync::watch::Receiver;
use tokio::time::Instant;

#[derive(Default)]
pub(crate) struct Handler {
    options: WaitAgentTimeoutOptions,
}

impl Handler {
    pub(crate) fn new(options: WaitAgentTimeoutOptions) -> Self {
        Self { options }
    }
}

pub(crate) fn resolve_wait_timeout_ms(
    requested_timeout_ms: Option<i64>,
    min_wait_timeout_ms: i64,
    max_wait_timeout_ms: i64,
    default_wait_timeout_ms: i64,
) -> Result<i64, FunctionCallError> {
    let min_timeout_ms = min_wait_timeout_ms.clamp(0, MAX_MULTI_AGENT_V2_WAIT_TIMEOUT_MS);
    let max_timeout_ms =
        max_wait_timeout_ms.clamp(min_timeout_ms, MAX_MULTI_AGENT_V2_WAIT_TIMEOUT_MS);
    let default_timeout_ms = default_wait_timeout_ms.clamp(min_timeout_ms, max_timeout_ms);

    match requested_timeout_ms {
        Some(ms) if ms < min_timeout_ms => Err(FunctionCallError::RespondToModel(format!(
            "timeout_ms must be at least {min_timeout_ms}"
        ))),
        Some(ms) if ms > max_timeout_ms => Err(FunctionCallError::RespondToModel(format!(
            "timeout_ms must be at most {max_timeout_ms}"
        ))),
        Some(ms) => Ok(ms),
        None => Ok(default_timeout_ms),
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum WakeSource {
    TargetCompletion,
    Mailbox,
    Timeout,
    SubscriptionLoss,
}

impl WakeSource {
    fn completion_reason(self) -> CollabWaitingCompletionReason {
        match self {
            WakeSource::TargetCompletion => CollabWaitingCompletionReason::Terminal,
            WakeSource::Mailbox => CollabWaitingCompletionReason::Mailbox,
            WakeSource::Timeout => CollabWaitingCompletionReason::Timeout,
            WakeSource::SubscriptionLoss => CollabWaitingCompletionReason::SubscriptionLoss,
        }
    }
}

#[derive(Debug, Clone, Copy)]
struct CompletionRule {
    return_when: ReturnWhen,
}

impl CompletionRule {
    fn new(return_when: ReturnWhen) -> Self {
        Self { return_when }
    }

    fn is_satisfied(
        self,
        statuses: &HashMap<ThreadId, AgentStatus>,
        receiver_thread_ids: &[ThreadId],
    ) -> bool {
        if receiver_thread_ids.is_empty() {
            return false;
        }

        match self.return_when {
            ReturnWhen::Any => !statuses.is_empty(),
            ReturnWhen::All => receiver_thread_ids
                .iter()
                .all(|id| statuses.get(id).is_some_and(is_final)),
        }
    }
}

impl ToolExecutor<ToolInvocation> for Handler {
    fn tool_name(&self) -> ToolName {
        ToolName::plain("wait_agent")
    }

    fn spec(&self) -> ToolSpec {
        create_wait_agent_tool_v2(self.options)
    }

    fn handle(&self, invocation: ToolInvocation) -> codex_tools::ToolExecutorFuture<'_> {
        Box::pin(self.handle_call(invocation))
    }
}

impl Handler {
    async fn handle_call(
        &self,
        invocation: ToolInvocation,
    ) -> Result<Box<dyn crate::tools::context::ToolOutput>, FunctionCallError> {
        let ToolInvocation {
            session,
            turn,
            payload,
            call_id,
            ..
        } = invocation;
        let arguments = function_arguments(payload)?;
        let args: WaitArgs = parse_arguments(&arguments)?;
        let wait_capability = registered_tool_runtime_capabilities().wait_agent;
        let native_event_capable = wait_capability
            .is_some_and(|capability| capability.native_event_wait && capability.mailbox_wake);
        if args.native_event_wait && !native_event_capable {
            return Err(FunctionCallError::RespondToModel(
                "native_event_wait requires native_event_wait and mailbox_wake capabilities"
                    .to_string(),
            ));
        }
        let receiver_thread_ids = if args.targets.is_empty() {
            Vec::new()
        } else {
            resolve_agent_targets(&session, &turn, args.targets).await?
        };
        let mut seen = HashSet::with_capacity(receiver_thread_ids.len());
        for id in &receiver_thread_ids {
            if !seen.insert(*id) {
                return Err(FunctionCallError::RespondToModel(
                    "targets must resolve to unique agents".to_string(),
                ));
            }
        }
        let mut receiver_agents = Vec::with_capacity(receiver_thread_ids.len());
        for receiver_thread_id in &receiver_thread_ids {
            let agent_metadata = session
                .services
                .agent_control
                .get_agent_metadata(*receiver_thread_id)
                .unwrap_or_default();
            receiver_agents.push(CollabAgentRef {
                thread_id: *receiver_thread_id,
                agent_nickname: agent_metadata.agent_nickname,
                agent_role: agent_metadata.agent_role,
            });
        }

        let timeout_ms = resolve_wait_timeout_ms(
            args.timeout_ms,
            turn.config.multi_agent_v2.min_wait_timeout_ms,
            turn.config.multi_agent_v2.max_wait_timeout_ms,
            turn.config.multi_agent_v2.default_wait_timeout_ms,
        )?;
        let (mut input_activity_rx, pending_input_activity) = session
            .input_queue
            .subscribe_activity(/*turn_state*/ None)
            .await;

        session
            .emit_turn_item_started(
                &turn,
                &TurnItem::CollabAgentToolCall(CollabAgentToolCallItem {
                    id: call_id.clone(),
                    tool: CollabAgentTool::Wait,
                    status: CollabAgentToolCallStatus::InProgress,
                    sender_thread_id: session.thread_id,
                    receiver_thread_ids: receiver_thread_ids.clone(),
                    receiver_agents: receiver_agents.clone(),
                    prompt: None,
                    model: None,
                    reasoning_effort: None,
                    requested_model: None,
                    requested_reasoning_effort: None,
                    agents_states: Default::default(),
                }),
            )
            .await;

        let mut status_rxs = Vec::with_capacity(receiver_thread_ids.len());
        let mut final_statuses = HashMap::new();
        for id in &receiver_thread_ids {
            match session.services.agent_control.subscribe_status(*id).await {
                Ok(rx) => {
                    let status = rx.borrow().clone();
                    if is_final(&status) {
                        final_statuses.insert(*id, status);
                    } else {
                        status_rxs.push((*id, rx));
                    }
                }
                Err(err) if matches!(err.details(), CodexErrorDetails::ThreadNotFound(_)) => {
                    final_statuses.insert(*id, AgentStatus::NotFound);
                }
                Err(err) => {
                    let agents_states =
                        collect_current_wait_statuses(session.as_ref(), &receiver_thread_ids).await;
                    emit_wait_completion(
                        session.as_ref(),
                        turn.as_ref(),
                        call_id.clone(),
                        receiver_thread_ids.clone(),
                        receiver_agents.clone(),
                        agents_states,
                        CollabWaitingCompletionReason::SubscriptionLoss,
                    )
                    .await;
                    return Err(collab_agent_error(*id, err));
                }
            }
        }

        let return_when = wait_capability
            .filter(|capability| capability.return_when)
            .map_or(ReturnWhen::Any, |_| args.return_when);
        // Preserve the existing broad mailbox eligibility rule. Typed mailbox
        // filtering is a follow-on; native mode only changes lease expiry.
        let wake_on_mailbox = wait_capability.is_some_and(|capability| capability.mailbox_wake);
        let native_event_wait = args.native_event_wait && native_event_capable;
        let completion_rule = CompletionRule::new(return_when);
        let wake_source = if let Some(wake_source) = ready_wake_source(
            session.as_ref(),
            completion_rule,
            &mut final_statuses,
            &receiver_thread_ids,
            wake_on_mailbox,
            pending_input_activity,
            &mut status_rxs,
        )
        .await
        {
            wake_source
        } else {
            wait_for_wake_source(
                session.clone(),
                &mut input_activity_rx,
                status_rxs,
                &receiver_thread_ids,
                completion_rule,
                &mut final_statuses,
                wake_on_mailbox,
                &call_id,
                native_event_wait,
                lease_timer_enabled(native_event_wait, timeout_ms),
                Instant::now() + Duration::from_millis(timeout_ms as u64),
                #[cfg(test)] /*lease_observer*/ None,
            )
            .await
        };
        let completion_reason = wake_source.completion_reason();

        let candidate_pending_ids = receiver_thread_ids
            .iter()
            .filter(|receiver_thread_id| !final_statuses.contains_key(receiver_thread_id))
            .copied()
            .collect::<Vec<_>>();
        let mut pending_statuses = Vec::with_capacity(candidate_pending_ids.len());
        for pending_thread_id in &candidate_pending_ids {
            pending_statuses.push((
                *pending_thread_id,
                session
                    .services
                    .agent_control
                    .get_status(*pending_thread_id)
                    .await,
            ));
        }
        let statuses_by_id = merge_wait_end_statuses(final_statuses.clone(), pending_statuses);
        let pending_thread_ids = pending_wait_thread_ids(&receiver_thread_ids, &statuses_by_id);
        let result = WaitAgentResult::new(
            receiver_thread_ids.clone(),
            pending_thread_ids,
            completion_reason,
        );

        emit_wait_completion(
            session.as_ref(),
            turn.as_ref(),
            call_id,
            receiver_thread_ids,
            receiver_agents,
            statuses_by_id,
            completion_reason,
        )
        .await;

        Ok(boxed_tool_output(result))
    }
}

fn lease_timer_enabled(native_event_wait: bool, timeout_ms: i64) -> bool {
    !native_event_wait || timeout_ms > 0
}

impl CoreToolRuntime for Handler {
    fn matches_kind(&self, payload: &ToolPayload) -> bool {
        matches!(payload, ToolPayload::Function { .. })
    }
}

#[derive(Debug, Deserialize)]
struct WaitArgs {
    #[serde(default)]
    #[serde(alias = "ids")]
    targets: Vec<String>,
    timeout_ms: Option<i64>,
    #[serde(default)]
    return_when: ReturnWhen,
    #[serde(default)]
    native_event_wait: bool,
}

#[derive(Debug, Deserialize, Clone, Copy)]
#[serde(rename_all = "lowercase")]
#[derive(Default)]
enum ReturnWhen {
    #[default]
    Any,
    All,
}

#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
pub(crate) struct WaitAgentResult {
    pub(crate) message: String,
    pub(crate) requested_ids: Vec<ThreadId>,
    pub(crate) pending_ids: Vec<ThreadId>,
    pub(crate) completion_reason: CollabWaitingCompletionReason,
    pub(crate) timed_out: bool,
}

async fn ready_wake_source(
    session: &Session,
    completion_rule: CompletionRule,
    final_statuses: &mut HashMap<ThreadId, AgentStatus>,
    receiver_thread_ids: &[ThreadId],
    wake_on_mailbox: bool,
    pending_input_activity: Option<InputQueueActivity>,
    status_rxs: &mut [(ThreadId, Receiver<AgentStatus>)],
) -> Option<WakeSource> {
    if completion_rule.is_satisfied(final_statuses, receiver_thread_ids) {
        Some(WakeSource::TargetCompletion)
    } else if wake_on_mailbox
        && (pending_input_activity.is_some()
            || session
                .input_queue
                .has_pending_input(&session.active_turn)
                .await)
    {
        let latest = collect_current_wait_statuses(session, receiver_thread_ids).await;
        final_statuses.extend(latest.into_iter().filter(|(_, status)| is_final(status)));
        if completion_rule.is_satisfied(final_statuses, receiver_thread_ids) {
            Some(WakeSource::TargetCompletion)
        } else if status_rxs
            .iter()
            .any(|(id, rx)| rx.has_changed().is_err() && !final_statuses.contains_key(id))
        {
            Some(WakeSource::SubscriptionLoss)
        } else {
            Some(WakeSource::Mailbox)
        }
    } else {
        None
    }
}

impl WaitAgentResult {
    fn new(
        requested_ids: Vec<ThreadId>,
        pending_ids: Vec<ThreadId>,
        completion_reason: CollabWaitingCompletionReason,
    ) -> Self {
        let message = match completion_reason {
            CollabWaitingCompletionReason::Terminal => "Wait completed.",
            CollabWaitingCompletionReason::Mailbox => "Wait woke due to mailbox activity.",
            CollabWaitingCompletionReason::Timeout => "Wait timed out.",
            CollabWaitingCompletionReason::SubscriptionLoss => {
                "Wait ended because its event subscription was lost."
            }
        };
        Self {
            message: message.to_string(),
            requested_ids,
            pending_ids,
            completion_reason,
            timed_out: matches!(completion_reason, CollabWaitingCompletionReason::Timeout),
        }
    }

    fn output_value(&self, capabilities: ToolRuntimeCapabilities) -> JsonValue {
        let wait_capability = capabilities.wait_agent;
        let mut output = serde_json::Map::from_iter([
            ("message".to_string(), json!(self.message)),
            ("requested_ids".to_string(), json!(self.requested_ids)),
            ("timed_out".to_string(), json!(self.timed_out)),
        ]);
        if wait_capability.is_some_and(|capability| capability.pending_ids) {
            output.insert("pending_ids".to_string(), json!(self.pending_ids));
        }
        if wait_capability.is_some_and(|capability| capability.completion_reason) {
            output.insert(
                "completion_reason".to_string(),
                json!(self.completion_reason),
            );
        }
        JsonValue::Object(output)
    }

    fn output_json_text(&self, capabilities: ToolRuntimeCapabilities) -> String {
        self.output_value(capabilities).to_string()
    }
}

fn merge_wait_end_statuses<I>(
    mut final_statuses: HashMap<ThreadId, AgentStatus>,
    pending_statuses: I,
) -> HashMap<ThreadId, AgentStatus>
where
    I: IntoIterator<Item = (ThreadId, AgentStatus)>,
{
    for (thread_id, status) in pending_statuses {
        final_statuses.insert(thread_id, status);
    }
    final_statuses
}

fn pending_wait_thread_ids(
    receiver_thread_ids: &[ThreadId],
    statuses_by_id: &HashMap<ThreadId, AgentStatus>,
) -> Vec<ThreadId> {
    receiver_thread_ids
        .iter()
        .filter(|receiver_thread_id| !statuses_by_id.get(receiver_thread_id).is_some_and(is_final))
        .copied()
        .collect()
}

async fn collect_current_wait_statuses(
    session: &Session,
    receiver_thread_ids: &[ThreadId],
) -> HashMap<ThreadId, AgentStatus> {
    let mut statuses = HashMap::with_capacity(receiver_thread_ids.len());
    for receiver_thread_id in receiver_thread_ids {
        statuses.insert(
            *receiver_thread_id,
            session
                .services
                .agent_control
                .get_status(*receiver_thread_id)
                .await,
        );
    }
    statuses
}

async fn emit_wait_completion(
    session: &Session,
    turn: &TurnContext,
    call_id: String,
    receiver_thread_ids: Vec<ThreadId>,
    receiver_agents: Vec<CollabAgentRef>,
    agents_states: HashMap<ThreadId, AgentStatus>,
    completion_reason: CollabWaitingCompletionReason,
) {
    let status = if completion_reason == CollabWaitingCompletionReason::SubscriptionLoss
        // Subscription loss is an unsuccessful lifecycle outcome even when
        // the last authoritative statuses are still running.
        || agents_states.values().any(|agent_status| {
        matches!(
            agent_status,
            AgentStatus::Errored(_) | AgentStatus::NotFound
        )
    }) {
        CollabAgentToolCallStatus::Failed
    } else {
        CollabAgentToolCallStatus::Completed
    };

    session
        .emit_turn_item_completed(
            turn,
            TurnItem::CollabAgentToolCall(CollabAgentToolCallItem {
                id: call_id,
                tool: CollabAgentTool::Wait,
                status,
                sender_thread_id: session.thread_id,
                receiver_thread_ids,
                receiver_agents,
                prompt: None,
                model: None,
                reasoning_effort: None,
                requested_model: None,
                requested_reasoning_effort: None,
                agents_states,
            }),
        )
        .await;
}

impl ToolOutput for WaitAgentResult {
    fn log_preview(&self) -> String {
        self.output_json_text(registered_tool_runtime_capabilities())
    }

    fn success_for_logging(&self) -> bool {
        true
    }

    fn to_response_item(&self, call_id: &str, payload: &ToolPayload) -> ResponseInputItem {
        FunctionToolOutput::from_text(
            self.output_json_text(registered_tool_runtime_capabilities()),
            /*success*/ None,
        )
        .to_response_item(call_id, payload)
    }

    fn code_mode_result(&self, _payload: &ToolPayload) -> JsonValue {
        self.output_value(registered_tool_runtime_capabilities())
    }
}

async fn wait_for_final_status(
    session: std::sync::Arc<Session>,
    thread_id: ThreadId,
    mut status_rx: Receiver<AgentStatus>,
) -> Option<(ThreadId, AgentStatus, bool)> {
    let mut status = status_rx.borrow().clone();
    if is_final(&status) {
        return Some((thread_id, status, false));
    }

    loop {
        if status_rx.changed().await.is_err() {
            let latest = session.services.agent_control.get_status(thread_id).await;
            // A closed subscription is an actionable identity/lifecycle loss
            // when the authoritative status is not already terminal. Surface
            // it as a terminal NotFound state so native mode cannot silently
            // re-arm forever or hot-loop on a dead channel.
            return Some((thread_id, latest, true));
        }
        status = status_rx.borrow().clone();
        if is_final(&status) {
            return Some((thread_id, status, false));
        }
    }
}

#[allow(clippy::too_many_arguments)]
async fn wait_for_wake_source(
    session: std::sync::Arc<Session>,
    input_activity_rx: &mut tokio::sync::watch::Receiver<InputQueueActivity>,
    status_rxs: Vec<(ThreadId, Receiver<AgentStatus>)>,
    receiver_thread_ids: &[ThreadId],
    completion_rule: CompletionRule,
    final_statuses: &mut HashMap<ThreadId, AgentStatus>,
    wake_on_mailbox: bool,
    call_id: &str,
    native_event_wait: bool,
    lease_timer_enabled: bool,
    mut deadline: Instant,
    #[cfg(test)] mut lease_observer: Option<&mut (dyn FnMut() + Send)>,
) -> WakeSource {
    let lease_duration = if lease_timer_enabled {
        deadline
            .saturating_duration_since(Instant::now())
            .max(Duration::from_millis(1))
    } else {
        Duration::ZERO
    };
    let closure_rxs = status_rxs
        .iter()
        .map(|(id, rx)| (*id, rx.clone()))
        .collect::<Vec<_>>();
    let mut futures = FuturesUnordered::new();
    for (id, rx) in status_rxs {
        let session = session.clone();
        futures.push(wait_for_final_status(session, id, rx));
    }

    loop {
        if completion_rule.is_satisfied(final_statuses, receiver_thread_ids) {
            return WakeSource::TargetCompletion;
        }

        let timer = if lease_timer_enabled {
            future::Either::Left(tokio::time::sleep_until(deadline))
        } else {
            future::Either::Right(future::pending())
        };
        tokio::pin!(timer);

        tokio::select! {
            maybe_status = futures.next(), if !futures.is_empty() => {
                match maybe_status {
                    Some(Some((id, status, subscription_lost))) => {
                        let is_terminal = is_final(&status);
                        if is_terminal {
                            final_statuses.insert(id, status);
                        }
                        if subscription_lost {
                            let latest = collect_current_wait_statuses(
                                session.as_ref(),
                                receiver_thread_ids,
                            )
                            .await;
                            final_statuses.extend(
                                latest
                                    .into_iter()
                                    .filter(|(_, status)| is_final(status)),
                            );
                            if !is_terminal {
                                return if completion_rule.is_satisfied(
                                    final_statuses,
                                    receiver_thread_ids,
                                ) {
                                    WakeSource::TargetCompletion
                                } else {
                                    WakeSource::SubscriptionLoss
                                };
                            }
                            // A terminally-closing subscription is only a
                            // completion wake when the requested any/all
                            // rule is satisfied. Otherwise keep the other
                            // status subscriptions armed.
                            if completion_rule.is_satisfied(
                                final_statuses,
                                receiver_thread_ids,
                            ) {
                                return WakeSource::TargetCompletion;
                            }
                        }
                    }
                    Some(None) => {}
                    None => {}
                }
            }
            input_activity_changed = input_activity_rx.changed(), if wake_on_mailbox || native_event_wait => {
                match input_activity_changed {
                    Ok(())
                        if session
                            .input_queue
                            .has_pending_input(&session.active_turn)
                            .await =>
                    {
                        let latest = collect_current_wait_statuses(
                            session.as_ref(),
                            receiver_thread_ids,
                        )
                        .await;
                        final_statuses.extend(
                            latest
                                .into_iter()
                                .filter(|(_, status)| is_final(status)),
                        );
                        let status_loss = closure_rxs.iter().any(|(id, rx)| {
                            rx.has_changed().is_err() && !final_statuses.contains_key(id)
                        });
                        if completion_rule.is_satisfied(final_statuses, receiver_thread_ids) {
                            return WakeSource::TargetCompletion;
                        } else if status_loss {
                            return WakeSource::SubscriptionLoss;
                        }
                        return WakeSource::Mailbox;
                    }
                    Err(_) => {
                        // The mailbox subscription is gone. End the pending
                        // invocation so the owner can re-establish identity;
                        // never re-arm a closed receiver in a hot loop.
                        let latest = collect_current_wait_statuses(
                            session.as_ref(),
                            receiver_thread_ids,
                        )
                        .await;
                        final_statuses.extend(
                            latest
                                .into_iter()
                                .filter(|(_, status)| is_final(status)),
                        );
                        return if completion_rule.is_satisfied(
                            final_statuses,
                            receiver_thread_ids,
                        ) {
                            WakeSource::TargetCompletion
                        } else {
                            WakeSource::SubscriptionLoss
                        };
                    }
                    _ => {}
                }
            }
            _ = &mut timer => {
                if native_event_wait {
                    tracing::trace!(
                        target: "codex.native_wait",
                        "native_wait_lease_renewed call_id={call_id}"
                    );
                    #[cfg(test)]
                    if let Some(observer) = lease_observer.as_deref_mut() {
                        observer();
                    }
                    // The timeout is an internal lease/observation expiry. Keep
                    // this invocation owned by the runtime and re-arm the
                    // same subscriptions without producing a tool result or
                    // starting another model/provider turn.
                    deadline = Instant::now() + lease_duration;
                    continue;
                }
                return WakeSource::Timeout;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn positive_native_lease_reports_each_rearm_before_terminal_wake() {
        let (session, _turn) = crate::session::tests::make_session_and_context().await;
        let target_id = ThreadId::new();
        let (status_tx, status_rx) = tokio::sync::watch::channel(AgentStatus::Running);
        let expiries = std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let observer_expiries = expiries.clone();
        let wait = tokio::spawn(async move {
            let (mut input_activity_rx, _) = session
                .input_queue
                .subscribe_activity(/*turn_state*/ None)
                .await;
            let mut final_statuses = HashMap::new();
            let status_rxs = vec![(target_id, status_rx)];
            let mut observer = || {
                observer_expiries.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
            };
            wait_for_wake_source(
                session.into(),
                &mut input_activity_rx,
                status_rxs,
                &[target_id],
                CompletionRule::new(ReturnWhen::Any),
                &mut final_statuses,
                /*wake_on_mailbox*/ false,
                "test-native-wait-call",
                /*native_event_wait*/ true,
                /*lease_timer_enabled*/ true,
                Instant::now() + Duration::from_millis(5),
                /*lease_observer*/ Some(&mut observer),
            )
            .await
        });

        tokio::time::timeout(Duration::from_secs(1), async {
            while expiries.load(std::sync::atomic::Ordering::SeqCst) < 2 {
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("native lease should rearm at least twice");
        status_tx
            .send(AgentStatus::Shutdown)
            .expect("status receiver should remain active");

        assert_eq!(
            wait.await.expect("wait task should join"),
            WakeSource::TargetCompletion
        );
        assert!(expiries.load(std::sync::atomic::Ordering::SeqCst) >= 2);
    }

    #[test]
    fn wake_source_maps_to_public_completion_reason() {
        assert_eq!(
            WakeSource::TargetCompletion.completion_reason(),
            CollabWaitingCompletionReason::Terminal
        );
        assert_eq!(
            WakeSource::Mailbox.completion_reason(),
            CollabWaitingCompletionReason::Mailbox
        );
        assert_eq!(
            WakeSource::Timeout.completion_reason(),
            CollabWaitingCompletionReason::Timeout
        );
        assert_eq!(
            WakeSource::SubscriptionLoss.completion_reason(),
            CollabWaitingCompletionReason::SubscriptionLoss
        );
    }

    #[test]
    fn subscription_loss_result_is_not_a_timeout() {
        let result = WaitAgentResult::new(
            Vec::new(),
            Vec::new(),
            CollabWaitingCompletionReason::SubscriptionLoss,
        );
        assert!(!result.timed_out);
        assert!(result.message.contains("subscription"));
    }

    #[test]
    fn all_completion_waits_for_remaining_targets_after_terminal_closure() {
        let first = ThreadId::new();
        let second = ThreadId::new();
        let mut statuses = HashMap::from([(first, AgentStatus::Completed(None))]);
        let rule = CompletionRule::new(ReturnWhen::All);
        assert!(!rule.is_satisfied(&statuses, &[first, second]));
        statuses.insert(second, AgentStatus::Completed(None));
        assert!(rule.is_satisfied(&statuses, &[first, second]));
    }

    #[test]
    fn completion_rule_distinguishes_any_from_all() {
        let finished_id = ThreadId::new();
        let running_id = ThreadId::new();
        let receiver_thread_ids = vec![finished_id, running_id];
        let statuses = HashMap::from([(
            finished_id,
            AgentStatus::Completed(Some("done".to_string())),
        )]);

        assert!(CompletionRule::new(ReturnWhen::Any).is_satisfied(&statuses, &receiver_thread_ids));
        assert!(
            !CompletionRule::new(ReturnWhen::All).is_satisfied(&statuses, &receiver_thread_ids)
        );
    }

    #[test]
    fn merge_wait_end_statuses_includes_pending_targets() {
        let completed_id = ThreadId::new();
        let refreshed_completed_id = ThreadId::new();
        let statuses_by_id = merge_wait_end_statuses(
            HashMap::from([(
                completed_id,
                AgentStatus::Completed(Some("done".to_string())),
            )]),
            [(
                refreshed_completed_id,
                AgentStatus::Completed(Some("just finished".to_string())),
            )],
        );

        assert_eq!(
            statuses_by_id.get(&completed_id),
            Some(&AgentStatus::Completed(Some("done".to_string())))
        );
        assert_eq!(
            statuses_by_id.get(&refreshed_completed_id),
            Some(&AgentStatus::Completed(Some("just finished".to_string())))
        );
        assert!(
            pending_wait_thread_ids(&[completed_id, refreshed_completed_id], &statuses_by_id)
                .is_empty()
        );
    }

    #[test]
    fn resolve_wait_timeout_uses_configured_default() {
        assert_eq!(
            resolve_wait_timeout_ms(
                /*requested_timeout_ms*/ None, /*min_wait_timeout_ms*/ 1,
                /*max_wait_timeout_ms*/ 1_000, /*default_wait_timeout_ms*/ 50
            )
            .expect("configured default should be accepted"),
            50
        );
    }

    #[test]
    fn native_zero_timeout_disables_internal_lease_timer() {
        assert!(!lease_timer_enabled(
            /*native_event_wait*/ true, /*timeout_ms*/ 0
        ));
        assert!(lease_timer_enabled(
            /*native_event_wait*/ true, /*timeout_ms*/ 1
        ));
        assert!(lease_timer_enabled(
            /*native_event_wait*/ false, /*timeout_ms*/ 0
        ));
    }

    #[test]
    fn wait_agent_output_omits_capability_owned_fields_without_provider() {
        let requested_id = ThreadId::new();
        let pending_id = ThreadId::new();
        let result = WaitAgentResult::new(
            vec![requested_id],
            vec![pending_id],
            CollabWaitingCompletionReason::Timeout,
        );

        let output = result.output_value(ToolRuntimeCapabilities::upstream_default());

        assert_eq!(output["message"], json!("Wait timed out."));
        assert_eq!(output["requested_ids"], json!([requested_id]));
        assert_eq!(output["timed_out"], json!(true));
        assert!(
            !output
                .as_object()
                .expect("output should be object")
                .contains_key("pending_ids")
        );
        assert!(
            !output
                .as_object()
                .expect("output should be object")
                .contains_key("completion_reason")
        );
    }

    #[test]
    fn status_classification_keeps_only_non_final_targets_pending() {
        let finished_id = ThreadId::new();
        let running_id = ThreadId::new();
        let errored_id = ThreadId::new();
        let receiver_thread_ids = vec![finished_id, running_id, errored_id];
        let statuses = HashMap::from([
            (
                finished_id,
                AgentStatus::Completed(Some("done".to_string())),
            ),
            (running_id, AgentStatus::Running),
            (
                errored_id,
                AgentStatus::Errored("permission denied".to_string()),
            ),
        ]);
        let pending_thread_ids = pending_wait_thread_ids(&receiver_thread_ids, &statuses);

        assert_eq!(pending_thread_ids, vec![running_id]);
        assert_eq!(statuses.get(&running_id), Some(&AgentStatus::Running));
        assert_eq!(
            statuses.get(&errored_id),
            Some(&AgentStatus::Errored("permission denied".to_string()))
        );
    }
}
