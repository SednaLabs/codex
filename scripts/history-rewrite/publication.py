#!/usr/bin/env python3
"""Guarded, executable publication for the w13828 history rewrite.

The workflow prepares a candidate-only bundle without maintenance or a write
credential. Its protected handoff then binds actual administrator approval,
independent observable controls, the recovery receipt and approved manifest
before minting the narrowly scoped publisher App token. ``publish`` performs
one atomic push with an explicit lease for every ref and resolves transport
ambiguity by readback; it never retries a push.
"""
from __future__ import annotations

import argparse
import base64
import copy
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
from datetime import datetime, timedelta, timezone
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
PUBLISHER_APP_SLUG = "sedna-release-publisher"
PUBLISHER_INSTALLATION_ID = 127511662
PUBLISHER_PERMISSIONS = {"actions": "write", "contents": "write", "metadata": "read"}
PUBLISHER_VIEWER_QUERY = "query { viewer { login } }"
PUBLISHER_REPOSITORIES_PATH = "/installation/repositories?per_page=100"
OBSERVER_APP_ID = 4838068
OBSERVER_APP_NODE_ID = "A_kwHODOdWjM4ASdK0"
OBSERVER_APP_SLUG = "sedna-codex-delivery-coordinator"
OBSERVER_INSTALLATION_ID = 159211338
REPOSITORY_ID = 1152496647
OBSERVER_PERMISSIONS = {"administration": "read", "metadata": "read"}
OBSERVER_APP_GRANTS = {name: "read" for name in (
    "actions", "administration", "checks", "contents", "merge_queues", "metadata", "pull_requests", "statuses",
)}
OBSERVER_VIEWER_QUERY = "query { viewer { login } }"
OBSERVER_REPOSITORIES_PATH = "/installation/repositories?per_page=100"
PUBLISHER_ACTOR = {"__typename": "App", "id": PUBLISHER_APP_NODE_ID,
                   "databaseId": PUBLISHER_APP_ID, "slug": "sedna-release-publisher"}
