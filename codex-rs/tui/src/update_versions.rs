use crate::version::CODEX_RELEASE_TAG_PREFIX;
use semver::Version;

pub(crate) use codex_utils_version::is_newer_sedna_release as is_newer;
pub(crate) use codex_utils_version::is_sedna_release_version;

/// Cache values are untrusted across releases. A cached value may be compared
/// only after it passes the Sedna release grammar used for live release tags.
pub(crate) fn is_actionable_sedna_update(latest: &str, current: &str) -> bool {
    codex_utils_version::is_sedna_release_version(latest)
        && codex_utils_version::is_sedna_release_version(current)
        && is_newer(latest, current).unwrap_or(false)
}

pub(crate) fn extract_version_from_latest_tag(latest_tag_name: &str) -> anyhow::Result<String> {
    let version = latest_tag_name
        .strip_prefix(CODEX_RELEASE_TAG_PREFIX)
        .filter(|version| is_sedna_release_version(version))
        .ok_or_else(|| anyhow::anyhow!("Failed to parse latest tag name '{latest_tag_name}'"))?;
    Ok(version.to_owned())
}

pub(crate) fn is_source_build_version(version: &str) -> bool {
    parse_version(version) == Some(Version::new(0, 0, 0))
}

fn parse_version(v: &str) -> Option<Version> {
    Version::parse(v.trim()).ok()
}

#[cfg(test)]
mod tests {
    use super::*;
    use pretty_assertions::assert_eq;

    #[test]
    fn extracts_version_from_latest_tag() {
        assert!(extract_version_from_latest_tag("rust-v1.5.0").is_err());
        assert_eq!(
            extract_version_from_latest_tag("v1.5.0-sedna.1").expect("failed to parse version"),
            "1.5.0-sedna.1"
        );
        assert_eq!(
            extract_version_from_latest_tag("v0.119.0-alpha.4-sedna.2+upstream.17")
                .expect("failed to parse version"),
            "0.119.0-alpha.4-sedna.2+upstream.17"
        );
    }

    #[test]
    fn latest_tag_without_version_prefix_is_invalid() {
        assert!(extract_version_from_latest_tag("release-1.5.0").is_err());
    }

    #[test]
    fn only_sedna_release_tags_are_admitted() {
        for tag in [
            "rust-v1.5.0",
            "v1.5.0",
            "v1.5.0-sedna",
            "v1.5.0-sedna.x",
            "v1.5.0-sedna.1+build.2",
            "v1.5.0-sedna.1+upstream.x",
            "v1.5.0-rc-1-sedna.1",
            "v1.5.0-sedna.1+upstream.2+extra",
        ] {
            assert!(
                extract_version_from_latest_tag(tag).is_err(),
                "accepted {tag}"
            );
        }
    }

    #[test]
    fn cached_updates_must_be_sedna_releases_before_comparison() {
        assert!(is_actionable_sedna_update("1.5.0-sedna.2", "1.5.0-sedna.1"));
        for cached_version in [
            "1.5.1",
            "1.5.1+upstream.4",
            "1.5.1-sedna.x",
            "not-a-sedna-release",
        ] {
            assert!(
                !is_actionable_sedna_update(cached_version, "1.5.0-sedna.1"),
                "accepted cached version {cached_version}"
            );
        }
    }

    #[test]
    fn prerelease_version_is_not_considered_newer() {
        assert_eq!(
            is_newer("0.11.0-beta.1-sedna.1", "0.11.0-sedna.1"),
            Some(false)
        );
        assert_eq!(is_newer("1.0.0-rc.1-sedna.1", "1.0.0-sedna.1"), Some(false));
    }

    #[test]
    fn fork_release_suffixes_compare_correctly() {
        assert_eq!(is_newer("0.117.0-sedna.2", "0.117.0-sedna.1"), Some(true));
        assert_eq!(is_newer("0.117.0-sedna.1", "0.117.0+abcdef12"), None);
        assert_eq!(
            is_newer("0.119.0-sedna.2", "0.119.0-alpha.2-sedna.1"),
            Some(true)
        );
        assert_eq!(
            is_newer("0.119.0-alpha.10-sedna.1", "0.119.0-alpha.2-sedna.99"),
            Some(true)
        );
        assert_eq!(
            is_newer("0.119.0-alpha.10-sedna.2", "0.119.0-alpha.10-sedna.1"),
            Some(true)
        );
    }

    #[test]
    fn non_sedna_or_malformed_versions_fail_closed() {
        assert_eq!(is_newer("0.11.1", "0.11.0"), None);
        assert_eq!(is_newer("0.11.0-sedna.x", "0.11.0-sedna.1"), None);
    }

    #[test]
    fn source_build_version_is_not_checked() {
        assert!(is_source_build_version("0.0.0"));
        assert!(!is_source_build_version("0.1.0"));
    }

    #[test]
    fn whitespace_is_ignored() {
        assert_eq!(
            parse_version(" 1.2.3 \n"),
            Some(Version::parse("1.2.3").expect("valid semver"))
        );
        assert_eq!(is_newer(" 1.2.3-sedna.2 ", "1.2.3-sedna.1"), None);
    }
}
