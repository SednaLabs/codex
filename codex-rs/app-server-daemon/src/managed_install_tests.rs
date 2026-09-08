use pretty_assertions::assert_eq;
use std::fs;
use std::os::unix::fs::symlink;
use std::path::Path;
use std::path::PathBuf;
use tempfile::TempDir;

use super::ManagedReleaseMetadata;
use super::executable_identity;
use super::executable_identity_from_bytes;
use super::managed_sedna_automatic_update_release_from_metadata;
use super::parse_codex_version;
use super::resolved_managed_standalone_release;
use super::sha256_file;
use super::sha256_hex;

#[test]
fn parses_codex_cli_version_output() {
    assert_eq!(
        parse_codex_version("codex 1.2.3\n").expect("version"),
        "1.2.3"
    );
}

#[test]
fn rejects_malformed_codex_cli_version_output() {
    assert!(parse_codex_version("codex\n").is_err());
}

#[test]
fn executable_identity_uses_binary_contents() {
    let old = executable_identity_from_bytes(b"old");
    let same = executable_identity_from_bytes(b"old");
    let new = executable_identity_from_bytes(b"new");

    assert_eq!(old, same);
    assert_ne!(old, new);
}

#[tokio::test]
async fn streamed_sha256_matches_in_memory_hash_across_chunk_boundaries() {
    let temp = TempDir::new().expect("temporary directory");
    let bytes: Vec<u8> = (0..(64 * 1024 * 2 + 17))
        .map(|index| (index % 251) as u8)
        .collect();
    let executable = temp.path().join("codex");
    fs::write(&executable, &bytes).expect("executable");

    let streamed = sha256_file(&executable).await.expect("streamed digest");
    assert_eq!(sha256_hex(&bytes), sha256_hex(&streamed));
    assert_eq!(
        executable_identity(&executable).await.expect("identity"),
        executable_identity_from_bytes(&bytes)
    );
}

#[test]
fn managed_release_metadata_controls_sedna_automatic_update_authority() {
    let managed_sedna = ManagedReleaseMetadata {
        release_tag: "v1.2.3-sedna.1".to_string(),
        release_version: "1.2.3-sedna.1".to_string(),
        repository: "sednalabs/codex".to_string(),
        target: "x86_64-unknown-linux-gnu".to_string(),
    };
    for invoking_binary in ["Sedna", "upstream"] {
        assert_eq!(
            managed_sedna_automatic_update_release_from_metadata(
                &managed_sedna,
                "linux",
                "x86_64",
            )
            .map(|release| release.version),
            Some("1.2.3-sedna.1".to_string()),
            "{invoking_binary} caller must not override eligible managed Sedna metadata"
        );
    }

    let managed_upstream = ManagedReleaseMetadata {
        repository: "openai/codex".to_string(),
        ..managed_sedna
    };
    assert_eq!(
        managed_sedna_automatic_update_release_from_metadata(&managed_upstream, "linux", "x86_64",),
        None,
        "Sedna caller must fail closed for managed upstream metadata"
    );
}

#[test]
fn managed_release_metadata_rejects_prerelease_and_wrong_target() {
    for metadata in [
        ManagedReleaseMetadata {
            release_tag: "v1.2.3-alpha.1-sedna.1".to_string(),
            release_version: "1.2.3-alpha.1-sedna.1".to_string(),
            repository: "sednalabs/codex".to_string(),
            target: "x86_64-unknown-linux-gnu".to_string(),
        },
        ManagedReleaseMetadata {
            release_tag: "v1.2.3-sedna.1".to_string(),
            release_version: "1.2.3-sedna.1".to_string(),
            repository: "sednalabs/codex".to_string(),
            target: "x86_64-apple-darwin".to_string(),
        },
    ] {
        assert_eq!(
            managed_sedna_automatic_update_release_from_metadata(&metadata, "linux", "x86_64"),
            None
        );
    }
}

#[tokio::test]
async fn resolved_release_uses_the_installer_manifest_and_rereads_current() {
    let temp = TempDir::new().expect("temporary Codex home");
    let standalone = temp.path().join("packages/standalone");
    let stable = write_release(
        &standalone,
        "v1.2.3-sedna.1",
        &release_metadata(
            "sednalabs/codex",
            "v1.2.3-sedna.1",
            "1.2.3-sedna.1",
            current_target(),
        ),
    );
    let upstream = write_release(
        &standalone,
        "rust-v1.2.4",
        &release_metadata("openai/codex", "rust-v1.2.4", "1.2.4", current_target()),
    );
    let current = standalone.join("current");
    symlink(&stable, &current).expect("initial current symlink");
    let workflow_manifest =
        fs::read_to_string(stable.join("SHA256SUMS.txt")).expect("workflow checksum manifest");
    assert!(!workflow_manifest.contains("  codex\n"));
    assert!(workflow_manifest.contains("RELEASE-METADATA-aarch64-unknown-linux-gnu.json"));

    let first = resolved_managed_standalone_release(&current.join("codex"))
        .await
        .expect("managed stable release");
    assert_eq!(
        first.release_dir,
        fs::canonicalize(&stable).expect("stable release")
    );
    assert_eq!(
        first.executable,
        fs::canonicalize(stable.join("codex")).expect("stable binary")
    );
    assert_eq!(
        first.sedna_auto_update.is_some(),
        matches!(
            (std::env::consts::OS, std::env::consts::ARCH),
            ("linux", "x86_64") | ("linux", "aarch64")
        )
    );

    let next = standalone.join("current.next");
    symlink(&upstream, &next).expect("replacement current symlink");
    fs::rename(next, &current).expect("replace current symlink");

    let second = resolved_managed_standalone_release(&current.join("codex"))
        .await
        .expect("managed upstream release");
    assert_eq!(
        second.release_dir,
        fs::canonicalize(&upstream).expect("upstream release")
    );
    assert_eq!(
        second.executable,
        fs::canonicalize(upstream.join("codex")).expect("upstream binary")
    );
    assert_eq!(second.sedna_auto_update, None);
}

