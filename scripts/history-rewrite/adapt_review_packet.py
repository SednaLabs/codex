#!/usr/bin/env python3
"""Convert a reviewed, non-executable rule packet into driver policy.

The private review packet is evidence, not code.  This adapter verifies its
identity and closed schema, then emits a public policy containing only exact
object identities and mechanical replacements.  Canonical document contents
are retrieved from Git objects by the rewrite driver; they are never copied
into this policy.
"""

import argparse
import hashlib
import json
import re
from pathlib import Path


HEX40 = re.compile(r"[0-9a-f]{40}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
ALLOWED_PACKET_KEYS = {
    "schema",
    "executable",
    "repository",
    "canonical_source",
    "ops_receipt",
    "purpose",
    "rules",
    "requirements",
}
EXPECTED_KINDS = (
    "exact_blob_replacement",
    "exact_blob_replacement",
    "path",
    "blob_literal",
    "blob_literal",
    "blob_literal",
    "blob_literal",
)


def fail(message: str) -> None:
    raise SystemExit(f"review packet adapter: {message}")


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_packet(raw: bytes, expected_sha256: str, expected_bytes: int) -> dict:
    if not HEX64.fullmatch(expected_sha256) or sha256(raw) != expected_sha256:
        fail("packet SHA-256 does not match the bound identity")
    if len(raw) != expected_bytes:
        fail("packet byte length does not match the bound identity")
    packet = json.loads(raw)
    if not isinstance(packet, dict) or set(packet) != ALLOWED_PACKET_KEYS:
        fail("packet has an unsupported field set")
    if packet.get("schema") != "review-packet-v1" or packet.get("executable") is not False:
        fail("packet must be review-packet-v1 and explicitly non-executable")
    if packet.get("repository") != "sednalabs/codex":
        fail("packet repository is not sednalabs/codex")
    if not HEX40.fullmatch(str(packet.get("canonical_source", ""))):
        fail("canonical source must be a full commit ID")
    if not isinstance(packet.get("ops_receipt"), str) or not packet["ops_receipt"]:
        fail("packet lacks its Ops review receipt")
    rules = packet.get("rules")
    if not isinstance(rules, list) or tuple(rule.get("kind") for rule in rules if isinstance(rule, dict)) != EXPECTED_KINDS:
        fail("packet must contain the reviewed ordered seven-rule shape")
    ids = [rule.get("id") for rule in rules]
    if not all(isinstance(value, str) and value for value in ids) or len(ids) != len(set(ids)):
        fail("rule IDs must be unique non-empty strings")
    return packet


def adapt(packet: dict, packet_sha256: str, packet_bytes: int) -> dict:
    path_rules = [rule for rule in packet["rules"] if rule["kind"] == "path"]
    rename = {rule["old"]: rule["new"] for rule in path_rules}
    rules = []
    for rule in packet["rules"]:
        kind = rule["kind"]
        if kind == "exact_blob_replacement":
            path = rule.get("path")
            old_blob, new_blob = rule.get("old_blob"), rule.get("new_blob")
            if not isinstance(path, str) or not path or not HEX40.fullmatch(str(old_blob)) or not HEX40.fullmatch(str(new_blob)) or old_blob == new_blob:
                fail(f"{rule.get('id')}: invalid exact blob replacement")
            rules.append(
                {
                    "id": rule["id"],
                    "kind": kind,
                    "scope": "blob",
                    "target_paths": [path],
                    "old_blob": old_blob,
                    "new_blob": new_blob,
                    "source_path": rename.get(path, path),
                    "classification": "inaccurate_provenance",
                    "proof": f"review-packet:{packet_sha256}#{rule['id']}",
                }
            )
            continue
        if kind not in {"path", "blob_literal"}:
            fail(f"{rule.get('id')}: unsupported rule kind")
        old, new, target_paths = rule.get("old"), rule.get("new"), rule.get("target_paths")
        if not isinstance(old, str) or not isinstance(new, str) or not old or not new or old == new:
            fail(f"{rule.get('id')}: invalid mechanical replacement")
        if not isinstance(target_paths, list) or not target_paths or not all(isinstance(path, str) and path for path in target_paths):
            fail(f"{rule.get('id')}: target_paths must be non-empty strings")
        rules.append(
            {
                "id": rule["id"],
                "kind": kind,
                "scope": "path" if kind == "path" else "blob",
                "old": old,
                "new": new,
                "target_paths": target_paths,
                "classification": "mechanical_reference",
                "proof": f"review-packet:{packet_sha256}#{rule['id']}",
            }
        )
    return {
        "schema": 2,
        "policy_id": "w13828-history-rewrite-reviewed-v2",
        "repository": packet["repository"],
        "canonical_source_commit": packet["canonical_source"],
        "review_packet": {
            "schema": packet["schema"],
            "sha256": packet_sha256,
            "bytes": packet_bytes,
            "executable": False,
            "ops_receipt": packet["ops_receipt"],
        },
        "rules": rules,
        "ambiguous": [],
        "patterns": [
            {
                "id": "security-circumvention-context",
                "pattern": "(?i)policy\\s+circumvention|security\\s+research|responsible\\s+disclosure",
                "classification": "preserve_accurate_unrelated_security_or_research",
                "priority": 100,
                "rationale": "Security and research references are unrelated provenance and must remain unchanged.",
            },
            {
                "id": "cleanroom-legal-context",
                "pattern": "(?i)clean[- ]room|legal\\s+(review|context)|copyright\\s+(notice|holder)|licen[cs]e\\s+(review|text)",
                "classification": "preserve_accurate_unrelated_security_or_research",
                "priority": 100,
                "rationale": "Legal and implementation-method terms remain contextual outside exact reviewed rules.",
            },
        ],
        "notes": "Generated from a non-executable reviewed packet. Exact document bytes are retrieved by object ID; unmatched variants remain review-required.",
    }


def validate_policy(policy: dict) -> None:
    if not isinstance(policy, dict) or policy.get("schema") != 2 or policy.get("repository") != "sednalabs/codex":
        fail("unsupported policy schema or repository")
    source = policy.get("canonical_source_commit")
    if not HEX40.fullmatch(str(source or "")):
        fail("policy lacks a full canonical source commit")
    review = policy.get("review_packet")
    if not isinstance(review, dict) or review.get("schema") != "review-packet-v1" or review.get("executable") is not False:
        fail("policy must preserve the packet's non-executable status")
    if not HEX64.fullmatch(str(review.get("sha256", ""))) or not isinstance(review.get("bytes"), int) or review["bytes"] <= 0:
        fail("policy lacks the packet content identity")
    rules = policy.get("rules")
    if not isinstance(rules, list) or tuple(rule.get("kind") for rule in rules if isinstance(rule, dict)) != EXPECTED_KINDS:
        fail("policy lacks the ordered seven-rule adapter result")
    seen_noncanonical = False
    for rule in rules:
        if rule["kind"] == "exact_blob_replacement":
            if seen_noncanonical:
                fail("canonical replacements must precede mechanical rules")
            if not HEX40.fullmatch(str(rule.get("old_blob", ""))) or not HEX40.fullmatch(str(rule.get("new_blob", ""))):
                fail(f"{rule.get('id')}: invalid object guard")
            if not isinstance(rule.get("source_path"), str) or not rule["source_path"]:
                fail(f"{rule.get('id')}: source_path is required")
        else:
            seen_noncanonical = True
            if rule.get("scope") not in {"path", "blob"} or not rule.get("old") or not rule.get("new"):
                fail(f"{rule.get('id')}: invalid mechanical adapter rule")


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    generate = sub.add_parser("generate")
    generate.add_argument("--packet", type=Path, required=True)
    generate.add_argument("--expected-sha256", required=True)
    generate.add_argument("--expected-bytes", type=int, required=True)
    generate.add_argument("--output", type=Path, required=True)
    check = sub.add_parser("check-policy")
    check.add_argument("--policy", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "check-policy":
        validate_policy(json.loads(args.policy.read_text(encoding="utf-8")))
        return
    raw = args.packet.read_bytes()
    policy = adapt(validate_packet(raw, args.expected_sha256, args.expected_bytes), args.expected_sha256, args.expected_bytes)
    validate_policy(policy)
    args.output.write_bytes(canonical_json(policy) + b"\n")


if __name__ == "__main__":
    main()
