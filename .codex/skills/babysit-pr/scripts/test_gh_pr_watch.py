import argparse
import importlib.util
import json
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).with_name("gh_pr_watch.py")
MODULE_SPEC = importlib.util.spec_from_file_location("gh_pr_watch", MODULE_PATH)
gh_pr_watch = importlib.util.module_from_spec(MODULE_SPEC)
assert MODULE_SPEC.loader is not None
MODULE_SPEC.loader.exec_module(gh_pr_watch)


def sample_pr():
    return {
        "number": 123,
        "url": "https://github.com/openai/codex/pull/123",
        "repo": "openai/codex",
        "head_sha": "abc123",
        "head_branch": "feature",
        "state": "OPEN",
        "merged": False,
        "closed": False,
        "mergeable": "MERGEABLE",
        "merge_state_status": "CLEAN",
        "review_decision": "",
    }


def sample_checks(**overrides):
    checks = {
        "pending_count": 0,
        "failed_count": 0,
        "passed_count": 12,
        "all_terminal": True,
    }
    checks.update(overrides)
    return checks


@pytest.fixture(autouse=True)
def default_queue_absent(monkeypatch, request):
    if request.node.name.startswith(("test_collect_snapshot_reads_graphql", "test_collect_malformed_queue")):
        return
    monkeypatch.setattr(
        gh_pr_watch,
        "get_merge_queue_entry",
        lambda *_args, **_kwargs: gh_pr_watch.normalize_merge_queue_entry(None),
    )


