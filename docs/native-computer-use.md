# Native Computer-Use Adapter Tooling

This document describes the first-party computer-use surface in the Sedna
Codex fork. It is intentionally scoped to Codex-owned protocol, transcript,
tool-registry, app-server, TUI, rollout, and validation behavior.

Native runtime backends are supplied by external providers. Android is the
first implemented provider and now uses a shared provider bridge for TUI and
`codex exec`. Browser is a registered adapter with a shared provider bridge
that can either invoke an operator-configured command or use the built-in
Playwright backend for `backend=auto`, `browser`, or `chromium`. A configured
command provider may claim `backend=chrome` for signed-in Chrome/extension/CDP
integration; Playwright never claims that backend.
Desktop is a registered adapter for independent macOS Screen
Recording/Accessibility-style runtimes and future native desktop providers.

## Ownership Boundaries

- Codex owns native computer-use transcript semantics: request and response
  protocol events, model-visible function tool definitions, thread-history
  projection, app-server request routing, TUI rendering, rollout persistence,
  and rollout-trace runtime boundaries.
- Runtime providers own backend/session lifecycle, screenshots or viewport
  capture, UI digest generation, input execution, launch/navigation behavior,
  and any emulator, physical-device, browser, or desktop setup.
- Solar Gravity Lab can consume and prove the Android flow, but it is not the
  generic owner of Codex computer-use tooling.
- Android MCP or app-specific repositories may provide runtime capability, but
  they should not redefine Codex's transcript or app-server protocol shape.

## Model-Facing Tools

Codex promotes a small set of bare dynamic tool names to native computer-use
handlers. Namespaced tools remain ordinary dynamic tools.

### Android

- `android_observe`: captures the current Android screen as model-visible
  image output, optionally paired with a compact UI digest.
- `android_step`: performs one or more bounded Android actions, then returns a
  fresh post-action observation.
- `android_install_build_from_run`: installs a GitHub Actions Android build
  artifact into the active Android session, optionally launches it, then
  returns a fresh post-install observation when available.

The existing Android adapter remains the reference MCP-backed runtime provider,
not a throwaway prototype. It already demonstrates the split this fork should
keep: Codex owns canonical tool schemas, transcript events, and native image
delivery; the Android provider owns emulator/device sessions, `adb`/
UIAutomator-style capture, screenshot artifacts, UI digests, input execution,
and provider-side build installation.

Android providers should internalize the practical emulator-QA discipline that
agents otherwise have to rediscover by hand: choose an explicit device serial,
prove boot/readiness, install or launch the target app, capture a screenshot,
dump a UI hierarchy, summarize visible controls, and collect logs or trace
artifacts when the task asks for debugging evidence. The Codex-facing result
should still stay small and visual: native `inputImage` content plus compact
state text. UI hierarchy XML, screenshot files, logcat, and run manifests are
receipts for audit and replay, not substitutes for model-visible pixels.

Selectors should be backed by the Android UI tree whenever possible. Providers
should prefer visible text, content descriptions, resource ids, class names, or
bounds derived from the current UI hierarchy before falling back to visually
guessed coordinates. Screenshots give the model product judgement; the UI tree
gives the provider reliable hands.

`android-emulator-mcp` or a successor should therefore be reused when it can
expose the current Android MCP tool contract (`android.inspect_ui`,
`android.wait_for_stable_ui`, `android.capture_screenshot`,
`android.read_artifact`, `android.input.*`, and
`interactive_session.install_build_from_run`). Codex may consume MCP
`content[]` image items directly, or use `android.read_artifact` to convert a
screenshot artifact into native model-visible image output. A provider that
only returns text, UI digests, or host-local artifact paths is not sufficient
for native computer use. If a provider needs a different internal harness,
adapt it inside the provider or through a thin provider-side compatibility
layer rather than changing Codex hot core paths. Do not fold Android into the
browser backend registry: Android is a peer native adapter, while the browser
registry is the routing layer for browser-specific backends such as
Playwright, in-app browser, and Chrome extension.

