#!/usr/bin/env python3
"""Hosted contract fixtures for guarded history-rewrite publication."""
from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import publication
from publication import PublicationError, digest


def protection_snapshot() -> dict:
    rules = []
    for ref in publication.PROTECTED_REFS:
        branch = ref.removeprefix("refs/heads/")
        rules.append({
            "id": publication.PROTECTION_RULE_IDS[branch],
            "pattern": branch,
            "allows_force_pushes": False,
            "is_admin_enforced": branch != "upstream-main",
            "requires_status_checks": False,
            "required_status_check_contexts": [],
            "required_status_checks": [],
            "requires_strict_status_checks": True,
            "requires_approving_reviews": branch != "upstream-main",
            "required_approving_review_count": 0,
            "requires_conversation_resolution": True,
            "restricts_pushes": True,
            "requires_commit_signatures": False, "requires_linear_history": False,
            "lock_branch": False, "requires_deployments": False,
            "required_deployment_environments": [], "require_last_push_approval": False,
            "requires_code_owner_reviews": False, "allows_deletions": False, "blocks_creations": False,
            "bypass_force_push_allowances": [copy.deepcopy(publication.PUBLISHER_ACTOR)],
            "bypass_pull_request_allowances": [copy.deepcopy(publication.PUBLISHER_ACTOR)] if branch != "upstream-main" else [],
            "push_allowances": [copy.deepcopy(publication.PUBLISHER_ACTOR)],
        })
    rules.sort(key=lambda item: (item["pattern"], item["id"]))
    return {
        "schema": "history-rewrite-protection-snapshot-v2",
        "protected_refs": list(publication.PROTECTED_REFS),
        "branch_protection_rules": rules,
        "repository_rulesets": [{
            "id": publication.QUEUE_ONLY_RULESET_ID,
            "name": publication.QUEUE_ONLY_RULESET_NAME,
            "target": "branch",
            "enforcement": "active",
            "bypass_actors_visibility": "not_returned",
            "bypass_actors": None,
            "conditions": {"ref_name": {"exclude": [], "include": ["refs/heads/main"]}},
            "rules": [{"type": "merge_queue", "parameters": {
                "merge_method": "SQUASH",
                "max_entries_to_build": 4,
                "min_entries_to_merge": 1,
                "max_entries_to_merge": 1,
                "min_entries_to_merge_wait_minutes": 0,
                "grouping_strategy": "ALLGREEN",
                "check_response_timeout_minutes": 180,
            }}],
        }],
    }


def before_snapshot(*, visible: bool) -> dict:
    snapshot = protection_snapshot()
    for rule in snapshot["branch_protection_rules"]:
        rule.update(allows_force_pushes=False, restricts_pushes=False,
                    bypass_force_push_allowances=[], bypass_pull_request_allowances=[], push_allowances=[])
        rule["requires_strict_status_checks"] = rule["pattern"] != "main"
        if rule["pattern"] != "upstream-main":
            rule.update(requires_status_checks=True, required_status_check_contexts=["CI required", "CodeQL required gate"],
                        required_status_checks=[{"context": context, "app": {"databaseId": 15368, "slug": "github-actions"}}
                                                for context in ("CI required", "CodeQL required gate")])
    if visible:
        snapshot["repository_rulesets"][0].update(bypass_actors_visibility="visible", bypass_actors=[])
    return snapshot


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def base_manifest(selected: dict[str, str] | None = None, output: dict[str, str] | None = None) -> dict:
    selected = selected or {"refs/heads/main": "1" * 40, "refs/tags/v1": "2" * 40}
    output = output or {"refs/heads/main": "3" * 40, "refs/tags/v1": "4" * 40}
    proofs = {name: hashlib.sha256(name.encode()).hexdigest() for name in publication.ARTIFACT_FILES}
    manifest = {
        "schema": "history-rewrite-publication-v1",
        "repository": publication.REPOSITORY,
        "harness_sha": "5" * 40,
        "harness_tree": "6" * 40,
        "source_sha": selected["refs/heads/main"],
        "tag_signature_ack": True,
        "selected_refs": selected,
        "output_refs": output,
        "selected_refs_sha256": digest(selected),
        "output_refs_sha256": digest(output),
        "policy": {"path": "scripts/history-rewrite/policy.json", "sha256": "7" * 64},
        "proof_digests": proofs,
        "proof_digests_sha256": digest(proofs),
        "backup": {"run_id": 41, "receipt_artifact_id": 410, "receipt_artifact_api_digest": "8" * 64},
        "proof": {"run_id": 42, "artifact_id": 420, "artifact_api_digest": "9" * 64},
        "controls": {
            "publication_branch": publication.PUBLICATION_BRANCH,
            "environment": publication.ENVIRONMENT_NAME,
            "reviewer_id": publication.REVIEWER_ID,
            "reviewer_login": publication.REVIEWER_LOGIN,
            "writer_workflows": publication.WRITER_WORKFLOWS,
            "mirror_workflow_id": publication.MIRROR_WORKFLOW_ID,
            "protected_refs": list(publication.PROTECTED_REFS),
            "protection_snapshot": protection_snapshot(),
        },
    }
    manifest["controls"]["protection_snapshot_sha256"] = digest(manifest["controls"]["protection_snapshot"])
    plan = publication.plan_maintenance(before_snapshot(visible=True), before_snapshot(visible=False))
    if plan["read_token_after"] != manifest["controls"]["protection_snapshot"]:
        raise SystemExit("maintenance planner does not match the independently specified expected after-state")
    manifest["controls"]["maintenance_plan"] = plan
    manifest["controls"]["maintenance_plan_sha256"] = digest(plan)
    return manifest


def environment_fixture() -> tuple[dict, dict]:
    environment = {
        "name": publication.ENVIRONMENT_NAME,
        "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True},
        "protection_rules": [
            {
                "id": publication.REVIEW_RULE_ID,
                "type": "required_reviewers",
                "prevent_self_review": False,
                "reviewers": [{"type": "User", "reviewer": {"id": publication.REVIEWER_ID, "login": publication.REVIEWER_LOGIN}}],
            },
            {"id": publication.BRANCH_RULE_PROTECTION_ID, "type": "branch_policy"},
        ],
    }
    policies = {"total_count": 1, "branch_policies": [{"id": publication.BRANCH_POLICY_ID, "name": publication.PUBLICATION_BRANCH, "type": "branch"}]}
    return environment, policies


def approval_fixture() -> list[dict]:
    return [{
        "state": "approved",
        "environments": [{"name": publication.ENVIRONMENT_NAME}],
        "user": {"id": publication.REVIEWER_ID, "login": publication.REVIEWER_LOGIN},
    }]


def expect_failure(name: str, function) -> str:
    try:
        function()
    except PublicationError as exc:
        return f"{name}:{hashlib.sha256(str(exc).encode()).hexdigest()}"
    raise SystemExit(f"negative publication fixture unexpectedly passed: {name}")


def observer_fixture_documents() -> dict:
    return {
        "observer_viewer": {"data": {"viewer": {"login": f"{publication.OBSERVER_APP_SLUG}[bot]"}}},
        "observer_app": {"id": publication.OBSERVER_APP_ID, "node_id": publication.OBSERVER_APP_NODE_ID,
                         "slug": publication.OBSERVER_APP_SLUG, "permissions": dict(publication.OBSERVER_APP_GRANTS)},
        "observer_repositories": {"total_count": 1, "repositories": [{"id": publication.REPOSITORY_ID, "full_name": publication.REPOSITORY}]},
    }


def publisher_fixture_documents() -> dict:
    return {
        "publisher_viewer": {"data": {"viewer": {"login": f"{publication.PUBLISHER_APP_SLUG}[bot]"}}},
        "publisher_app": {"id": publication.PUBLISHER_APP_ID, "node_id": publication.PUBLISHER_APP_NODE_ID,
                          "slug": publication.PUBLISHER_APP_SLUG, "permissions": dict(publication.PUBLISHER_PERMISSIONS)},
        "publisher_repositories": {"total_count": 1, "repositories": [{"id": publication.REPOSITORY_ID, "full_name": publication.REPOSITORY}]},
    }