def test_resolve_pr_rejects_bare_number_without_repo(monkeypatch):
    called = False

    def unexpected_gh_json(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(gh_pr_watch, "gh_json", unexpected_gh_json)

    with pytest.raises(
        gh_pr_watch.GhCommandError,
        match="Bare PR numbers are ambiguous",
    ):
        gh_pr_watch.resolve_pr("535")

    assert called is False


def test_resolve_pr_rejects_url_repo_override_mismatch(monkeypatch):
    monkeypatch.setattr(
        gh_pr_watch,
        "gh_json",
        lambda *_args, **_kwargs: {
            "number": 535,
            "url": "https://github.com/sednalabs/codex/pull/535",
            "state": "OPEN",
            "headRefOid": "abc123",
            "headRefName": "feature",
            "headRepository": {"nameWithOwner": "sednalabs/codex"},
            "baseRefName": "main",
            "baseRefOid": "def456",
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
        },
    )

    with pytest.raises(
        gh_pr_watch.GhCommandError,
        match="belongs to sednalabs/codex, not explicit --repo sednalabs/agent-ops",
    ):
        gh_pr_watch.resolve_pr(
            "https://github.com/sednalabs/codex/pull/535",
            repo_override="sednalabs/agent-ops",
        )


def test_collect_snapshot_fetches_review_items_before_ci(monkeypatch, tmp_path):
    call_order = []
    pr = sample_pr()

    monkeypatch.setattr(gh_pr_watch, "resolve_pr", lambda *args, **kwargs: pr)
    monkeypatch.setattr(
        gh_pr_watch,
        "detect_local_git_context",
        lambda: {
            "cwd": "",
            "git_root": "",
            "origin_repo": "openai/codex",
            "origin_url": "",
            "upstream_repo": "",
            "upstream_url": "",
        },
    )
    monkeypatch.setattr(gh_pr_watch, "load_state", lambda path: ({}, True))
    monkeypatch.setattr(
        gh_pr_watch,
        "get_authenticated_login",
        lambda *args, **kwargs: call_order.append("auth") or "octocat",
    )
    monkeypatch.setattr(
        gh_pr_watch,
        "fetch_new_review_items",
        lambda *args, **kwargs: call_order.append("review") or [],
    )
    monkeypatch.setattr(
        gh_pr_watch,
        "get_review_threads",
        lambda *args, **kwargs: call_order.append("threads") or [],
    )
    monkeypatch.setattr(
        gh_pr_watch,
        "partition_unresolved_review_threads",
        lambda *args, **kwargs: ([], []),
    )
    monkeypatch.setattr(
        gh_pr_watch,
        "build_actionable_review_items",
        lambda *args, **kwargs: call_order.append("actionable") or [],
    )
    monkeypatch.setattr(
        gh_pr_watch,
        "get_pr_checks",
        lambda *args, **kwargs: call_order.append("checks") or [],
    )
    monkeypatch.setattr(
        gh_pr_watch,
        "summarize_checks",
        lambda checks: call_order.append("summarize") or sample_checks(),
    )
    monkeypatch.setattr(
        gh_pr_watch,
        "get_workflow_runs_for_sha",
        lambda *args, **kwargs: call_order.append("workflow") or [],
    )
    monkeypatch.setattr(
        gh_pr_watch,
        "failed_runs_from_workflow_runs",
        lambda *args, **kwargs: call_order.append("failed_runs") or [],
    )
    monkeypatch.setattr(
        gh_pr_watch,
        "failed_jobs_from_workflow_runs",
        lambda *args, **kwargs: call_order.append("failed_jobs") or [],
    )
    monkeypatch.setattr(
        gh_pr_watch,
        "recommend_actions",
        lambda *args, **kwargs: call_order.append("recommend") or ["idle"],
    )
    monkeypatch.setattr(gh_pr_watch, "save_state", lambda *args, **kwargs: None)

    args = argparse.Namespace(
        pr="123",
        repo=None,
        state_file=f"{tmp_path.name}-watcher-state.json",
        ignore_review_thread=[],
        max_flaky_retries=3,
        reset_seen_feedback=False,
    )

    gh_pr_watch.collect_snapshot(args)

    assert "auth" in call_order
    assert call_order.index("review") < call_order.index("checks")
    assert call_order.index("review") < call_order.index("workflow")


def test_installation_observer_skips_user_lookup_and_passes_unbound_identity(
    monkeypatch, tmp_path
):
    pr = sample_pr()
    observed = {}

    monkeypatch.setattr(gh_pr_watch, "resolve_pr", lambda *_args, **_kwargs: pr)
    monkeypatch.setattr(gh_pr_watch, "detect_local_git_context", lambda: {})
    monkeypatch.setattr(gh_pr_watch, "load_state", lambda _path: ({}, True))

    def unexpected_user_lookup(*_args, **_kwargs):
        raise AssertionError("installation observer must not query gh api user")

    monkeypatch.setattr(gh_pr_watch, "get_authenticated_login", unexpected_user_lookup)
    monkeypatch.setattr(
        gh_pr_watch,
        "fetch_new_review_items",
        lambda *_args, **kwargs: (
            observed.setdefault("authenticated_login", kwargs["authenticated_login"])
            or []
        ),
    )
    monkeypatch.setattr(gh_pr_watch, "get_review_threads", lambda *_args: [])
    monkeypatch.setattr(
        gh_pr_watch, "partition_unresolved_review_threads", lambda *_args: ([], [])
    )
    monkeypatch.setattr(gh_pr_watch, "build_actionable_review_items", lambda *_args: [])
    monkeypatch.setattr(gh_pr_watch, "get_pr_checks", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(gh_pr_watch, "summarize_checks", lambda _checks: sample_checks())
    monkeypatch.setattr(gh_pr_watch, "get_workflow_runs_for_sha", lambda *_args: [])
    monkeypatch.setattr(gh_pr_watch, "failed_runs_from_workflow_runs", lambda *_args: [])
    monkeypatch.setattr(
        gh_pr_watch, "failed_jobs_from_workflow_runs", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(gh_pr_watch, "recommend_actions", lambda *_args: ["idle"])
    monkeypatch.setattr(gh_pr_watch, "save_state", lambda *_args: None)

    args = argparse.Namespace(
        pr="123",
        repo="openai/codex",
        state_file=f"{tmp_path.name}-watcher-state.json",
        ignore_review_thread=[],
        max_flaky_retries=3,
        reset_seen_feedback=False,
        installation_observer=True,
    )

    gh_pr_watch.collect_snapshot(args)

    assert observed["authenticated_login"] is None


def test_installation_observer_filters_untrusted_items_without_identity(monkeypatch):
    state = {}
    items = [
        {
            "id": 1,
            "user": {"login": "owner"},
            "author_association": "OWNER",
            "body": "ok",
        },
        {
            "id": 2,
            "user": {"login": "member"},
            "author_association": "MEMBER",
            "body": "ok",
        },
        {
            "id": 3,
            "user": {"login": "collab"},
            "author_association": "COLLABORATOR",
            "body": "ok",
        },
        {
            "id": 4,
            "user": {"login": "codex-review[bot]"},
            "author_association": "NONE",
            "body": "ok",
        },
        {
            "id": 5,
            "user": {"login": "outsider"},
            "author_association": "NONE",
            "body": "ignore",
        },
    ]
    monkeypatch.setattr(
        gh_pr_watch,
        "gh_api_list_paginated",
        lambda endpoint, **_kwargs: (
            items if endpoint.endswith("/issues/123/comments") else []
        ),
    )

    surfaced = gh_pr_watch.fetch_new_review_items(
        sample_pr(), state, fresh_state=True, authenticated_login=None
    )

    assert {item["id"] for item in surfaced} == {"1", "2", "3", "4"}


def test_parse_args_accepts_installation_observer(monkeypatch):
    monkeypatch.setattr(
        gh_pr_watch.sys, "argv", ["gh_pr_watch.py", "--installation-observer"]
    )

    args = gh_pr_watch.parse_args()

    assert args.installation_observer is True


def test_installation_observer_rejects_retry_without_mutation(monkeypatch):
    mutation_called = False
    gh_called = False

    def unexpected_mutation(*_args, **_kwargs):
        nonlocal mutation_called
        mutation_called = True

    def unexpected_gh(*_args, **_kwargs):
        nonlocal gh_called
        gh_called = True

    monkeypatch.setattr(
        gh_pr_watch.sys,
        "argv",
        [
            "gh_pr_watch.py",
            "--installation-observer",
            "--retry-failed-now",
            "--expected-head-sha",
            "abc123",
        ],
    )
    monkeypatch.setattr(gh_pr_watch, "retry_failed_now", unexpected_mutation)
    monkeypatch.setattr(gh_pr_watch, "gh_json", unexpected_gh)

    with pytest.raises(SystemExit) as error:
        gh_pr_watch.main()

    assert error.value.code == 2
    assert mutation_called is False
    assert gh_called is False


def test_collect_snapshot_discovers_pending_workflow_failures_and_reuses_completed_jobs(
    monkeypatch, tmp_path
):
    """Workflow-only failures must be actionable before the PR rollup settles."""
    pr = sample_pr()
    workflow_calls = []
    job_calls = []
    runs = iter(
        [
            [workflow_run(99, run_attempt=1)],
            [workflow_run(99, run_attempt=1)],
            [workflow_run(99, run_attempt=2)],
        ]
    )

    monkeypatch.setattr(gh_pr_watch, "resolve_pr", lambda *_args, **_kwargs: pr)
    monkeypatch.setattr(gh_pr_watch, "detect_local_git_context", lambda: {})
    monkeypatch.setattr(gh_pr_watch, "load_state", lambda _path: ({}, True))
    monkeypatch.setattr(gh_pr_watch, "get_authenticated_login", lambda *_args: "octocat")
    monkeypatch.setattr(gh_pr_watch, "fetch_new_review_items", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(gh_pr_watch, "get_review_threads", lambda *_args: [])
    monkeypatch.setattr(
        gh_pr_watch, "partition_unresolved_review_threads", lambda *_args: ([], [])
    )
    monkeypatch.setattr(gh_pr_watch, "build_actionable_review_items", lambda *_args: [])
    monkeypatch.setattr(
        gh_pr_watch,
        "get_pr_checks",
        lambda *_args, **_kwargs: [{"bucket": "pending", "state": "PENDING"}],
    )
    monkeypatch.setattr(
        gh_pr_watch,
        "get_workflow_runs_for_sha",
        lambda repo, head_sha: workflow_calls.append((repo, head_sha)) or next(runs),
    )
    monkeypatch.setattr(
        gh_pr_watch,
        "get_jobs_for_run",
        lambda repo, run_id: job_calls.append((repo, run_id))
        or [{"id": 501, "name": "unit", "conclusion": "failure"}],
    )
    monkeypatch.setattr(gh_pr_watch, "save_state", lambda *_args: None)
    args = argparse.Namespace(
        pr="123",
        repo="openai/codex",
        state_file=f"{tmp_path.name}-watcher-state.json",
        ignore_review_thread=[],
        max_flaky_retries=3,
        reset_seen_feedback=False,
    )
    cache = {}

    first, _ = gh_pr_watch.collect_snapshot(args, cache=cache)
    second, _ = gh_pr_watch.collect_snapshot(args, cache=cache)
    rerun, _ = gh_pr_watch.collect_snapshot(args, cache=cache)

    assert first["checks"]["all_terminal"] is False
    assert first["failed_runs"] == [{
        "run_id": 99,
        "workflow_name": "",
        "status": "completed",
        "conclusion": "failure",
        "html_url": "",
    }]
    assert first["actions"] == ["diagnose_ci_failure"]
    assert second["actions"] == ["diagnose_ci_failure"]
    assert rerun["actions"] == ["diagnose_ci_failure"]
    assert workflow_calls == [("openai/codex", "abc123")] * 3
    assert job_calls == [("openai/codex", 99), ("openai/codex", 99)]


def test_collect_snapshot_discovers_current_head_before_terminal_readiness(
    monkeypatch, tmp_path
):
    first_pr = sample_pr()
    second_pr = sample_pr()
    second_pr["head_sha"] = "def456"
    prs = iter([first_pr, second_pr])
    workflow_heads = []
    check_summaries = iter(
        [
            [{"bucket": "pending", "state": "PENDING"}],
            [{"bucket": "pass", "state": "SUCCESS"}],
        ]
    )

    monkeypatch.setattr(gh_pr_watch, "resolve_pr", lambda *_args, **_kwargs: next(prs))
    monkeypatch.setattr(gh_pr_watch, "detect_local_git_context", lambda: {})
    monkeypatch.setattr(gh_pr_watch, "load_state", lambda _path: ({}, True))
    monkeypatch.setattr(gh_pr_watch, "get_authenticated_login", lambda *_args: "octocat")
    monkeypatch.setattr(gh_pr_watch, "fetch_new_review_items", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(gh_pr_watch, "get_review_threads", lambda *_args: [])
    monkeypatch.setattr(
        gh_pr_watch, "partition_unresolved_review_threads", lambda *_args: ([], [])
    )
    monkeypatch.setattr(gh_pr_watch, "build_actionable_review_items", lambda *_args: [])
    monkeypatch.setattr(
        gh_pr_watch, "get_pr_checks", lambda *_args, **_kwargs: next(check_summaries)
    )
    monkeypatch.setattr(
        gh_pr_watch,
        "get_workflow_runs_for_sha",
        lambda _repo, head_sha: workflow_heads.append(head_sha) or [],
    )
    monkeypatch.setattr(gh_pr_watch, "save_state", lambda *_args: None)
    args = argparse.Namespace(
        pr="123",
        repo="openai/codex",
        state_file=f"{tmp_path.name}-watcher-state.json",
        ignore_review_thread=[],
        max_flaky_retries=3,
        reset_seen_feedback=False,
    )

    pending, _ = gh_pr_watch.collect_snapshot(args, cache={})
    terminal, _ = gh_pr_watch.collect_snapshot(args, cache={})

    assert pending["actions"] == ["idle"]
    assert terminal["checks"]["all_terminal"] is True
    assert terminal["actions"] == ["stop_ready_to_merge"]
    assert workflow_heads == ["abc123", "def456"]


def test_recommend_actions_prioritizes_review_comments():
    actions = gh_pr_watch.recommend_actions(
        sample_pr(),
        sample_checks(failed_count=1),
        [{"run_id": 99}],
        [],
        [{"kind": "review_comment", "id": "1"}],
        {},
        0,
        3,
    )

    assert actions == [
        "process_review_comment",
        "diagnose_ci_failure",
        "retry_failed_checks",
    ]


def test_blocked_merge_policy_is_action_required_but_clean_is_ready():
    blocked = sample_pr()
    blocked["merge_state_status"] = "BLOCKED"
    actions = gh_pr_watch.recommend_actions(
        blocked,
        sample_checks(),
        [],
        [],
        [],
        {},
        0,
        3,
    )
    assert actions == [gh_pr_watch.ACTION_REQUIRED_MERGE_POLICY_BLOCKED]

    clean = sample_pr()
    assert gh_pr_watch.recommend_actions(
        clean, sample_checks(), [], [], [], {}, 0, 3
    ) == ["stop_ready_to_merge"]


@pytest.mark.parametrize(
    "checks, failed_jobs, actionable, review_state, review_decision, expected",
    [
        (sample_checks(pending_count=1, all_terminal=False), [], [], {}, "", ["idle"]),
        (sample_checks(failed_count=1), [], [], {}, "", ["diagnose_ci_failure"]),
        (sample_checks(), [], [{"kind": "review_comment", "id": "1"}], {}, "", ["process_review_comment"]),
        (sample_checks(), [], [], {"active_unresolved_thread_count": 1}, "", ["idle"]),
        (sample_checks(), [], [], {}, "CHANGES_REQUESTED", ["idle"]),
    ],
)
def test_blocked_with_explaining_evidence_does_not_add_policy_action(
    checks, failed_jobs, actionable, review_state, review_decision, expected
):
    blocked = sample_pr()
    blocked["merge_state_status"] = "BLOCKED"
    blocked["review_decision"] = review_decision
    assert gh_pr_watch.recommend_actions(
        blocked, checks, [], failed_jobs, actionable, review_state, 0, 3
    ) == expected


@pytest.mark.parametrize("mergeable", ["CONFLICTING", "UNKNOWN", ""])
def test_blocked_mergeability_evidence_is_not_an_unexplained_policy(mergeable):
    blocked = sample_pr()
    blocked.update(merge_state_status="BLOCKED", mergeable=mergeable)
    assert gh_pr_watch.recommend_actions(
        blocked, sample_checks(), [], [], [], {}, 0, 3
    ) == ["idle"]


def test_policy_blocker_does_not_backoff_and_decision_is_exact_head(monkeypatch):
    args = argparse.Namespace(poll_seconds=30)
    snapshot = {
        "pr": {"repo": "openai/codex", "number": 123, "head_sha": "abc123"},
        "checks": sample_checks(),
        "review_state": {},
        "actions": [gh_pr_watch.ACTION_REQUIRED_MERGE_POLICY_BLOCKED],
    }
    delay, _ = gh_pr_watch.next_watch_poll_seconds(
        args, snapshot, ("unchanged",), 600, 3600
    )
    assert delay == 30
    decision = gh_pr_watch.build_watch_decision(snapshot, recorded_at=100)
    assert decision["head_sha"] == "abc123"
    assert decision["decision"] == "action_required"
    assert decision["primary_action"] == gh_pr_watch.ACTION_REQUIRED_MERGE_POLICY_BLOCKED


@pytest.mark.parametrize("queue_state", ["QUEUED", "AWAITING_CHECKS"])
def test_active_merge_queue_wait_uses_base_cadence_when_checks_are_green(queue_state):
    args = argparse.Namespace(poll_seconds=30)
    snapshot = {
        "checks": sample_checks(),
        "actions": ["idle"],
        "pr": {"repo": "openai/codex", "number": 123, "head_sha": "abc123", "merge_queue": {"status": "waiting", "id": "entry-1", "state": queue_state, "head_sha": "abc123"}},
    }

    delay, _ = gh_pr_watch.next_watch_poll_seconds(
        args, snapshot, gh_pr_watch.snapshot_change_key(snapshot), 600, 3600
    )
    assert delay == 30


def test_collect_snapshot_reads_graphql_queue_and_keeps_base_cadence(monkeypatch, tmp_path):
    pr = sample_pr()
    monkeypatch.setattr(gh_pr_watch, "resolve_pr", lambda *_args, **_kwargs: pr)
    monkeypatch.setattr(gh_pr_watch, "detect_local_git_context", lambda: {})
    monkeypatch.setattr(gh_pr_watch, "load_state", lambda _path: ({}, True))
    monkeypatch.setattr(gh_pr_watch, "save_state", lambda *_args: None)
    monkeypatch.setattr(gh_pr_watch, "get_authenticated_login", lambda *_args: "octocat")
    monkeypatch.setattr(gh_pr_watch, "fetch_new_review_items", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(gh_pr_watch, "get_review_threads", lambda *_args: [])
    monkeypatch.setattr(gh_pr_watch, "partition_unresolved_review_threads", lambda *_args: ([], []))
    monkeypatch.setattr(gh_pr_watch, "build_actionable_review_items", lambda *_args: [])
    monkeypatch.setattr(gh_pr_watch, "get_pr_checks", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(gh_pr_watch, "get_workflow_runs_for_sha", lambda *_args: [])
    monkeypatch.setattr(gh_pr_watch, "failed_runs_from_workflow_runs", lambda *_args: [])
    monkeypatch.setattr(gh_pr_watch, "failed_jobs_from_workflow_runs", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(gh_pr_watch, "gh_json", lambda *_args, **_kwargs: {"data": {"repository": {"pullRequest": {"mergeQueueEntry": {"id": "entry-1", "state": "QUEUED", "position": 2}}}}})
    args = argparse.Namespace(pr="123", repo="openai/codex", state_file=f"{tmp_path.name}.json", ignore_review_thread=[], max_flaky_retries=3, reset_seen_feedback=False)
    snapshot, _ = gh_pr_watch.collect_snapshot(args)
    assert snapshot["pr"]["merge_queue"]["status"] == "waiting"
    delay, _ = gh_pr_watch.next_watch_poll_seconds(argparse.Namespace(poll_seconds=30), snapshot, gh_pr_watch.snapshot_change_key(snapshot), 600, 3600)
    assert delay == 30


def test_unreadable_pending_queue_head_stays_on_base_cadence():
    args = argparse.Namespace(poll_seconds=30)
    snapshot = {
        "checks": sample_checks(),
        "actions": ["idle"],
        "pr": {"repo": "openai/codex", "number": 123, "head_sha": "abc123", "merge_queue": {"status": "waiting", "id": "entry-1", "state": "QUEUED", "head_sha": "", "read_state": "unreadable"}},
    }

    delay, _ = gh_pr_watch.next_watch_poll_seconds(
        args, snapshot, gh_pr_watch.snapshot_change_key(snapshot), 600, 3600
    )
    assert delay == 30


def test_queue_identity_change_resets_cadence():
    args = argparse.Namespace(poll_seconds=30)
    old = {
        "checks": sample_checks(),
        "actions": ["idle"],
        "pr": {"repo": "openai/codex", "number": 123, "head_sha": "abc123", "merge_queue": {"status": "waiting", "id": "entry-1", "state": "QUEUED"}},
    }
    current = {
        **old,
        "pr": {"repo": "openai/codex", "number": 123, "head_sha": "abc123", "merge_queue": {"status": "waiting", "id": "entry-2", "state": "QUEUED"}},
    }

    delay, _ = gh_pr_watch.next_watch_poll_seconds(
        args, current, gh_pr_watch.snapshot_change_key(old), 600, 3600
    )
    assert delay == 30


def test_nonqueued_green_idle_snapshot_keeps_backoff():
    args = argparse.Namespace(poll_seconds=30)
    snapshot = {
        "pr": {"repo": "openai/codex", "number": 123, "head_sha": "abc123"},
        "checks": sample_checks(),
        "actions": ["idle"],
        "merge_blockers": [],
    }

    delay, _ = gh_pr_watch.next_watch_poll_seconds(
        args, snapshot, gh_pr_watch.snapshot_change_key(snapshot), 600, 3600
    )
    assert delay == 1200


@pytest.mark.parametrize(
    "queue_status, expected",
    [
        ("failed", gh_pr_watch.STOP_MERGE_QUEUE_FAILED),
        ("removed", gh_pr_watch.STOP_MERGE_QUEUE_REMOVED),
    ],
)
def test_queue_failure_and_removal_are_actionable(queue_status, expected):
    pr = sample_pr()
    pr["merge_queue"] = {"status": queue_status, "state": "QUEUED", "id": "entry-1"}
    actions = gh_pr_watch.recommend_actions(
        pr, sample_checks(), [], [], [], {}, 0, 3
    )
    assert expected in actions


def test_active_queue_cannot_report_ready():
    pr = sample_pr()
    pr["merge_queue"] = {"status": "waiting", "state": "QUEUED", "id": "entry-1"}
    assert not gh_pr_watch.is_pr_ready_to_merge(pr, sample_checks(), [], {})


@pytest.mark.parametrize("state", sorted(gh_pr_watch.MERGE_QUEUE_WAITING_STATES))
def test_active_queue_explains_blocked_merge_state(state):
    pr = sample_pr()
    pr.update(merge_state_status="BLOCKED", merge_queue={"status": "waiting", "state": state, "id": "entry-1"})
    assert gh_pr_watch.recommend_actions(pr, sample_checks(), [], [], [], {}, 0, 3) == ["idle"]


def test_queue_tombstone_preserves_failure_and_removal_until_head_changes():
    base = {"head_sha": "head-1"}
    state = {}
    failed = {**base, "merge_queue": {"status": "failed", "id": "entry-1", "state": "FAILED"}}
    gh_pr_watch.reconcile_merge_queue_entry(failed, state)
    assert gh_pr_watch.reconcile_merge_queue_entry({**base, "merge_queue": {"status": "absent"}}, state)["status"] == "failed"

    removed = {**base, "merge_queue": {"status": "removed", "id": "entry-1", "state": "QUEUED"}}
    gh_pr_watch.reconcile_merge_queue_entry(removed, state)
    assert gh_pr_watch.reconcile_merge_queue_entry({**base, "merge_queue": {"status": "absent"}}, state)["status"] == "removed"

    new_head = {"head_sha": "head-2", "merge_queue": {"status": "absent"}}
    assert gh_pr_watch.reconcile_merge_queue_entry(new_head, state)["status"] == "absent"


@pytest.mark.parametrize("payload", [{"data": ["bad"]}, {"data": {"repository": "bad"}}, {"data": {"repository": {"pullRequest": "bad"}}}])
def test_collect_malformed_queue_payload_is_unknown_and_not_ready(monkeypatch, payload):
    monkeypatch.setattr(gh_pr_watch, "gh_json", lambda *_args, **_kwargs: payload)
    queue = gh_pr_watch.get_merge_queue_entry("openai/codex", 123)
    assert queue["status"] == "unknown"
    pr = sample_pr()
    pr["merge_queue"] = queue
    actions = gh_pr_watch.recommend_actions(pr, sample_checks(), [], [], [], {}, 0, 3)
    assert actions == [gh_pr_watch.STOP_MERGE_QUEUE_READ_ERROR]


def test_schedule_persists_exact_head_and_fake_clock(monkeypatch, tmp_path):
    saved = {}
    monkeypatch.setattr(gh_pr_watch, "load_state", lambda _path: ({}, True))
    monkeypatch.setattr(gh_pr_watch, "save_state", lambda _path, state: saved.update(state))
    snapshot = {"pr": {"repo": "openai/codex", "number": 123, "head_sha": "abc123"}}

    gh_pr_watch.persist_watch_schedule(
        tmp_path / "state.json",
        snapshot,
        "watch-until-action",
        30,
        scheduled_at=100,
    )

    assert saved["watch_schedule"] == {
        "schema_version": 1,
        "mode": "watch-until-action",
        "repo": "openai/codex",
        "number": 123,
        "head_sha": "abc123",
        "poll_seconds": 30,
        "scheduled_at": 100,
        "wake_at": 130,
    }


def test_pending_review_feedback_surfaces_only_after_publication(monkeypatch):
    state = {
        "seen_review_comment_ids": ["20"],
        "seen_review_ids": ["10"],
    }
    review = {
        "id": 10,
        "user": {"login": "octocat"},
        "author_association": "MEMBER",
        "state": "PENDING",
        "body": "Please rename this.",
        "created_at": "2026-06-08T10:00:00Z",
        "submitted_at": None,
        "html_url": "https://github.com/openai/codex/pull/123#pullrequestreview-10",
    }
    review_comment = {
        "id": 20,
        "pull_request_review_id": 10,
        "user": {"login": "octocat"},
        "author_association": "MEMBER",
        "body": "Please rename this.",
        "created_at": "2026-06-08T10:00:00Z",
        "path": "src/example.rs",
        "line": 7,
        "html_url": "https://github.com/openai/codex/pull/123#discussion_r20",
    }

    def fake_list(endpoint, **kwargs):
        if endpoint.endswith("/issues/123/comments"):
            return []
        if endpoint.endswith("/pulls/123/comments"):
            return [review_comment]
        if endpoint.endswith("/pulls/123/reviews"):
            return [review]
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    monkeypatch.setattr(gh_pr_watch, "gh_api_list_paginated", fake_list)

    assert (
        gh_pr_watch.fetch_new_review_items(
            sample_pr(),
            state,
            fresh_state=True,
            authenticated_login="octocat",
        )
        == []
    )
    assert state["seen_review_comment_ids"] == []
    assert state["seen_review_ids"] == []

    review["state"] = "COMMENTED"
    review["submitted_at"] = "2026-06-08T10:05:00Z"

    published_items = gh_pr_watch.fetch_new_review_items(
        sample_pr(),
        state,
        fresh_state=False,
        authenticated_login="octocat",
    )

    assert {(item["kind"], item["id"]) for item in published_items} == {
        ("review", "10"),
        ("review_comment", "20"),
    }
    assert state["seen_review_comment_ids"] == ["20"]
    assert state["seen_review_ids"] == ["10"]


def test_run_watch_keeps_polling_open_ready_to_merge_pr(monkeypatch):
    sleeps = []
    events = []
    snapshot = {
        "pr": sample_pr(),
        "checks": sample_checks(),
        "failed_runs": [],
        "failed_jobs": [],
        "new_review_items": [],
        "actions": ["ready_to_merge"],
        "retry_state": {
            "current_sha_retries_used": 0,
            "max_flaky_retries": 3,
        },
    }

    monkeypatch.setattr(
        gh_pr_watch,
        "collect_snapshot",
        lambda args, cache=None: (snapshot, Path("/tmp/codex-babysit-pr-state.json")),
    )
    monkeypatch.setattr(
        gh_pr_watch,
        "print_event",
        lambda event, payload: events.append((event, payload)),
    )

    class StopWatch(Exception):
        pass

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise StopWatch

    monkeypatch.setattr(gh_pr_watch.time, "sleep", fake_sleep)

    with pytest.raises(StopWatch):
        gh_pr_watch.run_watch(argparse.Namespace(poll_seconds=30))

    assert sleeps == [30, 60]
    assert [event for event, _ in events] == ["snapshot", "snapshot"]


def test_failed_jobs_include_direct_logs_endpoint(monkeypatch):
    jobs_by_run = {
        99: [
            {
                "id": 555,
                "name": "unit tests",
                "status": "completed",
                "conclusion": "failure",
                "html_url": "https://github.com/openai/codex/actions/runs/99/job/555",
            },
            {
                "id": 556,
                "name": "lint",
                "status": "completed",
                "conclusion": "success",
            },
        ]
    }

    monkeypatch.setattr(
        gh_pr_watch,
        "get_jobs_for_run",
        lambda repo, run_id: jobs_by_run[run_id],
    )

    failed_jobs = gh_pr_watch.failed_jobs_from_workflow_runs(
        "openai/codex",
        [
            {
                "id": 99,
                "name": "CI",
                "status": "in_progress",
                "conclusion": "",
                "head_sha": "abc123",
            }
        ],
        "abc123",
    )

    assert failed_jobs == [
        {
            "run_id": 99,
            "workflow_name": "CI",
            "run_status": "in_progress",
            "run_conclusion": "",
            "job_id": 555,
            "job_name": "unit tests",
            "status": "completed",
            "conclusion": "failure",
            "html_url": "https://github.com/openai/codex/actions/runs/99/job/555",
            "logs_endpoint": "repos/openai/codex/actions/jobs/555/logs",
        }
    ]


def test_failed_jobs_reuses_completed_run_attempt(monkeypatch):
    calls = []
    jobs = [{"id": 555, "name": "unit", "status": "completed", "conclusion": "failure"}]
    monkeypatch.setattr(
        gh_pr_watch,
        "get_jobs_for_run",
        lambda repo, run_id: calls.append((repo, run_id)) or jobs,
    )
    run = {
        "id": 99,
        "name": "CI",
        "status": "completed",
        "conclusion": "failure",
        "run_attempt": 2,
        "head_sha": "abc123",
    }
    cache = {}
    first = gh_pr_watch.failed_jobs_from_workflow_runs(
        "openai/codex", [run], "abc123", cache=cache
    )
    second = gh_pr_watch.failed_jobs_from_workflow_runs(
        "openai/codex", [run], "abc123", cache=cache
    )
    assert first == second
    assert calls == [("openai/codex", 99)]


def test_parse_args_watch_until_terminal_implies_terminal_checks(monkeypatch):
    monkeypatch.setattr(
        gh_pr_watch.sys,
        "argv",
        ["gh_pr_watch.py", "--watch-until-terminal"],
    )

    args = gh_pr_watch.parse_args()

    assert args.watch_until_terminal is True
    assert args.watch_until_action is True
    assert args.require_terminal_checks is True
    assert args.once is False


def test_compact_wait_snapshot_caps_review_body():
    snapshot = {
        "pr": {
            "repo": "openai/codex",
            "number": 123,
            "head_sha": "abc123",
            "state": "OPEN",
            "large_unneeded_field": "x" * 5000,
        },
        "checks": {"all_terminal": True},
        "actionable_review_items": [
            {
                "kind": "thread",
                "id": "thread-1",
                "body": "y" * 2000,
                "large_unneeded_field": "z" * 5000,
            }
        ],
        "actions": ["address_review_feedback"],
        "watch_decision": {"head_sha": "abc123", "decision": "action_required"},
    }

    compact = gh_pr_watch.compact_wait_snapshot(snapshot)

    assert "large_unneeded_field" not in compact["pr"]
    assert "large_unneeded_field" not in compact["actionable_review_items"][0]
    assert len(compact["actionable_review_items"][0]["body"]) == 1000
    assert compact["watch_decision"]["head_sha"] == "abc123"


def test_watch_until_action_is_silent_by_default(monkeypatch, tmp_path, capsys):
    idle_snapshot = {
        "pr": {"head_sha": "abc123"},
        "checks": {
            "all_terminal": False,
            "failed_count": 0,
            "pending_count": 1,
            "passed_count": 0,
        },
        "review_state": {"active_unresolved_thread_count": 0},
        "actionable_review_items": [],
        "actions": ["idle"],
    }
    action_snapshot = {
        "pr": {"head_sha": "abc123"},
        "checks": {
            "all_terminal": True,
            "failed_count": 1,
            "pending_count": 0,
            "passed_count": 0,
        },
        "review_state": {"active_unresolved_thread_count": 0},
        "actionable_review_items": [],
        "actions": ["diagnose_ci_failure"],
        "watch_decision": {"head_sha": "abc123", "decision": "action_required"},
    }
    snapshots = iter(
        [
            (idle_snapshot, tmp_path / "state.json"),
            (action_snapshot, tmp_path / "state.json"),
        ]
    )
    monkeypatch.setattr(gh_pr_watch, "collect_snapshot", lambda args, cache=None: next(snapshots))
    schedules = []
    monkeypatch.setattr(
        gh_pr_watch,
        "persist_watch_schedule",
        lambda _path, _snapshot, mode, delay: schedules.append((mode, delay)),
    )
    monkeypatch.setattr(gh_pr_watch.time, "sleep", lambda _seconds: None)
    args = argparse.Namespace(
        poll_seconds=30,
        require_terminal_checks=False,
        progress=False,
        verbose_details=False,
    )

    assert gh_pr_watch.run_watch_until_action(args) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    receipt = json.loads(captured.out)
    assert receipt["polls_completed"] == 2
    assert receipt["snapshot"]["actions"] == ["diagnose_ci_failure"]
    assert receipt["snapshot"]["watch_decision"]["head_sha"] == "abc123"
    assert schedules[-1] == ("watch-until-action", 0)


def test_watch_until_action_waits_for_terminal_ci_failure(
    monkeypatch, tmp_path, capsys
):
    in_progress_failure = {
        "pr": {"head_sha": "abc123"},
        "checks": {
            "all_terminal": False,
            "failed_count": 1,
            "pending_count": 1,
            "passed_count": 0,
        },
        "actions": ["diagnose_ci_failure"],
    }
    terminal_failure = {
        "pr": {"head_sha": "abc123"},
        "checks": {
            "all_terminal": True,
            "failed_count": 1,
            "pending_count": 0,
            "passed_count": 0,
        },
        "actions": ["diagnose_ci_failure"],
    }
    snapshots = iter(
        [
            (in_progress_failure, tmp_path / "state.json"),
            (terminal_failure, tmp_path / "state.json"),
        ]
    )
    monkeypatch.setattr(gh_pr_watch, "collect_snapshot", lambda args, cache=None: next(snapshots))
    monkeypatch.setattr(gh_pr_watch.time, "sleep", lambda _seconds: None)
    args = argparse.Namespace(
        poll_seconds=30,
        require_terminal_checks=True,
        progress=False,
        verbose_details=False,
    )

    assert gh_pr_watch.run_watch_until_action(args) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["polls_completed"] == 2
    assert receipt["snapshot"]["checks"]["all_terminal"] is True


def retry_snapshot(*run_ids, checks=None):
    return {
        "pr": sample_pr(),
        "checks": sample_checks(**(checks or {"failed_count": 1})),
        "failed_runs": [{"run_id": run_id} for run_id in run_ids],
        "retry_state": {
            "current_sha_retries_used": 0,
            "max_flaky_retries": 3,
        },
    }


def retry_args(*run_ids, expected_head_sha="abc123", max_flaky_retries=3):
    return argparse.Namespace(
        pr="https://github.com/openai/codex/pull/123",
        repo="openai/codex",
        expected_head_sha=expected_head_sha,
        run_ids=[str(run_id) for run_id in run_ids] or None,
        max_flaky_retries=max_flaky_retries,
    )


def workflow_run(run_id=99, **overrides):
    run = {
        "id": run_id,
        "head_sha": "abc123",
        "status": "completed",
        "conclusion": "failure",
        "run_attempt": 1,
        "pull_requests": [{"number": 123}],
    }
    run.update(overrides)
    return run


def install_retry_snapshot(monkeypatch, snapshot):
    monkeypatch.setattr(
        gh_pr_watch,
        "collect_snapshot",
        lambda _args, cache=None: (snapshot, Path("/tmp/codex-babysit-pr-state.json")),
    )
    monkeypatch.setattr(gh_pr_watch, "load_state", lambda _path: ({}, True))
    monkeypatch.setattr(gh_pr_watch, "save_state", lambda *_args: None)


def test_retry_rejects_pr_head_change_before_mutation(monkeypatch):
    install_retry_snapshot(monkeypatch, retry_snapshot(99))
    current_pr = sample_pr()
    current_pr["head_sha"] = "changed"
    monkeypatch.setattr(gh_pr_watch, "resolve_pr", lambda *_args, **_kwargs: current_pr)
    mutation_calls = []
    monkeypatch.setattr(
        gh_pr_watch,
        "gh_text",
        lambda *args, **kwargs: mutation_calls.append((args, kwargs)),
    )

    result = gh_pr_watch.retry_failed_now(retry_args(99))

    assert result["reason"] == "pr_head_mismatch"
    assert result["rerun_attempted"] is False
    assert mutation_calls == []


def test_retry_rejects_run_head_mismatch_before_mutation(monkeypatch):
    install_retry_snapshot(monkeypatch, retry_snapshot(99))
    monkeypatch.setattr(gh_pr_watch, "resolve_pr", lambda *_args, **_kwargs: sample_pr())
    monkeypatch.setattr(
        gh_pr_watch,
        "get_workflow_run",
        lambda *_args: workflow_run(head_sha="different"),
    )
    mutation_calls = []
    monkeypatch.setattr(
        gh_pr_watch,
        "gh_text",
        lambda *args, **kwargs: mutation_calls.append((args, kwargs)),
    )

    result = gh_pr_watch.retry_failed_now(retry_args(99))

    assert result["reason"] == "run_head_mismatch"
    assert result["rerun_attempted"] is False
    assert mutation_calls == []


def test_retry_rejects_stale_or_replaced_run_before_mutation(monkeypatch):
    install_retry_snapshot(monkeypatch, retry_snapshot(99))
    monkeypatch.setattr(gh_pr_watch, "resolve_pr", lambda *_args, **_kwargs: sample_pr())
    monkeypatch.setattr(
        gh_pr_watch,
        "get_workflow_run",
        lambda *_args: workflow_run(id=100),
    )
    mutation_calls = []
    monkeypatch.setattr(
        gh_pr_watch,
        "gh_text",
        lambda *args, **kwargs: mutation_calls.append((args, kwargs)),
    )

    result = gh_pr_watch.retry_failed_now(retry_args(99))

    assert result["reason"] == "run_id_mismatch"
    assert result["rerun_attempted"] is False
    assert mutation_calls == []


@pytest.mark.parametrize(
    ("pr_flags", "expected_reason"),
    [
        ({"closed": True}, "pr_closed_or_merged"),
        ({"merged": True}, "pr_closed_or_merged"),
    ],
)
def test_retry_rejects_closed_or_merged_pr(monkeypatch, pr_flags, expected_reason):
    install_retry_snapshot(monkeypatch, retry_snapshot(99))
    current_pr = sample_pr()
    current_pr.update(pr_flags)
    monkeypatch.setattr(gh_pr_watch, "resolve_pr", lambda *_args, **_kwargs: current_pr)
    mutation_calls = []
    monkeypatch.setattr(
        gh_pr_watch,
        "gh_text",
        lambda *args, **kwargs: mutation_calls.append((args, kwargs)),
    )

    result = gh_pr_watch.retry_failed_now(retry_args(99))

    assert result["reason"] == expected_reason
    assert result["rerun_attempted"] is False
    assert mutation_calls == []


@pytest.mark.parametrize(
    ("run_overrides", "expected_reason"),
    [
        ({"status": "in_progress", "conclusion": ""}, "run_not_terminal"),
        ({"status": "completed", "conclusion": "startup_failure"}, "run_startup_blocked"),
    ],
)
def test_retry_rejects_pending_or_startup_blocked_run(
    monkeypatch, run_overrides, expected_reason
):
    install_retry_snapshot(monkeypatch, retry_snapshot(99))
    monkeypatch.setattr(gh_pr_watch, "resolve_pr", lambda *_args, **_kwargs: sample_pr())
    run = workflow_run()
    run.update(run_overrides)
    monkeypatch.setattr(gh_pr_watch, "get_workflow_run", lambda *_args: run)
    mutation_calls = []
    monkeypatch.setattr(
        gh_pr_watch,
        "gh_text",
        lambda *args, **kwargs: mutation_calls.append((args, kwargs)),
    )

    result = gh_pr_watch.retry_failed_now(retry_args(99))

    assert result["reason"] == expected_reason
    assert result["rerun_attempted"] is False
    assert mutation_calls == []


def test_retry_performs_exactly_one_rerun_and_reports_new_attempt_identity(monkeypatch):
    install_retry_snapshot(monkeypatch, retry_snapshot(99))
    monkeypatch.setattr(gh_pr_watch, "resolve_pr", lambda *_args, **_kwargs: sample_pr())
    run_reads = iter(
        [
            workflow_run(),
            workflow_run(status="in_progress", conclusion=None, run_attempt=2),
        ]
    )
    monkeypatch.setattr(gh_pr_watch, "get_workflow_run", lambda *_args: next(run_reads))
    mutation_calls = []
    monkeypatch.setattr(
        gh_pr_watch,
        "gh_text",
        lambda *args, **kwargs: mutation_calls.append((args, kwargs)) or "",
    )

    result = gh_pr_watch.retry_failed_now(retry_args(99))

    assert result["reason"] == "rerun_triggered"
    assert result["rerun_attempted"] is True
    assert result["rerun_count"] == 1
    assert result["rerun_run_ids"] == [99]
    assert len(mutation_calls) == 1
    assert mutation_calls[0][0] == (["run", "rerun", "99", "--failed"],)
    assert result["attempts"][0]["new_attempt"] is True
    assert result["attempts"][0]["attempt_identity"] == "99:2"
    assert result["action_fingerprint"]["binding"]["selected_failed_run_ids"] == ["99"]


def test_retry_rejects_run_without_exact_pr_association(monkeypatch):
    install_retry_snapshot(monkeypatch, retry_snapshot(99))
    monkeypatch.setattr(gh_pr_watch, "resolve_pr", lambda *_args, **_kwargs: sample_pr())
    monkeypatch.setattr(
        gh_pr_watch,
        "get_workflow_run",
        lambda *_args: workflow_run(pull_requests=[]),
    )
    mutation_calls = []
    monkeypatch.setattr(
        gh_pr_watch,
        "gh_text",
        lambda *args, **kwargs: mutation_calls.append((args, kwargs)),
    )

    result = gh_pr_watch.retry_failed_now(retry_args(99))

    assert result["reason"] == "run_pr_association_missing"
    assert result["rerun_attempted"] is False
    assert mutation_calls == []


def test_retry_does_not_accept_stale_post_rerun_readback(monkeypatch):
    install_retry_snapshot(monkeypatch, retry_snapshot(99))
    monkeypatch.setattr(gh_pr_watch, "resolve_pr", lambda *_args, **_kwargs: sample_pr())
    monkeypatch.setattr(
        gh_pr_watch,
        "get_workflow_run",
        lambda *_args: workflow_run(run_attempt=1),
    )
    mutation_calls = []
    monkeypatch.setattr(
        gh_pr_watch,
        "gh_text",
        lambda *args, **kwargs: mutation_calls.append((args, kwargs)) or "",
    )

    result = gh_pr_watch.retry_failed_now(retry_args(99))

    assert result["reason"] == "post_rerun_readback_inconclusive"
    assert result["rerun_attempted"] is True
    assert result["rerun_count"] == 1
    assert len(mutation_calls) == 1
    assert result["attempts"][0]["attempt_identity_observable"] is True
    assert result["attempts"][0]["new_attempt"] is False


def test_retry_stops_on_ambiguous_command_without_second_mutation(monkeypatch):
    snapshot = retry_snapshot(99, 100)
    snapshot["retry_state"]["max_flaky_retries"] = 1
    install_retry_snapshot(monkeypatch, snapshot)
    state = {"retries_by_sha": {}}
    monkeypatch.setattr(gh_pr_watch, "load_state", lambda _path: (state, False))
    monkeypatch.setattr(gh_pr_watch, "resolve_pr", lambda *_args, **_kwargs: sample_pr())
    monkeypatch.setattr(
        gh_pr_watch,
        "get_workflow_run",
        lambda _repo, run_id: workflow_run(id=int(run_id)),
    )
    mutation_calls = []

    def ambiguous_command(*args, **kwargs):
        mutation_calls.append((args, kwargs))
        raise gh_pr_watch.GhCommandError("provider response was ambiguous")

    monkeypatch.setattr(gh_pr_watch, "gh_text", ambiguous_command)

    result = gh_pr_watch.retry_failed_now(
        retry_args(99, 100, max_flaky_retries=1)
    )

    assert result["reason"] == "rerun_command_ambiguous"
    assert result["rerun_attempted"] is False
    assert result["rerun_count"] == 0
    assert len(mutation_calls) == 1
    assert state["retries_by_sha"]["abc123"] == 1

    snapshot["retry_state"]["current_sha_retries_used"] = 1
    second = gh_pr_watch.retry_failed_now(
        retry_args(99, 100, max_flaky_retries=1)
    )

    assert second["reason"] == "retry_budget_exhausted"
    assert len(mutation_calls) == 1