The `multi_touch` Android step action sends two to five complete pointer paths
to `android.input.multi_touch` as one atomic provider call. Codex validates the
pointer count, non-negative integer coordinates, and the 50–2000 ms duration
before dispatch. If the provider does not advertise the capability, the action
fails with an explicit operator-facing message. It must never be approximated
with sequential taps or swipes because that changes the gesture's ownership
and timing semantics.

Local Android bridge configuration lives in
`~/.codex/android-computer-use.json`; the legacy
`~/.codex/android-dynamic-tools.json` and
`~/.codex/solarlab-android-dynamic-tools.json` names are still accepted while
older provider configs converge. The bridge requires `mcp_url`, and may also read
`default_serial`, `default_package_name`, and `default_activity`. Explicit tool
arguments always win. When a serial is not supplied, the bridge applies
`default_serial` to observe, step, screenshot fallback, and build-install
provider calls. When `launch_app` omits a package, the bridge uses
`default_package_name`; `default_activity` is applied only for the configured
default package so an explicit package does not accidentally inherit the wrong
activity.

When an Android action fails, the native bridge should return the action error
with a fresh post-failure observation when the provider can still capture one:
current screenshot, compact UI digest or selector candidates, and any completed
action summaries. This mirrors the browser failure contract and keeps agents
from blindly replaying mutating input after a partially completed flow. If
post-failure screenshot capture is unavailable, the response must say that the
current state is unproven and instruct the agent to recover with
`android_observe` before making visual claims.

When the Android provider itself is temporarily unreachable, including hosted
provider tunnel failures surfaced as HTTP 530, Codex returns a failed
computer-use response labeled `Android provider unavailable` with
`retryability: retry_same_request`. That classification is an operator hint:
the tool call did not prove anything about the Android screen, and retrying the
same non-mutating observe call is reasonable after the provider is restored.

Performance, memory, and jank analysis belong in a companion Android
performance workflow rather than the hot `android_step` path. Use adb-backed
tools such as Simpleperf, Perfetto, `gfxinfo`, `dumpsys meminfo`, heap dumps,
and logcat around a focused flow when the user asks about responsiveness,
startup, leaks, CPU, or frame timing. Native observe/step remains the default
eyes-and-hands loop; performance artifacts are an opt-in evidence lane.

### Browser

#### Surface identity and upstream relationship

The browser names span different runtime layers and are not interchangeable
aliases:

- Upstream product Browser Use (`@Browser`) is an app-supplied in-app browser
  surface. The official `@Chrome` integration is a separate app/extension
  surface for a signed-in Chrome profile.
- The stock upstream CLI/app-server can carry generic browser-like dynamic
  tools when an external host supplies them, but it does not discover and
  advertise the downstream local browser provider contract itself.
- Downstream bare `browser_observe` and `browser_step` are Codex-native because
  Codex owns their canonical schemas, transcript events, native image output,
  provider dispatch, and start/resume/fork propagation. A runtime provider may
  still use Playwright underneath that native contract.
- `backend: "chrome"` selects a configured Chrome-class provider. It does not
  by itself select upstream `@Chrome`, the official extension, or the user's
  regular signed-in Chrome profile. Likewise, `backend: "iab"` requires a
  provider that explicitly implements the app-integrated backend.

- `browser_observe`: captures the current browser viewport as model-visible
  image output, optionally paired with compact page metadata.
- `browser_step`: performs one or more bounded browser actions, then returns a
  fresh post-action viewport observation.

Browser tools include a `backend` hint with values such as `auto`, `browser`,
`chrome`, `chromium`, and `iab`. `auto` lets the provider choose the best
available browser backend. `browser` and `chromium` can be served by the
built-in Playwright provider when it is configured for local Chrome or Chromium.
`chrome` requires a provider that explicitly claims that backend, such as an
external signed-in Chrome/extension/CDP provider; Playwright never claims it.
`iab` is intended for the Codex app in-app browser and requires a provider that
declares that backend. The hint is part of the provider contract. Command
providers can still claim exact backends or wildcard routing for hosted,
extension-backed, remote, or app-integrated browser runtimes.

