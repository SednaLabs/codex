use schemars::JsonSchema;
use serde::Deserialize;
use serde::Serialize;
use std::cmp::Ordering;

/// The explicit repository identity for the Sedna release channel.
pub const SEDNA_RELEASE_REPOSITORY: &str = "sednalabs/codex";

/// The required tag prefix for the Sedna release channel.
pub const SEDNA_RELEASE_TAG_PREFIX: &str = "v";

/// The user-selectable Sedna release stream.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "lowercase")]
pub enum SednaReleaseChannel {
    #[default]
    Stable,
    Prerelease,
}

impl SednaReleaseChannel {
    /// The GitHub API is authoritative for a published release's channel.
    /// An upstream alpha suffix in a fork version is provenance, not a channel.
    pub const fn allows_api_prerelease(self, prerelease: bool) -> bool {
        matches!(self, Self::Prerelease) || !prerelease
    }
}

/// The canonical Sedna release version used for updater comparisons, persistence, and telemetry.
pub const RELEASE_VERSION: &str = env!("CODEX_RELEASE_VERSION_EFFECTIVE");

/// A compact human-readable version label that can include build provenance.
pub const DISPLAY_VERSION: &str = env!("CODEX_VERSION_DISPLAY");

/// The current build provenance label, if one was embedded at build time.
pub fn build_provenance() -> Option<&'static str> {
    match env!("CODEX_BUILD_PROVENANCE") {
        "" => None,
        value => Some(value),
    }
}

/// The current upstream track, if one was embedded at build time.
pub fn upstream_track() -> Option<&'static str> {
    match env!("CODEX_UPSTREAM_TRACK") {
        "" => None,
        value => Some(value),
    }
}

/// The merge-base commit against the mirrored upstream branch, if embedded.
pub fn upstream_base_commit() -> Option<&'static str> {
    match env!("CODEX_UPSTREAM_BASE_COMMIT") {
        "" => None,
        value => Some(value),
    }
}

/// The exact upstream tag at the merge-base commit, if there is one.
pub fn upstream_base_tag() -> Option<&'static str> {
    match env!("CODEX_UPSTREAM_BASE_TAG") {
        "" => None,
        value => Some(value),
    }
}

/// The current downstream commit identifier for this build.
pub const DOWNSTREAM_COMMIT: &str = env!("CODEX_DOWNSTREAM_COMMIT");

/// The full downstream commit SHA for this build when it is available.
pub fn downstream_commit_full() -> Option<&'static str> {
    match env!("CODEX_DOWNSTREAM_COMMIT_FULL") {
        "" => None,
        value => Some(value),
    }
}

/// The short git SHA used by local build displays.
pub const GIT_SHA: &str = env!("CODEX_GIT_SHA");

/// The human-readable git describe string for this build.
pub const GIT_DESCRIBE: &str = env!("CODEX_GIT_DESCRIBE");

/// Whether explicit build metadata identifies the Sedna release channel.
pub fn is_sedna_release_identity(repository: Option<&str>, tag_prefix: Option<&str>) -> bool {
    matches!(repository, Some(SEDNA_RELEASE_REPOSITORY))
        && matches!(tag_prefix, Some(SEDNA_RELEASE_TAG_PREFIX))
}

/// Whether the target is eligible for automatic Sedna updates.
///
/// Callers should pass `std::env::consts::OS` and `std::env::consts::ARCH`
/// so this remains both exact for the running binary and directly testable.
pub fn is_sedna_automatic_update_target_supported(os: &str, arch: &str) -> bool {
    matches!((os, arch), ("linux", "x86_64") | ("linux", "aarch64"))
}

/// Matches the strict Sedna release grammar:
/// `<track>-sedna.<ordinal>[+upstream.<distance>]`.
pub fn is_sedna_release_version(version: &str) -> bool {
    let (version, metadata) = match version.split_once('+') {
        Some((version, metadata)) => (version, Some(metadata)),
        None => (version, None),
    };
    if metadata.is_some_and(|metadata| {
        metadata.strip_prefix("upstream.").is_none_or(|distance| {
            distance.is_empty() || !distance.bytes().all(|byte| byte.is_ascii_digit())
        })
    }) {
        return false;
    }

    let Some((track, ordinal)) = version.rsplit_once("-sedna.") else {
        return false;
    };
    !ordinal.is_empty()
        && ordinal.bytes().all(|byte| byte.is_ascii_digit())
        && is_sedna_track(track)
}

/// Returns the version component of a strict Sedna release tag.
pub fn parse_sedna_release_tag(tag: &str) -> Option<String> {
    let version = tag.strip_prefix(SEDNA_RELEASE_TAG_PREFIX)?;
    is_sedna_release_version(version).then(|| version.to_owned())
}

/// Whether a strict Sedna release belongs to the stable track.
pub fn is_stable_sedna_release_version(version: &str) -> bool {
    let version_without_metadata = version.split_once('+').map_or(version, |(v, _)| v);
    let Some((track, _)) = version_without_metadata.rsplit_once("-sedna.") else {
        return false;
    };
    is_sedna_release_version(version) && !track.contains('-')
}

