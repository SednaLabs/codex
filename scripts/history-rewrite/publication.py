#!/usr/bin/env python3
"""Guarded, executable publication for the w13828 history rewrite.

The workflow runs ``preflight`` and ``prepare`` without a write credential.  It
only mints the narrowly scoped publisher App token after those commands have
bound live GitHub API evidence, a fresh recovery receipt, the operator-approved
manifest digest, and a deterministic regeneration.  ``publish`` then performs
one atomic push with an explicit lease for every ref and resolves transport
ambiguity by readback; it never retries a push.
"""
from __future__ import annotations

import argparse
import base64
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol, Sequence


REPOSITORY = "sednalabs/codex"
PUBLICATION_BRANCH = "repair/history-rewrite-publication-w13828"
ENVIRONMENT_NAME = "history-rewrite-publication"
REVIEWER_ID = 55840159
REVIEWER_LOGIN = "GraciousGazelles"
REVIEW_RULE_ID = 65216203
BRANCH_RULE_PROTECTION_ID = 65216204
BRANCH_POLICY_ID = 59660428
MIRROR_WORKFLOW_ID = 250252269
PUBLISHER_APP_ID = 3520391
PUBLISHER_APP_NODE_ID = "A_kwHODOdWjM4ANbeH"
QUEUE_ONLY_RULESET_ID = 20008703
QUEUE_ONLY_RULESET_NAME = "Serialized merge queue for main"
WRITER_WORKFLOWS = {
    ".github/workflows/rust-release.yml": 231747419,
    ".github/workflows/sedna-release.yml": 250252266,
}
PROTECTED_REFS = (
    "refs/heads/main",
    "refs/heads/upstream-main",
    "refs/heads/integration/app-server-delivery-train-20260824",
)
PROOF_FILES = {
    "annotated-tag-ref-transport.json",
    "commit-map.txt",
    "equivalence.txt",
    "filter-repo-ref-map.raw.txt",
    "map-proof.tsv",
    "ref-map.txt",
    "refs-after-staged.txt",
    "refs-after.txt",
    "refs-before-staged.txt",
    "refs-before.txt",
    "refs-final.txt",
    "residual-review.json",
    "tag-proof-summary.json",
    "tag-proof.tsv",
    "tag-signatures.txt",
    "untouched-proof.tsv",
}
ARTIFACT_FILES = PROOF_FILES | {
    "binding.txt",
    "original-to-isolated-ref-map.json",
    "policy.digest",
    "verified-backup.json",
}
OID = re.compile(r"[0-9a-f]{40}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
ACTIVE_RUN_STATES = {"queued", "in_progress", "waiting", "requested", "pending"}


class PublicationError(RuntimeError):
    """A fail-closed publication rejection."""


class Api(Protocol):
    def get(self, path: str) -> object: ...
    def put(self, path: str) -> None: ...
    def post_graphql(self, query: str, variables: Mapping[str, str]) -> object: ...


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def file_digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def load_object(path: Path, label: str = "JSON") -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PublicationError(f"invalid {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise PublicationError(f"{label} must be an object")
    return value


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json(value) + b"\n")


def _positive_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise PublicationError(f"{label} must be a positive integer")
    return value


def _sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or not HEX64.fullmatch(value):
        raise PublicationError(f"{label} must be a complete SHA-256")
    return value


def _valid_ref(name: str) -> bool:
    forbidden = set(" ~^:?*[\\")
    return (
        name.startswith(("refs/heads/", "refs/tags/"))
        and not any(character in forbidden or ord(character) < 32 or ord(character) == 127 for character in name)
        and ".." not in name
        and "@{" not in name
        and "//" not in name
        and not name.endswith(("/", ".", ".lock"))
    )


def ref_map(value: object, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or not value:
        raise PublicationError(f"{label} must be a non-empty object")
    result: dict[str, str] = {}
    for name, oid in value.items():
        if not isinstance(name, str) or not _valid_ref(name):
            raise PublicationError(f"{label} contains invalid ref {name!r}")
        if not isinstance(oid, str) or not OID.fullmatch(oid) or oid == "0" * 40:
            raise PublicationError(f"{label} contains invalid object for {name}")
        result[name] = oid
    return result


def validate_manifest(manifest: Mapping[str, object], *, frozen_sha: str, frozen_tree: str) -> tuple[dict[str, str], dict[str, str]]:
    """Validate the immutable operator input without trusting its evidence claims."""
    if manifest.get("schema") != "history-rewrite-publication-v1" or manifest.get("repository") != REPOSITORY:
        raise PublicationError("manifest schema or repository mismatch")
    if not OID.fullmatch(frozen_sha) or not OID.fullmatch(frozen_tree):
        raise PublicationError("frozen harness identity is not a full object ID")
    if manifest.get("harness_sha") != frozen_sha or manifest.get("harness_tree") != frozen_tree:
        raise PublicationError("manifest harness identity mismatch")
    source_sha = manifest.get("source_sha")
    if not isinstance(source_sha, str) or not OID.fullmatch(source_sha) or source_sha == "0" * 40:
        raise PublicationError("manifest source identity is invalid")
    if manifest.get("tag_signature_ack") is not True:
        raise PublicationError("tag signature acknowledgement is required")
    selected = ref_map(manifest.get("selected_refs"), "selected_refs")
    output = ref_map(manifest.get("output_refs"), "output_refs")
    if set(selected) != set(output) or selected.get("refs/heads/main") != source_sha:
        raise PublicationError("selected/output domain or source main binding mismatch")
    if manifest.get("selected_refs_sha256") != digest(selected) or manifest.get("output_refs_sha256") != digest(output):
        raise PublicationError("selected or output ref proof digest mismatch")
    policy = manifest.get("policy")
    if not isinstance(policy, dict) or policy.get("path") != "scripts/history-rewrite/policy.json":
        raise PublicationError("policy path is not the reviewed production policy")
    _sha256(policy.get("sha256"), "policy sha256")
    proofs = manifest.get("proof_digests")
    if not isinstance(proofs, dict) or set(proofs) != ARTIFACT_FILES:
        raise PublicationError("proof digest domain is incomplete or contains extra files")
    for name, value in proofs.items():
        _sha256(value, f"proof digest for {name}")
    if manifest.get("proof_digests_sha256") != digest(proofs):
        raise PublicationError("proof digest map mismatch")
    backup = manifest.get("backup")
    proof = manifest.get("proof")
    if not isinstance(backup, dict) or not isinstance(proof, dict):
        raise PublicationError("backup and proof run bindings are required")
    backup_run = _positive_int(backup.get("run_id"), "backup run id")
    _positive_int(backup.get("receipt_artifact_id"), "backup receipt artifact id")
    _sha256(backup.get("receipt_artifact_api_digest"), "backup receipt artifact digest")
    proof_run = _positive_int(proof.get("run_id"), "proof run id")
    _positive_int(proof.get("artifact_id"), "proof artifact id")
    _sha256(proof.get("artifact_api_digest"), "proof artifact digest")
    if proof_run <= backup_run:
        raise PublicationError("proof run must follow the fresh same-harness backup run")
    controls = manifest.get("controls")
    if not isinstance(controls, dict):
        raise PublicationError("control binding is required")
    if controls.get("publication_branch") != PUBLICATION_BRANCH or controls.get("environment") != ENVIRONMENT_NAME:
        raise PublicationError("publication branch or environment binding mismatch")
    if controls.get("reviewer_id") != REVIEWER_ID or controls.get("reviewer_login") != REVIEWER_LOGIN:
        raise PublicationError("required reviewer binding mismatch")
    if controls.get("writer_workflows") != WRITER_WORKFLOWS or controls.get("mirror_workflow_id") != MIRROR_WORKFLOW_ID:
        raise PublicationError("mandatory writer or mirror workflow binding mismatch")
    if controls.get("protected_refs") != list(PROTECTED_REFS):
        raise PublicationError("protected ref domain mismatch")
    snapshot = controls.get("protection_snapshot")
    if not isinstance(snapshot, dict) or controls.get("protection_snapshot_sha256") != digest(snapshot):
        raise PublicationError("approved protection snapshot is missing or has the wrong digest")
    validate_protection_snapshot(snapshot)
    return selected, output


def validate_environment(environment: object, policies: object) -> None:
    if not isinstance(environment, dict) or environment.get("name") != ENVIRONMENT_NAME:
        raise PublicationError("live publication environment is missing")
    if environment.get("deployment_branch_policy") != {"protected_branches": False, "custom_branch_policies": True}:
        raise PublicationError("live environment deployment policy mismatch")
    rules = environment.get("protection_rules")
    if not isinstance(rules, list) or {item.get("id") for item in rules if isinstance(item, dict)} != {REVIEW_RULE_ID, BRANCH_RULE_PROTECTION_ID}:
        raise PublicationError("live environment protection rule domain mismatch")
    reviewer_rules = [item for item in rules if isinstance(item, dict) and item.get("id") == REVIEW_RULE_ID and item.get("type") == "required_reviewers"]
    if len(reviewer_rules) != 1 or reviewer_rules[0].get("prevent_self_review") is not False:
        raise PublicationError("live required-review rule mismatch")
    reviewers = reviewer_rules[0].get("reviewers")
    expected = [("User", REVIEWER_ID, REVIEWER_LOGIN)]
    actual = [] if not isinstance(reviewers, list) else [
        (item.get("type"), item.get("reviewer", {}).get("id"), item.get("reviewer", {}).get("login"))
        for item in reviewers if isinstance(item, dict) and isinstance(item.get("reviewer"), dict)
    ]
    if actual != expected:
        raise PublicationError("live environment reviewer identity mismatch")
    if not isinstance(policies, dict) or policies.get("total_count") != 1:
        raise PublicationError("live deployment branch policy count mismatch")
    items = policies.get("branch_policies")
    if not isinstance(items, list) or len(items) != 1:
        raise PublicationError("live deployment branch policy listing mismatch")
    policy = items[0]
    if not isinstance(policy, dict) or (policy.get("id"), policy.get("name"), policy.get("type", "branch")) != (BRANCH_POLICY_ID, PUBLICATION_BRANCH, "branch"):
        raise PublicationError("live deployment branch policy identity mismatch")


BRANCH_PROTECTION_QUERY = """query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    branchProtectionRules(first: 100) {
      totalCount
      nodes {
        id pattern allowsForcePushes isAdminEnforced requiresStatusChecks
        requiredStatusCheckContexts requiresApprovingReviews requiredApprovingReviewCount
        requiresConversationResolution restrictsPushes
        bypassForcePushAllowances(first: 100) {
          totalCount
          nodes { actor { __typename ... on App { id databaseId slug } } }
        }
      }
    }
  }
}"""


def normalize_protection_snapshot(branch_document: object, ruleset_documents: Sequence[object]) -> dict:
    try:
        branch_rules = branch_document["data"]["repository"]["branchProtectionRules"]
        nodes = branch_rules["nodes"]
    except (KeyError, TypeError) as exc:
        raise PublicationError("branch protection GraphQL evidence is malformed") from exc
    if not isinstance(nodes, list) or branch_rules.get("totalCount") != len(nodes) or len(nodes) >= 100:
        raise PublicationError("branch protection GraphQL result is incomplete")
    normalized_rules = []
    for node in nodes:
        if not isinstance(node, dict):
            raise PublicationError("branch protection rule is malformed")
        allowances = node.get("bypassForcePushAllowances")
        allowance_nodes = allowances.get("nodes") if isinstance(allowances, dict) else None
        if not isinstance(allowance_nodes, list) or allowances.get("totalCount") != len(allowance_nodes) or len(allowance_nodes) >= 100:
            raise PublicationError("force-push allowance result is incomplete")
        actors = []
        for allowance in allowance_nodes:
            actor = allowance.get("actor") if isinstance(allowance, dict) else None
            if not isinstance(actor, dict):
                raise PublicationError("force-push allowance actor is malformed")
            actors.append({key: actor.get(key) for key in ("__typename", "id", "databaseId", "slug")})
        normalized_rules.append({
            "id": node.get("id"),
            "pattern": node.get("pattern"),
            "allows_force_pushes": node.get("allowsForcePushes"),
            "is_admin_enforced": node.get("isAdminEnforced"),
            "requires_status_checks": node.get("requiresStatusChecks"),
            "required_status_check_contexts": node.get("requiredStatusCheckContexts"),
            "requires_approving_reviews": node.get("requiresApprovingReviews"),
            "required_approving_review_count": node.get("requiredApprovingReviewCount"),
            "requires_conversation_resolution": node.get("requiresConversationResolution"),
            "restricts_pushes": node.get("restrictsPushes"),
            "bypass_force_push_allowances": sorted(actors, key=lambda item: canonical_json(item)),
        })
    normalized_rules.sort(key=lambda item: (str(item["pattern"]), str(item["id"])))
    rulesets = []
    for document in ruleset_documents:
        if not isinstance(document, dict) or not isinstance(document.get("id"), int):
            raise PublicationError("repository ruleset evidence is malformed")
        rulesets.append({key: document.get(key) for key in ("id", "name", "target", "enforcement", "bypass_actors", "conditions", "rules")})
    rulesets.sort(key=lambda item: item["id"])
    return {
        "schema": "history-rewrite-protection-snapshot-v1",
        "protected_refs": list(PROTECTED_REFS),
        "branch_protection_rules": normalized_rules,
        "repository_rulesets": rulesets,
    }


def validate_protection_snapshot(snapshot: object) -> None:
    if not isinstance(snapshot, dict) or snapshot.get("schema") != "history-rewrite-protection-snapshot-v1" or snapshot.get("protected_refs") != list(PROTECTED_REFS):
        raise PublicationError("protection snapshot schema or protected ref domain mismatch")
    branch_rules = snapshot.get("branch_protection_rules")
    rulesets = snapshot.get("repository_rulesets")
    if not isinstance(branch_rules, list) or not isinstance(rulesets, list):
        raise PublicationError("protection snapshot rule domains are malformed")
    def matches_ref(ref: str, pattern: object) -> bool:
        if pattern == "~ALL":
            return True
        if pattern == "~DEFAULT_BRANCH":
            return ref == "refs/heads/main"
        return isinstance(pattern, str) and fnmatch.fnmatchcase(ref, pattern)

    for ref in PROTECTED_REFS:
        branch = ref.removeprefix("refs/heads/")
        matches = [item for item in branch_rules if isinstance(item, dict) and item.get("pattern") == branch]
        if len(matches) != 1 or matches[0].get("allows_force_pushes") is not True:
            raise PublicationError(f"protected ref lacks an exact temporary force-push rule: {ref}")
        allowances = matches[0].get("bypass_force_push_allowances")
        if not isinstance(allowances, list) or not any(
            isinstance(item, dict)
            and item.get("__typename") == "App"
            and item.get("id") == PUBLISHER_APP_NODE_ID
            and item.get("databaseId") == PUBLISHER_APP_ID
            and isinstance(item.get("slug"), str)
            for item in allowances if isinstance(item, dict)
        ):
            raise PublicationError(f"protected ref lacks the exact publisher App allowance: {ref}")
    for ruleset in rulesets:
        if not isinstance(ruleset, dict) or ruleset.get("target") != "branch" or ruleset.get("enforcement") != "active":
            continue
        conditions = ruleset.get("conditions")
        ref_names = conditions.get("ref_name") if isinstance(conditions, dict) else None
        includes = ref_names.get("include") if isinstance(ref_names, dict) else None
        excludes = ref_names.get("exclude") if isinstance(ref_names, dict) else []
        if includes is None:
            applicable = list(PROTECTED_REFS)
        elif not isinstance(includes, list) or not isinstance(excludes, list):
            raise PublicationError(f"ruleset has malformed ref conditions: {ruleset.get('id')}")
        else:
            applicable = [
                ref for ref in PROTECTED_REFS
                if any(matches_ref(ref, pattern) for pattern in includes)
                and not any(matches_ref(ref, pattern) for pattern in excludes)
            ]
        if not applicable:
            continue
        bypass = ruleset.get("bypass_actors")
        rules = ruleset.get("rules")
        if (
            ruleset.get("id") == QUEUE_ONLY_RULESET_ID
            and ruleset.get("name") == QUEUE_ONLY_RULESET_NAME
            and bypass == []
            and isinstance(rules, list)
            and len(rules) == 1
            and isinstance(rules[0], dict)
            and rules[0].get("type") == "merge_queue"
        ):
            continue
        if not isinstance(bypass, list) or not any(
            isinstance(item, dict)
            and item.get("actor_type") == "Integration"
            and item.get("actor_id") == PUBLISHER_APP_ID
            and item.get("bypass_mode") == "always"
            for item in bypass
        ):
            raise PublicationError(f"applicable ruleset lacks the publisher App bypass: {ruleset.get('id')}")


def protection_snapshot_from_api(api: Api) -> dict:
    branch_document = api.post_graphql(BRANCH_PROTECTION_QUERY, {"owner": "sednalabs", "name": "codex"})
    listing = api.get(f"/repos/{REPOSITORY}/rulesets?includes_parents=true&per_page=100")
    if not isinstance(listing, list) or len(listing) >= 100:
        raise PublicationError("repository ruleset listing is malformed or incomplete")
    documents = [api.get(f"/repos/{REPOSITORY}/rulesets/{item.get('id')}") for item in listing if isinstance(item, dict)]
    if len(documents) != len(listing):
        raise PublicationError("repository ruleset listing contains a malformed identity")
    return normalize_protection_snapshot(branch_document, documents)


def validate_approval(approvals: object) -> None:
    if not isinstance(approvals, list):
        raise PublicationError("workflow approval history is malformed")
    matches = []
    for approval in approvals:
        if not isinstance(approval, dict) or approval.get("state") != "approved":
            continue
        user = approval.get("user")
        environments = approval.get("environments")
        if isinstance(user, dict) and isinstance(environments, list) and any(
            isinstance(item, dict) and item.get("name") == ENVIRONMENT_NAME for item in environments
        ):
            matches.append((user.get("id"), user.get("login")))
    if matches != [(REVIEWER_ID, REVIEWER_LOGIN)]:
        raise PublicationError("exact live environment approval is missing or ambiguous")


def validate_run(run: object, workflow: object, *, run_id: int, harness_sha: str, workflow_path: str) -> None:
    if not isinstance(run, dict) or not isinstance(workflow, dict):
        raise PublicationError("workflow run API evidence is malformed")
    if (run.get("id"), run.get("event"), run.get("status"), run.get("conclusion"), run.get("head_sha")) != (
        run_id, "workflow_dispatch", "completed", "success", harness_sha
    ):
        raise PublicationError("workflow run is not the exact successful same-harness dispatch")
    head_repo = run.get("head_repository")
    if not isinstance(head_repo, dict) or head_repo.get("full_name") != REPOSITORY:
        raise PublicationError("workflow run repository mismatch")
    if workflow.get("id") != run.get("workflow_id") or workflow.get("path") != workflow_path or workflow.get("state") != "active":
        raise PublicationError("workflow identity or live state mismatch")


def _one_artifact(artifacts: object, *, artifact_id: int, name: str, api_digest: str, run_id: int) -> dict:
    if not isinstance(artifacts, dict) or not isinstance(artifacts.get("artifacts"), list):
        raise PublicationError("artifact API listing is malformed")
    matches = [item for item in artifacts["artifacts"] if isinstance(item, dict) and item.get("id") == artifact_id]
    if len(matches) != 1:
        raise PublicationError("exact artifact is absent or duplicated")
    artifact = matches[0]
    if artifact.get("name") != name or artifact.get("expired") is not False or artifact.get("digest") != f"sha256:{api_digest}":
        raise PublicationError("artifact API identity, digest, or expiry mismatch")
    workflow_run = artifact.get("workflow_run")
    if workflow_run is not None and (not isinstance(workflow_run, dict) or workflow_run.get("id") != run_id):
        raise PublicationError("proof artifact is not bound to the exact proof run")
    return artifact


def verify_proof_zip(path: Path, expected: Mapping[str, str], api_digest: str, output: Path) -> None:
    if file_digest(path) != api_digest:
        raise PublicationError("proof artifact download differs from its API digest")
    try:
        with zipfile.ZipFile(path) as archive:
            basenames = [Path(name).name for name in archive.namelist() if not name.endswith("/")]
            members = {Path(name).name: name for name in archive.namelist() if not name.endswith("/")}
            if not ARTIFACT_FILES.issubset(members) or any(basenames.count(name) != 1 for name in ARTIFACT_FILES):
                raise PublicationError("proof artifact lacks the exact proof file domain")
            output.mkdir(parents=True, exist_ok=True)
            for name in sorted(ARTIFACT_FILES):
                info = archive.getinfo(members[name])
                if info.file_size <= 0 or info.file_size > 64 * 1024 * 1024:
                    raise PublicationError(f"proof artifact member size is invalid: {name}")
                raw = archive.read(info)
                if hashlib.sha256(raw).hexdigest() != expected[name]:
                    raise PublicationError(f"proof artifact member digest mismatch: {name}")
                (output / name).write_bytes(raw)
    except (OSError, zipfile.BadZipFile) as exc:
        raise PublicationError(f"invalid proof artifact zip: {exc}") from exc


def protection_snapshot_from_files(api_dir: Path) -> dict:
    branch_document = load_object(api_dir / "branch-protection-rules.json")
    try:
        listing = json.loads((api_dir / "rulesets.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PublicationError(f"invalid repository ruleset listing: {exc}") from exc
    if not isinstance(listing, list) or len(listing) >= 100:
        raise PublicationError("repository ruleset listing is malformed or incomplete")
    documents = []
    for item in listing:
        if not isinstance(item, dict) or not isinstance(item.get("id"), int):
            raise PublicationError("repository ruleset listing contains a malformed identity")
        documents.append(load_object(api_dir / f"ruleset-{item['id']}.json"))
    return normalize_protection_snapshot(branch_document, documents)


def check_writer_documents(documents: Mapping[int, object], *, current_run_id: int) -> dict:
    active = []
    for workflow_id in WRITER_WORKFLOWS.values():
        responses = documents.get(workflow_id)
        if not isinstance(responses, list) or len(responses) != len(ACTIVE_RUN_STATES):
            raise PublicationError("active writer state response domain is incomplete")
        if {item.get("requested_status") for item in responses if isinstance(item, dict)} != ACTIVE_RUN_STATES:
            raise PublicationError("active writer status-filter domain is incomplete")
        for item in responses:
            requested_status = item.get("requested_status") if isinstance(item, dict) else None
            value = item.get("response") if isinstance(item, dict) else None
            runs = value.get("workflow_runs") if isinstance(value, dict) else None
            if not isinstance(runs, list) or value.get("total_count") != len(runs) or len(runs) >= 100:
                raise PublicationError("active writer run listing is malformed or exceeds its bounded state page")
            if any(not isinstance(run, dict) or run.get("status") != requested_status for run in runs):
                raise PublicationError("active writer response contains a run outside its requested status")
            active.extend(
                {"workflow_id": workflow_id, "run_id": run.get("id"), "status": run.get("status"), "head_sha": run.get("head_sha")}
                for run in runs
                if isinstance(run, dict) and run.get("id") != current_run_id and run.get("status") in ACTIVE_RUN_STATES
            )
    if active:
        identities = ",".join(str(item["run_id"]) for item in active)
        raise PublicationError(f"active writer runs require external blocking-watcher drain before a fresh dispatch: {identities}")
    return {"schema": "history-rewrite-writer-check-v1", "active": [], "status": "drained-at-single-read"}


def validate_preflight(manifest: dict, *, frozen_sha: str, frozen_tree: str, manifest_sha256: str, api_dir: Path, output: Path) -> None:
    selected, _ = validate_manifest(manifest, frozen_sha=frozen_sha, frozen_tree=frozen_tree)
    if not HEX64.fullmatch(manifest_sha256) or digest(manifest) != manifest_sha256:
        raise PublicationError("operator-approved manifest digest mismatch")
    validate_environment(load_object(api_dir / "environment.json"), load_object(api_dir / "branch-policies.json"))
    approvals = json.loads((api_dir / "approvals.json").read_text(encoding="utf-8"))
    validate_approval(approvals)
    backup, proof = manifest["backup"], manifest["proof"]
    validate_run(load_object(api_dir / "backup-run.json"), load_object(api_dir / "backup-workflow.json"),
                 run_id=backup["run_id"], harness_sha=frozen_sha, workflow_path=".github/workflows/recovery-snapshot.yml")
    verified = load_object(api_dir / "verified-backup.json", "verified backup receipt")
    expected_selected = {"sha256": digest(selected), "count": len(selected), "bytes": len(canonical_json(selected))}
    if (
        verified.get("schema") != "verified-recovery-snapshot-v1"
        or verified.get("repository") != REPOSITORY
        or verified.get("run_id") != backup["run_id"]
        or verified.get("workflow_code_sha") != frozen_sha
        or verified.get("source_sha") != manifest["source_sha"]
        or verified.get("selected_refs") != expected_selected
        or verified.get("receipt_artifact", {}).get("id") != backup["receipt_artifact_id"]
        or verified.get("receipt_artifact", {}).get("api_digest") != f"sha256:{backup['receipt_artifact_api_digest']}"
    ):
        raise PublicationError("fresh independently verified backup does not match the manifest")
    selected_path = api_dir / "backup-selected-refs.json"
    if selected_path.read_bytes() != canonical_json(selected):
        raise PublicationError("backup selected-ref bytes differ from the approved manifest")
    validate_run(load_object(api_dir / "proof-run.json"), load_object(api_dir / "proof-workflow.json"),
                 run_id=proof["run_id"], harness_sha=frozen_sha, workflow_path=".github/workflows/history-rewrite-candidate.yml")
    _one_artifact(load_object(api_dir / "proof-artifacts.json"), artifact_id=proof["artifact_id"],
                  name=f"history-rewrite-rewrite-{proof['run_id']}", api_digest=proof["artifact_api_digest"], run_id=proof["run_id"])
    proof_output = output / "approved-proof"
    verify_proof_zip(api_dir / "proof.zip", manifest["proof_digests"], proof["artifact_api_digest"], proof_output)
    binding = {}
    for line in (proof_output / "binding.txt").read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1); binding[key] = value
    if binding != {
        "repository": REPOSITORY,
        "workflow_harness_sha": frozen_sha,
        "workflow_harness_tree": frozen_tree,
        "source_sha": manifest["source_sha"],
        "selected_refs_sha256": digest(selected),
        "selected_refs_count": str(len(selected)),
        "selected_refs_bytes": str(len(canonical_json(selected))),
        "backup_run_id": str(backup["run_id"]),
    }:
        raise PublicationError("proof artifact binding differs from the manifest and verified backup")
    if load_object(proof_output / "verified-backup.json") != verified:
        raise PublicationError("proof run and current preflight verified different backup receipts")
    live_protection = protection_snapshot_from_files(api_dir)
    if live_protection != manifest["controls"]["protection_snapshot"]:
        raise PublicationError("live branch protection, ruleset, or publisher App allowance differs from the approved manifest")
    workflow_snapshots = {}
    controls = manifest["controls"]
    expected_ids = set(WRITER_WORKFLOWS.values()) | {MIRROR_WORKFLOW_ID}
    for workflow_id in sorted(expected_ids):
        snapshot = load_object(api_dir / f"workflow-{workflow_id}.json")
        if snapshot.get("id") != workflow_id or not isinstance(snapshot.get("path"), str):
            raise PublicationError("active-writer workflow API identity mismatch")
        workflow_snapshots[str(workflow_id)] = {"id": workflow_id, "path": snapshot["path"], "state": snapshot.get("state")}
    mirror = workflow_snapshots[str(MIRROR_WORKFLOW_ID)]
    if mirror["state"] != "disabled_manually":
        raise PublicationError("mirror workflow is not in its required continuing pause")
    paths = {item["path"]: item for item in workflow_snapshots.values()}
    suppress_plan = []
    for path, workflow_id in WRITER_WORKFLOWS.items():
        item = paths.get(path)
        if item is None or item["id"] != workflow_id or item["state"] not in {"active", "disabled_manually"}:
            raise PublicationError("suppressed workflow live state is unavailable or unsupported")
        suppress_plan.append(item)
    writer_check = check_writer_documents(
        {workflow_id: load_object(api_dir / f"writer-runs-{workflow_id}.json") for workflow_id in WRITER_WORKFLOWS.values()},
        current_run_id=0,
    )
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "manifest.json", manifest)
    write_json(output / "control-plan.json", {
        "schema": "history-rewrite-control-plan-v1",
        "repository": REPOSITORY,
        "manifest_sha256": manifest_sha256,
        "suppression": suppress_plan,
        "writer_workflows": WRITER_WORKFLOWS,
        "mirror": mirror,
        "protection_snapshot_sha256": digest(live_protection),
    })
    write_json(output / "preflight.json", {
        "schema": "history-rewrite-publication-preflight-v1",
        "repository": REPOSITORY,
        "manifest_sha256": manifest_sha256,
        "harness_sha": frozen_sha,
        "harness_tree": frozen_tree,
        "selected_refs_sha256": manifest["selected_refs_sha256"],
        "output_refs_sha256": manifest["output_refs_sha256"],
        "backup_run_id": backup["run_id"],
        "proof_run_id": proof["run_id"],
        "approval": {"id": REVIEWER_ID, "login": REVIEWER_LOGIN},
        "writer_check": writer_check,
        "protection_snapshot_sha256": digest(live_protection),
        "status": "verified-before-app-token",
    })


def git(repo: Path, *args: str, env: Mapping[str, str] | None = None) -> str:
    try:
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True, stderr=subprocess.PIPE, env=env)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "git command failed").strip()[-2000:]
        raise PublicationError(detail) from exc


