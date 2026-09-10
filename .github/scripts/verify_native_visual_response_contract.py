#!/usr/bin/env python3
"""Verify fail-closed native visual-response CodeQL production wiring."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
PACK_ROOT = Path(".github/codeql/rust-computer-use-contract")
PRODUCTION_SUITE = PACK_ROOT / "suites/rust-computer-use-production.qls"

EXPECTED_PROVIDER_FILES = (
    Path("codex-rs/android-computer-use/src/lib.rs"),
    Path("codex-rs/browser-computer-use/src/lib.rs"),
)

EXPECTED_PRODUCTION_QUERIES = (
    "queries/AndroidVisualToolMissingNativeImageGuard.ql",
    "queries/NativeVisualResponseProductionCoverageMissing.ql",
    "queries/NativeVisualResponseCoverageWitness.ql",
)


def production_suite_queries(path: Path) -> tuple[str, ...]:
    queries: list[str] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line.startswith("- query:"):
            continue
        query = line.partition(":")[2].strip()
        if not query:
            raise ValueError(f"{path}:{line_number}: empty query entry")
        queries.append(query)
    return tuple(queries)


def verify(root: Path = REPO_ROOT) -> list[str]:
    errors: list[str] = []

    for relative_path in EXPECTED_PROVIDER_FILES:
        path = root / relative_path
        if not path.is_file():
            errors.append(f"missing production visual provider: {relative_path.as_posix()}")

    suite_path = root / PRODUCTION_SUITE
    if not suite_path.is_file():
        errors.append(f"missing production CodeQL suite: {PRODUCTION_SUITE.as_posix()}")
        return errors

    try:
        actual_queries = production_suite_queries(suite_path)
    except (OSError, ValueError) as exc:
        errors.append(str(exc))
        return errors

    if actual_queries != EXPECTED_PRODUCTION_QUERIES:
        errors.append(
            "production CodeQL suite membership mismatch: "
            f"expected {EXPECTED_PRODUCTION_QUERIES!r}, got {actual_queries!r}"
        )

    for query in EXPECTED_PRODUCTION_QUERIES:
        query_path = root / PACK_ROOT / query
        if not query_path.is_file():
            errors.append(f"missing production CodeQL query: {(PACK_ROOT / query).as_posix()}")

    return errors


def main() -> int:
    errors = verify()
    if errors:
        for error in errors:
            print(f"native visual response contract: {error}")
        return 1
    print("native visual response production contract verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
