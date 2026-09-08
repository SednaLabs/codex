use std::sync::Mutex;

use pretty_assertions::assert_eq;

use super::InstallerHttp;
use super::InstallerResponse;
use super::SEDNA_STANDALONE_INSTALLER_URL;
use super::fetch_installer_script_from_url;
#[cfg(unix)]
use super::install_latest_sedna_standalone;
use super::post_install_release_is_strictly_newer;
use super::update_modes_for_identities;
use crate::RestartIfRunningOutcome;
use crate::RestartMode;
use crate::UpdaterRefreshMode;
use crate::managed_install::executable_identity_from_bytes;
use crate::should_reexec_updater;

#[test]
fn unchanged_updater_uses_version_based_restart() {
    assert_eq!(
        update_modes_for_identities(
            &executable_identity_from_bytes(b"same"),
            &executable_identity_from_bytes(b"same"),
        ),
        (RestartMode::IfVersionChanged, UpdaterRefreshMode::None)
    );
}

#[test]
fn changed_updater_forces_refresh_even_when_version_may_match() {
    assert_eq!(
        update_modes_for_identities(
            &executable_identity_from_bytes(b"old"),
            &executable_identity_from_bytes(b"new"),
        ),
        (
            RestartMode::Always,
            UpdaterRefreshMode::ReexecIfManagedBinaryChanged,
        )
    );
}

#[test]
fn out_of_band_activation_converges_a_processes_to_current_b_before_an_equal_update() {
    let (restart_mode, updater_refresh_mode) = update_modes_for_identities(
        &executable_identity_from_bytes(b"updater-a"),
        &executable_identity_from_bytes(b"current-b"),
    );

    assert_eq!(restart_mode, RestartMode::Always);
    assert_eq!(
        updater_refresh_mode,
        UpdaterRefreshMode::ReexecIfManagedBinaryChanged
    );
    assert!(should_reexec_updater(
        updater_refresh_mode,
        RestartIfRunningOutcome::Restarted
    ));
}

#[test]
fn self_installed_current_b_reexecs_updater_when_app_server_is_not_running() {
    let (restart_mode, updater_refresh_mode) = update_modes_for_identities(
        &executable_identity_from_bytes(b"updater-a"),
        &executable_identity_from_bytes(b"current-b"),
    );

    assert_eq!(restart_mode, RestartMode::Always);
    assert!(should_reexec_updater(
        updater_refresh_mode,
        RestartIfRunningOutcome::NotRunning
    ));
}

#[test]
fn post_install_release_must_be_strictly_newer_before_restart_or_reexec() {
    let installed_from = "1.2.3-sedna.1";
    assert_eq!(
        [
            post_install_release_is_strictly_newer(installed_from, "1.2.2-sedna.1"),
            post_install_release_is_strictly_newer(installed_from, "1.2.3-sedna.1"),
            post_install_release_is_strictly_newer(installed_from, "1.2.4-sedna.1"),
            post_install_release_is_strictly_newer(installed_from, "not-a-sedna-release"),
        ],
        [false, false, true, false]
    );
}

#[test]
fn post_install_ineligible_release_still_reconciles_running_processes() {
    assert!(!post_install_release_is_strictly_newer(
        "1.2.3-sedna.1",
        "not-a-sedna-release"
    ));
    assert_eq!(
        update_modes_for_identities(
            &executable_identity_from_bytes(b"updater-a"),
            &executable_identity_from_bytes(b"current-b"),
        ),
        (
            RestartMode::Always,
            UpdaterRefreshMode::ReexecIfManagedBinaryChanged,
        )
    );
}

#[test]
fn post_install_non_newer_release_still_reconciles_before_reporting_failure() {
    assert!(!post_install_release_is_strictly_newer(
        "1.2.3-sedna.1",
        "1.2.3-sedna.1"
    ));
    assert_eq!(
        update_modes_for_identities(
            &executable_identity_from_bytes(b"updater-a"),
            &executable_identity_from_bytes(b"current-b"),
        ),
        (
            RestartMode::Always,
            UpdaterRefreshMode::ReexecIfManagedBinaryChanged,
        )
    );
}

#[tokio::test]
async fn sedna_installer_fetch_uses_exact_url_and_preserves_bytes() {
    let script = b"#!/bin/bash\nprintf 'sedna update bytes'\n".to_vec();
    let http = FakeInstallerHttp::new(InstallerResponse::Success(script.clone()));

    assert_eq!(
        fetch_installer_script_from_url(&http, SEDNA_STANDALONE_INSTALLER_URL)
            .await
            .expect("Sedna installer fetch should succeed"),
        script
    );
    assert_eq!(
        http.requested_urls(),
        vec![SEDNA_STANDALONE_INSTALLER_URL.to_string()]
    );
}

#[cfg(unix)]
#[tokio::test]
async fn sedna_updater_requires_a_newer_stable_release() {
    let script = br#"#!/usr/bin/env bash
set -euo pipefail
expected=(--repository sednalabs/codex --release-tag latest --require-newer-than 1.2.3-sedna.4)
[[ "$#" -eq "${#expected[@]}" ]]
for expected_arg in "${expected[@]}"; do
  [[ "$1" == "$expected_arg" ]]
  shift
done
printf 'fixture updater diagnostic\n' >&2
"#
    .to_vec();
    let http = FakeInstallerHttp::new(InstallerResponse::Success(script));

    install_latest_sedna_standalone(&http, "1.2.3-sedna.4")
        .await
        .expect("Sedna updater should require a newer stable release");
    assert_eq!(
        http.requested_urls(),
        vec![SEDNA_STANDALONE_INSTALLER_URL.to_string()]
    );
}

struct FakeInstallerHttp {
    response: InstallerResponse,
    requested_urls: Mutex<Vec<String>>,
}

impl FakeInstallerHttp {
    fn new(response: InstallerResponse) -> Self {
        Self {
            response,
            requested_urls: Mutex::new(Vec::new()),
        }
    }

    fn requested_urls(&self) -> Vec<String> {
        self.requested_urls
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .clone()
    }
}

impl InstallerHttp for FakeInstallerHttp {
    async fn get(&self, url: &str) -> anyhow::Result<InstallerResponse> {
        self.requested_urls
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .push(url.to_string());
        Ok(self.response.clone())
    }
}
