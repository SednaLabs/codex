## Sedna Release Policy

This fork keeps upstream version provenance visible while making published builds clearly distinct
from upstream OpenAI releases.

### Public topology

- Public repository owner: `sednalabs`
- Public default branch: `main`
- Exact-upstream mirror branch: `upstream-main`

### Release identity

- Release tags use `v<upstream-track>-sedna.<n>` when the upstream merge-base is exactly on the
  selected upstream tag
- Release tags use `v<upstream-track>-sedna.<n>+upstream.<distance>` when the upstream merge-base
  is ahead of the selected upstream tag
- Example: `v0.119.0-sedna.2`
- Offset example: `v0.126.0-alpha.5-sedna.1+upstream.1`
- `scripts/resolve_sedna_release_version` is the authoritative version resolver for official
  releases. Humans mark release intent; the resolver chooses and validates the tag.
- Sedna public tags stay human-readable and monotonic. Exact upstream provenance is recorded in
  release metadata instead of being overloaded into the public tag.
- Artifact names include `sedna` so they are not confused with upstream binaries
- Release builds embed `CODEX_RELEASE_VERSION` as the canonical SemVer and add a compact
  provenance label to `codex --version`
- Release artifacts include both `RELEASE-METADATA.txt` and `RELEASE-METADATA.json` with:
  `version_policy`, `release_channel`, `release_marker`, `upstream_track`,
  `upstream_base_commit`, `upstream_base_tag`, `upstream_base_tag_exact`,
  `upstream_distance_from_tag`, `upstream_position`, `downstream_commit`, `target_commit`, and
  the compact `build_provenance` / `version_display` strings
- Linux `x86_64` (`x86_64-unknown-linux-gnu`), Linux Arm64
  (`aarch64-unknown-linux-gnu`), and Intel macOS `x86_64` (`x86_64-apple-darwin`) are the officially
  supported Sedna release targets. Apple Silicon, Windows, and other upstream targets remain
  outside the current downstream release contract.

The upstream track is resolved from the target commit's merge-base with `origin/upstream-main`.
That merge-base is the upstream reference point for the release, even if `origin/upstream-main`
has advanced by the time the release runs. The resolver chooses the newest well-formed
`rust-v<semver>` upstream tag whose tag timestamp is at or before that merge-base commit, and
malformed double-prefixed upstream tags are ignored. If the merge-base is ahead of the selected
upstream tag, the release metadata records the commit distance instead of pretending the base was
an exact upstream tag.

Offset releases also include the distance in the public release version as SemVer build metadata.
The public tag remains anchored to the upstream track and Sedna ordinal, while the `+upstream.N`
suffix makes it visible that the upstream base is N commits above the upstream tag.

`version_display` and `build_provenance` include that same upstream position. Exact upstream-tag
builds use `rust-v<semver>@<upstream-sha>`, while builds whose upstream merge-base is above the
tag use `rust-v<semver>+<distance>@<upstream-sha>`, for example
`0.126.0-alpha.5-sedna.1+upstream.1 (up:rust-v0.126.0-alpha.5+1@4f1d5f00 down:82fafe27)`.

### Automatic update boundary

Automatic update discovery is enabled only for supported Linux `x86_64` and
`aarch64` installations. It selects a stable, strictly newer Sedna release;
the `--require-newer-than` floor is checked before release assets are fetched or
the `current` link can change. Equality and older candidates are rejected
without mutating the installed release.

Automatic discovery is a no-op for unsupported targets, prerelease candidates,
and macOS. A manual operator invocation may opt into a prerelease with
`--allow-prerelease`; Intel macOS preview verification is a separate explicit
`--macos-preview` mode. These manual opt-ins do not change the automatic
candidate boundary.

The current contract is covered by the installer lower-bound and candidate
tests (`scripts/install/test_sedna_release_lower_bound.py` and
`scripts/install/test_sedna_release_installer.py`), the TUI update-version and
prompt surfaces, `codex doctor` diagnostics, and the hosted
`sedna.update-installer-contract` validation lane. The tests use a legacy unsigned
fixture only to preserve compatibility; they are not modern release-trust
assurance.

The public installer form is explicit about its release source and candidate:

```bash
scripts/install_sedna_release_asset \
  --repository sednalabs/codex \
  --release-tag TAG
```

Manual prerelease verification adds `--allow-prerelease`. Intel macOS preview
verification requires both `--allow-prerelease` and `--macos-preview`; neither
flag changes automatic update discovery.

### GitHub Actions

Use the `sedna-release` workflow for fork-owned GitHub releases.

