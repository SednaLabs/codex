//! Goal-aware projection of child turn notifications.
//!
//! This is deliberately separate from [`AgentStatus`].  A completed turn is
//! not necessarily a completed goal, and an active goal can require an
//! operator action without the agent becoming terminal.  The projection keeps
//! the binding which made a notification meaningful so a late snapshot cannot
//! wake a replacement goal.

use codex_protocol::ThreadId;
use codex_protocol::protocol::ThreadGoalStatus;
use std::sync::Mutex;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct GoalNotificationBinding {
    pub parent_thread_id: ThreadId,
    pub child_thread_id: ThreadId,
    pub goal_id: String,
    pub control_generation: u64,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum GoalNotificationPhase {
    Running,
    ContinuationPending,
    DeferredWithOwner,
    ActionRequired,
    ResultReady,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum GoalNotificationClassification {
    Progress,
    Result,
    ActionRequired,
    Legacy,
    Stale,
}

impl GoalNotificationClassification {
    pub fn is_wake_eligible(self) -> bool {
        matches!(self, Self::Result | Self::ActionRequired | Self::Legacy)
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct GoalNotificationSnapshot {
    pub binding: GoalNotificationBinding,
    pub status: ThreadGoalStatus,
    pub phase: GoalNotificationPhase,
    pub revision: u64,
    pub last_completed_source_turn: Option<String>,
    pub result_reference: Option<String>,
    pub classification: GoalNotificationClassification,
}

impl GoalNotificationSnapshot {
    pub fn is_wake_eligible(&self) -> bool {
        self.classification.is_wake_eligible()
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum GoalNotificationInput {
    TurnProgress,
    TurnComplete {
        source_turn: String,
        result: Option<String>,
    },
    ActionRequired,
    ExplicitControl,
    ContinuationPending,
    DeferredWithOwner,
}

/// The stateful producer-facing projection.  Callers must supply the exact
/// binding on every update; mismatches are retained as stale observations and
/// are never eligible to wake a waiter.
#[derive(Clone, Debug)]
pub struct GoalNotificationProjection {
    snapshot: GoalNotificationSnapshot,
    opted_in: bool,
    completion_forwarded: bool,
}

/// Thread-scoped holder shared by goal lifecycle hooks and the V2 producer.
pub struct GoalNotificationStore {
    projection: Mutex<Option<GoalNotificationProjection>>,
    incarnation: ThreadId,
    source_turn_id: Mutex<Option<String>>,
    forwarded_completion: Mutex<Option<GoalNotificationTurnToken>>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct GoalNotificationTurnToken {
    pub incarnation: ThreadId,
    pub binding: GoalNotificationBinding,
    pub source_turn_id: String,
    pub generation: u64,
}

impl Default for GoalNotificationStore {
    fn default() -> Self {
        Self {
            projection: Mutex::new(None),
            incarnation: ThreadId::new(),
            source_turn_id: Mutex::new(None),
            forwarded_completion: Mutex::new(None),
        }
    }
}

impl GoalNotificationStore {
    pub fn incarnation(&self) -> ThreadId {
        self.incarnation
    }
    /// Installs a projection after the goal runtime has validated its binding.
    pub fn install_authoritative(&self, projection: GoalNotificationProjection) {
        *self
            .projection
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner) = Some(projection);
        *self
            .forwarded_completion
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner) = None;
    }

    pub fn set_source_turn(&self, source_turn_id: impl Into<String>) {
        *self
            .source_turn_id
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner) = Some(source_turn_id.into());
    }

    pub fn publish(&self, binding: &GoalNotificationBinding, status: ThreadGoalStatus) -> bool {
        let mut projection = self
            .projection
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let Some(projection) = projection.as_mut() else {
            return false;
        };
        projection.observe_goal(binding, status)
    }

    pub fn publish_continuation(&self, binding: &GoalNotificationBinding) -> bool {
        let mut projection = self
            .projection
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let Some(projection) = projection.as_mut() else {
            return false;
        };
        projection.observe(binding, GoalNotificationInput::ContinuationPending)
    }

    /// Publishes the authoritative completion after the turn lifecycle has
    /// reduced the terminal `TurnComplete` event. The token gate keeps a late
    /// event from attaching a result to a replacement goal.
    pub fn publish_turn_complete(
        &self,
        token: &GoalNotificationTurnToken,
        result: Option<String>,
    ) -> bool {
        if !self.token_matches(token) {
            return false;
        }
        let mut projection = self
            .projection
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let Some(projection) = projection.as_mut() else {
            return false;
        };
        projection.observe(
            &token.binding,
            GoalNotificationInput::TurnComplete {
                source_turn: token.source_turn_id.clone(),
                result,
            },
        )
    }

    /// Claims a suppressed completion for a terminal external goal mutation.
    /// The claim is idempotent until the store is invalidated, so repeated API
    /// or tool updates cannot send duplicate parent handbacks.
    pub fn take_pending_completion(&self) -> Option<(GoalNotificationTurnToken, Option<String>)> {
        let mut projection = self
            .projection
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let projection = projection.as_mut()?;
        if projection.snapshot.phase != GoalNotificationPhase::ContinuationPending
            || projection.completion_forwarded
        {
            return None;
        }
        let source_turn = projection.snapshot.last_completed_source_turn.clone()?;
        projection.completion_forwarded = true;
        let token = GoalNotificationTurnToken {
            incarnation: self.incarnation,
            binding: projection.snapshot.binding.clone(),
            source_turn_id: source_turn,
            generation: projection.snapshot.binding.control_generation,
        };
        Some((token, projection.snapshot.result_reference.clone()))
    }

    /// Records a successful queue delivery while retaining a one-shot tombstone
    /// so a concurrently finishing Session cannot forward the same completion.
    pub fn mark_completion_forwarded(&self, token: &GoalNotificationTurnToken) -> bool {
        if !self.token_matches(token) {
            return false;
        }
        let mut projection = self
            .projection
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let Some(projection) = projection.as_mut() else {
            return false;
        };
        if projection.snapshot.binding != token.binding
            || projection.snapshot.phase != GoalNotificationPhase::ContinuationPending
            || !projection.completion_forwarded
        {
            return false;
        }
        *self
            .forwarded_completion
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner) = Some(token.clone());
        true
    }

    pub fn has_forwarded_completion(&self) -> bool {
        self.forwarded_completion
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .is_some()
    }

    /// Releases a failed parent-delivery claim so a later external mutation
    /// can retry the same pending completion.
    pub fn restore_pending_completion(&self, binding: &GoalNotificationBinding) -> bool {
        let mut projection = self
            .projection
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let Some(projection) = projection.as_mut() else {
            return false;
        };
        if projection.snapshot.binding != *binding
            || projection.snapshot.phase != GoalNotificationPhase::ContinuationPending
        {
            return false;
        }
        projection.completion_forwarded = false;
        true
    }

    pub fn clear(&self) {
        *self
            .projection
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner) = None;
        *self
            .forwarded_completion
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner) = None;
    }

    pub fn clear_preserving_forwarded_completion(&self) {
        *self
            .projection
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner) = None;
    }

    pub fn snapshot(&self) -> Option<GoalNotificationSnapshot> {
        self.projection
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .as_ref()
            .map(|projection| projection.snapshot().clone())
    }

    pub fn terminal_turn_is_wake_eligible(&self) -> Option<bool> {
        self.projection
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .as_ref()
            .and_then(|projection| match projection.snapshot().phase {
                GoalNotificationPhase::ContinuationPending => Some(false),
                GoalNotificationPhase::ActionRequired | GoalNotificationPhase::ResultReady => {
                    Some(true)
                }
                GoalNotificationPhase::DeferredWithOwner => Some(false),
                GoalNotificationPhase::Running => None,
            })
    }

    pub fn terminal_turn_is_wake_eligible_for(
        &self,
        binding: &GoalNotificationBinding,
    ) -> Option<bool> {
        let projection = self
            .projection
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let projection = projection.as_ref()?;
        if projection.snapshot().binding != *binding {
            return None;
        }
        match projection.snapshot().phase {
            GoalNotificationPhase::ContinuationPending => Some(false),
            GoalNotificationPhase::ActionRequired | GoalNotificationPhase::ResultReady => {
                Some(true)
            }
            GoalNotificationPhase::DeferredWithOwner => Some(false),
            GoalNotificationPhase::Running => None,
        }
    }

    pub fn terminal_turn_is_wake_eligible_for_token(
        &self,
        token: &GoalNotificationTurnToken,
    ) -> Option<bool> {
        if self
            .forwarded_completion
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .as_ref()
            == Some(token)
        {
            return Some(false);
        }
        if !self.token_matches(token) {
            return None;
        }
        self.terminal_turn_is_wake_eligible_for(&token.binding)
    }

    fn token_matches(&self, token: &GoalNotificationTurnToken) -> bool {
        token.incarnation == self.incarnation
            && token.generation == token.binding.control_generation
            && self
                .source_turn_id
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .as_deref()
                == Some(token.source_turn_id.as_str())
    }
}

