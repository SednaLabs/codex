use std::num::NonZero;
use std::path::PathBuf;
use std::sync::Arc;
use std::sync::Mutex;
use std::sync::atomic::AtomicBool;
use std::sync::atomic::Ordering;

use codex_app_server_protocol::FuzzyFileSearchMatchType;
use codex_app_server_protocol::FuzzyFileSearchResult;
use codex_app_server_protocol::FuzzyFileSearchSessionCompletedNotification;
use codex_app_server_protocol::FuzzyFileSearchSessionUpdatedNotification;
use codex_app_server_protocol::ServerNotification;
use codex_file_search as file_search;
use tokio::sync::Notify;
use tokio::task::JoinHandle;
use tracing::warn;

use crate::outgoing_message::OutgoingMessageSender;

const MATCH_LIMIT: usize = 50;
const MAX_THREADS: usize = 12;

pub(crate) async fn run_fuzzy_file_search(
    query: String,
    roots: Vec<String>,
    cancellation_flag: Arc<AtomicBool>,
) -> Vec<FuzzyFileSearchResult> {
    if roots.is_empty() {
        return Vec::new();
    }

    #[expect(clippy::expect_used)]
    let limit = NonZero::new(MATCH_LIMIT).expect("MATCH_LIMIT should be a valid non-zero usize");

    let cores = std::thread::available_parallelism()
        .map(std::num::NonZero::get)
        .unwrap_or(1);
    let threads = cores.min(MAX_THREADS);
    #[expect(clippy::expect_used)]
    let threads = NonZero::new(threads.max(1)).expect("threads should be non-zero");
    let search_dirs: Vec<PathBuf> = roots.iter().map(PathBuf::from).collect();

    let mut files = match tokio::task::spawn_blocking(move || {
        file_search::run(
            query.as_str(),
            search_dirs,
            file_search::FileSearchOptions {
                limit,
                threads,
                compute_indices: true,
                ..Default::default()
            },
            Some(cancellation_flag),
        )
    })
    .await
    {
        Ok(Ok(res)) => res
            .matches
            .into_iter()
            .map(|m| {
                let file_name = m.path.file_name().unwrap_or_default();
                FuzzyFileSearchResult {
                    root: m.root.to_string_lossy().to_string(),
                    path: m.path.to_string_lossy().to_string(),
                    match_type: match m.match_type {
                        file_search::MatchType::File => FuzzyFileSearchMatchType::File,
                        file_search::MatchType::Directory => FuzzyFileSearchMatchType::Directory,
                    },
                    file_name: file_name.to_string_lossy().to_string(),
                    score: m.score,
                    indices: m.indices,
                }
            })
            .collect::<Vec<_>>(),
        Ok(Err(err)) => {
            warn!("fuzzy-file-search failed: {err}");
            Vec::new()
        }
        Err(err) => {
            warn!("fuzzy-file-search join failed: {err}");
            Vec::new()
        }
    };

    files.sort_by(file_search::cmp_by_score_desc_then_path_asc::<
        FuzzyFileSearchResult,
        _,
        _,
    >(|f| f.score, |f| f.path.as_str()));

    files
}

pub(crate) struct FuzzyFileSearchSession {
    session: file_search::FileSearchSession,
    shared: Arc<SessionShared>,
    notification_forwarder: JoinHandle<()>,
}

impl FuzzyFileSearchSession {
    pub(crate) fn update_query(&self, query: String) {
        if self.shared.canceled.load(Ordering::Relaxed) {
            return;
        }
        {
            #[expect(clippy::unwrap_used)]
            let mut latest_query = self.shared.latest_query.lock().unwrap();
            *latest_query = query.clone();
        }
        self.session.update_query(&query);
    }
}

impl Drop for FuzzyFileSearchSession {
    fn drop(&mut self) {
        self.shared.cancel();
        self.notification_forwarder.abort();
    }
}