QUEUE_MAINTENANCE_ACTORS = [{"actor_id": PUBLISHER_APP_ID, "actor_type": "Integration", "bypass_mode": "always"}]
PROTECTION_RULE_IDS = {
    "main": "BPR_kwDORLG0B84Eb5kA",
    "upstream-main": "BPR_kwDORLG0B84Eb5Z4",
    "integration/app-server-delivery-train-20260824": "BPR_kwDORLG0B84E5XWA",
}
QUEUE_ONLY_RULESET_ID = 20008703
QUEUE_ONLY_RULESET_NAME = "Serialized merge queue for main"
QUEUE_ONLY_RULESET_CONDITIONS = {"ref_name": {"exclude": [], "include": ["refs/heads/main"]}}
QUEUE_ONLY_RULESET_RULES = [{"type": "merge_queue", "parameters": {
    "merge_method": "SQUASH",
    "max_entries_to_build": 4,
    "min_entries_to_merge": 1,
    "max_entries_to_merge": 1,
    "min_entries_to_merge_wait_minutes": 0,
    "grouping_strategy": "ALLGREEN",
    "check_response_timeout_minutes": 180,
}}]
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
    plan = controls.get("maintenance_plan")
    if not isinstance(plan, dict) or controls.get("maintenance_plan_sha256") != digest(plan):
        raise PublicationError("operator maintenance plan or digest is missing")
    expected_plan = plan_maintenance(plan.get("administrator_before"), plan.get("read_token_before"))
    if plan != expected_plan or snapshot != plan["read_token_after"]:
        raise PublicationError("maintenance plan changes more than the exact approved transition")
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
        requiredStatusCheckContexts requiredStatusChecks { context app { databaseId slug } }
        requiresStrictStatusChecks requiresApprovingReviews requiredApprovingReviewCount
        requiresConversationResolution restrictsPushes requiresCommitSignatures
        requiresLinearHistory lockBranch requiresDeployments requiredDeploymentEnvironments
        requireLastPushApproval requiresCodeOwnerReviews allowsDeletions blocksCreations
        bypassForcePushAllowances(first: 100) {
          totalCount
          nodes { actor { __typename ... on App { id databaseId slug } } }
        }
        bypassPullRequestAllowances(first: 100) {
          totalCount nodes { actor { __typename ... on App { id databaseId slug } } }
        }
        pushAllowances(first: 100) {
          totalCount nodes { actor { __typename ... on App { id databaseId slug } } }
        }
      }
    }
  }
}"""


def normalize_protection_snapshot(branch_document: object, ruleset_documents: Sequence[object]) -> dict:
    if isinstance(branch_document, dict) and branch_document.get("errors"):
        paths = [item.get("path") for item in branch_document["errors"] if isinstance(item, dict)]
        raise PublicationError(f"branch protection GraphQL returned partial/error evidence at paths: {paths}")
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
        actors = {}
        for field in ("bypassForcePushAllowances", "bypassPullRequestAllowances", "pushAllowances"):
            allowances = node.get(field)
            allowance_nodes = allowances.get("nodes") if isinstance(allowances, dict) else None
            if not isinstance(allowance_nodes, list) or allowances.get("totalCount") != len(allowance_nodes) or len(allowance_nodes) >= 100:
                raise PublicationError(f"{field} result is incomplete")
            values = []
            for allowance in allowance_nodes:
                actor = allowance.get("actor") if isinstance(allowance, dict) else None
                if not isinstance(actor, dict):
                    raise PublicationError(f"{field} actor is malformed")
                values.append({key: actor.get(key) for key in ("__typename", "id", "databaseId", "slug")})
            actors[field] = sorted(values, key=canonical_json)
        normalized_rules.append({
            "id": node.get("id"),
            "pattern": node.get("pattern"),
            "allows_force_pushes": node.get("allowsForcePushes"),
            "is_admin_enforced": node.get("isAdminEnforced"),
            "requires_status_checks": node.get("requiresStatusChecks"),
            "required_status_check_contexts": node.get("requiredStatusCheckContexts"),
            "required_status_checks": node.get("requiredStatusChecks"),
            "requires_strict_status_checks": node.get("requiresStrictStatusChecks"),
            "requires_approving_reviews": node.get("requiresApprovingReviews"),
            "required_approving_review_count": node.get("requiredApprovingReviewCount"),
            "requires_conversation_resolution": node.get("requiresConversationResolution"),
            "restricts_pushes": node.get("restrictsPushes"),
            "requires_commit_signatures": node.get("requiresCommitSignatures"),
            "requires_linear_history": node.get("requiresLinearHistory"),
            "lock_branch": node.get("lockBranch"),
            "requires_deployments": node.get("requiresDeployments"),
            "required_deployment_environments": node.get("requiredDeploymentEnvironments"),
            "require_last_push_approval": node.get("requireLastPushApproval"),
            "requires_code_owner_reviews": node.get("requiresCodeOwnerReviews"),
            "allows_deletions": node.get("allowsDeletions"),
            "blocks_creations": node.get("blocksCreations"),
            "bypass_force_push_allowances": actors["bypassForcePushAllowances"],
            "bypass_pull_request_allowances": actors["bypassPullRequestAllowances"],
            "push_allowances": actors["pushAllowances"],
        })
    normalized_rules.sort(key=lambda item: (str(item["pattern"]), str(item["id"])))
    rulesets = []
    for document in ruleset_documents:
        if not isinstance(document, dict) or not isinstance(document.get("id"), int):
            raise PublicationError("repository ruleset evidence is malformed")
        if "bypass_actors" in document:
            bypass_actors = document["bypass_actors"]
            if not isinstance(bypass_actors, list):
                raise PublicationError("visible repository ruleset bypass actors are malformed")
            bypass_visibility = "visible"
            bypass_actors = sorted(bypass_actors, key=lambda item: canonical_json(item))
        else:
            bypass_visibility = "not_returned"
            bypass_actors = None
        rulesets.append({
            **{key: document.get(key) for key in ("id", "name", "target", "enforcement", "conditions", "rules")},
            "bypass_actors_visibility": bypass_visibility,
            "bypass_actors": bypass_actors,
        })
    rulesets.sort(key=lambda item: item["id"])
    return {
        "schema": "history-rewrite-protection-snapshot-v2",
        "protected_refs": list(PROTECTED_REFS),
        "branch_protection_rules": normalized_rules,
        "repository_rulesets": rulesets,
    }


def validate_snapshot_shape(snapshot: object) -> None:
    if not isinstance(snapshot, dict) or snapshot.get("schema") != "history-rewrite-protection-snapshot-v2" or snapshot.get("protected_refs") != list(PROTECTED_REFS):
        raise PublicationError("protection snapshot schema or protected ref domain mismatch")
    branch_rules = snapshot.get("branch_protection_rules")
    rulesets = snapshot.get("repository_rulesets")
    if not isinstance(branch_rules, list) or not isinstance(rulesets, list):
        raise PublicationError("protection snapshot rule domains are malformed")
    if len(branch_rules) != len(PROTECTION_RULE_IDS) or {item.get("pattern") for item in branch_rules if isinstance(item, dict)} != set(PROTECTION_RULE_IDS):
        raise PublicationError("classic protection rule domain differs from the admitted three branches")
    for rule in branch_rules:
        if rule.get("id") != PROTECTION_RULE_IDS[rule["pattern"]]:
            raise PublicationError("classic protection rule identity changed")
        for name in ("allows_force_pushes", "is_admin_enforced", "requires_status_checks", "requires_strict_status_checks",
                     "requires_approving_reviews", "requires_conversation_resolution", "restricts_pushes",
                     "requires_commit_signatures", "requires_linear_history", "lock_branch", "requires_deployments",
                     "require_last_push_approval", "requires_code_owner_reviews", "allows_deletions", "blocks_creations"):
            if type(rule.get(name)) is not bool:
                raise PublicationError(f"missing or malformed protection field: {name}")
        for name in ("required_status_check_contexts", "required_status_checks", "required_deployment_environments",
                     "bypass_force_push_allowances", "bypass_pull_request_allowances", "push_allowances"):
            if not isinstance(rule.get(name), list):
                raise PublicationError(f"missing protection list: {name}")
        if rule.get("required_approving_review_count") is not None and type(rule["required_approving_review_count"]) is not int:
            raise PublicationError("review count is malformed")
        contexts = rule["required_status_check_contexts"]
        if any(not isinstance(context, str) or not context for context in contexts) or len(contexts) != len(set(contexts)):
            raise PublicationError("required check contexts are malformed or duplicated")
        check_contexts = []
        for check in rule["required_status_checks"]:
            if not isinstance(check, dict) or set(check) != {"context", "app"} or not isinstance(check["context"], str) or not check["context"]:
                raise PublicationError("required check source identity is malformed")
            app = check["app"]
            if not isinstance(app, dict) or set(app) != {"databaseId", "slug"} or type(app["databaseId"]) is not int or app["databaseId"] <= 0 or not isinstance(app["slug"], str) or not app["slug"]:
                raise PublicationError("required check App identity is missing or malformed")
            check_contexts.append(check["context"])
        if len(check_contexts) != len(set(check_contexts)) or set(check_contexts) != set(contexts):
            raise PublicationError("required checks and contexts must have the same complete, unique domain")
    ids = [item.get("id") for item in rulesets if isinstance(item, dict)]
    if len(ids) != len(rulesets) or len(set(ids)) != len(ids) or any(type(value) is not int for value in ids):
        raise PublicationError("ruleset identity domain is malformed")
    for item in rulesets:
        if not isinstance(item.get("rules"), list) or item.get("target") not in {"branch", "tag", "push"} or item.get("enforcement") not in {"active", "disabled", "evaluate"}:
            raise PublicationError("ruleset semantics are missing")
        if not ((item.get("bypass_actors_visibility") == "visible" and isinstance(item.get("bypass_actors"), list)) or
                (item.get("bypass_actors_visibility") == "not_returned" and item.get("bypass_actors") is None)):
            raise PublicationError("ruleset actor visibility is contradictory")


def validate_protection_snapshot(snapshot: object) -> None:
    validate_snapshot_shape(snapshot)
    branch_rules, rulesets = snapshot["branch_protection_rules"], snapshot["repository_rulesets"]
    def matches_ref(ref: str, pattern: object) -> bool:
        if pattern == "~ALL":
            return True
        if pattern == "~DEFAULT_BRANCH":
            return ref == "refs/heads/main"
        return isinstance(pattern, str) and fnmatch.fnmatchcase(ref, pattern)

    for ref in PROTECTED_REFS:
        branch = ref.removeprefix("refs/heads/")
        matches = [item for item in branch_rules if isinstance(item, dict) and item.get("pattern") == branch]
        # The general force-push flag and actor-specific exceptions are distinct.
        # Only the named publisher exception is admitted, never general access.
        if len(matches) != 1 or matches[0].get("allows_force_pushes") is not False:
            raise PublicationError(f"protected ref permits general force pushes: {ref}")
        allowances = matches[0].get("bypass_force_push_allowances")
        rule = matches[0]
        if allowances != [PUBLISHER_ACTOR] or rule["push_allowances"] != [PUBLISHER_ACTOR] or rule["restricts_pushes"] is not True:
            raise PublicationError(f"protected ref lacks the exact publisher App allowance: {ref}")
        if rule["requires_status_checks"] or rule["required_status_checks"] or rule["required_status_check_contexts"]:
            raise PublicationError(f"rewritten commits cannot satisfy retained normal status gates: {ref}")
        review_required = branch != "upstream-main"
        if rule["requires_approving_reviews"] is not review_required or rule["bypass_pull_request_allowances"] != ([PUBLISHER_ACTOR] if review_required else []):
            raise PublicationError(f"exact App-only pull request exception is missing: {ref}")
        for field in ("requires_commit_signatures", "requires_linear_history", "lock_branch", "requires_deployments", "allows_deletions"):
            if rule[field]:
                raise PublicationError(f"unadmitted maintenance restriction or deletion permission: {field}")
    for ruleset in rulesets:
        if ruleset.get("enforcement") != "active":
            continue
        if ruleset.get("target") != "branch":
            raise PublicationError("active tag/push rules require a separate publication authority decision")
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
        bypass_visibility = ruleset.get("bypass_actors_visibility")
        rules = ruleset.get("rules")
        if (
            ruleset.get("id") == QUEUE_ONLY_RULESET_ID
            and ruleset.get("name") == QUEUE_ONLY_RULESET_NAME
            and ruleset.get("conditions") == QUEUE_ONLY_RULESET_CONDITIONS
            and rules == QUEUE_ONLY_RULESET_RULES
            and (
                (bypass_visibility == "visible" and bypass == QUEUE_MAINTENANCE_ACTORS)
                or (bypass_visibility == "not_returned" and bypass is None)
            )
        ):
            continue
        raise PublicationError(f"unexpected applicable active ruleset requires an explicit future authority decision: {ruleset.get('id')}")
    if not any(item["id"] == QUEUE_ONLY_RULESET_ID and item["enforcement"] == "active" for item in rulesets):
        raise PublicationError("the main queue ruleset must remain active during maintenance")


def project_actor_visibility(administrator: dict, observation: dict) -> dict:
    """Project only a documented hidden field; never infer an empty actor list."""
    validate_snapshot_shape(administrator); validate_snapshot_shape(observation)
    result = copy.deepcopy(administrator)
    observed = {item["id"]: item for item in observation["repository_rulesets"]}
    if set(observed) != {item["id"] for item in administrator["repository_rulesets"]}:
        raise PublicationError("administrator and read-token ruleset domains differ")
    for item in result["repository_rulesets"]:
        if item["bypass_actors_visibility"] != "visible":
            raise PublicationError("administrator actor evidence is not complete")
        if observed[item["id"]]["bypass_actors_visibility"] == "not_returned":
            item.update(bypass_actors_visibility="not_returned", bypass_actors=None)
    return result


def plan_maintenance(administrator_before: object, read_token_before: object) -> dict:
    """Build expected state and exact rollback evidence without changing GitHub."""
    validate_snapshot_shape(administrator_before); validate_snapshot_shape(read_token_before)
    if project_actor_visibility(administrator_before, read_token_before) != read_token_before:
        raise PublicationError("principals disagree on observable pre-maintenance protection state")
    after = copy.deepcopy(administrator_before)
    for rule in after["branch_protection_rules"]:
        if rule["allows_force_pushes"] or any(rule[key] for key in ("bypass_force_push_allowances", "bypass_pull_request_allowances", "push_allowances")) or rule["restricts_pushes"]:
            raise PublicationError("maintenance preimage differs from the admitted actor/force-push baseline")
        rule.update(allows_force_pushes=False, restricts_pushes=True,
                    bypass_force_push_allowances=[copy.deepcopy(PUBLISHER_ACTOR)], push_allowances=[copy.deepcopy(PUBLISHER_ACTOR)])
        if rule["pattern"] != "upstream-main":
            # Shape validation has already proved a one-to-one correspondence;
            # never filter or collapse entries before admitting their removal.
            if (rule["requires_status_checks"] is not True
                    or set(rule["required_status_check_contexts"]) != {"CI required", "CodeQL required gate"}
                    or any(check["app"] != {"databaseId": 15368, "slug": "github-actions"}
                           for check in rule["required_status_checks"])):
                raise PublicationError("normal status gate/source differs from the admitted maintenance baseline")
            # GitHub's observed disabled-check representation reports strict=true
            # (REST disables checks with null; GET omits them). Keep the raw value:
            # do not erase it in normalization or accept arbitrary after-state.
            # The exact live apply/read/restore transition must qualify this
            # representation; it is not a guarantee for every provider version.
            rule.update(requires_status_checks=False, requires_strict_status_checks=True,
                        required_status_checks=[], required_status_check_contexts=[],
                        bypass_pull_request_allowances=[copy.deepcopy(PUBLISHER_ACTOR)])
    queue = [item for item in after["repository_rulesets"] if item["id"] == QUEUE_ONLY_RULESET_ID]
    if len(queue) != 1 or queue[0]["bypass_actors"] != []:
        raise PublicationError("queue bypass preimage differs from the admitted empty baseline")
    queue[0]["bypass_actors"] = copy.deepcopy(QUEUE_MAINTENANCE_ACTORS)
    validate_protection_snapshot(after)
    return {
        "schema": "history-rewrite-maintenance-plan-v1", "repository": REPOSITORY,
        "administrator_before": administrator_before, "administrator_after": after,
        "read_token_before": read_token_before,
        "read_token_after": project_actor_visibility(after, read_token_before),
        "administrator_readback": "required immediately after the separately authorized control mutation and after exact rollback",
        "authority": "expected-state proposal only; the operator must approve the exact manifest and maintenance exception",
    }


def validate_observer_identity(api: Api) -> dict:
    viewer = api.post_graphql(OBSERVER_VIEWER_QUERY, {})
    data = viewer.get("data") if isinstance(viewer, dict) else None
    actor = data.get("viewer") if isinstance(data, dict) else None
    if (not isinstance(viewer, dict) or viewer.get("errors") or not isinstance(actor, dict)
            or actor.get("login") != f"{OBSERVER_APP_SLUG}[bot]"):
        raise PublicationError("protection observer is not the expected authenticated App bot")
    app = api.get(f"/apps/{OBSERVER_APP_SLUG}")
    if (not isinstance(app, dict) or app.get("id") != OBSERVER_APP_ID
            or app.get("node_id") != OBSERVER_APP_NODE_ID or app.get("slug") != OBSERVER_APP_SLUG
            or app.get("permissions") != OBSERVER_APP_GRANTS):
        raise PublicationError("protection observer App identity or read-only grant ceiling changed")
    selected = api.get(OBSERVER_REPOSITORIES_PATH)
    repositories = selected.get("repositories") if isinstance(selected, dict) else None
    if (not isinstance(selected, dict) or type(selected.get("total_count")) is not int or selected["total_count"] != 1
            or not isinstance(repositories, list) or len(repositories) != 1
            or not isinstance(repositories[0], dict) or repositories[0].get("id") != REPOSITORY_ID
            or repositories[0].get("full_name") != REPOSITORY):
        raise PublicationError("protection observer token is not scoped to exactly the selected repository")
    return {"schema": "history-rewrite-protection-observer-v1", "app_id": OBSERVER_APP_ID,
            "app_slug": OBSERVER_APP_SLUG, "installation_id": OBSERVER_INSTALLATION_ID,
            "repository": REPOSITORY, "repository_id": REPOSITORY_ID,
            "authenticated_login": viewer["data"]["viewer"]["login"],
            "requested_token_permissions": OBSERVER_PERMISSIONS,
            "token_scope_source": "pinned token action with explicit repository and permission inputs; authenticated bot and repository readback",
            "app_grants": app["permissions"]}


def protection_snapshot_from_api(api: Api, *, api_dir: Path | None = None) -> dict:
    branch_document = api.post_graphql(BRANCH_PROTECTION_QUERY, {"owner": "sednalabs", "name": "codex"})
    if api_dir is not None:
        api_dir.mkdir(parents=True, exist_ok=True)
        write_json(api_dir / "branch-protection-rules.json", branch_document)
    if isinstance(branch_document, dict) and branch_document.get("errors"):
        # Partial data is not an empty inventory. Do not retry with another
        # principal or promote a narrower query into complete control evidence.
        raise PublicationError(
            "classic protection inventory is unavailable to the observation principal; "
            "a separately authorized protection-read observer is required; no credential fallback was attempted"
        )
    listing = api.get(f"/repos/{REPOSITORY}/rulesets?includes_parents=true&per_page=100")
    if not isinstance(listing, list) or len(listing) >= 100:
        raise PublicationError("repository ruleset listing is malformed or incomplete")
    documents = [api.get(f"/repos/{REPOSITORY}/rulesets/{item.get('id')}") for item in listing if isinstance(item, dict)]
    if len(documents) != len(listing):
        raise PublicationError("repository ruleset listing contains a malformed identity")
    result = normalize_protection_snapshot(branch_document, documents)
    if api_dir is not None:
        api_dir.mkdir(parents=True, exist_ok=True)
        write_json(api_dir / "branch-protection-rules.json", branch_document)
        write_json(api_dir / "rulesets.json", listing)
        for document in documents:
            write_json(api_dir / f"ruleset-{document['id']}.json", document)
    return result


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


def validate_preparation(manifest: dict, *, frozen_sha: str, frozen_tree: str, manifest_sha256: str, api_dir: Path, output: Path) -> None:
    """Validate immutable inputs without asserting live maintenance or approval."""
    selected, _ = validate_manifest(manifest, frozen_sha=frozen_sha, frozen_tree=frozen_tree)
    if not HEX64.fullmatch(manifest_sha256) or digest(manifest) != manifest_sha256:
        raise PublicationError("operator-approved manifest digest mismatch")
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
    write_json(output / "preparation.json", preparation_binding(manifest))


def preparation_binding(manifest: dict) -> dict:
    return {"schema": "history-rewrite-preparation-v1", "repository": REPOSITORY,
            "manifest_sha256": digest(manifest), "harness_sha": manifest["harness_sha"],
            "harness_tree": manifest["harness_tree"], "selected_refs_sha256": manifest["selected_refs_sha256"],
            "output_refs_sha256": manifest["output_refs_sha256"], "proof_digests_sha256": digest(manifest["proof_digests"])}


def validate_preflight(manifest: dict, *, frozen_sha: str, frozen_tree: str, manifest_sha256: str, api_dir: Path, output: Path) -> None:
    validate_preparation(manifest, frozen_sha=frozen_sha, frozen_tree=frozen_tree,
                         manifest_sha256=manifest_sha256, api_dir=api_dir, output=output)
    validate_environment(load_object(api_dir / "environment.json"), load_object(api_dir / "branch-policies.json"))
    validate_approval(json.loads((api_dir / "approvals.json").read_text(encoding="utf-8")))
    live_protection = protection_snapshot_from_files(api_dir)
    if live_protection != manifest["controls"]["protection_snapshot"]:
        raise PublicationError("live branch protection, ruleset, or publisher App allowance differs from the approved manifest")
    record_live_preflight(manifest, api_dir=api_dir, output=output)


def record_live_preflight(manifest: dict, *, api_dir: Path, output: Path) -> None:
    """Called only after a complete protection/approval gate, never by preparation."""
    manifest_sha256 = digest(manifest)
    protection_digest = manifest["controls"]["protection_snapshot_sha256"]
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
        "protection_snapshot_sha256": protection_digest,
    })
    write_json(output / "preflight.json", {
        "schema": "history-rewrite-publication-preflight-v1",
        "repository": REPOSITORY,
        "manifest_sha256": manifest_sha256,
        "harness_sha": manifest["harness_sha"],
        "harness_tree": manifest["harness_tree"],
        "selected_refs_sha256": manifest["selected_refs_sha256"],
        "output_refs_sha256": manifest["output_refs_sha256"],
        "backup_run_id": manifest["backup"]["run_id"],
        "proof_run_id": manifest["proof"]["run_id"],
        "approval": {"id": REVIEWER_ID, "login": REVIEWER_LOGIN},
        "writer_check": writer_check,
        "protection_snapshot_sha256": protection_digest,
        "status": "verified-before-publisher-token",
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
        or preflight.get("status") != "verified-before-publisher-token"
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
    regenerate_candidate(manifest, remote_url=remote_url, work_root=work_root,
                         proof_dir=proof_dir, fixture_policy=fixture_policy)
    write_restoration_intent(manifest, preflight_path=preflight_path, control_plan=control_plan, intent_path=intent_path)


def regenerate_candidate(manifest: dict, *, remote_url: str, work_root: Path, proof_dir: Path,
                         fixture_policy: Path | None = None) -> None:
    selected, output = manifest["selected_refs"], manifest["output_refs"]
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


def write_restoration_intent(manifest: dict, *, preflight_path: Path, control_plan: Path, intent_path: Path) -> None:
    write_json(intent_path, {
        "schema": "history-rewrite-restoration-intent-v1",
        "repository": REPOSITORY,
        "manifest_sha256": digest(manifest),
        "harness_sha": manifest["harness_sha"],
        "harness_tree": manifest["harness_tree"],
        "preflight_sha256": file_digest(preflight_path),
        "control_plan_sha256": file_digest(control_plan),
        "before_refs_sha256": manifest["selected_refs_sha256"],
        "output_refs_sha256": manifest["output_refs_sha256"],
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
                       preflight_path: Path, control_plan: Path, intent_path: Path, read_api: Api, observer_api: Api,
                       current_run_id: int, repo: Path, remote_url: str, token: str | None = None,
                       handoff: dict | None = None) -> PublicationResult:
    selected, output = validate_phase_bindings(
        manifest, frozen_sha=frozen_sha, frozen_tree=frozen_tree, manifest_sha256=manifest_sha256,
        preflight_path=preflight_path, control_plan=control_plan, intent_path=intent_path,
    )
    final_state = verify_live_publication_state(manifest, read_api, observer_api=observer_api,
                                               current_run_id=current_run_id, handoff=handoff)
    env, credential_root = credential_environment(remote_url, token)
    try:
        before = advertised_refs(remote_url, env=env)
        if before != selected:
            raise PublicationError("remote changed after preparation; refusing stale publication")
        _object_types(repo, output)
        if handoff is not None:
            from protected_handoff import require_fresh_gate
            require_fresh_gate(handoff, manifest)
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


def observer_api_from_environment() -> GitHubApi:
    token = os.environ.get("HISTORY_REWRITE_OBSERVER_TOKEN", "")
    if not token or token in {os.environ.get("GH_TOKEN"), os.environ.get("HISTORY_REWRITE_READ_TOKEN")}:
        raise PublicationError("a distinct protection observer token is required; no credential fallback is allowed")
    if (os.environ.get("HISTORY_REWRITE_OBSERVER_INSTALLATION_ID") != str(OBSERVER_INSTALLATION_ID)
            or os.environ.get("HISTORY_REWRITE_OBSERVER_APP_SLUG") != OBSERVER_APP_SLUG):
        raise PublicationError("pinned token-action observer installation or App output mismatched")
    return GitHubApi(token)


def publisher_api_from_environment(api_url: str = "https://api.github.com") -> GitHubApi:
    token = os.environ.get("GH_TOKEN", "")
    if not token or token in {os.environ.get("HISTORY_REWRITE_OBSERVER_TOKEN"), os.environ.get("HISTORY_REWRITE_READ_TOKEN")}:
        raise PublicationError("a distinct publisher token is required; no credential fallback is allowed")
    if (os.environ.get("HISTORY_REWRITE_PUBLISHER_INSTALLATION_ID") != str(PUBLISHER_INSTALLATION_ID)
            or os.environ.get("HISTORY_REWRITE_PUBLISHER_APP_SLUG") != PUBLISHER_APP_SLUG):
        raise PublicationError("pinned token-action publisher installation or App output mismatched")
    return GitHubApi(token, api_url)


class FixtureApi:
    """File-backed API restricted to the hosted disposable-remote fixture."""
    def __init__(self, state: dict, *, principal: str = "publisher"):
        if principal not in {"publisher", "observer", "workflow"}:
            raise PublicationError("unknown fixture principal")
        self.state = state
        self.principal = principal

    def get(self, path: str) -> object:
        if path == OBSERVER_REPOSITORIES_PATH:
            return self.state[f"{self.principal}_repositories"]
        if path == f"/apps/{OBSERVER_APP_SLUG}":
            return self.state["observer_app"]
        if path == "/installation":
            raise PublicationError("unsupported bare installation endpoint")
        if path == f"/apps/{PUBLISHER_APP_SLUG}":
            return self.state["publisher_app"]
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
        if query == OBSERVER_VIEWER_QUERY:
            return self.state[f"{self.principal}_viewer"]
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
    # These documented operations accept installation tokens; App metadata alone
    # is not proof that the calling credential belongs to that App.
    viewer = api.post_graphql(PUBLISHER_VIEWER_QUERY, {})
    data = viewer.get("data") if isinstance(viewer, dict) else None
    actor = data.get("viewer") if isinstance(data, dict) else None
    if (not isinstance(viewer, dict) or viewer.get("errors") or not isinstance(actor, dict)
            or actor.get("login") != f"{PUBLISHER_APP_SLUG}[bot]"):
        raise PublicationError("publisher is not the expected authenticated App bot")
    app = api.get(f"/apps/{PUBLISHER_APP_SLUG}")
    if (not isinstance(app, dict) or app.get("id") != PUBLISHER_APP_ID
            or app.get("node_id") != PUBLISHER_APP_NODE_ID or app.get("slug") != PUBLISHER_APP_SLUG
            or app.get("permissions") != PUBLISHER_PERMISSIONS):
        raise PublicationError("publisher App identity or grant ceiling changed")
    selected = api.get(PUBLISHER_REPOSITORIES_PATH)
    repositories = selected.get("repositories") if isinstance(selected, dict) else None
    if (not isinstance(selected, dict) or type(selected.get("total_count")) is not int or selected["total_count"] != 1
            or not isinstance(repositories, list) or len(repositories) != 1
            or not isinstance(repositories[0], dict) or repositories[0].get("id") != REPOSITORY_ID
            or repositories[0].get("full_name") != REPOSITORY):
        raise PublicationError("publisher token is not scoped to exactly the selected repository")
    return {"schema": "history-rewrite-publisher-identity-v1", "app_id": PUBLISHER_APP_ID,
            "app_node_id": PUBLISHER_APP_NODE_ID, "app_slug": PUBLISHER_APP_SLUG,
            "installation_id": PUBLISHER_INSTALLATION_ID,
            "repository": REPOSITORY, "repository_id": REPOSITORY_ID,
            "authenticated_login": actor["login"], "app_grants": app["permissions"],
            "requested_token_permissions": dict(PUBLISHER_PERMISSIONS),
            "token_scope_source": "pinned token action with explicit repository and permission inputs; authenticated bot and repository readback"}


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


def verify_live_publication_state(manifest: dict, api: Api, *, observer_api: Api, current_run_id: int,
                                  handoff: dict | None = None) -> dict:
    observer = validate_observer_identity(observer_api)
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
    if handoff is None:
        protection = protection_snapshot_from_api(observer_api)
        if protection != manifest["controls"]["protection_snapshot"]:
            raise PublicationError("immediate pre-push protection or publisher App exception readback changed")
        protection_evidence = {"protection_snapshot_sha256": digest(protection)}
    else:
        from protected_handoff import require_fresh_gate, verify_current
        require_fresh_gate(handoff, manifest)
        if handoff["binding"]["run_id"] != current_run_id:
            raise PublicationError("final gate belongs to a different publication run")
        gate = verify_current(manifest, handoff["binding"], api, observer_api, now=datetime.now(timezone.utc))
        if gate["witness_sha256"] != handoff["witness_sha256"]:
            raise PublicationError("administrator approval changed after the protected gate")
        protection_evidence = {"protected_handoff": gate}
    return {
        "schema": "history-rewrite-final-state-v1",
        "writer_workflows": states,
        "mirror": {"id": MIRROR_WORKFLOW_ID, "state": "disabled_manually"},
        "writer_check": writer_check,
        **protection_evidence,
        "protection_observer": observer,
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


def custody_sources(manifest: dict, manifest_sha256: str, api: Api, output: Path, download) -> dict:
    """Preserve immutable encrypted ZIP bytes; never open or decrypt an archive."""
    if digest(manifest) != manifest_sha256 or manifest.get("schema") != "history-rewrite-custody-v1" or manifest.get("repository") != REPOSITORY or manifest.get("requested_retention_days") != 90:
        raise PublicationError("custody manifest identity, approval digest, or retention mismatch")
    sources = manifest.get("artifacts")
    if not isinstance(sources, list) or len(sources) != 4:
        raise PublicationError("custody requires exactly two encrypted ZIPs and their two proof/receipt ZIPs")
    expected_names = {"recovery-snapshot-ciphertext", "recovery-snapshot-receipt", "history-rewrite-candidate-ciphertext"}
    names = {item.get("name") for item in sources if isinstance(item, dict)}
    proof_names = names - expected_names
    if len(names) != 4 or not expected_names.issubset(names) or len(proof_names) != 1:
        raise PublicationError("custody source domain is incomplete or duplicated")
    proof_name = next(iter(proof_names))
    if not isinstance(proof_name, str) or not re.fullmatch(r"history-rewrite-rewrite-[1-9][0-9]*", proof_name):
        raise PublicationError("custody proof artifact name is invalid")
    verified, ids, runs = [], set(), {}
    for item in sources:
        artifact_id = _positive_int(item.get("id"), "custody artifact id")
        run_id = _positive_int(item.get("run_id"), "custody source run")
        size = _positive_int(item.get("size_in_bytes"), "custody source size")
        sha = _sha256(item.get("sha256"), "custody source digest")
        if artifact_id in ids or size > 600 * 1024 * 1024 or not OID.fullmatch(str(item.get("head_sha"))):
            raise PublicationError("custody source identity is duplicated, oversized, or malformed")
        ids.add(artifact_id)
        metadata = api.get(f"/repos/{REPOSITORY}/actions/artifacts/{artifact_id}")
        if not isinstance(metadata, dict) or any(metadata.get(key) != value for key, value in {
            "id": artifact_id, "name": item["name"], "size_in_bytes": size,
            "digest": "sha256:" + sha, "expired": False,
        }.items()) or metadata.get("workflow_run", {}).get("id") != run_id or metadata.get("workflow_run", {}).get("head_sha") != item["head_sha"]:
            raise PublicationError("custody source API identity/digest differs from its approved immutable binding")
        if run_id not in runs:
            runs[run_id] = api.get(f"/repos/{REPOSITORY}/actions/runs/{run_id}")
        run = runs[run_id]
        expected_path = ".github/workflows/recovery-snapshot.yml" if item["name"].startswith("recovery-snapshot-") else ".github/workflows/history-rewrite-candidate.yml"
        if not isinstance(run, dict) or run.get("id") != run_id or run.get("head_sha") != item["head_sha"] or run.get("status") != "completed" or run.get("conclusion") != "success" or run.get("repository", {}).get("full_name") != REPOSITORY or run.get("path") != expected_path:
            raise PublicationError("custody source is not the exact successful repository workflow run")
        if item["name"] == proof_name and proof_name != f"history-rewrite-rewrite-{run_id}":
            raise PublicationError("custody proof name/run binding mismatch")
        verified.append({**item, "original_expires_at": metadata.get("expires_at"), "filename": f"artifact-{artifact_id}.zip"})
    backup_runs = {item["run_id"] for item in verified if item["name"].startswith("recovery-snapshot-")}
    rewrite_runs = {item["run_id"] for item in verified if not item["name"].startswith("recovery-snapshot-")}
    if len(backup_runs) != 1 or len(rewrite_runs) != 1 or len({item["head_sha"] for item in verified}) != 1 or next(iter(backup_runs)) >= next(iter(rewrite_runs)):
        raise PublicationError("custody backup/proof pairs do not share the original frozen harness sequence")
    output.mkdir(parents=True, exist_ok=False)
    for item in verified:
        target = output / item["filename"]
        download(item["id"], target, item["size_in_bytes"])
        if target.stat().st_size != item["size_in_bytes"] or file_digest(target) != item["sha256"]:
            raise PublicationError("custody download did not preserve the exact original ZIP bytes")
    receipt = {"schema": "history-rewrite-custody-sources-v1", "repository": REPOSITORY,
               "manifest_sha256": manifest_sha256, "requested_retention_days": 90, "artifacts": verified,
               "content": "original encrypted ZIPs and original receipt/proof ZIPs, unopened and not decrypted",
               "status": "verified-source-bytes; destination retention not yet established"}
    write_json(output / "source-custody-receipt.json", receipt)
    write_json(output / "custody-manifest.json", manifest)
    return receipt


def download_custody_zip(artifact_id: int, target: Path, size: int) -> None:
    # gh handles GitHub's signed artifact redirect without forwarding our bearer
    # credential to the storage host. Keep both output and errors off public logs.
    with tempfile.TemporaryFile() as errors, target.open("xb") as destination:
        process = subprocess.Popen(["gh", "api", f"repos/{REPOSITORY}/actions/artifacts/{artifact_id}/zip"],
                                   stdout=subprocess.PIPE, stderr=errors)
        count = 0
        try:
            while chunk := process.stdout.read(1024 * 1024):
                count += len(chunk)
                if count > size:
                    raise PublicationError("custody download exceeded the approved size")
                destination.write(chunk)
            if process.wait() != 0:
                raise PublicationError("custody artifact download failed")
        finally:
            if process.poll() is None:
                process.terminate(); process.wait()


def custody_destination(source_receipt: dict, metadata: dict, *, run_id: int, artifact_id: int,
                        artifact_digest: str, head_sha: str, now: datetime) -> dict:
    if source_receipt.get("schema") != "history-rewrite-custody-sources-v1" or source_receipt.get("requested_retention_days") != 90:
        raise PublicationError("custody source receipt is invalid")
    _sha256(artifact_digest, "custody destination digest")
    if not isinstance(metadata, dict) or metadata.get("id") != artifact_id or metadata.get("name") != f"history-rewrite-encrypted-custody-{run_id}" or metadata.get("digest") != "sha256:" + artifact_digest or metadata.get("expired") is not False or metadata.get("workflow_run", {}).get("id") != run_id or metadata.get("workflow_run", {}).get("head_sha") != head_sha:
        raise PublicationError("custody destination API readback differs from the upload result")
    try:
        expiry = datetime.fromisoformat(metadata["expires_at"].replace("Z", "+00:00"))
        created = datetime.fromisoformat(metadata["created_at"].replace("Z", "+00:00"))
        if expiry <= now or expiry - created < timedelta(days=89):
            raise PublicationError("actual custody retention does not establish the approved 90-day bridge")
    except (KeyError, TypeError, ValueError) as exc:
        raise PublicationError("custody destination timestamps are invalid") from exc
    return {"schema": "history-rewrite-finite-custody-v1", "repository": REPOSITORY,
            "sources": source_receipt, "source_receipt_sha256": digest(source_receipt),
            "destination": {key: metadata[key] for key in ("id", "name", "size_in_bytes", "digest", "created_at", "expires_at", "workflow_run")},
            "archival_deadline": (expiry - timedelta(days=7)).isoformat(),
            "status": "verified finite hosted custody; not indefinite archival storage"}


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
    snapshot = sub.add_parser("snapshot")
    snapshot.add_argument("--output", type=Path, required=True)
    observer_snapshot = sub.add_parser("observer-snapshot")
    observer_snapshot.add_argument("--output", type=Path, required=True)
    observer_snapshot.add_argument("--identity-output", type=Path, required=True)
    observer_snapshot.add_argument("--api-dir", type=Path)
    publisher_identity = sub.add_parser("publisher-identity")
    publisher_identity.add_argument("--output", type=Path, required=True)
    planning = sub.add_parser("plan-maintenance")
    planning.add_argument("--administrator-before", type=Path, required=True)
    planning.add_argument("--read-token-before", type=Path, required=True)
    planning.add_argument("--output", type=Path, required=True)
    custody = sub.add_parser("custody")
    custody.add_argument("manifest", type=Path); custody.add_argument("--manifest-sha256", required=True)
    custody.add_argument("--output", type=Path, required=True)
    custody_readback = sub.add_parser("custody-readback")
    custody_readback.add_argument("--source-receipt", type=Path, required=True)
    custody_readback.add_argument("--artifact-id", type=int, required=True)
    custody_readback.add_argument("--artifact-digest", required=True)
    custody_readback.add_argument("--run-id", type=int, required=True)
    custody_readback.add_argument("--head-sha", required=True)
    custody_readback.add_argument("--output", type=Path, required=True)
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
    controls.add_argument("--handoff", type=Path)
    phase_arguments(controls, include_intent=True)
    publish = sub.add_parser("publish")
    publish.add_argument("manifest", type=Path); publish.add_argument("--repo", type=Path, required=True); publish.add_argument("--remote-url", required=True); publish.add_argument("--receipt", type=Path, required=True)
    publish.add_argument("--current-run-id", type=int, required=True)
    publish.add_argument("--fixture-api", type=Path)
    publish.add_argument("--handoff", type=Path)
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
        elif ns.command == "snapshot":
            value = protection_snapshot_from_api(GitHubApi(os.environ.get("GH_TOKEN", "")))
            validate_snapshot_shape(value)
            write_json(ns.output, value)
        elif ns.command == "observer-snapshot":
            api = observer_api_from_environment()
            identity = validate_observer_identity(api)
            value = protection_snapshot_from_api(api, api_dir=ns.api_dir)
            validate_snapshot_shape(value)
            write_json(ns.output, value)
            write_json(ns.identity_output, identity)
        elif ns.command == "publisher-identity":
            write_json(ns.output, validate_publisher_identity(publisher_api_from_environment()))
        elif ns.command == "plan-maintenance":
            write_json(ns.output, plan_maintenance(load_object(ns.administrator_before), load_object(ns.read_token_before)))
        elif ns.command == "custody":
            custody_sources(load_object(ns.manifest), ns.manifest_sha256, GitHubApi(os.environ.get("GH_TOKEN", "")), ns.output, download_custody_zip)
        elif ns.command == "custody-readback":
            api = GitHubApi(os.environ.get("GH_TOKEN", ""))
            metadata = api.get(f"/repos/{REPOSITORY}/actions/artifacts/{ns.artifact_id}")
            write_json(ns.output, custody_destination(load_object(ns.source_receipt), metadata,
                       run_id=ns.run_id, artifact_id=ns.artifact_id, artifact_digest=ns.artifact_digest,
                       head_sha=ns.head_sha, now=datetime.now(timezone.utc)))
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
            if ns.operation == "suppress" and os.environ.get("HISTORY_REWRITE_PUBLICATION_FIXTURE") != "1":
                from protected_handoff import require_fresh_gate
                if ns.handoff is None:
                    raise PublicationError("publisher effects require the protected handoff")
                require_fresh_gate(load_object(ns.handoff), manifest)
            api = publisher_api_from_environment(ns.api_url)
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
                    fixture_state = load_object(ns.fixture_api, "fixture API state")
                    publisher_api = FixtureApi(fixture_state)
                    read_api = FixtureApi(fixture_state, principal="workflow")
                    observer_api = FixtureApi(fixture_state, principal="observer")
                else:
                    if ns.handoff is None:
                        raise PublicationError("publication requires the protected handoff")
                    publisher_api = publisher_api_from_environment()
                    read_api = GitHubApi(os.environ.get("HISTORY_REWRITE_READ_TOKEN", ""))
                    observer_api = observer_api_from_environment()
                publisher = validate_publisher_identity(publisher_api)
                result = publish_repository(
                    manifest, frozen_sha=ns.frozen_sha, frozen_tree=ns.frozen_tree,
                    manifest_sha256=ns.manifest_sha256, preflight_path=ns.preflight,
                    control_plan=ns.control_plan, intent_path=ns.intent, read_api=read_api, observer_api=observer_api,
                    current_run_id=ns.current_run_id, repo=ns.repo, remote_url=ns.remote_url,
                    token=os.environ.get("GH_TOKEN"),
                    handoff=load_object(ns.handoff) if ns.handoff is not None else None,
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
                api = publisher_api_from_environment(ns.api_url)
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