impl GoalNotificationProjection {
    pub fn new(binding: GoalNotificationBinding, status: ThreadGoalStatus) -> Self {
        Self {
            snapshot: GoalNotificationSnapshot {
                binding,
                status,
                phase: GoalNotificationPhase::Running,
                revision: 0,
                last_completed_source_turn: None,
                result_reference: None,
                classification: GoalNotificationClassification::Progress,
            },
            opted_in: false,
            completion_forwarded: false,
        }
    }

    pub fn legacy(binding: GoalNotificationBinding) -> Self {
        let mut projection = Self::new(binding, ThreadGoalStatus::Active);
        projection.opted_in = false;
        projection.snapshot.classification = GoalNotificationClassification::Legacy;
        projection
    }

    pub fn opt_in(&mut self, binding: &GoalNotificationBinding) -> bool {
        if self.snapshot.binding != *binding {
            return false;
        }
        self.opted_in = true;
        true
    }

    pub fn snapshot(&self) -> &GoalNotificationSnapshot {
        &self.snapshot
    }

    pub fn is_wake_eligible(&self) -> bool {
        self.snapshot.is_wake_eligible()
    }

    pub fn observe_goal(
        &mut self,
        binding: &GoalNotificationBinding,
        status: ThreadGoalStatus,
    ) -> bool {
        if self.snapshot.binding != *binding {
            return false;
        }
        self.snapshot.status = status;
        self.snapshot.revision = self.snapshot.revision.saturating_add(1);
        if status != ThreadGoalStatus::Complete {
            self.snapshot.phase = GoalNotificationPhase::Running;
            self.snapshot.classification = GoalNotificationClassification::Progress;
        } else if !self.opted_in {
            self.snapshot.phase = GoalNotificationPhase::ActionRequired;
            self.snapshot.classification = GoalNotificationClassification::Legacy;
        } else if self.snapshot.result_reference.is_some() {
            self.snapshot.phase = GoalNotificationPhase::ResultReady;
            self.snapshot.classification = GoalNotificationClassification::Result;
        } else {
            self.snapshot.phase = GoalNotificationPhase::ActionRequired;
            self.snapshot.classification = GoalNotificationClassification::ActionRequired;
        }
        true
    }