The browser provider bridge is intentionally pluggable and now supports both
the original single-provider configuration and a provider registry:

- `CODEX_BROWSER_COMPUTER_USE_PROVIDER=playwright` enables the embedded
  Playwright bridge for `backend=auto`, `browser`, and `chromium`.
  The bridge launches a persistent browser profile, serializes access to that
  profile, executes bounded browser actions, and returns the viewport
  screenshot as native `inputImage` content. This backend requires `node` and a
  Playwright package that Node can resolve in the runtime environment. For
  realistic remote-editor review loops, configure it to run headed Google
  Chrome on a visible display rather than the default headless Chromium path.
  Headed launches normalize the initial browser window geometry, and capture
  falls back from Playwright page screenshots to CDP and locator screenshots so
  stale Chrome window placement does not turn a visible browser into a
  text-only failure. The bridge supports accessibility-oriented selectors plus
  human-like mouse and keyboard primitives: click, type, keypress, key down/up,
  scroll/wheel, hover, drag, mouse move/down/up, select, wait, focus, clear,
  and navigate. Selector text entry uses real keyboard events by default, with
  `method: "fill"` available as a compatibility escape hatch for pages where
  DOM-level filling is the better tool. `browser_observe` can request compact
  interaction metadata with `scope: "viewport_and_page"` and can capture a
  small labeled viewport bundle for top/bottom or desktop/mobile UX review.
  Interaction metadata uses labels, visible text, attributes, and selector
  hints; it must not echo arbitrary typed field values into model context or
  saved manifests.
  Maps rank focused and visible actionable controls before the 24-control cap,
  exclude hidden/inert ancestors, and report total, offset, and omitted counts.
  Use `interaction_map: {"scope":"page","offset":24}` for another bounded
  slice. Iframe entries describe only their containers, never frame contents.
  Capture receipts distinguish requested viewport, effective inner dimensions,
  document client dimensions, scroll dimensions/position, device-pixel ratio,
  and visual viewport scale. Each image keeps its own label and receipt;
  restored state is reported separately. Visual viewport scale is not a
  measurement of desktop browser zoom.
  Optional `inspection: {"sections":["runtime","frames","console","network"]}`
  adds fixed read-only structural facts and call-local event metadata. Event
  lists are capped at 20 rows with observed/omitted counts. Console text and
  arguments, network headers/bodies, URL paths/query/fragment/userinfo, and
  frame contents are omitted. Framework markers are hints, not version proof.
  Scroll trails retain immediate `after` and separate `settledAfter` state.
  When a Playwright-backed step fails, the bridge returns the current page
  state, visible controls, selector candidates, and a fresh native screenshot
  whenever screenshot capture still works.
- `CODEX_BROWSER_COMPUTER_USE_COMMAND` points to an external provider command.
  Codex sends `ComputerUseCallParams` JSON on stdin and expects a
  `ComputerUseCallResponse` JSON object on stdout. This is the extension point
  for in-app-browser, signed-in Chrome, remote, or hosted browser providers.
- `~/.codex/browser-computer-use.json` may provide the same configuration with
  `provider`, `command`, `node`, `node_path`, `timeout_secs`, `state_dir`,
  `headless`, `executable_path`, `channel`, `display`, `capture_mode`,
  `isolation`,
  `viewport_width`, `viewport_height`, `artifact_dir`, `artifact_policy`,
  `allow_call_extra_http_headers`, and `service_profiles` fields.
- `~/.codex/browser-computer-use.json` may also provide `providers[]` and
  `routing.fallback_order`. Each provider can declare an `id`, `provider`,
  `command`, `backends`, `platforms`, and provider-specific settings. Exact
  backend requests such as `chrome` or `iab` route only to providers that
  claim that backend; wildcard command providers can claim every backend.
