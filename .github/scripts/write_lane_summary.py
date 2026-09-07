#!/usr/bin/env python3
"""Write a compact per-lane validation summary artifact."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ERROR_RE = re.compile(
    r"(^error:|^thread '.*' panicked|\bFAILED\b|failures:|error\[|\bpanic(?:!|:|\s+at\b))"
)
PATH_RE = re.compile(r"(/home/\S+|/Users/\S+)")
WHITESPACE_RE = re.compile(r"\s+")
SECRET_LINE_RE = re.compile(
    r"(?i)(?:bearer\s+[A-Za-z0-9._~+/=-]{12,}|(?:access[_-]?token|api[_-]?key|authorization|cookie|credential|password|private[_-]?key|secret|session[_-]?token)\s*[:=]\s*\S{8,})"
)
SCHEMA_CONTENT_DRIFT_RE = re.compile(
    r"Vendored (?P<family>json|typescript) app-server schema fixture "
    r"(?P<fixture>.+?) differs from generated output\. Run `(?P<command>[^`]+)`",
)
SCHEMA_FILE_SET_DRIFT_RE = re.compile(
    r"Vendored (?P<family>json|typescript) app-server schema fixture file set "
    r"doesn't match freshly generated output\. Run `(?P<command>[^`]+)`",
)
DIAGNOSTIC_SCHEMA_VERSION = "ci-diagnostic-v1"
DIAGNOSTIC_MAX_BYTES = 64 * 1024


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lane-id", required=True)
    parser.add_argument("--lane-phase", default="downstream_lanes")
    parser.add_argument("--summary-title", required=True)
    parser.add_argument("--script-path", default="")
    parser.add_argument("--script-args-json", default="[]")
    parser.add_argument("--run-command", default="")
    parser.add_argument("--status-class", default="active")
    parser.add_argument("--frontier-default", default="false")
    parser.add_argument("--setup-class", default="rust_minimal")
    parser.add_argument("--frontier-role", default="sentinel")
    parser.add_argument("--summary-family", default="")
    parser.add_argument("--cost-class", default="medium")
    parser.add_argument("--outcome", default="unknown")
    parser.add_argument("--exit-code", default="")
    parser.add_argument("--log-file", default="")
    parser.add_argument("--job-started-at-ms", default="")
    parser.add_argument("--setup-finished-at-ms", default="")
    parser.add_argument("--started-at-ms", default="")
    parser.add_argument("--finished-at-ms", default="")
    parser.add_argument("--setup-duration-ms", default="")
    parser.add_argument("--command-duration-ms", default="")
    parser.add_argument("--duration-ms", default="")
    parser.add_argument("--cache-policy", default="")
    parser.add_argument("--cache-backend", default="")
    parser.add_argument("--sccache-restore-mode", default="")
    parser.add_argument("--batch-id", default="")
    parser.add_argument("--batch-index", default="")
    parser.add_argument("--batch-lane-count", default="")
    parser.add_argument("--batch-position", default="")
    parser.add_argument("--batch-weight-seconds", default="")
    parser.add_argument("--batch-setup-duration-ms", default="")
    parser.add_argument("--artifact-name", default="")
    parser.add_argument("--nextest-archive-artifact-name", default="")
    parser.add_argument("--nextest-archive-file-name", default="")
    parser.add_argument("--nextest-archive-mode", default="")
    parser.add_argument("--failure-diagnostic-json", default="")
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def read_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def primary_signal(error_lines: list[str], tail_lines: list[str], schema_drift: dict) -> str:
    if schema_drift:
        return sanitize_line(str(schema_drift.get("summary") or ""))
    if error_lines:
        return sanitize_line(error_lines[0])
    for line in reversed(tail_lines):
        stripped = line.strip()
        if stripped:
            return sanitize_line(stripped)
    return ""


def parse_exit_code(raw: str) -> int | None:
    value = raw.strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def parse_u64(raw: str) -> int | None:
    value = raw.strip()
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    if parsed < 0:
        return None
    return parsed


def parse_bool(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def sanitize_line(raw: str) -> str:
    compact = WHITESPACE_RE.sub(" ", raw.strip())
    if SECRET_LINE_RE.search(compact):
        return "<redacted-sensitive>"
    compact = PATH_RE.sub("<redacted-path>", compact)
    return compact[:240]


def detect_schema_fixture_drift(lines: list[str]) -> dict:
    file_set_match: re.Match[str] | None = None
    for line in lines:
        match = SCHEMA_CONTENT_DRIFT_RE.search(line)
        if match:
            family = match.group("family")
            fixture = sanitize_line(match.group("fixture"))
            return {
                "kind": "app_server_schema_fixture_drift",
                "fixture_family": family,
                "fixture_path": fixture,
                "direction": "vendored_differs_from_generated",
                "recommended_fix": sanitize_line(match.group("command")),
                "recommended_proof": {
                    "profile": "targeted",
                    "lane_ids": ["codex.app-server-protocol-test"],
                },
                "summary": f"{family} app-server schema fixture {fixture} differs from generated output",
            }
        if file_set_match is None:
            file_set_match = SCHEMA_FILE_SET_DRIFT_RE.search(line)

    if file_set_match:
        family = file_set_match.group("family")
        return {
            "kind": "app_server_schema_fixture_drift",
            "fixture_family": family,
            "fixture_path": "",
            "direction": "vendored_file_set_differs_from_generated",
            "recommended_fix": sanitize_line(file_set_match.group("command")),
            "recommended_proof": {
                "profile": "targeted",
                "lane_ids": ["codex.app-server-protocol-test"],
            },
            "summary": f"{family} app-server schema fixture file set differs from generated output",
        }

    return {}


def load_failure_diagnostic(path_value: str) -> dict:
    """Project a typed diagnostic into the public lane-summary contract."""

    invalid = {
        "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
        "status": "unknown",
        "diagnostic": {
            "kind": "unknown",
            "code": "diagnostic_artifact_invalid",
            "test_or_invariant_id": "",
            "location": "",
        },
    }
    if not path_value:
        return {}
    path = Path(path_value)
    try:
        if path.stat().st_size > DIAGNOSTIC_MAX_BYTES:
            return invalid
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return invalid
    if not isinstance(payload, dict) or payload.get("schema_version") != DIAGNOSTIC_SCHEMA_VERSION:
        invalid["diagnostic"]["code"] = "diagnostic_schema_unsupported"
        return invalid

    def scalar(name: str) -> str | int | None:
        value = payload.get(name)
        return value if isinstance(value, (str, int)) or value is None else None

    diagnostic = payload.get("diagnostic")
    if not isinstance(diagnostic, dict):
        diagnostic = {}
    evidence = payload.get("evidence")
    if not isinstance(evidence, dict):
        evidence = {}
    result = {
        "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
        "status": scalar("status") or "unknown",
        "command_status": scalar("command_status") or "unknown",
        "repository": scalar("repository"),
        "source_sha": scalar("source_sha"),
        "execution_sha": scalar("execution_sha"),
        "workflow_sha": scalar("workflow_sha"),
        "event": scalar("event"),
        "run_id": scalar("run_id"),
        "run_attempt": scalar("run_attempt"),
        "run_url": scalar("run_url"),
        "job": scalar("job"),
        "job_url": scalar("job_url"),
        "lane": scalar("lane"),
        "identity_status": scalar("identity_status") or "incomplete",
        "exit_code": scalar("exit_code"),
        "diagnostic": {
            "kind": diagnostic.get("kind") if isinstance(diagnostic.get("kind"), str) else "unknown",
            "code": diagnostic.get("code") if isinstance(diagnostic.get("code"), str) else "unknown_input",
            "test_or_invariant_id": (
                diagnostic.get("test_or_invariant_id")
                if isinstance(diagnostic.get("test_or_invariant_id"), str)
                else ""
            ),
            "location": diagnostic.get("location") if isinstance(diagnostic.get("location"), str) else "",
        },
        "evidence": {
            "url": evidence.get("url") if isinstance(evidence.get("url"), str) else None,
            "fingerprint": (
                evidence.get("fingerprint")
                if isinstance(evidence.get("fingerprint"), str)
                else None
            ),
            "fingerprint_scope": (
                evidence.get("fingerprint_scope")
                if isinstance(evidence.get("fingerprint_scope"), str)
                else DIAGNOSTIC_SCHEMA_VERSION
            ),
        },
    }
    identity_missing = payload.get("identity_missing")
    if isinstance(identity_missing, list) and all(isinstance(item, str) for item in identity_missing):
        result["identity_missing"] = identity_missing[:20]
    reproducer = payload.get("reproducer")
    if isinstance(reproducer, dict) and isinstance(reproducer.get("id"), str):
        arguments = reproducer.get("arguments", {})
        if isinstance(arguments, dict):
            result["reproducer"] = {"id": reproducer["id"], "arguments": arguments}
    return result


def main() -> None:
    args = parse_args()
    log_path = Path(args.log_file) if args.log_file else None
    lines = read_lines(log_path) if log_path is not None else []
    error_lines = [line for line in lines if ERROR_RE.search(line)][:20]
    tail_lines = lines[-80:]
    schema_drift = detect_schema_fixture_drift(lines)
    failure_diagnostic = load_failure_diagnostic(args.failure_diagnostic_json)
    script_args = json.loads(args.script_args_json or "[]")

    payload = {
        "lane_id": args.lane_id,
        "lane_phase": args.lane_phase or "downstream_lanes",
        "summary_title": args.summary_title,
        "script_path": args.script_path or ("legacy-run-command" if args.run_command else ""),
        "script_args": script_args if isinstance(script_args, list) else [],
        "status_class": args.status_class or "active",
        "frontier_default": parse_bool(args.frontier_default),
        "setup_class": args.setup_class or "rust_minimal",
        "frontier_role": args.frontier_role or "sentinel",
        "summary_family": args.summary_family or args.lane_id,
        "cost_class": args.cost_class or "medium",
        "outcome": args.outcome or "unknown",
        "exit_code": parse_exit_code(args.exit_code),
        "job_started_at_ms": parse_u64(args.job_started_at_ms),
        "setup_finished_at_ms": parse_u64(args.setup_finished_at_ms),
        "started_at_ms": parse_u64(args.started_at_ms),
        "finished_at_ms": parse_u64(args.finished_at_ms),
        "setup_duration_ms": parse_u64(args.setup_duration_ms),
        "command_duration_ms": parse_u64(args.command_duration_ms),
        "duration_ms": parse_u64(args.duration_ms),
        "cache_policy": args.cache_policy or "unspecified",
        "cache_backend": args.cache_backend or "not-applicable",
        "sccache_restore_mode": args.sccache_restore_mode or "not-applicable",
        "batch_id": args.batch_id or "",
        "batch_index": parse_u64(args.batch_index),
        "batch_lane_count": parse_u64(args.batch_lane_count),
        "batch_position": parse_u64(args.batch_position),
        "batch_weight_seconds": parse_u64(args.batch_weight_seconds),
        "batch_setup_duration_ms": parse_u64(args.batch_setup_duration_ms),
        "log_available": bool(lines),
        "primary_signal": primary_signal(error_lines, tail_lines, schema_drift),
        "schema_fixture_drift": schema_drift,
        "failure_diagnostic": failure_diagnostic,
        "artifact_name": args.artifact_name or "",
        "nextest_archive_artifact_name": args.nextest_archive_artifact_name or "",
        "nextest_archive_file_name": args.nextest_archive_file_name or "",
        "nextest_archive_mode": args.nextest_archive_mode or "not-applicable",
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
