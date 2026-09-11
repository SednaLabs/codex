# Carry Divergence Ledger

This document records the current live divergences of the downstream branch
(historically `carry/main`, now `main`) from `upstream/main`.

It is an audit ledger, not a changelog. Ahead-count alone is not evidence of a
live divergence.

The ledger remains manually curated. Its current counts and refs are delegated
to the generated status receipt so this historical narrative cannot silently
be mistaken for live state.

## Audit Baseline

- Audited on: `2026-07-28`
- downstream integration code tree: `bff348fd68a99e1996d00dce1d46ba8ed9d37be3`
- comparison basis: `upstream/main`
- mirror branch `upstream-main` (`origin/upstream-main`): `3418498f01422f5f650ea645d4bd19e05c3a9616`
- `upstream/main`: `7cde2323f3712999e9ab98b16287e08b7735d52f`
- downstream branch vs `upstream/main`: `2131` downstream ahead, `60` upstream ahead
- Mirror vs `upstream/main`: `0` ahead, `14` behind (`stale_ff_only`)
- Downstream-only non-merge commits at audit time: `1800` unique, `0` patch-equivalent

See [`generated/upstream-status.md`](generated/upstream-status.md) for the
reproducible current-state receipt and
[`upstream-sync/2026-07-28.md`](upstream-sync/2026-07-28.md) for harvest
decisions.

## Audit Rules

- Count a live divergence only when the maintained downstream branch tree
  (historically `carry/main`, now `main`) still differs from `upstream/main`.
- Count generated schemas, snapshots, and inline test-module moves as
  derivative churn, not as standalone divergence items.
- Keep canonical config schema generation in `codex-config`; `codex-core`
  delegates to that implementation so Cargo and Bazel cannot normalize the
  same fixture through different code paths.
- Track exact-subject upstream matches separately as historical carry history.
- Treat the exact-subject upstream match list as a lower bound for "already
  upstreamed" history, not a complete semantic-duplicate detector.

## Current Downstream Guardrails

### MCP JSON-family Resource Output Boundary

- A model-visible `read_mcp_resource` result must not report success after
  generic envelope truncation has cut a nested `application/json` or
  `application/*+json` text resource. The raw source payload remains available
  to code mode and records a successful execution for hooks, lifecycle, and
  rollout tracing, while the model receives a fixed compact failed structured
  result whenever that resource does not fit the model-output budget.
- The stored history receives only that compact error, so the generic history
  output cap cannot reintroduce malformed nested JSON. Plain text and Markdown
  resource rendering remains on the existing generic truncation path.
- `serialize_read_resource_output_preserves_small_json_for_model`,
  `large_json_resource_fails_closed_for_model_and_preserves_code_mode_payload`,
  `serialize_read_resource_output_fails_closed_for_vendor_json_media_type`,
  `serialize_read_resource_output_fails_closed_for_problem_json_media_type`,
  `serialize_read_resource_output_keeps_untyped_json_on_generic_truncation`,
  `serialize_read_resource_output_keeps_large_markdown_on_generic_truncation`,
  and `history_does_not_retruncate_bounded_json_resource_error` are the focused
  hosted guardrails. Drop this narrow carry when upstream provides an equivalent
  structured-output boundary.

### Agent-Tree Usage Lineage Telemetry

- The extension-owned local `usage.sqlite` records explicit lineage edge kind
  and concrete spawn-request identity when it is available. Older records are
  reported with an explicit inferred-confidence marker rather than being
  promoted to exact history.
- Experimental app-server `thread/usageSummary` returns one persisted root
  family with per-thread and aggregate tokens, provider-reported credits, and
  pricing coverage. Its direct-user continuation candidate is advisory only;
  neither this endpoint nor the shared usage ledger changes resume authority.
- Shared-ledger extraction preserves the legacy runtime-source field and adds
  a separate interaction-source field. It may report generic provenance and
  source-derived subagent edges, but it must never be used as the authority for
  `codex resume` selection.
- Hosted validation must cover the migration, state reader, app-server schema
  generation, and direct-user continuation guard before this carry is promoted.

### Core Compiler Query-Depth Guardrail

- Upstream commit `d7e8f4c3dc` makes the interrupted-MCP startup path preserve
  user input by threading cancellation through more of `run_turn`.
- The downstream turn path combines that upstream async shape with retained
  multi-agent, dynamic-tool, and native-computer-use state. GitHub-hosted
  targeted run `29968292080` then reached rustc's default query-depth limit
  while computing the `run_turn` layout before any selected test ran.
- `codex-core` sets `#![recursion_limit = "256"]`, exactly as rustc recommends
  for this crate. The attribute changes compile-time query evaluation only; it
  does not change runtime recursion, control flow, or request semantics.
- Keep `core-runtime-surface-smoke` as the focused hosted compilation proof.
  Drop this small carry when upstream adopts an equivalent limit or reduces the
  composed async type beneath the default limit.

### Multi-Agent Mode Reset Guardrail

- Upstream commit `0da13c6c99` adds multi-agent-mode state to the model-visible
  world-state baseline. A retained developer fragment is append-only, so
  emitting no fragment after a custom policy is removed leaves the old policy
  visible to the model.
- Normalize an empty custom mode to inactive and replace a prior custom or
  proactive fragment with the existing explicit-request-only fragment. This
  preserves the correct default without repeating a reset on later turns.
- `custom_mode_removal_replaces_retained_instructions` proves the retained
  history transition. The resumed-thread integration regression
  `changing_configured_mode_hint_to_empty_appends_explicit_reset` proves the
  emitted reset replaces the active mode while the prior custom fragment remains
  only as historical context. `codex.core-multi-agent-orchestration-targeted`
  runs both on GitHub-hosted validation. Remove this carry when upstream makes
  the same state-clearing transition explicit.

### Cancellation-Atomic Sub-Agent Publication

- The retained spawn lifecycle carries manager, reservation, and residency
  ownership through bounded cleanup until an unpublished child reaches a
  terminal state. Initial delivery claims ownership before the first input is
  submitted, late publication is rejected after cancellation is recorded, and
  a cancelled owner cannot remove a same-ID replacement.
- The current port is the r0-r5 successor chain (`58d9c95ce4`,
  `aaf2e01a0a`, `8626d877d6`, `6c243de2d9`, `5597d2cd6`) applied to the live
  downstream base. Focused tests cover both V1 and configured V2 spawn
  cancellation, terminal taxonomy, buffered history, capacity retention, and
  replacement protection.
- Keep `codex.core-multi-agent-orchestration-targeted` and
  `codex.core-subagent-surface-targeted` as the hosted proof lanes. Remove this
  carry when upstream provides an equivalent publication and retained-cleanup
  contract.

### Read-only GitHub App Installation-Token Broker

- `github_app_installation_broker.py` is a downstream operator boundary for
  the PR observer. It validates the App installation/account and one selected
  repository, reduces the requested set to explicit read permissions, and
  requests one short-lived installation token without using a personal token
  or App-user token.
- The broker keeps exactly one token in process memory, strips ambient GitHub
  token variables from a fingerprint-bound child, bounds and redacts output,
  and revokes the token on normal and exceptional exit. Private-key material is
  supplied through a fixed systemd `LoadCredential` basename and an inherited
  descriptor to OpenSSL; it is never persisted or printed.
- `test_github_app_installation_broker.py` is secret-free and mock-only. The
  `codex.agent-workflow-sanity` hosted lane compiles the broker and runs these
  tests. Phase A is development-ready for read-only selected-repository use;
  live installation proof, write permissions, broader repositories, and
  production commissioning are explicitly deferred.
- Preserve this carry until upstream supplies an equivalent independently
  attributable installation transport with selected-repository binding,
  read-only permission reduction, command fingerprinting, output redaction,
  and revocation guarantees. Do not expand the broker into queue/rerun
  business logic or provider mutation.

## Latest Upstream-Owned Integration

### Remote Plugins, Image Eligibility, App Metadata, And Sleeping Agent Mail

- Upstream `83ff1c2f80` caches remote plugin catalogs by global, user, and
  workspace scope with account-specific keys and a three-hour TTL. `plugin/list`
  serves a usable cache while stale catalog refreshes run in the background;
  `forceRefetch: true` bypasses the cache and only replaces it after a successful
  fetch. Sharing mutations invalidate the affected user and workspace scopes.
  This cache is plugin-discovery metadata, not an MCP connection or tool-runtime
  lifecycle replacement.
- Upstream `0a0a9b6c8f` excludes the standalone `image_generation` tool when
  cached authentication identifies a Free-plan account. Existing feature,
  provider-capability, model-modality, and authorization checks remain
  authoritative for other plans; image input/output plumbing and native image
  forwarding are otherwise unchanged.
- Upstream `b72079a2cf` loads plugin app metadata through authenticated batches
  of at most 100 IDs, preserving declared app identity and category when metadata
  is unavailable. `AppToolSummary` now carries enabled, disabled-reason, and
  read-only metadata with compatibility defaults. These are app-summary fields,
  not a parallel dynamic-tool registry.
- Upstream `44d76c6a6d` wakes an idle thread with a durable sleep when queue-only
  agent mail arrives, while ordinary idle threads still require `trigger_turn`.
  `queue_only_agent_mail_wakes_sleeping_root_and_persists_message` proves both
  wakeup and persisted history, and
  `codex.core-multi-agent-orchestration-targeted` carries it as a hosted
  guardrail.
- Upstream `4e0cee8030` makes local `plugin/list` requests with
  `forceRefetch: true` wait for configured plugin-cache reconciliation before
  building marketplace summaries. Reconciliation deduplication now includes
  the marketplace source, so same-path source changes can reinstall a plugin,
  and an effective-plugin change notification follows a cache update.
  `plugin_list_force_refetch_waits_for_same_path_local_plugin_upgrade` is
  included in `codex.app-server-v2-contract-targeted` as the hosted guardrail.
- Signed merge `839957c7a8` retains `4e0cee8030` as its exact upstream second
  parent. The integration branch adds no alternate plugin cache, image tool,
  app-summary, or mailbox wakeup implementation.

### Multi-Agent World State, Custom Web Search, And Guardian Limits

- Upstream `0da13c6c99` persists the effective multi-agent mode in world-state
  snapshots. The reset guardrail above is the only downstream addition around
  that new state: it replaces removed retained custom guidance with the existing
  explicit default rather than carrying an alternate mode model.
- Upstream `0f9fb40fa9` lets a custom provider opt into standalone web search.
  The provider capability gate leaves downstream dynamic-tool and MCP collision
  behavior unchanged.
- Upstream `9d82334302` lets Guardian review sessions use the selected review
  model's limits when it differs from the parent turn model. It does not change
  downstream child model/reasoning selection or Guardian reuse identity.
- Existing hosted recipes now pin upstream's custom-provider standalone
  web-search capability matrix, the custom-provider app-server round trip, and
  Guardian's same-model versus distinct-model limit behavior. This is
  validation-only integration coverage, not a downstream protocol change.
- Signed merge `e1187d4e3b` preserves `9d82334302` as its exact upstream second
  parent. Hosted mirror run `29969367710` fast-forwarded
  `origin/upstream-main` to that same SHA through successful sync job
  `89087782929`. Its separate baseline audit is intentionally not a promotion
  receipt: before promotion it evaluates `origin/main`, so its failure does not
  invalidate the exact mirror result.

### Hook Additional-Context Spill Limits

- The 2026-07-21 sync adopts upstream commit `e4836f998d`, which adds a
  per-command-hook `additionalContextLimit` for events that can emit
  `additionalContext`.
- Preserve upstream's exact semantics: an unset limit retains the approximate
  2,500-token default, `0` disables spilling for that hook, and each hook is
  evaluated independently before its context reaches the model.
- Preserve the field through JSON and TOML discovery, hook hashing, app-server
  config requirements and hook-list responses, generated schemas, and the TUI
  hook browser. Unsupported events ignore the setting with a warning.
- The generated app-server schemas are a shared sync seam. Future regeneration
  must retain this upstream field alongside downstream dynamic-tool and native
  computer-use protocol additions rather than selecting either parent schema.
- Exact-head Bazel run `29789793659` exposed a pre-existing command-runner race:
  a successful hook can emit stdout and exit without reading stdin, after which
  the parent's write observes `BrokenPipe` and previously discarded that valid
  output. Signed downstream commit `932cbceeb8` treats only `BrokenPipe` as a
  benign closed-input signal, then preserves the child's actual output and exit
  status; every other stdin write error retains the existing kill-and-report
  path.
- `hook_can_exit_successfully_without_reading_stdin` forces the closed-pipe path
  with a 2 MiB input on every supported host. The original
  `session_start_hooks_apply_additional_context_limits_individually` integration
  test remains enabled under Wine and guards the user-visible spill behavior.
  Drop this production-and-test carry together when upstream has equivalent
  successful-early-exit handling and regression coverage.

### Mid-Turn Compaction Hook Ordering

- The sync adopts upstream commit `8c41ed33ce`, which drains pending
  `SessionStart` hooks immediately after a successful mid-turn auto-compaction
  and before the next sampling request.
- A hook stop request ends the turn instead of allowing continuation. Otherwise,
  the hook's additional context is included in the immediately following
  sample, and repeated compactions in one turn repeat the same ordering.
- Preserve this ordering alongside downstream realtime and permission-context
  reconstruction. The upstream regressions
  `mid_turn_auto_compact_session_start_hooks_run_before_each_continuation` and
  `mid_turn_auto_compact_session_start_hook_stop_blocks_continuation` are the
  focused guardrails.

### Approval Rejection-Reason Propagation

- The sync adopts upstream commit `e52c35b000`, which changes protocol denials
  from a unit value to `Denied { rejection: String }` and serializes them as a
  structured `denied.rejection` object.
- Preserve the specific reason across command, patch, network, MCP, delegated,
  automatic-review, and shell-escalation paths. Invalid approval responses
  remain distinguishable from user declines, and model-visible rejection text
  remains bounded by upstream truncation.
- Conflict resolution kept upstream's structured denial schema and direct
  reason propagation while retaining the fork's independent `timed_out`
  semantics, `GuardianUserAuthorization` module path, dynamic-tool schema, and
  native computer-use schema additions.
- The shared-history assertion repair in `796d4248c5` only adapts a stale audio
  history test to the upstream copy-on-write `raw_items()` accessor. It does not
  introduce runtime carry.

### History And Hook Test API Convergence

- Upstream commit `ec3140db12` adopts the same `raw_items()` audio-history
  assertion and initializes the Windows command-hook fixture's
  `additional_context_limit` field.
- The earlier signed downstream test repair `796d4248c5` is now semantically
  upstream-owned and must not be counted as live carry.

### Paginated Rollout Lineage Resolution

- Upstream commit `b7e39aa316` adds the canonical bounded resolver for ordered
  paginated rollout segments, archived ancestors, and explicit history
  positions.
- Cycles, missing or mismatched source rollouts, non-paginated sources, and
  invalid cutoff bounds fail closed. Downstream resume, state, and usage
  overlays must use this lineage rather than inventing a competing traversal.

### Threadless MCP Connections

- Upstream commit `19940967bd` makes the MCP connection-manager event sender
  optional for threadless callers.
- Resource reads, status snapshots, and connector discovery skip session
  startup notifications, decline interactive elicitations, and continue
  non-interactive work. Downstream MCP pagination, OAuth, blocking waits, and
  runtime-snapshot ownership remain independent live carry.

### Linux `/proc` Preflight Isolation

- Upstream commit `44481a1c45` moves the bubblewrap `/proc` probe to a
  temporary minimal read-only filesystem view while preserving the requested
  network namespace mode.
- Conflict resolution retains downstream's constrained-host network-namespace
  fallback without restoring the command filesystem or working directory to
  the probe. `proc_mount_preflight_does_not_bind_the_full_filesystem` and
  `network_preflight_preserves_proc_mount_fallback` guard both halves.

### Absolute Test SQLite Paths

- Upstream commit `81e89fa5af` makes `SqliteConfig::new_for_testing` accept an
  `AbsolutePathBuf` directly. All downstream-only state fixtures use the same
  checked `.abs()` conversion; there is no production database-location or MCP
  behavior divergence.

### CSV Agent-Job Retirement And Migration Collision Repair

- Upstream commit `687f05cb94` removes `spawn_agents_on_csv`,
  `report_agent_job_result`, their coordinator/runtime state, and the
  `agent_jobs` / `agent_job_items` tables. The legacy feature and agent-config
  keys remain accepted as no-ops.
- Ordinary `spawn_agent`, MultiAgentV2, child model/reasoning selection, role
  skills, inventory, wait joins, dynamic tools, and native computer use are not
  part of the deleted CSV subsystem and remain intact.
- Preserve upstream `0042_drop_agent_jobs.sql` byte-for-byte. Move the
  already-shipped downstream `0042_external_agent_config_imports.sql` to
  `0047_external_agent_config_imports.sql`, then checksum-repair exact legacy
  records from `42` to `47` before SQLx applies upstream `42`.
- Upstream's provider-column migration for that table arrives at `0044`, which
  is already occupied by deployed downstream visible-sort indexes. Carry the
  SQL unchanged as `0049_external_agent_config_imports_provider_id.sql` so it
  follows table creation at `0047`. Checksum-repair only an exact upstream
  provider-column record from `44` to `49` before SQLx applies the downstream
  visible-sort migration at `44`; never restore the upstream filename.
- The production-path regression reconstructs exact `origin/main` through
  version `45`, simultaneously repairs remote-control-enabled `41 -> 46` and
  external-agent imports `42 -> 47` through `StateRuntime::init`, removes only
  the two job tables, and preserves thread rows, spawn edges, and the complete
  external-import record. Separate regressions prove a fresh database creates
  the import table before applying `0049`, and an upstream database that had
  provider history at `44` preserves both its provider value and checksum when
  repaired to `49`.
- The table drop intentionally discards unfinished CSV-job coordinator data.
  After both repairs and upstream migrations run, a pre-sync binary cannot
  reopen the database because it knows the former `41` and `42` checksums;
  rollback requires a pre-upgrade database copy.
- Hosted mirror run `29780661853` fast-forwarded `origin/upstream-main` to
  exact upstream `687f05cb94`. The sync job passed; the audit's exit `4` is the
  expected pre-promotion result while `origin/main` still points at the old
  downstream tree. Audit artifact `8476471215` has SHA-256
  `d03dd1c3b7ad0ad3bf4a1d6a3eca8e122d4cf5d385d9eb7e4688b53898dd432e`.

### Completed Hook Warning Headers

- Upstream commit `cf821e8ec8` renders the first line of a completed hook
  warning in the hook header after `says:` and indents any remaining lines.
  Warning entries are not repeated in the output body; hooks without warnings
  retain their existing headers.
- Downstream's compact transcript renderer shares the same completed-hook
  semantics while continuing to collapse only context entries. The holistic
  `verbose_transcript_preserves_hook_context_while_compact_collapses_it`
  regression protects rich, compact, and raw output together.
- Hosted mirror run `29781569004` fast-forwarded `origin/upstream-main` to
  exact upstream `cf821e8ec8`. Sync job `88483784931` passed. Audit job
  `88483884485` returned the expected pre-promotion exit `4` with
  `mirror=exact`; artifact `8476818381` has SHA-256
  `c9a3b5415b61aadc19153316fb9d3a9d0622ad63ab2940e16ca9378a178778e3`.
- Hosted generation run `29783168837` passed at temporary workflow head
  `321991090e` against exact integration source `74f7c82f02`. Artifact
  `8477717271` has archive SHA-256
  `81d9a3c91629578ac257281815c17e55b928c1a9c4fe50910204a07200ef9131`;
  its embedded 12-file patch has SHA-256
  `b13a993db6a856488fe060996de53641843125dc9c79dd8929faa558a8f69a68`.
  The run regenerated app-server and config schemas, the hook-browser snapshot,
  Rust formatting, and downstream table formatting, then passed both exact
  completed-hook regressions. Signed commit `3f15d104d7` records the verified
  artifact output without the temporary workflow.

### App-Read Connector Metadata And Auth Routing

- Upstream commit `60272096bc` enriches experimental `app/read` connector
  metadata with dark-icon URL aliases, distribution channel, install URL, and
  plugin display names. Plugin display names are derived from loaded plugin
  declarations without starting MCP servers.
- Downstream retains auth-dependent app/MCP routing. `app/read` synchronizes the
  shared `PluginsManager` from the same live ChatGPT auth snapshot used for the
  metadata request before it projects plugin names. This prevents an external
  auth transition or embedding-owned auth update from leaving the projection
  on an older API-key mode.
- `app_read_resynchronizes_plugin_auth_after_external_login_without_starting_mcp`
  covers an API-key-to-external-ChatGPT transition, sorted plugin names, and
  zero MCP startup requests. The complete `suite::v2::app_read::` group is part
  of `app-server-v2-contract-targeted`.
- Hosted mirror run `29784292249` fast-forwarded `origin/upstream-main` to exact
  upstream `60272096bc`; sync job `88492334147` passed and audit job
  `88492430174` returned the expected pre-promotion exit `4`. Artifact
  `8477855931` has SHA-256
  `a4a207bbae70b4f269b1ca605dd34622ddbdd268b1e50acbffe0ddf7d06e5c2c`.
- Hosted generation run `29784486643` passed all schema and focused app-read
  tests but failed closed because its disposable packager incorrectly required
  a non-empty patch. Corrected run `29785333279` passed at workflow head
  `ddbdf48e675460e04cd1b5238b00d87ac9b3b818` against source
  `4df3033de87cf8d3b2a716e3d01df75924106bcf`. Artifact `8478473413` has
  archive SHA-256
  `68685c9ab347da89c7145972fc504cacbbb9d46330231fe27d395bd2a59cfa1d`;
  its manifest records `changed_file_count=0` and the canonical empty-patch
  SHA-256 `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`.

### Exec-Server Windows Native Spawning

- Upstream commit `35c2278dd5` introduces the shared
  `codex_sandboxing::spawn_process` seam and routes executor-native pipe, PTY,
  inherited-file-descriptor, and Windows sandbox launches through it. The exec
  server now carries permission profiles, workspace roots, proxy state,
  filesystem overrides, sandbox level, and private-desktop selection into the
  canonical Windows session spawner.
- The only merge conflict was the obsolete downstream direct Windows launch
  block in `core/src/unified_exec/process_manager.rs`. Resolution adopts the
  upstream shared launcher and drops that duplicate block while preserving the
  downstream pre-spawn override preparation, proxy-aware effective-backend
  policy, metrics, telemetry, and unrelated bounded final-output carry.
- Hosted mirror run `29786347719` fast-forwarded `origin/upstream-main` to exact
  upstream `35c2278dd5`; sync job `88498617036` passed and audit job
  `88498704999` returned the expected pre-promotion exit `4`. Artifact
  `8478603020` has SHA-256
  `7eab307e0b280aca8b56b15cefce15e5f7b3af706b8af77c98d9c2ab0693b925`.
- Upstream's launcher API added inherited file descriptors but missed the Wine
  PTY regression call site. Signed downstream commit `932cbceeb8` passes the
  same empty descriptor slice used by the superseded wrapper, restoring the
  upstream Bazel target without changing runtime behavior. The same commit adds
  the exact optional-argument comment required by the fork's stricter Windows
  argument-comment lint. Drop each validation-only hunk independently when its
  upstream equivalent lands.

### Shared Skill Model Ownership

The public fork owns only agent workflow helpers that are coupled to Codex
workflow or session formats. `subagent-session-tail` remains here because its
parser follows rollout JSONL and usage-ledger contracts; its focused tests cover
timestamp handling and string, list, object, and scalar tool outputs. The former
repo-local `codex-orchestrator` and `sidecar-review-loop` bundles are retired:
semantic orchestration and review policy belongs in an external user policy
package with its own provenance and rollout controls. Do not restore those two
bundles during upstream sync merely because they once shared this carry entry.

- Upstream commit `56c11cf658` moves host and environment skill metadata,
  policy, dependencies, interface, and configuration-rule models into the
  lower-level `codex-skills` crate. `codex-core-skills` keeps compatibility
  re-exports, so existing consumers retain type identity rather than adding a
  downstream adapter.
- Implicit invocation still defaults to allowed unless explicitly disabled.
  Empty product restrictions remain unrestricted, while a non-empty product
  list still requires a matching product. Host and environment metadata now
  share that product-restriction implementation.
- The sole merge conflict was the import block in the downstream-expanded
  `core-plugins/src/manager_tests.rs`. Resolution keeps the still-used
  `PluginSkillSnapshots` compatibility import, drops two unused imports, and
  adopts upstream's canonical `codex_skills::SkillConfigRules` owner.
- Downstream preferred-user skill precedence, plugin auth projection, dynamic
  tools, and no-MCP-start app metadata behavior remain separate live carry.
  The relocation does not create a new downstream divergence.
- Upstream commit `6278742c41` keeps every skill-catalog entry visible under
  moderate metadata pressure by distributing the remaining description budget
  fairly, while retaining a bounded omission marker when even minimum entries
  cannot fit. Downstream adopts that renderer unchanged; the focused skill lane
  now runs its token, character, multibyte, omission, and fairness regressions
  so later syncs do not restore first-entry-wins truncation accidentally.
- Upstream commit `bd9a28a839` further preserves skill discoverability under
  extreme pressure by removing descriptions before omitting names and locators.
  Commit `2ffe8cd579` also makes core-compatible extension catalogs preserve
  core's system, admin, repo, and user ordering while extension-compatible
  catalogs retain insertion order; host entries carry their prompt scope into
  that decision. The same focused lane now pins both rendering policies, prompt
  scope propagation, and moderate and extreme pressure behavior.
- Upstream commit `f21f98936c` contains the exact extension-API fixture updates
  independently added in downstream commits `ec042322e2` and `ddb5443d28`:
  thread startup supplies the optional MCP resource client and context
  contribution no longer passes the removed step store. Treat those two local
  repairs as upstream-equivalent validation history, not live carry.
- Hosted mirror run `29921370577` advanced `origin/upstream-main` to exact
  upstream `65ae4c26e0` in successful sync job `88927457328`; audit job
  `88927594996` returned the expected pre-promotion exit `4`. Hosted exact-head
  validation run `29921594558` then passed at integration source
  `2ba1b403be`, including skill-catalog job `88928288442`, extension/core
  runtime job `88928288450`, and app-server protocol job `88928288507`.

### Remote Compaction History Efficiency