- The built-in Playwright provider defaults to `isolation: "thread"`, placing
  each Codex thread or spawned agent in its own persistent browser profile under
  `state_dir/profiles/`. Calls in the same thread reuse browser state, while
  concurrent sidecars do not share a Chrome profile, lock, or restored URL.
  Operators can set `isolation` to `shared`, `environment`, or `call` when a
  single shared profile, selected environment scope, or per-call ephemerality is
  the better runtime contract.

The built-in Playwright provider can save optional audit artifacts. Native
`inputImage` content remains the primary model-facing channel; artifacts are
diagnostic breadcrumbs for review bundles or failure triage. Set
`artifact_policy` to `failure` or `always`, and optionally set `artifact_dir`.
Failures save artifacts by default unless `artifact_policy` is `off`; successful
calls save artifacts only when requested or when policy is `always`. Artifact
write failures are reported in the browser observation text and should not turn
an otherwise useful browser observation into a provider failure.

Service-account navigation is configured locally with `service_profiles`.
Profiles declare an `id`, a non-secret `actor` label, `allowed_hosts`, and
headers supplied either directly from local config or through environment
variables. Tool calls may request a `service_profile`. Direct per-call headers
are accepted only when `allow_call_extra_http_headers` is enabled and
`allowed_hosts` is supplied. Provider text output and manifests may show actor
labels and header names, but must redact header values.

Example command-provider configuration:

```json
{
  "command": ["node", "/path/to/browser-provider.mjs"],
  "timeout_secs": 120
}
```

Example built-in Playwright configuration:

```json
{
  "provider": "playwright",
  "state_dir": "/path/to/browser-state",
  "isolation": "thread",
  "headless": true,
  "timeout_secs": 120
}
```

Example realistic headed Chrome configuration for a visible Linux display:

```json
{
  "provider": "playwright",
  "node": "node",
  "node_path": "/path/to/node_modules",
  "executable_path": "/usr/bin/google-chrome",
  "display": ":99",
  "state_dir": "/path/to/browser-profile",
  "headless": false,
  "capture_mode": "viewport",
  "viewport_width": 1440,
  "viewport_height": 1000,
  "artifact_policy": "failure",
  "artifact_dir": "/path/to/browser-artifacts",
  "service_profiles": [
    {
      "id": "staging-access",
      "actor": "staging service account",
      "allowed_hosts": ["staging.example.com"],
      "env_headers": {
        "CF-Access-Client-Id": "CF_ACCESS_CLIENT_ID",
        "CF-Access-Client-Secret": "CF_ACCESS_CLIENT_SECRET"
      }
    }
  ],
  "timeout_secs": 180
}
```

Example routed provider configuration:

```json
{
  "providers": [
    {
      "id": "local-playwright",
      "provider": "playwright",
      "backends": ["auto"],
      "state_dir": "/path/to/browser-state",
      "headless": true
    },
    {
      "id": "signed-in-chrome",
      "provider": "command",
      "backends": ["chrome"],
      "command": ["node", "/path/to/chrome-provider.mjs"]
    },
    {
      "id": "visible-browser-shell",
      "provider": "command",
      "backends": ["iab"],
      "platforms": ["windows"],
      "command": ["browser-shell-provider.exe"]
    }
  ],
  "routing": {
    "fallback_order": ["local-playwright", "signed-in-chrome"]
  },
  "timeout_secs": 120
}
```

An external command provider should read one `ComputerUseCallParams` JSON object
from stdin and write one `ComputerUseCallResponse` JSON object to stdout. For
successful visual responses, that object must include a native `inputImage`
content item. The shared browser bridge fails loudly when a successful browser
provider response contains only text, metadata, or artifact paths.

The North Star is that screenshots are delivered to the model as native
`inputImage` content items in the computer-use response. Provider artifact paths
may exist for audit, replay, and diagnostics, but they are not the primary
model-facing visual channel and should not be exposed as instructions for the
model to fetch local files. If screenshot inlining fails, the response may
include a concise diagnostic that names the provider artifact involved; that is
an error breadcrumb, not the normal contract.

