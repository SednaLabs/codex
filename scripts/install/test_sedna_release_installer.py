#!/usr/bin/env python3
"""Exercise the Sedna release installer with network and activation mocked.

The fixture uses a pre-trust-cutoff (2026-08-01) unsigned x86 release.  It
therefore covers legacy compatibility only; it is not modern signed-release
assurance.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest


INSTALLER = Path(__file__).parents[1] / "install_sedna_release_asset"


class SednaReleaseInstallerTest(unittest.TestCase):
    def test_automatic_candidate_rejections_preserve_current_and_request_order(
        self,
    ) -> None:
        current_version = "1.2.3-sedna.4"
        for candidate, expected_error in (
            ("v1.2.3-sedna.4", "is not newer than"),
            ("v1.2.3-sedna.3", "is not newer than"),
            ("not-a-sedna-release", "no valid published Sedna release"),
        ):
            with self.subTest(candidate=candidate):
                result, requests, current_target = run_installer(
                    candidate, current_version
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected_error, result.stderr)
                self.assertEqual(
                    requests,
                    [
                        "https://api.github.com/repos/sednalabs/codex/releases?per_page=100"
                    ],
                )
                self.assertEqual(current_target, "previous-managed-release")

    def test_automatic_newer_stable_candidate_activates_verified_legacy_release(
        self,
    ) -> None:
        result, requests, current_target = run_installer(
            "v1.2.4-sedna.1", "1.2.3-sedna.4", successful=True
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            requests,
            [
                "https://api.github.com/repos/sednalabs/codex/releases?per_page=100",
                "https://api.github.com/repos/sednalabs/codex/releases/tags/v1.2.4-sedna.1",
                "https://api.github.com/repos/sednalabs/codex/releases/assets/101",
                "https://api.github.com/repos/sednalabs/codex/releases/assets/102",
                "https://api.github.com/repos/sednalabs/codex/releases/assets/103",
            ],
        )
        self.assertTrue(current_target.endswith("/releases/v1.2.4-sedna.1"))
        self.assertIn("installed sednalabs/codex@v1.2.4-sedna.1", result.stdout)

    def test_manual_prerelease_explicit_opt_in_activates_verified_release(
        self,
    ) -> None:
        result, requests, current_target = run_installer(
            "v1.2.4-alpha.1-sedna.1",
            "1.2.3-sedna.4",
            allow_prerelease=True,
            use_latest=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            requests,
            [
                "https://api.github.com/repos/sednalabs/codex/releases/tags/v1.2.4-alpha.1-sedna.1",
                "https://api.github.com/repos/sednalabs/codex/releases/assets/101",
                "https://api.github.com/repos/sednalabs/codex/releases/assets/102",
                "https://api.github.com/repos/sednalabs/codex/releases/assets/103",
            ],
        )
        self.assertTrue(
            current_target.endswith("/releases/v1.2.4-alpha.1-sedna.1"),
            current_target,
        )
        self.assertIn("installed sednalabs/codex@v1.2.4-alpha.1-sedna.1", result.stdout)


def create_release(root: Path, release_tag: str) -> dict[str, Path]:
    release_version = release_tag.removeprefix("v")
    target = "x86_64-unknown-linux-gnu"
    archive_name = f"codex-sedna-{release_version}-{target}.tar.gz"
    release_root = root / "sedna-release"
    payload_root = release_root / "payload"
    payload_root.mkdir(parents=True)
    write_executable(
        payload_root / "codex",
        "#!/bin/sh\n"
        'case "${1:-}" in\n'
        f"  --version) printf 'codex {release_version}\\n' ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n",
    )
    write_executable(payload_root / "codex-responses-api-proxy", "#!/bin/sh\nexit 0\n")

    archive = release_root / archive_name
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(payload_root / "codex", arcname="codex")
        tar.add(
            payload_root / "codex-responses-api-proxy",
            arcname="codex-responses-api-proxy",
        )
    metadata = release_root / "RELEASE-METADATA.json"
    metadata.write_text(
        json.dumps(
            {
                "release_tag": release_tag,
                "release_version": release_version,
                "target": target,
                "repository": "sednalabs/codex",
            }
        ),
        encoding="utf-8",
    )
    checksum = release_root / "SHA256SUMS.txt"
    checksum.write_text(
        "\n".join(
            (
                f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive_name}",
                f"{hashlib.sha256(metadata.read_bytes()).hexdigest()}  {metadata.name}",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    release_json = release_root / "release.json"
    release_json.write_text(
        json.dumps(
            {
                "tag_name": release_tag,
                "draft": False,
                "prerelease": "-alpha." in release_tag,
                # Legacy fixture: before the trust-contract cutoff.  This
                # intentionally exercises compatibility, not signed assurance.
                "published_at": "2026-08-01T00:00:00Z",
                "assets": [
                    {"name": archive_name, "id": 101},
                    {"name": checksum.name, "id": 102},
                    {"name": metadata.name, "id": 103},
                ],
            }
        ),
        encoding="utf-8",
    )
    return {
        "archive": archive,
        "checksum": checksum,
        "metadata": metadata,
        "release_json": release_json,
    }


def write_executable(path: Path, contents: str) -> None:
    path.write_text(contents, encoding="utf-8")
    path.chmod(0o755)


def run_installer(
    selected_tag: str,
    current_version: str,
    *,
    allow_prerelease: bool = False,
    use_latest: bool = True,
    successful: bool = False,
) -> tuple[subprocess.CompletedProcess[str], list[str], str]:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        latest_json = root / "latest-release.json"
        latest_json.write_text(
            json.dumps(
                [
                    {
                        "tag_name": selected_tag,
                        "draft": False,
                        "prerelease": "-alpha." in selected_tag,
                    }
                ]
            ),
            encoding="utf-8",
        )
        request_log = root / "requests.log"
        release = (
            create_release(root, selected_tag) if successful or allow_prerelease else {}
        )
        fake_uname = bin_dir / "uname"
        fake_uname.write_text(
            "#!/usr/bin/env bash\n"
            'case "$1" in\n'
            "  -s) printf 'Linux\\n' ;;\n"
            "  -m) printf 'x86_64\\n' ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        fake_uname.chmod(0o755)
        fake_curl = bin_dir / "curl"
        fake_curl.write_text(
            "#!/usr/bin/env bash\nset -euo pipefail\n"
            'output=""; url=""\n'
            'while [[ "$#" -gt 0 ]]; do\n'
            '  if [[ "$1" == "--output" ]]; then output="$2"; shift 2; continue; fi\n'
            '  if [[ "$1" == http* ]]; then url="$1"; fi\n'
            "  shift\n"
            "done\n"
            'printf "%s\\n" "$url" >> "$SEDNA_TEST_REQUEST_LOG"\n'
            'if [[ "$url" == *"/releases?per_page=100" ]]; then cp "$SEDNA_TEST_LATEST_JSON" "$output"; exit 0; fi\n'
            'if [[ "$url" == */releases/tags/* && -n "${SEDNA_TEST_RELEASE_JSON:-}" ]]; then cp "$SEDNA_TEST_RELEASE_JSON" "$output"; exit 0; fi\n'
            'case "$url" in\n'
            '  */releases/assets/101) cp "$SEDNA_TEST_ARCHIVE" "$output" ;;\n'
            '  */releases/assets/102) cp "$SEDNA_TEST_CHECKSUM" "$output" ;;\n'
            '  */releases/assets/103) cp "$SEDNA_TEST_METADATA" "$output" ;;\n'
            "  *) exit 22 ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        fake_curl.chmod(0o755)
        home = root / "home"
        current = home / ".codex" / "packages" / "standalone" / "current"
        current.parent.mkdir(parents=True)
        previous = current.parent / "previous-managed-release"
        previous.mkdir()
        current.symlink_to(previous.name, target_is_directory=True)
        env = os.environ.copy()
        env.update(
            {
                "SEDNA_TEST_LATEST_JSON": str(latest_json),
                "SEDNA_TEST_REQUEST_LOG": str(request_log),
                "SEDNA_TEST_ARCHIVE": str(release.get("archive", "")),
                "SEDNA_TEST_CHECKSUM": str(release.get("checksum", "")),
                "SEDNA_TEST_METADATA": str(release.get("metadata", "")),
                "SEDNA_TEST_RELEASE_JSON": str(release.get("release_json", "")),
                "HOME": str(home),
                "PATH": f"{bin_dir}:/usr/bin:/bin",
            }
        )
        args = [
            "bash",
            str(INSTALLER),
            "--repository",
            "sednalabs/codex",
            "--release-tag",
            "latest" if use_latest else selected_tag,
        ]
        if allow_prerelease:
            args.append("--allow-prerelease")
        else:
            args.extend(["--require-newer-than", current_version])
        result = subprocess.run(
            args, capture_output=True, check=False, env=env, text=True
        )
        requests = (
            request_log.read_text(encoding="utf-8").splitlines()
            if request_log.exists()
            else []
        )
        return result, requests, os.readlink(current)


if __name__ == "__main__":
    unittest.main()
