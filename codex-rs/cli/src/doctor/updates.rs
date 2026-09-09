//! Diagnoses whether Codex update paths target the running installation.
//!
//! Update diagnostics combine cache metadata with a bounded probe of the Sedna
//! release channel. Other build channels fail closed: doctor must not suggest
//! that an upstream package manager can update a Sedna binary.

use std::path::Path;

use codex_core::config::Config;
use codex_install_context::InstallContext;
use codex_install_context::InstallMethod;
use codex_install_context::StandalonePlatform;
use codex_utils_version::RELEASE_VERSION;
use codex_utils_version::SednaReleaseChannel;
use codex_utils_version::is_newer_sedna_release;
use codex_utils_version::is_sedna_automatic_update_eligible_for_channel;
use codex_utils_version::is_sedna_release_identity;
use codex_utils_version::parse_sedna_release_tag;
use serde::Deserialize;

use super::CheckStatus;
use super::DoctorCheck;
use super::doctor_install_context;
use super::run_command;

const VERSION_FILE_NAME: &str = "version.json";
const LOCALLY_CONSISTENT_SUMMARY: &str = "update configuration is locally consistent";
const NEWER_SEDNA_RELEASE_SUMMARY: &str = "newer Sedna release is available";
const INVALID_SEDNA_VERSION_SUMMARY: &str = "Sedna release versions could not be compared";
const SEDNA_RELEASE_PROBE_WARNING_SUMMARY: &str = "Sedna release probe failed";

/// Builds the update-health row for the current installation.
///
/// Network failures while fetching latest-version metadata degrade the row to a
/// warning instead of failing doctor outright; update freshness is useful
/// support context but should not mask more direct install/config failures.
pub(super) fn updates_check(config: &Config) -> DoctorCheck {
    let install_context = doctor_install_context(std::env::current_exe().ok().as_deref());
    let mut details = vec![
        format!(
            "check for update on startup: {}",
            config.check_for_update_on_startup
        ),
        format!(
            "selected release channel: {}",
            serde_json::to_string(&config.sedna_release_channel)
                .unwrap_or_else(|_| "\"stable\"".to_string())
                .trim_matches('\"')
        ),
        format!(
            "update action: {}",
            update_action_label(&install_context, config.sedna_release_channel)
        ),
    ];
    let version_file = config.codex_home.join(VERSION_FILE_NAME);
    push_cached_version_details(
        &mut details,
        &version_file,
        &install_context,
        config.sedna_release_channel,
    );

    let mut status = CheckStatus::Ok;
    let mut summary = LOCALLY_CONSISTENT_SUMMARY.to_string();
    if is_sedna_automatic_update_probe_available(&install_context, config.sedna_release_channel) {
        match fetch_latest_sedna_release_version(&install_context, config.sedna_release_channel) {
            Ok(latest_version) => {
                details.push(format!("latest Sedna release: {latest_version}"));
                let comparison = is_newer_sedna_release(&latest_version, RELEASE_VERSION);
                let (comparison_status, comparison_summary) =
                    sedna_release_comparison_status(comparison);
                status = status.max(comparison_status);
                summary = comparison_summary.to_string();
                match comparison {
                    Some(true) => {
                        details
                            .push("latest version status: newer version is available".to_string());
                    }
                    Some(false) => {
                        details.push(
                            "latest version status: current version is not older".to_string(),
                        );
                    }
                    None => {
                        details.push(
                            "latest version status: running version is not a valid Sedna release"
                                .to_string(),
                        );
                    }
                }
            }
            Err(err) => {
                let (probe_status, probe_summary) = sedna_release_probe_failure_status();
                status = status.max(probe_status);
                summary = probe_summary.to_string();
                details.push(format!("latest version probe: {err}"));
            }
        }
    } else {
        details
            .push("latest version probe: unavailable for this automatic-update policy".to_string());
    }

    DoctorCheck::new("updates.status", "updates", status, summary).details(details)
}

fn sedna_release_comparison_status(is_newer: Option<bool>) -> (CheckStatus, &'static str) {
    match is_newer {
        Some(true) => (CheckStatus::Ok, NEWER_SEDNA_RELEASE_SUMMARY),
        Some(false) => (CheckStatus::Ok, LOCALLY_CONSISTENT_SUMMARY),
        None => (CheckStatus::Warning, INVALID_SEDNA_VERSION_SUMMARY),
    }
}

