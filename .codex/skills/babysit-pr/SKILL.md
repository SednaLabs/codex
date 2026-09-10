---
name: babysit-pr
description: Watch an open GitHub pull request from the current branch, a PR number, or a PR URL; monitor CI, reviewer feedback, and mergeability; fix valid branch-caused issues, rerun likely flaky failures up to 3 times, and keep monitoring until the PR is merged, ready to merge, or blocked on user help. For blocking babysitter waits, use `--watch-until-action`; use `--watch` only for active foreground JSONL consumption.
---

# PR Babysitter

## Objective
Babysit a PR persistently until one of these terminal outcomes occurs:

- The PR is merged or closed.
- CI is successful, there are no unaddressed review comments surfaced by the watcher, required review approval is not blocking merge, and there are no potential merge conflicts (PR is mergeable / not reporting conflict risk).
- A situation requires user help (for example CI infrastructure issues, repeated flaky failures after retry budget is exhausted, permission problems, or ambiguity that cannot be resolved safely).

Do not stop merely because a single snapshot returns `idle` while checks are still pending.

## Protected queue coordinator contract

When this skill is used as the read-only coordinator for a protected queue, one
coordinator owns the queue view and its handoff record. The observer's cutline
is one exact queue-entry snapshot; it does not claim queue throughput, merge
authority, or a ready horizon. Keep independent entries moving when another
entry is `UNMERGEABLE`: isolate that state to the PR owner, record its exact
blocker, and leave the other entries eligible for their own owner-controlled
progress.

Each entry must carry one exact identity record before it is watched:
repository and PR number, PR owner, head SHA, base ref and base SHA, an
explicit queue-entry reference and synthetic candidate SHA/source, static
ancestry evidence proving that the synthetic candidate contains the exact PR
head and current base, the complete workflow run/attempt set, and the active
protection ruleset IDs, conditions, and revision. A queue ID is not a queue
ref; neither is a PR head a synthetic `G`. A display branch, a green check, or
an owner name without these bindings is not an entry identity.

Stage identity is kept separate across the delivery path:

| Stage | Required identity |
| --- | --- |
| `validation-ref` | Exact repository/ref and workflow input, with the full candidate SHA and its run/`G` identity; a ref name alone is only a selector. |
| `pull_request` | Repository/PR, PR owner, full head SHA, base ref/SHA, workflow/run/`G`, and the `pull_request` event. |
| `merge_group` | Queue entry/ref, synthetic candidate SHA, selected workflow/run/`G`, and ancestry proving the exact PR head and current base are included. |
| `post-main` | Resulting merge SHA on `main`, selected post-main workflow/run/`G`, and the `push` event; this is fresh evidence after landing, not a reused PR or queue result. |

Workflow/run aliases are normalized as one identity record: conflicting aliases
or duplicate run IDs invalidate the complete set, and workflow reads must consume
all provider pages before conclusions are drawn. A queue entry reference must be
present as an authoritative field and must be distinct from the provider queue ID.

`ALLGREEN` is a scoped queue-readiness label only. It is emitted only when the
exact synthetic `merge_group` candidate has exactly one terminal-successful
`merge_group` run for each of `CI required` and `CodeQL required`, with matching
full SHA and positive attempt, no unrelated/empty runs, static ancestry proof,
and applicable ruleset identity. It never authorizes a merge or substitutes
for fresh post-main proof when landing applies.

Use the bundled `gh_pr_watch.py` in one helper-owned blocking mode
(`--watch-until-action` for an owner that may need to act, or
`--watch-until-terminal` for a delegated check wait) only for a separately
owned PR-local wait. The queue observer is strictly one-shot and never invokes
that helper: its receipt schema is not compatible with the PR watcher receipt,
and it cannot accept or validate invented provenance fields. The helper covers
PR-local checks/reviews only; it is not a merge-queue, ruleset, or synthetic
candidate event watcher. Do not recreate any cadence with repeated `--once`
calls, raw `gh`/API polling, or a second watcher. After any separately-owned
helper wake, rehydrate the queue, ruleset, head, and run surfaces and bind a
new wait before relying on the result.

