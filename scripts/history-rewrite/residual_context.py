#!/usr/bin/env python3
"""Build a content-free decision index for guarded canonical blob residuals."""
import argparse
import difflib
import hashlib
import json
from pathlib import Path

from object_index import GitObjectIndex
from rewrite_candidate import load_policy, preflight


def normalized_lines(data: bytes) -> tuple[list[bytes], bool]:
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return [], False
    return [b" ".join(line.split()) for line in data.splitlines() if line.split()], True


def sequence_metrics(approved: list[bytes], residual: list[bytes]) -> dict[str, int]:
    metrics = {"equal": 0, "insert": 0, "delete": 0, "replace_old": 0, "replace_new": 0}
    for operation, old_start, old_end, new_start, new_end in difflib.SequenceMatcher(None, approved, residual, autojunk=False).get_opcodes():
        if operation == "replace":
            metrics["replace_old"] += old_end - old_start
            metrics["replace_new"] += new_end - new_start
        else:
            metrics[operation] += (old_end - old_start) if operation != "insert" else (new_end - new_start)
    return metrics


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    policy = load_policy(args.policy)
    with GitObjectIndex(args.repo) as index:
        context = preflight(index, policy)
        result = {"schema": "history-rewrite-residual-context-v1", "rules": {}}
        for rule in policy["rules"]:
            if rule["kind"] != "exact_blob_replacement":
                continue
            old_data = index.blob(rule["old_blob"].encode("ascii"))
            new_data = index.blob(rule["new_blob"].encode("ascii"))
            old_lines, old_utf8 = normalized_lines(old_data)
            new_lines, new_utf8 = normalized_lines(new_data)
            approved = set(old_lines) | set(new_lines)
            members = []
            for identity in sorted(context["residuals"][rule["id"]]):
                data = index.blob(identity)
                lines, utf8 = normalized_lines(data)
                novel = sorted(set(lines) - approved) if utf8 and old_utf8 and new_utf8 else []
                decision_class = (
                    "binary-or-non-utf8-requires-context-review"
                    if not (utf8 and old_utf8 and new_utf8)
                    else "approved-lines-only"
                    if not novel
                    else "residual-only-text-requires-context-review"
                )
                members.append(
                    {
                        "oid": identity.decode("ascii"),
                        "bytes": len(data),
                        "normalized_nonblank_lines": len(lines),
                        "approved_line_occurrences": sum(line in approved for line in lines),
                        "residual_only_unique_lines": len(novel),
                        "residual_only_line_set_sha256": hashlib.sha256(b"\n".join(novel)).hexdigest(),
                        "old_diff": sequence_metrics(old_lines, lines) if utf8 and old_utf8 else None,
                        "new_diff": sequence_metrics(new_lines, lines) if utf8 and new_utf8 else None,
                        "decision_class": decision_class,
                    }
                )
            groups = {}
            for member in members:
                key = (member["decision_class"], member["residual_only_line_set_sha256"])
                groups.setdefault(key, []).append(member)
            group_rows = []
            for (decision_class, line_set_sha), values in sorted(groups.items()):
                group_rows.append(
                    {
                        "decision_class": decision_class,
                        "residual_only_line_set_sha256": line_set_sha,
                        "member_count": len(values),
                        "members": values,
                    }
                )
            oid_set = "\n".join(member["oid"] for member in members).encode()
            result["rules"][rule["id"]] = {
                "approved_old_blob": rule["old_blob"],
                "approved_old_bytes": len(old_data),
                "approved_old_normalized_nonblank_lines": len(old_lines),
                "approved_new_blob": rule["new_blob"],
                "approved_new_bytes": len(new_data),
                "approved_new_normalized_nonblank_lines": len(new_lines),
                "residual_count": len(members),
                "oid_set_sha256": hashlib.sha256(oid_set).hexdigest(),
                "group_count": len(group_rows),
                "groups": group_rows,
            }
    args.output.write_text(json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
