#!/usr/bin/env bash
set -euo pipefail

# This lane is intentionally hosted-only. Keep Playwright and its browser
# cache outside the checkout so validation never changes the repository lockfile
# or dependency graph.
playwright_version="1.55.0"
runner_temp="${RUNNER_TEMP:?RUNNER_TEMP must be set}"
playwright_prefix="$(mktemp -d "$runner_temp/native-browser-playwright.XXXXXX")"
playwright_venv="$(mktemp -d "$runner_temp/native-browser-skill-venv.XXXXXX")"
native_browser_evidence_dir="$runner_temp/native-browser-evidence"
mkdir -p "$native_browser_evidence_dir"
npm install --prefix "$playwright_prefix" --ignore-scripts "playwright@${playwright_version}"
PLAYWRIGHT_BROWSERS_PATH="${RUNNER_TEMP}/native-browser-browsers" \
  "$playwright_prefix/node_modules/.bin/playwright" install chromium --with-deps

export NODE_PATH="$playwright_prefix/node_modules${NODE_PATH:+:${NODE_PATH}}"
export PLAYWRIGHT_BROWSERS_PATH="${RUNNER_TEMP}/native-browser-browsers"
export NATIVE_BROWSER_EVIDENCE_DIR="$native_browser_evidence_dir"

test_files=(codex-rs/browser-computer-use/src/*_test.mjs)
if ((${#test_files[@]} == 0)) || [[ ! -e "${test_files[0]}" ]]; then
  echo "native-browser-evidence: no browser test files found" >&2
  exit 1
fi
node --test "${test_files[@]}"

python3 -m venv "$playwright_venv"
"$playwright_venv/bin/pip" install --quiet 'PyYAML==6.0.2'
"$playwright_venv/bin/python" \
  codex-rs/skills/src/assets/samples/skill-creator/scripts/quick_validate.py \
  .codex/skills/use-native-browser