Stop the affected wait and rebind before any further conclusion after a new
head SHA, base SHA/ref, validation ref, queue entry/candidate or queue head,
run/`G`, owner, required workflow, or ruleset/protection revision appears. A
changed or removed queue candidate, a stale run, or an `UNMERGEABLE` result
invalidates that entry's prior evidence; it does not invalidate independent
entries. Never borrow an older exact-SHA result for a successor candidate.

The coordinator is an observer and handoff surface, not a mutation authority.
It must not bypass protection, direct merge, rebase/update branches, resolve
review threads, cancel or rerun unrelated work, alter queue/ruleset settings,
or perform raw polling against a provider. Report the exact owner action and
evidence needed instead. Existing PR babysitter repair, review, retry, and landing
semantics below remain in force for an explicitly authorized owner; observing
an entry or emitting `ALLGREEN` grants none of that authority.

## Operating Model

### Merge-queue observer boundary

The bundled `gh_merge_queue_shepherd.py` is read-only and one-shot. It does
not enqueue, dequeue, merge, rerun, or alter rulesets. A snapshot with missing
queue ref, synthetic source, ancestry, required workflow evidence, ruleset
conditions/revision, or recognized queue state is deliberately unbound.
Ancestry is accepted only from the allowlisted `hosted-static-ancestry-v1`
schema; a caller-provided or `fabricated` source is never evidence. The
GraphQL adapter exposes only fields returned authoritatively by the provider;
it does not derive a queue ref from an entry ID or a synthetic SHA from a raw
head field. Its supported query currently returns the entry ID, position,
state, base commit, synthetic head commit, and pull-request identity. It does
not return `queueEntryRef`, queue attempt, or `ancestryEvidence`; those fields
remain unbound and are reported as an explicit external hosted prerequisite.
When the provider cannot return the required structural evidence, the runbook
outcome is `identity_mismatch_rebind_required` and a hosted follow-up must
obtain that evidence before claiming `ALLGREEN`.

The observer does not implement a delegated wait. PR-local helper output is
kept on its own owner-controlled surface and is never treated as a queue
identity receipt. This prevents the bundled `gh_pr_watch.py` receipt (which
lacks queue-observer helper/version/mode, run-set, required-conclusion,
thread-state, and fingerprint fields) from being accepted as if it supplied
queue evidence.

- Use `--watch-until-terminal` for delegated wait seams when current-head checks must finish before handoff. Use `--watch-until-action` for a repair owner that should wake on review feedback or a failure that can be acted on immediately.
- Use `--watch` only when the lane is actively consuming the live JSONL stream in the foreground.
- Use `--once` for one-shot diagnosis or local debugging, not for a full babysitting handoff.
- Blocking waits emit one final receipt and no per-poll progress by default. Use `--progress` only for deliberate interactive debugging; never enable it for a delegated wait whose output will be sent back to a model.
- Blocking waits return a compact action-complete receipt by default. Use `--verbose-details` only for debugging when the compact receipt and saved state file are insufficient.
- After any fix commit or flaky rerun, restart the same monitoring mode immediately and keep exactly one watcher session active for the PR.

The source watcher records a compact `watch_decision` and a `watch_schedule`
receipt keyed by the exact repository, PR number, and observed head SHA. An
unexplained `mergeStateStatus=BLOCKED` is reported as the actionable
`action_required_merge_policy_blocked` outcome. Readiness remains fail-closed,
and this blocker keeps the next wake bounded at the configured poll interval;
it must not enter green-state backoff. These receipts are observer state only:
they do not authorize merge, rerun, review, credential, or other provider
mutation.

An active merge-queue entry in `QUEUED` or `AWAITING_CHECKS` likewise keeps the
watcher on the configured base cadence, including when checks are green. A
missing or unreadable pending queue head is not readiness evidence and remains
on that base cadence. Queue-entry identity changes are part of snapshot change
detection, so re-enqueue, replacement, or removal cannot silently retain a
green-state backoff. Ordinary green PRs with no active queue entry retain the
existing bounded backoff.

## Inputs
Accept any of the following:

- No PR argument: infer the PR from the current branch (`--pr auto`)
- PR number together with explicit `--repo OWNER/REPO`
- PR URL

Bare PR numbers without `--repo` are rejected because repository-context inference can silently select an unrelated PR with the same number.

