#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from classify_ci_paths import CODEQL_ALL, classify  # noqa: E402


class ClassifyCiPathsTests(unittest.TestCase):
    def test_empty_change_set_selects_full_validation(self) -> None:
        scope = classify([])
        self.assertTrue(scope.force_full_blocking)
        self.assertTrue(scope.force_full_codeql)
        self.assertTrue(scope.clippy)
        self.assertTrue(scope.cargo_deny)
        self.assertTrue(scope.repo_policy)
        self.assertTrue(scope.repo_package)
        self.assertTrue(scope.repo_format)
        self.assertTrue(scope.repo_readme)
        self.assertTrue(scope.sdk_python)
        self.assertTrue(scope.sdk_typescript)
        self.assertEqual(scope.codeql_languages, CODEQL_ALL)

    def test_root_readme_is_lightweight(self) -> None:
        scope = classify(["README.md"])
        self.assertTrue(scope.repo_readme)
        self.assertFalse(scope.cargo_deny)
        self.assertFalse(scope.clippy)
        self.assertFalse(scope.repo_policy)
        self.assertFalse(scope.repo_package)
        self.assertFalse(scope.repo_format)
        self.assertFalse(scope.sdk_python)
        self.assertFalse(scope.sdk_typescript)
        self.assertEqual(scope.codeql_languages, ())

    def test_cargo_manifest_selects_dependency_policy_and_rust_consumers(self) -> None:
        scope = classify(["codex-rs/core/Cargo.toml"])
        self.assertTrue(scope.clippy)
        self.assertTrue(scope.cargo_deny)
        self.assertTrue(scope.repo_policy)
        self.assertTrue(scope.repo_format)
        self.assertTrue(scope.sdk_typescript)
        self.assertIn("rust", scope.codeql_languages)

    def test_python_sdk_change_does_not_select_typescript_sdk(self) -> None:
        scope = classify(["sdk/python/src/openai_codex/client.py"])
        self.assertTrue(scope.sdk_python)
        self.assertFalse(scope.sdk_typescript)
        self.assertEqual(scope.codeql_languages, ("python",))

    def test_typescript_sdk_change_selects_only_js_codeql(self) -> None:
        scope = classify(["sdk/typescript/src/index.ts"])
        self.assertTrue(scope.sdk_typescript)
        self.assertFalse(scope.sdk_python)
        self.assertEqual(scope.codeql_languages, ("javascript-typescript",))

    def test_packaging_change_selects_package_lane(self) -> None:
        scope = classify(["scripts/stage_npm_packages.py"])
        self.assertTrue(scope.repo_package)
        self.assertTrue(scope.repo_format)
        self.assertEqual(scope.codeql_languages, ("python",))

    def test_workflow_change_selects_actions_codeql(self) -> None:
        scope = classify([".github/workflows/docs-sanity.yml"])
        self.assertEqual(scope.codeql_languages, ("actions",))

    def test_workflow_changes_select_required_clippy(self) -> None:
        self.assertTrue(classify([".github/workflows/docs-sanity.yml"]).clippy)

    def test_unrelated_python_change_does_not_select_required_clippy(self) -> None:
        self.assertFalse(classify(["sdk/python/src/openai_codex/client.py"]).clippy)

    def test_codeql_config_change_forces_all_codeql_languages(self) -> None:
        scope = classify([".github/codeql/codeql-config.yml"])
        self.assertEqual(scope.codeql_languages, CODEQL_ALL)
        self.assertTrue(scope.force_full_codeql)

    def test_native_visual_contract_changes_select_repo_policy(self) -> None:
        for path in (
            ".github/codeql/rust-computer-use-contract/suites/rust-computer-use-production.qls",
            (
                ".github/codeql/rust-computer-use-contract/queries/"
                "NativeVisualResponseCoverageWitness.ql"
            ),
            ".github/scripts/verify_native_visual_response_contract.py",
            ".github/scripts/test_verify_native_visual_response_contract.py",
            "codex-rs/android-computer-use/src/lib.rs",
            "codex-rs/browser-computer-use/src/lib.rs",
        ):
            with self.subTest(path=path):
                self.assertTrue(classify([path]).repo_policy)

    def test_routing_change_forces_full_blocking_and_codeql(self) -> None:
        scope = classify([".github/scripts/classify_ci_paths.py"])
        self.assertTrue(scope.force_full_blocking)
        self.assertTrue(scope.cargo_deny)
        self.assertTrue(scope.repo_policy)
        self.assertTrue(scope.repo_package)
        self.assertTrue(scope.repo_format)
        self.assertTrue(scope.repo_readme)
        self.assertTrue(scope.sdk_python)
        self.assertTrue(scope.sdk_typescript)
        self.assertEqual(scope.codeql_languages, CODEQL_ALL)


if __name__ == "__main__":
    unittest.main()
