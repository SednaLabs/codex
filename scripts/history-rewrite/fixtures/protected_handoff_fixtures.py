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
from publication_fixtures import base_manifest, expect_failure


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

    def approval(value):
        return {"state": "approved", "user": {"id": p.REVIEWER_ID, "login": p.REVIEWER_LOGIN},
                "environments": [{"id": h.ENVIRONMENT_ID, "name": p.ENVIRONMENT_NAME}], "comment": p.canonical_json(value).decode()}

    def validate(items, expected=binding, current_run=run):
        return h.validate_attestation(items, expected, p.digest(administrator), current_run, now=now)

    records = [approval(attestation)]
    assert validate(records).document == attestation
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
    print(json.dumps({"protected_handoff": "passed", "negative_cases": len(failures), "evidence_sha256": p.digest(failures),
                      "compact_comment_bytes": gate["comment_bytes"], "comment_limit_bytes": h.MAX_COMMENT_BYTES}, sort_keys=True))


if __name__ == "__main__":
    main()
