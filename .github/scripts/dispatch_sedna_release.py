#!/usr/bin/env python3
"""Dispatch the Sedna release workflow after refreshing upstream Rust tags."""

from __future__ import annotations

import argparse
import importlib.util
import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Sequence


SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent.parent
RESOLVER_PATH = SCRIPTS_DIR / "resolve_sedna_release_version.py"
RUST_TAG_REFSPEC = "+refs/tags/rust-v*:refs/tags/rust-v*"


def load_resolver():
    spec = importlib.util.spec_from_file_location(
        "resolve_sedna_release_version_for_dispatch", RESOLVER_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load resolver from {RESOLVER_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


RESOLVER = load_resolver()


class DispatchError(RuntimeError):
    pass


def run_command(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    dry_run: bool = False,
) -> subprocess.CompletedProcess[str]:
    if dry_run:
        print(shell_join(command))
        return subprocess.CompletedProcess(command, 0, "", "")
    return subprocess.run(
        list(command),
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def shell_join(command: Sequence[str]) -> str:
    return shlex.join(list(command))


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def refresh_upstream_rust_tags(
    *,
    repo: Path,
    upstream_remote: str,
    dry_run: bool,
) -> None:
    run_command(
        [
            "git",
            "-C",
            str(repo),
            "fetch",
            "--no-tags",
            upstream_remote,
            RUST_TAG_REFSPEC,
        ],
        dry_run=dry_run,
    )


def resolve_target_sha(repo: Path, ref: str) -> str:
    return git(repo, "rev-parse", f"{ref}^{{commit}}")


def resolve_release_metadata(args: argparse.Namespace) -> dict[str, object]:
    return RESOLVER.resolve_release(
        repo=args.repo,
        target_sha=args.target_sha,
        main_ref=args.main_ref,
        upstream_ref=args.upstream_ref,
        repository=args.repo_slug,
        channel=args.channel,
        release_tag=args.release_tag,
        current_release_tag=None,
        require_marker=args.require_marker,
        missing_marker="error",
        github_releases=args.github_releases,
    )


def dispatch_release(args: argparse.Namespace, metadata: dict[str, object]) -> None:
    release_tag = str(metadata["release_tag"])
    target_commit = str(metadata["target_commit"])
    command = [
        "gh",
        "workflow",
        "run",
        args.workflow,
        "--repo",
        args.repo_slug,
        "--ref",
        args.dispatch_ref,
        "-f",
        f"target_sha={target_commit}",
        "-f",
        f"channel={args.channel}",
        "-f",
        f"release_tag={release_tag}",
        "-f",
        f"draft={str(args.draft).lower()}",
        "-f",
        f"macos_release_mode={args.macos_release_mode}",
    ]
    run_command(command, cwd=args.repo, dry_run=args.dry_run)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        type=Path,
        default=REPO_ROOT,
        help="Local repository path used for release metadata resolution.",
    )
    parser.add_argument(
        "--repo-slug",
        default="sednalabs/codex",
        help="GitHub repository to dispatch and query for existing releases.",
    )
    parser.add_argument(
        "--workflow",
        default="sedna-release.yml",
        help="GitHub Actions workflow file to dispatch.",
    )
    parser.add_argument(
        "--dispatch-ref",
        default="main",
        help="Workflow-host ref passed to gh workflow run.",
    )
    parser.add_argument(
        "--target-sha",
        default=None,
        help="Commit to release. Defaults to the dispatch ref resolved locally.",
    )
    parser.add_argument(
        "--channel",
        choices=("stable", "prerelease", "auto"),
        default="prerelease",
        help="Release channel passed to the resolver and workflow.",
    )
    parser.add_argument(
        "--release-tag",
        default=None,
        help="Optional expected release tag. Usually omitted so the helper computes it.",
    )
    parser.add_argument(
        "--main-ref",
        default="refs/remotes/origin/main",
        help="Local main ref used by the release resolver.",
    )
    parser.add_argument(
        "--upstream-ref",
        default="refs/remotes/origin/upstream-main",
        help="Local upstream mirror ref used by the release resolver.",
    )
    parser.add_argument(
        "--upstream-remote",
        default="upstream",
        help="Remote used to refresh upstream rust-v* tags.",
    )
    parser.add_argument(
        "--github-releases",
        choices=("required", "best-effort", "off"),
        default="required",
        help="How strictly to query GitHub releases while resolving the next tag.",
    )
    parser.add_argument(
        "--require-marker",
        action="store_true",
        help="Require the target commit to contain a Sedna-Release marker.",
    )
    parser.add_argument(
        "--draft",
        action="store_true",
        help="Create the GitHub release as draft.",
    )
    parser.add_argument(
        "--macos-release-mode",
        choices=("off", "preview", "unnotarized", "notarized"),
        default="off",
        help=(
            "Intel macOS asset policy. Preview is ad-hoc signed and only valid "
            "for prereleases; unnotarized is ad-hoc signed without an Apple "
            "identity and may be used for stable or prerelease releases; "
            "notarized requires the codesigning environment."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Refresh tags and resolve metadata, then print the workflow dispatch command.",
    )
    parser.add_argument(
        "--no-dispatch",
        action="store_true",
        help="Refresh tags and resolve metadata, but do not dispatch the workflow.",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    args.repo = args.repo.resolve()

    if args.target_sha is None:
        args.target_sha = resolve_target_sha(args.repo, args.dispatch_ref)

    try:
        refresh_upstream_rust_tags(
            repo=args.repo,
            upstream_remote=args.upstream_remote,
            dry_run=False,
        )
        metadata = resolve_release_metadata(args)
        if (
            args.macos_release_mode == "preview"
            and metadata.get("github_prerelease") is not True
        ):
            raise DispatchError(
                "Intel macOS preview assets may only be attached to prereleases"
            )
        print(json.dumps(metadata, indent=2, sort_keys=True))
        if args.no_dispatch:
            print("workflow dispatch skipped by --no-dispatch")
        else:
            dispatch_release(args, metadata)
    except (DispatchError, RESOLVER.ReleaseVersionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip()
        stdout = exc.stdout.strip()
        detail = stderr or stdout or f"exit status {exc.returncode}"
        print(f"error: {shell_join(exc.cmd)} failed: {detail}", file=sys.stderr)
        return exc.returncode or 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
