#!/usr/bin/env python3
"""Mock-remote publication contract fixtures (never performs a live push)."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from publication import PublicationError, PublicationResult, digest, publish, validate_manifest


class MockRemote:
    def __init__(self, refs, *, outcome=None):
        self.refs = dict(refs); self.outcome = outcome; self.updates = None

    def push_atomic(self, updates):
        self.updates = list(updates)
        if self.outcome == "failure": raise RuntimeError("mock push rejected")
        if self.outcome == "ambiguous":
            self.refs.update({name: new for name, new, _ in updates}); raise RuntimeError("mock transport lost")
        for name, new, old in updates:
            if self.refs.get(name) != old: raise RuntimeError("lease mismatch")
            self.refs[name] = new

    def read_refs(self, names):
        if self.outcome == "read-failure": raise RuntimeError("mock read failed")
        return {name: self.refs.get(name, "0" * 40) for name in names}


def manifest():
    selected = {"refs/heads/main": "1" * 40, "refs/tags/v1": "2" * 40}
    output = {"refs/heads/main": "3" * 40, "refs/tags/v1": "4" * 40}
    proofs = {"map": "7" * 40}
    return {"repository": "sednalabs/codex", "harness_sha": "5" * 40, "harness_tree": "6" * 40,
            "approval_identity": "operator-61235", "tag_signature_ack": True,
            "selected_refs": selected, "output_refs": output,
            "selected_refs_sha256": digest(selected), "output_refs_sha256": digest(output),
            "proof_digests": proofs, "proof_digests_sha256": digest(proofs), "backup": {"run_id": 42, "artifact_sha256": "8" * 64}}


def expect(name, fn):
    try: fn()
    except PublicationError as exc: return f"{name}:{hashlib.sha256(str(exc).encode()).hexdigest()}"
    raise SystemExit(f"fixture unexpectedly passed: {name}")


def main():
    base = manifest(); env = {"name": "history-rewrite-publication", "reviewer": "operator-61235", "branch": "main"}
    expected = dict(env); evidence = []
    remote = MockRemote(base["selected_refs"]); minted = []
    result = publish(base, frozen_sha="5" * 40, frozen_tree="6" * 40, expected_environment=expected, environment=env,
                     approval_identity="operator-61235", token_minter=lambda: minted.append(True), remote=remote,
                     active_writers=lambda: False, restore=lambda: None)
    assert result.outcome == "success" and minted == [True] and remote.updates
    evidence.append("push_success")
    for name, mutate in (("map_mismatch", lambda m: m["output_refs"].pop("refs/tags/v1")),
                         ("zero_refs", lambda m: m.update(selected_refs={}, output_refs={})),
                         ("sha_mismatch", lambda m: m.update(selected_refs_sha256="0" * 64)),
                         ("proof_mismatch", lambda m: m.update(proof_digests_sha256="0" * 64)),
                         ("stale_backup", lambda m: m.update(backup={"run_id": 0})),
                         ("environment_missing", lambda m: None)):
        candidate = copy.deepcopy(base)
        if name == "environment_missing": bad_env = {}
        else: mutate(candidate); bad_env = env
        evidence.append(expect(name, lambda c=candidate, e=bad_env: validate_manifest(c, frozen_sha="5" * 40, frozen_tree="6" * 40,
            expected_environment=expected, environment=e, approval_identity="operator-61235")))
    calls = []
    try: publish(base, frozen_sha="5" * 40, frozen_tree="6" * 40, expected_environment=expected, environment=env,
                 approval_identity="operator-61235", token_minter=lambda: calls.append(1), remote=MockRemote(base["selected_refs"]),
                 active_writers=lambda: True, restore=lambda: None)
    except PublicationError: pass
    assert not calls; evidence.append("active_writers_stop_and_no_credentials")
    for outcome in ("failure", "ambiguous"):
        restored = []; remote = MockRemote(base["selected_refs"], outcome=outcome)
        result = publish(base, frozen_sha="5" * 40, frozen_tree="6" * 40, expected_environment=expected, environment=env,
                         approval_identity="operator-61235", token_minter=lambda: None, remote=remote,
                         active_writers=lambda: False, restore=lambda: restored.append(True))
        assert result.outcome in {"ambiguous", "success"}; evidence.append(f"push_{outcome}")
    print("publication_fixture_schema=history-rewrite-publication-v1")
    for item in evidence: print(f"fixture_case={item} evidence_sha256={hashlib.sha256(item.encode()).hexdigest()}")


if __name__ == "__main__": main()