const fn sedna_release_probe_failure_status() -> (CheckStatus, &'static str) {
    (CheckStatus::Warning, SEDNA_RELEASE_PROBE_WARNING_SUMMARY)
}

fn push_cached_version_details(
    details: &mut Vec<String>,
    version_file: &Path,
    install_context: &InstallContext,
    release_channel: SednaReleaseChannel,
) {
    details.push(format!("version cache: {}", version_file.display()));
    match std::fs::read_to_string(version_file) {
        Ok(contents) => match serde_json::from_str::<VersionInfo>(&contents) {
            Ok(info) => {
                push_cache_info_details(
                    details,
                    &info,
                    is_sedna_automatic_update_probe_available(install_context, release_channel),
                    release_channel,
                );
            }
            Err(err) => details.push(format!("version cache parse: {err}")),
        },
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => {
            details.push("version cache: missing".to_string());
        }
        Err(err) => details.push(format!("version cache read: {err}")),
    }
}

fn push_cache_info_details(
    details: &mut Vec<String>,
    info: &VersionInfo,
    automatic_update_probe_available: bool,
    release_channel: SednaReleaseChannel,
) {
    if !automatic_update_probe_available {
        return;
    }
    if !info.matches_sedna_release_identity_and_channel(release_channel) {
        details.push("version cache: ignored (untrusted release identity or channel)".to_string());
        return;
    }

    details.push(format!("cached latest version: {}", info.latest_version));
    if let Some(last_checked_at) = &info.last_checked_at {
        details.push(format!("last checked at: {last_checked_at}"));
    }
    if let Some(dismissed_version) = &info.dismissed_version {
        details.push(format!("dismissed version: {dismissed_version}"));
    }
}

fn update_action_label(
    context: &InstallContext,
    release_channel: SednaReleaseChannel,
) -> &'static str {
    update_action_label_for_sedna_identity(
        context,
        is_sedna_release_identity(
            option_env!("CODEX_RELEASE_REPOSITORY"),
            option_env!("CODEX_RELEASE_TAG_PREFIX"),
        ),
        RELEASE_VERSION,
        release_channel,
    )
}

fn update_action_label_for_sedna_identity(
    context: &InstallContext,
    has_sedna_identity: bool,
    release_version: &str,
    release_channel: SednaReleaseChannel,
) -> &'static str {
    update_action_label_for_sedna_identity_on_target(
        context,
        has_sedna_identity,
        release_version,
        std::env::consts::OS,
        std::env::consts::ARCH,
        release_channel,
    )
}

fn update_action_label_for_sedna_identity_on_target(
    context: &InstallContext,
    has_sedna_identity: bool,
    release_version: &str,
    target_os: &str,
    target_arch: &str,
    release_channel: SednaReleaseChannel,
) -> &'static str {
    if !has_sedna_identity {
        return "no automatic update action outside the Sedna release channel";
    }
    if !is_sedna_automatic_update_eligible_for_channel(
        release_version,
        target_os,
        target_arch,
        release_channel,
    ) {
        return "no automatic update action";
    }
    match &context.method {
        InstallMethod::Standalone {
            platform: StandalonePlatform::Unix,
            ..
        } => "Sedna standalone installer",
        InstallMethod::Standalone {
            platform: StandalonePlatform::Windows,
            ..
        } => "no automatic update action",
        InstallMethod::Npm
        | InstallMethod::Bun
        | InstallMethod::Pnpm
        | InstallMethod::Brew
        | InstallMethod::Other => "no automatic update action",
    }
}

fn is_sedna_automatic_update_probe_available(
    context: &InstallContext,
    release_channel: SednaReleaseChannel,
) -> bool {
    is_sedna_automatic_update_probe_available_on_target(
        context,
        is_sedna_release_identity(
            option_env!("CODEX_RELEASE_REPOSITORY"),
            option_env!("CODEX_RELEASE_TAG_PREFIX"),
        ),
        RELEASE_VERSION,
        std::env::consts::OS,
        std::env::consts::ARCH,
        release_channel,
    )
}

fn is_sedna_automatic_update_probe_available_on_target(
    context: &InstallContext,
    has_sedna_identity: bool,
    release_version: &str,
    target_os: &str,
    target_arch: &str,
    release_channel: SednaReleaseChannel,
) -> bool {
    has_sedna_identity
        && is_sedna_automatic_update_eligible_for_channel(
            release_version,
            target_os,
            target_arch,
            release_channel,
        )
        && matches!(
            &context.method,
            InstallMethod::Standalone {
                platform: StandalonePlatform::Unix,
                ..
            }
        )
}

