# GitHub App installation broker

`github_app_installation_broker.py` is a one-shot transport boundary for the
PR observer. It gives one selected repository a short-lived GitHub App
installation token while keeping the operator's personal token out of the
child process. Phase A is development-ready and read-only; it does not prove a
live installation or production commissioning.

## Inputs and scope

The broker requires an App ID, installation ID, installation account, one
`OWNER/REPOSITORY` selection, and an explicit permission object. The permitted
permission values are `read` for exactly these eight App and installation
grants: `metadata`, `contents`, `pull_requests`, `merge_queues`, `checks`,
`actions`, `statuses`, and `administration`. Unknown permissions and all write
or admin values are rejected. Before minting, the installation endpoint is
checked for the expected App/installation/account identity and this exact
grant ceiling. Each access token may request a subset; the request always
carries exactly one repository name and that requested subset. Branch
protection parity requires requesting `administration:read` in that subset.

The private key is loaded only from the fixed basename below the systemd
`CREDENTIALS_DIRECTORY` (normally supplied with `LoadCredential=`). Arbitrary
paths, environment-held key contents, stdin keys, worktree files, symlinks, and
group/world-writable sources are rejected. The key is passed to the inherited
file descriptor of `openssl dgst -sha256 -sign`; it is never an argument,
environment value, log field, or returned result. The App JWT uses GitHub's
RS256 header and short `iat`/`exp` claims and is never printed.

## One-shot execution

Use the non-minting fingerprint operation first and bind its output in the
caller. The fingerprint covers the selected repository, normalized
permissions, complete argv, executable identity and SHA-256 bytes for the
executable and any path-like script arguments. A changed command, script,
source mode, or selected permission set fails closed.

The `exec` operation validates that fingerprint, caches exactly one installation
token in process memory, strips ambient `GH_TOKEN`, `GITHUB_TOKEN`, and related
GitHub token variables, and injects only the freshly validated token as
`GH_TOKEN`. Child stdout/stderr are bounded and redacted. Results contain only
the non-secret App/installation/account/repository identity, expiry,
permissions, rate-limit headers, exit status, and redacted output. There is no
PAT fallback and no App-user-token fallback.

The cached token is reused until the positive near-expiry threshold, then
refreshed once. It is never persisted. Refresh retains the cached token until
revocation succeeds; an unproven revocation or invalid-issued-token cleanup
enters sticky terminal pending-cleanup state, rejects further mint/execute
operations, and allows `close()`/atexit to retry cleanup. After `close()` (even
after a failed close), the broker is terminal and rejects normal use.
Normal and exceptional exits call `DELETE /installation/token`; revocation
failure is reported as a non-secret status and must be treated as an
operational follow-up, not silently ignored.
HTTP requests use the GitHub API version header, HTTPS origin pinning,
redirect/cross-host rejection, bounded response bodies, strict JSON/type
checks, and finite timeouts.

Example (with generic credential naming):

```sh
python3 github_app_installation_broker.py fingerprint \
  --app-id 123 --app-slug sedna-codex-delivery-coordinator \
  --installation-id 456 --account example-org \
  --repo example-org/codex --permissions '{"metadata":"read","contents":"read"}' \
  -- /usr/local/bin/codex-pr-observer --once

python3 github_app_installation_broker.py exec \
  --app-id 123 --app-slug sedna-codex-delivery-coordinator \
  --installation-id 456 --account example-org \
  --repo example-org/codex --permissions '{"metadata":"read","contents":"read"}' \
  --fingerprint BOUND_FINGERPRINT -- /usr/local/bin/codex-pr-observer --once
```

The service unit should use `LoadCredential=` and let systemd set
`CREDENTIALS_DIRECTORY`; it should not place key contents in an environment
file or command line. Rotate the App key and installation permissions through
the provider's administrative process, then restart the one-shot unit. A
rollback restores the previously recorded broker script and exact command
fingerprint, followed by a fresh fingerprint and hosted validation. Revoke any
token left by an interrupted process before retrying.

## Validation and promotion

The secret-free test file uses only mocked HTTP responses and temporary test
fixtures. It covers permission reduction and selected-repository binding, JWT
claim construction without material exposure, cache reuse and near-expiry
refresh, ambient-token stripping, fingerprint and path safety, body and
redirect bounds, redaction, success/failure revocation, and the no-fallback
boundary. Hosted `codex.agent-workflow-sanity` runs the broker's compile and
test commands. Live installation/token proof, broader repositories, write
permissions, and production commissioning remain outside the Phase A cutline.

## Read-only PR observer mapping

The watcher supports an explicit `--installation-observer` mode for a caller
that has already supplied a short-lived installation token. This mode never
calls `GET /user` (installation tokens do not provide that endpoint), leaves
the authenticated login unbound, and retains conservative review filtering:
`OWNER`, `MEMBER`, and `COLLABORATOR` associations plus approved Codex bots are
actionable; `NONE` and other untrusted authors remain ignored. Ordinary mode
continues to resolve the authenticated login through `gh api user`. No mode
writes to GitHub, reruns checks, merges, resolves threads, or prints a token.

The read-only endpoint-to-permission contract is:

| Watcher read | Installation permission |
| --- | --- |
| PR metadata via `gh pr view` | `pull_requests:read` |
| Checks/statuses via `gh pr checks` | `checks:read` and `statuses:read` |
| Actions runs and jobs | `actions:read` |
| Issue comments (`GET /repos/{owner}/{repo}/issues/{pr}/comments`) | `pull_requests:read` (no `issues:read` grant) |
| PR review comments, reviews, and review threads | `pull_requests:read` |
| Broker-only branch-protection/ruleset proof | `administration:read` |
| Broker-only merge-queue proof | `merge_queues:read` |
| Broker validation metadata/contents reads | `metadata:read`, `contents:read` |

The installation-token endpoint is GitHub's [`POST /app/installations/{installation_id}/access_tokens`](https://docs.github.com/en/rest/apps/installations#create-an-installation-access-token), and the issue-comment permission behavior is documented in the [list issue comments endpoint](https://docs.github.com/en/rest/issues/comments#list-issue-comments-for-a-repository). These links are references only; this watcher does not mint tokens or invoke either endpoint.
