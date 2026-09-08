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

# Keep the producer's report authoritative while exposing only a bounded,
# repository-relative diagnostic projection in the lane log.  The workflow
# summary intentionally omits raw commands and excerpts, so this is the
# narrowest safe readback available to the existing lane contract.
if [[ -f "${audit_report}" ]]; then
  if ! python3 - "${audit_report}" <<'PY'
import json
import re
import sys

MAX_PATHS = 64
MAX_PATH_BYTES = 240
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
MIRROR_HEALTH = {"exact", "stale_ff_only", "illegal_ahead", "illegal_diverged"}

try:
    with open(sys.argv[1], encoding="utf-8") as audit_file:
        audit = json.load(audit_file)
    snapshot = audit["snapshot"]["initial"]
    mirror = audit["mirror"]
    tree_diff = audit["tree_diff"]
    registry = audit["registry_reconciliation"]
    paths = registry["uncovered_code_paths"]
    if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
        raise TypeError("registry_reconciliation.uncovered_code_paths is not a string list")
    shas = [snapshot[key]["sha"] for key in ("downstream", "mirror", "upstream")]
    if not all(isinstance(sha, str) and SHA_RE.fullmatch(sha) for sha in shas):
        raise ValueError("snapshot SHA identity is malformed")
    if mirror["health"] not in MIRROR_HEALTH:
        raise ValueError("mirror health is outside the producer vocabulary")
    if not isinstance(tree_diff["tree_equal"], bool):
        raise TypeError("tree equality is not boolean")
    safe_paths = [
        path
        for path in paths[:MAX_PATHS]
        if path and "\\" not in path and not path.startswith("/") and ".." not in path.split("/")
        and len(path.encode("utf-8")) <= MAX_PATH_BYTES
    ]
    diagnostic = {
        "snapshot": {
            "downstream_sha": snapshot["downstream"]["sha"],
            "mirror_sha": snapshot["mirror"]["sha"],
            "upstream_sha": snapshot["upstream"]["sha"],
        },
        "mirror": {
            "health": mirror["health"],
            "tree_equal": tree_diff["tree_equal"],
        },
        "registry_reconciliation": {
            "uncovered_code_path_count": len(paths),
            "uncovered_code_paths": safe_paths,
            "paths_truncated": len(paths) > MAX_PATHS,
        },
    }
except (OSError, UnicodeError, ValueError, TypeError, KeyError):
    print("downstream divergence audit diagnostic unavailable")
else:
    print("downstream divergence audit diagnostic: " + json.dumps(diagnostic, sort_keys=True))
PY
  then
    echo "downstream divergence audit diagnostic unavailable"
  fi
fi

if [[ "${audit_exit}" -ne 0 ]]; then
  # The producer's stdout/stderr is intentionally left untouched above.  In
  # particular, do not try to parse a partial or stale report here: its parser
  # failure must not mask the producer's authoritative exit status.
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