fn fetch_latest_sedna_release_version(
    context: &InstallContext,
    release_channel: SednaReleaseChannel,
) -> Result<String, String> {
    if !is_sedna_automatic_update_probe_available(context, release_channel) {
        return Err(
            "latest release probe is unavailable outside the Sedna standalone automatic-update policy"
                .to_string(),
        );
    }
    let url = format!(
        "https://api.github.com/repos/{}/releases?per_page=100",
        codex_utils_version::SEDNA_RELEASE_REPOSITORY
    );
    let releases = http_get_json::<Vec<ReleaseInfo>>(&url)?;
    select_latest_sedna_release(releases, release_channel)
}

#[derive(Deserialize)]
struct ReleaseInfo {
    tag_name: String,
    #[serde(default)]
    prerelease: bool,
    #[serde(default)]
    draft: bool,
    #[serde(default)]
    assets: Vec<ReleaseAsset>,
}

#[derive(Deserialize)]
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
    release_channel: Option<SednaReleaseChannel>,
}

fn select_latest_sedna_release(
    releases: Vec<ReleaseInfo>,
    release_channel: SednaReleaseChannel,
) -> Result<String, String> {
    let target = match (std::env::consts::OS, std::env::consts::ARCH) {
        ("linux", "x86_64") => "x86_64-unknown-linux-gnu",
        ("linux", "aarch64") => "aarch64-unknown-linux-gnu",
        _ => return Err("unsupported Sedna installer target".to_string()),
    };
    let metadata_name = if target == "x86_64-unknown-linux-gnu" {
        "RELEASE-METADATA.json".to_string()
    } else {
        format!("RELEASE-METADATA-{target}.json")
    };
    let mut candidates = releases
        .into_iter()
        .filter(|release| {
            !release.draft && release_channel.allows_api_prerelease(release.prerelease)
        })
        .filter_map(|release| {
            parse_sedna_release_tag(&release.tag_name).map(|version| (release, version))
        })
        .collect::<Vec<_>>();
    candidates.sort_by(|(_, left), (_, right)| {
        is_newer_sedna_release(left, right)
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
        let Some(asset) = release
            .assets
            .iter()
            .find(|asset| asset.name == metadata_name)
        else {
            continue;
        };
        let metadata = http_get_json::<ReleaseMetadata>(&asset.browser_download_url)?;
        let api_channel = if release.prerelease {
            SednaReleaseChannel::Prerelease
        } else {
            SednaReleaseChannel::Stable
        };
        if release_metadata_matches(&metadata, &release.tag_name, &version, target, api_channel) {
            return Ok(version);
        }
    }
    Err("no valid published Sedna release matches the selected channel".to_string())
}

fn release_metadata_matches(
    metadata: &ReleaseMetadata,
    release_tag: &str,
    release_version: &str,
    target: &str,
    api_channel: SednaReleaseChannel,
) -> bool {
    metadata.release_tag == release_tag
        && metadata.release_version == release_version
        && metadata.repository == codex_utils_version::SEDNA_RELEASE_REPOSITORY
        && metadata.target == target
        // The GitHub API is the release-channel authority. Metadata may omit
        // the field for legacy releases, but it cannot contradict the API.
        && metadata.release_channel.is_none_or(|channel| channel == api_channel)
}

fn http_get_json<T>(url: &str) -> Result<T, String>
where
    T: for<'de> Deserialize<'de>,
{
    let body = run_command("curl", ["-fsSL", "--max-time", "5", url])?;
    serde_json::from_str::<T>(&body).map_err(|err| err.to_string())
}

#[derive(Deserialize)]
struct VersionInfo {
    latest_version: String,
    #[serde(default)]
    last_checked_at: Option<String>,
    #[serde(default)]
    dismissed_version: Option<String>,
    #[serde(default)]
    release_repository: Option<String>,
    #[serde(default)]
    release_tag_prefix: Option<String>,
    #[serde(default)]
    release_channel: Option<SednaReleaseChannel>,
}

impl VersionInfo {
    fn matches_sedna_release_identity_and_channel(
        &self,
        release_channel: SednaReleaseChannel,
    ) -> bool {
        is_sedna_release_identity(
            self.release_repository.as_deref(),
            self.release_tag_prefix.as_deref(),
        ) && self.release_channel == Some(release_channel)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_only_sedna_release_tags() {
        assert_eq!(
            parse_sedna_release_tag("v1.2.3-alpha.4-sedna.2+upstream.17"),
            Some("1.2.3-alpha.4-sedna.2+upstream.17".to_string())
        );
        for tag in [
            "rust-v1.2.3",
            "v1.2.3",
            "v1.2.3-sedna.x",
            "v1.2.3-sedna.1+build.2",
            "v1.2.3-sedna.1+upstream.x",
            "v1.2.3-rc-1-sedna.1",
        ] {
            assert_eq!(parse_sedna_release_tag(tag), None, "accepted {tag}");
        }
    }

    #[test]
    fn update_action_labels_never_suggest_upstream_package_managers() {
        for method in [
            InstallMethod::Npm,
            InstallMethod::Pnpm,
            InstallMethod::Other,
        ] {
            assert!(
                !update_action_label(
                    &InstallContext {
                        method,
                        package_layout: None,
                    },
                    SednaReleaseChannel::Stable
                )
                .contains("openai")
            );
        }
    }

    #[test]
    fn doctor_action_label_offers_sedna_installer_only_for_unix_standalone() {
        let native_release_dir = codex_utils_absolute_path::AbsolutePathBuf::from_absolute_path(
            std::env::temp_dir().join("native-release"),
        )
        .expect("temp dir path should be absolute");
        let unix = InstallContext {
            method: InstallMethod::Standalone {
                platform: StandalonePlatform::Unix,
                release_dir: native_release_dir.clone(),
                resources_dir: None,
            },
            package_layout: None,
        };
        let windows = InstallContext {
            method: InstallMethod::Standalone {
                platform: StandalonePlatform::Windows,
                release_dir: native_release_dir,
                resources_dir: None,
            },
            package_layout: None,
        };
        assert_eq!(
            update_action_label_for_sedna_identity_on_target(
                &unix,
                /*has_sedna_identity*/ true,
                "1.2.3-sedna.4",
                "linux",
                "x86_64",
                SednaReleaseChannel::Stable,
            ),
            "Sedna standalone installer"
        );
        assert_eq!(
            update_action_label_for_sedna_identity(
                &windows,
                /*has_sedna_identity*/ true,
                "1.2.3-sedna.4",
                SednaReleaseChannel::Stable,
            ),
            "no automatic update action"
        );
        for release_version in ["1.2.3", "not-a-Sedna-release"] {
            assert_eq!(
                update_action_label_for_sedna_identity(
                    &unix,
                    /*has_sedna_identity*/ true,
                    release_version,
                    SednaReleaseChannel::Stable,
                ),
                "no automatic update action",
                "accepted {release_version}"
            );
        }
        assert_eq!(
            update_action_label_for_sedna_identity(
                &unix,
                /*has_sedna_identity*/ true,
                "1.2.3-alpha.1-sedna.1",
                SednaReleaseChannel::Stable,
            ),
            "Sedna standalone installer"
        );
        for target in [
            ("macos", "x86_64"),
            ("macos", "aarch64"),
            ("freebsd", "x86_64"),
        ] {
            assert_eq!(
                update_action_label_for_sedna_identity_on_target(
                    &unix,
                    /*has_sedna_identity*/ true,
                    "1.2.3-sedna.4",
                    target.0,
                    target.1,
                    SednaReleaseChannel::Stable,
                ),
                "no automatic update action",
                "accepted unsupported target {}-{}",
                target.0,
                target.1
            );
        }
    }

    #[test]
    fn sedna_release_probe_requires_unix_standalone_install() {
        let native_release_dir = codex_utils_absolute_path::AbsolutePathBuf::from_absolute_path(
            std::env::temp_dir().join("native-release"),
        )
        .expect("temp dir path should be absolute");
        let unix = InstallContext {
            method: InstallMethod::Standalone {
                platform: StandalonePlatform::Unix,
                release_dir: native_release_dir,
                resources_dir: None,
            },
            package_layout: None,
        };
        let npm = InstallContext {
            method: InstallMethod::Npm,
            package_layout: None,
        };

        assert!(is_sedna_automatic_update_probe_available_on_target(
            &unix,
            /*has_sedna_identity*/ true,
            "1.2.3-sedna.4",
            "linux",
            "x86_64",
            SednaReleaseChannel::Stable,
        ));
        assert!(!is_sedna_automatic_update_probe_available_on_target(
            &npm,
            /*has_sedna_identity*/ true,
            "1.2.3-sedna.4",
            "linux",
            "x86_64",
            SednaReleaseChannel::Stable,
        ));
    }

    #[test]
    fn sedna_update_identity_requires_both_explicit_build_values() {
        assert!(is_sedna_release_identity(
            Some("sednalabs/codex"),
            Some("v")
        ));
        for identity in [
            (None, None),
            (Some("sednalabs/codex"), None),
            (None, Some("v")),
            (Some("openai/codex"), Some("v")),
            (Some("sednalabs/codex"), Some("rust-v")),
        ] {
            assert!(!is_sedna_release_identity(identity.0, identity.1));
        }
    }

    #[test]
    fn compares_only_valid_sedna_release_versions() {
        assert_eq!(
            is_newer_sedna_release("1.2.3-sedna.2", "1.2.3-sedna.1"),
            Some(true)
        );
        assert_eq!(
            is_newer_sedna_release("1.2.3-sedna.1", "1.2.3-sedna.2"),
            Some(false)
        );
        assert_eq!(
            is_newer_sedna_release("1.2.3-alpha.2-sedna.1", "1.2.3-alpha.1-sedna.9"),
            Some(true)
        );
        assert_eq!(
            is_newer_sedna_release("1.2.3-alpha.10-sedna.1", "1.2.3-alpha.2-sedna.99"),
            Some(true)
        );
        assert_eq!(is_newer_sedna_release("1.2.3", "1.2.2-sedna.1"), None);
        assert_eq!(
            is_newer_sedna_release("1.2.3-sedna.2", "1.2.2+upstream.3"),
            None
        );
    }

    #[test]
    fn sedna_release_summary_reports_newer_and_warning_states() {
        assert_eq!(
            sedna_release_comparison_status(Some(true)),
            (CheckStatus::Ok, NEWER_SEDNA_RELEASE_SUMMARY)
        );
        assert_eq!(
            sedna_release_comparison_status(Some(false)),
            (CheckStatus::Ok, LOCALLY_CONSISTENT_SUMMARY)
        );
        assert_eq!(
            sedna_release_comparison_status(/*is_newer*/ None),
            (CheckStatus::Warning, INVALID_SEDNA_VERSION_SUMMARY)
        );
        assert_eq!(
            sedna_release_probe_failure_status(),
            (CheckStatus::Warning, SEDNA_RELEASE_PROBE_WARNING_SUMMARY)
        );
    }

    #[test]
    fn matching_cache_identity_retains_cached_version_details() {
        let info = VersionInfo {
            latest_version: "1.2.3-sedna.2".to_string(),
            last_checked_at: Some("2026-08-20T00:00:00Z".to_string()),
            dismissed_version: Some("1.2.3-sedna.2".to_string()),
            release_repository: Some("sednalabs/codex".to_string()),
            release_tag_prefix: Some("v".to_string()),
            release_channel: Some(SednaReleaseChannel::Stable),
        };
        let mut details = Vec::new();

        push_cache_info_details(
            &mut details,
            &info,
            /*automatic_update_probe_available*/ true,
            SednaReleaseChannel::Stable,
        );

        assert_eq!(
            details,
            vec![
                "cached latest version: 1.2.3-sedna.2",
                "last checked at: 2026-08-20T00:00:00Z",
                "dismissed version: 1.2.3-sedna.2",
            ]
        );
    }

    #[test]
    fn prerelease_doctor_cache_and_metadata_must_match_the_selected_api_channel() {
        let info = VersionInfo {
            latest_version: "1.2.3-alpha.1-sedna.2".to_string(),
            last_checked_at: None,
            dismissed_version: None,
            release_repository: Some("sednalabs/codex".to_string()),
            release_tag_prefix: Some("v".to_string()),
            release_channel: Some(SednaReleaseChannel::Prerelease),
        };
        assert!(info.matches_sedna_release_identity_and_channel(SednaReleaseChannel::Prerelease));
        assert!(!info.matches_sedna_release_identity_and_channel(SednaReleaseChannel::Stable));

        let metadata = ReleaseMetadata {
            release_tag: "v1.2.3-alpha.1-sedna.2".to_string(),
            release_version: "1.2.3-alpha.1-sedna.2".to_string(),
            repository: "sednalabs/codex".to_string(),
            target: "x86_64-unknown-linux-gnu".to_string(),
            release_channel: Some(SednaReleaseChannel::Prerelease),
        };
        assert!(release_metadata_matches(
            &metadata,
            "v1.2.3-alpha.1-sedna.2",
            "1.2.3-alpha.1-sedna.2",
            "x86_64-unknown-linux-gnu",
            SednaReleaseChannel::Prerelease,
        ));
        assert!(!release_metadata_matches(
            &metadata,
            "v1.2.3-alpha.1-sedna.2",
            "1.2.3-alpha.1-sedna.2",
            "x86_64-unknown-linux-gnu",
            SednaReleaseChannel::Stable,
        ));
    }

    #[test]
    fn cached_update_details_require_unix_standalone_install() {
        let info = VersionInfo {
            latest_version: "1.2.3-sedna.2".to_string(),
            last_checked_at: Some("2026-08-20T00:00:00Z".to_string()),
            dismissed_version: Some("1.2.3-sedna.2".to_string()),
            release_repository: Some("sednalabs/codex".to_string()),
            release_tag_prefix: Some("v".to_string()),
            release_channel: Some(SednaReleaseChannel::Stable),
        };
        let native_release_dir = codex_utils_absolute_path::AbsolutePathBuf::from_absolute_path(
            std::env::temp_dir().join("native-release"),
        )
        .expect("temp dir path should be absolute");
        let unix = InstallContext {
            method: InstallMethod::Standalone {
                platform: StandalonePlatform::Unix,
                release_dir: native_release_dir,
                resources_dir: None,
            },
            package_layout: None,
        };
        let npm = InstallContext {
            method: InstallMethod::Npm,
            package_layout: None,
        };

        let mut npm_details = Vec::new();
        let npm_probe_available = is_sedna_automatic_update_probe_available_on_target(
            &npm,
            /*has_sedna_identity*/ true,
            "1.2.3-sedna.4",
            "linux",
            "x86_64",
            SednaReleaseChannel::Stable,
        );
        assert!(!npm_probe_available);
        push_cache_info_details(
            &mut npm_details,
            &info,
            npm_probe_available,
            SednaReleaseChannel::Stable,
        );
        assert!(npm_details.is_empty());

        let mut unix_details = Vec::new();
        let unix_probe_available = is_sedna_automatic_update_probe_available_on_target(
            &unix,
            /*has_sedna_identity*/ true,
            "1.2.3-sedna.4",
            "linux",
            "x86_64",
            SednaReleaseChannel::Stable,
        );
        assert!(unix_probe_available);
        push_cache_info_details(
            &mut unix_details,
            &info,
            unix_probe_available,
            SednaReleaseChannel::Stable,
        );
        assert_eq!(
            unix_details,
            vec![
                "cached latest version: 1.2.3-sedna.2",
                "last checked at: 2026-08-20T00:00:00Z",
                "dismissed version: 1.2.3-sedna.2",
            ]
        );
    }

    #[test]
    fn source_less_cache_identity_is_ignored() {
        let info = VersionInfo {
            latest_version: "1.2.3-sedna.2".to_string(),
            last_checked_at: None,
            dismissed_version: Some("1.2.3-sedna.2".to_string()),
            release_repository: None,
            release_tag_prefix: None,
            release_channel: None,
        };
        let mut details = Vec::new();

        push_cache_info_details(
            &mut details,
            &info,
            /*automatic_update_probe_available*/ true,
            SednaReleaseChannel::Stable,
        );

        assert_eq!(
            details,
            vec!["version cache: ignored (untrusted release identity or channel)"]
        );
    }

    #[test]
    fn mismatched_cache_identity_is_ignored() {
        let info = VersionInfo {
            latest_version: "999.0.0".to_string(),
            last_checked_at: None,
            dismissed_version: Some("999.0.0".to_string()),
            release_repository: Some("openai/codex".to_string()),
            release_tag_prefix: Some("rust-v".to_string()),
            release_channel: Some(SednaReleaseChannel::Stable),
        };
        let mut details = Vec::new();

        push_cache_info_details(
            &mut details,
            &info,
            /*automatic_update_probe_available*/ true,
            SednaReleaseChannel::Stable,
        );

        assert_eq!(
            details,
            vec!["version cache: ignored (untrusted release identity or channel)"]
        );
    }
}
