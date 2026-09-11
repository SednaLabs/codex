#!/usr/bin/env python3
"""Hosted synthetic coverage of the exact protected approval/scalar contract."""
import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import publication as p
import protected_handoff as h
from publication_fixtures import base_manifest, environment_fixture, expect_failure, observer_fixture_documents


def main() -> None:
    manifest = base_manifest()
    administrator = manifest["controls"]["maintenance_plan"]["administrator_after"]
    now = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
    prepared = {"phase": "publication", "run_id": 21, "run_attempt": 1}
    artifact = {"artifact_id": 99, "run_id": 21, "head_sha": manifest["harness_sha"],
                "artifact_api_digest": "c" * 64, "artifact_size": 1234}
    binding = h.approval_binding(manifest, prepared, artifact, run_id=22, attempt=2, phase="publication")
    run = {"id": 22, "run_attempt": 2, "head_sha": manifest["harness_sha"], "repository": {"id": p.REPOSITORY_ID},
           "event": "workflow_dispatch", "head_branch": p.PUBLICATION_BRANCH,
           "path": ".github/workflows/history-rewrite-candidate.yml", "run_started_at": (now - timedelta(minutes=5)).isoformat()}
    attestation = {"schema": h.ATTESTATION_SCHEMA, "binding_sha256": p.digest(binding),
                   "observed_at": (now - timedelta(seconds=15)).isoformat(timespec="microseconds"),
                   "expires_at": (now + timedelta(seconds=585)).isoformat(timespec="microseconds"),
                   "administrator_snapshot_sha256": p.digest(administrator)}

    # Field shape observed in the real approvals API. Non-authority metadata
    # uses public-safe synthetic values; id/name remain the exact contract.
    provider_environment = {"can_admins_bypass": True, "created_at": "2026-01-01T00:00:00Z",
        "html_url": f"https://github.com/{p.REPOSITORY}/deployments/activity_log?environments_filter={p.ENVIRONMENT_NAME}",
        "id": h.ENVIRONMENT_ID, "name": p.ENVIRONMENT_NAME, "node_id": "EN_fixture",
        "updated_at": "2026-01-01T00:00:00Z",
        "url": f"https://api.github.com/repos/{p.REPOSITORY}/environments/{p.ENVIRONMENT_NAME}"}

    def approval(value):
        return {"state": "approved", "user": {"id": p.REVIEWER_ID, "login": p.REVIEWER_LOGIN},
                "environments": [copy.deepcopy(provider_environment)], "comment": p.canonical_json(value).decode()}

    def validate(items, expected=binding, current_run=run):
        return h.validate_attestation(items, expected, p.digest(administrator), current_run, now=now)

    records = [approval(attestation)]
    assert validate(records).document == attestation
    assert validate([{**records[0], "environments": [{"id": h.ENVIRONMENT_ID, "name": p.ENVIRONMENT_NAME}]}]).document == attestation
    # Approval metadata cannot itself grant policy authority or revoke it;
    # the separate live environment-policy check remains mandatory.
    assert validate([{**records[0], "environments": [{**provider_environment, "can_admins_bypass": False}]}]).document == attestation
    assert len(records[0]["comment"].encode()) == 337 < h.MAX_COMMENT_BYTES
    old = {**attestation, "binding_sha256": p.digest({**binding, "run_attempt": 1}),
           "observed_at": (now - timedelta(days=1)).isoformat(), "expires_at": (now - timedelta(hours=23)).isoformat()}
    assert validate([approval(old), *records]).document == attestation
    failures = []
    failures.append(expect_failure("old_approval_alone", lambda: validate([approval(old)])))
    for key in binding:
        changed_binding = {**binding, key: None}
        bad = {**attestation, "binding_sha256": p.digest(changed_binding)}
        failures.append(expect_failure("wrong_" + key, lambda: validate([approval(bad)])))
    for key in artifact:
        changed_binding = {**binding, "prepared_artifact": {**artifact, key: None}}
        bad = {**attestation, "binding_sha256": p.digest(changed_binding)}
        failures.append(expect_failure("wrong_artifact_" + key, lambda: validate([approval(bad)])))
    for label, bad in (
        ("expired", {**attestation, "expires_at": now.isoformat()}),
        ("future", {**attestation, "observed_at": (now + timedelta(seconds=1)).isoformat()}),
        ("overlong", {**attestation, "expires_at": (now + timedelta(seconds=586)).isoformat()}),
        ("before_attempt", {**attestation, "observed_at": (now - timedelta(minutes=6)).isoformat(),
                           "expires_at": (now + timedelta(minutes=4)).isoformat()}),
        ("malformed_time", {**attestation, "observed_at": None}),
        ("wrong_snapshot_digest", {**attestation, "administrator_snapshot_sha256": "0" * 64}),
        ("malformed_binding_digest", {**attestation, "binding_sha256": "bad"}),
        ("malformed_snapshot_digest", {**attestation, "administrator_snapshot_sha256": "g" * 64}),
        ("unexpected_field", {**attestation, "administrator_snapshot": administrator}),
        ("unknown_schema", {**attestation, "schema": "other"}),
        ("missing_field", {key: value for key, value in attestation.items() if key != "observed_at"}),
    ):
        failures.append(expect_failure(label, lambda: validate([approval(bad)])))
    for label, changed in (("missing", []), ("duplicate", records * 2),
        ("foreign_reviewer_id", [{**records[0], "user": {"id": 1, "login": p.REVIEWER_LOGIN}}]),
        ("foreign_reviewer_login", [{**records[0], "user": {"id": p.REVIEWER_ID, "login": "other"}}]),
        ("malformed_comment", [{**records[0], "comment": "approved"}]),
        ("oversized_comment", [{**records[0], "comment": " " * 1025}]),
        ("noncanonical_comment", [{**records[0], "comment": json.dumps(attestation)}]),
        ("non_ascii_comment", [{**records[0], "comment": records[0]["comment"].replace("attestation", "attestatiön")}]),
        ("wrong_environment_name", [{**records[0], "environments": [{"id": h.ENVIRONMENT_ID, "name": "other"}]}]),
        ("wrong_environment_id", [{**records[0], "environments": [{"id": 1, "name": p.ENVIRONMENT_NAME}]}]),
        ("missing_environment_id", [{**records[0], "environments": [{key: value for key, value in provider_environment.items() if key != "id"}]}]),
        ("missing_environment_name", [{**records[0], "environments": [{key: value for key, value in provider_environment.items() if key != "name"}]}]),
        ("conflicting_environment", [{**records[0], "environments": [provider_environment, {"id": 1, "name": p.ENVIRONMENT_NAME}]}]),
        ("malformed_environment", [{**records[0], "environments": [None]}]),
        ("extra_environment", [{**records[0], "environments": records[0]["environments"] * 2}]),
        ("rejected", [{**records[0], "state": "rejected"}])):
        failures.append(expect_failure(label, lambda: validate(changed)))
    for mutate in ("null_actor", "hidden_ruleset"):
        changed = copy.deepcopy(administrator)
        if mutate == "null_actor":
            changed["branch_protection_rules"][0]["push_allowances"] = [None]
        else:
            changed["repository_rulesets"][0].update(bypass_actors_visibility="not_returned", bypass_actors=None)
        changed_manifest = {**manifest, "controls": {"maintenance_plan": {"administrator_after": changed}}}
        failures.append(expect_failure(mutate, lambda: h.expected_snapshot(changed_manifest, "publication")))
    for key in ("id", "run_attempt", "head_sha", "repository", "event", "head_branch", "path"):
        bad_run = {**run, key: {} if key == "repository" else None}
        failures.append(expect_failure("foreign_run_" + key, lambda: validate(records, current_run=bad_run)))

    class ReadApi:
        def get(self, path):
            if path.endswith("/approvals"):
                return records
            assert path.endswith("/attempts/2")
            return run

    class ObserverApi:
        changed_count = False
        changed_scalar = False
        denied = False
        def post_graphql(self, query, variables):
            assert query == h.SCALAR_QUERY and "actor {" not in query
            if self.denied:
                return {"errors": [{"type": "FORBIDDEN", "path": ["repository"], "locations": []}]}
            nodes = []
            for row in administrator["branch_protection_rules"]:
                node = {source: row[target] for source, target in h.SCALARS.items()}
                node.update({source: {"totalCount": len(row[target])} for source, target in h.ALLOWANCES.items()})
                nodes.append(node)
            if self.changed_count:
                nodes[0]["pushAllowances"]["totalCount"] += 1
            if self.changed_scalar:
                nodes[0]["allowsForcePushes"] = True
            return {"data": {"repository": {"branchProtectionRules": {"totalCount": len(nodes), "nodes": nodes}}}}
        def get(self, path):
            if "?" in path:
                return [{"id": row["id"]} for row in administrator["repository_rulesets"]]
            return {key: value for key, value in administrator["repository_rulesets"][0].items()
                    if key not in {"bypass_actors", "bypass_actors_visibility"}}

    observer = ObserverApi()
    gate = h.verify_current(manifest, binding, ReadApi(), observer, now=now)
    assert gate["comment_bytes"] == len(records[0]["comment"].encode()) and gate["comment_sha256"] == p.digest(attestation)
    assert gate["attested_administrator_snapshot_sha256"] == gate["expected_administrator_snapshot_sha256"] == p.digest(administrator)
    assert "administrator_snapshot" not in gate
    observer.changed_count = True
    failures.append(expect_failure("observer_count_mismatch", lambda: h.verify_current(manifest, binding, ReadApi(), observer, now=now)))
    observer.changed_count, observer.changed_scalar = False, True
    failures.append(expect_failure("observer_scalar_mismatch", lambda: h.verify_current(manifest, binding, ReadApi(), observer, now=now)))
    observer.changed_scalar, observer.denied = False, True
    with tempfile.TemporaryDirectory() as temporary:
        error = Path(temporary) / "error.json"
        failures.append(expect_failure("observer_denial", lambda: h.observe(observer, error_path=error)))
        assert p.load_object(error)["errors"][0]["type"] == "FORBIDDEN"
    observer.denied = False
    mismatched = copy.deepcopy(administrator)
    mismatched["branch_protection_rules"][0]["push_allowances"][0]["databaseId"] += 1
    records = [approval({**attestation, "administrator_snapshot_sha256": p.digest(mismatched)})]
    failures.append(expect_failure("wrong_actual_actor", lambda: h.verify_current(manifest, binding, ReadApi(), observer, now=now)))
    records = [approval(attestation)]
    qualification = {key: manifest[key] for key in ("harness_sha", "harness_tree", "selected_refs_sha256", "output_refs_sha256", "controls")}
    qualification_binding = h.approval_binding(qualification, prepared, artifact, run_id=22, attempt=2, phase="qualification")
    records = [approval({**attestation, "binding_sha256": p.digest(qualification_binding)})]
    qualification_gate = h.verify_current(qualification, qualification_binding, ReadApi(), observer, now=now)
    assert qualification_gate["expected_administrator_snapshot_sha256"] == p.digest(administrator)
    failures.append(expect_failure("qualification_missing_expected_state", lambda: h.expected_snapshot({}, "qualification")))
    failures.append(expect_failure("invalid_phase", lambda: h.expected_snapshot(manifest, "other")))
    changed_qualification = copy.deepcopy(qualification)
    changed_rule = changed_qualification["controls"]["maintenance_plan"]["administrator_after"]["branch_protection_rules"][0]
    changed_rule["requires_conversation_resolution"] = not changed_rule["requires_conversation_resolution"]
    changed_binding = h.approval_binding(changed_qualification, prepared, artifact, run_id=22, attempt=2, phase="qualification")
    failures.append(expect_failure("qualification_expected_state_binding_changed", lambda: validate(records, expected=changed_binding)))
    records = [approval(attestation)]
    # Exercise the actual read-only preparation CLI, including the expected
    # state crossing the candidate artifact boundary. No API may be called.
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        p.write_json(root / "qualification-expected.json", administrator)
        source_sha = p.git(Path.cwd(), "rev-parse", "HEAD").strip()
        source_tree = p.git(Path.cwd(), "rev-parse", "HEAD^{tree}").strip()
        environment = {"GITHUB_RUN_ID": "22", "GITHUB_RUN_ATTEMPT": "2", "MODE": "qualification",
                       "GH_TOKEN": "fixture-only", "HARNESS_SHA": source_sha, "HARNESS_TREE": source_tree,
                       "GITHUB_REPOSITORY": p.REPOSITORY, "GITHUB_REF_NAME": p.PUBLICATION_BRANCH, "GITHUB_SHA": source_sha}
        with patch.dict(os.environ, environment), patch.object(p, "GitHubApi", return_value=None), \
                patch.object(sys, "argv", ["protected_handoff.py", "qualification-prepare", "--root", str(root)]):
            h.main()
        prepared_manifest = p.load_object(root / "prepared/manifest.json")
        prepared_receipt = p.load_object(root / "prepared/prepared.json")
        assert h.expected_snapshot(prepared_manifest, "qualification") == administrator
        assert prepared_receipt["manifest_sha256"] == p.digest(prepared_manifest)
        assert prepared_receipt["phase"] == "qualification"
    class Clock:
        @staticmethod
        def now(zone): return now
    with patch.dict(os.environ, {"GITHUB_RUN_ID": "22", "GITHUB_RUN_ATTEMPT": "2"}), patch.object(h, "datetime", Clock):
        # timestamp parsing needs the genuine datetime parser.
        Clock.fromisoformat = datetime.fromisoformat
        h.require_fresh_gate(gate, manifest)
        failures.append(expect_failure("expired_before_effects", lambda: h.require_fresh_gate({**gate, "expires_at": now.isoformat()}, manifest)))
        failures.append(expect_failure("qualification_cannot_publish", lambda: h.require_fresh_gate(qualification_gate, qualification)))
    # Execute the actual publication gate, not qualification: API-shaped writer
    # responses must cross the on-disk array seam into preflight and recovery
    # intent. All authority checks stay real; API responses are isolated fixtures.
    source_sha = p.git(Path.cwd(), "rev-parse", "HEAD").strip()
    source_tree = p.git(Path.cwd(), "rev-parse", "HEAD^{tree}").strip()
    production_manifest = {**manifest, "harness_sha": source_sha, "harness_tree": source_tree}
    production_artifact = {**artifact, "head_sha": source_sha}
    production_binding = h.approval_binding(production_manifest, prepared, production_artifact,
                                            run_id=22, attempt=2, phase="publication")
    production_records = [approval({**attestation, "binding_sha256": p.digest(production_binding)})]
    production_run = {**run, "head_sha": source_sha}
    environment_document, policies = environment_fixture()
    observer_documents = observer_fixture_documents()
    workflow_paths = {value: key for key, value in p.WRITER_WORKFLOWS.items()}
    workflow_paths[p.MIRROR_WORKFLOW_ID] = ".github/workflows/sedna-sync-upstream.yml"

    class ProductionReadApi:
        def get(self, path):
            if path.endswith("/approvals"): return production_records
            if path.endswith("/attempts/2"): return production_run
            if path.endswith("/deployment-branch-policies?per_page=100"): return policies
            if path.endswith("/environments/" + p.ENVIRONMENT_NAME): return environment_document
            prefix = f"/repos/{p.REPOSITORY}/actions/workflows/"
            assert path.startswith(prefix), path
            suffix = path.removeprefix(prefix)
            workflow_id = int(suffix.split("/", 1)[0])
            assert workflow_id in workflow_paths
            if "/runs?status=" in suffix:
                state = suffix.split("/runs?status=", 1)[1].removesuffix("&per_page=100")
                assert workflow_id != p.MIRROR_WORKFLOW_ID and state in p.ACTIVE_RUN_STATES
                return {"total_count": 0, "workflow_runs": []}
            return {"id": workflow_id, "path": workflow_paths[workflow_id],
                    "state": "disabled_manually" if workflow_id == p.MIRROR_WORKFLOW_ID else "active"}

    class ProductionObserverApi(ObserverApi):
        def post_graphql(self, query, variables):
            if query == p.OBSERVER_VIEWER_QUERY: return observer_documents["observer_viewer"]
            return super().post_graphql(query, variables)
        def get(self, path):
            if path == "/apps/" + p.OBSERVER_APP_SLUG: return observer_documents["observer_app"]
            if path == p.OBSERVER_REPOSITORIES_PATH: return observer_documents["observer_repositories"]
            return super().get(path)

    def fixture_api(token):
        assert token in {"fixture-read", "fixture-observer"}, "unexpected credential path"
        return ProductionReadApi() if token == "fixture-read" else ProductionObserverApi()

    def production_preflight(corruption=None):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            p.write_json(root / "prepared/manifest.json", production_manifest)
            p.write_json(root / "prepared/prepared.json", prepared)
            environment = {"GITHUB_RUN_ID": "22", "GITHUB_RUN_ATTEMPT": "2", "MODE": "publication",
                "GH_TOKEN": "fixture-read", "HARNESS_SHA": source_sha, "HARNESS_TREE": source_tree,
                "GITHUB_REPOSITORY": p.REPOSITORY, "GITHUB_REF_NAME": p.PUBLICATION_BRANCH, "GITHUB_SHA": source_sha,
                "MANIFEST_SHA256": p.digest(production_manifest), "PREPARED_ARTIFACT": p.canonical_json(production_artifact).decode(),
                "HISTORY_REWRITE_OBSERVER_TOKEN": "fixture-observer",
                "HISTORY_REWRITE_OBSERVER_INSTALLATION_ID": str(p.OBSERVER_INSTALLATION_ID),
                "HISTORY_REWRITE_OBSERVER_APP_SLUG": p.OBSERVER_APP_SLUG}
            writer_files = []
            real_write = p.write_json
            def persist(path, value):
                if path.name.startswith("writer-runs-"):
                    writer_files.append(path)
                    assert isinstance(value, list) and len(value) == len(p.ACTIVE_RUN_STATES)
                    if corruption is not None and len(writer_files) == 1:
                        corruption(path, copy.deepcopy(value))
                        return
                real_write(path, value)
            real_git = p.git
            def read_only_git(repo, *args, **kwargs):
                assert args == ("rev-parse", "HEAD^{tree}"), "unexpected Git effect"
                return real_git(repo, *args, **kwargs)
            with patch.dict(os.environ, environment), patch.object(h, "datetime", Clock), \
                    patch.object(p, "GitHubApi", side_effect=fixture_api), patch.object(p, "write_json", side_effect=persist), \
                    patch.object(p, "git", side_effect=read_only_git), \
                    patch.object(p, "publisher_api_from_environment", side_effect=AssertionError("publisher forbidden")) as mint, \
                    patch.object(p, "set_controls", side_effect=AssertionError("writer mutation forbidden")) as controls, \
                    patch.object(p, "publish_repository", side_effect=AssertionError("push forbidden")) as publish, \
                    patch.object(p.urllib.request, "urlopen", side_effect=AssertionError("network forbidden")), \
                    patch.object(sys, "argv", ["protected_handoff.py", "gate", "--root", str(root)]):
                try:
                    h.main()
                except p.PublicationError:
                    assert (root / "handoff.json").exists(), "failure occurred before the production seam"
                    assert not (root / "preflight/preflight.json").exists()
                    assert not (root / "publication-restoration-intent.json").exists()
                    raise
                finally:
                    mint.assert_not_called(); controls.assert_not_called(); publish.assert_not_called()
            assert len(writer_files) == len(p.WRITER_WORKFLOWS)
            for path in writer_files:
                responses = json.loads(path.read_text())
                assert {row["requested_status"] for row in responses} == p.ACTIVE_RUN_STATES
            preflight = p.load_object(root / "preflight/preflight.json")
            assert preflight["status"] == "verified-before-publisher-token"
            assert preflight["writer_check"] == {"schema": "history-rewrite-writer-check-v1", "active": [], "status": "drained-at-single-read"}
            p.validate_phase_bindings(production_manifest, frozen_sha=source_sha, frozen_tree=source_tree,
                manifest_sha256=p.digest(production_manifest), preflight_path=root / "preflight/preflight.json",
                control_plan=root / "preflight/control-plan.json", intent_path=root / "publication-restoration-intent.json")
            assert not (root / "publication-receipt.json").exists()

    production_preflight()
    with patch.object(p, "load_writer_responses", side_effect=p.load_object):
        failures.append(expect_failure("production_preflight_rejects_previous_object_loader", production_preflight))
    def transformed(change):
        def persist(path, rows):
            change(rows)
            path.write_bytes(p.canonical_json(rows))
        return persist
    def nonempty(rows):
        rows[0]["response"] = {"total_count": 1, "workflow_runs": [
            {"id": 9876, "status": rows[0]["requested_status"], "head_sha": source_sha}]}
    production_negatives = [
        ("writer_object_instead_of_array", lambda path, rows: path.write_text("{}")),
        ("writer_invalid_json", lambda path, rows: path.write_text("[")),
        ("writer_invalid_encoding", lambda path, rows: path.write_bytes(b"\xff")),
        ("writer_file_missing", lambda path, rows: None),
        ("writer_status_missing", transformed(lambda rows: rows.pop())),
        ("writer_status_duplicate", transformed(lambda rows: rows.__setitem__(0, rows[1]))),
        ("writer_response_malformed", transformed(lambda rows: rows[0].__setitem__("response", None))),
        ("writer_count_mismatch", transformed(lambda rows: rows[0]["response"].__setitem__("total_count", 1))),
        ("writer_nonempty", transformed(nonempty)),
        ("writer_status_mismatch", transformed(lambda rows: rows[0].__setitem__("response", {
            "total_count": 1, "workflow_runs": [{"id": 9876, "status": "completed"}]}))),
    ]
    for label, corruption in production_negatives:
        failures.append(expect_failure(label, lambda corruption=corruption: production_preflight(corruption)))
    print(json.dumps({"protected_handoff": "passed", "negative_cases": len(failures), "evidence_sha256": p.digest(failures),
                      "production_preflight": "actual gate and persisted writer arrays through recovery intent; no publisher effects",
                      "production_preflight_negative_cases": len(production_negatives),
                      "compact_comment_bytes": gate["comment_bytes"], "comment_limit_bytes": h.MAX_COMMENT_BYTES}, sort_keys=True))


if __name__ == "__main__":
    main()