#[tokio::test]
async fn release_validation_fails_closed_for_outside_and_unverified_metadata() {
    let temp = TempDir::new().expect("temporary Codex home");
    let standalone = temp.path().join("packages/standalone");
    let current = standalone.join("current");
    fs::create_dir_all(standalone.join("releases")).expect("releases root");

    let outside = temp.path().join("outside");
    fs::create_dir_all(&outside).expect("outside release");
    fs::write(outside.join("codex"), b"outside binary").expect("outside binary");
    symlink(&outside, &current).expect("outside current symlink");
    assert!(
        resolved_managed_standalone_release(&current.join("codex"))
            .await
            .is_err()
    );

    fs::remove_file(&current).expect("remove outside current symlink");
    let invalid = write_release(
        &standalone,
        "invalid",
        &release_metadata(
            "sednalabs/codex",
            "v1.2.3-sedna.1",
            "1.2.3-sedna.1",
            current_target(),
        ),
    );
    symlink(&invalid, &current).expect("invalid current symlink");

    fs::remove_file(invalid.join("RELEASE-METADATA.json")).expect("remove metadata");
    assert_eq!(
        resolved_managed_standalone_release(&current.join("codex"))
            .await
            .expect("manual release authority")
            .sedna_auto_update,
        None
    );

    let malformed = b"not json";
    fs::write(invalid.join("RELEASE-METADATA.json"), malformed).expect("malformed metadata");
    write_installed_checksums(&invalid, malformed, b"managed codex binary");
    assert_eq!(
        resolved_managed_standalone_release(&current.join("codex"))
            .await
            .expect("manual release authority")
            .sedna_auto_update,
        None
    );

    let wrong_channel = release_metadata("openai/codex", "rust-v1.2.3", "1.2.3", current_target());
    fs::write(invalid.join("RELEASE-METADATA.json"), &wrong_channel).expect("upstream metadata");
    write_installed_checksums(&invalid, wrong_channel.as_bytes(), b"managed codex binary");
    assert_eq!(
        resolved_managed_standalone_release(&current.join("codex"))
            .await
            .expect("manual release authority")
            .sedna_auto_update,
        None
    );

    let version_mismatch = release_metadata(
        "sednalabs/codex",
        "v1.2.3-sedna.1",
        "1.2.4-sedna.1",
        current_target(),
    );
    fs::write(invalid.join("RELEASE-METADATA.json"), &version_mismatch)
        .expect("mismatched metadata");
    write_installed_checksums(
        &invalid,
        version_mismatch.as_bytes(),
        b"managed codex binary",
    );
    assert_eq!(
        resolved_managed_standalone_release(&current.join("codex"))
            .await
            .expect("manual release authority")
            .sedna_auto_update,
        None
    );

    fs::write(invalid.join("codex"), b"replaced executable").expect("tampered binary");
    assert_eq!(
        resolved_managed_standalone_release(&current.join("codex"))
            .await
            .expect("manual release authority")
            .sedna_auto_update,
        None
    );
}

fn write_release(standalone: &Path, release_name: &str, metadata: &str) -> PathBuf {
    let release = standalone.join("releases").join(release_name);
    fs::create_dir_all(&release).unwrap_or_else(|err| panic!("release directory: {err}"));
    fs::write(release.join("codex"), b"managed codex binary")
        .unwrap_or_else(|err| panic!("managed binary: {err}"));
    fs::write(release.join("RELEASE-METADATA.json"), metadata)
        .unwrap_or_else(|err| panic!("release metadata: {err}"));
    write_workflow_checksums(&release, metadata.as_bytes());
    write_installed_checksums(&release, metadata.as_bytes(), b"managed codex binary");
    release
}

fn write_workflow_checksums(release: &Path, metadata: &[u8]) {
    fs::write(
        release.join("SHA256SUMS.txt"),
        format!(
            "{}  codex-sedna-test-aarch64-unknown-linux-gnu.tar.gz\n{}  RELEASE-METADATA-aarch64-unknown-linux-gnu.json\n",
            "0".repeat(64),
            sha256_hex(metadata),
        ),
    )
    .unwrap_or_else(|err| panic!("workflow checksums: {err}"));
}

fn write_installed_checksums(release: &Path, metadata: &[u8], executable: &[u8]) {
    fs::write(
        release.join("INSTALLED-SHA256SUMS.txt"),
        format!(
            "{}  RELEASE-METADATA.json\n{}  codex\n",
            sha256_hex(metadata),
            sha256_hex(executable),
        ),
    )
    .unwrap_or_else(|err| panic!("release checksums: {err}"));
}

fn release_metadata(repository: &str, tag: &str, version: &str, target: &str) -> String {
    format!(
        r#"{{"release_tag":"{tag}","release_version":"{version}","repository":"{repository}","target":"{target}"}}"#
    )
}

fn current_target() -> &'static str {
    match (std::env::consts::OS, std::env::consts::ARCH) {
        ("linux", "x86_64") => "x86_64-unknown-linux-gnu",
        ("linux", "aarch64") => "aarch64-unknown-linux-gnu",
        _ => "unsupported-target",
    }
}