def advertised_refs(remote_url: str, *, env: Mapping[str, str] | None = None) -> dict[str, str]:
    try:
        raw = subprocess.check_output(
            ["git", "ls-remote", "--refs", remote_url, "refs/heads/*", "refs/tags/*"],
            text=True, stderr=subprocess.PIPE, env=env,
        )
    except subprocess.CalledProcessError as exc:
        raise PublicationError(f"remote advertised-ref read failed: {(exc.stderr or '').strip()[-2000:]}") from exc
    result = {}
    for line in raw.splitlines():
        fields = line.split("\t")
        if len(fields) != 2 or not OID.fullmatch(fields[0]) or not _valid_ref(fields[1]) or fields[1] in result:
            raise PublicationError("remote advertised an invalid or duplicate ref")
        result[fields[1]] = fields[0]
    return result


def _object_types(repo: Path, refs: Mapping[str, str]) -> dict[str, str]:
    result = {}
    for name, oid in refs.items():
        kind = git(repo, "cat-file", "-t", oid).strip()
        if kind not in {"commit", "tag"}:
            raise PublicationError(f"ref object has unsupported type {kind}: {name}")
        result[name] = kind
    return result


def validate_phase_bindings(manifest: dict, *, frozen_sha: str, frozen_tree: str, manifest_sha256: str,
                            preflight_path: Path, control_plan: Path, intent_path: Path | None = None) -> tuple[dict[str, str], dict[str, str]]:
    selected, output = validate_manifest(manifest, frozen_sha=frozen_sha, frozen_tree=frozen_tree)
    if not HEX64.fullmatch(manifest_sha256) or digest(manifest) != manifest_sha256:
        raise PublicationError("phase manifest differs from the externally approved digest")
    preflight = load_object(preflight_path, "durable preflight")
    expected_writer_check = {
        "schema": "history-rewrite-writer-check-v1",
        "active": [],
        "status": "drained-at-single-read",
    }
    if (
        preflight.get("schema") != "history-rewrite-publication-preflight-v1"
        or preflight.get("repository") != REPOSITORY
        or preflight.get("manifest_sha256") != manifest_sha256
        or preflight.get("harness_sha") != frozen_sha
        or preflight.get("harness_tree") != frozen_tree
        or preflight.get("selected_refs_sha256") != manifest["selected_refs_sha256"]
        or preflight.get("output_refs_sha256") != manifest["output_refs_sha256"]
        or preflight.get("backup_run_id") != manifest["backup"]["run_id"]
        or preflight.get("proof_run_id") != manifest["proof"]["run_id"]
        or preflight.get("approval") != {"id": REVIEWER_ID, "login": REVIEWER_LOGIN}
        or preflight.get("writer_check") != expected_writer_check
        or preflight.get("protection_snapshot_sha256") != manifest["controls"]["protection_snapshot_sha256"]
        or preflight.get("status") != "verified-before-app-token"
    ):
        raise PublicationError("durable preflight binding mismatch")
    plan = load_object(control_plan, "control plan")
    validate_control_plan(plan, manifest)
    if intent_path is not None:
        intent = load_object(intent_path, "durable restoration intent")
        expected = {
            "schema": "history-rewrite-restoration-intent-v1",
            "repository": REPOSITORY,
            "manifest_sha256": manifest_sha256,
            "harness_sha": frozen_sha,
            "harness_tree": frozen_tree,
            "preflight_sha256": file_digest(preflight_path),
            "control_plan_sha256": file_digest(control_plan),
            "before_refs_sha256": digest(selected),
            "output_refs_sha256": digest(output),
            "regenerated_proof_digests_sha256": digest(manifest["proof_digests"]),
            "status": "ready-no-write-credential-accessed",
        }
        if intent != expected:
            raise PublicationError("durable restoration intent binding mismatch")
    return selected, output