## Core Workflow

1. When the user asks to "monitor"/"watch"/"babysit" a PR, invoke one blocking watcher. Prefer `--watch-until-terminal` for a delegated check wait and `--watch-until-action` for a repair owner that should wake on actionable review or CI state. Use the continuous stream (`--watch`) only for deliberate foreground debugging.
2. Run the watcher script to snapshot PR/CI/review state (or consume each streamed snapshot from `--watch` / the final result from `--watch-until-action`).
3. Inspect the `actions` list in the JSON response.
4. If `diagnose_ci_failure` is present, inspect failed run logs and classify the failure.
5. If the failure is likely caused by the current branch, patch code locally, commit, and push. Do not patch random flaky tests, CI infrastructure, dependency outages, runner issues, or other failures that are unrelated to the branch.
6. If `process_review_comment` is present, inspect surfaced published review items and decide whether to address them.
7. If a review item is actionable and correct, patch code locally, commit, push, and then resolve the associated review thread only when allowed by the GitHub state mutation policy below.
8. Every Gemini Code Assist thread on a PR touched by the lane must receive a substantive, evidence-backed threaded reply and be resolved before the lane can be called clean, ready, or complete. This includes outdated threads. If a thread is genuinely blocked, leave it open with an explicit blocker and do not report terminal readiness. Re-read the live thread ledger at the final exact head.
9. Do not post replies to human-authored review comments/threads unless the user explicitly confirms the exact response. If a human review item is non-actionable, already addressed, or not valid, surface the item and recommended response to the user instead of replying on GitHub.
10. If the failure is likely flaky/unrelated and `retry_failed_checks` is present, rerun failed jobs with `--retry-failed-now`.
11. If both actionable review feedback and `retry_failed_checks` are present, prioritize review feedback first; a new commit will retrigger CI, so avoid rerunning flaky checks on the old SHA unless you intentionally defer the review change.
12. On every loop, look for newly surfaced review feedback before acting on CI failures or mergeability state, then verify mergeability / merge-conflict status (for example via `gh pr view`) alongside CI.
13. After any push or rerun action, immediately return to step 1 and continue polling on the updated SHA/state.
14. If you had been using `--watch` or `--watch-until-action` before pausing to patch/commit/push, relaunch that same monitoring mode yourself in the same turn immediately after the push (do not wait for the user to re-invoke the skill).
14. Repeat polling until the watcher reaches a terminal stop condition such as `stop_ready_to_merge`, `stop_pr_closed`, or a user-help-required blocker. A green + review-clean + mergeable PR is only a stopping point when the chosen watcher mode treats it as actionable.
15. Maintain terminal/session ownership: while babysitting is active, keep consuming watcher output in the same turn; do not leave a detached watcher process running and then end the turn as if monitoring were complete. When the lane is using a blocking terminal wait, prefer `--watch-until-action` so the process exits on actionable or terminal state instead of streaming forever.

## Commands

### One-shot snapshot

```bash
python3 .codex/skills/babysit-pr/scripts/gh_pr_watch.py --pr auto --once
```

### Actionable wait for Codex babysitter lanes

```bash
python3 .codex/skills/babysit-pr/scripts/gh_pr_watch.py --pr auto --watch-until-action
```

### Terminal wait for delegated CI lanes

```bash
python3 .codex/skills/babysit-pr/scripts/gh_pr_watch.py --pr auto --watch-until-terminal
```

### Ignore a known review thread while watching

```bash
python3 .codex/skills/babysit-pr/scripts/gh_pr_watch.py --pr <number-or-url> --repo <owner/repo> --watch-until-action --ignore-review-thread <thread-url-or-id>
```

### Continuous watch (foreground JSONL)

```bash
python3 .codex/skills/babysit-pr/scripts/gh_pr_watch.py --pr auto --watch
```

### Trigger flaky retry cycle (only when watcher indicates)

```bash
python3 .codex/skills/babysit-pr/scripts/gh_pr_watch.py \
  --pr <pr-url> --retry-failed-now --expected-head-sha <headRefOid> \
  --run-id <failed-run-id>
```