`codex doctor` includes a read-only browser computer-use check. It reports
configured browser provider files, provider ids, declared backends, Android
provider files, Android endpoint presence, desktop provider files, environment
overrides, whether configured browser, desktop, or Node executables are
resolvable, whether Node can resolve Playwright, whether Playwright has an
installed default browser executable when no explicit executable or channel is
configured, and the Android native-image contract. It does not launch browsers,
connect to user profiles, start emulators, call Android MCP servers, start
desktop providers, or repair configuration.

### Desktop

- `desktop_observe`: captures the current desktop app/window state as
  model-visible image output, optionally paired with a compact accessibility or
  UI digest.
- `desktop_step`: performs one or more bounded desktop UI actions, then
  returns a fresh post-action screenshot observation.

Desktop is the independent provider adapter for macOS Screen Recording and Accessibility
runtime providers. It intentionally uses a provider command behind the TUI
seam rather than linking provider implementation into Codex core. Configure it
with `CODEX_DESKTOP_COMPUTER_USE_COMMAND` or
`~/.codex/desktop-computer-use.json`:

```json
{
  "provider": "command",
  "platforms": ["macos"],
  "command": ["/path/to/desktop-provider", "stdio"],
  "timeout_secs": 120
}
```

The file form also supports a provider registry so operators can keep separate
desktop runtimes for macOS Screen Recording/Accessibility, Linux/noVNC, or
future Windows desktop shells without changing Codex core:

```json
{
  "providers": [
    {
      "id": "macos-accessibility",
      "provider": "command",
      "platforms": ["macos"],
      "command": ["/path/to/macos-desktop-provider", "stdio"]
    },
    {
      "id": "linux-visible-desktop",
      "provider": "command",
      "platforms": ["linux"],
      "command": ["/path/to/linux-desktop-provider", "stdio"],
      "timeout_secs": 180
    }
  ],
  "routing": {
    "fallback_order": ["macos-accessibility", "linux-visible-desktop"]
  },
  "timeout_secs": 120
}
```

The desktop provider receives the same `ComputerUseCallParams` object on stdin
and must return one `ComputerUseCallResponse` object on stdout. Successful
visual responses must include a native `inputImage` content item. Permission
prompts, Screen Recording/Accessibility state, lock-screen behavior, app
focus, screenshot capture, UI-tree generation, and input synthesis all remain
provider responsibilities.

## Provider Contracts

Native desktop and browser providers implement the interfaces described in
[`native-computer-use-contracts.md`](native-computer-use-contracts.md) behind
the command-provider or provider-registry seams. Codex owns the canonical
schemas, transcript events, lifecycle propagation, and native image-output
contract; runtime providers own environment-specific capability and
permissions.

These tools are installed from dynamic thread tools supplied through app-server
thread start, resume, or fork requests. When the tool has no namespace and the
name matches one of the native tool names above, Codex replaces the provider's
ad hoc schema with its canonical first-party function schema and registers the
handler as `ComputerUse`.

Namespaced tools are not promoted. For example, `codex_app.android_observe`
and `codex_app.browser_step` remain normal dynamic tools. This preserves room
for app-specific dynamic tools while keeping the bare names as the stable
native contract.

Observe tools are treated as non-mutating. Step and install tools are treated
as mutating. Android install receives a longer response timeout than ordinary
observe/step calls because it may need to download an artifact, install an APK,
launch it, and verify foreground state.

## Provider Capability and Manifest Integration

Runtime providers advertise support by adding dynamic tools to the thread.
`DynamicToolSpec` carries optional capability metadata:

- `family`, such as `android`
- `capabilityScope`, such as `environment`
- `mutationClass`, such as `mutating`
- `leaseMode`, such as `exclusive_write`

Codex preserves that metadata for dynamic tool discovery and state persistence,
but native promotion is intentionally based on the bare tool names above. The
provider capability describes the available runtime; Codex still owns the
canonical model-facing schema and computer-use handler once the bare native
names are selected.