def prepare_candidate(manifest: dict, *, frozen_sha: str, frozen_tree: str, manifest_sha256: str,
                      preflight_path: Path, remote_url: str, work_root: Path, proof_dir: Path, intent_path: Path,
                      control_plan: Path, fixture_policy: Path | None = None) -> None:
    selected, output = validate_phase_bindings(
        manifest, frozen_sha=frozen_sha, frozen_tree=frozen_tree, manifest_sha256=manifest_sha256,
        preflight_path=preflight_path, control_plan=control_plan,
    )
    if fixture_policy is not None:
        if os.environ.get("HISTORY_REWRITE_PUBLICATION_FIXTURE") != "1" or remote_url.startswith(("http://", "https://")):
            raise PublicationError("fixture policy override is restricted to an explicit local-remote fixture")
        policy = fixture_policy
    else:
        policy = Path(manifest["policy"]["path"])
    if file_digest(policy) != manifest["policy"]["sha256"]:
        raise PublicationError("checked-out production policy digest mismatch")
    before = advertised_refs(remote_url)
    if before != selected:
        raise PublicationError("full remote heads/tags namespace differs from the approved input map")
    if work_root.exists():
        shutil.rmtree(work_root)
    repo, preimage, generated = work_root / "repo.git", work_root / "preimage.git", work_root / "output"
    subprocess.run(["git", "init", "--bare", str(repo)], check=True, stdout=subprocess.DEVNULL)
    mappings = {name: "refs/rewrites/selected/" + hashlib.sha256(name.encode()).hexdigest() for name in selected}
    refspecs = [f"{selected[name]}:{mappings[name]}" for name in sorted(selected)]
    subprocess.run(["git", "-C", str(repo), "fetch", "--atomic", "--no-tags", "--no-write-fetch-head", remote_url, *refspecs], check=True)
    git(repo, "update-ref", "refs/rewrites/source", manifest["source_sha"])
    subprocess.run(["git", "clone", "--mirror", "--no-local", str(repo), str(preimage)], check=True, stdout=subprocess.DEVNULL)
    mapping_path = work_root / "original-to-isolated-ref-map.json"
    write_json(mapping_path, mappings)
    approved_mapping = proof_dir / "original-to-isolated-ref-map.json"
    if (
        not approved_mapping.is_file()
        or file_digest(mapping_path) != manifest["proof_digests"]["original-to-isolated-ref-map.json"]
        or mapping_path.read_bytes() != approved_mapping.read_bytes()
    ):
        raise PublicationError("deterministic original-to-isolated ref mapping differs from the approved proof")
    policy_digest_line = (proof_dir / "policy.digest").read_text(encoding="utf-8").split()
    if not policy_digest_line or policy_digest_line[0] != manifest["policy"]["sha256"]:
        raise PublicationError("approved proof artifact does not bind the exact production policy")
    driver = Path(__file__).with_name("rewrite_candidate.py")
    subprocess.run([
        "python3", str(driver), "apply", "--repo", str(repo), "--preimage", str(preimage),
        "--policy", str(policy), "--source-sha", manifest["source_sha"], "--work", str(work_root),
        "--output", str(generated), "--original-to-isolated-ref-map", str(mapping_path),
    ], check=True)
    actual_output = {name: git(repo, "rev-parse", mappings[name]).strip() for name in selected}
    if actual_output != output:
        raise PublicationError("deterministically regenerated ref map differs from approved output map")
    before_types = _object_types(preimage, selected)
    after_types = _object_types(repo, output)
    if before_types != after_types:
        raise PublicationError("ref object types changed during deterministic regeneration")
    for name in PROOF_FILES:
        candidate = generated / name
        approved = proof_dir / name
        if not candidate.is_file() or not approved.is_file() or file_digest(candidate) != manifest["proof_digests"][name] or candidate.read_bytes() != approved.read_bytes():
            raise PublicationError(f"deterministic regeneration proof mismatch: {name}")
    plan_digest = file_digest(control_plan)
    write_json(intent_path, {
        "schema": "history-rewrite-restoration-intent-v1",
        "repository": REPOSITORY,
        "manifest_sha256": manifest_sha256,
        "harness_sha": frozen_sha,
        "harness_tree": frozen_tree,
        "preflight_sha256": file_digest(preflight_path),
        "control_plan_sha256": plan_digest,
        "before_refs_sha256": digest(before),
        "output_refs_sha256": digest(output),
        "regenerated_proof_digests_sha256": digest(manifest["proof_digests"]),
        "status": "ready-no-write-credential-accessed",
    })


