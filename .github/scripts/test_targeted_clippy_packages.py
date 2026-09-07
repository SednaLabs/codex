#!/usr/bin/env python3
"""Focused tests for the fast targeted-Clippy package selector."""

from __future__ import annotations

import unittest
from pathlib import Path, PurePosixPath

from targeted_clippy_packages import select_packages


class TargetedClippyPackagesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path("/repo")
        self.packages = [
            (PurePosixPath("codex-rs/core"), "codex-core"),
            (PurePosixPath("codex-rs/app-server"), "codex-app-server"),
        ]

    def test_selects_packages_for_rust_sources_and_manifests(self) -> None:
        paths = [
            "codex-rs/core/src/lib.rs",
            "codex-rs/app-server/Cargo.toml",
            "docs/README.md",
        ]
        self.assertEqual(
            select_packages(self.repo_root, paths, self.packages),
            ["codex-app-server", "codex-core"],
        )

    def test_ignores_workspace_and_non_rust_paths(self) -> None:
        paths = [
            "codex-rs/Cargo.toml",
            "codex-rs/Cargo.lock",
            "codex-rs/core/README.md",
            ".github/workflows/rust-ci.yml",
        ]
        self.assertEqual(select_packages(self.repo_root, paths, self.packages), [])


if __name__ == "__main__":
    unittest.main()