class MockApi:
    def __init__(self, states: dict[int, str], runs: dict[int, list[dict]] | None = None, protection: dict | None = None):
        self.states = dict(states)
        self.runs = runs or {}
        self.protection = protection or protection_snapshot()
        self.mutations: list[tuple[int, str]] = []
        self.observer = observer_fixture_documents()
        self.publisher = publisher_fixture_documents()
        self.principal = "observer"

    def get(self, path: str) -> object:
        if path == publication.OBSERVER_REPOSITORIES_PATH:
            return getattr(self, self.principal)[f"{self.principal}_repositories"]
        if path == f"/apps/{publication.OBSERVER_APP_SLUG}":
            return self.observer["observer_app"]
        if path == "/installation":
            raise SystemExit("fixture must not fabricate a bare installation endpoint")
        if path == f"/apps/{publication.PUBLISHER_APP_SLUG}":
            return self.publisher["publisher_app"]
        if path.startswith(f"/repos/{publication.REPOSITORY}/rulesets?"):
            return [{"id": item["id"]} for item in self.protection["repository_rulesets"]]
        if path.startswith(f"/repos/{publication.REPOSITORY}/rulesets/"):
            ruleset_id = int(path.rsplit("/", 1)[1])
            ruleset = next(item for item in self.protection["repository_rulesets"] if item["id"] == ruleset_id)
            document = {key: ruleset[key] for key in ("id", "name", "target", "enforcement", "conditions", "rules")}
            if ruleset["bypass_actors_visibility"] == "visible":
                document["bypass_actors"] = ruleset["bypass_actors"]
            return document
        parts = path.split("?")[0].split("/")
        workflow_id = int(parts[6])
        if parts[-1] == "runs":
            status = path.split("status=", 1)[1].split("&", 1)[0]
            runs = [run for run in self.runs.get(workflow_id, []) if run.get("status") == status]
            return {"total_count": len(runs), "workflow_runs": runs}
        paths = {**{value: key for key, value in publication.WRITER_WORKFLOWS.items()}, publication.MIRROR_WORKFLOW_ID: ".github/workflows/sedna-sync-upstream.yml"}
        return {"id": workflow_id, "path": paths[workflow_id], "state": self.states[workflow_id]}

    def put(self, path: str) -> None:
        parts = path.split("/")
        workflow_id, action = int(parts[6]), parts[7]
        self.states[workflow_id] = "active" if action == "enable" else "disabled_manually"
        self.mutations.append((workflow_id, action))

    def post_graphql(self, query: str, variables: dict[str, str]) -> object:
        if query == publication.OBSERVER_VIEWER_QUERY:
            return getattr(self, self.principal)[f"{self.principal}_viewer"]
        nodes = []
        for item in self.protection["branch_protection_rules"]:
            nodes.append({
                "id": item["id"], "pattern": item["pattern"], "allowsForcePushes": item["allows_force_pushes"],
                "isAdminEnforced": item["is_admin_enforced"], "requiresStatusChecks": item["requires_status_checks"],
                "requiredStatusCheckContexts": item["required_status_check_contexts"],
                "requiredStatusChecks": item["required_status_checks"],
                "requiresStrictStatusChecks": item["requires_strict_status_checks"],
                "requiresApprovingReviews": item["requires_approving_reviews"],
                "requiredApprovingReviewCount": item["required_approving_review_count"],
                "requiresConversationResolution": item["requires_conversation_resolution"],
                "restrictsPushes": item["restricts_pushes"],
                "requiresCommitSignatures": item["requires_commit_signatures"],
                "requiresLinearHistory": item["requires_linear_history"], "lockBranch": item["lock_branch"],
                "requiresDeployments": item["requires_deployments"], "requiredDeploymentEnvironments": item["required_deployment_environments"],
                "requireLastPushApproval": item["require_last_push_approval"], "requiresCodeOwnerReviews": item["requires_code_owner_reviews"],
                "allowsDeletions": item["allows_deletions"], "blocksCreations": item["blocks_creations"],
                "bypassForcePushAllowances": {"totalCount": len(item["bypass_force_push_allowances"]), "nodes": [
                    {"actor": actor} for actor in item["bypass_force_push_allowances"]
                ]},
                "bypassPullRequestAllowances": {"totalCount": len(item["bypass_pull_request_allowances"]), "nodes": [
                    {"actor": actor} for actor in item["bypass_pull_request_allowances"]
                ]},
                "pushAllowances": {"totalCount": len(item["push_allowances"]), "nodes": [
                    {"actor": actor} for actor in item["push_allowances"]
                ]},
            })
        return {"data": {"repository": {"branchProtectionRules": {"totalCount": len(nodes), "nodes": nodes}}}}


class ObserverOnlyApi(MockApi):
    def get(self, path):
        if path == publication.OBSERVER_REPOSITORIES_PATH or path == f"/apps/{publication.OBSERVER_APP_SLUG}" or path.startswith(f"/repos/{publication.REPOSITORY}/rulesets"):
            return super().get(path)
        raise SystemExit("observer received a non-protection API operation")

    def put(self, path):
        raise SystemExit("observer received a mutation")


class PublisherIdentityApi(MockApi):
    def __init__(self):
        super().__init__({})
        self.principal = "publisher"

    def get(self, path):
        if path in {publication.PUBLISHER_REPOSITORIES_PATH, f"/apps/{publication.PUBLISHER_APP_SLUG}"}:
            return super().get(path)
        raise SystemExit("publisher identity used an unsupported or unrelated endpoint")

    def post_graphql(self, query, variables):
        if query != publication.PUBLISHER_VIEWER_QUERY or variables != {}:
            raise SystemExit("publisher identity issued an unrelated GraphQL operation")
        return super().post_graphql(query, variables)

    def put(self, path):
        raise SystemExit("publisher identity attempted mutation")


class WorkflowOnlyApi(MockApi):
    def get(self, path):
        if path.startswith(f"/repos/{publication.REPOSITORY}/actions/workflows/"):
            return super().get(path)
        raise SystemExit("workflow reader received an observer API operation")

    def post_graphql(self, query, variables):
        raise SystemExit("workflow reader received a protection GraphQL operation")

    def put(self, path):
        raise SystemExit("workflow reader received a mutation")


def control_plan(manifest: dict) -> dict:
    return {
        "schema": "history-rewrite-control-plan-v1",
        "repository": publication.REPOSITORY,
        "manifest_sha256": digest(manifest),
        "suppression": [
            {"id": 231747419, "path": ".github/workflows/rust-release.yml", "state": "active"},
            {"id": 250252266, "path": ".github/workflows/sedna-release.yml", "state": "disabled_manually"},
        ],
        "writer_workflows": publication.WRITER_WORKFLOWS,
        "mirror": {"id": publication.MIRROR_WORKFLOW_ID, "path": ".github/workflows/sedna-sync-upstream.yml", "state": "disabled_manually"},
        "protection_snapshot_sha256": manifest["controls"]["protection_snapshot_sha256"],
    }