- Upstream commit `fd3c1dc13d` estimates remote-compaction history items once,
  updates the unclamped token total as trailing tool outputs are rewritten,
  and installs the replacement history only after the selected rewrites are
  complete. It also snapshots compaction input only when rollout tracing is
  enabled and reuses the v2 prompt input instead of cloning it.
- The compaction files were upstream-exact before this merge and merged without
  conflict. Downstream realtime world-state reconstruction, compaction hooks,
  capacity retry, turn metadata, and dynamic-media accounting remain on their
  existing seams and are not reclassified as part of this optimization.
- Hosted mirror run `29788160229` fast-forwarded `origin/upstream-main` to
  exact upstream `fd3c1dc13d`; sync job `88504053569` passed and audit job
  `88504128184` returned the expected pre-promotion exit `4`. Artifact
  `8479246296` was independently downloaded with SHA-256
  `65100c7f559bdb708290142ae7ff729b9f409e433841dbe660f5ef271b7bfdb1`;
  its embedded report records `mirror=upstream=fd3c1dc13d` and exact mirror
  health.
- Hosted exact-head proof run `29788805449` passed at disposable workflow head
  `bf26fbc18d` against source `c86d398de2`. Jobs `88505945020`,
  `88505945036`, and `88505945060` respectively passed the remote-compaction,
  shared-skill/plugin-auth, and formatting/generation fanout. Artifact
  `8479620777` has archive SHA-256
  `153794fd6d68a47b0a5fa7738fd21373b435e8909a25fc8a3019d841f1334fc6`;
  its allowlisted two-file patch has SHA-256
  `f467c47a91d4e6d278892d2f80a7347af550d7c6b1afae0b0cbd7e3e8adae491`
  and was applied exactly in signed commit `b350254be3`. The temporary remote
  branch, worktree, and local branch were then removed.

### Catalog Messages For Non-Request Approval Policies

- Upstream commit `2be7d3bcd9` adds model-catalog approval messages for `never`
  and `unless_trusted`, selects the variant matching the active policy, and
  retains the built-in text when that catalog key is absent. An explicitly
  empty value suppresses only the built-in approval section, matching existing
  `on_request` semantics.
- A selected catalog value is the complete approval-section replacement: it
  does not receive the built-in `request_permissions`, approved-prefix, or
  auto-review supplements. Preserve that upstream behavior so an explicit
  empty value continues to suppress the section rather than being repopulated
  downstream.
- The model metadata and prompt changes merged without conflict. The
  downstream Schemars 1.2 adapter remains the only local shape difference in
  `openai_models.rs`; the new fields and their deserialization behavior are
  otherwise upstream-exact. This changes model-visible instructions only, not
  approval enforcement, sandbox policy, or escalation authorization.

### Explicit Outbound Proxy Route Resolution

- Upstream commit `c9ef7eff00` resolves system-proxy failure into an explicit
  environment-proxy or direct route, carries `NO_PROXY` through HTTP and
  WebSocket connections, uses cached decisions before platform lookup, and
  provides an async resolver that serializes blocking Windows/macOS discovery
  outside Tokio workers. The current production WebSocket connector still uses
  the synchronous resolver, so this sync does not claim that live call path has
  moved off Tokio workers yet.
- The sole merge conflict was the proxy-cache digest renderer. Resolution takes
  upstream's complete route design and keeps the existing sha2 0.11-compatible
  explicit hexadecimal encoder. No proxy policy or credential-bearing value is
  changed or exposed by that compatibility hunk. Drop the encoder only when the
  downstream digest line again supports upstream's formatter or upstream adopts
  an equivalent explicit encoder that passes the locked Cargo and Bazel graphs.
- Hosted mirror run `29791336871` advanced `origin/upstream-main` to exact
  `c9ef7eff00`; sync job `88513605376` passed and audit job `88513679966`
  returned the expected pre-promotion exit `4`. Artifact `8480383914` has
  GitHub SHA-256
  `66c768664ada195eb2d39c23e2d3eb2e04ab34e8b01d6f61175b1f4392f08158`
  and records exact mirror health. Signed two-parent merge `bf736ed0f8`
  preserves `c9ef7eff00` as its second parent.

### Managed Permission Profile Network Proxy Resolution

- Upstream commit `88fac6fe10` merges permission profiles supplied by
  `requirements.toml` with configured profiles before resolving the selected
  profile's network proxy settings. It reuses the same fail-closed duplicate-ID
  handling and resolved-profile inheritance used by initial permission
  selection.
- This upstream lookup repair does not weaken managed constraints or broaden
  profile selection. Top-level network requirements are still applied after
  profile lookup, and downstream Windows carry begins only after the resulting
  `NetworkProxySpec` has been resolved.
- `system_requirements_define_managed_permission_profiles` and
  `turn_start_accepts_managed_network_profile_from_requirements` guard the core
  loader and app-server turn projection. The production hunk and both tests are
  upstream-exact in the integration tree.

### Patch Approval Test Stabilization

- Upstream commit `c0cd337766` gives the patch-approval test helper a
  15-second per-event silence timeout. It preserves the existing request,
  completion, call-ID, decision, and patch-application assertions and changes
  no runtime approval behavior.
- The effective Linux timeout increases from 10 to 15 seconds; the shared
  macOS floor remains 30 seconds, and the approvals suite remains excluded on
  Windows. Keep this as upstream-owned test stabilization rather than creating
  downstream timeout carry.
- Hosted mirror run `29792987228` advanced `origin/upstream-main` to exact
  `c0cd337766`; sync job `88518517425` passed and audit job `88518586023`
  returned the expected pre-promotion exit `4`. Artifact `8480980477` has
  GitHub SHA-256
  `f60f0297a8bd882869dffdc4277e48c7e589531bff81a44093119fe861a841b3`
  and records exact mirror health. Signed two-parent merge `e4d86fd279` has
  `c0cd337766` as its second parent.

### Buffered Code-Mode Exec Yields

- Upstream commit `99efeef650` adds the disabled-by-default experimental
  `code_mode_buffered_exec` feature. When enabled, a nested code-mode `exec`
  call that omits `yield_time_ms` uses 30 seconds instead of 10 seconds; an
  explicit caller value remains authoritative.
- The effective default is included in the model-visible code-mode declaration.
  Merge resolution passes the upstream feature set into `CodeModeService` while
  retaining downstream usage logging, audio, generated-image, native-tool, and
  `ALL_TOOLS` description plumbing.
- `code_mode_buffered_exec_updates_exec_description` is the upstream guardrail.
  Keep the feature disabled until hosted runtime coverage also proves the
  implicit default and explicit-value override paths.

### Route-Aware HTTP Client Pool

- Upstream commit `9078e32371` exports `RouteAwareClientPool`, resolves each
  exact request URL before sending, reuses clients by resolved route, and bounds
  the cache at 16 routes. `RespectSystemProxy` disables transport redirects so a
  redirected URL cannot silently reuse the original route.
- The pool retains custom CA handling, shared Cloudflare cookies, request
  logging controls, and tracing, but has no production consumer at this
  boundary. It is upstream-owned transport infrastructure, not new downstream
  carry. A later bounded harvest may move compatible downstream HTTP consumers
  behind it; this sync does not broaden into that migration.
- Guardrails are `request_builder_debug_redacts_url_secrets`,
  `forwards_exact_urls_and_reuses_clients_by_resolved_route`,
  `reqwest_default_route_preserves_transport_redirects`, and
  `bounds_cached_routes_and_rebuilds_an_evicted_route`.
- Hosted mirror run `29796559999` advanced `origin/upstream-main` to exact
  `9078e32371`; sync job `88528977294` passed and audit job `88529041993`
  returned the expected pre-promotion exit `4`. Artifact `8482198577` has
  GitHub SHA-256
  `6df324ab5247682493977483be5acb08762da8f528280ef14d894feaba486040`.
  Signed two-parent merge `00953dde39` preserves `9078e32371` as its second
  parent.

### External Session Limits And Import Attribution

- Upstream commit `3bc49e1721` adds optional `maxSessionAgeDays` and
  `maxSessions` detection inputs. Omission retains the 30-day and 50-session
  defaults; the selected limits are threaded through home-scoped CLA and CUR
  session discovery without replacing downstream repository containment or
  imported-session ledger carry.
- Upstream commit `a30aee8d90` adds optional `providerId` attribution to import
  completion and failure analytics independently of `migrationSource`; the TUI
  identifies its selected migration source as the provider.
- The current upstream tests retain default-limit behavior and prove provider
  attribution across completion, failure, reducer, and serialization paths.
  Custom, zero, and maximum-limit coverage and an ingress bound for arbitrary
  provider IDs remain harvest candidates rather than hidden sync-time carry.
- Hosted mirror run `29798068332` advanced `origin/upstream-main` to exact
  `a30aee8d90`; sync job `88533404997` passed and audit job `88533479849`
  returned pre-promotion exit `4` because current `origin/main` predates the
  integration registry update for the daemon PID test carry. Artifact
  `8482730398` has GitHub SHA-256
  `fc1118a981f67fa7b5fbf6cc5e32901661c10794890fa886dd3b099cbea77882`.
  Signed two-parent merge `8f866250fa` preserves `a30aee8d90` as its second
  parent.

### Alpha Hotfix Release Versions

- Upstream commit `9970cd706f` centralizes Python-to-Codex release-version
  conversion and supports alpha hotfix versions: Python `aN.postM` maps to a
  Codex `-alpha.N.M` tag. Rust, npm, Python-runtime, Windows, and zsh release
  workflows now consume that shared conversion, and both public installers
  accept the resulting tag shape.
- The only merge conflicts were in `scripts/install/install.sh` and
  `scripts/install/install.ps1`. Both retain the downstream general SemVer
  validator because it already accepts `-alpha.N.M` and also preserves Sedna's
  `-sedna.N` prerelease suffix and optional build metadata. Every other file,
  workflow, conversion rule, and test from `9970cd706f` is upstream-exact.
- Guardrails are `test_alpha_hotfix_release_is_valid`,
  `test_runtime_setup_reads_independent_runtime_pin_and_release_tags`,
  `test_normalize_codex_version_accepts_release_tags_and_pep440_versions`,
  `test_release_version_conversions_map_python_versions_to_codex_tags`, and
  `test_release_version_cli_writes_python_runtime_outputs`.
- Hosted mirror run `29802575358` advanced `origin/upstream-main` to exact
  `9970cd706f`; sync job `88546363775` passed and audit job `88546438886`
  returned the expected pre-promotion exit `4`. Artifact `8484273086` has
  GitHub SHA-256
  `9cd08b7ef8d2ff85a6daf0045447b92776144369986b67e0a4beea1c86e02b6e`.
  Signed two-parent merge `b8579e9d39` preserves `9970cd706f` as its second
  parent.

### Release Distribution And Proxy-Aware Plugin Transport

- Upstream commits `cc875d61ce` and `a148e0b50a` mirror verified Rust release
  artifacts and channel metadata to the upstream release distribution service.
  Upstream commits `94bb6a09a6` and `d937bfac84` route startup and remote plugin
  HTTP through system proxy settings. These are upstream-owned distribution and
  transport changes, not native browser or computer-use providers.
- The upstream dependency update exposed a stale `http 1.4.0` Bazel lock key
  while Cargo selected `1.4.2`. Hosted lock-generation run `29822424437`
  produced the one-line lock repair, and signed commit `d3fd36dfa0` carries
  exactly that generated result.
- Hosted mirror run `29821981506` advanced `origin/upstream-main` to exact
  `d937bfac84`; sync job `88606503410` passed. The audit job returned the
  expected pre-promotion exit `4`, with artifact `8491750051` preserving the
  exact-mirror and downstream-divergence evidence.

### Optional Upstream Installer Source And Wine PTY Contract

- Upstream commit `765675a122` adds an opt-in `releases.openai.com` metadata and
  asset source with GitHub fallback, verified binary-version checks, legacy
  package fallback, and matching POSIX and PowerShell installer behavior.
- Downstream keeps its existing repository and tag-prefix adapter as a narrow
  origin boundary. The upstream source is used unchanged for the default
  `openai/codex` plus `rust-v` origin; any configured repository or tag prefix
  bypasses the OpenAI-only channel and retains GitHub metadata and asset URLs.
  `test_custom_repository_and_tag_prefix_drive_latest_urls` enables the new
  upstream preference flag while proving that custom routing remains isolated.
- Upstream commit `7982aa27ff` only extends codespell's accepted vocabulary.
  Upstream commit `b9800de486` supplies the named empty `inherited_fds` argument
  in the Wine PTY test, superseding the earlier downstream uncommented call-site
  repair.
- Signed merge `bf5fe13611` preserves `b9800de486` as its second parent.
  Hosted mirror run `29826519611` fast-forwarded `origin/upstream-main` to that
  exact SHA in sync job `88620996988`. Audit job `88621089852` returned exit
  `4` only because the audited `origin/main` predates this integration branch's
  PID-test registry update; the artifact reports an exact usable mirror and no
  stale registry entries.

### Focused MCP Connection-Manager Modules

- Upstream commit `2d85e6d3a6` moves required-server startup validation and
  tool listing, lookup, metadata attachment, and Codex Apps cache refresh into
  focused `connection_manager/required.rs` and
  `connection_manager/tool_catalog.rs` modules without changing the public
  manager API or intended behavior.
- Conflict resolution adopts that upstream structure and relocates only the
  existing downstream generation-aware catalogue adapter: live lookup awaits
  the atomic catalogue snapshot, while hard refresh begins and publishes the
  shared connector and regular-cache fetch tickets through `ManagedClient`.
  No duplicate parent-module implementation remains.
- This extraction is a useful seam for the separately tracked stale-MCP
  lifecycle work, but it does not itself unload a server or add a native tool
  provider. Signed merge `cd759ef851` preserves `2d85e6d3a6` as its second
  parent.
- Hosted mirror run `29827916758` advanced `origin/upstream-main` to exact
  `2d85e6d3a6` in sync job `88625450309`. Audit job `88625559435` returned the
  expected pre-promotion exit `4`; artifact `8494064503` reports no stale
  registry entries and only the already-covered integration-branch PID-test
  path missing from the older audited `origin/main`.

### Explicit Extension Contributor Capability Ownership

- Upstream commit `c44c4de7b4` introduced a per-sampling-step `ExtensionData`
  argument, but upstream commit `fd51e50540` removes it after the store remained
  an empty forwarding container. Context, world-state, turn-input, and tool
  contributors now receive only the session, thread, and turn stores whose
  lifetimes match the data they own.
- MCP resource access is no longer hidden in a generic store. The host passes an
  optional live-runtime `McpResourceClient` through `ThreadStartInput`, and the
  skills extension retains it in skills-owned session state for catalog and
  tool operations.
- The two 2026-07-22 session conflicts keep the downstream helper for shared
  turn-context contribution assembly on upstream's no-step-store API. Full
  initial context and steady-state updates still invoke the same helper, and
  the test-only contributor keeps the local `ExtensionFuture` alias without a
  dead capability parameter.
- Dynamic Tools, image generation and image detail, memories, goals, web
  search, realtime world state, and native computer use remain owned by their
  existing session, thread, turn, or tool-handler seams. Do not introduce a new
  step-local registry for them without a concrete request-stability need.
- The 2026-07-24 integration adopts upstream `7c71783135` and `fe8500c0a0`.
  Executor skill tools receive only a narrow per-sampling-step authority view
  built from the selected capability roots. `skills.list` and `skills.read`
  paginate bounded executor-owned data, and an explicitly selected executor
  skill may pass package-scoped `resource_access` metadata to read a referenced
  resource. The provider validates authority, package, resource, cursor, and
  package containment before it reads. This is upstream-owned executor-skill
  behavior, not a new shared capability store for Dynamic Tools, image
  plumbing, realtime world state, or native computer use.
- Signed merge `3fe6502c41` preserves `c44c4de7b4` as its second parent.
  Hosted mirror run `29828505403` advanced `origin/upstream-main` to that exact
  SHA in sync job `88627328251`. Audit job `88627431159` returned the expected
  pre-promotion exit `4`; artifact `8494287133` reports no stale registry
  entries and only the already-covered integration-branch PID-test path missing
  from the older audited `origin/main`.

### Centralized Compacted Rollout Item Construction

- Upstream commit `f69f88f811` centralizes persisted `CompactedItem`
  construction inside `Session::replace_compacted_history`, after missing
  response-item IDs have been assigned. Local, remote, remote-v2, and
  token-budget compaction now pass only the compaction message and window
  metadata, so persisted replacement history is built from the exact live
  history rather than cloned independently by each caller.
- Signed merge `7e09b99f60` adopts the upstream implementation without a
  carry-specific patch. Downstream capacity retry, realtime world-state,
  hooks, dynamic media, compaction metadata, and explicit session/thread/turn
  extension state remain composed around the centralized upstream boundary.
- Hosted mirror run `29832500831` advanced `origin/upstream-main` to exact
  `f69f88f811` in successful sync job `88640491987`. Audit job `88640608492`
  returned the expected pre-promotion exit `4`; artifact `8495902896` reports
  an exact mirror, no stale registry entries, and only the already-registered
  integration-branch PID-test path absent from the older audited
  `origin/main`.

### Git Attribution World State And Exec-Server Policy Callbacks

- Upstream commit `ab816f3ca0` adds an extension-owned git-attribution world
  state contributor. It resolves and caches the workspace policy by
  authentication generation, replaces legacy or stale instruction fragments,
  retries after authorization refresh, and fails disabled on timeout or
  settings errors. The new crate is registered in the workspace without adding
  a downstream adapter or moving Dynamic Tools, image plumbing, realtime world
  state, or native computer use into a hot core path.
- Upstream commit `32f4687b8c` lets exec-server issue bounded reverse JSON-RPC
  requests for network policy decisions. Response correlation, request limits,
  timeouts, disconnect and process cleanup, malformed-input denial, and
  inherited-stream lifetime handling are upstream-owned and fail closed.
- The integration merge was clean. Cargo and Bazel metadata retained the exact
  upstream crate shape, and no downstream production patch was added. Generic
  callback transport is not a native browser, Android, or desktop tool handler,
  and it does not replace downstream Windows effective-backend selection,
  filesystem override preparation, telemetry, or firewall enforcement.
- Signed merge `7036a16d3a` preserves `32f4687b8c` as its second parent. Signed
  follow-up `c148908ba6` extends existing hosted recipes to run the complete
  exec-server and git-attribution unit seams rather than pinning a brittle list
  of individual upstream tests.
- Hosted mirror run `29933738199` advanced `origin/upstream-main` to exact
  `32f4687b8c` in successful sync job `88969800383`. Audit job `88969938211`
  returned the expected pre-promotion exit `4` and uploaded its artifact; this
  records intentional live downstream divergence rather than a mirror failure.

### TUI Presentation And Turn Completion Convergence

- Upstream commits `9ee63da142`, `c00e2e851c`, and `730ec92003` respectively
  size unified mention popups from visible results, normalize whitespace-only
  lines in agent messages, and clamp session headers at narrow terminal widths.
  Upstream commit `ff8d521ba1` coalesces wrapped cells with one OSC 8 destination
  into a single terminal hyperlink and closes it before ordinary content.
- Upstream commit `80f3c3141e` includes the last non-empty final agent message in
  turn-completion summaries. App-server, exec, and TUI consumers can therefore
  repair dropped message deltas while avoiding duplicate item rendering and
  preserving a full persisted backfill when the summary is not the full view.
- Five conflict files were resolved by composition. App-server completion keeps
  downstream provider-confirmed model metadata alongside upstream summary
  items. TUI completion keeps downstream model-status refresh, compaction
  status, cyber-policy retry reset, and ratatui color conversion while adopting
  upstream summary replay, delta repair, and hyperlink coalescing. Both Guardian
  and upstream agent-message test imports remain; no broad ours/theirs strategy
  was used.
- Signed merge `c78f0b4bec` preserves `ff8d521ba1` as its second parent. Hosted
  targeted run `29934195127` passed workflow sanity, exec-server, and bounded
  unified-exec jobs, then exposed one stale upstream git-attribution test-helper
  field in core-runtime job `88971494184`. Signed test-only repair `03886698f2`
  removes that obsolete field without changing production extension behavior.
- Signed CI follow-up `3056f35a29` adds the upstream completion-summary,
  whitespace, narrow-header, OSC 8 hyperlink, and visible mention-popup tests to
  the existing `codex.tui-transcript-viewport-targeted` lane. This keeps future
  additions on one maintained TUI rendering seam rather than creating another
  lane.
- Hosted mirror run `29935755138` advanced `origin/upstream-main` to exact
  `ff8d521ba1` in successful sync job `88976767550`. Audit job `88976904847`
  returned the expected pre-promotion exit `4`.

### Live Parent Fork Context And Bazel Dependency Upgrade

- Upstream commit `c5779ed6bb` requires a live parent before forking and reads
  the parent's active history mode and MultiAgentV2 usage hints from that live
  thread. Downstream adopts that authority without weakening its independent
  child `model`, `model_provider`, or reasoning-setting selection and restore
  contracts.
- Upstream commit `cc559bb971` upgrades `rules_rs` and LLVM, adds native
  Windows GNU-LLVM argument-lint execution, moves `aws-lc-sys` to the Bazel
  Central Registry integration, and retires the superseded crate and Windows
  toolchain patches. Resolution took upstream `MODULE.bazel` and deleted the
  obsolete patch files instead of preserving downstream patch debt.
- `MODULE.bazel.lock` was never unioned by hand. GitHub-hosted run
  `29938097805` regenerated it from scratch at temporary signed workflow head
  `895b811bb9` against exact integration source `55bdfca770`. Artifact
  `8537022018` has GitHub archive digest
  `f090624a0dc8a410ab61b29344b6912c9047c793e0a4433672340f72d7fb1548`;
  its embedded patch has SHA-256
  `389edb84304a863bd94f47a46c56cfcb7df3e33c609470a2dbd51a5e5ba18231`
  and patch ID `94bcf677420d96bbc493766e5fa60f4f40c2825a`. The only generated
  file was `MODULE.bazel.lock`; its final SHA-256 is
  `f64831f7dac730abeeae28c84b4801890b3db224010051e0269b5f390df0fa0d`.
  Signed commit `dd42b37482` records exactly that output. Future Bazel module
  conflicts must repeat this hosted from-scratch regeneration path rather than
  carrying a merge placeholder.
- Signed two-parent merge `55bdfca770` preserves exact upstream
  `cc559bb971` as its second parent and keeps the live-parent history change in
  upstream shape.

### Reserved Environment IDs, Skill Reports, And App-Server Test Helpers

- Upstream commit `44436fd075` rejects the reserved dynamic-environment id
  `local` in the shared validator, preventing a remote registration from
  replacing the environment owned by `EnvironmentManager`. The existing
  complete `codex.exec-server-targeted` unit seam covers this upstream policy.
- Upstream commit `fe6aa9d16c` returns a `SkillRenderReport` even when metadata
  pressure prevents a catalog fragment, including included, omitted, and
  truncated-description counts. Downstream adopts the renderer unchanged and
  extends the existing skill lane with the partial-truncation and no-fragment
  report regressions rather than creating another lane.
- Upstream commit `10cc57c95c` centralizes typed app-server initialization,
  request, response, notification, thread-start, and mock-response helpers.
  Three downstream-expanded tests were composed onto those helpers: app/read
  still covers auth resynchronization without MCP startup, MCP status retains
  its slow-inventory budget, and thread resume retains the full persisted
  goal/fork/model/provider/reasoning/reviewer/permission/cwd contract.
- Signed two-parent merge `d8e3554223` preserves exact upstream
  `10cc57c95c` as its second parent. Signed follow-up `0c0249b14a` adds the
  slow-inventory, resume-settings, and skill-report guards to the existing
  app-server and skill lanes; no new workflow or production adapter was added.
- Exact-head targeted run `29940786871` found five retained downstream tests
  that still called helpers removed by the upstream migration. Signed commit
  `bde9e26567` moves those tests onto `MockResponsesConfig`, typed
  `read_response`, and typed `read_notification` rather than restoring the old
  parsing helpers. Exact-head hosted run `29942410506` passed both
  `codex.app-server-v2-contract-targeted` and
  `core-runtime-surface-smoke` on that repair.
- Hosted mirror run `29938416267` advanced `origin/upstream-main` to exact
  `10cc57c95c` in successful sync job `88985838499`. Audit job `88985969985`
  returned expected pre-promotion exit `4` because `origin/main` still held
  the older downstream tree; the mirror itself remained exact.

### Exact-Head Compatibility Findings Before The Final Checkpoint

- Targeted hosted run `29936369078` at source `4bbbb09d3f` exposed two narrow
  integration defects: a downstream TUI completion-event fixture omitted new
  provider-confirmed completion fields, and auth observers were gated by
  coarse `CodexAuth` equality that collapses managed ChatGPT accounts to auth
  mode. The latter allowed account-only changes to miss notification.
- Signed test-only commit `ebf7122a92` initializes `final_model` and
  `model_snapshot` to `None` in the downstream fixture. Signed production
  repair `36da0efb9c` restores the account/token-aware
  `auth_changed_for_refresh` notification predicate. That predicate is the
  correct broader observer decision here; it must not be replaced by coarse
  auth-mode equality during future syncs.

### Lazy Post-Sampling Token Estimate

- Upstream commit `66bd101fff` moves the expensive post-sampling token estimate
  to its own trace target and calculates it only when an explicitly configured
  subscriber enables that event. The always-on feedback and state-log sinks
  disable the target while retaining the ordinary post-sampling diagnostic.
- The only textual conflict was the declaration insertion point in
  `codex-rs/core/src/session/turn.rs`. Resolution keeps both downstream's cached
  endpoint-recommended-plugin candidates and upstream's trace-target constant.
  The runtime hunk otherwise adopts upstream's lazy estimate while preserving
  downstream provider-confirmed response-model identity and plugin guidance.
- Signed two-parent merge `0bf7a1075b` preserves exact upstream
  `66bd101fff` as its second parent. Signed validation commits `ded17bfd46` and
  `f0df4fc3df` add the live-parent fork and post-sampling trace tests to the
  existing sub-agent and core-runtime lanes rather than creating new lanes.
- Hosted mirror run `29940208004` advanced `origin/upstream-main` to exact
  `66bd101fff` in its successful sync job. The audit returned expected
  pre-promotion exit `4` with `mirror=exact, tree_equal=False`; the clean visible
  local `upstream-main` worktree was then fast-forwarded to the same exact SHA.

### Core-Compatible Skill Catalogs And Long-Line Highlighting