When capability metadata is present, app-server validates and forwards it as
part of the dynamic tool contract. That metadata describes runtime capability;
it does not replace the Codex-owned native schema or transcript behavior for
bare `android_observe`, `android_step`, `android_install_build_from_run`,
`browser_observe`, `browser_step`, `desktop_observe`, or `desktop_step`.

Deferred tool search also treats bare native dynamic tools as computer-use
candidates, so deferred discovery loads the canonical Codex tool definition
rather than the provider's raw dynamic schema.

## Runtime Flow

1. A thread is started, resumed, or forked with `dynamicTools` containing bare
   native computer-use tools such as `android_observe`, `android_step`,
   `android_install_build_from_run`, `browser_observe`, `browser_step`,
   `desktop_observe`, or `desktop_step`.
2. The tool registry promotes those names to canonical Codex function tools and
   registers `ToolHandlerKind::ComputerUse`.
3. When the model calls one of those tools, `codex-core` emits a
   `ComputerUseCallRequest` event with `callId`, `turnId`, optional
   `environmentId`, `adapter`, `tool`, and JSON arguments.
4. App-server API v2 projects the event to a `computerUseCall` thread item and
   sends `item/computerUse/call` to the connected client.
5. The capable client executes the provider operation and returns
   `ComputerUseCallResponse` with text plus native image content items,
   `success`, and optional `error`. For observe, step, and install
   observations, screenshots or browser viewports should be returned as
   `inputImage` data URLs or another Codex-supported image reference, not as
   model-facing local artifact paths.
   When the Android provider is reached through an MCP-style `tools/call`
   bridge, Codex treats `structuredContent` and `content[]` as complementary:
   `structuredContent` supplies state, artifacts, and UI digests, while
   `content[]` image entries supply the model-visible pixels. A provider must
   not rely on `structuredContent` alone for visual computer-use output.
6. Codex submits the response back into the active turn, emits
   `ComputerUseCallResponse`, and passes the resulting content to the model as
   function-call output.

When a native computer-use tool is invoked through code mode, Codex projects
the response as a typed `{ content, success }` object. The `content` array keeps
each `input_text` and `input_image` item intact. Code-mode callers must pass the
selected `input_image` item to `image(...)`; they must not serialize the whole
result through `text(...)`, because that would turn the inline image data into
model-facing text instead of a native image input. The same typed result is
retained when `success` is false, so provider diagnostics and a failure-time
screenshot can both remain available to the caller.

Ordinary dynamic tools and code mode also accept upstream `input_audio` output
through the `audio(...)` helper. Upstream owns audio preparation,
duration-aware context accounting, output truncation, compaction, and replay
through user and tool history. Those paths retain the downstream flat
dynamic-tool compatibility record and optional image detail. They do not widen
the native computer-use response enum: computer-use providers still return text
and image items.

If no selected environment exists, Codex returns a failed native response
without sending an external client request. If the client does not answer before
the computer-use timeout, Codex unregisters the pending response and returns a
failed timeout response.

## App-Server and TUI Projection

Native computer-use requires app-server API v2. Older API versions receive a
failed response explaining that v2 is required.

The v2 app-server protocol includes:

- `item/computerUse/call` server requests
- `ComputerUseCallParams`
- `ComputerUseCallResponse`
- `ComputerUseCallOutputContentItem`
- `ComputerUseCallStatus`
- `ThreadItem::ComputerUseCall`

Computer-use calls are live app-server projections: request and response events
are transient and are not canonical turn items or durable thread-history
events. Thread reads and resumed sessions therefore do not reconstruct earlier
`ThreadItem::ComputerUseCall` items. The TUI renders live computer-use cells,
including fallback messaging when the active TUI session has no native
computer-use provider for the request. Completed calls use adapter-specific
transcript labels such as `Used browser`, `Used computer`, or `Used Android
emulator`; in-flight calls use the matching `Using ...` label. The visible
transcript summarizes text output and records native screenshots as `<native
screenshot>` without embedding screenshot data into transcript text.

