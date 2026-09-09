#!/usr/bin/env python3
"""Execute the installer lower-bound gate with network and activation mocked."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "install_sedna_release_asset"


def run_case(
    fake_curl: Path,
    home: Path,
    candidate: str,
    bound: str | None,
    *extra: str,
) -> tuple[subprocess.CompletedProcess[str], bool]:
    marker = home / "curl-called"
    marker.unlink(missing_ok=True)
    command = [
        "bash",
        str(SCRIPT),
        "--repository",
        "sednalabs/codex",
        "--release-tag",
        candidate,
        *extra,
    ]
    if bound is not None:
        command.extend(["--require-newer-than", bound])
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["PATH"] = f"{fake_curl.parent}{os.pathsep}{env['PATH']}"
    result = subprocess.run(command, env=env, capture_output=True, text=True)
    return result, marker.exists()


def assert_rejected(
    fake_curl: Path,
    home: Path,
    candidate: str,
    bound: str | None,
    message: str,
    *extra: str,
) -> None:
    result, curl_called = run_case(fake_curl, home, candidate, bound, *extra)
    assert result.returncode != 0, result.stdout + result.stderr
    assert message in result.stderr, result.stderr
    assert not curl_called, "lower-bound rejection occurred after network access"


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="sedna-lower-bound-test-") as directory:
        root = Path(directory)
        fake_bin = root / "bin"
        fake_bin.mkdir()
        fake_curl = fake_bin / "curl"
        fake_curl.write_text(
            '#!/bin/sh\ntouch "$HOME/curl-called"\nexit 97\n',
            encoding="utf-8",
        )
        fake_curl.chmod(0o755)

        assert_rejected(
            fake_curl,
            root,
            "v1.2.3-sedna.3",
            "1.2.3-sedna.3",
            "is not newer than",
        )
        assert_rejected(
            fake_curl,
            root,
            "v1.2.3-sedna.2",
            "1.2.3-sedna.3",
            "is not newer than",
        )
        result, curl_called = run_case(
            fake_curl, root, "v1.2.3-sedna.4", "1.2.3-sedna.3"
        )
        assert result.returncode == 97, result.stdout + result.stderr
        assert curl_called, "newer candidate did not reach mocked release fetch"

        assert_rejected(
            fake_curl,
            root,
            "v1.2.3-sedna.x",
            "1.2.3-sedna.3",
            "release tag must look like",
        )
        assert_rejected(
            fake_curl,
            root,
            "v1.2.3-sedna.4",
            "not-a-sedna-release",
            "strict Sedna release version",
        )
        assert_rejected(
            fake_curl,
            root,
            "v1.2.3-alpha.1-sedna.4",
            "1.2.3-sedna.3",
            "is not newer than",
        )
        result, curl_called = run_case(
            fake_curl,
            root,
            "v1.2.4-alpha.10-sedna.1",
            "1.2.4-alpha.2-sedna.99",
            "--allow-prerelease",
        )
        assert result.returncode == 97, result.stdout + result.stderr
        assert curl_called, "numeric prerelease update did not reach mocked release fetch"


if __name__ == "__main__":
    main()
