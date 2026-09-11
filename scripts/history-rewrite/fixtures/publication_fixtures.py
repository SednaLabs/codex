#!/usr/bin/env python3
"""Hosted contract fixtures for guarded history-rewrite publication."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import publication
from publication import PublicationError, digest


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
            "suppress_workflows": [".github/workflows/rust-release.yml", ".github/workflows/sedna-release.yml"],
            "active_writer_workflows": [101, 102],
            "mirror_workflow_id": publication.MIRROR_WORKFLOW_ID,
        },
    }
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
    def __init__(self, states: dict[int, str], runs: dict[int, list[dict]] | None = None):
        self.states = dict(states)
        self.runs = runs or {}
        self.mutations: list[tuple[int, str]] = []

    def get(self, path: str) -> object:
        parts = path.split("?")[0].split("/")
        workflow_id = int(parts[6])
        if parts[-1] == "runs":
            return {"workflow_runs": self.runs.get(workflow_id, [])}
        return {"id": workflow_id, "state": self.states[workflow_id]}

    def put(self, path: str) -> None:
        parts = path.split("/")
        workflow_id, action = int(parts[6]), parts[7]
        self.states[workflow_id] = "active" if action == "enable" else "disabled_manually"
        self.mutations.append((workflow_id, action))


def control_plan(manifest: dict) -> dict:
    return {
        "schema": "history-rewrite-control-plan-v1",
        "repository": publication.REPOSITORY,
        "manifest_sha256": digest(manifest),
        "suppression": [
            {"id": 101, "path": ".github/workflows/rust-release.yml", "state": "active"},
            {"id": 102, "path": ".github/workflows/sedna-release.yml", "state": "disabled_manually"},
        ],
        "active_writer_workflow_ids": [101, 102],
        "mirror": {"id": publication.MIRROR_WORKFLOW_ID, "path": ".github/workflows/sedna-sync-upstream.yml", "state": "disabled_manually"},
    }


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
    result = publication.publish_repository(manifest, repo=source / ".git", remote_url=str(remote))
    if result.outcome != "success" or publication.advertised_refs(str(remote)) != output:
        raise SystemExit("production Git adapter did not publish the exact disposable ref map")
    positive = hashlib.sha256((old + new + result.reason).encode()).hexdigest()
    rejected = root / "rejected.git"
    subprocess.run(["git", "clone", "--bare", "--no-local", str(source), str(rejected)], check=True, stdout=subprocess.DEVNULL)
    # Put the disposable rejection remote back at the approved preimage.
    git(rejected, "update-ref", "refs/heads/main", old); git(rejected, "update-ref", "refs/tags/v1", old)
    hook = rejected / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8"); hook.chmod(0o700)
    rejected_result = publication.publish_repository(manifest, repo=source / ".git", remote_url=str(rejected))
    if rejected_result.outcome != "ambiguous" or publication.advertised_refs(str(rejected)) != selected:
        raise SystemExit("atomic rejection fixture changed a disposable remote ref")
    negative = hashlib.sha256(rejected_result.reason.encode()).hexdigest()
    return positive, negative


def main() -> None:
    manifest = base_manifest()
    evidence = []
    publication.validate_manifest(manifest, frozen_sha="5" * 40, frozen_tree="6" * 40)
    evidence.append("manifest_complete")
    cases = {
        "missing_ref": lambda value: value["output_refs"].pop("refs/tags/v1"),
        "extra_ref": lambda value: value["output_refs"].update({"refs/heads/extra": "a" * 40}),
        "zero_ref": lambda value: value["selected_refs"].update({"refs/heads/main": "0" * 40}),
        "map_digest": lambda value: value.update(selected_refs_sha256="0" * 64),
        "proof_digest": lambda value: value["proof_digests"].update({"commit-map.txt": "f" * 40}),
        "stale_backup": lambda value: value["proof"].update(run_id=value["backup"]["run_id"]),
        "wrong_branch": lambda value: value["controls"].update(publication_branch="main"),
        "unauthorised_suppression": lambda value: value["controls"]["suppress_workflows"].append(".github/workflows/other.yml"),
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
    plan = control_plan(manifest)
    api = MockApi({101: "active", 102: "disabled_manually", publication.MIRROR_WORKFLOW_ID: "disabled_manually"})
    suppression = publication.set_controls(plan, manifest, api, restore=False)
    restoration = publication.set_controls(plan, manifest, api, restore=True)
    if api.states != {101: "active", 102: "disabled_manually", publication.MIRROR_WORKFLOW_ID: "disabled_manually"}:
        raise SystemExit("control restoration did not reproduce the captured state")
    evidence.append("control_suppression_and_restoration")
    drain = publication.wait_for_writers(plan, manifest, api, current_run_id=99, timeout_seconds=0, interval_seconds=0)
    if drain["status"] != "drained" or suppression["operation"] != "suppress" or restoration["operation"] != "restore":
        raise SystemExit("control lifecycle receipt mismatch")
    busy = MockApi(api.states, {101: [{"id": 100, "status": "in_progress", "head_sha": "a" * 40}]})
    evidence.append(expect_failure("active_writer", lambda: publication.wait_for_writers(plan, manifest, busy, current_run_id=99, timeout_seconds=0, interval_seconds=0)))
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