- Upstream `2c49493b5b` removes the obsolete step store from git-attribution
  tests, matching the already-integrated downstream extension-input shape.
- Upstream `5381edb133` skips syntax highlighting when one input line exceeds
  4 KiB while preserving the existing whole-input limits and plain-text
  fallback. The downstream `ExecCall` fixture requires only
  `terminal_wait: None`; the runtime and snapshot stay upstream-owned.
- Upstream `f343d1237d` suppresses skill omission notices for core-compatible
  catalogs while preserving `SkillRenderReport` counts for both rendering
  policies. The existing skill lane follows the renamed policy-aware test and
  keeps the no-fragment report guard.
- Hosted mirror run `29943868334` advanced `origin/upstream-main` to exact
  `f343d1237d`; sync job `89004234423` passed and audit job `89004349014`
  returned the expected pre-promotion exit `4`. Signed two-parent merge
  `c40e573af9` preserves exact upstream `f343d1237d` as its second parent.
- Signed validation repair `c132d50f17` supplies the downstream fixture field,
  updates the renamed skill test selector, and adds both long-line TUI tests to
  `tui-transcript-viewport-targeted`. No upstream production hunk was changed.
- Exact-head hosted run `29944328610` passed the TUI transcript/viewport,
  skill-loader fixture, core-runtime surface, workflow planner, and downstream
  documentation lanes at signed source `ef4389f342`.

### Sandbox Templates, Thread Startup, And Realtime BEM Prefixes

- Hosted mirror run `29952957863` advanced `origin/upstream-main` to exact
  `4ebd976312`; sync job `89034892597` passed and audit job `89035341843`
  returned the expected pre-promotion exit `4`. The clean visible local
  `upstream-main` worktree was fast-forwarded to the same exact SHA.
- Upstream `06782eded7` canonicalizes the built-in sandbox permission-template
  placeholder as `{{ network_access }}`. Downstream adopts the three template
  changes unchanged; the shared renderer already accepts both spellings, and
  no enforcement, approval, or network-authorization behavior changes.
- Upstream `08ae0fc0ce` consolidates thread startup on
  `ThreadManager::start_thread(StartThreadOptions)`. The merge does not restore
  the removed helper APIs. Downstream flat dynamic-tool compatibility records
  enter through `StartThreadOptions::dynamic_tools`; explicit empty environment
  selections remain distinct from omitted selections; child model, provider,
  reasoning effort, fork history, residency, cold reload, and persisted identity
  remain on their existing agent-control seams.
- Upstream `4ebd976312` adds configurable text prefixes for V3 Frameless Bidi
  BEM handoff channels. Downstream adopts its protocol, parser, buffering, and
  app-server routing. This adds no native browser, Android, desktop,
  dynamic-tool, image, or audio provider; downstream custom realtime world
  state remains orthogonal.
- Signed two-parent merge `e054a14ce3` has first parent `de2ba7d16b` and exact
  upstream second parent `4ebd976312`. The only textual conflicts were
  `codex-rs/core/src/tools/handlers/multi_agents_tests.rs` and
  `codex-rs/core/tests/suite/code_mode.rs`; both were resolved upstream-first
  without a blanket side-selection strategy.
- Existing validation recipes were strengthened rather than creating parallel
  workflow lanes. They now select paginated child-setting reload, hidden and
  nested-namespace dynamic-tool behavior, six BEM parser/streaming regressions,
  the core streamed V3 handoff test, and the app-server V3 routing test.
- Exact-head targeted run `29954085793` passed the other six selected lanes and
  found one shared blocker in the two app-server lanes: the vendored
  `ClientRequest.json` did not contain the newly generated BEM-prefix field.
  Disposable GitHub-hosted generator run `29955881580` produced artifact
  `8544097360`, whose archive digest is
  `96de8ad9f6a8a6292ba100e114a25fb59d13ab2d8943e12acf89b411d2c57358`.
  Its two-file patch has SHA-256
  `b2c8df873b2268925f26dc0a04aa09f942b9e54b611d5d232ee555cd83969637`
  and changes only `ClientRequest.json` plus the generator's canonical trailing
  newline in `ConfigRequirementsReadResponse.json`. Signed commit `ce7c9f9815`
  records that exact output; neither schema was hand-edited or generated
  locally. Exact-head validation run `29956395279` passed both previously
  failing app-server lanes at that repair.

### Attribution Entry Points, Code-Mode CI, Guardian Paths, And Response Requests

- Hosted mirror run `29957773023` advanced `origin/upstream-main` to exact
  `12c115d558`; sync job `89051059848` passed and audit job `89051332617`
  returned the expected pre-promotion exit `4`. The clean visible local
  `upstream-main` worktree was fast-forwarded to the same SHA.
- Upstream `88eb3a2b8a` installs authenticated workspace-policy git attribution
  in app-server, MCP server, and CLI prompt-debug entry points. Downstream keeps
  its app-server hook seam beside that upstream contributor; process-level
  `chatgpt_base_url` remains the policy authority, and attribution fragments
  remain contextual developer state. Upstream `2c49493b5b` had already made the
  prior two-line test-fixture carry equivalent, and `88eb3a2b8a` removes the
  obsolete contributor-composition helper and test entirely.
- Upstream `7fd7a2f9a2` removes the broad non-Windows Bazel exclusion for the
  code-mode integration suite while retaining the two narrow Windows
  exclusions. This increases hosted coverage of downstream native-browser image
  forwarding and flat dynamic-tool metadata without adding or changing a tool
  provider.
- Upstream `bbfc3f0152` stores Guardian review cwd reuse identity as `PathUri`.
  Model, provider, reasoning, permissions, MCP configuration, and the other
  reuse-key fields remain independent invalidation boundaries; downstream split
  sandbox-policy coverage remains orthogonal.
- Upstream `12c115d558` serializes Responses tool definitions once as shared raw
  JSON and compares incremental WebSocket prefixes in place. The merged tree
  preserves downstream provider-confirmed response identity, receiver retention,
  retry behavior, native computer-use exports, dynamic-tool schemas, and image
  items. This is allocation and request-construction work, not a new capability.
- Signed two-parent merge `f7165094c8` has first parent `1c186d9664` and exact
  upstream second parent `12c115d558`; it merged without textual conflicts.
  Signed validation commit `0ecb2c0f02` extends existing targeted recipes over
  attribution entry points, Guardian reuse identity, raw Responses tool JSON,
  and the newly unskipped code-mode carry seams. No parallel workflow or
  production adapter was added.

### Analytics Flush, Interrupted MCP Startup, Thread Pinning, And App Metadata

- Hosted mirror run `29967541744` advanced `origin/upstream-main` to exact
  `79500d3cc1`; its sync job `89082210987` passed. The downstream audit job
  returned the expected pre-promotion divergence result.
- Upstream `88f1cd9664` flushes analytics after the in-process app-server
  runtime drain, `bd5b55e403` records compaction time in turn profiles, and
  `d7e8f4c3dc` retains submitted user input when MCP startup is interrupted.
  The latter retains one consistent tool-router snapshot, so existing dynamic
  tools and native computer-use providers keep their advertised/executable
  alignment.
- Upstream `400ee190c3` persists app-server `is_pinned` metadata, filters it
  during listing, and preserves it through reconciliation, archive, and
  unarchive paths. Merge resolution keeps that upstream field beside the
  independent downstream `model`, `reasoning_effort`, `thread_source`, and
  `history_mode` metadata in protocol schemas, tests, and replay paths.
- Upstream `79500d3cc` removes `first_party_type` from the current Rust/JSON/
  TypeScript app metadata contract. The checked-in Python SDK remains generated
  from its intentionally pinned released `openai-codex-cli-bin==0.144.4`
  runtime; it is not a source-head schema artifact and must only change with a
  reviewed Python runtime-pin release.
- Signed two-parent merge `680fd3386d` has first parent `444e3b92fe` and exact
  upstream second parent `79500d3cc1`. It resolves every generated-schema
  overlap by retaining both independent fields, never by selecting one parent
  wholesale. Existing targeted lanes now include the upstream interrupted-MCP
  history regression and pinned filtered-pagination regression.

## Current Live Divergences

### Fork Workflow And Validation Policy

- `main` is now the default PR and integration branch, while `upstream-main`
  is the exact upstream mirror.
- Downstream sync policy is merge-based, not rebase-based.
- Upstream now groups PR-blocking checks through reusable leaf workflows called
  by `blocking-ci.yml`. Downstream preserves that upstream topology and carries
  only the wrapper entrypoint expansion for `merge_group` and `upstream-main`
  pushes, instead of reintroducing direct triggers on every child workflow.
- The protected queue requires `CI required` from `blocking-ci.yml` and
  `CodeQL required gate` from `codeql.yml`; both workflows run against the
  synthetic `merge_group` commit for `main`.
- Hosted Rust archive builders reclaim common Linux runner disk headroom before
  `cargo nextest archive`, build the `ci-test` archive payload with debug info
  disabled and symbols stripped, stay archive-only, and leave test execution
  to the archive-consuming `tests` and `remote_tests` jobs. Those replay jobs also
  install `bubblewrap` and reclaim hosted disk before archive extraction so
  sandbox and remote replay failures are not artifacts of runner packaging or
  disk pressure. The `remote_tests` replay job builds its Docker remote-env
  Codex binary in an isolated temporary Cargo target directory and removes
  those host-side build artifacts before replaying the shared nextest archive,
  so remote-env setup does not consume the extraction headroom needed by the
  227-binary archive. The archive uses default Cargo features, matching upstream
  and the non-sandbox V8 release artifact; explicit sandbox coverage remains in
  `v8-canary`. The `remote_tests` replay job keeps a 45-minute hosted budget so
  long archive download and remote-environment setup time does not masquerade
  as a product failure. Remote replay skips host-only compact/resume, hook, and
  forced-`rm` shell-safety/approval fixtures, while Guardian's local proxy
  fixtures use a host-native cwd. The large-output summary remains host-only
  until exec-server replay preserves
  bounded head, tail, and omission metadata before core subscribes. The
  `codex.skill-loader-fixture-hermeticity-targeted` lane pins the two
  skill-loader fixture assertions that suppress or ignore ambient parent
  project layers, so hosted-runner repository markers cannot alter the result.
  It also pins upstream skill-report accounting when descriptions are partially
  truncated or no rendered fragment fits the catalog budget.
  The rust-ci-full summary parser records final nextest
  retry statuses so `TRY 1 FAIL` followed by `TRY 2 PASS` does not block, while
  persistent `TRY 2 FAIL` / `TRY 2 TIMEOUT` lines still appear in structured
  harvest artifacts. Validation-lab Rust batches reclaim target artifacts before
  the first lane and between later lanes when hosted disk falls below the safety
  floor, the link-heavy native computer-use tool-registry lane is weighted as a
  singleton batch, archive jobs skip sccache, and validation-lab Rust batches
  retry once on narrow Cargo registry transport failures such as crates.io
  HTTP/2 or EOF download flakes, and argument-comment lint retries once on the
  same narrow Cargo metadata/fetch failure class before reporting a lint blocker.
  Large validation-lab plans keep their resolved metadata in a runner-temp
  JSON file for per-field parsing. The live workflow feeds that file to the
  fingerprint helper through stdin instead of granting the helper a
  caller-selected path or placing the complete plan in argv or the process
  environment. The helper retains environment input only for legacy direct
  callers; workflow wiring must preserve stdin until those callers are migrated.
  This keeps `full`, `broad`, and Frontier Max dispatches below host exec limits
  without widening the helper's path authority.
  CodeQL's compare-API diff discovery stops when a pull request reaches 300
  changed files. For large upstream-sync pull requests, downstream restores the
  same diff-informed query restriction after CodeQL initialization by deriving
  the complete added/modified line ranges from the runner's checked-out Git
  history. This avoids treating unchanged base alerts as new without suppressing
  queries, dismissing the base backlog, or weakening normal pull-request scans.
  Runtime
  permission policy keeps the configured `codex_linux_sandbox_exe` readable
  under restricted filesystem profiles so GitHub-hosted archived nextest runs
  can re-enter the sandbox helper from extracted test binaries; the Linux bwrap
  launch path also adds the helper directory and `:minimal` system runtime roots
  to the outer bootstrap filesystem view before re-entering the inner seccomp
  stage. After upstream `87f71e35b8` added optional missing-path behavior, the
  synthetic `:minimal` bootstrap root is constructed with the upstream default
  (`None`), not `skip`, so a missing mandatory runtime substrate remains a
  fail-closed sandbox setup error. Carried core and sandbox test fixtures also
  set `missing_path_behavior: None` explicitly; the app-server projection
  intentionally remains its stable two-field API. `skip` remains limited to
  upstream's deliberate metadata-protection cases. The downstream
  `codex-memories-write`, `codex-mcp-server`, and the `codex` and
  `codex-responses-api-proxy` release binary roots keep the workspace-standard
  `#![recursion_limit = "256"]`: upstream's deeper startup-prewarm async
  instrumentation otherwise exceeds the compiler's default query depth in the
  GitHub-hosted locked release build. GitHub validation-lab release sentinel
  `29893833019` proved the complete root set on signed head `b637026177`.
  Frontier Max harvest `29896241048` then exposed two upstream-generated seam
  transitions: `interrupt_tool_records_history_entries` now checks the stable
  serialized abort marker while `tools::parallel` owns the exact message
  formatting, and `config.schema.json` tracks the generator's nullable
  `filters`, optional login-shell default, and flattened paragraph
  descriptions. Targeted hosted repair `29903579393` proved both affected
  lanes on signed head `bebe996608`. The
  2026-07-22 Frontier Max harvest `29922887214` passed every selected lane
  except `codex.workflow-ci-sanity` at integration head `859d82a942`; that
  sentinel found a stale exact command-count assertion after four guarded
  unified-exec regressions were added. The repaired planner assertion verifies
  that every unified-exec Cargo command carries `RUST_MIN_STACK` without
  coupling the check to the recipe's size, while mixed-sensitivity recipes keep
  their narrower counts. Exact-head targeted run `29927093758` passed the
  workflow sentinel in job `88947042183` at signed head `6c4ab0214b`. The
  workspace JWT dependency uses `jsonwebtoken` with the
  `aws_lc_rs` provider so hosted Cargo/Bazel `--locked` runs avoid pulling the
  RustCrypto RSA graph. After the `a26bc337cf` upstream merge, the combined
  workspace lock was regenerated by GitHub-hosted validation-lab run
  `29886877350` from the exact scratch target `5cb0d70462`. Its raw resolver
  output was `fc1f9e41352e2aa5fd223e8b0220df8cd6574d154f87ca494fbbddc9d9d4dd9c`;
  the final carried lock is
  `5733a67daee4f971afe48a3143e680168abc0538d3a95e8a9aeca2626be5ea00`.
  Future manifest/lock conflicts must use the same hosted resolver rather than
  hand-merging package entries. The resolver output still needs its locked
  compatibility check: retain one compatible
  `rama-*` `0.3.0-alpha.4` family: permissive transitive prerelease ranges can
  otherwise select the incompatible stable `rama-error`, `rama-macros`, and
  `rama-utils` `0.3.0` releases. Downstream dependency-policy validation preserves
  upstream's current `quick-xml` advisory shape: the direct workspace
  dependency is on the fixed line, and the remaining trusted transitive
  `plist`/`syntect` and `wayland-scanner`/`arboard` paths keep synchronized
  RustSec exceptions in `deny.toml` and `.cargo/audit.toml` until those
  upstream crates can use `quick-xml >=0.41.0`. The reqwest ownership ratchet
  names `codex-android-computer-use` as temporary downstream migration debt
  until its MCP transport moves behind `codex-http-client`; other new direct
  reqwest owners remain denied. Hosted macOS V8 staging, Bazel
  clippy, and Bazel
  release-build verification keep fanout below runner process/thread ceilings.
  Python SDK runtime-package staging rejects archive traversal, links, and
  special entries before writing ordinary package files beneath the staging
  root, without an unfiltered compatibility fallback on older Python runtimes.
  The Python SDK schema-generation development tool is pinned to
  `datamodel-code-generator==0.71.0`; retain that dependency-only carry until
  upstream adopts an equivalent or newer compatible pin, and prove changes
  through the hosted Python SDK and downstream-divergence lanes.
  Hosted frontier argument-comment lint uses the prebuilt linter package so
  cold validation-lab runs do not spend the lane compiling V8/ICU before
  linting ordinary Rust call sites; V8 proof-of-concept buildability remains
  covered by build/test workflows. Hosted `rust-ci` callers pass `GH_TOKEN`
  through to the composite action so DotSlash can use authenticated
  `gh release download` fallback on Windows. Direct-runtime permission profiles
  stay on the bubblewrap/seccomp
  enforcement path when legacy Landlock is configured so sandbox validation
  fails safely instead of weakening policy. TUI carry smoke uses the same
  hosted test stack floor as core carry smoke so frontier/checkpoint validation
  can stay on GitHub hosted compute instead of falling back to local compute.
  Remote executor sweeps skip host-local managed-network approval and denial
  fixtures until the remote harness provides a proxy endpoint reachable from the
  target process; environment-specific approval scoping remains covered by unit
  tests and host-local integration. Compact/resume rollback fixtures remain
  host-only, wait for the thread-idle lifecycle before rollback admission, and
  correlate typed rollback errors to the exact submission under a short
  phase-labelled deadline. This isolates the fixtures from the terminal-event
  visibility window without claiming the production persist/clear/deliver
  ordering issue is resolved.
- The Bazel crate macro accepts and forwards optional unit-test arguments so
  upstream's serialized exec-server unit-test declaration remains analyzable
  until equivalent macro support lands upstream.
- Windows hosted setup prefers a real Dev Drive but falls back to an existing
  secondary or system volume when the runner image lacks Dev Drive formatting,
  so validation does not fail before the requested command starts.
- Windows Bazel shards serialize local test actions because sandbox identities,
  ACLs, and firewall rules are host-global; concurrent policy tests can
  otherwise invalidate one another and create false allow/deny results. The
  Bazel CI wrapper applies its selected rc config before explicit caller flags,
  so the workflow's `--local_test_jobs=1` limit cannot be silently reset by the
  Windows cross-build config.
- Helper-backed local validation and release flows may be used when configured,
  but those presets are not a tracked repository contract.
- Divergence regression ownership is tracked in
  [`downstream-regression-matrix.md`](downstream-regression-matrix.md).
- Field-level native tool-surface deltas are summarized in
  [`downstream-tool-surface-matrix.md`](downstream-tool-surface-matrix.md).
- Future registry-plus-generation maintenance direction is captured in
  [`downstream-divergence-tracking.md`](downstream-divergence-tracking.md).
- Downstream guidance prefers MCP tool surfaces with blocking wait
  semantics over transcript-driven polling when the tool contract supports it.
- Primary files:
  - `.github/scripts/run_validation_lane_batch.py`
  - `.github/scripts/prepare_codeql_diff_ranges.py`
  - `.github/scripts/rusty_v8_bazel.py`
  - `.github/scripts/test_ci_planners.py`
  - `.github/workflows/blocking-ci.yml`
  - `.github/workflows/bazel.yml`
  - `.github/workflows/codeql.yml`
  - `.github/workflows/rust-ci-full.yml`
  - `.github/workflows/v8-canary.yml`
  - `defs.bzl`
  - `codex-rs/core/src/config/mod.rs`
  - `codex-rs/core/src/config/permissions.rs`
  - `codex-rs/core/src/config/permissions_tests.rs`
  - `codex-rs/linux-sandbox/src/linux_run_main.rs`
  - `codex-rs/Cargo.toml`
  - `codex-rs/Cargo.lock`
  - `docs/contributing.md`
  - `docs/downstream.md`

### Git Attribution Extension-API Test Alignment

- Upstream `fd51e50540` removed step-scoped contributor data before upstream
  `ab816f3ca0` added a git-attribution test helper that still constructed and
  passed `WorldStateContributionInput.step_store`. The resulting crate test did
  not compile at the combined upstream head.
- Signed carry `03886698f2` deletes only that unused test store and initializer
  field. Production git-attribution policy resolution, cache behavior, world
  state, and retry semantics remain byte-for-byte upstream.
- `core-runtime-surface-smoke` runs the whole `codex-git-attribution` library
  test seam, so compile compatibility and all three upstream behavior tests are
  proved together. Remove this two-line carry as soon as upstream drops the
  stale field or otherwise supplies equivalent no-step-store fixture coverage.
- Hosted run `29936369078` proved that the complete seam also catches auth
  observer regressions: its unauthorized-recovery coverage exposed the coarse
  equality gate repaired by signed commit `36da0efb9c`. That production repair
  belongs to the shared auth-observer contract, not to this test-only fixture
  carry.
- Primary file:
  - `codex-rs/ext/git-attribution/src/git_attribution_tests.rs`

### Hook Command Early-Exit Output Preservation

- Signed downstream commit `932cbceeb8` first preserved a successful hook's
  stdout, stderr, and exit status when the hook closed stdin before consuming
  the full payload and the parent observed `BrokenPipe`.
- Upstream `634a998d8a` now owns the same production behavior and a
  cross-platform fast-exit regression. The retained Windows-specific
  `hook_can_exit_successfully_without_reading_stdin` test is supplemental
  validation only; it adds no runtime behavior. This is historical
  upstream-equivalent evidence, not live carry.

### Upstream Launcher Validation Repairs

- Signed downstream commit `932cbceeb8` supplies the empty inherited-descriptor
  slice missed by the upstream Wine PTY API migration and adds the exact
  optional sandbox-executable comment required by downstream's Windows
  argument lint.
- These validation-only hunks are independent. Drop the Wine call-site repair
  when upstream updates that caller, and drop the comment when upstream adopts
  an equivalent annotation or the downstream lint no longer requires it.

### Windows Filesystem Boundary Compatibility

- The 2026-07-16 sync adopts upstream commit `5a85351dfe`, which retires the
  `GeneratedDefault` provenance and its setup-time metadata normalization.
  Downstream no longer claims that either Windows backend can reserve absent
  `.git`, `.agents`, or `.codex` child names without filesystem interception.
- The 2026-07-21 sync adopts upstream commit `bd92b056dd`, which distinguishes
  explicit from inherited allow ACEs when deciding whether a write root needs
  repair. Preserve that distinction: `SET_ACCESS` cannot replace an inherited
  `FILE_DELETE_CHILD` grant, so inherited stale rights must not trigger a
  refresh loop that cannot converge.
- The `/tmp` special root remains Unix-only. Treating root-relative `/tmp` as a
  Windows drive path would create a split writable-root policy that the legacy
  restricted-token backend cannot enforce.
- Explicit read-only carveouts and pre-existing protected metadata remain the
  enforceable boundary. Keep those assertions holistic rather than restoring
  removed setup-side-effect tests or persistent sentinel directories.
- The compatible legacy token keeps Everyone on its default DACL for IPC, but
  excludes Everyone from the restricting SID set and retains `WRITE_RESTRICTED`
  so existing launch and runtime dependencies remain readable. This preserves
  the pre-existing backend behavior; it does not make capability-SID deny ACEs
  authoritative for standalone `DELETE` or `FILE_DELETE_CHILD` checks.
- Because Everyone is absent from the restricting SID set, the matching
  root-capability allow ACE is mandatory for every promised workspace write.
  Signed downstream commit `0b66edecaf` makes ACL preparation fail closed with
  path context instead of launching a child after silently losing that grant.
- Hosted comparison run `29628256459` proved the remaining limitation:
  `WRITE_RESTRICTED` started successfully but deleted both normalized hostile
  targets, while full restriction without Everyone could not start the process.
  Full restriction with Everyone passed that fixture and the complete Windows
  sandbox unit target in run `29628486469`, but review found that any path which
  grants Everyone write access would also satisfy the restricting check. The
  merged-head run `29636964666` additionally proved that full restriction breaks
  PowerShell and gnullvm runtime loading without explicit capability-SID read
  access. Those runs are diagnostic evidence, not proof that either incomplete
  full-restriction shape is safe to ship.
- A durable full-restriction design must provision the exact active capability
  SIDs with synchronous read/execute access to bounded launch and runtime
  dependencies, must not derive those roots from ambient `PATH`, and must avoid
  stale capability ACEs widening later policies. Until then, the normalized
  deletion fixture deliberately characterizes the legacy limitation rather than
  claiming containment.
- Hosted Windows guardrails:
  `slash_tmp_permission_path_is_unix_only`,
  `filesystem_policy_blocks_protected_metadata_path_writes_by_default`,
  `missing_symbolic_metadata_carveouts_need_direct_runtime_enforcement`,
  `windows_restricted_token_supports_full_read_split_write_read_carveouts`, and
  `legacy_write_restricted_deletion_limitation_is_explicit`, plus
  `restricted_sids_exclude_everyone` and
  `default_dacl_keeps_everyone_for_ipc_compatibility`. The direct helper
  boundary is `file_system_local_fs_helper_allows_windows_workspace_root_write`;
  it performs the positive write assertion on release-shaped targets and emits
  an exact diagnostic instead of misreporting Bazel gnullvm status
  `0xc0000142` as an ACL denial. Hosted Windows MSVC proof remains mandatory.
- `workspace_roots_allow_file_and_command_writes` independently retains the
  successful command-write assertion when gnullvm cannot re-enter the
  filesystem helper, while `error_output_preserves_source_chain` ensures a real
  helper failure reaches the caller with its full source chain.
- The deletion characterization uses PowerShell 7, which adjacent
  restricted-token tests prove can initialize under the compatible token.
  Bazel's gnullvm test executable can still fail during helper re-entry with
  exact status `0xc0000142`; that status proves the wrapper was reached but
  does not replace the read and denied-write assertions on MSVC and other
  release-shaped targets.
- The Windows network-proxy stable-ingress test uses the same bounded
  classification for its first restricted-token self-reexec only. It emits the
  exact status and skips the remaining proxy assertions solely on gnullvm; any
  other child exit remains a failure, and MSVC must execute the complete route,
  policy, and cleanup matrix.
- Primary files:
  - `codex-rs/protocol/src/permissions.rs`
  - `codex-rs/sandboxing/src/policy_transforms.rs`
  - `codex-rs/sandboxing/src/policy_transforms_tests.rs`
  - `codex-rs/sandboxing/src/windows.rs`
  - `codex-rs/apply-patch/src/lib.rs`
  - `codex-rs/core/src/exec_tests.rs`
  - `codex-rs/core/tests/suite/workspace_roots.rs`
  - `codex-rs/windows-sandbox-rs/src/token.rs`
  - `codex-rs/windows-sandbox-rs/src/token_tests.rs`
  - `codex-rs/windows-sandbox-rs/src/spawn_prep.rs`
  - `codex-rs/windows-sandbox-rs/src/unified_exec/tests.rs`
  - `codex-rs/exec-server/tests/file_system_windows.rs`
  - `codex-rs/network-proxy/tests/windows_stable_ingress.rs`