def credential_environment(remote_url: str, token: str | None) -> tuple[dict[str, str], Path | None]:
    env = dict(os.environ)
    if not token or not remote_url.startswith("https://github.com/"):
        return env, None
    root = Path(tempfile.mkdtemp(prefix="history-rewrite-askpass-"))
    helper = root / "askpass.sh"
    helper.write_text("#!/bin/sh\ncase \"$1\" in *Username*) printf '%s\\n' x-access-token;; *) printf '%s\\n' \"$HISTORY_REWRITE_APP_TOKEN\";; esac\n", encoding="utf-8")
    helper.chmod(0o700)
    env.update({"GIT_ASKPASS": str(helper), "GIT_TERMINAL_PROMPT": "0", "HISTORY_REWRITE_APP_TOKEN": token})
    return env, root


@dataclass(frozen=True)
class PublicationResult:
    outcome: str
    reason: str
    after_refs: Mapping[str, str]
    final_state: Mapping[str, object] | None = None


def resolve_push_failure(output: Mapping[str, str], error: PublicationError, readback) -> PublicationResult:
    """Resolve one failed push by authoritative readback, without retrying it."""
    try:
        after = dict(readback())
    except PublicationError as read_error:
        raise PublicationError(f"ambiguous atomic push; authoritative readback failed: {read_error}") from error
    if after != output:
        return PublicationResult("ambiguous", str(error), after)
    return PublicationResult("success", "transport exception resolved by exact full-namespace readback", after)


