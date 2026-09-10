# codex-app-server-daemon

> `codex-app-server-daemon` is experimental and its lifecycle contract may
> change while the remote-management flow is still being developed.

`codex-app-server-daemon` backs the machine-readable `codex app-server`
lifecycle commands used by remote clients such as the desktop and mobile apps.
It is intended for Codex instances launched over SSH, including fresh developer
machines that should expose app-server with `remote_control` enabled.

## Platform support

The current daemon implementation is Unix-only. It uses pidfile-backed
daemonization plus Unix process and file-locking primitives, and does not yet
support Windows lifecycle management.

## Commands

```sh
codex app-server daemon start
codex app-server daemon restart
codex app-server daemon enable-remote-control
codex app-server daemon disable-remote-control
codex app-server daemon stop
codex app-server daemon version
codex app-server daemon bootstrap --remote-control
```

On success, every command writes exactly one JSON object to stdout. Consumers
should parse that JSON rather than relying on human-readable text. Lifecycle
responses report the resolved backend, socket path, local CLI version, and
running app-server version when applicable.

## Bootstrap flow

For a new remote machine running a stable Sedna Linux release, choose and copy
the exact release tag from the [Sedna releases page](https://github.com/sednalabs/codex/releases),
then install that fork-owned release before bootstrapping:

```sh
release_tag='v0.124.0-sedna.2' # replace with the exact selected release-page tag
curl -fsSL https://raw.githubusercontent.com/sednalabs/codex/main/scripts/install_sedna_release_asset \
  | CODEX_NON_INTERACTIVE=1 bash -s -- \
      --repository sednalabs/codex \
      --release-tag "$release_tag"
$HOME/.codex/packages/standalone/current/codex app-server daemon bootstrap --remote-control
```

`bootstrap` requires the standalone managed install. It records the daemon
settings under `CODEX_HOME/app-server-daemon/`, starts app-server as a
pidfile-backed detached process, and launches a detached updater loop only
when the release is eligible for the automatic Sedna channel. The persisted
bootstrap marker means app-server has completed initial setup; it does not
replace updater reconciliation.

## Installation and update cases

The daemon always resolves `current/codex` to a canonical executable inside
`CODEX_HOME/packages/standalone/releases/` before it launches app-server or an
updater. Automatic updates are available only for stable Sedna releases on
Linux `x86_64` and Linux `aarch64` that were installed through the fork-owned
standalone release installer. The daemon verifies the resolved release's
`RELEASE-METADATA.json` and executable against the installer-written
`INSTALLED-SHA256SUMS.txt` manifest, then validates its repository, version,
and target. The binary that
invokes `bootstrap` does not grant update authority to a different managed
release.

| Situation | What starts | Does this daemon fetch new binaries? | Does a running app-server eventually move to a newer binary on its own? |
| --- | --- | --- | --- |
| A managed standalone release is installed, but only `start` is used | `start` resolves the canonical executable from `CODEX_HOME/packages/standalone/releases/` | No | No. The managed release is used when starting or restarting, but no updater is installed. |
| An eligible stable Sedna Linux release is installed, then `bootstrap` is used | The pidfile backend uses the canonical executable selected from `CODEX_HOME/packages/standalone/releases/` | Yes. Bootstrap launches a detached updater loop that resolves the fork release candidate hourly. The installer accepts it only when its strict Sedna version is stable and newer than the running release. | Yes, while that updater process is alive and app-server is already running. After a successful eligible update, the updater revalidates the final managed release, restarts app-server with its canonical executable, and only then replaces its own process image. |
| Prerelease, macOS, package-manager, or unsupported installation | Lifecycle commands use a canonical managed release if one is present | No automatic action. Select and install a compatible release manually. | No. The daemon does not select or activate an automatic release for these installation classes. |

### Standalone installs

For eligible stable Sedna Linux installs created by the fork-owned standalone
release installer:

- lifecycle commands launch the canonical executable selected from the
  standalone releases root
- `bootstrap` is supported
- `bootstrap` starts a detached pid-backed updater loop that resolves `latest`
  and accepts only a strictly newer stable Sedna release
- after a successful eligible refresh, if app-server is running and the managed
  binary contents changed, the updater restarts app-server with that binary
  first and only then replaces its own process image
- the updater loop is not reboot-persistent; a later `codex remote-control`
  start restores a missing eligible updater and app-server without repeating
  bootstrap

Prerelease, macOS, package-manager, and unsupported installations are
manual-only. Choose the exact release tag from the Sedna releases page; use an
explicit prerelease allowance or macOS preview selector only when the selected
release requires it.

### Out-of-band updates

This daemon does not watch arbitrary executable files for replacement. If some
other tool updates the managed binary path:

- without `bootstrap`, a currently running app-server remains on the old
  executable image until an explicit `restart`
- with `bootstrap` on an eligible stable Sedna Linux install, the detached
  updater loop notices the changed managed binary on its next successful
  selected-candidate pass; if app-server is running, it refreshes app-server
  first and then refreshes itself once that replacement starts successfully

## Lifecycle semantics

`start` is idempotent and returns after app-server is ready to answer the normal
JSON-RPC initialize handshake on the Unix control socket.

`restart` stops any managed daemon and starts it again.

`enable-remote-control` and `disable-remote-control` persist the launch setting
for future starts. If a managed app-server is already running, they restart it
so the new setting takes effect immediately.

Top-level `codex remote-control` bootstraps with `--remote-control` only before
initial setup. On every later start it preserves a running app-server and
reconciles the updater against the current validated release: it starts a
missing eligible updater, preserves an eligible updater only when its recorded
executable identity matches the current release, replaces a stale eligible
updater, stops an ineligible running updater, and leaves an ineligible missing
updater absent.
This also restores both services after a reboot when the current release is
eligible.

`stop` sends a graceful termination request first, then sends a second
termination signal after the grace window if the process is still alive.

All mutating lifecycle commands are serialized per `CODEX_HOME`, so a concurrent
`start`, `restart`, `enable-remote-control`, `disable-remote-control`, `stop`,
or `bootstrap` does not race another in-flight lifecycle operation.

## State

The daemon stores its local state under `CODEX_HOME/app-server-daemon/`:

- `settings.json` for persisted launch settings and bootstrap completion state
- `app-server.pid` for the app-server process record
- `app-server-updater.pid` for the pid-backed standalone updater loop
- `daemon.lock` for daemon-wide lifecycle serialization