### Windows Proxy-Aware Backend Selection

- Upstream commit `88fac6fe10` owns requirements-defined profile lookup while
  resolving the active network proxy specification. Downstream Windows carry
  starts after that `NetworkProxySpec` exists and continues to own effective
  backend selection, prepared filesystem overrides, telemetry, firewall
  enforcement, and direct-bypass denial.
- Upstream commit `32f4687b8c` now owns exec-server's bounded reverse-RPC
  transport for client policy decisions and keeps those callbacks alive until
  inherited process streams close. Do not recreate that transport downstream;
  the remaining carry begins at the distinct Windows launch and enforcement
  policy after a decision is available.
- A managed network proxy is an effective elevated-backend requirement on
  Windows, regardless of whether the configured sandbox level is elevated or
  restricted token.
- Direct exec and unified exec share the same backend selector and filesystem
  override resolver. The prepared deny-read/write and split-root overrides
  must reach the canonical Windows session spawner unchanged.
- PowerShell `-NoProfile` startup, spawn-failure metrics, tool telemetry, and
  turn metadata follow the effective backend, not only the configured level.
- The direct-loopback denial fixture runs native `curl.exe` with explicit direct
  routing and connect/overall deadlines. Because a WFP-blocked connect can
  outlive curl's own deadline, the fixture makes curl its first potentially
  blocking operation, observes the bounded unified-exec yield, and terminates
  only the registered background session through Codex's process manager. The
  unique non-proxy endpoint must record zero direct requests. Distinct
  connected, expected-denial, and probe-error outcomes prevent a missing or
  broken probe from passing.
- Hosted guardrails:
  `windows_proxy_enforcement_uses_elevated_backend`,
  `windows_spawn_failure_metric_uses_effective_backend`,
  `proxy_enforced_restricted_token_uses_windows_elevated_tag`,
  `proxy_enforced_windows_sandbox_prepares_elevated_filesystem_overrides`,
  `inserts_no_profile_for_proxy_selected_elevated_windows_sandbox`, and
  `unified_exec_proxy_blocks_direct_loopback_bypass_on_windows`.
- Upstream provenance: `4bc2c723ef` introduced proxy-selected elevation for
  direct exec, and `35c2278dd5` now owns the shared native launcher used by
  unified exec and exec-server process spawning. The obsolete downstream
  direct launch block is retired. Preserve the remaining downstream completion
  carry for override preparation, PowerShell startup, metrics, telemetry, and
  effective-backend attribution until those adjacent policies are equivalent
  upstream.
- Primary files:
  - `codex-rs/windows-sandbox-rs/src/lib.rs`
  - `codex-rs/sandboxing/src/spawn.rs`
  - `codex-rs/sandboxing/src/windows.rs`
  - `codex-rs/core/src/sandboxing/mod.rs`
  - `codex-rs/core/src/exec.rs`
  - `codex-rs/core/src/sandbox_tags.rs`
  - `codex-rs/core/src/unified_exec/process_manager.rs`
  - `codex-rs/windows-sandbox-rs/src/unified_exec/mod.rs`

### App-Server Command-Cwd Windows Sandbox Mode

- `command/exec` with `permissionProfile` reloads the trusted project selected
  by the command `cwd`; its Windows sandbox mode travels with its permission
  profile, workspace roots, and network policy.
- Do not replace that reloaded mode with the app-server process's global mode.
  A global disabled mode must not silently disable a command project's explicit
  unelevated sandbox.
- Hosted guardrails:
  `command_exec_permission_profile_project_roots_use_command_cwd` and
  `command_exec_permission_profile_uses_command_cwd_windows_sandbox_mode`.
- Upstream provenance: `8e8fd94c60` introduced the command-cwd reload behavior;
  `4bc2c723ef` accidentally dropped the selected level while changing proxy
  handling. Keep the restoration until upstream restores it.
- Primary files:
  - `codex-rs/app-server/src/request_processors/command_exec_processor.rs`
  - `codex-rs/app-server/tests/suite/v2/command_exec.rs`
  - `codex-rs/app-server/README.md`

### App-Read Plugin Auth Synchronization

- Upstream owns the enriched experimental `app/read` metadata contract through
  `60272096bc`, including plugin display-name projection without MCP startup.
- Downstream plugin app declarations remain gated by the current auth mode.
  Before reading plugins, `app/read` copies the live auth snapshot into
  `PluginsManager`, matching the synchronization already used by neighboring
  plugin list, installed, and read RPCs.
- Ordinary serialized login completion already refreshes the manager before the
  next request on the standard transport. The request-boundary synchronization
  additionally covers embedding-owned or externally updated auth managers and
  keeps the metadata and plugin projections on one snapshot.
- Hosted guardrail:
  `app_read_resynchronizes_plugin_auth_after_external_login_without_starting_mcp`
  in `codex.app-server-v2-contract-targeted`, together with the complete
  `suite::v2::app_read::` group.
- Primary files:
  - `codex-rs/app-server/src/request_processors/apps_processor/read.rs`
  - `codex-rs/app-server/tests/suite/v2/app_read.rs`
  - `justfile`

### Python Static-Analysis Corrections

- Downstream carries three upstreamable Python maintenance corrections found
  during an earlier static-analysis pass.
- The Windows timeout security smoke exceeds the harness deadline, records its
  expected `subprocess.TimeoutExpired`, and allows unexpected failures to
  surface before checking that outside writes remain denied.
- The Unix `just` shell launcher declares its `os.execvp` path as non-returning,
  and the issue-digest collector no longer retains an unused derived local.
- The same maintenance change removes an unused list from the downstream
  divergence audit; that script remains covered by the existing branch-policy
  carry rather than this upstream-fix record.
- Preserve these corrections during upstream syncs until equivalent control
  flow lands upstream. Once it does, drop the redundant patch and this carry
  record.
- Primary files:
  - `codex-rs/windows-sandbox-rs/sandbox_smoketests.py`
  - `scripts/just-shell.py`
  - `.codex/skills/codex-issue-digest/scripts/collect_issue_digest.py`

### Coverage Upload Disablement

- Upstream commit `dfdf9ff36b` added Code Coverage report uploads and assumes
  that the repository has GitHub Code Quality enabled.
- This repository deliberately does not configure that product. The three
  report-generation test steps remain required, but the workflow does not
  request `code-quality: write` or call `actions/upload-code-coverage`.
- This prevents the checked-in workflow from activating the Code Quality
  integration or invoking `github-code-quality[bot]`, while preserving the
  ordinary coverage-instrumented tests.
- Preserve this operator-specific policy carry unless the repository owner
  explicitly changes the Code Quality product policy. Upstream harvests must
  not silently restore the upload action or its token permission.
- Hosted guardrail:
  `test_coverage_workflow_does_not_use_code_quality_product` in
  `codex.workflow-ci-sanity`.
- Primary file:
  - `.github/workflows/code-coverage.yml`

### First-Party Usage Ledger Ownership

- Downstream keeps usage-ledger ownership in this repo.
- Billing-turn canonicalization and historical AUD reporting remain downstream
  requirements, and the canonical local ledger implementation lives in
  `usage.sqlite` rather than an external sibling repository.
- `codex-rs/state/src/runtime/usage.rs` and
  `codex-rs/state/usage_migrations/0001_usage_tables.sql` do not currently
  have upstream counterparts, so future sync passes should treat them as
  downstream-owned behavior to preserve rather than as stale carry to delete.
- Usage-ledger ownership stays here: any upstream-native reimplementation must
  reproduce the downstream per-turn ledger, provider/token metadata, and
  billing-turn reporting semantics before the canonical source of truth can
  move out of this repository.
- The 2026-07-23 sync through upstream `946ed315a4` adopts `SqliteConfig` as the single connection
  factory for state, logs, goals, memories, and the downstream usage database.
  Downstream extension migrations, generalized state-migration repair, usage
  telemetry, storage mapping, and failure-path pool cleanup remain additive
  behavior around that upstream-owned connection seam.
- Do not restore the removed runtime-owned database specs, public filename
  constants, or free path helpers in a future conflict. Add downstream databases
  and pre-migration phases to `SqliteConfig`, and keep runtime initialization as
  orchestration around that shared owner.
- Exact-head GitHub-hosted run `29951153938` passed `core-ledger-smoke`,
  `codex.state-migration-repair-targeted`, and the adjacent thread-store,
  app-server, CLI, shell, and sub-agent compatibility lanes at signed merge
  `de2ba7d16b`.
- Migration `0013_usage_codex_credit_rate_update_20260905.sql` adds the
  operator-authoritative STANDARD/default rate observation while preserving
  prior half-open intervals. It prices the provider's Daybreak Blue aliases
  (`gpt-daybreak-blue`, `gpt-daybreak-blue-latest`) with the Sol card and the
  Daybreak Red aliases (`gpt-daybreak-red`, `gpt-daybreak-red-latest`) with
  the Cyber card; the `gpt-5.6` Sol alias is covered as well. GPT-Image-2 image/text cards are retained as explicit,
  non-selectable records until usage calls expose a modality discriminator;
  ambiguous calls remain partial/unpriced.
- Primary files:
  - `codex-rs/core/src/session/session.rs`
  - `codex-rs/state/src/runtime.rs`
  - `codex-rs/state/src/sqlite.rs`
  - `codex-rs/state/src/runtime/usage.rs`
  - `codex-rs/state/usage_migrations/0001_usage_tables.sql`
  - `codex-rs/state/usage_migrations/0013_usage_codex_credit_rate_update_20260905.sql`
  - `docs/downstream.md`

### Usage Event Logging And Metadata Capture

- Sessions record a downstream-only usage log database that tracks token,
  provider, tool, and spawn metadata per thread so downstream reporting can
  export a full list of agents, tool calls, weighting/effort metadata, rate-
  limit snapshots, and completion/forging regions for downstream billing and
  audit workflows.
- The new `usage` SQLite DB stores `usage_threads`, `usage_provider_calls`,
  `usage_tool_calls`, `usage_quota_snapshots`, `usage_spawn_requests`, and
  `usage_fork_snapshots`, capturing per-turn requested model/provider hints,
  tool invocation lifecycles, rate-limit snapshots, and parent/child thread
  relationships for spawn requests.
- `usage_provider_calls` also stores provider-confirmed `final_model` and
  `model_snapshot` values when turn completion reports them, preserving the
  downstream distinction between requested/configured model, historical
  `actual_model_used`, and final provider identity. Core turn completion
  captures terminal `ResponseEvent::ServerModelIdentity` values so app-server,
  TUI, and usage-ledger consumers receive provider-confirmed identity instead
  of falling back to `None`.
- Provider calls and fork snapshots retain upstream's prompt-cache write token
  count as a distinct accounting dimension. The existing non-cached input
  total remains input minus cache reads, matching upstream telemetry semantics;
  cache-write usage is stored alongside it rather than silently collapsed.
- Provider calls record requested and runtime-established service tiers,
  Fast-mode request/use flags, billing surface, and account plan. The shared
  `priority` wire value is priced as Codex Fast only when authentication
  establishes the ChatGPT credit surface; API Priority remains unpriced in
  Codex-credit views.
- `usage_codex_credit_rates` and `usage_codex_credit_policies` retain
  source-backed, half-open effective intervals. The call, turn, and thread
  views expose raw tokens, provenance, component estimates, partial coverage,
  and explicit uncertainty. Provider-reported credits take precedence without
  overwriting the independently calculated rate-card estimate.
- Migration `0007_usage_codex_credit_rate_update.sql` records the official
  2026-07-30 rate revision without rewriting history: it closes only the four
  incumbent GPT-5.6 Luna/Terra Standard/Fast rows at the UTC boundary and
  inserts four new rows there with their source provenance. Luna Standard and
  Terra Standard are 5 / 0.5 / 30 and 50 / 5 / 300 credits per 1M
  uncached input / cached input / output; their explicit Fast rows remain the
  2.5x card at 12.5 / 1.25 / 75 and 125 / 12.5 / 750. Sol Standard and Fast
  intervals remain open, gap-free, and unchanged.
- Completed thread/list/read and TUI status surfaces prefer thread-local
  provider identity evidence from turn completion or the usage ledger before
  falling back to configured session metadata; active/running threads keep the
  live effective model first so sub-agent status does not regress to the
  parent/session model.
- The 2026-07-18 sync adopts upstream `tui.resume_cwd`, including remembered
  current/session directory choices, explicit `--cd` precedence, remote
  workspace rejection, and persistence-error handling. The same
  `session_resume` module retains downstream model and reasoning-effort
  recovery; future syncs must preserve both behaviors rather than selecting
  one side of that additive seam.
- Primary files:
  - `codex-rs/core/src/session/turn.rs`
  - `codex-rs/core/src/session/turn_context.rs`
  - `codex-rs/core/src/tasks/mod.rs`
  - `codex-rs/app-server/src/request_processors/thread_processor.rs`
  - `codex-rs/core/src/state/service.rs`
  - `codex-rs/protocol/src/protocol.rs`
  - `codex-rs/state/src/lib.rs`
  - `codex-rs/state/src/migrations.rs`
  - `codex-rs/state/src/runtime.rs`
  - `codex-rs/state/src/runtime/usage.rs`
  - `codex-rs/state/usage_migrations/0001_usage_tables.sql`
  - `codex-rs/state/usage_migrations/0003_usage_provider_call_model_identity.sql`
  - `codex-rs/state/usage_migrations/0004_usage_cache_write_tokens.sql`
  - `codex-rs/state/usage_migrations/0005_usage_codex_credits.sql`
  - `codex-rs/state/usage_migrations/0007_usage_codex_credit_rate_update.sql`
  - `codex-rs/tui/src/chatwidget/status_surfaces.rs`
  - `codex-rs/tui/src/session_resume.rs`
  - `codex-rs/state/Cargo.toml`

### Provider Inference-Attempt Observation Contract

- Downstream defines a payload-free `inference_call` protocol event for one
  concrete client-side provider attempt. The contract contains no prompts,
  outputs, headers, error bodies, or other request payloads.
- The local call identifier and thread, turn, and optional spawn-request
  correlation remain exact. Oversized correlation identifiers reject the
  event instead of producing a misleading shortened identity.
- Configured provider, requested model and tier, and provider-observed
  provider, model, snapshot, and tier are separate evidence fields. Missing
  provider evidence remains `null`; completion usage is exact for one response
  or `null`, never accumulated or estimated.
- Started, completed, failed, and cancelled observations have bounded lifecycle
  shapes. Required configured/requested strings use UTF-8-safe truncation,
  optional evidence is omitted when it cannot be preserved exactly, and the
  event records both decisions under a 4096-byte serialized cap.
- Readers accept unknown future event types as an ignored `Unknown` event so a
  newer rollout producer does not make an older event consumer fail.
- This stage defines the protocol and guardrail only. Runtime transport
  emission, terminal arbitration, persistence and query projection, pricing,
  and identity reconciliation remain separate delivery stages.
- Preserve this carry until upstream offers equivalent payload exclusion,
  exact correlation, requested-versus-observed evidence separation, bounds
  provenance, and unknown-event compatibility. Drop the event, consumer match
  arms, focused lane, and this record together once that equivalence exists.
- Primary files:
  - `codex-rs/protocol/src/protocol.rs`
  - `codex-rs/protocol/src/protocol/inference_observation.rs`
  - `codex-rs/protocol/src/protocol/inference_observation_tests.rs`
  - `.github/validation-lanes.json`
  - `justfile`

### Provider-Observed Turn Usage

- `TurnCompleteEvent.provider_usage` is optional for rollout compatibility and
  contains only the element-wise aggregate of exact provider-reported usage
  from successful response completions in that turn. Missing provider evidence
  stays absent; configured identity, session-total deltas, estimates, pricing,
  and call counts are not substitutes.
- Normal sampling, local compaction, and remote-v2 compaction contribute to the
  same turn-scoped aggregate. The terminal `final_model` and `model_snapshot`
  continue to describe only the terminal successful sampling response rather
  than every response included in the usage aggregate. Each ordinary or
  compaction task starts with an empty aggregate, and sampling re-entry after
  steering or local compaction retains earlier usage in the same logical turn.
  Usage from a successful response remains attributable to that turn even if a
  later follow-up fails.
- `TurnAbortedEvent.provider_usage` carries the same optional exact aggregate
  through interrupted, replaced, and budget-limited terminal paths. Legacy
  rollout events without the field resume as absent, and a persisted receipt
  never seeds the next turn after resume.
- Primary files:
  - `codex-rs/protocol/src/protocol.rs`
  - `codex-rs/core/src/session/turn_context.rs`
  - `codex-rs/core/src/session/turn.rs`
  - `codex-rs/core/src/compact.rs`
  - `codex-rs/core/src/compact_remote_v2.rs`
  - `codex-rs/core/src/tasks/mod.rs`

### Side Chat Persistence And Usage Ledger Tracking

- `/side` conversations are persisted as side-tagged fork threads instead of
  pathless ephemeral forks, so they keep rollout transcripts, remain resumable,
  and can be forked by thread id or rollout path.
- Default history/list/search surfaces hide side chats; explicit
  `threadSources: ["side"]` requests expose the side-chat history class.
- Usage-ledger lineage records side forks in `usage_threads` and
  `usage_fork_snapshots`, marks `usage_threads.thread_source = "side"`, and
  writes normal provider-call rows for side turns.
- The upstream-shaped fork presentation is the single TUI authority for the
  fork shape: regular presentation maps to `ThreadSource::User`, while side
  presentation maps to `ThreadSource::Side`, excludes inherited response
  turns, and skips the redundant parent-title lookup. The `/side` caller keeps
  the fork persisted and seeds navigation before delayed `thread/started`
  delivery so selection does not race a second liveness read.
- Fork and fresh-session lifecycle paths adopt upstream's metadata-read
  de-duplication. Resume and picker backfill reuse discovered server status,
  while locally live descendant channels remain authoritative and downstream
  V1 writable versus V2 parent-owned input behavior remains unchanged.
- Forks created from an existing side conversation inherit the side
  `thread_source` unless the caller explicitly supplies a different source,
  keeping nested side-chat forks hidden from default history surfaces and
  marked in usage-ledger lineage.
- `scripts/codex-resume-recent.sh` skips side chats by default, with
  `--include-side` available when an operator deliberately wants side-chat
  resume candidates.
- Primary files:
  - `codex-rs/tui/src/app/side.rs`
  - `codex-rs/tui/src/app/agent_navigation.rs`
  - `codex-rs/tui/src/app/loaded_threads.rs`
  - `codex-rs/tui/src/app/session_lifecycle.rs`
  - `codex-rs/tui/src/app/tests/session_lifecycle_requests.rs`
  - `codex-rs/tui/src/app_server_session.rs`
  - `codex-rs/app-server/src/request_processors/thread_processor.rs`
  - `codex-rs/app-server/src/filters.rs`
  - `codex-rs/state/src/runtime/usage.rs`
  - `codex-rs/state/usage_migrations/0002_usage_thread_source.sql`
  - `scripts/codex-resume-recent.sh`

### App-server Thread Source, History Mode, Name, And Resume-Settings Compatibility

- Preserve downstream `thread_source` provenance alongside upstream
  `history_mode` metadata in thread listing, summary, resume, persisted
  metadata, and generated protocol schemas.
- These fields are independent dimensions: history storage mode must not erase
  whether a thread came from a side conversation, sub-agent, or another
  attributed source.
- Upstream paginated-thread compatibility views are adopted: full read/resume
  history, `initialTurnsPage`, and item/turn listing hydrate the stored
  `ThreadItem` projection rather than rebuilding paginated context through the
  legacy event-history path. That upstream ownership must continue to preserve
  downstream `thread_source` alongside `history_mode`.
- Upstream state-backed thread names are likewise additive metadata. Named
  paginated threads must preserve `thread_source`, `history_mode`, and `name`
  together across read, state-only list, and metadata-only resume responses.
- Upstream paginated Git metadata updates and paginated memory eligibility are
  also adopted. SQLite-only Git patches and memory-mode reconciliation must
  remain additive to downstream `thread_source`; none may coerce a paginated
  thread back to legacy history or erase its name or provenance.
- Cold app-server resume restores the newest persisted approval policy and
  permission profile together with the already preserved model, provider,
  reasoning effort, approvals reviewer, cwd, and named active permission
  profile. Explicit resume-request overrides remain authoritative.
- Settings history is scanned newest-first across both `TurnContext` and
  `ThreadSettingsApplied`, so a later turn context supersedes an older snapshot.
  `thread_resume_preserves_goal_first_and_fork_settings` exercises a goal-first
  thread and its fork against conflicting restart defaults, while
  `merge_persisted_approval_and_permissions_prefers_later_turn_context` locks
  the chronology rule directly.
- When upstream refactors app-server fork initialization, retain its shared
  `thread_history` construction and reviewer scan over the untruncated source
  history. Reapply only the downstream `thread_source` fallback,
  `core_dynamic_tools` conversion, and persisted approval/permission merge;
  this makes the three carry points explicit rather than preserving a parallel
  fork implementation. Hosted app-server fork and protocol validation run
  `29909770697` passed the combined shape on signed merge head `c673caf16f`.
- Primary files:
  - `codex-rs/protocol/`
  - `codex-rs/rollout/`
  - `codex-rs/state/`
  - `codex-rs/thread-store/`
  - `codex-rs/app-server-protocol/`
  - `codex-rs/app-server/tests/suite/conversation_summary.rs`
  - `codex-rs/app-server/tests/suite/v2/thread_read.rs`

### Direct App-Server Serialization And Missing-Response Retry

- Upstream commits `f69dc49b15` and `e370d23691` remove redundant JSON value
  round trips from app-server request decoding, typed-request diagnostics,
  tracing, and outgoing transport serialization. Every client request now has
  an explicit wire name, and diagnostics allocate it only when constructing an
  error. Adopt those paths unchanged rather than restoring the removed
  serialization-based lookup helper.
- The sole merge conflict was the remote-client import block. Resolution drops
  obsolete `request_method_name` as upstream requires and retains only
  downstream `server_notification_requires_delivery`, which classifies the
  bounded remote-event queue. The upstream typed request path and downstream
  delivery backpressure therefore compose without a second method-name adapter.
- Upstream commit `64dc1c7a01` treats the typed
  `previous_response_not_found` WebSocket code as retryable so the turn loop can
  send the full request. It preserves a server message when supplied and uses
  the upstream fallback only when that message is absent. Provider-confirmed
  model identity remains emitted alongside this upstream retry mapping.
- Native `ComputerUseCall`, dynamic-tool protocol methods, generated-schema
  compatibility, and TypeScript response export remain additive to upstream's
  explicit request wire names. `codex.app-server-protocol-test`,
  `codex.app-server-v2-contract-targeted`, and `core-runtime-surface-smoke` are
  the focused hosted proof; the app-server V2 slice also runs upstream's direct
  transport wire-shape regression.

### Thread-store History And Metadata Ordering

- A clone-shared asynchronous operation permit serializes each `LiveThread`
  mutation that can change canonical JSONL history, derive SQLite metadata, or
  acknowledge a pending metadata generation. This keeps rollout order and the
  indexed model, provider, reasoning, cwd, and related metadata projection in
  one observable order.
- The same boundary covers inherited-history persistence, explicit persist and
  flush barriers, shutdown, discard, memory-mode updates, and direct metadata
  updates. Read-only history and rollout-path access remains concurrent.
- The permit is owned across awaits rather than holding a synchronous mutex.
  This preserves async runtime safety while preventing one clone's `persist()`
  from acknowledging metadata before another clone has observed its already
  appended settings event.
- `concurrent_appends_keep_sqlite_metadata_in_canonical_history_order` and
  `persist_waits_for_append_observation_before_flushing_pending_metadata`
  deterministically guard both races through the real local JSONL and SQLite
  store boundary.
