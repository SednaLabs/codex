#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_native_visual_response_contract import (  # noqa: E402
    EXPECTED_PRODUCTION_QUERIES,
    EXPECTED_PROVIDER_FILES,
    PACK_ROOT,
    PRODUCTION_SUITE,
    verify,
)


class VerifyNativeVisualResponseContractTests(unittest.TestCase):
    def make_repo(self, root: Path) -> None:
        for relative_path in EXPECTED_PROVIDER_FILES:
            path = root / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("// fixture\n", encoding="utf-8")

        for query in EXPECTED_PRODUCTION_QUERIES:
            path = root / PACK_ROOT / query
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("// query fixture\n", encoding="utf-8")

        suite_path = root / PRODUCTION_SUITE
        suite_path.parent.mkdir(parents=True, exist_ok=True)
        suite_path.write_text(
            "- description: fixture\n"
            + "".join(f"- query: {query}\n" for query in EXPECTED_PRODUCTION_QUERIES),
            encoding="utf-8",
        )

    def test_valid_contract_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self.make_repo(root)
            self.assertEqual(verify(root), [])

    def test_missing_provider_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self.make_repo(root)
            missing = EXPECTED_PROVIDER_FILES[0]
            (root / missing).unlink()
            self.assertIn(
                f"missing production visual provider: {missing.as_posix()}",
                verify(root),
            )

    def test_missing_suite_member_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self.make_repo(root)
            suite_path = root / PRODUCTION_SUITE
            suite_path.write_text(
                "- description: fixture\n"
                + "".join(
                    f"- query: {query}\n" for query in EXPECTED_PRODUCTION_QUERIES[:-1]
                ),
                encoding="utf-8",
            )
            errors = verify(root)
            self.assertTrue(
                any(
                    error.startswith("production CodeQL suite membership mismatch:")
                    for error in errors
                ),
                errors,
            )

    def test_extra_suite_member_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self.make_repo(root)
            suite_path = root / PRODUCTION_SUITE
            suite_path.write_text(
                suite_path.read_text(encoding="utf-8")
                + "- query: queries/UnexpectedProductionQuery.ql\n",
                encoding="utf-8",
            )
            errors = verify(root)
            self.assertTrue(
                any(
                    error.startswith("production CodeQL suite membership mismatch:")
                    for error in errors
                ),
                errors,
            )

    def test_missing_query_file_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self.make_repo(root)
            missing_query = EXPECTED_PRODUCTION_QUERIES[1]
            (root / PACK_ROOT / missing_query).unlink()
            self.assertIn(
                f"missing production CodeQL query: {(PACK_ROOT / missing_query).as_posix()}",
                verify(root),
            )


if __name__ == "__main__":
    unittest.main()
