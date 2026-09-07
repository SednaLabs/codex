#!/usr/bin/env python3
"""Hosted fixture coverage for the typed early-failure diagnostic producer."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import aggregate_validation_summary as aggregate


SCRIPTS_DIR = Path(__file__).resolve().parent
PRODUCER = SCRIPTS_DIR / "write_failure_diagnostic.py"
LANE_SUMMARY = SCRIPTS_DIR / "write_lane_summary.py"


def identity_args() -> list[str]:
    return [
        "--repository",
        "sednalabs/codex",
        "--source-sha",
        "1111111111111111111111111111111111111111",
        "--execution-sha",
        "2222222222222222222222222222222222222222",
        "--workflow-sha",
        "3333333333333333333333333333333333333333",
        "--event",
        "pull_request",
        "--run-id",
        "123",
        "--run-attempt",
        "1",
        "--run-url",
        "https://github.com/sednalabs/codex/actions/runs/123",
        "--job",
        "clippy",
        "--job-url",
        "https://github.com/sednalabs/codex/actions/runs/123/job/456",
        "--lane",
        "codex.clippy",
    ]


def run_producer(root: Path, *extra: str) -> dict:
    output = root / "diagnostic.json"
    subprocess.run(
        ["python3", str(PRODUCER), "--output", str(output), *identity_args(), *extra],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(output.read_text(encoding="utf-8"))


class FailureDiagnosticTests(unittest.TestCase):
    def test_compiler_json_keeps_exact_identity_and_no_raw_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "compiler.json"
            source.write_text(
                json.dumps(
                    {
                        "reason": "compiler-message",
                        "message": {
                            "level": "error",
                            "message": "missing field `value`",
                            "code": {"code": "E0063"},
                            "spans": [
                                {
                                    "file_name": "codex-rs/core/src/lib.rs",
                                    "line_start": 12,
                                    "column_start": 4,
                                }
                            ],
                        },
                    }
                ),
                encoding="utf-8",
            )
            payload = run_producer(
                root,
                "--outcome",
                "failure",
                "--exit-code",
                "101",
                "--structured-input",
                str(source),
                "--reproducer-id",
                "cargo-check",
                "--reproducer-args-json",
                '{"package":"codex-core","locked":true}',
            )

        self.assertEqual(payload["schema_version"], "ci-diagnostic-v1")
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["identity_status"], "complete")
        self.assertEqual(payload["run_id"], "123")
        self.assertEqual(payload["job_url"].rsplit("/", 1)[-1], "456")
        self.assertEqual(payload["diagnostic"]["kind"], "compiler")
        self.assertEqual(payload["diagnostic"]["code"], "E0063")
        self.assertEqual(payload["diagnostic"]["location"], "codex-rs/core/src/lib.rs:12:4")
        self.assertNotIn("missing field", json.dumps(payload))
        self.assertEqual(payload["reproducer"]["id"], "cargo-check")

    def test_clippy_and_rustfmt_text_are_structured_without_command_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            clippy_log = root / "clippy.log"
            clippy_log.write_text(
                "error[clippy::needless_return]: needless return\n"
                "  --> codex-rs/core/src/lib.rs:7:9\n",
                encoding="utf-8",
            )
            clippy = run_producer(
                root,
                "--outcome",
                "failure",
                "--diagnostic-kind",
                "clippy",
                "--log-file",
                str(clippy_log),
                "--reproducer-id",
                "cargo-clippy",
                "--reproducer-args-json",
                '{"package":"codex-core"}',
            )
            rustfmt_log = root / "rustfmt.log"
            rustfmt_log.write_text("Diff in codex-rs/core/src/lib.rs at line 9:\n", encoding="utf-8")
            rustfmt = run_producer(
                root,
                "--outcome",
                "failure",
                "--diagnostic-kind",
                "rustfmt",
                "--log-file",
                str(rustfmt_log),
                "--reproducer-id",
                "cargo-fmt-check",
                "--reproducer-args-json",
                "{}",
            )

        self.assertEqual(clippy["diagnostic"]["kind"], "clippy")
        self.assertEqual(clippy["diagnostic"]["code"], "clippy::needless_return")
        self.assertEqual(rustfmt["diagnostic"]["kind"], "rustfmt")
        self.assertEqual(rustfmt["diagnostic"]["code"], "format_diff")
        self.assertNotIn("cargo-clippy", json.dumps(clippy))

    def test_planner_payload_and_fingerprint_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            planner = root / "planner.json"
            planner.write_text(json.dumps({"code": "lane_matrix_invalid", "planner": "validation"}), encoding="utf-8")
            first = run_producer(
                root,
                "--outcome",
                "failure",
                "--diagnostic-kind",
                "planner",
                "--structured-input",
                str(planner),
            )
            second = run_producer(
                root,
                "--outcome",
                "failure",
                "--diagnostic-kind",
                "planner",
                "--structured-input",
                str(planner),
                "--source-sha",
                "4444444444444444444444444444444444444444",
            )

        self.assertEqual(first["diagnostic"]["kind"], "planner")
        self.assertEqual(first["diagnostic"]["code"], "lane_matrix_invalid")
        self.assertEqual(first["evidence"]["fingerprint_scope"], "ci-diagnostic-v1")
        self.assertEqual(first["evidence"]["fingerprint"], second["evidence"]["fingerprint"])

    def test_malformed_oversized_secret_and_missing_inputs_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            malformed = root / "malformed.json"
            malformed.write_text("{not-json", encoding="utf-8")
            malformed_payload = run_producer(
                root,
                "--outcome",
                "failure",
                "--structured-input",
                str(malformed),
            )
            oversized = root / "oversized.json"
            oversized.write_text("x" * (64 * 1024 + 1), encoding="utf-8")
            oversized_payload = run_producer(
                root,
                "--outcome",
                "failure",
                "--structured-input",
                str(oversized),
            )
            secret = root / "secret.json"
            secret.write_text(json.dumps({"access_token": "do-not-publish-this-token"}), encoding="utf-8")
            secret_payload = run_producer(
                root,
                "--outcome",
                "failure",
                "--structured-input",
                str(secret),
            )
            missing_payload = run_producer(
                root,
                "--outcome",
                "failure",
                "--log-file",
                str(root / "does-not-exist.log"),
            )
            cancelled_payload = run_producer(root, "--outcome", "cancelled")

        for payload in (malformed_payload, oversized_payload, secret_payload):
            self.assertEqual(payload["status"], "unknown")
            self.assertEqual(payload["diagnostic"]["kind"], "unknown")
        self.assertEqual(secret_payload["input_classification"], "sensitive")
        self.assertNotIn("do-not-publish", json.dumps(secret_payload))
        self.assertEqual(missing_payload["status"], "missing")
        self.assertEqual(cancelled_payload["status"], "cancelled")

    def test_fingerprints_separate_independent_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            first = run_producer(
                root,
                "--outcome",
                "failure",
                "--diagnostic-kind",
                "compiler",
                "--diagnostic-code",
                "E0063",
                "--location",
                "codex-rs/core/src/lib.rs:12:4",
            )
            second = run_producer(
                root,
                "--outcome",
                "failure",
                "--diagnostic-kind",
                "compiler",
                "--diagnostic-code",
                "E0277",
                "--location",
                "codex-rs/core/src/lib.rs:12:4",
            )

        self.assertNotEqual(first["evidence"]["fingerprint"], second["evidence"]["fingerprint"])

    def test_lane_summary_and_aggregate_keep_typed_missing_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            diagnostic = root / "diagnostic.json"
            subprocess.run(
                [
                    "python3",
                    str(PRODUCER),
                    "--output",
                    str(diagnostic),
                    *identity_args(),
                    "--outcome",
                    "cancelled",
                ],
                check=True,
            )
            lane_summary = root / "lane.json"
            subprocess.run(
                [
                    "python3",
                    str(LANE_SUMMARY),
                    "--lane-id",
                    "codex.example",
                    "--summary-title",
                    "example",
                    "--outcome",
                    "cancelled",
                    "--failure-diagnostic-json",
                    str(diagnostic),
                    "--output",
                    str(lane_summary),
                ],
                check=True,
            )
            lane = json.loads(lane_summary.read_text(encoding="utf-8"))
            results = aggregate.build_results(
                planned_matrix=[
                    {
                        "lane_id": "codex.missing",
                        "setup_class": "rust_minimal",
                        "summary_family": "example",
                        "frontier_role": "sentinel",
                        "status_class": "active",
                    }
                ],
                selected_lane_ids=["codex.missing"],
                actual_by_lane={},
                smoke_gate_result="skipped",
                setup_class_results={"rust_minimal": "cancelled"},
                matrix_fail_fast=False,
            )

        self.assertEqual(lane["failure_diagnostic"]["status"], "cancelled")
        self.assertEqual(results[0]["failure_diagnostic"]["status"], "cancelled")
        self.assertEqual(
            aggregate.lane_signal(results[0]),
            "unknown lane_cancelled_before_payload",
        )


if __name__ == "__main__":
    unittest.main()