def phase_files(root: Path, manifest: dict) -> tuple[Path, Path, Path, Path]:
    manifest_path = root / "manifest.json"
    preflight = root / "preflight.json"
    plan_path = root / "control-plan.json"
    intent = root / "publication-restoration-intent.json"
    manifest_path.write_bytes(publication.canonical_json(manifest))
    publication.write_json(preflight, {
        "schema": "history-rewrite-publication-preflight-v1", "repository": publication.REPOSITORY,
        "manifest_sha256": digest(manifest), "harness_sha": manifest["harness_sha"],
        "harness_tree": manifest["harness_tree"], "selected_refs_sha256": manifest["selected_refs_sha256"],
        "output_refs_sha256": manifest["output_refs_sha256"], "backup_run_id": manifest["backup"]["run_id"],
        "proof_run_id": manifest["proof"]["run_id"],
        "approval": {"id": publication.REVIEWER_ID, "login": publication.REVIEWER_LOGIN},
        "writer_check": {"schema": "history-rewrite-writer-check-v1", "active": [], "status": "drained-at-single-read"},
        "protection_snapshot_sha256": manifest["controls"]["protection_snapshot_sha256"],
        "status": "verified-before-publisher-token",
    })
    publication.write_json(plan_path, control_plan(manifest))
    publication.write_json(intent, {
        "schema": "history-rewrite-restoration-intent-v1", "repository": publication.REPOSITORY,
        "manifest_sha256": digest(manifest), "harness_sha": manifest["harness_sha"],
        "harness_tree": manifest["harness_tree"], "preflight_sha256": publication.file_digest(preflight),
        "control_plan_sha256": publication.file_digest(plan_path), "before_refs_sha256": digest(manifest["selected_refs"]),
        "output_refs_sha256": digest(manifest["output_refs"]),
        "regenerated_proof_digests_sha256": digest(manifest["proof_digests"]),
        "status": "ready-no-write-credential-accessed",
    })
    return manifest_path, preflight, plan_path, intent


def git_adapter_fixture(root: Path) -> tuple[str, str]:
    source, remote = root / "source", root / "remote.git"
    subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
    git(source, "config", "user.name", "fixture"); git(source, "config", "user.email", "fixture@example.invalid")
    (source / "value").write_text("old\n", encoding="utf-8")
    git(source, "add", "value"); git(source, "commit", "-qm", "old")
    old = git(source, "rev-parse", "HEAD")
    git(source, "tag", "v1")
    subprocess.run(["git", "clone", "--bare", "--no-local", str(source), str(remote)], check=True, stdout=subprocess.DEVNULL)
    (source / "value").write_text("new\n", encoding="utf-8")
    git(source, "commit", "-qam", "new")
    new = git(source, "rev-parse", "HEAD")
    selected = {"refs/heads/main": old, "refs/tags/v1": old}
    output = {"refs/heads/main": new, "refs/tags/v1": new}
    manifest = base_manifest(selected, output)
    _, preflight, plan, intent = phase_files(root, manifest)
    api = WorkflowOnlyApi({231747419: "disabled_manually", 250252266: "disabled_manually", publication.MIRROR_WORKFLOW_ID: "disabled_manually"})
    observer_api = ObserverOnlyApi({})
    result = publication.publish_repository(
        manifest, frozen_sha=manifest["harness_sha"], frozen_tree=manifest["harness_tree"],
        manifest_sha256=digest(manifest), preflight_path=preflight, control_plan=plan, intent_path=intent,
        read_api=api, observer_api=observer_api, current_run_id=99, repo=source / ".git", remote_url=str(remote),
    )
    if result.outcome != "success" or publication.advertised_refs(str(remote)) != output:
        raise SystemExit("production Git adapter did not publish the exact disposable ref map")
    positive = hashlib.sha256((old + new + result.reason).encode()).hexdigest()
    rejected = root / "rejected.git"
    subprocess.run(["git", "clone", "--bare", "--no-local", str(source), str(rejected)], check=True, stdout=subprocess.DEVNULL)
    # Put the disposable rejection remote back at the approved preimage.
    git(rejected, "update-ref", "refs/heads/main", old); git(rejected, "update-ref", "refs/tags/v1", old)
    hook = rejected / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8"); hook.chmod(0o700)
    rejected_result = publication.publish_repository(
        manifest, frozen_sha=manifest["harness_sha"], frozen_tree=manifest["harness_tree"],
        manifest_sha256=digest(manifest), preflight_path=preflight, control_plan=plan, intent_path=intent,
        read_api=api, observer_api=observer_api, current_run_id=99, repo=source / ".git", remote_url=str(rejected),
    )
    if rejected_result.outcome != "ambiguous" or publication.advertised_refs(str(rejected)) != selected:
        raise SystemExit("atomic rejection fixture changed a disposable remote ref")
    negative = hashlib.sha256(rejected_result.reason.encode()).hexdigest()
    return positive, negative


def custody_fixtures() -> list[str]:
    names = ["recovery-snapshot-ciphertext", "recovery-snapshot-receipt",
             "history-rewrite-candidate-ciphertext", "history-rewrite-rewrite-42"]
    payloads = {index + 100: ("opaque-original-artifact-" + name).encode() for index, name in enumerate(names)}
    artifacts = [{"id": index + 100, "name": name, "run_id": 41 if index < 2 else 42,
                  "head_sha": "5" * 40, "size_in_bytes": len(payloads[index + 100]),
                  "sha256": hashlib.sha256(payloads[index + 100]).hexdigest()} for index, name in enumerate(names)]
    manifest = {"schema": "history-rewrite-custody-v1", "repository": publication.REPOSITORY,
                "requested_retention_days": 90, "artifacts": artifacts}
    documents = {}
    for item in artifacts:
        documents[f"/repos/{publication.REPOSITORY}/actions/artifacts/{item['id']}"] = {
            "id": item["id"], "name": item["name"], "size_in_bytes": item["size_in_bytes"],
            "digest": "sha256:" + item["sha256"], "expired": False, "expires_at": "2026-09-14T00:00:00Z",
            "workflow_run": {"id": item["run_id"], "head_sha": item["head_sha"]},
        }
        documents[f"/repos/{publication.REPOSITORY}/actions/runs/{item['run_id']}"] = {
            "id": item["run_id"], "head_sha": item["head_sha"], "status": "completed", "conclusion": "success",
            "repository": {"full_name": publication.REPOSITORY},
            "path": ".github/workflows/recovery-snapshot.yml" if item["run_id"] == 41 else ".github/workflows/history-rewrite-candidate.yml",
        }

    class ReadOnlyApi:
        def get(self, path):
            return copy.deepcopy(documents[path])

        def put(self, path):
            raise SystemExit("custody attempted a control mutation")

        def post_graphql(self, query, variables):
            raise SystemExit("custody attempted an unrelated API call")

    downloads = []

    def download(artifact_id, target, size):
        downloads.append(artifact_id)
        target.write_bytes(payloads[artifact_id])

    evidence = []
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        receipt = publication.custody_sources(manifest, digest(manifest), ReadOnlyApi(), root / "good", download)
        if downloads != [item["id"] for item in artifacts] or any(
            (root / "good" / f"artifact-{item['id']}.zip").read_bytes() != payloads[item["id"]] for item in artifacts
        ):
            raise SystemExit("custody did not preserve the exact original archive bytes")
        evidence.append("custody_exact_original_bytes_without_extraction")
        for name, mutate in (
            ("custody_digest", lambda value: value["artifacts"][0].update(sha256="0" * 64)),
            ("custody_name", lambda value: value["artifacts"][0].update(name="arbitrary-publication")),
            ("custody_source_run", lambda value: value["artifacts"][0].update(run_id=999)),
            ("custody_source_head", lambda value: value["artifacts"][0].update(head_sha="0" * 40)),
            ("custody_duplicate", lambda value: value["artifacts"].append(value["artifacts"][0])),
            ("custody_oversized", lambda value: value["artifacts"][0].update(size_in_bytes=2**40)),
        ):
            changed = copy.deepcopy(manifest); mutate(changed)
            before_downloads = list(downloads)
            evidence.append(expect_failure(name, lambda candidate=changed: publication.custody_sources(
                candidate, digest(candidate), ReadOnlyApi(), root / name, download)))
            if downloads != before_downloads:
                raise SystemExit("custody downloaded before all metadata bindings were verified")
        evidence.append(expect_failure("custody_external_approval_digest", lambda: publication.custody_sources(
            manifest, "0" * 64, ReadOnlyApi(), root / "unapproved", download)))
        artifact_path = f"/repos/{publication.REPOSITORY}/actions/artifacts/100"
        documents[artifact_path]["expired"] = True
        evidence.append(expect_failure("custody_expired_source", lambda: publication.custody_sources(
            manifest, digest(manifest), ReadOnlyApi(), root / "expired", download)))
        documents[artifact_path]["expired"] = False
        evidence.append(expect_failure("custody_short_corrupt_download", lambda: publication.custody_sources(
            manifest, digest(manifest), ReadOnlyApi(), root / "corrupt", lambda artifact_id, target, size: target.write_bytes(b"wrong"))))
        now = datetime(2026, 9, 11, tzinfo=timezone.utc)
        destination = {"id": 999, "name": "history-rewrite-encrypted-custody-43", "size_in_bytes": 1234,
                       "digest": "sha256:" + "9" * 64, "expired": False,
                       "created_at": now.isoformat(), "expires_at": (now + timedelta(days=90)).isoformat(),
                       "workflow_run": {"id": 43, "head_sha": "6" * 40}}

        def verify_destination(value):
            return publication.custody_destination(receipt, value, run_id=43, artifact_id=999,
                artifact_digest="9" * 64, head_sha="6" * 40, now=now)

        retained = verify_destination(destination)
        if retained["archival_deadline"] != (now + timedelta(days=83)).isoformat():
            raise SystemExit("custody deadline was not derived from actual destination expiry")
        evidence.append("custody_actual_expiry_and_finite_archival_deadline")
        for name, key, value in (
            ("custody_truncated_retention", "expires_at", (now + timedelta(days=3)).isoformat()),
            ("custody_wrong_destination_digest", "digest", "sha256:" + "0" * 64),
            ("custody_wrong_destination_run", "workflow_run", {"id": 44, "head_sha": "6" * 40}),
            ("custody_wrong_destination_head", "workflow_run", {"id": 43, "head_sha": "7" * 40}),
        ):
            changed = copy.deepcopy(destination); changed[key] = value
            evidence.append(expect_failure(name, lambda value=changed: verify_destination(value)))
    return evidence