- The ordering regression compares the full projected metadata after
  canonicalizing both cwd values. This keeps the holistic assertion intact on
  Windows, where SQLite may expose a verbatim `\\?\` prefix, instead of
  weakening it to field-by-field checks.
- Primary files:
  - `codex-rs/thread-store/src/live_thread.rs`
  - `codex-rs/thread-store/src/live_thread_tests.rs`
  - `codex-rs/thread-store/src/lib.rs`

### Private Configured Thread Identity Provenance Contract

- Keep configured-identity provenance storage out of generic `ThreadMetadata`.
  SQLite owns the private tri-state fact, while `codex-state` exposes only the
  typed `Unknown`, `KnownAbsent`, or `Present` enum plus three narrow
  `StateRuntime` read/transition methods. `Unknown` must never be interpreted
  as evidence of absence.
- Migration `0045` defaults existing rows and old-binary-shaped inserts to
  `Unknown`. StateRuntime permits only atomic forward transitions:
  `Unknown -> KnownAbsent`, `Unknown -> Present`, and
  `KnownAbsent -> Present`; mutation methods report a missing thread row
  explicitly rather than treating it as an idempotent transition.
- Generic thread-metadata inserts and upserts deliberately omit the private
  column, so unrelated metadata writes cannot reset or fabricate provenance.
- Provenance and collision-repair fixtures use upstream's on-disk
  `SqliteConfig` test topology. The writer-slot guard holds `BEGIN IMMEDIATE`
  through a writable pool while generalized repair reads through a separate
  read-only pool, proving a no-op repair does not need the writer slot.
- This stage does not classify rollout events, reconstruct history, store
  configured identity values, enforce precedence, or synchronize live state.
- Primary files:
  - `codex-rs/state/migrations/0045_threads_configured_identity_provenance.sql`
  - `codex-rs/state/src/migrations_tests.rs`
  - `codex-rs/state/src/runtime/configured_identity_provenance.rs`
  - `codex-rs/state/src/runtime/configured_identity_provenance_tests.rs`

### Phase-2 Memory Attestation And Prepared-Input Fingerprinting

- Downstream phase-2 memory consolidation remains fail-closed once attestation
  support has been initialized for a memory root.
- Consolidated memory artifacts are fingerprinted against the prepared input
  tree, the effective consolidator contract, and the output tree, then recorded
  in runtime state so unchanged workspaces can safely reuse existing outputs
  while drifted or tampered artifacts are rejected after bootstrap.
- This is an intentional downstream carry, not derivative test churn: losing
  the attestation runtime while keeping the attestation tests is a regression.
- Because these downstream state migrations occupy slots that upstream did not
  have at the time they were introduced, later upstream migrations may need to
  be replayed into the next free downstream migration version while preserving
  their SQL content. Current examples include upstream's device-key binding
  table (`0028_device_key_bindings.sql` upstream, `0031_device_key_bindings.sql`
  downstream) and upstream's thread-goals table (`0029_thread_goals.sql`
  upstream, `0032_thread_goals.sql` downstream), avoiding collisions with the
  already-shipped downstream `0028` through `0031` migration versions. When an
  upstream sync collides with already-shipped downstream state migration
  versions, keep upstream migration numbers when possible and move downstream
  additive carry to the next free version with checksum-gated runtime repair
  for databases that already recorded the old version. The current example is
  preserving upstream `0040_threads_history_mode.sql` while moving downstream
  visible-thread sort indexes to `0044_threads_visible_sort_indexes.sql`.
  The 2026-07-20 sync likewise preserves upstream
  `0041_threads_name.sql` and moves the previously renumbered
  remote-control-enabled migration from downstream slot `0041` to
  `0046_remote_control_enrollments_enabled.sql`; checksum-gated repair moves
  already-applied downstream `0041` records before the upstream migration runs.
  The same sync preserves upstream `0042_drop_agent_jobs.sql` byte-for-byte and
  moves the already-shipped downstream external-agent import ledger from slot
  `0042` to `0047_external_agent_config_imports.sql`. The runtime repairs exact
  known downstream checksums for both `0041` and `0042` before SQLx applies the
  upstream migrations. The `0042` upgrade deliberately removes only the
  retired CSV coordinator's `agent_jobs` and `agent_job_items` tables; ordinary
  threads, spawn edges, and external-agent import records remain intact.
- Upstream's provider-column addition for `external_agent_config_imports`
  collides with deployed downstream visible-sort indexes at `0044`. Preserve
  the upstream SQL content as `0049_external_agent_config_imports_provider_id.sql`
  so it follows the moved table creation at `0047`, and checksum-repair an
  exact upstream provider-column history record from `0044` to `0049` before
  the downstream visible-sort migration can use `0044`. Future syncs must not
  restore the upstream `0044` filename or renumber the deployed visible-sort
  migration.
- Upstream's `0043_threads_is_pinned.sql` arrives after downstream has already
  shipped `0043_threads_recency_at.sql`. Keep that deployed recency migration
  and its checksum at `0043`; import the upstream pinning SQL unchanged as
  `0048_threads_is_pinned.sql`, after the downstream `0047` allocation. The
  pin index depends on `recency_at_ms`, so this allocation keeps fresh and
  deployed databases on the same ordering without rewriting either migration.
  Future syncs must not restore the upstream `0043` filename or renumber the
  deployed recency migration.
  Once that repair and upstream `0041` have run, a pre-sync binary cannot reopen
  the same database because it knows the old `0041` and `0042` checksums;
  rollback therefore requires a pre-upgrade database copy rather than only
  replacing the binary.
- Upstream `thread_history_migrations/0002_thread_items_item_type.sql` belongs
  to the separate rebuildable `thread_history_1.sqlite` migrator. It does not
  collide with downstream state migration `0045`, and must not trigger state
  migration renumbering or checksum repair.
- Primary files:
  - `codex-rs/memories/write/src/phase2.rs`
  - `codex-rs/memories/write/src/phase2_attestation.rs`
  - `codex-rs/memories/write/src/phase2_attestation_tests.rs`
  - `codex-rs/memories/write/src/startup_tests.rs`
  - `codex-rs/state/src/migrations.rs`
  - `codex-rs/state/src/migrations_tests.rs`
  - `codex-rs/state/src/runtime/migration_repair.rs`
  - `codex-rs/state/src/runtime/phase2_attestation.rs`
  - `codex-rs/state/migrations/0024_phase2_attestation_roots.sql`
  - `codex-rs/state/migrations/0038_phase2_attested_baselines.sql`
  - `codex-rs/state/migrations/0031_device_key_bindings.sql`
  - `codex-rs/state/migrations/0032_thread_goals.sql`
  - `codex-rs/state/migrations/0043_threads_recency_at.sql`
  - `codex-rs/state/migrations/0044_threads_visible_sort_indexes.sql`
  - `codex-rs/state/migrations/0046_remote_control_enrollments_enabled.sql`
  - `codex-rs/state/migrations/0047_external_agent_config_imports.sql`
  - `codex-rs/state/migrations/0048_threads_is_pinned.sql`
  - `codex-rs/state/migrations/0049_external_agent_config_imports_provider_id.sql`
  - `docs/memories.md`

### Release Metadata, Installer Routing, And Rebuild Triggers

- Release builds embed canonical release identity plus compact provenance
  metadata.
- Version metadata rebuilds when git state changes, including shared worktree
  git state.
- Public POSIX and PowerShell installers retain the downstream release
  repository and tag-prefix adapter. Upstream's optional release-distribution
  source is available only when those values remain `openai/codex` and
  `rust-v`; custom downstream origins always resolve and download through their
  configured GitHub repository.
- The manual branch-build workflow keeps Linux `x86_64` as its default, adds an explicit native
  Linux Arm64 preview mode, and offers an explicit macOS preview mode. The macOS mode builds one Intel x64
  artifact, applies ad hoc signatures for local execution, records that the
  artifact is not notarized, and reuses Cargo-home and compiler-cache entries
  without changing the release optimization profile. It remains a disposable
  preview and does not publish a GitHub Release. Linux branch and validation-artifact builders
  isolate workflow-owned helper/cache paths from source provenance, require a clean checkout before
  Cargo starts, defer repository-root `dist/` creation until after compilation, and reject a staged
  binary whose displayed git provenance contains `-dirty`. The
  official release contract independently
  supports native Linux x64, Linux Arm64, and Intel macOS x64 archives with target-bound metadata,
  checksums, and native-runner verification. Linux assets also carry keyless Sigstore bundles,
  SPDX SBOMs, and GitHub build-provenance attestations; Apple Silicon remains unsupported.
- Preview and release Cargo dependency/compiler caches use separate authority namespaces and
  exclude executable Cargo tool paths. Current x86 and all Arm64 installs default to exact-source
  hardened verification, using checksum-pinned temporary verifier tools and downloaded local
  provenance bundles without updater-host GitHub authentication when compatible host tools are
  absent; current x86 callers cannot select only one trust layer. Immutable pre-cutoff x86-only releases without modern assets automatically preserve
  stale updater compatibility; the explicit historical-x86 escape hatch cannot downgrade current
  releases. A previously present release directory is activated only when it matches the freshly
  verified binaries, metadata, and checksum manifest byte-for-byte.
- Automatic update discovery is intentionally narrower than the published target matrix: it is
  enabled only for stable, strictly newer Linux `x86_64` and `aarch64` releases. Unsupported
  targets, prerelease candidates, and macOS are automatic no-ops. The `--require-newer-than`
  floor rejects equality or older candidates before asset fetch or `current` activation;
  `--allow-prerelease` and `--macos-preview` are explicit manual modes and do not broaden that
  automatic boundary. The lower-bound, legacy-fixture installer tests, TUI update-version and
  prompt surfaces, `codex doctor` diagnostics, and hosted `sedna.update-installer-contract` lane are
  the current references.
- Preserve the dual-native Linux matrix, target-isolated names and caches, exact-source
  hosted-runner trust checks, mandatory Arm64 hardening, historical x86 compatibility, protected-main
  marker/manual publication, and release tags as outputs rather than workflow authority as one
  release contract during upstream syncs. Drop or narrow
  it only when upstream provides an equivalent downstream-origin and dual-architecture release
  contract.
- Primary files:
  - `.github/workflows/_sedna-linux-rust.yml`
  - `.github/workflows/sedna-release.yml`
  - `.github/workflows/sedna-release-install.yml`
  - `.github/workflows/sedna-branch-build.yml`
  - `.github/workflows/_validation-lane-release.yml`
  - `.github/scripts/isolate_workflow_paths_from_provenance.sh`
  - `.github/scripts/validation-lanes/artifact-build.sh`
  - `.github/scripts/validation-lanes/sedna-update-installer-contract.sh`
  - `.github/scripts/test_ci_planners.py`
  - `codex-rs/cli/src/doctor/updates.rs`
  - `codex-rs/cli/src/main.rs`
  - `codex-rs/app-server-daemon/src/update_loop.rs`
  - `codex-rs/tui/src/update_action.rs`
  - `codex-rs/tui/src/updates.rs`
  - `codex-rs/tui/src/update_versions.rs`
  - `docs/sedna-release.md`
  - `scripts/install_sedna_release_asset`
  - `scripts/install/test_sedna_release_lower_bound.py`
  - `scripts/install/test_sedna_release_installer.py`
  - `codex-rs/utils/version/build.rs`
  - `codex-rs/utils/version/src/lib.rs`
  - `scripts/install/install.sh`
  - `scripts/install/install.ps1`
  - `scripts/install/test_install_sh.py`

### Sub-agent selection compatibility, inventory metadata, and wait joins

- Upstream owns configured default and explicit
  `spawn_agent(model=..., reasoning_effort=...)` child selection, applies role
  settings after that selection, and validates the final effective model and
  reasoning pair.
- The 2026-07-16 sync adopts upstream's unified `[agents]` configuration,
  canonical `max_concurrent_threads_per_session` key, legacy `max_threads`
  normalization, flattened role declarations, and conditional `agent_type`
  exposure when roles are configured. Configured default sub-agent model and
  reasoning settings are now active upstream behavior rather than downstream
  carry.
- Downstream's Schemars 1.2 adapter intentionally leaves schema-only
  `deny_unknown_fields` off the flattened `AgentsToml` object so arbitrary
  `[agents.<role>]` keys remain validated as `AgentRoleToml`. A semantic schema
  regression locks that role-valued `additionalProperties` contract instead of
  relying only on generated-fixture equality.
- Spawn-agent tool guidance should follow upstream's authorization wording that
  a user request or applicable `AGENTS.md`/skill instruction can authorize
  delegation, and should keep upstream's warning that `model` overrides are
  exceptional. Downstream additionally keeps the guardrail that requests for
  depth, thoroughness, research, investigation, or detailed codebase analysis
  do not by themselves authorize spawning.
- Keep downstream itineraries that explicitly request a child model/economy
  aligned with the upstream selection and role precedence pipeline.
- Roles remain authoritative when they set `model`, `model_provider`,
  `model_reasoning_effort`, or `model_verbosity`.
- The remaining role carry preserves the resolved runtime provider object plus
  `model_reasoning_summary` and verbosity when a role does not replace them.
  Downstream also treats an empty supported-reasoning list as unknown rather
  than rejecting an otherwise selectable model.
- Models whose MultiAgentV2 backend assignment is unspecified remain selectable;
  a known, different backend still fails closed. The targeted role regression
  proves the provider object, reasoning summary, and verbosity together rather
  than checking only the selected model slug.
- `gpt-5.6-luna` is the narrow exception: downstream advertises and accepts it
  as a V2 child while the upstream-synced catalog still selects V1 for Luna
  top-level sessions. Remove this compatibility rule once upstream metadata
  marks Luna V2-compatible; do not widen it to other v1-tagged models.
- `core/src/agent/role.rs` stays on the upstream-native layered reload shape;
  downstream no longer carries a duplicate model/reasoning selection pipeline.
- The live tool-contract schema in
  `codex-rs/core/src/tools/handlers/multi_agents_spec.rs` and
  `codex-rs/core/src/tools/spec_plan.rs`, plus the regression suite in
  `codex-rs/core/src/tools/handlers/multi_agents_tests.rs`, are already back
  on upstream-native shape; the remaining carry is
  concentrated in role application, descendant inventory, spawn result
  metadata, wait summaries, and `agent/control.rs`.
- The historical `spawn_approval` argument was unused by both spawn handlers;
  the upstream removal is retained rather than carried as a phantom contract.
- MultiAgentV2 keeps the configurable `agents` namespace and the shared usage
  hint that distinguishes direct agent tool calls from `functions.exec` tools.
  This is intentional downstream routing behavior, not a reason to retain old
  handler implementations. Description tests derive the effective namespace
  from configuration rather than hard-coding either upstream or downstream
  defaults. Namespace-sensitive upstream fixtures must likewise use the
  effective downstream namespace rather than assuming upstream's default.
- A delegate whose cancellation token is already cancelled returns
  `TurnAborted` before allocating channels or spawning a child session; the
  subsequent cancellation-aware spawn remains responsible for races after
  that initial boundary.
- The 2026-07-15 sync adopts upstream's removal of `last_task_message` and
  `last_task_message_preview` from `list_agents` and `inspect_agent_tree`.
  Descendant counts, status, role, nickname, and live/stale structure remain,
  but inventory output does not expose instruction content.
- The v1 spawn result retains upstream `agent_id`/`nickname`. The v2 result exposes canonical `task_name`, conditionally visible `agent_id`/`nickname`, and the requested/effective model and reasoning fields after role application. Role, status, identity source, provider ID, and reasoning summary remain inventory or internal metadata rather than spawn-result fields.
- The live collab-spawn identity carry is phase-compatible across core emission,
  app-server conversion and lifecycle merging, history replay, TUI rendering,
  analytics and usage, schemas, and generated clients. At spawn start, the
  established `model` and `reasoningEffort` aliases carry the caller request.
  At terminal completion, those aliases carry only the observed effective
  snapshot. Additive `requestedModel` and `requestedReasoningEffort` preserve
  request provenance at both phases; additive `effectiveModel` and
  `effectiveReasoningEffort` repeat only an observed terminal snapshot.
- The four additive identity fields are required nullable on current V2
  responses: unknown values serialize as `null`. Historic terminal records
  may omit all four additive fields; when they retain observed legacy aliases
  but no request provenance, those aliases are terminal effective identity and
  never `requested*`. A current terminal item with known requested provenance
  but no observed effect keeps `effective*` null and must not be backfilled
  from its legacy aliases or metadata. Replay, TUI, analytics, and usage must
  neither infer an effective identity from a request or metadata nor
  reclassify an observed terminal alias as a request.
- The generated Python V2 model represents the current response contract: all
  four identity keys are required even though each accepts `null`. Its
  `CollabAgentToolCallThreadItem` validator materializes `null` only when all
  four additive wire keys are absent. Scoping compatibility to that model
  leaves identically named objects inside opaque tool arguments unchanged;
  partial shapes retain their validation errors, and aliases are not
  rewritten. Rust serde conversion retains the same historic shape for
  persisted input before the current contract is projected.
- Hosted guardrails for this cross-surface contract are
  `codex.core-subagent-model-pinning-targeted`
  (`failed_spawn_keeps_requested_identity_separate_from_terminal_effective_identity`
  and `multi_agent_v2_spawn_without_snapshot_omits_unobserved_effective_identity`),
  `codex.app-server-protocol-test`
  (`collab_spawn_identity_is_phase_compatible_across_current_and_historic_protocol_conversions`
  and `unknown_terminal_collab_spawn_serializes_all_identity_fields_as_null`),
  `codex.collab-spawn-identity-consumers-targeted`
  (`analytics_client_tests::collab_tool_item_analytics_keeps_requested_identity_from_the_started_event`
  and `runtime::usage::tests::usage_logger_preserves_optional_spawn_request_identity`),
  `codex.sdk-python-targeted`
  (`test_generated_files_are_up_to_date`,
  `test_thread_read_response_normalizes_only_legacy_collab_identity`,
  `test_legacy_collab_normalization_does_not_touch_opaque_tool_arguments`,
  `test_collab_identity_transport_keeps_current_and_partial_shapes_strict`, and
  `test_generated_collab_spawn_identity_is_required_nullable`),
  `codex.app-server-collab-spawn-identity-targeted`
  (`turn_start_emits_multi_agent_v1_spawn_requested_and_effective_identity_v2`
  and `turn_start_emits_multi_agent_v1_role_spawn_requested_and_effective_identity_v2`),
  and `codex.tui-collab-spawn-identity-targeted`
  (`replayed_collab_spawn_terminal_uses_only_explicit_effective_identity`,
  `replayed_historic_terminal_collab_spawn_renders_legacy_identity_as_effective`,
  `replayed_failed_collab_spawn_without_receiver_keeps_requested_identity`,
  `live_app_server_collab_spawn_completed_renders_requested_model_and_effort`,
  and `live_app_server_spawn_completion_does_not_fill_missing_effective_identity_from_metadata`).
- `collab-spawn-identity` has priority 10, while unmarked follow-up routes use
  the safe priority-0 default. Both the heavy-validation and light Rust CI
  selectors apply the same unique-highest-priority rule. For a change confined to its enumerated
  protocol, lifecycle, core-spawn, replay/rendering, schema, SDK, and carry
  paths, an identity implementation or SDK path lets that route win generic
  overlap and select `codex.core-subagent-model-pinning-targeted`,
  `codex.app-server-protocol-test`, the two protocol/UI identity lanes, the
  direct analytics/usage consumer lane, and `codex.sdk-python-targeted`.
  Core snapshot authority, protocol identity storage/conversion, analytics,
  state usage, and Python SDK changes are explicitly required route inputs so
  those authorities and consumers receive direct hosted coverage. Schema-only
  and docs/config-only changes do not trigger this route;
  only equal highest-priority matches fail closed. The route includes
  explicit-only lanes intentionally; it is not a broad default selection for
  unrelated app-server or TUI changes.
- Preserve the complete carry during future upstream syncs unless upstream
  supplies the same phase contract, historic terminal no-request fallback,
  current and historic conversion/replay behavior, TUI/analytics/usage
  consumers, schema and generated-client shapes, and equivalent regression
  coverage. When it does, drop the downstream identity slice and these
  dedicated guardrails together; do not retain a partial legacy-alias adapter.
- V2 requires `task_name`; when no effective reasoning effort is known it
  serializes `null` rather than manufacturing a `medium` value. Wait completion
  derives pending target ids from refreshed status snapshots, not the original
  requested-id list.
- `list_agents` is a first-class inventory tool on `carry/main`: the live handler is already on the upstream `multi_agents_v2` path, and the stale downstream `multi_agents/list_agents.rs` copy was dead carry rather than active behavior.
- The remaining inventory divergence is therefore not a separate handler path; it is the extra descendant and persisted edge-status plumbing available from `agent/control.rs`, which still needs to be re-homed onto the upstream-native v2 inventory shape rather than dropped.
- Downstream policy is to preserve the intent of the live carry while keeping the tree as close to upstream as possible; we explicitly carry the always-on, cheap live `list_agents` surface (including `has_active_subagents`/`active_subagent_count` and nested visibility/status metadata) to keep nested-agent live visibility intact, pair it with a richer, potentially stale `inspect_agent_tree` surface for deeper inventory sweeps, and welcome upstream-native reimplementation whenever it preserves these behaviors with less divergence.
- `inspect_agent_tree` now surfaces the richer tree inspection contract: it can toggle `live` vs `stale` descendant visibility, focus on selected `agent_roots`, and returns compact depth/row-limited tree rows so downstream observability stays explicit without replaying bulky historical snapshots.
- `wait_agent` adds `return_when=any|all` plus `requested_ids`, `pending_ids`,
  `completion_reason`, and `timed_out` so downstream joins happen on explicit
  tool contracts rather than transcript polling. These completion fields are
  public tool-output-only; canonical transcript items retain target identities
  and agent-state snapshots without duplicating timeout, mailbox, or pending
  outcome state. The v2 schema also permits omitting `targets` when the caller
  intentionally wants a current-turn input-activity wait, including mailbox
  delivery or user steering, or timeout.
- Full-history forks preserve conversation and agent identity while accepting
  configured or explicit child model/reasoning selection; only `agent_type`
  remains invalid for that fork shape. Upstream `c5779ed6bb` now requires the
  parent to be live and takes active history mode plus MultiAgentV2 usage hints
  from that parent; the existing model-pinning lane pins
  `spawn_agent_can_fork_parent_thread_history_with_sanitized_items` alongside
  downstream model/provider/reasoning precedence.
- Cold V2 descendant reloads preserve the child's indexed agent path, model,
  provider, and reasoning effort rather than inheriting the resumed root's
  selection. Rollout previews may supply history and display context, but
  cannot overwrite the complete indexed identity used to reload the child.
  Legacy rows with no
  indexed model identity retain their rollout model and effort, while a
  populated indexed model makes an absent indexed effort an intentional clear.
- A paginated V2 child reload also restores the newest persisted
  `approvals_reviewer` from its own `TurnContext` or `ThreadSettingsApplied`
  history instead of inheriting the reload caller's ambient reviewer.
  `paginated_subagent_fork_cold_resume_preserves_child_settings` proves model,
  provider, reasoning, reviewer, identity, and copied-history boundaries
  together; exact hosted run `29822598887` passed that named regression.
- Capacity-triggered V2 residency eviction first materializes and shuts down an
  unloadable quiescent thread, then removes only the exact `Arc<CodexThread>` it
  examined. A concurrently installed replacement is never removed by a stale
  eviction attempt.
- The evicted identity retains only a bounded `Completed`, `Errored`, or
  `Interrupted` status behind the current registry generation. Status queries
  and newly created subscriptions expose that cold snapshot, failed reloads
  keep it, and a successful reload clears it. Bridging an already-open cold
  subscription across reload remains separate follow-up work.
  This is a manual port of the useful mechanism from draft upstream PR #30154
  onto the current tree, extended to keep interrupted identities observable and
  reloadable.
- Each registered agent generation now owns one serialized lifecycle authority
  for V2 unload, reload, message delivery, and explicit close. Queue-only mail
  sent to an unloaded agent remains in a registry-owned FIFO without starting a
  runtime. Reloads serialize on a gate that eviction never acquires; residency
  reservation releases the mailbox lock and rechecks the registry generation
  before reload. A triggering follow-up transfers the FIFO first and retains it
  if reload fails.
- Residency eviction may move pending queue-only mail out of a completed,
  errored, or interrupted runtime instead of pinning that runtime indefinitely.
  Triggering mail remains live and blocks eviction; failed shutdown restores
  the exact drained FIFO, identity-checked replacement races receive the FIFO,
  and explicit close discards cold mail under the same lifecycle authority.
  Preserve this direct fix for upstream issue #32353 until upstream provides an
  equivalent generation-safe lifecycle and mailbox-transfer contract.
- Eviction fails closed for ephemeral V2 sessions without a durable rollout;
  capacity pressure leaves that runtime resident rather than manufacturing a
  cold identity that persisted-history reload cannot restore.
- The built-in downstream awaiter profile also raises its default background timeout and prefers longer blocking waits plus `list_agents` snapshots over repeated short polling from the model layer. The built-in `terminal-babysitter` role deliberately locks `gpt-5.6-luna` with low reasoning for bounded monitored-wait seams.
- Live `inspect_agent_tree` rows expose the effective model and reasoning effort from each loaded thread's configuration snapshot. Stale rows leave both fields null because persisted agent metadata does not prove a runtime configuration; these values are configuration evidence, not provider-usage proof.
- The TUI carries the same identity boundary into its activity and picker surfaces: V2 start activity includes the effective child model and reasoning effort, `/agent` and `/subagents` retain friendly names and canonical paths, and waiting rows say who they are waiting on with the known model/effort. Searchable picker rows hide inactive or stale sidecars by default while keeping them available through a `closed` search; the existing slash aliases remain unchanged.
- Queue-only `send_message` returns a structured receipt containing the canonical target plus effective model, provider, reasoning effort, and service tier when its runtime is already loaded. Those configuration fields are null for a cold or evicted target so the receipt preserves non-activating delivery. `handoff_state: queued` means the runtime accepted the handoff; it is deliberately not an agent acknowledgement or completion signal.
- Upstream harvest decision, refreshed 2026-07-22:

  | Candidate                                                        | Decision      | Reason                                                                                             |
  | ---------------------------------------------------------------- | ------------- | -------------------------------------------------------------------------------------------------- |
  | Current `openai/codex` main                                      | Carry locally | No equivalent terminal-babysitter Luna fallback or live inspect/send configuration receipt exists. |
  | Open and draft upstream PRs matching the runtime surfaces        | Carry locally | No relevant candidate was found.                                                                   |
  | Historical agent-identity auth/task stack reverted by `be757855` | Ignore        | It concerns backend identity and task lifecycle, not this model/configuration receipt seam.        |

- Primary files:
  - `codex-rs/analytics/src/analytics_client_tests.rs`
  - `codex-rs/analytics/src/reducer.rs`
  - `codex-rs/app-server-protocol/schema/json/`
  - `codex-rs/app-server-protocol/schema/typescript/v2/ThreadItem.ts`
  - `codex-rs/app-server-protocol/src/protocol/collab_agent_lifecycle.rs`
  - `codex-rs/app-server-protocol/src/protocol/collab_agent_lifecycle_tests.rs`
  - `codex-rs/app-server-protocol/src/protocol/event_mapping.rs`
  - `codex-rs/app-server-protocol/src/protocol/thread_history.rs`
  - `codex-rs/app-server-protocol/src/protocol/v2/item.rs`
  - `codex-rs/app-server-protocol/src/protocol/v2/tests.rs`
  - `codex-rs/app-server/tests/suite/v2/turn_start.rs`
  - `codex-rs/core/src/agent/builtins/awaiter.toml`
  - `codex-rs/core/src/agent/builtins/terminal-babysitter.toml`
  - `codex-rs/core/src/agent/control.rs`
  - `codex-rs/core/src/agent/control/legacy.rs`
  - `codex-rs/core/src/agent/control/lifecycle.rs`
  - `codex-rs/core/src/agent/control/residency.rs`
  - `codex-rs/core/src/agent/control/residency_tests.rs`
  - `codex-rs/core/src/agent/control/spawn.rs`
  - `codex-rs/core/src/agent/control_tests.rs`
  - `codex-rs/core/src/agent/lifecycle.rs`
  - `codex-rs/core/src/agent/mod.rs`
  - `codex-rs/core/src/agent/registry.rs`
  - `codex-rs/core/src/agent/registry_tests.rs`
  - `codex-rs/core/src/agent/role.rs`
  - `codex-rs/core/src/agent/role_tests.rs`
  - `codex-rs/config/src/config_toml.rs`
  - `codex-rs/core/config.schema.json`
  - `codex-rs/core/src/codex_delegate.rs`
  - `codex-rs/core/src/config/mod.rs`
  - `codex-rs/core/src/config/schema_tests.rs`
  - `codex-rs/core/src/session/input_queue.rs`
  - `codex-rs/core/src/session/mod.rs`
  - `codex-rs/core/src/tools/handlers/multi_agents_v2/list_agents.rs`
  - `codex-rs/core/src/tools/handlers/multi_agents_v2/message_tool.rs`
  - `codex-rs/protocol/src/items.rs`
  - `codex-rs/protocol/src/legacy_events.rs`
  - `codex-rs/protocol/src/protocol.rs`
  - `codex-rs/state/src/runtime/usage.rs`
  - `codex-rs/tui/src/chatwidget/protocol.rs`
  - `codex-rs/tui/src/chatwidget/replay.rs`
  - `codex-rs/tui/src/chatwidget/tests/app_server.rs`
  - `codex-rs/tui/src/chatwidget/tests/history_replay.rs`
  - `codex-rs/tui/src/multi_agents.rs`
  - `codex-rs/tui/src/app/agent_navigation.rs`
  - `codex-rs/tui/src/app/session_lifecycle.rs`
  - `codex-rs/tui/src/app/thread_routing.rs`
  - `codex-rs/tui/src/bottom_pane/list_selection_view.rs`
  - `codex-rs/core/src/tools/handlers/multi_agents/spawn.rs`
  - `codex-rs/core/src/tools/handlers/multi_agents_v2/spawn.rs`
  - `codex-rs/core/src/tools/handlers/multi_agents/wait.rs`
  - `codex-rs/core/src/tools/handlers/multi_agents_common.rs`
  - `codex-rs/core/src/tools/handlers/multi_agents_tests.rs`
  - `codex-rs/core/src/tools/handlers/multi_agents_spec.rs`
  - `codex-rs/core/src/tools/handlers/multi_agents_spec_tests.rs`
  - `codex-rs/core/src/tools/spec_plan.rs`
  - `codex-rs/core/src/tools/tool_runtime_capabilities.rs`
  - `codex-rs/core/src/thread_manager.rs`
  - `codex-rs/core/tests/suite/agent_execution.rs`
  - `codex-rs/core/tests/suite/spawn_agent_description.rs`
  - `codex-rs/core/tests/suite/multi_agent_resume.rs`
  - `codex-rs/core/tests/suite/subagent_notifications.rs`
  - `codex-rs/thread-store/src/local/read_thread.rs`
  - `.github/scripts/test_ci_planners.py`
  - `justfile`
  - `docs/config.md`
  - `docs/downstream-tool-surface-matrix.md`
  - `sdk/python/scripts/update_sdk_artifacts.py`
  - `sdk/python/src/openai_codex/generated/v2_all.py`
  - `sdk/python/tests/test_client_rpc_methods.py`

### Dead-Cwd Absolute Path Handling

- `AbsolutePathBuf::from_absolute_path()` avoids consulting process cwd for
  already-absolute inputs.
- This preserves path resolution after cwd disappears.
- Primary files:
  - `codex-rs/utils/absolute-path/src/lib.rs`
  - `codex-rs/utils/absolute-path/tests/dead_cwd.rs`

### Session Environment And Thread-Tail State

- Session environment updates validate duplicate and unknown environment ids
  before mutating stored session state.
- When a session cwd/environment update changes the legacy fallback cwd,
  the current upstream explicit-environment cwd and workspace-root semantics
  are authoritative. The superseded fallback-cwd rewrite implementation is not
  carried merely because it previously conflicted.
- Default turns refresh runtime `ThreadEnvironments` from stored selections so
  explicit empty or non-fallback stored environments are honored.
- Mailbox deferral must not overtake explicit steered user input, while
  response-only queued items may still defer after an answer boundary.
- Legacy active turns that only contain `UserMessageEvent` tails are still
  treated as mid-turn so replay/fork state does not discard the active start.
- Upstream `PermissionsState` owns permission-instruction diffing and retained
  fragment matching. Downstream custom realtime-start instructions remain the
  earlier world-state contribution relative to permissions, including remote
  compaction and resume reconstruction, so adopting upstream ownership does not
  silently replace the configured realtime wording.
- Upstream V3 `initialItems` seed the realtime backend's initial history
  independently. A nonempty seed must not replace, reorder, or suppress the
  downstream custom realtime-start world-state instruction on the next Codex
  request.
- Primary files:
  - `codex-rs/core/src/context/world_state/permissions.rs`
  - `codex-rs/core/src/session/input_queue.rs`
  - `codex-rs/core/src/session/mod.rs`
  - `codex-rs/core/src/session/session.rs`
  - `codex-rs/core/src/session/turn_context.rs`
  - `codex-rs/core/src/session/world_state.rs`
  - `codex-rs/core/tests/suite/compact_remote.rs`
  - `codex-rs/core/src/thread_manager.rs`

### Blocking Unified-Exec Waits And Compaction-Aware Turn Completion

- `exec_command` and `write_stdin` support blocking wait semantics via
  `wait_until_terminal`, `max_wait_ms`, and `heartbeat_interval_ms`.
- `write_stdin` still requires an empty `chars` payload when
  `wait_until_terminal=true`.
- Spec and regression guardrails also cover the surfaced wait fields, reject
  invalid `wait_until_terminal` types, and enforce the empty-`chars`
  requirement for blocking `write_stdin`.
- Timeout notes are appended to returned `raw_output`.
- Canonical command-execution items retain optional live `terminal_wait`
  metadata so upstream item-lifecycle adapters reproduce it on legacy begin
  and end events and app-server v2 projects it into command items. History
  reconstruction uses `None` when older persisted items do not contain that
  live-only detail.
- TUI command cells use `duration` rather than output presence as completion
  authority, so streamed output deltas remain active. Interrupting an
  unfinished command preserves its streamed output while marking it failed,
  and downstream `terminal_wait` metadata still drives the active wait label.
- The downstream intent is to absorb long-running shell waits in the tool layer
  instead of spending model turns on repeated short-poll status checks.
- Code-mode nested `exec_command` output follows the same model-policy bounded
  unified-exec summary shape before JavaScript observes `result.output`; do not
  restore raw large-output preservation expectations in code-mode tests when
  the tool response already carries truncation warning headers.
- Remote unified-exec command resolution must keep the session/user shell for
  commands that omit `shell`, while matching explicit aliases such as
  `powershell` reuse the selected environment shell instead of resolving that
  alias on the host running Codex.
- Code mode may expose the read-only `get_context_remaining` helper so scripts
  can inspect remaining budget, but interactive direct-model-only tools such as
  `request_user_input` remain hidden from nested execution.
- In local downstream workflows, this composes with existing blocking
  coordination primitives such as `wait_agent` and helper-backed `*_and_wait`
  calls so joins happen on state transitions rather than transcript churn.
- This blocking MCP tool pattern was carried downstream before task support was
  fully operational.
- `TurnCompleteEvent` retains upstream's optional structured terminal `error`
  payload alongside downstream `compaction_events_in_turn`, `final_model`, and
  `model_snapshot` metadata. Future conflict resolution must keep this additive
  union rather than choosing either field set.
- Token-count events also carry provider and model context in downstream flow.
- Sub-agent delegate forwarding should continue to surface `TokenCount` events
  back to the parent session; preserve this behavior even when re-homing the
  delegate code onto newer upstream structure.
- Provider `ServerOverloaded` responses use cancellable capacity-retry backoff
  for sampling, inline compaction, and remote compaction instead of terminating
  the turn immediately. Preserve the `capacity_retry` loops in
  `session/turn.rs`, `compact.rs`, and `compact_remote.rs` during upstream
  syncs.
- Primary files:
  - `codex-rs/core/src/capacity_retry.rs`
  - `codex-rs/core/src/session/turn.rs`
  - `codex-rs/core/src/compact.rs`
  - `codex-rs/core/src/compact_remote.rs`
  - `codex-rs/core/src/tools/spec_plan.rs`
  - `codex-rs/core/src/tools/handlers/unified_exec.rs`
  - `codex-rs/core/src/tools/handlers/unified_exec/exec_command.rs`
  - `codex-rs/core/src/tools/events.rs`
  - `codex-rs/app-server/src/bespoke_event_handling.rs`
  - `codex-rs/app-server-protocol/src/protocol/v2/item.rs`
  - `codex-rs/core/tests/suite/code_mode.rs`
  - `codex-rs/core/tests/suite/remote_env.rs`
  - `codex-rs/core/tests/suite/unified_exec.rs`
  - `codex-rs/protocol/src/items.rs`
  - `codex-rs/protocol/src/legacy_events.rs`
  - `codex-rs/protocol/src/protocol.rs`
  - `codex-rs/core/src/session/mod.rs`
  - `codex-rs/tui/src/chatwidget/command_lifecycle.rs`
  - `codex-rs/tui/src/chatwidget/tests/app_server.rs`
  - `codex-rs/tui/src/exec_cell/model.rs`
  - `codex-rs/tui/src/exec_cell/render.rs`
  - `docs/downstream.md`
  - `docs/downstream-regression-matrix.md`

### Source-Owned Unified-Exec Final Transcript

- Unified exec records each source output chunk in a bounded, non-draining
  final transcript before offering it to the drainable response buffer or the
  best-effort delta broadcast.
- Normal command completion waits for the local stdout/stderr sources or the
  exec-server event source to close, then drains already-published delta
  messages before emitting `ExecCommandEnd`.
- Source draining is bounded by the established exec I/O drain timeout so a
  daemonized descendant or broken backend cannot suppress the final event.
  Chunks recorded before that deadline remain authoritative; output produced
  only after the deadline is intentionally outside the completed transcript.
- Exec-server exit events start that bounded drain immediately even when
  inherited descriptors delay the later source-closed event.
- The bounded transcript intentionally retains a head and tail and represents
  discarded middle bytes with an omission marker; this carry prevents delta
  lag from silently losing final output or corrupting that accounting.
- Drop this direct upstream fix when upstream provides equivalent source-owned
  bounded final aggregation, bounded source-close ordering, and regression
  coverage.
- Upstream `9fc715c086` is adopted as the lifecycle-ordering authority: its
  per-process interaction lock serializes `write_stdin` interaction events
  before command completion, its close guard and trailing-output grace order
  streaming shutdown, and its exit watcher waits for deferred network-denial
  classification. Downstream keeps the source-owned bounded transcript and
  bounded source-task join as the final-output authority.
- `WriteStdinInteractionEvent` is the only interaction-publication seam. It
  carries downstream `terminal_wait` metadata and an explicit
  `emit_when_process_exited` flag for blocking waits; do not restore a second
  handler-side `TerminalInteraction` emitter during future syncs. Internal
  blocking-wait polls pass no interaction event and retain the shorter
  `empty_input_min_yield_time_ms` window.
- Primary files:
  - `codex-rs/core/src/unified_exec/process.rs`
  - `codex-rs/core/src/unified_exec/async_watcher.rs`
  - `codex-rs/core/src/unified_exec/process_manager.rs`
  - `codex-rs/core/src/unified_exec/process_manager_tests.rs`
  - `codex-rs/core/src/unified_exec/process_output_tests.rs`
  - `codex-rs/core/tests/suite/unified_exec.rs`
  - `justfile`
  - `.github/validation-lanes.json`

### App-Server Remote Control Account Wake

- Remote-control enrollment waits must wake when cached ChatGPT auth changes
  the account id, not only when refresh-token material changes.
- `AuthManager::auth_change_receiver()` is therefore gated by the account- and
  token-aware `auth_changed_for_refresh` predicate. Do not gate observers with
  `CodexAuth::PartialEq`: managed ChatGPT auth equality collapses to auth mode
  and misses account-only changes.
- This prevents remote control from sleeping until the retry interval after an
  account-id-only reload, and keeps `UnauthorizedRecovery` aligned with the
  fresh auth state before reconnect/enroll attempts.
- Signed commit `36da0efb9c` restores that upstream-shaped predicate after
  targeted hosted run `29936369078` exposed the regression through the complete
  git-attribution authorization-recovery seam.
- Primary files:
  - `codex-rs/app-server-transport/src/transport/remote_control/websocket.rs`
  - `codex-rs/login/src/auth/manager.rs`

### Native Computer-Use Adapter Bridge

- Downstream promotes bare `android_observe`, `android_step`,
  `android_install_build_from_run`, `browser_observe`, `browser_step`,
  `desktop_observe`, and `desktop_step` dynamic tools into first-party native
  computer-use function tools with Codex-owned schemas.
- The Android reference bridge exposes atomic two-to-five-pointer
  `multi_touch` input through `android.input.multi_touch`, validates the whole
  gesture before dispatch, and reports an explicit unsupported-capability
  failure instead of synthesizing sequential single-touch input.
- Evaluate browser equivalence at the stock CLI/runtime boundary, not from the
  existence of an upstream app feature or feature flag. Upstream product
  Browser Use (`@Browser`) and the official signed-in Chrome integration
  (`@Chrome`) are app-supplied surfaces. Upstream generic dynamic-tool plumbing
  can carry browser-like tools when an external host supplies them, but that is
  not equivalent to downstream CLI/TUI provider discovery and advertisement.
- Downstream `browser_observe` and `browser_step` are the stable bare native
  contract. "Native" means Codex owns their schemas, transcript events, image
  output, provider dispatch, and lifecycle; the configured runtime may still
  use Playwright. Likewise, `backend: "chrome"` selects a configured provider
  backend and does not itself select upstream `@Chrome`, the official
  extension, or a user's regular signed-in profile.
- Do not drop this browser carry merely because upstream ships Browser Use,
  Chrome integration, `browser_use` feature controls, or generic host-injected
  dynamic tools. Treat upstream as equivalent only when the stock CLI owns an
  equivalent bare-tool schema, provider discovery and advertisement, native
  image/transcript semantics, and start/resume/fork propagation. Port these
  guarantees over upstream dynamic-tool representation changes during sync.
- Hosted `codex.native-browser-evidence-targeted` preserves the browser carry's
  viewport-ranked maps, per-image geometry receipts, optional bounded
  inspection metadata, and scroll trail agreement through real Chromium tests.
  It also preserves the
  focused Node/Playwright evidence boundary and validates the generic
  `use-native-browser` skill. It runs the provider's Node tests in hosted
  Chromium without treating provider artifacts or text summaries as native
  screenshots. Drop this lane only when an upstream-equivalent hosted seam
  proves the same checks.
- Namespaced Android-like, browser-like, or desktop-like tools remain ordinary dynamic tools
  so app-specific providers can keep their own tool surfaces without taking
  over the native Codex contract.
- App-server `dynamicTools` accepts a deferred bare native tool so it can be
  discovered through `tool_search`; the same bare deferred shape remains
  invalid for ordinary dynamic tools. A capability-bearing native tool forces
  a loaded-thread resume reload because its provider contract may have changed.
- Upstream's reference-backed paginated `thread/fork` path is retained. The
  app-server forwards request-scoped dynamic tools through both the copied and
  prepared fork shapes, while `ThreadManager` keeps upstream's prepared-fork
  reservation release and persistence boundary. Do not route a prepared fork
  through the copied-history helper or substitute an empty tool list: that
  would create a valid child whose next model request silently loses its
  requested native provider surface.
- `paginated_thread_fork_injects_native_android_tools_into_model_requests`
  is the focused regression guard for that combined seam. It proves a
  reference-backed child advertises `android_step` on its first model request.
- The upstream `history_base` field is explicit at every `CreateThreadParams`
  construction: ordinary and copied-history fixtures use `None`, while only
  reference-backed paginated forks persist the inherited base. This keeps
  test-only constructors aligned with the storage contract instead of
  reintroducing an implicit legacy default.
- The merged workspace still retains an older transitive `zstd` package, so
  the new thread-store dependency is recorded as `zstd 0.13.3` in
  `Cargo.lock`. Keep that qualified lock edge while both versions coexist;
  regenerating the full graph merely to resolve the ambiguity would introduce
  unrelated dependency upgrades.
- This sync intentionally retains the flat `DynamicToolSpec` compatibility
  shape (`namespace` plus function metadata) because app-server requests,
  persisted SQLite rows, provider registries, and resume filtering still share
  that representation. Upstream's tagged function/namespace model should land
  only through the tracked Dynamic Tools alignment work with lossless legacy
  ingestion and state migration, not as an incidental conflict resolution.
- `codex-core` owns `ComputerUseCallRequest` and
  `ComputerUseCallResponse` events, pending response registration, timeout
  cleanup, success/error projection, adapter selection, mutating
  classification, install-specific timeout selection, and hook payload
  formatting.
- App-server API v2 owns `item/computerUse/call`, response forwarding, and
  `ThreadItem::ComputerUseCall` start/completion projection.
- Dispatch resolves the primary environment from the refreshed per-step
  snapshot, not the frozen turn-start snapshot, so a provider that becomes
  ready after turn creation is immediately available to native computer-use.
- The active TUI session renders native computer-use items from live protocol
  events. Computer-use events are transient, so thread history and snapshots do
  not replay them after resume. The TUI provider registry handles Android and
  routes browser calls to either a configured provider command or the built-in
  Playwright provider for `backend=auto`; when that browser provider is
  configured, CLI/TUI thread start, resume, and fork requests advertise
  `browser_observe` and `browser_step` automatically. The Playwright bridge
  supports accessibility-oriented selectors plus human-like mouse and keyboard
  primitives, defaults to per-thread browser profile isolation for concurrent
  sidecars, can return visible-control metadata and selector candidates for UX
  loops, can save redacted audit artifacts, can use locally configured
  service-account navigation headers for allowed hosts, and can still be
  configured for shared, environment-scoped, or per-call profiles when that
  lifecycle is intentional. Thread-spawned agents
  inherit the parent thread's native dynamic tools, so browser-capable sidecars
  receive the native browser surface rather than silently dropping to a
  compatibility adapter.
- The Android adapter is retained as the reference MCP-backed runtime provider:
  reuse `android-emulator-mcp` or a successor when it exposes the current
  Android MCP contract, and adapt harness-specific behavior provider-side
  rather than in hot Codex core paths.
- The desktop adapter is the independent provider seam for macOS Screen
  Recording/Accessibility-style runtimes and future native desktop providers.
  TUI dispatch stays behind operator-configured command providers, selected by
  the platform-filtered desktop provider registry.
- `codex doctor` includes read-only native provider diagnostics for browser
  provider configuration, headed display/Chrome fields, and Android provider
  endpoint/credential shape without launching browsers, connecting to profiles,
  or starting emulator sessions.
- Android screenshots and browser viewport captures are expected to reach the
  model as native image content items. Provider artifact paths are kept for
  diagnostics, audit, or replay; they are not the normal model-facing visual
  channel.
- Nested code-mode calls retain native computer-use responses as a typed
  `{ content, success }` object. Callers forward an `input_image` item with
  `image(...)` and never serialize the complete image-bearing result to text.
  Failed provider responses preserve the same typed content contract, including
  diagnostic text and any screenshot returned with the failure.
- Upstream dynamic-tool and code-mode audio output is adopted as
  `InputAudio { audio_url }` and the `audio(...)` helper. Upstream now also owns
  audio preparation, duration-aware context accounting, output truncation,
  compaction, and replay through user and tool history. Those paths are
  additive to the downstream optional `detail` carried on dynamic
  `InputImage`; app-server events, stored thread items, generated schemas, and
  JSONL previews preserve both. The two upstream dynamic-tool audio fixtures
  use downstream's flat `DynamicToolSpec` compatibility record without
  restoring the tagged namespace shape. Regenerate Cargo and Bazel locks from
  the merged dependency graph rather than unioning parent entries: upstream's
  Symphonia lock can name a `bitflags` version that the downstream graph has
  already superseded. Native computer-use responses remain text/image-only and
  keep their fail-loud screenshot contract.
- Computer-use events remain transient in every history mode; live rollout
  tracing maps them to tool-runtime start/end boundaries without writing them
  into thread snapshots.
- Occurrence search continues to index only user messages and final agent
  messages. Its exhaustive `ThreadItem` classification explicitly excludes
  `ComputerUseCall` alongside other tool-call items, preserving the upstream
  search contract without dropping downstream enum coverage at compile time.
- Runtime providers own Android sessions, browser sessions, screenshots,
  viewport capture, UI digests, input execution, and provider-side build
  installation. Solar Gravity Lab is a proving and consumer app, not the
  generic owner of Codex computer-use tooling.
- The built-in Playwright browser provider clears Chromium tab-session restore
  artifacts before opening a persistent browser context, so stale restored tabs
  cannot hit an old localhost target before an explicit requested navigation.
  Provider-managed `state.json`, cookies, local storage, and other profile data
  are preserved.
- Plugin app declarations are validated on the authenticated ChatGPT app
  projection. The unauthenticated plugin projection may intentionally omit apps,
  so future syncs should not treat app absence from unauthenticated manager
  tests as evidence that downstream app/plugin carry is removable.
- Primary files:
  - `codex-rs/protocol/src/computer_use.rs`
  - `codex-rs/protocol/src/protocol.rs`
  - `codex-rs/tools/src/android_tool.rs`
  - `codex-rs/tools/src/browser_tool.rs`
  - `codex-rs/tools/src/computer_use_tool.rs`
  - `codex-rs/tools/src/desktop_tool.rs`
  - `codex-rs/core-plugins/src/lib.rs`
  - `codex-rs/core/src/tools/handlers/computer_use.rs`
  - `codex-rs/core/src/tools/handlers/computer_use_code_mode.rs`
  - `codex-rs/code-mode-protocol/src/description.rs`
  - `codex-rs/core/tests/suite/code_mode.rs`
  - `.github/validation-lanes.json`
  - `.github/scripts/test_ci_planners.py`
  - `justfile`
  - `codex-rs/tools/src/tool_search.rs`
  - `codex-rs/app-server/src/computer_use.rs`
  - `codex-rs/app-server/src/bespoke_event_handling.rs`
  - `codex-rs/app-server-protocol/src/protocol/common.rs`
  - `codex-rs/app-server-protocol/src/protocol/v2/item.rs`
  - `codex-rs/app-server-protocol/src/protocol/thread_history.rs`
  - `codex-rs/thread-store/src/local/thread_history/search.rs`
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
  - `docs/native-computer-use.md`
  - `docs/native-computer-use-contracts.md`

### Review And History Accounting Alignment

- Review and history token summaries use a stable unavailable fallback string
  instead of ad hoc formatting.
- Review-mode accounting remains aligned with live runtime state rather than
  stale defaults.
- Primary files:
  - `codex-rs/protocol/src/protocol.rs`
  - `codex-rs/core/src/guardian/review_session.rs`
  - `codex-rs/core/src/context_manager/history.rs`
  - `docs/downstream.md`

### Direct And App-Backed MCP Tool Catalog Reconciliation

- When a direct MCP server and an app connector expose the same logical
  callable, Codex compares the complete model-visible contract before reducing
  the catalog to one route.
- Exact matches prefer the direct MCP route and make that routing decision
  visible in the namespace description. Direct-only and app-only environments
  remain unchanged, so the app route is still a working fallback.
- A difference in descriptions, input or output schemas, safety annotations,
  or task execution keeps both routes visible, labels both namespaces, and
  emits a structured warning. Routing names, connector metadata, icons, and
  server scheduling policy are provenance rather than callable-contract fields.
- Primary files:
  - `codex-rs/core/src/mcp_tool_exposure.rs`
  - `codex-rs/core/src/mcp_tool_exposure_test.rs`
  - `.github/scripts/test_ci_planners.py`
  - `.github/validation-lanes.json`
  - `.github/workflows/sedna-heavy-tests.yml`
  - `justfile`
  - `docs/downstream-tool-surface-matrix.md`

### Complete MCP Tool Catalogue Collection And Refresh

- MCP pagination and host-side deferred loading are separate contracts. Codex
  drains every non-null opaque `tools/list` cursor, including the empty string,
  before publishing a catalogue for ordinary exposure or deferred tool search.
- Collection fails closed on cursor cycles, duplicate tool names, more than 64
  pages, or more than 10,000 tools. A partial walk is never published.
- Tool-list change notifications advance a generation. Codex discards and
  retries a walk that crosses generations, atomically swaps only a complete
  replacement, and retains the last complete snapshot when refresh fails.
- Upstream commit `65f8bf6853` binds prepared calls to the exact client and
  catalog revision advertised to a model step. A later catalog revision rejects
  an obsolete prepared call before it reaches the server; it does not reroute
  an already advertised callable through a replacement connection.
- The 2026-07-22 integration keeps that upstream authority fence while retaining
  downstream raw complete snapshots and last-known-good failure behavior. Every
  successful `list_changed` publication observed by tool exposure or binding
  capture advances the shared manager revision. Per-client filtering happens
  only after the raw snapshot is published, so one client's filter cannot
  erase another client's catalog or cache entry.
- A hard Codex Apps refresh captures its raw replacement before publication,
  publishes it under the same revision write lock as the app override, and
  then exposes the model-filtered tools. This keeps the app override local to
  the newly created binding even if a shared cache publication races later.
- The 2026-07-15 sync re-homes this contract on upstream's
  `connector_runtime` and `tool_catalog_cache` ownership. Startup, hard-refresh,
  and list-changed fetches publish the raw complete catalogue to the applicable
  shared cache before per-client filtering; the removed `codex_apps_cache.rs`
  implementation is not carried.
- Test fixtures that instantiate `ManagedClient` must mirror the upstream
  revisioned catalogue shape: an atomic complete `ToolCatalogueSnapshot`, its
  refresh lock, and explicit server identity. They must not restore the removed
  mutable `tools` field merely to keep downstream binding coverage compiling.
- Upstream commits `3307ea8b63` and `1bbdb32789` are adopted as complementary
  cache safety: a server can disable shared tool-catalog caching, remotely
  sourced environment variables bypass that cache, and cached definitions do
  not substitute stale process metadata or output for the live connection.
  These guarantees narrow the carry but do not replace downstream's bounded
  pagination and atomic `list_changed` refresh contract.
- The 2026-07-18 sync also adopts upstream commit `f24e695470`'s thread-level
  `McpRuntime` ownership. `McpRuntime` is the sole owner of live connections;
  immutable `McpRuntimeSnapshot` values pair projected config with the
  connection set used by an in-flight step, so refresh must not eagerly shut
  down the previous snapshot. Downstream catalogue pagination, OAuth backend,
  safety policy, and environment-scoped projection continue through this
  upstream-owned runtime rather than a second manager mirror.
- The 2026-07-22 sync adopts upstream commit `84d2b203ed`'s split authority:
  prepared tool calls remain bound to the exact client and catalogue revision
  advertised for their model step, while session-scoped `McpResourceClient`
  clones resolve each resource operation and cache key from the latest
  published `McpRuntime` snapshot. Do not restore per-binding resource clients;
  `out_of_band_resource_read_reconciles_the_published_mcp_runtime` is the
  focused hosted
  regression for refresh behavior.
- All-server resource and resource-template listings use the same immutable
  step binding. A configured server without an exact ready client, or a list
  failure through a captured client, leaves ready-server results available and
  adds a bounded `failures` entry with `notReady` or `listFailed`. Complete
  results retain their previous shape. Explicit unavailable-server requests
  still fail, and only a later binding may expose a recovered client; neither
  path substitutes cached authority or retargets a prepared call.
- Upstream commit `65ae4c26e0` registers disabled-by-default experimental
  feature `mcp_2026_07_28` in core, the generated config schema, and app-server
  feature enablement. It does not yet change the runtime resource or prepared
  call contracts above; adopt the registration unchanged so later protocol
  implementation can remain upstream-owned.
- `list_all_tools()` treats a fresh shared cache snapshot, including an
  intentionally empty catalog, as the nonblocking inventory while startup is
  pending. It refreshes only after startup is ready or no cache exists, so a
  pending client cannot make the published catalog unreachable before tool
  exposure or inference initialization. `mcp-tool-exposure-targeted` runs the
  two shared-cache ordering regressions directly on GitHub-hosted validation.
- Centralized ownership does not by itself unload quiescent threads retained by
  `ThreadManager`. Capacity-triggered V2 residency eviction now calls
  `shutdown_and_wait()` before generation-fenced, thread-instance-checked
  removal so runtime-backed resource access does not retain the evicted
  connection set.
  Timed idle eviction remains follow-up work and must retain the capacity path's
  existing active-turn and pending-mailbox guards; idle timestamps,
  configuration, and operator observability remain outside this bounded slice.
- The Streamable HTTP regression performs deferred `tool_search` for a tool
  supplied only on page two, invokes that tool, and verifies its output.
- The 2026-07-23 sync adopts upstream `e497325a6a` as the sole owner of
  thread MCP runtime state. Configuration updates mark that one runtime dirty;
  refresh rebuilds and atomically publishes a replacement while immutable
  per-step bindings keep their captured connection set. Keep downstream
  pagination, OAuth-store selection, and cache ordering inside that runtime;
  do not restore a session-level config/connection mirror.
- The 2026-07-24 integration adopts upstream `ef2d3edb95`'s background
  prewarm while preserving that step boundary during execution. Tool dispatch
  prepares a call directly from `StepContext::mcp`; it must not refresh and
  recapture a binding per call. A prepared RPC holds catalogue read authority
  through the call, whereas recapture refresh needs the competing write
  authority and would serialize calls that the model and server permit to run
  in parallel. The retained end-to-end event-order regressions are
  `stdio_mcp_read_only_tool_calls_run_concurrently_without_server_opt_in` and
  `stdio_mcp_parallel_tool_calls_opt_in_runs_concurrently`.
- The same integration through upstream `000d2540ad` adopts
  `3645a4397c`'s startup refresh coordination and `000d2540ad`'s current
  runtime authority for Guardian elicitation review. Refresh invalidation and
  publication remain serialized inside the upstream `McpRefresh` owner, while
  each model-visible call remains prepared from its immutable step binding.
  Elicitation approval reads the current runtime configuration rather than a
  stale turn snapshot. Do not reintroduce a session-level mirror or a per-call
  binding refresh around either path.
- Upstream `6e0455fdc4` sends `codex-mcp-client/<version>` on default
  Streamable HTTP and OAuth requests. An explicitly configured `user-agent`
  header remains authoritative. The focused regressions are
  `streamable_http_requests_preserve_configured_user_agent` and
  `refreshes_expired_persisted_token_before_initialize`.
- Preserve this carry until upstream issue #26094 is resolved by behavior that
  covers the complete bounded snapshot and refresh contract, not only a basic
  happy-path page walk.
- Primary files:
  - `codex-rs/rmcp-client/src/rmcp_client.rs`
  - `codex-rs/connectors/src/connector_runtime/mod.rs`
  - `codex-rs/codex-mcp/src/binding.rs`
  - `codex-rs/codex-mcp/src/binding_clients.rs`
  - `codex-rs/codex-mcp/src/lib.rs`
  - `codex-rs/codex-mcp/src/connection_manager/tool_catalog.rs`
  - `codex-rs/codex-mcp/src/runtime.rs`
  - `codex-rs/codex-mcp/src/resource_client.rs`
  - `codex-rs/codex-mcp/src/rmcp_client.rs`
  - `codex-rs/codex-mcp/src/rmcp_client_tests.rs`
  - `codex-rs/codex-mcp/src/tool_catalog_cache.rs`
  - `codex-rs/app-server/tests/suite/v2/mcp_server_status.rs`
  - `codex-rs/core/src/mcp_tool_call.rs`
  - `codex-rs/core/src/tools/handlers/mcp_resource.rs`
  - `codex-rs/core/src/tools/handlers/mcp_resource_spec.rs`
  - `codex-rs/core/src/tools/handlers/mcp_resource_spec_tests.rs`
  - `codex-rs/core/src/tools/handlers/mcp_resource_tests.rs`
  - `codex-rs/core/tests/suite/mcp_tool_cache.rs`
  - `codex-rs/core/tests/suite/rmcp_client.rs`
  - `codex-rs/core/src/session/mcp.rs`
  - `codex-rs/core/src/session/mcp_refresh.rs`
  - `codex-rs/core/src/state/service.rs`

### MCP Transient Startup Recovery

- An ordinary Streamable HTTP MCP server that fails its initial startup can
  recover during the same Codex session. A later tool-list or binding capture
  starts one bounded background retry; it does not require an operator restart
  and does not wait on the retry before preserving already-ready servers in
  that step. Stdio startup remains one-shot.
- The reconnect state is single-flight and applies exponential backoff after a
  failed retry. A successful client becomes the exact live catalogue and call
  authority for the next binding.
- Cancellation and shutdown fence the reconnect generation. A client that
  finishes after cancellation is shut down instead of being installed, and a
  recovered client found closed is retired before another bounded retry.
- Upstream cached optional-startup, lazy-subagent, OAuth reauthentication, and
  startup-status fixes are complementary but do not provide this generic
  ordinary-failure recovery contract on the maintained downstream runtime.
- `codex.mcp-tool-exposure-targeted` proves the lifecycle with deterministic
  connection-manager tests and literal Streamable HTTP initial-failure and
  cancellation integration tests.
- Primary files:
  - `codex-rs/codex-mcp/src/connection_manager.rs`
  - `codex-rs/codex-mcp/src/connection_manager/tool_catalog.rs`
  - `codex-rs/codex-mcp/src/rmcp_client.rs`
  - `codex-rs/codex-mcp/src/connection_manager_tests.rs`
  - `codex-rs/core/tests/suite/rmcp_client.rs`
  - `codex-rs/rmcp-client/src/bin/test_streamable_http_server.rs`

### 2026-07-23 Upstream Integration Boundaries

- Upstream `4462b9deef` adds default-on
  `features.multi_agent_v2.wait_agent_enabled`, independently of the sleep
  tool. The optional downstream schema field remains nullable, and this gate
  only controls exposure of `wait_agent`; it does not alter downstream's
  upstream-native explicit model and reasoning selection contract. Preserve
  `multi_agent_v2_can_disable_wait_agent` and
  `multi_agent_v2_wait_agent_tool_follows_configuration` in the hosted
  sub-agent surface lane.
- The upstream exact namespace assertion is extended only with downstream
  `inspect_agent_tree` when `wait_agent` is disabled. It must continue to prove
  that `wait_agent` and `clock.sleep` are absent; do not satisfy the test by
  dropping the independent inspection surface.
- The upstream `wait_agent` configuration test must read the resolved V2 tool
  namespace from its test configuration. Downstream deliberately uses `agents`;
  upstream's literal `collaboration` is a reserved namespace here. This is a
  test-only compatibility adaptation and must retain all four wait/sleep gate
  permutations rather than changing the runtime namespace.
- Hosted run `30017713903` reached
  `suite::rmcp_client::streamable_http_with_oauth_round_trip` and overflowed
  its Tokio worker thread. The upstream test already allocates an 8 MiB outer
  thread, so the nested one-worker runtime must receive that same explicit
  stack budget and `codex.mcp-safety-targeted` must propagate the repository
  standard `RUST_MIN_STACK` value to its process family. This is test-harness
  stability only; preserve the exact OAuth round-trip coverage and validate it
  in `codex.mcp-safety-targeted`.
- Hosted run `30026968799` exhausted its GitHub-hosted runner disk while
  compiling the app-server V2 contract lane; this is not a source failure.
  The reusable Rust-integration lane must proactively discard its unused hosted
  SDK/toolchain bundles and reject a runner below the 12 GiB Rust safety floor,
  matching the established Rust-batch policy before Cargo expands `target/`.
- Upstream `39a2438d16` makes verified `releases.openai.com` metadata and
  assets the default only for the unconfigured `openai/codex` plus `rust-v`
  origin, with validated GitHub Release fallback. The downstream repository and
  tag-prefix boundary remains deliberate: custom origins bypass the OpenAI-only
  endpoint and retain GitHub routing, while the broad SemVer validator keeps
  Sedna prerelease and build-metadata releases installable.
- Upstream `48ebbf5334` selects the API curated plugin marketplace from the
  resolved Bedrock provider rather than only from configured defaults. Retain
  the upstream provider-direction regression
  `list_marketplaces_uses_resolved_provider_instead_of_configured_default` and
  the existing API-curated cache and skills tests; do not reintroduce a second
  marketplace-selection rule in downstream configuration code.
- Upstream `808d3c2702` treats config batches containing only future-session
  defaults (`model`, model reasoning effort, plan-mode reasoning effort,
  service tier, or personality) as static updates: they must not reload skills
  or plugins for an active session. Explicit user-layer reloads still invalidate
  both caches. Preserve
  `skills_list_uses_cached_result_after_session_default_writes_until_force_reload`
  in the app-server V2 contract lane.
- The `808d3c2702` merge also restores the upstream local-environment hook
  shell selection that an earlier merge resolution had inadvertently replaced
  with the legacy session shell. This is upstream parity, not downstream carry:
  hooks derive their shell from the selected local turn environment when one is
  available and otherwise retain the upstream empty-shell behavior.
- Signed merge `120d4314a2` preserves `48ebbf5334` as its second parent. The
  follow-up documentation commit records validation coverage only and does not
  change runtime behavior.
- Signed merge `b0fdc2cb2b` preserves `808d3c2702` as its second parent.
- Frontier Max run `30013991321` on `dc7d40ca3e` found two independent
  upstream-integration repairs: the session-initialization hook caller still
  passed the legacy shell after the environment-aware hook builder was restored,
  and the generated JSON schema fixture tree was stale. Signed commit
  `b144fc2f5c` restores the exact selected-local-environment caller.
- GitHub-hosted generator run `30015826457` was based on `b144fc2f5c` and
  produced a 14-file, schema-only patch with SHA-256
  `5b0edbb069f2a4fde9e09b58a90a9f4d1d91242d87a97b09f28d1ab3f3faba6f`.
  It updates four `reloadUserConfig` description wraps and ten generated final
  newline normalizations; signed commit `dabdc88354` applies the complete
  verified output. Do not hand-edit or partially select this fixture set.
- Upstream `e19e65317a` and `34b935e3e5` reuse a ready MCP connection across
  runtime reconciliation and replace a closed one. Preserve that upstream
  connection/view split: `McpServerConnection` owns the reusable transport and
  raw complete catalogue, while `McpServerView` owns the current filter,
  timeout, metadata, and provenance projection. A successor connection set
  clones the predecessor's manager catalogue-revision `Arc` only when it
  actually reuses a connection; a fully replaced set receives a fresh revision.
  This retains upstream view-only refresh behavior while a later
  `list_changed` rejects prepared calls from either runtime generation.
- The connection-local snapshot must remain the exact unfiltered tools fetched
  from that live client. Connector-runtime and regular catalogue cache winners
  remain discovery fallbacks during pending or failed startup and must never
  replace binding authority. Hard refresh and `list_changed` share the client
  refresh lock; a newer successful notification clears the older Apps override
  before advancing the shared revision. Preserve the raw-snapshot, revision,
  cache-race, and view-reconciliation regressions in the hosted MCP lane.
- GitHub-hosted schema-generation run `30038302868` ran the pinned generator
  against signed integration head `de98effd7d` through temporary signed branch
  commit `6a3ec97d31`. Its verified artifact SHA-256 is
  `580c63758e81be116f6d37ac35082517671ba15c51735690a5bff72bd94318b7`
  and replaces the complete generated six-file JSON subset for the combined
  Schemars 1.2 and external-agent import-history sources. The temporary branch
  contains only the generator workflow and is removed after this receipt is
  applied; do not recreate these fixtures by hand.
- Signed merge `9aabe48158` preserves upstream `94ebae725e` as its second
  parent. It adopts upstream plugin-script attribution, refreshed Apps-tool
  persistence, configurable SQLite homes, Browser Use requirements,
  configurable `update_plan`, and proxy-aware exec-server WebSockets. The
  retained carries are disjoint: state collision repair and migration ordering,
  interrupted V2-agent reload, App authentication propagation before plugin
  loading, terminal-wait event fields alongside upstream plugin attribution,
  and native computer-use, dynamic-tool, and image-item coverage.
- Ready MCP clients are the exact binding authority. Shared cache snapshots are
  discovery fallbacks only while startup is pending or a live client is absent;
  `capture_binding_uses_the_ready_clients_own_tools` must never regress to
  advertising a stale shared-cache tool for a ready client.
- `codex.mcp-tool-exposure-targeted` includes the ready-client authority check
  plus both RMCP overlap tests. The read-only and explicit server-parallel
  paths must emit both call-begin events before either call-end event; the
  separate mutable default-false test remains the serial baseline.
- Signed merge `ff5c3efd1a` preserves upstream `3947f0d0c3` as its second
  parent. It adopts upstream deferred-tool world-state source listings,
  source-listing-aware tool-search caching, and the exec-server HTTP transport
  decoupling without adding a downstream transport fork.
- The tool-search union keeps the upstream prebuilt, source-listing-aware
  `ToolSpec` and cache key. Downstream normalized entries remain only for exact
  identifier ranking and namespace coalescing, including deferred dynamic
  tools and native image-search results; they must not restore duplicate source
  listings or bypass upstream world-state advertisement.
- GitHub-hosted schema generation run `30059844196` consumed exact integration
  source `9e67309024` and produced a complete artifact with SHA-256
  `4cf69ec6fc7260a644f8a57ff108a27e3f4d5244f2f6abbf13615ac416596b31`.
  Signed commit `f3e6877afb` applies all three outputs together: one workspace
  lockfile edge and the JSON and TypeScript `ConfigRequirements` fixtures. The
  JSON fixture uses the current `$defs` Browser Use reference and the TypeScript
  map signatures match the generator. Do not hand-edit or partially select
  this artifact.
- Signed merge `9092762358` preserves upstream `f47f28cd0d` as its second
  parent. It adopts upstream's CLI snapshot runfiles and Windows-only sandbox
  binary-test declaration. The upstream commit omitted the corresponding
  `codex_rust_crate` macro parameter and failed its own hosted Bazel analysis, so
  downstream temporarily accepts that platform constraint and applies it to the
  generated public unit-test wrapper. The inner binary deliberately remains
  analyzable by cross-platform lint targets; constraining it caused Linux Bazel
  clippy to reject an explicitly selected incompatible target. Drop the shim
  when upstream supplies the missing macro support. The argument-label changes
  from that upstream commit were already present in the integrated tree, so this
  boundary adds no downstream lint carry or duplicate test edits.
- Signed merge `9e5b6909de` incorporates the landed runtime-receipt carry from
  `origin/main` through `4614b8037f`. Its documentation resolution is a union:
  Luna-low terminal babysitting and live inspect/send receipts remain, together
  with the newer paginated cold-reload and policy-removal guarantees.
- Signed merge `74c1d89ee4` preserves upstream `0dfa778dae` as its second
  parent and adopts the WebSocket transport for the code-mode host without a
  downstream transport fork.
- The same hosted blocking run exposed two independent downstream integration
  repairs. `features_schema` must insert `NonPrefixedMcpToolNames` into the
  Schemars 1.2 `properties` map, and
  `codex.mcp-tool-exposure-targeted` must declare `needs_nextest` because its
  existing `just` recipe invokes `cargo nextest`. Existing config-schema and CI
  planner tests are the regression guards.
- GitHub-hosted format-generation run `30062964365` used the repository's
  pinned nightly rustfmt and Prettier against exact candidate
  `f0b43d9719466f21f3743b0c8980c15160d531e5`. Temporary workflow commit
  `0d366cc86f7eb14be9e3bd6e1e7eb42da60fc7f8` produced the complete six-file
  patch with SHA-256
  `8f2843a661bcb2a7371c8485a9f820c907a85461bacb0f2b5b13f7212db5115f`.
  The artifact formats `agent_resolver.rs`, `sqlite_state.rs`, `landlock.rs`,
  both state runtime test files, and this sync's regression-matrix prose; apply
  the output as one generated set rather than hand-selecting hunks.
- Exact-head hosted run `30063180432` then exposed API drift only in retained
  downstream seams. Error handling now inspects upstream's
  `CodexErr::details()` payload for server-overload and missing-thread
  classifications; downstream thread-store tests pass the complete upstream
  `SqliteConfig`; and the app-server history fixture explicitly records no
  terminal wait. These are compatibility repairs, not new runtime behavior.
  The same run proved rustfmt clean before its final generated-lock check.
- GitHub-hosted lock generation run `30064291087` consumed exact candidate
  `e37d906f8d7d3455c9cce9c05cc27683e6cd5889`. Temporary workflow commit
  `c29c768a0cb7281d6a28ba655c19af6e486eafb1` produced a one-file patch with
  SHA-256 `8d30aef5007cec54c44b3c4066c6eb157e28e5b2d5eaada5c7b3839e368a9f35`.
  The generated lock change qualifies the code-mode host's upstream WebSocket
  dependency as `tokio-tungstenite 0.28.0` because the combined downstream
  graph contains another version; no package version or Bazel lock changed.
- Exact-head hosted run `30064450281` then reached the remaining test-fixture
  adapters. Retained tests now use upstream's `CodexErrorDetails`, flattened
  dynamic-tool shape, complete MCP safety configuration, `StartThreadOptions`,
  plugin attribution argument, testing `SqliteConfig`, and Guardian plugin
  metadata. These fixtures preserve the existing downstream behavior while
  keeping their constructors and assertions aligned with upstream APIs.
- Signed merge `3c96cb99b5` preserves upstream `f61b51ddd9` and adopts remote
  code-mode-host support in the app server. The CLI conflict resolves to the
  upstream `AppServerCodeModeHostArgs`, `code_mode_host_transport`, and explicit
  disabled remote-control startup state; no fork-only transport override is
  retained.
- GitHub-hosted lock generation run `30072197446` consumed exact candidate
  `e64bf6a204cfaf2f255b9f00bb621f60b9205b4b`. Temporary workflow commit
  `052ecf763d59d72b2e8ea170c168fefb126b0a54` produced a one-file patch with
  SHA-256 `3b8f4c8b28fabf04fd8f949b7b7ae73992aa95ce5865f3d4b86af5f87ba712bb`.
  It qualifies the `codex-cli` edge as `tokio-tungstenite 0.28.0`; no package
  version or Bazel lock changed.
- Signed merge `7de965e86f` preserves upstream `81da9deb06` and its parent
  `a28374e0db`: Agent Plugin manifests and host-customized
  `wait_for_environment` descriptions are adopted without a downstream plugin
  or tool-description fork. The merge leaves generated locks unchanged.
- The 2026-07-24 integration retains upstream `0dfa778dae`'s code-mode host
  WebSocket transport but constrains its listener to loopback addresses. The
  framed protocol has no peer-authentication layer, so `stdio`, `127.0.0.1`,
  and `::1` remain supported while wildcard and non-loopback addresses fail
  before binding. Preserve this narrow boundary until upstream provides an
  authenticated remote-listener contract; do not treat Origin-header filtering
  as peer authorization. `parse_listen_url_rejects_non_loopback_websocket_addresses`
  is the direct regression guard.
- Exact-head hosted run `30072479060` found two retained-fixture adapters after
  that upstream API expansion: a tool-router test now explicitly leaves the
  optional host `wait_for_environment` configuration unset, and the SQLite
  resume assertion uses the canonical namespace description for the flat
  downstream `DynamicToolSpec`. These are upstream-owned defaults, not a
  downstream tool-description or persistence-policy fork.
- Exact-head hosted run `30076449242` then exposed the remaining retained
  validation drift: the flat deferred-tool namespace assertion needed the same
  canonical default, the Windows filesystem regression needed its direct
  `codex-utils-cargo-bin` test dependency, and the generated config schema and
  keymap counts were stale. GitHub-hosted generator run `30079131774` consumed
  candidate `b6563273d977016adc5e1cf156cedd2ade29785b` through temporary
  workflow head `b2ef8a15a4268759e05cb51811bf5380ee657f15` and produced the
  six-file patch SHA-256
  `6dfd7e18e50acce19f2eb986f8fe5a5a8c10db58d273982414283bb47f6762c4`.
  It changes only `codex-rs/Cargo.lock`, `codex-rs/core/config.schema.json`,
  and the four `keymap_setup` snapshots; `MODULE.bazel.lock` was regenerated
  and unchanged.
  Preserve generated output rather than hand-editing the schema or snapshots.

### Typed Cyber-Policy Retry Boundary

- The optional `notices.auto_continue_on_cyber_policy` carry reacts only to the
  typed `CodexErrorInfo::CyberPolicy` signal from a live app-server turn. It
  does not match server error text or retry untrusted content.
- A retry remains on the original thread and ordinary submission path, keeps
  the selected model, reasoning effort, and complete context unchanged, and is
  disabled for replayed events and direct-input-blocked sessions. It is capped
  at three attempts and re-arms only after a successful turn start.
- This is an operator opt-in for transient server-side outcomes, not a bypass
  of policy enforcement. Any future sync must preserve the typed, same-thread,
  bounded contract or replace it with an upstream-equivalent control signal.
- Primary files:
  - `codex-rs/tui/src/chatwidget/turn_runtime.rs`
  - `codex-rs/tui/src/chatwidget/tests/app_server.rs`

### MCP Server Safety Policy Extensions

- Downstream retains per-server safety controls:
  - `enable_elicitation`
  - `read_only`
  - `strict_tool_classification`
  - `require_approval_for_mutating`
- These coexist with upstream `oauth_resource` support.
- When upstream adds a programmatic `McpServerConfig` literal, retain these
  controls explicitly. Most fixtures should set all four to `false`, preserving
  their original behavior without activating an additional downstream policy.
- Primary files:
  - `codex-rs/core/src/config/types.rs`
  - `codex-rs/core/src/config/edit.rs`
  - `docs/config.md`
  - `docs/downstream.md`

### MCP OAuth Fallback Hardening And Keyring Backend Preservation

- Downstream treats the MCP OAuth fallback credential file as best-effort
  recovery state: empty files are absent, corrupt files after keyring failure
  are warnings instead of hard startup failures, and writes use atomic temp-file
  replacement with explicit syncs.
- Cached expired tokens remain visibly expired when reloaded so the OAuth
  manager refreshes them before reconnecting instead of treating stale access
  tokens as usable.
- Proactive refresh failures preserve the existing durable credentials and remain
  ordinary refresh failures unless the response is an explicit terminal
  credential rejection such as `invalid_grant` (or the refresh token is
  missing). Malformed, non-OAuth, transient, and upstream-unavailable token
  responses must not be presented as authentication-required startup failures.
  RMCP 1.8 erases the underlying OAuth error type, so the compatibility
  classifier accepts only the stable `oauth2` server-error display envelope
  for `invalid_grant`; a future typed RMCP rejection variant can remove this
  narrow boundary.
- The selected keyring backend is intentional carry now that upstream supports
  encrypted local secrets storage. Syncs must preserve both upstream
  concrete-store pinning and `AuthKeyringBackendKind::Secrets` support, plus the
  downstream resolved-store refresh lock that prevents replaying stale rotating
  refresh tokens from a different backend.
- Normal requests stage access-only credentials in RMCP. Refresh material is
  installed only inside the serialized refresh transaction, and RMCP-derived
  persistence reacquires that same per-server lock, rereads the pinned store,
  and adopts newer durable credentials rather than overwriting or deleting them.
- Request-only staging strips `refresh_token` and derived `expires_in`; durable
  reconciliation restores omitted refresh material, scopes, and expiry fields.
  A matching `expires_at` makes a countdown-only `expires_in` difference
  non-conflicting, and a newly rotated refresh token is retained in memory
  before the durable write is attempted.
- Refresh-only credential staging deliberately omits granted scopes when
  handing credentials to RMCP so refresh requests do not broaden explicit
  persisted scopes with authorization-server-advertised `offline_access`; Codex
  preserves the durable stored scope set when the provider omits scopes.
- During future syncs, do not "simplify" the OAuth helpers by dropping either
  the secrets-backed keyring path or the downstream resolved-store reread/save
  discipline unless an upstream replacement covers those behaviors and the
  no-unrequested-`offline_access` refresh contract.
- Primary files:
  - `codex-rs/rmcp-client/src/oauth.rs`
  - `codex-rs/rmcp-client/src/oauth/resolved_store.rs`
  - `codex-rs/rmcp-client/src/oauth/store_lock.rs`
  - `codex-rs/rmcp-client/tests/streamable_http_oauth_startup.rs`
  - `codex-rs/rmcp-client/src/rmcp_client.rs`
  - `codex-rs/rmcp-client/src/startup_error.rs`
  - `codex-rs/codex-mcp/src/connection_manager.rs`

### MCP OAuth Device Login For Headless Servers

- `codex mcp login --device-auth <server>` lets an operator complete MCP OAuth
  login from SSH-only or browserless hosts through the OAuth Device
  Authorization Grant instead of relying on a local browser callback.
- Streamable HTTP OAuth discovery preserves `token_endpoint`,
  `device_authorization_endpoint`, `registration_endpoint`, and
  `grant_types_supported`, so the CLI can fail loudly when a server does not
  actually advertise device-login support or lacks both configured client id
  and dynamic registration support.
- The device-login flow uses a configured public MCP OAuth `client_id` when one
  is available. Otherwise, it performs standards-based dynamic client
  registration using the device grant shape, a public-client token endpoint
  auth method, optional requested scopes, and refresh-token registration only
  when server grant metadata permits or omits grant support.
- After client-id resolution, the flow uses PKCE, the identity-provider
  verification URL/user code, token-endpoint polling, and the existing MCP
  OAuth token cache.
- Upstream `AuthorizationManager` is the primary discovery authority for both
  local and runtime-routed HTTP clients. The downstream mapping preserves the
  device endpoint and grant fields from upstream metadata extensions. A narrow
  fallback remains only for device-only metadata, which upstream's current
  metadata type cannot represent because it requires an authorization endpoint;
  it must not grow back into a parallel normal or protected-resource discovery
  implementation.
- This is an intentional downstream carry for headless MCP server login until
  upstream ships an equivalent headless MCP OAuth login contract. During
  upstream syncs, preserve this behavior unless the upstream replacement covers
  the same discovery, grant-validation, dynamic-registration fallback,
  configured-client-id fast path, PKCE, polling, and token-cache path.
- Primary files:
  - `codex-rs/cli/src/mcp_cmd.rs`
  - `codex-rs/codex-mcp/src/mcp/auth.rs`
  - `codex-rs/rmcp-client/src/auth_status.rs`
  - `codex-rs/rmcp-client/src/perform_oauth_device_login.rs`
  - `codex-rs/rmcp-client/src/lib.rs`
  - `.github/scripts/test_ci_planners.py`
  - `.github/validation-lanes.json`
  - `.github/workflows/sedna-heavy-tests.yml`
  - `justfile`
  - `docs/downstream.md`
  - `docs/downstream-regression-matrix.md`

### TUI Session-State, Queue, Interrupt, And Usage Surfaces

- Per-thread approval/sandbox/reviewer overrides survive thread switches.
- Active-thread session state survives config refresh and fresh-session clones
  keep policy mutability before new-thread and fork flows.
- `/agent` picker rows expose per-thread used-token totals from cached thread
  usage without requiring a broader context-window plumbing pass.
- Combined session token totals remain visible across `/status` and
  footer/status-line surfaces without overwriting the active thread's own usage
  totals.
- Unavailable slash commands queue and replay after the current task instead of
  being rejected immediately.
- Active-turn runtime choice commands such as `/model`, `/permissions`, `/plan`,
  and model service-tier slash commands remain selectable while a task is
  running so their chosen settings apply to queued follow-up turns.
- Interrupt handling defaults to double-`Esc` confirmation, including status-row
  interrupts while a turn is running, and preserves queued follow-ups and
  queued model changes coherently.
- Active-turn status labels preserve downstream operator cues, including
  showing `Compacting context` while context compaction is running instead of
  falling back to generic `Working`.
- Selected-turn copy keeps `CopyStatus` success/error feedback in the
  transcript overlay footer, including clipboard failures and expiry/replacement
  behavior, so copy actions never fail silently. The copied payload remains
  `## User\n\n<prompt>\n\n## Assistant\n\n<markdown>` and takes the last
  finalized source-backed assistant or proposed-plan markdown before the next
  visible prompt. After upstream moved retry safety to source-preserving forks,
  this path intentionally reads canonical transcript cells and carries no
  ordinal cache, rollback truncation, or rollback-only error state.
