use crate::legacy_core::config::Config;
use crate::update_versions::is_actionable_sedna_update;
use crate::version::CODEX_RELEASE_REPOSITORY;
use crate::version::CODEX_RELEASE_TAG_PREFIX;
use chrono::DateTime;
use chrono::Utc;
use codex_utils_version::SednaReleaseChannel;
use serde::Deserialize;
use serde::Serialize;
use std::path::Path;
use std::path::PathBuf;

#[derive(Serialize, Deserialize, Debug, Clone)]
pub(crate) struct VersionInfo {
    pub(crate) latest_version: String,
    // ISO-8601 timestamp (RFC3339)
    pub(crate) last_checked_at: DateTime<Utc>,
    #[serde(default)]
    pub(crate) dismissed_version: Option<String>,
    /// Legacy records have no identity and therefore fail closed.
    #[serde(default)]
    pub(crate) release_repository: Option<String>,
    #[serde(default)]
    pub(crate) release_tag_prefix: Option<String>,
    /// Cache records without a channel are legacy and fail closed.
    #[serde(default)]
    pub(crate) release_channel: Option<SednaReleaseChannel>,
}

impl VersionInfo {
    pub(crate) fn for_current_channel(
        latest_version: String,
        last_checked_at: DateTime<Utc>,
        dismissed_version: Option<String>,
        release_channel: SednaReleaseChannel,
    ) -> Self {
        Self {
            latest_version,
            last_checked_at,
            dismissed_version,
            release_repository: Some(CODEX_RELEASE_REPOSITORY.to_string()),
            release_tag_prefix: Some(CODEX_RELEASE_TAG_PREFIX.to_string()),
            release_channel: Some(release_channel),
        }
    }

    pub(crate) fn matches_current_channel(&self, release_channel: SednaReleaseChannel) -> bool {
        self.release_repository.as_deref() == Some(CODEX_RELEASE_REPOSITORY)
            && self.release_tag_prefix.as_deref() == Some(CODEX_RELEASE_TAG_PREFIX)
            && self.release_channel == Some(release_channel)
    }

    pub(crate) fn dismissed_version_for_current_channel(
        &self,
        release_channel: SednaReleaseChannel,
    ) -> Option<String> {
        self.matches_current_channel(release_channel)
            .then(|| self.dismissed_version.clone())
            .flatten()
    }

    pub(crate) fn actionable_latest_version(
        &self,
        current_version: &str,
        release_channel: SednaReleaseChannel,
    ) -> Option<&str> {
        (self.matches_current_channel(release_channel)
            && is_actionable_sedna_update(&self.latest_version, current_version))
        .then_some(self.latest_version.as_str())
    }
}

const VERSION_FILENAME: &str = "version.json";

pub(crate) fn version_filepath(config: &Config) -> PathBuf {
    config.codex_home.join(VERSION_FILENAME).into_path_buf()
}

pub(crate) fn read_version_info(version_file: &Path) -> anyhow::Result<VersionInfo> {
    let contents = std::fs::read_to_string(version_file)?;
    Ok(serde_json::from_str(&contents)?)
}

/// Persist a dismissal for the current latest version so we don't show
/// the update popup again for this version.
pub(crate) async fn dismiss_version(config: &Config, version: &str) -> anyhow::Result<()> {
    let version_file = version_filepath(config);
    let mut info = match read_version_info(&version_file) {
        Ok(info) if info.matches_current_channel(config.sedna_release_channel) => info,
        Err(_) => {
            VersionInfo::for_current_channel(
                version.to_string(),
                DateTime::<Utc>::UNIX_EPOCH,
                /*dismissed_version*/ None,
                config.sedna_release_channel,
            )
        }
        Ok(_) => {
            VersionInfo::for_current_channel(
                version.to_string(),
                DateTime::<Utc>::UNIX_EPOCH,
                /*dismissed_version*/ None,
                config.sedna_release_channel,
            )
        }
    };
    info.dismissed_version = Some(version.to_string());
    let json_line = format!("{}\n", serde_json::to_string(&info)?);
    if let Some(parent) = version_file.parent() {
        tokio::fs::create_dir_all(parent).await?;
    }
    tokio::fs::write(version_file, json_line).await?;
    Ok(())
}

#[cfg(test)]
#[path = "updates_cache_tests.rs"]
mod tests;
