#[cfg(unix)]
use std::process::Command as StdCommand;
#[cfg(unix)]
use std::process::Stdio;
#[cfg(unix)]
use std::time::Duration;

#[cfg(unix)]
use anyhow::Context;
use anyhow::Result;
#[cfg(not(unix))]
use anyhow::bail;
#[cfg(unix)]
use codex_http_client::ClientRouteClass;
use codex_http_client::HttpClientFactory;
#[cfg(unix)]
use codex_http_client::RouteAwareClientPool;
#[cfg(unix)]
use futures::FutureExt;
#[cfg(unix)]
use std::os::unix::process::CommandExt;
#[cfg(unix)]
use tokio::io::AsyncWriteExt;
#[cfg(unix)]
use tokio::process::Command;
#[cfg(unix)]
use tokio::signal::unix::Signal;
#[cfg(unix)]
use tokio::signal::unix::SignalKind;
#[cfg(unix)]
use tokio::signal::unix::signal;
#[cfg(unix)]
use tokio::time::sleep;

#[cfg(unix)]
use crate::Daemon;
#[cfg(unix)]
use crate::RestartIfRunningOutcome;
#[cfg(unix)]
use crate::RestartMode;
#[cfg(unix)]
use crate::UpdaterRefreshMode;
#[cfg(unix)]
use crate::managed_install::ExecutableIdentity;
#[cfg(unix)]
use crate::managed_install::executable_identity;
#[cfg(unix)]
use crate::managed_install::resolved_managed_standalone_release;

#[cfg(unix)]
const INITIAL_UPDATE_DELAY: Duration = Duration::from_secs(5 * 60);
#[cfg(unix)]
const RESTART_RETRY_INTERVAL: Duration = Duration::from_millis(50);
#[cfg(unix)]
const UPDATE_INTERVAL: Duration = Duration::from_secs(60 * 60);
#[cfg(unix)]
const SEDNA_STANDALONE_INSTALLER_URL: &str =
    "https://raw.githubusercontent.com/sednalabs/codex/main/scripts/install_sedna_release_asset";

#[cfg(unix)]
pub(crate) async fn run(http_client_factory: HttpClientFactory) -> Result<()> {
    let mut terminate =
        signal(SignalKind::terminate()).context("failed to install updater shutdown handler")?;
    let running_updater_identity = current_updater_identity().await?;
    let http = RouteAwareClientPool::new_without_request_logging(
        http_client_factory,
        ClientRouteClass::Other,
    );
    if sleep_or_terminate(INITIAL_UPDATE_DELAY, &mut terminate).await {
        return Ok(());
    }
    loop {
        match update_once(&http, &running_updater_identity, &mut terminate).await {
            Ok(UpdateLoopControl::Continue) | Err(_) => {}
            Ok(UpdateLoopControl::Stop) => return Ok(()),
        }
        if sleep_or_terminate(UPDATE_INTERVAL, &mut terminate).await {
            return Ok(());
        }
    }
}

#[cfg(not(unix))]
pub(crate) async fn run(_http_client_factory: HttpClientFactory) -> Result<()> {
    bail!("pid-managed updater loop is unsupported on this platform")
}

#[cfg(unix)]
async fn sleep_or_terminate(duration: Duration, terminate: &mut Signal) -> bool {
    tokio::select! {
        _ = sleep(duration) => false,
        _ = terminate.recv() => true,
    }
}

#[cfg(unix)]
enum UpdateLoopControl {
    Continue,
    Stop,
}

#[cfg(unix)]
async fn update_once(
    http: &RouteAwareClientPool,
    running_updater_identity: &ExecutableIdentity,
    terminate: &mut Signal,
) -> Result<UpdateLoopControl> {
    let daemon = Daemon::from_environment()?;
    let managed_release = resolved_managed_standalone_release(&daemon.managed_codex_bin).await?;
    let Some(managed_sedna_release) = managed_release.sedna_auto_update else {
        // A manually selected or otherwise ineligible current release must not
        // leave a previously eligible updater alive. Reconcile it against this
        // validated release without attempting an automatic network update.
        let settings = daemon.load_settings().await?;
        daemon
            .reconcile_updater(&settings, &managed_release)
            .await?;
        return Ok(UpdateLoopControl::Continue);
    };
    let installed_from_version = managed_sedna_release.version;

    // Reconcile an already-activated release before asking the installer for a
    // newer one. In particular, an equality/no-update installer result must
    // not leave an A updater or app server running after current moved to B.
    if let UpdateLoopControl::Stop = reconcile_running_processes_to_managed_release(
        &daemon,
        running_updater_identity,
        &managed_release.executable,
        terminate,
    )
    .await?
    {
        return Ok(UpdateLoopControl::Stop);
    }

    install_latest_standalone(http, &installed_from_version).await?;

    let managed_release = resolved_managed_standalone_release(&daemon.managed_codex_bin).await?;
    let Some(installed_release) = managed_release.sedna_auto_update else {
        let settings = daemon.load_settings().await?;
        daemon
            .reconcile_updater(&settings, &managed_release)
            .await?;
        return Err(anyhow::anyhow!(
            "managed release is no longer eligible for automatic updates after installation"
        ));
    };
    if !post_install_release_is_strictly_newer(&installed_from_version, &installed_release.version)
    {
        if let UpdateLoopControl::Stop = reconcile_running_processes_to_managed_release(
            &daemon,
            running_updater_identity,
            &managed_release.executable,
            terminate,
        )
        .await?
        {
            return Ok(UpdateLoopControl::Stop);
        }
        return Err(anyhow::anyhow!(
            "managed release after installation was not strictly newer than the release selected for update"
        ));
    }
    reconcile_running_processes_to_managed_release(
        &daemon,
        running_updater_identity,
        &managed_release.executable,
        terminate,
    )
    .await
}