def observer_fixtures() -> list[str]:
    evidence = []
    observer = ObserverOnlyApi({})
    identity = publication.validate_observer_identity(observer)
    if identity["app_id"] != publication.OBSERVER_APP_ID or identity["requested_token_permissions"] != {"administration": "read", "metadata": "read"}:
        raise SystemExit("observer identity receipt is incomplete")
    evidence.append("observer_exact_authenticated_identity_and_repository")
    changes = {
        "workflow_token_rejected": lambda value: value["observer_viewer"]["data"]["viewer"].update(login="github-actions[bot]"),
        "publisher_token_rejected": lambda value: value["observer_viewer"]["data"]["viewer"].update(login="sedna-release-publisher[bot]"),
        "operator_token_rejected": lambda value: value["observer_viewer"]["data"]["viewer"].update(login="operator-fixture"),
        "viewer_null": lambda value: value.update(observer_viewer={"data": None}),
        "viewer_errors": lambda value: value["observer_viewer"].update(errors=[{"type": "FORBIDDEN"}]),
        "wrong_app_id": lambda value: value["observer_app"].update(id=1),
        "wrong_app_node": lambda value: value["observer_app"].update(node_id="other"),
        "app_write_grant": lambda value: value["observer_app"]["permissions"].update(administration="write"),
        "app_extra_grant": lambda value: value["observer_app"]["permissions"].update(unknown="read"),
        "app_missing_grant": lambda value: value["observer_app"]["permissions"].pop("administration"),
        "wrong_repository_id": lambda value: value["observer_repositories"]["repositories"][0].update(id=1),
        "wrong_repository_name": lambda value: value["observer_repositories"]["repositories"][0].update(full_name="other/repository"),
        "multiple_repositories": lambda value: value["observer_repositories"].update(total_count=2),
        "hidden_extra_repository": lambda value: value["observer_repositories"]["repositories"].append({"id": 2}),
        "repository_count_boolean": lambda value: value["observer_repositories"].update(total_count=True),
    }
    for name, change in changes.items():
        invalid = ObserverOnlyApi({}); change(invalid.observer)
        evidence.append(expect_failure(f"observer_{name}", lambda api=invalid: publication.validate_observer_identity(api)))

    class ExpiredObserver(ObserverOnlyApi):
        def post_graphql(self, query, variables):
            raise PublicationError("observer token expired or denied; HTTP 401")

    workflow_reader = WorkflowOnlyApi({231747419: "disabled_manually", 250252266: "disabled_manually", publication.MIRROR_WORKFLOW_ID: "disabled_manually"})
    evidence.append(expect_failure("observer_expiry_does_not_fallback_to_workflow_reader",
                                   lambda: publication.verify_live_publication_state(base_manifest(), workflow_reader,
                                   observer_api=ExpiredObserver({}), current_run_id=99)))
    good_environment = {"HISTORY_REWRITE_OBSERVER_TOKEN": "fixture-observer-token",
                        "HISTORY_REWRITE_OBSERVER_INSTALLATION_ID": str(publication.OBSERVER_INSTALLATION_ID),
                        "HISTORY_REWRITE_OBSERVER_APP_SLUG": publication.OBSERVER_APP_SLUG,
                        "GH_TOKEN": "fixture-publisher-token", "HISTORY_REWRITE_READ_TOKEN": "fixture-workflow-token"}
    with patch.dict(os.environ, good_environment, clear=True):
        publication.observer_api_from_environment()
    for name, field, value in (
        ("missing_token", "HISTORY_REWRITE_OBSERVER_TOKEN", ""),
        ("publisher_alias", "HISTORY_REWRITE_OBSERVER_TOKEN", "fixture-publisher-token"),
        ("workflow_alias", "HISTORY_REWRITE_OBSERVER_TOKEN", "fixture-workflow-token"),
        ("wrong_installation_output", "HISTORY_REWRITE_OBSERVER_INSTALLATION_ID", "1"),
        ("missing_installation_output", "HISTORY_REWRITE_OBSERVER_INSTALLATION_ID", ""),
        ("wrong_app_output", "HISTORY_REWRITE_OBSERVER_APP_SLUG", "other"),
    ):
        with patch.dict(os.environ, {**good_environment, field: value}, clear=True):
            evidence.append(expect_failure(f"observer_{name}", publication.observer_api_from_environment))
    with tempfile.TemporaryDirectory() as temporary:
        api_dir = Path(temporary)
        captured = publication.protection_snapshot_from_api(observer, api_dir=api_dir)
        if captured != publication.protection_snapshot_from_files(api_dir):
            raise SystemExit("observer API capture changed the preflight protection evidence")
    evidence.append("observer_capture_preserves_preflight_protection_evidence")
    return evidence


