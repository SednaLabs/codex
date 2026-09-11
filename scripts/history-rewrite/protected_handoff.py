"""Protected approval evidence and independently observed control scalars.

The administrator witnesses actor identity. The read-only App observes scalar
controls and allowance counts. Neither view is represented as the other.
"""
import argparse
import copy
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile

import candidate_bundle as bundle
import publication as p

ENVIRONMENT_ID = 21690526893
MAX_WITNESS_SECONDS = 600
ALLOWANCES = {"bypassForcePushAllowances": "bypass_force_push_allowances",
              "bypassPullRequestAllowances": "bypass_pull_request_allowances", "pushAllowances": "push_allowances"}
SCALARS = dict(zip(
    "id pattern allowsForcePushes isAdminEnforced requiresStatusChecks requiredStatusCheckContexts requiredStatusChecks requiresStrictStatusChecks requiresApprovingReviews requiredApprovingReviewCount requiresConversationResolution restrictsPushes requiresCommitSignatures requiresLinearHistory lockBranch requiresDeployments requiredDeploymentEnvironments requireLastPushApproval requiresCodeOwnerReviews allowsDeletions blocksCreations".split(),
    "id pattern allows_force_pushes is_admin_enforced requires_status_checks required_status_check_contexts required_status_checks requires_strict_status_checks requires_approving_reviews required_approving_review_count requires_conversation_resolution restricts_pushes requires_commit_signatures requires_linear_history lock_branch requires_deployments required_deployment_environments require_last_push_approval requires_code_owner_reviews allows_deletions blocks_creations".split()))
# This is deliberately a different observation contract, not an actor fallback.
SCALAR_QUERY = p.BRANCH_PROTECTION_QUERY.replace("nodes { actor { __typename ... on App { id databaseId slug } } }", "")


@dataclass(frozen=True)
class ObserverScalars:
    document: dict


@dataclass(frozen=True)
class AdministratorWitness:
    document: dict


def timestamp(value: object) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError("timezone required")
        return result
    except (AttributeError, TypeError, ValueError) as exc:
        raise p.PublicationError("witness timestamp is malformed") from exc


def project_scalars(administrator: dict, visibility: dict) -> ObserverScalars:
    p.validate_snapshot_shape(administrator)
    result = copy.deepcopy(administrator)
    result["schema"] = "history-rewrite-observer-scalars-v1"
    for rule in result["branch_protection_rules"]:
        for field in ALLOWANCES.values():
            rule[field + "_count"] = len(rule.pop(field))
    for ruleset in result["repository_rulesets"]:
        if ruleset["bypass_actors_visibility"] != "visible":
            raise p.PublicationError("administrator ruleset actor inventory is not complete")
        observed = visibility.get(ruleset["id"])
        if observed == "not_returned":
            ruleset.update(bypass_actors_visibility="not_returned", bypass_actors=None)
        elif observed != "visible":
            raise p.PublicationError("observer ruleset domain or visibility mismatch")
    return ObserverScalars(result)