Retry mode requires the exact caller-observed PR head SHA. Repeat `--run-id` to
select a bounded set of failed workflow runs; when omitted, the current
snapshot's failed run IDs are bound into the receipt. Immediately before any
rerun, the helper re-reads the PR and every selected run and fails closed on a
head, lifecycle, run identity, terminal-state, or rerunnable-conclusion
mismatch. Each admitted run is mutated at most once. A successful rerun is
followed by an authoritative run readback that reports `run_attempt` and the
derived attempt identity when GitHub exposes it; the receipt is inconclusive
unless the attempt is strictly newer than the pre-mutation attempt. The run's
authoritative `pull_requests` association must include the selected PR. An
ambiguous provider command failure stops without a second mutation. One retry
cycle is durably reserved immediately after preflight and before the first
provider mutation, so a partial batch cannot silently reset its retry budget.

### Explicit PR target

```bash
python3 .codex/skills/babysit-pr/scripts/gh_pr_watch.py --pr <number-or-url> --once
```

### GitHub App installation observer

When a separately governed broker has supplied a short-lived GitHub App
installation token, opt into the read-only observer path explicitly:

```bash
python3 .codex/skills/babysit-pr/scripts/gh_pr_watch.py \
  --pr <number-or-url> --installation-observer --once
```

This mode does not call `gh api user` because installation tokens have no
`/user` endpoint. Review filtering remains conservative when identity is
unbound: `OWNER`, `MEMBER`, `COLLABORATOR`, and approved Codex bots are
actionable, while untrusted/`NONE` authors remain ignored. It does not mint,
print, or persist tokens and does not broaden the watcher's read-only scope.

## CI Failure Classification
Use `gh` commands to inspect failed runs before deciding to rerun.

- `gh run view <run-id> --json jobs,name,workflowName,conclusion,status,url,headSha`
- `gh api repos/<owner>/<repo>/actions/runs/<run-id>/jobs -X GET -f per_page=100`
- `gh api repos/<owner>/<repo>/actions/jobs/<job-id>/logs > /tmp/codex-gh-job-<job-id>-logs.zip`
- `gh run view <run-id> --log-failed` as a fallback after the overall workflow run is complete

`gh run view --log-failed` is workflow-run scoped and may not expose failed-job logs until the overall run finishes. For faster diagnosis, poll the run's jobs first and, as soon as a specific job has failed, fetch that job's logs directly from the Actions job logs endpoint. The watcher includes a `failed_jobs` list with each failed job's `job_id` and `logs_endpoint` when GitHub exposes one.

Prefer treating failures as branch-related when failed-job logs point to changed code (compile/test/lint/typecheck/snapshots/static analysis in touched areas).

Prefer treating failures as flaky/unrelated when logs show transient infra/external issues (timeouts, runner provisioning failures, registry/network outages, GitHub Actions infra errors).

Do not attempt to fix flaky/unrelated failures by changing tests, build scripts, CI configuration, dependency pins, or infrastructure-adjacent code unless the logs clearly connect the failure to the PR branch. For flaky/unrelated failures, rerun only when the watcher recommends `retry_failed_checks`; otherwise wait or stop for user help.

If classification is ambiguous, perform one manual diagnosis attempt before choosing rerun.

Read `.codex/skills/babysit-pr/references/heuristics.md` for a concise checklist.

## Review Comment Handling
The watcher surfaces review items from:

- PR issue comments
- Inline review comments
- Review submissions (COMMENT / APPROVED / CHANGES_REQUESTED)

Only act on published feedback. Ignore review submissions in GitHub's `PENDING` state and inline
comments attached to those pending reviews. Do not mark pending review feedback as seen; it should
be eligible to surface after the reviewer submits the review.

It intentionally surfaces Codex reviewer bot feedback (for example comments/reviews from `chatgpt-codex-connector[bot]`) in addition to human reviewer feedback. Most unrelated bot noise should still be ignored.
For safety, the watcher only auto-surfaces trusted human review authors (for example repo OWNER/MEMBER/COLLABORATOR, plus the authenticated operator) and approved review bots such as Codex.
On a fresh watcher state file, existing unaddressed published review feedback may be surfaced immediately (not only comments that arrive after monitoring starts). This is intentional so already-open review comments are not missed.

When you agree with a comment and it is actionable:

