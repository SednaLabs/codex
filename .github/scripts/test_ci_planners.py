#!/usr/bin/env python3
"""Fixture tests for CI planner scripts and follow-up route selection."""

from __future__ import annotations

import contextlib
import hashlib
import io
import importlib.util
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

import yaml


SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent.parent
UNIFIED_DIFF_HUNK_HEADER = re.compile(
    r"^@@ -\d+(?:,(?P<old_count>\d+))? \+\d+(?:,(?P<new_count>\d+))? @@"
)


def unified_diff_hunk_line_counts(patch: str) -> list[tuple[str, int, int, int, int]]:
    """Return each hunk's declared and observed old/new line counts."""

    hunks: list[tuple[str, int, int, int, int]] = []
    header: str | None = None
    expected_old = expected_new = observed_old = observed_new = 0

    def finish_hunk() -> None:
        if header is not None:
            hunks.append(
                (header, expected_old, observed_old, expected_new, observed_new)
            )

    for line in patch.splitlines():
        match = UNIFIED_DIFF_HUNK_HEADER.match(line)
        if match:
            finish_hunk()
            header = line
            expected_old = int(match["old_count"] or 1)
            expected_new = int(match["new_count"] or 1)
            observed_old = observed_new = 0
            continue
        if line.startswith("@@"):
            raise AssertionError(f"malformed unified diff hunk header: {line}")
        if line.startswith("diff --git "):
            finish_hunk()
            header = None
            continue
        if header is None:
            continue
        if line.startswith((" ", "-")):
            observed_old += 1
        if line.startswith((" ", "+")):
            observed_new += 1

    finish_hunk()
    return hunks


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


RESOLVE_VALIDATION_PLAN = load_module(
    "resolve_validation_plan_module", SCRIPTS_DIR / "resolve_validation_plan.py"
)
RESOLVE_RUST_CI_MODE = load_module(
    "resolve_rust_ci_mode_module", SCRIPTS_DIR / "resolve_rust_ci_mode.py"
)
RESOLVE_BAZEL_CI_MODE = load_module(
    "resolve_bazel_ci_mode_module", SCRIPTS_DIR / "resolve_bazel_ci_mode.py"
)
AGGREGATE_VALIDATION_SUMMARY = load_module(
    "aggregate_validation_summary_module", SCRIPTS_DIR / "aggregate_validation_summary.py"
)
REPORT_ACTIONS_CACHE_OCCUPANCY = load_module(
    "report_actions_cache_occupancy_module", SCRIPTS_DIR / "report_actions_cache_occupancy.py"
)
CHECK_MARKDOWN_LINKS = load_module(
    "check_markdown_links_module", SCRIPTS_DIR / "check_markdown_links.py"
)
CHECK_WORKFLOW_POLICY = load_module(
    "check_workflow_policy_module", SCRIPTS_DIR / "check_workflow_policy.py"
)
SUMMARIZE_RUST_CI_FULL = load_module(
    "summarize_rust_ci_full_module", SCRIPTS_DIR / "summarize_rust_ci_full.py"
)
SKIP_DUPLICATE_WORKFLOW_RUN = load_module(
    "skip_duplicate_workflow_run_module", SCRIPTS_DIR / "skip_duplicate_workflow_run.py"
)
VALIDATION_PLAN_FINGERPRINT = load_module(
    "validation_plan_fingerprint_module", SCRIPTS_DIR / "validation_plan_fingerprint.py"
)
PREPARE_CODEQL_DIFF_RANGES = load_module(
    "prepare_codeql_diff_ranges_module", SCRIPTS_DIR / "prepare_codeql_diff_ranges.py"
)
SYNC_UPSTREAM_MIRROR = load_module(
    "sync_upstream_mirror_module", SCRIPTS_DIR / "sync_upstream_mirror.py"
)
DISPATCH_SEDNA_RELEASE = load_module(
    "dispatch_sedna_release_module", SCRIPTS_DIR / "dispatch_sedna_release.py"
)
RESOLVE_SEDNA_RELEASE_VERSION = load_module(
    "resolve_sedna_release_version_module",
    SCRIPTS_DIR / "resolve_sedna_release_version.py",
)
DOWNSTREAM_DIVERGENCE_AUDIT = load_module(
    "downstream_divergence_audit_module",
    REPO_ROOT / "scripts" / "downstream-divergence-audit.py",
)


def run_script(script: Path, *args: str) -> dict:
    proc = subprocess.run(
        ["python3", str(script), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(proc.stdout)


def parse_workflow_dispatch_lane_options(workflow_path: Path) -> list[str]:
    payload = yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    return (
        (((payload.get("on") or {}).get("workflow_dispatch") or {}).get("inputs") or {})
        .get("lane", {})
        .get("options", [])
    )


def parse_pull_request_types(workflow_path: Path) -> list[str]:
    payload = yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    return (((payload.get("on") or {}).get("pull_request") or {}).get("types") or [])


def load_workflow_payload(workflow_path: Path) -> dict:
    payload = yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    return payload if isinstance(payload, dict) else {}


def parse_github_output_file(output_path: Path) -> dict[str, str]:
    outputs: dict[str, str] = {}
    lines = output_path.read_text(encoding="utf-8").splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if not line:
            continue
        if "<<" in line:
            key, delimiter = line.split("<<", 1)
            if not key or not delimiter:
                continue
            value_lines: list[str] = []
            while index < len(lines) and lines[index] != delimiter:
                value_lines.append(lines[index])
                index += 1
            if index < len(lines):
                index += 1
            outputs[key] = "\n".join(value_lines)
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key:
            outputs[key] = value
    return outputs


def workflow_step_by_name(workflow_path: Path, job_name: str, step_name: str) -> dict:
    payload = load_workflow_payload(workflow_path)
    steps = (((payload.get("jobs") or {}).get(job_name) or {}).get("steps") or [])
    for step in steps:
        if step.get("name") == step_name:
            return step
    raise AssertionError(f"missing step {step_name!r} in {workflow_path}")


def workflow_checkout_identity_script(workflow_path: Path) -> str:
    run_script = workflow_step_by_name(
        workflow_path,
        "metadata",
        "Compute checkout ref",
    ).get("run") or ""
    start_marker = "# BEGIN checkout identity"
    end_marker = "# END checkout identity"
    start = run_script.index(start_marker)
    end = run_script.index(end_marker, start) + len(end_marker)
    return "\n".join(
        line[10:] if line.startswith("          ") else line
        for line in run_script[start:end].splitlines()
    )


def run_workflow_step_script(
    script: str, event: dict, *, event_name: str = "push"
) -> tuple[subprocess.CompletedProcess, dict]:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        event_path = root / "event.json"
        output_path = root / "github-output.txt"
        event_path.write_text(json.dumps(event), encoding="utf-8")
        output_path.write_text("", encoding="utf-8")
        env = {
            **os.environ,
            "EVENT_AFTER": str(event.get("after") or ""),
            "GITHUB_EVENT_NAME": event_name,
            "GITHUB_EVENT_PATH": str(event_path),
            "GITHUB_OUTPUT": str(output_path),
            "GITHUB_SHA": str(event.get("after") or "abc123"),
            "EVENT_NAME": event_name,
            "EVENT_REF": str(event.get("ref") or ""),
            "EVENT_SHA": str(event.get("after") or "abc123"),
            "HEAD_MESSAGE": str((event.get("head_commit") or {}).get("message") or "")
            if isinstance(event.get("head_commit"), dict)
            else "",
        }
        proc = subprocess.run(
            ["bash", "-c", f"set -euo pipefail\n{script}"],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        return proc, parse_github_output_file(output_path)


def just_recipe_names(header: str) -> list[str]:
    names: list[str] = []
    for recipe_part in header.split(","):
        tokens = recipe_part.strip().split()
        if tokens:
            names.append(tokens[0])
    return names


def just_recipe_bodies(justfile_path: Path) -> dict[str, list[str]]:
    recipes: dict[str, list[str]] = {}
    current_names: list[str] = []
    current_body: list[str] = []
    for line in justfile_path.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith((" ", "\t", "#")) and ":" in line:
            for name in current_names:
                recipes[name] = current_body
            current_names = just_recipe_names(line.split(":", 1)[0].strip())
            current_body = []
        elif current_names:
            current_body.append(line)
    for name in current_names:
        recipes[name] = current_body
    return recipes


def just_recipes_with_nextest(justfile_path: Path) -> set[str]:
    recipes = just_recipe_bodies(justfile_path)
    return {name for name, body in recipes.items() if any("cargo nextest" in line for line in body)}


class TempGitRepo:
    def __init__(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)
        self._git("init", "--initial-branch=main")
        self._git("config", "user.name", "CI Planner Tests")
        self._git("config", "user.email", "ci-planner-tests@example.invalid")
        self._git("config", "commit.gpgSign", "false")
        self._git("config", "tag.gpgSign", "false")

    def cleanup(self) -> None:
        self._tmpdir.cleanup()

    def write_files(self, files: dict[str, str]) -> None:
        for relative_path, content in files.items():
            path = self.root / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    def commit(self, message: str, files: dict[str, str]) -> str:
        self.write_files(files)
        self._git("add", "--all")
        self._git("commit", "-m", message)
        return self.rev_parse("HEAD")

    def rev_parse(self, ref: str) -> str:
        return self._git("rev-parse", ref)

    def _git(self, *args: str, env: dict[str, str] | None = None) -> str:
        git_env = os.environ.copy()
        if env is not None:
            git_env.update(env)
        proc = subprocess.run(
            [
                "git",
                "-c",
                "commit.gpgSign=false",
                "-c",
                "tag.gpgSign=false",
                "-C",
                str(self.root),
                *args,
            ],
            check=True,
            capture_output=True,
            env=git_env,
            text=True,
        )
        return proc.stdout.strip()


class CodeqlDiffRangeTests(unittest.TestCase):
    def test_collect_uses_direct_tree_diff_for_shallow_pr_checkout(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="+++ b/changed.py\n@@ -0,0 +1 @@\n+changed\n",
            stderr="",
        )
        with mock.patch.object(
            PREPARE_CODEQL_DIFF_RANGES.subprocess,
            "run",
            return_value=completed,
        ) as run:
            ranges = PREPARE_CODEQL_DIFF_RANGES.collect_diff_ranges("base", "head")

        self.assertEqual(
            ranges,
            [{"path": "changed.py", "startLine": 1, "endLine": 1}],
        )
        command = run.call_args.args[0]
        self.assertIn("base..head", command)
        self.assertNotIn("base...head", command)

    def test_parses_added_and_modified_ranges_and_merges_adjacent_hunks(self) -> None:
        diff = """\
diff --git a/alpha.py b/alpha.py
--- a/alpha.py
+++ b/alpha.py
@@ -1 +1,2 @@
+one
+two
@@ -4 +5 @@
+five
diff --git a/old.py b/new.py
similarity index 80%
rename from old.py
rename to new.py
--- a/old.py
+++ b/new.py
@@ -8,0 +9,3 @@
+nine
+ten
+eleven
"""

        self.assertEqual(
            PREPARE_CODEQL_DIFF_RANGES.parse_diff_ranges(diff),
            [
                {"path": "alpha.py", "startLine": 1, "endLine": 2},
                {"path": "alpha.py", "startLine": 5, "endLine": 5},
                {"path": "new.py", "startLine": 9, "endLine": 11},
            ],
        )

    def test_skips_deletions_and_decodes_quoted_git_paths(self) -> None:
        diff = """\
diff --git a/deleted.py b/deleted.py
--- a/deleted.py
+++ /dev/null
@@ -1 +0,0 @@
-deleted
diff --git \"a/dir name.py\" \"b/dir name.py\"
--- \"a/dir name.py\"
+++ \"b/dir name.py\"
@@ -3,0 +4 @@
+added
"""

        self.assertEqual(
            PREPARE_CODEQL_DIFF_RANGES.parse_diff_ranges(diff),
            [{"path": "dir name.py", "startLine": 4, "endLine": 4}],
        )

    def test_rejects_non_repository_new_path(self) -> None:
        with self.assertRaisesRegex(ValueError, "unexpected Git new-path header"):
            PREPARE_CODEQL_DIFF_RANGES.parse_diff_ranges(
                "+++ /tmp/outside.py\n@@ -0,0 +1 @@\n+bad\n"
            )


class SyncUpstreamMirrorTests(unittest.TestCase):
    def test_read_only_fallback_uses_stale_mirror_for_pr_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo, _origin_bare, upstream_bare, old_sha, _new_sha = self.create_fixture(
                Path(tmpdir), mirror_state="stale"
            )

            result = SYNC_UPSTREAM_MIRROR.sync_upstream_mirror(
                repo=repo,
                mode="read-only-fallback",
                upstream_url=str(upstream_bare),
            )

        self.assertEqual(
            {
                "audit_baseline": result["audit_baseline"],
                "expected_mirror_sha": result["expected_mirror_sha"],
                "mirror_audit_args": result["mirror_audit_args"],
                "mirror_state": result["mirror_state"],
                "wrote_mirror": result["wrote_mirror"],
            },
            {
                "audit_baseline": "origin-mirror",
                "expected_mirror_sha": old_sha,
                "mirror_audit_args": [
                    "--upstream-remote",
                    "origin",
                    "--upstream-branch",
                    "upstream-main",
                    "--mirror-remote",
                    "origin",
                    "--mirror-branch",
                    "upstream-main",
                ],
                "mirror_state": "stale_ff_only",
                "wrote_mirror": False,
            },
        )

    def test_required_write_requires_a_token_even_when_mirror_is_exact(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo, _origin_bare, upstream_bare, _old_sha, _new_sha = self.create_fixture(
                Path(tmpdir), mirror_state="exact"
            )

            with self.assertRaisesRegex(
                SYNC_UPSTREAM_MIRROR.MirrorSyncError,
                "missing upstream sync token",
            ):
                SYNC_UPSTREAM_MIRROR.sync_upstream_mirror(
                    repo=repo,
                    mode="required-write",
                    upstream_url=str(upstream_bare),
                )

    def test_required_write_fast_forwards_stale_mirror(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo, origin_bare, upstream_bare, _old_sha, new_sha = self.create_fixture(
                Path(tmpdir), mirror_state="stale"
            )

            result = SYNC_UPSTREAM_MIRROR.sync_upstream_mirror(
                repo=repo,
                mode="required-write",
                upstream_url=str(upstream_bare),
                token="dummy-token",
                mirror_push_url=str(origin_bare),
            )
            mirror_sha = self.git(
                origin_bare,
                "--git-dir",
                str(origin_bare),
                "rev-parse",
                "refs/heads/upstream-main",
            )

        self.assertEqual(
            {
                "audit_baseline": result["audit_baseline"],
                "expected_mirror_sha": result["expected_mirror_sha"],
                "mirror_audit_args": result["mirror_audit_args"],
                "mirror_sha": mirror_sha,
                "mirror_state": result["mirror_state"],
                "wrote_mirror": result["wrote_mirror"],
            },
            {
                "audit_baseline": "origin-mirror",
                "expected_mirror_sha": new_sha,
                "mirror_audit_args": [
                    "--mirror-remote",
                    "origin",
                    "--mirror-branch",
                    "upstream-main",
                ],
                "mirror_sha": new_sha,
                "mirror_state": "exact",
                "wrote_mirror": True,
            },
        )

    def create_fixture(
        self, root: Path, *, mirror_state: str
    ) -> tuple[Path, Path, Path, str, str]:
        origin_bare = root / "origin.git"
        upstream_bare = root / "upstream.git"
        source = root / "source"
        repo = root / "repo"

        self.git(root, "init", "--bare", str(origin_bare))
        self.git(root, "init", "--bare", str(upstream_bare))
        self.git(root, "init", "--initial-branch=main", str(source))
        self.git(source, "config", "user.name", "CI Planner Tests")
        self.git(source, "config", "user.email", "ci-planner-tests@example.invalid")

        (source / "payload.txt").write_text("old\n", encoding="utf-8")
        self.git(source, "add", "payload.txt")
        self.git(source, "commit", "-m", "old")
        old_sha = self.git(source, "rev-parse", "HEAD")

        (source / "payload.txt").write_text("new\n", encoding="utf-8")
        self.git(source, "commit", "-am", "new")
        new_sha = self.git(source, "rev-parse", "HEAD")

        self.git(source, "push", str(upstream_bare), "main:refs/heads/main")
        mirror_sha = new_sha if mirror_state == "exact" else old_sha
        self.git(source, "push", str(origin_bare), f"{mirror_sha}:refs/heads/upstream-main")

        self.git(root, "init", "--initial-branch=main", str(repo))
        self.git(repo, "remote", "add", "origin", str(origin_bare))
        self.git(repo, "remote", "add", "upstream", str(upstream_bare))
        return repo, origin_bare, upstream_bare, old_sha, new_sha

    def git(self, cwd: Path, *args: str) -> str:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )
        return proc.stdout.strip()


class DispatchSednaReleaseTests(unittest.TestCase):
    def test_refresh_upstream_rust_tags_fetches_only_rust_release_tags(self) -> None:
        with mock.patch.object(DISPATCH_SEDNA_RELEASE, "run_command") as run_command:
            DISPATCH_SEDNA_RELEASE.refresh_upstream_rust_tags(
                repo=Path("/repo"),
                upstream_remote="upstream",
                dry_run=False,
            )

        run_command.assert_called_once_with(
            [
                "git",
                "-C",
                "/repo",
                "fetch",
                "--no-tags",
                "upstream",
                "+refs/tags/rust-v*:refs/tags/rust-v*",
            ],
            dry_run=False,
        )

    def test_dispatch_release_uses_computed_release_metadata(self) -> None:
        args = mock.Mock(
            workflow="sedna-release.yml",
            repo_slug="sednalabs/codex",
            dispatch_ref="main",
            channel="prerelease",
            draft=False,
            macos_release_mode="off",
            repo=Path("/repo"),
            dry_run=True,
        )
        metadata = {
            "release_tag": "v0.133.0-sedna.1+upstream.31",
            "target_commit": "d4b356a4c23ff606556dac7232353c80d2ce8deb",
        }

        with mock.patch.object(DISPATCH_SEDNA_RELEASE, "run_command") as run_command:
            DISPATCH_SEDNA_RELEASE.dispatch_release(args, metadata)

        run_command.assert_called_once_with(
            [
                "gh",
                "workflow",
                "run",
                "sedna-release.yml",
                "--repo",
                "sednalabs/codex",
                "--ref",
                "main",
                "-f",
                "target_sha=d4b356a4c23ff606556dac7232353c80d2ce8deb",
                "-f",
                "channel=prerelease",
                "-f",
                "release_tag=v0.133.0-sedna.1+upstream.31",
                "-f",
                "draft=false",
                "-f",
                "macos_release_mode=off",
            ],
            cwd=Path("/repo"),
            dry_run=True,
        )

    def test_main_refreshes_tags_before_resolving_release_metadata(self) -> None:
        events: list[str] = []
        metadata = {
            "release_tag": "v0.133.0-sedna.1+upstream.31",
            "target_commit": "d4b356a4c23ff606556dac7232353c80d2ce8deb",
        }

        def refresh_tags(**kwargs: object) -> None:
            self.assertIs(kwargs["dry_run"], False)
            events.append("refresh")

        def resolve_metadata(_args: object) -> dict[str, object]:
            self.assertEqual(events, ["refresh"])
            events.append("resolve")
            return metadata

        def dispatch(_args: object, dispatch_metadata: dict[str, object]) -> None:
            self.assertEqual(events, ["refresh", "resolve"])
            self.assertEqual(dispatch_metadata, metadata)
            events.append("dispatch")

        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                mock.patch.object(
                    DISPATCH_SEDNA_RELEASE,
                    "refresh_upstream_rust_tags",
                    side_effect=refresh_tags,
                ),
                mock.patch.object(
                    DISPATCH_SEDNA_RELEASE,
                    "resolve_release_metadata",
                    side_effect=resolve_metadata,
                ),
                mock.patch.object(
                    DISPATCH_SEDNA_RELEASE,
                    "dispatch_release",
                    side_effect=dispatch,
                ),
            ):
                with contextlib.redirect_stdout(io.StringIO()):
                    result = DISPATCH_SEDNA_RELEASE.main(
                        [
                            "--repo",
                            tmpdir,
                            "--target-sha",
                            "d4b356a4c23ff606556dac7232353c80d2ce8deb",
                            "--github-releases",
                            "off",
                        ]
                    )

        self.assertEqual(result, 0)
        self.assertEqual(events, ["refresh", "resolve", "dispatch"])

    def test_main_rejects_macos_preview_for_stable_release(self) -> None:
        metadata = {
            "release_tag": "v0.133.0-sedna.1",
            "target_commit": "d4b356a4c23ff606556dac7232353c80d2ce8deb",
            "github_prerelease": False,
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                mock.patch.object(
                    DISPATCH_SEDNA_RELEASE,
                    "refresh_upstream_rust_tags",
                ),
                mock.patch.object(
                    DISPATCH_SEDNA_RELEASE,
                    "resolve_release_metadata",
                    return_value=metadata,
                ),
                mock.patch.object(
                    DISPATCH_SEDNA_RELEASE,
                    "dispatch_release",
                ) as dispatch,
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()) as stderr,
            ):
                result = DISPATCH_SEDNA_RELEASE.main(
                    [
                        "--repo",
                        tmpdir,
                        "--target-sha",
                        metadata["target_commit"],
                        "--macos-release-mode",
                        "preview",
                    ]
                )

        self.assertEqual(result, 1)
        dispatch.assert_not_called()
        self.assertIn(
            "Intel macOS preview assets may only be attached to prereleases",
            stderr.getvalue(),
        )


class RouteSelectionTests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = RESOLVE_VALIDATION_PLAN.load_catalog()
        cls.routes = cls.catalog["followup_routes"]

    collab_spawn_identity_candidate_paths = [
        ".github/scripts/resolve_validation_plan.py",
        ".github/scripts/resolve_rust_ci_mode.py",
        ".github/scripts/test_ci_planners.py",
        ".github/scripts/validation-lanes/app-server-protocol-test.sh",
        ".github/scripts/validation-lanes/sdk-python-targeted.sh",
        ".github/validation-lanes.json",
        ".github/workflows/sedna-heavy-tests.yml",
        "codex-rs/analytics/src/analytics_client_tests.rs",
        "codex-rs/analytics/src/reducer.rs",
        "codex-rs/app-server-protocol/schema/json/ServerNotification.json",
        "codex-rs/app-server-protocol/schema/json/codex_app_server_protocol.schemas.json",
        "codex-rs/app-server-protocol/schema/json/codex_app_server_protocol.v2.schemas.json",
        "codex-rs/app-server-protocol/schema/json/v2/ItemCompletedNotification.json",
        "codex-rs/app-server-protocol/schema/json/v2/ItemStartedNotification.json",
        "codex-rs/app-server-protocol/schema/json/v2/ReviewStartResponse.json",
        "codex-rs/app-server-protocol/schema/json/v2/ThreadForkResponse.json",
        "codex-rs/app-server-protocol/schema/json/v2/ThreadListResponse.json",
        "codex-rs/app-server-protocol/schema/json/v2/ThreadMetadataUpdateResponse.json",
        "codex-rs/app-server-protocol/schema/json/v2/ThreadReadResponse.json",
        "codex-rs/app-server-protocol/schema/json/v2/ThreadResumeResponse.json",
        "codex-rs/app-server-protocol/schema/json/v2/ThreadRollbackResponse.json",
        "codex-rs/app-server-protocol/schema/json/v2/ThreadStartResponse.json",
        "codex-rs/app-server-protocol/schema/json/v2/ThreadStartedNotification.json",
        "codex-rs/app-server-protocol/schema/json/v2/ThreadUnarchiveResponse.json",
        "codex-rs/app-server-protocol/schema/json/v2/TurnCompletedNotification.json",
        "codex-rs/app-server-protocol/schema/json/v2/TurnStartResponse.json",
        "codex-rs/app-server-protocol/schema/json/v2/TurnStartedNotification.json",
        "codex-rs/app-server-protocol/schema/typescript/v2/ThreadItem.ts",
        "codex-rs/app-server-protocol/src/protocol/collab_agent_lifecycle.rs",
        "codex-rs/app-server-protocol/src/protocol/collab_agent_lifecycle_tests.rs",
        "codex-rs/app-server-protocol/src/protocol/event_mapping.rs",
        "codex-rs/app-server-protocol/src/protocol/mod.rs",
        "codex-rs/app-server-protocol/src/protocol/thread_history.rs",
        "codex-rs/app-server-protocol/src/protocol/v2/item.rs",
        "codex-rs/app-server-protocol/src/protocol/v2/tests.rs",
        "codex-rs/app-server/README.md",
        "codex-rs/app-server/tests/suite/v2/turn_start.rs",
        "codex-rs/core/src/agent/control.rs",
        "codex-rs/core/src/tools/handlers/multi_agents/close_agent.rs",
        "codex-rs/core/src/tools/handlers/multi_agents/resume_agent.rs",
        "codex-rs/core/src/tools/handlers/multi_agents/send_input.rs",
        "codex-rs/core/src/tools/handlers/multi_agents/spawn.rs",
        "codex-rs/core/src/tools/handlers/multi_agents/wait.rs",
        "codex-rs/core/src/tools/handlers/multi_agents_tests.rs",
        "codex-rs/core/src/tools/handlers/multi_agents_v2/spawn.rs",
        "codex-rs/core/src/tools/handlers/multi_agents_v2/wait.rs",
        "codex-rs/exec/tests/event_processor_with_json_output.rs",
        "codex-rs/protocol/src/items.rs",
        "codex-rs/protocol/src/legacy_events.rs",
        "codex-rs/protocol/src/protocol.rs",
        "codex-rs/state/src/runtime/usage.rs",
        "codex-rs/tui/src/app/tests.rs",
        "codex-rs/tui/src/chatwidget/protocol.rs",
        "codex-rs/tui/src/chatwidget/replay.rs",
        "codex-rs/tui/src/chatwidget/snapshots/codex_tui__chatwidget__tests__app_server_collab_spawn_completed_renders_requested_model_and_effort.snap",
        "codex-rs/tui/src/chatwidget/tests/app_server.rs",
        "codex-rs/tui/src/chatwidget/tests/history_replay.rs",
        "codex-rs/tui/src/multi_agents.rs",
        "docs/carry-divergence-ledger.md",
        "docs/divergences/index.yaml",
        "docs/downstream-regression-matrix.md",
        "docs/downstream.md",
        "justfile",
        "sdk/python/docs/api-reference.md",
        "sdk/python/scripts/update_sdk_artifacts.py",
        "sdk/python/src/openai_codex/client.py",
        "sdk/python/src/openai_codex/generated/v2_all.py",
        "sdk/python/tests/test_artifact_workflow_and_binaries.py",
        "sdk/python/tests/test_client_rpc_methods.py",
    ]

    def test_picker_shared_surface_routes_to_both_picker_lanes(self) -> None:
        lanes = RESOLVE_VALIDATION_PLAN.select_followup_lanes(
            ["codex-rs/tui/src/app.rs"],
            self.routes,
        )
        self.assertEqual(
            lanes,
            [
                "codex.tui-agent-picker-targeted",
                "codex.tui-agent-picker-tree-targeted",
            ],
        )

    def test_picker_lifecycle_surface_routes_to_both_picker_lanes(self) -> None:
        lanes = RESOLVE_VALIDATION_PLAN.select_followup_lanes(
            [
                "codex-rs/tui/src/app/loaded_threads.rs",
                "codex-rs/tui/src/app/session_lifecycle.rs",
                "codex-rs/tui/src/app/tests/session_lifecycle_requests.rs",
            ],
            self.routes,
        )
        self.assertEqual(
            lanes,
            [
                "codex.tui-agent-picker-targeted",
                "codex.tui-agent-picker-tree-targeted",
            ],
        )

    def test_picker_tree_unique_files_keep_tree_route_exact(self) -> None:
        lanes = RESOLVE_VALIDATION_PLAN.select_followup_lanes(
            [
                "codex-rs/tui/src/app.rs",
                "codex-rs/tui/src/app/agent_navigation.rs",
            ],
            self.routes,
        )
        self.assertEqual(lanes, ["codex.tui-agent-picker-tree-targeted"])

    def test_spawn_tool_surface_routes_to_both_related_lanes(self) -> None:
        lanes = RESOLVE_VALIDATION_PLAN.select_followup_lanes(
            ["codex-rs/tools/src/agent_tool.rs"],
            self.routes,
        )
        self.assertEqual(
            lanes,
            [
                "codex.spawn-agent-tool-model-surface-targeted",
            ],
        )

    def test_v2_residency_route_stays_on_multi_agent_orchestration_lane(self) -> None:
        lanes = RESOLVE_VALIDATION_PLAN.select_followup_lanes(
            [
                "codex-rs/core/src/agent/control.rs",
                "codex-rs/core/src/agent/control/residency.rs",
                "codex-rs/core/src/agent/control/residency_tests.rs",
                "codex-rs/core/src/agent/control/spawn.rs",
                "codex-rs/core/src/agent/control_tests.rs",
                "codex-rs/core/src/agent/registry.rs",
                "codex-rs/core/src/agent/registry_tests.rs",
                "codex-rs/core/src/thread_manager.rs",
                "codex-rs/core/tests/suite/agent_execution.rs",
                ".github/scripts/test_ci_planners.py",
                ".github/validation-lanes.json",
                "docs/carry-divergence-ledger.md",
                "docs/divergences/index.yaml",
                "docs/downstream-regression-matrix.md",
                "docs/downstream.md",
                "justfile",
            ],
            self.routes,
        )
        self.assertEqual(lanes, ["codex.core-multi-agent-orchestration-targeted"])

        recipe = "\n".join(
            just_recipe_bodies(REPO_ROOT / "justfile")[
                "core-multi-agent-orchestration-targeted"
            ]
        )
        self.assertEqual(recipe.count("cargo nextest run"), 4)
        self.assertEqual(recipe.count("RUST_MIN_STACK="), 4)
        self.assertEqual(recipe.count("--no-tests=fail"), 4)
        for test_name in (
            "agent::control::residency::tests::"
            "residency_slot_reservation_unloads_oldest_idle_v2_agent",
            "agent::control::residency::tests::"
            "interrupted_v2_agent_remains_known_and_reloads_after_residency_eviction",
            "agent::control::residency::tests::"
            "ephemeral_v2_agent_is_not_evicted_without_reloadable_history",
            "agent::registry::tests::cold_status_text_stays_compact_when_json_escaped",
            "agent::control::tests::ensure_v2_agent_loaded_reloads_registered_unloaded_agent",
            "suite::agent_execution::v2_evicted_completed_agent_keeps_final_status",
        ):
            self.assertIn(test_name, recipe)

    def test_openai_models_route_stays_out_of_app_server_lane(self) -> None:
        lanes = RESOLVE_VALIDATION_PLAN.select_followup_lanes(
            ["codex-rs/protocol/src/openai_models.rs"],
            self.routes,
        )
        self.assertEqual(
            lanes,
            [
                "codex.spawn-agent-tool-model-surface-targeted",
                "codex.spawn-agent-description-model-surface-targeted",
            ],
        )

    def test_picker_model_tui_path_reuses_shared_non_tui_routes(self) -> None:
        lanes = RESOLVE_VALIDATION_PLAN.select_followup_lanes(
            ["codex-rs/tui/src/chatwidget.rs"],
            self.routes,
        )
        self.assertEqual(
            lanes,
            [
                "codex.spawn-agent-tool-model-surface-targeted",
                "codex.spawn-agent-description-model-surface-targeted",
            ],
        )

    def test_workflow_ci_route_stays_lightweight(self) -> None:
        lanes = RESOLVE_VALIDATION_PLAN.select_followup_lanes(
            [
                ".github/workflows/validation-lab.yml",
                ".github/scripts/resolve_validation_plan.py",
                "docs/validation_workflow.md",
                "justfile",
            ],
            self.routes,
        )
        self.assertEqual(
            lanes,
            [
                "codex.workflow-ci-sanity",
                "codex.downstream-docs-check",
            ],
        )

    def test_workflow_ci_route_accepts_lane_reusable_workflows_and_catalog(self) -> None:
        lanes = RESOLVE_VALIDATION_PLAN.select_followup_lanes(
            [
                ".github/workflows/_validation-lane-rust-minimal.yml",
                ".github/workflows/_validation-lane-rust-integration.yml",
                ".github/validation-lanes.json",
                ".github/scripts/test_ci_planners.py",
            ],
            self.routes,
        )
        self.assertEqual(
            lanes,
            [
                "codex.workflow-ci-sanity",
                "codex.downstream-docs-check",
            ],
        )

    def test_workflow_ci_route_accepts_plan_fingerprint_helper_alone(self) -> None:
        lanes = RESOLVE_VALIDATION_PLAN.select_followup_lanes(
            [".github/scripts/validation_plan_fingerprint.py"],
            self.routes,
        )
        self.assertEqual(
            lanes,
            [
                "codex.workflow-ci-sanity",
                "codex.downstream-docs-check",
            ],
        )

    def test_workflow_ci_route_accepts_downstream_audit_plumbing(self) -> None:
        lanes = RESOLVE_VALIDATION_PLAN.select_followup_lanes(
            [
                ".github/scripts/validation-lanes/downstream-docs-check.sh",
                ".github/scripts/validation-lanes/downstream-divergence-audit.sh",
                "scripts/downstream-divergence-audit.py",
            ],
            self.routes,
        )
        self.assertEqual(
            lanes,
            [
                "codex.workflow-ci-sanity",
                "codex.downstream-docs-check",
            ],
        )

    def test_skill_loader_fixture_route_stays_exact(self) -> None:
        lanes = RESOLVE_VALIDATION_PLAN.select_followup_lanes(
            ["codex-rs/core-skills/src/loader_tests.rs"],
            self.routes,
        )
        self.assertEqual(
            lanes,
            ["codex.skill-loader-fixture-hermeticity-targeted"],
        )

    def test_app_server_client_route_selects_focused_client_lane(self) -> None:
        changed_files = [
            ".github/scripts/test_ci_planners.py",
            ".github/validation-lanes.json",
            ".github/workflows/sedna-heavy-tests.yml",
            "codex-rs/app-server-client/src/lib.rs",
            "codex-rs/app-server-client/src/remote.rs",
            "docs/carry-divergence-ledger.md",
            "docs/divergences/index.yaml",
            "docs/downstream-regression-matrix.md",
            "justfile",
        ]
        lanes = RESOLVE_VALIDATION_PLAN.select_followup_lanes(
            changed_files,
            self.routes,
        )
        self.assertEqual(
            lanes,
            [
                "codex.app-server-client-targeted",
                "codex.workflow-ci-sanity",
                "codex.downstream-docs-check",
                "codex.downstream-divergence-audit",
            ],
        )

    def test_skill_loader_fixture_lane_pins_both_hermeticity_tests(self) -> None:
        lane = next(
            lane
            for lane in self.catalog["lanes"]
            if lane["lane_id"] == "codex.skill-loader-fixture-hermeticity-targeted"
        )
        self.assertEqual(lane["setup_class"], "rust_minimal")
        self.assertEqual(
            lane["script_args"],
            ["skill-loader-fixture-hermeticity-targeted"],
        )
        self.assertTrue(lane["needs_nextest"])

        recipe = "\n".join(
            just_recipe_bodies(REPO_ROOT / "justfile")[
                "skill-loader-fixture-hermeticity-targeted"
            ]
        )
        self.assertIn("RUST_MIN_STACK=", recipe)
        self.assertIn("cargo nextest run -p codex-core-skills --lib", recipe)
        self.assertIn("--no-tests=fail", recipe)
        self.assertIn(
            "loader::tests::non_git_repo_skills_search_does_not_walk_parents",
            recipe,
        )
        self.assertIn(
            "loader::tests::skill_roots_include_admin_with_lowest_priority",
            recipe,
        )
        self.assertEqual(recipe.count("loader::tests::"), 2)
        self.assertIn("--exact", recipe)

    def test_downstream_docs_route_includes_registry_and_tracking_docs(self) -> None:
        lanes = RESOLVE_VALIDATION_PLAN.select_followup_lanes(
            [
                "docs/divergences/index.yaml",
                "docs/downstream-divergence-tracking.md",
            ],
            self.routes,
        )
        self.assertEqual(
            lanes,
            [
                "codex.downstream-docs-check",
                "codex.downstream-divergence-audit",
            ],
        )

    def test_downstream_docs_lane_is_pr_local_sanity(self) -> None:
        lane = next(
            lane
            for lane in self.catalog["lanes"]
            if lane["lane_id"] == "codex.downstream-docs-check"
        )
        self.assertEqual(
            lane["script_path"],
            ".github/scripts/validation-lanes/downstream-docs-check.sh",
        )
        self.assertEqual(lane.get("checkout_fetch_depth"), 1)
        self.assertFalse(lane["needs_just"])

    def test_downstream_divergence_audit_lane_is_explicit_global_audit(self) -> None:
        lane = next(
            lane
            for lane in self.catalog["lanes"]
            if lane["lane_id"] == "codex.downstream-divergence-audit"
        )
        self.assertTrue(lane["explicit_only"])
        self.assertTrue(lane["pilot_only"])
        self.assertEqual(
            lane["script_path"],
            ".github/scripts/validation-lanes/downstream-divergence-audit.sh",
        )
        self.assertEqual(lane.get("checkout_fetch_depth"), 0)
        self.assertFalse(lane["needs_just"])

    def test_nextest_archive_pilot_declares_archive_contract(self) -> None:
        lane = next(
            lane
            for lane in self.catalog["lanes"]
            if lane["lane_id"] == "codex.nextest-archive-core-carry-pilot"
        )
        archive = lane.get("nextest_archive") or {}

        self.assertTrue(lane["explicit_only"])
        self.assertTrue(lane["pilot_only"])
        self.assertEqual(lane["setup_class"], "rust_integration")
        self.assertEqual(archive.get("cohort"), "core-carry-pilot")
        self.assertEqual(
            archive.get("artifact_name"),
            "validation-lab-nextest-core-carry-pilot",
        )
        self.assertEqual(archive.get("archive_file_name"), "codex-core-carry-nextest.tar.zst")
        self.assertEqual(
            archive.get("build_script_path"),
            ".github/scripts/validation-lanes/build-nextest-archive-core-carry-pilot.sh",
        )

    def test_app_server_followup_route_picks_full_carry_bundle(self) -> None:
        lanes = RESOLVE_VALIDATION_PLAN.select_followup_lanes(
            ["codex-rs/app-server/src/router.rs"],
            self.routes,
        )
        self.assertEqual(
            lanes,
            [
                "codex.app-server-protocol-test",
                "codex.app-server-thread-cwd-targeted",
                "codex.blocking-waits-app-server-targeted",
            ],
        )

    def test_native_computer_use_code_mode_route_covers_wrapper_and_provider_lanes(
        self,
    ) -> None:
        lanes = RESOLVE_VALIDATION_PLAN.select_followup_lanes(
            [
                ".github/scripts/test_ci_planners.py",
                ".github/validation-lanes.json",
                "codex-rs/code-mode-protocol/src/description.rs",
                "codex-rs/core/src/tools/handlers/computer_use.rs",
                "codex-rs/core/src/tools/handlers/computer_use_code_mode.rs",
                "codex-rs/core/src/tools/handlers/mod.rs",
                "codex-rs/core/tests/suite/code_mode.rs",
                "docs/carry-divergence-ledger.md",
                "docs/divergences/index.yaml",
                "docs/downstream-regression-matrix.md",
                "docs/downstream-tool-surface-matrix.md",
                "docs/native-computer-use.md",
                "justfile",
            ],
            self.routes,
        )
        self.assertEqual(
            lanes,
            [
                "codex.app-server-protocol-test",
                "codex.app-server-computer-use-targeted",
                "codex.tui-native-computer-use-targeted",
                "codex.exec-native-computer-use-targeted",
                "codex.native-computer-use-tool-registry-targeted",
                "codex.code-mode-declaration-targeted",
                "codex.native-computer-use-doctor-targeted",
            ],
        )

    def test_app_server_schema_fixture_route_stays_on_schema_contract_lane(self) -> None:
        lanes = RESOLVE_VALIDATION_PLAN.select_followup_lanes(
            ["codex-rs/app-server-protocol/schema/json/v2/ThreadReadResponse.json"],
            self.routes,
        )
        self.assertEqual(lanes, ["codex.app-server-protocol-test"])

    def test_collab_spawn_identity_source_outranks_generic_routes(self) -> None:
        route = next(
            route
            for route in self.routes
            if route["route_id"] == "collab-spawn-identity"
        )
        self.assertEqual(route["priority"], 10)
        self.assertEqual(
            route["lane_ids"],
            [
                "codex.core-subagent-model-pinning-targeted",
                "codex.app-server-protocol-test",
                "codex.app-server-collab-spawn-identity-targeted",
                "codex.tui-collab-spawn-identity-targeted",
                "codex.collab-spawn-identity-consumers-targeted",
                "codex.sdk-python-targeted",
            ],
        )

        lanes = RESOLVE_VALIDATION_PLAN.select_followup_lanes(
            ["codex-rs/app-server-protocol/src/protocol/collab_agent_lifecycle.rs"],
            self.routes,
        )
        self.assertEqual(lanes, route["lane_ids"])

        rust_ci_lanes = RESOLVE_RUST_CI_MODE.select_followup_lanes(
            ["codex-rs/core/src/agent/control.rs"],
            self.routes,
        )
        self.assertEqual(rust_ci_lanes, route["lane_ids"])

    def test_collab_spawn_identity_consumer_sources_use_direct_consumer_lane(self) -> None:
        expected = [
            "codex.core-subagent-model-pinning-targeted",
            "codex.app-server-protocol-test",
            "codex.app-server-collab-spawn-identity-targeted",
            "codex.tui-collab-spawn-identity-targeted",
            "codex.collab-spawn-identity-consumers-targeted",
            "codex.sdk-python-targeted",
        ]
        for path in (
            "codex-rs/analytics/src/analytics_client_tests.rs",
            "codex-rs/state/src/runtime/usage.rs",
        ):
            with self.subTest(path=path):
                self.assertEqual(
                    RESOLVE_VALIDATION_PLAN.select_followup_lanes([path], self.routes),
                    expected,
                )

    def test_collab_spawn_identity_authority_sources_trigger_dedicated_route(self) -> None:
        route = next(
            route
            for route in self.routes
            if route["route_id"] == "collab-spawn-identity"
        )
        for path in (
            "codex-rs/core/src/agent/control.rs",
            "codex-rs/protocol/src/items.rs",
            "codex-rs/protocol/src/legacy_events.rs",
            "codex-rs/protocol/src/protocol.rs",
        ):
            with self.subTest(path=path):
                self.assertEqual(
                    RESOLVE_VALIDATION_PLAN.select_followup_lanes([path], self.routes),
                    route["lane_ids"],
                )

    def test_collab_spawn_identity_sdk_sources_include_python_sdk_lane(self) -> None:
        route = next(
            route
            for route in self.routes
            if route["route_id"] == "collab-spawn-identity"
        )
        for path in (
            "sdk/python/src/openai_codex/client.py",
            "sdk/python/src/openai_codex/generated/v2_all.py",
            "sdk/python/tests/test_client_rpc_methods.py",
            "sdk/python/tests/test_contract_generation.py",
        ):
            with self.subTest(path=path):
                lanes = RESOLVE_VALIDATION_PLAN.select_followup_lanes([path], self.routes)
                self.assertEqual(lanes, route["lane_ids"])
                self.assertIn("codex.sdk-python-targeted", lanes)

    def test_followup_route_priority_fails_closed_on_equal_highest_match(self) -> None:
        routes = [
            {
                "route_id": "first",
                "priority": 1,
                "lane_ids": ["codex.first"],
                "allowed_paths": ["identity/**"],
            },
            {
                "route_id": "second",
                "priority": 1,
                "lane_ids": ["codex.second"],
                "allowed_paths": ["identity/**"],
            },
        ]

        self.assertEqual(
            RESOLVE_VALIDATION_PLAN.select_followup_lanes(["identity/source.rs"], routes),
            [],
        )
        self.assertEqual(
            RESOLVE_RUST_CI_MODE.select_followup_lanes(["identity/source.rs"], routes),
            [],
        )

    def test_route_priority_helpers_reject_invalid_unrelated_route(self) -> None:
        routes = [
            {
                "route_id": "identity",
                "priority": 10,
                "lane_ids": ["codex.identity"],
                "allowed_paths": ["identity/**"],
            },
            {
                "route_id": "unrelated-invalid",
                "priority": False,
                "lane_ids": ["codex.unrelated"],
                "allowed_paths": ["unrelated/**"],
            },
        ]

        for selector in (
            RESOLVE_VALIDATION_PLAN.select_followup_lanes,
            RESOLVE_RUST_CI_MODE.select_followup_lanes,
        ):
            with self.subTest(selector=selector.__module__, files=["identity/source.rs"]):
                with self.assertRaisesRegex(
                    SystemExit,
                    "unrelated-invalid must set priority to a non-negative integer",
                ):
                    selector(["identity/source.rs"], routes)
            with self.subTest(selector=selector.__module__, files=[]):
                with self.assertRaisesRegex(
                    SystemExit,
                    "unrelated-invalid must set priority to a non-negative integer",
                ):
                    selector([], routes)

    def test_brokered_tool_replay_route_stays_tight(self) -> None:
        lanes = RESOLVE_VALIDATION_PLAN.select_followup_lanes(
            ["codex-rs/tui/src/app/app_server_adapter.rs"],
            self.routes,
        )
        self.assertEqual(
            lanes,
            [
                "codex.app-server-protocol-test",
                "codex.tui-brokered-tool-replay-targeted",
            ],
        )

    def test_custom_prompt_review_prompt_crate_path_stays_targeted(self) -> None:
        lanes = RESOLVE_VALIDATION_PLAN.select_followup_lanes(
            ["codex-rs/prompts/src/review_request.rs"],
            self.routes,
        )
        self.assertEqual(
            lanes,
            ["codex.custom-prompts-targeted"],
        )

    def test_heavy_workflow_dispatch_options_cover_catalog_lanes(self) -> None:
        workflow_options = parse_workflow_dispatch_lane_options(
            REPO_ROOT / ".github/workflows/sedna-heavy-tests.yml"
        )
        expected_lane_ids = [
            lane["lane_id"]
            for lane in self.catalog["lanes"]
            if lane.get("lane_id")
        ]
        self.assertEqual(
            workflow_options,
            ["all", *expected_lane_ids],
        )

    def test_coverage_workflow_does_not_use_code_quality_product(self) -> None:
        workflow_path = REPO_ROOT / ".github/workflows/code-coverage.yml"
        workflow_text = workflow_path.read_text(encoding="utf-8")
        payload = load_workflow_payload(workflow_path)

        self.assertEqual(payload.get("name"), "Coverage Tests")
        self.assertNotIn("actions/upload-code-coverage@", workflow_text)
        self.assertNotIn("code-quality:", workflow_text)
        self.assertEqual(
            set((payload.get("jobs") or {}).keys()),
            {"python-tools", "python-sdk", "typescript-sdk"},
        )

    def test_workflow_ci_sanity_lane_uses_direct_script_contract(self) -> None:
        lane = next(
            lane
            for lane in self.catalog["lanes"]
            if lane["lane_id"] == "codex.workflow-ci-sanity"
        )
        self.assertEqual(
            lane["script_path"],
            ".github/scripts/validation-lanes/workflow-ci-sanity.sh",
        )
        self.assertEqual(lane["script_args"], [])
        self.assertFalse(lane["needs_just"])

    def test_argument_comment_lint_lane_uses_prebuilt_setup_contract(self) -> None:
        lane = next(
            lane
            for lane in self.catalog["lanes"]
            if lane["lane_id"] == "codex.argument-comment-lint"
        )
        self.assertEqual(lane["setup_class"], "workflow")
        self.assertTrue(lane["explicit_only"])
        self.assertEqual(
            lane["script_path"],
            ".github/scripts/validation-lanes/argument-comment-lint.sh",
        )
        self.assertEqual(lane["script_args"], [])
        self.assertFalse(lane["needs_bazel"])
        self.assertTrue(lane["needs_linux_build_deps"])
        self.assertTrue(lane["needs_dotslash"])
        self.assertFalse(lane["needs_sccache"])
        self.assertEqual(lane["timeout_minutes"], 30)

    def test_bazel_macos_clippy_caps_hosted_runner_fanout(self) -> None:
        payload = load_workflow_payload(REPO_ROOT / ".github/workflows/bazel.yml")
        clippy_steps = ((payload.get("jobs") or {}).get("clippy") or {}).get("steps") or []
        clippy_run = next(
            step
            for step in clippy_steps
            if step.get("name") == "bazel build --config=clippy lint targets"
        ).get("run") or ""
        self.assertIn('[[ "${RUNNER_OS}" == "macOS" ]]', clippy_run)
        self.assertIn("--jobs=96", clippy_run)
        self.assertIn("--loading_phase_threads=8", clippy_run)

    def test_bazel_windows_tests_serialize_host_global_policy_state(self) -> None:
        payload = load_workflow_payload(REPO_ROOT / ".github/workflows/bazel.yml")
        windows_steps = (
            ((payload.get("jobs") or {}).get("test-windows-shard") or {}).get("steps")
            or []
        )
        windows_test_run = next(
            step for step in windows_steps if step.get("name") == "bazel test shard"
        ).get("run") or ""
        self.assertIn("--local_test_jobs=1", windows_test_run)

    def test_bazel_windows_native_main_uses_gnullvm_proc_macro_toolchain(self) -> None:
        bazel = load_workflow_payload(REPO_ROOT / ".github/workflows/bazel.yml")
        self.assertNotIn("test-windows-native-main", bazel.get("jobs") or {})

        payload = load_workflow_payload(
            REPO_ROOT / ".github/workflows/native-windows-bazel-health.yml"
        )
        self.assertEqual(payload.get("name"), "Native Windows Bazel health")
        triggers = payload.get("on") or {}
        self.assertEqual((triggers.get("push") or {}).get("branches"), ["main"])
        dispatch_inputs = (triggers.get("workflow_dispatch") or {}).get("inputs") or {}
        self.assertEqual(
            (dispatch_inputs.get("collect_diagnostics") or {}).get("type"),
            "boolean",
        )
        self.assertEqual(
            payload.get("concurrency"),
            {
                "group": "native-windows-bazel-health-${{ github.ref }}",
                "cancel-in-progress": "false",
                "queue": "max",
            },
        )

        native_job = (payload.get("jobs") or {}).get("test-windows-native-main") or {}
        self.assertEqual(native_job.get("timeout-minutes"), "90")
        self.assertEqual(native_job.get("runs-on"), "windows-2022")
        self.assertEqual(
            native_job.get("name"),
            "Bazel test on windows-latest for x86_64-pc-windows-gnullvm (native health)",
        )
        native_steps = native_job.get("steps") or []
        prepare_step = next(
            (step for step in native_steps if step.get("name") == "Prepare Bazel CI"),
            None,
        )
        self.assertIsNotNone(prepare_step, "Step 'Prepare Bazel CI' not found")
        self.assertEqual(
            (prepare_step.get("with") or {}).get("target"),
            "x86_64-pc-windows-gnullvm",
        )
        native_step = next(
            (step for step in native_steps if step.get("name") == "bazel test //..."),
            None,
        )
        self.assertIsNotNone(native_step, "Step 'bazel test //...' not found")
        self.assertNotIn("continue-on-error", native_step)
        native_test_run = native_step.get("run") or ""
        self.assertNotIn("--platforms=", native_test_run)
        self.assertNotIn("--windows-msvc-host-platform", native_test_run)
        self.assertNotIn("toolchain_linker_preference=rust", native_test_run)
        self.assertIn(
            "--modify_execution_info=Rustc=+no-remote-cache",
            native_test_run,
        )
        self.assertIn("--local_test_jobs=1", native_test_run)

        module_bazel = (REPO_ROOT / "MODULE.bazel").read_text(encoding="utf-8")
        self.assertIn(
            '"//patches:rules_rs_windows_gnullvm_exec_toolchain.patch"',
            module_bazel,
        )
        gnullvm_exec_patch = (
            REPO_ROOT / "patches/rules_rs_windows_gnullvm_exec_toolchain.patch"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "SUPPORTED_RUST_EXEC_TRIPLES = SUPPORTED_EXEC_TRIPLES + [",
            gnullvm_exec_patch,
        )
        self.assertIn(
            '''+SUPPORTED_RUST_EXEC_TRIPLES = SUPPORTED_EXEC_TRIPLES + [
+    "x86_64-pc-windows-gnullvm",
+    "aarch64-pc-windows-gnullvm",
+]''',
            gnullvm_exec_patch,
        )
        self.assertIn(
            "@@ -61,0 +62,7 @@ SUPPORTED_EXEC_TRIPLES = [",
            gnullvm_exec_patch,
        )
        for hunk, expected_old, observed_old, expected_new, observed_new in (
            unified_diff_hunk_line_counts(gnullvm_exec_patch)
        ):
            with self.subTest(hunk=hunk):
                self.assertEqual(observed_old, expected_old)
                self.assertEqual(observed_new, expected_new)
        # `//:local_windows` inherits the runner CPU. Each compiler-tool
        # declaration must therefore resolve using full target-triple
        # constraints, including ARM64 gnullvm rather than an MSVC fallback.
        self.assertEqual(
            gnullvm_exec_patch.count(
                "exec_compatible_with = triple_to_rust_constraint_set(triple),"
            ),
            3,
        )
        self.assertEqual(
            gnullvm_exec_patch.count("execs = SUPPORTED_RUST_EXEC_TRIPLES"),
            3,
        )
        self.assertIn(
            "if triple in SUPPORTED_EXEC_TRIPLES and exec_triple.arch == host_arch "
            "and exec_triple.system == host_os:",
            gnullvm_exec_patch,
        )
        self.assertEqual(
            gnullvm_exec_patch.count(
                "if version in miri_versions and triple in SUPPORTED_EXEC_TRIPLES:"
            ),
            2,
        )
        self.assertIn(
            "name = \"miri_{}_{}_{}\".format(exec_triple.system, exec_triple.arch, version_key),",
            gnullvm_exec_patch,
        )

        execution_logs_upload = next(
            step for step in native_steps if step.get("name") == "Upload Bazel execution logs"
        )
        self.assertEqual(
            (execution_logs_upload.get("with") or {}).get("name"),
            "bazel-execution-logs-test-windows-native-x86_64-pc-windows-gnullvm",
        )

        diagnostics_step = next(
            step
            for step in native_steps
            if step.get("name") == "Collect native Windows Bazel diagnostics"
        )
        self.assertIn("failure()", diagnostics_step.get("if") or "")
        self.assertIn("inputs.collect_diagnostics", diagnostics_step.get("if") or "")
        self.assertIn(
            "collect-native-windows-bazel-diagnostics.ps1",
            diagnostics_step.get("run") or "",
        )
        diagnostics_upload = next(
            step
            for step in native_steps
            if step.get("name") == "Upload native Windows Bazel diagnostics"
        )
        self.assertEqual((diagnostics_upload.get("with") or {}).get("retention-days"), "3")

    def test_native_windows_health_analyzes_arm64_gnullvm_rust_toolchain_selection(
        self,
    ) -> None:
        payload = load_workflow_payload(
            REPO_ROOT / ".github/workflows/native-windows-bazel-health.yml"
        )
        analysis_job = (
            (payload.get("jobs") or {}).get("analyze-windows-arm64-gnullvm-toolchain")
            or {}
        )
        self.assertEqual(analysis_job.get("runs-on"), "ubuntu-latest")
        self.assertEqual(
            analysis_job.get("name"),
            "Bazel aquery for aarch64-pc-windows-gnullvm Rust toolchain selection",
        )
        self.assertEqual(analysis_job.get("timeout-minutes"), "20")

        analysis_steps = analysis_job.get("steps") or []
        prepare_step = next(
            (step for step in analysis_steps if step.get("name") == "Prepare Bazel CI"),
            None,
        )
        self.assertIsNotNone(prepare_step, "Step 'Prepare Bazel CI' not found")
        self.assertEqual(prepare_step.get("id"), "prepare_bazel")
        self.assertEqual(
            (prepare_step.get("with") or {}).get("target"),
            "aarch64-pc-windows-gnullvm",
        )
        self.assertEqual(
            (prepare_step.get("with") or {}).get("cache-scope"),
            "bazel-${{ github.job }}",
        )

        analysis_step = next(
            (
                step
                for step in analysis_steps
                if step.get("name")
                == "Assert ARM64 gnullvm Rust action-toolchain selection"
            ),
            None,
        )
        self.assertIsNotNone(
            analysis_step,
            "ARM64 gnullvm toolchain selection step not found",
        )
        analysis_run = analysis_step.get("run") or ""
        self.assertIn("./.github/scripts/run_bazel_with_buildbuddy.py", analysis_run)
        self.assertIn("aquery", analysis_run)
        self.assertNotIn("\n          bazel aquery", analysis_run)
        self.assertNotIn("bazel build", analysis_run)
        self.assertNotIn("bazel test", analysis_run)
        self.assertIn(
            "--platforms=@rules_rs//rs/platforms:aarch64-pc-windows-gnullvm",
            analysis_run,
        )
        self.assertIn(
            "--extra_execution_platforms=@rules_rs//rs/platforms:aarch64-pc-windows-gnullvm",
            analysis_run,
        )
        self.assertIn(
            "--extra_toolchains=//:windows_aarch64_gnullvm_rust_toolchain_for_health",
            analysis_run,
        )
        self.assertIn("--include_artifacts=true", analysis_run)
        self.assertIn("//codex-rs/otel:otel", analysis_run)
        self.assertIn(
            "python3 .github/scripts/verify_arm64_gnullvm_aquery.py",
            analysis_run,
        )
        self.assertIn("--target //codex-rs/otel:otel", analysis_run)
        self.assertNotIn("grep", analysis_run)
        aquery_upload = next(
            (
                step
                for step in analysis_steps
                if step.get("name") == "Upload ARM64 gnullvm aquery evidence"
            ),
            None,
        )
        self.assertIsNotNone(aquery_upload, "ARM64 aquery evidence upload not found")
        self.assertEqual(
            aquery_upload.get("uses"),
            "actions/upload-artifact@bbbca2ddaa5d8feaa63e36b76fdaad77386f024f",
        )
        self.assertEqual(aquery_upload.get("if"), "always() && !cancelled()")
        self.assertEqual(aquery_upload.get("continue-on-error"), "true")
        self.assertEqual(
            (aquery_upload.get("with") or {}).get("name"),
            "native-windows-arm64-aquery-${{ github.run_id }}-${{ github.sha }}",
        )
        self.assertEqual(
            (aquery_upload.get("with") or {}).get("path"),
            "${{ runner.temp }}/aarch64-windows-gnullvm-aquery.txt",
        )
        self.assertEqual(
            (aquery_upload.get("with") or {}).get("if-no-files-found"),
            "error",
        )
        self.assertEqual((aquery_upload.get("with") or {}).get("retention-days"), "1")
        build_bazel = (REPO_ROOT / "BUILD.bazel").read_text(encoding="utf-8")
        self.assertIn(
            'name = "windows_aarch64_gnullvm_rust_toolchain_for_health"',
            build_bazel,
        )
        self.assertRegex(
            build_bazel,
            r'toolchain = "@default_rust_toolchains//:windows_aarch64_gnullvm_\d+_\d+_\d+_rust_toolchain"',
        )
        selector_match = re.search(
            r'toolchain\(\n    name = "windows_aarch64_gnullvm_rust_toolchain_for_health",'
            r'.*?\n\)\n',
            build_bazel,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(
            selector_match, "ARM64 health selector declaration not found"
        )
        selector = selector_match.group(0)
        for constraint in (
            '"@platforms//cpu:aarch64"',
            '"@platforms//os:windows"',
            '"@llvm//constraints/windows/abi:gnullvm"',
            '"@llvm//constraints/windows/crt:msvcrt"',
        ):
            with self.subTest(constraint=constraint):
                self.assertIn(constraint, selector)
        self.assertIn('toolchain_type = "@rules_rust//rust:toolchain_type"', selector)
        self.assertIn('"@rules_rs//rs/toolchains:non_bpf_targets"', selector)
        self.assertNotIn('"@rules_rs//rs/toolchains:bpf_targets"', selector)
        self.assertNotIn("target_compatible_with", selector)
        analysis_cache_save = next(
            (
                step
                for step in analysis_steps
                if step.get("name") == "Save ARM64 gnullvm aquery repository cache"
            ),
            None,
        )
        self.assertIsNotNone(analysis_cache_save, "ARM64 analysis cache save not found")
        self.assertEqual(analysis_cache_save.get("continue-on-error"), "true")
        self.assertEqual(
            analysis_cache_save.get("uses"),
            "actions/cache/save@55cc8345863c7cc4c66a329aec7e433d2d1c52a9",
        )
        self.assertIn(
            "steps.prepare_bazel.outputs.repository-cache-hit != 'true'",
            analysis_cache_save.get("if") or "",
        )
        self.assertIn(
            "steps.prepare_bazel.outputs.repository-cache-write-enabled == 'true'",
            analysis_cache_save.get("if") or "",
        )
        self.assertEqual(
            (analysis_cache_save.get("with") or {}).get("path"),
            "${{ steps.prepare_bazel.outputs.repository-cache-path }}",
        )
        self.assertEqual(
            (analysis_cache_save.get("with") or {}).get("key"),
            "${{ steps.prepare_bazel.outputs.repository-cache-key }}",
        )

    def test_bazel_ci_docs_only_plan_is_fail_closed_and_preserves_required_signal(self) -> None:
        payload = load_workflow_payload(REPO_ROOT / ".github/workflows/bazel.yml")
        jobs = payload.get("jobs") or {}
        plan_job = jobs.get("plan") or {}
        self.assertEqual(
            plan_job.get("outputs"),
            {
                "mode": "${{ steps.resolve.outputs.mode }}",
                "run_bazel": "${{ steps.resolve.outputs.run_bazel }}",
                "run_observer": "${{ steps.resolve.outputs.run_observer }}",
            },
        )
        plan_steps = plan_job.get("steps") or []
        compare_step = next(
            step for step in plan_steps if step.get("name") == "Compare exact changed files"
        )
        self.assertEqual(compare_step.get("uses"), "actions/github-script@v9.0.0")
        self.assertEqual(
            compare_step.get("if"),
            "${{ github.event_name == 'pull_request' || github.event_name == 'merge_group' }}",
        )
        compare_script = (compare_step.get("with") or {}).get("script") or ""
        self.assertIn("github.rest.repos.compareCommitsWithBasehead", compare_script)
        self.assertIn("context.payload.pull_request?.base?.sha", compare_script)
        self.assertIn("context.payload.pull_request?.head?.sha", compare_script)
        self.assertIn("context.payload.merge_group?.base_sha", compare_script)
        self.assertIn("context.sha", compare_script)
        self.assertIn("data.files", compare_script)
        self.assertIn("files.length >= 300", compare_script)
        self.assertIn("file.previous_filename", compare_script)
        self.assertIn("comparison_complete", compare_script)
        self.assertIn("statuses", compare_script)

        resolve_step = next(
            step for step in plan_steps if step.get("name") == "Resolve Bazel CI mode"
        )
        self.assertIn(
            "resolve_bazel_ci_mode.py",
            resolve_step.get("run") or "",
        )
        self.assertIn("--github-output", resolve_step.get("run") or "")

        docs_job = jobs.get("docs-only") or {}
        self.assertEqual(docs_job.get("needs"), "plan")
        self.assertEqual(docs_job.get("if"), "${{ needs.plan.outputs.mode == 'docs_only' }}")
        docs_step = next(
            step
            for step in docs_job.get("steps") or []
            if step.get("name") == "Check markdown links"
        )
        self.assertEqual(docs_step.get("run"), "python3 .github/scripts/check_markdown_links.py")
        observer_job = jobs.get("observer-only") or {}
        self.assertEqual(observer_job.get("needs"), "plan")
        self.assertEqual(observer_job.get("if"), "${{ needs.plan.outputs.run_observer == 'true' }}")
        observer_run = next(
            step for step in observer_job.get("steps") or [] if step.get("name") == "Run observer validation lanes"
        ).get("run") or ""
        self.assertIn("agent-workflow-sanity.sh", observer_run)
        self.assertIn("workflow-security-targeted.sh", observer_run)

        for job_name in ["test", "test-windows-shard", "clippy", "verify-release-build"]:
            with self.subTest(job=job_name):
                job = jobs.get(job_name) or {}
                self.assertEqual(job.get("needs"), "plan")
                self.assertEqual(job.get("if"), "${{ needs.plan.outputs.run_bazel == 'true' }}")

        windows_gate = jobs.get("test-windows") or {}
        self.assertEqual(windows_gate.get("needs"), ["plan", "test-windows-shard"])
        self.assertEqual(
            windows_gate.get("if"),
            "${{ always() && needs.plan.outputs.run_bazel == 'true' }}",
        )
        results_job = jobs.get("results") or {}
        self.assertEqual(results_job.get("name"), "Bazel required gate")
        self.assertEqual(
            results_job.get("needs"),
            [
                "plan",
                "docs-only",
                "observer-only",
                "test",
                "test-windows-shard",
                "test-windows",
                "clippy",
                "verify-release-build",
            ],
        )
        results_run = (
            next(
                step
                for step in results_job.get("steps") or []
                if step.get("name") == "Require the selected Bazel CI mode"
            ).get("run")
            or ""
        )
        self.assertIn('if mode == "docs_only"', results_run)
        self.assertIn('elif mode == "full"', results_run)
        self.assertIn('elif mode == "observer_only"', results_run)
        self.assertIn('require("docs-only", "success")', results_run)
        self.assertIn('require(job, "skipped")', results_run)
        self.assertIn('require(job, "success")', results_run)

        heredoc_start = "python3 - <<'PY'\n"
        self.assertIn(heredoc_start, results_run)
        gate_script = results_run.split(heredoc_start, 1)[1].rsplit("\nPY", 1)[0]
        for mode, docs_result, normal_result in [
            ("docs_only", "success", "skipped"),
            ("observer_only", "skipped", "skipped"),
            ("full", "skipped", "success"),
        ]:
            with self.subTest(mode=mode):
                needs = {
                    "plan": {"result": "success", "outputs": {"mode": mode}},
                    "docs-only": {"result": docs_result},
                    "observer-only": {"result": "success" if mode == "observer_only" else "skipped"},
                    "test": {"result": normal_result},
                    "test-windows-shard": {"result": normal_result},
                    "test-windows": {"result": normal_result},
                    "clippy": {"result": normal_result},
                    "verify-release-build": {"result": normal_result},
                }
                proc = subprocess.run(
                    ["python3", "-c", gate_script],
                    check=False,
                    capture_output=True,
                    text=True,
                    env={**os.environ, "NEEDS_JSON": json.dumps(needs)},
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_bazel_cache_writes_are_limited_to_trusted_post_merge_pushes(self) -> None:
        setup_action = load_workflow_payload(REPO_ROOT / ".github/actions/setup-bazel-ci/action.yml")
        setup_bazel_step = next(
            step
            for step in ((setup_action.get("runs") or {}).get("steps") or [])
            if step.get("name") == "Set up Bazel"
        )
        self.assertEqual(
            (setup_bazel_step.get("with") or {}).get("bazelisk-cache"),
            "true",
        )
        self.assertEqual(
            (setup_bazel_step.get("with") or {}).get("disk-cache"),
            "${{ github.workflow }}",
        )
        self.assertEqual(
            (setup_bazel_step.get("with") or {}).get("cache-save"),
            "${{ github.event_name == 'push' && (github.ref == 'refs/heads/main' || github.ref == 'refs/heads/upstream-main') }}",
        )

        prepare_action = load_workflow_payload(REPO_ROOT / ".github/actions/prepare-bazel-ci/action.yml")
        self.assertEqual(
            (prepare_action.get("outputs") or {}).get("repository-cache-write-enabled", {}).get(
                "value"
            ),
            "${{ steps.repository_cache_write_policy.outputs.repository-cache-write-enabled }}",
        )
        policy_step = next(
            step
            for step in ((prepare_action.get("runs") or {}).get("steps") or [])
            if step.get("name") == "Determine Bazel repository cache write eligibility"
        )
        policy_run = policy_step.get("run") or ""
        self.assertIn('"${EVENT_NAME}" == "push"', policy_run)
        self.assertIn("refs/heads/main", policy_run)
        self.assertIn("refs/heads/upstream-main", policy_run)

        cache_key_step = next(
            step
            for step in ((prepare_action.get("runs") or {}).get("steps") or [])
            if step.get("name") == "Compute bazel repository cache key"
        )
        self.assertIn(
            "bazel-cache-${CACHE_SCOPE}-${TARGET}-${CACHE_HASH}",
            cache_key_step.get("run") or "",
        )
        cache_summary_step = next(
            step
            for step in ((prepare_action.get("runs") or {}).get("steps") or [])
            if step.get("name") == "Summarize Bazel repository cache"
        )
        cache_summary_env = cache_summary_step.get("env") or {}
        self.assertEqual(
            cache_summary_env.get("CACHE_KEY"),
            "${{ steps.cache_bazel_repository_key.outputs.repository-cache-key }}",
        )
        self.assertEqual(
            cache_summary_env.get("CACHE_HIT"),
            "${{ steps.cache_bazel_repository_restore.outputs.cache-hit }}",
        )
        self.assertEqual(
            cache_summary_env.get("WRITE_ENABLED"),
            "${{ steps.repository_cache_write_policy.outputs.repository-cache-write-enabled }}",
        )
        self.assertEqual(
            cache_summary_env.get("SETUP_BAZEL_DISK_CACHE_SCOPE"),
            "${{ github.workflow }}",
        )
        cache_summary_run = cache_summary_step.get("run") or ""
        self.assertIn("setup-bazel Bazelisk download cache", cache_summary_run)
        self.assertIn("setup-bazel disk-cache scope", cache_summary_run)
        self.assertIn("Repository cache primary key", cache_summary_run)

        bazel = load_workflow_payload(REPO_ROOT / ".github/workflows/bazel.yml")
        cache_save_steps = [
            step
            for job in (bazel.get("jobs") or {}).values()
            for step in (job.get("steps") or [])
            if step.get("name") == "Save bazel repository cache"
        ]
        self.assertEqual(len(cache_save_steps), 3)
        for save_step in cache_save_steps:
            with self.subTest(cache_key=(save_step.get("with") or {}).get("key")):
                self.assertEqual(save_step.get("continue-on-error"), "true")
                self.assertIn(
                    "steps.prepare_bazel.outputs.repository-cache-write-enabled == 'true'",
                    save_step.get("if") or "",
                )

        native_health = load_workflow_payload(
            REPO_ROOT / ".github/workflows/native-windows-bazel-health.yml"
        )
        native_steps = (
            ((native_health.get("jobs") or {}).get("test-windows-native-main") or {}).get(
                "steps"
            )
            or []
        )
        native_cache_save = next(
            step for step in native_steps if step.get("name") == "Save bazel repository cache"
        )
        self.assertEqual(native_cache_save.get("continue-on-error"), "true")
        self.assertIn(
            "steps.prepare_bazel.outputs.repository-cache-write-enabled == 'true'",
            native_cache_save.get("if") or "",
        )

    def test_bazel_ci_applies_caller_flags_after_remote_config(self) -> None:
        script = (REPO_ROOT / ".github/scripts/run-bazel-ci.sh").read_text()
        config_append = 'bazel_run_args+=("--config=${ci_config}")'
        caller_append = 'bazel_run_args+=("${bazel_args[@]:1}")'

        self.assertIn('bazel_run_args=("${bazel_args[0]}")', script)
        self.assertLess(script.index(config_append), script.index(caller_append))


class DownstreamDivergenceAuditTests(unittest.TestCase):
    def run_git(self, repo: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def commit_all(self, repo: Path, message: str) -> str:
        self.run_git(repo, "add", ".")
        self.run_git(repo, "commit", "-m", message)
        return self.run_git(repo, "rev-parse", "HEAD")

    def valid_registry_entry(self, **overrides: object) -> dict[str, object]:
        entry: dict[str, object] = {
            "id": "guarded-carry",
            "status": "live",
            "files": ["carry.py"],
            "upstreamability_tier": "upstream-pr",
            "boundary_type": "runtime-contract",
            "hotspot_files": [],
            "extraction_target": "upstream carry",
            "owner": "downstream",
            "guardrail_lane": "hosted-test",
        }
        entry.update(overrides)
        return entry

    def test_required_markers_verify_exact_downstream_commit_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self.run_git(repo, "init", "-b", "main")
            self.run_git(repo, "config", "user.email", "ci@example.invalid")
            self.run_git(repo, "config", "user.name", "CI")
            (repo / "carry.py").write_text(
                "def bounded_complete_snapshot():\n    return True\n",
                encoding="utf-8",
            )
            downstream_sha = self.commit_all(repo, "guarded carry")
            (repo / "carry.py").write_text(
                "def working_tree_only():\n    return False\n",
                encoding="utf-8",
            )
            registry = {
                "divergences": [
                    self.valid_registry_entry(
                        required_markers={
                            "carry.py": ["bounded_complete_snapshot", "return True"]
                        }
                    )
                ]
            }

            DOWNSTREAM_DIVERGENCE_AUDIT.validate_registry(registry)
            checks = DOWNSTREAM_DIVERGENCE_AUDIT.verify_required_markers(
                repo,
                downstream_sha,
                registry,
                {"guarded-carry"},
            )

            self.assertEqual(
                checks,
                [
                    {
                        "entry_id": "guarded-carry",
                        "path": "carry.py",
                        "markers": ["bounded_complete_snapshot", "return True"],
                    }
                ],
            )

    def test_required_markers_reject_missing_file_and_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self.run_git(repo, "init", "-b", "main")
            self.run_git(repo, "config", "user.email", "ci@example.invalid")
            self.run_git(repo, "config", "user.name", "CI")
            (repo / "carry.py").write_text("present = True\n", encoding="utf-8")
            downstream_sha = self.commit_all(repo, "guarded carry")

            cases = {
                "missing marker": (
                    {"carry.py": ["bounded_complete_snapshot"]},
                    "carry.py is missing required markers: 'bounded_complete_snapshot'",
                ),
                "missing file": (
                    {"missing.py": ["bounded_complete_snapshot"]},
                    "required marker file is missing: missing.py",
                ),
            }
            for label, (required_markers, message) in cases.items():
                with self.subTest(label=label):
                    registry = {
                        "divergences": [
                            self.valid_registry_entry(
                                files=["*.py"],
                                required_markers=required_markers,
                            )
                        ]
                    }
                    DOWNSTREAM_DIVERGENCE_AUDIT.validate_registry(registry)
                    with self.assertRaisesRegex(ValueError, message):
                        DOWNSTREAM_DIVERGENCE_AUDIT.verify_required_markers(
                            repo,
                            downstream_sha,
                            registry,
                            {"guarded-carry"},
                        )

    def test_required_markers_report_failures_without_enforcement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self.run_git(repo, "init", "-b", "main")
            self.run_git(repo, "config", "user.email", "ci@example.invalid")
            self.run_git(repo, "config", "user.name", "CI")
            (repo / "carry.py").write_text("present = True\n", encoding="utf-8")
            downstream_sha = self.commit_all(repo, "guarded carry")
            registry = {
                "divergences": [
                    self.valid_registry_entry(
                        required_markers={
                            "carry.py": ["missing_marker"],
                        }
                    )
                ]
            }

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                checks = DOWNSTREAM_DIVERGENCE_AUDIT.verify_required_markers(
                    repo,
                    downstream_sha,
                    registry,
                    {"guarded-carry"},
                    enforce=False,
                )

            self.assertEqual(checks, [])
            self.assertEqual(
                stderr.getvalue(),
                "warning: guarded-carry: carry.py is missing required markers: "
                "'missing_marker'\n",
            )

    def test_required_markers_report_invalid_contract_without_enforcement(self) -> None:
        registry = {
            "divergences": [
                self.valid_registry_entry(
                    required_markers={
                        "carry.py": "not-a-list",
                    }
                )
            ]
        }

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            checks = DOWNSTREAM_DIVERGENCE_AUDIT.verify_required_markers(
                Path("."),
                "unused",
                registry,
                {"guarded-carry"},
                enforce=False,
            )

        self.assertEqual(checks, [])
        self.assertEqual(
            stderr.getvalue(),
            "warning: guarded-carry: required_markers['carry.py'] must be a "
            "non-empty list\n",
        )

    def test_required_markers_reject_unsafe_or_uncovered_paths(self) -> None:
        cases = {
            "parent traversal": (
                {"../carry.py": ["marker"]},
                "path must be a safe repo-relative POSIX path",
            ),
            "absolute": (
                {"/carry.py": ["marker"]},
                "path must be a safe repo-relative POSIX path",
            ),
            "uncovered": (
                {"other.py": ["marker"]},
                "path is not covered by files: other.py",
            ),
        }
        for label, (required_markers, message) in cases.items():
            with self.subTest(label=label):
                registry = {
                    "divergences": [
                        self.valid_registry_entry(required_markers=required_markers)
                    ]
                }
                with self.assertRaisesRegex(ValueError, message):
                    DOWNSTREAM_DIVERGENCE_AUDIT.validate_registry(registry)

    def test_required_markers_reject_empty_marker_contracts(self) -> None:
        cases = {
            "empty object": ({}, "must be a non-empty object"),
            "empty marker list": ({"carry.py": []}, "must be a non-empty list"),
            "empty marker": (
                {"carry.py": [""]},
                "entries must be non-empty strings",
            ),
        }
        for label, (required_markers, message) in cases.items():
            with self.subTest(label=label):
                registry = {
                    "divergences": [
                        self.valid_registry_entry(required_markers=required_markers)
                    ]
                }
                with self.assertRaisesRegex(ValueError, message):
                    DOWNSTREAM_DIVERGENCE_AUDIT.validate_registry(registry)

    def test_registry_gate_uses_downstream_carry_not_upstream_ahead_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self.run_git(repo, "init", "-b", "main")
            self.run_git(repo, "config", "user.email", "ci@example.invalid")
            self.run_git(repo, "config", "user.name", "CI")

            (repo / "shared.py").write_text("print('base')\n", encoding="utf-8")
            base_sha = self.commit_all(repo, "base")

            self.run_git(repo, "checkout", "-b", "upstream")
            (repo / "upstream_only.py").write_text("print('upstream')\n", encoding="utf-8")
            upstream_sha = self.commit_all(repo, "upstream")

            self.run_git(repo, "checkout", "-b", "downstream", base_sha)
            (repo / "downstream_only.py").write_text("print('downstream')\n", encoding="utf-8")
            downstream_sha = self.commit_all(repo, "downstream")

            all_items = DOWNSTREAM_DIVERGENCE_AUDIT.diff_items_between(
                repo,
                upstream_sha,
                downstream_sha,
            )
            all_code_items = [item for item in all_items if item.is_code]
            all_paths = sorted({path for item in all_code_items for path in item.paths})
            self.assertEqual(all_paths, ["downstream_only.py", "upstream_only.py"])

            merge_base = DOWNSTREAM_DIVERGENCE_AUDIT.merge_base_sha(
                repo,
                upstream_sha,
                downstream_sha,
            )
            carry_items = DOWNSTREAM_DIVERGENCE_AUDIT.diff_items_between(
                repo,
                merge_base,
                downstream_sha,
            )
            carry_code_items = [item for item in carry_items if item.is_code]
            registry = {
                "_path": "docs/divergences/index.yaml",
                "divergences": [
                    {
                        "id": "downstream-only",
                        "status": "live",
                        "files": ["downstream_only.py"],
                    }
                ],
            }

            registry_state = DOWNSTREAM_DIVERGENCE_AUDIT.reconcile_registry(
                registry,
                carry_code_items,
            )

            self.assertEqual(registry_state["uncovered_code_paths"], [])
            self.assertEqual(
                registry_state["path_registry_ids"],
                {"downstream_only.py": ["downstream-only"]},
            )

    def test_registry_gate_projects_carry_onto_exact_live_tree_diff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self.run_git(repo, "init", "-b", "main")
            self.run_git(repo, "config", "user.email", "ci@example.invalid")
            self.run_git(repo, "config", "user.name", "CI")

            (repo / "phantom.py").write_text("removed by both\n", encoding="utf-8")
            (repo / "unchanged.py").write_text("base\n", encoding="utf-8")
            (repo / "changed.py").write_text("base\n", encoding="utf-8")
            base_sha = self.commit_all(repo, "base")

            self.run_git(repo, "checkout", "-b", "upstream")
            self.run_git(repo, "rm", "phantom.py")
            (repo / "unchanged.py").write_text("shared result\n", encoding="utf-8")
            (repo / "changed.py").write_text("upstream result\n", encoding="utf-8")
            upstream_sha = self.commit_all(repo, "upstream changes")

            self.run_git(repo, "checkout", "-b", "downstream", base_sha)
            self.run_git(repo, "rm", "phantom.py")
            # This downstream change is byte-identical to upstream and must
            # not be treated as an uncovered divergence.
            (repo / "unchanged.py").write_text("shared result\n", encoding="utf-8")
            # A different downstream edit to an upstream-modified path is a
            # genuine downstream divergence and must remain visible.
            (repo / "changed.py").write_text("downstream result\n", encoding="utf-8")
            (repo / "downstream_only.py").write_text("downstream\n", encoding="utf-8")
            downstream_sha = self.commit_all(repo, "downstream changes")

            live_items = DOWNSTREAM_DIVERGENCE_AUDIT.diff_items_between(
                repo, upstream_sha, downstream_sha
            )
            merge_base = DOWNSTREAM_DIVERGENCE_AUDIT.merge_base_sha(
                repo, upstream_sha, downstream_sha
            )
            carry_items = DOWNSTREAM_DIVERGENCE_AUDIT.diff_items_between(
                repo, merge_base, downstream_sha
            )
            projected_items = (
                DOWNSTREAM_DIVERGENCE_AUDIT.downstream_carry_items_for_live_diff(
                    live_items, carry_items
                )
            )
            projected_paths = sorted(
                {
                    path
                    for item in projected_items
                    if item.is_code
                    for path in item.paths
                }
            )

            self.assertEqual(
                projected_paths,
                ["changed.py", "downstream_only.py"],
            )

    def test_registry_projection_ignores_docs_and_readme_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self.run_git(repo, "init", "-b", "main")
            self.run_git(repo, "config", "user.email", "ci@example.invalid")
            self.run_git(repo, "config", "user.name", "CI")

            (repo / "README.md").write_text("base README\n", encoding="utf-8")
            (repo / "docs").mkdir()
            (repo / "docs" / "guide.md").write_text("base guide\n", encoding="utf-8")
            base_sha = self.commit_all(repo, "base")

            self.run_git(repo, "checkout", "-b", "upstream")
            (repo / "README.md").write_text("upstream README\n", encoding="utf-8")
            (repo / "docs" / "guide.md").write_text("upstream guide\n", encoding="utf-8")
            upstream_sha = self.commit_all(repo, "upstream docs")

            self.run_git(repo, "checkout", "-b", "downstream", base_sha)
            (repo / "README.md").write_text("downstream README\n", encoding="utf-8")
            (repo / "docs" / "guide.md").write_text("downstream guide\n", encoding="utf-8")
            downstream_sha = self.commit_all(repo, "downstream docs")

            live_items = DOWNSTREAM_DIVERGENCE_AUDIT.diff_items_between(
                repo, upstream_sha, downstream_sha
            )
            carry_items = DOWNSTREAM_DIVERGENCE_AUDIT.diff_items_between(
                repo,
                DOWNSTREAM_DIVERGENCE_AUDIT.merge_base_sha(
                    repo, upstream_sha, downstream_sha
                ),
                downstream_sha,
            )
            projected_items = (
                DOWNSTREAM_DIVERGENCE_AUDIT.downstream_carry_items_for_live_diff(
                    live_items, carry_items
                )
            )

            self.assertEqual(projected_items, [])
            registry_state = DOWNSTREAM_DIVERGENCE_AUDIT.reconcile_registry(
                {"_path": "docs/divergences/index.yaml", "divergences": []},
                projected_items,
            )
            self.assertEqual(registry_state["uncovered_code_paths"], [])

    def test_registry_projection_is_endpoint_precise_for_renames_copies_and_changes(
        self,
    ) -> None:
        def item(status: str, *paths: str) -> DOWNSTREAM_DIVERGENCE_AUDIT.DiffItem:
            return DOWNSTREAM_DIVERGENCE_AUDIT.DiffItem(
                status=status,
                paths=paths,
                old_mode="100644",
                new_mode="100644",
                rename_score=status[1:] if len(status) > 1 else None,
                is_code=True,
            )

        # The upstream endpoint differs from the downstream carry endpoint;
        # only the shared destination is attributable to downstream.
        live_items = [
            item("R100", "upstream-renamed.py", "renamed.py"),
            item("C100", "upstream-copied.py", "copied.py"),
            item("R100", "source.py", "README.md"),
            item("D", "deleted.py"),
            item("A", "added.py"),
            item("T", "typechanged.py"),
        ]
        carry_items = [
            item("R100", "original.py", "renamed.py"),
            item("C100", "source.py", "copied.py"),
            item("C100", "other.py", "README.md"),
            item("D", "deleted.py"),
            item("A", "added.py"),
            item("T", "typechanged.py"),
        ]

        projected_items = (
            DOWNSTREAM_DIVERGENCE_AUDIT.downstream_carry_items_for_live_diff(
                live_items, carry_items
            )
        )
        projected_paths = [
            path
            for projected in projected_items
            if projected.is_code
            for path in projected.paths
        ]

        self.assertEqual(
            projected_paths,
            ["renamed.py", "copied.py", "deleted.py", "added.py", "typechanged.py"],
        )
        self.assertEqual(
            DOWNSTREAM_DIVERGENCE_AUDIT.carry_endpoint_paths(carry_items[1]),
            ("copied.py",),
        )
        self.assertNotIn("upstream-renamed.py", projected_paths)
        self.assertNotIn("upstream-copied.py", projected_paths)

        rename_projection = next(
            projected
            for projected in projected_items
            if projected.paths == ("renamed.py",)
        )
        self.assertEqual(rename_projection.status, "M")
        self.assertIsNone(rename_projection.rename_score)

        copy_projection = next(
            projected
            for projected in projected_items
            if projected.paths == ("copied.py",)
        )
        self.assertEqual(copy_projection.status, "M")
        self.assertIsNone(copy_projection.rename_score)

        documentation_projection = next(
            projected
            for projected in projected_items
            if projected.paths == ("README.md",)
        )
        self.assertEqual(documentation_projection.status, "M")
        self.assertIsNone(documentation_projection.rename_score)
        self.assertFalse(documentation_projection.is_code)

    def test_superseded_hunk_requires_complete_carry_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self.run_git(repo, "init", "-b", "main")
            self.run_git(repo, "config", "user.email", "ci@example.invalid")
            self.run_git(repo, "config", "user.name", "CI")

            (repo / "carry.py").write_text(
                "before\n\n\n\n\nend\n",
                encoding="utf-8",
            )
            base_sha = self.commit_all(repo, "base")

            self.run_git(repo, "checkout", "-b", "upstream")
            (repo / "carry.py").write_text(
                "before\nshared\n\n\n\n\nend\n",
                encoding="utf-8",
            )
            upstream_sha = self.commit_all(repo, "upstream shared hunk")
            upstream_hunk_header = DOWNSTREAM_DIVERGENCE_AUDIT.diff_hunks(
                repo,
                base_sha,
                upstream_sha,
                "carry.py",
            )[0].header

            self.run_git(repo, "checkout", "-b", "downstream", base_sha)
            (repo / "carry.py").write_text(
                "before\nshared\n\n\n\n\nend\n",
                encoding="utf-8",
            )
            downstream_sha = self.commit_all(repo, "downstream shared hunk")

            registry = {
                "divergences": [],
                "superseded_hunks": [
                    {
                        "id": "shared-hunk",
                        "path": "carry.py",
                        "upstream_commit": upstream_sha,
                        "hunk_header": upstream_hunk_header,
                        "hunk_lines": ["+shared"],
                    }
                ],
            }
            DOWNSTREAM_DIVERGENCE_AUDIT.validate_registry(registry)
            state = DOWNSTREAM_DIVERGENCE_AUDIT.verify_superseded_hunks(
                repo,
                registry,
                upstream_sha,
                base_sha,
                downstream_sha,
            )
            self.assertEqual(state["superseded_carry_paths"], ["carry.py"])

            live_items = [
                DOWNSTREAM_DIVERGENCE_AUDIT.DiffItem(
                    status="M",
                    paths=("carry.py",),
                    old_mode="100644",
                    new_mode="100644",
                    rename_score=None,
                    is_code=True,
                )
            ]
            carry_items = list(live_items)
            self.assertEqual(
                DOWNSTREAM_DIVERGENCE_AUDIT.downstream_carry_items_for_live_diff(
                    live_items,
                    carry_items,
                    superseded_carry_paths=set(state["superseded_carry_paths"]),
                ),
                [],
            )

            (repo / "carry.py").write_text(
                "before\nshared\n\n\n\n\nnew downstream\nend\n",
                encoding="utf-8",
            )
            downstream_with_new_delta = self.commit_all(
                repo, "downstream new delta"
            )
            with self.assertRaisesRegex(
                ValueError,
                r"declared hunk position/context is absent",
            ):
                DOWNSTREAM_DIVERGENCE_AUDIT.verify_superseded_hunks(
                    repo,
                    registry,
                    upstream_sha,
                    base_sha,
                    downstream_with_new_delta,
                )

    def test_superseded_hunk_position_context_proof_rejects_relocation_duplicates_and_reorder(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self.run_git(repo, "init", "-b", "main")
            self.run_git(repo, "config", "user.email", "ci@example.invalid")
            self.run_git(repo, "config", "user.name", "CI")

            base_text = "before\nanchor\nend\n"
            upstream_text = "before\nshared-a\nshared-b\nanchor\nend\n"
            (repo / "carry.py").write_text(base_text, encoding="utf-8")
            base_sha = self.commit_all(repo, "base")

            self.run_git(repo, "checkout", "-b", "upstream")
            (repo / "carry.py").write_text(upstream_text, encoding="utf-8")
            upstream_sha = self.commit_all(repo, "upstream shared hunk")
            upstream_hunk_header = DOWNSTREAM_DIVERGENCE_AUDIT.diff_hunks(
                repo,
                base_sha,
                upstream_sha,
                "carry.py",
            )[0].header

            registry = {
                "divergences": [],
                "superseded_hunks": [
                    {
                        "id": "shared-hunk",
                        "path": "carry.py",
                        "upstream_commit": upstream_sha,
                        "hunk_header": upstream_hunk_header,
                        "hunk_lines": ["+shared-a", "+shared-b"],
                    }
                ],
            }
            DOWNSTREAM_DIVERGENCE_AUDIT.validate_registry(registry)

            self.run_git(repo, "checkout", "-b", "downstream-valid", base_sha)
            (repo / "carry.py").write_text(upstream_text, encoding="utf-8")
            valid_downstream_sha = self.commit_all(repo, "downstream equivalent hunk")
            valid_state = DOWNSTREAM_DIVERGENCE_AUDIT.verify_superseded_hunks(
                repo,
                registry,
                upstream_sha,
                base_sha,
                valid_downstream_sha,
            )
            self.assertEqual(valid_state["superseded_carry_paths"], ["carry.py"])

            drifted_registry = {
                **registry,
                "superseded_hunks": [
                    {
                        **registry["superseded_hunks"][0],
                        "hunk_header": "@@ -2,2 +3,2 @@ fn moved_elsewhere(",
                    }
                ],
            }
            with self.assertRaisesRegex(
                ValueError,
                r"declared hunk header/body is absent from upstream",
            ):
                DOWNSTREAM_DIVERGENCE_AUDIT.verify_superseded_hunks(
                    repo,
                    drifted_registry,
                    upstream_sha,
                    base_sha,
                    valid_downstream_sha,
                )

            invalid_variants = {
                "relocated": "before\nanchor\nend\nshared-a\nshared-b\n",
                "duplicated": (
                    "before\nshared-a\nshared-b\nanchor\nshared-a\nshared-b\nend\n"
                ),
                "reordered": "before\nshared-b\nshared-a\nanchor\nend\n",
            }
            for label, downstream_text in invalid_variants.items():
                with self.subTest(label=label):
                    self.run_git(
                        repo,
                        "checkout",
                        "-b",
                        f"downstream-{label}",
                        base_sha,
                    )
                    (repo / "carry.py").write_text(
                        downstream_text,
                        encoding="utf-8",
                    )
                    downstream_sha = self.commit_all(
                        repo,
                        f"downstream {label} hunk",
                    )
                    with self.assertRaisesRegex(
                        ValueError,
                        r"declared hunk position/context is absent",
                    ):
                        DOWNSTREAM_DIVERGENCE_AUDIT.verify_superseded_hunks(
                            repo,
                            registry,
                            upstream_sha,
                            base_sha,
                            downstream_sha,
                        )

    def test_superseded_hunk_requires_contextual_addition_only_evidence(self) -> None:
        cases = {
            "pure deletion": DOWNSTREAM_DIVERGENCE_AUDIT.DiffHunk(
                "@@ -1,1 +0,0 @@",
                ("-gone",),
            ),
            "zero context addition": DOWNSTREAM_DIVERGENCE_AUDIT.DiffHunk(
                "@@ -0,0 +1,1 @@",
                ("+added",),
            ),
            "replacement with deletion": DOWNSTREAM_DIVERGENCE_AUDIT.DiffHunk(
                "@@ -1,1 +1,1 @@",
                ("-old", "+new"),
            ),
            "no final newline marker": DOWNSTREAM_DIVERGENCE_AUDIT.DiffHunk(
                "@@ -1,1 +1,2 @@",
                (" context", "+added"),
                no_newline_marker=True,
            ),
        }
        for label, hunk in cases.items():
            with self.subTest(label=label):
                self.assertFalse(
                    DOWNSTREAM_DIVERGENCE_AUDIT.is_safe_addition_hunk(hunk)
                )

    def test_superseded_hunk_requires_complete_line_boundaries(self) -> None:
        hunk = DOWNSTREAM_DIVERGENCE_AUDIT.DiffHunk(
            "@@ -1,2 +1,3 @@",
            (" context", "+added", " tail"),
        )
        variants = {
            "exact": (b"context\nadded\ntail\n", True),
            "prefix-context-drift": (b"prefix-context\nadded\ntail\n", False),
            "suffix-context-drift": (b"context\nadded\ntail-suffix\n", False),
        }
        for label, (contents, expected) in variants.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp)
                self.run_git(repo, "init", "-b", "main")
                self.run_git(repo, "config", "user.email", "ci@example.invalid")
                self.run_git(repo, "config", "user.name", "CI")
                (repo / "carry.py").write_bytes(contents)
                commit_sha = self.commit_all(repo, label)
                self.assertEqual(
                    DOWNSTREAM_DIVERGENCE_AUDIT.file_contains_hunk_new_lines(
                        repo, commit_sha, "carry.py", hunk
                    ),
                    expected,
                )

    def test_superseded_hunk_rejects_ambiguous_text_and_tree_metadata(self) -> None:
        base_text = b"before\nanchor\nend\n"
        updated_text = b"before\nshared\nanchor\nend\n"
        variants: dict[str, tuple[bytes, bytes, str]] = {
            "crlf": (
                base_text,
                b"before\r\nshared\r\nanchor\r\nend\r\n",
                "bytes are binary",
            ),
            "final newline": (
                base_text,
                b"before\nshared\nanchor\nend",
                "bytes are binary",
            ),
            "binary": (
                base_text,
                b"before\nshared\n\x00anchor\nend\n",
                "bytes are binary",
            ),
        }

        for label, (base_bytes, upstream_bytes, error_pattern) in variants.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp)
                self.run_git(repo, "init", "-b", "main")
                self.run_git(repo, "config", "user.email", "ci@example.invalid")
                self.run_git(repo, "config", "user.name", "CI")

                (repo / "carry.py").write_bytes(base_bytes)
                base_sha = self.commit_all(repo, "base")
                self.run_git(repo, "checkout", "-b", "upstream")
                (repo / "carry.py").write_bytes(upstream_bytes)
                upstream_sha = self.commit_all(repo, f"upstream {label}")
                upstream_hunks = DOWNSTREAM_DIVERGENCE_AUDIT.diff_hunks(
                    repo,
                    base_sha,
                    upstream_sha,
                    "carry.py",
                )
                hunk_header = (
                    upstream_hunks[0].header
                    if upstream_hunks
                    else "@@ -1,3 +1,4 @@"
                )

                self.run_git(repo, "checkout", "-b", "downstream", base_sha)
                (repo / "carry.py").write_bytes(upstream_bytes)
                downstream_sha = self.commit_all(repo, f"downstream {label}")
                registry = {
                    "divergences": [],
                    "superseded_hunks": [
                        {
                            "id": label,
                            "path": "carry.py",
                            "upstream_commit": upstream_sha,
                            "hunk_header": hunk_header,
                            "hunk_lines": ["+shared"],
                        }
                    ],
                }

                with self.assertRaisesRegex(ValueError, error_pattern):
                    DOWNSTREAM_DIVERGENCE_AUDIT.verify_superseded_hunks(
                        repo,
                        registry,
                        upstream_sha,
                        base_sha,
                        downstream_sha,
                    )

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self.run_git(repo, "init", "-b", "main")
            self.run_git(repo, "config", "user.email", "ci@example.invalid")
            self.run_git(repo, "config", "user.name", "CI")
            (repo / "carry.py").write_bytes(base_text)
            base_sha = self.commit_all(repo, "base")
            self.run_git(repo, "checkout", "-b", "upstream")
            (repo / "carry.py").write_bytes(updated_text)
            os.chmod(repo / "carry.py", 0o700)
            upstream_sha = self.commit_all(repo, "upstream executable mode")
            upstream_hunks = DOWNSTREAM_DIVERGENCE_AUDIT.diff_hunks(
                repo,
                base_sha,
                upstream_sha,
                "carry.py",
            )

            self.run_git(repo, "checkout", "-b", "downstream", base_sha)
            (repo / "carry.py").write_bytes(updated_text)
            downstream_sha = self.commit_all(repo, "downstream content")
            registry = {
                "divergences": [],
                "superseded_hunks": [
                    {
                        "id": "executable-mode",
                        "path": "carry.py",
                        "upstream_commit": upstream_sha,
                        "hunk_header": upstream_hunks[0].header,
                        "hunk_lines": ["+shared"],
                    }
                ],
            }
            with self.assertRaisesRegex(ValueError, "mode/type-stable|regular 100644"):
                DOWNSTREAM_DIVERGENCE_AUDIT.verify_superseded_hunks(
                    repo,
                    registry,
                    upstream_sha,
                    base_sha,
                    downstream_sha,
                )

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self.run_git(repo, "init", "-b", "main")
            self.run_git(repo, "config", "user.email", "ci@example.invalid")
            self.run_git(repo, "config", "user.name", "CI")
            (repo / "carry.py").write_bytes(base_text)
            base_sha = self.commit_all(repo, "base")
            self.run_git(repo, "checkout", "-b", "upstream")
            (repo / "carry.py").unlink()
            (repo / "carry.py").symlink_to("target")
            upstream_sha = self.commit_all(repo, "upstream symlink type")

            self.run_git(repo, "checkout", "-b", "downstream", base_sha)
            (repo / "carry.py").write_bytes(updated_text)
            downstream_sha = self.commit_all(repo, "downstream content")
            registry = {
                "divergences": [],
                "superseded_hunks": [
                    {
                        "id": "symlink-type",
                        "path": "carry.py",
                        "upstream_commit": upstream_sha,
                        "hunk_header": "@@ -1,3 +1,4 @@",
                        "hunk_lines": ["+shared"],
                    }
                ],
            }
            with self.assertRaisesRegex(ValueError, "regular 100644|mode/type-stable"):
                DOWNSTREAM_DIVERGENCE_AUDIT.verify_superseded_hunks(
                    repo,
                    registry,
                    upstream_sha,
                    base_sha,
                    downstream_sha,
                )

    def test_superseded_hunk_schema_rejects_stale_or_ambiguous_evidence(self) -> None:
        cases = {
            "short upstream sha": {
                "id": "short-sha",
                "path": "carry.py",
                "upstream_commit": "c8ddb210",
                "hunk_header": "@@ -1,1 +1,2 @@",
                "hunk_lines": ["+shared"],
            },
            "missing diff marker": {
                "id": "missing-marker",
                "path": "carry.py",
                "upstream_commit": "c8ddb210d2429cacacf86593e157114b00634f13",
                "hunk_header": "@@ -1,1 +1,2 @@",
                "hunk_lines": ["shared"],
            },
            "malformed hunk header": {
                "id": "malformed-header",
                "path": "carry.py",
                "upstream_commit": "c8ddb210d2429cacacf86593e157114b00634f13",
                "hunk_header": "not-a-hunk",
                "hunk_lines": ["+shared"],
            },
        }
        for label, declaration in cases.items():
            with self.subTest(label=label):
                with self.assertRaisesRegex(ValueError, "superseded_hunks"):
                    DOWNSTREAM_DIVERGENCE_AUDIT.validate_registry(
                        {"divergences": [], "superseded_hunks": [declaration]}
                    )

    def test_superseded_hunk_registry_binds_expected_literal_header(self) -> None:
        registry = json.loads(
            (REPO_ROOT / "docs/divergences/index.yaml").read_text(encoding="utf-8")
        )
        declaration = next(
            entry
            for entry in registry["superseded_hunks"]
            if entry["id"] == "http-client-outbound-proxy-blocking-read-upstreamed"
        )
        self.assertEqual(
            declaration["hunk_header"],
            "@@ -64,6 +64,9 @@ fn spawn_http_listener(",
        )

    def test_superseded_hunk_no_enforce_is_fail_closed_for_malformed_evidence(
        self,
    ) -> None:
        cases = {
            "non-list": {"superseded_hunks": {"path": "carry.py"}},
            "non-object entry": {"superseded_hunks": ["not-an-object"]},
            "non-string path": {
                "superseded_hunks": [
                    {
                        "id": "bad-path",
                        "path": ["carry.py"],
                        "upstream_commit": "c8ddb210d2429cacacf86593e157114b00634f13",
                        "hunk_lines": ["+shared"],
                    }
                ]
            },
        }
        for label, registry in cases.items():
            with self.subTest(label=label):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    state = DOWNSTREAM_DIVERGENCE_AUDIT.verify_superseded_hunks(
                        Path("."),
                        registry,
                        "unused",
                        "unused",
                        "unused",
                        enforce=False,
                    )
                self.assertEqual(state, {"checks": [], "superseded_carry_paths": []})
                self.assertIn("warning:", stderr.getvalue())


class SednaHeavyCheckoutIdentityTests(unittest.TestCase):
    workflow_path = REPO_ROOT / ".github/workflows/sedna-heavy-tests.yml"

    def _run_identity_fixture(
        self,
        repo: TempGitRepo | Path,
        *,
        event_name: str,
        checkout_ref: str,
        input_ref: str = "",
        pr_head_sha: str = "",
        pr_head_ref: str = "",
    ) -> tuple[subprocess.CompletedProcess, dict[str, str]]:
        script = workflow_checkout_identity_script(self.workflow_path)
        command = f"set -euo pipefail\n{script}\n"
        repo_root = repo.root if isinstance(repo, TempGitRepo) else repo
        with tempfile.TemporaryDirectory(prefix="ci-planner-output-") as tmpdir:
            output_path = Path(tmpdir) / "github-output.txt"
            output_path.write_text("", encoding="utf-8")
            env = {
                **os.environ,
                "GITHUB_EVENT_NAME": event_name,
                "GITHUB_OUTPUT": str(output_path),
                "CHECKOUT_REF": checkout_ref,
                "DISPLAY_REF": "main",
                "INPUT_REF": input_ref,
                "PR_HEAD_SHA": pr_head_sha,
                "PR_HEAD_REF": pr_head_ref,
            }
            proc = subprocess.run(
                ["bash", "-c", command],
                cwd=repo_root,
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            return proc, parse_github_output_file(output_path)

    def _fixture_repo(
        self,
    ) -> tuple[
        TempGitRepo,
        tempfile.TemporaryDirectory,
        tempfile.TemporaryDirectory,
        Path,
        str,
        str,
        str,
    ]:
        source = TempGitRepo()
        base_sha = source.commit("fixture base", {"payload.txt": "base\n"})
        source._git("checkout", "--orphan", "target")
        source._git("rm", "-rf", ".")
        resolved_sha = source.commit("fixture resolved", {"payload.txt": "resolved\n"})
        source._git("checkout", "main")
        moved_sha = source.commit("fixture moved", {"payload.txt": "moved\n"})
        source._git("branch", "mutable", resolved_sha)
        source._git("tag", "lightweight", resolved_sha)
        source._git("tag", "-a", "annotated", "-m", "annotated fixture", resolved_sha)
        origin_tmpdir = tempfile.TemporaryDirectory(prefix="ci-planner-origin-")
        origin = Path(origin_tmpdir.name) / "origin.git"
        source._git("clone", "--bare", str(source.root), str(origin))
        source._git("remote", "add", "origin", str(origin))
        checkout_tmpdir = tempfile.TemporaryDirectory(prefix="ci-planner-checkout-")
        checkout = Path(checkout_tmpdir.name) / "checkout"
        for args in (
            ("init", "--initial-branch=main", str(checkout)),
            ("-C", str(checkout), "remote", "add", "origin", f"file://{origin}"),
            (
                "-C",
                str(checkout),
                "fetch",
                "--depth=1",
                "--no-tags",
                "origin",
                "refs/heads/main:refs/remotes/origin/main",
            ),
            ("-C", str(checkout), "checkout", "-B", "main", "origin/main"),
        ):
            subprocess.run(
                ["git", *args],
                check=True,
                capture_output=True,
                text=True,
            )
        return (
            source,
            origin_tmpdir,
            checkout_tmpdir,
            checkout,
            base_sha,
            resolved_sha,
            moved_sha,
        )

    def _move_fixture_refs(
        self,
        repo: TempGitRepo,
        origin_tmpdir: tempfile.TemporaryDirectory,
        moved_sha: str,
    ) -> None:
        origin = Path(origin_tmpdir.name) / "origin.git"
        repo._git("branch", "-f", "mutable", moved_sha)
        repo._git("tag", "-f", "lightweight", moved_sha)
        repo._git("tag", "-f", "-a", "annotated", "-m", "moved annotated fixture", moved_sha)
        repo._git(
            "push",
            "--force",
            str(origin),
            "refs/heads/mutable:refs/heads/mutable",
            "refs/tags/lightweight:refs/tags/lightweight",
            "refs/tags/annotated:refs/tags/annotated",
        )

    def test_complete_output_boundary_uses_real_git_refs_and_peels_to_immutable_sha(self) -> None:
        (
            source,
            origin_tmpdir,
            checkout_tmpdir,
            checkout,
            base_sha,
            resolved_sha,
            moved_sha,
        ) = self._fixture_repo()
        try:
            pr_head_sha = "a" * 40
            captured_outputs: dict[str, dict[str, str]] = {}
            workflow_sha = moved_sha
            self.assertNotEqual(
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(checkout),
                        "cat-file",
                        "-e",
                        f"{resolved_sha}^{{commit}}",
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                ).returncode,
                0,
            )
            cases = [
                (
                    "pull_request",
                    "",
                    pr_head_sha,
                    "feature/head",
                    pr_head_sha,
                ),
                ("workflow_dispatch", "", "", "", workflow_sha),
                ("workflow_dispatch", moved_sha, "", "", moved_sha),
                ("workflow_dispatch", resolved_sha, "", "", resolved_sha),
                ("workflow_dispatch", "mutable", "", "", resolved_sha),
                ("workflow_dispatch", "refs/heads/mutable", "", "", resolved_sha),
                ("workflow_dispatch", "lightweight", "", "", resolved_sha),
                ("workflow_dispatch", "annotated", "", "", resolved_sha),
            ]
            for event_name, input_ref, pr_head, pr_ref, expected_sha in cases:
                with self.subTest(event_name=event_name, input_ref=input_ref):
                    proc, outputs = self._run_identity_fixture(
                        checkout,
                        event_name=event_name,
                        checkout_ref=workflow_sha,
                        input_ref=input_ref,
                        pr_head_sha=pr_head,
                        pr_head_ref=pr_ref,
                    )
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    self.assertEqual(outputs.get("checkout_ref"), expected_sha)
                    self.assertEqual(outputs.get("checkout_sha"), expected_sha)
                    self.assertEqual(
                        outputs.get("event_policy"),
                        (
                            "pull_request_exact_head_lane_fingerprint"
                            if event_name == "pull_request"
                            else "workflow_dispatch_exact_head_manual"
                        ),
                    )
                    self.assertEqual(
                        outputs.get("display_ref"),
                        pr_ref or input_ref or "main",
                    )
                    if event_name == "workflow_dispatch":
                        captured_outputs[input_ref or "<default>"] = outputs

            self._move_fixture_refs(source, origin_tmpdir, moved_sha)
            source._git("fetch", "origin", "mutable")
            self.assertEqual(source.rev_parse("origin/mutable"), moved_sha)
            for input_ref, outputs in captured_outputs.items():
                captured_sha = outputs.get("checkout_sha")
                self.assertIsNotNone(captured_sha, input_ref)
                resolved = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(checkout),
                        "rev-parse",
                        "--verify",
                        f"{captured_sha}^{{commit}}",
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(resolved.returncode, 0, input_ref)
                self.assertEqual(resolved.stdout.strip(), captured_sha, input_ref)
                self.assertEqual(
                    subprocess.run(
                        [
                            "git",
                            "-C",
                            str(checkout),
                            "cat-file",
                            "-e",
                            f"{captured_sha}^{{commit}}",
                        ],
                        check=False,
                        capture_output=True,
                        text=True,
                    ).returncode,
                    0,
                    input_ref,
                )
            self.assertEqual(resolved_sha, source.rev_parse(resolved_sha))
            self.assertNotEqual(resolved_sha, moved_sha)
        finally:
            source.cleanup()
            origin_tmpdir.cleanup()
            checkout_tmpdir.cleanup()

    def test_unsupported_event_fails_closed_before_identity_output(self) -> None:
        source, origin_tmpdir, checkout_tmpdir, checkout, base_sha, _, _ = self._fixture_repo()
        try:
            proc, outputs = self._run_identity_fixture(
                checkout,
                event_name="push",
                checkout_ref=base_sha,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(outputs, {})
            self.assertIn("unsupported event", proc.stderr)
        finally:
            source.cleanup()
            origin_tmpdir.cleanup()
            checkout_tmpdir.cleanup()

    def test_full_sha_manual_input_fails_closed_when_commit_is_missing(self) -> None:
        source, origin_tmpdir, checkout_tmpdir, checkout, base_sha, _, _ = self._fixture_repo()
        try:
            proc, outputs = self._run_identity_fixture(
                checkout,
                event_name="workflow_dispatch",
                checkout_ref=base_sha,
                input_ref="e" * 40,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(outputs, {})
            self.assertIn("unable to fetch checkout ref", proc.stderr)
        finally:
            source.cleanup()
            origin_tmpdir.cleanup()
            checkout_tmpdir.cleanup()

    def test_unsafe_manual_ref_inputs_fail_before_fetch_or_identity_output(self) -> None:
        source, origin_tmpdir, checkout_tmpdir, checkout, base_sha, _, _ = self._fixture_repo()
        try:
            fetch_head = checkout / ".git" / "FETCH_HEAD"
            initial_fetch_head = fetch_head.read_bytes()
            for input_ref in ("--upload-pack=evil", "bad\nref"):
                with self.subTest(input_ref=repr(input_ref)):
                    proc, outputs = self._run_identity_fixture(
                        checkout,
                        event_name="workflow_dispatch",
                        checkout_ref=base_sha,
                        input_ref=input_ref,
                    )
                    self.assertNotEqual(proc.returncode, 0)
                    self.assertEqual(outputs, {})
                    self.assertIn("unsafe checkout ref input rejected", proc.stderr)
                    self.assertEqual(fetch_head.read_bytes(), initial_fetch_head)
        finally:
            source.cleanup()
            origin_tmpdir.cleanup()
            checkout_tmpdir.cleanup()

    def test_identity_variables_are_not_reassigned_after_output_boundary(self) -> None:
        payload = load_workflow_payload(self.workflow_path)
        metadata_step = workflow_step_by_name(
            self.workflow_path,
            "metadata",
            "Compute checkout ref",
        )
        run_script = metadata_step.get("run") or ""
        end = run_script.index("# END checkout identity")
        post_identity = run_script[end:]
        for variable in ("checkout_ref", "checkout_sha", "display_ref"):
            self.assertNotRegex(post_identity, rf"(?m)^\s*{variable}=")

        jobs = payload.get("jobs") or {}
        metadata_outputs = jobs["metadata"].get("outputs") or {}
        self.assertEqual(
            metadata_outputs.get("checkout_ref"),
            "${{ steps.meta.outputs.checkout_ref }}",
        )
        self.assertEqual(
            metadata_outputs.get("checkout_sha"),
            "${{ steps.meta.outputs.checkout_sha }}",
        )
        self.assertEqual(
            metadata_outputs.get("display_ref"),
            "${{ steps.meta.outputs.display_ref }}",
        )
        expected_consumers = {
            "smoke_workflow_lanes",
            "smoke_node_lanes",
            "smoke_rust_minimal_lanes",
            "smoke_rust_integration_lanes",
            "smoke_release_lanes",
            "workflow_lanes",
            "node_lanes",
            "rust_minimal_lanes",
            "rust_minimal_batches",
            "rust_integration_lanes",
            "rust_integration_batches",
            "release_lanes_eager",
            "release_lanes",
        }
        actual_consumers = {
            name
            for name, job in jobs.items()
            if ((job or {}).get("with") or {}).get("checkout_ref")
            == "${{ needs.metadata.outputs.checkout_ref }}"
        }
        self.assertEqual(actual_consumers, expected_consumers)
        self.assertEqual(len(actual_consumers), 13)
        summary_run = next(
            step.get("run") or ""
            for step in (jobs["summary"].get("steps") or [])
            if "aggregate_validation_summary.py" in (step.get("run") or "")
        )
        self.assertIn('--checkout-ref "${{ needs.metadata.outputs.checkout_ref }}"', summary_run)
        self.assertIn('--head-sha "${{ needs.metadata.outputs.checkout_sha }}"', summary_run)


class ValidationPlanScriptTests(unittest.TestCase):
    collab_spawn_identity_candidate_paths = (
        RouteSelectionTests.collab_spawn_identity_candidate_paths
    )

    maxDiff = None

    def validation_lab_fingerprint(
        self,
        *,
        selection_meta: dict | None = None,
        artifact_build: bool = False,
        include_explicit_lanes: bool = False,
    ) -> str:
        selection = {
            "fanout_tier": "enterprise",
            "run_selected_lanes": True,
            "run_smoke_gate": False,
            "smoke_gate_kind": "none",
            "run_artifact": artifact_build,
            "matrix_fail_fast": False,
            "matrix_max_parallel": 4,
            "workflow_max_parallel": 4,
            "node_max_parallel": 4,
            "rust_minimal_max_parallel": 4,
            "rust_integration_max_parallel": 4,
            "release_max_parallel": 4,
            "rust_batching_mode": "auto",
            "selected_setup_classes": ["workflow"],
            "selected_lane_ids": ["codex.workflow-ci-sanity"],
            "planned_matrix": {
                "include": [
                    {
                        "lane_id": "codex.workflow-ci-sanity",
                        "setup_class": "workflow",
                    }
                ]
            },
            "smoke_matrix": {"include": []},
            "selected_matrix": {
                "include": [
                    {
                        "lane_id": "codex.workflow-ci-sanity",
                        "setup_class": "workflow",
                    }
                ]
            },
            "selected_rust_minimal_batch_matrix": {"include": []},
            "selected_rust_integration_batch_matrix": {"include": []},
        }
        if selection_meta:
            selection.update(selection_meta)
        payload = VALIDATION_PLAN_FINGERPRINT.plan_fingerprint_payload(
            selection_meta=selection,
            workflow="validation-lab.yml",
            workflow_ref="sednalabs/codex/.github/workflows/validation-lab.yml@refs/heads/main",
            workflow_sha="feedface",
            target_head_sha="abc123",
            profile="targeted",
            lane_set="docs",
            fanout_tier="enterprise",
            lanes="codex.workflow-ci-sanity",
            rust_batching="auto",
            artifact_build=artifact_build,
            include_explicit_lanes=include_explicit_lanes,
        )
        return VALIDATION_PLAN_FINGERPRINT.fingerprint_payload(payload)

    def test_validation_lab_plan_fingerprint_is_stable_for_exact_plan(self) -> None:
        first = self.validation_lab_fingerprint()
        second = self.validation_lab_fingerprint()

        self.assertEqual(first, second)

    def test_validation_lab_plan_fingerprint_changes_for_lane_list(self) -> None:
        baseline = self.validation_lab_fingerprint()
        changed = self.validation_lab_fingerprint(
            selection_meta={
                "selected_lane_ids": [
                    "codex.workflow-ci-sanity",
                    "codex.downstream-docs-check",
                ],
                "planned_matrix": {
                    "include": [
                        {
                            "lane_id": "codex.workflow-ci-sanity",
                            "setup_class": "workflow",
                        },
                        {
                            "lane_id": "codex.downstream-docs-check",
                            "setup_class": "workflow",
                        },
                    ]
                },
            }
        )

        self.assertNotEqual(baseline, changed)

    def test_validation_lab_plan_fingerprint_changes_for_artifact_flag(self) -> None:
        baseline = self.validation_lab_fingerprint(artifact_build=False)
        artifact = self.validation_lab_fingerprint(artifact_build=True)

        self.assertNotEqual(baseline, artifact)

    def test_validation_lab_plan_fingerprint_reports_missing_selection_env(self) -> None:
        env = dict(os.environ)
        env.pop("SELECTION_META", None)
        proc = subprocess.run(
            [
                "python3",
                str(SCRIPTS_DIR / "validation_plan_fingerprint.py"),
                "--workflow",
                "validation-lab.yml",
                "--workflow-ref",
                "sednalabs/codex/.github/workflows/validation-lab.yml@refs/heads/main",
                "--workflow-sha",
                "feedface",
                "--target-head-sha",
                "abc123",
                "--profile",
                "targeted",
                "--lane-set",
                "docs",
                "--fanout-tier",
                "enterprise",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("missing selection metadata env: SELECTION_META", proc.stderr)

    def test_validation_lab_plan_fingerprint_reads_selection_stdin(self) -> None:
        selection = {
            "selected_lane_ids": ["codex.workflow-ci-sanity"],
            "planned_matrix": {"include": []},
        }
        env = dict(os.environ)
        env.pop("SELECTION_META", None)
        proc = subprocess.run(
            [
                "python3",
                str(SCRIPTS_DIR / "validation_plan_fingerprint.py"),
                "--selection-meta-stdin",
                "--workflow",
                "validation-lab.yml",
                "--workflow-ref",
                "sednalabs/codex/.github/workflows/validation-lab.yml@refs/heads/main",
                "--workflow-sha",
                "feedface",
                "--target-head-sha",
                "abc123",
                "--profile",
                "targeted",
                "--lane-set",
                "docs",
                "--fanout-tier",
                "enterprise",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=env,
            input=json.dumps(selection),
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        expected_payload = VALIDATION_PLAN_FINGERPRINT.plan_fingerprint_payload(
            selection_meta=selection,
            workflow="validation-lab.yml",
            workflow_ref=(
                "sednalabs/codex/.github/workflows/validation-lab.yml@refs/heads/main"
            ),
            workflow_sha="feedface",
            target_head_sha="abc123",
            profile="targeted",
            lane_set="docs",
            fanout_tier="enterprise",
            lanes="",
            rust_batching="auto",
            artifact_build=False,
            include_explicit_lanes=False,
        )
        self.assertEqual(
            proc.stdout.strip(),
            VALIDATION_PLAN_FINGERPRINT.fingerprint_payload(expected_payload),
        )

    def test_validation_lab_plan_fingerprint_reports_missing_selection_stdin(self) -> None:
        proc = subprocess.run(
            [
                "python3",
                str(SCRIPTS_DIR / "validation_plan_fingerprint.py"),
                "--selection-meta-stdin",
                "--workflow",
                "validation-lab.yml",
                "--workflow-ref",
                "sednalabs/codex/.github/workflows/validation-lab.yml@refs/heads/main",
                "--workflow-sha",
                "feedface",
                "--target-head-sha",
                "abc123",
                "--profile",
                "targeted",
                "--lane-set",
                "docs",
                "--fanout-tier",
                "enterprise",
            ],
            check=False,
            capture_output=True,
            text=True,
            input="",
        )

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("missing selection metadata on stdin", proc.stderr)

    def recommend_lab_for_files(self, files: list[str]) -> dict:
        return run_script(
            SCRIPTS_DIR / "resolve_validation_plan.py",
            "recommend-lab",
            "--changed-files-json",
            json.dumps(files),
            "--catalog-path",
            str(REPO_ROOT / ".github/validation-lanes.json"),
        )

    def test_recommend_lab_workflow_only_uses_workflow_route(self) -> None:
        payload = self.recommend_lab_for_files([".github/workflows/validation-lab.yml"])

        self.assertTrue(payload["advisory"])
        self.assertEqual(payload["profile"], "targeted")
        self.assertEqual(payload["lane_set"], "docs")
        self.assertEqual(payload["source"], "followup_route")
        self.assertEqual(
            payload["lane_ids"],
            [
                "codex.workflow-ci-sanity",
                "codex.downstream-docs-check",
            ],
        )
        self.assertEqual(
            payload["dispatch_inputs"]["lanes"],
            "codex.workflow-ci-sanity,codex.downstream-docs-check",
        )

    def test_recommend_lab_rust_core_path_keeps_core_lane_set(self) -> None:
        payload = self.recommend_lab_for_files(["codex-rs/core/src/lib.rs"])

        self.assertEqual(payload["profile"], "targeted")
        self.assertEqual(payload["lane_set"], "core-carry")
        self.assertEqual(payload["source"], "followup_route")
        self.assertEqual(
            payload["lane_ids"],
            [
                "codex.blocking-waits-core-targeted",
                "codex.blocking-waits-unified-exec-targeted",
                "codex.blocking-waits-app-server-targeted",
                "codex.blocking-waits-mcp-targeted",
            ],
        )

    def test_recommend_lab_ui_protocol_path_uses_exact_route(self) -> None:
        payload = self.recommend_lab_for_files(
            ["codex-rs/app-server-protocol/src/protocol/v2/thread.rs"]
        )

        self.assertEqual(payload["profile"], "targeted")
        self.assertEqual(payload["lane_set"], "ui-protocol")
        self.assertEqual(payload["source"], "followup_route")
        self.assertEqual(
            payload["lane_ids"],
            [
                "codex.app-server-protocol-test",
                "codex.app-server-thread-cwd-targeted",
                "codex.blocking-waits-app-server-targeted",
            ],
        )

    def test_recommend_lab_full_collab_spawn_identity_candidate_includes_explicit_lanes(
        self,
    ) -> None:
        payload = self.recommend_lab_for_files(self.collab_spawn_identity_candidate_paths)

        self.assertEqual(payload["profile"], "targeted")
        self.assertEqual(payload["lane_set"], "ui-protocol")
        self.assertEqual(payload["source"], "followup_route")
        self.assertEqual(
            payload["lane_ids"],
            [
                "codex.core-subagent-model-pinning-targeted",
                "codex.app-server-protocol-test",
                "codex.app-server-collab-spawn-identity-targeted",
                "codex.tui-collab-spawn-identity-targeted",
                "codex.collab-spawn-identity-consumers-targeted",
                "codex.sdk-python-targeted",
            ],
        )
        self.assertTrue(payload["include_explicit_lanes"])
        self.assertEqual(payload["dispatch_inputs"]["include_explicit_lanes"], "true")

    def test_recommend_lab_external_agent_containment_route_is_fail_closed(self) -> None:
        payload = self.recommend_lab_for_files(
            [
                "codex-rs/external-agent-migration/src/service.rs",
                "codex-rs/external-agent-migration/Cargo.toml",
                "codex-rs/Cargo.lock",
                "MODULE.bazel.lock",
                "codex-rs/app-server/tests/suite/v2/external_agent_config.rs",
                ".github/scripts/test_ci_planners.py",
                ".github/workflows/sedna-heavy-tests.yml",
                "docs/carry-divergence-ledger.md",
            ]
        )

        self.assertEqual(payload["profile"], "targeted")
        self.assertEqual(payload["source"], "followup_route")
        self.assertEqual(
            payload["lane_ids"],
            ["codex.external-agent-migration-containment-targeted"],
        )

        lane = next(
            lane
            for lane in RESOLVE_VALIDATION_PLAN.load_catalog()["lanes"]
            if lane["lane_id"]
            == "codex.external-agent-migration-containment-targeted"
        )
        self.assertTrue(lane["needs_nextest"])
        recipe = "\n".join(
            just_recipe_bodies(REPO_ROOT / "justfile")[
                "external-agent-migration-containment-targeted"
            ]
        )
        self.assertEqual(recipe.count("cargo nextest run --locked"), 2)
        self.assertEqual(recipe.count("--no-tests=fail"), 2)
        self.assertIn(
            "suite::v2::external_agent_config::"
            "external_agent_memory_import_rejects_stale_symlink_before_workspace_mutation",
            recipe,
        )
        self.assertIn("--exact", recipe)

    def test_recommend_lab_subagent_model_pinning_route_is_fail_closed(self) -> None:
        payload = self.recommend_lab_for_files(
            [
                "codex-rs/core/src/agent/control.rs",
                "codex-rs/core/src/agent/control/spawn.rs",
                "codex-rs/core/src/agent/control_tests.rs",
                "codex-rs/core/src/agent/builtins/terminal-babysitter.toml",
                "codex-rs/core/src/agent/role_tests.rs",
                "codex-rs/core/src/tools/handlers/multi_agents_common.rs",
                "codex-rs/core/src/tools/handlers/multi_agents_spec_tests.rs",
                "codex-rs/core/src/tools/handlers/multi_agents_tests.rs",
                "codex-rs/core/src/tools/handlers/multi_agents_v2/spawn.rs",
                "codex-rs/core/tests/suite/subagent_notifications.rs",
                "codex-rs/core/tests/suite/multi_agent_resume.rs",
                "codex-rs/state/src/extract.rs",
                "codex-rs/thread-store/src/local/read_thread.rs",
                "codex-rs/thread-store/src/thread_metadata_sync.rs",
                "codex-rs/thread-store/src/types.rs",
                ".github/scripts/test_ci_planners.py",
                ".github/validation-lanes.json",
                "docs/carry-divergence-ledger.md",
                "docs/divergences/index.yaml",
                "docs/downstream-regression-matrix.md",
                "docs/downstream.md",
                "justfile",
            ]
        )

        self.assertEqual(payload["profile"], "targeted")
        self.assertEqual(payload["source"], "followup_route")
        self.assertEqual(
            payload["lane_ids"],
            ["codex.core-subagent-model-pinning-targeted"],
        )

        lane = next(
            lane
            for lane in RESOLVE_VALIDATION_PLAN.load_catalog()["lanes"]
            if lane["lane_id"] == "codex.core-subagent-model-pinning-targeted"
        )
        self.assertTrue(lane["needs_nextest"])
        recipe = "\n".join(
            just_recipe_bodies(REPO_ROOT / "justfile")[
                "core-subagent-model-pinning-targeted"
            ]
        )
        self.assertEqual(recipe.count("cargo nextest run"), 6)
        self.assertEqual(recipe.count("RUST_MIN_STACK="), 6)
        self.assertEqual(recipe.count("--no-tests=fail"), 6)
        self.assertIn(
            "tools::handlers::multi_agents_spec::tests::"
            "spawn_agent_tool_v2_requires_task_name_and_lists_visible_models",
            recipe,
        )
        self.assertIn(
            "agent::role::tests::apply_role_preserves_unspecified_keys",
            recipe,
        )
        self.assertIn(
            "agent::role::tests::"
            "spawn_tool_spec_marks_terminal_babysitter_locked_model_and_reasoning_effort",
            recipe,
        )
        self.assertIn(
            "tools::handlers::multi_agents::tests::"
            "spawn_agent_reasoning_effort_accepts_empty_support_metadata",
            recipe,
        )
        self.assertIn(
            "tools::handlers::multi_agents::tests::"
            "multi_agent_v2_spawn_accepts_child_model_without_backend_assignment",
            recipe,
        )
        self.assertIn(
            "tools::handlers::multi_agents::tests::"
            "multi_agent_v2_spawn_accepts_luna_compatibility_override",
            recipe,
        )
        self.assertIn(
            "tools::handlers::multi_agents::tests::"
            "multi_agent_v2_spawn_rejects_child_model_from_different_backend",
            recipe,
        )
        self.assertIn(
            "tools::handlers::multi_agents::tests::"
            "multi_agent_v2_spawn_fork_turns_all_rejects_agent_type_override",
            recipe,
        )
        self.assertIn(
            "tools::handlers::multi_agents::tests::"
            "multi_agent_v2_spawn_partial_fork_turns_allows_agent_type_override",
            recipe,
        )
        self.assertIn(
            "suite::subagent_notifications::"
            "spawn_agent_uses_configured_subagent_defaults",
            recipe,
        )
        self.assertIn(
            "suite::subagent_notifications::"
            "spawn_agent_preserves_configured_defaults_through_unrelated_role",
            recipe,
        )
        self.assertIn(
            "suite::subagent_notifications::"
            "spawn_agent_requested_model_and_reasoning_override_inherited_settings_without_role",
            recipe,
        )
        self.assertIn(
            "suite::subagent_notifications::"
            "spawn_agent_role_overrides_requested_model_and_reasoning_settings",
            recipe,
        )
        self.assertIn(
            "suite::subagent_notifications::"
            "spawn_agent_rejects_reasoning_effort_unsupported_by_role_model",
            recipe,
        )
        self.assertIn(
            "suite::subagent_notifications::"
            "spawned_full_history_v2_child_uses_model_precedence_without_dropping_context",
            recipe,
        )
        self.assertIn(
            "local::read_thread::tests::"
            "read_thread_keeps_complete_indexed_identity_during_rollout_overlay",
            recipe,
        )
        self.assertIn(
            "suite::multi_agent_resume::"
            "cold_root_resume_restores_agent_identity_and_reloads_target_on_followup",
            recipe,
        )
        self.assertIn(
            "suite::multi_agent_resume::"
            "cold_root_resume_restores_agent_identity_and_role_on_followup",
            recipe,
        )
        self.assertEqual(recipe.count("--exact"), 5)

    def test_collab_spawn_identity_recipes_pin_exact_cross_surface_checks(self) -> None:
        catalog = RESOLVE_VALIDATION_PLAN.load_catalog()
        expected_lanes = {
            "codex.app-server-collab-spawn-identity-targeted": (
                "app-server-collab-spawn-identity-targeted",
                "rust_integration",
                [
                    "suite::v2::turn_start::turn_start_emits_multi_agent_v1_spawn_requested_and_effective_identity_v2",
                    "suite::v2::turn_start::turn_start_emits_multi_agent_v1_role_spawn_requested_and_effective_identity_v2",
                ],
            ),
            "codex.tui-collab-spawn-identity-targeted": (
                "tui-collab-spawn-identity-targeted",
                "rust_minimal",
                [
                    "chatwidget::tests::history_replay::replayed_collab_spawn_terminal_uses_only_explicit_effective_identity",
                    "chatwidget::tests::history_replay::replayed_historic_terminal_collab_spawn_renders_legacy_identity_as_effective",
                    "chatwidget::tests::history_replay::replayed_failed_collab_spawn_without_receiver_keeps_requested_identity",
                    "chatwidget::tests::app_server::live_app_server_collab_spawn_completed_renders_requested_model_and_effort",
                    "chatwidget::tests::app_server::live_app_server_spawn_completion_does_not_fill_missing_effective_identity_from_metadata",
                ],
            ),
            "codex.collab-spawn-identity-consumers-targeted": (
                "collab-spawn-identity-consumers-targeted",
                "rust_minimal",
                [
                    "analytics_client_tests::collab_tool_item_analytics_keeps_requested_identity_from_the_started_event",
                    "runtime::usage::tests::usage_logger_preserves_optional_spawn_request_identity",
                ],
            ),
        }
        recipes = just_recipe_bodies(REPO_ROOT / "justfile")

        for lane_id, (recipe_name, setup_class, selectors) in expected_lanes.items():
            with self.subTest(lane_id=lane_id):
                lane = next(
                    lane for lane in catalog["lanes"] if lane["lane_id"] == lane_id
                )
                self.assertTrue(lane["explicit_only"])
                self.assertEqual(lane["setup_class"], setup_class)
                self.assertEqual(lane["script_args"], [recipe_name])

                exact_selectors = []
                for line in recipes[recipe_name]:
                    command, separator, _ = line.strip().partition(" -- --exact ")
                    if separator:
                        exact_selectors.append(
                            command.removesuffix(" --lib").rsplit(maxsplit=1)[-1]
                        )
                self.assertEqual(len(exact_selectors), len(selectors))
                self.assertCountEqual(exact_selectors, selectors)

    def test_recommend_lab_docs_path_uses_docs_domain_fallback(self) -> None:
        payload = self.recommend_lab_for_files(["docs/validation_workflow.md"])

        self.assertEqual(payload["profile"], "targeted")
        self.assertEqual(payload["lane_set"], "docs")
        self.assertEqual(payload["source"], "domain_rules")
        self.assertEqual(payload["lane_ids"], ["codex.downstream-docs-check"])

    def test_recommend_lab_release_path_uses_release_domain_fallback(self) -> None:
        payload = self.recommend_lab_for_files(
            [".github/workflows/sedna-branch-build.yml"]
        )

        self.assertEqual(payload["profile"], "targeted")
        self.assertEqual(payload["lane_set"], "release")
        self.assertEqual(payload["source"], "domain_rules")
        self.assertEqual(payload["lane_ids"], [])

    def test_recommend_lab_unknown_path_uses_frontier_fallback(self) -> None:
        payload = self.recommend_lab_for_files(["unknown/place/example.txt"])

        self.assertEqual(payload["profile"], "frontier")
        self.assertEqual(payload["lane_set"], "all")
        self.assertEqual(payload["source"], "conservative_fallback")
        self.assertEqual(payload["lane_ids"], [])
        self.assertEqual(payload["domains"], ["unknown"])

    def test_recommend_lab_missing_metadata_uses_frontier_fallback(self) -> None:
        payload = self.recommend_lab_for_files([])

        self.assertEqual(payload["profile"], "frontier")
        self.assertEqual(payload["lane_set"], "all")
        self.assertEqual(payload["source"], "conservative_fallback")
        self.assertIn("metadata was empty", payload["reason"])

    def test_recommend_lab_rejects_route_with_unknown_lane(self) -> None:
        catalog = json.loads((REPO_ROOT / ".github/validation-lanes.json").read_text())
        catalog["followup_routes"].append(
            {
                "route_id": "synthetic-missing-lane",
                "lane_ids": ["codex.synthetic-missing-lane"],
                "allowed_paths": ["synthetic/missing-lane.txt"],
            }
        )

        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json") as handle:
            json.dump(catalog, handle)
            handle.flush()

            proc = subprocess.run(
                [
                    "python3",
                    str(SCRIPTS_DIR / "resolve_validation_plan.py"),
                    "recommend-lab",
                    "--changed-files-json",
                    json.dumps(["synthetic/missing-lane.txt"]),
                    "--catalog-path",
                    handle.name,
                ],
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(
            "matched follow-up route contains unknown lane IDs: "
            "codex.synthetic-missing-lane",
            proc.stderr,
        )

    def test_lab_targeted_ui_protocol_lane_set_returns_selected_matrix(self) -> None:
        payload = run_script(
            SCRIPTS_DIR / "resolve_validation_plan.py",
            "lab",
            "--profile",
            "targeted",
            "--lane-set",
            "ui-protocol",
            "--catalog-path",
            str(REPO_ROOT / ".github/validation-lanes.json"),
        )

        self.assertEqual(payload["run_selected_lanes"], "true")
        self.assertEqual(payload["run_smoke_gate"], "false")
        self.assertEqual(len(payload["selected_matrix"]["include"]), 27)
        self.assertEqual(payload["planned_job_count"], 17)
        self.assertEqual(payload["rust_batching_mode"], "auto")
        self.assertEqual(payload["selected_workflow_lane_count"], 0)
        self.assertEqual(payload["selected_node_lane_count"], 0)
        self.assertEqual(payload["selected_rust_minimal_lane_count"], 0)
        self.assertEqual(payload["selected_rust_minimal_batch_count"], 9)
        self.assertEqual(payload["selected_rust_integration_lane_count"], 1)
        self.assertEqual(payload["selected_rust_integration_batch_count"], 7)
        self.assertEqual(payload["selected_release_lane_count"], 0)
        for batch in (
            payload["selected_rust_minimal_batch_matrix"]["include"]
            + payload["selected_rust_integration_batch_matrix"]["include"]
        ):
            self.assertLessEqual(batch["batch_lane_count"], 2)
            self.assertLessEqual(batch["estimated_weight_seconds"], 720)
        self.assertTrue(
            all(
                lane.get("checkout_fetch_depth") == 1
                for lane in payload["selected_matrix"]["include"]
            )
        )
        self.assertIn("codex.app-server-protocol-test", payload["selected_lane_ids"])
        blocking_wait_lanes = {
            "codex.blocking-waits-app-server-targeted",
            "codex.blocking-waits-core-targeted",
            "codex.blocking-waits-mcp-targeted",
            "codex.blocking-waits-unified-exec-targeted",
        }
        selected_lane_ids = set(payload["selected_lane_ids"])
        self.assertTrue(
            blocking_wait_lanes.issubset(selected_lane_ids),
            f"missing blocking wait lanes: {blocking_wait_lanes - selected_lane_ids}",
        )
        blocking_wait_batches = {}
        for batch in payload["selected_rust_integration_batch_matrix"]["include"]:
            lane_ids = batch["lane_ids"]
            if len(lane_ids) == 1 and lane_ids[0] in blocking_wait_lanes:
                blocking_wait_batches[lane_ids[0]] = batch
        self.assertEqual(set(blocking_wait_batches), blocking_wait_lanes)
        for batch in blocking_wait_batches.values():
            self.assertEqual(batch["batch_lane_count"], 1)
            self.assertEqual(batch["estimated_weight_seconds"], 720)

        native_registry_batches = {}
        for batch in payload["selected_rust_minimal_batch_matrix"]["include"]:
            lane_ids = batch["lane_ids"]
            if lane_ids == ["codex.native-computer-use-tool-registry-targeted"]:
                native_registry_batches[lane_ids[0]] = batch
        self.assertEqual(
            set(native_registry_batches),
            {"codex.native-computer-use-tool-registry-targeted"},
        )
        for batch in native_registry_batches.values():
            self.assertEqual(batch["batch_lane_count"], 1)
            self.assertEqual(batch["estimated_weight_seconds"], 720)

    def test_lab_targeted_ui_protocol_can_disable_rust_batching(self) -> None:
        payload = run_script(
            SCRIPTS_DIR / "resolve_validation_plan.py",
            "lab",
            "--profile",
            "targeted",
            "--lane-set",
            "ui-protocol",
            "--rust-batching",
            "off",
            "--catalog-path",
            str(REPO_ROOT / ".github/validation-lanes.json"),
        )

        self.assertEqual(payload["planned_job_count"], 27)
        self.assertEqual(payload["rust_batching_mode"], "off")
        self.assertEqual(payload["rust_batching_reason"], "disabled by workflow input")
        self.assertEqual(payload["selected_rust_minimal_lane_count"], 17)
        self.assertEqual(payload["selected_rust_minimal_batch_count"], 0)
        self.assertEqual(payload["selected_rust_integration_lane_count"], 10)
        self.assertEqual(payload["selected_rust_integration_batch_count"], 0)

    def test_lab_product_surface_lane_set_returns_first_wave_lanes(self) -> None:
        payload = run_script(
            SCRIPTS_DIR / "resolve_validation_plan.py",
            "lab",
            "--profile",
            "targeted",
            "--lane-set",
            "product-surfaces",
            "--catalog-path",
            str(REPO_ROOT / ".github/validation-lanes.json"),
        )

        self.assertEqual(payload["planned_job_count"], 5)
        self.assertEqual(payload["selected_workflow_lane_count"], 1)
        self.assertEqual(payload["selected_rust_minimal_lane_count"], 1)
        self.assertEqual(payload["selected_rust_integration_lane_count"], 3)
        self.assertEqual(
            payload["selected_lane_ids"],
            [
                "codex.app-server-v2-contract-targeted",
                "codex.mcp-server-contract-targeted",
                "codex.exec-server-targeted",
                "codex.cli-surface-targeted",
                "codex.workflow-security-targeted",
            ],
        )

    def test_lab_sdk_lane_set_returns_python_and_typescript_lanes(self) -> None:
        payload = run_script(
            SCRIPTS_DIR / "resolve_validation_plan.py",
            "lab",
            "--profile",
            "targeted",
            "--lane-set",
            "sdk",
            "--catalog-path",
            str(REPO_ROOT / ".github/validation-lanes.json"),
        )

        self.assertEqual(payload["planned_job_count"], 2)
        self.assertEqual(payload["selected_workflow_lane_count"], 1)
        self.assertEqual(payload["selected_node_lane_count"], 1)
        self.assertEqual(
            payload["selected_lane_ids"],
            [
                "codex.sdk-python-targeted",
                "codex.sdk-typescript-targeted",
            ],
        )

    def test_lab_smoke_profile_uses_wider_rust_integration_parallelism(self) -> None:
        payload = run_script(
            SCRIPTS_DIR / "resolve_validation_plan.py",
            "lab",
            "--profile",
            "smoke",
            "--lane-set",
            "all",
            "--catalog-path",
            str(REPO_ROOT / ".github/validation-lanes.json"),
        )

        self.assertEqual(payload["run_smoke_gate"], "true")
        self.assertEqual(payload["smoke_rust_integration_lane_count"], 5)
        self.assertEqual(payload["rust_integration_max_parallel"], "5")

    def test_lab_full_all_tolerates_null_groups_entries(self) -> None:
        catalog_path = REPO_ROOT / ".github/validation-lanes.json"
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))

        # Reproduce production failure mode where one lane has groups=null.
        catalog["lanes"][0]["groups"] = None

        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json") as handle:
            json.dump(catalog, handle)
            handle.flush()

            payload = run_script(
                SCRIPTS_DIR / "resolve_validation_plan.py",
                "lab",
                "--profile",
                "full",
                "--lane-set",
                "all",
                "--catalog-path",
                handle.name,
            )

        self.assertEqual(payload["run_selected_lanes"], "true")
        self.assertIn("planned_matrix", payload)
        self.assertIn("selected_matrix", payload)
        self.assertIn("selected_workflow_matrix", payload)
        self.assertIn("smoke_workflow_matrix", payload)

    def test_lab_rejects_matrix_plans_above_job_limit(self) -> None:
        def workflow_lane(index: int) -> dict:
            return {
                "lane_id": f"codex.synthetic-workflow-{index:03d}",
                "groups": ["workflow"],
                "lane_sets": ["all"],
                "status_class": "active",
                "setup_class": "workflow",
                "frontier_role": "depth",
                "summary_family": f"synthetic-workflow-{index:03d}",
                "cost_class": "low",
                "checkout_fetch_depth": 1,
                "timeout_minutes": 30,
                "working_directory": ".",
                "script_path": ".github/scripts/validation-lanes/workflow-ci-sanity.sh",
                "script_args": [],
                "needs_just": False,
                "needs_node": False,
                "needs_nextest": False,
                "needs_linux_build_deps": False,
                "needs_dotslash": False,
                "needs_sccache": False,
                "needs_bazel": False,
            }

        catalog = {"lanes": [workflow_lane(index) for index in range(257)]}
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json") as handle:
            json.dump(catalog, handle)
            handle.flush()

            proc = subprocess.run(
                [
                    "python3",
                    str(SCRIPTS_DIR / "resolve_validation_plan.py"),
                    "lab",
                    "--profile",
                    "frontier",
                    "--lane-set",
                    "all",
                    "--artifact-build",
                    "false",
                    "--catalog-path",
                    handle.name,
                ],
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("would create 257 matrix/artifact jobs", proc.stderr)
        self.assertIn("above the 256 job cap", proc.stderr)

    def test_lab_targeted_rejects_boolean_checkout_fetch_depth_metadata(self) -> None:
        catalog_path = REPO_ROOT / ".github/validation-lanes.json"
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        catalog["lanes"][0]["checkout_fetch_depth"] = False

        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json") as handle:
            json.dump(catalog, handle)
            handle.flush()

            proc = subprocess.run(
                [
                    "python3",
                    str(SCRIPTS_DIR / "resolve_validation_plan.py"),
                    "lab",
                    "--profile",
                    "targeted",
                    "--lane-set",
                    "all",
                    "--catalog-path",
                    handle.name,
                ],
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(
            "must set checkout_fetch_depth to a non-negative integer",
            proc.stderr,
        )

    def test_validation_catalog_rejects_absolute_and_traversal_paths(self) -> None:
        catalog = RESOLVE_VALIDATION_PLAN.normalize_catalog(RESOLVE_VALIDATION_PLAN.load_catalog())

        absolute_catalog = json.loads(json.dumps(catalog))
        absolute_catalog["lanes"][0]["working_directory"] = "/tmp"
        with self.assertRaisesRegex(
            SystemExit,
            "must be a relative path within the repository root",
        ):
            RESOLVE_VALIDATION_PLAN.validate_catalog(absolute_catalog, repo_root=REPO_ROOT)

        traversal_catalog = json.loads(json.dumps(catalog))
        traversal_catalog["lanes"][0]["script_path"] = "../escape.sh"
        with self.assertRaisesRegex(
            SystemExit,
            "must not contain '..' path segments",
        ):
            RESOLVE_VALIDATION_PLAN.validate_catalog(traversal_catalog, repo_root=REPO_ROOT)

    def test_validation_catalog_rejects_invalid_followup_route_priority(self) -> None:
        catalog = RESOLVE_VALIDATION_PLAN.normalize_catalog(RESOLVE_VALIDATION_PLAN.load_catalog())
        route = next(
            route
            for route in catalog["followup_routes"]
            if route["route_id"] == "collab-spawn-identity"
        )
        route["priority"] = False

        with self.assertRaisesRegex(
            SystemExit,
            "must set priority to a non-negative integer",
        ):
            RESOLVE_VALIDATION_PLAN.validate_catalog(catalog, repo_root=REPO_ROOT)

    def test_heavy_plan_splits_selected_lanes_by_setup_class(self) -> None:
        payload = run_script(
            SCRIPTS_DIR / "resolve_validation_plan.py",
            "heavy",
            "--event-name",
            "pull_request",
            "--requested-lane",
            "",
            "--run-all-lanes",
            "false",
            "--run-core-family",
            "true",
            "--run-attestation-family",
            "false",
            "--run-workflow-family",
            "false",
            "--run-ui-protocol-family",
            "true",
            "--run-docs-family",
            "true",
            "--changed-files-json",
            json.dumps(
                [
                    "codex-rs/core/src/tools/handlers/multi_agents_common.rs",
                    "codex-rs/tui/src/app.rs",
                    "docs/downstream.md",
                ]
            ),
        )

        self.assertEqual(payload["run_smoke_gate"], "true")
        self.assertEqual(payload["smoke_gate_kind"], "runtime")
        self.assertEqual(payload["selected_workflow_lane_count"], 1)
        self.assertEqual(payload["selected_node_lane_count"], 0)
        self.assertEqual(payload["selected_rust_minimal_lane_count"], 0)
        self.assertEqual(payload["selected_rust_minimal_batch_count"], 13)
        self.assertEqual(payload["selected_rust_integration_lane_count"], 2)
        self.assertEqual(payload["selected_rust_integration_batch_count"], 12)
        self.assertEqual(payload["selected_release_lane_count"], 0)
        self.assertEqual(payload["smoke_rust_integration_lane_count"], 5)
        self.assertEqual(payload["smoke_release_lane_count"], 1)
        for (
            setup_class,
            expected_limit,
        ) in RESOLVE_VALIDATION_PLAN.DEFAULT_TARGETED_PARALLEL_LIMITS.items():
            self.assertEqual(
                payload[f"{setup_class}_max_parallel"],
                str(expected_limit),
            )
        self.assertEqual(payload["rust_batching_mode"], "auto")
        self.assertIn(
            "codex.core-multi-agent-orchestration-targeted",
            payload["selected_lane_ids"],
        )

    def test_heavy_plan_can_disable_rust_batching(self) -> None:
        payload = run_script(
            SCRIPTS_DIR / "resolve_validation_plan.py",
            "heavy",
            "--event-name",
            "workflow_dispatch",
            "--requested-lane",
            "all",
            "--run-all-lanes",
            "true",
            "--run-core-family",
            "false",
            "--run-attestation-family",
            "false",
            "--run-workflow-family",
            "false",
            "--run-ui-protocol-family",
            "false",
            "--run-docs-family",
            "false",
            "--rust-batching",
            "off",
        )

        self.assertEqual(payload["selected_rust_minimal_batch_count"], 0)
        self.assertEqual(payload["selected_rust_integration_batch_count"], 0)
        self.assertGreater(payload["selected_rust_minimal_lane_count"], 0)
        self.assertGreater(payload["selected_rust_integration_lane_count"], 0)

    def test_heavy_plan_route_uses_bounded_shared_spawn_surface(self) -> None:
        payload = run_script(
            SCRIPTS_DIR / "resolve_validation_plan.py",
            "heavy",
            "--event-name",
            "pull_request",
            "--requested-lane",
            "",
            "--run-all-lanes",
            "false",
            "--run-core-family",
            "false",
            "--run-attestation-family",
            "false",
            "--run-workflow-family",
            "false",
            "--run-ui-protocol-family",
            "false",
            "--run-docs-family",
            "false",
            "--changed-files-json",
            json.dumps(
                [
                    "codex-rs/tools/src/agent_tool.rs",
                    "codex-rs/tools/src/agent_tool_tests.rs",
                ]
            ),
        )

        self.assertEqual(
            [lane["lane_id"] for lane in payload["selected_matrix"]["include"]],
            [
                "codex.spawn-agent-tool-model-surface-targeted",
            ],
        )

    def test_heavy_plan_exact_workflow_dispatch_lane_skips_smoke_gate(self) -> None:
        payload = run_script(
            SCRIPTS_DIR / "resolve_validation_plan.py",
            "heavy",
            "--event-name",
            "workflow_dispatch",
            "--requested-lane",
            "codex.tui-agent-picker-model-surface-targeted",
            "--run-all-lanes",
            "true",
            "--run-core-family",
            "false",
            "--run-attestation-family",
            "false",
            "--run-workflow-family",
            "false",
            "--run-ui-protocol-family",
            "false",
            "--run-docs-family",
            "false",
            "--changed-files-json",
            "[]",
        )

        self.assertEqual(payload["run_smoke_gate"], "false")
        self.assertEqual(payload["smoke_gate_kind"], "")
        self.assertEqual(payload["smoke_rust_integration_lane_count"], 0)
        self.assertEqual(payload["selected_workflow_lane_count"], 0)
        self.assertEqual(payload["selected_node_lane_count"], 0)
        self.assertEqual(payload["selected_rust_minimal_lane_count"], 1)
        self.assertEqual(payload["selected_rust_integration_lane_count"], 0)
        self.assertEqual(
            [lane["lane_id"] for lane in payload["selected_matrix"]["include"]],
            ["codex.tui-agent-picker-model-surface-targeted"],
        )

    def test_heavy_plan_route_keeps_workflow_ci_changes_on_workflow_lanes(self) -> None:
        payload = run_script(
            SCRIPTS_DIR / "resolve_validation_plan.py",
            "heavy",
            "--event-name",
            "pull_request",
            "--requested-lane",
            "",
            "--run-all-lanes",
            "false",
            "--run-core-family",
            "false",
            "--run-attestation-family",
            "false",
            "--run-workflow-family",
            "true",
            "--run-ui-protocol-family",
            "false",
            "--run-docs-family",
            "true",
            "--changed-files-json",
            json.dumps(
                [
                    ".github/workflows/validation-lab.yml",
                    ".github/scripts/resolve_validation_plan.py",
                    "docs/validation_workflow.md",
                    "justfile",
                ]
            ),
        )

        self.assertEqual(payload["run_smoke_gate"], "false")
        self.assertEqual(payload["selected_workflow_lane_count"], 2)
        self.assertEqual(payload["selected_node_lane_count"], 0)
        self.assertEqual(payload["selected_rust_minimal_lane_count"], 0)
        self.assertEqual(payload["selected_rust_integration_lane_count"], 0)
        self.assertEqual(payload["selected_release_lane_count"], 0)
        self.assertEqual(
            [lane["lane_id"] for lane in payload["selected_matrix"]["include"]],
            [
                "codex.workflow-ci-sanity",
                "codex.downstream-docs-check",
            ],
        )

    def test_validation_lab_selected_lanes_do_not_block_on_smoke_gate(self) -> None:
        payload = load_workflow_payload(REPO_ROOT / ".github/workflows/validation-lab.yml")
        jobs = payload.get("jobs") or {}

        self.assertEqual((jobs.get("workflow_lanes") or {}).get("needs"), ["metadata"])
        self.assertEqual((jobs.get("node_lanes") or {}).get("needs"), ["metadata"])
        self.assertEqual(
            (jobs.get("rust_minimal_lanes") or {}).get("needs"), ["metadata"]
        )
        self.assertEqual(
            (jobs.get("rust_minimal_batches") or {}).get("needs"), ["metadata"]
        )
        self.assertEqual((jobs.get("nextest_archives") or {}).get("needs"), ["metadata"])
        self.assertEqual(
            (jobs.get("rust_integration_archive_lanes") or {}).get("needs"),
            ["metadata", "nextest_archives"],
        )
        self.assertEqual(
            (jobs.get("rust_integration_lanes") or {}).get("needs"), ["metadata"]
        )
        self.assertEqual(
            (jobs.get("rust_integration_batches") or {}).get("needs"), ["metadata"]
        )
        self.assertEqual((jobs.get("release_lanes") or {}).get("needs"), ["metadata"])

    def test_validation_lab_summary_waits_for_smoke_and_selected_fanout(self) -> None:
        payload = load_workflow_payload(REPO_ROOT / ".github/workflows/validation-lab.yml")
        jobs = payload.get("jobs") or {}
        summary = jobs.get("summary") or {}

        self.assertEqual(
            summary.get("needs"),
            [
                "metadata",
                "smoke_workflow_lanes",
                "smoke_node_lanes",
                "smoke_rust_minimal_lanes",
                "smoke_rust_integration_lanes",
                "smoke_release_lanes",
                "workflow_lanes",
                "node_lanes",
                "rust_minimal_lanes",
                "rust_minimal_batches",
                "nextest_archives",
                "rust_integration_archive_lanes",
                "rust_integration_lanes",
                "rust_integration_batches",
                "release_lanes",
                "artifact",
            ],
        )

    def test_validation_lab_summary_records_cache_occupancy(self) -> None:
        payload = load_workflow_payload(REPO_ROOT / ".github/workflows/validation-lab.yml")
        summary = ((payload.get("jobs") or {}).get("summary") or {})
        steps = summary.get("steps") or []

        self.assertEqual((summary.get("permissions") or {}).get("actions"), "read")
        record_step = next(
            (
                step
                for step in steps
                if step.get("name") == "Record Actions cache occupancy"
            ),
            {},
        )
        report_step = next(
            (
                step
                for step in steps
                if "--cache-occupancy-json" in (step.get("run") or "")
            ),
            {},
        )
        self.assertIn(
            "report_actions_cache_occupancy.py",
            record_step.get("run") or "",
        )
        self.assertIn(
            "--cache-occupancy-json",
            report_step.get("run") or "",
        )
        self.assertIn(
            '--rust-batching-mode "${{ needs.metadata.outputs.rust_batching_mode }}"',
            report_step.get("run") or "",
        )
        self.assertIn(
            '--rust-batching-reason "${{ needs.metadata.outputs.rust_batching_reason }}"',
            report_step.get("run") or "",
        )

    def test_validation_lab_only_fetches_target_history_for_artifact_versioning(self) -> None:
        payload = load_workflow_payload(REPO_ROOT / ".github/workflows/validation-lab.yml")
        metadata_steps = (((payload.get("jobs") or {}).get("metadata") or {}).get("steps") or [])
        target_checkout = next(
            step for step in metadata_steps if step.get("name") == "Check out validation target"
        )

        self.assertEqual(
            (target_checkout.get("with") or {}).get("fetch-depth"),
            "${{ (inputs.profile == 'artifact' || inputs.artifact_build) && '0' || '1' }}",
        )

        compute_plan = next(
            step for step in metadata_steps if step.get("name") == "Compute validation-lab plan"
        )
        run_script = compute_plan.get("run") or ""
        self.assertIn('if [[ "${LAB_PROFILE}" == "artifact"', run_script)
        self.assertIn("git -C \"${target_checkout}\" tag --merged HEAD", run_script)

    def test_sedna_branch_build_uses_safe_ref_env_and_cached_macos_x64_preview(
        self,
    ) -> None:
        payload = load_workflow_payload(REPO_ROOT / ".github/workflows/sedna-branch-build.yml")
        workflow_dispatch_inputs = (
            (payload.get("on") or {}).get("workflow_dispatch") or {}
        ).get("inputs") or {}
        self.assertEqual(
            (workflow_dispatch_inputs.get("platform") or {}).get("options"),
            ["linux-x86_64", "linux-aarch64", "macos"],
        )
        self.assertEqual(
            (workflow_dispatch_inputs.get("platform") or {}).get("default"),
            "linux-x86_64",
        )

        metadata_step = workflow_step_by_name(
            REPO_ROOT / ".github/workflows/sedna-branch-build.yml",
            "metadata",
            "Compute preview version",
        )
        env = metadata_step.get("env") or {}
        self.assertEqual(
            env.get("CHECKOUT_REF"),
            "${{ github.event_name == 'workflow_dispatch' && inputs.ref || github.sha }}",
        )
        self.assertEqual(
            env.get("DISPLAY_REF"),
            "${{ github.event_name == 'workflow_dispatch' && inputs.ref || github.ref_name }}",
        )
        run_script = metadata_step.get("run") or ""
        self.assertIn('checkout_ref="${CHECKOUT_REF}"', run_script)
        self.assertIn('branch_name="${DISPLAY_REF}"', run_script)
        self.assertNotIn("checkout_ref='${{", run_script)

        build_job = (payload.get("jobs") or {}).get("build-linux-x86_64") or {}
        self.assertEqual(
            (build_job.get("with") or {}).get("display_ref"),
            "${{ needs.metadata.outputs.display_ref }}",
        )
        run_command = (build_job.get("with") or {}).get("run_command") or ""
        self.assertIn('os.environ["DISPLAY_REF"]', run_command)
        self.assertIn("json.dump(payload, sys.stdout, indent=2)", run_command)
        self.assertNotIn("${{ needs.metadata.outputs.display_ref }}", run_command)

        arm_job = (payload.get("jobs") or {}).get("build-linux-aarch64") or {}
        self.assertEqual(arm_job.get("if"), "${{ inputs.platform == 'linux-aarch64' }}")
        self.assertEqual((arm_job.get("with") or {}).get("runner"), "ubuntu-24.04-arm")
        self.assertEqual(
            (arm_job.get("with") or {}).get("target"),
            "aarch64-unknown-linux-gnu",
        )
        self.assertIn(
            "-aarch64-unknown-linux-gnu",
            (arm_job.get("with") or {}).get("artifact_name") or "",
        )
        self.assertIn(
            "--target aarch64-unknown-linux-gnu",
            (arm_job.get("with") or {}).get("run_command") or "",
        )

        macos_job = (payload.get("jobs") or {}).get("build-macos") or {}
        self.assertEqual(macos_job.get("if"), "${{ inputs.platform == 'macos' }}")
        self.assertEqual(macos_job.get("runs-on"), "macos-15-intel")
        self.assertEqual(macos_job.get("timeout-minutes"), "210")
        self.assertNotIn("strategy", macos_job)
        self.assertEqual((macos_job.get("env") or {}).get("TARGET"), "x86_64-apple-darwin")
        self.assertNotIn("CARGO_PROFILE_RELEASE_LTO", macos_job.get("env") or {})
        self.assertNotIn("CARGO_PROFILE_RELEASE_CODEGEN_UNITS", macos_job.get("env") or {})
        install_sccache_step = workflow_step_by_name(
            REPO_ROOT / ".github/workflows/sedna-branch-build.yml",
            "build-macos",
            "Install sccache",
        )
        self.assertEqual(
            install_sccache_step.get("uses"),
            "taiki-e/install-action@5bf6ce016fd2e72eefc647cbca1e4213f65955b8",
        )
        configure_sccache_step = workflow_step_by_name(
            REPO_ROOT / ".github/workflows/sedna-branch-build.yml",
            "build-macos",
            "Configure sccache backend",
        )
        self.assertIn(
            "configure_sccache_backend.sh write-fallback",
            configure_sccache_step.get("run") or "",
        )
        restore_sccache_step = workflow_step_by_name(
            REPO_ROOT / ".github/workflows/sedna-branch-build.yml",
            "build-macos",
            "Restore sccache cache",
        )
        self.assertIn(
            "sedna-preview-sccache-macos-15-intel-x86_64-apple-darwin-",
            (restore_sccache_step.get("with") or {}).get("key") or "",
        )
        save_sccache_step = workflow_step_by_name(
            REPO_ROOT / ".github/workflows/sedna-branch-build.yml",
            "build-macos",
            "Save sccache cache",
        )
        self.assertEqual(
            save_sccache_step.get("uses"),
            "actions/cache/save@55cc8345863c7cc4c66a329aec7e433d2d1c52a9",
        )
        self.assertIn(
            "steps.build_macos.outcome == 'success'",
            save_sccache_step.get("if") or "",
        )
        macos_build_step = workflow_step_by_name(
            REPO_ROOT / ".github/workflows/sedna-branch-build.yml",
            "build-macos",
            "Build macOS preview binaries",
        )
        macos_build_script = macos_build_step.get("run") or ""
        self.assertIn("--locked", macos_build_script)
        self.assertIn("--bin codex-code-mode-host", macos_build_script)

        macos_stage_step = workflow_step_by_name(
            REPO_ROOT / ".github/workflows/sedna-branch-build.yml",
            "build-macos",
            "Stage ad hoc signed macOS preview artifact",
        )
        macos_stage_script = macos_stage_step.get("run") or ""
        self.assertIn('"signing": "ad-hoc"', macos_stage_script)
        self.assertIn('"notarized": False', macos_stage_script)
        self.assertNotIn("${{ needs.metadata.outputs.display_ref }}", macos_stage_script)

    def test_sedna_preview_artifact_builds_require_clean_git_provenance(
        self,
    ) -> None:
        payload = load_workflow_payload(
            REPO_ROOT / ".github/workflows/sedna-branch-build.yml"
        )
        jobs = payload.get("jobs") or {}
        clean_check = 'source_status="$(git status --porcelain --untracked-files=normal)"'
        version_check = 'version_output="$("${stage_dir}/codex" --version)"'

        reusable_workflows = (
            ("_sedna-linux-rust.yml", "run", "Run requested command"),
            ("_validation-lane-release.yml", "run", "Run requested lane script"),
        )
        isolation_helper = (
            REPO_ROOT
            / ".github/scripts/isolate_workflow_paths_from_provenance.sh"
        ).read_text(encoding="utf-8")
        self.assertIn('echo "/.workflow-src/"', isolation_helper)
        self.assertIn('echo "/.sccache/"', isolation_helper)
        self.assertIn("status --porcelain --untracked-files=normal", isolation_helper)

        for workflow_name, job_name, run_step_name in reusable_workflows:
            with self.subTest(workflow=workflow_name):
                reusable_payload = load_workflow_payload(
                    REPO_ROOT / ".github/workflows" / workflow_name
                )
                reusable_steps = (
                    ((reusable_payload.get("jobs") or {}).get(job_name) or {}).get(
                        "steps"
                    )
                    or []
                )
                reusable_names = [step.get("name") for step in reusable_steps]
                isolation_step = next(
                    step
                    for step in reusable_steps
                    if step.get("name")
                    == "Isolate workflow-owned paths from source provenance"
                )
                self.assertIn(
                    "isolate_workflow_paths_from_provenance.sh",
                    isolation_step.get("run") or "",
                )
                self.assertLess(
                    reusable_names.index(
                        "Isolate workflow-owned paths from source provenance"
                    ),
                    reusable_names.index(run_step_name),
                )

        for job_name in ("build-linux-x86_64", "build-linux-aarch64"):
            with self.subTest(job=job_name):
                run_command = ((jobs.get(job_name) or {}).get("with") or {}).get(
                    "run_command"
                ) or ""
                cargo_index = run_command.index("cargo build --locked")
                self.assertIn(clean_check, run_command)
                self.assertLess(run_command.index(clean_check), cargo_index)
                self.assertLess(cargo_index, run_command.index("mkdir -p ../dist"))
                self.assertIn(version_check, run_command)
                self.assertIn('[[ "${version_output}" == *-dirty* ]]', run_command)

        artifact_script = (
            REPO_ROOT / ".github/scripts/validation-lanes/artifact-build.sh"
        ).read_text(encoding="utf-8")
        cargo_index = artifact_script.index("cargo build --locked")
        self.assertIn(clean_check, artifact_script)
        self.assertLess(artifact_script.index(clean_check), cargo_index)
        self.assertLess(cargo_index, artifact_script.index("mkdir -p ../dist"))
        self.assertIn(version_check, artifact_script)
        self.assertIn('[[ "${version_output}" == *-dirty* ]]', artifact_script)

    def test_sedna_preview_and_release_caches_have_separate_non_executable_authority(
        self,
    ) -> None:
        preview_payload = load_workflow_payload(
            REPO_ROOT / ".github/workflows/_sedna-linux-rust.yml"
        )
        preview_steps = ((preview_payload.get("jobs") or {}).get("run") or {}).get(
            "steps"
        ) or []
        preview_named = {
            step.get("name"): step for step in preview_steps if step.get("name")
        }
        release_payload = load_workflow_payload(
            REPO_ROOT / ".github/workflows/sedna-release.yml"
        )
        release_steps = (
            ((release_payload.get("jobs") or {}).get("release-linux") or {}).get(
                "steps"
            )
            or []
        )
        release_named = {
            step.get("name"): step for step in release_steps if step.get("name")
        }

        for name in ("Restore cargo home cache", "Save cargo home cache"):
            preview_cache = preview_named[name].get("with") or {}
            release_cache = release_named[name].get("with") or {}
            self.assertNotIn("~/.cargo/bin/", preview_cache.get("path") or "")
            self.assertNotIn("~/.cargo/bin/", release_cache.get("path") or "")
            self.assertNotIn("sedna-release-", json.dumps(preview_cache))
            self.assertNotIn("sedna-preview-", json.dumps(release_cache))
            self.assertTrue(
                (preview_cache.get("key") or "").startswith(
                    "sedna-preview-cargo-home-linux-rust-"
                )
            )
            self.assertTrue(
                (release_cache.get("key") or "").startswith(
                    "sedna-release-cargo-home-linux-rust-"
                )
            )

        for name in ("Restore sccache cache (fallback)", "Save sccache cache (fallback)"):
            preview_cache = preview_named[name].get("with") or {}
            release_cache = release_named[name].get("with") or {}
            self.assertNotIn("sedna-release-", json.dumps(preview_cache))
            self.assertNotIn("sedna-preview-", json.dumps(release_cache))
            self.assertTrue(
                (preview_cache.get("key") or "").startswith(
                    "sedna-preview-sccache-linux-rust-"
                )
            )
            self.assertTrue(
                (release_cache.get("key") or "").startswith(
                    "sedna-release-sccache-linux-rust-"
                )
            )

        branch_payload = load_workflow_payload(
            REPO_ROOT / ".github/workflows/sedna-branch-build.yml"
        )
        branch_macos_steps = (
            ((branch_payload.get("jobs") or {}).get("build-macos") or {}).get("steps")
            or []
        )
        branch_macos_named = {
            step.get("name"): step for step in branch_macos_steps if step.get("name")
        }
        release_macos_steps = (
            ((release_payload.get("jobs") or {}).get("release-macos-build") or {}).get(
                "steps"
            )
            or []
        )
        release_macos_named = {
            step.get("name"): step for step in release_macos_steps if step.get("name")
        }
        for name in ("Restore Cargo home cache", "Save Cargo home cache"):
            preview_cache = branch_macos_named[name].get("with") or {}
            release_cache = release_macos_named[name].get("with") or {}
            self.assertNotIn("~/.cargo/bin/", preview_cache.get("path") or "")
            self.assertNotIn("~/.cargo/bin/", release_cache.get("path") or "")
            self.assertNotIn("sedna-release-", json.dumps(preview_cache))
            self.assertNotIn("sedna-preview-", json.dumps(release_cache))
            self.assertTrue(
                (preview_cache.get("key") or "").startswith(
                    "sedna-preview-cargo-home-macos-"
                )
            )
            self.assertTrue(
                (release_cache.get("key") or "").startswith(
                    "sedna-release-cargo-home-macos-"
                )
            )
        for name in ("Restore sccache cache", "Save sccache cache"):
            preview_cache = branch_macos_named[name].get("with") or {}
            release_cache = release_macos_named[name].get("with") or {}
            self.assertNotIn("sedna-release-", json.dumps(preview_cache))
            self.assertNotIn("sedna-preview-", json.dumps(release_cache))
            self.assertNotIn(
                "sccache-macos-15-intel-x86_64-apple-darwin-release-",
                json.dumps(preview_cache),
            )
            self.assertNotIn(
                "sccache-macos-15-intel-x86_64-apple-darwin-release-",
                json.dumps(release_cache),
            )
            self.assertTrue(
                (preview_cache.get("key") or "").startswith(
                    "sedna-preview-sccache-macos-"
                )
            )
            self.assertTrue(
                (release_cache.get("key") or "").startswith(
                    "sedna-release-sccache-macos-"
                )
            )

    def test_validation_lab_uses_safe_ref_env_for_checkout_and_display_refs(self) -> None:
        metadata_step = workflow_step_by_name(
            REPO_ROOT / ".github/workflows/validation-lab.yml",
            "metadata",
            "Compute validation-lab plan",
        )
        env = metadata_step.get("env") or {}
        self.assertEqual(env.get("LAB_HOST_REF"), "${{ github.ref_name }}")
        self.assertEqual(env.get("LAB_CHECKOUT_REF"), "${{ inputs.ref || github.sha }}")
        self.assertEqual(env.get("LAB_DISPLAY_REF"), "${{ inputs.ref || github.ref_name }}")
        run_script = metadata_step.get("run") or ""
        self.assertIn('host_ref="${LAB_HOST_REF}"', run_script)
        self.assertIn('checkout_ref="${LAB_CHECKOUT_REF}"', run_script)
        self.assertIn('display_ref="${LAB_DISPLAY_REF}"', run_script)
        self.assertNotIn("checkout_ref='${{", run_script)
        self.assertNotIn("display_ref='${{", run_script)

    def test_validation_lab_exposes_fanout_and_batching_controls(self) -> None:
        payload = load_workflow_payload(REPO_ROOT / ".github/workflows/validation-lab.yml")
        workflow_dispatch_inputs = (
            (((payload.get("on") or {}).get("workflow_dispatch") or {}).get("inputs") or {})
        )
        workflow_call_inputs = (
            (((payload.get("on") or {}).get("workflow_call") or {}).get("inputs") or {})
        )
        lane_set_options = (workflow_dispatch_inputs.get("lane_set") or {}).get("options") or []

        self.assertIn("product-surfaces", lane_set_options)
        self.assertIn("sdk", lane_set_options)
        self.assertEqual(
            (workflow_dispatch_inputs.get("fanout_tier") or {}).get("options"),
            ["balanced", "enterprise", "soak"],
        )
        self.assertEqual(
            (workflow_dispatch_inputs.get("rust_batching") or {}).get("options"),
            ["auto", "off", "force"],
        )
        self.assertEqual(
            (workflow_call_inputs.get("fanout_tier") or {}).get("default"),
            "enterprise",
        )
        self.assertEqual(
            (workflow_call_inputs.get("rust_batching") or {}).get("default"), "auto"
        )
        for workflow_name in ("validation-lab.yml", "sedna-heavy-tests.yml"):
            workflow_text = (
                REPO_ROOT / ".github/workflows" / workflow_name
            ).read_text()
            self.assertIn('          - "off"\n', workflow_text)
            self.assertNotIn("          - off\n", workflow_text)

        metadata_job = ((payload.get("jobs") or {}).get("metadata") or {})
        self.assertEqual(
            (metadata_job.get("outputs") or {}).get("fanout_tier"),
            "${{ steps.meta.outputs.fanout_tier }}",
        )
        self.assertEqual(
            (metadata_job.get("outputs") or {}).get("planned_job_count"),
            "${{ steps.meta.outputs.planned_job_count }}",
        )
        metadata_step = workflow_step_by_name(
            REPO_ROOT / ".github/workflows/validation-lab.yml",
            "metadata",
            "Compute validation-lab plan",
        )
        env = metadata_step.get("env") or {}
        self.assertEqual(
            env.get("LAB_FANOUT_TIER"),
            "${{ inputs.fanout_tier || 'enterprise' }}",
        )
        self.assertEqual(env.get("LAB_RUST_BATCHING"), "${{ inputs.rust_batching || 'auto' }}")
        self.assertEqual(
            env.get("LAB_RUST_BATCHING_OVERRIDE"),
            "${{ vars.VALIDATION_LAB_RUST_BATCHING }}",
        )
        run_script = metadata_step.get("run") or ""
        self.assertIn('--fanout-tier "${LAB_FANOUT_TIER}"', run_script)
        self.assertIn('--rust-batching "${LAB_RUST_BATCHING}"', run_script)
        self.assertIn('--rust-batching-override "${LAB_RUST_BATCHING_OVERRIDE}"', run_script)

    def test_validation_lab_exposes_exact_plan_dedupe_metadata(self) -> None:
        payload = load_workflow_payload(REPO_ROOT / ".github/workflows/validation-lab.yml")
        jobs = payload.get("jobs") or {}
        metadata_job = jobs.get("metadata") or {}
        outputs = metadata_job.get("outputs") or {}
        steps = metadata_job.get("steps") or []

        self.assertEqual((metadata_job.get("permissions") or {}).get("actions"), "read")
        self.assertEqual(
            outputs.get("planner_fingerprint"),
            "${{ steps.meta.outputs.planner_fingerprint }}",
        )
        self.assertEqual(
            outputs.get("dedupe_should_skip"),
            "${{ steps.dedupe.outputs.should_skip || 'false' }}",
        )
        compute_step = next(
            step for step in steps if step.get("name") == "Compute validation-lab plan"
        )
        compute_env = compute_step.get("env") or {}
        compute_run = compute_step.get("run") or ""
        self.assertEqual(compute_env.get("LAB_WORKFLOW_REF"), "${{ github.workflow_ref }}")
        self.assertEqual(compute_env.get("LAB_WORKFLOW_SHA"), "${{ github.sha }}")
        self.assertIn("validation_plan_fingerprint.py", compute_run)
        self.assertIn("--selection-meta-stdin", compute_run)
        self.assertIn('< "${selection_meta_path}"', compute_run)
        self.assertNotIn("--selection-meta-path", compute_run)
        self.assertIn("planner_fingerprint=${planner_fingerprint}", compute_run)

        dedupe_step = next(
            step for step in steps if step.get("name") == "Check exact-plan evidence reuse"
        )
        dedupe_run = dedupe_step.get("run") or ""
        self.assertIn("skip_duplicate_workflow_run.py", dedupe_run)
        self.assertIn("--summary-artifact-name validation-summary", dedupe_run)
        self.assertIn(
            '--required-planner-fingerprint "${LAB_PLANNER_FINGERPRINT}"',
            dedupe_run,
        )
        self.assertIn('if [[ "${LAB_SUPERSESSION_MODE}" != "auto"', dedupe_run)
        self.assertIn("exact_plan_success_available_retained_by_", dedupe_run)

    def test_validation_lab_exact_plan_skip_gates_fanout_jobs_only(self) -> None:
        payload = load_workflow_payload(REPO_ROOT / ".github/workflows/validation-lab.yml")
        jobs = payload.get("jobs") or {}
        fanout_jobs = [
            "smoke_workflow_lanes",
            "smoke_node_lanes",
            "smoke_rust_minimal_lanes",
            "smoke_rust_integration_lanes",
            "smoke_release_lanes",
            "workflow_lanes",
            "node_lanes",
            "rust_minimal_lanes",
            "rust_minimal_batches",
            "nextest_archives",
            "rust_integration_archive_lanes",
            "rust_integration_lanes",
            "rust_integration_batches",
            "release_lanes",
            "artifact",
        ]

        for job_name in fanout_jobs:
            with self.subTest(job=job_name):
                self.assertIn(
                    "needs.metadata.outputs.dedupe_should_skip != 'true'",
                    (jobs.get(job_name) or {}).get("if") or "",
                )
        self.assertNotIn(
            "dedupe_should_skip != 'true'",
            (jobs.get("summary") or {}).get("if") or "",
        )

    def test_validation_lab_nextest_archive_jobs_build_and_download_artifacts(self) -> None:
        payload = load_workflow_payload(REPO_ROOT / ".github/workflows/validation-lab.yml")
        jobs = payload.get("jobs") or {}
        metadata_outputs = (jobs.get("metadata") or {}).get("outputs") or {}

        self.assertEqual(
            metadata_outputs.get("selected_nextest_archive_matrix"),
            "${{ steps.meta.outputs.selected_nextest_archive_matrix }}",
        )
        self.assertEqual(
            metadata_outputs.get("selected_rust_integration_archive_matrix"),
            "${{ steps.meta.outputs.selected_rust_integration_archive_matrix }}",
        )

        archive_job = jobs.get("nextest_archives") or {}
        self.assertEqual(archive_job.get("needs"), ["metadata"])
        self.assertIn(
            "needs.metadata.outputs.selected_nextest_archive_count",
            archive_job.get("if") or "",
        )
        archive_steps = archive_job.get("steps") or []
        build_step = next(
            step for step in archive_steps if step.get("name") == "Build nextest archive"
        )
        self.assertIn("run_validation_lane.py", build_step.get("run") or "")
        self.assertEqual(
            (build_step.get("env") or {}).get("VALIDATION_LAB_NEXTEST_ARCHIVE_FILE"),
            "${{ runner.temp }}/validation-lab-nextest-archives/${{ matrix.archive_file_name }}",
        )
        upload_step = next(
            step for step in archive_steps if step.get("name") == "Upload nextest archive"
        )
        self.assertEqual(upload_step.get("uses"), "actions/upload-artifact@v7")
        self.assertEqual((upload_step.get("with") or {}).get("name"), "${{ matrix.artifact_name }}")

        archive_lanes = jobs.get("rust_integration_archive_lanes") or {}
        self.assertEqual(archive_lanes.get("needs"), ["metadata", "nextest_archives"])
        self.assertIn("needs.nextest_archives.result == 'success'", archive_lanes.get("if") or "")
        self.assertEqual(
            ((archive_lanes.get("with") or {}).get("nextest_archive_artifact_name")),
            "${{ matrix.nextest_archive_artifact_name }}",
        )
        self.assertEqual(
            ((archive_lanes.get("with") or {}).get("nextest_archive_file_name")),
            "${{ matrix.nextest_archive_file_name }}",
        )
        self.assertNotIn(
            "nextest_archive_artifact_name",
            (jobs.get("rust_integration_lanes") or {}).get("with") or {},
        )

    def test_validation_lab_rust_integration_workflow_downloads_nextest_archive(self) -> None:
        payload = load_workflow_payload(
            REPO_ROOT / ".github/workflows/_validation-lane-rust-integration.yml"
        )
        workflow_call = ((payload.get("on") or {}).get("workflow_call") or {})
        inputs = workflow_call.get("inputs") or {}
        run_job = (payload.get("jobs") or {}).get("run") or {}
        steps = run_job.get("steps") or []

        self.assertIn("nextest_archive_artifact_name", inputs)
        self.assertIn("nextest_archive_file_name", inputs)
        download_step = next(
            step for step in steps if step.get("name") == "Download nextest archive"
        )
        self.assertEqual(download_step.get("uses"), "actions/download-artifact@v8")
        self.assertEqual(
            (download_step.get("with") or {}).get("name"),
            "${{ inputs.nextest_archive_artifact_name }}",
        )
        export_step = next(
            step for step in steps if step.get("name") == "Export nextest archive path"
        )
        self.assertIn("VALIDATION_LAB_NEXTEST_ARCHIVE_FILE", export_step.get("run") or "")

        summary_step = next(
            step for step in steps if step.get("name") == "Prepare lane summary artifact"
        )
        summary_run = summary_step.get("run") or ""
        self.assertIn("--nextest-archive-artifact-name", summary_run)
        self.assertIn("--nextest-archive-file-name", summary_run)
        self.assertIn("--nextest-archive-mode", summary_run)

    def test_validation_lab_summary_records_plan_dedupe_fields(self) -> None:
        summary_step = workflow_step_by_name(
            REPO_ROOT / ".github/workflows/validation-lab.yml",
            "summary",
            "Build validation summary artifact",
        )
        run_script = summary_step.get("run") or ""

        self.assertIn(
            '--planner-fingerprint "${{ needs.metadata.outputs.planner_fingerprint }}"',
            run_script,
        )
        self.assertIn(
            '--dedupe-should-skip "${{ needs.metadata.outputs.dedupe_should_skip }}"',
            run_script,
        )
        self.assertIn(
            '--dedupe-matched-run-url "${{ needs.metadata.outputs.dedupe_matched_run_url }}"',
            run_script,
        )
        self.assertIn(
            '--latest-head-sha "${{ needs.metadata.outputs.head_sha }}"',
            run_script,
        )

    def test_sedna_heavy_tests_uses_safe_ref_env_and_requested_lane_inputs(self) -> None:
        metadata_step = workflow_step_by_name(
            REPO_ROOT / ".github/workflows/sedna-heavy-tests.yml",
            "metadata",
            "Compute checkout ref",
        )
        env = metadata_step.get("env") or {}
        self.assertEqual(env.get("CHECKOUT_REF"), "${{ github.sha }}")
        self.assertEqual(env.get("DISPLAY_REF"), "${{ github.ref_name }}")
        self.assertEqual(env.get("INPUT_REF"), "${{ inputs.ref }}")
        self.assertEqual(env.get("PR_HEAD_SHA"), "${{ github.event.pull_request.head.sha }}")
        self.assertEqual(env.get("PR_HEAD_REF"), "${{ github.event.pull_request.head.ref }}")
        self.assertEqual(env.get("REQUESTED_LANE"), "${{ inputs.lane }}")
        self.assertEqual(env.get("INPUT_RUST_BATCHING"), "${{ inputs.rust_batching || 'auto' }}")
        self.assertEqual(
            env.get("INPUT_RUST_BATCHING_OVERRIDE"),
            "${{ vars.SEDNA_HEAVY_RUST_BATCHING }}",
        )
        run_script = metadata_step.get("run") or ""
        self.assertIn("# BEGIN checkout identity", run_script)
        self.assertIn("resolve_checkout_identity", run_script)
        self.assertIn("FETCH_HEAD^{commit}", run_script)
        self.assertIn('echo "checkout_ref=${checkout_ref}"', run_script)
        self.assertIn('echo "checkout_sha=${checkout_sha}"', run_script)
        self.assertIn('echo "display_ref=${display_ref}"', run_script)
        self.assertNotIn('checkout_sha="$(git rev-parse FETCH_HEAD)', run_script)
        self.assertIn('checkout_ref="${CHECKOUT_REF}"', run_script)
        self.assertIn('checkout_ref="${PR_HEAD_SHA}"', run_script)
        self.assertIn('checkout_sha="${PR_HEAD_SHA}"', run_script)
        self.assertIn('display_ref="${DISPLAY_REF}"', run_script)
        self.assertIn('--requested-lane "${REQUESTED_LANE}"', run_script)
        self.assertIn('--rust-batching "${INPUT_RUST_BATCHING}"', run_script)
        self.assertIn('--rust-batching-override "${INPUT_RUST_BATCHING_OVERRIDE}"', run_script)
        self.assertIn('os.environ["REQUESTED_LANE"]', run_script)
        self.assertNotIn('"requested_lane": "${{ inputs.lane }}"', run_script)

    def test_rust_ci_full_nextest_platform_uses_versioned_tool_syntax(self) -> None:
        payload = load_workflow_payload(
            REPO_ROOT / ".github/workflows/rust-ci-full-nextest-platform.yml"
        )
        archive_env = ((payload.get("jobs") or {}).get("archive") or {}).get("env") or {}
        self.assertEqual(archive_env.get("CARGO_PROFILE_CI_TEST_DEBUG"), "0")
        self.assertEqual(archive_env.get("CARGO_PROFILE_CI_TEST_STRIP"), "symbols")

        tool_values: list[str] = []
        for job in (payload.get("jobs") or {}).values():
            for step in (job or {}).get("steps") or []:
                if step.get("uses") != "taiki-e/install-action@5bf6ce016fd2e72eefc647cbca1e4213f65955b8":
                    continue
                with_section = step.get("with") or {}
                self.assertNotIn("version", with_section)
                tool_values.append(with_section.get("tool"))

        self.assertEqual(
            len(tool_values),
            3,
        )
        self.assertCountEqual(
            tool_values,
            ["sccache@0.7.5", "nextest@0.9.103", "nextest@0.9.103"],
        )
        workflow_text = (
            REPO_ROOT / ".github/workflows/rust-ci-full-nextest-platform.yml"
        ).read_text(encoding="utf-8")
        self.assertNotIn("run_id }}-${{ matrix.shard", workflow_text)
        self.assertNotIn("remote-env-target-${{ matrix.shard", workflow_text)
        self.assertNotIn('hash:${{ matrix.shard }}/4', workflow_text)

    def test_rust_ci_full_nextest_platform_keeps_inputs_out_of_shell_source(self) -> None:
        payload = load_workflow_payload(
            REPO_ROOT / ".github/workflows/rust-ci-full-nextest-platform.yml"
        )
        jobs = payload.get("jobs") or {}

        self.assertEqual(
            (jobs["archive"].get("env") or {}).get("INPUT_TARGET"),
            "${{ inputs.target }}",
        )
        self.assertEqual(
            (jobs["archive"].get("env") or {}).get("INPUT_PROFILE"),
            "${{ inputs.profile }}",
        )
        self.assertEqual(
            (jobs["shard"].get("env") or {}).get("INPUT_TEST_THREADS"),
            "${{ inputs.test_threads }}",
        )

        unsafe_expressions = (
            "${{ inputs.target }}",
            "${{ inputs.profile }}",
            "${{ inputs.test_threads }}",
        )
        for job_name, job in jobs.items():
            for step in (job or {}).get("steps") or []:
                run_script = step.get("run") or ""
                step_name = step.get("name") or step.get("id")
                for expression in unsafe_expressions:
                    message = (
                        f"{job_name}/{step_name} interpolates {expression} into shell source"
                    )
                    self.assertNotIn(
                        expression,
                        run_script,
                        message,
                    )

    def test_just_recipe_bodies_handles_comma_separated_recipe_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            justfile = Path(tmpdir) / "justfile"
            justfile.write_text(
                "\n".join(
                    [
                        "foo, bar:",
                        "    cargo nextest run -p codex-core",
                        "",
                        "with-param target='default':",
                        "    cargo test -p codex-tui",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            recipes = just_recipe_bodies(justfile)

        self.assertEqual(recipes["foo"], ["    cargo nextest run -p codex-core", ""])
        self.assertEqual(recipes["bar"], ["    cargo nextest run -p codex-core", ""])
        self.assertEqual(recipes["with-param"], ["    cargo test -p codex-tui"])
        self.assertNotIn("foo,", recipes)

    def test_run_just_recipe_lanes_declare_nextest_when_recipe_uses_nextest(self) -> None:
        catalog = RESOLVE_VALIDATION_PLAN.load_catalog()
        nextest_recipes = just_recipes_with_nextest(REPO_ROOT / "justfile")
        missing: list[str] = []
        for lane in catalog["lanes"]:
            if lane.get("script_path") != ".github/scripts/validation-lanes/run-just-recipe.sh":
                continue
            script_args = lane.get("script_args") or []
            recipe = script_args[0] if script_args else ""
            if recipe in nextest_recipes and not lane.get("needs_nextest"):
                missing.append(str(lane.get("lane_id")))

        self.assertEqual(missing, [])

    def test_stack_sensitive_targeted_recipes_set_rust_min_stack(self) -> None:
        recipes = just_recipe_bodies(REPO_ROOT / "justfile")

        multi_agent_recipe = "\n".join(
            recipes["core-multi-agent-orchestration-targeted"]
        )
        self.assertEqual(multi_agent_recipe.count("RUST_MIN_STACK="), 4)

        blocking_waits_core_recipe = "\n".join(
            recipes["blocking-waits-core-targeted"]
        )
        self.assertEqual(blocking_waits_core_recipe.count("RUST_MIN_STACK="), 1)

        unified_exec_lines = recipes["blocking-waits-unified-exec-targeted"]
        unified_exec_cargo_lines = [
            line for line in unified_exec_lines if "cargo " in line
        ]
        self.assertTrue(unified_exec_cargo_lines)
        self.assertEqual(
            [
                line
                for line in unified_exec_cargo_lines
                if "RUST_MIN_STACK=" not in line
            ],
            [],
        )

    def test_run_just_recipe_lanes_declare_linux_build_deps_when_recipe_compiles_linux_sandbox(
        self,
    ) -> None:
        catalog = RESOLVE_VALIDATION_PLAN.load_catalog()
        recipe_bodies = just_recipe_bodies(REPO_ROOT / "justfile")
        direct_linux_build_deps_recipes = {
            name
            for name, body in recipe_bodies.items()
            if any(
                command in line
                for line in body
                for command in ("cargo test", "cargo nextest", "cargo check", "cargo build")
            )
            and any("codex-core" in line or "codex-tui" in line for line in body)
        }
        nested_linux_build_deps_recipes = {
            name
            for name, body in recipe_bodies.items()
            if any("just --justfile ../justfile" in line for line in body)
            and any(
                any(subrecipe in line for subrecipe in direct_linux_build_deps_recipes)
                for line in body
            )
        }
        linux_build_deps_recipes = direct_linux_build_deps_recipes | nested_linux_build_deps_recipes
        missing: list[str] = []
        for lane in catalog["lanes"]:
            if lane.get("script_path") != ".github/scripts/validation-lanes/run-just-recipe.sh":
                continue
            script_args = lane.get("script_args") or []
            recipe = script_args[0] if script_args else ""
            if recipe in linux_build_deps_recipes and not lane.get("needs_linux_build_deps"):
                missing.append(str(lane.get("lane_id")))

        self.assertEqual(missing, [])

    def test_expensive_rust_minimal_lanes_enable_sccache(self) -> None:
        catalog = RESOLVE_VALIDATION_PLAN.load_catalog()
        enabled = {
            lane["lane_id"]
            for lane in catalog["lanes"]
            if lane.get("setup_class") == "rust_minimal" and lane.get("needs_sccache")
        }
        self.assertEqual(
            enabled,
            {
                "codex.app-server-protocol-test",
                "codex.cli-surface-targeted",
                "codex.exec-native-computer-use-targeted",
                "codex.external-agent-session-migration-targeted",
                "codex.inference-observation-contract-targeted",
                "codex.native-computer-use-tool-registry-targeted",
                "codex.core-subagent-notification-visibility-targeted",
                "codex.spawn-agent-description-model-surface-targeted",
                "codex.spawn-agent-tool-model-surface-targeted",
                "codex.tui-agent-picker-model-surface-targeted",
                "codex.tui-agent-picker-targeted",
                "codex.tui-agent-picker-tree-targeted",
                "codex.tui-agent-picker-usage-targeted",
                "codex.tui-agent-usage-totals-targeted",
                "codex.tui-brokered-tool-replay-targeted",
                "codex.tui-collab-spawn-identity-targeted",
                "codex.model-catalog-compat-targeted",
                "codex.tui-config-refresh-session-targeted",
                "codex.tui-esc-interrupt-targeted",
                "codex.tui-front-queue-submit-targeted",
                "codex.tui-native-computer-use-targeted",
                "codex.tui-thread-session-policy-targeted",
                "codex.tui-transcript-viewport-targeted",
                "codex.tui-weekly-pacing-status-line-targeted",
            },
        )

    def test_tui_weekly_pacing_lane_pins_live_status_line_contract(self) -> None:
        catalog = RESOLVE_VALIDATION_PLAN.load_catalog()
        lane = next(
            lane
            for lane in catalog["lanes"]
            if lane["lane_id"] == "codex.tui-weekly-pacing-status-line-targeted"
        )
        self.assertEqual(
            lane["script_path"], ".github/scripts/validation-lanes/run-just-recipe.sh"
        )
        self.assertEqual(lane["script_args"], ["tui-weekly-pacing-status-line-targeted"])

        recipe = "\n".join(
            just_recipe_bodies(REPO_ROOT / "justfile")[
                "tui-weekly-pacing-status-line-targeted"
            ]
        )
        self.assertIn("--exact", recipe)
        for test_name in [
            "status_line_weekly_limit_renders_pacing_suffixes_from_live_status_line",
            "status_line_weekly_limit_renders_stale_suffix_over_pace_details",
            "status_line_weekly_limit_omits_pacing_when_inputs_are_missing",
        ]:
            self.assertIn(test_name, recipe)

    def test_validation_lab_passes_sccache_policy_only_to_sccache_lanes(self) -> None:
        payload = load_workflow_payload(REPO_ROOT / ".github/workflows/validation-lab.yml")
        jobs = payload.get("jobs") or {}
        expected_policy = (
            "${{ inputs.supersession_mode != 'auto' && "
            "'write-fallback' || 'restore-only' }}"
        )

        sccache_jobs = [
            "smoke_rust_minimal_lanes",
            "smoke_rust_integration_lanes",
            "smoke_release_lanes",
            "rust_minimal_lanes",
            "rust_minimal_batches",
            "rust_integration_lanes",
            "rust_integration_batches",
            "release_lanes",
            "artifact",
        ]
        for job_name in sccache_jobs:
            with self.subTest(job=job_name):
                self.assertEqual(
                    ((jobs.get(job_name) or {}).get("with") or {}).get("cache_policy"),
                    expected_policy,
                )

        non_sccache_jobs = [
            "smoke_workflow_lanes",
            "smoke_node_lanes",
            "workflow_lanes",
            "node_lanes",
        ]
        for job_name in non_sccache_jobs:
            with self.subTest(job=job_name):
                self.assertNotIn("cache_policy", (jobs.get(job_name) or {}).get("with") or {})

    def test_validation_lab_passes_bazel_setup_to_workflow_lanes(self) -> None:
        payload = load_workflow_payload(REPO_ROOT / ".github/workflows/validation-lab.yml")
        jobs = payload.get("jobs") or {}

        for job_name in ["smoke_workflow_lanes", "workflow_lanes"]:
            with self.subTest(job=job_name):
                self.assertEqual(
                    ((jobs.get(job_name) or {}).get("with") or {}).get("needs_bazel"),
                    "${{ matrix.needs_bazel }}",
                )

    def test_validation_lab_passes_timeout_to_workflow_lanes(self) -> None:
        payload = load_workflow_payload(REPO_ROOT / ".github/workflows/validation-lab.yml")
        jobs = payload.get("jobs") or {}

        for job_name in ["smoke_workflow_lanes", "workflow_lanes"]:
            with self.subTest(job=job_name):
                self.assertEqual(
                    ((jobs.get(job_name) or {}).get("with") or {}).get("timeout_minutes"),
                    "${{ matrix.timeout_minutes }}",
                )

        for job_name in ["smoke_node_lanes", "node_lanes"]:
            with self.subTest(job=job_name):
                self.assertNotIn("timeout_minutes", (jobs.get(job_name) or {}).get("with") or {})

    def test_validation_lab_workflow_lanes_do_not_inherit_secrets_from_operator_refs(self) -> None:
        payload = load_workflow_payload(REPO_ROOT / ".github/workflows/validation-lab.yml")
        jobs = payload.get("jobs") or {}

        for job_name in ["smoke_workflow_lanes", "workflow_lanes"]:
            with self.subTest(job=job_name):
                self.assertNotIn("secrets", jobs.get(job_name) or {})

    def test_sedna_heavy_writes_fallback_cache_only_for_manual_dispatch(self) -> None:
        payload = load_workflow_payload(REPO_ROOT / ".github/workflows/sedna-heavy-tests.yml")
        jobs = payload.get("jobs") or {}
        expected_policy = "${{ github.event_name == 'workflow_dispatch' && 'write-fallback' || 'restore-only' }}"

        for job_name in [
            "smoke_rust_minimal_lanes",
            "smoke_rust_integration_lanes",
            "smoke_release_lanes",
            "rust_minimal_lanes",
            "rust_minimal_batches",
            "rust_integration_lanes",
            "rust_integration_batches",
            "release_lanes",
        ]:
            with self.subTest(job=job_name):
                self.assertEqual(
                    ((jobs.get(job_name) or {}).get("with") or {}).get("cache_policy"),
                    expected_policy,
                )

    def test_sedna_heavy_passes_bazel_setup_to_workflow_lanes(self) -> None:
        payload = load_workflow_payload(REPO_ROOT / ".github/workflows/sedna-heavy-tests.yml")
        jobs = payload.get("jobs") or {}

        for job_name in ["smoke_workflow_lanes", "workflow_lanes"]:
            with self.subTest(job=job_name):
                self.assertEqual(
                    ((jobs.get(job_name) or {}).get("with") or {}).get("needs_bazel"),
                    "${{ matrix.needs_bazel }}",
                )

    def test_sedna_heavy_passes_timeout_to_workflow_lanes(self) -> None:
        payload = load_workflow_payload(REPO_ROOT / ".github/workflows/sedna-heavy-tests.yml")
        jobs = payload.get("jobs") or {}

        for job_name in ["smoke_workflow_lanes", "workflow_lanes"]:
            with self.subTest(job=job_name):
                self.assertEqual(
                    ((jobs.get(job_name) or {}).get("with") or {}).get("timeout_minutes"),
                    "${{ matrix.timeout_minutes }}",
                )

        for job_name in ["smoke_node_lanes", "node_lanes"]:
            with self.subTest(job=job_name):
                self.assertNotIn("timeout_minutes", (jobs.get(job_name) or {}).get("with") or {})

    def test_reusable_sccache_workflows_require_explicit_fallback_writes(self) -> None:
        for workflow_name in [
            "_validation-lane-rust-minimal.yml",
            "_validation-lane-rust-integration.yml",
            "_validation-lane-rust-batch.yml",
            "_validation-lane-release.yml",
            "_sedna-linux-rust.yml",
        ]:
            with self.subTest(workflow=workflow_name):
                workflow_path = REPO_ROOT / ".github/workflows" / workflow_name
                workflow_text = workflow_path.read_text(encoding="utf-8")
                payload = load_workflow_payload(workflow_path)
                inputs = (((payload.get("on") or {}).get("workflow_call") or {}).get("inputs") or {})
                self.assertEqual((inputs.get("checkout_fetch_depth") or {}).get("default"), "1")
                self.assertEqual((inputs.get("cache_policy") or {}).get("default"), "restore-only")
                self.assertNotIn("ACTIONS_RUNTIME_TOKEN", workflow_text)
                self.assertNotIn("SCCACHE_GHA_ENABLED=true", workflow_text)

                run_job = (payload.get("jobs") or {}).get("run") or {}
                checkout_step = next(
                    step
                    for step in run_job.get("steps") or []
                    if step.get("uses") == "actions/checkout@v7"
                )
                self.assertEqual(
                    (checkout_step.get("with") or {}).get("fetch-depth"),
                    "${{ inputs.checkout_fetch_depth }}",
                )
                self.assertEqual((run_job.get("env") or {}).get("SCCACHE_CACHE_SIZE"), "2G")
                self.assertFalse(
                    any(
                        step.get("name") == "Expose GitHub cache-service env for sccache"
                        for step in run_job.get("steps") or []
                    )
                )
                configure_step = next(
                    step
                    for step in run_job.get("steps") or []
                    if step.get("name") == "Configure sccache backend"
                )
                workflow_src_prefix = (
                    "../.workflow-src"
                    if workflow_name == "_sedna-linux-rust.yml"
                    else ".workflow-src"
                )
                self.assertEqual(
                    configure_step.get("run"),
                    f"bash {workflow_src_prefix}/.github/scripts/configure_sccache_backend.sh '${{{{ inputs.cache_policy }}}}'",
                )

                save_step = next(
                    step
                    for step in run_job.get("steps") or []
                    if step.get("name") == "Save sccache cache (fallback)"
                )
                self.assertIn(
                    "steps.sccache_backend.outputs.policy == 'write-fallback'",
                    save_step.get("if") or "",
                )

    def test_rust_batch_workflow_reclaims_disk_and_uses_small_debug_profiles(self) -> None:
        payload = load_workflow_payload(
            REPO_ROOT / ".github/workflows/_validation-lane-rust-batch.yml"
        )
        run_job = (payload.get("jobs") or {}).get("run") or {}
        env = run_job.get("env") or {}

        self.assertEqual(env.get("CARGO_PROFILE_DEV_DEBUG"), "0")
        self.assertEqual(env.get("CARGO_PROFILE_DEV_STRIP"), "symbols")
        self.assertEqual(env.get("CARGO_PROFILE_TEST_DEBUG"), "0")
        self.assertEqual(env.get("CARGO_PROFILE_TEST_STRIP"), "symbols")

        cleanup_step = next(
            step
            for step in run_job.get("steps") or []
            if step.get("name") == "Reclaim runner disk headroom"
        )
        cleanup_script = cleanup_step.get("run") or ""
        self.assertIn("/usr/local/lib/android", cleanup_script)
        self.assertIn("/usr/share/dotnet", cleanup_script)
        self.assertIn("/opt/ghc", cleanup_script)
        self.assertIn("/usr/local/share/boost", cleanup_script)
        self.assertIn("/opt/hostedtoolcache/CodeQL", cleanup_script)
        self.assertIn("12 GiB safety floor", cleanup_script)

    def test_reusable_validation_lane_workflows_source_helpers_from_workflow_ref(self) -> None:
        for workflow_name in [
            "_validation-lane-workflow.yml",
            "_validation-lane-node.yml",
            "_validation-lane-rust-minimal.yml",
            "_validation-lane-rust-integration.yml",
            "_validation-lane-release.yml",
        ]:
            with self.subTest(workflow=workflow_name):
                payload = load_workflow_payload(REPO_ROOT / ".github/workflows" / workflow_name)
                run_job = (payload.get("jobs") or {}).get("run") or {}
                checkout_steps = [
                    step
                    for step in run_job.get("steps") or []
                    if step.get("uses") == "actions/checkout@v7"
                ]
                self.assertGreaterEqual(len(checkout_steps), 2)
                self.assertEqual(
                    (checkout_steps[1].get("with") or {}).get("ref"),
                    "${{ github.sha }}",
                )
                self.assertEqual(
                    (checkout_steps[1].get("with") or {}).get("path"),
                    ".workflow-src",
                )

                run_lane_step = next(
                    step
                    for step in run_job.get("steps") or []
                    if step.get("name") == "Run requested lane script"
                )
                self.assertIn(
                    ".workflow-src/.github/scripts/run_validation_lane.py",
                    run_lane_step.get("run") or "",
                )

                summary_step = next(
                    step
                    for step in run_job.get("steps") or []
                    if step.get("name") == "Prepare lane summary artifact"
                )
                self.assertIn(
                    ".workflow-src/.github/scripts/write_lane_summary.py",
                    summary_step.get("run") or "",
                )

    def test_validation_lane_workflow_keeps_secrets_out_of_target_controlled_scripts(self) -> None:
        payload = load_workflow_payload(REPO_ROOT / ".github/workflows/_validation-lane-workflow.yml")
        workflow_call = ((payload.get("on") or {}).get("workflow_call") or {})
        self.assertNotIn("secrets", workflow_call)

        run_job = (payload.get("jobs") or {}).get("run") or {}
        run_lane_step = next(
            step
            for step in run_job.get("steps") or []
            if step.get("name") == "Run requested lane script"
        )
        run_lane_env = run_lane_step.get("env") or {}
        for env_name, env_value in run_lane_env.items():
            with self.subTest(env=env_name):
                self.assertNotRegex(env_name, r"(API_KEY|PRIVATE_KEY|SECRET|TOKEN)")
                self.assertNotIn("secrets.", str(env_value))

    def test_sync_models_json_splits_read_check_from_write_pr_creation(self) -> None:
        payload = load_workflow_payload(REPO_ROOT / ".github/workflows/sync-models-json.yml")
        jobs = payload.get("jobs") or {}
        check_job = jobs.get("check") or {}
        create_pr_job = jobs.get("create_pr") or {}

        self.assertEqual(payload.get("permissions"), {"contents": "read"})
        self.assertEqual(check_job.get("permissions"), {"contents": "read"})
        self.assertEqual(
            create_pr_job.get("permissions"),
            {"contents": "write", "pull-requests": "write"},
        )
        self.assertEqual(create_pr_job.get("needs"), "check")
        self.assertEqual(create_pr_job.get("if"), "needs.check.outputs.changed == 'true'")
        self.assertEqual(
            (check_job.get("outputs") or {}).get("changed"),
            "${{ steps.diff.outputs.changed }}",
        )
        self.assertEqual(
            (check_job.get("outputs") or {}).get("upstream_short_sha"),
            "${{ steps.upstream.outputs.upstream_short_sha }}",
        )

        check_steps = check_job.get("steps") or []
        upload_step = next(
            step for step in check_steps if step.get("name") == "Upload sync payload"
        )
        self.assertEqual(upload_step.get("if"), "steps.diff.outputs.changed == 'true'")
        self.assertEqual(upload_step.get("uses"), "actions/upload-artifact@v7")

        create_steps = create_pr_job.get("steps") or []
        download_step = next(
            step for step in create_steps if step.get("name") == "Download sync payload"
        )
        self.assertEqual(download_step.get("uses"), "actions/download-artifact@v8")
        create_step = next(step for step in create_steps if step.get("name") == "Create PR")
        self.assertIn(
            "needs.check.outputs.upstream_short_sha",
            (create_step.get("with") or {}).get("branch", ""),
        )
        self.assertEqual(
            (create_step.get("with") or {}).get("body-path"),
            "sync-models-json-payload/summary.md",
        )

    def test_codeql_advanced_workflow_is_authoritative_hardened_setup(self) -> None:
        payload = load_workflow_payload(REPO_ROOT / ".github/workflows/codeql.yml")
        trigger = payload.get("on") or {}
        jobs = payload.get("jobs") or {}
        plan_job = jobs.get("plan") or {}
        analyze_job = jobs.get("analyze") or {}
        results_job = jobs.get("results") or {}
        plan_steps = plan_job.get("steps") or []
        steps = analyze_job.get("steps") or []

        self.assertIn("workflow_dispatch", trigger)
        self.assertEqual(
            (trigger.get("push") or {}).get("branches"),
            ["main", "upstream-main"],
        )
        self.assertEqual(
            (trigger.get("pull_request") or {}).get("branches"),
            ["main", "upstream-main", "integration/**"],
        )
        self.assertEqual(trigger.get("merge_group"), {"types": ["checks_requested"]})
        self.assertEqual(payload.get("permissions"), {"contents": "read"})
        concurrency = payload.get("concurrency") or {}
        concurrency_group = str(concurrency.get("group") or "")
        self.assertIn("concurrency-group::${{ github.workflow }}::", concurrency_group)
        self.assertIn("format('merge-group-{0}', github.sha)", concurrency_group)
        self.assertIn("format('pr-{0}', github.event.pull_request.number)", concurrency_group)
        self.assertIn("format('push-{0}', github.sha)", concurrency_group)
        self.assertIn("format('{0}-{1}', github.event_name, github.run_id)", concurrency_group)
        self.assertEqual(
            concurrency.get("cancel-in-progress"),
            "${{ github.event_name == 'pull_request' }}",
        )

        self.assertEqual(plan_job.get("name"), "CodeQL language planner")
        self.assertEqual(plan_job.get("runs-on"), "ubuntu-24.04")
        self.assertEqual(plan_job.get("permissions") or {}, {"contents": "read"})
        self.assertEqual(
            plan_job.get("outputs") or {},
            {
                "matrix": "${{ steps.plan.outputs.matrix }}",
                "run_analysis": "${{ steps.plan.outputs.run_analysis }}",
                "selected_languages": "${{ steps.plan.outputs.selected_languages }}",
            },
        )
        plan_checkout = next(
            step
            for step in plan_steps
            if step.get("name") == "Checkout PR head for path classification"
        )
        self.assertEqual(
            plan_checkout.get("if"),
            "${{ github.event_name == 'pull_request' || github.event_name == 'merge_group' }}",
        )
        self.assertEqual(plan_checkout.get("uses"), "actions/checkout@v7")
        self.assertEqual(
            plan_checkout.get("with") or {},
            {
                "ref": "${{ github.event_name == 'pull_request' && github.event.pull_request.head.sha || github.sha }}",
                "fetch-depth": "1",
                "persist-credentials": "false",
            },
        )
        select_step = next(
            step for step in plan_steps if step.get("name") == "Select CodeQL languages"
        )
        self.assertEqual(select_step.get("id"), "plan")
        self.assertEqual(
            select_step.get("env") or {},
            {
                "BASE_SHA": "${{ github.event.pull_request.base.sha || github.event.merge_group.base_sha || '' }}",
                "HEAD_SHA": "${{ github.event.pull_request.head.sha || github.sha }}",
            },
        )
        select_run = select_step.get("run") or ""
        self.assertIn(
            'full_languages=\'["actions","c-cpp","javascript-typescript","python","rust"]\'',
            select_run,
        )
        self.assertIn(
            "if [[ '${{ github.event_name }}' != 'pull_request' && '${{ github.event_name }}' != 'merge_group' ]]; then",
            select_run,
        )
        self.assertIn('git fetch --no-tags --depth=1 origin "${BASE_SHA}"', select_run)
        self.assertIn("classify_ci_paths.py --base-sha", select_run)
        self.assertIn("Unable to fetch PR base; running every CodeQL language.", select_run)
        self.assertIn("Unable to classify PR paths; running every CodeQL language.", select_run)
        self.assertIn('["codeql_languages"]', select_run)
        self.assertIn('"language": "not-applicable"', select_run)
        for rust_scope in (
            '"codex-rs-core"',
            '"codex-rs-tui"',
            '"codex-rs-services"',
            '"codex-rs-rest-a"',
            '"codex-rs-rest-b"',
            '"codex-rs-rest-c"',
            '"codex-rs-rest-d"',
            '"tools"',
        ):
            self.assertIn(rust_scope, select_run)
        self.assertIn('"scope": scope', select_run)
        self.assertIn("codeql-rust-{scope}.yml", select_run)

        self.assertEqual(analyze_job.get("needs"), "plan")
        self.assertEqual(
            analyze_job.get("if"),
            "${{ needs.plan.outputs.run_analysis == 'true' }}",
        )
        self.assertEqual(analyze_job.get("runs-on"), "ubuntu-24.04")
        self.assertEqual(analyze_job.get("timeout-minutes"), "360")
        self.assertEqual(
            analyze_job.get("permissions") or {},
            {
                "actions": "read",
                "contents": "read",
                "packages": "read",
                "security-events": "write",
            },
        )
        self.assertEqual(
            ((analyze_job.get("strategy") or {}).get("matrix")),
            "${{ fromJSON(needs.plan.outputs.matrix) }}",
        )

        workflow_json = json.dumps(payload, sort_keys=True)
        self.assertNotIn("autobuild", workflow_json)
        self.assertNotIn('"build-mode": "manual"', workflow_json)

        checkout_step = next(step for step in steps if step.get("name") == "Checkout repository")
        self.assertEqual(checkout_step.get("uses"), "actions/checkout@v7")
        self.assertEqual((checkout_step.get("with") or {}).get("persist-credentials"), "false")

        install_rust_step = next(
            step for step in steps if step.get("name") == "Install Rust toolchains for CodeQL"
        )
        self.assertEqual(install_rust_step.get("if"), "${{ matrix.language == 'rust' }}")
        install_rust_run = install_rust_step.get("run") or ""
        self.assertIn("rust-toolchain*", install_rust_run)
        self.assertIn('"--component"', install_rust_run)
        self.assertIn('"rust-src"', install_rust_run)
        self.assertNotIn('toolchain.get("components"', install_rust_run)
        self.assertNotIn('"clippy"', install_rust_run)
        self.assertNotIn('"rustfmt"', install_rust_run)
        self.assertNotIn('"rustc-dev"', install_rust_run)
        self.assertNotIn('"llvm-tools-preview"', install_rust_run)
        self.assertIn("subprocess.run(command, check=True)", install_rust_run)

        restore_rust_cache_step = next(
            step for step in steps if step.get("name") == "Restore Rust dependency cache for CodeQL"
        )
        self.assertEqual(restore_rust_cache_step.get("if"), "${{ matrix.language == 'rust' }}")
        self.assertEqual(restore_rust_cache_step.get("uses"), "actions/cache/restore@v6")
        restore_cache_with = restore_rust_cache_step.get("with") or {}
        self.assertIn("~/.cargo/registry/cache/", restore_cache_with.get("path") or "")
        self.assertIn("~/.cargo/git/db/", restore_cache_with.get("path") or "")
        self.assertIn("codeql-rust-cargo-home-v1-", restore_cache_with.get("key") or "")
        self.assertNotIn("~/.rustup/toolchains", workflow_json)

        telemetry_step = next(
            step for step in steps if step.get("name") == "Record Rust cache telemetry for CodeQL"
        )
        self.assertEqual(telemetry_step.get("if"), "${{ matrix.language == 'rust' }}")
        telemetry_run = telemetry_step.get("run") or ""
        self.assertIn("CodeQL Rust cache telemetry", telemetry_run)
        self.assertIn("cache_codeql_rust_cargo_home_restore.outputs.cache-hit", telemetry_run)

        prefetch_rust_step = next(
            step for step in steps if step.get("name") == "Prefetch Rust dependencies for CodeQL"
        )
        self.assertEqual(prefetch_rust_step.get("if"), "${{ matrix.language == 'rust' }}")
        self.assertEqual(prefetch_rust_step.get("continue-on-error"), "true")
        prefetch_run = prefetch_rust_step.get("run") or ""
        self.assertIn("cargo fetch --locked --manifest-path codex-rs/Cargo.toml", prefetch_run)
        self.assertIn(
            "cargo fetch --locked --manifest-path tools/argument-comment-lint/Cargo.toml",
            prefetch_run,
        )

        scoped_rust_config_step = next(
            step for step in steps if step.get("name") == "Prepare scoped Rust CodeQL config"
        )
        self.assertEqual(scoped_rust_config_step.get("if"), "${{ matrix.language == 'rust' }}")
        self.assertEqual(
            scoped_rust_config_step.get("env") or {},
            {
                "RUST_SCOPE": "${{ matrix.scope }}",
                "RUST_CONFIG": "${{ matrix.config_file }}",
            },
        )
        scoped_rust_config_run = scoped_rust_config_step.get("run") or ""
        for rust_scope in (
            "codex-rs-core",
            "codex-rs-tui",
            "codex-rs-services",
            "codex-rs-rest-a",
            "codex-rs-rest-b",
            "codex-rs-rest-c",
            "codex-rs-rest-d",
            "tools",
        ):
            self.assertIn(f"{rust_scope})", scoped_rust_config_run)
        service_paths = (
            "codex-rs/app-server",
            "codex-rs/app-server-client",
            "codex-rs/app-server-daemon",
            "codex-rs/app-server-protocol",
            "codex-rs/app-server-test-client",
            "codex-rs/app-server-transport",
            "codex-rs/bwrap",
            "codex-rs/exec",
            "codex-rs/exec-server",
            "codex-rs/file-watcher",
            "codex-rs/http-client",
            "codex-rs/linux-sandbox",
            "codex-rs/network-proxy",
            "codex-rs/process-hardening",
            "codex-rs/sandboxing",
            "codex-rs/shell-command",
            "codex-rs/shell-escalation",
            "codex-rs/terminal-detection",
            "codex-rs/windows-sandbox-rs",
        )
        for service_path in service_paths:
            self.assertIn(service_path, scoped_rust_config_run)
        service_case = re.search(
            r"codex-rs-services\)\s+scope_paths=\(\s*(.*?)\s*\)\s+scope_ignores=\(\)",
            scoped_rust_config_run,
            re.DOTALL,
        )
        self.assertIsNotNone(service_case)
        self.assertEqual(tuple(service_case.group(1).split()), service_paths)
        rest_scopes = (
            "codex-rs-rest-a",
            "codex-rs-rest-b",
            "codex-rs-rest-c",
            "codex-rs-rest-d",
        )
        for rust_scope in rest_scopes:
            self.assertIn(f"{rust_scope})", scoped_rust_config_run)
        rest_paths = {
            scope: [
                line.strip()
                for line in re.search(
                    rf"{scope}\)\s+scope_paths=\(\s*(.*?)\s*\)\s+scope_ignores=\(\)",
                    scoped_rust_config_run,
                    re.DOTALL,
                ).group(1).split()
            ]
            for scope in rest_scopes
        }
        all_rest_paths = sum((rest_paths.values()), [])
        self.assertEqual(len(all_rest_paths), len(set(all_rest_paths)))
        tracked_codex_rs_dirs = set(
            subprocess.run(
                ["git", "ls-tree", "-d", "--name-only", "HEAD:codex-rs"],
                cwd=REPO_ROOT,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.splitlines()
        )
        self.assertEqual(
            set(rest_paths["codex-rs-rest-a"])
            | set(rest_paths["codex-rs-rest-b"])
            | set(rest_paths["codex-rs-rest-c"])
            | set(rest_paths["codex-rs-rest-d"]),
            {
                f"codex-rs/{path}"
                for path in tracked_codex_rs_dirs
                if path not in {
                    "core",
                    "tui",
                    *(service_path.split("/", 1)[1] for service_path in service_paths),
                }
            },
        )
        self.assertIn("security-and-quality", scoped_rust_config_run)
        self.assertIn("paths-ignore:", scoped_rust_config_run)
        self.assertIn('> "${RUST_CONFIG}"', scoped_rust_config_run)

        expected_scope_paths = {
            "codex-rs-core": ["codex-rs/core"],
            "codex-rs-tui": ["codex-rs/tui"],
            "codex-rs-services": list(service_paths),
            **rest_paths,
            "tools": ["tools"],
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            for rust_scope, expected_paths in expected_scope_paths.items():
                config_path = Path(tmp_dir) / f"{rust_scope}.yml"
                subprocess.run(
                    ["bash", "-c", scoped_rust_config_run],
                    cwd=REPO_ROOT,
                    env={
                        **os.environ,
                        "RUST_SCOPE": rust_scope,
                        "RUST_CONFIG": str(config_path),
                    },
                    check=True,
                    capture_output=True,
                    text=True,
                )
                generated = yaml.safe_load(config_path.read_text(encoding="utf-8"))
                self.assertEqual(
                    generated,
                    {
                        "name": f"codex-codeql-rust-{rust_scope}",
                        "queries": [
                            {"uses": "security-and-quality"},
                            {
                                "uses": "./.github/codeql/rust-computer-use-contract/suites/rust-computer-use-production.qls"
                            },
                        ],
                        "paths": expected_paths,
                        "paths-ignore": [".github/codeql/rust-computer-use-contract/test/**"],
                        "threat-models": "local",
                    },
                )

        actions_config_step = next(
            step for step in steps if step.get("name") == "Prepare Actions CodeQL query pack config"
        )
        self.assertEqual(actions_config_step.get("if"), "${{ matrix.language == 'actions' }}")
        actions_config_run = actions_config_step.get("run") or ""
        self.assertIn(".github/codeql/actions-workflow-security", actions_config_run)
        self.assertIn("github.event.pull_request.head.repo.full_name", actions_config_run)
        self.assertIn("github.repository", actions_config_run)
        self.assertIn("github.event.pull_request.base.sha", actions_config_run)
        self.assertIn(".codeql-runtime/trusted-base", actions_config_run)
        self.assertIn("security-and-quality", actions_config_run)
        self.assertIn(".codeql-runtime/codeql-actions.yml", actions_config_run)

        init_step = next(step for step in steps if step.get("name") == "Initialize CodeQL")
        self.assertEqual(init_step.get("uses"), "github/codeql-action/init@v4.37.9")
        self.assertEqual(
            init_step.get("with") or {},
            {
                "languages": "${{ matrix.language }}",
                "build-mode": "${{ matrix.build-mode }}",
                "config-file": "${{ matrix.config_file }}",
                "dependency-caching": "${{ github.event_name == 'pull_request' && 'restore' || 'full' }}",
            },
        )

        diff_ranges_step = next(
            step
            for step in steps
            if step.get("name")
            == "Restore complete CodeQL diff ranges for large pull requests"
        )
        self.assertEqual(
            diff_ranges_step.get("if"),
            "${{ github.event_name == 'pull_request' && github.event.pull_request.changed_files >= 300 }}",
        )
        self.assertEqual(
            (diff_ranges_step.get("env") or {}).get("BASE_SHA"),
            "${{ github.event.pull_request.base.sha }}",
        )
        diff_ranges_run = diff_ranges_step.get("run") or ""
        self.assertIn("prepare_codeql_diff_ranges.py", diff_ranges_run)
        self.assertIn('> "${RUNNER_TEMP}/pr-diff-range.json"', diff_ranges_run)
        self.assertNotIn("--output", diff_ranges_run)
        self.assertLess(steps.index(init_step), steps.index(diff_ranges_step))
        analyze_step = next(step for step in steps if step.get("name") == "Perform CodeQL Analysis")
        self.assertLess(steps.index(diff_ranges_step), steps.index(analyze_step))
        self.assertEqual(
            (analyze_step.get("with") or {}).get("category"),
            "${{ matrix.scope != '' && format('/language:{0}/{1}', matrix.language, matrix.scope) || format('/language:{0}', matrix.language) }}",
        )

        save_rust_cache_step = next(
            step for step in steps if step.get("name") == "Save Rust dependency cache for CodeQL"
        )
        self.assertEqual(save_rust_cache_step.get("continue-on-error"), "true")
        self.assertEqual(save_rust_cache_step.get("uses"), "actions/cache/save@v6")
        self.assertIn("matrix.language == 'rust'", save_rust_cache_step.get("if") or "")
        self.assertIn("github.event_name != 'pull_request'", save_rust_cache_step.get("if") or "")
        self.assertIn("refs/heads/main", save_rust_cache_step.get("if") or "")
        self.assertIn("refs/heads/upstream-main", save_rust_cache_step.get("if") or "")
        self.assertNotIn("target/", (save_rust_cache_step.get("with") or {}).get("path") or "")

        self.assertEqual(results_job.get("name"), "CodeQL required gate")
        self.assertEqual(results_job.get("needs"), ["plan", "analyze"])
        self.assertEqual(results_job.get("if"), "always()")
        self.assertEqual(results_job.get("permissions") or {}, {"actions": "read"})
        timing_step = next(
            step for step in results_job.get("steps") or [] if step.get("name") == "Report CodeQL timing"
        )
        timing_run = timing_step.get("run") or ""
        self.assertIn("CodeQL timing", timing_run)
        self.assertIn("actions/runs/${{ github.run_id }}/jobs", timing_run)
        self.assertIn("CodeQL language planner", timing_run)
        self.assertIn("Analyze \\\\(", timing_run)
        results_run = "\n".join(
            step.get("run") or "" for step in results_job.get("steps") or []
        )
        self.assertIn("CodeQL planner failed", results_run)
        self.assertIn("Selected CodeQL analysis failed", results_run)
        self.assertIn("No CodeQL language analysis was applicable", results_run)

        config = yaml.load(
            (REPO_ROOT / ".github/codeql/codeql-config.yml").read_text(encoding="utf-8"),
            Loader=yaml.BaseLoader,
        )
        self.assertEqual(
            config,
            {
                "name": "codex-codeql-advanced",
                "queries": [{"uses": "security-and-quality"}],
                "threat-models": "local",
            },
        )
        actions_config = yaml.load(
            (REPO_ROOT / ".github/codeql/codeql-actions.yml").read_text(encoding="utf-8"),
            Loader=yaml.BaseLoader,
        )
        self.assertEqual(
            actions_config,
            {
                "name": "codex-codeql-actions",
                "queries": [
                    {"uses": "security-and-quality"},
                    {"uses": "./.github/codeql/actions-workflow-security"},
                ],
                "threat-models": "local",
            },
        )
        actions_pack = yaml.load(
            (REPO_ROOT / ".github/codeql/actions-workflow-security/qlpack.yml").read_text(
                encoding="utf-8"
            ),
            Loader=yaml.BaseLoader,
        )
        self.assertEqual(actions_pack.get("name"), "sednalabs/actions-workflow-security")
        self.assertEqual(actions_pack.get("extractor"), "actions")
        self.assertEqual(actions_pack.get("dependencies"), {"codeql/actions-all": "*"})
        self.assertEqual(
            actions_pack.get("defaultSuiteFile"),
            "suites/actions-workflow-security.qls",
        )
        self.assertIn(
            "@id actions/sensitive-workflow-value-to-log",
            (REPO_ROOT / ".github/codeql/actions-workflow-security/SensitiveWorkflowValueToLog.ql")
            .read_text(encoding="utf-8"),
        )
        self.assertIn(
            "@id actions/sensitive-workflow-value-to-verbose-tool",
            (REPO_ROOT / ".github/codeql/actions-workflow-security/SensitiveWorkflowValueToVerboseTool.ql")
            .read_text(encoding="utf-8"),
        )
        for query_id in [
            "actions/unsafe-release-publishing-path",
            "actions/release-publisher-with-untrusted-input",
            "actions/overbroad-workflow-permissions",
            "actions/write-token-on-untrusted-trigger",
            "actions/artifact-published-without-provenance",
            "actions/repo-security-invariant-violation",
        ]:
            self.assertTrue(
                any(
                    f"@id {query_id}" in path.read_text(encoding="utf-8")
                    for path in (REPO_ROOT / ".github/codeql/actions-workflow-security").glob("*.ql")
                ),
                query_id,
            )
        self.assertIn(
            "getAWriteToGitHubEnv",
            (REPO_ROOT / ".github/codeql/actions-workflow-security/LogExposure.qll").read_text(
                encoding="utf-8"
            ),
        )
        workflow_security = (
            REPO_ROOT / ".github/codeql/actions-workflow-security/WorkflowSecurity.qll"
        ).read_text(encoding="utf-8")
        self.assertIn("jobHasPublishingSink", workflow_security)
        self.assertIn("jobEffectiveWritePermission", workflow_security)
        self.assertIn("jobHasRepoApprovedProvenance", workflow_security)
        self.assertIn(
            'jobUsesActionMatching(job, "(?i)^actions/github-script(@.*)?$", _)',
            workflow_security,
        )
        self.assertFalse((REPO_ROOT / ".github/codeql/codeql-rust-pr.yml").exists())
        rust_config = yaml.load(
            (REPO_ROOT / ".github/codeql/codeql-rust.yml").read_text(encoding="utf-8"),
            Loader=yaml.BaseLoader,
        )
        self.assertEqual(
            rust_config,
            {
                "name": "codex-codeql-rust",
                "queries": [
                    {"uses": "security-and-quality"},
                    {
                        "uses": "./.github/codeql/rust-computer-use-contract/suites/rust-computer-use-production.qls"
                    },
                ],
                "paths": ["codex-rs", "tools"],
                "paths-ignore": [".github/codeql/rust-computer-use-contract/test/**"],
                "threat-models": "local",
            },
        )
        rust_contract_pack = yaml.load(
            (REPO_ROOT / ".github/codeql/rust-computer-use-contract/qlpack.yml").read_text(
                encoding="utf-8"
            ),
            Loader=yaml.BaseLoader,
        )
        self.assertEqual(
            rust_contract_pack.get("name"),
            "sednalabs/rust-computer-use-contract",
        )
        self.assertEqual(rust_contract_pack.get("extractor"), "rust")
        self.assertEqual(rust_contract_pack.get("dependencies"), {"codeql/rust-all": "*"})
        for query_id in [
            "rust/computer-use-match-drops-native-image",
            "rust/android-visual-tool-missing-native-image-guard",
            "rust/computer-use-response-success-with-error",
        ]:
            self.assertTrue(
                any(
                    f"@id {query_id}" in path.read_text(encoding="utf-8")
                    for path in (REPO_ROOT / ".github/codeql/rust-computer-use-contract/queries").glob("*.ql")
                ),
                query_id,
            )

    def test_closed_pr_run_canceller_preserves_post_merge_branch_runs(self) -> None:
        payload = load_workflow_payload(REPO_ROOT / ".github/workflows/cancel-pr-runs.yml")
        trigger = payload.get("on") or {}
        job = ((payload.get("jobs") or {}).get("cancel") or {})
        steps = job.get("steps") or []

        self.assertEqual(
            ((trigger.get("pull_request_target") or {}).get("types") or []),
            ["closed"],
        )
        self.assertEqual(payload.get("permissions"), {"actions": "write", "contents": "read"})
        self.assertEqual(job.get("permissions") or {}, {})
        self.assertEqual(job.get("runs-on"), "ubuntu-latest")

        workflow_json = json.dumps(payload, sort_keys=True)
        self.assertNotIn("actions/checkout", workflow_json)
        self.assertNotIn("github.event.pull_request.head.repo.clone_url", workflow_json)

        cancel_step = next(
            step for step in steps if step.get("name") == "Cancel stale runs for the closed PR"
        )
        self.assertEqual(cancel_step.get("uses"), "actions/github-script@v9.0.0")
        script = (cancel_step.get("with") or {}).get("script") or ""

        self.assertIn("github.rest.actions.listWorkflowRunsForRepo", script)
        self.assertIn("event: 'pull_request'", script)
        self.assertIn("run.pull_requests.some((pr) => pr.number === prNumber)", script)
        self.assertIn("github.rest.actions.cancelWorkflowRun", script)
        self.assertIn("headRepo === currentRepo", script)
        self.assertIn("!protectedBranches.has(headBranch)", script)
        self.assertIn("'main'", script)
        self.assertIn("'upstream-main'", script)
        self.assertIn("mayCancelHeadPushRuns &&", script)
        self.assertIn("Post-merge push runs on ${baseBranch}", script)

    def test_supersede_automation_prs_uses_pr_write_and_idempotent_comments(self) -> None:
        payload = load_workflow_payload(
            REPO_ROOT / ".github/workflows/supersede-automation-prs.yml"
        )
        reconcile = ((payload.get("jobs") or {}).get("reconcile") or {})

        self.assertEqual(
            payload.get("concurrency"),
            {
                "group": "${{ github.workflow }}",
                "cancel-in-progress": "false",
            },
        )

        self.assertEqual(
            reconcile.get("permissions"),
            {"contents": "read", "pull-requests": "write"},
        )

        apply_steps = [
            step
            for step in reconcile.get("steps") or []
            if (step.get("name") or "").startswith("Apply ")
        ]
        self.assertEqual(len(apply_steps), 2)
        for step in apply_steps:
            with self.subTest(step=step.get("name")):
                script = (step.get("with") or {}).get("script") or ""
                self.assertIn("github.paginate(", script)
                self.assertIn("github.rest.issues.listComments", script)
                self.assertIn("comment.user?.login?.toLowerCase() === 'github-actions[bot]'", script)
                self.assertIn("comment.body === action.message", script)
                self.assertIn("github.rest.issues.createComment", script)
                self.assertIn("github.rest.pulls.update", script)
                self.assertIn("pull_number: action.number", script)
                self.assertNotIn("github.rest.issues.update", script)

    def test_sedna_sync_upstream_uses_github_app_token_and_shared_helper(self) -> None:
        payload = load_workflow_payload(REPO_ROOT / ".github/workflows/sedna-sync-upstream.yml")
        sync_job = ((payload.get("jobs") or {}).get("sync") or {})
        steps = sync_job.get("steps") or []

        credential_step = next(
            step
            for step in steps
            if step.get("name") == "Resolve upstream sync credential mode"
        )
        self.assertIn("SEDNA_SYNC_UPSTREAM_APP_CLIENT_ID", (credential_step.get("env") or {}).get("APP_CLIENT_ID", ""))
        self.assertIn("SEDNA_SYNC_UPSTREAM_APP_PRIVATE_KEY", (credential_step.get("env") or {}).get("APP_PRIVATE_KEY", ""))

        token_step = next(
            step
            for step in steps
            if step.get("name") == "Generate upstream sync app token"
        )
        self.assertEqual(
            token_step.get("if"),
            "${{ steps.credential-mode.outputs.use_app_token == 'true' }}",
        )
        self.assertEqual(token_step.get("uses"), "actions/create-github-app-token@v3")
        self.assertEqual(
            token_step.get("with") or {},
            {
                "client-id": "${{ vars.SEDNA_SYNC_UPSTREAM_APP_CLIENT_ID }}",
                "private-key": "${{ secrets.SEDNA_SYNC_UPSTREAM_APP_PRIVATE_KEY }}",
                "permission-contents": "write",
                "permission-workflows": "write",
            },
        )

        sync_step = next(
            step for step in steps if step.get("name") == "Fast-forward upstream mirror"
        )
        self.assertIn(".github/scripts/sync_upstream_mirror.py", sync_step.get("run") or "")
        self.assertIn("--mode required-write", sync_step.get("run") or "")
        self.assertEqual(
            (sync_job.get("outputs") or {}).get("synced_upstream_main_sha"),
            "${{ steps.sync-mirror.outputs.synced_upstream_main_sha }}",
        )

    def test_sedna_sync_upstream_keeps_audit_in_separate_read_only_job(self) -> None:
        payload = load_workflow_payload(REPO_ROOT / ".github/workflows/sedna-sync-upstream.yml")
        jobs = payload.get("jobs") or {}
        sync_job = jobs.get("sync") or {}
        audit_job = jobs.get("audit") or {}

        self.assertEqual(payload.get("permissions"), {"contents": "read"})
        self.assertEqual(audit_job.get("needs"), "sync")
        audit_job_json = json.dumps(audit_job, sort_keys=True)
        self.assertNotIn("secrets.", audit_job_json)
        self.assertNotIn("SYNC_UPSTREAM_APP_TOKEN", audit_job_json)
        self.assertNotIn("SYNC_UPSTREAM_LEGACY_TOKEN", audit_job_json)
        self.assertIn("Generate upstream sync app token", json.dumps(sync_job, sort_keys=True))

    def test_rust_ci_full_fallback_sccache_writes_are_disabled_by_default(self) -> None:
        payload = load_workflow_payload(REPO_ROOT / ".github/workflows/rust-ci-full.yml")
        jobs = payload.get("jobs") or {}

        for job_name in ["lint_build"]:
            with self.subTest(job=job_name):
                job = jobs.get(job_name) or {}
                workflow_text = (REPO_ROOT / ".github/workflows/rust-ci-full.yml").read_text(
                    encoding="utf-8"
                )
                env = job.get("env") or {}
                self.assertEqual(env.get("SCCACHE_CACHE_SIZE"), "2G")
                self.assertEqual(env.get("SCCACHE_FALLBACK_CACHE_POLICY"), "restore-only")
                self.assertNotIn("ACTIONS_RUNTIME_TOKEN", workflow_text)
                self.assertNotIn("SCCACHE_GHA_ENABLED=true", workflow_text)

                save_step = next(
                    step
                    for step in job.get("steps") or []
                    if step.get("name") == "Save sccache cache (fallback)"
                )
                self.assertIn(
                    "steps.sccache_backend.outputs.policy == 'write-fallback'",
                    save_step.get("if") or "",
                )
                install_step = next(
                    step for step in job.get("steps") or [] if step.get("name") == "Install sccache"
                )
                self.assertNotIn("version", install_step.get("with") or {})
                configure_step = next(
                    step
                    for step in job.get("steps") or []
                    if step.get("name") == "Configure sccache backend"
                )
                self.assertIn(
                    "${{ github.workspace }}/.github/scripts/configure_sccache_backend.sh",
                    configure_step.get("run") or "",
                )

                for step_name in [
                    "Summarize clippy failure",
                    "Summarize nextest failures",
                ]:
                    matching_steps = [
                        step for step in job.get("steps") or [] if step.get("name") == step_name
                    ]
                    for step in matching_steps:
                        self.assertIn(
                            "${{ github.workspace }}/.github/scripts/summarize_rust_ci_full.py",
                            step.get("run") or "",
                        )

        archive_job = jobs.get("nextest_archive") or {}
        archive_env = archive_job.get("env") or {}
        self.assertEqual(archive_env.get("USE_SCCACHE"), "false")
        self.assertNotIn("SCCACHE_CACHE_SIZE", archive_env)
        archive_steps_json = json.dumps(archive_job.get("steps") or [], sort_keys=True)
        self.assertNotIn("RUSTC_WRAPPER=sccache", archive_steps_json)
        self.assertNotIn("Configure sccache backend", archive_steps_json)
        self.assertNotIn("cargo nextest run", archive_steps_json)

    def test_rust_ci_full_runs_after_successful_scheduled_rust_ci_only(self) -> None:
        payload = load_workflow_payload(REPO_ROOT / ".github/workflows/rust-ci-full.yml")
        trigger = payload.get("on") or {}
        jobs = payload.get("jobs") or {}
        schedule_gate = jobs.get("schedule_gate") or {}

        self.assertEqual(
            ((trigger.get("workflow_run") or {}).get("workflows") or []),
            ["rust-ci"],
        )
        self.assertNotIn("schedule", trigger)
        self.assertEqual(payload.get("permissions"), {"actions": "read", "contents": "read"})

        gate = schedule_gate.get("if") or ""
        self.assertIn("github.event.workflow_run.event == 'schedule'", gate)
        self.assertIn("github.event.workflow_run.conclusion == 'success'", gate)
        self.assertIn("github.event.workflow_run.head_branch == 'main'", gate)

        self.assertEqual((jobs.get("matrix_plan") or {}).get("needs"), "schedule_gate")
        self.assertIn(
            "needs.schedule_gate.outputs.should_run == 'true'",
            (jobs.get("matrix_plan") or {}).get("if") or "",
        )
        dedupe_step = next(
            step
            for step in schedule_gate.get("steps") or []
            if step.get("name") == "Check duplicate scheduled rust-ci-full success"
        )
        dedupe_run = dedupe_step.get("run") or ""
        self.assertIn("skip_duplicate_workflow_run.py", dedupe_run)
        self.assertIn("--workflow rust-ci-full.yml", dedupe_run)
        self.assertIn("github.event.workflow_run.head_sha", dedupe_run)

        result_gate = (jobs.get("results") or {}).get("if") or ""
        self.assertIn("always()", result_gate)
        self.assertIn("github.event.workflow_run.event == 'schedule'", result_gate)
        self.assertIn("github.event.workflow_run.conclusion == 'success'", result_gate)

    def test_rust_ci_schedule_reuses_equivalent_same_sha_success(self) -> None:
        payload = load_workflow_payload(REPO_ROOT / ".github/workflows/rust-ci.yml")
        self.assertEqual(
            payload.get("permissions"),
            {
                "actions": "read",
                "contents": "read",
                "checks": "read",
                "pull-requests": "read",
            },
        )
        jobs = payload.get("jobs") or {}
        changed = jobs.get("changed") or {}
        outputs = changed.get("outputs") or {}
        steps = changed.get("steps") or []

        self.assertEqual(
            outputs.get("scheduled_duplicate_skip"),
            "${{ steps.schedule_duplicate.outputs.should_skip || 'false' }}",
        )
        self.assertIn(
            "steps.scheduled_skip_plan.outputs.validation_mode",
            outputs.get("validation_mode") or "",
        )

        dedupe_step = next(
            step
            for step in steps
            if step.get("name") == "Check duplicate scheduled rust-ci success"
        )
        self.assertEqual(dedupe_step.get("if"), "${{ github.event_name == 'schedule' }}")
        dedupe_run = dedupe_step.get("run") or ""
        self.assertIn("skip_duplicate_workflow_run.py", dedupe_run)
        self.assertIn("--workflow rust-ci.yml", dedupe_run)
        self.assertIn("--head-sha \"${{ github.sha }}\"", dedupe_run)

        detect_step = next(
            step for step in steps if step.get("name") == "Detect changed paths and rust-ci mode"
        )
        self.assertIn(
            "steps.schedule_duplicate.outputs.should_skip != 'true'",
            detect_step.get("if") or "",
        )
        skip_step = next(
            step for step in steps if step.get("name") == "Emit duplicate scheduled skip plan"
        )
        self.assertEqual(
            skip_step.get("if"),
            "${{ steps.schedule_duplicate.outputs.should_skip == 'true' }}",
        )
        self.assertIn("validation_mode=scheduled_duplicate", skip_step.get("run") or "")

        results_run = (
            next(
                step
                for step in (jobs.get("results") or {}).get("steps") or []
                if step.get("name") == "Summarize"
            ).get("run")
            or ""
        )
        self.assertIn("scheduled_duplicate_skip", results_run)
        self.assertIn("Equivalent rust-ci run already passed", results_run)

    def test_rust_ci_argument_comment_lint_timeout_matches_lane_contract(self) -> None:
        rust_ci = load_workflow_payload(REPO_ROOT / ".github/workflows/rust-ci.yml")
        rust_ci_full = load_workflow_payload(REPO_ROOT / ".github/workflows/rust-ci-full.yml")

        plan_run = (
            (((rust_ci.get("jobs") or {}).get("matrix_plan") or {}).get("steps") or [])[0].get(
                "run"
            )
            or ""
        )
        self.assertIn('"timeout_minutes": 30', plan_run)

        rust_ci_full_job = (rust_ci_full.get("jobs") or {}).get(
            "argument_comment_lint_prebuilt"
        ) or {}
        self.assertEqual(rust_ci_full_job.get("timeout-minutes"), "240")

    def test_rust_ci_full_results_understands_archive_and_remote_test_jobs(self) -> None:
        payload = load_workflow_payload(REPO_ROOT / ".github/workflows/rust-ci-full.yml")
        jobs = payload.get("jobs") or {}
        results = jobs.get("results") or {}
        steps = results.get("steps") or []

        self.assertEqual(
            results.get("needs"),
            [
                "general",
                "cargo_shear",
                "schedule_gate",
                "matrix_plan",
                "argument_comment_lint_package",
                "argument_comment_lint_prebuilt",
                "lint_build",
                "nextest_archive",
                "tests",
                "remote_tests",
            ],
        )
        self.assertIn("remote_tests_matrix", (jobs.get("matrix_plan") or {}).get("outputs") or {})
        self.assertEqual((jobs.get("tests") or {}).get("needs"), ["matrix_plan", "nextest_archive"])
        self.assertEqual(
            (jobs.get("remote_tests") or {}).get("needs"), ["matrix_plan", "nextest_archive"]
        )

        download_step = next(
            step for step in steps if step.get("name") == "Download failure summaries"
        )
        self.assertEqual(
            download_step.get("uses"),
            "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c",
        )
        self.assertEqual((download_step.get("with") or {}).get("pattern"), "rust-ci-full-*-summary-*")
        self.assertEqual((download_step.get("with") or {}).get("merge-multiple"), "true")

        aggregate_step = next(
            step for step in steps if step.get("name") == "Build structured summary"
        )
        self.assertIn(
            "${{ github.workspace }}/.github/scripts/summarize_rust_ci_full.py\" aggregate",
            aggregate_step.get("run") or "",
        )
        verify_step = next(step for step in steps if step.get("name") == "Verify full CI result")
        verify_run = verify_step.get("run") or ""
        self.assertIn("needs.schedule_gate.outputs.should_skip", verify_run)
        self.assertIn("Equivalent rust-ci-full run already passed", verify_run)
        self.assertIn("require_success \"nextest_archive\"", verify_run)
        self.assertIn("require_success \"remote_tests\"", verify_run)

    def test_rust_ci_full_archive_test_and_results_jobs_do_not_receive_secrets(self) -> None:
        payload = load_workflow_payload(REPO_ROOT / ".github/workflows/rust-ci-full.yml")
        jobs = payload.get("jobs") or {}

        for job_name in [
            "schedule_gate",
            "lint_build",
            "nextest_archive",
            "tests",
            "remote_tests",
            "results",
        ]:
            with self.subTest(job=job_name):
                job = jobs.get(job_name) or {}
                self.assertNotIn("secrets", job)
                self.assertNotIn("secrets.", json.dumps(job, sort_keys=True))
                self.assertNotIn("ACTIONS_RUNTIME_TOKEN", json.dumps(job, sort_keys=True))
                self.assertNotIn("SCCACHE_GHA_ENABLED=true", json.dumps(job, sort_keys=True))

    def test_rust_ci_full_nextest_archive_is_reused_by_test_families(self) -> None:
        payload = load_workflow_payload(REPO_ROOT / ".github/workflows/rust-ci-full.yml")
        jobs = payload.get("jobs") or {}

        archive_env = (jobs.get("nextest_archive") or {}).get("env") or {}
        self.assertEqual(archive_env.get("CARGO_PROFILE_CI_TEST_DEBUG"), "0")
        self.assertEqual(archive_env.get("CARGO_PROFILE_CI_TEST_STRIP"), "symbols")

        archive_steps = (jobs.get("nextest_archive") or {}).get("steps") or []
        disk_reclaim_step = next(
            step for step in archive_steps if step.get("name") == "Reclaim runner disk headroom"
        )
        self.assertEqual(disk_reclaim_step.get("if"), "${{ runner.os == 'Linux' }}")
        self.assertIn("/usr/share/dotnet", disk_reclaim_step.get("run") or "")
        self.assertIn("6 GiB safety floor", disk_reclaim_step.get("run") or "")

        archive_run = next(
            step for step in archive_steps if step.get("name") == "Build nextest archive"
        ).get("run") or ""
        self.assertIn("cargo nextest archive", archive_run)
        self.assertIn("--archive-file", archive_run)
        self.assertNotIn("--all-features", archive_run)
        self.assertNotIn("cargo nextest run", archive_run)
        self.assertNotIn("tests", [step.get("name") for step in archive_steps])

        for job_name in ["tests", "remote_tests"]:
            with self.subTest(job=job_name):
                steps = (jobs.get(job_name) or {}).get("steps") or []
                install_step = next(
                    step
                    for step in steps
                    if step.get("name") == "Install Linux build dependencies"
                )
                self.assertIn("bubblewrap", install_step.get("run") or "")

                replay_disk_step = next(
                    step for step in steps if step.get("name") == "Reclaim runner disk headroom"
                )
                self.assertEqual(replay_disk_step.get("if"), "${{ runner.os == 'Linux' }}")
                self.assertIn("/usr/share/dotnet", replay_disk_step.get("run") or "")
                self.assertIn("6 GiB safety floor", replay_disk_step.get("run") or "")

                download_step = next(
                    step for step in steps if step.get("name") == "Download nextest archive"
                )
                self.assertEqual(
                    (download_step.get("with") or {}).get("name"),
                    "rust-ci-full-nextest-archive-${{ matrix.target }}-${{ matrix.profile }}",
                )
                run_step = next(
                    step
                    for step in steps
                    if step.get("name") in {"tests", "remote tests"}
                )
                self.assertIn("cargo nextest run", run_step.get("run") or "")
                self.assertIn("--archive-file", run_step.get("run") or "")

        remote_matrix = (
            (jobs.get("matrix_plan") or {})
            .get("outputs", {})
            .get("remote_tests_matrix", "")
        )
        self.assertEqual(
            remote_matrix,
            "${{ steps.plan.outputs.remote_tests_matrix }}",
        )
        plan_run = (
            ((jobs.get("matrix_plan") or {}).get("steps") or [])[0].get("run") or ""
        )
        self.assertNotIn('"filter"', plan_run)
        remote_run = next(
            step
            for step in (jobs.get("remote_tests") or {}).get("steps") or []
            if step.get("name") == "remote tests"
        ).get("run") or ""
        self.assertNotIn(" -E ", remote_run)
        remote_setup_run = next(
            step
            for step in (jobs.get("remote_tests") or {}).get("steps") or []
            if step.get("name") == "Set up remote test env (Docker)"
        ).get("run") or ""
        self.assertIn("CODEX_TEST_REMOTE_ENV_CARGO_TARGET_DIR", remote_setup_run)
        remote_cleanup_run = next(
            step
            for step in (jobs.get("remote_tests") or {}).get("steps") or []
            if step.get("name") == "Reclaim remote env build artifacts"
        ).get("run") or ""
        self.assertIn("20 GiB extraction safety floor", remote_cleanup_run)

    def test_rust_ci_full_summary_parser_extracts_compact_blockers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            nextest_log = root / "nextest.log"
            nextest_log.write_text(
                "\n".join(
                    [
                        "Starting 6 tests across 2 binaries (1 tests skipped)",
                        "        FAIL [   0.042s] (1/3) codex_core::remote_env::fails_cleanly",
                        "     TIMEOUT [  60.000s] (2/3) codex_core::remote_exec_server::hangs",
                        "   TRY 1 FAIL [   0.120s] codex_core::flaky_once",
                        "   TRY 2 PASS [   0.011s] codex_core::flaky_once",
                        "   TRY 1 FAIL [   0.220s] codex-core session::stable_failure",
                        "   TRY 2 FAIL [   0.230s] codex-core session::stable_failure",
                        "   TRY 1 TIMEOUT [  30.000s] codex-core remote::stable_timeout",
                        "   TRY 2 TIMEOUT [  30.001s] codex-core remote::stable_timeout",
                    ]
                ),
                encoding="utf-8",
            )
            clippy_log = root / "clippy.log"
            clippy_log.write_text(
                "\n".join(
                    [
                        "error: this expression creates a reference",
                        "  --> codex-rs/core/src/lib.rs:12:34",
                        "error: could not compile `codex-core` due to 1 previous error",
                    ]
                ),
                encoding="utf-8",
            )

            nextest = SUMMARIZE_RUST_CI_FULL.nextest_summary(nextest_log, "nextest-linux")
            clippy = SUMMARIZE_RUST_CI_FULL.clippy_summary(clippy_log, "clippy-linux")

        self.assertEqual(
            nextest,
            {
                "type": "nextest",
                "suite": "nextest-linux",
                "log_missing": False,
                "started": {"tests": 6, "binaries": 2, "skipped": 1},
                "failure_signal_count": 4,
                "unique_failure_count": 4,
                "status_counts": {"FAIL": 2, "TIMEOUT": 2},
                "failures": [
                    {
                        "status": "fail",
                        "duration": "0.042s",
                        "test": "codex_core::remote_env::fails_cleanly",
                    },
                    {
                        "status": "timeout",
                        "duration": "60.000s",
                        "test": "codex_core::remote_exec_server::hangs",
                    },
                    {
                        "status": "fail",
                        "duration": "0.230s",
                        "test": "codex-core session::stable_failure",
                    },
                    {
                        "status": "timeout",
                        "duration": "30.001s",
                        "test": "codex-core remote::stable_timeout",
                    },
                ],
                "truncated": False,
            },
        )
        self.assertEqual(
            clippy,
            {
                "type": "clippy",
                "suite": "clippy-linux",
                "log_missing": False,
                "error_count": 1,
                "errors": [
                    {
                        "message": "this expression creates a reference",
                        "location": "codex-rs/core/src/lib.rs:12:34",
                    }
                ],
                "truncated": False,
            },
        )

    def test_rust_ci_full_summary_aggregate_keeps_skips_non_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            summary_dir = root / "summaries"
            summary_dir.mkdir()
            (summary_dir / "nextest.json").write_text(
                json.dumps(
                    {
                        "type": "nextest",
                        "suite": "nextest-linux",
                        "failures": [{"status": "fail", "test": "a::test"}],
                        "unique_failure_count": 1,
                    }
                ),
                encoding="utf-8",
            )
            output = root / "summary.json"
            SUMMARIZE_RUST_CI_FULL.aggregate_summary(
                needs_json=json.dumps(
                    {
                        "general": {"result": "skipped"},
                        "tests": {"result": "failure"},
                        "remote_tests": {"result": "success"},
                    }
                ),
                summary_dir=summary_dir,
                checkout_ref="abc123",
                source_event="schedule",
                output=output,
            )
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(payload["checkout_ref"], "abc123")
        self.assertEqual(payload["source_event"], "schedule")
        self.assertEqual(
            payload["jobs"],
            {"general": "skipped", "remote_tests": "success", "tests": "failure"},
        )
        self.assertEqual(
            payload["primary_blockers"],
            [
                {"type": "job", "job": "tests", "result": "failure"},
                {
                    "type": "nextest",
                    "suite": "nextest-linux",
                    "status": "fail",
                    "test": "a::test",
                    "unique_failure_count": 1,
                },
            ],
        )

    def test_lane_summary_records_cache_telemetry_without_raw_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "summary.json"
            subprocess.run(
                [
                    "python3",
                    str(SCRIPTS_DIR / "write_lane_summary.py"),
                    "--lane-id",
                    "codex.example",
                    "--summary-title",
                    "example",
                    "--run-command",
                    "cargo test --locked",
                    "--cache-policy",
                    "restore-only",
                    "--cache-backend",
                    "fallback",
                    "--sccache-restore-mode",
                    "restore-key-or-miss",
                    "--output",
                    str(output),
                ],
                check=True,
            )

            summary = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(summary["script_path"], "legacy-run-command")
        self.assertEqual(summary["cache_policy"], "restore-only")
        self.assertEqual(summary["cache_backend"], "fallback")
        self.assertEqual(summary["sccache_restore_mode"], "restore-key-or-miss")
        self.assertNotIn("run_command", summary)

    def test_lane_summary_does_not_treat_panic_crate_names_as_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            log = root / "lane.log"
            output = root / "summary.json"
            log.write_text(
                "\n".join(
                    [
                        "Checking sentry-panic v0.46.2",
                        "error[E0277]: a real compiler failure",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            subprocess.run(
                [
                    "python3",
                    str(SCRIPTS_DIR / "write_lane_summary.py"),
                    "--lane-id",
                    "codex.example",
                    "--summary-title",
                    "example",
                    "--outcome",
                    "failure",
                    "--log-file",
                    str(log),
                    "--output",
                    str(output),
                ],
                check=True,
            )

            summary = json.loads(output.read_text(encoding="utf-8"))

        self.assertNotIn("error_lines", summary)
        self.assertEqual(summary["primary_signal"], "error[E0277]: a real compiler failure")

    def test_lane_summary_records_script_metadata_and_cache_telemetry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "summary.json"
            subprocess.run(
                [
                    "python3",
                    str(SCRIPTS_DIR / "write_lane_summary.py"),
                    "--lane-id",
                    "codex.example",
                    "--summary-title",
                    "example",
                    "--script-path",
                    ".github/scripts/validation-lanes/run-just-recipe.sh",
                    "--script-args-json",
                    '["blocking-waits-core-targeted"]',
                    "--cache-policy",
                    "restore-only",
                    "--cache-backend",
                    "gha",
                    "--sccache-restore-mode",
                    "not-applicable",
                    "--nextest-archive-artifact-name",
                    "validation-lab-nextest-core-carry-pilot",
                    "--nextest-archive-file-name",
                    "codex-core-carry-nextest.tar.zst",
                    "--nextest-archive-mode",
                    "downloaded",
                    "--output",
                    str(output),
                ],
                check=True,
            )

            summary = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(
            summary["script_path"], ".github/scripts/validation-lanes/run-just-recipe.sh"
        )
        self.assertEqual(summary["script_args"], ["blocking-waits-core-targeted"])
        self.assertEqual(summary["cache_policy"], "restore-only")
        self.assertEqual(summary["cache_backend"], "gha")
        self.assertEqual(summary["sccache_restore_mode"], "not-applicable")
        self.assertEqual(
            summary["nextest_archive_artifact_name"],
            "validation-lab-nextest-core-carry-pilot",
        )
        self.assertEqual(summary["nextest_archive_file_name"], "codex-core-carry-nextest.tar.zst")
        self.assertEqual(summary["nextest_archive_mode"], "downloaded")
        self.assertNotIn("run_command", summary)

    def test_lane_summary_detects_server_notification_schema_fixture_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            log = root / "lane.log"
            output = root / "summary.json"
            log.write_text(
                "\n".join(
                    [
                        "thread 'json_schema_fixtures_match_generated' panicked at tests/schema_fixtures.rs:98:9:",
                        "Vendored json app-server schema fixture ServerNotification.json differs from generated output. Run `just write-app-server-schema` to overwrite with your changes.",
                        "--- fixture",
                        "+++ generated",
                        '-        "threadGoalUpdated": {',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            subprocess.run(
                [
                    "python3",
                    str(SCRIPTS_DIR / "write_lane_summary.py"),
                    "--lane-id",
                    "codex.app-server-protocol-test",
                    "--summary-title",
                    "app-server protocol",
                    "--script-path",
                    ".github/scripts/validation-lanes/app-server-protocol-test.sh",
                    "--outcome",
                    "failure",
                    "--log-file",
                    str(log),
                    "--output",
                    str(output),
                ],
                check=True,
            )

            summary = json.loads(output.read_text(encoding="utf-8"))

        drift = summary["schema_fixture_drift"]
        self.assertEqual(drift["kind"], "app_server_schema_fixture_drift")
        self.assertEqual(drift["fixture_family"], "json")
        self.assertEqual(drift["fixture_path"], "ServerNotification.json")
        self.assertEqual(drift["direction"], "vendored_differs_from_generated")
        self.assertEqual(drift["recommended_fix"], "just write-app-server-schema")
        self.assertEqual(
            drift["recommended_proof"],
            {"profile": "targeted", "lane_ids": ["codex.app-server-protocol-test"]},
        )
        self.assertEqual(
            summary["primary_signal"],
            "json app-server schema fixture ServerNotification.json differs from generated output",
        )

    def test_aggregate_summary_surfaces_schema_fixture_drift_in_candidates(self) -> None:
        drift = {
            "kind": "app_server_schema_fixture_drift",
            "fixture_family": "json",
            "fixture_path": "ServerNotification.json",
            "direction": "vendored_differs_from_generated",
            "recommended_proof": {
                "profile": "targeted",
                "lane_ids": ["codex.app-server-protocol-test"],
            },
            "summary": "json app-server schema fixture ServerNotification.json differs from generated output",
        }
        results = AGGREGATE_VALIDATION_SUMMARY.build_results(
            planned_matrix=[
                {
                    "lane_id": "codex.app-server-protocol-test",
                    "setup_class": "rust_minimal",
                    "summary_family": "app-server-protocol",
                    "frontier_role": "sentinel",
                    "status_class": "active",
                }
            ],
            selected_lane_ids=["codex.app-server-protocol-test"],
            actual_by_lane={
                "codex.app-server-protocol-test": {
                    "lane_id": "codex.app-server-protocol-test",
                    "outcome": "failure",
                    "exit_code": 101,
                    "schema_fixture_drift": drift,
                }
            },
            smoke_gate_result="skipped",
            setup_class_results={"rust_minimal": "failure"},
            matrix_fail_fast=False,
        )
        setup_rows = AGGREGATE_VALIDATION_SUMMARY.setup_class_rows(
            results, {"rust_minimal": "failure"}
        )
        primary, secondary = AGGREGATE_VALIDATION_SUMMARY.derive_primary_and_secondary(
            results, setup_rows
        )
        queue = [*primary, *secondary]
        candidates = []
        for item in queue:
            candidates.append(
                {
                    "kind": "lane",
                    "lane_id": item["lane_id"],
                    "signal": item.get("signal", ""),
                    "schema_fixture_drift": item.get("schema_fixture_drift") or {},
                }
            )

        self.assertEqual(primary[0]["signal"], drift["summary"])
        self.assertEqual(primary[0]["schema_fixture_drift"], drift)
        self.assertEqual(candidates[0]["schema_fixture_drift"], drift)

    def test_validation_lab_frontier_all_widens_to_all_active_non_explicit_lanes(self) -> None:
        payload = run_script(
            SCRIPTS_DIR / "resolve_validation_plan.py",
            "lab",
            "--profile",
            "frontier",
            "--lane-set",
            "all",
            "--lanes",
            "",
            "--artifact-build",
            "false",
        )

        selected_lane_ids = [lane["lane_id"] for lane in payload["selected_matrix"]["include"]]
        self.assertIn("codex.downstream-docs-check", selected_lane_ids)
        self.assertIn("codex.workflow-ci-sanity", selected_lane_ids)
        self.assertIn("sedna.release-linux-smoke", selected_lane_ids)
        self.assertIn("codex.tui-config-refresh-session-targeted", selected_lane_ids)
        self.assertIn("codex.spawn-agent-description-model-surface-targeted", selected_lane_ids)
        self.assertIn("codex.core-multi-agent-orchestration-targeted", selected_lane_ids)
        self.assertNotIn("codex.tui-agent-picker-model-surface-targeted", selected_lane_ids)
        self.assertEqual(payload["planned_job_count"], 41)
        self.assertEqual(payload["selected_workflow_lane_count"], 6)
        self.assertEqual(payload["selected_node_lane_count"], 2)
        self.assertEqual(payload["selected_rust_minimal_lane_count"], 1)
        self.assertEqual(payload["selected_rust_minimal_batch_count"], 13)
        self.assertEqual(payload["selected_rust_integration_lane_count"], 6)
        self.assertEqual(payload["selected_rust_integration_batch_count"], 12)
        self.assertEqual(payload["selected_release_lane_count"], 1)
        self.assertEqual(payload["workflow_max_parallel"], "6")
        self.assertEqual(payload["node_max_parallel"], "2")
        self.assertEqual(payload["rust_minimal_max_parallel"], "24")
        self.assertEqual(payload["rust_integration_max_parallel"], "25")
        self.assertEqual(payload["release_max_parallel"], "1")

    def test_validation_lab_frontier_all_can_include_explicit_only_lanes(self) -> None:
        payload = run_script(
            SCRIPTS_DIR / "resolve_validation_plan.py",
            "lab",
            "--profile",
            "frontier",
            "--lane-set",
            "all",
            "--lanes",
            "",
            "--artifact-build",
            "false",
            "--include-explicit-lanes",
            "true",
        )

        selected_lane_ids = [lane["lane_id"] for lane in payload["selected_matrix"]["include"]]
        self.assertIn("codex.tui-agent-picker-model-surface-targeted", selected_lane_ids)
        self.assertIn("codex.argument-comment-lint", selected_lane_ids)
        self.assertIn("downstream-ledger-seam", selected_lane_ids)
        self.assertIn("codex.core-multi-agent-orchestration-targeted", selected_lane_ids)
        self.assertEqual(payload["planned_job_count"], 47)
        self.assertEqual(payload["selected_workflow_lane_count"], 8)
        self.assertEqual(payload["selected_node_lane_count"], 2)
        self.assertEqual(payload["selected_rust_minimal_lane_count"], 1)
        self.assertEqual(payload["selected_rust_minimal_batch_count"], 15)
        self.assertEqual(payload["selected_rust_integration_lane_count"], 7)
        self.assertEqual(payload["selected_rust_integration_batch_count"], 13)
        self.assertEqual(payload["selected_release_lane_count"], 1)
        self.assertEqual(payload["rust_minimal_max_parallel"], "29")
        self.assertEqual(payload["rust_integration_max_parallel"], "27")

    def test_validation_lab_frontier_all_excludes_smoke_gate_lanes_by_metadata(self) -> None:
        catalog = {
            "lanes": [
                {
                    "lane_id": "codex.synthetic-runtime-gate",
                    "groups": ["core"],
                    "status_class": "active",
                    "setup_class": "rust_integration",
                    "frontier_role": "sentinel",
                    "summary_family": "synthetic-gate",
                    "cost_class": "high",
                    "working_directory": ".",
                    "script_path": ".github/scripts/validation-lanes/run-just-recipe.sh",
                    "script_args": ["synthetic-runtime-gate"],
                    "needs_just": True,
                    "needs_node": False,
                    "needs_nextest": False,
                    "needs_linux_build_deps": True,
                    "needs_dotslash": True,
                    "needs_sccache": True,
                    "smoke_gate_only": True,
                    "smoke_gate_kinds": ["runtime"],
                },
                {
                    "lane_id": "codex.synthetic-real-lane",
                    "groups": ["core"],
                    "status_class": "active",
                    "setup_class": "rust_minimal",
                    "frontier_role": "sentinel",
                    "summary_family": "synthetic-real-lane",
                    "cost_class": "medium",
                    "working_directory": ".",
                    "script_path": ".github/scripts/validation-lanes/run-just-recipe.sh",
                    "script_args": ["synthetic-real-lane"],
                    "needs_just": True,
                    "needs_node": False,
                    "needs_nextest": False,
                    "needs_linux_build_deps": False,
                    "needs_dotslash": False,
                    "needs_sccache": False,
                },
            ]
        }

        selected = RESOLVE_VALIDATION_PLAN.select_frontier_all(catalog)

        self.assertEqual(
            [lane["lane_id"] for lane in selected],
            ["codex.synthetic-real-lane"],
        )

    def test_frontier_helper_rejects_boolean_checkout_fetch_depth(self) -> None:
        catalog = {
            "lanes": [
                {
                    "lane_id": "codex.synthetic-real-lane",
                    "groups": ["core"],
                    "status_class": "active",
                    "setup_class": "rust_minimal",
                    "frontier_role": "sentinel",
                    "summary_family": "synthetic-real-lane",
                    "cost_class": "medium",
                    "checkout_fetch_depth": True,
                    "working_directory": ".",
                    "script_path": ".github/scripts/validation-lanes/run-just-recipe.sh",
                    "script_args": ["synthetic-real-lane"],
                    "needs_just": True,
                    "needs_node": False,
                    "needs_nextest": False,
                    "needs_linux_build_deps": False,
                    "needs_dotslash": False,
                    "needs_sccache": False,
                }
            ]
        }

        with self.assertRaisesRegex(
            SystemExit,
            "must set checkout_fetch_depth to a non-negative integer",
        ):
            RESOLVE_VALIDATION_PLAN.select_frontier_all(catalog)

    def test_heavy_plan_workflow_dispatch_all_uses_frontier_harvest_policy(self) -> None:
        payload = run_script(
            SCRIPTS_DIR / "resolve_validation_plan.py",
            "heavy",
            "--event-name",
            "workflow_dispatch",
            "--requested-lane",
            "all",
            "--run-all-lanes",
            "true",
            "--run-core-family",
            "false",
            "--run-attestation-family",
            "false",
            "--run-workflow-family",
            "false",
            "--run-ui-protocol-family",
            "false",
            "--run-docs-family",
            "false",
            "--changed-files-json",
            "[]",
        )

        self.assertEqual(payload["matrix_fail_fast"], "false")
        self.assertEqual(payload["continue_after_smoke_failure"], "true")
        self.assertEqual(payload["eager_release_lanes"], "true")
        self.assertEqual(payload["workflow_max_parallel"], "8")
        self.assertEqual(payload["node_max_parallel"], "2")
        self.assertEqual(payload["rust_minimal_max_parallel"], "20")
        self.assertEqual(payload["rust_integration_max_parallel"], "8")
        self.assertEqual(payload["release_max_parallel"], "1")
        planned_lane_ids = [lane["lane_id"] for lane in payload["planned_matrix"]["include"]]
        selected_lane_ids = payload["selected_lane_ids"]
        self.assertEqual(
            planned_lane_ids[:6],
            [
                "core-compile-smoke",
                "core-carry-core-smoke",
                "core-carry-ui-smoke",
                "core-ledger-smoke",
                "core-runtime-surface-smoke",
                "sedna.release-linux-smoke",
            ],
        )
        self.assertEqual(planned_lane_ids[6:], selected_lane_ids)
        self.assertIn("codex.core-startup-sync-targeted", selected_lane_ids)
        self.assertIn("codex.downstream-docs-check", selected_lane_ids)
        self.assertNotIn("codex.downstream-divergence-audit", selected_lane_ids)
        self.assertNotIn("sedna.release-linux-smoke", selected_lane_ids)
        self.assertNotIn("codex.nextest-archive-core-carry-pilot", selected_lane_ids)

    def test_heavy_plan_ci_heavy_pr_uses_frontier_harvest_policy(self) -> None:
        payload = run_script(
            SCRIPTS_DIR / "resolve_validation_plan.py",
            "heavy",
            "--event-name",
            "pull_request",
            "--requested-lane",
            "",
            "--run-all-lanes",
            "true",
            "--run-core-family",
            "false",
            "--run-attestation-family",
            "false",
            "--run-workflow-family",
            "false",
            "--run-ui-protocol-family",
            "false",
            "--run-docs-family",
            "false",
            "--changed-files-json",
            "[]",
        )

        self.assertEqual(payload["matrix_fail_fast"], "false")
        self.assertEqual(payload["continue_after_smoke_failure"], "true")
        self.assertEqual(payload["eager_release_lanes"], "true")
        self.assertEqual(payload["rust_integration_max_parallel"], "8")
        self.assertEqual(payload["release_max_parallel"], "1")
        self.assertEqual(payload["smoke_release_lane_count"], 1)
        self.assertEqual(payload["selected_release_lane_count"], 0)
        self.assertNotIn(
            "codex.nextest-archive-core-carry-pilot",
            payload["selected_lane_ids"],
        )
        self.assertNotIn("sedna.release-linux-smoke", payload["selected_lane_ids"])

    def test_nextest_archive_pilot_is_explicit_only(self) -> None:
        payload = run_script(
            SCRIPTS_DIR / "resolve_validation_plan.py",
            "heavy",
            "--event-name",
            "workflow_dispatch",
            "--requested-lane",
            "codex.nextest-archive-core-carry-pilot",
            "--run-all-lanes",
            "true",
            "--run-core-family",
            "false",
            "--run-attestation-family",
            "false",
            "--run-workflow-family",
            "false",
            "--run-ui-protocol-family",
            "false",
            "--run-docs-family",
            "false",
            "--changed-files-json",
            "[]",
        )

        self.assertEqual(payload["run_smoke_gate"], "false")
        self.assertEqual(payload["selected_lane_ids"], ["codex.nextest-archive-core-carry-pilot"])
        self.assertEqual(payload["matrix_fail_fast"], "true")
        self.assertEqual(payload["eager_release_lanes"], "false")

    def test_validation_lab_explicit_nextest_archive_pilot_splits_archive_matrices(
        self,
    ) -> None:
        payload = run_script(
            SCRIPTS_DIR / "resolve_validation_plan.py",
            "lab",
            "--profile",
            "targeted",
            "--lane-set",
            "core-carry",
            "--fanout-tier",
            "enterprise",
            "--lanes",
            "codex.nextest-archive-core-carry-pilot",
            "--rust-batching",
            "auto",
            "--artifact-build",
            "false",
            "--include-explicit-lanes",
            "true",
        )

        self.assertEqual(payload["selected_lane_ids"], ["codex.nextest-archive-core-carry-pilot"])
        self.assertEqual(payload["selected_rust_integration_lane_count"], 0)
        self.assertEqual(payload["selected_rust_integration_batch_count"], 0)
        self.assertEqual(payload["selected_nextest_archive_count"], 1)
        self.assertEqual(payload["selected_rust_integration_archive_lane_count"], 1)
        self.assertEqual(payload["planned_job_count"], 2)

        archive = payload["selected_nextest_archive_matrix"]["include"][0]
        self.assertEqual(archive["archive_cohort"], "core-carry-pilot")
        self.assertEqual(archive["artifact_name"], "validation-lab-nextest-core-carry-pilot")
        self.assertEqual(archive["archive_file_name"], "codex-core-carry-nextest.tar.zst")
        self.assertEqual(archive["lane_ids"], ["codex.nextest-archive-core-carry-pilot"])

        archive_lane = payload["selected_rust_integration_archive_matrix"]["include"][0]
        self.assertTrue(archive_lane["uses_nextest_archive"])
        self.assertEqual(
            archive_lane["nextest_archive_artifact_name"],
            "validation-lab-nextest-core-carry-pilot",
        )

    def test_validation_lab_frontier_does_not_silently_select_archive_pilot(self) -> None:
        payload = run_script(
            SCRIPTS_DIR / "resolve_validation_plan.py",
            "lab",
            "--profile",
            "frontier",
            "--lane-set",
            "core-carry",
            "--fanout-tier",
            "enterprise",
            "--lanes",
            "",
            "--rust-batching",
            "auto",
            "--artifact-build",
            "false",
            "--include-explicit-lanes",
            "true",
        )

        self.assertNotIn("codex.nextest-archive-core-carry-pilot", payload["selected_lane_ids"])
        self.assertEqual(payload["selected_nextest_archive_count"], 0)
        self.assertEqual(payload["selected_rust_integration_archive_lane_count"], 0)
        self.assertGreater(len(payload["selected_lane_ids"]), 0)

    def test_sedna_heavy_manual_harvest_jobs_follow_metadata_fail_fast(self) -> None:
        payload = load_workflow_payload(REPO_ROOT / ".github/workflows/sedna-heavy-tests.yml")
        jobs = payload.get("jobs") or {}

        metadata_outputs = (jobs.get("metadata") or {}).get("outputs") or {}
        self.assertEqual(metadata_outputs.get("display_ref"), "${{ steps.meta.outputs.display_ref }}")
        self.assertEqual(
            metadata_outputs.get("event_policy"),
            "${{ steps.meta.outputs.event_policy }}",
        )
        self.assertEqual(metadata_outputs.get("checkout_sha"), "${{ steps.meta.outputs.checkout_sha }}")
        self.assertEqual(
            metadata_outputs.get("planned_matrix"),
            "${{ steps.meta.outputs.planned_matrix }}",
        )
        self.assertEqual(
            metadata_outputs.get("selected_lane_ids"),
            "${{ steps.meta.outputs.selected_lane_ids }}",
        )
        self.assertEqual(
            metadata_outputs.get("eager_release_lanes"),
            "${{ steps.meta.outputs.eager_release_lanes }}",
        )
        self.assertEqual(
            ((jobs.get("smoke_rust_integration_lanes") or {}).get("strategy") or {}).get(
                "fail-fast"
            ),
            "${{ fromJson(needs.metadata.outputs.matrix_fail_fast) }}",
        )
        self.assertEqual(
            ((jobs.get("rust_integration_lanes") or {}).get("strategy") or {}).get(
                "fail-fast"
            ),
            "${{ fromJson(needs.metadata.outputs.matrix_fail_fast) }}",
        )
        rust_if = (jobs.get("rust_integration_lanes") or {}).get("if") or ""
        self.assertIn("needs.metadata.outputs.continue_after_smoke_failure == 'true'", rust_if)
        release_eager = jobs.get("release_lanes_eager") or {}
        self.assertEqual(release_eager.get("needs"), ["metadata"])
        self.assertIn(
            "needs.metadata.outputs.eager_release_lanes == 'true'",
            release_eager.get("if") or "",
        )
        release_if = (jobs.get("release_lanes") or {}).get("if") or ""
        self.assertIn("needs.metadata.outputs.eager_release_lanes != 'true'", release_if)

    def test_sedna_heavy_pr_triggers_keep_ready_for_review(self) -> None:
        trigger_types = parse_pull_request_types(
            REPO_ROOT / ".github/workflows/sedna-heavy-tests.yml"
        )
        self.assertEqual(
            trigger_types,
            ["opened", "synchronize", "reopened", "ready_for_review", "labeled"],
        )

    def test_sedna_heavy_is_advisory_and_does_not_run_in_merge_queue(self) -> None:
        payload = load_workflow_payload(REPO_ROOT / ".github/workflows/sedna-heavy-tests.yml")
        self.assertNotIn("merge_group", payload.get("on") or {})

    def test_sedna_heavy_metadata_skips_draft_pr_churn_without_ci_heavy(self) -> None:
        payload = load_workflow_payload(REPO_ROOT / ".github/workflows/sedna-heavy-tests.yml")
        metadata_if = ((payload.get("jobs") or {}).get("metadata") or {}).get("if") or ""

        self.assertIn("github.event.pull_request.draft == false", metadata_if)
        self.assertIn("github.event.label.name == 'ci:heavy'", metadata_if)

    def test_sedna_heavy_workflow_dispatch_concurrency_keys_on_lane(self) -> None:
        payload = load_workflow_payload(REPO_ROOT / ".github/workflows/sedna-heavy-tests.yml")
        concurrency = payload.get("concurrency") or {}
        group = concurrency.get("group") or ""

        self.assertIn("inputs.lane || 'all'", group)
        self.assertIn("github.event.pull_request.number", group)

    def test_sedna_heavy_metadata_exposes_planner_fingerprint_and_dedupe_reason(self) -> None:
        payload = load_workflow_payload(REPO_ROOT / ".github/workflows/sedna-heavy-tests.yml")
        metadata_outputs = (((payload.get("jobs") or {}).get("metadata") or {}).get("outputs") or {})
        metadata_steps = (((payload.get("jobs") or {}).get("metadata") or {}).get("steps") or [])
        metadata_run = next(
            step for step in metadata_steps if step.get("name") == "Compute checkout ref"
        ).get("run") or ""

        self.assertEqual(
            metadata_outputs.get("planner_fingerprint"),
            "${{ steps.meta.outputs.planner_fingerprint }}",
        )
        self.assertEqual(
            metadata_outputs.get("dedupe_reason"),
            "${{ steps.meta.outputs.dedupe_reason }}",
        )
        self.assertEqual(
            metadata_outputs.get("event_policy"),
            "${{ steps.meta.outputs.event_policy }}",
        )
        self.assertIn('event_policy="pull_request_exact_head_lane_fingerprint"', metadata_run)
        self.assertIn('event_policy="workflow_dispatch_exact_head_manual"', metadata_run)
        self.assertIn("unsupported event for sedna-heavy-tests checkout identity", metadata_run)
        self.assertIn(".ci_proof_v1.schema_version == \"ci-proof-v1\"", metadata_run)
        self.assertIn(".ci_proof_v1.planner_fingerprint == $planner", metadata_run)
        self.assertIn(".ci_proof_v1.conclusion == \"success\"", metadata_run)
    def test_sedna_heavy_summary_job_aggregates_lane_artifacts(self) -> None:
        payload = load_workflow_payload(REPO_ROOT / ".github/workflows/sedna-heavy-tests.yml")
        jobs = payload.get("jobs") or {}
        summary = jobs.get("summary") or {}

        self.assertEqual(
            summary.get("needs"),
            [
                "metadata",
                "smoke_workflow_lanes",
                "smoke_node_lanes",
                "smoke_rust_minimal_lanes",
                "smoke_rust_integration_lanes",
                "smoke_release_lanes",
                "workflow_lanes",
                "node_lanes",
                "rust_minimal_lanes",
                "rust_minimal_batches",
                "rust_integration_lanes",
                "rust_integration_batches",
                "release_lanes_eager",
                "release_lanes",
            ],
        )
        summary_if = summary.get("if") or ""
        self.assertIn("always()", summary_if)
        self.assertIn("needs.metadata.result == 'success'", summary_if)
        self.assertEqual(summary.get("runs-on"), "ubuntu-24.04")

        steps = summary.get("steps") or []
        self.assertEqual((summary.get("permissions") or {}).get("actions"), "read")
        uses_steps = [step.get("uses") for step in steps]
        self.assertIn("actions/checkout@v7", uses_steps)
        self.assertIn("actions/download-artifact@v8", uses_steps)
        self.assertIn("actions/upload-artifact@v7", uses_steps)
        self.assertTrue(
            any(step.get("name") == "Record Actions cache occupancy" for step in steps)
        )
        report_step = next(
            (
                step
                for step in steps
                if "aggregate_validation_summary.py" in (step.get("run") or "")
            ),
            {},
        )
        self.assertIn(
            "aggregate_validation_summary.py",
            report_step.get("run") or "",
        )
        self.assertIn(
            '--planned-matrix-json \'${{ needs.metadata.outputs.planned_matrix }}\'',
            report_step.get("run") or "",
        )
        self.assertIn(
            "--cache-occupancy-json",
            report_step.get("run") or "",
        )
        self.assertIn(
            '--head-sha "${{ needs.metadata.outputs.checkout_sha }}"',
            report_step.get("run") or "",
        )
        self.assertIn(
            '--latest-head-sha "${{ needs.metadata.outputs.checkout_sha }}"',
            report_step.get("run") or "",
        )
        self.assertIn(
            '--workflow-file "sedna-heavy-tests.yml"',
            report_step.get("run") or "",
        )
        self.assertIn(
            '--event-policy "${{ needs.metadata.outputs.event_policy }}"',
            report_step.get("run") or "",
        )
        self.assertIn(
            '--planner-fingerprint "${{ needs.metadata.outputs.planner_fingerprint }}"',
            (steps[3] or {}).get("run") or "",
        )
        self.assertIn(
            '--workflow-result "${WORKFLOW_RESULT}"',
            report_step.get("run") or "",
        )
        self.assertIn(
            '--rust-minimal-result "${rust_minimal_result}"',
            report_step.get("run") or "",
        )
        self.assertIn(
            '--rust-integration-result "${rust_integration_result}"',
            report_step.get("run") or "",
        )

    def test_blocking_ci_uses_documented_merge_group_trigger(self) -> None:
        payload = load_workflow_payload(REPO_ROOT / ".github/workflows/blocking-ci.yml")
        self.assertEqual(
            (payload.get("on") or {}).get("merge_group"),
            {"types": ["checks_requested"]},
        )

    def test_blocking_ci_scope_routes_pull_requests_and_merge_groups(self) -> None:
        payload = load_workflow_payload(REPO_ROOT / ".github/workflows/blocking-ci.yml")
        scope_job = (payload.get("jobs") or {}).get("scope") or {}
        scope_steps = scope_job.get("steps") or []
        checkout_step = next(
            step
            for step in scope_steps
            if step.get("name") == "Checkout PR head for path classification"
        )
        self.assertEqual(
            checkout_step.get("if"),
            "${{ github.event_name == 'pull_request' || github.event_name == 'merge_group' }}",
        )
        self.assertEqual(
            checkout_step.get("with") or {},
            {
                "ref": "${{ github.event_name == 'pull_request' && github.event.pull_request.head.sha || github.sha }}",
                "fetch-depth": "1",
                "persist-credentials": "false",
            },
        )
        classify_step = next(
            step for step in scope_steps if step.get("name") == "Classify blocking CI scope"
        )
        self.assertEqual(
            classify_step.get("env") or {},
            {
                "BASE_SHA": "${{ github.event.pull_request.base.sha || github.event.merge_group.base_sha || '' }}",
                "HEAD_SHA": "${{ github.event.pull_request.head.sha || github.sha }}",
            },
        )
        classify_run = classify_step.get("run") or ""
        self.assertIn(
            "if [[ '${{ github.event_name }}' != 'pull_request' && '${{ github.event_name }}' != 'merge_group' ]]; then",
            classify_run,
        )
        self.assertIn("classify_ci_paths.py --base-sha", classify_run)
        self.assertIn("Unable to fetch PR base; running full blocking CI.", classify_run)
        self.assertIn("Unable to classify PR paths; running full blocking CI.", classify_run)

    def test_blocking_ci_clippy_required_lane_is_path_aware_and_fail_closed(self) -> None:
        payload = load_workflow_payload(REPO_ROOT / ".github/workflows/blocking-ci.yml")
        triggers = payload.get("on") or {}
        self.assertIn("pull_request", triggers)
        self.assertEqual(
            triggers.get("merge_group"),
            {"types": ["checks_requested"]},
        )
        jobs = payload.get("jobs") or {}
        scope = jobs.get("scope") or {}
        outputs = scope.get("outputs") or {}
        self.assertEqual(outputs.get("clippy"), "${{ steps.scope.outputs.clippy }}")
        scope_run = next(
            step for step in scope.get("steps") or [] if step.get("name") == "Classify blocking CI scope"
        ).get("run") or ""
        self.assertIn('echo "clippy=true"', scope_run)
        self.assertIn('"clippy",', scope_run)
        self.assertIn("if ! plan=", scope_run)

        clippy = jobs.get("clippy-required") or {}
        self.assertEqual(clippy.get("name"), "Linux full Clippy")
        self.assertEqual(clippy.get("needs"), "scope")
        self.assertEqual(clippy.get("if"), "${{ always() }}")
        steps = clippy.get("steps") or []
        verify = next(step for step in steps if step.get("name") == "Verify Clippy scope classification")
        verify_run = verify.get("run") or ""
        self.assertIn('[[ "${SCOPE_RESULT}" == "success" ]]', verify_run)
        self.assertIn("Unknown Clippy scope classification", verify_run)
        command = next(
            step for step in steps
            if step.get("name") == "cargo clippy --target x86_64-unknown-linux-gnu --all-features --tests --profile dev -- -D warnings"
        )
        self.assertEqual(
            command.get("run"),
            "cargo clippy --target x86_64-unknown-linux-gnu --all-features --tests --profile dev -- -D warnings",
        )
        self.assertEqual(
            command.get("if"),
            "${{ needs.scope.outputs.clippy == 'true' }}",
        )
        required = jobs.get("required") or {}
        self.assertIn("clippy-required", required.get("needs") or [])

    def test_merge_group_concurrency_is_sha_scoped_and_not_cancelled(self) -> None:
        merge_group_workflows = []
        for workflow_path in sorted((REPO_ROOT / ".github/workflows").glob("*.y*ml")):
            payload = load_workflow_payload(workflow_path)
            if "merge_group" not in (payload.get("on") or {}):
                continue
            merge_group_workflows.append(workflow_path.name)
            workflow_name = workflow_path.name
            with self.subTest(workflow=workflow_name):
                concurrency = payload.get("concurrency") or {}
                group = str(concurrency.get("group") or "")
                self.assertIn("concurrency-group::${{ github.workflow }}::", group)
                self.assertIn("github.event_name == 'merge_group'", group)
                self.assertIn("format('merge-group-{0}', github.sha)", group)
                self.assertIn("format('pr-{0}', github.event.pull_request.number)", group)
                self.assertIn("format('push-{0}', github.sha)", group)
                self.assertIn("format('{0}-{1}', github.event_name, github.run_id)", group)
                cancel = str(concurrency.get("cancel-in-progress") or "")
                self.assertEqual(cancel, "${{ github.event_name == 'pull_request' }}")

        self.assertEqual(
            merge_group_workflows,
            [
                "blocking-ci.yml",
                "codeql.yml",
                "osv-scanner.yml",
                "runner-label-policy.yml",
            ],
        )

        bazel = load_workflow_payload(REPO_ROOT / ".github/workflows/bazel.yml")
        self.assertNotIn(
            "concurrency",
            bazel,
            "Bazel is always admitted through blocking-ci, whose caller-level key owns queue isolation.",
        )

    def test_blob_size_policy_uses_queue_base_for_merge_groups(self) -> None:
        payload = load_workflow_payload(REPO_ROOT / ".github/workflows/blob-size-policy.yml")
        check_steps = ((payload.get("jobs") or {}).get("check") or {}).get("steps") or []
        range_step = next(
            step for step in check_steps if step.get("name") == "Determine comparison range"
        )
        run_script = range_step.get("run") or ""
        self.assertIn(
            'if [[ "${{ github.event_name }}" == "merge_group" ]]; then',
            run_script,
        )
        self.assertIn("github.event.merge_group.base_sha", run_script)
        self.assertIn("head='${{ github.sha }}'", run_script)


class BazelCiModeScriptTests(unittest.TestCase):
    def test_docs_only_mode_requires_a_complete_nonempty_docs_file_list(self) -> None:
        self.assertEqual(
            RESOLVE_BAZEL_CI_MODE.resolve_bazel_ci_mode(
                comparison_complete=True,
                files=["README.md", "docs/guide.md", "docs/reference/config.md"],
                statuses=["A", "M", "M"],
            ),
            {"mode": "docs_only", "run_bazel": "false", "run_observer": "false"},
        )

        self.assertEqual(
            RESOLVE_BAZEL_CI_MODE.resolve_bazel_ci_mode(
                comparison_complete=True,
                files=[
                    ".codex/skills/babysit-pr/scripts/gh_pr_watch.py",
                    ".codex/skills/babysit-pr/scripts/test_gh_pr_watch.py",
                    ".codex/skills/babysit-pr/scripts/github_app_installation_broker.py",
                    ".codex/skills/babysit-pr/scripts/test_github_app_installation_broker.py",
                    ".codex/skills/babysit-gh-workflow-run/scripts/gh_workflow_run_watch.py",
                    ".codex/skills/babysit-gh-workflow-run/tests/test_gh_workflow_run_watch.py",
                    ".codex/skills/babysit-gh-workflow-run/scripts/gh_dispatch_and_watch.py",
                    ".codex/skills/babysit-gh-workflow-run/tests/test_gh_dispatch_and_watch.py",
                    ".codex/skills/sedna/subagent-session-tail/scripts/inspect_subagent_tail.py",
                    ".codex/skills/sedna/subagent-session-tail/tests/test_inspect_subagent_tail.py",
                ],
                statuses=["modified", "added"] * 5,
            ),
            {"mode": "observer_only", "run_bazel": "false", "run_observer": "true"},
        )

        self.assertEqual(
            RESOLVE_BAZEL_CI_MODE.resolve_bazel_ci_mode(
                comparison_complete=True,
                files=[".codex/skills/babysit-pr/scripts/gh_pr_watch.py"],
                statuses=["M"],
            )["mode"],
            "observer_only",
        )

        for comparison_complete, files in [
            (False, ["README.md"]),
            (True, []),
            (True, ["README.md", "codex-rs/core/src/lib.rs"]),
            (True, [".github/workflows/bazel.yml"]),
            (True, ["docs/guide.md", 42]),
            (True, {"filename": "docs/guide.md"}),
            (True, [".github/scripts/validation-lanes/agent-workflow-sanity.sh"]),
        ]:
            with self.subTest(
                comparison_complete=comparison_complete,
                files=files,
            ):
                self.assertEqual(
                    RESOLVE_BAZEL_CI_MODE.resolve_bazel_ci_mode(
                    comparison_complete=comparison_complete,
                    files=files,
                    statuses=["M"] * len(files) if isinstance(files, list) else None,
                ),
                    {"mode": "full", "run_bazel": "true", "run_observer": "false"},
                )

        for status in ["removed", "renamed", "copied", "unknown", None]:
            with self.subTest(status=status):
                self.assertEqual(
                    RESOLVE_BAZEL_CI_MODE.resolve_bazel_ci_mode(
                        comparison_complete=True,
                        files=[".codex/skills/babysit-pr/scripts/gh_pr_watch.py"],
                        statuses=[status],
                    )["mode"],
                    "full",
                )

    def test_command_line_mode_resolver_fails_closed_on_bad_json(self) -> None:
        outputs = run_script(
            SCRIPTS_DIR / "resolve_bazel_ci_mode.py",
            "--comparison-complete",
            "true",
            "--files-json",
            "not-json",
            "--statuses-json",
            "not-json",
        )
        self.assertEqual(outputs, {"mode": "full", "run_bazel": "true", "run_observer": "false"})

    def test_command_line_mode_resolver_writes_github_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "github-output"
            proc = subprocess.run(
                [
                    "python3",
                    str(SCRIPTS_DIR / "resolve_bazel_ci_mode.py"),
                    "--comparison-complete",
                    "true",
                    "--files-json",
                    '["README.md", "docs/guide.md"]',
                    "--statuses-json",
                    '["A", "M"]',
                    "--github-output",
                    str(output_path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                json.loads(proc.stdout),
                {"mode": "docs_only", "run_bazel": "false", "run_observer": "false"},
            )
            self.assertEqual(
                parse_github_output_file(output_path),
                {"mode": "docs_only", "run_bazel": "false", "run_observer": "false"},
            )


class RustCiModeScriptTests(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.repo = TempGitRepo()
        self.base_sha = self.repo.commit("base", {"README.md": "base\n"})

    def tearDown(self) -> None:
        self.repo.cleanup()

    def run_rust_ci_mode(
        self,
        *,
        event_action: str,
        head_files: dict[str, str],
        previous_green_required: str = "false",
        before_sha: str = "",
        extra_args: list[str] | None = None,
    ) -> dict:
        head_sha = self.repo.commit("head", head_files)
        args = [
            "--repo-root",
            str(self.repo.root),
            "--event-name",
            "pull_request",
            "--event-action",
            event_action,
            "--base-sha",
            self.base_sha,
            "--head-sha",
            head_sha,
            "--before-sha",
            before_sha,
            "--previous-green-required",
            previous_green_required,
        ]
        if extra_args:
            args.extend(extra_args)
        return run_script(SCRIPTS_DIR / "resolve_rust_ci_mode.py", *args)

    def test_rust_ci_changed_job_uses_pr_metadata_fast_path_with_git_fallback(self) -> None:
        payload = load_workflow_payload(REPO_ROOT / ".github/workflows/rust-ci.yml")
        changed = ((payload.get("jobs") or {}).get("changed") or {})
        steps = changed.get("steps") or []
        checkout = next(
            step
            for step in steps
            if step.get("uses") == "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
        )
        self.assertEqual((checkout.get("with") or {}).get("fetch-depth"), "1")

        previous_required_step = next(
            step for step in steps if step.get("name") == "Check previous required result on follow-up head"
        )
        self.assertEqual(previous_required_step.get("uses"), "actions/github-script@v9.0.0")
        self.assertIn("github.event.action == 'synchronize'", previous_required_step.get("if") or "")
        previous_required_script = (
            (previous_required_step.get("with") or {}).get("script") or ""
        )
        self.assertIn("github.rest.pulls.listCommits", previous_required_script)
        self.assertIn("github.rest.checks.listForRef", previous_required_script)
        self.assertIn("context.payload.before", previous_required_script)
        self.assertIn("pullRequest?.before", previous_required_script)
        self.assertIn("candidateShas.push(eventBefore)", previous_required_script)
        self.assertIn("previous_green_sha", previous_required_script)
        self.assertIn("Rust CI required gate", previous_required_script)

        metadata_step = next(
            step for step in steps if step.get("name") == "Resolve PR changed files via API"
        )
        self.assertEqual(metadata_step.get("uses"), "actions/github-script@v9.0.0")
        metadata_script = ((metadata_step.get("with") or {}).get("script") or "")
        self.assertIn("github.paginate(github.rest.pulls.listFiles", metadata_script)
        self.assertIn("github.rest.repos.compareCommitsWithBasehead", metadata_script)
        self.assertEqual(
            (metadata_step.get("env") or {}).get("BEFORE_SHA"),
            "${{ steps.previous_required.outputs.previous_green_sha || steps.shas.outputs.before_sha }}",
        )

        fallback_step = next(
            step for step in steps if step.get("name") == "Fetch history for git diff fallback"
        )
        self.assertIn(
            "steps.pr_diff.outputs.needs_git_fallback == 'true'",
            fallback_step.get("if") or "",
        )
        fallback_run = fallback_step.get("run") or ""
        self.assertIn(
            "before_sha='${{ steps.previous_required.outputs.previous_green_sha || steps.shas.outputs.before_sha }}'",
            fallback_run,
        )
        self.assertIn('git fetch --no-tags --depth=1 "${head_repo}" "${before_sha}"', fallback_run)
        self.assertIn('"${before_sha}^{commit}"', fallback_run)

        detect_step = next(
            step for step in steps if step.get("name") == "Detect changed paths and rust-ci mode"
        )
        detect_env = detect_step.get("env") or {}
        self.assertEqual(
            detect_env.get("PREVIOUS_GREEN_REQUIRED"),
            "${{ steps.previous_required.outputs.previous_green_required || 'false' }}",
        )
        self.assertEqual(
            detect_env.get("COMPARISON_BEFORE_SHA"),
            "${{ steps.previous_required.outputs.previous_green_sha || steps.shas.outputs.before_sha }}",
        )
        detect_run = detect_step.get("run") or ""
        self.assertIn('--before-sha "${COMPARISON_BEFORE_SHA}"', detect_run)
        self.assertIn('--previous-green-required "${PREVIOUS_GREEN_REQUIRED}"', detect_run)
        self.assertIn("--primary-files-json", detect_run)
        self.assertIn("--primary-line-count", detect_run)
        self.assertIn("--latest-delta-files-json", detect_run)
        self.assertIn("--latest-delta-line-count", detect_run)

    def test_rust_ci_results_gate_honors_selected_run_flags(self) -> None:
        payload = load_workflow_payload(REPO_ROOT / ".github/workflows/rust-ci.yml")
        jobs = payload.get("jobs") or {}
        results_run = (
            next(
                step
                for step in (jobs.get("results") or {}).get("steps") or []
                if step.get("name") == "Summarize"
            ).get("run")
            or ""
        )

        self.assertIn("needs.changed.outputs.run_argument_comment_lint_package", results_run)
        self.assertIn("needs.changed.outputs.run_argument_comment_lint_prebuilt", results_run)
        self.assertIn("needs.changed.outputs.run_general", results_run)
        self.assertIn("needs.changed.outputs.run_cargo_shear", results_run)
        self.assertIn("needs.changed.outputs.run_incremental_validation", results_run)
        self.assertIn("needs.changed.result", results_run)
        self.assertIn("changed planner failed", results_run)
        self.assertIn("needs.matrix_plan.result", results_run)
        self.assertIn("matrix_plan failed", results_run)
        self.assertIn("needs.planner_fixtures.result", results_run)
        self.assertIn('"${NEEDS_CHANGED_OUTPUTS_WORKFLOWS}" == \'true\'', results_run)
        self.assertIn("planner_fixtures failed", results_run)
        self.assertIn("incremental_validation failed", results_run)
        no_relevant_gate = results_run.split("No relevant changes -> CI not required.")[0]
        self.assertIn("NEEDS_CHANGED_OUTPUTS_WORKFLOWS", no_relevant_gate)
        self.assertIn("needs.changed.outputs.run_incremental_validation", no_relevant_gate)
        self.assertNotIn(
            'NEEDS_CHANGED_OUTPUTS_CODEX}" == \'true\' || "${NEEDS_CHANGED_OUTPUTS_WORKFLOWS}" == \'true\'',
            results_run,
        )

        argpkg_job = jobs.get("argument_comment_lint_package") or {}
        self.assertEqual(
            argpkg_job.get("if"),
            "${{ needs.changed.outputs.run_argument_comment_lint_package == 'true' }}",
        )

    def test_rust_ci_general_runs_pinned_targeted_clippy_before_bench_smoke(self) -> None:
        payload = load_workflow_payload(REPO_ROOT / ".github/workflows/rust-ci.yml")
        general = (payload.get("jobs") or {}).get("general") or {}
        self.assertEqual(general.get("name"), "Fast format / Clippy / etc")
        steps = general.get("steps") or []
        names = [step.get("name") for step in steps]
        self.assertLess(names.index("cargo fmt"), names.index("cargo clippy (targeted packages)"))
        self.assertLess(
            names.index("cargo clippy (targeted packages)"),
            names.index("Rust benchmark smoke test"),
        )
        toolchain = next(
            step
            for step in steps
            if step.get("uses") == "dtolnay/rust-toolchain@ebb3d1676050bfd0971c36c1e215b5751473994d"
        )
        self.assertEqual(
            (toolchain.get("with") or {}).get("target"),
            "x86_64-unknown-linux-gnu",
        )
        clippy = next(
            step for step in steps if step.get("name") == "cargo clippy (targeted packages)"
        )
        clippy_run = clippy.get("run") or ""
        self.assertIn("run_targeted_clippy.sh", clippy_run)
        helper = (REPO_ROOT / ".github/scripts/run_targeted_clippy.sh").read_text()
        for flag in (
            "--all-features",
            "--tests",
            "--profile dev",
            "--no-deps",
            "-- -D warnings",
        ):
            self.assertIn(flag, helper)
        self.assertNotIn("--workspace", helper)
        self.assertIn("steps.targeted_clippy_packages.outputs.packages", clippy.get("if") or "")

    def test_rust_ci_argument_comment_lint_uses_single_cached_bazel_action(self) -> None:
        payload = load_workflow_payload(REPO_ROOT / ".github/workflows/rust-ci.yml")
        jobs = payload.get("jobs") or {}

        matrix_plan_run = (
            next(
                step
                for step in (jobs.get("matrix_plan") or {}).get("steps") or []
                if step.get("name") == "Compute platform matrices"
            ).get("run")
            or ""
        )
        self.assertIn('"timeout_minutes": 30', matrix_plan_run)

        arglint_job = jobs.get("argument_comment_lint_prebuilt") or {}
        self.assertEqual(arglint_job.get("needs"), ["changed", "matrix_plan"])
        self.assertEqual(
            arglint_job.get("if"),
            "${{ needs.changed.outputs.run_argument_comment_lint_prebuilt == 'true' }}",
        )
        self.assertEqual(
            (arglint_job.get("strategy") or {}).get("matrix"),
            "${{ fromJSON(needs.matrix_plan.outputs.argument_comment_lint_matrix) }}",
        )
        self.assertNotIn("environment", arglint_job)

        arglint_steps = arglint_job.get("steps") or []
        lint_steps = [
            step
            for step in arglint_steps
            if step.get("name") == "Run argument comment lint on codex-rs"
        ]
        self.assertEqual(len(lint_steps), 1)
        self.assertEqual(lint_steps[0].get("uses"), "./.github/actions/run-argument-comment-lint")
        self.assertNotIn("buildbuddy-api-key", lint_steps[0].get("with") or {})

    def test_argument_comment_lint_platform_workflow_is_pr_only_advisory_coverage(self) -> None:
        payload = load_workflow_payload(
            REPO_ROOT / ".github/workflows/argument-comment-lint-platform.yml"
        )
        trigger = payload.get("on") or {}
        pull_request = trigger.get("pull_request") or {}
        self.assertEqual(set(trigger), {"pull_request"})
        self.assertEqual(pull_request.get("branches"), ["main", "upstream-main"])
        self.assertEqual(
            pull_request.get("paths"),
            [
                "codex-rs/**",
                "tools/argument-comment-lint/**",
                "Cargo.lock",
                "Cargo.toml",
                "**/Cargo.toml",
                "rust-toolchain.toml",
                "MODULE.bazel",
                "MODULE.bazel.lock",
                ".github/**",
                "justfile",
                "scripts/**",
            ],
        )
        self.assertEqual(payload.get("permissions"), {"contents": "read"})
        self.assertEqual(
            payload.get("concurrency"),
            {
                "group": "concurrency-group::${{ github.workflow }}::pr-${{ github.event.pull_request.number }}",
                "cancel-in-progress": "true",
            },
        )

        lint_job = (payload.get("jobs") or {}).get("lint") or {}
        self.assertNotIn("environment", lint_job)
        self.assertEqual(
            (lint_job.get("strategy") or {}).get("matrix"),
            {
                "include": [
                    {"name": "macOS", "runner": "macos-15", "timeout_minutes": "30"},
                    {
                        "name": "Windows",
                        "runner": "windows-x64",
                        "runs_on": "windows-2022",
                        "timeout_minutes": "30",
                    },
                ]
            },
        )
        steps = lint_job.get("steps") or []
        checkout = steps[0]
        self.assertEqual(
            checkout.get("uses"), "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
        )
        self.assertEqual((checkout.get("with") or {}).get("persist-credentials"), "false")
        lint_step = next(
            step for step in steps if step.get("name") == "Run argument comment lint on codex-rs"
        )
        self.assertEqual(lint_step.get("uses"), "./.github/actions/run-argument-comment-lint")

    def test_explicit_primary_diff_inputs_route_without_git_history(self) -> None:
        outputs = run_script(
            SCRIPTS_DIR / "resolve_rust_ci_mode.py",
            "--repo-root",
            str(self.repo.root),
            "--event-name",
            "pull_request",
            "--event-action",
            "opened",
            "--base-sha",
            "0" * 40,
            "--head-sha",
            "1" * 40,
            "--primary-files-json",
            json.dumps(["codex-rs/protocol/src/openai_models.rs"]),
            "--primary-line-count",
            "2",
        )

        self.assertEqual(outputs["validation_mode"], "light_initial")
        self.assertEqual(outputs["run_incremental_validation"], "true")
        self.assertEqual(
            outputs["incremental_lanes"],
            ",".join(
                [
                    "codex.spawn-agent-tool-model-surface-targeted",
                    "codex.spawn-agent-description-model-surface-targeted",
                ]
            ),
        )

    def test_explicit_latest_delta_inputs_route_green_followup_without_git_history(self) -> None:
        outputs = run_script(
            SCRIPTS_DIR / "resolve_rust_ci_mode.py",
            "--repo-root",
            str(self.repo.root),
            "--event-name",
            "pull_request",
            "--event-action",
            "synchronize",
            "--base-sha",
            "0" * 40,
            "--head-sha",
            "1" * 40,
            "--before-sha",
            "2" * 40,
            "--previous-green-required",
            "true",
            "--primary-files-json",
            json.dumps(["codex-rs/tools/src/agent_tool.rs"]),
            "--primary-line-count",
            "20",
            "--latest-delta-files-json",
            json.dumps(["codex-rs/tools/src/agent_tool.rs"]),
            "--latest-delta-line-count",
            "1",
        )

        self.assertEqual(outputs["validation_mode"], "light_followup")
        self.assertEqual(outputs["run_incremental_validation"], "true")
        self.assertEqual(
            outputs["incremental_lanes"],
            ",".join(
                [
                    "codex.spawn-agent-tool-model-surface-targeted",
                ]
            ),
        )

    def test_explicit_workflow_catalog_diff_stays_on_workflow_lanes(self) -> None:
        outputs = run_script(
            SCRIPTS_DIR / "resolve_rust_ci_mode.py",
            "--repo-root",
            str(self.repo.root),
            "--event-name",
            "pull_request",
            "--event-action",
            "opened",
            "--base-sha",
            "0" * 40,
            "--head-sha",
            "1" * 40,
            "--primary-files-json",
            json.dumps(
                [
                    ".github/workflows/_validation-lane-rust-minimal.yml",
                    ".github/workflows/validation-lab.yml",
                    ".github/validation-lanes.json",
                    ".github/scripts/test_ci_planners.py",
                ]
            ),
            "--primary-line-count",
            "40",
        )

        self.assertEqual(outputs["validation_mode"], "light_initial")
        self.assertEqual(outputs["workflows"], "true")
        self.assertEqual(outputs["run_general"], "false")
        self.assertEqual(outputs["run_cargo_shear"], "false")
        self.assertEqual(
            outputs["incremental_lanes"],
            ",".join(
                [
                    "codex.workflow-ci-sanity",
                    "codex.downstream-docs-check",
                ]
            ),
        )

    def test_explicit_large_primary_diff_does_not_enter_light_route(self) -> None:
        outputs = run_script(
            SCRIPTS_DIR / "resolve_rust_ci_mode.py",
            "--repo-root",
            str(self.repo.root),
            "--event-name",
            "pull_request",
            "--event-action",
            "opened",
            "--base-sha",
            "0" * 40,
            "--head-sha",
            "1" * 40,
            "--primary-files-json",
            json.dumps(["codex-rs/prompts/src/review_request.rs"]),
            "--primary-line-count",
            "401",
        )

        self.assertEqual(outputs["validation_mode"], "full")
        self.assertEqual(outputs["run_incremental_validation"], "false")

    def test_explicit_changed_files_rejects_malformed_json_cleanly(self) -> None:
        proc = subprocess.run(
            [
                "python3",
                str(SCRIPTS_DIR / "resolve_rust_ci_mode.py"),
                "--repo-root",
                str(self.repo.root),
                "--event-name",
                "pull_request",
                "--event-action",
                "opened",
                "--base-sha",
                "0" * 40,
                "--head-sha",
                "1" * 40,
                "--primary-files-json",
                "not-json",
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("invalid JSON input for changed-files", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_merge_group_complete_docs_delta_uses_lightweight_route(self) -> None:
        outputs = run_script(
            SCRIPTS_DIR / "resolve_rust_ci_mode.py",
            "--repo-root", str(self.repo.root), "--event-name", "merge_group",
            "--merge-group-comparison-complete", "true",
            "--merge-group-files-json", json.dumps(["README.md", "docs/ci.md"]),
            "--merge-group-status-json", json.dumps(["A", "M"]),
            "--merge-group-line-count", "12",
        )
        self.assertEqual(outputs["validation_mode"], "light_merge_group")
        self.assertEqual(outputs["run_incremental_validation"], "true")
        self.assertEqual(
            outputs["incremental_lanes"],
            "codex.downstream-docs-check,codex.downstream-divergence-audit",
        )
        self.assertEqual(outputs["run_general"], "false")

    def test_merge_group_incomplete_or_non_docs_delta_stays_full(self) -> None:
        cases = [
            ("false", ["README.md"], "1"),
            ("true", ["codex-rs/core/src/lib.rs"], "1"),
            ("true", ["docs/ci.md"], ""),
            ("true", None, "1"),
        ]
        for complete, files, lines in cases:
            with self.subTest(complete=complete, files=files, lines=lines):
                args = [
                    "--repo-root", str(self.repo.root), "--event-name", "merge_group",
                    "--merge-group-comparison-complete", complete,
                    "--merge-group-files-json", "not-json" if files is None else json.dumps(files),
                    "--merge-group-status-json", json.dumps(["M"] * len(files)) if files is not None else "not-json",
                    "--merge-group-line-count", lines,
                ]
                outputs = run_script(SCRIPTS_DIR / "resolve_rust_ci_mode.py", *args)
                self.assertEqual(outputs["validation_mode"], "full")
                self.assertEqual(outputs["run_incremental_validation"], "false")

    def test_merge_group_rename_delete_and_uncertain_metadata_stays_full(self) -> None:
        for files in (["docs/old.md", "docs/new.md"], ["docs/deleted.md"]):
            outputs = run_script(
                SCRIPTS_DIR / "resolve_rust_ci_mode.py",
                "--repo-root", str(self.repo.root), "--event-name", "merge_group",
                "--merge-group-comparison-complete", "true",
                "--merge-group-files-json", json.dumps(files),
                "--merge-group-status-json", json.dumps(["R100", "D"] if len(files) == 2 else ["D"]),
                "--merge-group-line-count", "2",
            )
            self.assertEqual(outputs["validation_mode"], "full")

    def test_merge_group_unknown_or_malformed_line_count_stays_full(self) -> None:
        for count in ("10000", "not-a-count"):
            outputs = run_script(
                SCRIPTS_DIR / "resolve_rust_ci_mode.py",
                "--repo-root", str(self.repo.root), "--event-name", "merge_group",
                "--merge-group-comparison-complete", "true",
                "--merge-group-files-json", json.dumps(["docs/ci.md"]),
                "--merge-group-status-json", json.dumps(["M"]),
                "--merge-group-line-count", count,
            )
            self.assertEqual(outputs["validation_mode"], "full")

    def test_rust_ci_producer_output_delimiter_is_closed_and_collision_free(self) -> None:
        producer_step = workflow_step_by_name(
            REPO_ROOT / ".github/workflows/rust-ci.yml",
            "changed",
            "Resolve merge-group queue-base delta",
        )
        producer_script = producer_step.get("run") or ""
        self.assertIn('line.split("\\t", 1)', producer_script)

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = root / "repo"
            origin = root / "origin.git"
            subprocess.run(
                ["git", "init", "--initial-branch=main", str(repo)],
                check=True,
                capture_output=True,
                text=True,
            )
            for key, value in (
                ("user.name", "CI Planner Tests"),
                ("user.email", "ci-planner-tests@example.invalid"),
                ("commit.gpgSign", "false"),
            ):
                subprocess.run(
                    ["git", "-C", str(repo), "config", key, value],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            (repo / "README.md").write_text("base\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(repo), "add", "README.md"],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "-C", str(repo), "commit", "-m", "base"],
                check=True,
                capture_output=True,
                text=True,
            )
            base_sha = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            subprocess.run(
                ["git", "clone", "--bare", str(repo), str(origin)],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "-C", str(repo), "remote", "add", "origin", str(origin)],
                check=True,
                capture_output=True,
                text=True,
            )
            (repo / "README.md").write_text("head\n", encoding="utf-8")
            docs = repo / "docs"
            docs.mkdir()
            (docs / "ci.md").write_text("docs\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(repo), "add", "README.md", "docs/ci.md"],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "-C", str(repo), "commit", "-m", "docs delta"],
                check=True,
                capture_output=True,
                text=True,
            )
            head_sha = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            output_path = root / "github-output.txt"
            output_path.write_text("", encoding="utf-8")
            env = {
                **os.environ,
                "BASE_SHA": base_sha,
                "HEAD_SHA": head_sha,
                "GITHUB_OUTPUT": str(output_path),
            }
            producer = subprocess.run(
                ["bash", "-c", f"set -euo pipefail\n{producer_script}"],
                cwd=repo,
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(producer.returncode, 0, producer.stderr)
            produced = parse_github_output_file(output_path)
            self.assertEqual(json.loads(produced["status_json"]), ["M", "A"])
            self.assertEqual(
                json.loads(produced["files_json"]), ["README.md", "docs/ci.md"]
            )
            outputs = run_script(
                SCRIPTS_DIR / "resolve_rust_ci_mode.py",
                "--repo-root", str(repo),
                "--event-name", "merge_group",
                "--merge-group-comparison-complete", produced["comparison_complete"],
                "--merge-group-files-json", produced["files_json"],
                "--merge-group-status-json", produced["status_json"],
                "--merge-group-line-count", produced["line_count"],
            )
            self.assertEqual(outputs["validation_mode"], "light_merge_group")
            self.assertEqual(outputs["run_incremental_validation"], "true")

    def test_light_initial_routes_small_openai_models_pr_to_exact_lane(self) -> None:
        outputs = self.run_rust_ci_mode(
            event_action="opened",
            head_files={"codex-rs/protocol/src/openai_models.rs": "first\nsecond\n"},
        )

        self.assertEqual(outputs["validation_mode"], "light_initial")
        self.assertEqual(outputs["run_incremental_validation"], "true")
        self.assertEqual(
            outputs["incremental_lanes"],
            ",".join(
                [
                    "codex.spawn-agent-tool-model-surface-targeted",
                    "codex.spawn-agent-description-model-surface-targeted",
                ]
            ),
        )
        self.assertEqual(outputs["run_general"], "false")
        self.assertEqual(outputs["run_cargo_shear"], "false")

    def test_light_followup_routes_small_spawn_tool_delta_to_shared_lanes(self) -> None:
        green_sha = self.repo.commit(
            "green",
            {"codex-rs/tools/src/agent_tool.rs": "base\n"},
        )
        outputs = run_script(
            SCRIPTS_DIR / "resolve_rust_ci_mode.py",
            "--repo-root",
            str(self.repo.root),
            "--event-name",
            "pull_request",
            "--event-action",
            "synchronize",
            "--base-sha",
            self.base_sha,
            "--head-sha",
            self.repo.commit(
                "followup",
                {"codex-rs/tools/src/agent_tool.rs": "base\nfollowup\n"},
            ),
            "--before-sha",
            green_sha,
            "--previous-green-required",
            "true",
        )

        self.assertEqual(outputs["validation_mode"], "light_followup")
        self.assertEqual(outputs["run_incremental_validation"], "true")
        self.assertEqual(
            outputs["incremental_lanes"],
            ",".join(
                [
                    "codex.spawn-agent-tool-model-surface-targeted",
                ]
            ),
        )
        self.assertEqual(outputs["run_argument_comment_lint_prebuilt"], "false")

    def test_light_followup_accepts_small_workflow_catalog_delta_after_green_head(self) -> None:
        green_sha = self.repo.commit(
            "green",
            {".github/workflows/validation-lab.yml": "base\n"},
        )
        outputs = run_script(
            SCRIPTS_DIR / "resolve_rust_ci_mode.py",
            "--repo-root",
            str(self.repo.root),
            "--event-name",
            "pull_request",
            "--event-action",
            "synchronize",
            "--base-sha",
            self.base_sha,
            "--head-sha",
            self.repo.commit(
                "workflow-followup",
                {
                    ".github/workflows/_validation-lane-rust-minimal.yml": "one\n",
                    ".github/workflows/validation-lab.yml": "two\n",
                    ".github/validation-lanes.json": "three\n",
                    ".github/scripts/test_ci_planners.py": "four\n",
                },
            ),
            "--before-sha",
            green_sha,
            "--previous-green-required",
            "true",
        )

        self.assertEqual(outputs["validation_mode"], "light_followup")
        self.assertEqual(outputs["run_incremental_validation"], "true")
        self.assertEqual(
            outputs["incremental_lanes"],
            ",".join(
                [
                    "codex.workflow-ci-sanity",
                    "codex.downstream-docs-check",
                ]
            ),
        )

    def test_workflow_only_pr_skips_rust_compile_gates(self) -> None:
        outputs = self.run_rust_ci_mode(
            event_action="opened",
            head_files={
                ".github/workflows/rust-ci.yml": "workflow\n",
                ".github/scripts/resolve_rust_ci_mode.py": "planner\n",
                "justfile": "validation:\n",
            },
        )

        self.assertEqual(outputs["validation_mode"], "light_initial")
        self.assertEqual(outputs["workflows"], "true")
        self.assertEqual(outputs["has_relevant_changes"], "true")
        self.assertEqual(outputs["run_general"], "false")
        self.assertEqual(outputs["run_cargo_shear"], "false")
        self.assertEqual(outputs["run_argument_comment_lint_prebuilt"], "false")
        self.assertEqual(outputs["run_argument_comment_lint_package"], "false")
        self.assertEqual(outputs["run_incremental_validation"], "true")
        self.assertEqual(
            outputs["incremental_lanes"],
            ",".join(
                [
                    "codex.workflow-ci-sanity",
                    "codex.downstream-docs-check",
                ]
            ),
        )

    def test_docs_only_light_route_still_requires_incremental_gate(self) -> None:
        outputs = self.run_rust_ci_mode(
            event_action="opened",
            head_files={"docs/native-computer-use.md": "docs\n"},
        )

        self.assertEqual(outputs["validation_mode"], "light_initial")
        self.assertEqual(outputs["argument_comment_lint"], "false")
        self.assertEqual(outputs["codex"], "false")
        self.assertEqual(outputs["workflows"], "false")
        self.assertEqual(outputs["run_incremental_validation"], "true")
        self.assertEqual(
            outputs["incremental_lanes"],
            "codex.downstream-docs-check,codex.downstream-divergence-audit",
        )

    def test_skill_only_pr_is_irrelevant_to_rust_ci(self) -> None:
        outputs = self.run_rust_ci_mode(
            event_action="opened",
            head_files={".codex/skills/example/SKILL.md": "hello\n"},
        )

        self.assertEqual(outputs["has_relevant_changes"], "false")
        self.assertEqual(outputs["run_general"], "false")
        self.assertEqual(outputs["run_cargo_shear"], "false")
        self.assertEqual(outputs["run_argument_comment_lint_package"], "false")
        self.assertEqual(outputs["run_argument_comment_lint_prebuilt"], "false")

    def test_non_rust_codex_rs_asset_pr_is_irrelevant_to_rust_ci(self) -> None:
        outputs = self.run_rust_ci_mode(
            event_action="opened",
            head_files={
                "codex-rs/skills/src/assets/samples/skill-creator/scripts/init_skill.py": "print('hi')\n",
            },
        )

        self.assertEqual(outputs["has_relevant_changes"], "false")
        self.assertEqual(outputs["codex"], "false")
        self.assertEqual(outputs["argument_comment_lint"], "false")
        self.assertEqual(outputs["run_general"], "false")
        self.assertEqual(outputs["run_cargo_shear"], "false")
        self.assertEqual(outputs["run_argument_comment_lint_package"], "false")
        self.assertEqual(outputs["run_argument_comment_lint_prebuilt"], "false")

    def test_rust_build_script_pr_still_triggers_rust_ci(self) -> None:
        outputs = self.run_rust_ci_mode(
            event_action="opened",
            head_files={"codex-rs/cli/build.rs": "fn main() {}\n"},
        )

        self.assertEqual(outputs["has_relevant_changes"], "true")
        self.assertEqual(outputs["codex"], "true")
        self.assertEqual(outputs["argument_comment_lint"], "true")
        self.assertEqual(outputs["run_general"], "true")
        self.assertEqual(outputs["run_cargo_shear"], "true")
        self.assertEqual(outputs["run_argument_comment_lint_prebuilt"], "true")

    def test_review_request_pr_routes_to_custom_prompt_targeted_validation(self) -> None:
        outputs = self.run_rust_ci_mode(
            event_action="opened",
            head_files={"codex-rs/prompts/src/review_request.rs": "fn review_prompt() {}\n"},
        )

        self.assertEqual(outputs["validation_mode"], "light_initial")
        self.assertEqual(outputs["codex"], "true")
        self.assertEqual(outputs["run_general"], "false")
        self.assertEqual(outputs["run_cargo_shear"], "false")
        self.assertEqual(outputs["run_incremental_validation"], "true")
        self.assertEqual(outputs["incremental_lanes"], "codex.custom-prompts-targeted")


class HelperScriptTests(unittest.TestCase):
    def run_sedna_installer_fixture(
        self,
        failure: str | None = None,
        *,
        arch: str = "x86_64",
        hardened: bool = True,
        dry_run: bool = True,
        verification_args_override: list[str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        release_tag = "v0.146.0-alpha.8-sedna.99+upstream.3"
        release_version = release_tag.removeprefix("v")
        target_commit = "a" * 40
        target = (
            "aarch64-unknown-linux-gnu"
            if arch == "aarch64"
            else "x86_64-unknown-linux-gnu"
        )
        archive_name = f"codex-sedna-{release_version}-{target}.tar.gz"
        archive_base = archive_name.removesuffix(".tar.gz")
        metadata_name = (
            f"RELEASE-METADATA-{target}.json"
            if arch == "aarch64"
            else "RELEASE-METADATA.json"
        )
        checksum_name = (
            f"SHA256SUMS-{target}.txt" if arch == "aarch64" else "SHA256SUMS.txt"
        )
        sbom_name = f"{archive_base}.spdx.json"
        attestation_name = f"{archive_base}.intoto.jsonl"
        codex_sigstore_name = f"{archive_base}-codex.sigstore"
        proxy_sigstore_name = f"{archive_base}-codex-responses-api-proxy.sigstore"

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            assets_dir = root / "assets"
            fake_bin = root / "bin"
            stage = root / "stage"
            assets_dir.mkdir()
            fake_bin.mkdir()
            stage.mkdir()

            codex = stage / "codex"
            codex.write_text(
                "#!/usr/bin/env bash\n"
                f"if [[ ${{1:-}} == --version ]]; then echo 'codex-cli {release_version}'; fi\n",
                encoding="utf-8",
            )
            proxy = stage / "codex-responses-api-proxy"
            proxy.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            codex.chmod(0o755)
            proxy.chmod(0o755)

            archive_path = root / archive_name
            with tarfile.open(archive_path, "w:gz") as archive:
                archive.add(codex, arcname="codex")
                archive.add(proxy, arcname="codex-responses-api-proxy")
                if failure == "unsafe_archive":
                    archive.add(codex, arcname="../escape")

            metadata = {
                "release_tag": release_tag,
                "release_version": release_version,
                "target": (
                    (
                        "x86_64-unknown-linux-gnu"
                        if arch == "aarch64"
                        else "aarch64-unknown-linux-gnu"
                    )
                    if failure == "malformed_metadata"
                    else target
                ),
                "repository": "sednalabs/codex",
                "target_commit": target_commit,
            }
            if failure == "legacy_x86":
                metadata.pop("target_commit")
            (root / metadata_name).write_text(json.dumps(metadata), encoding="utf-8")
            (root / sbom_name).write_text(
                json.dumps(
                    {
                        "spdxVersion": (
                            "SPDX-2.2"
                            if failure == "malformed_sbom"
                            else "SPDX-2.3"
                        )
                    }
                ),
                encoding="utf-8",
            )
            (root / attestation_name).write_text("{}\n", encoding="utf-8")
            (root / codex_sigstore_name).write_text("{}\n", encoding="utf-8")
            (root / proxy_sigstore_name).write_text("{}\n", encoding="utf-8")

            asset_names = [
                archive_name,
                metadata_name,
                sbom_name,
                attestation_name,
                codex_sigstore_name,
                proxy_sigstore_name,
            ]
            checksums = []
            for name in asset_names:
                digest = hashlib.sha256((root / name).read_bytes()).hexdigest()
                checksums.append(f"{digest}  {name}")
            (root / checksum_name).write_text("\n".join(checksums) + "\n", encoding="utf-8")
            if failure == "checksum_mismatch":
                (root / codex_sigstore_name).write_text("tampered\n", encoding="utf-8")

            all_asset_names = [
                archive_name,
                checksum_name,
                metadata_name,
                sbom_name,
                attestation_name,
                codex_sigstore_name,
                proxy_sigstore_name,
            ]
            if failure in ("missing_sbom", "arm_missing_sbom"):
                all_asset_names.remove(sbom_name)
            if failure == "legacy_x86":
                for trust_name in (
                    sbom_name,
                    attestation_name,
                    codex_sigstore_name,
                    proxy_sigstore_name,
                ):
                    all_asset_names.remove(trust_name)
            release_assets = []
            for asset_id, name in enumerate(all_asset_names, start=1):
                release_assets.append({"id": asset_id, "name": name})
                (assets_dir / str(asset_id)).write_bytes((root / name).read_bytes())
            (root / "release.json").write_text(
                json.dumps(
                    {
                        "tag_name": release_tag,
                        "draft": False,
                        "prerelease": True,
                        "published_at": (
                            "2026-07-31T05:48:08Z"
                            if failure == "legacy_x86"
                            else "2026-08-16T12:00:00Z"
                        ),
                        "target_commitish": (
                            "b" * 40
                            if failure == "wrong_source_commit"
                            else (
                                "main" if failure == "legacy_x86" else target_commit
                            )
                        ),
                        "assets": release_assets,
                    }
                ),
                encoding="utf-8",
            )

            if failure in ("existing_tampered", "existing_matching"):
                release_dir = (
                    root
                    / "home"
                    / ".codex"
                    / "packages"
                    / "standalone"
                    / "releases"
                    / release_tag
                )
                release_dir.mkdir(parents=True)
                for name in (
                    "codex",
                    "codex-responses-api-proxy",
                    "RELEASE-METADATA.json",
                    "SHA256SUMS.txt",
                ):
                    source = {
                        "codex": codex,
                        "codex-responses-api-proxy": proxy,
                        "RELEASE-METADATA.json": root / metadata_name,
                        "SHA256SUMS.txt": root / checksum_name,
                    }[name]
                    shutil.copy2(source, release_dir / name)
                if failure == "existing_tampered":
                    (release_dir / "codex").write_text(
                        "#!/usr/bin/env bash\necho tampered\n", encoding="utf-8"
                    )
                    (release_dir / "codex").chmod(0o755)

            predecessor_link = None
            if failure == "activation_restore":
                predecessor_link = root / "previous-codex"
                predecessor_link.write_text("previous\n", encoding="utf-8")
                visible_path = root / "home" / ".local" / "bin" / "codex"
                visible_path.parent.mkdir(parents=True)
                visible_path.symlink_to(predecessor_link)
            elif failure == "restore_temp_collision":
                predecessor_link = root / "previous-codex"
                predecessor_link.write_text("previous\n", encoding="utf-8")
                visible_path = root / "home" / ".local" / "bin" / "codex"
                visible_path.parent.mkdir(parents=True)
                visible_path.symlink_to(predecessor_link)
                current_path = root / "home" / ".codex" / "packages" / "standalone" / "current"
                current_path.parent.mkdir(parents=True)
                current_path.symlink_to(root / "previous-current")
                (visible_path.parent / ".codex.restore.collision").symlink_to(
                    root / "attacker-visible"
                )
                (current_path.parent / ".current.restore.collision").symlink_to(
                    root / "attacker-current"
                )
            elif failure == "rollback_attacker_replacement":
                predecessor_link = root / "previous-codex"
                predecessor_link.write_text("previous\n", encoding="utf-8")
                visible_path = root / "home" / ".local" / "bin" / "codex"
                visible_path.parent.mkdir(parents=True)
                visible_path.symlink_to(predecessor_link)
                current_path = root / "home" / ".codex" / "packages" / "standalone" / "current"
                current_path.parent.mkdir(parents=True)
                current_path.symlink_to(root / "previous-current")
            elif failure == "empty_attacker_replacement":
                pass
            if failure == "symlink_releases":
                releases_path = root / "home" / ".codex" / "packages" / "standalone" / "releases"
                releases_path.parent.mkdir(parents=True)
                releases_path.symlink_to(root / "outside-releases")
            elif failure == "symlink_bin":
                bin_path = root / "home" / ".local" / "bin"
                bin_path.parent.mkdir(parents=True)
                bin_path.symlink_to(root / "outside-bin")
            elif failure == "symlink_local_parent":
                local_path = root / "home" / ".local"
                local_path.parent.mkdir(parents=True)
                local_path.symlink_to(root / "outside-local")
            elif failure == "symlink_release_entry":
                release_path = (
                    root / "home" / ".codex" / "packages" / "standalone" / "releases" / release_tag
                )
                release_path.mkdir(parents=True)
                (release_path / "codex").symlink_to(root / "outside-codex")
            elif failure == "symlink_backups":
                backups_path = root / "home" / ".codex" / "packages" / "standalone" / "backups"
                backups_path.parent.mkdir(parents=True)
                backups_path.symlink_to(root / "outside-backups")

            fake_curl = fake_bin / "curl"
            fake_curl.write_text(
                """#!/usr/bin/env bash
set -euo pipefail
output=''
url=''
while [[ $# -gt 0 ]]; do
  case "$1" in
    --output) output="$2"; shift 2 ;;
    http*) url="$1"; shift ;;
    *) shift ;;
  esac
done
if [[ "$url" == */releases/tags/* ]]; then
  cp "$ASSET_FIXTURE_DIR/release.json" "$output"
elif [[ "$url" == *sigstore/cosign/releases/download/* || "$url" == *cli/cli/releases/download/* ]]; then
  printf 'untrusted verifier fixture' > "$output"
else
  cp "$ASSET_FIXTURE_DIR/assets/${url##*/}" "$output"
fi
""",
                encoding="utf-8",
            )
            fake_file = fake_bin / "file"
            if failure == "legacy_x86":
                fake_file.write_text("#!/usr/bin/env bash\nexit 97\n", encoding="utf-8")
            else:
                fake_file.write_text(
                    "#!/usr/bin/env bash\n"
                    'echo "${FAKE_FILE_OUTPUT:-ELF 64-bit LSB pie executable, x86-64}"\n',
                    encoding="utf-8",
                )
            fake_uname = fake_bin / "uname"
            fake_uname.write_text(
                "#!/usr/bin/env bash\n"
                "if [[ ${1:-} == -s ]]; then echo Linux; else echo \"${FAKE_UNAME_ARCH}\"; fi\n",
                encoding="utf-8",
            )
            for name in ("cosign", "gh", "readelf"):
                helper = fake_bin / name
                if failure == "legacy_x86" and name == "readelf":
                    helper.write_text(
                        "#!/usr/bin/env bash\nexit 97\n", encoding="utf-8"
                    )
                elif name == "gh":
                    helper.write_text(
                        """#!/usr/bin/env bash
set -euo pipefail
if [[ ${1:-} == attestation && ${2:-} == verify && ${3:-} == --help ]]; then
  echo '--bundle --deny-self-hosted-runners --source-digest --signer-digest'
  exit 0
fi
if [[ ${1:-} == attestation && ${2:-} == verify && ${3:-} != --help ]]; then
  deny_self_hosted=false
  bundle=''
  source_digest=''
  signer_digest=''
  while [[ $# -gt 0 ]]; do
    arg="$1"
    if [[ "$arg" == --deny-self-hosted-runners ]]; then
      deny_self_hosted=true
      shift
    elif [[ "$arg" == --bundle ]]; then
      bundle="${2:-}"
      shift 2
    elif [[ "$arg" == --source-digest ]]; then
      source_digest="${2:-}"
      shift 2
    elif [[ "$arg" == --signer-digest ]]; then
      signer_digest="${2:-}"
      shift 2
    else
      shift
    fi
  done
  if [[ "$deny_self_hosted" != true || ! -s "$bundle" || "$source_digest" != "$EXPECTED_SOURCE_COMMIT" || "$signer_digest" != "$EXPECTED_SOURCE_COMMIT" ]]; then
    exit 98
  fi
fi
exit 0
""",
                        encoding="utf-8",
                    )
                elif name == "cosign":
                    helper.write_text(
                        """#!/usr/bin/env bash
set -euo pipefail
if [[ ${1:-} == verify-blob && ${2:-} == --help ]]; then
  echo '--certificate-github-workflow-sha --certificate-identity-regexp'
  exit 0
fi
workflow_sha=''
while [[ $# -gt 0 ]]; do
  if [[ "$1" == --certificate-github-workflow-sha ]]; then
    workflow_sha="${2:-}"
    shift 2
  else
    shift
  fi
done
if [[ "$workflow_sha" != "$EXPECTED_SOURCE_COMMIT" ]]; then
  exit 99
fi
""",
                        encoding="utf-8",
                    )
                else:
                    helper.write_text(
                        "#!/usr/bin/env bash\n"
                        + (
                            "echo \"Requesting program interpreter: ${FAKE_ELF_INTERPRETER}\"\n"
                            if name == "readelf"
                            else "exit 0\n"
                        ),
                        encoding="utf-8",
                    )
                helper.chmod(0o755)
            if failure == "outdated_verifiers":
                for name in ("cosign", "gh"):
                    helper = fake_bin / name
                    helper.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
                    helper.chmod(0o755)
            if failure == "signature_failure":
                (fake_bin / "cosign").write_text(
                    "#!/usr/bin/env bash\n"
                    "if [[ ${1:-} == verify-blob && ${2:-} == --help ]]; then "
                    "echo --certificate-github-workflow-sha; exit 0; fi\n"
                    "exit 91\n",
                    encoding="utf-8",
                )
                (fake_bin / "cosign").chmod(0o755)
            if failure == "attestation_failure":
                (fake_bin / "gh").write_text(
                    "#!/usr/bin/env bash\n"
                    "if [[ ${1:-} == attestation && ${2:-} == verify && ${3:-} == --help ]]; then "
                    "echo '--bundle --deny-self-hosted-runners --source-digest --signer-digest'; exit 0; fi\n"
                    "exit 92\n",
                    encoding="utf-8",
                )
                (fake_bin / "gh").chmod(0o755)
            fake_curl.chmod(0o755)
            fake_file.chmod(0o755)
            fake_uname.chmod(0o755)

            env = {
                **os.environ,
                "ASSET_FIXTURE_DIR": str(root),
                "FAKE_FILE_OUTPUT": (
                    "ELF 64-bit LSB pie executable, ARM aarch64"
                    if arch == "aarch64" or failure == "wrong_elf"
                    else "ELF 64-bit LSB pie executable, x86-64"
                ),
                "FAKE_ELF_INTERPRETER": (
                    "/lib/ld-linux-aarch64.so.1"
                    if arch == "aarch64"
                    else "/lib64/ld-linux-x86-64.so.2"
                ),
                "FAKE_UNAME_ARCH": arch,
                "EXPECTED_SOURCE_COMMIT": target_commit,
                "HOME": str(root / "home"),
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
            }
            if failure == "activation_restore":
                env["SEDNA_INSTALLER_TEST_FAULT"] = "after-visible-predecessor"
                env["SEDNA_INSTALLER_TESTING"] = "1"
            elif failure == "restore_temp_collision":
                env["SEDNA_INSTALLER_TEST_FAULT"] = "after-visible-replacement"
                env["SEDNA_INSTALLER_TESTING"] = "1"
                env["SEDNA_INSTALLER_TEST_RESTORE_SUFFIX"] = "collision"
            elif failure == "rollback_attacker_replacement":
                env["SEDNA_INSTALLER_TEST_FAULT"] = "replace-before-rollback"
                env["SEDNA_INSTALLER_TESTING"] = "1"
                env["SEDNA_INSTALLER_TEST_ATTACKER_VISIBLE"] = str(root / "attacker-visible")
                env["SEDNA_INSTALLER_TEST_ATTACKER_CURRENT"] = str(root / "attacker-current")
            elif failure == "empty_attacker_replacement":
                env["SEDNA_INSTALLER_TEST_FAULT"] = "replace-before-rollback"
                env["SEDNA_INSTALLER_TESTING"] = "1"
                env["SEDNA_INSTALLER_TEST_ATTACKER_VISIBLE"] = str(root / "attacker-visible")
                env["SEDNA_INSTALLER_TEST_ATTACKER_CURRENT"] = str(root / "attacker-current")
            elif failure == "activation_fault_inert":
                env["SEDNA_INSTALLER_TEST_FAULT"] = "after-visible-predecessor"
                env.pop("SEDNA_INSTALLER_TESTING", None)
            elif failure == "activation_empty_restore":
                env["SEDNA_INSTALLER_TEST_FAULT"] = "after-visible-replacement"
                env["SEDNA_INSTALLER_TESTING"] = "1"
            env.pop("GH_TOKEN", None)
            env.pop("GITHUB_TOKEN", None)
            verification_args = []
            if verification_args_override is not None:
                verification_args = verification_args_override
            elif arch == "x86_64" and not hardened:
                verification_args = ["--allow-historical-x86"]
            proc = subprocess.run(
                [
                    str(REPO_ROOT / "scripts/install_sedna_release_asset"),
                    "--repository",
                    "sednalabs/codex",
                    "--release-tag",
                    release_tag,
                    "--allow-prerelease",
                    *verification_args,
                    *(["--dry-run"] if dry_run else []),
                ],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            if predecessor_link is not None and failure == "activation_restore":
                visible_path = root / "home" / ".local" / "bin" / "codex"
                self.assertTrue(visible_path.is_symlink())
                self.assertEqual(visible_path.readlink(), predecessor_link)
            if failure == "activation_empty_restore":
                self.assertFalse((root / "home" / ".local" / "bin" / "codex").exists())
                self.assertFalse(
                    (root / "home" / ".codex" / "packages" / "standalone" / "current").exists()
                )
            if failure == "restore_temp_collision":
                visible_temp = root / "home" / ".local" / "bin" / ".codex.restore.collision"
                current_temp = root / "home" / ".codex" / "packages" / "standalone" / ".current.restore.collision"
                self.assertEqual(visible_temp.readlink(), root / "attacker-visible")
                self.assertEqual(current_temp.readlink(), root / "attacker-current")
                visible_path = root / "home" / ".local" / "bin" / "codex"
                current_path = root / "home" / ".codex" / "packages" / "standalone" / "current"
                self.assertFalse(visible_path.exists() or visible_path.is_symlink())
                self.assertFalse(current_path.exists() or current_path.is_symlink())
            if failure == "rollback_attacker_replacement":
                self.assertEqual(
                    (root / "home" / ".local" / "bin" / "codex").readlink(),
                    root / "attacker-visible",
                )
                self.assertEqual(
                    (root / "home" / ".codex" / "packages" / "standalone" / "current").readlink(),
                    root / "attacker-current",
                )
            if failure == "empty_attacker_replacement":
                self.assertEqual(
                    (root / "home" / ".local" / "bin" / "codex").readlink(),
                    root / "attacker-visible",
                )
                self.assertEqual(
                    (root / "home" / ".codex" / "packages" / "standalone" / "current").readlink(),
                    root / "attacker-current",
                )
            return proc

    def test_sedna_release_installer_executes_hardened_linux_fixture(self) -> None:
        for arch, target in (
            ("x86_64", "x86_64-unknown-linux-gnu"),
            ("aarch64", "aarch64-unknown-linux-gnu"),
        ):
            with self.subTest(arch=arch):
                proc = self.run_sedna_installer_fixture(arch=arch)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn(f"dry-run: verified sednalabs/codex@", proc.stdout)
                self.assertIn(f"for {target}", proc.stdout)

    def test_sedna_release_installer_serializes_activation_before_mutation(self) -> None:
        script = (REPO_ROOT / "scripts/install_sedna_release_asset").read_text(
            encoding="utf-8"
        )
        self.assertIn("acquire_activation_lock", script)
        self.assertIn('activation_started=true', script)
        self.assertIn('activation_previous_current="$(readlink', script)
        self.assertIn("refusing to overwrite an existing installer backup", script)
        self.assertIn("while os.getppid() == parent_pid", script)
        self.assertIn(
            "same-UID process that concurrently replaces directories",
            (REPO_ROOT / "docs/install.md").read_text(encoding="utf-8"),
        )

    def test_sedna_release_installer_restores_visible_predecessor_on_failure(self) -> None:
        proc = self.run_sedna_installer_fixture(
            "activation_restore", dry_run=False
        )
        self.assertEqual(proc.returncode, 73, proc.stderr)

    def test_sedna_release_installer_fault_hook_is_inert_without_testing_mode(self) -> None:
        proc = self.run_sedna_installer_fixture(
            "activation_fault_inert", dry_run=False
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("installed sednalabs/codex@", proc.stdout)

    def test_sedna_release_installer_removes_new_links_on_empty_predecessor_failure(self) -> None:
        proc = self.run_sedna_installer_fixture(
            "activation_empty_restore", dry_run=False
        )
        self.assertEqual(proc.returncode, 74, proc.stderr)

    def test_sedna_release_installer_fails_closed_on_restore_temp_collisions(self) -> None:
        proc = self.run_sedna_installer_fixture(
            "restore_temp_collision", dry_run=False
        )
        self.assertEqual(proc.returncode, 74, proc.stderr)

    def test_sedna_release_installer_preserves_empty_predecessor_replacements(self) -> None:
        proc = self.run_sedna_installer_fixture(
            "empty_attacker_replacement", dry_run=False
        )
        self.assertEqual(proc.returncode, 74, proc.stderr)

    def test_sedna_release_installer_rejects_symlinked_owned_paths(self) -> None:
        for failure in (
            "symlink_releases",
            "symlink_bin",
            "symlink_local_parent",
            "symlink_release_entry",
            "symlink_backups",
        ):
            with self.subTest(failure=failure):
                proc = self.run_sedna_installer_fixture(failure)
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("refusing symlinked", proc.stderr)

    def test_sedna_release_installer_rejects_invalid_release_assets(self) -> None:
        cases = {
            "missing_sbom": "missing required assets",
            "malformed_metadata": "metadata target",
            "malformed_sbom": "not an SPDX 2.3 document",
            "checksum_mismatch": "checksum mismatch",
            "unsafe_archive": "refusing unexpected archive member",
            "wrong_elf": "unexpected binary architecture",
            "wrong_source_commit": "does not match metadata target_commit",
            "existing_tampered": "does not match verified payload",
            "outdated_verifiers": "tool checksum mismatch",
        }
        for failure, expected in cases.items():
            with self.subTest(failure=failure):
                proc = self.run_sedna_installer_fixture(failure)
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn(expected, proc.stderr)

        arm_proc = self.run_sedna_installer_fixture(
            "arm_missing_sbom", arch="aarch64"
        )
        self.assertNotEqual(arm_proc.returncode, 0)
        self.assertIn("missing required assets", arm_proc.stderr)

        legacy_x86_proc = self.run_sedna_installer_fixture(
            "legacy_x86", arch="x86_64", hardened=False
        )
        self.assertEqual(legacy_x86_proc.returncode, 0, legacy_x86_proc.stderr)
        self.assertIn("for x86_64-unknown-linux-gnu", legacy_x86_proc.stdout)

        stale_updater_legacy_x86_proc = self.run_sedna_installer_fixture(
            "legacy_x86", arch="x86_64", hardened=True
        )
        self.assertEqual(
            stale_updater_legacy_x86_proc.returncode,
            0,
            stale_updater_legacy_x86_proc.stderr,
        )

        downgrade_current_x86_proc = self.run_sedna_installer_fixture(
            arch="x86_64", hardened=False
        )
        self.assertNotEqual(downgrade_current_x86_proc.returncode, 0)
        self.assertIn(
            "cannot downgrade a current release",
            downgrade_current_x86_proc.stderr,
        )

        signatures_only_proc = self.run_sedna_installer_fixture(
            "attestation_failure",
            verification_args_override=["--verify-signatures"],
        )
        self.assertNotEqual(signatures_only_proc.returncode, 0)

        attestation_only_proc = self.run_sedna_installer_fixture(
            "signature_failure",
            verification_args_override=["--verify-attestation"],
        )
        self.assertNotEqual(attestation_only_proc.returncode, 0)

        existing_matching_proc = self.run_sedna_installer_fixture(
            "existing_matching", dry_run=False
        )
        self.assertEqual(
            existing_matching_proc.returncode,
            0,
            existing_matching_proc.stderr,
        )

    def test_sedna_release_installer_python_elf_fallback(self) -> None:
        installer = (REPO_ROOT / "scripts/install_sedna_release_asset").read_text(
            encoding="utf-8"
        )
        marker = (
            'python3 - "$target" "$expected_elf_interpreter" '
            '"$staged/codex" "$staged/codex-responses-api-proxy" <<\'PY\'\n'
        )
        fallback = installer.split(marker, 1)[1].split("\nPY", 1)[0]

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            def write_elf(path: Path, machine: int, interpreter: str) -> None:
                encoded_interpreter = interpreter.encode() + b"\0"
                data = bytearray(120 + len(encoded_interpreter))
                data[:6] = b"\x7fELF\x02\x01"
                struct.pack_into("<H", data, 18, machine)
                struct.pack_into("<Q", data, 32, 64)
                struct.pack_into("<H", data, 54, 56)
                struct.pack_into("<H", data, 56, 1)
                struct.pack_into("<I", data, 64, 3)
                struct.pack_into("<Q", data, 72, 120)
                struct.pack_into("<Q", data, 96, len(encoded_interpreter))
                data[120:] = encoded_interpreter
                path.write_bytes(data)

            for target, machine, interpreter in (
                (
                    "x86_64-unknown-linux-gnu",
                    62,
                    "/lib64/ld-linux-x86-64.so.2",
                ),
                (
                    "aarch64-unknown-linux-gnu",
                    183,
                    "/lib/ld-linux-aarch64.so.1",
                ),
            ):
                with self.subTest(target=target):
                    codex = root / f"codex-{machine}"
                    proxy = root / f"proxy-{machine}"
                    write_elf(codex, machine, interpreter)
                    write_elf(proxy, machine, interpreter)
                    proc = subprocess.run(
                        [sys.executable, "-", target, interpreter, str(codex), str(proxy)],
                        input=fallback,
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(proc.returncode, 0, proc.stderr)

            malformed = root / "malformed"
            malformed.write_bytes(b"not-elf")
            proc = subprocess.run(
                [
                    sys.executable,
                    "-",
                    "x86_64-unknown-linux-gnu",
                    "/lib64/ld-linux-x86-64.so.2",
                    str(malformed),
                ],
                input=fallback,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("not a little-endian ELF64", proc.stderr)

            x86 = root / "codex-62"
            wrong_machine_proc = subprocess.run(
                [
                    sys.executable,
                    "-",
                    "aarch64-unknown-linux-gnu",
                    "/lib/ld-linux-aarch64.so.1",
                    str(x86),
                ],
                input=fallback,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(wrong_machine_proc.returncode, 0)
            self.assertIn("unexpected ELF machine", wrong_machine_proc.stderr)

            wrong_interpreter_proc = subprocess.run(
                [
                    sys.executable,
                    "-",
                    "x86_64-unknown-linux-gnu",
                    "/wrong/interpreter",
                    str(x86),
                ],
                input=fallback,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(wrong_interpreter_proc.returncode, 0)
            self.assertIn("uses ELF interpreter", wrong_interpreter_proc.stderr)

    def test_duplicate_workflow_finder_matches_same_branch_sha_success(self) -> None:
        runs = [
            {
                "id": 12,
                "head_branch": "main",
                "head_sha": "abc123",
                "status": "completed",
                "conclusion": "success",
                "event": "schedule",
                "html_url": "https://example.test/runs/12",
            },
            {
                "id": 11,
                "head_branch": "main",
                "head_sha": "abc123",
                "status": "completed",
                "conclusion": "success",
                "event": "workflow_dispatch",
                "html_url": "https://example.test/runs/11",
            },
        ]

        match = SKIP_DUPLICATE_WORKFLOW_RUN.find_equivalent_success(
            runs,
            branch="main",
            head_sha="abc123",
            current_run_id=12,
            allowed_events=set(),
        )

        self.assertEqual(match["id"], 11)
        self.assertEqual(
            SKIP_DUPLICATE_WORKFLOW_RUN.result_from_match(match),
            {
                "should_skip": "true",
                "should_run": "false",
                "proof_found": "true",
                "reason": "equivalent_success_found",
                "proof_reason": "equivalent_success_found",
                "matched_run_id": "11",
                "matched_run_url": "https://example.test/runs/11",
                "matched_run_event": "workflow_dispatch",
                "matched_run_created_at": "",
                "proof_run_id": "11",
                "proof_run_url": "https://example.test/runs/11",
                "evidence_key": "main:abc123",
            },
        )

    def test_duplicate_workflow_finder_ignores_wrong_sha_branch_or_failed_runs(self) -> None:
        runs = [
            {
                "id": 21,
                "head_branch": "main",
                "head_sha": "other",
                "status": "completed",
                "conclusion": "success",
                "event": "schedule",
            },
            {
                "id": 22,
                "head_branch": "feature",
                "head_sha": "abc123",
                "status": "completed",
                "conclusion": "success",
                "event": "schedule",
            },
            {
                "id": 23,
                "head_branch": "main",
                "head_sha": "abc123",
                "status": "completed",
                "conclusion": "failure",
                "event": "schedule",
            },
        ]

        self.assertIsNone(
            SKIP_DUPLICATE_WORKFLOW_RUN.find_equivalent_success(
                runs,
                branch="main",
                head_sha="abc123",
                current_run_id=None,
                allowed_events=set(),
            )
        )

    def test_duplicate_workflow_finder_requires_matching_summary_fingerprint(self) -> None:
        runs = [
            {
                "id": 31,
                "head_branch": "main",
                "head_sha": "abc123",
                "status": "completed",
                "conclusion": "success",
                "event": "workflow_dispatch",
            },
            {
                "id": 32,
                "head_branch": "main",
                "head_sha": "abc123",
                "status": "completed",
                "conclusion": "success",
                "event": "workflow_dispatch",
            },
        ]
        summaries = {
            31: {
                "selection": {"planner_fingerprint": "different"},
                "summary": {"overall_conclusion": "success"},
                "dedupe": {"should_skip": False},
            },
            32: {
                "selection": {"planner_fingerprint": "plan-fp"},
                "summary": {"overall_conclusion": "success"},
                "dedupe": {"should_skip": False},
            },
        }

        def metadata_matcher(run: dict) -> bool:
            return SKIP_DUPLICATE_WORKFLOW_RUN.validation_summary_matches(
                summaries[run["id"]],
                planner_fingerprint="plan-fp",
            )

        match = SKIP_DUPLICATE_WORKFLOW_RUN.find_equivalent_success(
            runs,
            branch="main",
            head_sha="abc123",
            current_run_id=None,
            allowed_events={"workflow_dispatch"},
            metadata_matcher=metadata_matcher,
        )

        self.assertEqual(match["id"], 32)

    def test_duplicate_workflow_finder_ignores_reused_summary_artifacts(self) -> None:
        payload = {
            "selection": {"planner_fingerprint": "plan-fp"},
            "summary": {"overall_conclusion": "success"},
            "dedupe": {"should_skip": True},
        }

        self.assertFalse(
            SKIP_DUPLICATE_WORKFLOW_RUN.validation_summary_matches(
                payload,
                planner_fingerprint="plan-fp",
            )
        )

    def test_artifact_download_drops_github_auth_on_signed_redirect(self) -> None:
        requests: list[object] = []

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self) -> bytes:
                return b"artifact bytes"

        def fake_open(request, timeout):
            del timeout
            requests.append(request)
            if len(requests) == 1:
                raise SKIP_DUPLICATE_WORKFLOW_RUN.urllib.error.HTTPError(
                    request.full_url,
                    302,
                    "Found",
                    {"Location": "https://signed-artifacts.example/archive.zip"},
                    None,
                )
            return FakeResponse()

        opener = mock.Mock()
        opener.open.side_effect = fake_open
        with mock.patch.object(
            SKIP_DUPLICATE_WORKFLOW_RUN.urllib.request,
            "build_opener",
            return_value=opener,
        ):
            payload = SKIP_DUPLICATE_WORKFLOW_RUN.api_get_bytes(
                "https://api.github.com/repos/sednalabs/codex/actions/artifacts/1/zip",
                "token-value",
            )

        self.assertEqual(payload, b"artifact bytes")
        first_headers = {key.lower(): value for key, value in requests[0].header_items()}
        second_headers = {key.lower(): value for key, value in requests[1].header_items()}
        self.assertEqual(first_headers.get("authorization"), "Bearer token-value")
        self.assertNotIn("authorization", second_headers)

    def test_github_api_url_validation_rejects_non_github_hosts(self) -> None:
        with self.assertRaises(ValueError):
            SKIP_DUPLICATE_WORKFLOW_RUN.validated_github_api_url(
                "https://example.test/repos/sednalabs/codex/actions/runs"
            )

    def test_duplicate_workflow_script_fails_open_for_bad_current_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "outputs"
            proc = subprocess.run(
                [
                    "python3",
                    str(SCRIPTS_DIR / "skip_duplicate_workflow_run.py"),
                    "--repo",
                    "sednalabs/codex",
                    "--workflow",
                    "rust-ci.yml",
                    "--branch",
                    "main",
                    "--head-sha",
                    "abc123",
                    "--current-run-id",
                    "not-an-int",
                    "--github-output",
                    str(output),
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            payload = json.loads(proc.stdout)
            output_lines = parse_github_output_file(output)

        self.assertEqual(payload["should_skip"], "false")
        self.assertEqual(payload["should_run"], "true")
        self.assertEqual(payload["reason"], "lookup_failed_run_conservatively")
        self.assertEqual(output_lines["should_skip"], "false")
        self.assertEqual(output_lines["should_run"], "true")
        self.assertEqual(output_lines["reason"], "lookup_failed_run_conservatively")

    def test_github_output_parser_tolerates_malformed_and_multiline_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "github-output.txt"
            output.write_text(
                "\n".join(
                    [
                        "plain=value",
                        "malformed",
                        "empty_key_is_ignored",
                        "=missing-key",
                        "multi<<EOF",
                        "line one",
                        "line two",
                        "EOF",
                        "later=still parsed",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            self.assertEqual(
                parse_github_output_file(output),
                {
                    "plain": "value",
                    "multi": "line one\nline two",
                    "later": "still parsed",
                },
            )

    def test_repository_workflows_follow_static_policy(self) -> None:
        self.assertEqual(CHECK_WORKFLOW_POLICY.collect_violations(REPO_ROOT), [])

    def test_sedna_release_manual_dispatch_defaults_to_auto_channel(self) -> None:
        workflow_path = REPO_ROOT / ".github/workflows/sedna-release.yml"
        payload = load_workflow_payload(workflow_path)
        inputs = (
            ((payload.get("on") or {}).get("workflow_dispatch") or {}).get("inputs")
            or {}
        )
        channel_input = inputs.get("channel") or {}
        macos_input = inputs.get("macos_release_mode") or {}

        self.assertEqual(
            {
                "default": channel_input.get("default"),
                "options": channel_input.get("options"),
            },
            {
                "default": "auto",
                "options": ["auto", "stable", "prerelease"],
            },
        )
        markerless_input = inputs.get("allow_markerless_prerelease") or {}
        self.assertEqual(
            {
                "default": markerless_input.get("default"),
                "type": markerless_input.get("type"),
            },
            {"default": "false", "type": "boolean"},
        )
        self.assertEqual(
            {
                "default": macos_input.get("default"),
                "options": macos_input.get("options"),
            },
            {
                "default": "off",
                "options": ["off", "preview", "notarized"],
            },
        )

        self.assertEqual(
            yaml.safe_load("default: off\noptions:\n  - off\n"),
            {"default": False, "options": [False]},
        )
        for provider_workflow in (
            workflow_path,
            REPO_ROOT / ".github/workflows/sedna-release-install.yml",
        ):
            with self.subTest(workflow=provider_workflow.name):
                provider_payload = yaml.safe_load(
                    provider_workflow.read_text(encoding="utf-8")
                )
                provider_on = (
                    provider_payload.get("on") or provider_payload.get(True) or {}
                )
                provider_inputs = (
                    (provider_on.get("workflow_dispatch") or {}).get("inputs") or {}
                )
                provider_macos_input = (
                    provider_inputs.get("macos_release_mode") or {}
                )
                self.assertEqual(
                    {
                        "default": provider_macos_input.get("default"),
                        "options": provider_macos_input.get("options"),
                    },
                    {
                        "default": "off",
                        "options": ["off", "preview", "notarized"],
                    },
                )

    def test_prerelease_main_command_dispatches_explicit_markerless_opt_in(self) -> None:
        justfile = (REPO_ROOT / "justfile").read_text(encoding="utf-8")
        self.assertIn("prerelease-main:", justfile)
        self.assertIn(
            "gh workflow run sedna-release.yml --repo sednalabs/codex --ref main "
            "-f channel=prerelease -f draft=false -f macos_release_mode=off "
            "-f allow_markerless_prerelease=true",
            justfile,
        )

    def test_sedna_release_main_pushes_are_routed_before_publisher(self) -> None:
        release_payload = load_workflow_payload(
            REPO_ROOT / ".github/workflows/sedna-release.yml"
        )
        release_push = ((release_payload.get("on") or {}).get("push") or {})
        jobs = release_payload.get("jobs") or {}
        route_job = jobs.get("route") or {}
        router_steps = (
            (route_job.get("steps") or [])
        )
        named_steps = {step.get("name"): step for step in router_steps if "name" in step}
        resolve_job = jobs.get("resolve") or {}
        release_job = jobs.get("release-linux") or {}
        publish_job = jobs.get("publish-release") or {}

        self.assertIn("main release gate", release_payload.get("run-name") or "")
        self.assertNotIn("concurrency", release_payload)
        self.assertEqual(route_job.get("name"), "Release request gate")
        self.assertEqual(route_job.get("runs-on"), "ubuntu-slim")
        self.assertEqual(release_push.get("branches"), ["main"])
        self.assertNotIn("tags", release_push)
        self.assertEqual(release_payload.get("permissions"), {})
        self.assertEqual(route_job.get("permissions"), {})
        self.assertFalse(any("uses" in step for step in router_steps))
        self.assertIn("HEAD_MESSAGE", named_steps["Resolve release request"].get("env") or {})
        self.assertIn("^Sedna-Release:", named_steps["Resolve release request"].get("run") or "")
        self.assertIn(
            '"${EVENT_NAME}" == "workflow_dispatch" && "${EVENT_REF}" != "refs/heads/main"',
            named_steps["Resolve release request"].get("run") or "",
        )
        self.assertIn(
            "Publisher job: ${publisher_job}",
            named_steps["Summarize release gate"].get("run") or "",
        )
        self.assertEqual(resolve_job.get("name"), "Resolve release metadata")
        self.assertEqual(resolve_job.get("needs"), "route")
        self.assertIn(
            "needs.route.outputs.release_requested == 'true'",
            resolve_job.get("if") or "",
        )
        self.assertIn("release_tag", resolve_job.get("outputs") or {})
        resolve_steps = resolve_job.get("steps") or []
        resolve_named_steps = {
            step.get("name"): step for step in resolve_steps if "name" in step
        }
        self.assertIn(
            "--missing-marker error",
            resolve_named_steps["Resolve release metadata"].get("run") or "",
        )
        resolve_metadata_step = resolve_named_steps["Resolve release metadata"]
        self.assertEqual(
            (resolve_metadata_step.get("env") or {}).get("HOST_SHA"),
            "${{ github.sha }}",
        )
        self.assertIn(
            'if [[ "${target_sha}" != "${HOST_SHA}" ]]',
            resolve_metadata_step.get("run") or "",
        )
        resolve_script = resolve_named_steps["Resolve release metadata"].get("run") or ""
        self.assertIn(
            'INPUT_ALLOW_MARKERLESS_PRERELEASE',
            resolve_metadata_step.get("env") or {},
        )
        self.assertIn(
            '[[ "${INPUT_CHANNEL}" == "prerelease" && "${INPUT_ALLOW_MARKERLESS_PRERELEASE}" == "true" ]]',
            resolve_script,
        )
        self.assertIn(
            "--require-marker --missing-marker error",
            resolve_script,
        )
        self.assertEqual(
            release_job.get("name"),
            "Build Linux release artifacts (${{ matrix.target }})",
        )
        self.assertEqual(release_job.get("needs"), "resolve")
        self.assertIn(
            "needs.resolve.outputs.release_requested == 'true'",
            release_job.get("if") or "",
        )
        self.assertEqual(
            release_job.get("concurrency"),
            {
                "group": "${{ github.workflow }}-${{ needs.resolve.outputs.release_tag }}-${{ matrix.target }}",
                "cancel-in-progress": "false",
            },
        )
        self.assertEqual(
            release_job.get("permissions"),
            {"contents": "read", "id-token": "write", "attestations": "write"},
        )
        self.assertEqual(
            ((release_job.get("strategy") or {}).get("matrix") or {}).get("include"),
            [
                {
                    "runner": "ubuntu-24.04",
                    "target": "x86_64-unknown-linux-gnu",
                    "artifact_name": "sedna-release-linux-x86_64-unknown-linux-gnu",
                    "checksum_name": "SHA256SUMS.txt",
                    "metadata_json_name": "RELEASE-METADATA.json",
                    "metadata_text_name": "RELEASE-METADATA.txt",
                },
                {
                    "runner": "ubuntu-24.04-arm",
                    "target": "aarch64-unknown-linux-gnu",
                    "artifact_name": "sedna-release-linux-aarch64-unknown-linux-gnu",
                    "checksum_name": "SHA256SUMS-aarch64-unknown-linux-gnu.txt",
                    "metadata_json_name": "RELEASE-METADATA-aarch64-unknown-linux-gnu.json",
                    "metadata_text_name": "RELEASE-METADATA-aarch64-unknown-linux-gnu.txt",
                },
            ],
        )
        self.assertNotIn("environment", release_job)
        self.assertEqual(publish_job.get("name"), "Publish GitHub release")
        self.assertEqual(
            publish_job.get("needs"),
            [
                "resolve",
                "release-linux",
                "release-macos-preview-package",
                "release-macos-finalize",
            ],
        )
        self.assertIn("always()", publish_job.get("if") or "")
        self.assertIn("macos_release_mode == 'off'", publish_job.get("if") or "")
        self.assertIn(
            "needs.release-linux.result == 'success'",
            publish_job.get("if") or "",
        )
        self.assertEqual(publish_job.get("environment"), "release")
        self.assertEqual(
            publish_job.get("permissions"),
            {"actions": "read"},
        )

    def test_sedna_release_uses_dedicated_github_app_for_publication(self) -> None:
        payload = load_workflow_payload(REPO_ROOT / ".github/workflows/sedna-release.yml")
        jobs = payload.get("jobs") or {}
        release_job = jobs.get("release-linux") or {}
        publish_job = jobs.get("publish-release") or {}
        release_steps = release_job.get("steps") or []
        steps = publish_job.get("steps") or []
        release_named_steps = {
            step.get("name"): step for step in release_steps if "name" in step
        }
        named_steps = {step.get("name"): step for step in steps if "name" in step}

        self.assertIn("Upload workflow artifacts", release_named_steps)
        self.assertEqual(
            release_named_steps["Generate SPDX SBOM"].get("uses"),
            "anchore/sbom-action@3ad7283483fc7af8ff2b4ea19663c2d5ca935e26",
        )
        self.assertEqual(
            release_named_steps["Attest Linux archive and SBOM provenance"].get("uses"),
            "actions/attest-build-provenance@4d101475d8b20a2381f78447822ac1eab6504dd8",
        )
        self.assertEqual(
            named_steps["Download Linux x86-64 release artifacts"].get("uses"),
            "actions/download-artifact@v8",
        )
        self.assertEqual(
            named_steps["Download Linux x86-64 release artifacts"].get("with") or {},
            {"name": "sedna-release-linux-x86_64-unknown-linux-gnu", "path": "dist"},
        )
        self.assertEqual(
            named_steps["Download Linux Arm64 release artifacts"].get("with") or {},
            {"name": "sedna-release-linux-aarch64-unknown-linux-gnu", "path": "dist"},
        )
        self.assertEqual(
            named_steps["Download Intel macOS release artifacts"].get("with") or {},
            {"name": "sedna-release-macos-x64", "path": "dist"},
        )
        self.assertEqual(
            named_steps["Download Intel macOS preview artifacts"].get("with") or {},
            {"name": "sedna-release-macos-x64-preview", "path": "dist"},
        )

        config_step = named_steps["Check release publisher app configuration"]
        self.assertEqual(
            {
                "APP_CLIENT_ID": (
                    (config_step.get("env") or {}).get("APP_CLIENT_ID")
                ),
                "APP_PRIVATE_KEY": (
                    (config_step.get("env") or {}).get("APP_PRIVATE_KEY")
                ),
            },
            {
                "APP_CLIENT_ID": "${{ vars.SEDNA_RELEASE_PUBLISHER_APP_CLIENT_ID }}",
                "APP_PRIVATE_KEY": "${{ secrets.SEDNA_RELEASE_PUBLISHER_APP_PRIVATE_KEY }}",
            },
        )
        self.assertIn(
            "Missing release publisher GitHub App configuration",
            config_step.get("run") or "",
        )

        token_step = named_steps["Generate release publisher app token"]
        self.assertEqual(token_step.get("id"), "release_publisher_token")
        self.assertEqual(
            token_step.get("uses"),
            "actions/create-github-app-token@1b10c78c7865c340bc4f6099eb2f838309f1e8c3",
        )
        self.assertEqual(
            token_step.get("with") or {},
            {
                "client-id": "${{ vars.SEDNA_RELEASE_PUBLISHER_APP_CLIENT_ID }}",
                "private-key": "${{ secrets.SEDNA_RELEASE_PUBLISHER_APP_PRIVATE_KEY }}",
                "permission-actions": "write",
                "permission-contents": "write",
            },
        )

        for step_name in ("Create GitHub release", "Dispatch release asset verifier"):
            self.assertEqual(
                (named_steps[step_name].get("env") or {}).get("GH_TOKEN"),
                "${{ steps.release_publisher_token.outputs.token }}",
            )
        self.assertEqual(publish_job.get("permissions"), {"actions": "read"})

    def test_sedna_release_verifier_checks_staged_binary_version_in_dry_run(self) -> None:
        installer = (REPO_ROOT / "scripts/install_sedna_release_asset").read_text(
            encoding="utf-8"
        )
        dry_run_index = installer.index('if [[ "$dry_run" == "true" ]]')
        version_check_index = installer.index(
            'staged_version_output="$("$staged/codex" --version 2>&1)"'
        )

        self.assertLess(version_check_index, dry_run_index)
        self.assertIn(
            "error: staged codex --version did not report %s: %s",
            installer,
        )
        self.assertIn('echo "$staged_version_output"', installer)

    def test_sedna_release_macos_x64_is_signed_notarized_and_verified(self) -> None:
        payload = load_workflow_payload(REPO_ROOT / ".github/workflows/sedna-release.yml")
        jobs = payload.get("jobs") or {}
        preflight = jobs.get("release-macos-signing-preflight") or {}
        build = jobs.get("release-macos-build") or {}
        sign = jobs.get("release-macos-sign") or {}
        package = jobs.get("release-macos-package") or {}
        sign_dmg = jobs.get("release-macos-sign-dmg") or {}
        finalize = jobs.get("release-macos-finalize") or {}

        self.assertEqual(preflight.get("runs-on"), "ubuntu-slim")
        self.assertIn(
            "macos_release_mode == 'notarized'",
            preflight.get("if") or "",
        )
        self.assertEqual(
            preflight.get("environment"),
            {"name": "codesigning", "deployment": "false"},
        )
        preflight_steps = {
            step.get("name"): step
            for step in preflight.get("steps") or []
            if step.get("name")
        }
        config = preflight_steps["Check Intel macOS signing configuration"]
        self.assertIn("Missing Intel macOS release signing configuration", config.get("run") or "")
        self.assertIn("APPLE_NOTARIZATION_KEY_P8", config.get("env") or {})

        self.assertEqual(build.get("runs-on"), "macos-15-intel")
        self.assertEqual(build.get("needs"), ["resolve", "release-macos-signing-preflight"])
        self.assertEqual((build.get("env") or {}).get("TARGET"), "x86_64-apple-darwin")
        self.assertEqual(sign.get("runs-on"), "ubuntu-24.04")
        self.assertIn(
            "macos_release_mode == 'notarized'",
            sign.get("if") or "",
        )
        self.assertEqual(sign.get("environment"), {"name": "codesigning", "deployment": "false"})
        self.assertEqual((sign.get("permissions") or {}).get("id-token"), "write")
        sign_steps = {step.get("name"): step for step in sign.get("steps") or [] if step.get("name")}
        self.assertIn(
            "notarize_macos_binary_with_rcodesign.sh",
            sign_steps["Sign and notarize Intel macOS binaries"].get("run") or "",
        )

        self.assertEqual(package.get("runs-on"), "macos-15-intel")
        package_steps = {
            step.get("name"): step for step in package.get("steps") or [] if step.get("name")
        }
        package_script = package_steps["Package Intel macOS release assets"].get("run") or ""
        self.assertIn("hdiutil create", package_script)
        self.assertIn("codex codex-responses-api-proxy", package_script)

        self.assertEqual(sign_dmg.get("environment"), {"name": "codesigning", "deployment": "false"})
        sign_dmg_steps = {
            step.get("name"): step for step in sign_dmg.get("steps") or [] if step.get("name")
        }
        self.assertIn(
            "notarize_macos_dmg_with_rcodesign.sh",
            sign_dmg_steps["Sign, notarize, and staple Intel macOS DMG"].get("run") or "",
        )

        self.assertEqual(finalize.get("runs-on"), "macos-15-intel")
        finalize_steps = {
            step.get("name"): step for step in finalize.get("steps") or [] if step.get("name")
        }
        verify_script = finalize_steps["Verify Intel macOS release assets"].get("run") or ""
        for evidence in (
            "lipo",
            "codesign --verify --strict",
            "diff -u",
            "hdiutil verify",
            "xcrun stapler validate",
            "hdiutil attach",
        ):
            self.assertIn(evidence, verify_script)

    def test_sedna_release_macos_preview_is_explicit_and_unnotarized(self) -> None:
        payload = load_workflow_payload(REPO_ROOT / ".github/workflows/sedna-release.yml")
        jobs = payload.get("jobs") or {}
        build = jobs.get("release-macos-build") or {}
        preview = jobs.get("release-macos-preview-package") or {}
        publish = jobs.get("publish-release") or {}
        resolve_script = workflow_step_by_name(
            REPO_ROOT / ".github/workflows/sedna-release.yml",
            "resolve",
            "Resolve release metadata",
        )["run"]

        self.assertIn('macos_release_mode="off"', resolve_script)
        self.assertIn(
            "Ad-hoc signed Intel macOS assets may only be attached to prereleases",
            resolve_script,
        )
        self.assertIn("always()", build.get("if") or "")
        self.assertIn("macos_release_mode == 'preview'", build.get("if") or "")
        self.assertEqual(preview.get("runs-on"), "macos-15-intel")
        self.assertEqual(preview.get("needs"), ["resolve", "release-macos-build"])
        self.assertIn("macos_release_mode == 'preview'", preview.get("if") or "")

        preview_steps = {
            step.get("name"): step
            for step in preview.get("steps") or []
            if step.get("name")
        }
        package_script = preview_steps[
            "Ad-hoc sign and package Intel macOS preview"
        ].get("run") or ""
        for evidence in (
            "--identity -",
            "Signature=adhoc",
            "UNNOTARIZED-PREVIEW",
            '"signing": "ad-hoc"',
            '"notarized": False',
            '"gatekeeper_ready": False',
        ):
            self.assertIn(evidence, package_script)

        publish_steps = {
            step.get("name"): step
            for step in publish.get("steps") or []
            if step.get("name")
        }
        create_script = publish_steps["Create GitHub release"].get("run") or ""
        self.assertIn("not Developer ID signed or notarized", create_script)
        self.assertIn("not an official supported macOS distribution", create_script)

    def test_sedna_release_installer_targets_native_linux_and_intel_macos(self) -> None:
        payload = load_workflow_payload(
            REPO_ROOT / ".github/workflows/sedna-release-install.yml"
        )
        jobs = payload.get("jobs") or {}
        plan = jobs.get("plan") or {}
        install = jobs.get("install") or {}
        self.assertEqual(
            payload.get("permissions"),
            {"contents": "read", "attestations": "read"},
        )
        matrix_script = workflow_step_by_name(
            REPO_ROOT / ".github/workflows/sedna-release-install.yml",
            "plan",
            "Build verification matrix",
        )["run"]

        def resolve_matrix(mode: str, *, require_arm64: bool = True) -> list[dict[str, str]]:
            with tempfile.TemporaryDirectory() as tmpdir:
                output_path = Path(tmpdir) / "github-output.txt"
                output_path.write_text("", encoding="utf-8")
                proc = subprocess.run(
                    ["bash", "-c", f"set -euo pipefail\n{matrix_script}"],
                    check=False,
                    capture_output=True,
                    text=True,
                    env={
                        **os.environ,
                        "GITHUB_OUTPUT": str(output_path),
                        "MACOS_RELEASE_MODE": mode,
                        "REQUIRE_LINUX_ARM64": str(require_arm64).lower(),
                    },
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                return json.loads(parse_github_output_file(output_path)["matrix"])

        linux = {
            "platform": "Linux x64",
            "runner": "ubuntu-24.04",
            "target": "x86_64-unknown-linux-gnu",
        }
        linux_arm = {
            "platform": "Linux Arm64",
            "runner": "ubuntu-24.04-arm",
            "target": "aarch64-unknown-linux-gnu",
        }
        macos = {
            "platform": "Intel macOS x64",
            "runner": "macos-15-intel",
            "target": "x86_64-apple-darwin",
        }
        self.assertEqual(resolve_matrix("off"), [linux, linux_arm])
        self.assertEqual(resolve_matrix("off", require_arm64=False), [linux])
        self.assertEqual(resolve_matrix("preview"), [linux, linux_arm, macos])
        self.assertEqual(resolve_matrix("notarized"), [linux, linux_arm, macos])
        self.assertEqual(plan.get("runs-on"), "ubuntu-slim")
        compatibility_step = workflow_step_by_name(
            REPO_ROOT / ".github/workflows/sedna-release-install.yml",
            "plan",
            "Validate x86-only compatibility request",
        )
        self.assertEqual(compatibility_step.get("if"), "${{ !inputs.require_linux_arm64 }}")
        self.assertIn("releases/tags/${RELEASE_TAG}", compatibility_step.get("run") or "")
        self.assertIn("-aarch64-unknown-linux-gnu", compatibility_step.get("run") or "")
        self.assertIn('> "${asset_names}"', compatibility_step.get("run") or "")
        gh_api_lines = [
            line
            for line in (compatibility_step.get("run") or "").splitlines()
            if "gh api" in line
        ]
        self.assertEqual(len(gh_api_lines), 1)
        self.assertNotIn("|| true", gh_api_lines[0])
        self.assertEqual(install.get("needs"), "plan")
        self.assertEqual(
            ((install.get("strategy") or {}).get("matrix") or {}).get("include"),
            "${{ fromJSON(needs.plan.outputs.matrix) }}",
        )
        installer = (REPO_ROOT / "scripts/install_sedna_release_asset").read_text(
            encoding="utf-8"
        )
        self.assertIn("Darwin/x86_64", installer)
        self.assertIn("Linux/aarch64", installer)
        self.assertIn("aarch64-unknown-linux-gnu", installer)
        self.assertIn("x86_64-apple-darwin", installer)
        self.assertIn("codesign --verify --strict", installer)
        self.assertIn("--macos-preview", installer)
        self.assertIn("UNNOTARIZED-PREVIEW", installer)
        self.assertIn("cosign verify-blob", installer)
        self.assertIn("gh attestation verify", installer)
        self.assertEqual(
            installer.count('--bundle "$work_dir/$attestation_bundle_name"'), 3
        )
        self.assertIn("sigstore/cosign/releases/download/v3.1.3", installer)
        self.assertIn("cli/cli/releases/download/v2.97.0", installer)
        self.assertIn("verification_tools_dir", installer)
        self.assertIn("verify_download_sha256", installer)
        self.assertIn("2026, 8, 16", installer)
        self.assertIn("published_at", installer)
        self.assertIn('.decode(\n            "utf-8", errors="strict"\n        )', installer)
        self.assertNotIn("hashlib.file_digest", installer)
        self.assertIn("--signer-workflow", installer)
        daemon_update_loop = (
            REPO_ROOT / "codex-rs/app-server-daemon/src/update_loop.rs"
        ).read_text(encoding="utf-8")
        sedna_updater = daemon_update_loop.split(
            "async fn install_latest_sedna_standalone", 1
        )[1]
        self.assertIn(".stderr(Stdio::inherit())", sedna_updater)
        self.assertEqual(installer.count('--source-digest "$expected_source_commit"'), 3)
        self.assertEqual(installer.count('--signer-digest "$expected_source_commit"'), 3)
        self.assertEqual(
            installer.count(
                '--certificate-github-workflow-sha "$expected_source_commit"'
            ),
            2,
        )
        deny_runner_lines = [
            line
            for line in installer.splitlines()
            if line.strip() == "--deny-self-hosted-runners " + "\\"
        ]
        self.assertEqual(len(deny_runner_lines), 3)
        self.assertIn("SPDX-2.3", installer)
        self.assertIn('verify_signatures=true', installer)
        self.assertIn('verify_attestation=true', installer)
        self.assertIn('release_dir_name="${release_tag}-${target}"', installer)
        self.assertIn('actual_target not in (None, expected_target)', installer)
        self.assertIn("readelf -l", installer)
        self.assertIn('"$staged/codex" exec --help', installer)
        self.assertIn('"$staged/codex" app-server --help', installer)
        verify_step = workflow_step_by_name(
            REPO_ROOT / ".github/workflows/sedna-release-install.yml",
            "install",
            "Verify release assets",
        )
        verify_script = verify_step.get("run") or ""
        self.assertIn("attestation_arg+=(--verify-signatures)", verify_script)
        self.assertIn("attestation_arg+=(--verify-attestation)", verify_script)
        self.assertIn("historical_x86_arg+=(--allow-historical-x86)", verify_script)
        self.assertIn('"${REQUIRE_LINUX_ARM64}" == "true"', verify_script)
        self.assertEqual(
            verify_script.count(
                'if [[ "${REQUIRE_LINUX_ARM64}" == "true" && "${TARGET}" == *-unknown-linux-gnu ]]'
            ),
            1,
        )
        cosign_step = workflow_step_by_name(
            REPO_ROOT / ".github/workflows/sedna-release-install.yml",
            "install",
            "Install Cosign",
        )
        self.assertEqual(
            cosign_step.get("if"),
            "${{ runner.os == 'Linux' && inputs.require_linux_arm64 }}",
        )

        release_payload = load_workflow_payload(
            REPO_ROOT / ".github/workflows/sedna-release.yml"
        )
        release_steps = (
            ((release_payload.get("jobs") or {}).get("release-linux") or {}).get("steps")
            or []
        )
        release_named_steps = {
            step.get("name"): step for step in release_steps if step.get("name")
        }
        stage_script = release_named_steps["Stage release assets"].get("run") or ""
        self.assertIn('install -m 0644 Cargo.toml Cargo.lock "${stage_dir}/"', stage_script)
        attest_subjects = (
            release_named_steps["Attest Linux archive and SBOM provenance"].get("with")
            or {}
        ).get("subject-path") or ""
        self.assertNotIn("codex-rs/dist/", attest_subjects)
        self.assertIn("${{ github.workspace }}/dist/", attest_subjects)
        self.assertIn("matrix.metadata_json_name", attest_subjects)
        self.assertIn("matrix.metadata_text_name", attest_subjects)
        sbom_output = (
            release_named_steps["Generate SPDX SBOM"].get("with") or {}
        ).get("output-file") or ""
        self.assertTrue(sbom_output.startswith("${{ github.workspace }}/dist/"))
        provenance_step = release_named_steps["Stage provenance bundle and checksums"]
        self.assertIsNone(provenance_step.get("working-directory"))
        self.assertEqual(
            (provenance_step.get("env") or {}).get("DIST_DIR"),
            "${{ github.workspace }}/dist",
        )
        self.assertIn('cd "${DIST_DIR}"', provenance_step.get("run") or "")

    def test_sedna_release_verifier_tag_grammar_matches_resolver_shape(self) -> None:
        install_workflow = (
            REPO_ROOT / ".github/workflows/sedna-release-install.yml"
        ).read_text(encoding="utf-8")
        installer = (REPO_ROOT / "scripts/install_sedna_release_asset").read_text(
            encoding="utf-8"
        )
        shared_tail = (
            r"(-[0-9A-Za-z]+(\.[0-9A-Za-z]+)*)?-sedna\.[0-9]+"
            r"(\+upstream\.[0-9]+)?"
        )

        self.assertIn(shared_tail, install_workflow)
        self.assertIn(shared_tail, installer)
        self.assertNotIn("([-.][0-9A-Za-z.]+)*-sedna", install_workflow)
        self.assertNotIn("([-.][0-9A-Za-z]+)*-sedna", installer)

    def test_sedna_release_router_detects_release_marker(self) -> None:
        script = workflow_step_by_name(
            REPO_ROOT / ".github/workflows/sedna-release.yml",
            "route",
            "Resolve release request",
        )["run"]
        event = {
            "ref": "refs/heads/main",
            "after": "abc123",
            "head_commit": {
                "message": "release commit\n\nSedna-Release: prerelease\n",
            },
        }

        proc, outputs = run_workflow_step_script(script, event)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            outputs,
            {
                "release_requested": "true",
                "reason": "release_marker",
                "target_sha": "abc123",
                "channel": "prerelease",
            },
        )

    def test_sedna_release_router_skips_unmarked_main_pushes(self) -> None:
        script = workflow_step_by_name(
            REPO_ROOT / ".github/workflows/sedna-release.yml",
            "route",
            "Resolve release request",
        )["run"]
        event = {
            "ref": "refs/heads/main",
            "after": "abc123",
            "head_commit": {
                "message": "ordinary maintenance commit",
            },
        }

        proc, outputs = run_workflow_step_script(script, event)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            outputs,
            {
                "release_requested": "false",
                "reason": "missing_sedna_release_marker",
                "target_sha": "abc123",
                "channel": "",
            },
        )

    def test_sedna_release_router_rejects_invalid_release_marker(self) -> None:
        script = workflow_step_by_name(
            REPO_ROOT / ".github/workflows/sedna-release.yml",
            "route",
            "Resolve release request",
        )["run"]
        event = {
            "ref": "refs/heads/main",
            "after": "abc123",
            "head_commit": {
                "message": "release commit\n\nSedna-Release: beta\n",
            },
        }

        proc, outputs = run_workflow_step_script(script, event)

        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(outputs, {})
        self.assertIn(
            "Sedna-Release marker must be either 'stable' or 'prerelease'",
            proc.stderr,
        )

    def test_sedna_release_router_rejects_off_main_manual_dispatch(self) -> None:
        script = workflow_step_by_name(
            REPO_ROOT / ".github/workflows/sedna-release.yml",
            "route",
            "Resolve release request",
        )["run"]
        event = {
            "ref": "refs/heads/release-candidate",
            "after": "abc123",
        }

        proc, outputs = run_workflow_step_script(
            script, event, event_name="workflow_dispatch"
        )

        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(outputs, {})
        self.assertIn(
            "Manual releases must be dispatched from the protected main branch",
            proc.stderr,
        )

    def test_sedna_release_does_not_accept_tag_push_authority(self) -> None:
        workflow_path = REPO_ROOT / ".github/workflows/sedna-release.yml"
        payload = load_workflow_payload(workflow_path)
        push = ((payload.get("on") or {}).get("push") or {})
        self.assertEqual(push, {"branches": ["main"]})
        self.assertNotIn("tags", push)

        script = workflow_step_by_name(
            workflow_path, "route", "Resolve release request"
        )["run"]
        for after in ("abc123", "0" * 40):
            with self.subTest(after=after):
                proc, outputs = run_workflow_step_script(
                    script,
                    {"ref": "refs/tags/v0.146.0-sedna.1", "after": after},
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(
                    outputs,
                    {
                        "release_requested": "false",
                        "reason": "unsupported_ref",
                        "target_sha": "",
                        "channel": "",
                    },
                )

    def test_sedna_release_uses_synced_upstream_mirror_as_version_base(self) -> None:
        workflow = (REPO_ROOT / ".github/workflows/sedna-release.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            "+refs/heads/upstream-main:refs/remotes/origin/upstream-main",
            workflow,
        )
        self.assertIn("--upstream-ref refs/remotes/origin/upstream-main", workflow)
        self.assertNotIn("--upstream-ref refs/remotes/upstream/main", workflow)

    def test_sedna_release_dispatches_public_asset_verification_only(self) -> None:
        release_workflow = (REPO_ROOT / ".github/workflows/sedna-release.yml").read_text(
            encoding="utf-8"
        )
        install_payload = load_workflow_payload(
            REPO_ROOT / ".github/workflows/sedna-release-install.yml"
        )
        install_job = ((install_payload.get("jobs") or {}).get("install") or {})
        plan_job = ((install_payload.get("jobs") or {}).get("plan") or {})
        plan_steps = {
            step.get("name"): step for step in plan_job.get("steps") or [] if "name" in step
        }

        self.assertIn("Dispatch release asset verifier", release_workflow)
        self.assertIn('--ref "${RELEASE_TAG}"', release_workflow)
        self.assertIn('-f "dry_run=true"', release_workflow)
        self.assertIn('-f "require_linux_arm64=true"', release_workflow)
        self.assertIn('-f "macos_release_mode=${MACOS_RELEASE_MODE}"', release_workflow)
        self.assertNotIn('-f "dry_run=false"', release_workflow)
        exact_ref_step = plan_steps["Require exact release-tag workflow ref"]
        self.assertEqual(
            (exact_ref_step.get("env") or {}).get("EVENT_REF"), "${{ github.ref }}"
        )
        self.assertIn('expected_ref="refs/tags/${RELEASE_TAG}"', exact_ref_step["run"])
        self.assertIn('"${EVENT_REF}" != "${expected_ref}"', exact_ref_step["run"])
        release_tag = "v0.146.0-alpha.8-sedna.99+upstream.3"
        for event_ref, expected_returncode in (
            (f"refs/tags/{release_tag}", 0),
            ("refs/heads/main", 1),
        ):
            with self.subTest(event_ref=event_ref):
                proc = subprocess.run(
                    ["bash", "-s"],
                    input=exact_ref_step["run"],
                    text=True,
                    capture_output=True,
                    check=False,
                    env={
                        **os.environ,
                        "EVENT_REF": event_ref,
                        "RELEASE_TAG": release_tag,
                    },
                )
                self.assertEqual(proc.returncode, expected_returncode, proc.stderr)
        self.assertEqual(install_job.get("runs-on"), "${{ matrix.runner }}")
        self.assertEqual(
            ((install_job.get("strategy") or {}).get("matrix") or {}).get("include"),
            "${{ fromJSON(needs.plan.outputs.matrix) }}",
        )
        workflow_json = json.dumps(install_payload, sort_keys=True)
        self.assertNotIn("self-hosted", workflow_json)
        self.assertIn("public workflow requires true", workflow_json)
        self.assertIn("--dry-run", workflow_json)
        self.assertIn("external deployment path", workflow_json)

    def test_sedna_release_reuses_caches_without_reusing_smoke_artifacts(self) -> None:
        payload = load_workflow_payload(REPO_ROOT / ".github/workflows/sedna-release.yml")
        release_job = ((payload.get("jobs") or {}).get("release-linux") or {})
        steps = release_job.get("steps") or []
        named_steps = {step.get("name"): step for step in steps if "name" in step}

        self.assertEqual(
            {
                "cargo_home_restore": named_steps["Restore cargo home cache"].get("uses"),
                "sccache_install": named_steps["Install sccache"].get("uses"),
                "sccache_configure_run": named_steps["Configure sccache backend"].get("run"),
                "sccache_restore": named_steps["Restore sccache cache (fallback)"].get("uses"),
                "cargo_home_save": named_steps["Save cargo home cache"].get("uses"),
                "sccache_save": named_steps["Save sccache cache (fallback)"].get("uses"),
            },
            {
                "cargo_home_restore": "actions/cache/restore@v6",
                "sccache_install": "taiki-e/install-action@5bf6ce016fd2e72eefc647cbca1e4213f65955b8",
                "sccache_configure_run": "bash .github/scripts/configure_sccache_backend.sh write-fallback",
                "sccache_restore": "actions/cache/restore@v6",
                "cargo_home_save": "actions/cache/save@v6",
                "sccache_save": "actions/cache/save@v6",
            },
        )

        self.assertIn(
            "steps.build_release.outcome == 'success'",
            named_steps["Save cargo home cache"].get("if") or "",
        )
        self.assertIn(
            "steps.build_release.outcome == 'success'",
            named_steps["Save sccache cache (fallback)"].get("if") or "",
        )
        self.assertIn(
            "steps.sccache_backend.outputs.policy == 'write-fallback'",
            named_steps["Save sccache cache (fallback)"].get("if") or "",
        )
        self.assertIn(
            "SCCACHE_BASEDIRS=${GITHUB_WORKSPACE}",
            named_steps["Enable sccache wrapper and reset stats"].get("run") or "",
        )
        self.assertEqual(
            (
                (named_steps["Build release binaries"].get("env") or {}).get(
                    "CODEX_UPSTREAM_DISTANCE_FROM_TAG"
                )
            ),
            "${{ needs.resolve.outputs.upstream_distance_from_tag }}",
        )
        self.assertIn(
            "upstream_position=${UPSTREAM_POSITION}",
            named_steps["Stage release assets"].get("run") or "",
        )
        self.assertIn(
            '"upstream_position": os.environ["UPSTREAM_POSITION"]',
            named_steps["Stage release assets"].get("run") or "",
        )
        self.assertEqual(named_steps["Build release binaries"].get("id"), "build_release")
        self.assertFalse(
            any(step.get("uses", "").startswith("actions/download-artifact") for step in steps)
        )

    def test_workflow_policy_rejects_missing_node_version_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workflow = root / ".github/workflows/ci.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                """
name: ci
on: push
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-node@v6
        with:
          node-version-file: codex-rs/node-version.txt
""".lstrip(),
                encoding="utf-8",
            )

            violations = CHECK_WORKFLOW_POLICY.collect_violations(root)

        self.assertEqual(
            violations,
            [
                ".github/workflows/ci.yml: actions/setup-node references missing "
                "node-version-file 'codex-rs/node-version.txt'; use node-version "
                "when the version is repository policy."
            ],
        )

    def test_workflow_policy_rejects_install_action_version_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workflow = root / ".github/workflows/ci.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                """
name: ci
on: push
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: taiki-e/install-action@v2
        with:
          tool: nextest
          version: 0.9.103
""".lstrip(),
                encoding="utf-8",
            )

            violations = CHECK_WORKFLOW_POLICY.collect_violations(root)

        self.assertEqual(
            violations,
            [
                ".github/workflows/ci.yml: taiki-e/install-action does not support "
                "with.version; use tool: nextest@0.9.103 instead."
            ],
        )

    def test_workflow_policy_rejects_self_hosted_runners_in_public_workflows(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workflow = root / ".github/workflows/deploy.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                """
name: deploy
on: workflow_dispatch
jobs:
  install:
    runs-on: [self-hosted, linux, x64, example-runner]
    steps:
      - run: true
""".lstrip(),
                encoding="utf-8",
            )

            violations = CHECK_WORKFLOW_POLICY.collect_violations(root)

        self.assertEqual(
            violations,
            [
                ".github/workflows/deploy.yml: self-hosted runners are not allowed; "
                "use external deployment automation for host-local operations."
            ],
        )

    def test_workflow_policy_rejects_larger_runner_matrix_labels(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workflow = root / ".github/workflows/release.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                """
name: release
on: workflow_dispatch
jobs:
  build:
    runs-on: ${{ matrix.runner }}
    strategy:
      matrix:
        include:
          - runner: macos-15-xlarge
          - runner: macos-15-large
          - runner: windows-2022-xlarge
          - runner: ${{ github.event.repository.name }}-linux-arm64
    steps:
      - run: true
""".lstrip(),
                encoding="utf-8",
            )

            violations = CHECK_WORKFLOW_POLICY.collect_violations(root)

        repo_scoped_runner = (
            ".github/workflows/release.yml: repo-scoped runner label "
            + "'${{ github.event.repository.name }}-linux-arm64' is not allowed; "
            + "use a standard public GitHub-hosted runner label."
        )
        macos_large_runner = (
            ".github/workflows/release.yml: runner label 'macos-15-large' uses "
            + "a larger-runner size token; use standard public GitHub-hosted "
            + "runner labels."
        )
        macos_xlarge_runner = (
            ".github/workflows/release.yml: runner label 'macos-15-xlarge' uses "
            + "a larger-runner size token; use standard public GitHub-hosted "
            + "runner labels."
        )
        windows_xlarge_runner = (
            ".github/workflows/release.yml: runner label 'windows-2022-xlarge' "
            + "uses a larger-runner size token; use standard public GitHub-hosted "
            + "runner labels."
        )
        self.assertEqual(
            violations,
            [
                repo_scoped_runner,
                macos_large_runner,
                macos_xlarge_runner,
                windows_xlarge_runner,
            ],
        )

    def test_workflow_policy_rejects_runner_group_selectors(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workflow = root / ".github/workflows/release.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                """
name: release
on: workflow_dispatch
jobs:
  build:
    runs-on:
      group: custom-runners
      labels: custom-windows-x64
    steps:
      - run: true
""".lstrip(),
                encoding="utf-8",
            )

            violations = CHECK_WORKFLOW_POLICY.collect_violations(root)

        runner_group_violation = (
            ".github/workflows/release.yml: runner groups are not allowed; use "
            + "standard public GitHub-hosted runner labels directly."
        )
        self.assertEqual(
            violations,
            [runner_group_violation],
        )

    def test_workflow_policy_rejects_runner_group_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workflow = root / ".github/workflows/reusable.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                """
name: reusable
on:
  workflow_call:
    inputs:
      runner:
        required: true
        type: string
      runner_group:
        required: false
        type: string
jobs:
  build:
    runs-on: ${{ inputs.runner }}
    steps:
      - run: true
""".lstrip(),
                encoding="utf-8",
            )

            violations = CHECK_WORKFLOW_POLICY.collect_violations(root)

        runner_group_input_violation = (
            ".github/workflows/reusable.yml: runner group inputs are not allowed; "
            + "use standard public GitHub-hosted runner labels directly."
        )
        self.assertEqual(
            violations,
            [runner_group_input_violation],
        )

    def test_workflow_policy_does_not_treat_unused_runs_on_as_runner_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workflow = root / ".github/workflows/release.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                """
name: release
on: workflow_dispatch
jobs:
  build:
    runs-on: ${{ matrix.runner }}
    strategy:
      matrix:
        include:
          - runner: macos-15-xlarge
            runs_on: ubuntu-24.04
    steps:
      - run: true
""".lstrip(),
                encoding="utf-8",
            )

            violations = CHECK_WORKFLOW_POLICY.collect_violations(root)

        xlarge_runner_violation = (
            ".github/workflows/release.yml: runner label 'macos-15-xlarge' uses "
            + "a larger-runner size token; use standard public GitHub-hosted "
            + "runner labels."
        )
        self.assertEqual(
            violations,
            [xlarge_runner_violation],
        )

    def test_workflow_policy_allows_standard_public_runners(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workflow = root / ".github/workflows/release.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                """
name: release
on: workflow_dispatch
jobs:
  build:
    runs-on: ${{ matrix.runner }}
    strategy:
      matrix:
        include:
          - runner: ubuntu-latest
          - runner: ubuntu-slim
          - runner: ubuntu-24.04
          - runner: ubuntu-24.04-arm
          - runner: ubuntu-26.04
          - runner: ubuntu-26.04-arm
          - runner: windows-latest
          - runner: windows-2022
          - runner: windows-2025
          - runner: windows-2025-vs2026
          - runner: windows-11-arm
          - runner: windows-11-vs2026-arm
          - runner: macos-latest
          - runner: macos-15
          - runner: macos-15-intel
          - runner: macos-26
          - runner: macos-26-intel
          - runner: macos-14
    steps:
      - run: true
""".lstrip(),
                encoding="utf-8",
            )

            violations = CHECK_WORKFLOW_POLICY.collect_violations(root)

        self.assertEqual(violations, [])

    def test_workflow_policy_rejects_release_install_dispatch_without_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workflow = root / ".github/workflows/release.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                """
name: release
on: workflow_dispatch
jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - run: |
          gh workflow run sedna-release-install.yml \\
            -f "release_tag=v0.126.0-sedna.1" \\
            -f "dry_run=false"
""".lstrip(),
                encoding="utf-8",
            )

            violations = CHECK_WORKFLOW_POLICY.collect_violations(root)

        self.assertEqual(
            violations,
            [
                ".github/workflows/release.yml: public workflows must dispatch "
                "sedna-release-install.yml with dry_run=true; use external "
                "deployment automation for host-local installs."
            ],
        )

    def test_workflow_policy_accepts_release_install_dry_run_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workflow = root / ".github/workflows/release.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                """
name: release
on: workflow_dispatch
jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - run: |
          gh workflow run sedna-release-install.yml \\
            -f "release_tag=v0.126.0-sedna.1" \\
            -f "dry_run=true"
""".lstrip(),
                encoding="utf-8",
            )

            violations = CHECK_WORKFLOW_POLICY.collect_violations(root)

        self.assertEqual(violations, [])

    def test_workflow_policy_rejects_release_install_script_without_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workflow = root / ".github/workflows/install.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                """
name: install
on: workflow_dispatch
jobs:
  install:
    runs-on: ubuntu-latest
    steps:
      - run: |
          scripts/install_sedna_release_asset \\
            --repository sednalabs/codex \\
            --release-tag v0.126.0-sedna.1
""".lstrip(),
                encoding="utf-8",
            )

            violations = CHECK_WORKFLOW_POLICY.collect_violations(root)

        self.assertEqual(
            violations,
            [
                ".github/workflows/install.yml: public workflows must call "
                "scripts/install_sedna_release_asset with --dry-run; use external "
                "deployment automation for host-local installs."
            ],
        )

    def test_workflow_policy_rejects_write_all_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workflow = root / ".github/workflows/ci.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                """
name: ci
on: push
permissions: write-all
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: true
""".lstrip(),
                encoding="utf-8",
            )

            violations = CHECK_WORKFLOW_POLICY.collect_violations(root)

        self.assertEqual(
            violations,
            [
                ".github/workflows/ci.yml: permissions must not use write-all; "
                "use job-scoped least privilege instead."
            ],
        )

    def test_workflow_policy_rejects_pull_request_target_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workflow = root / ".github/workflows/pr.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                """
name: pr
on: pull_request_target
permissions:
  contents: read
jobs:
  inspect:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
""".lstrip(),
                encoding="utf-8",
            )

            violations = CHECK_WORKFLOW_POLICY.collect_violations(root)

        self.assertEqual(
            violations,
            [
                ".github/workflows/pr.yml: pull_request_target jobs must not checkout "
                "repository code; split trusted writes from untrusted PR context."
            ],
        )

    def test_workflow_policy_rejects_unscoped_direct_release_create(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workflow = root / ".github/workflows/release.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                """
name: release
on: workflow_dispatch
permissions:
  contents: read
jobs:
  publish:
    runs-on: ubuntu-latest
    steps:
      - run: gh release create "$TAG" dist/*
""".lstrip(),
                encoding="utf-8",
            )

            violations = CHECK_WORKFLOW_POLICY.collect_violations(root)

        self.assertEqual(
            violations,
            [
                ".github/workflows/release.yml: job 'publish' creates a GitHub release "
                + "without the release environment.",
                ".github/workflows/release.yml: job 'publish' creates a GitHub release "
                + "without contents: write scoped to the publishing job.",
                ".github/workflows/release.yml: job 'publish' creates a GitHub release "
                + "without id-token: write for release signing or provenance.",
            ],
        )

    def test_workflow_policy_accepts_guarded_direct_release_create(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workflow = root / ".github/workflows/release.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                """
name: release
on: workflow_dispatch
permissions: {}
jobs:
  publish:
    runs-on: ubuntu-latest
    environment: release
    permissions:
      contents: write
      id-token: write
    steps:
      - run: gh release create "$TAG" dist/*
""".lstrip(),
                encoding="utf-8",
            )

            violations = CHECK_WORKFLOW_POLICY.collect_violations(root)

        self.assertEqual(violations, [])

    def test_workflow_policy_accepts_app_token_release_create_with_read_only_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workflow = root / ".github/workflows/release.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                """
name: release
on: workflow_dispatch
permissions: {}
jobs:
  publish:
    runs-on: ubuntu-latest
    environment: release
    permissions:
      actions: read
    steps:
      - uses: actions/download-artifact@v8
        with:
          name: release-assets
          path: dist
      - id: release_publisher_token
        uses: actions/create-github-app-token@v3
        with:
          client-id: app-id
          private-key: app-key
          permission-actions: write
          permission-contents: write
      - run: gh release create "$TAG" dist/*
        env:
          GH_TOKEN: ${{ steps.release_publisher_token.outputs.token }}
""".lstrip(),
                encoding="utf-8",
            )

            violations = CHECK_WORKFLOW_POLICY.collect_violations(root)

        self.assertEqual(violations, [])

    def test_configure_sccache_restore_only_uses_read_only_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            github_output = root / "github-output"
            github_env = root / "github-env"
            workspace = root / "workspace"
            workspace.mkdir()

            subprocess.run(
                [
                    "bash",
                    str(SCRIPTS_DIR / "configure_sccache_backend.sh"),
                    "restore-only",
                ],
                check=True,
                env={
                    **os.environ,
                    "GITHUB_OUTPUT": str(github_output),
                    "GITHUB_ENV": str(github_env),
                    "GITHUB_WORKSPACE": str(workspace),
                },
            )

            output = github_output.read_text(encoding="utf-8")
            env = github_env.read_text(encoding="utf-8")

        self.assertIn("policy=restore-only", output)
        self.assertIn("backend=fallback", output)
        self.assertIn("SCCACHE_GHA_ENABLED=false", env)
        self.assertIn(f"SCCACHE_DIR={workspace}/.sccache", env)
        self.assertNotIn("SCCACHE_GHA_ENABLED=true", env)

    def test_actions_cache_occupancy_summary_groups_refs_and_prefixes(self) -> None:
        summary = REPORT_ACTIONS_CACHE_OCCUPANCY.summarize_caches(
            [
                {
                    "key": "sccache/a/b/c",
                    "ref": "refs/pull/164/merge",
                    "size_in_bytes": 1024,
                },
                {
                    "key": "cargo-home-linux-rust-hash",
                    "ref": "refs/heads/main",
                    "size_in_bytes": 2048,
                },
                {
                    "key": "sccache/d/e/f",
                    "ref": "refs/pull/164/merge",
                    "size_in_bytes": 4096,
                },
            ]
        )

        self.assertEqual(summary["total_entries"], 3)
        self.assertEqual(summary["total_size_bytes"], 7168)
        self.assertEqual(
            summary["by_prefix"][0],
            {"name": "sccache", "entries": 2, "size_bytes": 5120},
        )
        self.assertEqual(summary["by_ref"][0]["name"], "refs/pull/164/merge")
        self.assertEqual(summary["by_ref"][0]["entries"], 2)

    def test_build_results_tolerates_selected_lane_missing_from_matrix(self) -> None:
        results = AGGREGATE_VALIDATION_SUMMARY.build_results(
            planned_matrix=[],
            selected_lane_ids=["lane.only.in.selection"],
            actual_by_lane={},
            smoke_gate_result="skipped",
            setup_class_results={},
            matrix_fail_fast=False,
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["lane_id"], "lane.only.in.selection")
        self.assertEqual(results[0]["outcome"], "missing")
        self.assertEqual(results[0]["summary_family"], "lane.only.in.selection")

    def test_aggregate_summary_treats_exact_plan_reuse_as_success(self) -> None:
        args = mock.Mock(
            dedupe_should_skip="true",
            dedupe_matched_run_url="https://example.test/runs/42",
        )

        self.assertEqual(
            AGGREGATE_VALIDATION_SUMMARY.overall_conclusion(
                primary=[{"kind": "lane"}],
                secondary=[],
                downstream_result="failure",
                args=args,
            ),
            "success",
        )

    def test_aggregate_summary_marks_stale_frontier_failures_for_targeted_latest_head_proof(self) -> None:
        args = mock.Mock(
            head_sha="1111111111111111111111111111111111111111",
            latest_head_sha="2222222222222222222222222222222222222222",
            smoke_gate_result="success",
            artifact_result="skipped",
            profile="frontier",
        )
        freshness = AGGREGATE_VALIDATION_SUMMARY.classify_head_freshness(
            args,
            queue=[
                {"kind": "lane", "lane_id": "codex.api-client-targeted", "outcome": "failure"},
                {"kind": "lane", "lane_id": "codex.api-types-targeted", "outcome": "failure"},
            ],
            candidate_next_slices=[
                {"kind": "lane", "lane_id": "codex.api-client-targeted", "signal": "API fixture failed"},
                {"kind": "lane", "lane_id": "codex.api-types-targeted", "signal": "API type drift"},
            ],
            downstream_result="failure",
        )

        self.assertEqual(freshness["run_head_status"], "stale")
        self.assertEqual(
            freshness["failed_lane_classification"],
            "needs_targeted_latest_head_proof",
        )
        self.assertEqual(
            freshness["recommended_rerun"]["lane_ids"],
            ["codex.api-client-targeted", "codex.api-types-targeted"],
        )
        self.assertEqual(freshness["recommended_rerun"]["profile"], "targeted")

    def test_aggregate_summary_marks_schema_fixture_failure_for_latest_head_proof(self) -> None:
        args = mock.Mock(
            head_sha="aaaaaaaabbbbbbbbccccccccddddddddeeeeeeee",
            latest_head_sha="ffffffffeeeeeeeeddddddddccccccccbbbbbbbb",
            smoke_gate_result="success",
            artifact_result="skipped",
            profile="targeted",
        )
        freshness = AGGREGATE_VALIDATION_SUMMARY.classify_head_freshness(
            args,
            queue=[
                {
                    "kind": "lane",
                    "lane_id": "codex.app-server-protocol-test",
                    "outcome": "failure",
                }
            ],
            candidate_next_slices=[
                {
                    "kind": "lane",
                    "lane_id": "codex.app-server-protocol-test",
                    "signal": "schema fixture drift",
                }
            ],
            downstream_result="failure",
        )

        self.assertEqual(freshness["run_head_status"], "stale")
        self.assertEqual(
            freshness["failed_lane_classification"],
            "needs_targeted_latest_head_proof",
        )
        self.assertEqual(
            freshness["recommended_rerun"]["lane_ids"],
            ["codex.app-server-protocol-test"],
        )

    def test_aggregate_summary_keeps_unknown_head_failure_active(self) -> None:
        args = mock.Mock(
            head_sha="aaaaaaaabbbbbbbbccccccccddddddddeeeeeeee",
            latest_head_sha="",
            smoke_gate_result="success",
            artifact_result="skipped",
            profile="targeted",
        )
        freshness = AGGREGATE_VALIDATION_SUMMARY.classify_head_freshness(
            args,
            queue=[
                {
                    "kind": "lane",
                    "lane_id": "codex.app-server-protocol-test",
                    "outcome": "failure",
                }
            ],
            candidate_next_slices=[
                {
                    "kind": "lane",
                    "lane_id": "codex.app-server-protocol-test",
                    "signal": "schema fixture drift",
                }
            ],
            downstream_result="failure",
        )

        self.assertEqual(freshness["run_head_status"], "unknown")
        self.assertEqual(freshness["failed_lane_classification"], "active")
        self.assertFalse(freshness["recommended_rerun"]["needed"])

    def test_aggregate_summary_marks_cancelled_release_smoke_as_cancelled(self) -> None:
        args = mock.Mock(
            head_sha="1234567890abcdef1234567890abcdef12345678",
            latest_head_sha="1234567890abcdef1234567890abcdef12345678",
            smoke_gate_result="cancelled",
            artifact_result="skipped",
            profile="checkpoint",
        )
        freshness = AGGREGATE_VALIDATION_SUMMARY.classify_head_freshness(
            args,
            queue=[
                {
                    "kind": "lane",
                    "lane_id": "codex.release-smoke",
                    "outcome": "cancelled",
                }
            ],
            candidate_next_slices=[
                {
                    "kind": "lane",
                    "lane_id": "codex.release-smoke",
                    "signal": "release smoke cancelled",
                }
            ],
            downstream_result="cancelled",
        )

        self.assertEqual(freshness["run_head_status"], "current")
        self.assertEqual(freshness["failed_lane_classification"], "cancelled")
        self.assertTrue(freshness["recommended_rerun"]["needed"])
        self.assertEqual(freshness["recommended_rerun"]["lane_ids"], ["codex.release-smoke"])

    def test_markdown_link_regex_excludes_optional_title(self) -> None:
        match = CHECK_MARKDOWN_LINKS.INLINE_LINK_RE.search(
            '[Spec](docs/example.md "Optional title")'
        )
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "docs/example.md")

    def test_resolve_target_treats_root_relative_paths_as_repo_relative(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            docs_dir = root / "docs"
            docs_dir.mkdir(parents=True, exist_ok=True)
            source = docs_dir / "guide.md"
            source.write_text("guide\n", encoding="utf-8")
            readme = root / "README.md"
            readme.write_text("root\n", encoding="utf-8")

            original_root = CHECK_MARKDOWN_LINKS.ROOT
            CHECK_MARKDOWN_LINKS.ROOT = root
            try:
                resolved = CHECK_MARKDOWN_LINKS.resolve_target(source, "/README.md")
            finally:
                CHECK_MARKDOWN_LINKS.ROOT = original_root

        self.assertEqual(resolved, readme.resolve())


class ValidationLaneRunnerTests(unittest.TestCase):
    def _run_downstream_divergence_audit_fixture(
        self,
        producer_source: str,
        *,
        stale_report: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir) / "repo"
            script_dir = repo_root / ".github/scripts/validation-lanes"
            scripts_dir = repo_root / "scripts"
            script_dir.mkdir(parents=True)
            scripts_dir.mkdir()

            (script_dir / "downstream-docs-check.sh").write_text(
                "#!/usr/bin/env bash\nset -euo pipefail\n",
                encoding="utf-8",
            )
            (script_dir / "downstream-docs-check.sh").chmod(0o755)
            (repo_root / ".github/scripts/sync_upstream_mirror.py").parent.mkdir(
                parents=True, exist_ok=True
            )
            (repo_root / ".github/scripts/sync_upstream_mirror.py").write_text(
                "import json\n"
                "print(json.dumps({'expected_mirror_sha': 'mirror', "
                "'mirror_audit_args': []}))\n",
                encoding="utf-8",
            )
            audit_script = scripts_dir / "downstream-divergence-audit.py"
            audit_script.write_text(producer_source, encoding="utf-8")

            subprocess.run(
                ["git", "init", "--initial-branch=main"],
                cwd=repo_root,
                check=True,
                stdout=subprocess.DEVNULL,
            )
            subprocess.run(
                ["git", "config", "user.name", "CI Planner Tests"],
                cwd=repo_root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "ci-planner-tests@example.invalid"],
                cwd=repo_root,
                check=True,
            )
            subprocess.run(["git", "add", "."], cwd=repo_root, check=True)
            subprocess.run(
                ["git", "commit", "-m", "fixture"],
                cwd=repo_root,
                check=True,
                stdout=subprocess.DEVNULL,
            )

            if stale_report is not None:
                report = (
                    repo_root
                    / "target/downstream-divergence-audit/downstream-divergence-audit.json"
                )
                report.parent.mkdir(parents=True, exist_ok=True)
                report.write_text(stale_report, encoding="utf-8")

            proc = subprocess.run(
                [
                    "bash",
                    str(
                        REPO_ROOT
                        / ".github/scripts/validation-lanes/downstream-divergence-audit.sh"
                    ),
                ],
                cwd=repo_root,
                check=False,
                capture_output=True,
                text=True,
            )

            return proc

    def test_downstream_divergence_audit_preserves_failure_without_report(self) -> None:
        proc = self._run_downstream_divergence_audit_fixture(
            "import sys\n"
            "print('producer failure: upstream audit aborted', file=sys.stderr)\n"
            "raise SystemExit(17)\n"
        )

        self.assertEqual(proc.returncode, 17)
        self.assertIn("producer failure: upstream audit aborted", proc.stderr)
        self.assertNotIn("FileNotFoundError", proc.stderr)

    def test_downstream_divergence_audit_discards_stale_report_on_failure(self) -> None:
        proc = self._run_downstream_divergence_audit_fixture(
            "import sys\n"
            "from pathlib import Path\n"
            "report = Path('target/downstream-divergence-audit/downstream-divergence-audit.json')\n"
            "if report.exists():\n"
            "    print('stale report was not removed', file=sys.stderr)\n"
            "    raise SystemExit(19)\n"
            "print('producer failure: upstream audit aborted', file=sys.stderr)\n"
            "raise SystemExit(17)\n",
            stale_report=json.dumps(
                {
                    "registry_reconciliation": {
                        "uncovered_code_paths": [],
                        "stale_entry_ids": ["stale-entry-from-previous-run"],
                    }
                }
            ),
        )

        self.assertEqual(proc.returncode, 17)
        self.assertIn("producer failure: upstream audit aborted", proc.stderr)
        self.assertNotIn("stale report was not removed", proc.stderr)
        self.assertNotIn("stale-entry-from-previous-run", proc.stderr)

    def test_downstream_divergence_audit_rejects_malformed_success_report(self) -> None:
        proc = self._run_downstream_divergence_audit_fixture(
            "from pathlib import Path\n"
            "output_dir = Path('target/downstream-divergence-audit')\n"
            "output_dir.mkdir(parents=True, exist_ok=True)\n"
            "(output_dir / 'downstream-divergence-audit.json').write_text('{not-json', encoding='utf-8')\n"
        )

        self.assertEqual(proc.returncode, 70)
        self.assertIn("artifact-contract failure", proc.stderr)
        self.assertIn("malformed downstream divergence audit report", proc.stderr)
        self.assertIn("report validation failed", proc.stderr)

    def test_downstream_divergence_audit_rejects_missing_success_report(self) -> None:
        proc = self._run_downstream_divergence_audit_fixture("# successful producer\n")

        self.assertEqual(proc.returncode, 70)
        self.assertIn("artifact-contract failure", proc.stderr)
        self.assertIn("did not produce", proc.stderr)

    def test_runner_executes_valid_paths_and_rejects_escape_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir) / "repo"
            workdir = repo_root / "workdir"
            script_dir = repo_root / ".github/scripts/validation-lanes"
            repo_root.mkdir(parents=True)
            workdir.mkdir(parents=True)
            script_dir.mkdir(parents=True)

            script_path = script_dir / "capture_pwd.sh"
            script_path.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env bash",
                        "set -euo pipefail",
                        "pwd > ../cwd.txt",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            valid = subprocess.run(
                [
                    "python3",
                    str(SCRIPTS_DIR / "run_validation_lane.py"),
                    "--repo-root",
                    str(repo_root),
                    "--working-directory",
                    "workdir",
                    "--script-path",
                    ".github/scripts/validation-lanes/capture_pwd.sh",
                    "--script-args-json",
                    "[]",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(valid.returncode, 0, valid.stderr)
            self.assertEqual(
                (repo_root / "cwd.txt").read_text(encoding="utf-8").strip(),
                str(workdir),
            )

            absolute_script = subprocess.run(
                [
                    "python3",
                    str(SCRIPTS_DIR / "run_validation_lane.py"),
                    "--repo-root",
                    str(repo_root),
                    "--working-directory",
                    "workdir",
                    "--script-path",
                    str(script_path),
                    "--script-args-json",
                    "[]",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(absolute_script.returncode, 0)
            self.assertIn(
                "must be a relative path within the repository root",
                absolute_script.stderr,
            )

            traversal_cwd = subprocess.run(
                [
                    "python3",
                    str(SCRIPTS_DIR / "run_validation_lane.py"),
                    "--repo-root",
                    str(repo_root),
                    "--working-directory",
                    "../workdir",
                    "--script-path",
                    ".github/scripts/validation-lanes/capture_pwd.sh",
                    "--script-args-json",
                    "[]",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(traversal_cwd.returncode, 0)
            self.assertIn(
                "must not contain '..' path segments",
                traversal_cwd.stderr,
            )


class ValidationLaneBatchRunnerTests(unittest.TestCase):
    def test_batch_runner_loads_catalog_from_target_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_root = root / "repo"
            output_dir = root / "out"
            script_dir = repo_root / ".github/scripts/validation-lanes"
            script_dir.mkdir(parents=True)

            (script_dir / "branch-only.sh").write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env bash",
                        "set -euo pipefail",
                        "echo branch-only-lane-ran",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            (repo_root / ".github/validation-lanes.json").write_text(
                json.dumps(
                    {
                        "lanes": [
                            {
                                "lane_id": "codex.branch-only-targeted",
                                "groups": ["core"],
                                "lane_sets": ["all"],
                                "status_class": "active",
                                "setup_class": "rust_integration",
                                "frontier_default": True,
                                "frontier_role": "sentinel",
                                "summary_family": "branch-only",
                                "cost_class": "high",
                                "checkout_fetch_depth": 1,
                                "timeout_minutes": 30,
                                "working_directory": ".",
                                "script_path": ".github/scripts/validation-lanes/branch-only.sh",
                                "script_args": [],
                                "needs_just": False,
                                "needs_node": False,
                                "needs_nextest": False,
                                "needs_linux_build_deps": False,
                                "needs_dotslash": False,
                                "needs_sccache": False,
                                "needs_bazel": False,
                            }
                        ]
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            fake_bin = root / "bin"
            fake_bin.mkdir()
            fake_df = fake_bin / "df"
            fake_df.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env bash",
                        "set -euo pipefail",
                        "printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\\n'",
                        "printf 'fake 100000000 1 100000000 1%% .\\n'",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            fake_df.chmod(0o755)

            proc = subprocess.run(
                [
                    "python3",
                    str(SCRIPTS_DIR / "run_validation_lane_batch.py"),
                    "--repo-root",
                    str(repo_root),
                    "--workflow-src",
                    str(REPO_ROOT),
                    "--setup-class",
                    "rust_integration",
                    "--batch-id",
                    "rust_integration-01",
                    "--lane-ids-json",
                    '["codex.branch-only-targeted"]',
                    "--output-dir",
                    str(output_dir),
                ],
                check=False,
                capture_output=True,
                text=True,
                env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"},
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("branch-only-lane-ran", proc.stdout)
            results = json.loads((output_dir / "batch-results.json").read_text(encoding="utf-8"))
            self.assertEqual(
                results["results"][0]["lane_id"],
                "codex.branch-only-targeted",
            )

    def test_batch_runner_reclaims_disk_headroom_before_first_lane(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_root = root / "repo"
            output_dir = root / "out"
            script_dir = repo_root / ".github/scripts/validation-lanes"
            script_dir.mkdir(parents=True)

            (script_dir / "assert-clean-target.sh").write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env bash",
                        "set -euo pipefail",
                        "test ! -e codex-rs/target/stale-artifact",
                        "echo first-lane-target-was-reclaimed",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            (repo_root / ".github/validation-lanes.json").write_text(
                json.dumps(
                    {
                        "lanes": [
                            {
                                "lane_id": "codex.low-disk-targeted",
                                "groups": ["core"],
                                "lane_sets": ["all"],
                                "status_class": "active",
                                "setup_class": "rust_integration",
                                "frontier_default": True,
                                "frontier_role": "sentinel",
                                "summary_family": "low-disk",
                                "cost_class": "high",
                                "checkout_fetch_depth": 1,
                                "timeout_minutes": 30,
                                "working_directory": ".",
                                "script_path": ".github/scripts/validation-lanes/assert-clean-target.sh",
                                "script_args": [],
                                "needs_just": False,
                                "needs_node": False,
                                "needs_nextest": False,
                                "needs_linux_build_deps": False,
                                "needs_dotslash": False,
                                "needs_sccache": False,
                                "needs_bazel": False,
                            }
                        ]
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            subprocess.run(
                ["git", "init", "--initial-branch=main"],
                cwd=repo_root,
                check=True,
                stdout=subprocess.DEVNULL,
            )
            subprocess.run(
                ["git", "config", "user.name", "CI Planner Tests"],
                cwd=repo_root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "ci-planner-tests@example.invalid"],
                cwd=repo_root,
                check=True,
            )
            subprocess.run(["git", "add", "."], cwd=repo_root, check=True)
            subprocess.run(
                ["git", "commit", "-m", "initial"],
                cwd=repo_root,
                check=True,
                stdout=subprocess.DEVNULL,
            )

            target_dir = repo_root / "codex-rs/target"
            target_dir.mkdir(parents=True)
            (target_dir / "stale-artifact").write_text("stale\n", encoding="utf-8")
            fake_bin = root / "bin"
            fake_bin.mkdir()
            fake_df = fake_bin / "df"
            fake_df.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env bash",
                        "set -euo pipefail",
                        "printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\\n'",
                        "printf 'fake 1 1 0 100%% .\\n'",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            fake_df.chmod(0o755)

            proc = subprocess.run(
                [
                    "python3",
                    str(SCRIPTS_DIR / "run_validation_lane_batch.py"),
                    "--repo-root",
                    str(repo_root),
                    "--workflow-src",
                    str(REPO_ROOT),
                    "--setup-class",
                    "rust_integration",
                    "--batch-id",
                    "rust_integration-01",
                    "--lane-ids-json",
                    '["codex.low-disk-targeted"]',
                    "--output-dir",
                    str(output_dir),
                ],
                check=False,
                capture_output=True,
                text=True,
                env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"},
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("first-lane-target-was-reclaimed", proc.stdout)
            self.assertIn(
                "Reclaiming validation workspace disk headroom",
                proc.stdout,
            )


class SednaReleaseVersionResolverTests(unittest.TestCase):
    def create_fixture(
        self,
        marker: str | None = "Sedna-Release: prerelease",
    ) -> tuple[TempGitRepo, str, str]:
        repo = TempGitRepo()
        old_upstream = repo.commit("upstream stable", {"upstream.txt": "stable\n"})
        repo._git("tag", "rust-v0.124.0", old_upstream)
        repo._git("tag", "rust-vv9.999.0", old_upstream)
        upstream = repo.commit("upstream alpha", {"upstream.txt": "alpha\n"})
        repo._git("tag", "rust-v0.126.0-alpha.3", upstream)
        repo._git("update-ref", "refs/remotes/origin/upstream-main", upstream)

        message = "downstream release"
        if marker is not None:
            message = f"{message}\n\n{marker}"
        downstream = repo.commit(message, {"downstream.txt": "release\n"})
        repo._git("update-ref", "refs/remotes/origin/main", downstream)
        return repo, upstream, downstream

    def resolve(
        self,
        repo: TempGitRepo,
        target: str,
        *,
        channel: str = "prerelease",
        release_tag: str | None = None,
        current_release_tag: str | None = None,
        require_marker: bool = False,
        missing_marker: str = "skip",
    ) -> dict:
        return RESOLVE_SEDNA_RELEASE_VERSION.resolve_release(
            repo=repo.root,
            target_sha=target,
            main_ref="refs/remotes/origin/main",
            upstream_ref="refs/remotes/origin/upstream-main",
            repository="",
            channel=channel,
            release_tag=release_tag,
            current_release_tag=current_release_tag,
            require_marker=require_marker,
            missing_marker=missing_marker,
            github_releases="off",
        )

    def test_prerelease_marker_computes_next_sedna_ordinal(self) -> None:
        repo, upstream, downstream = self.create_fixture()
        try:
            repo._git("tag", "v0.126.0-alpha.3-sedna.1", downstream)
            result = self.resolve(repo, downstream, channel="auto", require_marker=True)
        finally:
            repo.cleanup()

        self.assertEqual(
            {
                "release_requested": result["release_requested"],
                "release_channel": result["release_channel"],
                "release_tag": result["release_tag"],
                "release_version": result["release_version"],
                "github_prerelease": result["github_prerelease"],
                "upstream_track": result["upstream_track"],
                "upstream_base_tag": result["upstream_base_tag"],
                "upstream_position": result["upstream_position"],
                "build_provenance": result["build_provenance"],
            },
            {
                "release_requested": True,
                "release_channel": "prerelease",
                "release_tag": "v0.126.0-alpha.3-sedna.2",
                "release_version": "0.126.0-alpha.3-sedna.2",
                "github_prerelease": True,
                "upstream_track": "0.126.0-alpha.3",
                "upstream_base_tag": "rust-v0.126.0-alpha.3",
                "upstream_position": f"rust-v0.126.0-alpha.3@{upstream[:8]}",
                "build_provenance": f"up:rust-v0.126.0-alpha.3@{upstream[:8]} down:{downstream[:8]}",
            },
        )

    def test_upstream_position_marks_commits_after_upstream_tag(self) -> None:
        repo = TempGitRepo()
        tagged_upstream = repo.commit("upstream alpha tag", {"upstream.txt": "alpha\n"})
        repo._git("tag", "rust-v0.126.0-alpha.3", tagged_upstream)
        upstream_plus_one = repo.commit(
            "upstream alpha follow-up",
            {"upstream.txt": "alpha\nfollow-up\n"},
        )
        repo._git("update-ref", "refs/remotes/origin/upstream-main", upstream_plus_one)
        downstream = repo.commit(
            "downstream release\n\nSedna-Release: prerelease",
            {"downstream.txt": "release\n"},
        )
        repo._git("update-ref", "refs/remotes/origin/main", downstream)
        try:
            result = self.resolve(repo, downstream, channel="auto", require_marker=True)
        finally:
            repo.cleanup()

        self.assertEqual(result["upstream_distance_from_tag"], 1)
        self.assertEqual(result["upstream_base_tag_exact"], False)
        self.assertEqual(
            result["upstream_position"],
            f"rust-v0.126.0-alpha.3+1@{upstream_plus_one[:8]}",
        )
        self.assertEqual(
            result["release_tag"],
            "v0.126.0-alpha.3-sedna.1+upstream.1",
        )
        self.assertEqual(
            result["version_display"],
            "0.126.0-alpha.3-sedna.1+upstream.1 "
            f"(up:rust-v0.126.0-alpha.3+1@{upstream_plus_one[:8]} down:{downstream[:8]})",
        )

    def test_consecutive_release_markers_get_deterministic_ordinals(self) -> None:
        repo, _upstream, first_release = self.create_fixture()
        second_release = repo.commit(
            "second downstream release\n\nSedna-Release: prerelease",
            {"downstream.txt": "second release\n"},
        )
        repo._git("update-ref", "refs/remotes/origin/main", second_release)
        try:
            first_result = self.resolve(
                repo,
                first_release,
                channel="auto",
                require_marker=True,
            )
            second_result = self.resolve(
                repo,
                second_release,
                channel="auto",
                require_marker=True,
            )
        finally:
            repo.cleanup()

        self.assertEqual(
            {
                "first": first_result["release_tag"],
                "second": second_result["release_tag"],
            },
            {
                "first": "v0.126.0-alpha.3-sedna.1",
                "second": "v0.126.0-alpha.3-sedna.2",
            },
        )

    def test_release_marker_after_existing_tag_uses_next_ordinal(self) -> None:
        repo, _upstream, first_release = self.create_fixture()
        second_release = repo.commit(
            "second downstream release\n\nSedna-Release: prerelease",
            {"downstream.txt": "second release\n"},
        )
        repo._git("tag", "v0.126.0-alpha.3-sedna.1", first_release)
        repo._git("update-ref", "refs/remotes/origin/main", second_release)
        try:
            result = self.resolve(
                repo,
                second_release,
                channel="auto",
                require_marker=True,
            )
        finally:
            repo.cleanup()

        self.assertEqual(result["release_tag"], "v0.126.0-alpha.3-sedna.2")

    def test_supplied_tag_can_assert_second_pending_release_marker(self) -> None:
        repo, _upstream, _first_release = self.create_fixture()
        second_release = repo.commit(
            "second downstream release\n\nSedna-Release: prerelease",
            {"downstream.txt": "second release\n"},
        )
        repo._git("update-ref", "refs/remotes/origin/main", second_release)
        try:
            result = self.resolve(
                repo,
                second_release,
                channel="auto",
                release_tag="v0.126.0-alpha.3-sedna.2",
            )
        finally:
            repo.cleanup()

        self.assertEqual(result["release_tag"], "v0.126.0-alpha.3-sedna.2")

    def test_manual_markerless_release_without_tag_can_error(self) -> None:
        repo, _upstream, downstream = self.create_fixture(marker=None)
        try:
            with self.assertRaisesRegex(
                RESOLVE_SEDNA_RELEASE_VERSION.ReleaseVersionError,
                "manual markerless releases must supply release_tag",
            ):
                self.resolve(
                    repo,
                    downstream,
                    channel="auto",
                    require_marker=True,
                    missing_marker="error",
                )
        finally:
            repo.cleanup()

    def test_manual_auto_channel_infers_prerelease_from_upstream_track(self) -> None:
        repo, _upstream, downstream = self.create_fixture(marker=None)
        try:
            result = self.resolve(repo, downstream, channel="auto")
            with self.assertRaisesRegex(
                RESOLVE_SEDNA_RELEASE_VERSION.ReleaseVersionError,
                "does not match computed tag v0.126.0-alpha.3-sedna.1",
            ):
                self.resolve(
                    repo,
                    downstream,
                    channel="prerelease",
                    release_tag="v0.126.0-alpha.4-sedna.1",
                )
        finally:
            repo.cleanup()

        self.assertEqual(
            {
                "release_channel": result["release_channel"],
                "release_tag": result["release_tag"],
                "github_prerelease": result["github_prerelease"],
            },
            {
                "release_channel": "prerelease",
                "release_tag": "v0.126.0-alpha.3-sedna.1",
                "github_prerelease": True,
            },
        )

    def test_explicit_prerelease_channel_allows_markerless_tag_allocation(self) -> None:
        repo, _upstream, downstream = self.create_fixture(marker=None)
        try:
            result = self.resolve(repo, downstream, channel="prerelease")
        finally:
            repo.cleanup()

        self.assertEqual(result["release_channel"], "prerelease")
        self.assertEqual(result["release_tag"], "v0.126.0-alpha.3-sedna.1")
        self.assertTrue(result["github_prerelease"])

    def test_future_upstream_tag_is_not_used_for_older_synced_upstream_base(self) -> None:
        repo, upstream, downstream = self.create_fixture(marker=None)
        try:
            repo._git(
                "tag",
                "-a",
                "rust-v0.127.0-alpha.1",
                upstream,
                "-m",
                "Release 0.127.0-alpha.1",
                env={"GIT_COMMITTER_DATE": "2099-01-01T00:00:00+00:00"},
            )
            result = self.resolve(repo, downstream, channel="auto")
        finally:
            repo.cleanup()

        self.assertEqual(
            {
                "release_tag": result["release_tag"],
                "upstream_track": result["upstream_track"],
                "upstream_base_tag": result["upstream_base_tag"],
            },
            {
                "release_tag": "v0.126.0-alpha.3-sedna.1",
                "upstream_track": "0.126.0-alpha.3",
                "upstream_base_tag": "rust-v0.126.0-alpha.3",
            },
        )

    def test_upstream_mirror_ahead_of_target_does_not_advance_release_track(self) -> None:
        repo, upstream, downstream = self.create_fixture(marker=None)
        try:
            repo._git("checkout", "-B", "upstream-main-fixture", upstream)
            future_upstream = repo.commit(
                "upstream newer alpha",
                {"upstream.txt": "newer alpha\n"},
            )
            repo._git(
                "tag",
                "-a",
                "rust-v0.126.0-alpha.4",
                future_upstream,
                "-m",
                "Release 0.126.0-alpha.4",
                env={"GIT_COMMITTER_DATE": "2099-01-01T00:00:00+00:00"},
            )
            repo._git("update-ref", "refs/remotes/origin/upstream-main", future_upstream)
            repo._git("checkout", "main")

            result = self.resolve(repo, downstream, channel="auto")
        finally:
            repo.cleanup()

        self.assertEqual(
            {
                "release_tag": result["release_tag"],
                "upstream_track": result["upstream_track"],
                "upstream_base_commit": result["upstream_base_commit"],
                "upstream_base_tag": result["upstream_base_tag"],
            },
            {
                "release_tag": "v0.126.0-alpha.3-sedna.1",
                "upstream_track": "0.126.0-alpha.3",
                "upstream_base_commit": upstream,
                "upstream_base_tag": "rust-v0.126.0-alpha.3",
            },
        )

    def test_stable_channel_rejects_upstream_prerelease_track(self) -> None:
        repo, _upstream, downstream = self.create_fixture("Sedna-Release: stable")
        try:
            with self.assertRaisesRegex(
                RESOLVE_SEDNA_RELEASE_VERSION.ReleaseVersionError,
                "stable Sedna releases cannot use prerelease upstream track",
            ):
                self.resolve(repo, downstream, channel="auto", require_marker=True)
        finally:
            repo.cleanup()

    def test_missing_release_marker_is_clean_noop_for_main_pushes(self) -> None:
        repo, _upstream, downstream = self.create_fixture(marker=None)
        try:
            result = self.resolve(repo, downstream, channel="auto", require_marker=True)
        finally:
            repo.cleanup()

        self.assertEqual(
            result,
            {
                "release_requested": False,
                "skip_reason": "missing_sedna_release_marker",
                "target_commit": downstream,
                "version_policy": "sedna-upstream-track-v2",
            },
        )

    def test_supplied_tag_must_match_computed_tag(self) -> None:
        repo, _upstream, downstream = self.create_fixture()
        try:
            with self.assertRaisesRegex(
                RESOLVE_SEDNA_RELEASE_VERSION.ReleaseVersionError,
                "does not match computed tag v0.126.0-alpha.3-sedna.1",
            ):
                self.resolve(
                    repo,
                    downstream,
                    channel="prerelease",
                    release_tag="v0.126.0-alpha.3-sedna.2",
                )
        finally:
            repo.cleanup()

    def test_current_tag_is_ignored_for_tag_push_validation(self) -> None:
        repo, _upstream, downstream = self.create_fixture()
        try:
            repo._git("tag", "v0.126.0-alpha.3-sedna.1", downstream)
            result = self.resolve(
                repo,
                downstream,
                channel="auto",
                release_tag="v0.126.0-alpha.3-sedna.1",
                current_release_tag="v0.126.0-alpha.3-sedna.1",
            )
        finally:
            repo.cleanup()

        self.assertEqual(result["release_tag"], "v0.126.0-alpha.3-sedna.1")

    def test_existing_supplied_tag_for_target_can_be_released(self) -> None:
        repo, _upstream, downstream = self.create_fixture()
        try:
            repo._git("tag", "v0.126.0-alpha.3-sedna.1", downstream)
            result = self.resolve(
                repo,
                downstream,
                channel="prerelease",
                release_tag="v0.126.0-alpha.3-sedna.1",
            )
        finally:
            repo.cleanup()

        self.assertEqual(result["release_tag"], "v0.126.0-alpha.3-sedna.1")

    def test_existing_supplied_tag_must_point_at_target(self) -> None:
        repo, _upstream, downstream = self.create_fixture()
        try:
            other = repo.commit("other downstream", {"downstream.txt": "other\n"})
            repo._git("tag", "v0.126.0-alpha.3-sedna.1", other)
            with self.assertRaisesRegex(
                RESOLVE_SEDNA_RELEASE_VERSION.ReleaseVersionError,
                "not target commit",
            ):
                self.resolve(
                    repo,
                    downstream,
                    channel="prerelease",
                    release_tag="v0.126.0-alpha.3-sedna.1",
                )
        finally:
            repo.cleanup()

    def test_current_release_tag_is_ignored_after_remote_release_union(self) -> None:
        repo, _upstream, downstream = self.create_fixture()
        try:
            with mock.patch.object(
                RESOLVE_SEDNA_RELEASE_VERSION,
                "github_release_tags",
                return_value={"v0.126.0-alpha.3-sedna.1"},
            ):
                result = self.resolve(
                    repo,
                    downstream,
                    channel="auto",
                    release_tag="v0.126.0-alpha.3-sedna.1",
                    current_release_tag="v0.126.0-alpha.3-sedna.1",
                )
        finally:
            repo.cleanup()

        self.assertEqual(result["release_tag"], "v0.126.0-alpha.3-sedna.1")


if __name__ == "__main__":
    unittest.main()
