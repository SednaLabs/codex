#!/usr/bin/env python3
"""Exercise the production targeted-Clippy JSON-to-argv bridge."""

from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".github" / "scripts" / "run_targeted_clippy.sh"
BASE_ARGS = [
    "clippy",
    "--target",
    "x86_64-unknown-linux-gnu",
    "--all-features",
    "--tests",
    "--profile",
    "dev",
    "--no-deps",
]


class RunTargetedClippyTests(unittest.TestCase):
    def run_helper(
        self, packages: str, cargo_exit: int = 0
    ) -> tuple[subprocess.CompletedProcess[str], list[str] | None]:
        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            bin_dir = temp / "bin"
            bin_dir.mkdir()
            argv_file = temp / "argv.json"
            fake_cargo = bin_dir / "cargo"
            fake_cargo.write_text(
                "#!/usr/bin/env python3\n"
                "import json, pathlib, sys\n"
                f"pathlib.Path({str(argv_file)!r}).write_text(json.dumps(sys.argv[1:]))\n"
                f"raise SystemExit({cargo_exit})\n",
                encoding="utf-8",
            )
            fake_cargo.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
            env["TARGETED_CLIPPY_PACKAGES"] = packages
            result = subprocess.run(
                ["bash", str(SCRIPT)],
                cwd=ROOT / "codex-rs",
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
            argv = (
                json.loads(argv_file.read_text(encoding="utf-8"))
                if argv_file.exists()
                else None
            )
            return result, argv

    def test_zero_packages_runs_base_command(self) -> None:
        result, argv = self.run_helper("[]")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(argv, [*BASE_ARGS, "--", "-D", "warnings"])

    def test_one_and_two_packages_preserve_exact_argv(self) -> None:
        for packages in (["codex-core"], ["codex-core", "codex-goal-extension"]):
            with self.subTest(packages=packages):
                result, argv = self.run_helper(json.dumps(packages))
                expected = [*BASE_ARGS]
                for package in packages:
                    expected.extend(["--package", package])
                expected.extend(["--", "-D", "warnings"])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(argv, expected)

    def test_invalid_json_fails_before_cargo(self) -> None:
        for packages in ("not-json", "{}", '["codex-core", 7]'):
            with self.subTest(packages=packages):
                result, argv = self.run_helper(packages)
                self.assertNotEqual(result.returncode, 0)
                self.assertIsNone(argv)

    def test_cargo_exit_status_is_preserved(self) -> None:
        result, argv = self.run_helper(
            '["codex-core", "codex-goal-extension"]', cargo_exit=37
        )
        self.assertEqual(result.returncode, 37, result.stderr)
        expected = [*BASE_ARGS]
        expected.extend(["--package", "codex-core", "--package", "codex-goal-extension"])
        expected.extend(["--", "-D", "warnings"])
        self.assertEqual(argv, expected)


if __name__ == "__main__":
    unittest.main()
