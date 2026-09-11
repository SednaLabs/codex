set working-directory := "codex-rs"
set positional-arguments

export JUST_SHELL := justfile_directory() / "scripts/just-shell.py"

set shell := ["python3", "-c", 'import os, runpy; runpy.run_path(os.environ["JUST_SHELL"], run_name="__main__")']
set windows-shell := ["python", "-c", 'import os, runpy; runpy.run_path(os.environ["JUST_SHELL"], run_name="__main__")']

rust_min_stack := "8388608"
python := if os_family() == "windows" { "python" } else { "python3" }

# Display help
help:
    just -l

# Dispatch a prerelease for the exact protected main tip captured by GitHub.
# The workflow resolves the version and tag on its runner, so this command does
# not depend on a potentially stale local main ref.
[no-cd]
prerelease-main:
    gh workflow run sedna-release.yml --repo sednalabs/codex --ref main -f channel=prerelease -f draft=false -f macos_release_mode=off -f allow_markerless_prerelease=true

# Dispatch the same Linux prerelease plus an explicitly labelled Intel macOS
# ad-hoc asset. The asset is not Developer ID signed or notarized.
[no-cd]
prerelease-main-macos:
    gh workflow run sedna-release.yml --repo sednalabs/codex --ref main -f channel=prerelease -f draft=false -f macos_release_mode=unnotarized -f allow_markerless_prerelease=true

# `codex`

alias c := codex

codex *args:
    cargo run --bin codex -- {args}

# `codex exec`
exec *args:
    cargo run --bin codex -- exec {args}

# Start `codex exec-server` and run codex-tui.
[no-cd]
[positional-arguments]
[unix]
tui-with-exec-server *args:
    {{ justfile_directory() }}/scripts/run_tui_with_exec_server.sh "$@"

# Run the CLI version of the file-search crate.
file-search *args:
    cargo run --bin codex-file-search -- {args}

# Run the standalone code-mode host from source.
code-mode-host *args:
    cargo run --bin codex-code-mode-host -- {args}

# Build the CLI and run the app-server test client
app-server-test-client *args:
    cargo build -p codex-cli
    cargo run -p codex-app-server-test-client -- --codex-bin ./target/debug/codex {args}

# Format the justfile, Rust, Bazel/Starlark, Python SDK code, and Python scripts.
fmt:
    @{{ python }} ../scripts/format.py

# Check formatting without modifying files.
fmt-check:
    @{{ python }} ../scripts/format.py --check

core-websocket-targeted:
    set -euo pipefail; \
    export CODEX_JS_REPL_NODE_PATH="${CODEX_JS_REPL_NODE_PATH:-/tmp/codex-node22/bin/node}"; \
    cargo test -p codex-core --test all suite::agent_websocket -- --exact --test-threads=1; \
    cargo test -p codex-core --test all suite::client_websockets -- --exact --test-threads=1; \
    cargo test -p codex-core --test all suite::websocket_fallback -- --exact --test-threads=1; \
    cargo test -p codex-core --test all suite::turn_state::websocket_turn_state_persists_within_turn_and_resets_after -- --exact --test-threads=1

fix *args:
    cargo clippy --fix --tests --allow-dirty {args}

clippy *args:
    cargo clippy --tests {args}

[unix]
install:
    rustup show active-toolchain
    cargo fetch

