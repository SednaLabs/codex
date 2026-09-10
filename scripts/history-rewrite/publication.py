#!/usr/bin/env python3
"""Guarded publication primitives for the w13828 history rewrite.

This module deliberately knows nothing about GitHub credentials.  Callers provide
an already-authorised environment snapshot, a token minting callback, and a small
remote adapter (the fixtures use a local mock).  Validation is completed before
the minting callback is invoked.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Callable, Mapping, Protocol, Sequence

OID = re.compile(r"^[0-9a-f]{40}$")
REF = re.compile(r"^refs/(?:heads|tags)/[A-Za-z0-9._/-]+$")


class PublicationError(RuntimeError):
    """A fail-closed publication rejection."""


class Remote(Protocol):
    def push_atomic(self, updates: Sequence[tuple[str, str, str]]) -> None:
        """Push (ref, new oid, expected old oid) updates atomically."""

    def read_refs(self, names: Sequence[str]) -> Mapping[str, str]:
        """Read back the requested refs after a push."""


@dataclass(frozen=True)
class PublicationResult:
    outcome: str
    reason: str
    after_refs: Mapping[str, str]
    manifest_sha256: str


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _ref_map(value: object, label: str, *, allow_empty: bool = False) -> dict[str, str]:
    if not isinstance(value, dict) or (not value and not allow_empty):
        raise PublicationError(f"{label} must be a non-empty object")
    result: dict[str, str] = {}
    for name, oid in value.items():
        if not isinstance(name, str) or not REF.fullmatch(name):
            raise PublicationError(f"{label} contains invalid ref: {name!r}")
        if not isinstance(oid, str) or not OID.fullmatch(oid):
            raise PublicationError(f"{label} contains invalid object for {name}")
        result[name] = oid
    return result


def validate_manifest(manifest: Mapping[str, object], *, frozen_sha: str, frozen_tree: str,
                      expected_environment: Mapping[str, object], environment: Mapping[str, object],
                      approval_identity: str) -> tuple[dict[str, str], dict[str, str]]:
    """Validate the complete immutable binding and independently checked environment."""
    if manifest.get("repository") != "sednalabs/codex":
        raise PublicationError("manifest repository mismatch")
    if manifest.get("harness_sha") != frozen_sha or manifest.get("harness_tree") != frozen_tree:
        raise PublicationError("manifest harness identity mismatch")
    if manifest.get("approval_identity") != approval_identity or not approval_identity:
        raise PublicationError("manifest approval identity mismatch")
    if manifest.get("tag_signature_ack") is not True:
        raise PublicationError("tag signature acknowledgement is required")
    if environment != expected_environment:
        raise PublicationError("required reviewer environment missing or mismatched")
    selected = _ref_map(manifest.get("selected_refs"), "selected_refs")
    output = _ref_map(manifest.get("output_refs"), "output_refs")
    if set(selected) != set(output):
        raise PublicationError("output ref domain differs from selected ref domain")
    if manifest.get("selected_refs_sha256") != digest(selected):
        raise PublicationError("selected ref proof digest mismatch")
    if manifest.get("output_refs_sha256") != digest(output):
        raise PublicationError("output ref proof digest mismatch")
    proofs = manifest.get("proof_digests")
    if not isinstance(proofs, dict) or not proofs or any(not isinstance(k, str) or not isinstance(v, str) or not OID.fullmatch(v) for k, v in proofs.items()):
        raise PublicationError("proof digests are missing or malformed")
    if manifest.get("proof_digests_sha256") != digest(proofs):
        raise PublicationError("proof digest map mismatch")
    backup = manifest.get("backup")
    if not isinstance(backup, dict) or not backup.get("run_id") or not backup.get("artifact_sha256"):
        raise PublicationError("stale or incomplete backup identity")
    return selected, output


def publish(manifest: Mapping[str, object], *, frozen_sha: str, frozen_tree: str,
            expected_environment: Mapping[str, object], environment: Mapping[str, object],
            approval_identity: str, token_minter: Callable[[], object], remote: Remote,
            active_writers: Callable[[], bool], restore: Callable[[], None]) -> PublicationResult:
    """Publish explicit refs once, classifying transport failures without retries."""
    selected, output = validate_manifest(manifest, frozen_sha=frozen_sha, frozen_tree=frozen_tree,
                                         expected_environment=expected_environment, environment=environment,
                                         approval_identity=approval_identity)
    if active_writers():
        raise PublicationError("active writers must be drained before publication")
    # Credential access is intentionally after every preflight gate.
    token_minter()
    updates = [(name, output[name], selected[name]) for name in sorted(selected)]
    try:
        remote.push_atomic(updates)
    except Exception as exc:
        # A transport exception is ambiguous until all refs are read back.  The
        # caller must decide whether the resulting map is safe; this function
        # never retries blindly.
        try:
            after = dict(remote.read_refs(sorted(selected)))
        except Exception as read_exc:
            restore()
            raise PublicationError(f"ambiguous push; readback failed: {read_exc}") from exc
        if after == output:
            return PublicationResult("success", "transport exception resolved by exact readback", after, digest(manifest))
        restore()
        return PublicationResult("ambiguous", str(exc), after, digest(manifest))
    after = dict(remote.read_refs(sorted(selected)))
    if after != output:
        restore()
        raise PublicationError("push succeeded but exact after-map verification failed")
    return PublicationResult("success", "atomic push and clean fetch verified", after, digest(manifest))


def _cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=argparse.FileType("r"))
    parser.add_argument("--frozen-sha", required=True)
    parser.add_argument("--frozen-tree", required=True)
    parser.add_argument("--environment", required=True, help="JSON environment snapshot")
    parser.add_argument("--approval-identity", required=True)
    parser.add_argument("--expected-environment", required=True, help="JSON expected snapshot")
    args = parser.parse_args()
    manifest = json.load(args.manifest)
    validate_manifest(manifest, frozen_sha=args.frozen_sha, frozen_tree=args.frozen_tree,
                      expected_environment=json.loads(args.expected_environment), environment=json.loads(args.environment),
                      approval_identity=args.approval_identity)
    print(json.dumps({"outcome": "ready", "manifest_sha256": digest(manifest)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
