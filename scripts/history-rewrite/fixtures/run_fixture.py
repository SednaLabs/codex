#!/usr/bin/env python3
"""Hosted-only synthetic execution of the production rewrite candidate."""
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "rewrite_candidate.py"
ADAPTER = ROOT / "adapt_review_packet.py"
RECEIPT = ROOT.parent / "recovery-snapshot" / "receipt.py"


def run(*args: str, cwd: Path | None = None, input: bytes | None = None) -> str:
    return subprocess.check_output(args, cwd=cwd, input=input, text=input is None).decode() if input is not None else subprocess.check_output(args, cwd=cwd, text=True)


def command(repo: Path, *args: str) -> str:
    return run("git", "-C", str(repo), *args)


def write_policy(path: Path, rules: list[dict], canonical_source: str) -> None:
    path.write_text(
        json.dumps(
            {
                "schema": 2,
                "repository": "sednalabs/codex",
                "canonical_source_commit": canonical_source,
                "review_packet": {"schema": "review-packet-v1", "executable": False},
                "rules": rules,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def make_repo(root: Path, *, shared_target_unrelated: bool = False, collision: bool = False) -> tuple[Path, str, str, str, str, str]:
    repo = root / "repo"
    run("git", "init", "-q", "-b", "main", str(repo)); command(repo, "config", "user.name", "fixture"); command(repo, "config", "user.email", "fixture@example.invalid")
    (repo / "rename-old.txt").write_bytes(b"rename\n")
    (repo / "canonical.txt").write_bytes(b"unsupported historical variant\n")
    (repo / "target.txt").write_bytes(b"target old value\n")
    (repo / "target-copy.txt").write_bytes(b"target old value\n")
    (repo / "unrelated-a.txt").write_bytes(b"unrelated duplicate\n")
    (repo / "unrelated-b.txt").write_bytes(b"unrelated duplicate\n")
    (repo / "binary.dat").write_bytes(b"\xff\x00binary\n")
    if shared_target_unrelated:
        (repo / "unrelated-a.txt").write_bytes(b"target old value\n")
    if collision:
        (repo / "rename-new.txt").write_bytes(b"already here\n")
    command(repo, "add", "."); command(repo, "commit", "-qm", "old subject")
    old_blob = command(repo, "rev-parse", "HEAD:canonical.txt").strip()
    base = command(repo, "rev-parse", "HEAD").strip(); command(repo, "tag", "lightweight"); command(repo, "tag", "-a", "annotated", "-m", "annotated", base)
    command(repo, "checkout", "-qb", "side"); (repo / "side.txt").write_bytes(b"side\n"); command(repo, "add", "."); command(repo, "commit", "-qm", "side")
    command(repo, "checkout", "-q", "main"); (repo / "target.txt").write_bytes(b"target old value main\n"); command(repo, "add", "target.txt"); command(repo, "commit", "-qm", "old main")
    command(repo, "merge", "--no-ff", "-m", "old merge", "side")
    (repo / "canonical.txt").write_bytes(b"unmatched historical variant\n"); command(repo, "add", "canonical.txt"); command(repo, "commit", "-qm", "intermediate canonical variant")
    (repo / "canonical.txt").write_bytes(b"reviewed canonical replacement\n"); command(repo, "add", "canonical.txt"); command(repo, "commit", "-qm", "canonical source")
    canonical_source = command(repo, "rev-parse", "HEAD").strip()
    new_blob = command(repo, "rev-parse", "HEAD:canonical.txt").strip()
    remote = root / "synthetic-source.git"
    run("git", "clone", "--bare", "--no-local", str(repo), str(remote))
    command(repo, "remote", "add", "synthetic-source", str(remote))
    source = command(repo, "rev-parse", "HEAD").strip()
    return repo, source, "main", canonical_source, old_blob, new_blob


def make_bare_mirror(root: Path, source: Path) -> tuple[Path, str, str]:
    repo = root / "repo.git"
    run("git", "clone", "--mirror", "--no-local", str(source), str(repo))
    return repo, command(repo, "rev-parse", "HEAD").strip(), "master"


def make_isolated_mirror(root: Path, source: Path, source_sha: str) -> Path:
    repo = root / "repo.git"
    run("git", "clone", "--mirror", "--no-local", str(source), str(repo))
    for line in command(repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads", "refs/tags").splitlines():
        name, object_id = line.split()
        isolated = "refs/rewrites/selected/" + hashlib.sha256(name.encode()).hexdigest()
        command(repo, "update-ref", isolated, object_id)
        command(repo, "update-ref", "-d", name)
    command(repo, "update-ref", "refs/rewrites/source", source_sha)
    return repo


def base_rules(old_blob: str, new_blob: str) -> list[dict]:
    return [
        {"id": "canonical", "kind": "exact_blob_replacement", "scope": "blob", "old_blob": old_blob, "new_blob": new_blob, "source_path": "canonical.txt", "target_paths": ["canonical.txt"]},
        {"id": "subject", "kind": "literal", "scope": "commit_subject", "old": "old", "new": "approved"},
        {"id": "rename", "kind": "path", "scope": "path", "old": "rename-old", "new": "rename-new", "target_paths": ["rename-old.txt"]},
        {"id": "blob", "kind": "blob_literal", "scope": "blob", "old": "old value", "new": "approved value", "target_paths": ["target.txt", "target-copy.txt"]},
    ]


def invoke(repo: Path, source: str, policy: Path, root: Path, mode: str = "apply", commit_map: Path | None = None, ref_map: Path | None = None) -> subprocess.CompletedProcess[str]:
    preimage = root / "preimage.git"
    if not preimage.exists():
        run("git", "clone", "--mirror", "--no-local", str(repo), str(preimage))
    output, work = root / "output", root / "work"
    args = ["python3", str(DRIVER), mode, "--repo", str(repo), "--preimage", str(preimage), "--policy", str(policy), "--source-sha", source, "--work", str(work), "--output", str(output)]
    if mode == "verify":
        args += ["--commit-map", str(commit_map), "--ref-map", str(ref_map)]
    return subprocess.run(args, text=True, capture_output=True)


def expect_failure(name: str, result: subprocess.CompletedProcess[str]) -> str:
    if result.returncode == 0:
        raise SystemExit(f"negative fixture unexpectedly passed: {name}")
    return hashlib.sha256((result.stdout + result.stderr).encode()).hexdigest()


def require_driver_success(phase: str, result: subprocess.CompletedProcess[str]) -> None:
    if result.returncode == 0:
        return
    # The synthetic route has no real-history input.  Preserve the production
    # driver's own failure without emitting unbounded subprocess output.
    diagnostic = (result.stderr or result.stdout or "driver produced no diagnostic").strip()
    print(f"fixture_phase={phase} driver_exit={result.returncode}", file=sys.stderr)
    print(diagnostic[-4096:], file=sys.stderr)
    raise SystemExit(f"synthetic production driver failed during {phase}")


def corrupt_map(path: Path, kind: str, source: str, branch: str, repo: Path) -> tuple[Path, Path]:
    cmap, rmap = path / "output/commit-map.txt", path / "output/ref-map.txt"
    cmap_copy, rmap_copy = path / f"{kind}.commit-map", path / f"{kind}.ref-map"
    shutil.copy2(cmap, cmap_copy); shutil.copy2(rmap, rmap_copy)
    lines = cmap_copy.read_text().splitlines()
    if kind == "domain":
        cmap_copy.write_text("\n".join(lines[:-1]) + "\n")
    elif kind == "zero":
        cmap_copy.write_text("\n".join((line.split()[0] + " " + "0" * 40) if line.split() and line.split()[0] == source else line for line in lines) + "\n")
    elif kind == "refchange":
        command(repo, "update-ref", f"refs/heads/{branch}", command(repo, "rev-parse", "side").strip())
    return cmap_copy, rmap_copy


def tampered_tree(root: Path, source: str, branch: str, variant: str) -> tuple[Path, Path, Path]:
    repo = root / "repo"; tamper = root / f"tamper-{variant}"; shutil.copytree(repo, tamper)
    command(tamper, "config", "user.name", "fixture"); command(tamper, "config", "user.email", "fixture@example.invalid")
    target = tamper / "unrelated-a.txt"
    if variant == "bytes": target.write_bytes(b"tampered\n")
    elif variant == "mode": target.chmod(0o755)
    else:
        target.unlink(); target.symlink_to("unrelated-b.txt")
    command(tamper, "add", "-A"); command(tamper, "commit", "-qm", f"tamper {variant}")
    new = command(tamper, "rev-parse", "HEAD").strip()
    cmap, rmap = root / "output/commit-map.txt", root / "output/ref-map.txt"
    cm, rm = root / f"{variant}.commit-map", root / f"{variant}.ref-map"; shutil.copy2(cmap, cm); shutil.copy2(rmap, rm)
    cm.write_text("\n".join((line.split()[0] + " " + new) if line.split() and line.split()[0] == source else line for line in cm.read_text().splitlines()) + "\n")
    rm.write_text("\n".join((line.split()[0] + " " + new + " " + line.split()[2]) if len(line.split()) == 3 and line.split()[2] == f"refs/heads/{branch}" else line for line in rm.read_text().splitlines()) + "\n")
    return tamper, cm, rm


def tampered_parent_order(root: Path, source: str, branch: str) -> tuple[Path, Path, Path]:
    repo = root / "repo"; tamper = root / "tamper-parent-order"; shutil.copytree(repo, tamper)
    command(tamper, "config", "user.name", "fixture"); command(tamper, "config", "user.email", "fixture@example.invalid")
    old_new = next(line.split()[1] for line in (root / "output/commit-map.txt").read_text().splitlines() if line.split() and line.split()[0] == source)
    parents = command(tamper, "show", "-s", "--format=%P", old_new).split()
    if len(parents) != 2:
        raise SystemExit("synthetic merge unexpectedly lacks two ordered parents")
    tree = command(tamper, "rev-parse", old_new + "^{tree}").strip()
    new = subprocess.check_output(["git", "-C", str(tamper), "commit-tree", tree, "-p", parents[1], "-p", parents[0]], input=b"parent order tamper\n").decode().strip()
    command(tamper, "update-ref", f"refs/heads/{branch}", new)
    cmap, rmap = root / "output/commit-map.txt", root / "output/ref-map.txt"
    cm, rm = root / "parent-order.commit-map", root / "parent-order.ref-map"; shutil.copy2(cmap, cm); shutil.copy2(rmap, rm)
    cm.write_text("\n".join((line.split()[0] + " " + new) if line.split() and line.split()[0] == source else line for line in cm.read_text().splitlines()) + "\n")
    rm.write_text("\n".join((line.split()[0] + " " + new + " " + line.split()[2]) if len(line.split()) == 3 and line.split()[2] == f"refs/heads/{branch}" else line for line in rm.read_text().splitlines()) + "\n")
    return tamper, cm, rm


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")), encoding="utf-8")


def receipt_fixture(root: Path) -> list[tuple[str, str]]:
    root.mkdir()
    repository = "sednalabs/codex"
    run_id, workflow_id = 77, 88
    harness_sha, source_sha = "a" * 40, "b" * 40
    selected_map = {"refs/heads/main": source_sha, "refs/tags/v1": "4" * 40}
    selected_raw = json.dumps(selected_map, sort_keys=True, separators=(",", ":")).encode()
    selected_sha = hashlib.sha256(selected_raw).hexdigest()
    (root / "selected-ref-map.json").write_bytes(selected_raw)
    package = {
        "inner_sha256": "d" * 64,
        "inner_size": 1234,
        "selected_refs_sha256": selected_sha,
        "selected_refs_count": 2,
        "selected_refs_bytes": len(selected_raw),
        "rich_ref_manifest_sha256": "e" * 64,
    }
    restore = {"restore_test": "passed", "selected_refs_sha256": selected_sha, "restore_test_identity": "f" * 64}
    ciphertext = {
        "id": 901,
        "name": "recovery-snapshot-ciphertext",
        "size_in_bytes": 4321,
        "digest": "sha256:" + "1" * 64,
        "expired": False,
        "expires_at": "2030-01-02T00:00:00Z",
        "workflow_run": {"id": run_id},
    }
    for name, value in (("package.json", package), ("restore.json", restore), ("ciphertext.json", ciphertext)):
        write_json(root / name, value)
    write_json(root / "tool-versions.json", {"age": "fixture", "git": "fixture"})
    build = subprocess.run(
        [
            "python3", str(RECEIPT), "build", "--repository", repository, "--source-repository", repository,
            "--run-id", str(run_id), "--run-attempt", "1", "--workflow-id", str(workflow_id),
            "--workflow-path", ".github/workflows/recovery-snapshot.yml", "--workflow-ref", f"{repository}/.github/workflows/recovery-snapshot.yml@refs/heads/fixture",
            "--workflow-code-sha", harness_sha, "--source-sha", source_sha, "--package-info", str(root / "package.json"),
            "--restore-info", str(root / "restore.json"), "--artifact-json", str(root / "ciphertext.json"),
            "--metadata-manifest-sha256", "2" * 64, "--ciphertext-sha256", "3" * 64, "--ciphertext-size", "3210",
            "--recipient-sha256", "5" * 64, "--tool-versions-json", str(root / "tool-versions.json"),
            "--output", str(root / "recovery-snapshot-receipt.json"),
        ],
        text=True,
        capture_output=True,
    )
    require_driver_success("receipt_build", build)
    receipt_zip = root / "receipt.zip"
    with zipfile.ZipFile(receipt_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(root / "recovery-snapshot-receipt.json", "recovery-snapshot-receipt.json")
        archive.write(root / "selected-ref-map.json", "selected-ref-map.json")
    receipt_artifact = {
        "id": 902,
        "name": "recovery-snapshot-receipt",
        "size_in_bytes": receipt_zip.stat().st_size,
        "digest": "sha256:" + hashlib.sha256(receipt_zip.read_bytes()).hexdigest(),
        "expired": False,
        "expires_at": "2030-01-02T00:00:00Z",
        "workflow_run": {"id": run_id},
    }
    run_api = {
        "id": run_id,
        "run_attempt": 1,
        "status": "completed",
        "conclusion": "success",
        "event": "workflow_dispatch",
        "head_sha": harness_sha,
        "head_repository": {"full_name": repository},
        "workflow_id": workflow_id,
    }
    workflow_api = {"id": workflow_id, "path": ".github/workflows/recovery-snapshot.yml", "state": "active"}
    artifacts_api = {"artifacts": [ciphertext, receipt_artifact]}
    for name, value in (("run.json", run_api), ("workflow.json", workflow_api), ("artifacts.json", artifacts_api)):
        write_json(root / name, value)

    def verify_case(name: str, *, repository_value: str = repository, source_value: str = source_sha, run_value: int = run_id, run_data: dict | None = None, artifact_data: dict | None = None) -> subprocess.CompletedProcess[str]:
        write_json(root / f"{name}-run.json", run_api if run_data is None else run_data)
        write_json(root / f"{name}-artifacts.json", artifacts_api if artifact_data is None else artifact_data)
        return subprocess.run(
            [
                "python3", str(RECEIPT), "verify", "--repository", repository_value, "--run-id", str(run_value),
                "--harness-sha", harness_sha, "--source-sha", source_value, "--selected-refs-sha256", selected_sha,
                "--selected-refs-count", "2", "--selected-refs-bytes", str(len(selected_raw)), "--now", "2030-01-01T00:00:00Z",
                "--run-json", str(root / f"{name}-run.json"), "--workflow-json", str(root / "workflow.json"),
                "--artifacts-json", str(root / f"{name}-artifacts.json"), "--receipt-zip", str(receipt_zip),
                "--output", str(root / f"{name}-verified.json"), "--selected-refs-output", str(root / f"{name}-selected.json"),
            ],
            text=True,
            capture_output=True,
        )

    evidence: list[tuple[str, str]] = []
    positive = verify_case("receipt-positive")
    require_driver_success("receipt_verify", positive)
    if (root / "receipt-positive-selected.json").read_bytes() != selected_raw:
        raise SystemExit("verified receipt did not return the exact frozen selected-ref map")
    evidence.append(("backup_receipt_positive", hashlib.sha256((root / "receipt-positive-verified.json").read_bytes()).hexdigest()))
    cases = {
        "backup_receipt_source_mismatch": verify_case("source", source_value="9" * 40),
        "backup_receipt_repository_mismatch": verify_case("repository", repository_value="other/repository"),
        "backup_receipt_run_mismatch": verify_case("run", run_value=78),
    }
    artifact_mismatch = json.loads(json.dumps(artifacts_api)); artifact_mismatch["artifacts"][0]["id"] = 999
    cases["backup_receipt_artifact_mismatch"] = verify_case("artifact", artifact_data=artifact_mismatch)
    digest_mismatch = json.loads(json.dumps(artifacts_api)); digest_mismatch["artifacts"][1]["digest"] = "sha256:" + "0" * 64
    cases["backup_receipt_digest_mismatch"] = verify_case("digest", artifact_data=digest_mismatch)
    expiry_mismatch = json.loads(json.dumps(artifacts_api)); expiry_mismatch["artifacts"][0]["expires_at"] = "2029-12-31T00:00:00Z"
    cases["backup_receipt_expiry_mismatch"] = verify_case("expiry", artifact_data=expiry_mismatch)
    for name, result in cases.items():
        evidence.append((name, expect_failure(name, result)))
    return evidence


def main() -> None:
    evidence: list[tuple[str, str]] = []
    with tempfile.TemporaryDirectory() as temporary:
        adapter_check = subprocess.run(["python3", str(ADAPTER), "check-policy", "--policy", str(ROOT / "policy.json")], text=True, capture_output=True)
        require_driver_success("review_packet_adapter_policy", adapter_check)
        evidence.append(("review_packet_non_executable_adapter", hashlib.sha256((ROOT / "policy.json").read_bytes()).hexdigest()))
        root = Path(temporary) / "positive"; root.mkdir(); repo, source, branch, canonical_source, old_blob, new_blob = make_repo(root); policy = root / "policy.json"; write_policy(policy, base_rules(old_blob, new_blob), canonical_source)
        remote_before = command(root / "synthetic-source.git", "show-ref")
        positive = invoke(repo, source, policy, root)
        require_driver_success("positive_apply_and_verify", positive)
        bare_root = Path(temporary) / "positive-bare"; bare_root.mkdir()
        bare_repo, bare_source, _ = make_bare_mirror(bare_root, root / "preimage.git")
        bare_positive = invoke(bare_repo, bare_source, policy, bare_root)
        require_driver_success("positive_apply_and_verify_bare", bare_positive)
        if command(root / "synthetic-source.git", "show-ref") != remote_before:
            raise SystemExit("synthetic source remote changed during candidate execution")
        evidence.append(("positive_linear_merge_binary_nonutf8_repeated_target_unrelated_duplicate_tags", hashlib.sha256((root / "output/map-proof.tsv").read_bytes()).hexdigest()))
        evidence.append(("positive_bare_linear_merge_binary_nonutf8_repeated_target_unrelated_duplicate_tags", hashlib.sha256((bare_root / "output/map-proof.tsv").read_bytes()).hexdigest()))
        residual = json.loads((root / "output/residual-review.json").read_text())
        if residual.get("canonical", {}).get("count") != 1 or residual["canonical"].get("disposition") != "review-required":
            raise SystemExit("unmatched exact-path variants were not retained as review-required residuals")
        evidence.append(("guarded_canonical_replacement_residual", hashlib.sha256((root / "output/residual-review.json").read_bytes()).hexdigest()))
        isolated_root = Path(temporary) / "positive-isolated"; isolated_root.mkdir()
        isolated_repo = make_isolated_mirror(isolated_root, root / "preimage.git", source)
        isolated_positive = invoke(isolated_repo, source, policy, isolated_root)
        require_driver_success("positive_isolated_main_input", isolated_positive)
        isolated_refs = command(isolated_repo, "for-each-ref", "--format=%(refname)").splitlines()
        if not isolated_refs or any(not name.startswith("refs/rewrites/") for name in isolated_refs):
            raise SystemExit("original main input escaped the isolated rewrite namespace")
        evidence.append(("positive_original_main_isolated_under_rewrites", hashlib.sha256("\n".join(isolated_refs).encode()).hexdigest()))
        guarded_policy = json.loads(policy.read_text()); guarded_policy["rules"][0]["old_blob"] = "9" * 40; write_json(root / "guarded-policy.json", guarded_policy)
        guarded_root = root / "guarded-negative"; guarded_root.mkdir()
        evidence.append(("guarded_old_blob_mismatch", expect_failure("guarded-old-blob", invoke(root / "preimage.git", source, root / "guarded-policy.json", guarded_root))))
        for variant in ("bytes", "mode", "type"):
            tamper, cm, rm = tampered_tree(root, source, branch, variant)
            evidence.append((f"modified_non_target_{variant}", expect_failure(variant, invoke(tamper, source, policy, root, "verify", cm, rm))))
        tamper, cm, rm = tampered_parent_order(root, source, branch)
        evidence.append(("ordered_parent_topology", expect_failure("parent-order", invoke(tamper, source, policy, root, "verify", cm, rm))))
        for kind in ("domain", "zero", "refchange"):
            cm, rm = corrupt_map(root, kind, source, branch, repo)
            evidence.append((f"invalid_{kind}", expect_failure(kind, invoke(repo, source, policy, root, "verify", cm, rm))))
        for name, kwargs in (("targeted_shared_unrelated_path", {"shared_target_unrelated": True}), ("rename_collision", {"collision": True})):
            negative_root = Path(temporary) / name; negative_root.mkdir(); neg_repo, neg_source, _, neg_canonical, neg_old, neg_new = make_repo(negative_root, **kwargs); neg_policy = negative_root / "policy.json"; write_policy(neg_policy, base_rules(neg_old, neg_new), neg_canonical)
            evidence.append((name, expect_failure(name, invoke(neg_repo, neg_source, neg_policy, negative_root))))
        evidence.extend(receipt_fixture(Path(temporary) / "receipt"))
    for name, digest in evidence:
        print(f"fixture_case={name} evidence_sha256={digest}")


if __name__ == "__main__":
    main()