[windows]
install:
    #!powershell.exe -File
    $pwsh = Get-Command pwsh.exe -ErrorAction SilentlyContinue
    if (-not $pwsh) {
        winget install --exact --id Microsoft.PowerShell --source winget --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
    rustup show active-toolchain
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    cargo fetch
    exit $LASTEXITCODE

# Run nextest with --no-fail-fast so all tests are run.
#
# Run `cargo install --locked cargo-nextest` if you don't have it installed.
# Prefer this for routine local runs. Workspace crate features are banned, so

# there should be no need to add `--all-features`.
[unix]
test *args:
    RUST_MIN_STACK={{ rust_min_stack }} NEXTEST_PROFILE=local cargo nextest run --no-fail-fast "$@"

[windows]
test *args:
    $env:RUST_MIN_STACK = "{{ rust_min_stack }}"; $env:NEXTEST_PROFILE = "local"; cargo nextest run --no-fail-fast @($args | Select-Object -Skip 1)

# Run from the repository root so scripts that resolve paths from `cwd` see

# the same layout they use in GitHub Actions.
[no-cd]
test-github-scripts:
    {{ python }} -m unittest discover -s {{ justfile_directory() }}/.github/scripts -p 'test_*.py'

# Run explicit workspace benchmark targets.
bench *args:
    cargo bench --workspace --bench '*' {args}

# Run benchmark targets once to ensure they start successfully.
bench-smoke:
    just bench -- --test

# Compile-focused guardrail for high-churn core + sandbox seams.
core-compile-smoke:
    cargo check -p codex-linux-sandbox -p codex-core --tests

# Focused model catalog compatibility and overlay regression slice.
model-catalog-compat-targeted:
    cargo test -p codex-protocol --lib model_catalog_deserializer
    cargo test -p codex-models-manager --lib bundled_models_json_roundtrips
    cargo test -p codex-models-manager --lib cache_deserializer_accepts_catalog_without_legacy_base
    cargo test -p codex-models-manager --lib openai_overlay_preserves_unrelated_metadata_and_static_catalog_precedence
    cargo test -p codex-models-manager --lib openai_overlay_applies_after_remote_and_cache_composition
    cargo fmt -p codex-protocol -p codex-models-manager -- --check

# Carry-only downstream behavior smoke checks (core-only seam).
core-carry-core-smoke:
    RUST_MIN_STACK={{ rust_min_stack }} CODEX_JS_REPL_NODE_PATH="${CODEX_JS_REPL_NODE_PATH:-/tmp/codex-node22/bin/node}" cargo nextest run -p codex-core --no-fail-fast --test all -- suite::subagent_notifications::spawn_agent_requested_model_and_reasoning_override_inherited_settings_without_role suite::subagent_notifications::spawn_agent_role_overrides_requested_model_and_reasoning_settings suite::code_mode::code_mode_exports_all_tools_metadata_for_builtin_tools suite::code_mode::code_mode_exports_all_tools_metadata_for_namespaced_mcp_tools suite::code_mode::code_mode_exec_nested_limit_formats_result_variable_before_default_history_truncation suite::code_mode::code_mode_exec_nested_limit_truncates_result_variable_when_exceeded suite::code_mode::code_mode_exec_nested_limit_formats_result_variable_before_configured_history_truncation suite::code_mode::code_mode_exec_without_nested_limit_formats_result_variable_before_default_history_truncation suite::code_mode::code_mode_exec_without_nested_limit_formats_result_variable_before_configured_history_truncation suite::compact_remote::remote_request_with_v3_initial_items_uses_custom_experimental_realtime_start_instructions suite::compact_resume_fork::snapshot_rollback_past_compaction_replays_append_only_history suite::compact_resume_fork::snapshot_rollback_followup_turn_trims_context_updates suite::unified_exec::exec_command_reports_chunk_and_exit_metadata suite::unified_exec::write_stdin_returns_exit_metadata_and_clears_session --exact
    CODEX_JS_REPL_NODE_PATH="${CODEX_JS_REPL_NODE_PATH:-/tmp/codex-node22/bin/node}" cargo test -p codex-core completion_rule_distinguishes_any_from_all --lib -- --exact --test-threads=1

# Carry-only downstream behavior smoke checks (TUI/UI seam).
core-carry-ui-smoke:
    RUST_MIN_STACK={{ rust_min_stack }} CODEX_JS_REPL_NODE_PATH="${CODEX_JS_REPL_NODE_PATH:-/tmp/codex-node22/bin/node}" cargo nextest run -p codex-tui --no-fail-fast -- chatwidget::tests::slash_commands::queued_popup_command_replay_waits_before_submitting_next_message chatwidget::tests::slash_commands::slash_quit_in_side_conversation_requests_side_exit chatwidget::tests::slash_commands::slash_exit_in_side_conversation_requests_side_exit chatwidget::tests::composer_submission::alt_up_restores_most_recent_queued_slash_command chatwidget::tests::composer_submission::alt_up_restored_state_with_missing_insert_order_preserves_front_back_recall_order app::tests::replayed_turn_complete_submits_restored_queued_follow_up app::agent_navigation::tests::active_agent_label_tracks_current_thread streaming::render::tests::visualization_context_without_directive_keeps_incremental_rendering --exact
    cargo test -p codex-tui updates::tests:: --lib -- --test-threads=1

# Compatibility wrapper while callers migrate to split core/UI smoke lanes.
core-carry-smoke:
    just core-carry-core-smoke
    just core-carry-ui-smoke

# Focused startup sync regression slice for bounded-wait and abort/re-arm behavior.
core-startup-sync-targeted:
    cargo test -p codex-core --lib startup_remote_plugin_sync_ -- --test-threads=1

# Focused external-agent session import and content-hash compatibility slice.
external-agent-session-migration-targeted:
    cargo test --locked -p codex-external-agent-migration --lib -- --test-threads=1

# Focused containment slice for repository and memory migration paths.
external-agent-migration-containment-targeted:
    cargo nextest run --locked -p codex-external-agent-migration --no-fail-fast --no-tests=fail --lib
    cargo nextest run --locked -p codex-app-server --no-fail-fast --no-tests=fail --test all -- suite::v2::external_agent_config::external_agent_memory_import_rejects_stale_symlink_before_workspace_mutation --exact

# Focused downstream sub-agent surface contract slice.
core-subagent-surface-targeted:
    RUST_MIN_STACK="${RUST_MIN_STACK:-{{ rust_min_stack }}}" CODEX_JS_REPL_NODE_PATH="${CODEX_JS_REPL_NODE_PATH:-/tmp/codex-node22/bin/node}" cargo nextest run -p codex-core --no-fail-fast --lib -- multi_agent_v2_list_agents_returns_completed_status multi_agent_v2_list_agents_filters_by_relative_path_prefix multi_agent_v2_list_agents_omits_closed_agents spawn_agent_tool_v2_requires_task_name_and_lists_visible_models list_agents_tool_includes_path_prefix_and_agent_fields multi_agent_v2_can_disable_wait_agent send_message_tool_requires_target_items_interrupt_and_receipt_schema send_message_tool_declares_non_acknowledgement_handoff_receipt multi_agent_v2_inspect_agent_tree_receipt_includes_live_effective_identity multi_agent_v2_spawn_returns_path_and_send_message_accepts_relative_path multi_agent_v2_send_message_keeps_cold_target_unloaded
    RUST_MIN_STACK="${RUST_MIN_STACK:-{{ rust_min_stack }}}" CODEX_JS_REPL_NODE_PATH="${CODEX_JS_REPL_NODE_PATH:-/tmp/codex-node22/bin/node}" cargo nextest run -p codex-core --no-fail-fast --no-tests=fail --lib -- config::schema::tests::config_schema_matches_fixture config::schema::tests::config_schema_allows_named_agent_roles codex_delegate_tests::run_codex_thread_interactive_respects_pre_cancelled_spawn --exact
    RUST_MIN_STACK="${RUST_MIN_STACK:-{{ rust_min_stack }}}" CODEX_JS_REPL_NODE_PATH="${CODEX_JS_REPL_NODE_PATH:-/tmp/codex-node22/bin/node}" cargo nextest run -p codex-core --no-fail-fast --no-tests=fail --test all -- suite::spawn_agent_description::configured_agent_roles_control_spawn_agent_type
    RUST_MIN_STACK="${RUST_MIN_STACK:-{{ rust_min_stack }}}" CODEX_JS_REPL_NODE_PATH="${CODEX_JS_REPL_NODE_PATH:-/tmp/codex-node22/bin/node}" cargo nextest run -p codex-core --no-fail-fast --no-tests=fail --test all -- suite::spawn_agent_description::multi_agent_v2_wait_agent_tool_follows_configuration

# Focused inspect_agent_tree stale-descendant fallback regression.
core-subagent-inspect-tree-fallback-targeted:
    RUST_MIN_STACK="${RUST_MIN_STACK:-{{ rust_min_stack }}}" cargo nextest run -p codex-core --lib --no-tests=fail -- agent::control::tests::inspect_agent_tree_without_state_db_points_to_subagent_tail --exact

# Focused core-side sub-agent notification contract slice.
core-subagent-notification-contract-targeted:
    CODEX_JS_REPL_NODE_PATH="${CODEX_JS_REPL_NODE_PATH:-/tmp/codex-node22/bin/node}" cargo nextest run -p codex-core --no-fail-fast --no-tests=fail --lib -- context_manager::history::tests::drop_last_n_user_turns_ignores_session_prefix_user_messages --exact
    CODEX_JS_REPL_NODE_PATH="${CODEX_JS_REPL_NODE_PATH:-/tmp/codex-node22/bin/node}" cargo nextest run -p codex-core --no-fail-fast --no-tests=fail --lib -- session_prefix::tests::format_subagent_notification_message_round_trips_completed_status --exact
    cargo nextest run -p codex-memories-write --no-fail-fast --no-tests=fail --lib -- phase1::job::tests::classifies_memory_excluded_fragments --exact
    cargo nextest run -p codex-memories-write --no-fail-fast --no-tests=fail --lib -- phase1::tests::serializes_memory_rollout_with_agents_removed_but_environment_kept --exact

# Focused sub-agent completion-notification parser + TUI render slice after the

# tui_app_server -> tui cutover.
core-subagent-notification-visibility-targeted:
    cargo test -p codex-protocol parse_subagent_notification_response_item_ --lib -- --test-threads=1
    cargo test -p codex-tui raw_response_subagent_notification_renders_history -- --exact --test-threads=1

# Focused payload-free inference-attempt protocol and privacy-bounds contract.
inference-observation-contract-targeted:
    cargo test --locked -p codex-protocol protocol::inference_observation::tests --lib -- --test-threads=1

# Focused TUI thread-session approval persistence slice.
tui-thread-session-policy-targeted:
    cargo test -p codex-tui app::tests::store_active_thread_receiver_persists_per_thread_policy_overrides --lib -- --exact --test-threads=1
    cargo test -p codex-tui app::thread_settings::tests::permission_overrides_project_disabled_profile_without_active_profile --lib -- --exact --test-threads=1

# Focused TUI config-refresh session-state persistence slice.
tui-config-refresh-session-targeted:
    cargo test -p codex-tui app::tests::refresh_in_memory_config_from_disk_preserves_active_thread_session_state --lib -- --exact --test-threads=1
    cargo test -p codex-tui app::tests::fresh_session_config_uses_current_session_state --lib -- --exact --test-threads=1
    cargo test -p codex-tui app::tests::fresh_session_config_preserves_policy_mutability --lib -- --exact --test-threads=1

# Focused /agent picker, thread replay, and side-parent liveness slice.
tui-agent-picker-targeted:
    cargo test -p codex-tui app::tests::open_agent_picker_marks_loaded_threads_open --lib -- --exact --test-threads=1
    cargo test -p codex-tui app::tests::inactive_thread_started_notification_initializes_replay_session --lib -- --exact --test-threads=1
    cargo test -p codex-tui app::tests::session_lifecycle_requests::session_lifecycle_avoids_redundant_subagent_metadata_reads --lib -- --exact --test-threads=1
    cargo test -p codex-tui app::tests::selected_and_resumed_threads_use_server_capability_for_v1_and_v2_children --lib -- --exact --test-threads=1
    cargo test -p codex-tui app::thread_events::tests::thread_event_store_skips_large_replay_irrelevant_notifications --lib -- --exact --test-threads=1
    cargo test -p codex-tui app::thread_events::tests::thread_event_store_tracks_active_turn_lifecycle --lib -- --exact --test-threads=1
    cargo test -p codex-tui app::thread_events::tests::thread_event_store_rebase_preserves_mcp_startup_notifications --lib -- --exact --test-threads=1
    cargo test -p codex-tui app::tests::enqueue_thread_event_does_not_block_when_channel_full --lib -- --exact --test-threads=1
    cargo test -p codex-tui app::tests::side_parent_status_tracks_parent_turn_lifecycle --lib -- --exact --test-threads=1
    cargo test -p codex-tui app::tests::side_parent_status_prioritizes_input_over_approval --lib -- --exact --test-threads=1
    cargo test -p codex-tui app::tests::handle_start_side_seeds_navigation_before_thread_started --lib -- --exact --test-threads=1
    cargo test -p codex-tui app::tests::side_fork_config_is_persistent_and_appends_developer_guardrails --lib -- --exact --test-threads=1
    cargo test -p codex-tui app_server_session::tests::side_fork_skips_parent_title_lookup_but_normal_ephemeral_fork_keeps_it --lib -- --exact --test-threads=1
    cargo test -p codex-tui app_server_session::tests::side_fork_excludes_turns_without_clearing_regular_ephemeral_fork --lib -- --exact --test-threads=1
    cargo test -p codex-tui multi_agents::tests::picker_description_falls_back_to_thread_id_without_usage --lib -- --exact --test-threads=1
    cargo test -p codex-tui multi_agents::tests::picker_description_includes_compact_token_usage_when_present --lib -- --exact --test-threads=1
    cargo test -p codex-tui multi_agents::tests::picker_description_includes_remaining_context_when_known --lib -- --exact --test-threads=1
    cargo test -p codex-tui multi_agents::tests::picker_description_includes_compact_age_when_known --lib -- --exact --test-threads=1
    cargo test -p codex-tui multi_agents::tests::picker_description_includes_model_effort_and_task_when_available --lib -- --exact --test-threads=1

# Focused TUI replay and live collab-spawn requested-identity slice.
tui-collab-spawn-identity-targeted:
    cargo test -p codex-tui chatwidget::tests::history_replay::replayed_collab_spawn_terminal_uses_only_explicit_effective_identity --lib -- --exact --test-threads=1
    cargo test -p codex-tui chatwidget::tests::history_replay::replayed_historic_terminal_collab_spawn_renders_legacy_identity_as_effective --lib -- --exact --test-threads=1
    cargo test -p codex-tui chatwidget::tests::history_replay::replayed_failed_collab_spawn_without_receiver_keeps_requested_identity --lib -- --exact --test-threads=1
    cargo test -p codex-tui chatwidget::tests::app_server::live_app_server_collab_spawn_completed_renders_requested_model_and_effort --lib -- --exact --test-threads=1
    cargo test -p codex-tui chatwidget::tests::app_server::live_app_server_spawn_completion_does_not_fill_missing_effective_identity_from_metadata --lib -- --exact --test-threads=1

# Focused shared picker-model tool-description slice for upgradeable legacy

# visibility without widening to the TUI/app-server build graph.
spawn-agent-tool-model-surface-targeted:
    cargo test -p codex-tools spawn_agent_tool_v2_requires_task_name_and_lists_visible_models --lib -- --exact --test-threads=1
    cargo test -p codex-tools spawn_agent_tool_v2_lists_upgradeable_legacy_models --lib -- --exact --test-threads=1

# Focused shared picker-model spawned-agent-description slice for upgradeable

# legacy visibility without widening to the TUI/app-server build graph.
spawn-agent-description-model-surface-targeted:
    CODEX_JS_REPL_NODE_PATH="${CODEX_JS_REPL_NODE_PATH:-/tmp/codex-node22/bin/node}" cargo test -p codex-core --test all suite::spawn_agent_description::spawn_agent_description_lists_visible_models_and_reasoning_efforts -- --exact --test-threads=1

# Compatibility wrapper for the picker-model shared surface. The interactive
# TUI consumer still shares the same protocol helper, but this exact lane
# intentionally avoids compiling codex-tui while app-server drift contaminates

# small mapped picker-model runs.
tui-agent-picker-model-surface-targeted:
    just --justfile ../justfile spawn-agent-tool-model-surface-targeted
    just --justfile ../justfile spawn-agent-description-model-surface-targeted

# Focused /agent picker hierarchy visibility slice.
tui-agent-picker-tree-targeted:
    cargo test -p codex-tui app::tests::open_agent_picker_marks_loaded_threads_open --lib -- --exact --test-threads=1
    cargo test -p codex-tui app::tests::inactive_thread_started_notification_initializes_replay_session --lib -- --exact --test-threads=1
    cargo test -p codex-tui app::agent_navigation::tests::upsert_preserves_running_state_until_closed --lib -- --exact --test-threads=1
    cargo test -p codex-tui app::agent_navigation::tests::picker_tree_prefixes_reflect_nested_agent_paths --lib -- --exact --test-threads=1
    cargo test -p codex-tui app::loaded_threads::tests::finds_loaded_subagent_tree_for_primary_thread --lib -- --exact --test-threads=1

# Focused /agent picker usage and remaining-context visibility slice.
tui-agent-picker-usage-targeted:
    cargo test -p codex-tui app::tests::agent_picker_thread_token_usage_reads_inactive_thread_store --lib -- --exact --test-threads=1
    cargo test -p codex-tui app::tests::agent_picker_thread_token_usage_prefers_live_active_thread_usage --lib -- --exact --test-threads=1
    cargo test -p codex-tui app::tests::agent_picker_thread_token_usage_does_not_fallback_when_active_live_usage_is_zero --lib -- --exact --test-threads=1
    cargo test -p codex-tui app::tests::open_agent_picker_marks_loaded_threads_open --lib -- --exact --test-threads=1
    cargo test -p codex-tui app::tests::inactive_thread_started_notification_initializes_replay_session --lib -- --exact --test-threads=1
    cargo test -p codex-tui multi_agents::tests::picker_description_falls_back_to_thread_id_without_usage --lib -- --exact --test-threads=1
    cargo test -p codex-tui multi_agents::tests::picker_description_includes_compact_token_usage_when_present --lib -- --exact --test-threads=1
    cargo test -p codex-tui multi_agents::tests::picker_description_includes_remaining_context_when_known --lib -- --exact --test-threads=1
    cargo test -p codex-tui multi_agents::tests::picker_description_includes_compact_age_when_known --lib -- --exact --test-threads=1
    cargo test -p codex-tui multi_agents::tests::picker_selected_description_includes_permission_details_when_available --lib -- --exact --test-threads=1

# Focused TUI combined session-vs-thread token usage slice.
tui-agent-usage-totals-targeted:
    cargo test -p codex-tui app::tests::sync_session_tree_token_usage_updates_combined_status_line_items --lib -- --exact --test-threads=1
    cargo test -p codex-tui app::tests::sync_session_tree_token_usage_prefers_selected_subagent_usage_for_status_line --lib -- --exact --test-threads=1
    cargo test -p codex-tui chatwidget::tests::app_server::live_app_server_context_compaction_start_updates_status_header --lib -- --exact --test-threads=1
    cargo test -p codex-tui chatwidget::tests::app_server::live_app_server_context_compaction_completion_updates_status_header --lib -- --exact --test-threads=1
    cargo test -p codex-tui chatwidget::tests::history_replay::replayed_compaction_item_completion_restores_finished_status --lib -- --exact --test-threads=1
    cargo test -p codex-tui chatwidget::tests::status_and_layout::status_line_combined_token_items_use_session_totals --lib -- --exact --test-threads=1
    cargo test -p codex-tui chatwidget::tests::status_and_layout::status_line_combined_used_tokens_footer_snapshot --lib -- --exact --test-threads=1
    cargo test -p codex-tui status::tests::status_snapshot_distinguishes_session_and_thread_token_usage --lib -- --exact --test-threads=1

# Focused TUI weekly usage pacing status-line slice.
tui-weekly-pacing-status-line-targeted:
    cargo test -p codex-tui chatwidget::tests::status_and_layout::status_line_weekly_limit_renders_pacing_suffixes_from_live_status_line --lib -- --exact --test-threads=1
    cargo test -p codex-tui chatwidget::tests::status_and_layout::status_line_weekly_limit_renders_stale_suffix_over_pace_details --lib -- --exact --test-threads=1
    cargo test -p codex-tui chatwidget::tests::status_and_layout::status_line_weekly_limit_omits_pacing_when_inputs_are_missing --lib -- --exact --test-threads=1

# Focused TUI interrupt confirmation slice for Alt/meta-safe Esc handling.
tui-esc-interrupt-targeted:
    cargo nextest run -p codex-tui --no-fail-fast -- bottom_pane::tests::esc_requires_double_press_for_interrupt_when_running_task_by_default bottom_pane::tests::first_esc_renders_again_to_interrupt_hint bottom_pane::tests::esc_release_does_not_confirm_interrupt bottom_pane::tests::esc_with_alt_does_not_interrupt_running_task bottom_pane::tests::esc_single_press_interrupts_when_double_press_disabled --exact

# Focused TUI queued-follow-up front-insert slice.
tui-front-queue-submit-targeted:
    cargo test -p codex-tui bottom_pane::chat_composer::tests::ctrl_shift_q_queues_front_when_task_running --lib -- --exact --test-threads=1
    cargo test -p codex-tui chatwidget::tests::slash_commands::active_turn_model_slash_opens_picker_and_selection_does_not_start_turn --lib -- --exact --test-threads=1
    cargo test -p codex-tui chatwidget::tests::slash_commands::active_turn_permissions_slash_opens_picker_and_selection_does_not_start_turn --lib -- --exact --test-threads=1
    cargo test -p codex-tui chatwidget::tests::slash_commands::active_turn_plan_with_args_queues_prompt_under_plan_mode --lib -- --exact --test-threads=1
    cargo test -p codex-tui chatwidget::tests::slash_commands::active_turn_fast_slash_applies_service_tier_without_starting_turn --lib -- --exact --test-threads=1
    cargo test -p codex-tui app::tests::front_queued_follow_up_runs_before_back_queued_follow_up --lib -- --exact --test-threads=1
    cargo test -p codex-tui app::tests::replayed_turn_complete_submits_restored_front_queued_follow_up_first --lib -- --exact --test-threads=1
    cargo test -p codex-tui footer_snapshots -- --exact --test-threads=1
    cargo test -p codex-tui footer_collapse_snapshots -- --exact --test-threads=1

# Focused TUI transcript, viewport, narrow-layout, and terminal rendering slice.
tui-transcript-viewport-targeted:
    cargo test -p codex-tui app_backtrack::tests::transcript_turn_copy_source_stops_at_next_prompt_and_uses_latest_markdown --lib -- --exact --test-threads=1
    cargo test -p codex-tui app_backtrack::tests::transcript_turn_copy_source_supports_proposed_plan --lib -- --exact --test-threads=1
    cargo test -p codex-tui app_backtrack::tests::transcript_turn_copy_source_requires_finalized_markdown --lib -- --exact --test-threads=1
    cargo test -p codex-tui chatwidget::tests::slash_commands::transcript_turn_copy_includes_user_prompt_and_agent_markdown --lib -- --exact --test-threads=1
    cargo test -p codex-tui history_cell::messages::tests::finalized_markdown_reuses_lines_primed_by_transcript_height --lib -- --exact --test-threads=1
    cargo test -p codex-tui history_cell::messages::tests::finalized_markdown_cache_misses_when_width_or_render_style_changes --lib -- --exact --test-threads=1
    cargo test -p codex-tui history_cell::messages::tests::raw_markdown_bypasses_the_rich_render_cache --lib -- --exact --test-threads=1
    cargo test -p codex-tui history_cell::messages::tests::visualization_directives_are_not_cached --lib -- --exact --test-threads=1
    cargo test -p codex-tui history_cell::tests::raw_mode_toggle_transcript_snapshot --lib -- --exact --test-threads=1
    cargo test -p codex-tui history_cell::tests::session_header_clamps_to_narrow_width --lib -- --exact --test-threads=1
    cargo test -p codex-tui history_cell::tests::single_line_command_over_highlight_limit_uses_plain_text_fallback --lib -- --exact --test-threads=1
    cargo test -p codex-tui render::highlight::tests::long_single_line_bash_skips_highlighting_and_preserves_text --lib -- --exact --test-threads=1
    cargo test -p codex-tui history_cell::plans::tests::finalized_plan_reuses_lines_primed_by_transcript_height --lib -- --exact --test-threads=1
    cargo test -p codex-tui custom_terminal::tests::terminal_draw_coalesces_wrapped_hyperlink_output --lib -- --exact --test-threads=1
    cargo test -p codex-tui bottom_pane::chat_composer::tests::default_unified_mention_popup_snapshot --lib -- --exact --test-threads=1
    cargo test -p codex-tui bottom_pane::chat_composer::tests::unified_mention_popup_falls_back_from_bound_plugin_on_right_snapshot --lib -- --exact --test-threads=1
    cargo test -p codex-tui chatwidget::tests::app_server::live_app_server_turn_completion_repairs_dropped_message_deltas --lib -- --exact --test-threads=1
    cargo test -p codex-tui inline_visualization::tests::transcript_overlay_remeasures_visualization_when_artifact_becomes_available --lib -- --exact --test-threads=1
    cargo test -p codex-tui pager_overlay::tests::transcript_overlay_footer_status_snapshot --lib -- --exact --test-threads=1
    cargo test -p codex-tui pager_overlay::tests::transcript_overlay_footer_status_replaces_previous_message --lib -- --exact --test-threads=1
    cargo test -p codex-tui pager_overlay::tests::transcript_overlay_insert_preserves_cached_cell_heights --lib -- --exact --test-threads=1
    cargo test -p codex-tui pager_overlay::tests::transcript_overlay_remeasures_dynamic_cells_on_same_width_redraw --lib -- --exact --test-threads=1
    cargo test -p codex-tui status::tests::transcript_overlay_remeasures_status_after_rate_limit_refresh --lib -- --exact --test-threads=1
    cargo test -p codex-tui --test all suite::vt100_history::tmux_like_viewport_preserves_preexisting_history_content -- --exact --test-threads=1
    cargo test -p codex-tui --test all suite::vt100_history::android_style_narrow_viewport_keeps_url_content_from_being_clipped -- --exact --test-threads=1
    cargo test -p codex-tui --test all suite::vt100_history::committed_rows_survive_redraw_and_viewport_pressure -- --exact --test-threads=1

# Focused brokered-tool replay slice for app-server dynamic-tool begin/end

# projection and TUI replay visibility.
tui-brokered-tool-replay-targeted:
    cargo test -p codex-tui bridges_dynamic_tool_items_from_server_notifications --lib -- --exact --test-threads=1
    cargo test -p codex-tui replays_in_progress_dynamic_tool_items_without_completion_event --lib -- --exact --test-threads=1
    cargo test -p codex-tui live_app_server_dynamic_tool_item_start_clears_compaction_status_header --lib -- --exact --test-threads=1
    cargo test -p codex-tui active_dynamic_tool_call_renders_exact_arguments_and_preview --lib -- --exact --test-threads=1
    cargo test -p codex-tui replays_computer_use_items_from_turn_snapshots --lib -- --exact --test-threads=1
    cargo test -p codex-tui computer_use_fallback_message_only_shows_for_primary_thread --lib -- --exact --test-threads=1

# Focused multi-agent orchestration slice covering wait semantics, tool guidance,

# and generation-safe V2 residency eviction.
core-multi-agent-orchestration-targeted:
    RUST_MIN_STACK="${RUST_MIN_STACK:-{{ rust_min_stack }}}" CODEX_JS_REPL_NODE_PATH="${CODEX_JS_REPL_NODE_PATH:-/tmp/codex-node22/bin/node}" cargo nextest run -p codex-core --no-fail-fast --no-tests=fail --lib -- tools::handlers::multi_agents::tests::multi_agent_v2_list_agents_returns_completed_status tools::handlers::multi_agents_v2::wait::tests::completion_rule_distinguishes_any_from_all agent::control::residency::tests::residency_slot_reservation_unloads_oldest_idle_v2_agent agent::control::residency::tests::interrupted_v2_agent_remains_known_and_reloads_after_residency_eviction agent::control::residency::tests::ephemeral_v2_agent_is_not_evicted_without_reloadable_history agent::control::residency::tests::spawned_v2_terminal_child_unloads_at_terminal_idle_deadline agent::registry::tests::cold_status_text_stays_compact_when_json_escaped agent::registry::tests::spawn_delivery_ownership_prevents_cancellation_from_claiming_abort agent::control::tests::delivery_owned_initial_input_failure_reconciles_and_preserves_the_delivery_error agent::control::tests::ensure_v2_agent_loaded_reloads_registered_unloaded_agent tools::parallel::tests::configured_v2_spawn_cancellation_after_new_thread_reconciles_before_runtime_abort tools::parallel::tests::v1_spawn_cancellation_after_initial_delivery_returns_published_result tools::parallel::tests::configured_v2_spawn_cancellation_after_initial_delivery_returns_published_result context::world_state::multi_agent_mode::tests::custom_mode_removal_replaces_retained_instructions context::world_state::multi_agent_mode::tests::snapshots --exact
    RUST_MIN_STACK="${RUST_MIN_STACK:-{{ rust_min_stack }}}" CODEX_JS_REPL_NODE_PATH="${CODEX_JS_REPL_NODE_PATH:-/tmp/codex-node22/bin/node}" cargo nextest run -p codex-core --no-fail-fast --no-tests=fail --test all -- suite::spawn_agent_description::spawn_agent_description_lists_visible_models_and_reasoning_efforts suite::agent_execution::v2_evicted_completed_agent_keeps_final_status suite::agent_execution::v2_cold_mailbox_allows_eviction_and_replays_on_followup suite::multi_agent_mode::changing_configured_mode_hint_to_empty_appends_explicit_reset suite::pending_input::queue_only_agent_mail_wakes_sleeping_root_and_persists_message --exact
    RUST_MIN_STACK="${RUST_MIN_STACK:-{{ rust_min_stack }}}" CODEX_JS_REPL_NODE_PATH="${CODEX_JS_REPL_NODE_PATH:-/tmp/codex-node22/bin/node}" cargo test --locked -p codex-exec --test all suite::completion_backfill::ignores_unrelated_turn_completion_before_backfilling_primary_turn -- --exact --test-threads=1
    cargo nextest run -p codex-protocol --lib --no-tests=fail -- protocol::tests::turn_complete_without_provider_usage_remains_compatible --exact
    RUST_MIN_STACK=8388608 CODEX_JS_REPL_NODE_PATH="${CODEX_JS_REPL_NODE_PATH:-/tmp/codex-node22/bin/node}" cargo nextest run -p codex-core --no-fail-fast --test all --no-tests=fail -- suite::safety_check_downgrade::openai_model_header_mismatch_only_emits_one_warning_per_turn suite::safety_check_downgrade::nonterminal_response_identity_is_not_reported_when_follow_up_fails suite::compact_remote::remote_compact_replaces_history_for_followups suite::compact_remote::remote_compact_v2_reuses_compaction_trigger_for_followups suite::pending_input::steered_user_input_follows_compact_when_only_the_steer_needs_follow_up --exact

# Focused provider-usage persistence proof for interrupted and replaced turns.
core-provider-usage-aborted-targeted:
    cargo nextest run -p codex-protocol --lib --no-tests=fail -- protocol::tests::turn_aborted_without_provider_usage_remains_compatible --exact
    RUST_MIN_STACK=8388608 cargo nextest run -p codex-core --lib --no-tests=fail -- session::tests::self_aborted_task_preserves_provider_usage session::tests::replaced_and_budget_limited_turns_preserve_provider_usage --exact
    RUST_MIN_STACK=8388608 CODEX_JS_REPL_NODE_PATH="${CODEX_JS_REPL_NODE_PATH:-/tmp/codex-node22/bin/node}" cargo nextest run -p codex-core --no-fail-fast --test all --no-tests=fail -- suite::abort_tasks::interrupt_long_running_tool_emits_turn_aborted suite::resume::aborted_provider_usage_is_durable_isolated_and_legacy_compatible --exact

# Focused blocking-wait slices split by compile surface so hosted validation

# does not accumulate every target artifact in one runner workspace.
blocking-waits-core-targeted:
    cargo test -p codex-core capacity_retry::tests --lib -- --test-threads=1
    cargo test -p codex-api retryable_by_turn_loop --lib -- --test-threads=1
    CODEX_JS_REPL_NODE_PATH="${CODEX_JS_REPL_NODE_PATH:-/tmp/codex-node22/bin/node}" cargo test -p codex-core --test all server_overloaded_ -- --test-threads=1
    CODEX_JS_REPL_NODE_PATH="${CODEX_JS_REPL_NODE_PATH:-/tmp/codex-node22/bin/node}" cargo test -p codex-core --test all suite::compact_remote::auto_remote_compact_retries_server_overloaded -- --exact --test-threads=1
    RUST_MIN_STACK="${RUST_MIN_STACK:-{{ rust_min_stack }}}" CODEX_JS_REPL_NODE_PATH="${CODEX_JS_REPL_NODE_PATH:-/tmp/codex-node22/bin/node}" cargo test -p codex-core --test all suite::pending_input::any_new_input_interrupts_sleep -- --exact --test-threads=1
    CODEX_JS_REPL_NODE_PATH="${CODEX_JS_REPL_NODE_PATH:-/tmp/codex-node22/bin/node}" cargo test -p codex-core completion_rule_distinguishes_any_from_all --lib -- --exact --test-threads=1

blocking-waits-unified-exec-targeted:
    RUST_MIN_STACK="${RUST_MIN_STACK:-{{ rust_min_stack }}}" CODEX_JS_REPL_NODE_PATH="${CODEX_JS_REPL_NODE_PATH:-/tmp/codex-node22/bin/node}" cargo test -p codex-core unified_exec::process::tests::source_transcript_preserves_exec_end_when_delta_receiver_lags --lib -- --exact --test-threads=1
    RUST_MIN_STACK="${RUST_MIN_STACK:-{{ rust_min_stack }}}" CODEX_JS_REPL_NODE_PATH="${CODEX_JS_REPL_NODE_PATH:-/tmp/codex-node22/bin/node}" cargo test -p codex-core unified_exec::async_watcher::tests::streaming_output_finishes_on_close_without_waiting_for_grace --lib -- --exact --test-threads=1
    RUST_MIN_STACK="${RUST_MIN_STACK:-{{ rust_min_stack }}}" CODEX_JS_REPL_NODE_PATH="${CODEX_JS_REPL_NODE_PATH:-/tmp/codex-node22/bin/node}" cargo test -p codex-core unified_exec::async_watcher::tests::streaming_output_keeps_grace_as_fallback_without_close --lib -- --exact --test-threads=1
    RUST_MIN_STACK="${RUST_MIN_STACK:-{{ rust_min_stack }}}" CODEX_JS_REPL_NODE_PATH="${CODEX_JS_REPL_NODE_PATH:-/tmp/codex-node22/bin/node}" cargo test -p codex-core unified_exec::async_watcher::tests::exit_watcher_waits_for_late_network_denial_before_classifying_end --lib -- --exact --test-threads=1
    RUST_MIN_STACK="${RUST_MIN_STACK:-{{ rust_min_stack }}}" CODEX_JS_REPL_NODE_PATH="${CODEX_JS_REPL_NODE_PATH:-/tmp/codex-node22/bin/node}" cargo test -p codex-core unified_exec::process_manager::tests::pruning_does_not_evict_live_process_while_exited_process_is_finalizing --lib -- --exact --test-threads=1
    RUST_MIN_STACK="${RUST_MIN_STACK:-{{ rust_min_stack }}}" CODEX_JS_REPL_NODE_PATH="${CODEX_JS_REPL_NODE_PATH:-/tmp/codex-node22/bin/node}" cargo test -p codex-core unified_exec::process_manager::tests::failed_initial_end_for_unstored_process_prefers_source_transcript --lib -- --exact --test-threads=1
    RUST_MIN_STACK="${RUST_MIN_STACK:-{{ rust_min_stack }}}" CODEX_JS_REPL_NODE_PATH="${CODEX_JS_REPL_NODE_PATH:-/tmp/codex-node22/bin/node}" cargo test -p codex-core unified_exec::process_manager::tests::failed_exec_end_uses_fallback_when_source_transcript_is_empty --lib -- --exact --test-threads=1
    RUST_MIN_STACK="${RUST_MIN_STACK:-{{ rust_min_stack }}}" CODEX_JS_REPL_NODE_PATH="${CODEX_JS_REPL_NODE_PATH:-/tmp/codex-node22/bin/node}" cargo nextest run -j 1 --retries 0 -p codex-core --test all -- suite::unified_exec::unified_exec_formats_large_output_summary --exact
    RUST_MIN_STACK="${RUST_MIN_STACK:-{{ rust_min_stack }}}" CODEX_JS_REPL_NODE_PATH="${CODEX_JS_REPL_NODE_PATH:-/tmp/codex-node22/bin/node}" cargo nextest run -j 1 --retries 0 -p codex-core --test all -- suite::unified_exec::unified_exec_full_lifecycle_with_background_end_event --exact
    RUST_MIN_STACK="${RUST_MIN_STACK:-{{ rust_min_stack }}}" CODEX_JS_REPL_NODE_PATH="${CODEX_JS_REPL_NODE_PATH:-/tmp/codex-node22/bin/node}" cargo nextest run -j 1 --retries 0 -p codex-core --test all -- suite::unified_exec::unified_exec_end_event_is_bounded_when_descendant_holds_output_open --exact
    RUST_MIN_STACK="${RUST_MIN_STACK:-{{ rust_min_stack }}}" CODEX_JS_REPL_NODE_PATH="${CODEX_JS_REPL_NODE_PATH:-/tmp/codex-node22/bin/node}" cargo nextest run -j 1 -p codex-core --test all -- suite::unified_exec::exec_command_reports_chunk_and_exit_metadata --exact
    RUST_MIN_STACK="${RUST_MIN_STACK:-{{ rust_min_stack }}}" CODEX_JS_REPL_NODE_PATH="${CODEX_JS_REPL_NODE_PATH:-/tmp/codex-node22/bin/node}" cargo nextest run -j 1 -p codex-core --test all -- suite::unified_exec::write_stdin_returns_exit_metadata_and_clears_session --exact

blocking-waits-app-server-targeted:
    cargo test -p codex-tui live_app_server_retrying_server_overloaded_error_keeps_task_running --lib -- --test-threads=1
    cargo clean -p codex-tui
    RUST_MIN_STACK="${RUST_MIN_STACK:-{{ rust_min_stack }}}" cargo nextest run -j 1 -p codex-app-server --test all -- suite::v2::turn_start::command_execution_notifications_include_process_id --exact

blocking-waits-mcp-targeted:
    cargo nextest run -j 1 -p codex-mcp-server --test all -- suite::codex_tool::shell_command_approval_emits_task_complete_before_tool_response --exact

blocking-waits-targeted: blocking-waits-core-targeted blocking-waits-unified-exec-targeted blocking-waits-app-server-targeted blocking-waits-mcp-targeted

# Focused custom-prompt discovery and review-flow slice.
custom-prompts-targeted:
    cargo test -p codex-core custom_prompts::tests:: --lib -- --test-threads=1
    cargo test -p codex-prompts resolve_review_request_custom_target_ --lib -- --test-threads=1
    cargo test -p codex-tui chatwidget::tests::review_mode::review_popup_custom_prompt_action_sends_event --lib -- --exact --test-threads=1
    cargo test -p codex-tui chatwidget::tests::review_mode::custom_prompt_submit_sends_review_op --lib -- --exact --test-threads=1
    cargo test -p codex-tui chatwidget::tests::review_mode::custom_prompt_enter_empty_does_not_send --lib -- --exact --test-threads=1
    cargo test -p codex-tui chatwidget::tests::review_mode::review_custom_prompt_escape_navigates_back_then_dismisses --lib -- --exact --test-threads=1

# Focused downstream MCP safety slice for config mutability and OAuth fallback

# hardening.
mcp-tool-exposure-targeted:
    cargo test -p codex-core mcp_tool_exposure::tests:: --lib -- --test-threads=1
    cargo test -p codex-mcp list_all_tools_ --lib -- --test-threads=1
    cargo test -p codex-mcp capture_binding_uses_the_ready_clients_own_tools --lib -- --exact --test-threads=1
    cargo test -p codex-mcp startup_recovery_ --lib -- --test-threads=1
    cargo test -p codex-mcp binding_resource_catalogue_ --lib -- --test-threads=1
    cargo test -p codex-core all_server_resource_payload_reports_partial_listing_failures --lib -- --exact --test-threads=1
    cargo build -p codex-rmcp-client --bin test_streamable_http_server
    RUST_BACKTRACE=1 RUST_MIN_STACK="${RUST_MIN_STACK:-{{ rust_min_stack }}}" cargo nextest run -p codex-core --test all -- suite::rmcp_client::all_server_resource_catalogues_report_unready_server_without_hiding_ready_results --exact
    RUST_BACKTRACE=1 RUST_MIN_STACK="${RUST_MIN_STACK:-{{ rust_min_stack }}}" cargo nextest run -p codex-core --test all -- suite::rmcp_client::streamable_http_failed_startup_recovers_on_next_binding --exact
    RUST_BACKTRACE=1 RUST_MIN_STACK="${RUST_MIN_STACK:-{{ rust_min_stack }}}" cargo nextest run -p codex-core --test all -- suite::rmcp_client::streamable_http_cancelled_startup_reconnect_stays_unready --exact
    RUST_MIN_STACK="${RUST_MIN_STACK:-{{ rust_min_stack }}}" cargo nextest run -p codex-core --test all -- suite::rmcp_client::stdio_mcp_read_only_tool_calls_run_concurrently_without_server_opt_in --exact
    RUST_MIN_STACK="${RUST_MIN_STACK:-{{ rust_min_stack }}}" cargo nextest run -p codex-core --test all -- suite::rmcp_client::stdio_mcp_parallel_tool_calls_opt_in_runs_concurrently --exact

mcp-safety-targeted:
    cargo test -p codex-core config::edit_tests::blocking_replace_mcp_servers_round_trips --lib -- --exact --test-threads=1
    cargo test -p codex-core config::edit_tests::blocking_replace_mcp_servers_serializes_tool_approval_overrides --lib -- --exact --test-threads=1
    cargo test -p codex-core config::service_tests::write_value_supports_custom_mcp_server_default_tool_approval_mode --lib -- --exact --test-threads=1
    cargo test -p codex-rmcp-client load_oauth_tokens_ --lib -- --test-threads=1
    cargo test -p codex-rmcp-client oauth::tests::request_oauth_token_response_strips_refresh_material --lib -- --exact --test-threads=1
    cargo test -p codex-rmcp-client oauth::tests::refresh_expires_in_from_timestamp_marks_expired_tokens --lib -- --exact --test-threads=1
    cargo test -p codex-rmcp-client oauth::tests::refresh_credentials_do_not_expose_granted_scopes_to_rmcp --lib -- --exact --test-threads=1
    cargo test -p codex-rmcp-client persist_if_needed_ --lib -- --test-threads=1
    cargo test -p codex-rmcp-client rmcp_client::tests::oauth_authorization_required_refreshes_oauth --lib -- --exact --test-threads=1
    cargo test -p codex-rmcp-client rmcp_client::tests::streamable_http_auth_required_refreshes_oauth --lib -- --exact --test-threads=1
    cargo test --locked -p codex-rmcp-client --test streamable_http_user_agent streamable_http_requests_preserve_configured_user_agent -- --exact --test-threads=1
    cargo test --locked -p codex-rmcp-client --test streamable_http_oauth_startup refreshes_expired_persisted_token_before_initialize -- --exact --test-threads=1
    RUST_MIN_STACK="${RUST_MIN_STACK:-{{ rust_min_stack }}}" cargo test -p codex-core --test all suite::rmcp_client::streamable_http_with_oauth_round_trip -- --exact --test-threads=1

# Focused downstream MCP OAuth device-login slice for browserless hosts.
mcp-device-login-targeted:
    cargo test -p codex-rmcp-client auth_status::tests::discover_streamable_http_oauth_returns_normalized_scopes --lib -- --exact --test-threads=1
    cargo test -p codex-rmcp-client perform_oauth_login::tests::start_authorization_routes_dynamic_registration_through_configured_client --lib -- --exact --test-threads=1
    cargo test -p codex-rmcp-client perform_oauth_device_login::tests::device_login_dynamic_registration_uses_device_grant_shape --lib -- --exact --test-threads=1
    cargo test -p codex-rmcp-client perform_oauth_device_login::tests::device_login_dynamic_registration_omits_refresh_when_not_supported --lib -- --exact --test-threads=1
    cargo test -p codex-rmcp-client perform_oauth_device_login::tests::device_login_polls_until_authorized --lib -- --exact --test-threads=1
    cargo test -p codex-client custom_ca::tests::reqwest_client_builder_installs_rustls_provider_without_custom_ca --lib -- --exact --test-threads=1

# Focused sub-agent selection, role, backend, and cold-reload slice.
core-subagent-model-pinning-targeted:
    RUST_MIN_STACK="${RUST_MIN_STACK:-{{ rust_min_stack }}}" CODEX_JS_REPL_NODE_PATH="${CODEX_JS_REPL_NODE_PATH:-/tmp/codex-node22/bin/node}" cargo nextest run -p codex-core --no-fail-fast --no-tests=fail --lib -- agent::role::tests::apply_role_preserves_unspecified_keys agent::role::tests::spawn_tool_spec_marks_terminal_babysitter_locked_model_and_reasoning_effort tools::handlers::multi_agents_spec::tests::spawn_agent_tool_v2_requires_task_name_and_lists_visible_models tools::handlers::multi_agents::tests::spawn_agent_reasoning_effort_accepts_empty_support_metadata tools::handlers::multi_agents::tests::multi_agent_v2_spawn_accepts_child_model_without_backend_assignment tools::handlers::multi_agents::tests::multi_agent_v2_spawn_accepts_luna_compatibility_override tools::handlers::multi_agents::tests::multi_agent_v2_spawn_rejects_child_model_from_different_backend tools::handlers::multi_agents::tests::multi_agent_v2_spawn_fork_turns_all_rejects_agent_type_override tools::handlers::multi_agents::tests::multi_agent_v2_spawn_partial_fork_turns_allows_agent_type_override tools::handlers::multi_agents::tests::multi_agent_v2_spawn_terminal_babysitter_uses_role_locked_model tools::handlers::multi_agents::tests::failed_spawn_keeps_requested_identity_separate_from_terminal_effective_identity tools::handlers::multi_agents::tests::multi_agent_v2_spawn_without_snapshot_omits_unobserved_effective_identity --exact
    RUST_MIN_STACK="${RUST_MIN_STACK:-{{ rust_min_stack }}}" CODEX_JS_REPL_NODE_PATH="${CODEX_JS_REPL_NODE_PATH:-/tmp/codex-node22/bin/node}" cargo nextest run -p codex-core --no-fail-fast --no-tests=fail --test all -- suite::subagent_notifications::spawn_agent_uses_configured_subagent_defaults suite::subagent_notifications::spawn_agent_preserves_configured_defaults_through_unrelated_role suite::subagent_notifications::spawn_agent_requested_model_and_reasoning_override_inherited_settings_without_role suite::subagent_notifications::spawn_agent_role_overrides_requested_model_and_reasoning_settings suite::subagent_notifications::spawn_agent_rejects_reasoning_effort_unsupported_by_role_model --exact
    RUST_MIN_STACK="${RUST_MIN_STACK:-{{ rust_min_stack }}}" CODEX_JS_REPL_NODE_PATH="${CODEX_JS_REPL_NODE_PATH:-/tmp/codex-node22/bin/node}" cargo nextest run -p codex-core --no-fail-fast --no-tests=fail --test all -- suite::subagent_notifications::spawned_full_history_v2_child_uses_model_precedence_without_dropping_context
    RUST_MIN_STACK="${RUST_MIN_STACK:-{{ rust_min_stack }}}" cargo nextest run -p codex-state -p codex-thread-store --no-fail-fast --no-tests=fail --lib -- extract::tests::turn_context_sets_model_and_reasoning_effort extract::tests::thread_settings_applied_updates_resume_metadata local::read_thread::tests::read_thread_keeps_complete_indexed_identity_during_rollout_overlay thread_metadata_sync::tests::thread_settings_applied_updates_live_metadata types::tests::thread_metadata_patch_round_trips_optional_clears --exact
    RUST_MIN_STACK="${RUST_MIN_STACK:-{{ rust_min_stack }}}" cargo nextest run -p codex-core --no-fail-fast --no-tests=fail --lib -- agent::control::tests::spawn_agent_can_fork_parent_thread_history_with_sanitized_items agent::control::tests::paginated_subagent_fork_cold_resume_preserves_child_settings agent::control::tests::ensure_v2_agent_loaded_reloads_registered_unloaded_agent --exact
    RUST_MIN_STACK="${RUST_MIN_STACK:-{{ rust_min_stack }}}" CODEX_JS_REPL_NODE_PATH="${CODEX_JS_REPL_NODE_PATH:-/tmp/codex-node22/bin/node}" cargo nextest run -p codex-core --no-fail-fast --no-tests=fail --test all -- suite::multi_agent_resume::cold_root_resume_restores_agent_identity_and_reloads_target_on_followup suite::multi_agent_resume::cold_root_resume_restores_agent_identity_and_role_on_followup --exact

# Focused persisted-descendant inventory slice for subtree close/resume behavior.
core-persisted-subagent-descendants-targeted:
    cargo test -p codex-state thread_spawn_edges_track_directional_status --lib -- --exact --test-threads=1

# Focused app-server collab-spawn requested/effective identity projection slice.
app-server-collab-spawn-identity-targeted:
    RUST_MIN_STACK="${RUST_MIN_STACK:-{{ rust_min_stack }}}" cargo test --locked -p codex-app-server --test all suite::v2::turn_start::turn_start_emits_multi_agent_v1_spawn_requested_and_effective_identity_v2 -- --exact --test-threads=1
    RUST_MIN_STACK="${RUST_MIN_STACK:-{{ rust_min_stack }}}" cargo test --locked -p codex-app-server --test all suite::v2::turn_start::turn_start_emits_multi_agent_v1_role_spawn_requested_and_effective_identity_v2 -- --exact --test-threads=1

# Focused analytics and usage consumer slice for collab-spawn requested identity.
collab-spawn-identity-consumers-targeted:
    cargo test --locked -p codex-analytics analytics_client_tests::collab_tool_item_analytics_keeps_requested_identity_from_the_started_event --lib -- --exact --test-threads=1
    cargo test --locked -p codex-state runtime::usage::tests::usage_logger_preserves_optional_spawn_request_identity --lib -- --exact --test-threads=1

# Focused app-server thread surface slice.
app-server-thread-cwd-targeted:
    RUST_MIN_STACK="${RUST_MIN_STACK:-{{ rust_min_stack }}}" cargo test --locked -p codex-app-server --test all suite::conversation_summary:: -- --test-threads=1
    RUST_MIN_STACK="${RUST_MIN_STACK:-{{ rust_min_stack }}}" cargo test --locked -p codex-app-server --test all suite::v2::thread_list:: -- --test-threads=1
    RUST_MIN_STACK="${RUST_MIN_STACK:-{{ rust_min_stack }}}" cargo test --locked -p codex-app-server --test all suite::v2::thread_read::thread_read_returns_summary_without_turns -- --exact --test-threads=1
    RUST_MIN_STACK="${RUST_MIN_STACK:-{{ rust_min_stack }}}" cargo test --locked -p codex-app-server --test all suite::v2::thread_resume::thread_resume_returns_rollout_history -- --exact --test-threads=1
    RUST_MIN_STACK="${RUST_MIN_STACK:-{{ rust_min_stack }}}" cargo test --locked -p codex-app-server --test all suite::v2::thread_fork::thread_fork_treats_explicit_null_thread_instructions_as_missing -- --exact --test-threads=1
    RUST_MIN_STACK="${RUST_MIN_STACK:-{{ rust_min_stack }}}" cargo test --locked -p codex-app-server --test all suite::v2::turn_start::turn_start_treats_explicit_null_thread_instructions_as_missing -- --exact --test-threads=1
    RUST_MIN_STACK="${RUST_MIN_STACK:-{{ rust_min_stack }}}" cargo test --locked -p codex-app-server --test all suite::v2::turn_start::turn_start_emits_multi_agent_v1_role_spawn_requested_and_effective_identity_v2 -- --exact --test-threads=1

# Focused remote app-server client transport and lifecycle slice.
app-server-client-targeted:
    RUST_MIN_STACK="${RUST_MIN_STACK:-{{ rust_min_stack }}}" cargo test --locked -p codex-app-server-client

# Focused app-server v2 contract slice for high-signal client-facing RPCs.
app-server-v2-contract-targeted:
    cargo test --locked -p codex-app-server-protocol
    cargo test --locked -p codex-app-server-transport serialize_outgoing_message_preserves_wire_shape --lib -- --exact --test-threads=1
    cargo test --locked -p codex-app-server --test all suite::v2::app_read:: -- --test-threads=1
    cargo test --locked -p codex-app-server --test all suite::v2::initialize:: -- --test-threads=1
    cargo test --locked -p codex-app-server --test all suite::v2::plugin_list::plugin_list_force_refetch_waits_for_same_path_local_plugin_upgrade -- --exact --test-threads=1
    cargo test --locked -p codex-app-server --test all suite::v2::skills_list::skills_list_uses_cached_result_after_session_default_writes_until_force_reload -- --exact --test-threads=1
    cargo test --locked -p codex-app-server --test all suite::v2::mcp_server_status::mcp_server_status_list_tools_and_auth_only_skips_slow_inventory_calls -- --exact --test-threads=1
    cargo test --locked -p codex-app-server --test all suite::v2::thread_start:: -- --test-threads=1
    cargo test --locked -p codex-app-server --test all suite::v2::thread_metadata_update::thread_metadata_update_pins_and_unpins_with_filtered_recency_pagination -- --exact --test-threads=1
    cargo test --locked -p codex-app-server --test all suite::v2::thread_read::paginated_thread_name_preserves_metadata_across_read_list_and_resume -- --exact --test-threads=1
    cargo test --locked -p codex-app-server --test all suite::v2::thread_read::thread_search_occurrences_reads_paginated_projection -- --exact --test-threads=1
    cargo test --locked -p codex-app-server --test all suite::v2::thread_read::paginated_history_lists_use_projected_turns_and_items -- --exact --test-threads=1
    cargo test --locked -p codex-app-server --test all suite::v2::thread_resume::thread_resume_preserves_goal_first_and_fork_settings -- --exact --test-threads=1
    cargo test --locked -p codex-app-server --test all suite::v2::turn_start::turn_start_treats_explicit_null_thread_instructions_as_missing -- --exact --test-threads=1
    cargo test --locked -p codex-app-server --test all suite::v2::realtime_conversation::websocket_v3_routes_handoffs_by_session_mode -- --exact --test-threads=1
    cargo test --locked -p codex-app-server --test all suite::v2::git_attribution::git_attribution_follows_authenticated_workspace_policy -- --exact --test-threads=1
    cargo test --locked -p codex-app-server --test all suite::v2::git_attribution::cold_resume_replaces_legacy_attribution_without_duplication -- --exact --test-threads=1
    cargo test --locked -p codex-app-server --test all suite::v2::web_search::standalone_web_search_round_trips_output_for_custom_provider -- --exact --test-threads=1

# Focused MCP server contract slice for approval and tool response ordering.
mcp-server-contract-targeted:
    cargo test --locked -p codex-mcp-server --test all suite::codex_tool::shell_command_approval_emits_task_complete_before_tool_response -- --exact --test-threads=1
    cargo test --locked -p codex-mcp-server --test all suite::codex_tool::test_patch_approval_triggers_elicitation -- --exact --test-threads=1
    cargo test --locked -p codex-mcp-server --test all suite::codex_tool::test_codex_tool_passes_base_instructions -- --exact --test-threads=1

# Focused exec-server protocol slice for websocket, process, and policy lifecycle.
exec-server-targeted:
    cargo test --locked -p codex-exec-server --lib -- --test-threads=1
    cargo test --locked -p codex-exec-server --test initialize -- --test-threads=1
    cargo test --locked -p codex-exec-server --test websocket -- --test-threads=1
    cargo test --locked -p codex-exec-server --test process exec_server_starts_process_over_websocket -- --exact --test-threads=1

# Focused CLI surface slice for parser, subcommand, and diagnostics contracts.
cli-surface-targeted:
    cargo test --locked -p codex-cli --bin codex main::tests:: -- --test-threads=1
    cargo test --locked -p codex-cli --bin codex mcp_cmd::tests::mcp_login_parses_device_auth_flag -- --exact --test-threads=1
    cargo test --locked -p codex-cli doctor::tests:: --lib -- --test-threads=1
    cargo test --locked -p codex-cli --test debug_clear_memories -- --test-threads=1
    cargo test --locked -p codex-cli --test login debug_prompt_input_follows_authenticated_attribution_setting -- --exact --test-threads=1

# Focused native computer-use bridge slice for app-server protocol routing,

# client response handling, and Android tool lifecycle injection.
app-server-computer-use-targeted:
    cargo test --locked -p codex-app-server --test all suite::v2::computer_use:: -- --test-threads=1

# Focused native computer-use TUI app-server request and provider routing slice.
tui-native-computer-use-targeted:
    cargo test --locked -p codex-tui app::app_server_requests::tests::does_not_mark_computer_use_calls_as_unsupported --lib -- --exact --test-threads=1
    cargo test --locked -p codex-tui computer_use_display::tests::action_labels_match_native_surfaces --lib -- --exact --test-threads=1
    cargo test --locked -p codex-tui history_cell::tests::computer_use_call_labels_native_surfaces --lib -- --exact --test-threads=1
    cargo test --locked -p codex-tui history_cell::tests::computer_use_call_failure_is_visible --lib -- --exact --test-threads=1
    cargo test --locked -p codex-tui chatwidget::tests::history_replay::replayed_completed_computer_use_call_is_visible --lib -- --exact --test-threads=1
    cargo test --locked -p codex-tui chatwidget::tests::history_replay::live_computer_use_call_is_visible_while_active_and_after_completion --lib -- --exact --test-threads=1
    cargo test --locked -p codex-browser-computer-use --lib -- --test-threads=1
    cargo test --locked -p codex-tui desktop_computer_use_provider::tests:: --lib -- --test-threads=1
    cargo test --locked -p codex-tui computer_use_provider::tests:: --lib -- --test-threads=1

# Focused exec native computer-use slice for configured browser tool

# advertisement and provider request handling in non-interactive sessions.
exec-native-computer-use-targeted:
    cargo test --locked -p codex-exec tests::thread_lifecycle_params_include_configured_native_dynamic_tools --lib -- --exact --test-threads=1
    cargo test --locked -p codex-exec --test all event_processor_with_json_output::computer_use_started_and_completed_translate_to_thread_events -- --exact --test-threads=1

# Focused native computer-use tool registry slice for canonical schema conversion

# and deferred tool-search discovery.
native-computer-use-tool-registry-targeted:
    cargo test --locked -p codex-tools canonical_android_dynamic_tool --lib -- --test-threads=1
    cargo test --locked -p codex-tools canonical_browser_dynamic_tool --lib -- --test-threads=1
    cargo test --locked -p codex-tools desktop_tool --lib -- --test-threads=1
    cargo test --locked -p codex-tools browser_backend_schema_exposes_supported_provider_backends --lib -- --test-threads=1
    cargo test --locked -p codex-tools native_computer_use_registry_classifies_android_and_browser_tools --lib -- --test-threads=1
    cargo test --locked -p codex-tools native_computer_use_registry_classifies_desktop_tools --lib -- --test-threads=1
    cargo test --locked -p codex-android-computer-use configured_android_tools_load_from_explicit_codex_home --lib -- --test-threads=1
    cargo test --locked -p codex-android-computer-use prefer_stable_ui_defaults_to_true_unless_disabled --lib -- --test-threads=1
    cargo test --locked -p codex-core browser_handler_uses_browser_adapter --lib -- --test-threads=1
    cargo test --locked -p codex-core computer_use_call_times_out_and_unregisters_pending_response --lib -- --test-threads=1
    cargo test --locked -p codex-tui browser_provider_requires_configured_backend --lib -- --test-threads=1
    cargo test --locked -p codex-browser-computer-use command_provider_bridge_returns_native_image_response --lib -- --test-threads=1
    cargo test --locked -p codex-browser-computer-use browser_provider_response_preserves_native_image --lib -- --test-threads=1
    cargo test --locked -p codex-tui unknown_computer_use_tool_is_not_claimed_by_provider_registry --lib -- --test-threads=1

# Focused native computer-use operator diagnostics slice.
native-computer-use-doctor-targeted:
    cargo test --locked -p codex-cli doctor::tests::native_computer_use_check_reports_android_browser_and_desktop_config_files -- --exact --test-threads=1

# Hosted Playwright and skill-contract evidence for the native browser surface.
native-browser-evidence-targeted:
    .github/scripts/validation-lanes/native-browser-evidence.sh

# Focused downstream agent-workflow helper sanity slice.
[no-cd]
agent-workflow-sanity:
    cd "{{ justfile_directory() }}" && python3 -m py_compile \
        .codex/skills/babysit-pr/scripts/gh_pr_watch.py \
        .codex/skills/babysit-gh-workflow-run/scripts/gh_workflow_run_watch.py \
        .codex/skills/babysit-gh-workflow-run/scripts/gh_dispatch_and_watch.py \
        .codex/skills/babysit-gh-workflow-run/scripts/gh_pr_delivery_watch.py \
        .codex/skills/sedna/subagent-session-tail/scripts/inspect_subagent_tail.py
    cd "{{ justfile_directory() }}" && python3 .codex/skills/babysit-gh-workflow-run/tests/test_gh_workflow_run_watch.py
    cd "{{ justfile_directory() }}" && python3 .codex/skills/babysit-gh-workflow-run/tests/test_gh_dispatch_and_watch.py
    cd "{{ justfile_directory() }}" && python3 .codex/skills/babysit-gh-workflow-run/tests/test_gh_pr_delivery_watch.py
    cd "{{ justfile_directory() }}" && python3 .codex/skills/sedna/subagent-session-tail/tests/test_inspect_subagent_tail.py
    cd "{{ justfile_directory() }}" && python3 .codex/skills/sedna/subagent-session-tail/scripts/inspect_subagent_tail.py --help >/dev/null

# Focused shell-tool-mcp package sanity slice.
[no-cd]
shell-tool-mcp-ci:
    cd "{{ justfile_directory() }}" && corepack enable
    cd "{{ justfile_directory() }}" && pnpm install --frozen-lockfile
    cd "{{ justfile_directory() }}" && pnpm --filter @openai/codex-shell-tool-mcp run format
    cd "{{ justfile_directory() }}" && pnpm --filter @openai/codex-shell-tool-mcp test
    cd "{{ justfile_directory() }}" && pnpm --filter @openai/codex-shell-tool-mcp run build

# Focused build/config policy sanity slice for install and workspace checks.
[no-cd]
build-policy-sanity:
    cd "{{ justfile_directory() }}" && bash -n scripts/install/install.sh
    cd "{{ justfile_directory() }}" && python3 -m py_compile scripts/stage_npm_packages.py .github/scripts/verify_bazel_clippy_lints.py .github/scripts/verify_cargo_workspace_manifests.py
    cd "{{ justfile_directory() }}" && python3 .github/scripts/verify_bazel_clippy_lints.py
    cd "{{ justfile_directory() }}" && python3 .github/scripts/verify_cargo_workspace_manifests.py

# Focused code-mode declaration rendering and metadata slice.
code-mode-declaration-targeted:
    cargo test --locked -p codex-tools code_mode_ --lib -- --test-threads=1
    cargo test --locked -p codex-tools raw_tool_json_matches_value_encoding --lib -- --exact --test-threads=1
    RUST_MIN_STACK="${RUST_MIN_STACK:-{{ rust_min_stack }}}" CODEX_JS_REPL_NODE_PATH="${CODEX_JS_REPL_NODE_PATH:-/tmp/codex-node22/bin/node}" cargo nextest run -p codex-core --no-fail-fast --test-threads=1 --test all -- suite::code_mode::code_mode_exports_all_tools_metadata_for_builtin_tools suite::code_mode::code_mode_exports_all_tools_metadata_for_namespaced_mcp_tools suite::code_mode::code_mode_declaration_normalization_is_layout_tolerant_and_semantically_strict suite::code_mode::code_mode_native_browser_result_forwards_screenshot_as_input_image suite::code_mode::code_mode_failed_native_browser_result_forwards_input_image suite::code_mode::code_mode_can_call_hidden_dynamic_tools suite::code_mode::code_mode_excludes_configured_nested_tool_namespaces --exact

# Focused tool-context serialization slice for custom/function/abort outputs.
core-context-serialization-targeted:
    cargo test -p codex-core tools::handlers::mcp_resource::tests::serialize_read_resource_output_ --lib -- --test-threads=1
    cargo test -p codex-core tools::handlers::mcp_resource::tests::large_json_resource_fails_closed_for_model_and_preserves_code_mode_payload --lib -- --exact --test-threads=1
    cargo test -p codex-core tools::handlers::mcp_resource::tests::history_does_not_retruncate_bounded_json_resource_error --lib -- --exact --test-threads=1
    cargo test -p codex-core tools::context::tests::custom_tool_calls_should_roundtrip_as_custom_outputs --lib -- --exact
    cargo test -p codex-core tools::context::tests::function_payloads_remain_function_outputs --lib -- --exact
    cargo test -p codex-core tools::context::tests::aborted_tool_output_serializes_ --lib -- --test-threads=1
    cargo test -p codex-core --test all suite::abort_tasks::interrupt_tool_records_history_entries -- --exact --test-threads=1

# Focused attestation contract slice for phase-2 fail-closed reuse semantics.
core-attestation-targeted:
    RUST_MIN_STACK="${RUST_MIN_STACK:-{{ rust_min_stack }}}" cargo test -p codex-memories-write phase2_attestation --lib -- --test-threads=1
    RUST_MIN_STACK="${RUST_MIN_STACK:-{{ rust_min_stack }}}" cargo test -p codex-memories-write memories_startup_phase2 --lib -- --test-threads=1
    RUST_MIN_STACK="${RUST_MIN_STACK:-{{ rust_min_stack }}}" cargo test -p codex-state phase2_attestation --lib -- --test-threads=1

# Focused startup repair slice for state DBs with schema changes applied but

# missing SQLx migration records.
state-migration-repair-targeted:
    cargo test -p codex-state migrations::tests::state_migration_versions_are_unique --lib -- --exact --test-threads=1
    cargo test -p codex-state migrations::tests::pinned_threads_migration_defaults_existing_and_legacy_rows_to_unpinned --lib -- --exact --test-threads=1
    cargo test -p codex-state migrations::tests::repairs_recency_migration_that_was_applied_as_version_38 --lib -- --exact --test-threads=1
    cargo test -p codex-state migrations::tests::repairs_visible_sort_indexes_migration_that_was_applied_as_version_40 --lib -- --exact --test-threads=1
    cargo test -p codex-state migrations::tests::repairs_remote_control_enabled_migration_that_was_applied_as_version_41 --lib -- --exact --test-threads=1
    cargo test -p codex-state migrations::tests::repairs_external_agent_config_import_migration_that_was_applied_as_version_42 --lib -- --exact --test-threads=1
    cargo test -p codex-state migrations::tests::external_agent_config_import_provider_migration_follows_table_creation_on_fresh_database --lib -- --exact --test-threads=1
    cargo test -p codex-state migrations::tests::repairs_external_agent_config_import_provider_migration_that_was_applied_as_version_44 --lib -- --exact --test-threads=1
    cargo test -p codex-state runtime::external_agent_config_imports::tests::records_completion_by_import_id --lib -- --exact --test-threads=1
    cargo test -p codex-state migrations::tests::repair_state_migration_version_collisions_succeeds_while_writer_slot_is_held --lib -- --exact --test-threads=1
    cargo test -p codex-state runtime::tests::open_state_sqlite_marks_existing_thread_source_migration_applied -- --exact --test-threads=1

# Codex authoritative usage.sqlite logging contracts.
core-ledger-smoke:
    cargo nextest run -p codex-state --no-fail-fast -- runtime::tests::init_removes_legacy_logs_and_usage_db_files runtime::usage::tests::usage_logger_records_requested_model_and_quota_snapshot runtime::usage::tests::usage_logger_tracks_tool_call_lifecycle runtime::usage::tests::usage_logger_captures_spawn_request_and_fork_snapshot runtime::usage::tests::usage_logger_resolves_root_thread_from_parent_or_fork runtime::usage::tests::usage_logger_clears_turn_snapshot_after_turn_complete runtime::usage::tests::usage_logger_resolves_root_thread_from_persisted_lineage_after_restart --exact
    cargo test -p codex-thread-store live_thread_tests::concurrent_appends_keep_sqlite_metadata_in_canonical_history_order --lib -- --exact --test-threads=1
    cargo test -p codex-thread-store live_thread_tests::persist_waits_for_append_observation_before_flushing_pending_metadata --lib -- --exact --test-threads=1

# Fast smoke checks for fragile codex-core integration buckets that still fit

# one bounded runtime shard.
core-runtime-surface-smoke:
    RUST_MIN_STACK="${RUST_MIN_STACK:-{{ rust_min_stack }}}" CODEX_JS_REPL_NODE_PATH="${CODEX_JS_REPL_NODE_PATH:-/tmp/codex-node22/bin/node}" cargo nextest run -p codex-core --no-fail-fast --test all -- suite::rmcp_client::stdio_server_round_trip suite::plugins::plugin_mcp_tools_are_listed suite::truncation::mcp_tool_call_output_exceeds_limit_truncated_for_model suite::client::usage_limit_error_emits_rate_limit_event suite::client_websockets::responses_websocket_usage_limit_error_emits_rate_limit_event suite::realtime_conversation::conversation_flushes_assistant_deltas_every_200ms_for_v3_handoff suite::guardian_review::guardian_session_prewarms_and_is_reused_for_first_review suite::responses_lite::responses_lite_exposes_standalone_web_search_for_opted_in_custom_provider suite::responses_lite::responses_lite_does_not_expose_standalone_web_search_for_custom_provider_by_default suite::responses_lite::responses_lite_does_not_expose_disabled_standalone_web_search_for_opted_in_provider suite::responses_lite::responses_lite_does_not_expose_standalone_web_search_for_opted_in_bedrock_provider --exact
    RUST_MIN_STACK="${RUST_MIN_STACK:-{{ rust_min_stack }}}" CODEX_JS_REPL_NODE_PATH="${CODEX_JS_REPL_NODE_PATH:-/tmp/codex-node22/bin/node}" cargo nextest run -p codex-core --no-fail-fast --test all -- suite::rmcp_client::interrupt_during_mcp_startup_preserves_user_input_in_history
    RUST_MIN_STACK="${RUST_MIN_STACK:-{{ rust_min_stack }}}" CODEX_JS_REPL_NODE_PATH="${CODEX_JS_REPL_NODE_PATH:-/tmp/codex-node22/bin/node}" cargo nextest run -p codex-core --no-fail-fast --lib -- session::tests::build_initial_context_includes_turn_context_fragments_from_extensions session::tests::record_context_updates_includes_turn_context_fragments_on_steady_state_turns session::turn_tests::post_sampling_token_estimate_is_disabled_by_always_on_sinks guardian::review_session::tests::guardian_review_session_config_change_invalidates_cached_session guardian::tests::guardian_review_session_config_clears_context_overrides_for_distinct_effective_model guardian::tests::guardian_review_session_config_preserves_context_overrides_for_same_effective_model realtime_conversation::bem_tests::maps_bem_channels_to_realtime_phases realtime_conversation::bem_tests::client_prefixes_override_only_their_configured_channels realtime_conversation::bem_tests::empty_client_prefixes_do_not_match_every_message realtime_conversation::bem_tests::buffers_streamed_text_until_the_bem_channel_is_complete realtime_conversation::bem_tests::buffers_a_client_prefix_until_the_streamed_header_is_complete realtime_conversation::bem_tests::preserves_unrecognized_output_when_the_stream_finishes --exact
    cargo test --locked -p codex-git-attribution --lib -- --test-threads=1
    cargo test -p codex-core-skills preferred_user_skill_names_from_stack_collects_user_and_session_layers --lib -- --exact --test-threads=1
    cargo test -p codex-core-skills finalize_skill_outcome_disables_repo_skill_when_user_preference_is_configured --lib -- --exact --test-threads=1
    cargo test -p codex-core parses_prefer_user_skill_names --lib -- --exact --test-threads=1
    cargo test -p codex-core tools::runtimes::shell::tests::approval_key_uses_path_uri_and_includes_environment_id --lib -- --exact --test-threads=1
    cargo test -p codex-core tools::runtimes::disable_powershell_profile_tests::inserts_no_profile_for_proxy_selected_elevated_windows_sandbox --lib -- --exact --test-threads=1

# Focused skill-loader hermeticity and skill-catalog budget slice.
skill-loader-fixture-hermeticity-targeted:
    RUST_MIN_STACK="${RUST_MIN_STACK:-{{ rust_min_stack }}}" cargo nextest run -p codex-core-skills --lib --no-tests=fail -- loader::tests::non_git_repo_skills_search_does_not_walk_parents loader::tests::skill_roots_include_admin_with_lowest_priority --exact
    RUST_MIN_STACK="${RUST_MIN_STACK:-{{ rust_min_stack }}}" cargo nextest run -p codex-skills-extension --lib --no-tests=fail -- provider::host::tests::host_catalog_entries_carry_their_prompt_scope render::tests::ordering_follows_render_policy render::tests::description_selection_follows_render_policy render::tests::omission_notice_follows_render_policy_and_is_charged_to_catalog_budget render::tests::character_fallback_counts_multibyte_metadata_by_characters render::tests::catalog_report_counts_partial_description_truncation render::tests::catalog_emits_omission_marker_when_every_minimum_skill_line_exceeds_budget render::tests::catalog_preserves_report_when_no_fragment_fits_budget --exact
    RUST_MIN_STACK="${RUST_MIN_STACK:-{{ rust_min_stack }}}" cargo nextest run -p codex-skills-extension --test skills_extension --no-tests=fail -- moderate_budget_pressure_keeps_every_catalog_entry extreme_budget_pressure_removes_descriptions_before_omitting_entries --exact

# Focused persisted-state/usage lineage contract slice for subagent graph adoption.
core-state-spawn-lineage-contract-targeted:
    cargo test -p codex-state usage_spawn_lineage_matches_persisted_state_edge_for_child_thread -- --test-threads=1

# Cross-repo ledger seam validation (agent-usage-ledger + Postgres).
[no-cd]
downstream-ledger-seam:
    ledger_repo_root="${LEDGER_REPO_ROOT:-../agent-usage-ledger}"; \
    ledger_scripts_dir="$ledger_repo_root/scripts/llm_usage"; \
    if [ ! -d "$ledger_repo_root" ]; then \
      echo "Skipping downstream-ledger-seam: missing ledger repo at $ledger_repo_root"; \
      exit 0; \
    fi; \
    if ! command -v psql >/dev/null 2>&1; then \
      echo "Skipping downstream-ledger-seam: missing psql"; \
      exit 0; \
    fi; \
    for required_script in \
      "$ledger_scripts_dir/ensure_schema.sh" \
      "$ledger_scripts_dir/ingest_codex_rollouts_to_postgres.sh" \
      "$ledger_scripts_dir/test_codex_copied_history_filter.sh" \
      "$ledger_scripts_dir/test_codex_source_row_identity.sh"; do \
      if [ ! -x "$required_script" ]; then \
        echo "Skipping downstream-ledger-seam: missing ledger helper $required_script"; \
        exit 0; \
      fi; \
    done; \
    "$ledger_scripts_dir/ensure_schema.sh" --schema "${LLM_USAGE_DB_SCHEMA:-llm_usage}"; \
    "$ledger_scripts_dir/ingest_codex_rollouts_to_postgres.sh" --schema "${LLM_USAGE_DB_SCHEMA:-llm_usage}" --skip-schema; \
    "$ledger_scripts_dir/test_codex_copied_history_filter.sh"; \
    "$ledger_scripts_dir/test_codex_source_row_identity.sh"

[no-cd]
downstream-docs-check:
    git diff --check -- docs/downstream.md docs/native-computer-use.md docs/native-computer-use-contracts.md docs/carry-divergence-ledger.md docs/downstream-regression-matrix.md docs/downstream-tool-surface-matrix.md docs/divergences/index.yaml
    cd "{{ justfile_directory() }}" && python3 -m json.tool docs/divergences/index.yaml >/dev/null
    cd "{{ justfile_directory() }}" && python3 .github/scripts/check_markdown_links.py

[no-cd]
workflow-ci-sanity:
    cd "{{ justfile_directory() }}" && python3 -m py_compile .github/scripts/aggregate_validation_summary.py .github/scripts/check_markdown_links.py .github/scripts/resolve_rust_ci_mode.py .github/scripts/resolve_sedna_release_version.py .github/scripts/resolve_validation_plan.py .github/scripts/test_ci_planners.py scripts/downstream-divergence-audit.py
    cd "{{ justfile_directory() }}" && python3 -m unittest discover -s .github/scripts -p 'test_ci_planners.py'
    cd "{{ justfile_directory() }}" && ruby -e 'require "yaml"; %w[.github/workflows/_sedna-linux-rust.yml .github/workflows/codeql.yml .github/workflows/docs-sanity.yml .github/workflows/rust-ci-full.yml .github/workflows/rust-ci.yml .github/workflows/sedna-heavy-tests.yml .github/workflows/sedna-release.yml .github/workflows/validation-lab.yml].each { |path| YAML.load_file(path) }; puts "yaml-ok"'

[no-cd]
downstream-divergence-audit:
    cd "{{ justfile_directory() }}" && python3 scripts/downstream-divergence-audit.py --repo . --downstream-remote origin --downstream-branch main --mirror-remote origin --mirror-branch upstream-main --upstream-remote upstream --upstream-branch main --registry-path docs/divergences/index.yaml --output-dir target/downstream-divergence-audit --format both --code-only --enforce-registry

# Early non-publishing Linux release-build smoke coverage.
sedna-release-linux-smoke:
    CODEX_RELEASE_VERSION="${CODEX_RELEASE_VERSION:-0.0.0-sedna.smoke}" cargo build --locked --target x86_64-unknown-linux-gnu --release --bin codex --bin codex-responses-api-proxy

# Fast smoke checks for fragile codex-core integration buckets.
core-test-smoke:
    just core-compile-smoke
    just core-carry-core-smoke
    just core-carry-ui-smoke
    just core-ledger-smoke
    just core-runtime-surface-smoke

# Progressive codex-core ladder:

# 1) smoke gate, 2) high-churn buckets, 3) full suite.
core-test-progressive:
    just core-test-smoke
    CODEX_JS_REPL_NODE_PATH="${CODEX_JS_REPL_NODE_PATH:-/tmp/codex-node22/bin/node}" cargo nextest run -p codex-core --no-fail-fast --test all -- suite::rmcp_client::
    CODEX_JS_REPL_NODE_PATH="${CODEX_JS_REPL_NODE_PATH:-/tmp/codex-node22/bin/node}" cargo nextest run -p codex-core --no-fail-fast --test all -- suite::code_mode::
    CODEX_JS_REPL_NODE_PATH="${CODEX_JS_REPL_NODE_PATH:-/tmp/codex-node22/bin/node}" cargo nextest run -p codex-core --no-fail-fast --test all -- suite::truncation::
    CODEX_JS_REPL_NODE_PATH="${CODEX_JS_REPL_NODE_PATH:-/tmp/codex-node22/bin/node}" cargo nextest run -p codex-core --no-fail-fast --test all -- suite::plugins::
    CODEX_JS_REPL_NODE_PATH="${CODEX_JS_REPL_NODE_PATH:-/tmp/codex-node22/bin/node}" cargo nextest run -p codex-core --no-fail-fast

# Run Bazel-backed end-to-end macrobenchmarks with optimized binaries.
bench-e2e:
    # Keep measured binaries comparable to production-style optimized builds.
    bazel test --compilation_mode=opt --cache_test_results=no --test_output=streamed //codex-rs:e2e-benchmarks

# Run Bazel-backed end-to-end macrobenchmarks once per case with release-like

# Rust cfg paths but fastbuild codegen.
bench-e2e-smoke:
    # Avoid optimizer cost because smoke runs only check that benchmarks work.
    # Compile target Rust code through the same release-only cfg paths as opt.
    # Compile exec-platform Rust tools through those release-only cfg paths too.
    bazel test --compilation_mode=fastbuild --@rules_rust//rust/settings:extra_rustc_flag=-Cdebug-assertions=no --@rules_rust//rust/settings:extra_exec_rustc_flag=-Cdebug-assertions=no --cache_test_results=no --test_output=streamed --test_arg=--test //codex-rs:e2e-benchmarks

# Build and run Codex from source using Bazel.
# On Unix, use `[no-cd]` and `--run_under="cd $PWD &&"` to ensure Bazel runs

# the command in the current working directory.
[no-cd]
[unix]
bazel-codex *args:
    bazel run //codex-rs/cli:codex --run_under="cd $PWD &&" -- "$@"

[windows]
bazel-codex *args:
    bazel run //codex-rs/cli:codex --run_under='cd /d "{{ invocation_directory_native() }}" &&' -- @($args | Select-Object -Skip 1)

# Build and run the standalone code-mode host from source using Bazel.
[no-cd]
[unix]
bazel-code-mode-host *args:
    bazel run //codex-rs/code-mode-host:codex-code-mode-host --run_under="cd $PWD &&" -- "$@"

[windows]
bazel-code-mode-host *args:
    bazel run //codex-rs/code-mode-host:codex-code-mode-host --run_under='cd /d "{{ invocation_directory_native() }}" &&' -- @($args | Select-Object -Skip 1)

[no-cd]
bazel-lock-update:
    bazel mod deps --lockfile_mode=update

[no-cd]
[unix]
bazel-lock-check:
    {{ justfile_directory() }}/scripts/check-module-bazel-lock.sh

[windows]
bazel-lock-check:
    bazel mod deps --lockfile_mode=error; if ($LASTEXITCODE -ne 0) { Write-Error "MODULE.bazel.lock is out of date. Run 'just bazel-lock-update' and commit the updated lockfile."; exit 1 }

bazel-test:
    bazel test --test_tag_filters=-argument-comment-lint //... --keep_going

[no-cd]
[unix]
bazel-clippy:
    bazel_targets="$({{ justfile_directory() }}/scripts/list-bazel-clippy-targets.sh)" && bazel build --config=clippy -- ${bazel_targets}

[no-cd]
[unix]
bazel-argument-comment-lint:
    bazel build --config=argument-comment-lint -- $({{ justfile_directory() }}/tools/argument-comment-lint/list-bazel-targets.sh)

build-for-release:
    bazel build //codex-rs/cli:release_binaries

# Run the MCP server
mcp-server-run *args:
    cargo run -p codex-mcp-server -- {args}

# Regenerate the json schema for config.toml from the current config types.
write-config-schema:
    cargo run -p codex-core --bin codex-write-config-schema

# Regenerate vendored app-server protocol schema artifacts.
write-app-server-schema *args:
    cargo run -p codex-app-server-protocol --bin write_schema_fixtures -- {args}

[no-cd]
write-hooks-schema:
    cargo run --manifest-path {{ justfile_directory() }}/codex-rs/Cargo.toml -p codex-hooks --bin write_hooks_schema_fixtures

# Run the argument-comment Dylint checks across codex-rs.
[no-cd]
[unix]
_run-bazel-argument-comment-lint:
    cd "{{ justfile_directory() }}" && bazel build --config=argument-comment-lint -- $("{{ justfile_directory() }}"/tools/argument-comment-lint/list-bazel-targets.sh)

[no-cd]
[unix]
argument-comment-lint *args:
    if [ "$#" -eq 0 ]; then \
      bazel build --config=argument-comment-lint -- $({{ justfile_directory() }}/tools/argument-comment-lint/list-bazel-targets.sh); \
    else \
      {{ justfile_directory() }}/tools/argument-comment-lint/run-prebuilt-linter.py "$@"; \
    fi

[no-cd]
argument-comment-lint-from-source *args:
    {{ python }} {{ justfile_directory() }}/tools/argument-comment-lint/run.py {args}

# Tail logs from the state SQLite database
[unix]
log *args:
    if [ "${1:-}" = "--" ]; then shift; fi; cargo run -p codex-state --bin logs_client -- "$@"

[windows]
log *args:
    $forwarded_args = @($args | Select-Object -Skip 1); if ($forwarded_args.Count -gt 0 -and $forwarded_args[0] -eq "--") { $forwarded_args = @($forwarded_args | Select-Object -Skip 1) }; cargo run -p codex-state --bin logs_client -- @forwarded_args