pub(crate) fn start_fuzzy_file_search_session(
    session_id: String,
    roots: Vec<String>,
    outgoing: Arc<OutgoingMessageSender>,
) -> anyhow::Result<FuzzyFileSearchSession> {
    #[expect(clippy::expect_used)]
    let limit = NonZero::new(MATCH_LIMIT).expect("MATCH_LIMIT should be a valid non-zero usize");
    let cores = std::thread::available_parallelism()
        .map(std::num::NonZero::get)
        .unwrap_or(1);
    let threads = cores.min(MAX_THREADS);
    #[expect(clippy::expect_used)]
    let threads = NonZero::new(threads.max(1)).expect("threads should be non-zero");
    let search_dirs: Vec<PathBuf> = roots.iter().map(PathBuf::from).collect();
    let canceled = Arc::new(AtomicBool::new(false));

    let runtime = tokio::runtime::Handle::current();
    let shared = Arc::new(SessionShared {
        session_id,
        latest_query: Mutex::new(String::new()),
        outgoing,
        canceled: canceled.clone(),
        pending_notifications: Mutex::new(PendingNotifications::new()),
        notification_ready: Notify::new(),
    });

    let reporter = Arc::new(SessionReporterImpl {
        shared: shared.clone(),
    });
    let session = file_search::create_session(
        search_dirs,
        file_search::FileSearchOptions {
            limit,
            threads,
            compute_indices: true,
            ..Default::default()
        },
        reporter,
        Some(canceled),
    )?;

    let notification_forwarder = runtime.spawn(forward_notifications(shared.clone()));

    Ok(FuzzyFileSearchSession {
        session,
        shared,
        notification_forwarder,
    })
}

struct SessionShared {
    session_id: String,
    latest_query: Mutex<String>,
    outgoing: Arc<OutgoingMessageSender>,
    canceled: Arc<AtomicBool>,
    pending_notifications: Mutex<PendingNotifications<FuzzyFileSearchSessionUpdatedNotification>>,
    notification_ready: Notify,
}

impl SessionShared {
    fn enqueue_update(&self, notification: FuzzyFileSearchSessionUpdatedNotification) {
        let queued = {
            #[expect(clippy::unwrap_used)]
            self.pending_notifications
                .lock()
                .unwrap()
                .replace_update(notification)
        };
        if queued {
            self.notification_ready.notify_one();
        }
    }

    fn enqueue_completion(&self) {
        let queued = {
            #[expect(clippy::unwrap_used)]
            self.pending_notifications.lock().unwrap().push_completion()
        };
        if queued {
            self.notification_ready.notify_one();
        }
    }

    fn cancel(&self) {
        self.canceled.store(true, Ordering::Relaxed);
        #[expect(clippy::unwrap_used)]
        self.pending_notifications.lock().unwrap().stop();
        self.notification_ready.notify_one();
    }
}

#[derive(Debug, Eq, PartialEq)]
enum PendingNotification<T> {
    Update(T),
    Complete,
}

/// Constant-payload custody for one serialized notification forwarder.
///
/// Completion counters on either side of the latest update preserve callback
/// order when a newer update replaces an older one.
struct PendingNotifications<T> {
    latest_update: Option<T>,
    completions_before_update: usize,
    completions_after_update: usize,
    stopped: bool,
}

impl<T> PendingNotifications<T> {
    fn new() -> Self {
        Self {
            latest_update: None,
            completions_before_update: 0,
            completions_after_update: 0,
            stopped: false,
        }
    }

    fn replace_update(&mut self, update: T) -> bool {
        if self.stopped {
            return false;
        }

        if self.latest_update.is_some() {
            add_completion_count(
                &mut self.completions_before_update,
                self.completions_after_update,
            );
            self.completions_after_update = 0;
        }
        self.latest_update = Some(update);
        true
    }

    fn push_completion(&mut self) -> bool {
        if self.stopped {
            return false;
        }

        if self.latest_update.is_some() {
            add_completion_count(&mut self.completions_after_update, /*additional*/ 1);
        } else {
            add_completion_count(&mut self.completions_before_update, /*additional*/ 1);
        }
        true
    }

    fn take_next(&mut self) -> Option<PendingNotification<T>> {
        if self.completions_before_update > 0 {
            self.completions_before_update -= 1;
            return Some(PendingNotification::Complete);
        }

        if let Some(update) = self.latest_update.take() {
            self.completions_before_update = self.completions_after_update;
            self.completions_after_update = 0;
            return Some(PendingNotification::Update(update));
        }

        None
    }

    fn stop(&mut self) {
        self.stopped = true;
        self.latest_update = None;
        self.completions_before_update = 0;
        self.completions_after_update = 0;
    }
}

fn add_completion_count(counter: &mut usize, additional: usize) {
    #[expect(clippy::expect_used)]
    let updated = counter
        .checked_add(additional)
        .expect("fuzzy file search completion backlog should fit in usize");
    *counter = updated;
}

