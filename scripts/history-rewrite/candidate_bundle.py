"""Candidate-only Git object handoff; never archive a working mirror or config."""
import json
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import zipfile

import publication as p

FILES = {"candidate.bundle", "manifest.json", "prepared.json"}
LIMIT = 2 * 1024 * 1024 * 1024


def init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "--bare", str(path)], check=True, stdout=subprocess.DEVNULL)


def verify_objects(repo: Path, refs: dict) -> None:
    actual = dict(line.split(" ", 1)[::-1] for line in p.git(repo, "for-each-ref", "--format=%(objectname) %(refname)").splitlines())
    if actual != refs:
        raise p.PublicationError("candidate bundle ref domain or identities differ")
    reachable = {line.split(" ", 1)[0] for line in p.git(repo, "rev-list", "--objects", "--all").splitlines()}
    stored = set(p.git(repo, "cat-file", "--batch-all-objects", "--batch-check=%(objectname)").splitlines())
    if stored != reachable:
        raise p.PublicationError("candidate handoff contains unreachable or missing objects")
    p.git(repo, "fsck", "--full", "--no-reflogs")


def import_bundle(bundle: Path, repo: Path, refs: dict) -> None:
    heads = p.git(bundle.parent, "bundle", "list-heads", str(bundle))
    lines = [line.split(" ", 1) for line in heads.splitlines()]
    if len(lines) != len(refs) or {name: oid for oid, name in lines} != refs:
        raise p.PublicationError("bundle contains unexpected or duplicate advertised refs")
    if repo.exists():
        raise p.PublicationError("candidate import destination must not exist")
    init_repo(repo)
    p.git(repo, "bundle", "verify", str(bundle))
    p.git(repo, "fetch", "--atomic", "--no-tags", "--no-write-fetch-head", str(bundle),
          *(f"{name}:{name}" for name in sorted(refs)))
    verify_objects(repo, refs)


def export_candidate(repo: Path, manifest: dict, output: Path, *, phase: str, run_id: int, attempt: int) -> dict:
    if phase not in {"publication", "qualification"}:
        raise p.PublicationError("invalid prepared phase")
    output.mkdir(parents=True, exist_ok=False)
    refs = manifest["output_refs"]
    with tempfile.TemporaryDirectory(prefix="candidate-export-") as temporary:
        clean = Path(temporary) / "clean.git"
        init_repo(clean)
        p.git(clean, "fetch", "--atomic", "--no-tags", "--no-write-fetch-head", str(repo),
              *(f"{oid}:{name}" for name, oid in sorted(refs.items())))
        bundle = output / "candidate.bundle"
        p.git(clean, "bundle", "create", "--version=2", str(bundle), *sorted(refs))
        import_bundle(bundle, Path(temporary) / "verified.git", refs)
    prepared = {**p.preparation_binding(manifest), "schema": "history-rewrite-prepared-v1",
                "phase": phase, "run_id": run_id, "run_attempt": attempt,
                "bundle_sha256": p.file_digest(bundle), "bundle_bytes": bundle.stat().st_size}
    p.write_json(output / "manifest.json", manifest)
    p.write_json(output / "prepared.json", prepared)
    return prepared


def descriptor(metadata: dict, *, head_sha: str) -> dict:
    binding = metadata.get("workflow_run", {})
    if (metadata.get("expired") is not False or binding.get("head_sha") != head_sha
            or binding.get("repository_id") != p.REPOSITORY_ID
            or not isinstance(metadata.get("digest"), str) or not metadata["digest"].startswith("sha256:")):
        raise p.PublicationError("prepared artifact repository, head, digest or expiry mismatch")
    result = {"artifact_id": metadata.get("id"), "run_id": binding.get("id"), "head_sha": head_sha,
              "artifact_api_digest": metadata["digest"][7:], "artifact_size": metadata.get("size_in_bytes")}
    for key in ("artifact_id", "run_id", "artifact_size"):
        p._positive_int(result[key], key)
    p._sha256(result["artifact_api_digest"], "prepared API digest")
    if result["artifact_size"] > LIMIT or metadata.get("name") != f"history-rewrite-prepared-{result['run_id']}":
        raise p.PublicationError("prepared artifact name or size mismatch")
    return result


def read_candidate(binding: dict, api, output: Path, *, frozen_sha: str, frozen_tree: str,
                   manifest_sha256: str, phase: str, download=p.download_custody_zip) -> dict:
    artifact_id = p._positive_int(binding.get("artifact_id"), "prepared artifact id")
    metadata = api.get(f"/repos/{p.REPOSITORY}/actions/artifacts/{artifact_id}")
    if descriptor(metadata, head_sha=frozen_sha) != binding:
        raise p.PublicationError("prepared artifact descriptor differs from API readback")
    # A successful prepare job may belong to a run currently awaiting approval.
    # The immutable artifact and preparation receipt bind that producer without
    # pretending the entire run is already successful.
    run = api.get(f"/repos/{p.REPOSITORY}/actions/runs/{binding['run_id']}")
    if (run.get("head_sha") != frozen_sha or run.get("head_branch") != p.PUBLICATION_BRANCH
            or run.get("event") != "workflow_dispatch" or run.get("path") != ".github/workflows/history-rewrite-candidate.yml"
            or run.get("repository", {}).get("id") != p.REPOSITORY_ID):
        raise p.PublicationError("prepared producer workflow identity mismatch")
    output.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="candidate-download-") as temporary:
        archive_path = Path(temporary) / "artifact.zip"
        download(artifact_id, archive_path, binding["artifact_size"])
        if archive_path.stat().st_size != binding["artifact_size"] or p.file_digest(archive_path) != binding["artifact_api_digest"]:
            raise p.PublicationError("prepared artifact download size or digest mismatch")
        with zipfile.ZipFile(archive_path) as archive:
            entries = archive.infolist()
            if len(entries) != len(FILES) or {entry.filename for entry in entries} != FILES:
                raise p.PublicationError("prepared artifact contains extra, missing or duplicate members")
            for entry in entries:
                limit = LIMIT if entry.filename == "candidate.bundle" else 1024 * 1024
                if not 0 < entry.file_size <= limit or entry.flag_bits & 1 or stat.S_ISLNK(entry.external_attr >> 16):
                    raise p.PublicationError("prepared artifact member type or size rejected")
                with archive.open(entry) as source, (output / entry.filename).open("xb") as target:
                    shutil.copyfileobj(source, target, 1024 * 1024)
    manifest, prepared = p.load_object(output / "manifest.json"), p.load_object(output / "prepared.json")
    if p.digest(manifest) != manifest_sha256 or manifest.get("harness_sha") != frozen_sha or manifest.get("harness_tree") != frozen_tree:
        raise p.PublicationError("prepared manifest or harness binding mismatch")
    expected = {**p.preparation_binding(manifest), "schema": "history-rewrite-prepared-v1", "phase": phase,
                "run_id": binding["run_id"], "run_attempt": prepared.get("run_attempt"),
                "bundle_sha256": p.file_digest(output / "candidate.bundle"),
                "bundle_bytes": (output / "candidate.bundle").stat().st_size}
    p._positive_int(prepared.get("run_attempt"), "preparation attempt")
    if prepared != expected:
        raise p.PublicationError("prepared receipt differs from exact imported candidate")
    import_bundle(output / "candidate.bundle", output / "repo.git", manifest["output_refs"])
    return prepared
