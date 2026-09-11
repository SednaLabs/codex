#!/usr/bin/env python3
"""Hosted candidate-only handoff tests; no real repository or provider writes."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import candidate_bundle as b
import publication as p
from publication_fixtures import expect_failure


def main() -> None:
    failures = []
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        repo = root / "source.git"
        b.init_repo(repo)
        tree = subprocess.check_output(["git", "-C", str(repo), "mktree"], input=b"").decode().strip()
        env = {**os.environ, "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
               "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid"}
        commit = p.git(repo, "commit-tree", tree, "-m", "candidate", env=env).strip()
        other = p.git(repo, "commit-tree", tree, "-m", "private preimage", env=env).strip()
        p.git(repo, "update-ref", "refs/backup/original", other)
        p.git(repo, "config", "fixture.private", "not-for-export")
        refs = {"refs/heads/main": commit}
        manifest = {"harness_sha": "1" * 40, "harness_tree": "2" * 40, "selected_refs": refs, "output_refs": refs,
                    "selected_refs_sha256": p.digest(refs), "output_refs_sha256": p.digest(refs), "proof_digests": {}}
        prepared = b.export_candidate(repo, manifest, root / "export", phase="qualification", run_id=21, attempt=1)
        assert {path.name for path in (root / "export").iterdir()} == b.FILES
        clean = root / "clean.git"
        b.import_bundle(root / "export/candidate.bundle", clean, refs)
        assert other not in p.git(clean, "cat-file", "--batch-all-objects", "--batch-check=%(objectname)").splitlines()
        assert "not-for-export" not in (clean / "config").read_text()
        p.git(clean, "update-ref", "refs/heads/unexpected", commit)
        failures.append(expect_failure("extra_ref", lambda: b.verify_objects(clean, refs)))
        p.git(clean, "update-ref", "-d", "refs/heads/unexpected")
        p.git(clean, "commit-tree", tree, "-m", "unreachable extra", env=env)
        failures.append(expect_failure("extra_unreachable_object", lambda: b.verify_objects(clean, refs)))
        failures.append(expect_failure("wrong_ref_map", lambda: b.import_bundle(root / "export/candidate.bundle", root / "wrong.git", {"refs/heads/main": other})))

        def make_zip(extra=False):
            archive = root / ("extra.zip" if extra else "candidate.zip")
            with zipfile.ZipFile(archive, "w") as target:
                for name in sorted(b.FILES): target.write(root / "export" / name, name)
                if extra: target.writestr("config", "private")
            return archive

        archive = make_zip()
        metadata = {"id": 99, "name": "history-rewrite-prepared-21", "expired": False, "size_in_bytes": archive.stat().st_size,
                    "digest": "sha256:" + p.file_digest(archive), "workflow_run": {"id": 21, "head_sha": "1" * 40, "repository_id": p.REPOSITORY_ID}}
        descriptor = b.descriptor(metadata, head_sha="1" * 40)
        class Api:
            def get(self, path):
                if "/artifacts/" in path: return metadata
                return {"head_sha": "1" * 40, "head_branch": p.PUBLICATION_BRANCH, "event": "workflow_dispatch",
                        "path": ".github/workflows/history-rewrite-candidate.yml", "repository": {"id": p.REPOSITORY_ID}}
        def download(identifier, destination, size): shutil.copyfile(archive, destination)
        def read(name, phase="qualification"):
            return b.read_candidate(descriptor, Api(), root / name, frozen_sha="1" * 40, frozen_tree="2" * 40,
                                    manifest_sha256=p.digest(manifest), phase=phase, download=download)
        assert read("imported") == prepared
        failures.append(expect_failure("qualification_not_publication", lambda: read("wrong-phase", "publication")))
        archive = make_zip(extra=True)
        metadata.update(size_in_bytes=archive.stat().st_size, digest="sha256:" + p.file_digest(archive))
        descriptor = b.descriptor(metadata, head_sha="1" * 40)
        failures.append(expect_failure("unexpected_config_member", lambda: read("extra")))
        metadata["workflow_run"]["head_sha"] = "3" * 40
        failures.append(expect_failure("foreign_artifact_head", lambda: read("foreign")))
    print(json.dumps({"candidate_only_bundle": "passed", "negative_cases": len(failures), "evidence_sha256": p.digest(failures)}, sort_keys=True))


if __name__ == "__main__":
    main()