#[cfg(unix)]
async fn reconcile_running_processes_to_managed_release(
    daemon: &Daemon,
    running_updater_identity: &ExecutableIdentity,
    managed_codex_bin: &std::path::Path,
    terminate: &mut Signal,
) -> Result<UpdateLoopControl> {
    let managed_identity = executable_identity(managed_codex_bin).await?;
    let (restart_mode, updater_refresh_mode) =
        update_modes_for_identities(running_updater_identity, &managed_identity);

    loop {
        if terminate.recv().now_or_never().flatten().is_some() {
            return Ok(UpdateLoopControl::Stop);
        }
        match daemon
            .try_restart_if_running(restart_mode, updater_refresh_mode, managed_codex_bin)
            .await?
        {
            RestartIfRunningOutcome::Busy => {
                if sleep_or_terminate(RESTART_RETRY_INTERVAL, terminate).await {
                    return Ok(UpdateLoopControl::Stop);
                }
            }
            _ => return Ok(UpdateLoopControl::Continue),
        }
    }
}

#[cfg(unix)]
async fn current_updater_identity() -> Result<ExecutableIdentity> {
    let current_exe =
        std::env::current_exe().context("failed to resolve current updater executable")?;
    executable_identity(&current_exe).await
}

#[cfg(unix)]
fn update_modes_for_identities(
    running_updater_identity: &ExecutableIdentity,
    managed_identity: &ExecutableIdentity,
) -> (RestartMode, UpdaterRefreshMode) {
    if running_updater_identity == managed_identity {
        (RestartMode::IfVersionChanged, UpdaterRefreshMode::None)
    } else {
        (
            RestartMode::Always,
            UpdaterRefreshMode::ReexecIfManagedBinaryChanged,
        )
    }
}

#[cfg(unix)]
fn post_install_release_is_strictly_newer(
    installed_from_version: &str,
    installed_release_version: &str,
) -> bool {
    codex_utils_version::is_newer_sedna_release(installed_release_version, installed_from_version)
        .unwrap_or(false)
}

#[cfg(unix)]
pub(crate) fn reexec_managed_updater(managed_codex_bin: &std::path::Path) -> Result<()> {
    let err = StdCommand::new(managed_codex_bin)
        .args(["app-server", "daemon", "pid-update-loop"])
        .exec();
    Err(err).with_context(|| {
        format!(
            "failed to replace updater with managed Codex binary {}",
            managed_codex_bin.display()
        )
    })
}

#[cfg(unix)]
async fn install_latest_standalone(
    http: &RouteAwareClientPool,
    managed_release_version: &str,
) -> Result<()> {
    install_latest_sedna_standalone(http, managed_release_version).await
}

#[cfg(unix)]
async fn install_latest_sedna_standalone(
    http: &impl InstallerHttp,
    current_release_version: &str,
) -> Result<()> {
    let script = fetch_installer_script_from_url(http, SEDNA_STANDALONE_INSTALLER_URL).await?;

    let mut child = Command::new("bash")
        .args([
            "-s",
            "--",
            "--repository",
            "sednalabs/codex",
            "--release-tag",
            "latest",
        ])
        .args(["--require-newer-than", current_release_version])
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::inherit())
        .spawn()
        .context("failed to invoke Sedna standalone Codex updater")?;
    let mut stdin = child
        .stdin
        .take()
        .context("Sedna standalone Codex updater stdin was unavailable")?;
    stdin
        .write_all(&script)
        .await
        .context("failed to pass Sedna standalone Codex updater to shell")?;
    drop(stdin);
    let status = child
        .wait()
        .await
        .context("failed to wait for Sedna standalone Codex updater")?;

    if status.success() {
        Ok(())
    } else {
        anyhow::bail!("Sedna standalone Codex updater exited with status {status}")
    }
}

#[cfg(unix)]
async fn fetch_installer_script_from_url(http: &impl InstallerHttp, url: &str) -> Result<Vec<u8>> {
    match http.get(url).await? {
        InstallerResponse::Success(body) => Ok(body),
        InstallerResponse::Unsuccessful { status } => {
            anyhow::bail!("standalone Codex updater request failed with status {status}")
        }
    }
}

#[cfg(unix)]
#[derive(Clone, Debug, PartialEq, Eq)]
enum InstallerResponse {
    Success(Vec<u8>),
    Unsuccessful { status: u16 },
}

#[cfg(unix)]
/// HTTP boundary used to download the standalone installer.
///
/// Implementations must issue a GET for the supplied URL, return exact response bytes for a
/// successful status, and report a non-success status without buffering its response body.
trait InstallerHttp: Send + Sync {
    fn get<'a>(
        &'a self,
        url: &'a str,
    ) -> impl std::future::Future<Output = Result<InstallerResponse>> + Send + 'a;
}

#[cfg(unix)]
impl InstallerHttp for RouteAwareClientPool {
    async fn get(&self, url: &str) -> Result<InstallerResponse> {
        let response = RouteAwareClientPool::get(self, url)
            .send()
            .await
            .context("failed to fetch standalone Codex updater")?;
        if !response.status().is_success() {
            return Ok(InstallerResponse::Unsuccessful {
                status: response.status().as_u16(),
            });
        }
        let body = response
            .bytes()
            .await
            .context("failed to read standalone Codex updater")?
            .to_vec();
        Ok(InstallerResponse::Success(body))
    }
}

#[cfg(all(test, unix))]
#[path = "update_loop_tests.rs"]
mod tests;
