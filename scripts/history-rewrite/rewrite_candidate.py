#!/usr/bin/env python3
"""Apply and verify the bounded w13828 history-rewrite candidate.

This is deliberately the only implementation of the filter-repo callbacks and
their proof.  The hosted candidate workflow and its disposable synthetic suite
both call this file; neither carries a second transformation.
"""
import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from object_index import GitObjectIndex, TreeEntry

HEX = re.compile(r"[0-9a-f]{40}\Z")
ZERO = "0" * 40
TAG_TRANSPORT_PREFIX = "refs/tags/history-rewrite-stage/"


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


def write_refs(path: Path, values: dict[str, str], *, name_first: bool = False) -> None:
    path.write_text(
        "".join(
            f"{name} {value}\n" if name_first else f"{value} {name}\n"
            for name, value in sorted(values.items())
        ),
        encoding="utf-8",
    )


def ref_transaction(repo: Path, operations: list[tuple[str, str, str]]) -> None:
    commands = ["start"]
    commands.extend(f"{operation} {name} {value}" for operation, name, value in operations)
    commands.extend(("prepare", "commit", ""))
    result = subprocess.run(
        ["git", "-C", str(repo), "update-ref", "--no-deref", "--stdin"],
        input="\n".join(commands),
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        fail("atomic ref transport transaction failed")


def annotated_tag_transports(repo: Path, before: dict[str, str]) -> dict[str, str]:
    transports = {}
    for name, value in sorted(before.items()):
        if name.startswith("refs/tags/") or git(repo, "cat-file", "-t", value).strip() != "tag":
            continue
        staging = TAG_TRANSPORT_PREFIX + hashlib.sha256(name.encode("utf-8")).hexdigest()
        if staging in before or staging in transports.values():
            fail("annotated-tag transport ref collision")
        transports[name] = staging
    if transports and any(name.startswith(TAG_TRANSPORT_PREFIX) for name in before):
        fail("annotated-tag transport namespace is not empty")
    return transports


def stage_annotated_tags(repo: Path, before: dict[str, str], transports: dict[str, str]) -> dict[str, str]:
    if transports:
        operations = []
        for logical, staging in sorted(transports.items()):
            operations.append(("create", staging, before[logical]))
            operations.append(("delete", logical, before[logical]))
        ref_transaction(repo, operations)
    expected = dict(before)
    for logical, staging in transports.items():
        expected[staging] = expected.pop(logical)
    staged = refs(repo)
    if staged != expected:
        fail("annotated-tag staging ref readback mismatch")
    return staged


def restore_annotated_tags(repo: Path, staged: dict[str, str], transports: dict[str, str]) -> dict[str, str]:
    if transports:
        operations = []
        for logical, staging in sorted(transports.items()):
            if logical in staged or staging not in staged:
                fail("annotated-tag restoration domain mismatch")
            operations.append(("create", logical, staged[staging]))
            operations.append(("delete", staging, staged[staging]))
        ref_transaction(repo, operations)
    expected = dict(staged)
    for logical, staging in transports.items():
        expected[logical] = expected.pop(staging)
    restored = refs(repo)
    if restored != expected or any(name.startswith(TAG_TRANSPORT_PREFIX) for name in restored):
        fail("annotated-tag restoration ref readback mismatch")
    return restored


def load_policy(path: Path) -> dict:
    policy = json.loads(path.read_text(encoding="utf-8"))
    if policy.get("schema") != 2 or policy.get("repository") != "sednalabs/codex":
        fail("unsupported policy schema or repository")
    review_packet = policy.get("review_packet")
    if not isinstance(review_packet, dict) or review_packet.get("schema") != "review-packet-v1" or review_packet.get("executable") is not False:
        fail("policy must identify its non-executable review packet")
    canonical_source = policy.get("canonical_source_commit")
    if not isinstance(canonical_source, str) or not HEX.fullmatch(canonical_source):
        fail("policy requires a full canonical source commit")
    rules = policy.get("rules")
    if not isinstance(rules, list):
        fail("policy rules must be a list")
    canonical_phase = True
    for rule in rules:
        if not isinstance(rule, dict) or rule.get("scope") not in {"commit_subject", "commit_body", "path", "blob"}:
            fail("unsupported rewrite rule")
        if not isinstance(rule.get("id"), str) or not rule["id"]:
            fail("rewrite rule lacks a string id")
        kind = rule.get("kind")
        if kind == "exact_blob_replacement":
            if not canonical_phase:
                fail("canonical replacements must run before mechanical rules")
            if rule["scope"] != "blob" or not HEX.fullmatch(str(rule.get("old_blob", ""))) or not HEX.fullmatch(str(rule.get("new_blob", ""))):
                fail(f"{rule['id']}: invalid exact blob replacement")
            if rule["old_blob"] == rule["new_blob"] or not isinstance(rule.get("source_path"), str) or not rule["source_path"]:
                fail(f"{rule['id']}: invalid canonical source binding")
        else:
            canonical_phase = False
            if kind not in {"path", "blob_literal", "literal"}:
                fail(f"{rule['id']}: unsupported mechanical rule kind")
            if not isinstance(rule.get("old"), str) or not isinstance(rule.get("new"), str) or not rule["old"] or not rule["new"] or rule["old"] == rule["new"]:
                fail(f"{rule['id']}: rewrite rule lacks distinct old and new literals")
            if (kind == "path") != (rule["scope"] == "path") or (kind == "blob_literal") != (rule["scope"] == "blob") or (kind == "literal") != (rule["scope"] in {"commit_subject", "commit_body"}):
                fail(f"{rule['id']}: rule kind and scope disagree")
        if rule["scope"] in {"path", "blob"}:
            paths = rule.get("target_paths")
            if not isinstance(paths, list) or not paths or not all(isinstance(item, str) and item for item in paths):
                fail(f"{rule['id']}: target_paths is required")
            if rule.get("global"):
                fail(f"{rule['id']}: global blob rewrite is outside the scoped candidate")
    if len({rule["id"] for rule in rules}) != len(rules):
        fail("rewrite rule IDs must be unique")
    return policy


def changed_path(name: bytes, rules: list[dict]) -> bytes:
    result = name
    for rule in rules:
        if rule["scope"] == "path" and result in {item.encode() for item in rule["target_paths"]}:
            result = result.replace(rule["old"].encode(), rule["new"].encode())
    return result


def blob_rule_context(index: GitObjectIndex, rules: list[dict], canonical_source: str) -> dict:
    commits = index.commits()
    source_tree = index.commit(canonical_source).tree
    roots = tuple(dict.fromkeys(commit.tree for commit in commits))
    selected_by_rule: dict[str, set[bytes]] = {}
    replacement_by_rule: dict[str, bytes] = {}
    residual_by_rule: dict[str, set[bytes]] = {}
    blob_cache: dict[bytes, bytes] = {}

    def read_blob(identity: bytes) -> bytes:
        cached = blob_cache.get(identity)
        if cached is None:
            cached = index.blob(identity)
            blob_cache[identity] = cached
        return cached

    for rule in rules:
        if rule["scope"] != "blob":
            continue
        targets = {item.encode() for item in rule["target_paths"]}
        candidates = {
            entry.identity
            for tree in roots
            for path in targets
            if (entry := index.lookup_path(tree, path)) is not None and entry.kind == b"blob"
        }
        if rule["kind"] == "exact_blob_replacement":
            old_blob = rule["old_blob"].encode("ascii")
            new_blob = rule["new_blob"].encode("ascii")
            source_path = rule["source_path"].encode()
            source_value = index.lookup_path(source_tree, source_path)
            if source_value is None or source_value.kind != b"blob" or source_value.identity != new_blob:
                fail(f"{rule['id']}: canonical source path does not resolve to the reviewed new blob")
            replacement_by_rule[rule["id"]] = read_blob(new_blob)
            selected = {identity for identity in candidates if identity == old_blob}
            residual_by_rule[rule["id"]] = candidates - selected - {new_blob}
            if not selected:
                fail(f"{rule['id']}: guarded old blob is absent from its exact target path")
        else:
            old = rule["old"].encode()
            selected = {identity for identity in candidates if old in read_blob(identity)}
        locations = index.paths_for_oids(selected)
        for identity, paths in locations.items():
            if not paths.issubset(targets):
                fail(f"{rule['id']}: selected shared blob reaches an unrelated exact path")
        selected_by_rule[rule["id"]] = selected
    return {"selected": selected_by_rule, "replacements": replacement_by_rule, "residuals": residual_by_rule}


def preflight(index: GitObjectIndex, policy: dict) -> dict:
    rules = policy["rules"]
    index.commit(policy["canonical_source_commit"])
    path_rules = [rule for rule in rules if rule["scope"] == "path"]
    path_domain = {path.encode() for rule in path_rules for path in rule["target_paths"]}
    path_domain.update(changed_path(path, rules) for path in tuple(path_domain))
    roots = dict.fromkeys(commit.tree for commit in index.commits())
    for tree in roots:
        after_names = []
        for path in path_domain:
            if index.lookup_path(tree, path) is not None:
                after_names.append(changed_path(path, rules))
        if len(after_names) != len(set(after_names)):
            fail(f"{path_rules[0]['id']}: exact-path rename collision")
    return blob_rule_context(index, rules, policy["canonical_source_commit"])


def write_callbacks(work: Path, policy: Path, rule_context: dict) -> tuple[Path, Path, Path]:
    context = work / "rewrite-context.json"
    context.write_text(
        json.dumps(
            {
                "selected": {key: sorted(value.decode() for value in values) for key, values in rule_context["selected"].items()},
                "replacements": {key: base64.b64encode(value).decode("ascii") for key, value in rule_context["replacements"].items()},
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
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
        "import base64,json\npolicy=json.load(open(" + policy_literal + ", encoding='utf-8'))\n"
        "context=json.load(open(" + context_literal + ", encoding='utf-8')); selected=context['selected']\n"
        "for rule in policy['rules']:\n"
        "    if rule['scope']!='blob' or blob.original_id.decode('ascii') not in selected[rule['id']]: continue\n"
        "    if rule['kind']=='exact_blob_replacement': blob.data=base64.b64decode(context['replacements'][rule['id']], validate=True)\n"
        "    else: blob.data=blob.data.replace(rule['old'].encode(),rule['new'].encode())\n",
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


def normalize_ref_rows(
    rows: list[tuple[str, str, str]], transports: dict[str, str]
) -> list[tuple[str, str, str]]:
    staged_to_logical = {staged: logical for logical, staged in transports.items()}
    if len(staged_to_logical) != len(transports):
        fail("duplicate annotated-tag transport target")
    normalized = [(old, new, staged_to_logical.get(name, name)) for old, new, name in rows]
    if len({name for _, _, name in normalized}) != len(normalized):
        fail("annotated-tag normalized ref-map collision")
    if any((raw_old, raw_new) != (new_old, new_new) for (raw_old, raw_new, _), (new_old, new_new, _) in zip(rows, normalized)):
        fail("annotated-tag normalization changed an object ID")
    return normalized


def write_ref_map(path: Path, rows: list[tuple[str, str, str]]) -> None:
    path.write_text(
        f"{'old':40} {'new':40} ref\n"
        + "".join(f"{old} {new} {name}\n" for old, new, name in rows),
        encoding="utf-8",
    )


def transformed_bytes(data: bytes, identity: bytes, rules: list[dict], context: dict) -> bytes:
    for rule in rules:
        if rule["scope"] != "blob" or identity not in context["selected"][rule["id"]]:
            continue
        if rule["kind"] == "exact_blob_replacement":
            data = context["replacements"][rule["id"]]
        else:
            data = data.replace(rule["old"].encode(), rule["new"].encode())
    return data


def verify(
    repo: Path,
    preimage: Path,
    policy_path: Path,
    source_sha: str,
    work: Path,
    output: Path,
    commit_map: Path,
    ref_map: Path,
    preflight_context: dict | None = None,
) -> None:
    policy = load_policy(policy_path)
    rules = policy["rules"]
    before, after = refs(preimage), refs(repo)
    ref_rows = parse_ref_map(ref_map, before, after)
    output.mkdir(parents=True, exist_ok=True)
    with GitObjectIndex(preimage) as old_index, GitObjectIndex(repo) as new_index:
        old_commits = old_index.commits()
        new_index.commits()
        context = preflight_context if preflight_context is not None else preflight(old_index, policy)
        old_domain = {commit.identity for commit in old_commits}
        mapping = parse_commit_map(commit_map, old_domain)
        map_rows = []
        tree_pairs = []
        for old, new in sorted(mapping.items()):
            old_record, new_record = old_index.commit(old), new_index.commit(new)
            if [mapping[parent] for parent in old_record.parents] != list(new_record.parents):
                fail("ordered parent topology mismatch")
            map_rows.append((old, new, ",".join(old_record.parents), ",".join(new_record.parents), "verified"))
            tree_pairs.append((old_record.tree, new_record.tree))
        tag_rows = []
        for old, new, name in ref_rows:
            old_type = old_index.object_kind(old).decode("ascii")
            new_type = new_index.object_kind(new).decode("ascii")
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

        unique_tree_pairs = tuple(dict.fromkeys(tree_pairs))
        new_objects = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-path", "objects").strip())
        tree_diffs = old_index.batch_diff_trees(unique_tree_pairs, alternate_objects=new_objects)
        target_paths = {path.encode() for rule in rules if rule["scope"] in {"path", "blob"} for path in rule["target_paths"]}
        allowed_paths = set(target_paths)
        allowed_paths.update(changed_path(path, rules) for path in tuple(target_paths))
        selected_blobs = set().union(*context["selected"].values()) if context["selected"] else set()
        blob_pair_cache: dict[tuple[bytes, bytes], bool] = {}
        untouched_rows = []
        for old, new in sorted(mapping.items()):
            old_record, new_record = old_index.commit(old), new_index.commit(new)
            diffs = tree_diffs[(old_record.tree, new_record.tree)]
            for diff in diffs:
                if diff.path in allowed_paths:
                    continue
                if b"000000" in {diff.old_mode, diff.new_mode}:
                    fail("tree path domain changed outside approved exact transformations")
                if diff.old_mode != diff.new_mode:
                    fail("path mode or type changed")
                if diff.old_mode != b"160000":
                    fail("target bytes do not equal the approved transformation or non-target bytes changed")
                fail("non-target path, mode, type, or bytes changed")
            expected: dict[bytes, tuple[bytes, TreeEntry]] = {}
            excluded_from_untouched: set[bytes] = set()
            for path in allowed_paths:
                entry = old_index.lookup_path(old_record.tree, path)
                if entry is None:
                    continue
                expected_path = changed_path(path, rules)
                if expected_path in expected:
                    fail("tree path domain changed outside approved exact transformations")
                expected[expected_path] = (path, entry)
                if expected_path != path or (entry.kind == b"blob" and entry.identity in selected_blobs):
                    excluded_from_untouched.add(path)
            for path in allowed_paths:
                actual = new_index.lookup_path(new_record.tree, path)
                expected_item = expected.get(path)
                if expected_item is None:
                    if actual is not None:
                        fail("tree path domain changed outside approved exact transformations")
                    continue
                original_path, original = expected_item
                if actual is None or (actual.mode, actual.kind) != (original.mode, original.kind):
                    fail("path mode or type changed")
                selected_blob = original.kind == b"blob" and original.identity in selected_blobs
                if original.kind == b"blob":
                    pair = (original.identity, actual.identity)
                    valid = blob_pair_cache.get(pair)
                    if valid is None:
                        expected_data = transformed_bytes(old_index.blob(original.identity), original.identity, rules, context)
                        valid = new_index.blob(actual.identity) == expected_data
                        blob_pair_cache[pair] = valid
                    if not valid:
                        fail("target bytes do not equal the approved transformation or non-target bytes changed")
                if original_path == path and not selected_blob and actual != original:
                    fail("non-target path, mode, type, or bytes changed")
            untouched = old_index.leaf_count(old_record.tree) - len(excluded_from_untouched)
            untouched_rows.append((old, new, str(untouched), "verified"))
        (output / "untouched-proof.tsv").write_text("old_commit\tnew_commit\tuntouched_entries\tstatus\n" + "".join("\t".join(row) + "\n" for row in untouched_rows), encoding="utf-8")
        if source_sha not in mapping:
            fail("source SHA is absent from the exact commit-map domain")
        (output / "equivalence.txt").write_text(
            f"source_tree_before={old_index.commit(source_sha).tree}\nsource_tree_after={new_index.commit(mapping[source_sha]).tree}\n",
            encoding="utf-8",
        )
        residuals = {
            rule_id: {
                "count": len(values),
                "oid_set_sha256": hashlib.sha256("\n".join(sorted(value.decode("ascii") for value in values)).encode()).hexdigest(),
                "disposition": "review-required",
            }
            for rule_id, values in sorted(context["residuals"].items())
        }
        (output / "residual-review.json").write_text(json.dumps(residuals, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        if not any(rule["scope"] in {"path", "blob"} for rule in rules) and any(tree_diffs.values()):
            fail("tree changed without an explicit path/blob rule")


def apply(args: argparse.Namespace) -> None:
    policy = load_policy(args.policy)
    with GitObjectIndex(args.repo) as index:
        context = preflight(index, policy)
    before = refs(args.repo)
    if refs(args.preimage) != before:
        fail("logical preimage ref domain or object mismatch")
    args.work.mkdir(parents=True, exist_ok=True); args.output.mkdir(parents=True, exist_ok=True)
    callbacks = write_callbacks(args.work, args.policy, context)
    transports = annotated_tag_transports(args.repo, before)
    (args.output / "annotated-tag-ref-transport.json").write_text(
        json.dumps(
            {
                "schema": "annotated-tag-ref-transport-v1",
                "logical_to_staged": transports,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    write_refs(args.output / "refs-before.txt", before)
    staged_before = stage_annotated_tags(args.repo, before, transports)
    write_refs(args.output / "refs-before-staged.txt", staged_before)
    subprocess.run(["git", "-C", str(args.repo), "filter-repo", "--force", "--commit-callback", str(callbacks[0]), "--filename-callback", str(callbacks[1]), "--blob-callback", str(callbacks[2])], check=True)
    filter_repo_dir = git_path(args.repo, "filter-repo")
    commit_map = filter_repo_dir / "commit-map"; ref_map = filter_repo_dir / "ref-map"
    if not commit_map.is_file() or not ref_map.is_file():
        fail("git-filter-repo did not create both exact maps")
    shutil.copy2(commit_map, args.output / "commit-map.txt")
    raw_ref_map = args.output / "filter-repo-ref-map.raw.txt"
    shutil.copy2(ref_map, raw_ref_map)
    staged_after = refs(args.repo)
    write_refs(args.output / "refs-after-staged.txt", staged_after)
    raw_rows = parse_ref_map(raw_ref_map, staged_before, staged_after)
    normalized_rows = normalize_ref_rows(raw_rows, transports)
    after = restore_annotated_tags(args.repo, staged_after, transports)
    write_ref_map(args.output / "ref-map.txt", normalized_rows)
    parse_ref_map(args.output / "ref-map.txt", before, after)
    write_refs(args.output / "refs-after.txt", after)
    write_refs(args.output / "refs-final.txt", after, name_first=True)
    git_run(args.repo, "fsck", "--full", "--no-reflogs")
    (args.output / "tag-signatures.txt").write_text("tag_signature_consequence=rewritten commits require annotated-tag signature reassessment; no tags are created or uploaded\n", encoding="utf-8")
    verify(
        args.repo,
        args.preimage,
        args.policy,
        args.source_sha,
        args.work,
        args.output,
        args.output / "commit-map.txt",
        args.output / "ref-map.txt",
        context,
    )


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