For CLI/TUI sessions, configured local Android, browser, and desktop providers
advertise the bare native dynamic tools at thread start, resume, and fork time.
Android configuration advertises `android_observe`, `android_step`, and
`android_install_build_from_run`; browser configuration advertises
`browser_observe` and `browser_step`; desktop command-provider configuration
advertises `desktop_observe` and `desktop_step`. The advertised tools are
session-scoped and are not persisted blindly across resumes; each new CLI or
TUI session re-checks local provider configuration before exposing native
computer use to the model.
Thread-spawned agents inherit the parent thread's advertised dynamic tools, so
native-capable sidecars can use the native surface instead of falling back to
unrelated MCP adapters. Provider isolation still decides whether browser
sidecars share browser state with the parent or receive independent headed
profiles.

Transcript visibility depends on the native computer-use event path. Provider
operations are expected to enter Codex as `ComputerUseCallRequest` and
`ComputerUseCallResponse` events after bare native tool names are promoted to
`ToolHandlerKind::ComputerUse`. Calls injected by an outer host environment or
compatibility bridge are useful runtime probes, but they do not prove TUI or
`Ctrl+T` transcript visibility unless they are bridged back into those native
Codex events.

## Rollout and Trace Semantics

Computer-use request and response events are transient in every history mode;
they are not stored in a thread snapshot. Live rollout tracing maps them to
tool-runtime start and end boundaries:

- `ComputerUseCallRequest` starts the runtime span.
- `ComputerUseCallResponse` ends the runtime span.
- Successful responses map to completed execution status.
- Failed responses map to failed execution status.

This keeps native Android calls visible in the same trace vocabulary as exec,
patch, MCP, and collaboration tool runtimes without adding separate core hooks.

## Validation

For documentation-only changes in this downstream docs set, use the lightweight
repository checks before broader hosted validation:

```bash
python3 .github/scripts/check_markdown_links.py
just downstream-docs-check
git diff --check
```

For changes that touch the divergence registry or current divergence baseline,
run the PR-local downstream docs sanity lane:

```bash
bash .github/scripts/validation-lanes/downstream-docs-check.sh
```

That lane checks formatting, registry JSON syntax, and relative Markdown links
without requiring the whole downstream fork to have a complete current
divergence registry.

When the goal is to refresh or prove the full downstream divergence baseline,
run the explicit full-history audit instead:

```bash
bash .github/scripts/validation-lanes/downstream-divergence-audit.sh
```

The full audit compares the checked-out downstream head with the current
upstream mirror and enforces registry coverage for all live downstream code
differences, so it belongs on explicit baseline-maintenance or checkpoint
validation rather than ordinary docs-only PR validation.

For implementation changes, prefer hosted validation through `validation-lab`.
The focused lanes are:

- `codex.app-server-computer-use-targeted`: app-server v2 routing, client
  response handling, and thread start/resume/fork injection.
- `codex.tui-native-computer-use-targeted`: native request/response events
  render as transcript-visible computer-use cells and can be inserted into the
  live `Ctrl+T` transcript overlay.
- `codex.exec-native-computer-use-targeted`: configured Android and browser
  dynamic-tool advertisement, provider request handling, and compact
  computer-use event projection in `codex exec`.
- `codex.native-computer-use-tool-registry-targeted`: canonical Android,
  browser, and desktop schema conversion, adapter classification, duplicate handling,
  deferred tool search, and core timeout cleanup.
- `codex.native-computer-use-doctor-targeted`: `codex doctor` reporting for
  Android, browser, and desktop provider configuration.
- `codex.app-server-protocol-test`: protocol schema and thread-history
  projection coverage.
- Hosted CodeQL Rust contract checks cover native-image guard dominance,
  success-with-error contradictions, advisory text/image match drops, and a
  regression sentinel for Android MCP tool-result parsing that would directly
  return `structuredContent` while dropping sibling `content[]` images. The
  advisory text/image match query also covers app-server protocol conversion
  surfaces so native image preservation stays guarded across the thread-history
  boundary.

