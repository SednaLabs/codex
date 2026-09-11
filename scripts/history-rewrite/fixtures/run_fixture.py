#!/usr/bin/env python3
"""Hosted-only synthetic execution of the production rewrite candidate."""
import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tag_proof import signature_presence
import publication as publication_module

DRIVER = ROOT / "rewrite_candidate.py"
CLASSIFIER = ROOT / "classify.py"
ADAPTER = ROOT / "adapt_review_packet.py"
RECEIPT = ROOT.parent / "recovery-snapshot" / "receipt.py"
PUBLICATION = ROOT / "publication.py"


def run(*args: str, cwd: Path | None = None, input: bytes | None = None) -> str:
    return subprocess.check_output(args, cwd=cwd, input=input, text=input is None).decode() if input is not None else subprocess.check_output(args, cwd=cwd, text=True)


def command(repo: Path, *args: str) -> str:
    return run("git", "-C", str(repo), *args)


def write_policy(path: Path, rules: list[dict], canonical_source: str, patterns: list[dict] | None = None) -> None:
    path.write_text(
        json.dumps(
            {
                "schema": 2,
                "repository": "sednalabs/codex",
                "canonical_source_commit": canonical_source,
                "review_packet": {"schema": "review-packet-v1", "executable": False},
                "rules": rules,
                "patterns": patterns or [],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def make_repo(root: Path, *, shared_target_unrelated: bool = False, collision: bool = False) -> tuple[Path, str, str, str, str, str, str]:
    repo = root / "repo"
    run("git", "init", "-q", "-b", "main", str(repo)); command(repo, "config", "user.name", "fixture"); command(repo, "config", "user.email", "fixture@example.invalid")
    (repo / "rename-old.txt").write_bytes(b"rename\n")
    (repo / "canonical.txt").write_bytes(b"unsupported historical variant\n")
    (repo / "target.txt").write_bytes(b"target old value\n")
    (repo / "target-copy.txt").write_bytes(b"target old value\n")
    (repo / "unrelated-a.txt").write_bytes(b"unrelated duplicate\n")
    (repo / "unrelated-b.txt").write_bytes(b"unrelated duplicate\n")
    (repo / "binary.dat").write_bytes(b"\xff\x00binary\n")
    if shared_target_unrelated:
        (repo / "unrelated-a.txt").write_bytes(b"target old value\n")
    if collision:
        (repo / "rename-new.txt").write_bytes(b"already here\n")
    command(repo, "add", "."); command(repo, "commit", "-qm", "old subject")
    old_blob = command(repo, "rev-parse", "HEAD:canonical.txt").strip()
    base = command(repo, "rev-parse", "HEAD").strip(); command(repo, "tag", "lightweight"); command(repo, "tag", "-a", "annotated", "-m", "annotated", base)
    signed_tag = run(
        "git", "-C", str(repo), "hash-object", "-t", "tag", "-w", "--stdin",
        input=(
            f"object {base}\ntype commit\ntag annotated-signed\n"
            "tagger fixture <fixture@example.invalid> 1 +0000\n\n"
            "syntactically signed but deliberately unvalidated\n"
            "-----BEGIN PGP SIGNATURE-----\n\nZmFrZQ==\n-----END PGP SIGNATURE-----\n"
        ).encode(),
    ).strip()
    command(repo, "update-ref", "refs/tags/annotated-signed", signed_tag)
    command(repo, "checkout", "-qb", "side"); (repo / "side.txt").write_bytes(b"side\n"); command(repo, "add", "."); command(repo, "commit", "-qm", "side")
    command(repo, "checkout", "-q", "main"); (repo / "target.txt").write_bytes(b"target old value main\n"); command(repo, "add", "target.txt"); command(repo, "commit", "-qm", "old main")
    command(repo, "merge", "--no-ff", "-m", "old merge", "side")
    merge_source = command(repo, "rev-parse", "HEAD").strip()
    (repo / "canonical.txt").write_bytes(b"unmatched historical variant\n"); command(repo, "add", "canonical.txt"); command(repo, "commit", "-qm", "intermediate canonical variant")
    (repo / "canonical.txt").write_bytes(b"reviewed canonical replacement\n"); command(repo, "add", "canonical.txt"); command(repo, "commit", "-qm", "canonical source")
    canonical_source = command(repo, "rev-parse", "HEAD").strip()
    new_blob = command(repo, "rev-parse", "HEAD:canonical.txt").strip()
    (repo / "canonical.txt").write_bytes(b"unsupported historical variant\n"); command(repo, "add", "canonical.txt"); command(repo, "commit", "-qm", "collapse candidate")
    remote = root / "synthetic-source.git"
    run("git", "clone", "--bare", "--no-local", str(repo), str(remote))
    command(repo, "remote", "add", "synthetic-source", str(remote))
    source = command(repo, "rev-parse", "HEAD").strip()
    return repo, source, "main", canonical_source, old_blob, new_blob, merge_source


def make_bare_mirror(root: Path, source: Path) -> tuple[Path, str, str]:
    repo = root / "repo.git"
    run("git", "clone", "--mirror", "--no-local", str(source), str(repo))
    return repo, command(repo, "rev-parse", "HEAD").strip(), "master"


def make_isolated_mirror(root: Path, source: Path, source_sha: str) -> tuple[Path, Path]:
    repo = root / "repo.git"
    run("git", "clone", "--mirror", "--no-local", str(source), str(repo))
    mapping = {}
    for line in command(repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads", "refs/tags").splitlines():
        name, object_id = line.split()
        isolated = "refs/rewrites/selected/" + hashlib.sha256(name.encode()).hexdigest()
        mapping[name] = isolated
        command(repo, "update-ref", isolated, object_id)
        command(repo, "update-ref", "-d", name)
    command(repo, "update-ref", "refs/rewrites/source", source_sha)
    mapping_path = root / "original-to-isolated-ref-map.json"
    mapping_path.write_text(json.dumps(mapping, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    return repo, mapping_path


def base_rules(old_blob: str, new_blob: str) -> list[dict]:
    return [
        {"id": "canonical", "kind": "exact_blob_replacement", "scope": "blob", "old_blob": old_blob, "new_blob": new_blob, "source_path": "canonical.txt", "target_paths": ["canonical.txt"]},
        {"id": "subject", "kind": "literal", "scope": "commit_subject", "old": "old", "new": "approved"},
        {"id": "rename", "kind": "path", "scope": "path", "old": "rename-old", "new": "rename-new", "target_paths": ["rename-old.txt"]},
        {"id": "blob", "kind": "blob_literal", "scope": "blob", "old": "old value", "new": "approved value", "target_paths": ["target.txt", "target-copy.txt"]},
    ]


def invoke(repo: Path, source: str, policy: Path, root: Path, mode: str = "apply", commit_map: Path | None = None, ref_map: Path | None = None, *, relative_paths: bool = False, original_to_isolated_ref_map: Path | None = None) -> subprocess.CompletedProcess[str]:
    preimage = root / "preimage.git"
    if not preimage.exists():
        run("git", "clone", "--mirror", "--no-local", str(repo), str(preimage))
    output, work = root / "output", root / "work"
    invocation_root = root if relative_paths else None
    argument_path = (lambda path: os.path.relpath(path, root)) if relative_paths else str
    args = ["python3", str(DRIVER), mode, "--repo", argument_path(repo), "--preimage", argument_path(preimage), "--policy", argument_path(policy), "--source-sha", source, "--work", argument_path(work), "--output", argument_path(output)]
    if mode == "verify":
        args += ["--commit-map", argument_path(commit_map), "--ref-map", argument_path(ref_map)]
    if original_to_isolated_ref_map is not None:
        args += ["--original-to-isolated-ref-map", argument_path(original_to_isolated_ref_map)]
    return subprocess.run(args, cwd=invocation_root, text=True, capture_output=True)


def signature_parser_fixture() -> str:
    unsigned = b"object " + b"1" * 40 + b"\ntype commit\ntag unsigned\n\nmessage\n"
    signed = unsigned + b"-----BEGIN PGP SIGNATURE-----\n\nZmFrZQ==\n-----END PGP SIGNATURE-----\n"
    if signature_presence(unsigned) != "absent" or signature_presence(signed) != "present-openpgp":
        raise SystemExit("raw tag signature presence fixture mismatch")
    failures = (
        signed.removesuffix(b"-----END PGP SIGNATURE-----\n"),
        unsigned + b"-----BEGIN FUTURE SIGNATURE-----\nvalue\n-----END FUTURE SIGNATURE-----\n",
    )
    for raw in failures:
        try:
            signature_presence(raw)
        except SystemExit:
            continue
        raise SystemExit("malformed or unknown tag signature armor unexpectedly passed")
    return hashlib.sha256(unsigned + signed + b"".join(failures)).hexdigest()


def expect_failure(name: str, result: subprocess.CompletedProcess[str], expected_diagnostic: str | None = None) -> str:
    if result.returncode == 0:
        raise SystemExit(f"negative fixture unexpectedly passed: {name}")
    diagnostic = result.stdout + result.stderr
    if expected_diagnostic is not None and diagnostic.strip() != expected_diagnostic:
        raise SystemExit(f"negative fixture produced the wrong diagnostic: {name}")
    return hashlib.sha256(diagnostic.encode()).hexdigest()


def require_driver_success(phase: str, result: subprocess.CompletedProcess[str]) -> None:
    if result.returncode == 0:
        return
    # The synthetic route has no real-history input.  Preserve the production
    # driver's own failure without emitting unbounded subprocess output.
    diagnostic = (result.stderr or result.stdout or "driver produced no diagnostic").strip()
    print(f"fixture_phase={phase} driver_exit={result.returncode}", file=sys.stderr)
    print(diagnostic[-4096:], file=sys.stderr)
    raise SystemExit(f"synthetic production driver failed during {phase}")


def read_classifier_output(path: Path) -> tuple[list[tuple[str, ...]], dict[str, int]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    expected_header = "kind\tidentity\tpath\tpattern_class\tclassification\trationale_sha256\tproof_sha256\tmatch_count"
    if not lines or lines[0] != expected_header:
        raise SystemExit("synthetic classifier output header mismatch")
    rows = []
    counts = {}
    for line in lines[1:]:
        fields = line.split("\t")
        if fields[0] == "#count":
            if len(fields) != 3 or fields[1] in counts:
                raise SystemExit("synthetic classifier count row is malformed")
            counts[fields[1]] = int(fields[2])
        elif len(fields) != 8:
            raise SystemExit("synthetic classifier identity row is malformed")
        else:
            rows.append(tuple(fields))
    return rows, counts


def classifier_path_rename_fixture(root: Path, repo: Path, source: str, policy: Path) -> str:
    preimage = root / "preimage.git"
    before, after = root / "classification-before.tsv", root / "classification-after.tsv"
    for phase, target, output in (("classifier_before", preimage, before), ("classifier_after", repo, after)):
        result = subprocess.run(["python3", str(CLASSIFIER), str(target), str(policy), str(output)], text=True, capture_output=True)
        require_driver_success(phase, result)
    before_rows, before_counts = read_classifier_output(before)
    after_rows, after_counts = read_classifier_output(after)
    before_commits = command(preimage, "rev-list", "--all").splitlines()
    after_commits = command(repo, "rev-list", "--all").splitlines()
    if not before_commits or len(before_commits) != len(after_commits):
        raise SystemExit("synthetic classifier commit domain changed")
    occurrences = len(before_commits)
    original_blob = command(preimage, "rev-parse", f"{source}:rename-old.txt").strip()
    mapped_source = read_commit_map(root / "output/commit-map.txt")[source]
    rewritten_blob = command(repo, "rev-parse", f"{mapped_source}:rename-new.txt").strip()
    if original_blob != rewritten_blob:
        raise SystemExit("synthetic path rename changed the blob identity")
    rationale = hashlib.sha256(b"").hexdigest()
    old_proof = hashlib.sha256(b"rename-old.txt").hexdigest()
    new_proof = hashlib.sha256(b"rename-new.txt").hexdigest()
    expected_before = ("path", original_blob, "path_sha256:" + old_proof, "rename:old", "rewrite_rule_old", rationale, old_proof, "1")
    expected_after = ("path", rewritten_blob, "path_sha256:" + new_proof, "rename:new", "rewrite_rule_new", rationale, new_proof, "1")
    before_rename_rows = [row for row in before_rows if row[3].startswith("rename:")]
    after_rename_rows = [row for row in after_rows if row[3].startswith("rename:")]
    if before_rename_rows != [expected_before] * occurrences or after_rename_rows != [expected_after] * occurrences:
        raise SystemExit("synthetic classifier path rename identity rows mismatch")
    if before_counts.get("rename:old", 0) != occurrences or before_counts.get("rename:new", 0) != 0:
        raise SystemExit("synthetic classifier preimage path counts mismatch")
    if after_counts.get("rename:old", 0) != 0 or after_counts.get("rename:new", 0) != occurrences:
        raise SystemExit("synthetic classifier rewritten path counts mismatch")
    before_metadata = json.loads(Path(str(before) + ".metadata.json").read_text(encoding="utf-8"))
    after_metadata = json.loads(Path(str(after) + ".metadata.json").read_text(encoding="utf-8"))
    expected_pattern = {"matches": occurrences, "rows": occurrences}
    if before_metadata.get("per_pattern", {}).get("rename:old") != expected_pattern or "rename:new" in before_metadata.get("per_pattern", {}):
        raise SystemExit("synthetic classifier preimage metadata counts mismatch")
    if after_metadata.get("per_pattern", {}).get("rename:new") != expected_pattern or "rename:old" in after_metadata.get("per_pattern", {}):
        raise SystemExit("synthetic classifier rewritten metadata counts mismatch")
    evidence = {
        "before_sha256": hashlib.sha256(before.read_bytes()).hexdigest(),
        "after_sha256": hashlib.sha256(after.read_bytes()).hexdigest(),
        "blob": original_blob,
        "occurrences": occurrences,
    }
    return hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def classifier_exact_differential_fixture(root: Path) -> str:
    reference = os.environ.get("HISTORY_REWRITE_REFERENCE_CLASSIFIER")
    if not reference or not Path(reference).is_file():
        raise SystemExit("exact frozen classifier reference is unavailable")

    root.mkdir()
    repo, _, _, canonical_source, old_blob, new_blob, _ = make_repo(root)
    repo_bytes = os.fsencode(repo)
    raw_paths = [
        b"order-tab\tname.txt",
        b"order-newline\nname.txt",
        b"order-nonutf8-\xff.txt",
        b"nested-order/a-file.txt",
        b"nested-order/a-file.txt.child",
    ]
    os.makedirs(repo_bytes + b"/nested-order", exist_ok=True)
    for position, relative in enumerate(raw_paths):
        descriptor = os.open(repo_bytes + b"/" + relative, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            os.write(descriptor, f"raw-path-{position}\n".encode())
        finally:
            os.close(descriptor)
    command(repo, "add", "-A")
    command(repo, "commit", "-qm", "raw path ordering")
    for position in range(16):
        command(repo, "commit", "--allow-empty", "-qm", f"repeated root tree {position}")

    raw_commit = command_bytes(repo, "cat-file", "commit", "HEAD")
    headers, separator, _ = raw_commit.partition(b"\n\n")
    if separator != b"\n\n":
        raise SystemExit("synthetic NUL commit lacks a header separator")
    nul_commit = run(
        "git", "-C", str(repo), "hash-object", "--literally", "-t", "commit", "-w", "--stdin",
        input=headers + separator + b"old nul subject\n\nbefore-nul\x00after-nul\n",
    ).strip()
    if len(nul_commit) != 40:
        raise SystemExit("synthetic NUL commit object identity is malformed")
    command(repo, "update-ref", "refs/heads/nul-body", nul_commit)

    policy = root / "differential-policy.json"
    write_policy(
        policy,
        base_rules(old_blob, new_blob),
        canonical_source,
        patterns=[
            {
                "id": "all-nonempty-fields",
                "pattern": "(?s).",
                "classification": "fixture_context",
                "rationale": "exercise exact field order and occurrence accounting",
                "priority": 200,
            }
        ],
    )
    reference_output = root / "reference" / "classification.tsv"
    candidate_output = root / "candidate" / "classification.tsv"
    reference_output.parent.mkdir()
    candidate_output.parent.mkdir()
    for phase, classifier, output in (
        ("classifier_exact_reference", Path(reference), reference_output),
        ("classifier_exact_candidate", CLASSIFIER, candidate_output),
    ):
        result = subprocess.run(
            ["python3", str(classifier), str(repo), str(policy), str(output)],
            text=True,
            capture_output=True,
        )
        require_driver_success(phase, result)
    compared = {}
    for suffix in ("", ".gz", ".metadata.json"):
        expected = Path(str(reference_output) + suffix).read_bytes()
        actual = Path(str(candidate_output) + suffix).read_bytes()
        if actual != expected:
            raise SystemExit(f"exact classifier differential mismatch: {suffix or 'tsv'}")
        compared[suffix or "tsv"] = hashlib.sha256(actual).hexdigest()
    return hashlib.sha256(json.dumps(compared, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def corrupt_map(path: Path, kind: str, source: str, branch: str, repo: Path) -> tuple[Path, Path]:
    cmap, rmap = path / "output/commit-map.txt", path / "output/ref-map.txt"
    cmap_copy, rmap_copy = path / f"{kind}.commit-map", path / f"{kind}.ref-map"
    shutil.copy2(cmap, cmap_copy); shutil.copy2(rmap, rmap_copy)
    lines = cmap_copy.read_text().splitlines()
    if kind == "domain":
        cmap_copy.write_text("\n".join(lines[:-1]) + "\n")
    elif kind == "zero":
        cmap_copy.write_text("\n".join((line.split()[0] + " " + "0" * 40) if line.split() and line.split()[0] == source else line for line in lines) + "\n")
    elif kind == "refchange":
        command(repo, "update-ref", f"refs/heads/{branch}", command(repo, "rev-parse", "side").strip())
    return cmap_copy, rmap_copy


def commit_parents(repo: Path, commit: str) -> list[str]:
    return command(repo, "show", "-s", "--format=%P", commit).split()


def read_commit_map(path: Path) -> dict[str, str]:
    mapping = {}
    for line in path.read_text().splitlines():
        fields = line.split()
        if not fields or fields[0].lower() in {"old", "old_commit"}:
            continue
        if len(fields) != 2 or fields[0] in mapping:
            raise SystemExit("synthetic commit map is malformed")
        mapping[fields[0]] = fields[1]
    if not mapping:
        raise SystemExit("synthetic commit map is empty")
    return mapping


def replace_commit_map(path: Path, output: Path, replacements: dict[str, str]) -> None:
    seen = set()
    lines = []
    for line in path.read_text().splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0] in replacements:
            if fields[0] in seen:
                raise SystemExit("synthetic commit map replacement is duplicated")
            seen.add(fields[0])
            line = fields[0] + " " + replacements[fields[0]]
        lines.append(line)
    if seen != set(replacements):
        raise SystemExit("synthetic commit map replacement domain mismatch")
    output.write_text("\n".join(lines) + "\n")


def replace_ref_map(path: Path, output: Path, ref: str, new: str) -> None:
    matches = 0
    lines = []
    for line in path.read_text().splitlines():
        fields = line.split()
        if len(fields) == 3 and fields[2] == ref:
            matches += 1
            line = fields[0] + " " + new + " " + fields[2]
        lines.append(line)
    if matches != 1:
        raise SystemExit("synthetic ref map replacement domain mismatch")
    output.write_text("\n".join(lines) + "\n")


def rewrite_commit(repo: Path, commit: str, *, tree: str | None = None, parents: list[str] | None = None) -> str:
    raw = command_bytes(repo, "cat-file", "commit", commit)
    header, separator, message = raw.partition(b"\n\n")
    lines = header.split(b"\n")
    if separator != b"\n\n" or not lines or not lines[0].startswith(b"tree "):
        raise SystemExit("synthetic mapped commit is malformed")
    current_tree = lines[0][5:].decode()
    parent_end = 1
    while parent_end < len(lines) and lines[parent_end].startswith(b"parent "):
        parent_end += 1
    if any(line.startswith((b"tree ", b"parent ")) for line in lines[parent_end:]):
        raise SystemExit("synthetic mapped commit headers are malformed")
    selected_tree = current_tree if tree is None else tree
    selected_parents = [line[7:].decode() for line in lines[1:parent_end]] if parents is None else parents
    rewritten = b"\n".join(
        [b"tree " + selected_tree.encode()]
        + [b"parent " + parent.encode() for parent in selected_parents]
        + lines[parent_end:]
    ) + separator + message
    return run("git", "-C", str(repo), "hash-object", "-t", "commit", "-w", "--stdin", input=rewritten).strip()


def command_bytes(repo: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(repo), *args])


def tampered_tree(root: Path, source: str, branch: str, variant: str) -> tuple[Path, Path, Path]:
    repo = root / "repo"; tamper = root / f"tamper-{variant}"; shutil.copytree(repo, tamper)
    cmap, rmap = root / "output/commit-map.txt", root / "output/ref-map.txt"
    mapping = read_commit_map(cmap)
    if source not in mapping:
        raise SystemExit("synthetic source is absent from commit map")
    mapped_source = mapping[source]
    ref = f"refs/heads/{branch}"
    if command(tamper, "rev-parse", ref).strip() != mapped_source:
        raise SystemExit("synthetic source is not the mapped branch tip")
    mapped_tree = command(tamper, "rev-parse", mapped_source + "^{tree}").strip()
    if command(tamper, "write-tree").strip() != mapped_tree:
        raise SystemExit("synthetic index is not the mapped source tree")
    target = tamper / "unrelated-a.txt"
    if variant == "bytes": target.write_bytes(b"tampered\n")
    elif variant == "mode": target.chmod(0o755)
    else:
        target.unlink(); target.symlink_to("unrelated-b.txt")
    command(tamper, "add", "-A")
    tree = command(tamper, "write-tree").strip()
    if tree == mapped_tree:
        raise SystemExit("synthetic tree tamper did not change the tree")
    new = rewrite_commit(tamper, mapped_source, tree=tree)
    if commit_parents(tamper, new) != commit_parents(tamper, mapped_source):
        raise SystemExit("synthetic tree tamper changed mapped parents")
    command(tamper, "update-ref", ref, new, mapped_source)
    if command(tamper, "rev-parse", ref).strip() != new:
        raise SystemExit("synthetic tree tamper branch readback mismatch")
    cm, rm = root / f"{variant}.commit-map", root / f"{variant}.ref-map"; shutil.copy2(cmap, cm); shutil.copy2(rmap, rm)
    replace_commit_map(cmap, cm, {source: new})
    replace_ref_map(rmap, rm, ref, new)
    return tamper, cm, rm


def tampered_parent_order(root: Path, merge_source: str, source: str, branch: str) -> tuple[Path, Path, Path]:
    repo = root / "repo"; tamper = root / "tamper-parent-order"; shutil.copytree(repo, tamper)
    preimage = root / "preimage.git"
    cmap, rmap = root / "output/commit-map.txt", root / "output/ref-map.txt"
    mapping = read_commit_map(cmap)
    if merge_source not in mapping or source not in mapping:
        raise SystemExit("synthetic merge or source is absent from commit map")
    mapped_merge, mapped_source = mapping[merge_source], mapping[source]
    parents = commit_parents(tamper, mapped_merge)
    if len(parents) != 2:
        raise SystemExit("synthetic merge unexpectedly lacks two ordered parents")
    descendants = command(preimage, "rev-list", "--reverse", "--ancestry-path", f"{merge_source}..{source}").splitlines()
    if not descendants or descendants[-1] != source:
        raise SystemExit("synthetic source descendant path is incomplete")
    replacements = {merge_source: rewrite_commit(tamper, mapped_merge, parents=[parents[1], parents[0]])}
    if command(tamper, "rev-parse", replacements[merge_source] + "^{tree}").strip() != command(tamper, "rev-parse", mapped_merge + "^{tree}").strip():
        raise SystemExit("synthetic parent-order reconstruction changed the merge tree")
    old_predecessor, new_predecessor = merge_source, replacements[merge_source]
    for descendant in descendants:
        if commit_parents(preimage, descendant) != [old_predecessor]:
            raise SystemExit("synthetic source descendants are not a linear chain")
        mapped_descendant = mapping.get(descendant)
        if mapped_descendant is None or commit_parents(tamper, mapped_descendant) != [mapping[old_predecessor]]:
            raise SystemExit("mapped synthetic source descendants are inconsistent")
        replacement = rewrite_commit(tamper, mapped_descendant, parents=[new_predecessor])
        if command(tamper, "rev-parse", replacement + "^{tree}").strip() != command(tamper, "rev-parse", mapped_descendant + "^{tree}").strip():
            raise SystemExit("synthetic parent-order reconstruction changed a tree")
        replacements[descendant] = replacement
        old_predecessor, new_predecessor = descendant, replacement
    if old_predecessor != source:
        raise SystemExit("synthetic parent-order reconstruction did not reach source")
    ref = f"refs/heads/{branch}"
    if command(tamper, "rev-parse", ref).strip() != mapped_source:
        raise SystemExit("synthetic source is not the mapped branch tip")
    command(tamper, "update-ref", ref, new_predecessor, mapped_source)
    if command(tamper, "rev-parse", ref).strip() != new_predecessor:
        raise SystemExit("synthetic parent-order branch readback mismatch")
    cm, rm = root / "parent-order.commit-map", root / "parent-order.ref-map"; shutil.copy2(cmap, cm); shutil.copy2(rmap, rm)
    replace_commit_map(cmap, cm, replacements)
    replace_ref_map(rmap, rm, ref, new_predecessor)
    return tamper, cm, rm


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")), encoding="utf-8")


def receipt_fixture(root: Path) -> list[tuple[str, str]]:
    root.mkdir()
    repository = "sednalabs/codex"
    run_id, workflow_id = 77, 88
    harness_sha, source_sha = "a" * 40, "b" * 40
    selected_map = {"refs/heads/main": source_sha, "refs/tags/v1": "4" * 40}
    selected_raw = json.dumps(selected_map, sort_keys=True, separators=(",", ":")).encode()
    selected_sha = hashlib.sha256(selected_raw).hexdigest()
    (root / "selected-ref-map.json").write_bytes(selected_raw)
    package = {
        "inner_sha256": "d" * 64,
        "inner_size": 1234,
        "selected_refs_sha256": selected_sha,
        "selected_refs_count": 2,
        "selected_refs_bytes": len(selected_raw),
        "rich_ref_manifest_sha256": "e" * 64,
    }
    restore = {"restore_test": "passed", "selected_refs_sha256": selected_sha, "restore_test_identity": "f" * 64}
    ciphertext = {
        "id": 901,
        "name": "recovery-snapshot-ciphertext",
        "size_in_bytes": 4321,
        "digest": "sha256:" + "1" * 64,
        "expired": False,
        "expires_at": "2030-01-02T00:00:00Z",
        "workflow_run": {"id": run_id},
    }
    for name, value in (("package.json", package), ("restore.json", restore), ("ciphertext.json", ciphertext)):
        write_json(root / name, value)
    write_json(root / "tool-versions.json", {"age": "fixture", "git": "fixture"})
    build = subprocess.run(
        [
            "python3", str(RECEIPT), "build", "--repository", repository, "--source-repository", repository,
            "--run-id", str(run_id), "--run-attempt", "1", "--workflow-id", str(workflow_id),
            "--workflow-path", ".github/workflows/recovery-snapshot.yml", "--workflow-ref", f"{repository}/.github/workflows/recovery-snapshot.yml@refs/heads/fixture",
            "--workflow-code-sha", harness_sha, "--source-sha", source_sha, "--package-info", str(root / "package.json"),
            "--restore-info", str(root / "restore.json"), "--artifact-json", str(root / "ciphertext.json"),
            "--metadata-manifest-sha256", "2" * 64, "--ciphertext-sha256", "3" * 64, "--ciphertext-size", "3210",
            "--recipient-sha256", "5" * 64, "--tool-versions-json", str(root / "tool-versions.json"),
            "--output", str(root / "recovery-snapshot-receipt.json"),
        ],
        text=True,
        capture_output=True,
    )
    require_driver_success("receipt_build", build)
    receipt_zip = root / "receipt.zip"
    with zipfile.ZipFile(receipt_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(root / "recovery-snapshot-receipt.json", "recovery-snapshot-receipt.json")
        archive.write(root / "selected-ref-map.json", "selected-ref-map.json")
    receipt_artifact = {
        "id": 902,
        "name": "recovery-snapshot-receipt",
        "size_in_bytes": receipt_zip.stat().st_size,
        "digest": "sha256:" + hashlib.sha256(receipt_zip.read_bytes()).hexdigest(),
        "expired": False,
        "expires_at": "2030-01-02T00:00:00Z",
        "workflow_run": {"id": run_id},
    }
    run_api = {
        "id": run_id,
        "run_attempt": 1,
        "status": "completed",
        "conclusion": "success",
        "event": "workflow_dispatch",
        "head_sha": harness_sha,
        "head_repository": {"full_name": repository},
        "workflow_id": workflow_id,
    }
    workflow_api = {"id": workflow_id, "path": ".github/workflows/recovery-snapshot.yml", "state": "active"}
    artifacts_api = {"artifacts": [ciphertext, receipt_artifact]}
    for name, value in (("run.json", run_api), ("workflow.json", workflow_api), ("artifacts.json", artifacts_api)):
        write_json(root / name, value)

    def verify_case(name: str, *, repository_value: str = repository, source_value: str = source_sha, run_value: int = run_id, run_data: dict | None = None, artifact_data: dict | None = None) -> subprocess.CompletedProcess[str]:
        write_json(root / f"{name}-run.json", run_api if run_data is None else run_data)
        write_json(root / f"{name}-artifacts.json", artifacts_api if artifact_data is None else artifact_data)
        return subprocess.run(
            [
                "python3", str(RECEIPT), "verify", "--repository", repository_value, "--run-id", str(run_value),
                "--harness-sha", harness_sha, "--source-sha", source_value, "--selected-refs-sha256", selected_sha,
                "--selected-refs-count", "2", "--selected-refs-bytes", str(len(selected_raw)), "--now", "2030-01-01T00:00:00Z",
                "--run-json", str(root / f"{name}-run.json"), "--workflow-json", str(root / "workflow.json"),
                "--artifacts-json", str(root / f"{name}-artifacts.json"), "--receipt-zip", str(receipt_zip),
                "--output", str(root / f"{name}-verified.json"), "--selected-refs-output", str(root / f"{name}-selected.json"),
            ],
            text=True,
            capture_output=True,
        )

    evidence: list[tuple[str, str]] = []
    positive = verify_case("receipt-positive")
    require_driver_success("receipt_verify", positive)
    if (root / "receipt-positive-selected.json").read_bytes() != selected_raw:
        raise SystemExit("verified receipt did not return the exact frozen selected-ref map")
    evidence.append(("backup_receipt_positive", hashlib.sha256((root / "receipt-positive-verified.json").read_bytes()).hexdigest()))
    cases = {
        "backup_receipt_source_mismatch": verify_case("source", source_value="9" * 40),
        "backup_receipt_repository_mismatch": verify_case("repository", repository_value="other/repository"),
        "backup_receipt_run_mismatch": verify_case("run", run_value=78),
    }
    artifact_mismatch = json.loads(json.dumps(artifacts_api)); artifact_mismatch["artifacts"][0]["id"] = 999
    cases["backup_receipt_artifact_mismatch"] = verify_case("artifact", artifact_data=artifact_mismatch)
    digest_mismatch = json.loads(json.dumps(artifacts_api)); digest_mismatch["artifacts"][1]["digest"] = "sha256:" + "0" * 64
    cases["backup_receipt_digest_mismatch"] = verify_case("digest", artifact_data=digest_mismatch)
    expiry_mismatch = json.loads(json.dumps(artifacts_api)); expiry_mismatch["artifacts"][0]["expires_at"] = "2029-12-31T00:00:00Z"
    cases["backup_receipt_expiry_mismatch"] = verify_case("expiry", artifact_data=expiry_mismatch)
    for name, result in cases.items():
        evidence.append((name, expect_failure(name, result)))
    return evidence


def publication_pipeline_fixture(root: Path, remote: Path, source_sha: str, rewritten: Path,
                                 original_to_isolated: Path, policy: Path, proof_output: Path) -> str:
    """Run the production preparation and publisher CLI against a disposable bare remote."""
    selected = {
        name: object_id
        for name, object_id in (
            line.split() for line in command(remote, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads", "refs/tags").splitlines()
        )
    }
    isolated = json.loads(original_to_isolated.read_text(encoding="utf-8"))
    output = {name: command(rewritten, "rev-parse", isolated[name]).strip() for name in selected}
    approved = root / "approved-proof"; approved.mkdir()
    for name in publication_module.PROOF_FILES:
        shutil.copy2(proof_output / name, approved / name)
    shutil.copy2(original_to_isolated, approved / "original-to-isolated-ref-map.json")
    policy_sha256 = hashlib.sha256(policy.read_bytes()).hexdigest()
    (approved / "policy.digest").write_text(f"{policy_sha256}  scripts/history-rewrite/policy.json\n", encoding="utf-8")
    write_json(approved / "binding.txt", {"fixture": True})
    write_json(approved / "verified-backup.json", {"fixture": True})
    proof_digests = {}
    for name in publication_module.ARTIFACT_FILES:
        proof_digests[name] = hashlib.sha256((approved / name).read_bytes()).hexdigest()
    from publication_fixtures import MockApi, base_manifest, observer_fixture_documents
    fixture_manifest = base_manifest()
    controls = fixture_manifest["controls"]
    protection = controls["protection_snapshot"]
    protection_api = MockApi({}, protection=protection)
    branch_document = protection_api.post_graphql(publication_module.BRANCH_PROTECTION_QUERY, {})
    manifest = {
        "schema": "history-rewrite-publication-v1",
        "repository": publication_module.REPOSITORY,
        "harness_sha": "5" * 40,
        "harness_tree": "6" * 40,
        "source_sha": source_sha,
        "tag_signature_ack": True,
        "selected_refs": selected,
        "output_refs": output,
        "selected_refs_sha256": publication_module.digest(selected),
        "output_refs_sha256": publication_module.digest(output),
        "policy": {"path": "scripts/history-rewrite/policy.json", "sha256": policy_sha256},
        "proof_digests": proof_digests,
        "proof_digests_sha256": publication_module.digest(proof_digests),
        "backup": {"run_id": 41, "receipt_artifact_id": 410, "receipt_artifact_api_digest": "8" * 64},
        "proof": {"run_id": 42, "artifact_id": 420, "artifact_api_digest": "9" * 64},
        "controls": controls,
    }
    manifest_path = root / "manifest.json"; write_json(manifest_path, manifest)
    control_plan = {
        "schema": "history-rewrite-control-plan-v1",
        "repository": publication_module.REPOSITORY,
        "manifest_sha256": publication_module.digest(manifest),
        "suppression": [
            {"id": 231747419, "path": ".github/workflows/rust-release.yml", "state": "active"},
            {"id": 250252266, "path": ".github/workflows/sedna-release.yml", "state": "active"},
        ],
        "writer_workflows": publication_module.WRITER_WORKFLOWS,
        "mirror": {"id": publication_module.MIRROR_WORKFLOW_ID, "path": ".github/workflows/sedna-sync-upstream.yml", "state": "disabled_manually"},
        "protection_snapshot_sha256": publication_module.digest(protection),
    }
    control_path = root / "control-plan.json"; write_json(control_path, control_plan)
    preflight_path = root / "preflight.json"
    write_json(preflight_path, {
        "schema": "history-rewrite-publication-preflight-v1", "repository": publication_module.REPOSITORY,
        "manifest_sha256": publication_module.digest(manifest), "harness_sha": manifest["harness_sha"],
        "harness_tree": manifest["harness_tree"], "selected_refs_sha256": manifest["selected_refs_sha256"],
        "output_refs_sha256": manifest["output_refs_sha256"], "backup_run_id": manifest["backup"]["run_id"],
        "proof_run_id": manifest["proof"]["run_id"],
        "approval": {"id": publication_module.REVIEWER_ID, "login": publication_module.REVIEWER_LOGIN},
        "writer_check": {"schema": "history-rewrite-writer-check-v1", "active": [], "status": "drained-at-single-read"},
        "protection_snapshot_sha256": manifest["controls"]["protection_snapshot_sha256"],
        "status": "verified-before-publisher-token",
    })
    fixture_api = root / "fixture-api.json"
    workflows = {
        str(workflow_id): {"id": workflow_id, "path": path, "state": "disabled_manually"}
        for path, workflow_id in publication_module.WRITER_WORKFLOWS.items()
    }
    workflows[str(publication_module.MIRROR_WORKFLOW_ID)] = {
        "id": publication_module.MIRROR_WORKFLOW_ID, "path": ".github/workflows/sedna-sync-upstream.yml", "state": "disabled_manually",
    }
    write_json(fixture_api, {
        **observer_fixture_documents(),
        "installation": {"app_id": publication_module.PUBLISHER_APP_ID, "app_slug": "fixture-publisher"},
        "app": {"id": publication_module.PUBLISHER_APP_ID, "node_id": publication_module.PUBLISHER_APP_NODE_ID},
        "workflows": workflows,
        "writer_runs": {str(workflow_id): {"total_count": 0, "workflow_runs": []} for workflow_id in publication_module.WRITER_WORKFLOWS.values()},
        "branch_protection_document": branch_document,
        "rulesets": [{"id": publication_module.QUEUE_ONLY_RULESET_ID}],
        "ruleset_documents": {str(publication_module.QUEUE_ONLY_RULESET_ID): protection_api.get(
            f"/repos/{publication_module.REPOSITORY}/rulesets/{publication_module.QUEUE_ONLY_RULESET_ID}")},
    })
    work = root / "candidate"
    environment = dict(os.environ); environment["HISTORY_REWRITE_PUBLICATION_FIXTURE"] = "1"
    prepared = subprocess.run([
        "python3", str(PUBLICATION), "prepare", str(manifest_path), "--remote-url", str(remote),
        "--frozen-sha", manifest["harness_sha"], "--frozen-tree", manifest["harness_tree"],
        "--manifest-sha256", publication_module.digest(manifest), "--preflight", str(preflight_path),
        "--work-root", str(work), "--proof-dir", str(approved), "--control-plan", str(control_path),
        "--intent", str(root / "intent.json"), "--fixture-policy", str(policy),
    ], text=True, capture_output=True, env=environment)
    require_driver_success("production_publication_prepare", prepared)
    published = subprocess.run([
        "python3", str(PUBLICATION), "publish", str(manifest_path), "--repo", str(work / "repo.git"),
        "--frozen-sha", manifest["harness_sha"], "--frozen-tree", manifest["harness_tree"],
        "--manifest-sha256", publication_module.digest(manifest), "--preflight", str(preflight_path),
        "--control-plan", str(control_path), "--intent", str(root / "intent.json"), "--current-run-id", "99",
        "--fixture-api", str(fixture_api),
        "--remote-url", str(remote), "--receipt", str(root / "publication-receipt.json"),
    ], text=True, capture_output=True, env=environment)
    require_driver_success("production_publication_push", published)
    if publication_module.advertised_refs(str(remote)) != output:
        raise SystemExit("production publication CLI did not produce the approved complete remote map")
    receipt = json.loads((root / "publication-receipt.json").read_text(encoding="utf-8"))
    if receipt.get("outcome") != "success" or receipt.get("after_refs") != output:
        raise SystemExit("production publication receipt does not bind the exact disposable after-map")
    return hashlib.sha256((root / "publication-receipt.json").read_bytes()).hexdigest()


def main() -> None:
    evidence: list[tuple[str, str]] = []
    publication = subprocess.run(["python3", str(Path(__file__).with_name("publication_fixtures.py"))], text=True, capture_output=True)
    require_driver_success("publication_contract", publication)
    evidence.append(("publication_contract", hashlib.sha256(publication.stdout.encode()).hexdigest()))
    with tempfile.TemporaryDirectory() as temporary:
        adapter_check = subprocess.run(["python3", str(ADAPTER), "check-policy", "--policy", str(ROOT / "policy.json")], text=True, capture_output=True)
        require_driver_success("review_packet_adapter_policy", adapter_check)
        evidence.append(("review_packet_non_executable_adapter", hashlib.sha256((ROOT / "policy.json").read_bytes()).hexdigest()))
        evidence.append(("raw_tag_signature_presence_parser", signature_parser_fixture()))
        root = Path(temporary) / "positive"; root.mkdir(); repo, source, branch, canonical_source, old_blob, new_blob, merge_source = make_repo(root); policy = root / "policy.json"; write_policy(policy, base_rules(old_blob, new_blob), canonical_source)
        remote_before = command(root / "synthetic-source.git", "show-ref")
        positive = invoke(repo, source, policy, root, relative_paths=True)
        require_driver_success("positive_apply_and_verify", positive)
        mapping = read_commit_map(root / "output/commit-map.txt")
        collapse_parent = command(root / "preimage.git", "rev-parse", source + "^").strip()
        if source not in mapping or collapse_parent not in mapping:
            raise SystemExit("synthetic empty-collapse commit was pruned from the exact commit map")
        if command(repo, "rev-parse", mapping[source] + "^").strip() != mapping[collapse_parent]:
            raise SystemExit("synthetic empty-collapse commit parent topology changed")
        if command(repo, "rev-parse", mapping[source] + "^{tree}").strip() != command(repo, "rev-parse", mapping[collapse_parent] + "^{tree}").strip():
            raise SystemExit("synthetic empty-collapse commit did not exercise a rewritten empty tree delta")
        evidence.append(("rewritten_empty_commit_preserved_one_to_one", hashlib.sha256((source + mapping[source]).encode()).hexdigest()))
        evidence.append(("classifier_path_rename_before_after", classifier_path_rename_fixture(root, repo, source, policy)))
        evidence.append(("classifier_exact_bc957_differential_nul_and_raw_paths", classifier_exact_differential_fixture(Path(temporary) / "classifier-differential")))
        bare_root = Path(temporary) / "positive-bare"; bare_root.mkdir()
        bare_repo, bare_source, _ = make_bare_mirror(bare_root, root / "preimage.git")
        bare_positive = invoke(bare_repo, bare_source, policy, bare_root)
        require_driver_success("positive_apply_and_verify_bare", bare_positive)
        if command(root / "synthetic-source.git", "show-ref") != remote_before:
            raise SystemExit("synthetic source remote changed during candidate execution")
        evidence.append(("positive_linear_merge_binary_nonutf8_repeated_target_unrelated_duplicate_tags", hashlib.sha256((root / "output/map-proof.tsv").read_bytes()).hexdigest()))
        evidence.append(("positive_bare_linear_merge_binary_nonutf8_repeated_target_unrelated_duplicate_tags", hashlib.sha256((bare_root / "output/map-proof.tsv").read_bytes()).hexdigest()))
        residual = json.loads((root / "output/residual-review.json").read_text())
        if residual.get("canonical", {}).get("count") != 1 or residual["canonical"].get("disposition") != "review-required":
            raise SystemExit("unmatched exact-path variants were not retained as review-required residuals")
        evidence.append(("guarded_canonical_replacement_residual", hashlib.sha256((root / "output/residual-review.json").read_bytes()).hexdigest()))
        isolated_root = Path(temporary) / "positive-isolated"; isolated_root.mkdir()
        isolated_repo, original_to_isolated = make_isolated_mirror(isolated_root, root / "preimage.git", source)
        isolated_positive = invoke(isolated_repo, source, policy, isolated_root, original_to_isolated_ref_map=original_to_isolated)
        require_driver_success("positive_isolated_main_input", isolated_positive)
        isolated_refs = command(isolated_repo, "for-each-ref", "--format=%(refname)").splitlines()
        if not isolated_refs or any(not name.startswith("refs/rewrites/") for name in isolated_refs):
            raise SystemExit("original main input escaped the isolated rewrite namespace")
        tag_summary = json.loads((isolated_root / "output/tag-proof-summary.json").read_text())
        if tag_summary != {
            "annotated_count": 2,
            "cryptographic_validity": "not-assessed",
            "lightweight_count": 1,
            "new_recognized_signature_count": 0,
            "old_recognized_signature_count": 1,
            "schema": "history-rewrite-tag-proof-v1",
            "tag_count": 3,
        }:
            raise SystemExit("isolated tag proof summary mismatch")
        with (isolated_root / "output/tag-proof.tsv").open(newline="") as stream:
            tag_rows = list(csv.DictReader(stream, delimiter="\t"))
        if len(tag_rows) != 3 or {row["kind"] for row in tag_rows} != {"annotated", "lightweight"}:
            raise SystemExit("isolated tag proof is not one row per original tag")
        if any(row["cryptographic_validity"] != "not-assessed" for row in tag_rows):
            raise SystemExit("isolated tag proof claimed cryptographic validity")
        evidence.append(("isolated_original_tag_join_and_signature_presence", hashlib.sha256((isolated_root / "output/tag-proof.tsv").read_bytes()).hexdigest()))
        evidence.append(("positive_original_main_isolated_under_rewrites", hashlib.sha256("\n".join(isolated_refs).encode()).hexdigest()))
        guarded_policy = json.loads(policy.read_text()); guarded_policy["rules"][0]["old_blob"] = "9" * 40; write_json(root / "guarded-policy.json", guarded_policy)
        guarded_root = root / "guarded-negative"; guarded_root.mkdir()
        evidence.append(("guarded_old_blob_mismatch", expect_failure("guarded-old-blob", invoke(root / "preimage.git", source, root / "guarded-policy.json", guarded_root))))
        expected_tree_diagnostics = {
            "bytes": "target bytes do not equal the approved transformation or non-target bytes changed",
            "mode": "path mode or type changed",
            "type": "path mode or type changed",
        }
        for variant in ("bytes", "mode", "type"):
            tamper, cm, rm = tampered_tree(root, source, branch, variant)
            result = invoke(tamper, source, policy, root, "verify", cm, rm)
            evidence.append((f"modified_non_target_{variant}", expect_failure(variant, result, expected_tree_diagnostics[variant])))
        tamper, cm, rm = tampered_parent_order(root, merge_source, source, branch)
        result = invoke(tamper, source, policy, root, "verify", cm, rm)
        evidence.append(("ordered_parent_topology", expect_failure("parent-order", result, "ordered parent topology mismatch")))
        for kind in ("domain", "zero", "refchange"):
            cm, rm = corrupt_map(root, kind, source, branch, repo)
            evidence.append((f"invalid_{kind}", expect_failure(kind, invoke(repo, source, policy, root, "verify", cm, rm))))
        for name, kwargs in (("targeted_shared_unrelated_path", {"shared_target_unrelated": True}), ("rename_collision", {"collision": True})):
            negative_root = Path(temporary) / name; negative_root.mkdir(); neg_repo, neg_source, _, neg_canonical, neg_old, neg_new, _ = make_repo(negative_root, **kwargs); neg_policy = negative_root / "policy.json"; write_policy(neg_policy, base_rules(neg_old, neg_new), neg_canonical)
            evidence.append((name, expect_failure(name, invoke(neg_repo, neg_source, neg_policy, negative_root))))
        evidence.extend(receipt_fixture(Path(temporary) / "receipt"))
        publication_root = Path(temporary) / "publication"; publication_root.mkdir()
        evidence.append((
            "production_cli_deterministic_regeneration_atomic_leases_full_readback_clean_fetch",
            publication_pipeline_fixture(publication_root, root / "synthetic-source.git", source, isolated_repo,
                                         original_to_isolated, policy, isolated_root / "output"),
        ))
    for name, digest in evidence:
        print(f"fixture_case={name} evidence_sha256={digest}")


if __name__ == "__main__":
    main()
