use super::*;
use crate::legacy_core::config::ConfigBuilder;
use codex_utils_version::SednaReleaseChannel;
use pretty_assertions::assert_eq;
use tempfile::tempdir;

#[tokio::test]
async fn dismiss_version_creates_cache_file_when_missing() {
    let codex_home = tempdir().expect("temp codex home");
    let config = ConfigBuilder::default()
        .codex_home(codex_home.path().to_path_buf())
        .build()
        .await
        .expect("load config");
    let version_file = version_filepath(&config);

    dismiss_version(&config, "999.0.0")
        .await
        .expect("dismiss version");

    let info = read_version_info(&version_file).expect("read version info");
    assert_eq!(info.last_checked_at, DateTime::<Utc>::UNIX_EPOCH);
    assert_eq!(
        (
            info.latest_version.as_str(),
            info.dismissed_version.as_deref()
        ),
        ("999.0.0", Some("999.0.0"))
    );
}

#[tokio::test]
async fn dismiss_version_replaces_a_mismatched_cache() {
    let codex_home = tempdir().expect("temp codex home");
    let config = ConfigBuilder::default()
        .codex_home(codex_home.path().to_path_buf())
        .build()
        .await
        .expect("load config");
    let version_file = version_filepath(&config);
    let upstream_cache = VersionInfo {
        latest_version: "999.0.0".to_string(),
        last_checked_at: DateTime::<Utc>::UNIX_EPOCH,
        dismissed_version: Some("999.0.0".to_string()),
        release_repository: Some("openai/codex".to_string()),
        release_tag_prefix: Some("rust-v".to_string()),
        release_channel: None,
    };
    std::fs::write(
        &version_file,
        format!(
            "{}\n",
            serde_json::to_string(&upstream_cache).expect("serialize cache")
        ),
    )
    .expect("write cache");

    dismiss_version(&config, "999.0.0-sedna.2")
        .await
        .expect("dismiss version");

    let info = read_version_info(&version_file).expect("read version info");
    assert!(info.matches_current_channel(SednaReleaseChannel::Stable));
    assert_eq!(info.latest_version, "999.0.0-sedna.2");
    assert_eq!(info.dismissed_version.as_deref(), Some("999.0.0-sedna.2"));
}

#[test]
fn matching_channel_cache_preserves_its_dismissal() {
    let info = VersionInfo::for_current_channel(
        "999.0.0-sedna.2".to_string(),
        DateTime::<Utc>::UNIX_EPOCH,
        Some("999.0.0-sedna.2".to_string()),
        SednaReleaseChannel::Stable,
    );

    assert_eq!(
        info.dismissed_version_for_current_channel(SednaReleaseChannel::Stable)
            .as_deref(),
        Some("999.0.0-sedna.2")
    );
}

#[test]
fn mismatched_channel_cache_does_not_preserve_its_dismissal() {
    let info = VersionInfo {
        latest_version: "999.0.0".to_string(),
        last_checked_at: DateTime::<Utc>::UNIX_EPOCH,
        dismissed_version: Some("999.0.0".to_string()),
        release_repository: Some("openai/codex".to_string()),
        release_tag_prefix: Some("rust-v".to_string()),
        release_channel: None,
    };

    assert_eq!(
        info.dismissed_version_for_current_channel(SednaReleaseChannel::Stable),
        None
    );
}

#[test]
fn legacy_source_less_cache_does_not_preserve_its_dismissal() {
    let info = VersionInfo {
        latest_version: "999.0.0".to_string(),
        last_checked_at: DateTime::<Utc>::UNIX_EPOCH,
        dismissed_version: Some("999.0.0".to_string()),
        release_repository: None,
        release_tag_prefix: None,
        release_channel: None,
    };

    assert_eq!(
        info.dismissed_version_for_current_channel(SednaReleaseChannel::Stable),
        None
    );
}

#[test]
fn current_identity_cache_rejects_non_sedna_latest_versions() {
    for cached_version in ["999.0.0", "999.0.0+upstream.4", "999.0.0-sedna.x"] {
        let info = VersionInfo::for_current_channel(
            cached_version.to_string(),
            DateTime::<Utc>::UNIX_EPOCH,
            /*dismissed_version*/ None,
            SednaReleaseChannel::Stable,
        );

        assert_eq!(
            info.actionable_latest_version("998.0.0-sedna.1", SednaReleaseChannel::Stable,),
            None,
            "accepted cached version {cached_version}"
        );
    }
}