1. Patch code locally.
2. Commit with `codex: address PR review feedback (#<n>)`.
3. Push to the PR head branch.
4. After the push succeeds, resolve the associated GitHub review thread only when allowed by the GitHub state mutation policy below.
5. Resume watching on the new SHA immediately (do not stop after reporting the push).
6. If monitoring was running in `--watch` mode, restart `--watch` immediately after the push in the same turn; do not wait for the user to ask again.

Do not post replies to human-authored GitHub review comments/threads automatically. If you disagree with a human comment, believe it is non-actionable/already addressed, or need to answer a question, report the item to the user with a suggested response and wait for explicit confirmation before posting anything on GitHub. If the user approves a response, prefix it with `[codex]` so it is clear the response is automated and not from the human user.
If the watcher later surfaces your own approved reply because the authenticated operator is treated as a trusted review author, treat that self-authored item as already handled and do not reply again.
If a code review comment/thread is already marked as resolved in GitHub, treat it as non-actionable and safely ignore it unless new unresolved follow-up feedback appears.
`--watch-until-action` should verify the live unresolved-thread state before stopping on review feedback: open unresolved review threads remain actionable, but stale already-resolved review history should not trigger `action_required`.
If the operator knows a particular unresolved thread should be ignored for this babysitting run, pass `--ignore-review-thread <thread-url-or-id>` and keep that ignore list stable across restarts of the same watcher lane.

## GitHub State Mutation Policy

You can read any PR state you need for monitoring. Writes must comply with this policy.

You can push PRs to update the code under review or to force CI re-runs as described above.

You can resolve review comment threads from the human who requested babysitting or from the Codex
review bot. When resolving, leave a comment prefixed with `[from Codex]: ` and explain what changes
you made and which commit includes them. Don't touch review threads if other humans other than the
user who requested babysitting have participated.

Before making any changes, fetch the PR state yourself instead of relying on the PR watcher script's
output.

Unless explicitly asked, do not:

* comment on other humans' review threads, communicate with the user in chat instead
* resolve review threads from humans other than the user
* interact with humans other than the user
* mark PRs as drafts or ready for review
* close or reopen PRs

In general, never act on GitHub in ways that would make it hard to tell whether you or the user did
something visible to other humans. When in doubt, ask the user for clarification in chat.

## Git Safety Rules

- Work only on the PR head branch.
- Avoid destructive git commands.
- Do not switch branches unless necessary to recover context.
- Before editing, check for unrelated uncommitted changes. If present, stop and ask the user.
- After each successful fix, commit and `git push`, then re-run the watcher.
- If you interrupted a live watcher session to make the fix, restart the same watcher mode immediately after the push in the same turn.
- Do not run multiple concurrent `--watch` processes for the same PR/state file; keep one watcher session active and reuse it until it stops or you intentionally restart it.
- A push is not a terminal outcome; continue the monitoring loop unless a strict stop condition is met.

Commit message defaults:

- `codex: fix CI failure on PR #<n>`
- `codex: address PR review feedback (#<n>)`

## Monitoring Loop Pattern
Use this loop in a live Codex session:

1. Run `--once`.
2. Read `actions`.
3. First check whether the PR is now merged or otherwise closed; if so, report that terminal state and stop polling immediately.
4. Check CI summary, new review items, and mergeability/conflict status.
5. Diagnose CI failures and classify branch-related vs flaky/unrelated. If the overall run is still pending but `failed_jobs` already includes a failed job, fetch that job's logs and diagnose immediately instead of waiting for the whole workflow run to finish. Patch only when the failure is branch-related.
6. For each surfaced review item from another author, patch/commit/push if it is actionable, then resolve it only when allowed by the GitHub state mutation policy above. If it is non-actionable, already addressed, or requires a written answer, surface it to the user with a suggested response instead of posting automatically. If a later snapshot surfaces your own approved reply, treat it as informational and continue without responding again.
7. Process actionable review comments before flaky reruns when both are present; if a review fix requires a commit, push it and skip rerunning failed checks on the old SHA.
8. Retry failed checks only when `retry_failed_checks` is present and you are not about to replace the current SHA with a review/CI fix commit. Do not make code changes for unrelated flakes or infrastructure failures just to get CI green.
9. If you pushed a commit, resolved an eligible review thread, or triggered a rerun, report the action briefly and continue polling (do not stop). If a human review comment needs a written GitHub response, stop and ask for confirmation before posting.
10. After a review-fix push, proactively restart continuous monitoring (`--watch`) in the same turn unless a strict stop condition has already been reached.
11. If everything is passing, mergeable, not blocked on required review approval, and there are no unaddressed review items, report that the PR is currently ready to merge but keep the watcher running so new review comments are surfaced quickly while the PR remains open.
12. If blocked on a user-help-required issue (infra outage, exhausted flaky retries, unclear reviewer request, permissions), report the blocker and stop.
13. Otherwise sleep according to the polling cadence below and repeat.

