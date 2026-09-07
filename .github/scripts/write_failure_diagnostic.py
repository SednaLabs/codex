#!/usr/bin/env python3
"""Produce a bounded, typed diagnostic for one CI command attempt.

The producer is deliberately independent of any consumer or retry policy.  It
accepts a structured compiler/Clippy/planner payload when one is available and
falls back to a small, allowlisted text parser for rustfmt and ordinary Rust
diagnostics.  Raw command text, logs, environment values, and diagnostic
messages are never written to the resulting artifact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


SCHEMA_VERSION = "ci-diagnostic-v1"
MAX_INPUT_BYTES = 64 * 1024
MAX_LINE_BYTES = 16 * 1024
MAX_ARGS_BYTES = 8 * 1024
MAX_TEXT_LINES = 2000

STATUSES = {"failed", "cancelled", "missing", "unexercised", "unknown"}
KINDS = {"compiler", "clippy", "planner", "rustfmt", "unknown"}
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}$")
SAFE_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
SHA_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")
RUN_ID_RE = re.compile(r"^[0-9]{1,20}$")
PATH_RE = re.compile(r"^(?!/)(?![A-Za-z]:[\\/])(?!.*(?:^|/|\\)\.\.(?:/|\\|$))[^\x00\r\n]{1,300}$")
LOCATION_RE = re.compile(r"^(?P<path>[^:\r\n]+):(?P<line>[0-9]{1,7})(?::(?P<column>[0-9]{1,7}))?$")
TEXT_ERROR_RE = re.compile(r"\berror(?:\[(?P<code>[A-Za-z0-9_:-]+)\])?:\s*", re.IGNORECASE)
TEXT_LOCATION_RE = re.compile(r"^\s*-->\s+(?P<location>[^\s]+:[0-9]+(?::[0-9]+)?)")
RUSTFMT_DIFF_RE = re.compile(r"^\s*Diff in (?P<path>.+?) at line (?P<line>[0-9]+):\s*$")
SECRET_KEY_RE = re.compile(
    r"(?i)(?:access[_-]?token|api[_-]?key|authorization|cookie|credential|password|private[_-]?key|secret|session[_-]?token)"
)
SECRET_VALUE_RE = re.compile(
    r"(?i)(?:bearer\s+[A-Za-z0-9._~+/=-]{12,}|(?:token|secret|password|api[_-]?key)\s*[:=]\s*\S{8,})"
)
SHELL_TEXT_RE = re.compile(r"[`$;|&<>\r\n]")
UNSAFE_ARG_KEY_RE = re.compile(r"(?i)(?:command|cmd|env|message|raw|run|script|shell|token|secret|password)")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--repository", default="")
    parser.add_argument("--source-sha", default="")
    parser.add_argument("--execution-sha", default="")
    parser.add_argument("--workflow-sha", default="")
    parser.add_argument("--event", default="")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--run-attempt", default="")
    parser.add_argument("--run-url", default="")
    parser.add_argument("--job", default="")
    parser.add_argument("--job-url", default="")
    parser.add_argument("--lane", default="")
    parser.add_argument("--outcome", default="")
    parser.add_argument("--status", default="")
    parser.add_argument("--exit-code", default="")
    parser.add_argument("--diagnostic-kind", default="")
    parser.add_argument("--diagnostic-code", default="")
    parser.add_argument("--test-id", default="")
    parser.add_argument("--location", default="")
    parser.add_argument("--evidence-url", default="")
    parser.add_argument("--evidence-fingerprint", default="")
    parser.add_argument("--reproducer-id", default="")
    parser.add_argument("--reproducer-args-json", default="{}")
    parser.add_argument("--log-file", default=None, type=Path)
    parser.add_argument("--structured-input", default=None, type=Path)
    parser.add_argument("--input-json", default="", help=argparse.SUPPRESS)
    return parser.parse_args()


def safe_text(value: object, *, limit: int = 256) -> str:
    if not isinstance(value, str):
        return ""
    value = " ".join(value.split())
    if len(value) > limit:
        return ""
    return value


def safe_id(value: object, *, limit: int = 160) -> str:
    value = safe_text(value, limit=limit)
    return value if value and SAFE_ID_RE.fullmatch(value) else ""


def safe_code(value: object) -> str:
    value = safe_text(value, limit=64)
    return value if value and SAFE_CODE_RE.fullmatch(value) else ""


def valid_sha(value: str) -> str | None:
    value = value.strip().lower()
    return value if SHA_RE.fullmatch(value) else None


def valid_run_id(value: str) -> str | None:
    value = value.strip()
    return value if RUN_ID_RE.fullmatch(value) and int(value) > 0 else None


def valid_repository(value: str) -> str | None:
    value = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", value):
        return None
    return value


def valid_event(value: str) -> str | None:
    value = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        return None
    return value


def valid_url(value: str, *, repository: str | None, run_id: str | None, require_job: bool = False) -> str | None:
    value = value.strip()
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.netloc not in {"github.com", "www.github.com"}:
        return None
    path_parts = [part for part in parsed.path.split("/") if part]
    if not repository or len(path_parts) < 4 or "/".join(path_parts[:2]).lower() != repository.lower():
        return None
    if path_parts[2] != "actions" or path_parts[3] != "runs":
        return None
    if not run_id or len(path_parts) < 5 or path_parts[4] != run_id:
        return None
    if require_job and (len(path_parts) != 7 or path_parts[5] != "job" or not path_parts[6].isdigit()):
        return None
    if parsed.query or parsed.fragment:
        return None
    return value


def valid_repo_location(value: object) -> str | None:
    value = safe_text(value, limit=300).replace("\\", "/")
    if not value or not PATH_RE.fullmatch(value):
        return None
    if value.startswith("./"):
        value = value[2:]
    if value.startswith("/") or re.match(r"^[A-Za-z]:/", value):
        return None
    return value


def valid_location(value: object) -> str | None:
    value = safe_text(value, limit=320).replace("\\", "/")
    match = LOCATION_RE.fullmatch(value)
    if not match:
        return valid_repo_location(value)
    path = valid_repo_location(match.group("path"))
    if not path:
        return None
    location = f"{path}:{match.group('line')}"
    if match.group("column"):
        location += f":{match.group('column')}"
    return location


def contains_sensitive(value: object) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if SECRET_KEY_RE.search(str(key)):
                return True
            if contains_sensitive(item):
                return True
        return False
    if isinstance(value, list):
        return any(contains_sensitive(item) for item in value)
    if isinstance(value, str):
        return bool(SECRET_VALUE_RE.search(value))
    return False


def read_bounded(path: Path) -> tuple[str | None, str | None]:
    try:
        data = path.read_bytes()
    except OSError:
        return None, "missing"
    if len(data) > MAX_INPUT_BYTES:
        return None, "oversized"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None, "malformed"
    if contains_sensitive(text):
        return None, "sensitive"
    return text, None


def parse_json_payload(text: str) -> tuple[list[dict[str, Any]], str | None]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payloads: list[dict[str, Any]] = []
        for line in text.splitlines()[:MAX_TEXT_LINES]:
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                return [], "malformed"
            if isinstance(item, dict):
                payloads.append(item)
            else:
                return [], "malformed"
        return payloads, None if payloads else "malformed"
    if isinstance(payload, dict):
        return [payload], None
    if isinstance(payload, list) and all(isinstance(item, dict) for item in payload):
        return list(payload), None
    return [], "malformed"


def parse_exit_code(raw: str) -> int | None:
    raw = raw.strip()
    if not raw or not re.fullmatch(r"-?[0-9]{1,9}", raw):
        return None
    return int(raw)


def outcome_status(outcome: str, exit_code: int | None) -> str:
    normalized = outcome.strip().lower()
    if normalized in {"cancelled", "canceled"}:
        return "cancelled"
    if normalized in {"missing", "unexercised", "unknown"}:
        return normalized
    if normalized in {"failure", "failed", "startup_failure", "timed_out", "action_required"}:
        return "failed"
    if exit_code is not None and exit_code != 0:
        return "failed"
    return "unexercised"


def infer_kind(code: str, message: str, explicit: str) -> str:
    if explicit in KINDS:
        return explicit
    if code.lower().startswith("clippy::"):
        return "clippy"
    if re.fullmatch(r"E[0-9]{3,4}", code):
        return "compiler"
    if "rustfmt" in message.lower() or message.lstrip().startswith("Diff in "):
        return "rustfmt"
    return "unknown"


def compiler_payload(payload: dict[str, Any], explicit_kind: str) -> dict[str, str]:
    message = payload.get("message") if isinstance(payload.get("message"), dict) else payload
    if not isinstance(message, dict):
        return {}
    code_value = message.get("code")
    if isinstance(code_value, dict):
        code_value = code_value.get("code")
    code = safe_code(code_value)
    text = safe_text(message.get("message"), limit=512)
    kind = infer_kind(code, text, explicit_kind)
    if kind == "unknown" and explicit_kind not in KINDS:
        return {}
    spans = message.get("spans")
    location = None
    if isinstance(spans, list):
        for span in spans:
            if isinstance(span, dict):
                path = span.get("file_name")
                line = span.get("line_start")
                column = span.get("column_start")
                if isinstance(path, str) and isinstance(line, int) and line > 0:
                    location = valid_location(f"{path}:{line}:{column}")
                    if location:
                        break
    return {
        "kind": kind,
        "code": code or ("format_diff" if kind == "rustfmt" else "unknown_input"),
        "test_or_invariant_id": safe_id(
            message.get("test_id") or message.get("invariant_id") or payload.get("test_id")
        ),
        "location": location or "",
    }


def parse_text(lines: list[str], explicit_kind: str) -> dict[str, str]:
    if len(lines) > MAX_TEXT_LINES:
        lines = lines[:MAX_TEXT_LINES]
    code = ""
    location = ""
    for index, line in enumerate(lines):
        error_match = TEXT_ERROR_RE.search(line)
        if error_match:
            code = safe_code(error_match.group("code"))
            for candidate in lines[index + 1 : index + 12]:
                location_match = TEXT_LOCATION_RE.search(candidate)
                if location_match:
                    location = valid_location(location_match.group("location")) or ""
                    break
            break
        fmt_match = RUSTFMT_DIFF_RE.match(line)
        if fmt_match:
            path = valid_repo_location(fmt_match.group("path"))
            location = f"{path}:{fmt_match.group('line')}" if path else ""
            code = "format_diff"
            break
    if not code and explicit_kind == "planner":
        for line in lines:
            if "planner" in line.lower():
                code = "planner_failure"
                break
    if not code:
        return {}
    message = "\n".join(lines)
    kind = infer_kind(code, message, explicit_kind)
    if explicit_kind in KINDS:
        kind = explicit_kind
    return {"kind": kind, "code": code, "test_or_invariant_id": "", "location": location}


def parse_reproducer(raw_id: str, raw_args: str) -> tuple[dict[str, Any] | None, str | None]:
    if not raw_id and raw_args.strip() in {"", "{}"}:
        return None, None
    reproducer_id = safe_id(raw_id, limit=64)
    if not reproducer_id:
        return None, "invalid_reproducer"
    try:
        args = json.loads(raw_args or "{}")
    except json.JSONDecodeError:
        return None, "invalid_reproducer_args"
    if not isinstance(args, dict) or contains_sensitive(args):
        return None, "invalid_reproducer_args"
    try:
        encoded = json.dumps(args, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return None, "invalid_reproducer_args"
    if len(encoded.encode("utf-8")) > MAX_ARGS_BYTES:
        return None, "oversized_reproducer_args"
    def valid_argument(value: object, *, depth: int = 0) -> bool:
        if depth > 4:
            return False
        if value is None or isinstance(value, (int, float, bool)):
            return True
        if isinstance(value, str):
            return (
                len(value) <= 256
                and not SHELL_TEXT_RE.search(value)
                and not value.startswith("/")
                and not re.match(r"^[A-Za-z]:[\\/]", value)
            )
        if isinstance(value, list):
            return len(value) <= 32 and all(valid_argument(item, depth=depth + 1) for item in value)
        if isinstance(value, dict):
            return len(value) <= 32 and all(
                safe_id(key, limit=64)
                and not UNSAFE_ARG_KEY_RE.search(key)
                and valid_argument(item, depth=depth + 1)
                for key, item in value.items()
            )
        return False

    for key, value in args.items():
        if not safe_id(key, limit=64) or UNSAFE_ARG_KEY_RE.search(key):
            return None, "invalid_reproducer_args"
        if not valid_argument(value):
            return None, "invalid_reproducer_args"
    return {"id": reproducer_id, "arguments": args}, None


def diagnostic_fingerprint(diagnostic: dict[str, str], reproducer: dict[str, Any] | None) -> str:
    core = {
        "schema_version": SCHEMA_VERSION,
        "kind": diagnostic.get("kind", "unknown"),
        "code": diagnostic.get("code", "unknown_input"),
        "test_or_invariant_id": diagnostic.get("test_or_invariant_id", ""),
        "location": diagnostic.get("location", ""),
        "reproducer_id": (reproducer or {}).get("id", ""),
    }
    encoded = json.dumps(core, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def empty_diagnostic(code: str = "unknown_input") -> dict[str, str]:
    return {"kind": "unknown", "code": code, "test_or_invariant_id": "", "location": ""}


def main() -> int:
    args = parse_args()
    repository = valid_repository(args.repository)
    source_sha = valid_sha(args.source_sha)
    execution_sha = valid_sha(args.execution_sha)
    workflow_sha = valid_sha(args.workflow_sha)
    run_id = valid_run_id(args.run_id)
    run_attempt = valid_run_id(args.run_attempt)
    run_url = valid_url(args.run_url, repository=repository, run_id=run_id)
    job_url = valid_url(args.job_url, repository=repository, run_id=run_id, require_job=True)
    evidence_url = valid_url(args.evidence_url, repository=repository, run_id=run_id)
    if not evidence_url:
        evidence_url = job_url or run_url
    event = valid_event(args.event)
    lane = safe_id(args.lane, limit=160)
    job = safe_id(args.job, limit=100)
    exit_code = parse_exit_code(args.exit_code)
    requested_status = args.status.strip().lower()
    invalid_status = bool(args.status.strip() and requested_status not in STATUSES)
    command_status = (
        requested_status
        if requested_status in STATUSES
        else outcome_status(args.outcome, exit_code)
    )
    status = command_status
    diagnostic = empty_diagnostic()
    input_reason: str | None = None

    if args.structured_input:
        text, input_reason = read_bounded(args.structured_input)
        if text is not None:
            payloads, input_reason = parse_json_payload(text)
            if input_reason is None:
                for payload in payloads:
                    if contains_sensitive(payload):
                        input_reason = "sensitive"
                        break
                    candidate = compiler_payload(payload, args.diagnostic_kind.strip().lower())
                    if candidate:
                        diagnostic = candidate
                        break
                if diagnostic["kind"] == "unknown" and not payloads:
                    input_reason = "malformed"
    elif args.log_file:
        text, input_reason = read_bounded(args.log_file)
        if text is not None:
            parsed_diagnostic = parse_text(text.splitlines(), args.diagnostic_kind.strip().lower())
            if parsed_diagnostic:
                diagnostic = parsed_diagnostic
            else:
                input_reason = "unsupported"

    if args.input_json:
        try:
            inline = json.loads(args.input_json)
        except json.JSONDecodeError:
            inline = None
            input_reason = "malformed"
        if inline is None or not isinstance(inline, (dict, list)):
            input_reason = "malformed"
        elif contains_sensitive(inline):
            input_reason = "sensitive"
        else:
            payloads = inline if isinstance(inline, list) else [inline]
            for payload in payloads:
                if isinstance(payload, dict):
                    candidate = compiler_payload(payload, args.diagnostic_kind.strip().lower())
                    if candidate:
                        diagnostic = candidate
                        break

    explicit_kind = args.diagnostic_kind.strip().lower()
    if explicit_kind in KINDS and diagnostic["kind"] == "unknown":
        diagnostic["kind"] = explicit_kind
        diagnostic["code"] = safe_code(args.diagnostic_code) or {
            "rustfmt": "format_diff",
            "planner": "planner_failure",
        }.get(explicit_kind, "unknown_input")
    if args.diagnostic_code:
        diagnostic["code"] = safe_code(args.diagnostic_code) or "unknown_input"
    if args.test_id:
        diagnostic["test_or_invariant_id"] = safe_id(args.test_id)
    if args.location:
        diagnostic["location"] = valid_location(args.location) or ""

    reproducer, reproducer_error = parse_reproducer(
        args.reproducer_id, args.reproducer_args_json
    )
    if reproducer_error:
        input_reason = reproducer_error

    if invalid_status:
        status = "unknown"
        diagnostic = empty_diagnostic("status_invalid")

    if input_reason in {
        "missing",
        "oversized",
        "malformed",
        "sensitive",
        "invalid_reproducer",
        "invalid_reproducer_args",
        "oversized_reproducer_args",
    }:
        if input_reason == "missing":
            status = "cancelled" if command_status == "cancelled" else "missing"
        else:
            status = "unknown" if command_status not in {"cancelled", "missing"} else command_status
        diagnostic = empty_diagnostic(
            {
                "missing": "command_cancelled" if command_status == "cancelled" else "input_artifact_missing",
                "oversized": "input_oversized",
                "malformed": "input_malformed",
                "sensitive": "sensitive_input_rejected",
                "invalid_reproducer": "reproducer_invalid",
                "invalid_reproducer_args": "reproducer_args_invalid",
                "oversized_reproducer_args": "reproducer_args_oversized",
            }[input_reason]
        )
    elif input_reason == "unsupported" and command_status == "failed":
        status = "unknown"
        diagnostic = empty_diagnostic("unsupported_input")
    elif (
        not args.log_file
        and not args.structured_input
        and not args.input_json
        and not args.diagnostic_code
        and command_status == "failed"
    ):
        status = "unknown"
        diagnostic = empty_diagnostic("input_not_supplied")
    elif command_status in {"cancelled", "missing", "unexercised"} and not diagnostic.get("code"):
        diagnostic = empty_diagnostic(
            {
                "cancelled": "command_cancelled",
                "missing": "input_artifact_missing",
                "unexercised": "command_unexercised",
            }.get(command_status, "unknown_input")
        )
    elif not diagnostic.get("code"):
        diagnostic = empty_diagnostic()

    provided_fingerprint = args.evidence_fingerprint.strip().lower()
    if provided_fingerprint and not re.fullmatch(r"[0-9a-f]{64}", provided_fingerprint):
        provided_fingerprint = ""
        if status not in {"cancelled", "missing"}:
            status = "unknown"
            diagnostic = empty_diagnostic("evidence_fingerprint_invalid")

    identity = {
        "repository": repository,
        "source_sha": source_sha,
        "execution_sha": execution_sha,
        "workflow_sha": workflow_sha,
        "event": event,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "job": job,
        "job_url": job_url,
        "lane": lane,
    }
    identity_missing = [name for name, value in identity.items() if value is None or value == ""]
    # A capsule without the exact execution identity is useful only as a safe
    # unknown.  Keep command_status so a consumer can distinguish an
    # unattributed failure from a passing or unexercised command, but never
    # present the parsed diagnostic as attributable evidence.
    if identity_missing and status not in {"cancelled", "missing"}:
        status = "unknown"
        diagnostic = empty_diagnostic("identity_incomplete")

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": status if status in STATUSES else "unknown",
        "command_status": command_status,
        "repository": repository,
        "source_sha": source_sha,
        "execution_sha": execution_sha,
        "workflow_sha": workflow_sha,
        "event": event,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "run_url": run_url,
        "job": job,
        "job_url": job_url,
        "lane": lane,
        "identity_status": "complete" if not identity_missing else "incomplete",
        "identity_missing": identity_missing,
        "exit_code": exit_code,
        "diagnostic": diagnostic,
        "evidence": {
            "url": evidence_url,
            "fingerprint": diagnostic_fingerprint(diagnostic, reproducer),
            "fingerprint_scope": SCHEMA_VERSION,
        },
        "reproducer": reproducer,
    }
    if provided_fingerprint:
        payload["evidence"]["provided_fingerprint"] = provided_fingerprint
    if input_reason and input_reason not in {"unsupported"}:
        payload["input_classification"] = input_reason
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
