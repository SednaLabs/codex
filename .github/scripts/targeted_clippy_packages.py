#!/usr/bin/env python3
"""Select workspace packages touched by a Rust source diff for fast Clippy."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path, PurePosixPath


def run(repo_root: Path, *args: str, cwd: Path | None = None) -> str:
    return subprocess.check_output(
        ["git", *args],
        cwd=cwd or repo_root,
        text=True,
    )


def changed_paths(repo_root: Path, base_sha: str, head_sha: str) -> list[str]:
    output = run(
        repo_root,
        "diff",
        "--name-only",
        "--no-renames",
        base_sha,
        head_sha,
    )
    return [line for line in output.splitlines() if line]


def workspace_packages(repo_root: Path) -> list[tuple[PurePosixPath, str]]:
    manifest = repo_root / "codex-rs" / "Cargo.toml"
    metadata = subprocess.check_output(
        [
            "cargo",
            "metadata",
            "--format-version=1",
            "--no-deps",
            "--manifest-path",
            str(manifest),
        ],
        cwd=repo_root,
        text=True,
    )
    payload = json.loads(metadata)
    workspace_members = set(payload.get("workspace_members", []))
    packages = []
    resolved_repo_root = repo_root.resolve()
    for package in payload.get("packages", []):
        if package.get("id") not in workspace_members:
            continue
        manifest_path = Path(package["manifest_path"]).resolve()
        package_root = manifest_path.parent.relative_to(resolved_repo_root)
        packages.append((PurePosixPath(package_root.as_posix()), package["name"]))
    return packages


def select_packages(
    repo_root: Path,
    paths: list[str],
    packages: list[tuple[PurePosixPath, str]] | None = None,
) -> list[str]:
    """Return deterministic package names for package-local Rust changes."""
    package_roots = packages if packages is not None else workspace_packages(repo_root)
    package_map = {root: name for root, name in package_roots}
    selected: set[str] = set()
    for raw_path in paths:
        path = PurePosixPath(raw_path.replace("\\", "/"))
        if not path.parts or path.parts[0] != "codex-rs":
            continue
        if path.suffix != ".rs" and path.name != "Cargo.toml":
            continue
        for parent in path.parents:
            if parent in package_map:
                selected.add(package_map[parent])
                break
    return sorted(selected)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--head-sha", required=True)
    args = parser.parse_args()

    paths = changed_paths(args.repo_root, args.base_sha, args.head_sha)
    print(json.dumps(select_packages(args.repo_root, paths), separators=(",", ":")))


if __name__ == "__main__":
    main()