- Push to `main` with an exact commit trailer to request an automatic official release:
  - `Sedna-Release: stable`
  - `Sedna-Release: prerelease`
- `main` pushes first run a lightweight route job inside `sedna-release`; ordinary `main` pushes
  without a trailer skip before release metadata resolution or heavyweight publication.
- Release requests then resolve the exact tag, version, upstream position, channel, and target
  commit in a separate metadata job before any release build starts.
- Release publisher concurrency is keyed by the resolved release tag, not by `main`. Two different
  resolved release tags may build in parallel, while duplicate attempts for the same tag serialize
  and re-check that the GitHub Release still does not exist before spending build minutes.
- `Sedna-Release: stable` refuses upstream prerelease tracks such as `0.126.0-alpha.3`,
  publishes a full GitHub Release, and dispatches public asset verification for that exact tag.
- `Sedna-Release: prerelease` allows upstream prerelease tracks and publishes the GitHub Release as
  a prerelease. The release workflow dispatches asset verification with an explicit
  prerelease allowance for that exact tag.
- Release-tag pushes and deletions never initiate publication. The protected-`main` workflow resolves
  and creates the tag as an output after admission; a repeated protected-`main` request for an
  already-published tag/target pair is an idempotent skip. If the tag exists but points to a
  different commit, the workflow still fails to prevent accidental tag reuse.
- Manual `workflow_dispatch` accepts an optional `target_sha`, `channel`, and optional
  `release_tag`. If `release_tag` is supplied, it is an assertion checked against the resolver, not
  the source of truth.
- A manual `target_sha` must equal the exact commit at the workflow dispatch ref. This keeps the
  release binaries, Sigstore certificate workflow SHA, and GitHub provenance source/signer digests
  bound to the same immutable commit. Manual releases must be dispatched from protected `main`;
  arbitrary branch dispatches fail before any release build or publication.
- A synthetic/non-main ref presented to the route is classified as unsupported and cannot start
  build, signing, attestation, or publication work.
- Manual `workflow_dispatch` without `release_tag` requires the target commit to contain a valid
  `Sedna-Release:` trailer. Markerless manual releases must supply the expected tag explicitly.
- A supplied `release_tag` must match the upstream track computed from the target commit's
  merge-base. Supplying a tag from a newer upstream track fails instead of moving the release onto
  that newer track.
- Automatic trailer releases allocate Sedna ordinals from the first-parent `main` release-marker
  ledger for the resolved upstream track. This keeps back-to-back release commits deterministic
  even when their workflow runs resolve before either GitHub Release has been published.
- Existing release tags are immutable in normal release flow. Rerolls use the next trailing
  `sedna.<n>` value rather than clobbering published assets.

Current workflow characteristics:

- Native GitHub-hosted Linux `x86_64` and Arm64 release builds, with Intel macOS `x86_64` assets selected
  explicitly as `off`, `preview`, or `notarized`
- Release builds and GitHub Release publication are separate jobs: the build job keeps a read-only
  repository token while the small publication job owns the release environment and write-scoped
  publishing permissions.
- Authority-isolated Cargo dependency and `sccache` caches around the official release build.
  Release and arbitrary-ref preview namespaces are distinct, and executable `~/.cargo/bin`
  content is never restored from either cache.
- Keyless Sigstore signing for Linux binaries, SPDX 2.3 SBOMs, and GitHub build-provenance
  attestations for each Linux archive and SBOM; ad-hoc code-signing checks for Intel macOS
  previews; and an optional Developer ID signing and notarization path
- GitHub Release publication through a dedicated GitHub App installation token instead of the
  default workflow integration token
- GitHub Release assets named with the Sedna release identity
- Exact upstream/downstream provenance recorded in release metadata assets
- No dependency on upstream runner groups or upstream release tags

### Release environment governance

The `release` GitHub Actions environment admits protected branches only and requires explicit
environment approval before publication. This environment gate is authoritative; workflow-side
ref and release-route checks remain defense-in-depth and do not replace approval.

Release publication requires a dedicated GitHub App installed on this repository only. Configure
the app with repository permissions for `Contents: Read and write` and `Actions: Read and write`,
then store:

- repository or organization variable `SEDNA_RELEASE_PUBLISHER_APP_CLIENT_ID`
- repository or organization secret `SEDNA_RELEASE_PUBLISHER_APP_PRIVATE_KEY`

The workflow checks that these are configured before starting the release build, then mints the
short-lived installation token only after the assets are staged so the publication token is fresh
for GitHub Release creation and verifier dispatch.

Intel macOS publication has three explicit modes:

