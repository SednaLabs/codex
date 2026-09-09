#![cfg(not(debug_assertions))]

use crate::legacy_core::config::Config;
use crate::update_action;
use crate::update_action::UpdateAction;
use crate::update_versions::extract_version_from_latest_tag;
use crate::update_versions::is_sedna_release_version;
use crate::update_versions::is_source_build_version;
use crate::updates_cache::VersionInfo;
use crate::updates_cache::read_version_info;
use crate::updates_cache::version_filepath;
use chrono::Duration;
use chrono::Utc;
use codex_login::default_client::create_client;
use serde::Deserialize;
use std::path::Path;

use crate::version::CODEX_CLI_VERSION;
use crate::version::CODEX_RELEASE_REPOSITORY;

pub(crate) use crate::updates_cache::dismiss_version;

pub fn get_upgrade_version(config: &Config) -> Option<String> {
    if !config.check_for_update_on_startup
        || is_source_build_version(CODEX_CLI_VERSION)
        || !crate::version::is_sedna_release_channel()
        || !codex_utils_version::is_sedna_automatic_update_eligible_for_channel(
            CODEX_CLI_VERSION,
            std::env::consts::OS,
            std::env::consts::ARCH,
            codex_utils_version::SednaReleaseChannel::for_version(CODEX_CLI_VERSION)
                .unwrap_or(codex_utils_version::SednaReleaseChannel::Stable),
        )
    {
        return None;
    }

    let action = update_action::get_update_action()?;
    let version_file = version_filepath(config);
    let info = read_version_info(&version_file).ok();

    if match &info {
        None => true,
        Some(info) => {
            !info.matches_current_channel()
                || !is_sedna_release_version(&info.latest_version)
                || info.last_checked_at < Utc::now() - Duration::hours(20)
        }
    } {
        // Refresh the cached latest version in the background so TUI startup
        // isn’t blocked by a network call. The UI reads the previously cached
        // value (if any) for this run; the next run shows the banner if needed.
        tokio::spawn(async move {
            check_for_update(&version_file, Some(action))
                .await
                .inspect_err(|e| tracing::error!("Failed to update version: {e}"))
        });
    }

    info.and_then(|info| {
        info.actionable_latest_version(CODEX_CLI_VERSION)
            .map(str::to_owned)
    })
}

#[derive(Deserialize, Debug, Clone)]
struct ReleaseInfo {
    tag_name: String,
    #[serde(default)]
    prerelease: bool,
    #[serde(default)]
    draft: bool,
    #[serde(default)]
    release_channel: Option<codex_utils_version::SednaReleaseChannel>,
}

async fn check_for_update(version_file: &Path, action: Option<UpdateAction>) -> anyhow::Result<()> {
    if !crate::version::is_sedna_release_channel()
        || !codex_utils_version::is_sedna_automatic_update_eligible_for_channel(
            CODEX_CLI_VERSION,
            std::env::consts::OS,
            std::env::consts::ARCH,
            codex_utils_version::SednaReleaseChannel::for_version(CODEX_CLI_VERSION)
                .unwrap_or(codex_utils_version::SednaReleaseChannel::Stable),
        )
        || action.is_none()
    {
        return Ok(());
    }
    let latest_version = fetch_latest_github_release_version().await?;

    // Preserve a dismissal only when it belongs to this release channel.
    let prev_info = read_version_info(version_file).ok();
    let info = VersionInfo::for_current_channel(
        latest_version,
        Utc::now(),
        prev_info.and_then(|info| info.dismissed_version_for_current_channel()),
    );

    let json_line = format!("{}\n", serde_json::to_string(&info)?);
    if let Some(parent) = version_file.parent() {
        tokio::fs::create_dir_all(parent).await?;
    }
    tokio::fs::write(version_file, json_line).await?;
    Ok(())
}

async fn fetch_latest_github_release_version() -> anyhow::Result<String> {
    let releases_url = format!(
        "https://api.github.com/repos/{}/releases?per_page=100",
        CODEX_RELEASE_REPOSITORY
    );
    let releases = create_client()
        .get(releases_url)
        .send()
        .await?
        .error_for_status()?
        .await?
        .json::<Vec<ReleaseInfo>>()
        .await?;
    let channel = codex_utils_version::SednaReleaseChannel::for_version(CODEX_CLI_VERSION)
        .ok_or_else(|| anyhow::anyhow!("current version is not a Sedna release"))?;
    releases
        .into_iter()
        .filter(|release| !release.draft)
        .filter_map(|release| {
            let version = extract_version_from_latest_tag(&release.tag_name).ok()?;
            let derived = codex_utils_version::SednaReleaseChannel::for_version(&version)?;
            (release.release_channel.is_none() || release.release_channel == Some(derived))
                .then_some((version, release.prerelease, derived))
        })
        .filter(|(_, api_prerelease, derived)| {
            // A contradictory API flag/metadata pair is rejected, never guessed.
            *api_prerelease == (*derived == codex_utils_version::SednaReleaseChannel::Prerelease)
        })
        .filter(|(_, _, derived)| {
            channel == codex_utils_version::SednaReleaseChannel::Prerelease
                || *derived == codex_utils_version::SednaReleaseChannel::Stable
        })
        .max_by(|(left, _, _), (right, _, _)| {
            codex_utils_version::is_newer_sedna_release(left, right)
                .map(|newer| {
                    if newer {
                        std::cmp::Ordering::Greater
                    } else {
                        std::cmp::Ordering::Less
                    }
                })
                .unwrap_or(std::cmp::Ordering::Equal)
        })
        .map(|(version, _, _)| version)
        .ok_or_else(|| {
            anyhow::anyhow!("no valid published Sedna release matches the selected channel")
        })
}

/// Returns the latest version to show in a popup, if it should be shown.
/// This respects the user's dismissal choice for the current latest version.
pub fn get_upgrade_version_for_popup(config: &Config) -> Option<String> {
    if !config.check_for_update_on_startup || is_source_build_version(CODEX_CLI_VERSION) {
        return None;
    }

    let version_file = version_filepath(config);
    let latest = get_upgrade_version(config)?;
    // If the user dismissed this exact version previously, do not show the popup.
    if let Ok(info) = read_version_info(&version_file)
        && info.matches_current_channel()
        && info.dismissed_version.as_deref() == Some(latest.as_str())
    {
        return None;
    }
    Some(latest)
}
