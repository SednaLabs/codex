#!/usr/bin/env python3
"""Read-only, immutable artifact transport for an externally approved manifest.

The producer reconstructs only ref maps from already bound proof metadata. Its
artifact is transport, not new rewrite/backup proof or publication authority.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import selectors
import stat
import subprocess
import tempfile
import time
from datetime import datetime, timezone
import zipfile

import publication as p


WORKFLOW = ".github/workflows/history-rewrite-candidate.yml"
MANIFEST_NAME = "publication-manifest.json"
MANIFEST_LIMIT = 1024 * 1024
ZIP_LIMIT = 2 * MANIFEST_LIMIT
PROOF_ZIP_LIMIT = 64 * MANIFEST_LIMIT
DESCRIPTOR_KEYS = {"schema", "repository", "run_id", "head_sha", "artifact_id", "artifact_api_digest", "artifact_size"}


def canonical_manifest(raw: bytes, expected: str) -> dict:
    p._sha256(expected, "external manifest digest")
    if not raw or len(raw) > MANIFEST_LIMIT:
        raise p.PublicationError("manifest bytes exceed the bound or are empty")
    try:
        value = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise p.PublicationError("manifest is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict) or raw != p.canonical_json(value) or hashlib.sha256(raw).hexdigest() != expected:
        raise p.PublicationError("canonical manifest or external digest mismatch")
    return value


def bounded_download(artifact_id: int, target: Path, size: int) -> None:
    """Let gh handle its signed redirect; cap elapsed time and output bytes."""
    deadline = time.monotonic() + 120
    with tempfile.TemporaryFile() as errors, target.open("xb") as destination:
        process = subprocess.Popen(
            ["gh", "api", f"repos/{p.REPOSITORY}/actions/artifacts/{artifact_id}/zip"],
            stdout=subprocess.PIPE, stderr=errors,
        )
        count = 0
        try:
            with selectors.DefaultSelector() as ready:
                ready.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not ready.select(remaining):
                        raise p.PublicationError("manifest artifact download timed out")
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        break
                    count += len(chunk)
                    if count > size:
                        raise p.PublicationError("artifact download exceeded the approved size")
                    destination.write(chunk)
            remaining = deadline - time.monotonic()
            if remaining <= 0 or process.wait(timeout=remaining) != 0 or count != size:
                raise p.PublicationError("artifact download failed or size mismatched")
        except subprocess.TimeoutExpired as exc:
            raise p.PublicationError("manifest artifact download timed out") from exc
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdout.close()


def artifact_metadata(api, *, run_id: int, head_sha: str, artifact_id: int,
                      api_digest: str, name: str, size: int | None, limit: int) -> dict:
    p._positive_int(run_id, "producer run id")
    p._positive_int(artifact_id, "artifact id")
    p._sha256(api_digest, "artifact API digest")
    if not isinstance(head_sha, str) or not p.OID.fullmatch(head_sha):
        raise p.PublicationError("producer head is not an exact object ID")
    run = api.get(f"/repos/{p.REPOSITORY}/actions/runs/{run_id}")
    if not isinstance(run, dict):
        raise p.PublicationError("producer run metadata is malformed")
    workflow_id = p._positive_int(run.get("workflow_id"), "producer workflow id")
    workflow = api.get(f"/repos/{p.REPOSITORY}/actions/workflows/{workflow_id}")
    p.validate_run(run, workflow, run_id=run_id, harness_sha=head_sha, workflow_path=WORKFLOW)
    if run.get("head_repository", {}).get("id") != p.REPOSITORY_ID or run.get("head_branch") != p.PUBLICATION_BRANCH:
        raise p.PublicationError("producer repository id or branch mismatch")
    metadata = api.get(f"/repos/{p.REPOSITORY}/actions/artifacts/{artifact_id}")
    if not isinstance(metadata, dict):
        raise p.PublicationError("artifact metadata is malformed")
    actual_size = p._positive_int(metadata.get("size_in_bytes"), "artifact size")
    binding = metadata.get("workflow_run")
    if (metadata.get("id") != artifact_id or metadata.get("name") != name
            or metadata.get("digest") != "sha256:" + api_digest or metadata.get("expired") is not False
            or actual_size > limit or (size is not None and actual_size != size)
            or not isinstance(binding, dict) or binding.get("id") != run_id
            or binding.get("head_sha") != head_sha or binding.get("repository_id") != p.REPOSITORY_ID):
        raise p.PublicationError("artifact identity, size, digest, expiry or run binding mismatch")
    try:
        expires = datetime.fromisoformat(metadata["expires_at"].replace("Z", "+00:00"))
        if expires.tzinfo is None or expires <= datetime.now(timezone.utc):
            raise ValueError("expired")
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise p.PublicationError("artifact expiry is invalid or elapsed") from exc
    return metadata


def read_members(path: Path, api_digest: str, size: int, names: set[str], *, exact: bool) -> dict[str, bytes]:
    if path.stat().st_size != size or p.file_digest(path) != api_digest:
        raise p.PublicationError("download bytes differ from the bound artifact")
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            listed = [entry.filename for entry in entries]
            if len(listed) != len(set(listed)) or not names <= set(listed) or (exact and set(listed) != names):
                raise p.PublicationError("artifact member domain is missing, extra or duplicated")
            result = {}
            for name in names:
                entry = archive.getinfo(name)
                if (entry.is_dir() or stat.S_ISLNK(entry.external_attr >> 16) or entry.flag_bits & 1
                        or not 0 < entry.file_size <= MANIFEST_LIMIT):
                    raise p.PublicationError("artifact member is encrypted, empty or oversized")
                with archive.open(entry) as source:
                    raw = source.read(MANIFEST_LIMIT + 1)
                if len(raw) != entry.file_size or len(raw) > MANIFEST_LIMIT:
                    raise p.PublicationError("artifact member length mismatch")
                result[name] = raw
            return result
    except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
        raise p.PublicationError("invalid manifest transport archive") from exc


def read_manifest(descriptor: dict, expected: str, api, output: Path, download=bounded_download) -> dict:
    if (not isinstance(descriptor, dict) or set(descriptor) != DESCRIPTOR_KEYS
            or descriptor.get("schema") != "history-rewrite-manifest-artifact-v1"
            or descriptor.get("repository") != p.REPOSITORY):
        raise p.PublicationError("manifest transport descriptor identity mismatch")
    p._sha256(expected, "external manifest digest")
    size = p._positive_int(descriptor["artifact_size"], "approved artifact size")
    metadata = artifact_metadata(
        api, run_id=descriptor["run_id"], head_sha=descriptor["head_sha"],
        artifact_id=descriptor["artifact_id"], api_digest=descriptor["artifact_api_digest"],
        name=f"history-rewrite-publication-manifest-{descriptor['run_id']}", size=size, limit=ZIP_LIMIT,
    )
    with tempfile.TemporaryDirectory(prefix="manifest-transport-") as directory:
        archive = Path(directory) / "manifest.zip"
        download(descriptor["artifact_id"], archive, size)
        raw = read_members(archive, descriptor["artifact_api_digest"], size, {MANIFEST_NAME}, exact=True)[MANIFEST_NAME]
        manifest = canonical_manifest(raw, expected)
    # This is a transport/schema check only. Publication still binds this
    # manifest to the actual running harness and independently checks all proof.
    p.validate_manifest(manifest, frozen_sha=manifest.get("harness_sha", ""), frozen_tree=manifest.get("harness_tree", ""))
    output.write_bytes(raw)
    return {"schema": "history-rewrite-manifest-transport-v1", "transport": descriptor,
            "manifest_sha256": expected, "manifest_bytes": len(raw),
            "selected_refs_count": len(manifest["selected_refs"]),
            "output_refs_count": len(manifest["output_refs"]), "expires_at": metadata["expires_at"]}


def assemble_manifest(recipe: dict, expected: str, api, output: Path, download=bounded_download) -> dict:
    """Expand only the two omitted maps, with exact full-manifest approval."""
    if not isinstance(recipe, dict) or {"selected_refs", "output_refs"} & set(recipe):
        raise p.PublicationError("recipe must omit exactly the reconstructed ref maps")
    p._sha256(expected, "external manifest digest")
    proof = recipe.get("proof")
    if not isinstance(proof, dict):
        raise p.PublicationError("recipe lacks the exact proof artifact")
    metadata = artifact_metadata(
        api, run_id=proof.get("run_id"), head_sha=recipe.get("harness_sha"),
        artifact_id=proof.get("artifact_id"), api_digest=proof.get("artifact_api_digest"),
        name=f"history-rewrite-rewrite-{proof.get('run_id')}", size=None, limit=PROOF_ZIP_LIMIT,
    )
    names = {"original-to-isolated-ref-map.json", "refs-before.txt", "refs-final.txt"}
    with tempfile.TemporaryDirectory(prefix="manifest-recipe-") as directory:
        archive = Path(directory) / "proof.zip"
        download(proof["artifact_id"], archive, metadata["size_in_bytes"])
        members = read_members(archive, proof["artifact_api_digest"], metadata["size_in_bytes"], names, exact=False)
    for name, raw in members.items():
        if hashlib.sha256(raw).hexdigest() != recipe.get("proof_digests", {}).get(name):
            raise p.PublicationError("recipe proof member digest mismatch")
    try:
        isolated = json.loads(members["original-to-isolated-ref-map.json"])
        if (not isinstance(isolated, dict) or not isolated or not all(isinstance(value, str) for value in isolated.values())
                or len(set(isolated.values())) != len(isolated)):
            raise ValueError("invalid isolated map")
        def rows(name: str, reverse: bool) -> dict:
            pairs = [line.split() for line in members[name].decode().splitlines()]
            if any(len(pair) != 2 for pair in pairs):
                raise ValueError("invalid ref row")
            pairs = [pair[::-1] if reverse else pair for pair in pairs]
            if len({pair[0] for pair in pairs}) != len(pairs):
                raise ValueError("duplicate ref row")
            result = dict(pairs)
            if set(result) != set(isolated.values()) | {"refs/rewrites/source"}:
                raise ValueError("invalid isolated ref domain")
            return result
        before, after = rows("refs-before.txt", True), rows("refs-final.txt", False)
        manifest = {**recipe, "selected_refs": {name: before[ref] for name, ref in isolated.items()},
                    "output_refs": {name: after[ref] for name, ref in isolated.items()}}
        if (before["refs/rewrites/source"] != manifest["selected_refs"]["refs/heads/main"]
                or after["refs/rewrites/source"] != manifest["output_refs"]["refs/heads/main"]):
            raise ValueError("source main mismatch")
    except (ValueError, KeyError, TypeError, UnicodeError) as exc:
        raise p.PublicationError("recipe ref reconstruction failed") from exc
    raw = p.canonical_json(manifest)
    canonical_manifest(raw, expected)
    p.validate_manifest(manifest, frozen_sha=manifest.get("harness_sha", ""), frozen_tree=manifest.get("harness_tree", ""))
    output.write_bytes(raw)
    return {"manifest_sha256": expected, "manifest_bytes": len(raw), "selected_refs_count": len(isolated)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("assemble", "read"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--recipe", type=Path)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.operation == "assemble":
            if args.recipe is None:
                raise p.PublicationError("assembly requires a digest-verified recipe")
            receipt = assemble_manifest(p.load_object(args.recipe), args.manifest_sha256,
                                        p.GitHubApi(os.environ.get("GH_TOKEN", "")), args.output)
        else:
            descriptor = os.environ.get("PUBLICATION_MANIFEST_ARTIFACT", "")
            if descriptor:
                if os.environ.get("PUBLICATION_MANIFEST_GZIP_B64"):
                    raise p.PublicationError("choose exactly one manifest transport")
                receipt = read_manifest(json.loads(descriptor), args.manifest_sha256,
                                        p.GitHubApi(os.environ.get("GH_TOKEN", "")), args.output)
            else:
                p.decode_manifest(args.output, args.manifest_sha256)
                receipt = {"schema": "history-rewrite-manifest-transport-v1", "transport": "inline",
                           "manifest_sha256": args.manifest_sha256, "manifest_bytes": args.output.stat().st_size}
        p.write_json(args.receipt, receipt)
    except (p.PublicationError, ValueError, TypeError, OSError) as exc:
        parser.exit(1, f"manifest transport refused: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