def publisher_fixtures() -> list[str]:
    evidence = []
    good_environment = {
        "GH_TOKEN": "fixture-publisher-token",
        "HISTORY_REWRITE_PUBLISHER_INSTALLATION_ID": str(publication.PUBLISHER_INSTALLATION_ID),
        "HISTORY_REWRITE_PUBLISHER_APP_SLUG": publication.PUBLISHER_APP_SLUG,
        "HISTORY_REWRITE_OBSERVER_TOKEN": "fixture-observer-token",
        "HISTORY_REWRITE_READ_TOKEN": "fixture-workflow-token",
    }
    api = PublisherIdentityApi()
    identity = publication.validate_publisher_identity(api)
    if (identity["authenticated_login"] != "sedna-release-publisher[bot]"
            or identity["requested_token_permissions"] != publication.PUBLISHER_PERMISSIONS
            or identity["app_grants"] != publication.PUBLISHER_PERMISSIONS
            or "token_permissions" in identity):
        raise SystemExit("publisher receipt confuses requested token scope with direct permission introspection")
    evidence.append("publisher_authenticated_identity_exact_app_grants_and_repository")
    changes = {
        "workflow_token": lambda value: value["publisher_viewer"]["data"]["viewer"].update(login="github-actions[bot]"),
        "observer_token": lambda value: value["publisher_viewer"]["data"]["viewer"].update(login="sedna-codex-delivery-coordinator[bot]"),
        "operator_token": lambda value: value["publisher_viewer"]["data"]["viewer"].update(login="operator-fixture"),
        "viewer_null": lambda value: value.update(publisher_viewer={"data": None}),
        "viewer_errors": lambda value: value["publisher_viewer"].update(errors=[{"type": "FORBIDDEN"}]),
        "wrong_app_id": lambda value: value["publisher_app"].update(id=1),
        "wrong_app_node": lambda value: value["publisher_app"].update(node_id="other"),
        "wrong_app_slug": lambda value: value["publisher_app"].update(slug="other"),
        "extra_administration": lambda value: value["publisher_app"]["permissions"].update(administration="read"),
        "extra_permission": lambda value: value["publisher_app"]["permissions"].update(issues="write"),
        "missing_permission": lambda value: value["publisher_app"]["permissions"].pop("contents"),
        "changed_permission": lambda value: value["publisher_app"]["permissions"].update(contents="read"),
        "wrong_repository_id": lambda value: value["publisher_repositories"]["repositories"][0].update(id=1),
        "wrong_repository_name": lambda value: value["publisher_repositories"]["repositories"][0].update(full_name="other/repository"),
        "missing_repository": lambda value: value["publisher_repositories"].update(repositories=[]),
        "multiple_repositories": lambda value: value["publisher_repositories"].update(total_count=2),
        "hidden_extra_repository": lambda value: value["publisher_repositories"]["repositories"].append({"id": 2}),
        "repository_count_boolean": lambda value: value["publisher_repositories"].update(total_count=True),
    }
    for name, change in changes.items():
        invalid = PublisherIdentityApi(); change(invalid.publisher)
        evidence.append(expect_failure(f"publisher_{name}", lambda value=invalid: publication.validate_publisher_identity(value)))
    with patch.dict(os.environ, good_environment, clear=True):
        publication.publisher_api_from_environment()
    for name, field, value in (
        ("missing_token", "GH_TOKEN", ""),
        ("observer_alias", "GH_TOKEN", "fixture-observer-token"),
        ("workflow_alias", "GH_TOKEN", "fixture-workflow-token"),
        ("wrong_installation_output", "HISTORY_REWRITE_PUBLISHER_INSTALLATION_ID", "1"),
        ("missing_installation_output", "HISTORY_REWRITE_PUBLISHER_INSTALLATION_ID", ""),
        ("wrong_app_output", "HISTORY_REWRITE_PUBLISHER_APP_SLUG", "other"),
        ("missing_app_output", "HISTORY_REWRITE_PUBLISHER_APP_SLUG", ""),
    ):
        with patch.dict(os.environ, {**good_environment, field: value}, clear=True):
            evidence.append(expect_failure(f"publisher_{name}", publication.publisher_api_from_environment))

    # Exercise the actual CLI, HTTP request construction and JSON parser without
    # contacting GitHub or inventing an installation endpoint in the transport.
    paths = ["/graphql", f"/apps/{publication.PUBLISHER_APP_SLUG}", publication.PUBLISHER_REPOSITORIES_PATH]
    fields = ["publisher_viewer", "publisher_app", "publisher_repositories"]
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)

        def run_cli(documents, output, *, failure_path=None, status=None):
            calls = []

            def response(request, timeout):
                path = request.full_url.removeprefix("https://api.github.com")
                calls.append(path)
                if calls != paths[:len(calls)] or path not in paths:
                    raise SystemExit("publisher CLI retried, fell back, or used an unsupported endpoint")
                if request.get_header("Authorization") != "Bearer fixture-publisher-token":
                    raise SystemExit("publisher CLI replaced its bound credential")
                if path == "/graphql":
                    if request.get_method() != "POST" or json.loads(request.data) != {"query": publication.PUBLISHER_VIEWER_QUERY, "variables": {}}:
                        raise SystemExit("publisher CLI changed its identity-only GraphQL query")
                elif request.get_method() != "GET":
                    raise SystemExit("publisher identity CLI attempted a mutation")
                if path == failure_path:
                    raise publication.urllib.error.HTTPError(request.full_url, status, "fixture denial", {}, None)
                return io.BytesIO(publication.canonical_json(documents[fields[paths.index(path)]]))

            with patch.dict(os.environ, good_environment, clear=True), patch.object(sys, "argv", [
                    "publication.py", "publisher-identity", "--output", str(output)]), patch.object(
                    publication.urllib.request, "urlopen", side_effect=response):
                result = publication.main()
            return result, calls

        positive = root / "identity.json"
        result, calls = run_cli(publisher_fixture_documents(), positive)
        if result != 0 or calls != paths or publication.load_object(positive) != identity:
            raise SystemExit("publisher identity CLI diverges from the common validator")
        evidence.append("publisher_identity_cli_uses_documented_authenticated_operations")
        for name, change in changes.items():
            documents = publisher_fixture_documents(); change(documents)
            output = root / f"{name}.json"
            result, _ = run_cli(documents, output)
            if result == 0 or output.exists():
                raise SystemExit(f"publisher identity CLI emitted success for {name}")
        for path in paths:
            for status in (401, 403, 404):
                output = root / f"denied-{status}.json"
                result, calls = run_cli(publisher_fixture_documents(), output, failure_path=path, status=status)
                if result == 0 or output.exists() or calls[-1] != path:
                    raise SystemExit("publisher denial did not stop before receipt or fallback")
        evidence.append("publisher_cli_negative_identity_and_http_denial_without_fallback")
    return evidence


def protection_api_contract_fixtures() -> list[str]:
    """Independent API-shaped main-rule examples, not MockApi round trips."""
    examples = json.loads(Path(__file__).with_name("protection_api_states.json").read_text())
    before = before_snapshot(visible=True)
    original = copy.deepcopy(before)
    plan = publication.plan_maintenance(before, copy.deepcopy(before))
    for phase, expected in (("before", plan["administrator_before"]), ("after", plan["administrator_after"])):
        sample = examples[phase]
        observed = publication.normalize_protection_snapshot(sample["graphql"], [])
        rule = observed["branch_protection_rules"][0]
        expected_main = next(item for item in expected["branch_protection_rules"] if item["pattern"] == "main")
        if rule != expected_main:
            raise SystemExit(f"independent API-shaped {phase} rule differs from the plan")
        rest = sample["rest"]
        if rest["allow_force_pushes"]["enabled"] is not rule["allows_force_pushes"]:
            raise SystemExit("REST and GraphQL general force-push flags disagree")
        if phase == "before":
            if rest["required_status_checks"]["strict"] is not rule["requires_strict_status_checks"]:
                raise SystemExit("enabled check strictness was not preserved")
            if rest["restrictions"] is not None or rule["requires_status_checks"] is not True:
                raise SystemExit("enabled baseline check/restriction representation changed")
        else:
            if rest["required_status_checks"] is not None or rule["requires_status_checks"] is not False:
                raise SystemExit("disabled required checks were interpreted as an active strict gate")
            restrictions = rest["restrictions"]
            if (restrictions["users"] or restrictions["teams"]
                    or restrictions["apps"] != [{"id": publication.PUBLISHER_APP_ID, "slug": publication.PUBLISHER_APP_SLUG}]):
                raise SystemExit("REST push restriction is not exact publisher-only")
            if rule["requires_strict_status_checks"] is not True:
                raise SystemExit("observed inactive raw strictness was normalized away")
    if before != original or plan["administrator_before"] != original or plan["read_token_before"] != original:
        raise SystemExit("planning altered the exact rollback preimage")
    admitted = {"restricts_pushes", "bypass_force_push_allowances", "push_allowances",
                "requires_status_checks", "requires_strict_status_checks", "required_status_checks",
                "required_status_check_contexts", "bypass_pull_request_allowances"}
    for old, new in zip(original["branch_protection_rules"], plan["administrator_after"]["branch_protection_rules"], strict=True):
        if {key: value for key, value in old.items() if key not in admitted} != {key: value for key, value in new.items() if key not in admitted}:
            raise SystemExit("planning changed an unrelated classic protection field")
        if old["pattern"] == "upstream-main" and old["requires_strict_status_checks"] != new["requires_strict_status_checks"]:
            raise SystemExit("planning changed untouched disabled-check strictness")
    changed = copy.deepcopy(examples["after"]["graphql"])
    changed["data"]["repository"]["branchProtectionRules"]["nodes"][0]["requiresStrictStatusChecks"] = False
    changed_rule = publication.normalize_protection_snapshot(changed, [])["branch_protection_rules"][0]
    if changed_rule["requires_strict_status_checks"] is not False:
        raise SystemExit("normalization concealed an inactive strictness mismatch")
    return ["independent_api_general_force_vs_actor_allowance", "independent_api_disabled_check_representation",
            "inactive_strictness_retained_for_exact_comparison", "unrelated_controls_and_exact_rollback_preserved"]