async fn forward_notifications(shared: Arc<SessionShared>) {
    loop {
        let notified = shared.notification_ready.notified();
        let (next, stopped) = {
            #[expect(clippy::unwrap_used)]
            let mut pending = shared.pending_notifications.lock().unwrap();
            (pending.take_next(), pending.stopped)
        };

        let Some(next) = next else {
            if stopped {
                return;
            }
            notified.await;
            continue;
        };

        let notification = match next {
            PendingNotification::Update(notification) => {
                ServerNotification::FuzzyFileSearchSessionUpdated(notification)
            }
            PendingNotification::Complete => ServerNotification::FuzzyFileSearchSessionCompleted(
                FuzzyFileSearchSessionCompletedNotification {
                    session_id: shared.session_id.clone(),
                },
            ),
        };
        shared.outgoing.send_server_notification(notification).await;
    }
}

struct SessionReporterImpl {
    shared: Arc<SessionShared>,
}

impl SessionReporterImpl {
    fn send_snapshot(&self, snapshot: &file_search::FileSearchSnapshot) {
        if self.shared.canceled.load(Ordering::Relaxed) {
            return;
        }

        let query = {
            #[expect(clippy::unwrap_used)]
            self.shared.latest_query.lock().unwrap().clone()
        };
        if snapshot.query != query {
            return;
        }

        let files = if query.is_empty() {
            Vec::new()
        } else {
            collect_files(snapshot)
        };

        self.shared
            .enqueue_update(FuzzyFileSearchSessionUpdatedNotification {
                session_id: self.shared.session_id.clone(),
                query,
                files,
            });
    }

    fn send_complete(&self) {
        if self.shared.canceled.load(Ordering::Relaxed) {
            return;
        }
        self.shared.enqueue_completion();
    }
}

impl file_search::SessionReporter for SessionReporterImpl {
    fn on_update(&self, snapshot: &file_search::FileSearchSnapshot) {
        self.send_snapshot(snapshot);
    }

    fn on_complete(&self) {
        self.send_complete();
    }
}