/// Whether this release may use the automatic Sedna update channel.
pub fn is_sedna_automatic_update_eligible(
    release_version: &str,
    target_os: &str,
    target_arch: &str,
) -> bool {
    is_stable_sedna_release_version(release_version)
        && is_sedna_automatic_update_target_supported(target_os, target_arch)
}

pub fn is_sedna_automatic_update_eligible_for_channel(
    release_version: &str,
    target_os: &str,
    target_arch: &str,
    channel: SednaReleaseChannel,
) -> bool {
    is_sedna_release_version(release_version)
        && is_sedna_automatic_update_target_supported(target_os, target_arch)
        // The selected channel governs remote candidates. A local version only
        // establishes the strict Sedna identity required for comparisons.
        && matches!(channel, SednaReleaseChannel::Stable | SednaReleaseChannel::Prerelease)
}

/// Whether a validated managed binary carries a Sedna release identity. The
/// configured channel decides whether stable or prerelease updates are allowed.
pub fn is_sedna_managed_release(version: &str, target_os: &str, target_arch: &str) -> bool {
    is_sedna_release_version(version)
        && is_sedna_automatic_update_target_supported(target_os, target_arch)
}

/// Compares two strict Sedna release versions without treating the downstream
/// ordinal as a SemVer prerelease identifier.
pub fn is_newer_sedna_release(latest: &str, current: &str) -> Option<bool> {
    let latest = SednaReleaseVersion::parse(latest)?;
    let current = SednaReleaseVersion::parse(current)?;
    Some(latest > current)
}

fn is_sedna_track(track: &str) -> bool {
    let (core, prerelease) = match track.split_once('-') {
        Some((core, prerelease)) => (core, Some(prerelease)),
        None => (track, None),
    };
    let mut core_parts = core.split('.');
    let valid_core = (0..3).all(|_| {
        core_parts
            .next()
            .is_some_and(|part| !part.is_empty() && part.bytes().all(|byte| byte.is_ascii_digit()))
    }) && core_parts.next().is_none();
    valid_core
        && prerelease.is_none_or(|prerelease| {
            !prerelease.is_empty()
                && prerelease.split('.').all(|identifier| {
                    !identifier.is_empty()
                        && identifier.bytes().all(|byte| byte.is_ascii_alphanumeric())
                })
        })
}

#[derive(Debug, Eq, PartialEq)]
struct SednaReleaseVersion {
    core: [String; 3],
    track_prerelease: Vec<String>,
    ordinal: String,
}

impl SednaReleaseVersion {
    fn parse(version: &str) -> Option<Self> {
        if !is_sedna_release_version(version) {
            return None;
        }
        let version_without_metadata = version.split_once('+').map_or(version, |(v, _)| v);
        let (track, ordinal) = version_without_metadata.rsplit_once("-sedna.")?;
        let (core, track_prerelease) = match track.split_once('-') {
            Some((core, prerelease)) => (core, prerelease.split('.').map(str::to_owned).collect()),
            None => (track, Vec::new()),
        };
        let mut core_parts = core.split('.').map(str::to_owned);
        let core = [core_parts.next()?, core_parts.next()?, core_parts.next()?];
        if core_parts.next().is_some() {
            return None;
        }
        Some(Self {
            core,
            track_prerelease,
            ordinal: ordinal.to_string(),
        })
    }
}

impl Ord for SednaReleaseVersion {
    fn cmp(&self, other: &Self) -> Ordering {
        for (left, right) in self.core.iter().zip(&other.core) {
            let ordering = compare_numeric_identifiers(left, right);
            if ordering != Ordering::Equal {
                return ordering;
            }
        }
        let prerelease_ordering = match (
            self.track_prerelease.is_empty(),
            other.track_prerelease.is_empty(),
        ) {
            (true, true) => Ordering::Equal,
            (true, false) => Ordering::Greater,
            (false, true) => Ordering::Less,
            (false, false) => {
                compare_prerelease_identifiers(&self.track_prerelease, &other.track_prerelease)
            }
        };
        if prerelease_ordering != Ordering::Equal {
            return prerelease_ordering;
        }
        compare_numeric_identifiers(&self.ordinal, &other.ordinal)
    }
}

impl PartialOrd for SednaReleaseVersion {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

fn compare_prerelease_identifiers(left: &[String], right: &[String]) -> Ordering {
    for (left, right) in left.iter().zip(right) {
        let ordering = match (
            left.bytes().all(|byte| byte.is_ascii_digit()),
            right.bytes().all(|byte| byte.is_ascii_digit()),
        ) {
            (true, true) => compare_numeric_identifiers(left, right),
            (true, false) => Ordering::Less,
            (false, true) => Ordering::Greater,
            (false, false) => left.cmp(right),
        };
        if ordering != Ordering::Equal {
            return ordering;
        }
    }
    left.len().cmp(&right.len())
}

fn compare_numeric_identifiers(left: &str, right: &str) -> Ordering {
    let left = left.trim_start_matches('0');
    let right = right.trim_start_matches('0');
    left.len().cmp(&right.len()).then_with(|| left.cmp(right))
}