- `off` is the default, including automatic tag and release-marker events. It publishes no macOS
  asset and never reads the `codesigning` environment.
- `preview` is allowed only for prereleases. It publishes an Intel x64 tarball whose filename and
  metadata identify it as an unnotarized preview. The binaries are ad-hoc signed, architecture and
  signature checked, checksummed, and executed on an Intel macOS runner. They are not Developer ID
  signed, may be blocked by Gatekeeper, and are not an official supported macOS distribution.
- `notarized` is fail-closed. It publishes Intel x64 binaries and a DMG only after Developer ID
  signing, Apple notarization, stapling, and a final Intel-runner verification pass.

Apple provides Developer ID and notarization through the paid Apple Developer Program. This is a
product prerequisite for the `notarized` mode, not merely a CI configuration detail. The free
`preview` mode cannot provide the same Gatekeeper experience. See Apple's
[membership comparison](https://developer.apple.com/support/compare-memberships/) and
[notarization requirements](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution).

To enable `notarized`, configure a `codesigning` GitHub Actions environment with the Azure Key
Vault PKCS#11 and App Store Connect secrets expected by the macOS signing actions:

- `AKV_CODESIGN_RCODESIGN_BLOB_URI`
- `AKV_CODESIGN_RCODESIGN_SHA256`
- `AKV_CODESIGN_PKCS11_LIBRARY_BLOB_URI`
- `AKV_CODESIGN_PKCS11_LIBRARY_SHA256`
- `AKV_CODESIGN_AZURE_CLIENT_ID`
- `AKV_CODESIGN_TENANT`
- `AKV_CODESIGN_SUBSCRIPTION`
- `AKV_CODESIGN_KEY_VAULT_NAME`
- `AKV_CODESIGN_KEY_NAME`
- optional `AKV_CODESIGN_KEY_VERSION`
- optional `AKV_CODESIGN_CERTIFICATE_SHA256`
- `APPLE_NOTARIZATION_KEY_P8`
- `APPLE_NOTARIZATION_KEY_ID`
- `APPLE_NOTARIZATION_ISSUER_ID`

The signing job reports the exact missing secret names without exposing values. Ad-hoc assets can
never satisfy the notarized release gate.

For a zero-credential Intel preview, dispatch a prerelease explicitly:

```bash
python3 .github/scripts/dispatch_sedna_release.py \
  --channel prerelease \
  --macos-release-mode preview
```

Omit `--macos-release-mode` to publish without macOS assets. Use `notarized` only after the
`codesigning` environment has been provisioned.

The resolver writes `version_policy=sedna-upstream-track-v2` into release metadata so future policy
changes can be detected explicitly instead of inferred from tag shape alone.

### Branch artifacts and heavy validation

- `validation-lab` is the default remote-first surface for scratch refs, integration refs,
  orphan-branch experiments, and targeted heavy validation that should not pollute ordinary PR
  status surfaces
- `validation-lab` `profile=targeted` with `lane_set=release` is the preferred early Linux
  release-build smoke path when the question is dependency or lockfile drift under
  `cargo build --locked`
- the concrete preflight lane is `sedna.release-linux-smoke`; it also runs as a runtime smoke gate
  for core-heavy PR validation so release-mode compile breaks are caught before an official
  release dispatch is the first full release build
- keep that path separate from official release publication so operators can prove a ref is
  releasable without mutating GitHub Releases
- `sedna-release` reuses only its protected release cache namespace and still performs the
  authoritative build, signing, metadata, checksum, and publication steps itself; arbitrary-ref
  preview or smoke runs cannot populate that namespace
- `sedna-branch-build` produces disposable preview binaries only when manually
  dispatched. Its default remains Linux `x86_64`; `platform=linux-aarch64` uses a native
  GitHub-hosted Arm64 runner, while `platform=macos` produces
  one ad hoc signed, non-notarized Intel x64 artifact for preview use without
  publishing a GitHub Release. Cargo dependency and `sccache` reuse stays in a preview-only
  namespace and excludes executable tool paths. Linux preview and validation-artifact builds
  isolate workflow-owned helper/cache paths from source provenance, require a clean checkout before
  compilation, create repository-root `dist/` output only after compilation, and reject any binary
  whose displayed git provenance contains `-dirty`.
- `sedna-heavy-tests` runs expensive remote validation without using the local development machine as the build factory
- branch artifacts retain for 3 days and are never updater candidates
- only `sedna-release` is allowed to publish official GitHub Releases
- The initial Sedna release lane publishes direct GitHub release binaries. The legacy npm-style
  installer packages and artifact-runtime assets remain upstream-hosted until Sedna reaches asset
  parity for those families.

### Local versus CI builds

- Local builds remain useful for development, targeted tests, and smoke checks
- `validation-lab` is the default offload path for seam-level remote validation and experimental
  sweeps
- When the question is "will the Linux release binary set still build with `--locked`?", prefer
  `validation-lab` `profile=targeted` with `lane_set=release` before escalating to artifact mode
  or `sedna-release`
- When the question is "publish an official release on GitHub Releases," skip `validation-lab`
  publication entirely and use `sedna-release`
- Preview builds are intentionally opt-in rather than every-commit defaults
- GitHub-hosted branch builds remain useful when the actual question is preview artifact
  buildability
- GitHub-hosted release builds are the authoritative public release artifacts
- GitHub prereleases are intentionally opt-in through the `Sedna-Release: prerelease` marker or
  manual prerelease channel and are not the updater's default candidate path
- Local non-release builds may still show the workspace placeholder version when
  `CODEX_RELEASE_VERSION` is not set; published releases should come from CI so the embedded release
  metadata is consistent

### Release install verification workflow

`sedna-release-install` verifies already published Sedna release assets on a GitHub-hosted
runner. It intentionally does not perform host-local installation from the public Actions surface.

- Official release verification is explicitly dispatched by `sedna-release` after publishing a
  non-draft GitHub Release. This avoids relying on implicit follow-on workflow events from the
  release publisher token.
- Manual `workflow_dispatch` runs require `dry_run=true`
- Prerelease installs require `allow_prerelease=true` on `workflow_dispatch`
- New releases require native Linux Arm64 verification by default. Set
  `require_linux_arm64=false` only when manually verifying a historical x86-only release.
  That explicit compatibility mode uses the historical checksum, metadata, archive-safety, and
  executable checks; it cannot apply the newer exact-source signature and attestation contract to
  releases whose metadata predates that contract.
- The verifier checks all supported targets on native x86-64 Linux, Arm64 Linux, and Intel macOS
  runners, including tag shape, target-bound release metadata, checksums, safe archive membership,
  and executable payloads. Linux verification also checks keyless Sigstore identity, native ELF
  architecture, SPDX structure, and GitHub build attestations for the archive and SBOM.
- Hardened Linux verification passes `--verify-signatures --verify-attestation`. The verifier pins
  attestation acceptance to this repository's `sedna-release.yml` signer workflow and dispatches
  the verifier from the exact published release tag so the installer contract cannot drift from
  the release source. It rejects a manual dispatch unless the selected workflow ref is exactly
  `refs/tags/<release_tag>`; the public verifier has no cross-revision maintenance mode.
- Current x86-64 releases fail closed into signature, provenance, hosted-runner, and exact-source
  verification, including clients that fetch the current installer without new flags and callers
  that explicitly supply only one positive verification flag.
  When compatible host tools are absent, the installer uses checksum-pinned Cosign and GitHub CLI
  binaries in its temporary verification directory; it does not install tools globally. GitHub
  provenance checks use the downloaded local bundle, so updater hosts do not need GitHub
  authentication. The streaming checksum verifier supports Python 3.10. A pure Python ELF parser is
  the fallback when `file` or `readelf` is absent.
- Immutable x86-only releases published before the 2026-08-16 trust-contract cutoff and carrying
  no Arm64 or provenance assets automatically retain the historical path for already-shipped
  updater calls. `--allow-historical-x86` is the explicit operator form of that same bounded mode
  and cannot downgrade a current release. Arm64 never has a downgrade path.
- A non-dry install may reuse an existing release directory only when both executables, metadata,
  and checksum manifest are byte-for-byte identical to the freshly verified staged payload.
- Host-local installs should be performed by external deployment automation outside the public
  Actions log surface
- Drafts are not installed, and prereleases are refused unless an explicit dispatch allows them

### Operator deployment boundary

The public release architecture ends at GitHub Release publication and
GitHub-hosted asset verification. The repository tracks the release resolver,
metadata contract, signing/checksum process, artifact names, and dry-run asset
verification because those are public product behavior.

Operator deployment policy is downstream-owned and external to this public
Actions surface. Do not add hostnames, tunnel names, self-hosted runner labels,
service-unit paths, installation directories, or production-machine routing to
tracked workflows or public release notes. If a public workflow needs to refer
to that handoff, use repository-neutral wording such as external deployment
automation.

`.github/scripts/check_workflow_policy.py` is the checked-in guardrail for the
highest-risk workflow drift: it rejects public self-hosted runners, direct
release publication without the release environment, and public release-install
paths that are not dry-run asset verification.