    pub fn observe(
        &mut self,
        binding: &GoalNotificationBinding,
        input: GoalNotificationInput,
    ) -> bool {
        if self.snapshot.binding != *binding {
            return false;
        }
        self.snapshot.revision = self.snapshot.revision.saturating_add(1);
        match input {
            GoalNotificationInput::TurnProgress => {
                self.snapshot.phase = GoalNotificationPhase::Running;
                self.snapshot.classification = GoalNotificationClassification::Progress;
            }
            GoalNotificationInput::ContinuationPending => {
                if self.opted_in {
                    self.snapshot.phase = GoalNotificationPhase::ContinuationPending;
                    self.snapshot.classification = GoalNotificationClassification::Progress;
                } else {
                    self.snapshot.classification = GoalNotificationClassification::Legacy;
                }
            }
            GoalNotificationInput::DeferredWithOwner => {
                if self.opted_in {
                    self.snapshot.phase = GoalNotificationPhase::DeferredWithOwner;
                    self.snapshot.classification = GoalNotificationClassification::Progress;
                } else {
                    self.snapshot.classification = GoalNotificationClassification::Legacy;
                }
            }
            GoalNotificationInput::ActionRequired | GoalNotificationInput::ExplicitControl => {
                self.snapshot.phase = GoalNotificationPhase::ActionRequired;
                self.snapshot.classification = if self.opted_in {
                    GoalNotificationClassification::ActionRequired
                } else {
                    GoalNotificationClassification::Legacy
                };
            }
            GoalNotificationInput::TurnComplete {
                source_turn,
                result,
            } => {
                self.completion_forwarded = false;
                self.snapshot.last_completed_source_turn = Some(source_turn);
                self.snapshot.result_reference = result;
                if !self.opted_in {
                    self.snapshot.phase = GoalNotificationPhase::ActionRequired;
                    self.snapshot.classification = GoalNotificationClassification::Legacy;
                } else if self.snapshot.status == ThreadGoalStatus::Complete
                    && self.snapshot.result_reference.is_some()
                {
                    self.snapshot.phase = GoalNotificationPhase::ResultReady;
                    self.snapshot.classification = GoalNotificationClassification::Result;
                } else if self.snapshot.status == ThreadGoalStatus::Complete {
                    self.snapshot.phase = GoalNotificationPhase::ActionRequired;
                    self.snapshot.classification = GoalNotificationClassification::ActionRequired;
                } else if self.snapshot.status == ThreadGoalStatus::Active {
                    self.snapshot.phase = GoalNotificationPhase::ContinuationPending;
                    self.snapshot.classification = GoalNotificationClassification::Progress;
                } else {
                    self.snapshot.phase = GoalNotificationPhase::ActionRequired;
                    self.snapshot.classification = GoalNotificationClassification::ActionRequired;
                }
            }
        }
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn binding() -> GoalNotificationBinding {
        GoalNotificationBinding {
            parent_thread_id: ThreadId::new(),
            child_thread_id: ThreadId::new(),
            goal_id: "goal-1".into(),
            control_generation: 7,
        }
    }

    #[test]
    fn active_turn_completion_is_suppressed_until_goal_completes() {
        let binding = binding();
        let mut projection =
            GoalNotificationProjection::new(binding.clone(), ThreadGoalStatus::Active);
        projection.opt_in(&binding);
        projection.observe(
            &binding,
            GoalNotificationInput::TurnComplete {
                source_turn: "t1".into(),
                result: Some("r".into()),
            },
        );
        assert_eq!(
            projection.snapshot().phase,
            GoalNotificationPhase::ContinuationPending
        );
        assert!(!projection.snapshot().is_wake_eligible());
    }

    #[test]
    fn non_opted_in_continuation_cannot_suppress_legacy_wake() {
        let binding = binding();
        let mut projection =
            GoalNotificationProjection::new(binding.clone(), ThreadGoalStatus::Active);
        projection.observe(&binding, GoalNotificationInput::ContinuationPending);
        assert_eq!(
            projection.snapshot().classification,
            GoalNotificationClassification::Legacy
        );
        assert!(projection.snapshot().is_wake_eligible());
    }

    #[test]
    fn legacy_default_turn_completion_remains_wake_eligible() {
        let binding = binding();
        let mut projection =
            GoalNotificationProjection::new(binding.clone(), ThreadGoalStatus::Active);
        projection.observe(
            &binding,
            GoalNotificationInput::TurnComplete {
                source_turn: "t1".into(),
                result: Some("r".into()),
            },
        );
        assert_eq!(
            projection.snapshot().classification,
            GoalNotificationClassification::Legacy
        );
        assert!(projection.snapshot().is_wake_eligible());
    }

    #[test]
    fn complete_goal_without_result_requires_action() {
        let binding = binding();
        let mut projection =
            GoalNotificationProjection::new(binding.clone(), ThreadGoalStatus::Complete);
        projection.opt_in(&binding);
        projection.observe(
            &binding,
            GoalNotificationInput::TurnComplete {
                source_turn: "t1".into(),
                result: None,
            },
        );
        assert_eq!(
            projection.snapshot().classification,
            GoalNotificationClassification::ActionRequired
        );
        assert!(projection.snapshot().is_wake_eligible());
    }

    #[test]
    fn stale_generation_fails_closed() {
        let binding = binding();
        let mut replacement = binding.clone();
        replacement.control_generation += 1;
        let mut projection = GoalNotificationProjection::new(binding, ThreadGoalStatus::Active);
        assert!(!projection.observe(&replacement, GoalNotificationInput::ActionRequired));
        assert!(!projection.snapshot().is_wake_eligible());
    }

    #[test]
    fn store_exposes_only_projection_wake_decision() {
        let binding = binding();
        let store = GoalNotificationStore::default();
        let mut projection =
            GoalNotificationProjection::new(binding.clone(), ThreadGoalStatus::Active);
        projection.opt_in(&binding);
        projection.observe(&binding, GoalNotificationInput::ContinuationPending);
        store.install_authoritative(projection);
        assert_eq!(store.terminal_turn_is_wake_eligible(), Some(false));
        assert_eq!(
            store.snapshot().unwrap().phase,
            GoalNotificationPhase::ContinuationPending
        );
    }

    #[test]
    fn store_publishes_turn_completion_and_wakes_after_goal_completion() {
        let binding = binding();
        let store = GoalNotificationStore::default();
        let mut projection =
            GoalNotificationProjection::new(binding.clone(), ThreadGoalStatus::Active);
        projection.opt_in(&binding);
        store.install_authoritative(projection);
        store.set_source_turn("turn");
        let token = GoalNotificationTurnToken {
            incarnation: store.incarnation(),
            binding: binding.clone(),
            source_turn_id: "turn".into(),
            generation: binding.control_generation,
        };
        assert!(store.publish_continuation(&binding));
        assert!(store.publish_turn_complete(&token, Some("result".into())));
        assert_eq!(
            store.terminal_turn_is_wake_eligible_for_token(&token),
            Some(false)
        );
        assert!(store.publish(&binding, ThreadGoalStatus::Complete));
        assert_eq!(
            store.terminal_turn_is_wake_eligible_for_token(&token),
            Some(true)
        );
        assert_eq!(
            store
                .snapshot()
                .and_then(|snapshot| snapshot.result_reference),
            Some("result".into())
        );
    }

    #[test]
    fn blocked_goal_completion_remains_wake_eligible() {
        let binding = binding();
        let store = GoalNotificationStore::default();
        let mut projection =
            GoalNotificationProjection::new(binding.clone(), ThreadGoalStatus::Active);
        projection.opt_in(&binding);
        store.install_authoritative(projection);
        store.set_source_turn("turn");
        let token = GoalNotificationTurnToken {
            incarnation: store.incarnation(),
            binding: binding.clone(),
            source_turn_id: "turn".into(),
            generation: binding.control_generation,
        };
        assert!(store.publish(&binding, ThreadGoalStatus::Blocked));
        assert!(store.publish_turn_complete(&token, Some("result".into())));
        assert_eq!(
            store.terminal_turn_is_wake_eligible_for_token(&token),
            Some(true)
        );
    }

    #[test]
    fn forwarded_completion_tombstone_suppresses_concurrent_session_delivery() {
        let binding = binding();
        let store = GoalNotificationStore::default();
        let mut projection =
            GoalNotificationProjection::new(binding.clone(), ThreadGoalStatus::Active);
        projection.opt_in(&binding);
        store.install_authoritative(projection);
        store.set_source_turn("turn");
        let token = GoalNotificationTurnToken {
            incarnation: store.incarnation(),
            binding: binding.clone(),
            source_turn_id: "turn".into(),
            generation: binding.control_generation,
        };
        assert!(store.publish_continuation(&binding));
        assert!(store.publish_turn_complete(&token, Some("result".into())));
        let (claimed, result) = store.take_pending_completion().unwrap();
        assert_eq!(claimed, token);
        assert_eq!(result, Some("result".into()));
        assert!(store.mark_completion_forwarded(&claimed));
        store.clear_preserving_forwarded_completion();
        assert!(store.has_forwarded_completion());
        assert_eq!(
            store.terminal_turn_is_wake_eligible_for_token(&token),
            Some(false)
        );
        assert!(store.take_pending_completion().is_none());
        store.clear();
        assert!(!store.has_forwarded_completion());
        assert_eq!(store.terminal_turn_is_wake_eligible_for_token(&token), None);
    }

    #[test]
    fn store_stale_snapshot_cannot_wake_replacement() {
        let binding = binding();
        let mut replacement = binding.clone();
        replacement.control_generation += 1;
        let store = GoalNotificationStore::default();
        let mut projection = GoalNotificationProjection::new(binding, ThreadGoalStatus::Active);
        assert!(!projection.observe(&replacement, GoalNotificationInput::ActionRequired));
        store.install_authoritative(projection);
        assert_eq!(store.terminal_turn_is_wake_eligible(), None);
    }

    #[test]
    fn stale_token_generation_fails_closed() {
        let binding = binding();
        let store = GoalNotificationStore::default();
        let mut projection =
            GoalNotificationProjection::new(binding.clone(), ThreadGoalStatus::Active);
        projection.opt_in(&binding);
        projection.observe(&binding, GoalNotificationInput::ContinuationPending);
        store.install_authoritative(projection);
        let token = GoalNotificationTurnToken {
            incarnation: ThreadId::new(),
            binding: binding.clone(),
            source_turn_id: "turn".into(),
            generation: binding.control_generation,
        };
        assert_eq!(store.terminal_turn_is_wake_eligible_for_token(&token), None);
    }
}
