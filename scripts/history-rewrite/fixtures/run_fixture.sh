#!/usr/bin/env bash
set -euo pipefail

# Hosted-only fixture generator. It intentionally emits no file contents: the
# caller receives identity/digest evidence and a zero exit status only.
fixture_dir=$(mktemp -d)
trap 'rm -rf "$fixture_dir"' EXIT
repo="$fixture_dir/repo"
git init -q "$repo"
git -C "$repo" config user.name fixture
git -C "$repo" config user.email fixture@example.invalid

printf 'provenance\n' > "$fixture_dir/target.txt"
printf 'shared\n' > "$fixture_dir/shared.txt"
cp "$fixture_dir/shared.txt" "$fixture_dir/scoped-target.txt"
cp "$fixture_dir/shared.txt" "$fixture_dir/scoped-unrelated.txt"
printf '\377\000binary\n' > "$fixture_dir/binary.dat"
git -C "$repo" add .
git -C "$repo" commit -qm linear
base=$(git -C "$repo" rev-parse HEAD)
git -C "$repo" tag lightweight
git -C "$repo" tag -a annotated -m annotated "$base"

git -C "$repo" checkout -qb side
printf 'side\n' > "$fixture_dir/side.txt"
git -C "$repo" add . && git -C "$repo" commit -qm side
side=$(git -C "$repo" rev-parse HEAD)
git -C "$repo" checkout -q master 2>/dev/null || git -C "$repo" checkout -q main
printf 'main\n' >> "$fixture_dir/target.txt"
git -C "$repo" add . && git -C "$repo" commit -qm main
git -C "$repo" merge --no-ff -m merge "$side" >/dev/null

git -C "$repo" show-ref --verify --quiet refs/tags/lightweight
git -C "$repo" show-ref --verify --quiet refs/tags/annotated
test "$(git -C "$repo" rev-list --min-parents=2 --count HEAD)" -eq 1
git -C "$repo" ls-tree -r -z HEAD | sha256sum | awk '{print $1}' > "$fixture_dir/tree.sha256"
git -C "$repo" for-each-ref --format='%(refname) %(objectname)' | sort | sha256sum | awk '{print $1}' > "$fixture_dir/refs.sha256"
printf 'fixture_tree_sha256=%s\nfixture_refs_sha256=%s\n' "$(cat "$fixture_dir/tree.sha256")" "$(cat "$fixture_dir/refs.sha256")"