def publish_repository(manifest: dict, *, frozen_sha: str, frozen_tree: str, manifest_sha256: str,
                       preflight_path: Path, control_plan: Path, intent_path: Path, read_api: Api,
                       current_run_id: int, repo: Path, remote_url: str, token: str | None = None) -> PublicationResult:
    selected, output = validate_phase_bindings(
        manifest, frozen_sha=frozen_sha, frozen_tree=frozen_tree, manifest_sha256=manifest_sha256,
        preflight_path=preflight_path, control_plan=control_plan, intent_path=intent_path,
    )
    final_state = verify_live_publication_state(manifest, read_api, current_run_id=current_run_id)
    env, credential_root = credential_environment(remote_url, token)
    try:
        before = advertised_refs(remote_url, env=env)
        if before != selected:
            raise PublicationError("remote changed after preparation; refusing stale publication")
        _object_types(repo, output)
        args = ["push", "--atomic"]
        args.extend(f"--force-with-lease={name}:{selected[name]}" for name in sorted(selected))
        args.append(remote_url)
        args.extend(f"{output[name]}:{name}" for name in sorted(output))
        try:
            git(repo, *args, env=env)
        except PublicationError as push_error:
            resolved = resolve_push_failure(output, push_error, lambda: advertised_refs(remote_url, env=env))
            if resolved.outcome != "success":
                return PublicationResult(resolved.outcome, resolved.reason, resolved.after_refs, final_state)
            after, reason = dict(resolved.after_refs), resolved.reason
        else:
            after = advertised_refs(remote_url, env=env)
            if after != output:
                raise PublicationError("push returned success but full remote namespace differs from approved output")
            reason = "atomic leased push completed"
        with tempfile.TemporaryDirectory(prefix="history-rewrite-clean-fetch-") as temporary:
            clean = Path(temporary) / "repo.git"
            subprocess.run(["git", "init", "--bare", str(clean)], check=True, stdout=subprocess.DEVNULL)
            refspecs = [f"{name}:refs/verification/{hashlib.sha256(name.encode()).hexdigest()}" for name in sorted(output)]
            subprocess.run(["git", "-C", str(clean), "fetch", "--atomic", "--no-tags", "--no-write-fetch-head", remote_url, *refspecs], check=True, env=env)
            fetched = {
                name: git(clean, "rev-parse", f"refs/verification/{hashlib.sha256(name.encode()).hexdigest()}").strip()
                for name in output
            }
            if fetched != output or _object_types(clean, fetched) != _object_types(repo, output):
                raise PublicationError("clean isolated fetch identity or object-type proof failed")
            git(clean, "fsck", "--full", "--no-reflogs")
        final = advertised_refs(remote_url, env=env)
        if final != output:
            raise PublicationError("remote changed during clean-fetch verification")
        return PublicationResult("success", reason + "; full readback and clean fetch verified", final, final_state)
    finally:
        if credential_root is not None:
            shutil.rmtree(credential_root, ignore_errors=True)


