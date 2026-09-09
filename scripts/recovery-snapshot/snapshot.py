#!/usr/bin/env python3
"""Build and restore-test a single, encrypted recovery snapshot package."""

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path


def run(*args: str, cwd: Path | None = None, stdout=None) -> str:
    process = subprocess.run(
        args,
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE if stdout is None else stdout,
        stderr=subprocess.PIPE,
    )
    return process.stdout if stdout is None else ""


def sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def fail(message: str) -> None:
    raise SystemExit(f"recovery snapshot: {message}")


def selected_ref_map(repo: Path, refs: list[str]) -> dict[str, str]:
    return {ref: run("git", "rev-parse", ref, cwd=repo).strip() for ref in sorted(refs)}


def package(ns: argparse.Namespace) -> None:
    repo = Path(ns.repo_dir).resolve()
    output = Path(ns.output).resolve()
    refs = [line.strip() for line in Path(ns.refs_file).read_text().splitlines() if line.strip()]
    if not refs or any(not (ref.startswith("refs/heads/") or ref.startswith("refs/tags/")) for ref in refs):
        fail("refs file must contain one or more explicit refs/heads/* or refs/tags/* names")
    if len(set(refs)) != len(refs):
        fail("duplicate refs are not allowed")
    if len(ns.candidate_sha) != 40 or any(character not in "0123456789abcdefABCDEF" for character in ns.candidate_sha):
        fail("candidate SHA must be a full 40-character hexadecimal object ID")
    run("git", "cat-file", "-e", f"{ns.candidate_sha}^{{commit}}", cwd=repo)
    with tempfile.TemporaryDirectory(prefix="recovery-snapshot-") as temporary:
        root = Path(temporary)
        bare = root / "repository.git"
        shutil.copytree(repo, bare)
        for ref in run("git", "for-each-ref", "--format=%(refname)", cwd=bare).splitlines():
            if ref and ref not in refs:
                run("git", "update-ref", "-d", ref, cwd=bare)
        identity_map = selected_ref_map(bare, refs)
        if identity_map.get("refs/heads/main") != ns.candidate_sha:
            fail("candidate SHA must equal the selected refs/heads/main identity")
        selected_raw = canonical_json(identity_map)
        (root / "selected-ref-map.json").write_bytes(selected_raw)
        if ns.selected_ref_map_output:
            selected_output = Path(ns.selected_ref_map_output).resolve()
            selected_output.parent.mkdir(parents=True, exist_ok=True)
            selected_output.write_bytes(selected_raw)
        manifest = []
        for ref, oid in identity_map.items():
            object_type = run("git", "cat-file", "-t", oid, cwd=bare).strip()
            peeled = run("git", "rev-parse", f"{ref}^{{}}", cwd=bare).strip() if ref.startswith("refs/tags/") else None
            peeled_type = run("git", "cat-file", "-t", peeled, cwd=bare).strip() if peeled else None
            item = {"ref": ref, "object": oid, "type": object_type, "peeled_object": peeled, "peeled_type": peeled_type}
            if ref.startswith("refs/tags/") and object_type == "tag":
                raw = run("git", "cat-file", "-p", oid, cwd=bare)
                headers, _, message = raw.partition("\n\n")
                fields = dict(line.split(" ", 1) for line in headers.splitlines() if " " in line)
                item.update(
                    {
                        "tagger": fields.get("tagger"),
                        "tag_message": message.rstrip("\n"),
                        "signature_status": "valid"
                        if subprocess.run(("git", "verify-tag", "--raw", oid), cwd=bare, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
                        else "unverified",
                    }
                )
            manifest.append(item)
        rich_manifest = root / "ref-manifest.json"
        rich_manifest.write_text(json.dumps({"candidate_sha": ns.candidate_sha, "refs": manifest}, sort_keys=True, indent=2) + "\n")
        metadata = root / "github-metadata"
        metadata.mkdir()
        source_metadata = Path(ns.metadata_dir)
        for name in ("repository.json", "pull-requests.json", "releases.json", "rulesets.json", "default-branch.json"):
            candidate = source_metadata / name
            if not candidate.is_file():
                fail(f"missing metadata file {name}")
            shutil.copyfile(candidate, metadata / name)
        (root / "exclusions.json").write_text(
            json.dumps(
                {
                    "excluded": [
                        "upstream ref names and metadata",
                        "GitHub-managed refs",
                        "private keys and credentials",
                        "plaintext package after encryption",
                        "public release, R2, force-push, ref-delete, protection or main mutations",
                    ],
                    "shared_ancestor_objects": "remain when reachable from selected refs; this package does not erase Git objects",
                },
                indent=2,
            )
            + "\n"
        )
        run("git", "bundle", "create", str(root / "refs.bundle"), "--all", cwd=bare)
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists():
            output.unlink()
        run(
            "tar",
            "--sort=name",
            "--mtime=@0",
            "--owner=0",
            "--group=0",
            "--numeric-owner",
            "-czf",
            str(output),
            "refs.bundle",
            "selected-ref-map.json",
            "ref-manifest.json",
            "github-metadata",
            "exclusions.json",
            cwd=root,
        )
        result = {
            "archive": str(output),
            "inner_sha256": sha256(output),
            "inner_size": output.stat().st_size,
            "selected_refs_sha256": hashlib.sha256(selected_raw).hexdigest(),
            "selected_refs_count": len(identity_map),
            "selected_refs_bytes": len(selected_raw),
            "rich_ref_manifest_sha256": sha256(rich_manifest),
        }
    print(json.dumps(result, sort_keys=True))


def restore(ns: argparse.Namespace) -> None:
    archive = Path(ns.archive).resolve()
    if not archive.is_file() or archive.stat().st_size == 0:
        fail("archive is missing or empty")
    with tempfile.TemporaryDirectory(prefix="recovery-restore-") as temporary:
        root = Path(temporary)
        run("tar", "-xzf", str(archive), "-C", str(root))
        selected_raw = (root / "selected-ref-map.json").read_bytes()
        selected = json.loads(selected_raw)
        if not isinstance(selected, dict) or not selected or selected_raw != canonical_json(selected):
            fail("selected ref map is not canonical non-empty UTF-8 JSON")
        if selected.get("refs/heads/main") != ns.candidate_sha:
            fail("candidate SHA does not match selected main")
        manifest_path = root / "ref-manifest.json"
        manifest = json.loads(manifest_path.read_text())
        if not isinstance(manifest.get("refs"), list) or not manifest["refs"] or manifest.get("candidate_sha") != ns.candidate_sha:
            fail("rich ref manifest is invalid")
        manifest_map = {item.get("ref"): item.get("object") for item in manifest["refs"] if isinstance(item, dict)}
        if manifest_map != selected or len(manifest_map) != len(manifest["refs"]):
            fail("rich ref manifest and selected ref map differ")
        bundle = root / "refs.bundle"
        run("git", "bundle", "verify", str(bundle))
        restored = root / "restored.git"
        run("git", "clone", "--bare", str(bundle), str(restored))
        run("git", "cat-file", "-e", f"{ns.candidate_sha}^{{commit}}", cwd=restored)
        run("git", "fsck", "--full", "--no-reflogs", cwd=restored)
        for item in manifest["refs"]:
            actual = run("git", "rev-parse", item["ref"], cwd=restored).strip()
            if actual != item["object"]:
                fail(f"restore ref mismatch for {item['ref']}")
            if item["type"] == "tag":
                peeled = run("git", "rev-parse", f"{item['ref']}^{{}}", cwd=restored).strip()
                peeled_type = run("git", "cat-file", "-t", peeled, cwd=restored).strip()
                if peeled != item["peeled_object"] or peeled_type != item["peeled_type"]:
                    fail(f"restore peeled tag mismatch for {item['ref']}")
        for name in (
            "github-metadata/repository.json",
            "github-metadata/pull-requests.json",
            "github-metadata/releases.json",
            "github-metadata/rulesets.json",
            "github-metadata/default-branch.json",
            "exclusions.json",
        ):
            json.loads((root / name).read_text())
        evidence = {
            "restore_test": "passed",
            "archive_sha256": sha256(archive),
            "source_sha": ns.candidate_sha,
            "selected_refs_sha256": hashlib.sha256(selected_raw).hexdigest(),
            "selected_refs_count": len(selected),
            "selected_refs_bytes": len(selected_raw),
            "rich_ref_manifest_sha256": sha256(manifest_path),
        }
        evidence["restore_test_identity"] = hashlib.sha256(canonical_json(evidence)).hexdigest()
    print(json.dumps(evidence, sort_keys=True))


parser = argparse.ArgumentParser()
subparsers = parser.add_subparsers(dest="command", required=True)
package_parser = subparsers.add_parser("package")
package_parser.add_argument("--repo-dir", required=True)
package_parser.add_argument("--refs-file", required=True)
package_parser.add_argument("--metadata-dir", required=True)
package_parser.add_argument("--candidate-sha", required=True)
package_parser.add_argument("--output", required=True)
package_parser.add_argument("--selected-ref-map-output")
package_parser.set_defaults(func=package)
restore_parser = subparsers.add_parser("restore-test")
restore_parser.add_argument("--archive", required=True)
restore_parser.add_argument("--candidate-sha", required=True)
restore_parser.set_defaults(func=restore)
arguments = parser.parse_args()
try:
    arguments.func(arguments)
except subprocess.CalledProcessError as error:
    fail(error.stderr.strip() or "command failed")