The local just recipes behind those lanes are:

```bash
just app-server-computer-use-targeted
just tui-native-computer-use-targeted
just exec-native-computer-use-targeted
just native-computer-use-tool-registry-targeted
just native-computer-use-doctor-targeted
just native-browser-evidence-targeted
```

The hosted `codex.native-browser-evidence-targeted` lane installs the pinned
Playwright package and Chromium into runner-temporary paths, runs every
`codex-rs/browser-computer-use/src/*_test.mjs` file through Node's test runner,
and validates the generic `use-native-browser` skill in an isolated PyYAML
virtualenv. It is an evidence lane only: test results and any synthetic
artifact manifest remain provider/test receipts;
they do not replace native model-visible screenshots or claim a live provider.
Saved artifact runs use private unique directories and exclusive file creation
so concurrent captures with the same label cannot overwrite one another.

Do not use local Android builds, browser sessions, or app-specific validation
as the default proof for Codex protocol/tool semantics. Use Android harness,
browser-provider, or Solar Gravity Lab validation only when the question is the
runtime provider or a consumer app, not the generic Codex computer-use
contract.

The built-in Playwright browser provider owns browser profile launch hygiene:
before opening a persistent context, it clears Chromium tab-session restore
artifacts so stale restored tabs cannot navigate to old localhost targets ahead
of an explicit `browser_observe` or `browser_step` URL. Provider-managed
`state.json`, cookies, local storage, and other profile data remain intact.

## Primary Files

- `codex-rs/protocol/src/computer_use.rs`
- `codex-rs/protocol/src/protocol.rs`
- `codex-rs/protocol/src/dynamic_tools.rs`
- `codex-rs/tools/src/android_tool.rs`
- `codex-rs/tools/src/browser_tool.rs`
- `codex-rs/tools/src/computer_use_tool.rs`
- `codex-rs/tools/src/desktop_tool.rs`
- `codex-rs/core/src/tools/handlers/computer_use.rs`
- `codex-rs/tools/src/tool_search.rs`
- `codex-rs/core-plugins/src/lib.rs`
- `codex-rs/app-server/src/computer_use.rs`
- `codex-rs/app-server/src/bespoke_event_handling.rs`
- `codex-rs/app-server-protocol/src/protocol/common.rs`
- `codex-rs/app-server-protocol/src/protocol/v2/item.rs`
- `codex-rs/app-server-protocol/src/protocol/thread_history.rs`
- `codex-rs/android-computer-use/src/lib.rs`
- `codex-rs/tui/src/android_computer_use_provider.rs`
- `codex-rs/browser-computer-use/src/lib.rs`
- `codex-rs/browser-computer-use/src/browser_playwright_provider.mjs`
- `codex-rs/tui/src/browser_computer_use_provider.rs`
- `codex-rs/tui/src/computer_use_provider.rs`
- `codex-rs/tui/src/desktop_computer_use_provider.rs`
- `codex-rs/exec/src/lib.rs`
- `codex-rs/tui/src/app/app_server_requests.rs`
- `codex-rs/tui/src/app/app_server_events.rs`
- `codex-rs/tui/src/chatwidget.rs`
- `codex-rs/tui/src/chatwidget/interrupts.rs`
- `codex-rs/tui/src/history_cell/computer_use.rs`
- `codex-rs/rollout/src/policy.rs`
- `codex-rs/rollout-trace/src/protocol_event.rs`
- `codex-rs/app-server/tests/suite/v2/computer_use.rs`
- `codex-rs/tools/src/android_tool_tests.rs`
- `codex-rs/tools/src/browser_tool_tests.rs`
- `codex-rs/tools/src/computer_use_tool_tests.rs`
- `codex-rs/tools/src/desktop_tool_tests.rs`
- `.github/validation-lanes.json`
- `justfile`