class GitHubApi:
    def __init__(self, token: str, base_url: str = "https://api.github.com"):
        if not token:
            raise PublicationError("GitHub API token is unavailable")
        self.token, self.base_url = token, base_url.rstrip("/")

    def _request(self, method: str, path: str, body: object | None = None) -> object:
        data = None if body is None else canonical_json(body)
        request = urllib.request.Request(
            self.base_url + path,
            method=method,
            data=data,
            headers={"Accept": "application/vnd.github+json", "Content-Type": "application/json", "Authorization": f"Bearer {self.token}", "X-GitHub-Api-Version": "2022-11-28"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
        except urllib.error.URLError as exc:
            raise PublicationError(f"GitHub API {method} failed for {path}: {exc}") from exc
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PublicationError(f"GitHub API returned invalid JSON for {path}") from exc

    def get(self, path: str) -> object:
        return self._request("GET", path)

    def put(self, path: str) -> None:
        self._request("PUT", path)

    def post_graphql(self, query: str, variables: Mapping[str, str]) -> object:
        return self._request("POST", "/graphql", {"query": query, "variables": dict(variables)})


class FixtureApi:
    """File-backed API restricted to the hosted disposable-remote fixture."""
    def __init__(self, state: dict):
        self.state = state

    def get(self, path: str) -> object:
        if path == "/installation":
            return self.state["installation"]
        if path.startswith("/apps/"):
            return self.state["app"]
        if path.startswith(f"/repos/{REPOSITORY}/rulesets?"):
            return self.state["rulesets"]
        if f"/repos/{REPOSITORY}/rulesets/" in path:
            ruleset_id = path.rsplit("/", 1)[1]
            return self.state["ruleset_documents"][ruleset_id]
        parts = path.split("?")[0].split("/")
        workflow_id = parts[6]
        if parts[-1] == "runs":
            return self.state["writer_runs"][workflow_id]
        return self.state["workflows"][workflow_id]

    def put(self, path: str) -> None:
        parts = path.split("/")
        workflow_id, action = parts[6], parts[7]
        workflow = self.state["workflows"].get(workflow_id)
        if not isinstance(workflow, dict) or action not in {"enable", "disable"}:
            raise PublicationError("fixture workflow mutation is malformed")
        workflow["state"] = "active" if action == "enable" else "disabled_manually"

    def post_graphql(self, query: str, variables: Mapping[str, str]) -> object:
        return self.state["branch_protection_document"]


def validate_control_plan(plan: dict, manifest: dict) -> None:
    if (
        plan.get("schema") != "history-rewrite-control-plan-v1"
        or plan.get("repository") != REPOSITORY
        or plan.get("manifest_sha256") != digest(manifest)
        or plan.get("mirror", {}).get("id") != MIRROR_WORKFLOW_ID
        or plan.get("mirror", {}).get("state") != "disabled_manually"
    ):
        raise PublicationError("control plan binding mismatch")
    suppression = plan.get("suppression")
    if not isinstance(suppression, list) or {item.get("path") for item in suppression if isinstance(item, dict)} != set(WRITER_WORKFLOWS):
        raise PublicationError("control plan suppression domain mismatch")
    if (
        len(suppression) != len(WRITER_WORKFLOWS)
        or any(not isinstance(item, dict) or not isinstance(item.get("id"), int) or item["id"] <= 0 or item.get("state") not in {"active", "disabled_manually"} for item in suppression)
        or len({item["id"] for item in suppression}) != len(suppression)
        or {item["path"]: item["id"] for item in suppression} != WRITER_WORKFLOWS
        or plan.get("writer_workflows") != WRITER_WORKFLOWS
        or plan.get("protection_snapshot_sha256") != manifest["controls"]["protection_snapshot_sha256"]
    ):
        raise PublicationError("control plan workflow identity or captured state mismatch")


def set_controls(plan: dict, manifest: dict, api: Api, *, restore: bool) -> dict:
    validate_control_plan(plan, manifest)
    results = []
    for item in plan["suppression"]:
        workflow_id, original = item["id"], item["state"]
        live = api.get(f"/repos/{REPOSITORY}/actions/workflows/{workflow_id}")
        if not isinstance(live, dict) or live.get("id") != workflow_id or live.get("path") != item["path"]:
            raise PublicationError("workflow control readback identity mismatch")
        if not restore and live.get("state") != original:
            raise PublicationError("workflow state changed after the captured preflight")
        target = original if restore else "disabled_manually"
        if target not in {"active", "disabled_manually"}:
            raise PublicationError("workflow control target state is unsupported")
        if live.get("state") != target:
            action = "enable" if target == "active" else "disable"
            api.put(f"/repos/{REPOSITORY}/actions/workflows/{workflow_id}/{action}")
        after = api.get(f"/repos/{REPOSITORY}/actions/workflows/{workflow_id}")
        if not isinstance(after, dict) or after.get("id") != workflow_id or after.get("path") != item["path"] or after.get("state") != target:
            raise PublicationError("workflow control mutation did not reach its exact target state")
        results.append({"id": workflow_id, "path": item["path"], "before": live.get("state"), "after": target})
    mirror = api.get(f"/repos/{REPOSITORY}/actions/workflows/{MIRROR_WORKFLOW_ID}")
    if (
        not isinstance(mirror, dict)
        or mirror.get("id") != MIRROR_WORKFLOW_ID
        or mirror.get("path") != ".github/workflows/sedna-sync-upstream.yml"
        or mirror.get("state") != "disabled_manually"
    ):
        raise PublicationError("mirror workflow pause changed during publication")
    return {"schema": "history-rewrite-control-receipt-v1", "operation": "restore" if restore else "suppress", "results": results, "mirror_state": "disabled_manually"}


def validate_publisher_identity(api: Api) -> dict:
    installation = api.get("/installation")
    if not isinstance(installation, dict) or installation.get("app_id") != PUBLISHER_APP_ID or not isinstance(installation.get("app_slug"), str):
        raise PublicationError("publisher token installation identity mismatch")
    app = api.get(f"/apps/{installation['app_slug']}")
    if not isinstance(app, dict) or app.get("id") != PUBLISHER_APP_ID or app.get("node_id") != PUBLISHER_APP_NODE_ID:
        raise PublicationError("publisher App identity mismatch")
    return {"app_id": PUBLISHER_APP_ID, "app_node_id": PUBLISHER_APP_NODE_ID, "app_slug": installation["app_slug"]}


def restore_from_intent_artifact(*, artifact_zip: Path, artifact_api_json: Path, run_id: int, artifact_id: int,
                                 artifact_api_digest: str, frozen_sha: str, frozen_tree: str, manifest_sha256: str,
                                 api: Api) -> dict:
    metadata = load_object(artifact_api_json, "intent artifact API evidence")
    if (
        metadata.get("id") != artifact_id
        or metadata.get("name") != f"history-rewrite-publication-intent-{run_id}"
        or metadata.get("digest") != f"sha256:{artifact_api_digest}"
        or metadata.get("expired") is not False
        or metadata.get("workflow_run", {}).get("id") != run_id
        or file_digest(artifact_zip) != artifact_api_digest
    ):
        raise PublicationError("uploaded restoration-intent artifact identity or digest mismatch")
    expected_names = {"manifest.json", "control-plan.json", "preflight.json", "publication-restoration-intent.json"}
    try:
        with zipfile.ZipFile(artifact_zip) as archive, tempfile.TemporaryDirectory(prefix="history-rewrite-restore-") as temporary:
            files = [name for name in archive.namelist() if not name.endswith("/")]
            basenames = [Path(name).name for name in files]
            if set(basenames) != expected_names or len(basenames) != len(expected_names):
                raise PublicationError("restoration-intent artifact has an unexpected or duplicate file domain")
            root = Path(temporary)
            for name in files:
                info = archive.getinfo(name)
                if info.file_size <= 0 or info.file_size > 1024 * 1024:
                    raise PublicationError("restoration-intent artifact member size is invalid")
                (root / Path(name).name).write_bytes(archive.read(info))
            manifest = load_object(root / "manifest.json", "manifest")
            validate_phase_bindings(
                manifest, frozen_sha=frozen_sha, frozen_tree=frozen_tree, manifest_sha256=manifest_sha256,
                preflight_path=root / "preflight.json", control_plan=root / "control-plan.json",
                intent_path=root / "publication-restoration-intent.json",
            )
            publisher = validate_publisher_identity(api)
            receipt = set_controls(load_object(root / "control-plan.json"), manifest, api, restore=True)
    except (OSError, zipfile.BadZipFile) as exc:
        raise PublicationError(f"invalid restoration-intent artifact zip: {exc}") from exc
    return {
        "schema": "history-rewrite-independent-restoration-v1",
        "intent_artifact": {"run_id": run_id, "artifact_id": artifact_id, "api_digest": f"sha256:{artifact_api_digest}"},
        "manifest_sha256": manifest_sha256,
        "publisher": publisher,
        "restoration": receipt,
    }


def check_writers_once(api: Api, *, current_run_id: int) -> dict:
    documents = {
        workflow_id: [
            {
                "requested_status": status,
                "response": api.get(f"/repos/{REPOSITORY}/actions/workflows/{workflow_id}/runs?status={status}&per_page=100"),
            }
            for status in sorted(ACTIVE_RUN_STATES)
        ]
        for workflow_id in WRITER_WORKFLOWS.values()
    }
    return check_writer_documents(documents, current_run_id=current_run_id)


def verify_live_publication_state(manifest: dict, api: Api, *, current_run_id: int) -> dict:
    states = {}
    for path, workflow_id in WRITER_WORKFLOWS.items():
        live = api.get(f"/repos/{REPOSITORY}/actions/workflows/{workflow_id}")
        if not isinstance(live, dict) or live.get("id") != workflow_id or live.get("path") != path or live.get("state") != "disabled_manually":
            raise PublicationError(f"mandatory release writer is not exactly paused: {path}")
        states[path] = {"id": workflow_id, "state": "disabled_manually"}
    mirror = api.get(f"/repos/{REPOSITORY}/actions/workflows/{MIRROR_WORKFLOW_ID}")
    if not isinstance(mirror, dict) or mirror.get("id") != MIRROR_WORKFLOW_ID or mirror.get("state") != "disabled_manually":
        raise PublicationError("mirror workflow is not in its required continuing pause")
    writer_check = check_writers_once(api, current_run_id=current_run_id)
    protection = protection_snapshot_from_api(api)
    if protection != manifest["controls"]["protection_snapshot"]:
        raise PublicationError("immediate pre-push protection or publisher App exception readback changed")
    return {
        "schema": "history-rewrite-final-state-v1",
        "writer_workflows": states,
        "mirror": {"id": MIRROR_WORKFLOW_ID, "state": "disabled_manually"},
        "writer_check": writer_check,
        "protection_snapshot_sha256": digest(protection),
    }


def decode_manifest(output: Path, manifest_sha256: str) -> None:
    encoded = os.environ.get("PUBLICATION_MANIFEST_GZIP_B64", "")
    try:
        compressed = base64.b64decode(encoded, validate=True)
        import gzip
        raw = gzip.decompress(compressed)
    except Exception as exc:
        raise PublicationError(f"invalid compressed publication manifest: {exc}") from exc
    if len(raw) > 1024 * 1024:
        raise PublicationError("publication manifest exceeds the 1 MiB bound")
    try:
        value = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PublicationError(f"publication manifest is invalid JSON: {exc}") from exc
    if not isinstance(value, dict) or raw != canonical_json(value):
        raise PublicationError("publication manifest is not canonical UTF-8 JSON")
    if digest(value) != manifest_sha256:
        raise PublicationError("publication manifest input digest mismatch")
    output.write_bytes(raw)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    def phase_arguments(command: argparse.ArgumentParser, *, include_intent: bool) -> None:
        command.add_argument("--frozen-sha", required=True)
        command.add_argument("--frozen-tree", required=True)
        command.add_argument("--manifest-sha256", required=True)
        command.add_argument("--preflight", type=Path, required=True)
        command.add_argument("--control-plan", type=Path, required=True)
        if include_intent:
            command.add_argument("--intent", type=Path, required=True)

    decode = sub.add_parser("decode")
    decode.add_argument("--output", type=Path, required=True); decode.add_argument("--manifest-sha256", required=True)
    preflight = sub.add_parser("preflight")
    preflight.add_argument("manifest", type=Path); preflight.add_argument("--frozen-sha", required=True); preflight.add_argument("--frozen-tree", required=True)
    preflight.add_argument("--manifest-sha256", required=True); preflight.add_argument("--api-dir", type=Path, required=True); preflight.add_argument("--output", type=Path, required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("manifest", type=Path); prepare.add_argument("--remote-url", required=True); prepare.add_argument("--work-root", type=Path, required=True)
    prepare.add_argument("--proof-dir", type=Path, required=True); prepare.add_argument("--intent", type=Path, required=True)
    prepare.add_argument("--fixture-policy", type=Path)
    phase_arguments(prepare, include_intent=False)
    controls = sub.add_parser("controls")
    controls.add_argument("operation", choices=("suppress", "restore")); controls.add_argument("manifest", type=Path)
    controls.add_argument("--receipt", type=Path, required=True); controls.add_argument("--api-url", default="https://api.github.com")
    phase_arguments(controls, include_intent=True)
    publish = sub.add_parser("publish")
    publish.add_argument("manifest", type=Path); publish.add_argument("--repo", type=Path, required=True); publish.add_argument("--remote-url", required=True); publish.add_argument("--receipt", type=Path, required=True)
    publish.add_argument("--current-run-id", type=int, required=True)
    publish.add_argument("--fixture-api", type=Path)
    phase_arguments(publish, include_intent=True)
    restore_artifact = sub.add_parser("restore-intent-artifact")
    restore_artifact.add_argument("--artifact-zip", type=Path, required=True); restore_artifact.add_argument("--artifact-api-json", type=Path, required=True)
    restore_artifact.add_argument("--run-id", type=int, required=True); restore_artifact.add_argument("--artifact-id", type=int, required=True)
    restore_artifact.add_argument("--artifact-api-digest", required=True); restore_artifact.add_argument("--frozen-sha", required=True)
    restore_artifact.add_argument("--frozen-tree", required=True); restore_artifact.add_argument("--manifest-sha256", required=True)
    restore_artifact.add_argument("--receipt", type=Path, required=True); restore_artifact.add_argument("--api-url", default="https://api.github.com")
    restore_artifact.add_argument("--fixture-api", type=Path)
    ns = parser.parse_args()
    try:
        if ns.command == "decode":
            decode_manifest(ns.output, ns.manifest_sha256)
        elif ns.command == "preflight":
            validate_preflight(load_object(ns.manifest, "manifest"), frozen_sha=ns.frozen_sha, frozen_tree=ns.frozen_tree,
                               manifest_sha256=ns.manifest_sha256, api_dir=ns.api_dir, output=ns.output)
        elif ns.command == "prepare":
            prepare_candidate(load_object(ns.manifest, "manifest"), frozen_sha=ns.frozen_sha, frozen_tree=ns.frozen_tree,
                              manifest_sha256=ns.manifest_sha256, preflight_path=ns.preflight,
                              remote_url=ns.remote_url, work_root=ns.work_root,
                              proof_dir=ns.proof_dir, intent_path=ns.intent, control_plan=ns.control_plan,
                              fixture_policy=ns.fixture_policy)
        elif ns.command == "controls":
            manifest, plan = load_object(ns.manifest, "manifest"), load_object(ns.control_plan, "control plan")
            validate_phase_bindings(manifest, frozen_sha=ns.frozen_sha, frozen_tree=ns.frozen_tree,
                                    manifest_sha256=ns.manifest_sha256, preflight_path=ns.preflight,
                                    control_plan=ns.control_plan, intent_path=ns.intent)
            api = GitHubApi(os.environ.get("GH_TOKEN", ""), ns.api_url)
            publisher = validate_publisher_identity(api)
            receipt = set_controls(plan, manifest, api, restore=ns.operation == "restore")
            receipt["publisher"] = publisher
            write_json(ns.receipt, receipt)
        elif ns.command == "publish":
            manifest = load_object(ns.manifest, "manifest")
            try:
                if ns.fixture_api is not None:
                    if os.environ.get("HISTORY_REWRITE_PUBLICATION_FIXTURE") != "1" or ns.remote_url.startswith(("http://", "https://")):
                        raise PublicationError("fixture API is restricted to an explicit local-remote fixture")
                    publisher_api = read_api = FixtureApi(load_object(ns.fixture_api, "fixture API state"))
                else:
                    publisher_api = GitHubApi(os.environ.get("GH_TOKEN", ""))
                    read_api = GitHubApi(os.environ.get("HISTORY_REWRITE_READ_TOKEN", ""))
                publisher = validate_publisher_identity(publisher_api)
                result = publish_repository(
                    manifest, frozen_sha=ns.frozen_sha, frozen_tree=ns.frozen_tree,
                    manifest_sha256=ns.manifest_sha256, preflight_path=ns.preflight,
                    control_plan=ns.control_plan, intent_path=ns.intent, read_api=read_api,
                    current_run_id=ns.current_run_id, repo=ns.repo, remote_url=ns.remote_url,
                    token=os.environ.get("GH_TOKEN"),
                )
                write_json(ns.receipt, {"schema": "history-rewrite-publication-receipt-v1", "outcome": result.outcome,
                                       "reason": result.reason, "manifest_sha256": ns.manifest_sha256, "after_refs": result.after_refs,
                                       "after_refs_sha256": digest(result.after_refs), "final_state": result.final_state,
                                       "publisher": publisher,
                                       "external_administrator_intervention_risk": "controls or refs may still change after final readback; atomic ref leases prevent stale ref updates but do not lock administrative controls"})
                if result.outcome != "success":
                    raise PublicationError("atomic push outcome is ambiguous")
            except PublicationError as exc:
                write_json(ns.receipt, {"schema": "history-rewrite-publication-receipt-v1", "outcome": "failed-or-ambiguous",
                                       "reason_sha256": hashlib.sha256(str(exc).encode()).hexdigest(), "manifest_sha256": ns.manifest_sha256})
                raise
        else:
            if ns.fixture_api is not None:
                if os.environ.get("HISTORY_REWRITE_PUBLICATION_FIXTURE") != "1":
                    raise PublicationError("restoration fixture API requires the explicit fixture boundary")
                api = FixtureApi(load_object(ns.fixture_api, "fixture API state"))
            else:
                api = GitHubApi(os.environ.get("GH_TOKEN", ""), ns.api_url)
            receipt = restore_from_intent_artifact(
                artifact_zip=ns.artifact_zip, artifact_api_json=ns.artifact_api_json,
                run_id=ns.run_id, artifact_id=ns.artifact_id, artifact_api_digest=ns.artifact_api_digest,
                frozen_sha=ns.frozen_sha, frozen_tree=ns.frozen_tree,
                manifest_sha256=ns.manifest_sha256, api=api,
            )
            write_json(ns.receipt, receipt)
    except PublicationError as exc:
        print(f"history rewrite publication: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
