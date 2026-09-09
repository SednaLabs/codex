#!/usr/bin/env python3
"""Apply and verify the bounded w13828 history-rewrite candidate.

This is deliberately the only implementation of the filter-repo callbacks and
their proof.  The hosted candidate workflow and its disposable synthetic suite
both call this file; neither carries a second transformation.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HEX = re.compile(r"[0-9a-f]{40}\Z")
ZERO = "0" * 40


def fail(message: str) -> None:
    raise SystemExit(message)


def git(repo: Path, *args: str, text: bool = True) -> str | bytes:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=text)


def git_run(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True)


def git_path(repo: Path, path: str) -> Path:
    return Path(git(repo, "rev-parse", "--path-format=absolute", "--git-path", path).strip())


def oid(value: str, label: str) -> None:
    if not HEX.fullmatch(value) or value == ZERO:
        fail(f"{label} must be a nonzero full SHA")


def refs(repo: Path) -> dict[str, str]:
    result = {}
    for line in git(repo, "for-each-ref", "--format=%(refname) %(objectname)").splitlines():
        name, value = line.split()
        if name in result:
            fail("duplicate ref name")
        result[name] = value
    return result


def entries(repo: Path, commit: str) -> dict[bytes, tuple[bytes, bytes, bytes]]:
    result = {}
    raw = git(repo, "ls-tree", "-r", "-z", commit, text=False)
    for row in raw.split(b"\0"):
        if not row:
            continue
        meta, name = row.split(b"\t", 1)
        mode, kind, blob = meta.split()
        if name in result:
            fail("tree contains duplicate paths")
        result[name] = (mode, kind, blob)
    return result


def blob(repo: Path, identity: bytes) -> bytes:
    return git(repo, "cat-file", "-p", identity.decode(), text=False)


def commit_parents(repo: Path, identity: str) -> list[str]:
    raw = git(repo, "cat-file", "-p", identity, text=False)
    return [line[7:].decode() for line in raw.split(b"\n") if line.startswith(b"parent ")]


def load_policy(path: Path) -> list[dict]:
    policy = json.loads(path.read_text(encoding="utf-8"))
    rules = policy.get("rules")
    if not isinstance(rules, list):
        fail("policy rules must be a list")
    for rule in rules:
        if not isinstance(rule, dict) or rule.get("scope") not in {"commit_subject", "commit_body", "path", "blob"}:
            fail("unsupported rewrite rule")
        if not isinstance(rule.get("id"), str) or not isinstance(rule.get("old"), str) or not isinstance(rule.get("new"), str):
            fail("rewrite rule lacks a string id, old, or new value")
        if rule["scope"] in {"path", "blob"}:
            paths = rule.get("target_paths")
            if not isinstance(paths, list) or not paths or not all(isinstance(item, str) and item for item in paths):
                fail(f"{rule['id']}: target_paths is required")
            if rule.get("global"):
                fail(f"{rule['id']}: global blob rewrite is outside the scoped candidate")
    return rules


def changed_path(name: bytes, rules: list[dict]) -> bytes:
    result = name
    for rule in rules:
        if rule["scope"] == "path" and result in {item.encode() for item in rule["target_paths"]}:
            result = result.replace(rule["old"].encode(), rule["new"].encode())
    return result


def blob_rule_oids(repo: Path, rules: list[dict]) -> dict[str, set[bytes]]:
    all_entries = [entries(repo, commit) for commit in git(repo, "rev-list", "--all").splitlines()]
    answer: dict[str, set[bytes]] = {}
    for rule in rules:
        if rule["scope"] != "blob":
            continue
        targets = {item.encode() for item in rule["target_paths"]}
        selected = {value[2] for tree in all_entries for name, value in tree.items() if name in targets and value[1] == b"blob"}
        locations = {identity: set() for identity in selected}
        # Deliberately collect locations only for selected OIDs.  Unrelated
        # duplicated blobs are allowed; selected blobs may not escape scope.
        for tree in all_entries:
            for name, value in tree.items():
                if value[2] in locations:
                    locations[value[2]].add(name)
        for identity, paths in locations.items():
            if not paths.issubset(targets):
                fail(f"{rule['id']}: selected shared blob reaches an unrelated exact path")
        answer[rule["id"]] = selected
    return answer


def preflight(repo: Path, rules: list[dict]) -> dict[str, set[bytes]]:
    for rule in rules:
        if rule["scope"] != "path":
            continue
        for commit in git(repo, "rev-list", "--all").splitlines():
            before = entries(repo, commit)
            after_names = [changed_path(name, rules) for name in before]
            if len(after_names) != len(set(after_names)):
                fail(f"{rule['id']}: exact-path rename collision")
    return blob_rule_oids(repo, rules)


def write_callbacks(work: Path, policy: Path, blob_oids: dict[str, set[bytes]]) -> tuple[Path, Path, Path]:
    context = work / "rewrite-context.json"
    context.write_text(json.dumps({key: sorted(value.decode() for value in values) for key, values in blob_oids.items()}, sort_keys=True), encoding="utf-8")
    commit = work / "commit_callback.py"
    filename = work / "filename_callback.py"
    blob_cb = work / "blob_callback.py"
    policy_literal = repr(str(policy.resolve()))
    context_literal = repr(str(context.resolve()))
    commit.write_text(
        "import json\npolicy=json.load(open(" + policy_literal + ", encoding='utf-8'))\n"
        "for rule in policy['rules']:\n"
        "    if rule['scope'] not in {'commit_subject','commit_body'}: continue\n"
        "    message=commit.message.decode('utf-8','replace'); subject, sep, body=message.partition('\\n')\n"
        "    if rule['scope']=='commit_subject': commit.message=(subject.replace(rule['old'],rule['new'])+('\\n'+body if sep else '')).encode()\n"
        "    else: commit.message=(subject+('\\n' if sep else '')+body.replace(rule['old'],rule['new'])).encode()\n",
        encoding="utf-8",
    )
    filename.write_text(
        "import json\npolicy=json.load(open(" + policy_literal + ", encoding='utf-8'))\n"
        "for rule in policy['rules']:\n"
        "    if rule['scope']=='path' and filename in {p.encode() for p in rule['target_paths']}: filename=filename.replace(rule['old'].encode(),rule['new'].encode())\n"
        "return filename\n",
        encoding="utf-8",
    )
    blob_cb.write_text(
        "import json\npolicy=json.load(open(" + policy_literal + ", encoding='utf-8'))\n"
        "selected=json.load(open(" + context_literal + ", encoding='utf-8'))\n"
        "for rule in policy['rules']:\n"
        "    if rule['scope']=='blob' and blob.original_id.decode('ascii') in selected[rule['id']]: blob.data=blob.data.replace(rule['old'].encode(),rule['new'].encode())\n",
        encoding="utf-8",
    )
    return commit, filename, blob_cb


def parse_commit_map(path: Path, old_domain: set[str]) -> dict[str, str]:
    rows: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if not fields or fields[0].lower() in {"old", "old_commit"}:
            continue
        if len(fields) != 2:
            fail("malformed commit map")
        oid(fields[0], "commit-map old ID"); oid(fields[1], "commit-map new ID")
        rows.append((fields[0], fields[1]))
    if not rows or len({old for old, _ in rows}) != len(rows):
        fail("empty or duplicate commit-map key")
    mapping = dict(rows)
    if set(mapping) != old_domain:
        fail("commit-map key domain mismatch")
    return mapping


def parse_ref_map(path: Path, before: dict[str, str], after: dict[str, str]) -> list[tuple[str, str, str]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if not fields or fields[0].lower() in {"old", "old_object"}:
            continue
        if len(fields) != 3:
            fail("malformed ref map")
        oid(fields[0], "ref-map old ID"); oid(fields[1], "ref-map new ID")
        rows.append((fields[0], fields[1], fields[2]))
    if not rows or len({name for _, _, name in rows}) != len(rows):
        fail("empty or duplicate ref-map name")
    if {name for _, _, name in rows} != set(before) or set(before) != set(after):
        fail("ref-map exact name domain mismatch")
    for old, new, name in rows:
        if before[name] != old or after[name] != new:
            fail("ref-map object readback mismatch")
    return rows


def transformed_bytes(data: bytes, identity: bytes, rules: list[dict], selected: dict[str, set[bytes]]) -> bytes:
    for rule in rules:
        if rule["scope"] == "blob" and identity in selected[rule["id"]]:
            data = data.replace(rule["old"].encode(), rule["new"].encode())
    return data


def verify(repo: Path, preimage: Path, policy_path: Path, source_sha: str, work: Path, output: Path, commit_map: Path, ref_map: Path) -> None:
    rules = load_policy(policy_path)
    selected = preflight(preimage, rules)
    before, after = refs(preimage), refs(repo)
    old_domain = set(git(preimage, "rev-list", "--all").splitlines())
    mapping = parse_commit_map(commit_map, old_domain)
    ref_rows = parse_ref_map(ref_map, before, after)
    output.mkdir(parents=True, exist_ok=True)
    map_rows = []
    for old, new in sorted(mapping.items()):
        old_parents, new_parents = commit_parents(preimage, old), commit_parents(repo, new)
        if [mapping[parent] for parent in old_parents] != new_parents:
            fail("ordered parent topology mismatch")
        map_rows.append((old, new, ",".join(old_parents), ",".join(new_parents), "verified"))
    tag_rows = []
    for old, new, name in ref_rows:
        old_type, new_type = git(preimage, "cat-file", "-t", old).strip(), git(repo, "cat-file", "-t", new).strip()
        if old_type == new_type == "commit":
            if mapping.get(old) != new:
                fail("lightweight tag or branch is not commit-map bound")
            kind = "lightweight" if name.startswith("refs/tags/") else "branch"
        elif old_type == new_type == "tag":
            old_peeled = git(preimage, "rev-parse", f"{old}^{{commit}}").strip()
            new_peeled = git(repo, "rev-parse", f"{new}^{{commit}}").strip()
            if mapping.get(old_peeled) != new_peeled:
                fail("annotated tag peeled identity is not commit-map bound")
            kind = "annotated"
        else:
            fail("ref object type changed")
        if name.startswith("refs/tags/"):
            old_status = "verified" if subprocess.run(["git", "-C", str(preimage), "verify-tag", name], capture_output=True).returncode == 0 else "unsigned-or-unverified"
            new_status = "verified" if subprocess.run(["git", "-C", str(repo), "verify-tag", name], capture_output=True).returncode == 0 else "unsigned-or-unverified"
            tag_rows.append((name, kind, old, new, old_status, new_status, "rewritten-commits-require-signature-reassessment"))
    proof = output / "map-proof.tsv"
    proof.write_text("old_commit\tnew_commit\told_parents\tnew_parents\tstatus\n" + "".join("\t".join(row) + "\n" for row in map_rows), encoding="utf-8")
    (output / "tag-proof.tsv").write_text("ref\tkind\told_object\tnew_object\told_signature\tnew_signature\tconsequence\n" + "".join("\t".join(row) + "\n" for row in tag_rows), encoding="utf-8")
    untouched_rows = []
    for old, new in sorted(mapping.items()):
        original, rewritten = entries(preimage, old), entries(repo, new)
        expected_names = {changed_path(name, rules) for name in original}
        if set(rewritten) != expected_names:
            fail("tree path domain changed outside approved exact transformations")
        untouched = 0
        for name, (mode, kind, identity) in original.items():
            expected_name = changed_path(name, rules)
            got = rewritten.get(expected_name)
            if got is None or got[:2] != (mode, kind):
                fail("path mode or type changed")
            expected_data = transformed_bytes(blob(preimage, identity), identity, rules, selected) if kind == b"blob" else b""
            if kind == b"blob" and blob(repo, got[2]) != expected_data:
                fail("target bytes do not equal the approved transformation or non-target bytes changed")
            selected_blob = kind == b"blob" and any(identity in values for values in selected.values())
            if name == expected_name and not selected_blob and got != (mode, kind, identity):
                fail("non-target path, mode, type, or bytes changed")
            if name == expected_name and not selected_blob:
                untouched += 1
        untouched_rows.append((old, new, str(untouched), "verified"))
    (output / "untouched-proof.tsv").write_text("old_commit\tnew_commit\tuntouched_entries\tstatus\n" + "".join("\t".join(row) + "\n" for row in untouched_rows), encoding="utf-8")
    if source_sha not in mapping:
        fail("source SHA is absent from the exact commit-map domain")
    (output / "equivalence.txt").write_text(
        f"source_tree_before={git(preimage, 'rev-parse', source_sha + '^{tree}').strip()}\nsource_tree_after={git(repo, 'rev-parse', mapping[source_sha] + '^{tree}').strip()}\n",
        encoding="utf-8",
    )
    if not any(rule["scope"] in {"path", "blob"} for rule in rules):
        for old, new in mapping.items():
            if git(preimage, "rev-parse", old + "^{tree}") != git(repo, "rev-parse", new + "^{tree}"):
                fail("tree changed without an explicit path/blob rule")


def apply(args: argparse.Namespace) -> None:
    rules = load_policy(args.policy)
    selected = preflight(args.repo, rules)
    before = refs(args.repo)
    old_domain = set(git(args.repo, "rev-list", "--all").splitlines())
    args.work.mkdir(parents=True, exist_ok=True); args.output.mkdir(parents=True, exist_ok=True)
    callbacks = write_callbacks(args.work, args.policy, selected)
    subprocess.run(["git", "-C", str(args.repo), "filter-repo", "--force", "--commit-callback", str(callbacks[0]), "--filename-callback", str(callbacks[1]), "--blob-callback", str(callbacks[2])], check=True)
    filter_repo_dir = git_path(args.repo, "filter-repo")
    commit_map = filter_repo_dir / "commit-map"; ref_map = filter_repo_dir / "ref-map"
    if not commit_map.is_file() or not ref_map.is_file():
        fail("git-filter-repo did not create both exact maps")
    shutil.copy2(commit_map, args.output / "commit-map.txt"); shutil.copy2(ref_map, args.output / "ref-map.txt")
    (args.output / "refs-before.txt").write_text("".join(f"{value} {name}\n" for name, value in sorted(before.items())), encoding="utf-8")
    (args.output / "refs-after.txt").write_text("".join(f"{value} {name}\n" for name, value in sorted(refs(args.repo).items())), encoding="utf-8")
    (args.output / "refs-final.txt").write_text("".join(f"{name} {value}\n" for name, value in sorted(refs(args.repo).items())), encoding="utf-8")
    git_run(args.repo, "fsck", "--full", "--no-reflogs")
    (args.output / "tag-signatures.txt").write_text("tag_signature_consequence=rewritten commits require annotated-tag signature reassessment; no tags are created or uploaded\n", encoding="utf-8")
    verify(args.repo, args.preimage, args.policy, args.source_sha, args.work, args.output, args.output / "commit-map.txt", args.output / "ref-map.txt")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("apply", "verify"))
    parser.add_argument("--repo", type=Path, required=True); parser.add_argument("--preimage", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True); parser.add_argument("--source-sha", required=True)
    parser.add_argument("--work", type=Path, required=True); parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--commit-map", type=Path); parser.add_argument("--ref-map", type=Path)
    args = parser.parse_args()
    if args.mode == "apply":
        apply(args)
    else:
        if not args.commit_map or not args.ref_map:
            fail("verify requires --commit-map and --ref-map")
        verify(args.repo, args.preimage, args.policy, args.source_sha, args.work, args.output, args.commit_map, args.ref_map)


if __name__ == "__main__":
    main()
