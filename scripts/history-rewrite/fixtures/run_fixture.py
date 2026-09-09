#!/usr/bin/env python3
"""Hosted-only synthetic execution of the production rewrite candidate."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "rewrite_candidate.py"


def run(*args: str, cwd: Path | None = None, input: bytes | None = None) -> str:
    return subprocess.check_output(args, cwd=cwd, input=input, text=input is None).decode() if input is not None else subprocess.check_output(args, cwd=cwd, text=True)


def command(repo: Path, *args: str) -> str:
    return run("git", "-C", str(repo), *args)


def write_policy(path: Path, rules: list[dict]) -> None:
    path.write_text(json.dumps({"schema": 1, "rules": rules}, sort_keys=True), encoding="utf-8")


def make_repo(root: Path, *, shared_target_unrelated: bool = False, collision: bool = False) -> tuple[Path, str, str]:
    repo = root / "repo"
    run("git", "init", "-q", str(repo)); command(repo, "config", "user.name", "fixture"); command(repo, "config", "user.email", "fixture@example.invalid")
    (repo / "rename-old.txt").write_bytes(b"rename\n")
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
    base = command(repo, "rev-parse", "HEAD").strip(); command(repo, "tag", "lightweight"); command(repo, "tag", "-a", "annotated", "-m", "annotated", base)
    command(repo, "checkout", "-qb", "side"); (repo / "side.txt").write_bytes(b"side\n"); command(repo, "add", "."); command(repo, "commit", "-qm", "side")
    command(repo, "checkout", "-q", "master"); (repo / "target.txt").write_bytes(b"target old value main\n"); command(repo, "add", "target.txt"); command(repo, "commit", "-qm", "old main")
    command(repo, "merge", "--no-ff", "-m", "old merge", "side")
    remote = root / "synthetic-source.git"
    run("git", "clone", "--bare", "--no-local", str(repo), str(remote))
    command(repo, "remote", "add", "synthetic-source", str(remote))
    source = command(repo, "rev-parse", "HEAD").strip()
    return repo, source, "master"


def make_bare_mirror(root: Path, source: Path) -> tuple[Path, str, str]:
    repo = root / "repo.git"
    run("git", "clone", "--mirror", "--no-local", str(source), str(repo))
    return repo, command(repo, "rev-parse", "HEAD").strip(), "master"


def base_rules() -> list[dict]:
    return [
        {"id": "subject", "scope": "commit_subject", "old": "old", "new": "approved"},
        {"id": "rename", "scope": "path", "old": "rename-old", "new": "rename-new", "target_paths": ["rename-old.txt"]},
        {"id": "blob", "scope": "blob", "old": "old value", "new": "approved value", "target_paths": ["target.txt", "target-copy.txt"]},
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


def main() -> None:
    evidence: list[tuple[str, str]] = []
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "positive"; root.mkdir(); repo, source, branch = make_repo(root); policy = root / "policy.json"; write_policy(policy, base_rules())
        remote_before = command(root / "synthetic-source.git", "show-ref")
        positive = invoke(repo, source, policy, root)
        require_driver_success("positive_apply_and_verify", positive)
        bare_root = Path(temporary) / "positive-bare"; bare_root.mkdir()
        bare_repo, bare_source, _ = make_bare_mirror(bare_root, repo)
        bare_positive = invoke(bare_repo, bare_source, policy, bare_root)
        require_driver_success("positive_apply_and_verify_bare", bare_positive)
        if command(root / "synthetic-source.git", "show-ref") != remote_before:
            raise SystemExit("synthetic source remote changed during candidate execution")
        evidence.append(("positive_linear_merge_binary_nonutf8_repeated_target_unrelated_duplicate_tags", hashlib.sha256((root / "output/map-proof.tsv").read_bytes()).hexdigest()))
        evidence.append(("positive_bare_linear_merge_binary_nonutf8_repeated_target_unrelated_duplicate_tags", hashlib.sha256((bare_root / "output/map-proof.tsv").read_bytes()).hexdigest()))
        for variant in ("bytes", "mode", "type"):
            tamper, cm, rm = tampered_tree(root, source, branch, variant)
            evidence.append((f"modified_non_target_{variant}", expect_failure(variant, invoke(tamper, source, policy, root, "verify", cm, rm))))
        tamper, cm, rm = tampered_parent_order(root, source, branch)
        evidence.append(("ordered_parent_topology", expect_failure("parent-order", invoke(tamper, source, policy, root, "verify", cm, rm))))
        for kind in ("domain", "zero", "refchange"):
            cm, rm = corrupt_map(root, kind, source, branch, repo)
            evidence.append((f"invalid_{kind}", expect_failure(kind, invoke(repo, source, policy, root, "verify", cm, rm))))
        for name, kwargs in (("targeted_shared_unrelated_path", {"shared_target_unrelated": True}), ("rename_collision", {"collision": True})):
            negative_root = Path(temporary) / name; negative_root.mkdir(); neg_repo, neg_source, _ = make_repo(negative_root, **kwargs); neg_policy = negative_root / "policy.json"; write_policy(neg_policy, base_rules())
            evidence.append((name, expect_failure(name, invoke(neg_repo, neg_source, neg_policy, negative_root))))
    for name, digest in evidence:
        print(f"fixture_case={name} evidence_sha256={digest}")


if __name__ == "__main__":
    main()