- Finalized assistant Markdown and proposed plans may cache rich rendered lines
  by width, syntax-theme revision, terminal palette, and color level. Raw
  transcript and selected-turn copy remain source-backed, and visualization
  directives remain uncached because their filesystem-dependent result can
  change after insertion.
- Bottom-pane transient views run their pre-draw tick and completion path so
  request-user-input overlays and other timed active views can redraw,
  auto-resolve, and pop through the same active-view seam instead of stalling
  behind static composer redraws. Stacked selection popups can be replaced or
  dismissed by view id even when another view sits above them, `/resume` stays
  available during MCP startup while active-turn blocking remains in force, and
  `/status` history preserves cached status text while a refresh is pending.
- TUI realtime voice remains a downstream carry on non-Linux targets even
  though upstream removed that surface; Linux keeps explicit unavailable stubs,
  so syncs should preserve the platform split instead of deleting
  `audio_device.rs` or the Linux `voice` stub as stale code. The non-Linux
  split also depends on the isolated `codex-realtime-webrtc` crate, the
  target-scoped `cpal` entry in `codex-rs/tui/Cargo.toml`, and their Cargo and
  Bazel dependency graphs. The WebRTC crate is an intentional macOS transport
  boundary, not an orphan left behind by upstream's removal.
- The hermetic macOS `webrtc-sys` patch is also part of that transport boundary.
  Its `-p1` headers must name `webrtc-sys/build.rs`, the workspace-relative path
  at the locked `rust-sdks` revision, rather than a repository-root `build.rs`.
  When the lockfile source changes, rebase the patch against that exact source
  and prove it in GitHub-hosted macOS Bazel loading before changing the pinned
  dependency pair or removing the patch.