def observe(api, *, error_path: Path | None = None) -> ObserverScalars:
    raw = api.post_graphql(SCALAR_QUERY, {"owner": "sednalabs", "name": "codex"})
    if not isinstance(raw, dict) or raw.get("errors"):
        if error_path is not None:
            # Persist actionable error positions/types, not raw control data.
            errors = raw.get("errors", []) if isinstance(raw, dict) else []
            p.write_json(error_path, {"schema": "history-rewrite-observer-error-v1", "errors": [
                {key: item.get(key) for key in ("type", "path", "locations")} for item in errors if isinstance(item, dict)]})
        raise p.PublicationError("scalar observation was denied or partial; no alternate principal attempted")
    try:
        connection = raw["data"]["repository"]["branchProtectionRules"]
        nodes = connection["nodes"]
        if not isinstance(nodes, list) or connection["totalCount"] != len(nodes) or len(nodes) >= 100:
            raise ValueError("incomplete rule domain")
        rules = []
        for node in nodes:
            rule = {target: node[source] for source, target in SCALARS.items()}
            for source, target in ALLOWANCES.items():
                value = node[source]["totalCount"]
                if type(value) is not int or value < 0 or value >= 100:
                    raise ValueError("invalid count")
                rule[target + "_count"] = value
            rules.append(rule)
        listing = api.get(f"/repos/{p.REPOSITORY}/rulesets?includes_parents=true&per_page=100")
        if not isinstance(listing, list) or len(listing) >= 100:
            raise ValueError("incomplete rulesets")
        rulesets = []
        for row in listing:
            value = api.get(f"/repos/{p.REPOSITORY}/rulesets/{p._positive_int(row['id'], 'ruleset id')}")
            rulesets.append({**{key: value[key] for key in ("id", "name", "target", "enforcement", "conditions", "rules")},
                "bypass_actors_visibility": "visible" if "bypass_actors" in value else "not_returned",
                "bypass_actors": sorted(value["bypass_actors"], key=p.canonical_json) if "bypass_actors" in value else None})
        return ObserverScalars({"schema": "history-rewrite-observer-scalars-v1", "protected_refs": list(p.PROTECTED_REFS),
            "branch_protection_rules": sorted(rules, key=lambda rule: (rule["pattern"], rule["id"])),
            "repository_rulesets": sorted(rulesets, key=lambda rule: rule["id"])})
    except (KeyError, TypeError, ValueError) as exc:
        raise p.PublicationError("scalar observation schema is incomplete or malformed") from exc


def approval_binding(manifest: dict, prepared: dict, artifact: dict, *, run_id: int, attempt: int, phase: str) -> dict:
    return {"schema": "history-rewrite-administrator-witness-v1", "repository": p.REPOSITORY,
            "repository_id": p.REPOSITORY_ID, "run_id": run_id, "run_attempt": attempt, "phase": phase,
            "harness_sha": manifest["harness_sha"], "harness_tree": manifest["harness_tree"],
            "manifest_sha256": p.digest(manifest), "selected_refs_sha256": manifest["selected_refs_sha256"],
            "output_refs_sha256": manifest["output_refs_sha256"], "prepared_artifact": artifact,
            "prepared_sha256": p.digest(prepared)}


