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
}

/// Thread-scoped holder shared by goal lifecycle hooks and the V2 producer.
pub struct GoalNotificationStore {
    projection: Mutex<Option<GoalNotificationProjection>>,
    incarnation: ThreadId,
    source_turn_id: Mutex<Option<String>>,
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

    pub fn clear(&self) {
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
        if token.incarnation != self.incarnation {
            return None;
        }
        if token.generation != token.binding.control_generation {
            return None;
        }
        if self
            .source_turn_id
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .as_deref()
            != Some(token.source_turn_id.as_str())
        {
            return None;
        }
        self.terminal_turn_is_wake_eligible_for(&token.binding)
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
                } else {
                    self.snapshot.phase = GoalNotificationPhase::ContinuationPending;
                    self.snapshot.classification = GoalNotificationClassification::Progress;
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
