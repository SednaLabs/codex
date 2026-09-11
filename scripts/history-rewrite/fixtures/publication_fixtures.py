#!/usr/bin/env python3
"""Hosted contract fixtures for guarded history-rewrite publication."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import publication
from publication import PublicationError, digest


def protection_snapshot() -> dict:
    rules = []
    for ref in publication.PROTECTED_REFS:
        branch = ref.removeprefix("refs/heads/")
        rules.append({
            "id": "rule-" + hashlib.sha256(branch.encode()).hexdigest()[:12],
            "pattern": branch,
            "allows_force_pushes": True,
            "is_admin_enforced": True,
            "requires_status_checks": True,
            "required_status_check_contexts": ["fixture"],
            "requires_approving_reviews": True,
            "required_approving_review_count": 0,
            "requires_conversation_resolution": True,
            "restricts_pushes": False,
            "bypass_force_push_allowances": [{
                "__typename": "App", "id": publication.PUBLISHER_APP_NODE_ID,
                "databaseId": publication.PUBLISHER_APP_ID, "slug": "fixture-publisher",
            }],
        })
    rules.sort(key=lambda item: (item["pattern"], item["id"]))
    return {
        "schema": "history-rewrite-protection-snapshot-v1",
        "protected_refs": list(publication.PROTECTED_REFS),
        "branch_protection_rules": rules,
        "repository_rulesets": [{
            "id": publication.QUEUE_ONLY_RULESET_ID,
            "name": publication.QUEUE_ONLY_RULESET_NAME,
            "target": "branch",
            "enforcement": "active",
            "bypass_actors": [],
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


class MockApi:
    def __init__(self, states: dict[int, str], runs: dict[int, list[dict]] | None = None, protection: dict | None = None):
        self.states = dict(states)
        self.runs = runs or {}
        self.protection = protection or protection_snapshot()
        self.mutations: list[tuple[int, str]] = []

    def get(self, path: str) -> object:
        if path == "/installation":
            return {"app_id": publication.PUBLISHER_APP_ID, "app_slug": "fixture-publisher"}
        if path == "/apps/fixture-publisher":
            return {"id": publication.PUBLISHER_APP_ID, "node_id": publication.PUBLISHER_APP_NODE_ID}
        if path.startswith(f"/repos/{publication.REPOSITORY}/rulesets?"):
            return [{"id": item["id"]} for item in self.protection["repository_rulesets"]]
        if path.startswith(f"/repos/{publication.REPOSITORY}/rulesets/"):
            ruleset_id = int(path.rsplit("/", 1)[1])
            return next(item for item in self.protection["repository_rulesets"] if item["id"] == ruleset_id)
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
        nodes = []
        for item in self.protection["branch_protection_rules"]:
            nodes.append({
                "id": item["id"], "pattern": item["pattern"], "allowsForcePushes": item["allows_force_pushes"],
                "isAdminEnforced": item["is_admin_enforced"], "requiresStatusChecks": item["requires_status_checks"],
                "requiredStatusCheckContexts": item["required_status_check_contexts"],
                "requiresApprovingReviews": item["requires_approving_reviews"],
                "requiredApprovingReviewCount": item["required_approving_review_count"],
                "requiresConversationResolution": item["requires_conversation_resolution"],
                "restrictsPushes": item["restricts_pushes"],
                "bypassForcePushAllowances": {"totalCount": len(item["bypass_force_push_allowances"]), "nodes": [
                    {"actor": actor} for actor in item["bypass_force_push_allowances"]
                ]},
            })
        return {"data": {"repository": {"branchProtectionRules": {"totalCount": len(nodes), "nodes": nodes}}}}


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
        "status": "verified-before-app-token",
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
    api = MockApi({231747419: "disabled_manually", 250252266: "disabled_manually", publication.MIRROR_WORKFLOW_ID: "disabled_manually"})
    result = publication.publish_repository(
        manifest, frozen_sha=manifest["harness_sha"], frozen_tree=manifest["harness_tree"],
        manifest_sha256=digest(manifest), preflight_path=preflight, control_plan=plan, intent_path=intent,
        read_api=api, current_run_id=99, repo=source / ".git", remote_url=str(remote),
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
        read_api=api, current_run_id=99, repo=source / ".git", remote_url=str(rejected),
    )
    if rejected_result.outcome != "ambiguous" or publication.advertised_refs(str(rejected)) != selected:
        raise SystemExit("atomic rejection fixture changed a disposable remote ref")
    negative = hashlib.sha256(rejected_result.reason.encode()).hexdigest()
    return positive, negative


def main() -> None:
    manifest = base_manifest()
    evidence = []
    publication.validate_manifest(manifest, frozen_sha="5" * 40, frozen_tree="6" * 40)
    evidence.extend(["manifest_complete", "queue_only_ruleset_preserved_without_bypass"])
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
    bad_ruleset_bypass = copy.deepcopy(manifest)
    bad_ruleset_bypass["controls"]["protection_snapshot"]["repository_rulesets"].append({
        "id": 900, "name": "fixture-main", "target": "branch", "enforcement": "active",
        "bypass_actors": [{"actor_id": publication.PUBLISHER_APP_ID, "actor_type": "Integration", "bypass_mode": "pull_request"}],
        "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}}, "rules": [],
    })
    bad_ruleset_bypass["controls"]["protection_snapshot_sha256"] = digest(bad_ruleset_bypass["controls"]["protection_snapshot"])
    evidence.append(expect_failure("publisher_app_ruleset_bypass", lambda: publication.validate_manifest(
        bad_ruleset_bypass, frozen_sha="5" * 40, frozen_tree="6" * 40,
    )))
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
            "installation": {"app_id": publication.PUBLISHER_APP_ID, "app_slug": "fixture-publisher"},
            "app": {"id": publication.PUBLISHER_APP_ID, "node_id": publication.PUBLISHER_APP_NODE_ID},
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
