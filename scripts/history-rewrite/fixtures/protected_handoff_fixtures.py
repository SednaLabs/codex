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
    witness = {**binding, "observed_at": (now - timedelta(seconds=15)).isoformat(),
               "expires_at": (now + timedelta(seconds=585)).isoformat(),
               "administrator_snapshot": administrator, "administrator_snapshot_sha256": p.digest(administrator)}

    def approval(value):
        return {"state": "approved", "user": {"id": p.REVIEWER_ID, "login": p.REVIEWER_LOGIN},
                "environments": [{"id": h.ENVIRONMENT_ID, "name": p.ENVIRONMENT_NAME}], "comment": p.canonical_json(value).decode()}

    records = [approval(witness)]
    assert h.validate_witness(records, binding, run, now=now).document == witness
    old = {**witness, "run_attempt": 1}
    assert h.validate_witness([approval(old), *records], binding, run, now=now).document == witness
    failures = []
    for key in ("run_id", "run_attempt", "phase", "repository", "repository_id", "harness_sha", "harness_tree", "manifest_sha256",
                "selected_refs_sha256", "output_refs_sha256", "prepared_sha256", "prepared_artifact"):
        bad = copy.deepcopy(witness); bad[key] = None
        failures.append(expect_failure("wrong_" + key, lambda: h.validate_witness([approval(bad)], binding, run, now=now)))
    for label, bad in (
        ("expired", {**witness, "expires_at": now.isoformat()}),
        ("future", {**witness, "observed_at": (now + timedelta(seconds=1)).isoformat()}),
        ("overlong", {**witness, "expires_at": (now + timedelta(seconds=586)).isoformat()}),
        ("malformed_time", {**witness, "observed_at": None}),
        ("wrong_snapshot_digest", {**witness, "administrator_snapshot_sha256": "0" * 64}),
    ):
        failures.append(expect_failure(label, lambda: h.validate_witness([approval(bad)], binding, run, now=now)))
    for label, changed in (("missing", []), ("duplicate", records * 2),
        ("foreign_reviewer", [{**records[0], "user": {"id": 1, "login": "other"}}]),
        ("malformed_comment", [{**records[0], "comment": "approved"}]),
        ("oversized_comment", [{**records[0], "comment": " " * 60001}]),
        ("wrong_environment_name", [{**records[0], "environments": [{"id": h.ENVIRONMENT_ID, "name": "other"}]}]),
        ("rejected", [{**records[0], "state": "rejected"}])):
        failures.append(expect_failure(label, lambda: h.validate_witness(changed, binding, run, now=now)))
    for mutate in ("null_actor", "hidden_ruleset"):
        changed = copy.deepcopy(administrator)
        if mutate == "null_actor":
            changed["branch_protection_rules"][0]["push_allowances"] = [None]
        else:
            changed["repository_rulesets"][0].update(bypass_actors_visibility="not_returned", bypass_actors=None)
        bad = {**witness, "administrator_snapshot": changed, "administrator_snapshot_sha256": p.digest(changed)}
        failures.append(expect_failure(mutate, lambda: h.validate_witness([approval(bad)], binding, run, now=now)))
    failures.append(expect_failure("foreign_run_attempt", lambda: h.validate_witness(records, binding, {**run, "run_attempt": 3}, now=now)))

    class ReadApi:
        def get(self, path):
            if path.endswith("/approvals"):
                return records
            assert path.endswith("/attempts/2")
            return run

    class ObserverApi:
        changed_count = False
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
            return {"data": {"repository": {"branchProtectionRules": {"totalCount": len(nodes), "nodes": nodes}}}}
        def get(self, path):
            if "?" in path:
                return [{"id": row["id"]} for row in administrator["repository_rulesets"]]
            return {key: value for key, value in administrator["repository_rulesets"][0].items()
                    if key not in {"bypass_actors", "bypass_actors_visibility"}}

    observer = ObserverApi()
    gate = h.verify_current(manifest, binding, ReadApi(), observer, now=now)
    assert gate["comment_bytes"] == len(records[0]["comment"].encode()) and gate["comment_sha256"] == p.digest(witness)
    observer.changed_count = True
    failures.append(expect_failure("observer_count_mismatch", lambda: h.verify_current(manifest, binding, ReadApi(), observer, now=now)))
    observer.changed_count, observer.denied = False, True
    with tempfile.TemporaryDirectory() as temporary:
        error = Path(temporary) / "error.json"
        failures.append(expect_failure("observer_denial", lambda: h.observe(observer, error_path=error)))
        assert p.load_object(error)["errors"][0]["type"] == "FORBIDDEN"
    observer.denied = False
    mismatched = copy.deepcopy(administrator)
    mismatched["branch_protection_rules"][0]["push_allowances"][0]["databaseId"] += 1
    records = [approval({**witness, "administrator_snapshot": mismatched, "administrator_snapshot_sha256": p.digest(mismatched)})]
    failures.append(expect_failure("wrong_actual_actor", lambda: h.verify_current(manifest, binding, ReadApi(), observer, now=now)))
    records = [approval(witness)]
    class Clock:
        @staticmethod
        def now(zone): return now
    with patch.dict(os.environ, {"GITHUB_RUN_ID": "22", "GITHUB_RUN_ATTEMPT": "2"}), patch.object(h, "datetime", Clock):
        # timestamp parsing needs the genuine datetime parser.
        Clock.fromisoformat = datetime.fromisoformat
        h.require_fresh_gate(gate, manifest)
        failures.append(expect_failure("expired_before_effects", lambda: h.require_fresh_gate({**gate, "expires_at": now.isoformat()}, manifest)))
        changed = copy.deepcopy(gate); changed["binding"]["phase"] = "qualification"
        failures.append(expect_failure("qualification_cannot_publish", lambda: h.require_fresh_gate(changed, manifest)))
    print(json.dumps({"protected_handoff": "passed", "negative_cases": len(failures), "evidence_sha256": p.digest(failures),
                      "full_comment_bytes": gate["comment_bytes"]}, sort_keys=True))


if __name__ == "__main__":
    main()
