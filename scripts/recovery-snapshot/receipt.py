#!/usr/bin/env python3
"""Build and independently verify non-sensitive recovery snapshot receipts."""

import argparse
import hashlib
import json
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path


HEX40 = re.compile(r"[0-9a-f]{40}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
RECEIPT_NAME = "recovery-snapshot-receipt"
CIPHERTEXT_NAME = "recovery-snapshot-ciphertext"
RECEIPT_FILE = "recovery-snapshot-receipt.json"
SELECTED_REFS_FILE = "selected-ref-map.json"


def fail(message: str) -> None:
    raise SystemExit(f"recovery receipt: {message}")


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def parse_time(value: str) -> datetime:
    if not isinstance(value, str):
        fail("timestamp is not a string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        fail(f"invalid timestamp: {exc}")
    if parsed.tzinfo is None:
        fail("timestamp lacks a timezone")
    return parsed.astimezone(timezone.utc)


def api_digest(artifact: dict) -> str:
    value = artifact.get("digest")
    if not isinstance(value, str) or not value.startswith("sha256:") or not HEX64.fullmatch(value[7:]):
        fail("artifact lacks a complete API SHA-256 digest")
    return value


def validate_artifact(artifact: dict, name: str, run_id: int, now: datetime) -> None:
    if artifact.get("name") != name or not isinstance(artifact.get("id"), int) or artifact["id"] <= 0:
        fail(f"invalid {name} artifact identity")
    if not isinstance(artifact.get("size_in_bytes"), int) or artifact["size_in_bytes"] <= 0:
        fail(f"invalid {name} artifact size")
    api_digest(artifact)
    if artifact.get("expired") is not False or parse_time(artifact.get("expires_at")) <= now:
        fail(f"{name} artifact is expired")
    workflow_run = artifact.get("workflow_run")
    if workflow_run is not None and (not isinstance(workflow_run, dict) or workflow_run.get("id") != run_id):
        fail(f"{name} artifact is not bound to the exact backup run")


def build(ns: argparse.Namespace) -> None:
    package = load(ns.package_info)
    restore = load(ns.restore_info)
    artifact = load(ns.artifact_json)
    tool_versions = load(ns.tool_versions_json)
    if not isinstance(package, dict) or not isinstance(restore, dict) or not isinstance(artifact, dict) or not isinstance(tool_versions, dict):
        fail("build inputs must be JSON objects")
    if ns.repository != "sednalabs/codex" or ns.source_repository != ns.repository:
        fail("receipt repository identity mismatch")
    if not HEX40.fullmatch(ns.workflow_code_sha) or not HEX40.fullmatch(ns.source_sha):
        fail("workflow and source identities must be full commit IDs")
    if ns.run_id <= 0 or ns.run_attempt <= 0 or ns.workflow_id <= 0 or ns.workflow_path != ".github/workflows/recovery-snapshot.yml":
        fail("run or workflow identity is invalid")
    if not ns.workflow_ref.startswith(f"{ns.repository}/{ns.workflow_path}@"):
        fail("workflow ref is not bound to the receipt workflow path")
    if artifact.get("name") != CIPHERTEXT_NAME:
        fail("ciphertext artifact name mismatch")
    api_digest(artifact)
    if artifact.get("expired") is not False:
        fail("ciphertext artifact is already expired")
    selected = {
        "sha256": package.get("selected_refs_sha256"),
        "count": package.get("selected_refs_count"),
        "bytes": package.get("selected_refs_bytes"),
    }
    if not HEX64.fullmatch(str(selected["sha256"] or "")) or not isinstance(selected["count"], int) or selected["count"] <= 0 or not isinstance(selected["bytes"], int) or selected["bytes"] <= 0:
        fail("package lacks a canonical selected-ref map identity")
    if restore.get("selected_refs_sha256") != selected["sha256"] or restore.get("restore_test") != "passed":
        fail("restore result does not bind the selected-ref map")
    receipt = {
        "schema": "recovery-snapshot-receipt-v2",
        "repository": ns.repository,
        "source_repository": ns.source_repository,
        "run_id": ns.run_id,
        "run_attempt": ns.run_attempt,
        "workflow_id": ns.workflow_id,
        "workflow_path": ns.workflow_path,
        "workflow_ref": ns.workflow_ref,
        "workflow_code_sha": ns.workflow_code_sha,
        "source_sha": ns.source_sha,
        "selected_refs": selected,
        "recovery": {
            "inner_sha256": package.get("inner_sha256"),
            "inner_size": package.get("inner_size"),
            "rich_ref_manifest_sha256": package.get("rich_ref_manifest_sha256"),
            "metadata_manifest_sha256": ns.metadata_manifest_sha256,
            "restore_test_identity": restore.get("restore_test_identity"),
        },
        "ciphertext": {"sha256": ns.ciphertext_sha256, "size": ns.ciphertext_size},
        "encryption": {"recipient_sha256": ns.recipient_sha256, "tool_versions": tool_versions},
        "ciphertext_artifact": {
            "id": artifact["id"],
            "name": artifact["name"],
            "api_digest": artifact["digest"],
            "api_size": artifact["size_in_bytes"],
            "expires_at": artifact["expires_at"],
        },
    }
    for value in (
        receipt["recovery"]["inner_sha256"],
        receipt["recovery"]["rich_ref_manifest_sha256"],
        receipt["recovery"]["metadata_manifest_sha256"],
        receipt["recovery"]["restore_test_identity"],
        receipt["ciphertext"]["sha256"],
        receipt["encryption"]["recipient_sha256"],
    ):
        if not HEX64.fullmatch(str(value or "")):
            fail("receipt source data lacks a complete SHA-256 identity")
    ns.output.write_bytes(canonical_json(receipt) + b"\n")


def one_artifact(artifacts: object, name: str) -> dict:
    if not isinstance(artifacts, dict) or not isinstance(artifacts.get("artifacts"), list):
        fail("artifact listing is malformed")
    matches = [item for item in artifacts["artifacts"] if isinstance(item, dict) and item.get("name") == name]
    if len(matches) != 1:
        fail(f"expected exactly one {name} artifact")
    return matches[0]


def receipt_from_zip(path: Path, expected_digest: str) -> tuple[dict, str, bytes, dict]:
    raw = path.read_bytes()
    if digest(raw) != expected_digest.removeprefix("sha256:"):
        fail("receipt artifact download digest mismatch")
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if set(names) != {RECEIPT_FILE, SELECTED_REFS_FILE} or len(names) != 2:
            fail("receipt artifact must contain exactly the receipt and selected-ref map")
        for name in names:
            info = archive.getinfo(name)
            if info.is_dir() or info.file_size <= 0 or info.file_size > 262144:
                fail("receipt artifact member size is invalid")
        raw_receipt = archive.read(RECEIPT_FILE)
        selected_raw = archive.read(SELECTED_REFS_FILE)
    receipt = json.loads(raw_receipt)
    if not isinstance(receipt, dict) or raw_receipt != canonical_json(receipt) + b"\n":
        fail("receipt JSON is not canonical UTF-8 JSON")
    selected = json.loads(selected_raw)
    if not isinstance(selected, dict) or not selected or selected_raw != canonical_json(selected):
        fail("selected-ref map is not canonical non-empty UTF-8 JSON")
    return receipt, digest(raw_receipt), selected_raw, selected


def verify(ns: argparse.Namespace) -> None:
    now = parse_time(ns.now)
    run, workflow, artifacts = load(ns.run_json), load(ns.workflow_json), load(ns.artifacts_json)
    if not isinstance(run, dict) or not isinstance(workflow, dict):
        fail("run and workflow API inputs must be objects")
    if run.get("id") != ns.run_id or run.get("status") != "completed" or run.get("conclusion") != "success" or run.get("event") != "workflow_dispatch":
        fail("backup run is not the exact successful workflow_dispatch")
    if run.get("head_sha") != ns.harness_sha or not HEX40.fullmatch(ns.harness_sha):
        fail("backup run head differs from the frozen harness")
    head_repository = run.get("head_repository")
    if not isinstance(head_repository, dict) or head_repository.get("full_name") != ns.repository:
        fail("backup run repository mismatch")
    if workflow.get("id") != run.get("workflow_id") or workflow.get("path") != ".github/workflows/recovery-snapshot.yml" or workflow.get("state") != "active":
        fail("backup run workflow identity mismatch")
    receipt_artifact = one_artifact(artifacts, RECEIPT_NAME)
    ciphertext_artifact = one_artifact(artifacts, CIPHERTEXT_NAME)
    validate_artifact(receipt_artifact, RECEIPT_NAME, ns.run_id, now)
    validate_artifact(ciphertext_artifact, CIPHERTEXT_NAME, ns.run_id, now)
    receipt, receipt_sha256, selected_raw, selected_map = receipt_from_zip(ns.receipt_zip, api_digest(receipt_artifact))
    expected_selected = {"sha256": ns.selected_refs_sha256, "count": ns.selected_refs_count, "bytes": ns.selected_refs_bytes}
    if not HEX64.fullmatch(ns.selected_refs_sha256) or ns.selected_refs_count <= 0 or ns.selected_refs_bytes <= 0:
        fail("expected selected-ref map identity is invalid")
    if receipt.get("schema") != "recovery-snapshot-receipt-v2" or receipt.get("repository") != ns.repository or receipt.get("source_repository") != ns.repository:
        fail("receipt schema or repository mismatch")
    if receipt.get("run_id") != ns.run_id or receipt.get("run_attempt") != run.get("run_attempt") or receipt.get("workflow_id") != workflow.get("id"):
        fail("receipt run or workflow identity mismatch")
    if receipt.get("workflow_path") != workflow.get("path") or receipt.get("workflow_code_sha") != ns.harness_sha or receipt.get("source_sha") != ns.source_sha:
        fail("receipt workflow or source identity mismatch")
    if not isinstance(receipt.get("workflow_ref"), str) or not receipt["workflow_ref"].startswith(f"{ns.repository}/{workflow['path']}@"):
        fail("receipt workflow ref mismatch")
    if receipt.get("selected_refs") != expected_selected:
        fail("receipt selected-ref map identity mismatch")
    if digest(selected_raw) != expected_selected["sha256"] or len(selected_raw) != expected_selected["bytes"] or len(selected_map) != expected_selected["count"]:
        fail("selected-ref map content differs from its receipt identity")
    if selected_map.get("refs/heads/main") != ns.source_sha:
        fail("selected-ref map main differs from the bound source SHA")
    ciphertext = receipt.get("ciphertext_artifact")
    expected_ciphertext = {
        "id": ciphertext_artifact["id"],
        "name": ciphertext_artifact["name"],
        "api_digest": api_digest(ciphertext_artifact),
        "api_size": ciphertext_artifact["size_in_bytes"],
        "expires_at": ciphertext_artifact["expires_at"],
    }
    if ciphertext != expected_ciphertext:
        fail("receipt ciphertext artifact metadata differs from independent API readback")
    ciphertext_identity = receipt.get("ciphertext")
    if not isinstance(ciphertext_identity, dict) or not HEX64.fullmatch(str(ciphertext_identity.get("sha256", ""))) or not isinstance(ciphertext_identity.get("size"), int) or ciphertext_identity["size"] <= 0:
        fail("receipt lacks the encrypted file identity")
    encryption = receipt.get("encryption")
    if not isinstance(encryption, dict) or not HEX64.fullmatch(str(encryption.get("recipient_sha256", ""))) or not isinstance(encryption.get("tool_versions"), dict) or not encryption["tool_versions"]:
        fail("receipt lacks encryption recipient or tool provenance")
    recovery = receipt.get("recovery")
    if not isinstance(recovery, dict) or any(not HEX64.fullmatch(str(recovery.get(key, ""))) for key in ("inner_sha256", "rich_ref_manifest_sha256", "metadata_manifest_sha256", "restore_test_identity")):
        fail("receipt lacks complete recovery identities")
    verified = {
        "schema": "verified-recovery-snapshot-v1",
        "repository": ns.repository,
        "run_id": ns.run_id,
        "workflow_id": workflow["id"],
        "workflow_code_sha": ns.harness_sha,
        "source_sha": ns.source_sha,
        "selected_refs": expected_selected,
        "receipt_sha256": receipt_sha256,
        "receipt_artifact": {
            "id": receipt_artifact["id"],
            "api_digest": api_digest(receipt_artifact),
            "api_size": receipt_artifact["size_in_bytes"],
            "expires_at": receipt_artifact["expires_at"],
        },
        "ciphertext_artifact": expected_ciphertext,
    }
    ns.output.write_bytes(canonical_json(verified) + b"\n")
    ns.selected_refs_output.write_bytes(selected_raw)


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    build_parser = sub.add_parser("build")
    build_parser.add_argument("--repository", required=True)
    build_parser.add_argument("--source-repository", required=True)
    build_parser.add_argument("--run-id", type=int, required=True)
    build_parser.add_argument("--run-attempt", type=int, required=True)
    build_parser.add_argument("--workflow-id", type=int, required=True)
    build_parser.add_argument("--workflow-path", required=True)
    build_parser.add_argument("--workflow-ref", required=True)
    build_parser.add_argument("--workflow-code-sha", required=True)
    build_parser.add_argument("--source-sha", required=True)
    build_parser.add_argument("--package-info", type=Path, required=True)
    build_parser.add_argument("--restore-info", type=Path, required=True)
    build_parser.add_argument("--artifact-json", type=Path, required=True)
    build_parser.add_argument("--metadata-manifest-sha256", required=True)
    build_parser.add_argument("--ciphertext-sha256", required=True)
    build_parser.add_argument("--ciphertext-size", type=int, required=True)
    build_parser.add_argument("--recipient-sha256", required=True)
    build_parser.add_argument("--tool-versions-json", type=Path, required=True)
    build_parser.add_argument("--output", type=Path, required=True)
    verify_parser = sub.add_parser("verify")
    verify_parser.add_argument("--repository", required=True)
    verify_parser.add_argument("--run-id", type=int, required=True)
    verify_parser.add_argument("--harness-sha", required=True)
    verify_parser.add_argument("--source-sha", required=True)
    verify_parser.add_argument("--selected-refs-sha256", required=True)
    verify_parser.add_argument("--selected-refs-count", type=int, required=True)
    verify_parser.add_argument("--selected-refs-bytes", type=int, required=True)
    verify_parser.add_argument("--now", required=True)
    verify_parser.add_argument("--run-json", type=Path, required=True)
    verify_parser.add_argument("--workflow-json", type=Path, required=True)
    verify_parser.add_argument("--artifacts-json", type=Path, required=True)
    verify_parser.add_argument("--receipt-zip", type=Path, required=True)
    verify_parser.add_argument("--output", type=Path, required=True)
    verify_parser.add_argument("--selected-refs-output", type=Path, required=True)
    args = parser.parse_args()
    (build if args.command == "build" else verify)(args)


if __name__ == "__main__":
    main()