def main() -> None:
    manifest = base_manifest()
    evidence = []
    publication.validate_manifest(manifest, frozen_sha="5" * 40, frozen_tree="6" * 40)
    evidence.extend(["manifest_complete", "queue_actor_visibility_is_not_invented"])
    evidence.extend(protection_api_contract_fixtures())
    for field in ("requires_strict_status_checks", "is_admin_enforced"):
        changed = copy.deepcopy(manifest)
        controls = changed["controls"]
        plan = controls["maintenance_plan"]
        for snapshot in (plan["administrator_after"], plan["read_token_after"], controls["protection_snapshot"]):
            next(rule for rule in snapshot["branch_protection_rules"] if rule["pattern"] == "main")[field] = False
        controls["maintenance_plan_sha256"] = digest(plan)
        controls["protection_snapshot_sha256"] = digest(controls["protection_snapshot"])
        evidence.append(expect_failure(f"self_consistent_after_state_drift_{field}", lambda value=changed: publication.validate_manifest(
            value, frozen_sha="5" * 40, frozen_tree="6" * 40,
        )))
    visible_queue_manifest = copy.deepcopy(manifest)
    visible_plan = publication.plan_maintenance(before_snapshot(visible=True), before_snapshot(visible=True))
    visible_queue_manifest["controls"].update(maintenance_plan=visible_plan, maintenance_plan_sha256=digest(visible_plan),
                                           protection_snapshot=visible_plan["read_token_after"],
                                           protection_snapshot_sha256=digest(visible_plan["read_token_after"]))
    publication.validate_manifest(visible_queue_manifest, frozen_sha="5" * 40, frozen_tree="6" * 40)
    evidence.append("queue_only_exact_visible_maintenance_actor")
    cases = {
        "missing_ref": lambda value: value["output_refs"].pop("refs/tags/v1"),
        "extra_ref": lambda value: value["output_refs"].update({"refs/heads/extra": "a" * 40}),
        "zero_ref": lambda value: value["selected_refs"].update({"refs/heads/main": "0" * 40}),
        "map_digest": lambda value: value.update(selected_refs_sha256="0" * 64),
        "proof_digest": lambda value: value["proof_digests"].update({"commit-map.txt": "f" * 40}),
        "stale_backup": lambda value: value["proof"].update(run_id=value["backup"]["run_id"]),
        "wrong_branch": lambda value: value["controls"].update(publication_branch="main"),
        "missing_mandatory_writer": lambda value: value["controls"]["writer_workflows"].pop(".github/workflows/sedna-release.yml"),
        "arbitrary_writer": lambda value: value["controls"]["writer_workflows"].update({".github/workflows/other.yml": 999}),
        "missing_maintenance_plan": lambda value: value["controls"].pop("maintenance_plan"),
    }
    for name, mutate in cases.items():
        candidate = copy.deepcopy(manifest); mutate(candidate)
        evidence.append(expect_failure(name, lambda value=candidate: publication.validate_manifest(value, frozen_sha="5" * 40, frozen_tree="6" * 40)))
    environment, policies = environment_fixture()
    publication.validate_environment(environment, policies); publication.validate_approval(approval_fixture())
    evidence.append("live_api_shapes")
    bad_environment = copy.deepcopy(environment); bad_environment["protection_rules"][0]["reviewers"][0]["reviewer"]["id"] = 1
    evidence.append(expect_failure("environment", lambda: publication.validate_environment(bad_environment, policies)))
    bad_approval = approval_fixture(); bad_approval[0]["user"]["id"] = 1
    evidence.append(expect_failure("approval", lambda: publication.validate_approval(bad_approval)))
    bad_protection = copy.deepcopy(manifest)
    bad_protection["controls"]["protection_snapshot"]["branch_protection_rules"][0]["bypass_force_push_allowances"] = []
    bad_protection["controls"]["protection_snapshot_sha256"] = digest(bad_protection["controls"]["protection_snapshot"])
    evidence.append(expect_failure("publisher_app_exception", lambda: publication.validate_manifest(
        bad_protection, frozen_sha="5" * 40, frozen_tree="6" * 40,
    )))
    unknown_ruleset = copy.deepcopy(manifest)
    unknown_ruleset["controls"]["protection_snapshot"]["repository_rulesets"].append({
        "id": 900, "name": "fixture-main", "target": "branch", "enforcement": "active",
        "bypass_actors_visibility": "visible",
        "bypass_actors": [{"actor_id": publication.PUBLISHER_APP_ID, "actor_type": "Integration", "bypass_mode": "always"}],
        "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
        "rules": [{"type": "non_fast_forward"}],
    })
    unknown_ruleset["controls"]["protection_snapshot_sha256"] = digest(unknown_ruleset["controls"]["protection_snapshot"])
    evidence.append(expect_failure("unknown_ruleset_rejected_even_with_app_bypass", lambda: publication.validate_manifest(
        unknown_ruleset, frozen_sha="5" * 40, frozen_tree="6" * 40,
    )))
    visible_queue_actors = copy.deepcopy(visible_queue_manifest)
    visible_queue_actors["controls"]["protection_snapshot"]["repository_rulesets"][0]["bypass_actors"].append({
        "actor_id": 999, "actor_type": "Integration", "bypass_mode": "always",
    })
    visible_queue_actors["controls"]["protection_snapshot_sha256"] = digest(visible_queue_actors["controls"]["protection_snapshot"])
    evidence.append(expect_failure("queue_extra_visible_bypass_actor", lambda: publication.validate_manifest(
        visible_queue_actors, frozen_sha="5" * 40, frozen_tree="6" * 40,
    )))
    for name, field, value in (
        ("general_force_pushes_rejected_even_with_exact_app", "allows_force_pushes", True),
        ("force_exception_missing", "bypass_force_push_allowances", []),
        ("force_exception_wrong_app", "bypass_force_push_allowances", [{**publication.PUBLISHER_ACTOR, "databaseId": 999}]),
        ("force_exception_extra_app", "bypass_force_push_allowances", [publication.PUBLISHER_ACTOR, {**publication.PUBLISHER_ACTOR, "databaseId": 999}]),
        ("old_status_gate_blocks_publication", "requires_status_checks", True),
        ("pr_exception_absent", "bypass_pull_request_allowances", []),
        ("push_restriction_absent", "restricts_pushes", False),
        ("foreign_push_actor", "push_allowances", [publication.PUBLISHER_ACTOR, {"__typename": "User", "id": "foreign"}]),
        ("missing_strictness_evidence", "requires_strict_status_checks", None),
        ("signed_commit_gate_not_waived", "requires_commit_signatures", True),
    ):
        changed = protection_snapshot(); changed["branch_protection_rules"][0][field] = value
        evidence.append(expect_failure(name, lambda snapshot=changed: publication.validate_protection_snapshot(snapshot)))
    before = before_snapshot(visible=True)
    before["branch_protection_rules"][0]["required_status_checks"][0]["app"]["databaseId"] = 999
    evidence.append(expect_failure("wrong_check_source_not_inferred", lambda: publication.plan_maintenance(before, before)))

    def extra_gate(rule, entry):
        rule["required_status_checks"].append(entry)
        rule["required_status_check_contexts"].append(entry["context"])

    check_domain_cases = {
        "extra_null_source_gate": lambda rule: extra_gate(rule, {"context": "extra gate", "app": None}),
        "extra_missing_source_gate": lambda rule: extra_gate(rule, {"context": "extra gate"}),
        "extra_valid_source_gate": lambda rule: extra_gate(rule, {"context": "extra gate", "app": {"databaseId": 15368, "slug": "github-actions"}}),
        "required_source_null": lambda rule: rule["required_status_checks"][0].update(app=None),
        "required_source_missing": lambda rule: rule["required_status_checks"][0].pop("app"),
        "source_database_id_missing": lambda rule: rule["required_status_checks"][0]["app"].pop("databaseId"),
        "source_database_id_wrong_type": lambda rule: rule["required_status_checks"][0]["app"].update(databaseId="15368"),
        "source_slug_missing": lambda rule: rule["required_status_checks"][0]["app"].pop("slug"),
        "source_slug_null": lambda rule: rule["required_status_checks"][0]["app"].update(slug=None),
        "source_slug_contradiction": lambda rule: rule["required_status_checks"][0]["app"].update(slug="unknown-app"),
        "source_unknown_field": lambda rule: rule["required_status_checks"][0]["app"].update(unreviewed=True),
        "check_unknown_field": lambda rule: rule["required_status_checks"][0].update(unreviewed=True),
        "check_context_missing": lambda rule: rule["required_status_checks"][0].pop("context"),
        "check_entry_missing": lambda rule: rule["required_status_checks"].pop(),
        "check_entry_null": lambda rule: rule["required_status_checks"].append(None),
        "duplicate_identical_check": lambda rule: rule["required_status_checks"].append(copy.deepcopy(rule["required_status_checks"][0])),
        "duplicate_contradictory_check_source": lambda rule: rule["required_status_checks"].insert(0, {"context": "CI required", "app": {"databaseId": 999, "slug": "unknown-app"}}),
        "duplicate_context": lambda rule: rule["required_status_check_contexts"].append("CI required"),
        "context_missing": lambda rule: rule["required_status_check_contexts"].pop(),
        "context_extra": lambda rule: rule["required_status_check_contexts"].append("extra gate"),
        "context_null": lambda rule: rule["required_status_check_contexts"].append(None),
        "context_domain_contradiction": lambda rule: rule.update(required_status_check_contexts=["CI required", "different gate"]),
        "both_required_domains_empty": lambda rule: rule.update(required_status_checks=[], required_status_check_contexts=[]),
    }
    for name, change in check_domain_cases.items():
        for pattern in ("main", "integration/app-server-delivery-train-20260824"):
            changed = before_snapshot(visible=True)
            change(next(rule for rule in changed["branch_protection_rules"] if rule["pattern"] == pattern))
            evidence.append(expect_failure(f"maintenance_{name}_{pattern.split('/')[0]}",
                                           lambda value=changed: publication.plan_maintenance(value, value)))
    reordered = before_snapshot(visible=True)
    for rule in reordered["branch_protection_rules"]:
        rule["required_status_check_contexts"].reverse()
    publication.plan_maintenance(reordered, copy.deepcopy(reordered))
    evidence.append("maintenance_exact_check_domain_order_independent")
    reader = before_snapshot(visible=False); reader["branch_protection_rules"][0]["requires_strict_status_checks"] = False
    evidence.append(expect_failure("principal_semantic_disagreement", lambda: publication.plan_maintenance(before_snapshot(visible=True), reader)))
    source = before_snapshot(visible=True)
    before_bytes = publication.canonical_json(source)
    publication.plan_maintenance(source, before_snapshot(visible=False))
    if publication.canonical_json(source) != before_bytes:
        raise SystemExit("maintenance planning modified the rollback preimage")
    evidence.append("exact_rollback_preimage_preserved_without_mutation")
    reader_api = MockApi({}, protection=before_snapshot(visible=False))
    observed = publication.protection_snapshot_from_api(reader_api)
    publication.validate_snapshot_shape(observed)
    if observed != before_snapshot(visible=False) or reader_api.mutations:
        raise SystemExit("read-only snapshot changed state or failed exact principal projection")
    evidence.append("read_only_snapshot_current_blocked_state")

    class DeniedProtectionApi(MockApi):
        def __init__(self):
            super().__init__({})
            self.graphql_calls = 0

        def post_graphql(self, query, variables):
            self.graphql_calls += 1
            if self.graphql_calls != 1:
                raise SystemExit("denied observer retried a narrower query")
            return {"data": {"repository": {"branchProtectionRules": None}},
                    "errors": [{"type": "FORBIDDEN", "path": ["repository", "branchProtectionRules"]}]}

        def get(self, path):
            raise SystemExit("denied observer continued collecting partial evidence")

    denied_api = DeniedProtectionApi()
    evidence.append(expect_failure("denied_protection_observer_fails_closed_without_fallback",
                                   lambda: publication.protection_snapshot_from_api(denied_api)))
    if denied_api.graphql_calls != 1 or denied_api.mutations:
        raise SystemExit("denied observer changed capabilities or state")
    workflow = (Path(__file__).resolve().parents[3] / ".github/workflows/history-rewrite-candidate.yml").read_text()
    for job_name in ("snapshot", "custody"):
        block = workflow.split(f"\n  {job_name}:\n", 1)[1].split("\n  synthetic:\n", 1)[0]
        if job_name == "snapshot":
            block = block.split("\n  custody:\n", 1)[0]
        forbidden = ("contents: write", "actions: write", " controls ", " publish ", "SEDNA_RELEASE_PUBLISHER")
        if job_name == "custody":
            forbidden += ("secrets.", "environment:", "create-github-app-token")
        elif re.findall(r"secrets\.([A-Z_]+)", block) != ["HISTORY_REWRITE_OBSERVER_APP_PRIVATE_KEY"] or "name: history-rewrite-publication" not in block:
            raise SystemExit("snapshot does not isolate its approved observer credential and environment")
        if any(value in block for value in forbidden):
            raise SystemExit(f"{job_name} workflow acquired publication capability")
        if f"if: inputs.mode == '{job_name}'" not in block:
            raise SystemExit(f"{job_name} mode can enter through another dispatch")
    evidence.append("snapshot_and_custody_job_capability_exclusions")
    token_blocks = workflow.split("      - name: Mint repository-scoped protection observer\n")[1:]
    if len(token_blocks) != 2:
        raise SystemExit("observer mint placement differs from the two admitted jobs")
    for remainder in token_blocks:
        block = remainder.split("      - name:", 1)[0]
        for required in ("actions/create-github-app-token@1b10c78c7865c340bc4f6099eb2f838309f1e8c3", "client-id: Iv23liJxB0M5u6W3cehS",
                         "owner: sednalabs", "repositories: codex", "permission-administration: read", "permission-metadata: read", "skip-token-revoke: false"):
            if required not in block:
                raise SystemExit(f"observer mint omitted its explicit binding: {required}")
        if re.findall(r"permission-([a-z-]+): ([a-z]+)", block) != [("administration", "read"), ("metadata", "read")]:
            raise SystemExit("observer token permission request escaped its exact two-read boundary")
    evidence.append("observer_mint_repository_permission_and_revocation_contract")
    publisher_block = workflow.split("      - name: Mint release publisher App token after environment approval and preflight\n", 1)[1].split("      - name:", 1)[0]
    for required in ("actions/create-github-app-token@1b10c78c7865c340bc4f6099eb2f838309f1e8c3",
                     "owner: sednalabs", "repositories: codex", "skip-token-revoke: false"):
        if required not in publisher_block:
            raise SystemExit(f"publisher token action omitted its exact scope: {required}")
    if re.findall(r"permission-([a-z-]+): ([a-z]+)", publisher_block) != [("actions", "write"), ("contents", "write"), ("metadata", "read")]:
        raise SystemExit("publisher token request differs from its admitted exact permissions")
    for step_name in ("Verify minted publisher App identity", "Suppress only manifest-authorised release writers",
                      "Publish explicit refs once with atomic per-ref leases", "Restore captured release workflow states"):
        block = workflow.split(f"      - name: {step_name}\n", 1)[1].split("      - name:", 1)[0]
        for key, output in (("GH_TOKEN", "token"), ("HISTORY_REWRITE_PUBLISHER_INSTALLATION_ID", "installation-id"),
                            ("HISTORY_REWRITE_PUBLISHER_APP_SLUG", "app-slug")):
            if f"{key}: ${{{{ steps.release_publisher_token.outputs.{output} }}}}" not in block:
                raise SystemExit("publisher consumer lost its pinned action output binding")
    if "gh api installation " in workflow or "publication.py publisher-identity" not in workflow:
        raise SystemExit("workflow bypasses the common documented publisher identity validator")
    evidence.append("publisher_token_scope_and_all_consumer_bindings")
    evidence.extend(observer_fixtures())
    evidence.extend(publisher_fixtures())
    evidence.extend(custody_fixtures())
    queue_rule_type_drift = copy.deepcopy(manifest)
    queue_rule_type_drift["controls"]["protection_snapshot"]["repository_rulesets"][0]["rules"].append({
        "type": "non_fast_forward",
    })
    queue_rule_type_drift["controls"]["protection_snapshot_sha256"] = digest(queue_rule_type_drift["controls"]["protection_snapshot"])
    evidence.append(expect_failure("queue_only_ruleset_type_drift", lambda: publication.validate_manifest(
        queue_rule_type_drift, frozen_sha="5" * 40, frozen_tree="6" * 40,
    )))
    plan = control_plan(manifest)
    api = MockApi({231747419: "active", 250252266: "disabled_manually", publication.MIRROR_WORKFLOW_ID: "disabled_manually"})
    suppression = publication.set_controls(plan, manifest, api, restore=False)
    restoration = publication.set_controls(plan, manifest, api, restore=True)
    if api.states != {231747419: "active", 250252266: "disabled_manually", publication.MIRROR_WORKFLOW_ID: "disabled_manually"}:
        raise SystemExit("control restoration did not reproduce the captured state")
    evidence.append("control_suppression_and_restoration")
    writer_check = publication.check_writers_once(api, current_run_id=99)
    if writer_check["status"] != "drained-at-single-read" or suppression["operation"] != "suppress" or restoration["operation"] != "restore":
        raise SystemExit("control lifecycle receipt mismatch")
    busy = MockApi(api.states, {231747419: [{"id": 100, "status": "in_progress", "head_sha": "a" * 40}]})
    evidence.append(expect_failure("active_writer_single_check", lambda: publication.check_writers_once(busy, current_run_id=99)))
    with tempfile.TemporaryDirectory() as phase_temporary:
        phase_root = Path(phase_temporary)
        manifest_path, preflight, plan_path, intent = phase_files(phase_root, manifest)
        publication.validate_phase_bindings(
            manifest, frozen_sha="5" * 40, frozen_tree="6" * 40, manifest_sha256=digest(manifest),
            preflight_path=preflight, control_plan=plan_path, intent_path=intent,
        )
        evidence.append(expect_failure("external_manifest_digest", lambda: publication.validate_phase_bindings(
            manifest, frozen_sha="5" * 40, frozen_tree="6" * 40, manifest_sha256="0" * 64,
            preflight_path=preflight, control_plan=plan_path, intent_path=intent,
        )))
        evidence.append(expect_failure("external_frozen_harness", lambda: publication.validate_phase_bindings(
            manifest, frozen_sha="0" * 40, frozen_tree="6" * 40, manifest_sha256=digest(manifest),
            preflight_path=preflight, control_plan=plan_path, intent_path=intent,
        )))
        artifact = phase_root / "intent.zip"
        with zipfile.ZipFile(artifact, "w") as archive:
            for path in (manifest_path, preflight, plan_path, intent):
                archive.write(path, path.name)
        artifact_digest = publication.file_digest(artifact)
        metadata = phase_root / "artifact.json"
        publication.write_json(metadata, {
            "id": 700, "name": "history-rewrite-publication-intent-70", "digest": "sha256:" + artifact_digest,
            "expired": False, "workflow_run": {"id": 70},
        })
        fixture_api = phase_root / "fixture-api.json"
        publication.write_json(fixture_api, {
            **publisher_fixture_documents(),
            "workflows": {
                "231747419": {"id": 231747419, "path": ".github/workflows/rust-release.yml", "state": "disabled_manually"},
                "250252266": {"id": 250252266, "path": ".github/workflows/sedna-release.yml", "state": "disabled_manually"},
                str(publication.MIRROR_WORKFLOW_ID): {"id": publication.MIRROR_WORKFLOW_ID, "path": ".github/workflows/sedna-sync-upstream.yml", "state": "disabled_manually"},
            },
        })
        receipt = phase_root / "independent-restoration.json"
        command = [
            sys.executable, str(Path(publication.__file__)), "restore-intent-artifact",
            "--artifact-zip", str(artifact), "--artifact-api-json", str(metadata),
            "--run-id", "70", "--artifact-id", "700", "--artifact-api-digest", artifact_digest,
            "--frozen-sha", "5" * 40, "--frozen-tree", "6" * 40,
            "--manifest-sha256", digest(manifest), "--receipt", str(receipt),
            "--fixture-api", str(fixture_api),
        ]
        environment = dict(os.environ); environment["HISTORY_REWRITE_PUBLICATION_FIXTURE"] = "1"
        rejected_command = list(command)
        rejected_command[rejected_command.index(digest(manifest))] = "0" * 64
        rejected = subprocess.run(rejected_command, text=True, capture_output=True, env=environment)
        if rejected.returncode == 0:
            raise SystemExit("independent restoration CLI accepted a changed external manifest digest")
        recovered = subprocess.run(command, text=True, capture_output=True, env=environment)
        if recovered.returncode != 0:
            raise SystemExit("independent hard-kill restoration CLI failed: " + recovered.stderr.strip())
        recovered_receipt = publication.load_object(receipt, "independent restoration receipt")
        if recovered_receipt.get("restoration", {}).get("operation") != "restore":
            raise SystemExit("independent hard-kill restoration CLI did not restore captured states")
        evidence.extend(["independent_restoration_rejects_changed_manifest", "independent_uploaded_intent_restoration_cli"])
    with tempfile.TemporaryDirectory() as temporary:
        positive, rejected = git_adapter_fixture(Path(temporary))
        evidence.extend(["real_git_atomic_leases:" + positive, "real_git_atomic_rejection:" + rejected])
    resolved = publication.resolve_push_failure(manifest["output_refs"], PublicationError("transport lost"), lambda: manifest["output_refs"])
    if resolved.outcome != "success":
        raise SystemExit("transport ambiguity was not resolved by the exact after-map")
    evidence.append("ambiguous_transport_exact_readback")
    evidence.append(expect_failure("readback_error", lambda: publication.resolve_push_failure(
        manifest["output_refs"], PublicationError("transport lost"),
        lambda: (_ for _ in ()).throw(PublicationError("readback unavailable")),
    )))
    print("publication_fixture_schema=history-rewrite-publication-v2")
    for item in evidence:
        print(f"fixture_case={item} evidence_sha256={hashlib.sha256(item.encode()).hexdigest()}")


if __name__ == "__main__":
    main()