- Thread replay routing may omit only notifications that are already handled or
  ignored during replay. It must update active-turn and side-parent state first,
  retain replay-visible native computer-use completion items, and keep
  downstream realtime audio forwarding on its dedicated transport.
- Weekly status-line pacing keeps downstream stale handling and selectable
  render styles.
- Upgradeable legacy models stay visible in the model picker even when ordinary
  hidden presets are excluded.
- `/quit` and `/exit` inside an active `/side` conversation close only that side
  conversation and return to the parent session; the same commands in the main
  conversation remain application exits.
- Primary files:
  - `codex-rs/tui/src/app.rs`
  - `codex-rs/tui/src/app/thread_events.rs`
  - `codex-rs/tui/src/app/thread_routing.rs`
  - `codex-rs/tui/src/app/side.rs`
  - `codex-rs/tui/src/app/event_dispatch.rs`
  - `codex-rs/tui/src/app_backtrack.rs`
  - `codex-rs/tui/src/app_event.rs`
  - `codex-rs/tui/Cargo.toml`
  - `codex-rs/Cargo.lock`
  - `codex-rs/realtime-webrtc/BUILD.bazel`
  - `codex-rs/realtime-webrtc/Cargo.toml`
  - `codex-rs/realtime-webrtc/src/lib.rs`
  - `codex-rs/realtime-webrtc/src/native.rs`
  - `codex-rs/tui/src/audio_device.rs`
  - `codex-rs/tui/src/bottom_pane/mod.rs`
  - `codex-rs/tui/src/bottom_pane/textarea.rs`
  - `codex-rs/tui/src/chatwidget/slash_dispatch.rs`
  - `codex-rs/tui/src/multi_agents.rs`
  - `codex-rs/tui/src/voice.rs`
  - `codex-rs/tui/src/slash_command.rs`
  - `codex-rs/tui/src/bottom_pane/chat_composer.rs`
  - `codex-rs/tui/src/bottom_pane/slash_commands.rs`
  - `codex-rs/tui/src/bottom_pane/status_line_setup.rs`
  - `codex-rs/tui/src/chatwidget.rs`
  - `codex-rs/tui/src/chatwidget/interaction.rs`
  - `codex-rs/tui/src/chatwidget/transcript.rs`
  - `codex-rs/tui/src/chatwidget/slash_dispatch.rs`
  - `codex-rs/tui/src/chatwidget/protocol.rs`
  - `codex-rs/tui/src/chatwidget/tool_lifecycle.rs`
  - `codex-rs/tui/src/chatwidget/status_surfaces.rs`
  - `codex-rs/tui/src/history_cell/mod.rs`
  - `codex-rs/tui/src/history_cell/markdown_render_cache.rs`
  - `codex-rs/tui/src/history_cell/messages.rs`
  - `codex-rs/tui/src/history_cell/messages_tests.rs`
  - `codex-rs/tui/src/history_cell/plans.rs`
  - `codex-rs/tui/src/history_cell/plans_tests.rs`
  - `codex-rs/tui/src/pager_overlay.rs`
  - `codex-rs/tui/src/render/highlight.rs`
  - `codex-rs/tui/src/status/card.rs`
  - `codex-rs/tui/src/status/rate_limits.rs`
  - `docs/config.md`
  - `docs/tui-weekly-usage-pacing-status-line.md`
  - `docs/downstream.md`
  - `docs/downstream-regression-matrix.md`

