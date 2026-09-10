use std::path::Path;
use std::path::PathBuf;

#[cfg(unix)]
use anyhow::Context;
#[cfg(unix)]
use anyhow::Result;
#[cfg(unix)]
use anyhow::anyhow;
#[cfg(unix)]
use serde::Deserialize;
#[cfg(unix)]
use sha2::Digest;
#[cfg(unix)]
use sha2::Sha256;
#[cfg(unix)]
use tokio::fs;
#[cfg(unix)]
use tokio::io::AsyncReadExt;
#[cfg(unix)]
use tokio::process::Command;

pub(crate) fn managed_codex_bin(codex_home: &Path) -> PathBuf {
    codex_home
        .join("packages")
        .join("standalone")
        .join("current")
        .join(managed_codex_file_name())
}

#[cfg(unix)]
pub(crate) async fn resolved_managed_codex_bin(codex_bin: &Path) -> Result<PathBuf> {
    fs::canonicalize(codex_bin).await.with_context(|| {
        format!(
            "failed to resolve managed Codex binary {}",
            codex_bin.display()
        )
    })
}

#[cfg(unix)]
pub(crate) async fn managed_codex_version(codex_bin: &Path) -> Result<String> {
    let output = Command::new(codex_bin)
        .arg("--version")
        .output()
        .await
        .with_context(|| {
            format!(
                "failed to invoke managed Codex binary {}",
                codex_bin.display()
            )
        })?;
    if !output.status.success() {
        return Err(anyhow!(
            "managed Codex binary {} exited with status {}",
            codex_bin.display(),
            output.status
        ));
    }

    let stdout = String::from_utf8(output.stdout).with_context(|| {
        format!(
            "managed Codex version was not utf-8: {}",
            codex_bin.display()
        )
    })?;
    parse_codex_version(&stdout)
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct ManagedSednaRelease {
    pub(crate) version: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct ManagedStandaloneRelease {
    pub(crate) release_dir: PathBuf,
    pub(crate) executable: PathBuf,
    pub(crate) sedna_auto_update: Option<ManagedSednaRelease>,
}

#[cfg(unix)]
#[derive(Debug, Deserialize)]
struct ManagedReleaseMetadata {
    release_tag: String,
    release_version: String,
    repository: String,
    target: String,
}

#[cfg(unix)]
pub(crate) async fn resolved_managed_standalone_release(
    codex_bin: &Path,
) -> Result<ManagedStandaloneRelease> {
    let standalone_root = codex_bin
        .parent()
        .and_then(Path::parent)
        .ok_or_else(|| anyhow!("managed Codex binary path has no standalone root"))?;
    let releases_root = fs::canonicalize(standalone_root.join("releases"))
        .await
        .context("failed to resolve managed standalone releases root")?;
    let executable = resolved_managed_codex_bin(codex_bin).await?;
    let release_dir = executable
        .parent()
        .ok_or_else(|| anyhow!("managed Codex executable has no release directory"))?
        .to_path_buf();
    if release_dir.parent() != Some(releases_root.as_path()) {
        return Err(anyhow!(
            "managed Codex executable {} is outside managed standalone releases root {}",
            executable.display(),
            releases_root.display()
        ));
    }

    let sedna_auto_update = verified_sedna_auto_update_release(&release_dir).await;
    let resolved_again = resolved_managed_codex_bin(codex_bin).await?;
    if resolved_again != executable {
        return Err(anyhow!(
            "managed Codex executable changed while validating release authority"
        ));
    }

    Ok(ManagedStandaloneRelease {
        release_dir,
        executable,
        sedna_auto_update,
    })
}

#[cfg(unix)]
async fn verified_sedna_auto_update_release(release_dir: &Path) -> Option<ManagedSednaRelease> {
    let metadata_path = release_dir.join("RELEASE-METADATA.json");
    let metadata = fs::read(&metadata_path).await.ok()?;
    let checksums = fs::read_to_string(release_dir.join("INSTALLED-SHA256SUMS.txt"))
        .await
        .ok()?;
    let executable_digest = sha256_file(&release_dir.join(managed_codex_file_name()))
        .await
        .ok()?;
    if !checksum_matches(&checksums, "RELEASE-METADATA.json", &metadata)
        || !checksum_matches_digest(&checksums, managed_codex_file_name(), executable_digest)
    {
        return None;
    }
    let metadata = serde_json::from_slice(&metadata).ok()?;
    managed_sedna_automatic_update_release_from_metadata(
        &metadata,
        std::env::consts::OS,
        std::env::consts::ARCH,
    )
}

#[cfg(unix)]
fn sha256_hex(contents: &[u8]) -> String {
    Sha256::digest(contents)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

#[cfg(unix)]
fn checksum_matches(checksums: &str, file_name: &str, contents: &[u8]) -> bool {
    let expected = checksums.lines().find_map(|line| {
        let mut fields = line.split_whitespace();
        let digest = fields.next()?;
        let candidate = fields.next()?.trim_start_matches('*');
        (candidate == file_name).then_some(digest)
    });
    expected.is_some_and(|expected| {
        let actual = sha256_hex(contents);
        expected.len() == 64
            && expected.bytes().all(|byte| byte.is_ascii_hexdigit())
            && expected.eq_ignore_ascii_case(&actual)
    })
}

#[cfg(unix)]
fn checksum_matches_digest(checksums: &str, file_name: &str, digest: [u8; 32]) -> bool {
    let expected = checksums.lines().find_map(|line| {
        let mut fields = line.split_whitespace();
        let digest = fields.next()?;
        let candidate = fields.next()?.trim_start_matches('*');
        (candidate == file_name).then_some(digest)
    });
    expected.is_some_and(|expected| {
        expected.len() == 64
            && expected.bytes().all(|byte| byte.is_ascii_hexdigit())
            && expected.eq_ignore_ascii_case(&digest_hex(&digest))
    })
}

#[cfg(unix)]
fn digest_hex(digest: &[u8; 32]) -> String {
    digest.iter().map(|byte| format!("{byte:02x}")).collect()
}

#[cfg(unix)]
async fn sha256_file(path: &Path) -> Result<[u8; 32]> {
    let mut file = fs::File::open(path)
        .await
        .with_context(|| format!("failed to read file {}", path.display()))?;
    let mut hasher = Sha256::new();
    let mut buffer = [0u8; 64 * 1024];
    loop {
        let read = file
            .read(&mut buffer)
            .await
            .with_context(|| format!("failed to read file {}", path.display()))?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
    }
    Ok(hasher.finalize().into())
}

#[cfg(unix)]
fn managed_sedna_automatic_update_release_from_metadata(
    metadata: &ManagedReleaseMetadata,
    target_os: &str,
    target_arch: &str,
) -> Option<ManagedSednaRelease> {
    let metadata_version = codex_utils_version::parse_sedna_release_tag(&metadata.release_tag)?;
    (metadata.repository == codex_utils_version::SEDNA_RELEASE_REPOSITORY
        && metadata_version == metadata.release_version
        && metadata.target == expected_sedna_standalone_target(target_os, target_arch)?
        && codex_utils_version::is_sedna_managed_release(
            &metadata.release_version,
            target_os,
            target_arch,
        ))
    .then(|| ManagedSednaRelease {
        version: metadata.release_version.clone(),
    })
}

#[cfg(unix)]
fn expected_sedna_standalone_target(target_os: &str, target_arch: &str) -> Option<&'static str> {
    match (target_os, target_arch) {
        ("linux", "x86_64") => Some("x86_64-unknown-linux-gnu"),
        ("linux", "aarch64") => Some("aarch64-unknown-linux-gnu"),
        _ => None,
    }
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub(crate) struct ExecutableIdentity {
    digest: [u8; 32],
}

#[cfg(unix)]
pub(crate) async fn executable_identity(executable: &Path) -> Result<ExecutableIdentity> {
    Ok(ExecutableIdentity {
        digest: sha256_file(executable)
            .await
            .with_context(|| format!("failed to read executable {}", executable.display()))?,
    })
}

#[cfg(all(test, unix))]
pub(crate) fn executable_identity_from_bytes(bytes: &[u8]) -> ExecutableIdentity {
    ExecutableIdentity {
        digest: Sha256::digest(bytes).into(),
    }
}

fn managed_codex_file_name() -> &'static str {
    if cfg!(windows) { "codex.exe" } else { "codex" }
}

#[cfg(unix)]
fn parse_codex_version(output: &str) -> Result<String> {
    let version = output
        .split_whitespace()
        .nth(1)
        .filter(|version| !version.is_empty())
        .ok_or_else(|| anyhow!("managed Codex version output was malformed"))?;
    Ok(version.to_string())
}

#[cfg(all(test, unix))]
#[path = "managed_install_tests.rs"]
mod tests;