def validate_witness(approvals: object, expected: dict, run: dict, *, now: datetime) -> AdministratorWitness:
    if (run.get("id") != expected["run_id"] or run.get("run_attempt") != expected["run_attempt"]
            or run.get("head_sha") != expected["harness_sha"] or run.get("repository", {}).get("id") != p.REPOSITORY_ID
            or run.get("event") != "workflow_dispatch" or run.get("head_branch") != p.PUBLICATION_BRANCH
            or run.get("path") != ".github/workflows/history-rewrite-candidate.yml"):
        raise p.PublicationError("current workflow run/attempt identity mismatch")
    if not isinstance(approvals, list):
        raise p.PublicationError("approval history is not a list")
    matches = []
    for item in approvals:
        if not isinstance(item, dict):
            raise p.PublicationError("approval history is malformed")
        environments = item.get("environments", [])
        if not isinstance(environments, list):
            raise p.PublicationError("approval environment evidence malformed")
        if not any(isinstance(env, dict) and env.get("id") == ENVIRONMENT_ID for env in environments):
            continue
        if item.get("state") != "approved":
            raise p.PublicationError("publication environment has a non-approval record")
        try:
            comment = item["comment"]
            if not isinstance(comment, str) or len(comment.encode()) > 60000:
                raise ValueError("comment bound")
            value = json.loads(comment)
            if not isinstance(value, dict) or comment != p.canonical_json(value).decode():
                raise ValueError("canonical structured comment required")
        except (KeyError, ValueError, UnicodeError) as exc:
            raise p.PublicationError("approval comment is malformed or not canonical") from exc
        # GitHub may retain approvals from older attempts. A previous attempt
        # is evidence of history, never authorization for this retry.
        if (value.get("schema") == expected["schema"] and value.get("run_id") == expected["run_id"]
                and type(value.get("run_attempt")) is int and value["run_attempt"] < expected["run_attempt"]):
            continue
        if (item.get("user", {}).get("id"), item.get("user", {}).get("login")) != (p.REVIEWER_ID, p.REVIEWER_LOGIN):
            raise p.PublicationError("approval is not from the exact administrator")
        if environments != [{"id": ENVIRONMENT_ID, "name": p.ENVIRONMENT_NAME}]:
            raise p.PublicationError("approval environment domain mismatch")
        extra = {"observed_at", "expires_at", "administrator_snapshot", "administrator_snapshot_sha256"}
        if set(value) != set(expected) | extra or any(value.get(key) != wanted for key, wanted in expected.items()):
            raise p.PublicationError("approval does not bind the exact phase, attempt and prepared artifact")
        observed, expires = timestamp(value["observed_at"]), timestamp(value["expires_at"])
        if not timestamp(run["run_started_at"]) <= observed <= now < expires or not 0 < (expires - observed).total_seconds() <= MAX_WITNESS_SECONDS:
            raise p.PublicationError("administrator observation is stale, future-dated or overlong")
        snapshot = value["administrator_snapshot"]
        p.validate_snapshot_shape(snapshot)
        for rule in snapshot["branch_protection_rules"]:
            for field in ALLOWANCES.values():
                for actor in rule[field]:
                    if (not isinstance(actor, dict) or set(actor) != {"__typename", "id", "databaseId", "slug"}
                            or actor.get("__typename") != "App" or type(actor.get("databaseId")) is not int
                            or actor["databaseId"] <= 0 or not all(isinstance(actor.get(key), str) and actor[key] for key in ("id", "slug"))):
                        raise p.PublicationError("administrator allowance actor identity is null or malformed")
        if p.digest(snapshot) != value["administrator_snapshot_sha256"]:
            raise p.PublicationError("administrator snapshot digest mismatch")
        if any(row["bypass_actors_visibility"] != "visible" for row in snapshot["repository_rulesets"]):
            raise p.PublicationError("administrator actor evidence is incomplete")
        matches.append(value)
    if len(matches) != 1:
        raise p.PublicationError("current-attempt administrator witness is missing or ambiguous")
    return AdministratorWitness(matches[0])


def verify_current(manifest: dict, binding: dict, read_api, observer_api, *, now: datetime, error_path: Path | None = None) -> dict:
    run_id, attempt = binding["run_id"], binding["run_attempt"]
    run = read_api.get(f"/repos/{p.REPOSITORY}/actions/runs/{run_id}/attempts/{attempt}")
    witness = validate_witness(read_api.get(f"/repos/{p.REPOSITORY}/actions/runs/{run_id}/approvals"), binding, run, now=now)
    snapshot = witness.document["administrator_snapshot"]
    if binding["phase"] == "publication":
        p.validate_protection_snapshot(snapshot)
        if snapshot != manifest["controls"]["maintenance_plan"]["administrator_after"]:
            raise p.PublicationError("actual administrator state differs from the approved full maintenance plan")
    elif binding["phase"] != "qualification":
        raise p.PublicationError("invalid protected handoff phase")
    observed = observe(observer_api, error_path=error_path)
    visibility = {row["id"]: row["bypass_actors_visibility"] for row in observed.document["repository_rulesets"]}
    if observed != project_scalars(snapshot, visibility):
        raise p.PublicationError("independent observer scalars/counts differ from administrator observation")
    return {"schema": "history-rewrite-protected-gate-v1", "binding": binding,
            "witness_sha256": p.digest(witness.document), "administrator_snapshot_sha256": p.digest(snapshot),
            "observer_scalars_sha256": p.digest(observed.document), "expires_at": witness.document["expires_at"],
            "observed_at": witness.document["observed_at"], "verified_at": now.isoformat(),
            "comment_bytes": len(p.canonical_json(witness.document)), "comment_sha256": p.digest(witness.document)}


