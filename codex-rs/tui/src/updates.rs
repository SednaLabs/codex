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
            config.sedna_release_channel,
        )
    {
        return None;
    }

    let action = update_action::get_update_action()?;
    let version_file = version_filepath(config);
    let channel = config.sedna_release_channel;
    let info = read_version_info(&version_file).ok();

    if match &info {
        None => true,
        Some(info) => {
            !info.matches_current_channel(channel)
                || !is_sedna_release_version(&info.latest_version)
                || info.last_checked_at < Utc::now() - Duration::hours(20)
        }
    } {
        // Refresh the cached latest version in the background so TUI startup
        // isn’t blocked by a network call. The UI reads the previously cached
        // value (if any) for this run; the next run shows the banner if needed.
        tokio::spawn(async move {
            check_for_update(&version_file, Some(action), channel)
                .await
                .inspect_err(|e| tracing::error!("Failed to update version: {e}"))
        });
    }

    info.and_then(|info| {
        info.actionable_latest_version(CODEX_CLI_VERSION, channel)
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
    assets: Vec<ReleaseAsset>,
}

#[derive(Deserialize, Debug, Clone)]
struct ReleaseAsset {
    name: String,
    browser_download_url: String,
}

#[derive(Deserialize)]
struct ReleaseMetadata {
    release_tag: String,
    release_version: String,
    repository: String,
    target: String,
    #[serde(default)]
    release_channel: Option<codex_utils_version::SednaReleaseChannel>,
}

async fn check_for_update(
    version_file: &Path,
    action: Option<UpdateAction>,
    channel: codex_utils_version::SednaReleaseChannel,
) -> anyhow::Result<()> {
    if !crate::version::is_sedna_release_channel()
        || !codex_utils_version::is_sedna_automatic_update_eligible_for_channel(
            CODEX_CLI_VERSION,
            std::env::consts::OS,
            std::env::consts::ARCH,
            channel,
        )
        || action.is_none()
    {
        return Ok(());
    }
    let latest_version = fetch_latest_github_release_version(channel).await?;

    // Preserve a dismissal only when it belongs to this release channel.
    let prev_info = read_version_info(version_file).ok();
    let info = VersionInfo::for_current_channel(
        latest_version,
        Utc::now(),
        prev_info.and_then(|info| info.dismissed_version_for_current_channel(channel)),
        channel,
    );

    let json_line = format!("{}\n", serde_json::to_string(&info)?);
    if let Some(parent) = version_file.parent() {
        tokio::fs::create_dir_all(parent).await?;
    }
    tokio::fs::write(version_file, json_line).await?;
    Ok(())
}

async fn fetch_latest_github_release_version(
    channel: codex_utils_version::SednaReleaseChannel,
) -> anyhow::Result<String> {
    let releases_url = format!(
        "https://api.github.com/repos/{}/releases?per_page=100",
        CODEX_RELEASE_REPOSITORY
    );
    let releases = create_client()
        .get(releases_url)
        .send()
        .await?
        .error_for_status()?
        .json::<Vec<ReleaseInfo>>()
        .await?;
    let mut candidates = releases
        .into_iter()
        .filter(|release| !release.draft)
        .filter(|release| channel.allows_api_prerelease(release.prerelease))
        .filter_map(|release| {
            let version = extract_version_from_latest_tag(&release.tag_name).ok()?;
            Some((release, version))
        })
        .collect::<Vec<_>>();
    candidates.sort_by(|(_, left), (_, right)| {
        codex_utils_version::is_newer_sedna_release(left, right)
            .map(|newer| {
                if newer {
                    std::cmp::Ordering::Greater
                } else {
                    std::cmp::Ordering::Less
                }
            })
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    for (release, version) in candidates.into_iter().rev() {
        if release_metadata_is_valid(&release, &version).await? {
            return Ok(version);
        }
    }
    anyhow::bail!("no valid published Sedna release matches the selected channel")
}

async fn release_metadata_is_valid(release: &ReleaseInfo, version: &str) -> anyhow::Result<bool> {
    let target = match (std::env::consts::OS, std::env::consts::ARCH) {
        ("linux", "x86_64") => "x86_64-unknown-linux-gnu",
        ("linux", "aarch64") => "aarch64-unknown-linux-gnu",
        _ => return Ok(false),
    };
    let metadata_name = if target == "x86_64-unknown-linux-gnu" {
        "RELEASE-METADATA.json".to_string()
    } else {
        format!("RELEASE-METADATA-{target}.json")
    };
    let Some(asset) = release
        .assets
        .iter()
        .find(|asset| asset.name == metadata_name)
    else {
        return Ok(false);
    };
    let metadata = create_client()
        .get(&asset.browser_download_url)
        .send()
        .await?
        .error_for_status()?
        .json::<ReleaseMetadata>()
        .await?;
    let api_channel = if release.prerelease {
        codex_utils_version::SednaReleaseChannel::Prerelease
    } else {
        codex_utils_version::SednaReleaseChannel::Stable
    };
    Ok(metadata.release_tag == release.tag_name
        && metadata.release_version == version
        && metadata.repository == CODEX_RELEASE_REPOSITORY
        && metadata.target == target
        // Legacy metadata did not carry a channel. Its API flag remains the
        // authority; when the field exists, disagreement is a hard rejection.
        && metadata.release_channel.is_none_or(|candidate| candidate == api_channel))
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
        && info.matches_current_channel(config.sedna_release_channel)
        && info.dismissed_version.as_deref() == Some(latest.as_str())
    {
        return None;
    }
    Some(latest)
}