### TUI Transcript Compact Detail Mode

- The `Ctrl+T` transcript overlay intentionally carries two detail modes:
  verbose mode for the complete transcript and compact mode for prompt/agent
  review without terminal/tool noise.
- Verbose mode remains the default audit surface. Compact mode keeps user
  prompts, assistant-facing responses, selected reasoning summaries, and
  important warning/error/stop hook output visible while collapsing injected
  session context, context-only hook entries, and other detail that belongs in
  verbose transcript inspection.
- The overlay footer exposes the active detail mode and the toggle key, and the
  overlay state preserves scroll, selected prompt, raw/rich render mode, and
  verbose/compact detail mode across close/reopen.
- The transcript header now names the active mode (`Transcript: verbose` or
  `Transcript: compact`), and the footer spells out the switch keys as
  `raw render` / `rich render` and `compact view` / `verbose view` when there
  is enough terminal width.
- `[tui].transcript_default_detail_mode = "verbose" | "compact"` chooses the
  default detail mode at startup and after active config refreshes; verbose
  remains the default so the complete audit transcript is preserved unless the
  user opts into quiet review. An already-open transcript keeps its current
  mode until it is closed.
- This is an intentional downstream TUI ergonomics carry. Preserve it during
  upstream syncs unless upstream lands equivalent transcript detail-mode
  behavior that keeps the same verbose audit fallback and compact prompt/agent
  review path.
- Primary files:
  - `codex-rs/config/src/profile_toml.rs`
  - `codex-rs/config/src/types.rs`
  - `codex-rs/core/src/config/mod.rs`
  - `codex-rs/core/config.schema.json`
  - `codex-rs/tui/src/pager_overlay.rs`
  - `codex-rs/tui/src/history_cell/mod.rs`
  - `codex-rs/tui/src/history_cell/hook_cell.rs`
  - `codex-rs/tui/src/history_cell/messages.rs`
  - `codex-rs/tui/src/chatwidget.rs`
  - `codex-rs/tui/src/chatwidget/transcript.rs`
  - `codex-rs/tui/src/app.rs`
  - `codex-rs/tui/src/app/input.rs`
  - `codex-rs/tui/src/app_backtrack.rs`
  - `codex-rs/tui/src/footer_hints.rs`
  - `codex-rs/tui/src/keymap.rs`
  - `codex-rs/tui/src/keymap_setup.rs`
  - `codex-rs/tui/src/resume_picker.rs`
  - `codex-rs/config/src/tui_keymap.rs`
  - `docs/downstream-regression-matrix.md`

### Custom Prompt Discovery And Review Prompt Flow

- Downstream restores a file-backed custom prompt catalogue under
  `$CODEX_HOME/prompts`, including optional frontmatter metadata for prompt
  descriptions and argument hints.
- Downstream also preserves the live ad hoc custom-review prompt entry point in
  the TUI review flow, so users can still open the dedicated custom prompt
  view from the review popup and submit review text without losing the standard
  review interaction.
- Today the runtime wiring is clearest for the ad hoc `ReviewTarget::Custom`
  review path. The separate file-backed prompt catalogue remains a carried
  downstream surface, but it should not be described as fully reconnected to a
  user-facing picker until that runtime wiring is verified or intentionally
  restored.
- Primary files:
  - `codex-rs/core/src/custom_prompts.rs`
  - `codex-rs/core/src/custom_prompts_tests.rs`
  - `codex-rs/protocol/src/custom_prompts.rs`
  - `codex-rs/core/src/lib.rs`
  - `codex-rs/protocol/src/lib.rs`
  - `codex-rs/tui/src/app.rs`
  - `codex-rs/tui/src/app_event.rs`
  - `codex-rs/tui/src/bottom_pane/custom_prompt_view.rs`
  - `codex-rs/tui/src/chatwidget.rs`

### Code-Mode Declaration Formatting

- `main` still emits imported tool declarations of the form:
  `import { tools } from "..."; declare function ...`
- `upstream/main` still emits the older inline
  `declare const tools: { ... }` example.
- This is a live carry-only divergence.
- `ToolRouter` preserves `ResponseItem::CustomToolCall.namespace` when
  constructing registry tool names, so namespaced MCP/app custom tools do not
  flatten into plain names before routing.
- Primary files:
  - `codex-rs/core/src/tools/code_mode_description.rs`
  - `codex-rs/core/src/tools/router.rs`

### External-Agent Session Import Compatibility

- Upstream consolidated the former downstream session-import crate into
  `codex-external-agent-migration::sessions`. Preserve the imported-session
  ledger, source-content SHA-256 identity, and single-pass record parsing while
  using the upstream module and package layout.
- The workspace uses `sha2` 0.11, so content hashes retain the downstream
  explicit hexadecimal encoder rather than relying on the older digest
  formatting implementation. Callers inside the consolidated module must
  resolve that helper through the `sessions` module, not the crate root.
- `codex.external-agent-session-migration-targeted` is the focused hosted
  guardrail for future upstream moves. The divergence registry deliberately
  lists only the five session files that remain different from upstream so
  adjacent upstream-owned migration code is not hidden from the audit.
- Primary files:
  - `codex-rs/external-agent-migration/src/sessions/export.rs`
  - `codex-rs/external-agent-migration/src/sessions/ledger.rs`
  - `codex-rs/external-agent-migration/src/sessions/ledger_tests.rs`
  - `codex-rs/external-agent-migration/src/sessions/mod.rs`
  - `codex-rs/external-agent-migration/src/sessions/records.rs`
  - `.github/validation-lanes.json`
  - `justfile`

### External-Agent Migration Path Containment

- Repository-scoped external-agent detection and import canonicalize the
  project selected by the trusted local app-server client before deriving
  migration paths. Static source and destination components under that root
  must not be symlinks, including settings leaves, MCP configuration leaves,
  hook scripts, and dangling target leaves.
- This fail-closed check applies to configuration, MCP settings, subagents,
  hooks, commands, skills, and instruction-file imports. Home-scoped imports
  retain their separate source-root behavior, while shared target-leaf checks
  avoid overwriting symlink entries in either scope. It does not claim
  handle-relative protection against concurrent replacement of an already
  checked repository path.
- Upstream MEMORY migration remains home-scoped, but its client-selected project
  keys must be exactly one normal path component. Before memory detection or
  import mutates the workspace, the memory root, extension/resource ancestors,
  project root, `scope.json`, Markdown resource leaves, and `instructions.md`
  are checked beneath `CODEX_HOME` and rejected when any existing component is
  a symlink. This preserves stale-project removal without allowing absolute or
  parent-relative selections to escape the extension workspace.
- A resource directory is managed only when it contains a regular, non-symlink
  `scope.json` marker. Ordinary files and unmarked metadata directories are
  ignored, but every directory entry and candidate marker is symlink-preflighted
  before its type is used, so stale project-root and marker symlinks fail closed.
- The marker rule affects future detection. A one-time backfill for unchanged
  imports that predate the marker is an explicit follow-up, not an automatic
  mutation performed by this sync.
- The focused lane covers both the extracted migration-crate denial tests and
  the app-server request boundary.
  `projects_needing_import_rejects_symlinked_stale_memory_project` proves stale
  owned projects are preflighted before detection offers them. In particular,
  `projects_needing_import_rejects_symlinked_stale_project_scope` proves a real
  project directory cannot redirect its ownership marker, while
  `external_agent_memory_import_rejects_stale_symlink_before_workspace_mutation`
  proves a rejected stale-project selection cannot initialize or rewrite the
  memory workspace before the containment error is reported.
- Recursive copy helpers skip symlink entries and refuse symlinked target
  directories. Empty-text target checks use `symlink_metadata`, so an empty or
  dangling symlink is protected rather than treated as an overwritable file.
- Upstream's closed draft containment change covered an empty leaf target but
  did not cover exact repository settings/MCP leaves, source roots, or
  destination-ancestor symlinks. Keep this carry until upstream has equivalent
  detection-and-import containment.
- Primary files:
  - `.github/scripts/test_ci_planners.py`
  - `.github/validation-lanes.json`
  - `.github/workflows/sedna-heavy-tests.yml`
  - `codex-rs/app-server/tests/suite/v2/external_agent_config.rs`
  - `codex-rs/external-agent-migration/Cargo.toml`
  - `codex-rs/external-agent-migration/src/detect/mod.rs`
  - `codex-rs/external-agent-migration/src/hooks_common.rs`
  - `codex-rs/external-agent-migration/src/lib_tests.rs`
  - `codex-rs/external-agent-migration/src/memory_import.rs`
  - `codex-rs/external-agent-migration/src/memory_import_tests.rs`
  - `codex-rs/external-agent-migration/src/migration_source.rs`
  - `codex-rs/external-agent-migration/src/scope.rs`
  - `codex-rs/external-agent-migration/src/scope_tests.rs`
  - `codex-rs/external-agent-migration/src/service.rs`
  - `codex-rs/external-agent-migration/src/service_tests.rs`
  - `codex-rs/external-agent-migration/src/service_tests/containment.rs`
  - `codex-rs/external-agent-migration/src/service_tests/general/config_import.rs`
  - `codex-rs/external-agent-migration/src/service_tests/general/detection.rs`
  - `codex-rs/external-agent-migration/src/service_tests/general/repo_import.rs`
  - `codex-rs/external-agent-migration/src/service_tests/plugins/basics.rs`
  - `codex-rs/external-agent-migration/src/service_tests/plugins/marketplaces.rs`
  - `codex-rs/external-agent-migration/src/source/cla.rs`
  - `codex-rs/external-agent-migration/src/source/cur.rs`
  - `codex-rs/external-agent-migration/src/subagents.rs`
  - `codex-rs/external-agent-migration/src/utils.rs`
  - `justfile`
- Hosted guardrails:
  - `codex.external-agent-migration-containment-targeted`
  - `rust-ci-full`
  - `CodeQL Advanced`

## Not Counted As Standalone Live Divergences

- Merge and sync history:
  - carry-only merge commits are sync history, not independent downstream
    behaviors; do not treat their count as a behavior inventory.
- Merge-repair and promotion-fix history:
  - examples include `Fix main core regressions after upstream sync`,
    `Fix main promotion follow-ups`, and
    `Fix hybrid merge API drift in core/tui tests`
  - hosted strict-lint compatibility repairs that preserve an upstream API's
    behavior, such as using `SseStream::from_bytes_stream` after the dependency
    deprecated its typo-named predecessor; retain only until upstream adopts
    the same API rename and do not classify the rename as MCP policy carry
- Generated and derivative churn:
  - schema outputs under `codex-rs/app-server-protocol/schema/`
  - generated SDK outputs under `sdk/python/`
  - TUI snapshot updates under `codex-rs/tui/src/**/snapshots/`
- Structural test-only churn in large modules:
  - `codex-rs/core/src/plugins/manager.rs`
  - startup plugin sync bounded wait and completion re-arm
  - `codex-rs/core/src/config/edit.rs`
  - `codex-rs/core/src/tools/spec_plan.rs`
  - `codex-rs/tui/src/test_support.rs` centralizes the existing 8 MiB test
    stack budget for stack-heavy app and app-server-session regressions; keep
    their behavioral assertions intact when importing upstream test changes
  - `codex-rs/app-server-daemon/src/backend/pid_tests.rs` removes the PID file
    while its reservation lock remains held, preventing test cleanup races;
    drop this carry when upstream adopts equivalent ordering
  - `codex-rs/tui/src/diff_render.rs` retains the downstream ratatui `0.30.1`
    `Widget::render` test adapter and tab-free gallery fixtures; production
    rendering follows upstream's borrowed-file-change implementation
  - `codex-rs/tui/src/history_cell/plans_tests.rs` asserts that transcript
    measurement primes the plan cache without pinning upstream's
    ratatui-version-specific row count; the sentinel equality still proves
    reuse, and this adaptation can drop when ratatui versions converge or
    upstream adopts an equivalent semantic assertion
  - `codex-rs/core/tests/suite/pending_input.rs` uses the interrupted sleep
    completion as the sole consequence barrier so an unrelated event wait
    cannot consume the completion before its assertion
  - `codex-rs/core/tests/suite/otel.rs` waits for orderly session shutdown
    before reading the trace buffer so retained response spans are closed;
    both metadata assertions remain unchanged
- Schema-generation adapters that preserve legacy wire deserialization while
  keeping generated app-server schemas on the current public shape, such as
  `#[schemars(!from)]` around `MultiAgentMode` wire aliases, belong with
  app-server/protocol maintenance rather than as standalone behavior.
- The removed config template-interpolation module is deliberately not carry.
  Effective config now follows upstream's authoritative layer-stack
  materialization; old interpolation helpers and their tests must not be
  resurrected during later syncs.

## Historical Carry Commits Now Upstream-Equivalent

The following carry commits have exact-subject matches on `upstream/main`. They
should not be treated as current fork-only behavior by title alone.

Two additional validation-only repairs have behavior-equivalent upstream
coverage despite different subjects: `ec042322e2` and `ddb5443d28` are both
absorbed by upstream `f21f98936c` and must not be counted as live carry.

```text
027afb885 -> 3b1c78a5c | [skill-creator] Add forward-testing instructions (#13600)
8b3348530 -> 07c22d20f | Add code_mode output helpers for text and images (#14244)
e4bc35278 -> 8ac27b2a1 | Add ephemeral flag support to thread fork (#14248)
22d0aea5b -> 3d4628c9c | Add granular metrics for cloud requirements load (#14108)
052ec629b -> 180a5820f | Add keyboard based fast switching between agents in TUI (#13923)
e79155902 -> 3d41ff0b7 | Add model-controlled truncation for code mode results (#14258)
01e2c3b8d -> b7f8e9195 | Add OpenAI Docs skill (#13596)
c7e28cffa -> ee8f84153 | Add output schema to MCP tools and expose MCP tool results in code mode (#14236)
5b10b93ba -> 39c1bc1c6 | Add realtime start instructions config override (#14270)
816e447ea -> 12ee9eb6e | Add snippets annotated with types to tools when code mode enabled (#14284)
2895d3571 -> 91ca20c7c | Add spawn_agent model overrides (#14160)
18199d4e0 -> 83b22bb61 | Add store/load support for code mode (#14259)
bda9e55c7 -> f2d66fadd | add(core): arc_monitor (#13936)
15163050d -> d5694529c | app-server: propagate nested experimental gating for AskForApproval::Reject (#14191)
295b56bec -> c1a424691 | chore: add a separate reject-policy flag for skill approvals (#14271)
bf936fa0c -> ce1d9abf1 | Clarify close_agent tool description (#14269)
e52afd28b -> 00ea8aa7e | Expose strongly-typed result for exec_command (#14183)
de2a73cd9 -> 889b4796f | feat: Add additional macOS Sandbox Permissions for Launch Services, Contacts, Reminders (#14155)
2544bd02a -> d751e68f4 | feat: Allow sync with remote plugin status. (#14176)
9a501ddb0 -> 026cfde02 | Fix Linux tmux segfault in user shell lookup (#13900)
b90921eba -> 7144f84c6 | Fix release-mode integration test compiler failure (#13603)
78280f872 -> f385199cc | fix(arc_monitor): api path (#14290)
44bfd2f12 -> b1dddcb76 | Increase sdk workflow timeout to 15 minutes (#14252)
b73228722 -> a67660da2 | Load agent metadata from role files (#14177)
e4edafe1a -> f9cba5cb1 | Log ChatGPT user ID for feedback tags (#13901)
566897d42 -> 31bf1dbe6 | Make unified exec session_id numeric (#14279)
b33edebd6 -> 4ac604285 | Mark incomplete resumed turns interrupted when idle (#14125)
f8ef154a6 -> 2621ba17e | Pass more params to compaction (#14247)
24b8d443b -> 01792a4c6 | Prefix code mode output with success or failure message and include error stack (#14272)
16daab66d -> e77b2fd92 | prompt changes to guardian (#14263)
37f51382f -> 8a099b3df | Rename code mode tool to exec (#14254)
cec211cab -> da74da668 | render local file links from target paths (#13857)
2cfa10609 -> fd4a67352 | Responses: set x-client-request-id as convesration_id when talking to responses (#14312)
46e6661d4 -> c4d35084f | Reuse McpToolOutput in McpHandler (#14229)
8af97ce4b -> 7f2232938 | Revert "Pass more params to compaction" (#14298)
567ad7faf -> 285b3a514 | Show spawned agent model and effort in TUI (#14273)
cc417c39a -> a4d884c76 | Split spawn_csv from multi_agent (#14282)
f6e966e64 -> 9b5078d3e | Stabilize pipe process stdin round-trip test (#14013)
77a02909a -> 52a7f4b68 | Stabilize split PTY output on Windows (#14003)
3f7cb0304 -> c8446d7cf | Stabilize websocket response.failed error delivery (#14017)
28934762d -> 722e8f08e | unifying all image saves to /tmp to bug-proof (#14149)
```

## GitHub App installation observer carry

The PR watcher now has an explicit `--installation-observer` source-mode
adaptation for a caller that already supplies a short-lived GitHub App
installation token. It skips `gh api user`, passes an unbound login to the
existing conservative review filter, and preserves the default login lookup
outside that mode. The path is read-only and does not mint, print, persist, or
broaden credentials. Hosted tests must cover parser selection, observer
no-`/user` behavior, default-mode login behavior, and trusted-association/bot
filtering with a blank identity; no live token or watcher activation belongs to
this carry.

## Exact-head PR watcher policy carry

The canonical PR watcher keeps its existing read-only observer and review/check
contracts while recording a compact decision receipt and the next scheduled wake
against the exact repository, PR number, and observed head SHA. An unexplained
GitHub `BLOCKED` merge state is surfaced as
`action_required_merge_policy_blocked`; it fails closed for readiness and keeps
the polling interval bounded instead of entering green-state backoff.

Focused source guardrails cover BLOCKED versus CLEAN actions, pending checks,
published review evidence, and a fake-clock exact-head schedule receipt. This
carry also keeps active `QUEUED` and `AWAITING_CHECKS` merge-queue entries on
the configured base cadence, including when the pending queue head is missing
or unreadable, and includes queue-entry identity in change detection so a
replacement or removal cannot preserve stale green backoff. Ordinary green
PRs without an active queue entry retain bounded backoff. Queue failure,
removal, readiness, credential, and mutation behavior remain fail-closed and
separate ownership boundaries.
