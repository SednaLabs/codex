#!/usr/bin/env bash
set -euo pipefail

bash .github/scripts/validation-lanes/downstream-docs-check.sh

mirror_state_json="$(
  python3 .github/scripts/sync_upstream_mirror.py \
    --repo "$PWD" \
    --mode read-only-fallback
)"
expected_mirror_sha="$(
  python3 -c 'import json, sys; print(json.load(sys.stdin)["expected_mirror_sha"])' <<< "${mirror_state_json}"
)"
mapfile -t mirror_audit_args < <(
  python3 -c 'import json, sys; [print(arg) for arg in json.load(sys.stdin)["mirror_audit_args"]]' \
    <<< "${mirror_state_json}"
)

downstream_ref="$(git rev-parse HEAD)"
audit_output_dir="target/downstream-divergence-audit"
audit_report="${audit_output_dir}/downstream-divergence-audit.json"
# The report is an artifact of this invocation.  Remove any prior copy before
# running the producer so a failed producer can never be paired with stale
# diagnostics from an earlier audit.
rm -f -- "${audit_report}"

set +e
python3 scripts/downstream-divergence-audit.py \
  --repo "$PWD" \
  --downstream-ref "${downstream_ref}" \
  --upstream-remote upstream \
  --upstream-branch main \
  "${mirror_audit_args[@]}" \
  --expected-mirror-sha "${expected_mirror_sha}" \
  --registry-path docs/divergences/index.yaml \
  --output-dir "${audit_output_dir}" \
  --format both \
  --code-only \
  --enforce-registry
audit_exit=$?
set -e

if [[ "${audit_exit}" -ne 0 ]]; then
  # Project a bounded, escaped diagnostic from this invocation's report without
  # rerunning the producer or replacing its authoritative exit status.
  python3 - "${audit_report}" <<'PY' || true
import json
import sys

MAX_ITEMS = 32
MAX_ITEM_BYTES = 256
MAX_LIST_BYTES = 4096


def bounded_strings(values):
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise TypeError("expected a string list")
    items = []
    omitted_bytes = 0
    for value in values:
        value_bytes = len(value.encode("utf-8"))
        candidate = items + [value]
        candidate_bytes = len(
            json.dumps(candidate, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        )
        if (
            len(items) >= MAX_ITEMS
            or value_bytes > MAX_ITEM_BYTES
            or candidate_bytes > MAX_LIST_BYTES
        ):
            omitted_bytes += value_bytes
            continue
        items.append(value)
    omitted_count = len(values) - len(items)
    return {
        "items": items,
        "count": len(values),
        "omitted_count": omitted_count,
        "omitted_bytes": omitted_bytes,
        "truncated": omitted_count > 0,
    }


try:
    with open(sys.argv[1], encoding="utf-8") as audit_file:
        audit = json.load(audit_file)
    mirror = audit["mirror"]
    snapshot = audit["snapshot"]
    verdict = audit["verdict"]
    registry = audit["registry_reconciliation"]
    initial_mirror_sha = snapshot["initial"]["mirror"]["sha"]
    final_mirror_sha = snapshot["final"]["mirror"]["sha"]
    if not isinstance(mirror["health"], str):
        raise TypeError("mirror.health is not a string")
    if not isinstance(mirror["expected_mirror_sha"], str):
        raise TypeError("mirror.expected_mirror_sha is not a string")
    if not isinstance(mirror["expected_mirror_matches"], bool):
        raise TypeError("mirror.expected_mirror_matches is not boolean")
    if not isinstance(mirror["usable_as_compare_baseline"], bool):
        raise TypeError("mirror.usable_as_compare_baseline is not boolean")
    if not isinstance(snapshot["stable"], bool):
        raise TypeError("snapshot.stable is not boolean")
    if not isinstance(initial_mirror_sha, str) or not isinstance(final_mirror_sha, str):
        raise TypeError("snapshot mirror SHA is not a string")
    if not isinstance(verdict["exit_code"], int):
        raise TypeError("verdict.exit_code is not an integer")
    if not isinstance(verdict["ok"], bool):
        raise TypeError("verdict.ok is not boolean")
    diagnostic = {
        "diagnostic": "downstream-divergence-audit",
        "snapshot": {
            "stable": snapshot["stable"],
            "initial_mirror_sha": initial_mirror_sha,
            "final_mirror_sha": final_mirror_sha,
        },
        "mirror": {
            "health": mirror["health"],
            "expected_mirror_sha": mirror["expected_mirror_sha"],
            "expected_mirror_matches": mirror["expected_mirror_matches"],
            "usable_as_compare_baseline": mirror["usable_as_compare_baseline"],
        },
        "verdict": {
            "exit_code": verdict["exit_code"],
            "ok": verdict["ok"],
            "reasons": bounded_strings(verdict["reasons"]),
        },
        "registry_reconciliation": {
            "uncovered_code_paths": bounded_strings(registry["uncovered_code_paths"]),
            "stale_entry_ids": bounded_strings(registry["stale_entry_ids"]),
        },
    }
except (OSError, UnicodeError, ValueError, TypeError, KeyError):
    diagnostic = {
        "diagnostic": "downstream-divergence-audit",
        "error": "missing or malformed report",
    }
print(json.dumps(diagnostic, ensure_ascii=True, separators=(",", ":"), sort_keys=True), file=sys.stderr)
PY
  exit "${audit_exit}"
fi

if [[ ! -f "${audit_report}" ]]; then
  echo "artifact-contract failure: downstream divergence audit did not produce ${audit_report}" >&2
  exit 70
fi

if ! python3 - "${audit_report}" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as audit_file:
        audit = json.load(audit_file)
    registry = audit["registry_reconciliation"]
    uncovered_code_paths = registry["uncovered_code_paths"]
    stale_entry_ids = registry["stale_entry_ids"]
    if not isinstance(uncovered_code_paths, list):
        raise TypeError("registry_reconciliation.uncovered_code_paths is not a list")
    if not isinstance(stale_entry_ids, list):
        raise TypeError("registry_reconciliation.stale_entry_ids is not a list")
except (OSError, ValueError, TypeError, KeyError) as error:
    print(f"report validation failed: {error}", file=sys.stderr)
    raise SystemExit(1)

for path in uncovered_code_paths:
    print(f"uncovered divergence path: {path}")
for entry_id in stale_entry_ids:
    print(f"stale divergence entry: {entry_id}")
PY
then
  echo "artifact-contract failure: malformed downstream divergence audit report ${audit_report}" >&2
  exit 70
fi

exit 0
