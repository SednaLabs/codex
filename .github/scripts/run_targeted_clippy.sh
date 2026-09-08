#!/usr/bin/env bash

set -euo pipefail

package_lines="$(python3 -c '
import json
import os

packages = json.loads(os.environ["TARGETED_CLIPPY_PACKAGES"])
if not isinstance(packages, list) or any(
    not isinstance(package, str) or not package for package in packages
):
    raise ValueError("targeted Clippy packages must be a JSON array of non-empty strings")
print("\n".join(packages), end="")
')"
packages=()
if [[ -n "${package_lines}" ]]; then
  mapfile -t packages <<< "${package_lines}"
fi

package_args=()
for package in "${packages[@]}"; do
  package_args+=(--package "${package}")
done

cargo clippy \
  --target x86_64-unknown-linux-gnu \
  --all-features \
  --tests \
  --profile dev \
  --no-deps \
  "${package_args[@]}" \
  -- -D warnings