def require_fresh_gate(receipt: dict, manifest: dict) -> None:
    binding = receipt.get("binding", {})
    fields = {"phase": "publication", "manifest_sha256": p.digest(manifest),
              "harness_sha": manifest["harness_sha"], "harness_tree": manifest["harness_tree"],
              "selected_refs_sha256": manifest["selected_refs_sha256"], "output_refs_sha256": manifest["output_refs_sha256"],
              "run_id": int(os.environ["GITHUB_RUN_ID"]), "run_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"])}
    now = datetime.now(timezone.utc)
    if (receipt.get("schema") != "history-rewrite-protected-gate-v1"
            or any(binding.get(key) != value for key, value in fields.items())
            or not timestamp(receipt.get("observed_at")) <= timestamp(receipt.get("verified_at")) <= now < timestamp(receipt.get("expires_at"))
            or (timestamp(receipt["expires_at"]) - timestamp(receipt["observed_at"])).total_seconds() > MAX_WITNESS_SECONDS):
        raise p.PublicationError("protected gate binding or freshness expired; reuse the prepared artifact with a new approval")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "import", "gate", "describe", "freshness", "qualification-prepare"))
    parser.add_argument("--root", type=Path, required=True)
    ns = parser.parse_args()
    root = ns.root
    sha, tree = os.environ["HARNESS_SHA"], os.environ["HARNESS_TREE"]
    run_id, attempt = int(os.environ["GITHUB_RUN_ID"]), int(os.environ["GITHUB_RUN_ATTEMPT"])
    phase = "qualification" if os.environ.get("MODE") == "qualification" else "publication"
    expected_digest = os.environ.get("MANIFEST_SHA256", "")
    if ns.command == "freshness":
        require_fresh_gate(p.load_object(root / "handoff.json"), p.load_object(root / "prepared/manifest.json"))
        return
    api = p.GitHubApi(os.environ["GH_TOKEN"])
    if (os.environ["GITHUB_REPOSITORY"] != p.REPOSITORY or os.environ["GITHUB_REF_NAME"] != p.PUBLICATION_BRANCH
            or os.environ["GITHUB_SHA"] != sha or p.git(Path.cwd(), "rev-parse", "HEAD^{tree}").strip() != tree):
        raise p.PublicationError("checked-out workflow host binding mismatch")
    if ns.command == "describe":
        reuse = os.environ.get("PREPARED_ARTIFACT", "")
        previous = json.loads(reuse) if reuse else None
        artifact_id = previous["artifact_id"] if previous else int(os.environ["PREPARED_ID"])
        metadata = api.get(f"/repos/{p.REPOSITORY}/actions/artifacts/{artifact_id}")
        value = bundle.descriptor(metadata, head_sha=sha)
        if previous is not None and previous != value:
            raise p.PublicationError("reuse descriptor changed")
        manifest_digest = expected_digest if previous else p.digest(p.load_object(root / "prepared/manifest.json"))
        p._sha256(manifest_digest, "prepared manifest digest")
        if previous is None:
            manifest = p.load_object(root / "prepared/manifest.json")
            prepared = p.load_object(root / "prepared/prepared.json")
            p.write_json(root / "prepared-binding.json", {
                "schema": "history-rewrite-prepared-binding-v1", "prepared": prepared,
                "approval_binding": approval_binding(manifest, prepared, value, run_id=run_id, attempt=attempt, phase=phase)})
        print("artifact=" + p.canonical_json(value).decode())
        print("manifest_sha256=" + manifest_digest)
        return
    if ns.command == "qualification-prepare":
        # An isolated synthetic object graph exercises the same artifact path;
        # its phase cannot be consumed by any publisher command.
        with tempfile.TemporaryDirectory(prefix="qualification-source-") as temporary:
            repo = Path(temporary) / "source.git"
            bundle.init_repo(repo)
            empty_tree = subprocess.check_output(["git", "-C", str(repo), "mktree"], input=b"").decode().strip()
            env = {**os.environ, "GIT_AUTHOR_NAME": "Qualification", "GIT_AUTHOR_EMAIL": "qualification@example.invalid",
                   "GIT_COMMITTER_NAME": "Qualification", "GIT_COMMITTER_EMAIL": "qualification@example.invalid"}
            commit = p.git(repo, "commit-tree", empty_tree, "-m", "Candidate transport qualification", env=env).strip()
            refs = {"refs/heads/main": commit}
            manifest = {"harness_sha": sha, "harness_tree": tree, "selected_refs": refs, "output_refs": refs,
                        "selected_refs_sha256": p.digest(refs), "output_refs_sha256": p.digest(refs), "proof_digests": {}}
            bundle.export_candidate(repo, manifest, root / "prepared", phase=phase, run_id=run_id, attempt=attempt)
        return
    if ns.command == "prepare":
        manifest = p.load_object(root / "manifest.json")
        p.validate_preparation(manifest, frozen_sha=sha, frozen_tree=tree, manifest_sha256=expected_digest,
                               api_dir=root / "api", output=root / "preflight")
        p.regenerate_candidate(manifest, remote_url=os.environ["REPO_URL"], work_root=root / "candidate",
                               proof_dir=root / "preflight/approved-proof")
        bundle.export_candidate(root / "candidate/repo.git", manifest, root / "prepared", phase=phase, run_id=run_id, attempt=attempt)
        return
    artifact = json.loads(os.environ["PREPARED_ARTIFACT"])
    if ns.command == "import":
        bundle.read_candidate(artifact, api, root / "prepared", frozen_sha=sha, frozen_tree=tree,
                              manifest_sha256=expected_digest, phase=phase)
        return
    manifest, prepared = p.load_object(root / "prepared/manifest.json"), p.load_object(root / "prepared/prepared.json")
    if p.digest(manifest) != expected_digest or prepared["phase"] != phase:
        raise p.PublicationError("protected manifest digest/phase mismatch")
    if phase == "publication":
        p.validate_manifest(manifest, frozen_sha=sha, frozen_tree=tree)
    p.validate_environment(api.get(f"/repos/{p.REPOSITORY}/environments/{p.ENVIRONMENT_NAME}"),
        api.get(f"/repos/{p.REPOSITORY}/environments/{p.ENVIRONMENT_NAME}/deployment-branch-policies?per_page=100"))
    observer = p.observer_api_from_environment()
    p.validate_observer_identity(observer)
    binding = approval_binding(manifest, prepared, artifact, run_id=run_id, attempt=attempt, phase=phase)
    receipt = verify_current(manifest, binding, api, observer, now=datetime.now(timezone.utc), error_path=root / "observer-error.json")
    p.write_json(root / "handoff.json", receipt)
    if phase == "publication":
        api_dir = root / "api"
        for workflow_id in {*p.WRITER_WORKFLOWS.values(), p.MIRROR_WORKFLOW_ID}:
            p.write_json(api_dir / f"workflow-{workflow_id}.json", api.get(f"/repos/{p.REPOSITORY}/actions/workflows/{workflow_id}"))
            if workflow_id != p.MIRROR_WORKFLOW_ID:
                p.write_json(api_dir / f"writer-runs-{workflow_id}.json", [{"requested_status": state,
                    "response": api.get(f"/repos/{p.REPOSITORY}/actions/workflows/{workflow_id}/runs?status={state}&per_page=100")}
                    for state in sorted(p.ACTIVE_RUN_STATES)])
        p.record_live_preflight(manifest, api_dir=api_dir, output=root / "preflight")
        p.write_restoration_intent(manifest, preflight_path=root / "preflight/preflight.json",
                                  control_plan=root / "preflight/control-plan.json", intent_path=root / "publication-restoration-intent.json")


if __name__ == "__main__":
    try:
        main()
    except p.PublicationError as exc:
        raise SystemExit(f"protected handoff: {exc}") from exc
