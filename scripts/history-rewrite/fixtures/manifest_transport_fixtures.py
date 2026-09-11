#!/usr/bin/env python3
"""Hosted-only production transport fixtures, including a 1,088-ref payload."""
from __future__ import annotations

import base64
import copy
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import publication as p
import manifest_transport as transport
from publication_fixtures import base_manifest


def archive_bytes(members: list[tuple[str, bytes]]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, raw in members:
            archive.writestr(name, raw)
    return output.getvalue()


class Api:
    def __init__(self, run_id: int, head: str, artifact_id: int, name: str, data: bytes):
        self.run = {"id": run_id, "event": "workflow_dispatch", "status": "completed", "conclusion": "success",
                    "head_sha": head, "head_repository": {"id": p.REPOSITORY_ID, "full_name": p.REPOSITORY},
                    "head_branch": p.PUBLICATION_BRANCH, "workflow_id": 91}
        self.workflow = {"id": 91, "path": transport.WORKFLOW, "state": "active"}
        self.artifact = {"id": artifact_id, "name": name, "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
                         "size_in_bytes": len(data), "expired": False,
                         "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
                         "workflow_run": {"id": run_id, "head_sha": head, "repository_id": p.REPOSITORY_ID}}
        prefix = f"/repos/{p.REPOSITORY}/actions"
        self.routes = {f"{prefix}/runs/{run_id}": self.run, f"{prefix}/workflows/91": self.workflow,
                       f"{prefix}/artifacts/{artifact_id}": self.artifact}
        self.data = data
        self.downloads = 0

    def get(self, path: str):
        if path not in self.routes:
            raise p.PublicationError("unexpected or non-read-only API operation")
        return self.routes[path]

    def download(self, artifact_id: int, path: Path, size: int):
        if artifact_id != self.artifact["id"] or size != len(self.data):
            raise AssertionError("download is not exactly bound")
        self.downloads += 1
        path.write_bytes(self.data)

    def descriptor(self) -> dict:
        return {"schema": "history-rewrite-manifest-artifact-v1", "repository": p.REPOSITORY,
                "run_id": self.run["id"], "head_sha": self.run["head_sha"], "artifact_id": self.artifact["id"],
                "artifact_size": len(self.data), "artifact_api_digest": self.artifact["digest"].removeprefix("sha256:")}


def main() -> None:
    selected = {"refs/heads/main": "1" * 40}
    for index in range(1087):
        name = f"refs/heads/transport-{index:04d}-" + hashlib.sha256(str(index).encode()).hexdigest()[:24]
        selected[name] = hashlib.sha1(f"before-{index}".encode()).hexdigest()
    output = {name: hashlib.sha1(f"after-{name}".encode()).hexdigest() for name in selected}
    manifest = base_manifest(selected, output)
    isolated = {name: f"refs/rewrites/selected/{index}" for index, name in enumerate(sorted(selected))}
    before = {isolated[name]: oid for name, oid in selected.items()} | {"refs/rewrites/source": selected["refs/heads/main"]}
    after = {isolated[name]: oid for name, oid in output.items()} | {"refs/rewrites/source": output["refs/heads/main"]}
    members = {"original-to-isolated-ref-map.json": p.canonical_json(isolated),
               "refs-before.txt": "".join(f"{oid} {name}\n" for name, oid in before.items()).encode(),
               "refs-final.txt": "".join(f"{name} {oid}\n" for name, oid in after.items()).encode()}
    for name, raw in members.items():
        manifest["proof_digests"][name] = hashlib.sha256(raw).hexdigest()
    manifest["proof_digests_sha256"] = p.digest(manifest["proof_digests"])
    proof_bytes = archive_bytes(list(members.items()))
    proof_api = Api(42, manifest["harness_sha"], 420, "history-rewrite-rewrite-42", proof_bytes)
    manifest["proof"]["artifact_api_digest"] = hashlib.sha256(proof_bytes).hexdigest()
    expected = p.digest(manifest)
    raw = p.canonical_json(manifest)
    encoded_size = len(base64.b64encode(gzip.compress(raw, compresslevel=9, mtime=0)))
    assert encoded_size > 65535 and len(selected) == 1088
    recipe = {key: value for key, value in manifest.items() if key not in {"selected_refs", "output_refs"}}
    assert len(base64.b64encode(gzip.compress(p.canonical_json(recipe)))) < 65535
    data = archive_bytes([(transport.MANIFEST_NAME, raw)])
    api = Api(93, "9" * 40, 930, "history-rewrite-publication-manifest-93", data)
    descriptor = api.descriptor()
    cases = []
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        assembled = root / "assembled.json"
        transport.assemble_manifest(recipe, expected, proof_api, assembled, proof_api.download)
        assert assembled.read_bytes() == raw
        received = root / "received.json"
        receipt = transport.read_manifest(descriptor, expected, api, received, api.download)
        assert received.read_bytes() == raw and receipt["selected_refs_count"] == 1088
        cases.append("1088_ref_producer_consumer_exact_bytes_above_dispatch_capacity")

        def rejected(name, callback, target=None):
            target = target or root / "rejected.json"
            assert not target.exists()
            try:
                callback()
            except p.PublicationError:
                assert not target.exists(), "invalid transport created its output"
                cases.append(name)
            else:
                raise AssertionError(f"{name} unexpectedly accepted")

        for scope, key, value in [
            ("run", "head_sha", "a" * 40), ("run", "conclusion", "failure"),
            ("run", "event", "pull_request"), ("run", "head_branch", "different"),
            ("run", "head_repository", {"id": 1, "full_name": p.REPOSITORY}),
            ("workflow", "path", ".github/workflows/unrelated.yml"),
            ("artifact", "expired", True), ("artifact", "expires_at", "2000-01-01T00:00:00Z"),
            ("artifact", "digest", "sha256:" + "0" * 64), ("artifact", "id", 999),
            ("artifact", "name", "unrelated"), ("artifact", "size_in_bytes", len(data) + 1),
            ("artifact", "workflow_run", {"id": 92, "head_sha": "9" * 40, "repository_id": p.REPOSITORY_ID}),
        ]:
            bad_api = copy.deepcopy(api)
            getattr(bad_api, scope)[key] = value
            before_downloads = bad_api.downloads
            rejected(f"reject_{scope}_{key}", lambda: transport.read_manifest(
                descriptor, expected, bad_api, root / "rejected.json", bad_api.download))
            assert bad_api.downloads == before_downloads
        rejected("reject_external_manifest_hash", lambda: transport.read_manifest(
            descriptor, "0" * 64, api, root / "rejected.json", api.download))
        rejected("reject_self_reported_hash_extra_descriptor_key", lambda: transport.read_manifest(
            descriptor | {"manifest_sha256": expected}, expected, api, root / "rejected.json", api.download))
        rejected("reject_wrong_repository_descriptor", lambda: transport.read_manifest(
            descriptor | {"repository": "other/repo"}, expected, api, root / "rejected.json", api.download))
        rejected("reject_changed_download", lambda: transport.read_manifest(
            descriptor, expected, api, root / "rejected.json", lambda _id, path, _size: path.write_bytes(data[:-1])))
        for name, bad_members in [
            ("extra_zip_member", [(transport.MANIFEST_NAME, raw), ("extra", b"x")]),
            ("duplicate_zip_member", [(transport.MANIFEST_NAME, raw), (transport.MANIFEST_NAME, raw)]),
            ("missing_zip_member", [("wrong", raw)]),
            ("noncanonical_json", [(transport.MANIFEST_NAME, raw + b"\n")]),
            ("oversized_member", [(transport.MANIFEST_NAME, b"x" * (transport.MANIFEST_LIMIT + 1))]),
        ]:
            bad_api = Api(93, "9" * 40, 930, "history-rewrite-publication-manifest-93", archive_bytes(bad_members))
            rejected(name, lambda: transport.read_manifest(bad_api.descriptor(), expected, bad_api,
                                                           root / "rejected.json", bad_api.download))
        rejected("reject_recipe_included_map", lambda: transport.assemble_manifest(
            recipe | {"selected_refs": selected}, expected, proof_api, root / "rejected.json", proof_api.download))
        rejected("reject_recipe_changed_control", lambda: transport.assemble_manifest(
            recipe | {"tag_signature_ack": False}, expected, proof_api, root / "rejected.json", proof_api.download))
        rejected("reject_recipe_external_hash", lambda: transport.assemble_manifest(
            recipe, "0" * 64, proof_api, root / "rejected.json", proof_api.download))
        inline_output = root / "inline.json"
        with patch.dict(os.environ, {"PUBLICATION_MANIFEST_GZIP_B64": base64.b64encode(gzip.compress(raw)).decode()}, clear=True):
            p.decode_manifest(inline_output, expected)
        assert inline_output.read_bytes() == raw
        cases.append("inline_compatibility")
        with patch.dict(os.environ, {"PUBLICATION_MANIFEST_GZIP_B64": "present", "PUBLICATION_MANIFEST_ARTIFACT": json.dumps(descriptor)}, clear=True), \
                patch.object(sys, "argv", ["manifest_transport", "read", "--output", str(root / "ambiguous.json"),
                                           "--manifest-sha256", expected, "--receipt", str(root / "ambiguous-receipt.json")]):
            try:
                transport.main()
            except SystemExit as exc:
                assert exc.code == 1
            else:
                raise AssertionError("ambiguous CLI input accepted")
        assert not (root / "ambiguous.json").exists() and not (root / "ambiguous-receipt.json").exists()
        cases.append("production_cli_rejects_ambiguous_transport_before_api")
    print(f"manifest_transport_ref_count=1088 inline_base64_chars={encoded_size} case_count={len(cases)}")
    for case in cases:
        print(f"fixture_case={case}")


if __name__ == "__main__":
    main()