fn collect_files(snapshot: &file_search::FileSearchSnapshot) -> Vec<FuzzyFileSearchResult> {
    let mut files = snapshot
        .matches
        .iter()
        .map(|m| {
            let file_name = m.path.file_name().unwrap_or_default();
            FuzzyFileSearchResult {
                root: m.root.to_string_lossy().to_string(),
                path: m.path.to_string_lossy().to_string(),
                match_type: match m.match_type {
                    file_search::MatchType::File => FuzzyFileSearchMatchType::File,
                    file_search::MatchType::Directory => FuzzyFileSearchMatchType::Directory,
                },
                file_name: file_name.to_string_lossy().to_string(),
                score: m.score,
                indices: m.indices.clone(),
            }
        })
        .collect::<Vec<_>>();

    files.sort_by(file_search::cmp_by_score_desc_then_path_asc::<
        FuzzyFileSearchResult,
        _,
        _,
    >(|f| f.score, |f| f.path.as_str()));
    files
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;
    use std::sync::atomic::AtomicUsize;
    use std::sync::atomic::Ordering;

    use codex_analytics::AnalyticsEventsClient;
    use codex_app_server_protocol::ServerNotificationEnvelope;
    use pretty_assertions::assert_eq;
    use tokio::sync::mpsc;

    use super::FuzzyFileSearchSessionUpdatedNotification;
    use super::PendingNotification;
    use super::PendingNotifications;
    use super::SessionShared;
    use super::forward_notifications;
    use crate::outgoing_message::OutgoingEnvelope;
    use crate::outgoing_message::OutgoingMessage;
    use crate::outgoing_message::OutgoingMessageSender;

    #[test]
    fn pending_notifications_replace_latest_and_preserve_completion_order() {
        let mut pending = PendingNotifications::new();

        assert!(pending.replace_update("first"));
        assert!(pending.push_completion());
        assert!(pending.push_completion());
        assert!(pending.replace_update("latest"));
        assert!(pending.push_completion());

        let drained = [
            pending.take_next(),
            pending.take_next(),
            pending.take_next(),
            pending.take_next(),
            pending.take_next(),
        ];
        assert_eq!(
            drained,
            [
                Some(PendingNotification::Complete),
                Some(PendingNotification::Complete),
                Some(PendingNotification::Update("latest")),
                Some(PendingNotification::Complete),
                None,
            ]
        );
    }

    #[test]
    fn stopping_pending_notifications_releases_payload_and_rejects_callbacks() {
        struct DropMarker(Arc<AtomicUsize>);

        impl Drop for DropMarker {
            fn drop(&mut self) {
                self.0.fetch_add(1, Ordering::Relaxed);
            }
        }

        let drops = Arc::new(AtomicUsize::new(0));
        let mut pending = PendingNotifications::new();
        assert!(pending.replace_update(DropMarker(drops.clone())));

        pending.stop();
        assert_eq!(drops.load(Ordering::Relaxed), 1);
        assert!(!pending.replace_update(DropMarker(drops.clone())));
        assert!(!pending.push_completion());
        assert_eq!(drops.load(Ordering::Relaxed), 2);
        assert!(pending.take_next().is_none());
    }

    #[test]
    fn finite_completion_churn_preserves_fifo_to_latest_update() {
        let mut pending = PendingNotifications::new();
        for _ in 0..9 {
            assert!(pending.push_completion());
        }
        assert!(pending.replace_update("latest"));
        assert!(pending.push_completion());

        for _ in 0..9 {
            assert_eq!(pending.take_next(), Some(PendingNotification::Complete));
        }
        assert_eq!(
            pending.take_next(),
            Some(PendingNotification::Update("latest"))
        );
        assert_eq!(pending.take_next(), Some(PendingNotification::Complete));
    }

    #[tokio::test]
    async fn blocked_outgoing_sender_still_delivers_latest_update() {
        let (event_tx, mut event_rx) = mpsc::channel(1);
        let (response_tx, _response_rx) = mpsc::channel(1);
        let outgoing = Arc::new(OutgoingMessageSender::new_with_senders(
            event_tx,
            response_tx,
            AnalyticsEventsClient::disabled(),
        ));
        let shared = Arc::new(SessionShared {
            session_id: "session".to_string(),
            latest_query: std::sync::Mutex::new(String::new()),
            outgoing,
            canceled: Arc::new(std::sync::atomic::AtomicBool::new(false)),
            pending_notifications: std::sync::Mutex::new(PendingNotifications::new()),
            notification_ready: tokio::sync::Notify::new(),
        });

        shared.enqueue_update(FuzzyFileSearchSessionUpdatedNotification {
            session_id: "session".to_string(),
            query: "first".to_string(),
            files: Vec::new(),
        });
        let forwarder = tokio::spawn(forward_notifications(shared.clone()));
        let _ = event_rx.recv().await.expect("first update should be sent");

        for _ in 0..9 {
            shared.enqueue_completion();
        }
        shared.enqueue_update(FuzzyFileSearchSessionUpdatedNotification {
            session_id: "session".to_string(),
            query: "latest".to_string(),
            files: Vec::new(),
        });

        for _ in 0..9 {
            let _ = event_rx
                .recv()
                .await
                .expect("completion should be retained");
        }
        let queued = event_rx
            .recv()
            .await
            .expect("latest update should progress");
        let OutgoingEnvelope::Broadcast { message } = queued else {
            panic!("expected broadcast event");
        };
        let OutgoingMessage::AppServerNotification(ServerNotificationEnvelope {
            notification, ..
        }) = message
        else {
            panic!("expected server notification");
        };
        assert!(matches!(
            notification,
            codex_app_server_protocol::ServerNotification::FuzzyFileSearchSessionUpdated(
                notification
            ) if notification.query == "latest"
        ));

        shared.cancel();
        forwarder.abort();
    }

    #[tokio::test]
    async fn cancellation_releases_forwarder_blocked_on_outgoing_send() {
        let (event_tx, mut event_rx) = mpsc::channel(1);
        let (response_tx, _response_rx) = mpsc::channel(1);
        let shared = Arc::new(SessionShared {
            session_id: "session".to_string(),
            latest_query: std::sync::Mutex::new(String::new()),
            outgoing: Arc::new(OutgoingMessageSender::new_with_senders(
                event_tx,
                response_tx,
                AnalyticsEventsClient::disabled(),
            )),
            canceled: Arc::new(std::sync::atomic::AtomicBool::new(false)),
            pending_notifications: std::sync::Mutex::new(PendingNotifications::new()),
            notification_ready: tokio::sync::Notify::new(),
        });

        shared.enqueue_completion();
        shared.enqueue_completion();
        shared.enqueue_completion();
        let forwarder = tokio::spawn(forward_notifications(shared.clone()));
        let _ = event_rx
            .recv()
            .await
            .expect("first completion should be sent");
        tokio::task::yield_now().await;

        shared.cancel();
        forwarder.abort();
        assert!(forwarder.await.is_err());
        assert!(
            shared
                .pending_notifications
                .lock()
                .unwrap()
                .take_next()
                .is_none()
        );
    }
}