When the user explicitly asks to monitor/watch/babysit a PR, select one blocking helper mode and let it own its internal GitHub cadence. Repeated `--once` snapshots are only for debugging, local testing, or an explicitly requested one-shot check.
Do not stop to ask the user whether to continue polling; continue autonomously until a strict stop condition is met or the user explicitly interrupts.
Do not hand control back to the user after a review-fix push just because a new SHA was created; restarting the watcher and re-entering the poll loop is part of the same babysitting task.
If a foreground `--watch` process is still running and no strict stop condition has been reached, the babysitting task is still in progress; keep streaming/consuming watcher output instead of ending the turn. If the lane is blocked on a terminal wait, use `--watch-until-action` so the watcher exits when there is a strict stop or actionable work to surface.

## Polling Cadence
Use adaptive polling and continue monitoring even after CI turns green:

- While CI is not green (pending/running/queued or failing): poll every 1 minute.
- After CI turns green: start at every 1 minute, then back off exponentially when there is no change. For foreground `--watch`, cap at every 1 hour. For `--watch-until-action`, cap at every 20 minutes so new review/check regressions surface in bounded time without chatty polling.
- Reset the green-state polling interval back to 1 minute whenever anything changes (new commit/SHA, check status changes, new review comments, mergeability changes, review decision changes).
- If CI stops being green again (new commit, rerun, or regression): return to 1-minute polling.
- If any poll shows the PR is merged or otherwise closed: stop polling immediately and report the terminal state.

## Stop Conditions (Strict)
Stop only when one of the following is true:

- PR merged or closed (stop as soon as a poll/snapshot confirms this).
- PR is ready to merge: CI succeeded, no surfaced unaddressed review comments, not blocked on required review approval, and no merge conflict risk.
- User intervention is required and Codex cannot safely proceed alone.

Keep polling when:

- `actions` contains only `idle` but checks are still pending.
- CI is still running/queued.
- Review state is quiet but CI is not terminal.
- CI is green but mergeability is unknown/pending.
- CI is green and mergeable, but the PR is still open and you are waiting for possible new review comments or merge-conflict changes per the green-state cadence.
- The PR is green but blocked on review approval (`REVIEW_REQUIRED` / similar); continue polling on the green-state cadence and surface any new review comments without asking for confirmation to keep watching.

## Output Expectations
Blocking waits produce no periodic model-visible heartbeat. The helper returns one final receipt; `--progress` is an explicit debugging-only exception. The final summary includes:

- Treat push confirmations, intermediate CI snapshots, and review-action updates as progress updates only; do not emit the final summary or end the babysitting session unless a strict stop condition is met.
- A user request to "monitor" is not satisfied by a couple of sample polls; remain in the loop until a strict stop condition or an explicit user interruption.
- A review-fix commit + push is not a completion event; immediately resume monitoring in the same turn and continue reporting progress updates.
- Do not send the final summary while a watcher terminal is still running unless the watcher has emitted/confirmed a strict stop condition; otherwise continue with progress updates. For blocking waits, prefer `--watch-until-action` so the process itself returns when a meaningful state change occurs.

- Final PR SHA
- CI status summary
- Mergeability / conflict status
- Fixes pushed
- Flaky retry cycles used
- Remaining unresolved failures or review comments

## References

- Heuristics and decision tree: `.codex/skills/babysit-pr/references/heuristics.md`
- GitHub CLI/API details used by the watcher: `.codex/skills/babysit-pr/references/github-api-notes.md`
