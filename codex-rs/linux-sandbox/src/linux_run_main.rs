use clap::Parser;
use std::ffi::CString;
use std::fmt;
use std::fs;
use std::fs::File;
use std::fs::OpenOptions;
use std::io::Read;
use std::io::Write;
use std::os::fd::AsRawFd;
use std::os::fd::FromRawFd;
use std::os::fd::OwnedFd;
use std::os::fd::RawFd;
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::OpenOptionsExt;
use std::os::unix::fs::PermissionsExt;
use std::path::Component;
use std::path::Path;
use std::path::PathBuf;
use std::sync::Arc;
use std::sync::OnceLock;
use std::sync::atomic::AtomicBool;
use std::sync::atomic::AtomicI32;
use std::sync::atomic::Ordering;
use std::thread;
use std::time::Duration;

use crate::bwrap::BwrapNetworkMode;
use crate::bwrap::BwrapOptions;
use crate::bwrap::BwrapProcessLifetime;
use crate::bwrap::create_bwrap_command_args;
use crate::bwrap_output_relay::BwrapOutputRelay;
use crate::landlock::apply_permission_profile_to_current_thread;
use crate::launcher::exec_bwrap;
use crate::launcher::preferred_bwrap_supports_argv0;
use crate::proxy_routing::activate_proxy_routes_in_netns;
use crate::proxy_routing::prepare_host_proxy_route_spec;
use codex_protocol::error::Result as CodexResult;
use codex_protocol::models::PermissionProfile;
use codex_protocol::permissions::FileSystemAccessMode;
use codex_protocol::permissions::FileSystemPath;
use codex_protocol::permissions::FileSystemSandboxEntry;
use codex_protocol::permissions::FileSystemSpecialPath;
use codex_protocol::protocol::FileSystemSandboxPolicy;
use codex_protocol::protocol::NetworkSandboxPolicy;
use codex_sandboxing::landlock::CODEX_LINUX_SANDBOX_ARG0;
use codex_utils_absolute_path::AbsolutePathBuf;

static BWRAP_CHILD_PID: AtomicI32 = AtomicI32::new(0);
static PENDING_FORWARDED_SIGNAL: AtomicI32 = AtomicI32::new(0);
#[cfg(test)]
static FORCE_DETACHED_MONITOR_EXIT_AFTER_READY: AtomicBool = AtomicBool::new(false);

const FORWARDED_SIGNALS: &[libc::c_int] =
    &[libc::SIGHUP, libc::SIGINT, libc::SIGQUIT, libc::SIGTERM];
const SYNTHETIC_MOUNT_MARKER_SYNTHETIC: &[u8] = b"synthetic\n";
const SYNTHETIC_MOUNT_MARKER_EXISTING: &[u8] = b"existing\n";
const PROTECTED_CREATE_MARKER: &[u8] = b"protected-create\n";

#[derive(Debug)]
struct SyntheticMountTargetRegistration {
    target: crate::bwrap::SyntheticMountTarget,
    marker_file: PathBuf,
    marker_dir: PathBuf,
}

#[derive(Debug)]
struct ProtectedCreateTargetRegistration {
    target: crate::bwrap::ProtectedCreateTarget,
    marker_file: PathBuf,
    marker_dir: PathBuf,
}

struct ProtectedCreateMonitor {
    stop: Arc<AtomicBool>,
    violation: Arc<AtomicBool>,
    handle: thread::JoinHandle<()>,
}

struct BwrapNamespaceInit {
    pid: libc::pid_t,
    pidfd: OwnedFd,
}

struct ProtectedCreateWatcher {
    fd: libc::c_int,
    _watches: Vec<libc::c_int>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum ProtectedCreateRemoval {
    Directory,
    Other,
}

#[derive(Debug, Parser)]
/// CLI surface for the Linux sandbox helper.
///
/// The type name remains `LandlockCommand` for compatibility with existing
/// wiring, but bubblewrap is now the default filesystem sandbox and Landlock
/// is the legacy fallback.
pub struct LandlockCommand {
    /// It is possible that the cwd used in the context of the sandbox policy
    /// is different from the cwd of the process to spawn.
    #[arg(long = "sandbox-policy-cwd")]
    pub sandbox_policy_cwd: PathBuf,

    /// The logical working directory for the command being sandboxed.
    ///
    /// This can intentionally differ from `sandbox_policy_cwd` when the
    /// command runs from a symlinked alias of the policy workspace. Keep it
    /// explicit so bubblewrap can preserve the caller's logical cwd when that
    /// alias would otherwise disappear inside the sandbox namespace.
    #[arg(long = "command-cwd", hide = true)]
    pub command_cwd: Option<PathBuf>,

    /// Canonical runtime permissions for the command.
    #[arg(
        long = "permission-profile",
        hide = true,
        value_parser = parse_permission_profile
    )]
    pub permission_profile: Option<PermissionProfile>,

    /// Opt-in: use the legacy Landlock Linux sandbox fallback.
    ///
    /// When not set, the helper uses the default bubblewrap pipeline.
    #[arg(long = "use-legacy-landlock", hide = true, default_value_t = false)]
    pub use_legacy_landlock: bool,

    /// Internal: apply seccomp and `no_new_privs` in the already-sandboxed
    /// process, then exec the user command.
    ///
    /// This exists so we can run bubblewrap first (which may rely on setuid)
    /// and only tighten with seccomp after the filesystem view is established.
    #[arg(long = "apply-seccomp-then-exec", hide = true, default_value_t = false)]
    pub apply_seccomp_then_exec: bool,

    /// Internal compatibility flag.
    ///
    /// By default, restricted-network sandboxing uses isolated networking.
    /// If set, sandbox setup switches to proxy-only network mode with
    /// managed routing bridges.
    #[arg(long = "allow-network-for-proxy", hide = true, default_value_t = false)]
    pub allow_network_for_proxy: bool,

    /// Internal: permit intentionally detached command descendants to outlive
    /// the one-shot sandbox helper invocation.
    #[arg(long = "allow-detached-children", hide = true, default_value_t = false)]
    pub allow_detached_children: bool,

    /// Internal write end of the one-shot command-status channel. The
    /// namespace reaper reports the initial command before it continues
    /// reaping intentionally detached descendants.
    #[arg(long = "detached-status-fd", hide = true)]
    pub detached_status_fd: Option<libc::c_int>,

    /// Internal route spec used for managed proxy routing in bwrap mode.
    #[arg(long = "proxy-route-spec", hide = true)]
    pub proxy_route_spec: Option<String>,

    /// When set, skip mounting a fresh `/proc` even though PID isolation is
    /// still enabled. This is primarily intended for restrictive container
    /// environments that deny `--proc /proc`.
    #[arg(long = "no-proc", default_value_t = false)]
    pub no_proc: bool,

    /// Full command args to run under the Linux sandbox helper.
    #[arg(trailing_var_arg = true)]
    pub command: Vec<String>,
}

/// Entry point for the Linux sandbox helper.
///
/// The sequence is:
/// 1. When needed, wrap the command with bubblewrap to construct the
///    filesystem view.
/// 2. Apply in-process restrictions (no_new_privs + seccomp).
/// 3. `execvp` into the final command.
pub fn run_main() -> ! {
    let LandlockCommand {
        sandbox_policy_cwd,
        command_cwd,
        permission_profile,
        use_legacy_landlock,
        apply_seccomp_then_exec,
        allow_network_for_proxy,
        allow_detached_children,
        detached_status_fd,
        proxy_route_spec,
        no_proc,
        command,
    } = LandlockCommand::parse();

    if command.is_empty() {
        panic!("No command specified to execute.");
    }
    ensure_inner_stage_mode_is_valid(apply_seccomp_then_exec, use_legacy_landlock);
    let EffectivePermissions {
        permission_profile,
        mut file_system_sandbox_policy,
        network_sandbox_policy,
    } = resolve_permission_profile(permission_profile).unwrap_or_else(|err| panic!("{err}"));
    ensure_legacy_landlock_mode_supports_policy(
        use_legacy_landlock,
        &file_system_sandbox_policy,
        network_sandbox_policy,
        &sandbox_policy_cwd,
    );

    // Inner stage: apply seccomp/no_new_privs after bubblewrap has already
    // established the filesystem view.
    if apply_seccomp_then_exec {
        if allow_network_for_proxy {
            let spec = proxy_route_spec
                .as_deref()
                .unwrap_or_else(|| panic!("managed proxy mode requires --proxy-route-spec"));
            if let Err(err) = activate_proxy_routes_in_netns(spec) {
                panic!("error activating Linux proxy routing bridge: {err}");
            }
        }
        let proxy_routing_active = allow_network_for_proxy;
        if let Err(e) = apply_permission_profile_to_current_thread(
            &permission_profile,
            &sandbox_policy_cwd,
            /*apply_landlock_fs*/ false,
            allow_network_for_proxy,
            proxy_routing_active,
        ) {
            panic!("error applying Linux sandbox restrictions: {e:?}");
        }

        // Preserve the historical direct-exec path unless this caller
        // explicitly requested detached-child custody. The reaper/fork path
        // is only needed to report the initial status before continuing to
        // supervise descendants.
        if !allow_detached_children {
            exec_or_panic(command);
        }

        let signal_mask = ForwardedSignalMask::block();
        let command_pid = unsafe { libc::fork() };
        if command_pid < 0 {
            let err = std::io::Error::last_os_error();
            panic!("failed to fork sandboxed command: {err}");
        }

        if command_pid == 0 {
            // The status pipe belongs solely to the namespace reaper. Closing
            // it before exec prevents the foreground command (or a detached
            // descendant) from forging the four-byte host result.
            if allow_detached_children {
                let status_fd = detached_status_fd.unwrap_or_else(|| {
                    panic!("detached namespace reaper is missing its command-status fd")
                });
                mark_fd_close_on_exec(status_fd);
            }
            reset_forwarded_signal_handlers_to_default();
            signal_mask.restore();
            exec_or_panic(command);
        }

        let signal_forwarders = install_bwrap_signal_forwarders(command_pid);
        signal_mask.restore();
        loop {
            let mut status = 0;
            let reaped_pid = unsafe { libc::waitpid(-1, &mut status, 0) };
            if reaped_pid == command_pid {
                if allow_detached_children {
                    let status_fd = detached_status_fd.unwrap_or_else(|| {
                        panic!("detached namespace reaper is missing its command-status fd")
                    });
                    mark_fd_close_on_exec(status_fd);
                    write_detached_command_status(status_fd, status);
                    continue;
                }
                let exit_signal_mask = ForwardedSignalMask::block();
                signal_forwarders.restore();
                exit_signal_mask.restore();
                exit_with_wait_status(status);
            }
            if reaped_pid >= 0 {
                continue;
            }

            let err = std::io::Error::last_os_error();
            if err.raw_os_error() == Some(libc::ECHILD) && allow_detached_children {
                // The original command status was reported before this loop
                // started reaping descendants. Once no descendants remain,
                // this PID-1 reaper may exit normally.
                std::process::exit(0);
            }
            if err.raw_os_error() != Some(libc::EINTR) {
                panic!("failed to reap sandboxed child: {err}");
            }
        }
    }

    if file_system_sandbox_policy.has_full_disk_write_access() && !allow_network_for_proxy {
        if let Err(e) = apply_permission_profile_to_current_thread(
            &permission_profile,
            &sandbox_policy_cwd,
            /*apply_landlock_fs*/ false,
            allow_network_for_proxy,
            /*proxy_routed_network*/ false,
        ) {
            panic!("error applying Linux sandbox restrictions: {e:?}");
        }
        exec_or_panic(command);
    }

    if !use_legacy_landlock {
        // Outer stage: bubblewrap first, then re-enter this binary in the
        // sandboxed environment to apply seccomp. This path never falls back
        // to legacy Landlock on failure.
        let proxy_route_spec = if allow_network_for_proxy {
            let (proxy_route_spec, socket_dir) = prepare_host_proxy_route_spec()
                .unwrap_or_else(|err| panic!("failed to prepare host proxy routing bridge: {err}"));
            file_system_sandbox_policy = file_system_sandbox_policy.with_additional_readable_roots(
                &sandbox_policy_cwd,
                std::slice::from_ref(&socket_dir),
            );
            Some(proxy_route_spec)
        } else {
            None
        };
        let inner = build_inner_seccomp_command(InnerSeccompCommandArgs {
            sandbox_policy_cwd: &sandbox_policy_cwd,
            command_cwd: command_cwd.as_deref(),
            permission_profile: &permission_profile,
            allow_network_for_proxy,
            allow_detached_children,
            proxy_route_spec,
            command,
        });
        let bwrap_file_system_sandbox_policy =
            file_system_policy_with_bwrap_bootstrap_roots(&file_system_sandbox_policy);
        run_bwrap_with_proc_fallback(
            &sandbox_policy_cwd,
            command_cwd.as_deref(),
            &bwrap_file_system_sandbox_policy,
            network_sandbox_policy,
            inner,
            !no_proc,
            allow_network_for_proxy,
            if allow_detached_children {
                BwrapProcessLifetime::AllowDetachedChildren
            } else {
                BwrapProcessLifetime::TerminateWithParent
            },
        );
    }

    // Legacy path: Landlock enforcement only, when bwrap sandboxing is not enabled.
    if let Err(e) = apply_permission_profile_to_current_thread(
        &permission_profile,
        &sandbox_policy_cwd,
        /*apply_landlock_fs*/ true,
        allow_network_for_proxy,
        /*proxy_routed_network*/ false,
    ) {
        panic!("error applying legacy Linux sandbox restrictions: {e:?}");
    }
    exec_or_panic(command);
}

#[derive(Debug, Clone)]
struct EffectivePermissions {
    permission_profile: PermissionProfile,
    file_system_sandbox_policy: FileSystemSandboxPolicy,
    network_sandbox_policy: NetworkSandboxPolicy,
}

#[derive(Debug, PartialEq, Eq)]
enum ResolvePermissionProfileError {
    MissingConfiguration,
}

impl fmt::Display for ResolvePermissionProfileError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::MissingConfiguration => write!(f, "missing permission profile configuration"),
        }
    }
}

fn parse_permission_profile(value: &str) -> std::result::Result<PermissionProfile, String> {
    serde_json::from_str(value).map_err(|err| format!("invalid permission profile JSON: {err}"))
}

fn file_system_policy_with_bwrap_bootstrap_roots(
    file_system_sandbox_policy: &FileSystemSandboxPolicy,
) -> FileSystemSandboxPolicy {
    if file_system_sandbox_policy.has_full_disk_read_access() {
        return file_system_sandbox_policy.clone();
    }

    let mut file_system_sandbox_policy = file_system_sandbox_policy.clone();
    if let Some(current_exe_parent) = current_exe_parent_readable_root() {
        file_system_sandbox_policy = file_system_sandbox_policy
            .with_additional_readable_roots(Path::new("/"), &[current_exe_parent]);
    }
    if !file_system_sandbox_policy.include_platform_defaults() {
        file_system_sandbox_policy
            .entries
            .push(FileSystemSandboxEntry::new(
                FileSystemPath::Special {
                    value: FileSystemSpecialPath::Minimal,
                },
                FileSystemAccessMode::Read,
            ));
    }
    file_system_sandbox_policy
}

fn current_exe_parent_readable_root() -> Option<AbsolutePathBuf> {
    let current_exe = std::env::current_exe().ok()?;
    let current_exe = AbsolutePathBuf::from_absolute_path(&current_exe).ok()?;
    current_exe.parent()
}

fn resolve_permission_profile(
    permission_profile: Option<PermissionProfile>,
) -> Result<EffectivePermissions, ResolvePermissionProfileError> {
    let permission_profile =
        permission_profile.ok_or(ResolvePermissionProfileError::MissingConfiguration)?;
    let (file_system_sandbox_policy, network_sandbox_policy) =
        permission_profile.to_runtime_permissions();
    Ok(EffectivePermissions {
        permission_profile,
        file_system_sandbox_policy,
        network_sandbox_policy,
    })
}

fn ensure_inner_stage_mode_is_valid(apply_seccomp_then_exec: bool, use_legacy_landlock: bool) {
    if apply_seccomp_then_exec && use_legacy_landlock {
        panic!("--apply-seccomp-then-exec is incompatible with --use-legacy-landlock");
    }
}

fn ensure_legacy_landlock_mode_supports_policy(
    use_legacy_landlock: bool,
    file_system_sandbox_policy: &FileSystemSandboxPolicy,
    network_sandbox_policy: NetworkSandboxPolicy,
    sandbox_policy_cwd: &Path,
) {
    if use_legacy_landlock
        && file_system_sandbox_policy
            .needs_direct_runtime_enforcement(network_sandbox_policy, sandbox_policy_cwd)
    {
        panic!(
            "permission profiles requiring direct runtime enforcement are incompatible with --use-legacy-landlock"
        );
    }
}

#[expect(clippy::too_many_arguments)]
fn run_bwrap_with_proc_fallback(
    sandbox_policy_cwd: &Path,
    command_cwd: Option<&Path>,
    file_system_sandbox_policy: &FileSystemSandboxPolicy,
    network_sandbox_policy: NetworkSandboxPolicy,
    inner: Vec<String>,
    mount_proc: bool,
    allow_network_for_proxy: bool,
    process_lifetime: BwrapProcessLifetime,
) -> ! {
    let mut network_mode = bwrap_network_mode(network_sandbox_policy, allow_network_for_proxy);
    let mut mount_proc = mount_proc;
    let command_cwd = command_cwd.unwrap_or(sandbox_policy_cwd);

    if mount_proc
        && !preflight_proc_mount_support(network_mode)
            .unwrap_or_else(|err| exit_with_bwrap_build_error(err))
    {
        // Keep the retry silent so sandbox-internal diagnostics do not leak into the
        // child process stderr stream.
        mount_proc = false;
    }

    if network_mode.should_unshare_network()
        && !preflight_network_namespace_support(network_mode, mount_proc)
            .unwrap_or_else(|err| exit_with_bwrap_build_error(err))
    {
        // Some constrained hosts deny loopback setup in an unshared netns.
        // Fall back to a shared netns; inner-stage seccomp still enforces
        // network policy semantics for restricted-network turns.
        network_mode = BwrapNetworkMode::FullAccess;
    }

    let options = BwrapOptions {
        mount_proc,
        network_mode,
        process_lifetime,
        ..Default::default()
    };
    let mut bwrap_args = build_bwrap_argv(
        inner,
        file_system_sandbox_policy,
        sandbox_policy_cwd,
        command_cwd,
        options,
    )
    .unwrap_or_else(|err| exit_with_bwrap_build_error(err));
    apply_inner_command_argv0(&mut bwrap_args.args);
    if process_lifetime == BwrapProcessLifetime::AllowDetachedChildren {
        let status_pipe = create_detached_command_status_pipe();
        let command_separator = bwrap_args
            .args
            .iter()
            .rposition(|arg| arg == "--")
            .unwrap_or_else(|| panic!("bubblewrap argv is missing inner command separator '--'"));
        bwrap_args.args.splice(
            command_separator..command_separator,
            [
                "--detached-status-fd".to_string(),
                status_pipe[1].to_string(),
            ],
        );
        // `exec_bwrap` makes preserved descriptors inheritable for the outer
        // exec. The inner reaper resets close-on-exec before it forks the user
        // command, so the status writer never leaks into that command.
        bwrap_args
            .preserved_files
            .push(unsafe { File::from_raw_fd(status_pipe[1]) });
        run_bwrap_in_child_with_synthetic_mount_cleanup(bwrap_args, status_pipe[0]);
    }
    run_or_exec_bwrap(bwrap_args);
}

fn bwrap_network_mode(
    network_sandbox_policy: NetworkSandboxPolicy,
    allow_network_for_proxy: bool,
) -> BwrapNetworkMode {
    if allow_network_for_proxy {
        BwrapNetworkMode::ProxyOnly
    } else if network_sandbox_policy.is_enabled() {
        BwrapNetworkMode::FullAccess
    } else {
        BwrapNetworkMode::Isolated
    }
}

fn build_bwrap_argv(
    inner: Vec<String>,
    file_system_sandbox_policy: &FileSystemSandboxPolicy,
    sandbox_policy_cwd: &Path,
    command_cwd: &Path,
    options: BwrapOptions,
) -> CodexResult<crate::bwrap::BwrapArgs> {
    let bwrap_args = create_bwrap_command_args(
        inner,
        file_system_sandbox_policy,
        sandbox_policy_cwd,
        command_cwd,
        options,
    )?;

    let mut argv = vec!["bwrap".to_string()];
    argv.extend(bwrap_args.args);
    Ok(crate::bwrap::BwrapArgs {
        args: argv,
        preserved_files: bwrap_args.preserved_files,
        synthetic_mount_targets: bwrap_args.synthetic_mount_targets,
        protected_create_targets: bwrap_args.protected_create_targets,
        process_lifetime: bwrap_args.process_lifetime,
    })
}

fn exit_with_bwrap_build_error(err: codex_protocol::error::CodexErr) -> ! {
    eprintln!("error building bubblewrap command: {err}");
    std::process::exit(1);
}

fn apply_inner_command_argv0(argv: &mut Vec<String>) {
    apply_inner_command_argv0_for_launcher(
        argv,
        preferred_bwrap_supports_argv0(),
        current_process_argv0(),
    );
}

fn apply_inner_command_argv0_for_launcher(
    argv: &mut Vec<String>,
    supports_argv0: bool,
    argv0_fallback_command: String,
) {
    let command_separator_index = argv
        .iter()
        .position(|arg| arg == "--")
        .unwrap_or_else(|| panic!("bubblewrap argv is missing command separator '--'"));

    if supports_argv0 {
        argv.splice(
            command_separator_index..command_separator_index,
            ["--argv0".to_string(), CODEX_LINUX_SANDBOX_ARG0.to_string()],
        );
        return;
    }

    let command_index = command_separator_index + 1;
    let Some(command) = argv.get_mut(command_index) else {
        panic!("bubblewrap argv is missing inner command after '--'");
    };
    *command = argv0_fallback_command;
}

fn current_process_argv0() -> String {
    match std::env::args_os().next() {
        Some(argv0) => argv0.to_string_lossy().into_owned(),
        None => panic!("failed to resolve current process argv[0]"),
    }
}

fn preflight_proc_mount_support(network_mode: BwrapNetworkMode) -> CodexResult<bool> {
    let preflight_argv = build_preflight_bwrap_argv(network_mode, /*mount_proc*/ true)?;
    let output = run_bwrap_in_child_capture_output(preflight_argv);
    Ok(!is_proc_mount_failure(output.as_str()))
}

fn preflight_network_namespace_support(
    network_mode: BwrapNetworkMode,
    mount_proc: bool,
) -> CodexResult<bool> {
    let preflight_argv = build_preflight_bwrap_argv(network_mode, mount_proc)?;
    let output = run_bwrap_in_child_capture_output(preflight_argv);
    Ok(!is_loopback_setup_failure(output.as_str()))
}

fn build_preflight_bwrap_argv(
    network_mode: BwrapNetworkMode,
    mount_proc: bool,
) -> CodexResult<crate::bwrap::BwrapArgs> {
    let file_system_sandbox_policy =
        FileSystemSandboxPolicy::restricted(vec![FileSystemSandboxEntry {
            path: FileSystemPath::Special {
                value: FileSystemSpecialPath::Minimal,
            },
            access: FileSystemAccessMode::Read,
            missing_path_behavior: None,
        }]);
    let preflight_command = vec![resolve_true_command()];
    build_bwrap_argv(
        preflight_command,
        &file_system_sandbox_policy,
        Path::new("/"),
        Path::new("/"),
        BwrapOptions {
            mount_proc,
            network_mode,
            ..Default::default()
        },
    )
}

fn resolve_true_command() -> String {
    for candidate in ["/usr/bin/true", "/bin/true"] {
        if Path::new(candidate).exists() {
            return candidate.to_string();
        }
    }
    "true".to_string()
}

fn run_or_exec_bwrap(bwrap_args: crate::bwrap::BwrapArgs) -> ! {
    if bwrap_args.process_lifetime == BwrapProcessLifetime::TerminateWithParent
        && bwrap_args.synthetic_mount_targets.is_empty()
        && bwrap_args.protected_create_targets.is_empty()
    {
        exec_bwrap(bwrap_args.args, bwrap_args.preserved_files);
    }
    run_bwrap_in_child_with_synthetic_mount_cleanup(
        bwrap_args, /*detached_status_read_fd*/ -1,
    );
}

fn run_bwrap_in_child_with_synthetic_mount_cleanup(
    bwrap_args: crate::bwrap::BwrapArgs,
    detached_status_read_fd: libc::c_int,
) -> ! {
    let crate::bwrap::BwrapArgs {
        mut args,
        mut preserved_files,
        synthetic_mount_targets,
        protected_create_targets,
        process_lifetime,
    } = bwrap_args;
    let setup_signal_mask = ForwardedSignalMask::block();
    let synthetic_mount_registrations = register_synthetic_mount_targets(&synthetic_mount_targets);
    let protected_create_registrations =
        register_protected_create_targets(&protected_create_targets);
    let exec_start_pipe = create_exec_start_pipe(!protected_create_targets.is_empty());
    // Detached execution always needs a status handoff from the namespace
    // reaper. Protected targets additionally need the namespace identity so
    // the monitor can retain custody after this launcher returns.
    let detached_reaper_required = process_lifetime == BwrapProcessLifetime::AllowDetachedChildren;
    let bwrap_info_pipe = create_bwrap_info_pipe(detached_reaper_required);
    if bwrap_info_pipe[1] >= 0 {
        args.insert(1, bwrap_info_pipe[1].to_string());
        args.insert(1, "--info-fd".to_string());
        // The info pipe must survive the system-bwrap exec, but must not leak
        // into unrelated descendants. The launcher makes this exact fd
        // inheritable for the exec boundary and the child closes its read end.
        preserved_files.push(unsafe { File::from_raw_fd(bwrap_info_pipe[1]) });
    }
    let mut output_relay = (process_lifetime == BwrapProcessLifetime::AllowDetachedChildren)
        .then(BwrapOutputRelay::new);
    let parent_pid = unsafe { libc::getpid() };
    let pid = unsafe { libc::fork() };
    if pid < 0 {
        let err = std::io::Error::last_os_error();
        panic!("failed to fork for bubblewrap: {err}");
    }

    if pid == 0 {
        close_fd_if_open(bwrap_info_pipe[0]);
        reset_forwarded_signal_handlers_to_default();
        setup_signal_mask.restore();
        if let Some(output_relay) = output_relay {
            output_relay.redirect_child_output();
        }
        let setpgid_res = unsafe { libc::setpgid(0, 0) };
        if setpgid_res < 0 {
            let err = std::io::Error::last_os_error();
            panic!("failed to place bubblewrap child in its own process group: {err}");
        }
        terminate_with_parent(parent_pid);
        wait_for_parent_exec_start(exec_start_pipe[0], exec_start_pipe[1]);
        exec_bwrap(args, preserved_files);
    }

    // `preserved_files` contains the parent-owned duplicates of the status and
    // info write ends. Drop them before reading so an early bwrap failure is
    // observable as EOF instead of an indefinite wait.
    drop(preserved_files);
    close_child_exec_start_read(exec_start_pipe[0]);
    let protected_create_monitor = ProtectedCreateMonitor::start(&protected_create_targets);
    let signal_forwarders = install_bwrap_signal_forwarders(pid);
    release_child_exec_start(exec_start_pipe[1]);
    // Detached execution must drain foreground output while it waits for the
    // initial status record. Otherwise a command that fills stdout/stderr can
    // block the namespace reaper before it reports status. The detached
    // thread is intentionally not joined: its lifetime may include orphaned
    // descendants, while this launcher returns as soon as custody is handed
    // off.
    let _detached_output_drain = if detached_reaper_required {
        output_relay
            .take()
            .map(|relay| thread::spawn(move || relay.forward_until_eof()))
    } else {
        None
    };
    setup_signal_mask.restore();
    let namespace_init = read_bwrap_namespace_init(bwrap_info_pipe[0], pid);
    if detached_reaper_required {
        let initial_status = read_detached_command_status(detached_status_read_fd, pid);
        let cleanup_signal_mask = ForwardedSignalMask::block();
        let protected_create_monitor_violation = protected_create_monitor
            .map(ProtectedCreateMonitor::stop)
            .unwrap_or(false);
        cleanup_synthetic_mount_targets(&synthetic_mount_registrations);
        let protected_create_violation = if protected_create_targets.is_empty() {
            protected_create_monitor_violation
                || cleanup_protected_create_targets(&protected_create_registrations)
        } else if let Some(namespace_init) = namespace_init
            && !namespace_init.has_exited()
        {
            hand_off_protected_create_monitor(
                namespace_init,
                protected_create_targets,
                protected_create_registrations,
                &cleanup_signal_mask,
            );
            protected_create_monitor_violation
        } else {
            protected_create_monitor_violation
                || cleanup_protected_create_targets(&protected_create_registrations)
        };
        signal_forwarders.restore();
        cleanup_signal_mask.restore();
        exit_with_wait_status_or_policy_violation(initial_status, protected_create_violation);
    }
    let status = match output_relay {
        Some(output_relay) => output_relay.forward_until_child_exit(pid),
        None => wait_for_bwrap_child(pid),
    };
    let cleanup_signal_mask = ForwardedSignalMask::block();
    BWRAP_CHILD_PID.store(0, Ordering::SeqCst);
    let protected_create_monitor_violation = protected_create_monitor
        .map(ProtectedCreateMonitor::stop)
        .unwrap_or(false);
    cleanup_synthetic_mount_targets(&synthetic_mount_registrations);
    let protected_create_violation = if let Some(namespace_init) = namespace_init
        && !namespace_init.has_exited()
    {
        hand_off_protected_create_monitor(
            namespace_init,
            protected_create_targets,
            protected_create_registrations,
            &cleanup_signal_mask,
        );
        protected_create_monitor_violation
    } else {
        protected_create_monitor_violation
            || cleanup_protected_create_targets(&protected_create_registrations)
    };
    signal_forwarders.restore();
    cleanup_signal_mask.restore();
    exit_with_wait_status_or_policy_violation(status, protected_create_violation);
}

impl ProtectedCreateMonitor {
    fn start(targets: &[crate::bwrap::ProtectedCreateTarget]) -> Option<Self> {
        if targets.is_empty() {
            return None;
        }

        let targets = targets.to_vec();
        let stop = Arc::new(AtomicBool::new(false));
        let violation = Arc::new(AtomicBool::new(false));
        let monitor_stop = Arc::clone(&stop);
        let monitor_violation = Arc::clone(&violation);
        let handle = thread::spawn(move || {
            let watcher = ProtectedCreateWatcher::new(&targets);
            while !monitor_stop.load(Ordering::SeqCst) {
                for target in &targets {
                    if remove_protected_create_target_best_effort(target).is_some() {
                        monitor_violation.store(true, Ordering::SeqCst);
                    }
                }
                if let Some(watcher) = &watcher {
                    watcher.wait_for_create_event(&monitor_stop);
                } else {
                    thread::sleep(Duration::from_millis(1));
                }
            }
        });

        Some(Self {
            stop,
            violation,
            handle,
        })
    }

    fn stop(self) -> bool {
        self.stop.store(true, Ordering::SeqCst);
        self.handle
            .join()
            .unwrap_or_else(|_| panic!("protected create monitor thread panicked"));
        self.violation.load(Ordering::SeqCst)
    }
}

impl BwrapNamespaceInit {
    fn from_bwrap_info(info: &str, launcher_pid: libc::pid_t) -> Option<Self> {
        let pid = parse_bwrap_child_pid(info).unwrap_or_else(|| {
            send_signal_to_bwrap_child(launcher_pid, libc::SIGKILL);
            panic!("bubblewrap child information omitted a valid child-pid")
        });
        // Bind the numeric PID to an fd before inspecting any proc metadata.
        // A detached namespace reaper legitimately becomes reparented after
        // the outer launcher exits; PPid is therefore neither a liveness nor
        // identity proof. The pidfd closes the reuse window instead.
        let raw_pidfd = unsafe { libc::syscall(libc::SYS_pidfd_open, pid, 0) };
        if raw_pidfd == -1 {
            let err = std::io::Error::last_os_error();
            if err.raw_os_error() == Some(libc::ESRCH) {
                return None;
            }
            send_signal_to_bwrap_child(launcher_pid, libc::SIGKILL);
            panic!("failed to open bubblewrap namespace init pidfd: {err}");
        }
        let pidfd = unsafe { OwnedFd::from_raw_fd(raw_pidfd as libc::c_int) };

        let status = match fs::read_to_string(format!("/proc/{pid}/status")) {
            Ok(status) => status,
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => return None,
            Err(err) => {
                send_signal_to_bwrap_child(launcher_pid, libc::SIGKILL);
                panic!("failed to read bubblewrap namespace init status: {err}")
            }
        };
        let is_namespace_reaper = namespace_status_is_pid_one(&status);
        if !is_namespace_reaper {
            send_signal_to_bwrap_child(launcher_pid, libc::SIGKILL);
            panic!("bubblewrap child is not the namespace PID-1 reaper");
        }
        Some(Self { pid, pidfd })
    }

    fn has_exited(&self) -> bool {
        let mut poll_fd = libc::pollfd {
            fd: self.pidfd.as_raw_fd(),
            events: libc::POLLIN,
            revents: 0,
        };
        let result = unsafe { libc::poll(&mut poll_fd, 1, 0) };
        result > 0
    }

    fn kill(&self) {
        unsafe {
            libc::syscall(
                libc::SYS_pidfd_send_signal,
                self.pidfd.as_raw_fd(),
                libc::SIGKILL,
                std::ptr::null::<libc::siginfo_t>(),
                0,
            );
        }
    }

    fn wait_for_exit(&self) {
        let mut poll_fd = libc::pollfd {
            fd: self.pidfd.as_raw_fd(),
            events: libc::POLLIN,
            revents: 0,
        };
        loop {
            let result = unsafe { libc::poll(&mut poll_fd, 1, -1) };
            if result > 0 {
                return;
            }
            if result < 0
                && std::io::Error::last_os_error().kind() != std::io::ErrorKind::Interrupted
            {
                return;
            }
        }
    }
}

/// Parse bubblewrap's JSON info record without depending on field ordering or
/// its pretty-printing choices. Treat malformed or non-positive identifiers as
/// a fail-closed protocol violation at the caller.
fn parse_bwrap_child_pid(info: &str) -> Option<libc::pid_t> {
    let value: serde_json::Value = serde_json::from_str(info.trim()).ok()?;
    let object = value.as_object()?;
    let pid = object.get("child-pid")?.as_i64()?;
    libc::pid_t::try_from(pid).ok().filter(|pid| *pid > 0)
}

fn namespace_status_is_pid_one(status: &str) -> bool {
    status
        .lines()
        .find_map(|line| line.strip_prefix("NSpid:"))
        .and_then(|value| value.split_whitespace().last())
        .is_some_and(|namespace_pid| namespace_pid == "1")
}

fn create_bwrap_info_pipe(enabled: bool) -> [libc::c_int; 2] {
    if !enabled {
        return [-1, -1];
    }
    let mut pipe = [-1, -1];
    if unsafe { libc::pipe2(pipe.as_mut_ptr(), libc::O_CLOEXEC) } < 0 {
        let err = std::io::Error::last_os_error();
        panic!("failed to create bubblewrap info pipe: {err}");
    }
    [
        reserve_fd_above_stdio(pipe[0]),
        reserve_fd_above_stdio(pipe[1]),
    ]
}

fn create_detached_command_status_pipe() -> [libc::c_int; 2] {
    let mut pipe = [-1, -1];
    if unsafe { libc::pipe2(pipe.as_mut_ptr(), libc::O_CLOEXEC) } < 0 {
        let err = std::io::Error::last_os_error();
        panic!("failed to create detached command-status pipe: {err}");
    }
    [
        reserve_fd_above_stdio(pipe[0]),
        reserve_fd_above_stdio(pipe[1]),
    ]
}

/// Keep protocol descriptors out of stdin/stdout/stderr. A caller may have
/// intentionally closed one of those descriptors; accepting fd 0, 1, or 2
/// would make a later dup2 or child exec close a protocol stream.
fn reserve_fd_above_stdio(fd: libc::c_int) -> libc::c_int {
    if fd > libc::STDERR_FILENO {
        return fd;
    }
    let reserved = unsafe { libc::fcntl(fd, libc::F_DUPFD_CLOEXEC, libc::STDERR_FILENO + 1) };
    if reserved < 0 {
        let err = std::io::Error::last_os_error();
        panic!("failed to reserve protocol fd above stdio: {err}");
    }
    if unsafe { libc::close(fd) } < 0 {
        let err = std::io::Error::last_os_error();
        panic!("failed to close temporary protocol fd: {err}");
    }
    reserved
}

fn write_detached_command_status(fd: libc::c_int, status: libc::c_int) {
    let bytes = status.to_ne_bytes();
    let mut offset = 0;
    while offset < bytes.len() {
        let written = unsafe {
            libc::write(
                fd,
                bytes[offset..].as_ptr().cast(),
                bytes.len().saturating_sub(offset),
            )
        };
        if written > 0 {
            offset += written as usize;
            continue;
        }
        let err = std::io::Error::last_os_error();
        if err.kind() == std::io::ErrorKind::Interrupted {
            continue;
        }
        panic!("failed to report detached initial command status: {err}");
    }
    close_fd_if_open(fd);
}

fn read_detached_command_status(read_fd: libc::c_int, launcher_pid: libc::pid_t) -> libc::c_int {
    if read_fd < 0 {
        send_signal_to_bwrap_child(launcher_pid, libc::SIGKILL);
        panic!("detached sandbox is missing its command-status pipe");
    }
    let mut bytes = [0_u8; std::mem::size_of::<libc::c_int>()];
    let mut offset = 0;
    while offset < bytes.len() {
        let read = unsafe {
            libc::read(
                read_fd,
                bytes[offset..].as_mut_ptr().cast(),
                bytes.len().saturating_sub(offset),
            )
        };
        if read > 0 {
            offset += read as usize;
            continue;
        }
        if read == 0 {
            close_fd_if_open(read_fd);
            send_signal_to_bwrap_child(launcher_pid, libc::SIGKILL);
            panic!("detached namespace reaper closed the command-status pipe early");
        }
        let err = std::io::Error::last_os_error();
        if err.kind() == std::io::ErrorKind::Interrupted {
            continue;
        }
        close_fd_if_open(read_fd);
        send_signal_to_bwrap_child(launcher_pid, libc::SIGKILL);
        panic!("failed to read detached command status: {err}");
    }
    close_fd_if_open(read_fd);
    libc::c_int::from_ne_bytes(bytes)
}

fn mark_fd_close_on_exec(fd: libc::c_int) {
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFD) };
    if flags < 0 || unsafe { libc::fcntl(fd, libc::F_SETFD, flags | libc::FD_CLOEXEC) } < 0 {
        let err = std::io::Error::last_os_error();
        panic!("failed to mark detached command-status fd close-on-exec: {err}");
    }
}

fn read_bwrap_namespace_init(
    read_fd: libc::c_int,
    launcher_pid: libc::pid_t,
) -> Option<BwrapNamespaceInit> {
    if read_fd < 0 {
        return None;
    }
    let mut info = String::new();
    let mut file = unsafe { File::from_raw_fd(read_fd) };
    std::io::Read::by_ref(&mut file)
        .take(64 * 1024)
        .read_to_string(&mut info)
        .unwrap_or_else(|err| {
            send_signal_to_bwrap_child(launcher_pid, libc::SIGKILL);
            panic!("failed to read bubblewrap child information: {err}")
        });
    BwrapNamespaceInit::from_bwrap_info(&info, launcher_pid)
}

fn hand_off_protected_create_monitor(
    namespace_init: BwrapNamespaceInit,
    targets: Vec<crate::bwrap::ProtectedCreateTarget>,
    mut registrations: Vec<ProtectedCreateTargetRegistration>,
    signal_mask: &ForwardedSignalMask,
) {
    // Transfer custody before publishing READY. The supervisor owns the
    // monitor after the one-shot parent returns, so monitor death after READY
    // still terminates the namespace and cleans the authoritative markers.
    transfer_protected_create_registrations(&mut registrations, namespace_init.pid);
    let mut parent_ready_pipe = [-1, -1];
    if unsafe { libc::pipe2(parent_ready_pipe.as_mut_ptr(), libc::O_CLOEXEC) } < 0 {
        terminate_namespace_and_cleanup(&namespace_init, &registrations);
        let err = std::io::Error::last_os_error();
        panic!("failed to create detached guard readiness pipe: {err}");
    }
    let supervisor_pid = unsafe { libc::fork() };
    if supervisor_pid < 0 {
        close_fd_if_open(parent_ready_pipe[0]);
        close_fd_if_open(parent_ready_pipe[1]);
        terminate_namespace_and_cleanup(&namespace_init, &registrations);
        let err = std::io::Error::last_os_error();
        panic!("failed to fork detached guard supervisor: {err}");
    }
    if supervisor_pid == 0 {
        close_fd_if_open(parent_ready_pipe[0]);
        reset_forwarded_signal_handlers_to_default();
        signal_mask.restore();
        if unsafe { libc::setsid() } < 0 {
            unsafe { libc::_exit(1) };
        }
        redirect_standard_streams_to_dev_null();
        supervise_protected_create_monitor(
            namespace_init,
            targets,
            registrations,
            parent_ready_pipe[1],
        );
    }

    close_fd_if_open(parent_ready_pipe[1]);
    let mut ready = [0_u8; 1];
    loop {
        let result =
            unsafe { libc::read(parent_ready_pipe[0], ready.as_mut_ptr().cast(), ready.len()) };
        if result > 0 {
            if ready[0] != 1 {
                close_fd_if_open(parent_ready_pipe[0]);
                terminate_namespace_and_cleanup(&namespace_init, &registrations);
                panic!("detached guard supervisor rejected the monitor handoff");
            }
            break;
        }
        if result == 0 {
            terminate_namespace_and_cleanup(&namespace_init, &registrations);
            panic!("detached guard exited before becoming ready");
        }
        let err = std::io::Error::last_os_error();
        if err.kind() != std::io::ErrorKind::Interrupted {
            terminate_namespace_and_cleanup(&namespace_init, &registrations);
            panic!("failed waiting for detached guard readiness: {err}");
        }
    }
    close_fd_if_open(parent_ready_pipe[0]);
}

fn supervise_protected_create_monitor(
    namespace_init: BwrapNamespaceInit,
    targets: Vec<crate::bwrap::ProtectedCreateTarget>,
    registrations: Vec<ProtectedCreateTargetRegistration>,
    parent_ready_fd: libc::c_int,
) -> ! {
    let mut monitor_ready_pipe = [-1, -1];
    if unsafe { libc::pipe2(monitor_ready_pipe.as_mut_ptr(), libc::O_CLOEXEC) } < 0 {
        terminate_namespace_and_cleanup(&namespace_init, &registrations);
        unsafe { libc::_exit(1) };
    }
    let monitor_pid = unsafe { libc::fork() };
    if monitor_pid < 0 {
        close_fd_if_open(monitor_ready_pipe[0]);
        close_fd_if_open(monitor_ready_pipe[1]);
        terminate_namespace_and_cleanup(&namespace_init, &registrations);
        unsafe { libc::_exit(1) };
    }
    if monitor_pid == 0 {
        close_fd_if_open(monitor_ready_pipe[0]);
        close_fd_if_open(parent_ready_fd);
        monitor_protected_create_targets_until_namespace_exit(
            namespace_init,
            &targets,
            &registrations,
            monitor_ready_pipe[1],
        );
    }

    close_fd_if_open(monitor_ready_pipe[1]);
    let mut ready = [0_u8; 1];
    let ready_result = unsafe {
        libc::read(
            monitor_ready_pipe[0],
            ready.as_mut_ptr().cast(),
            ready.len(),
        )
    };
    close_fd_if_open(monitor_ready_pipe[0]);
    if ready_result != 1 || ready[0] != 1 {
        kill_and_reap_monitor(monitor_pid);
        terminate_namespace_and_cleanup(&namespace_init, &registrations);
        close_fd_if_open(parent_ready_fd);
        unsafe { libc::_exit(1) };
    }
    if write_monitor_status(parent_ready_fd, /*status*/ 1).is_err() {
        kill_and_reap_monitor(monitor_pid);
        terminate_namespace_and_cleanup(&namespace_init, &registrations);
        unsafe { libc::_exit(1) };
    }
    close_fd_if_open(parent_ready_fd);
    let mut status = 0;
    loop {
        let result = unsafe { libc::waitpid(monitor_pid, &mut status, 0) };
        if result == monitor_pid {
            break;
        }
        if result < 0 && std::io::Error::last_os_error().kind() == std::io::ErrorKind::Interrupted {
            continue;
        }
        terminate_namespace_and_cleanup(&namespace_init, &registrations);
        unsafe { libc::_exit(1) };
    }
    if !libc::WIFEXITED(status) || libc::WEXITSTATUS(status) != 0 {
        terminate_namespace_and_cleanup(&namespace_init, &registrations);
        unsafe { libc::_exit(1) };
    }
    unsafe { libc::_exit(0) }
}

fn terminate_namespace_and_cleanup(
    namespace_init: &BwrapNamespaceInit,
    registrations: &[ProtectedCreateTargetRegistration],
) {
    namespace_init.kill();
    namespace_init.wait_for_exit();
    let _ = cleanup_protected_create_targets(registrations);
}

fn write_monitor_status(fd: libc::c_int, status: u8) -> std::io::Result<()> {
    let byte = [status];
    loop {
        let written = unsafe { libc::write(fd, byte.as_ptr().cast(), byte.len()) };
        if written == 1 {
            return Ok(());
        }
        if written < 0 {
            let err = std::io::Error::last_os_error();
            if err.kind() == std::io::ErrorKind::Interrupted {
                continue;
            }
            return Err(err);
        }
        return Err(std::io::Error::new(
            std::io::ErrorKind::WriteZero,
            "detached guard readiness pipe closed",
        ));
    }
}

fn kill_and_reap_monitor(pid: libc::pid_t) {
    unsafe {
        libc::kill(pid, libc::SIGKILL);
    }
    loop {
        let mut status = 0;
        let result = unsafe { libc::waitpid(pid, &mut status, 0) };
        if result == pid || result < 0 {
            return;
        }
    }
}

fn transfer_protected_create_registrations(
    registrations: &mut [ProtectedCreateTargetRegistration],
    namespace_init_pid: libc::pid_t,
) {
    with_synthetic_mount_registry_lock(|| {
        for registration in registrations {
            let transferred_marker = registration.marker_dir.join(namespace_init_pid.to_string());
            fs::rename(&registration.marker_file, &transferred_marker).unwrap_or_else(|err| {
                panic!(
                    "failed to transfer protected create target {}: {err}",
                    registration.target.path().display()
                )
            });
            registration.marker_file = transferred_marker;
        }
    });
}

fn monitor_protected_create_targets_until_namespace_exit(
    namespace_init: BwrapNamespaceInit,
    targets: &[crate::bwrap::ProtectedCreateTarget],
    registrations: &[ProtectedCreateTargetRegistration],
    ready_fd: libc::c_int,
) -> ! {
    let watcher = ProtectedCreateWatcher::new(targets);
    for target in targets {
        if let Err(err) = remove_protected_create_target_with_retries(target) {
            terminate_namespace_and_cleanup(&namespace_init, registrations);
            eprintln!(
                "failed to remove protected create target {} before detached monitor readiness: {err}",
                target.path().display()
            );
            unsafe { libc::_exit(1) };
        }
    }
    if write_monitor_status(ready_fd, /*status*/ 1).is_err() {
        terminate_namespace_and_cleanup(&namespace_init, registrations);
        unsafe { libc::_exit(1) };
    }
    close_fd_if_open(ready_fd);
    #[cfg(test)]
    if FORCE_DETACHED_MONITOR_EXIT_AFTER_READY.load(Ordering::SeqCst) {
        if let Some(target) = targets.first() {
            fs::create_dir(target.path()).unwrap_or_else(|err| {
                panic!("create simulated post-ready protected target: {err}")
            });
        }
        unsafe { libc::_exit(1) };
    }
    let never_stop = AtomicBool::new(false);
    while !namespace_init.has_exited() {
        for target in targets {
            if let Err(err) = remove_protected_create_target_with_retries(target) {
                terminate_namespace_and_cleanup(&namespace_init, registrations);
                eprintln!(
                    "failed to remove protected create target {} from detached sandbox: {err}",
                    target.path().display()
                );
                unsafe { libc::_exit(1) };
            }
        }
        if let Some(watcher) = &watcher {
            watcher.wait_for_create_event(&never_stop);
        } else {
            thread::sleep(Duration::from_millis(1));
        }
    }
    cleanup_protected_create_targets(registrations);
    unsafe { libc::_exit(0) }
}

fn redirect_standard_streams_to_dev_null() {
    let Ok(dev_null) = OpenOptions::new().read(true).write(true).open("/dev/null") else {
        unsafe { libc::_exit(1) };
    };
    for fd in [libc::STDIN_FILENO, libc::STDOUT_FILENO, libc::STDERR_FILENO] {
        if unsafe { libc::dup2(dev_null.as_raw_fd(), fd) } < 0 {
            unsafe { libc::_exit(1) };
        }
    }
}

fn close_fd_if_open(fd: libc::c_int) {
    if fd >= 0 {
        unsafe {
            libc::close(fd);
        }
    }
}

impl ProtectedCreateWatcher {
    fn new(targets: &[crate::bwrap::ProtectedCreateTarget]) -> Option<Self> {
        let fd = unsafe { libc::inotify_init1(libc::IN_NONBLOCK | libc::IN_CLOEXEC) };
        if fd < 0 {
            return None;
        }

        let mut watched_parents = Vec::<PathBuf>::new();
        let mut watches = Vec::new();
        for target in targets {
            let Some(parent) = target.path().parent() else {
                continue;
            };
            if watched_parents.iter().any(|watched| watched == parent) {
                continue;
            }
            watched_parents.push(parent.to_path_buf());
            let Ok(parent_cstr) = CString::new(parent.as_os_str().as_bytes()) else {
                continue;
            };
            let mask =
                libc::IN_CREATE | libc::IN_MOVED_TO | libc::IN_DELETE_SELF | libc::IN_MOVE_SELF;
            let watch = unsafe { libc::inotify_add_watch(fd, parent_cstr.as_ptr(), mask) };
            if watch >= 0 {
                watches.push(watch);
            }
        }

        if watches.is_empty() {
            unsafe {
                libc::close(fd);
            }
            return None;
        }

        Some(Self {
            fd,
            _watches: watches,
        })
    }

    fn wait_for_create_event(&self, stop: &AtomicBool) {
        let mut poll_fd = libc::pollfd {
            fd: self.fd,
            events: libc::POLLIN,
            revents: 0,
        };
        while !stop.load(Ordering::SeqCst) {
            let res = unsafe { libc::poll(&mut poll_fd, 1, 10) };
            if res > 0 {
                self.drain_events();
                return;
            }
            if res == 0 {
                return;
            }
            let err = std::io::Error::last_os_error();
            if err.kind() == std::io::ErrorKind::Interrupted {
                continue;
            }
            return;
        }
    }

    fn drain_events(&self) {
        let mut buf = [0_u8; 4096];
        loop {
            let read = unsafe { libc::read(self.fd, buf.as_mut_ptr().cast(), buf.len()) };
            if read > 0 {
                continue;
            }
            if read == 0 {
                return;
            }
            let err = std::io::Error::last_os_error();
            if err.kind() == std::io::ErrorKind::Interrupted {
                continue;
            }
            return;
        }
    }
}

impl Drop for ProtectedCreateWatcher {
    fn drop(&mut self) {
        unsafe {
            libc::close(self.fd);
        }
    }
}

fn create_exec_start_pipe(enabled: bool) -> [libc::c_int; 2] {
    if !enabled {
        return [-1, -1];
    }
    let mut pipe = [-1, -1];
    if unsafe { libc::pipe2(pipe.as_mut_ptr(), libc::O_CLOEXEC) } < 0 {
        let err = std::io::Error::last_os_error();
        panic!("failed to create bubblewrap exec start pipe: {err}");
    }
    pipe
}

fn wait_for_parent_exec_start(read_fd: libc::c_int, write_fd: libc::c_int) {
    if write_fd >= 0 {
        unsafe {
            libc::close(write_fd);
        }
    }
    if read_fd < 0 {
        return;
    }

    let mut byte = [0_u8; 1];
    loop {
        let read = unsafe { libc::read(read_fd, byte.as_mut_ptr().cast(), byte.len()) };
        if read >= 0 {
            break;
        }
        let err = std::io::Error::last_os_error();
        if err.kind() != std::io::ErrorKind::Interrupted {
            break;
        }
    }
    unsafe {
        libc::close(read_fd);
    }
}

fn close_child_exec_start_read(read_fd: libc::c_int) {
    if read_fd >= 0 {
        unsafe {
            libc::close(read_fd);
        }
    }
}

fn release_child_exec_start(write_fd: libc::c_int) {
    if write_fd < 0 {
        return;
    }
    let byte = [0_u8; 1];
    unsafe {
        libc::write(write_fd, byte.as_ptr().cast(), byte.len());
        libc::close(write_fd);
    }
}

struct ForwardedSignalMask {
    previous: libc::sigset_t,
}

struct ForwardedSignalHandlers {
    previous: Vec<(libc::c_int, libc::sigaction)>,
}

impl ForwardedSignalMask {
    fn block() -> Self {
        let mut blocked: libc::sigset_t = unsafe { std::mem::zeroed() };
        let mut previous: libc::sigset_t = unsafe { std::mem::zeroed() };
        unsafe {
            libc::sigemptyset(&mut blocked);
            for signal in FORWARDED_SIGNALS {
                libc::sigaddset(&mut blocked, *signal);
            }
            if libc::sigprocmask(libc::SIG_BLOCK, &blocked, &mut previous) < 0 {
                let err = std::io::Error::last_os_error();
                panic!("failed to block bubblewrap forwarded signals: {err}");
            }
        }
        Self { previous }
    }

    fn restore(&self) {
        let mut restored = self.previous;
        unsafe {
            for signal in FORWARDED_SIGNALS {
                libc::sigdelset(&mut restored, *signal);
            }
            if libc::sigprocmask(libc::SIG_SETMASK, &restored, std::ptr::null_mut()) < 0 {
                let err = std::io::Error::last_os_error();
                panic!("failed to restore bubblewrap forwarded signals: {err}");
            }
        }
    }
}

fn terminate_with_parent(parent_pid: libc::pid_t) {
    let res = unsafe { libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGTERM) };
    if res < 0 {
        let err = std::io::Error::last_os_error();
        panic!("failed to set bubblewrap child parent-death signal: {err}");
    }
    if unsafe { libc::getppid() } != parent_pid {
        unsafe {
            libc::raise(libc::SIGTERM);
        }
    }
}

impl ForwardedSignalHandlers {
    fn restore(self) {
        BWRAP_CHILD_PID.store(0, Ordering::SeqCst);
        PENDING_FORWARDED_SIGNAL.store(0, Ordering::SeqCst);
        for (signal, previous_action) in self.previous {
            unsafe {
                if libc::sigaction(signal, &previous_action, std::ptr::null_mut()) < 0 {
                    let err = std::io::Error::last_os_error();
                    panic!("failed to restore bubblewrap signal handler for {signal}: {err}");
                }
            }
        }
    }
}

fn install_bwrap_signal_forwarders(pid: libc::pid_t) -> ForwardedSignalHandlers {
    BWRAP_CHILD_PID.store(pid, Ordering::SeqCst);
    let mut previous = Vec::with_capacity(FORWARDED_SIGNALS.len());
    for signal in FORWARDED_SIGNALS {
        let mut action: libc::sigaction = unsafe { std::mem::zeroed() };
        let mut previous_action: libc::sigaction = unsafe { std::mem::zeroed() };
        action.sa_sigaction = forward_signal_to_bwrap_child as *const () as libc::sighandler_t;
        unsafe {
            libc::sigemptyset(&mut action.sa_mask);
            if libc::sigaction(*signal, &action, &mut previous_action) < 0 {
                let err = std::io::Error::last_os_error();
                panic!("failed to install bubblewrap signal forwarder for {signal}: {err}");
            }
        }
        previous.push((*signal, previous_action));
    }
    replay_pending_forwarded_signal(pid);
    ForwardedSignalHandlers { previous }
}

extern "C" fn forward_signal_to_bwrap_child(signal: libc::c_int) {
    PENDING_FORWARDED_SIGNAL.store(signal, Ordering::SeqCst);
    let pid = BWRAP_CHILD_PID.load(Ordering::SeqCst);
    if pid > 0 {
        send_signal_to_bwrap_child(pid, signal);
    }
}

fn replay_pending_forwarded_signal(pid: libc::pid_t) {
    let signal = PENDING_FORWARDED_SIGNAL.swap(0, Ordering::SeqCst);
    if signal > 0 {
        send_signal_to_bwrap_child(pid, signal);
    }
}

fn send_signal_to_bwrap_child(pid: libc::pid_t, signal: libc::c_int) {
    unsafe {
        libc::kill(-pid, signal);
        libc::kill(pid, signal);
    }
}

fn reset_forwarded_signal_handlers_to_default() {
    for signal in FORWARDED_SIGNALS {
        unsafe {
            if libc::signal(*signal, libc::SIG_DFL) == libc::SIG_ERR {
                let err = std::io::Error::last_os_error();
                panic!("failed to reset bubblewrap signal handler for {signal}: {err}");
            }
        }
    }
}

fn wait_for_bwrap_child(pid: libc::pid_t) -> libc::c_int {
    loop {
        let mut status: libc::c_int = 0;
        let wait_res = unsafe { libc::waitpid(pid, &mut status as *mut libc::c_int, 0) };
        if wait_res >= 0 {
            return status;
        }
        let err = std::io::Error::last_os_error();
        if err.raw_os_error() == Some(libc::EINTR) {
            continue;
        }
        panic!("waitpid failed for bubblewrap child: {err}");
    }
}

fn register_synthetic_mount_targets(
    targets: &[crate::bwrap::SyntheticMountTarget],
) -> Vec<SyntheticMountTargetRegistration> {
    with_synthetic_mount_registry_lock(|| {
        targets
            .iter()
            .map(|target| {
                let marker_dir = synthetic_mount_marker_dir(target.path());
                ensure_marker_directory(&marker_dir, "synthetic");
                let target = if target.preserves_pre_existing_path()
                    && synthetic_mount_marker_dir_has_active_synthetic_owner(&marker_dir)
                {
                    match target.kind() {
                        crate::bwrap::SyntheticMountTargetKind::EmptyFile => {
                            crate::bwrap::SyntheticMountTarget::missing(target.path())
                        }
                        crate::bwrap::SyntheticMountTargetKind::EmptyDirectory => {
                            crate::bwrap::SyntheticMountTarget::missing_empty_directory(
                                target.path(),
                            )
                        }
                    }
                } else {
                    target.clone()
                };
                let marker_file = marker_dir.join(std::process::id().to_string());
                write_marker_file(
                    &marker_file,
                    synthetic_mount_marker_contents(&target),
                    target.path(),
                    "synthetic",
                );
                SyntheticMountTargetRegistration {
                    target,
                    marker_file,
                    marker_dir,
                }
            })
            .collect()
    })
}

fn register_protected_create_targets(
    targets: &[crate::bwrap::ProtectedCreateTarget],
) -> Vec<ProtectedCreateTargetRegistration> {
    with_synthetic_mount_registry_lock(|| {
        targets
            .iter()
            .map(|target| {
                let marker_dir = synthetic_mount_marker_dir(target.path());
                ensure_marker_directory(&marker_dir, "protected create");
                let marker_file = marker_dir.join(std::process::id().to_string());
                write_marker_file(
                    &marker_file,
                    PROTECTED_CREATE_MARKER,
                    target.path(),
                    "protected create",
                );
                ProtectedCreateTargetRegistration {
                    target: target.clone(),
                    marker_file,
                    marker_dir,
                }
            })
            .collect()
    })
}

fn ensure_marker_directory(path: &Path, kind: &str) {
    match fs::symlink_metadata(path) {
        Ok(metadata) if metadata.file_type().is_symlink() || !metadata.is_dir() => panic!(
            "{kind} marker path must be a real directory: {}",
            path.display()
        ),
        Ok(_) => {}
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => {
            fs::create_dir(path).unwrap_or_else(|create_err| {
                panic!(
                    "failed to create {kind} marker directory {}: {create_err}",
                    path.display()
                )
            });
        }
        Err(err) => panic!(
            "failed to inspect {kind} marker directory {}: {err}",
            path.display()
        ),
    }
}

fn write_marker_file(path: &Path, contents: &[u8], target: &Path, kind: &str) {
    let mut file = OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)
        .unwrap_or_else(|err| {
            panic!(
                "failed to register {kind} target {}: {err}",
                target.display()
            )
        });
    file.write_all(contents).unwrap_or_else(|err| {
        panic!(
            "failed to write {kind} marker for {}: {err}",
            target.display()
        )
    });
}

fn synthetic_mount_marker_contents(target: &crate::bwrap::SyntheticMountTarget) -> &'static [u8] {
    if target.preserves_pre_existing_path() {
        SYNTHETIC_MOUNT_MARKER_EXISTING
    } else {
        SYNTHETIC_MOUNT_MARKER_SYNTHETIC
    }
}

fn synthetic_mount_marker_dir_has_active_synthetic_owner(marker_dir: &Path) -> bool {
    synthetic_mount_marker_dir_has_active_process_matching(marker_dir, |path| {
        match fs::read(path) {
            Ok(contents) => contents == SYNTHETIC_MOUNT_MARKER_SYNTHETIC,
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => false,
            Err(err) => panic!(
                "failed to read synthetic bubblewrap mount marker {}: {err}",
                path.display()
            ),
        }
    })
}

fn synthetic_mount_marker_dir_has_active_process(marker_dir: &Path) -> bool {
    synthetic_mount_marker_dir_has_active_process_matching(marker_dir, |_| true)
}

fn synthetic_mount_marker_dir_has_active_process_matching(
    marker_dir: &Path,
    matches_marker: impl Fn(&Path) -> bool,
) -> bool {
    let entries = match fs::read_dir(marker_dir) {
        Ok(entries) => entries,
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => return false,
        Err(err) => panic!(
            "failed to read synthetic bubblewrap mount marker directory {}: {err}",
            marker_dir.display()
        ),
    };
    for entry in entries {
        let entry = entry.unwrap_or_else(|err| {
            panic!(
                "failed to read synthetic bubblewrap mount marker in {}: {err}",
                marker_dir.display()
            )
        });
        let path = entry.path();
        let Some(pid) = path
            .file_name()
            .and_then(|name| name.to_str())
            .and_then(|name| name.parse::<libc::pid_t>().ok())
        else {
            continue;
        };
        if !process_is_active(pid) {
            match fs::remove_file(&path) {
                Ok(()) => {}
                Err(err) if err.kind() == std::io::ErrorKind::NotFound => {}
                Err(err) => panic!(
                    "failed to remove stale synthetic bubblewrap mount marker {}: {err}",
                    path.display()
                ),
            }
            continue;
        }
        let matches_marker = matches_marker(&path);
        if matches_marker {
            return true;
        }
    }
    false
}

fn cleanup_synthetic_mount_targets(targets: &[SyntheticMountTargetRegistration]) {
    with_synthetic_mount_registry_lock(|| {
        for target in targets.iter().rev() {
            match fs::remove_file(&target.marker_file) {
                Ok(()) => {}
                Err(err) if err.kind() == std::io::ErrorKind::NotFound => {}
                Err(err) => panic!(
                    "failed to unregister synthetic bubblewrap mount target {}: {err}",
                    target.target.path().display()
                ),
            }
        }

        for target in targets.iter().rev() {
            if synthetic_mount_marker_dir_has_active_process(&target.marker_dir) {
                continue;
            }
            remove_synthetic_mount_target(&target.target);
            match fs::remove_dir(&target.marker_dir) {
                Ok(()) => {}
                Err(err) if err.kind() == std::io::ErrorKind::NotFound => {}
                Err(err) if err.kind() == std::io::ErrorKind::DirectoryNotEmpty => {}
                Err(err) => panic!(
                    "failed to remove synthetic bubblewrap mount marker directory {}: {err}",
                    target.marker_dir.display()
                ),
            }
        }
    });
}

fn cleanup_protected_create_targets(targets: &[ProtectedCreateTargetRegistration]) -> bool {
    with_synthetic_mount_registry_lock(|| {
        for target in targets.iter().rev() {
            match fs::remove_file(&target.marker_file) {
                Ok(()) => {}
                Err(err) if err.kind() == std::io::ErrorKind::NotFound => {}
                Err(err) => panic!(
                    "failed to unregister protected create target {}: {err}",
                    target.target.path().display()
                ),
            }
        }

        let mut violation = false;
        for target in targets.iter().rev() {
            if synthetic_mount_marker_dir_has_active_process(&target.marker_dir) {
                if target.target.path().exists() {
                    violation = true;
                }
                continue;
            }
            violation |= remove_protected_create_target(&target.target);
            match fs::remove_dir(&target.marker_dir) {
                Ok(()) => {}
                Err(err) if err.kind() == std::io::ErrorKind::NotFound => {}
                Err(err) if err.kind() == std::io::ErrorKind::DirectoryNotEmpty => {}
                Err(err) => panic!(
                    "failed to remove protected create marker directory {}: {err}",
                    target.marker_dir.display()
                ),
            }
        }
        violation
    })
}

fn remove_protected_create_target(target: &crate::bwrap::ProtectedCreateTarget) -> bool {
    for attempt in 0..100 {
        match try_remove_protected_create_target(target) {
            Ok(removal) => return removal.is_some(),
            Err(err) if err.kind() == std::io::ErrorKind::DirectoryNotEmpty && attempt < 99 => {
                thread::sleep(Duration::from_millis(1));
            }
            Err(err) => {
                panic!(
                    "failed to remove protected create target {}: {err}",
                    target.path().display()
                );
            }
        }
    }
    unreachable!("protected create removal retry loop should return or panic")
}

fn remove_protected_create_target_best_effort(
    target: &crate::bwrap::ProtectedCreateTarget,
) -> Option<ProtectedCreateRemoval> {
    for _ in 0..100 {
        match try_remove_protected_create_target(target) {
            Ok(removal) => return removal,
            Err(err) if err.kind() == std::io::ErrorKind::DirectoryNotEmpty => {
                thread::sleep(Duration::from_millis(1));
            }
            Err(_) => return Some(ProtectedCreateRemoval::Other),
        }
    }
    Some(ProtectedCreateRemoval::Other)
}

fn remove_protected_create_target_with_retries(
    target: &crate::bwrap::ProtectedCreateTarget,
) -> std::io::Result<Option<ProtectedCreateRemoval>> {
    for attempt in 0..100 {
        match try_remove_protected_create_target(target) {
            Ok(removal) => return Ok(removal),
            Err(err) if err.kind() == std::io::ErrorKind::DirectoryNotEmpty && attempt < 99 => {
                thread::sleep(Duration::from_millis(1));
            }
            Err(err) => return Err(err),
        }
    }
    unreachable!("protected create removal retry loop should return")
}

fn try_remove_protected_create_target(
    target: &crate::bwrap::ProtectedCreateTarget,
) -> std::io::Result<Option<ProtectedCreateRemoval>> {
    let Some(normalized) = normalized_protected_create_path(target.path(), target.root())? else {
        return Ok(None);
    };
    let root = open_directory_fd(&normalized.root)?;
    let (parent, name) = open_protected_create_parent(&root, &normalized.relative)?;
    let removal = match remove_entry_at(parent.as_raw_fd(), &name) {
        Ok(removal) => removal,
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(err) => return Err(err),
    };
    eprintln!(
        "sandbox blocked creation of protected workspace metadata path {}",
        normalized.display.display()
    );
    Ok(Some(removal))
}

fn open_directory_fd(path: &Path) -> std::io::Result<OwnedFd> {
    let directory = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_PATH | libc::O_DIRECTORY | libc::O_NOFOLLOW)
        .open(path)?;
    Ok(directory.into())
}

fn open_directory_at(parent: RawFd, name: &CString) -> std::io::Result<OwnedFd> {
    // SAFETY: `name` is a NUL-terminated component owned by the caller and
    // `parent` is an open directory descriptor retained for this operation.
    let fd = unsafe {
        libc::openat(
            parent,
            name.as_ptr(),
            libc::O_PATH | libc::O_DIRECTORY | libc::O_NOFOLLOW,
            0,
        )
    };
    if fd < 0 {
        return Err(std::io::Error::last_os_error());
    }
    // SAFETY: the successful openat call transfers ownership of this fd.
    Ok(unsafe { OwnedFd::from_raw_fd(fd) })
}

fn stat_at(parent: RawFd, name: &CString) -> std::io::Result<libc::stat> {
    let mut metadata = std::mem::MaybeUninit::<libc::stat>::uninit();
    // SAFETY: `metadata` points to writable storage and `name` is a valid
    // NUL-terminated component for the duration of the call.
    let result = unsafe {
        libc::fstatat(
            parent,
            name.as_ptr(),
            metadata.as_mut_ptr(),
            libc::AT_SYMLINK_NOFOLLOW,
        )
    };
    if result < 0 {
        return Err(std::io::Error::last_os_error());
    }
    // SAFETY: fstatat initialized metadata after returning success.
    Ok(unsafe { metadata.assume_init() })
}

fn is_directory(metadata: &libc::stat) -> bool {
    (metadata.st_mode & libc::S_IFMT) == libc::S_IFDIR
}

fn unlink_at(parent: RawFd, name: &CString, flags: libc::c_int) -> std::io::Result<()> {
    // SAFETY: `name` is a NUL-terminated component and `parent` is an open
    // directory descriptor retained for this operation.
    let result = unsafe { libc::unlinkat(parent, name.as_ptr(), flags) };
    if result < 0 {
        return Err(std::io::Error::last_os_error());
    }
    Ok(())
}

fn proc_fd_path(fd: RawFd) -> PathBuf {
    PathBuf::from(format!("/proc/self/fd/{fd}"))
}

fn remove_entry_at(parent: RawFd, name: &CString) -> std::io::Result<ProtectedCreateRemoval> {
    let metadata = stat_at(parent, name)?;
    if !is_directory(&metadata) {
        unlink_at(parent, name, /*flags*/ 0)?;
        return Ok(ProtectedCreateRemoval::Other);
    }

    let directory = open_directory_at(parent, name)?;
    let directory_path = proc_fd_path(directory.as_raw_fd());
    fs::set_permissions(&directory_path, fs::Permissions::from_mode(0o700))?;
    for entry in fs::read_dir(directory_path)? {
        let entry = entry?;
        let child_name = CString::new(entry.file_name().as_bytes()).map_err(|_| {
            std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                "protected-create directory entry contains NUL",
            )
        })?;
        match remove_entry_at(directory.as_raw_fd(), &child_name) {
            Ok(_) => {}
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => {}
            Err(err) => return Err(err),
        }
    }
    unlink_at(parent, name, libc::AT_REMOVEDIR)?;
    Ok(ProtectedCreateRemoval::Directory)
}

fn open_protected_create_parent(
    root: &OwnedFd,
    relative: &Path,
) -> std::io::Result<(OwnedFd, CString)> {
    let mut components = relative
        .components()
        .map(|component| match component {
            Component::Normal(name) => CString::new(name.as_bytes()).map_err(|_| {
                std::io::Error::new(
                    std::io::ErrorKind::InvalidInput,
                    "protected-create path component contains NUL",
                )
            }),
            _ => Err(std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                "protected-create path contains a non-normal component",
            )),
        })
        .collect::<std::io::Result<Vec<_>>>()?;
    let name = components.pop().ok_or_else(|| {
        std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "protected-create path has no final component",
        )
    })?;
    let mut parent = duplicate_fd(root.as_raw_fd())?;
    for component in components {
        parent = open_directory_at(parent.as_raw_fd(), &component)?;
    }
    Ok((parent, name))
}

fn duplicate_fd(fd: RawFd) -> std::io::Result<OwnedFd> {
    // SAFETY: `fd` is retained by the caller for the duration of this call.
    let duplicate = unsafe { libc::fcntl(fd, libc::F_DUPFD_CLOEXEC, libc::STDERR_FILENO + 1) };
    if duplicate < 0 {
        return Err(std::io::Error::last_os_error());
    }
    // SAFETY: the successful fcntl call transfers ownership of this fd.
    Ok(unsafe { OwnedFd::from_raw_fd(duplicate) })
}

/// Normalize a protected-create target's parent and keep its final component
/// beneath the writable root. The final component is deliberately not
/// canonicalized: descriptor-relative no-follow operations must still observe
/// a symlink at the target itself rather than following it.
struct NormalizedProtectedCreatePath {
    root: PathBuf,
    relative: PathBuf,
    display: PathBuf,
}

fn normalized_protected_create_path(
    path: &Path,
    root: &Path,
) -> std::io::Result<Option<NormalizedProtectedCreatePath>> {
    if !path.is_absolute() || !root.is_absolute() {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "protected-create paths must be absolute",
        ));
    }

    let canonical_root = match root.canonicalize() {
        Ok(root) => root,
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(err) => return Err(err),
    };
    let parent = path.parent().ok_or_else(|| {
        std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "protected-create path has no parent",
        )
    })?;
    let canonical_parent = match parent.canonicalize() {
        Ok(parent) => parent,
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(err) => return Err(err),
    };
    let file_name = path.file_name().ok_or_else(|| {
        std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "protected-create path has no final component",
        )
    })?;
    if file_name
        .to_str()
        .is_some_and(|name| matches!(name, "." | ".."))
    {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "protected-create path has an invalid final component",
        ));
    }

    let normalized = canonical_parent.join(file_name);
    if !normalized.starts_with(&canonical_root) {
        return Err(std::io::Error::new(
            std::io::ErrorKind::PermissionDenied,
            "protected-create path escaped its writable root",
        ));
    }
    let relative = normalized
        .strip_prefix(&canonical_root)
        .map_err(|_| {
            std::io::Error::new(
                std::io::ErrorKind::PermissionDenied,
                "protected-create path escaped its writable root",
            )
        })?
        .to_path_buf();
    Ok(Some(NormalizedProtectedCreatePath {
        root: canonical_root,
        relative,
        display: normalized,
    }))
}

fn remove_synthetic_mount_target(target: &crate::bwrap::SyntheticMountTarget) {
    let path = target.path();
    let metadata = match fs::symlink_metadata(path) {
        Ok(metadata) => metadata,
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => return,
        Err(err) => panic!(
            "failed to inspect synthetic bubblewrap mount target {}: {err}",
            path.display()
        ),
    };
    if !target.should_remove_after_bwrap(&metadata) {
        return;
    }
    match target.kind() {
        crate::bwrap::SyntheticMountTargetKind::EmptyFile => match fs::remove_file(path) {
            Ok(()) => {}
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => {}
            Err(err) => panic!(
                "failed to remove synthetic bubblewrap mount target {}: {err}",
                path.display()
            ),
        },
        crate::bwrap::SyntheticMountTargetKind::EmptyDirectory => match fs::remove_dir(path) {
            Ok(()) => {}
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => {}
            Err(err) if err.kind() == std::io::ErrorKind::DirectoryNotEmpty => {}
            Err(err) => panic!(
                "failed to remove synthetic bubblewrap mount target {}: {err}",
                path.display()
            ),
        },
    }
}

fn process_is_active(pid: libc::pid_t) -> bool {
    // `kill(pid, 0)` succeeds for zombies until their parent reaps them. A
    // zombie cannot retain protected-create custody, so classify it first.
    if process_is_zombie(pid) {
        return false;
    }
    let result = unsafe { libc::kill(pid, 0) };
    if result == 0 {
        return true;
    }
    let err = std::io::Error::last_os_error();
    if matches!(err.raw_os_error(), Some(libc::ESRCH)) {
        return false;
    }
    !process_is_zombie(pid)
}

fn process_is_zombie(pid: libc::pid_t) -> bool {
    let Ok(stat) = fs::read_to_string(format!("/proc/{pid}/stat")) else {
        return false;
    };
    // Field three is the state. Split after the final ')' because comm may
    // itself contain spaces or parentheses.
    stat.rsplit_once(')')
        .and_then(|(_, fields)| fields.split_whitespace().next())
        == Some("Z")
}

fn with_synthetic_mount_registry_lock<T>(f: impl FnOnce() -> T) -> T {
    let registry_root = synthetic_mount_registry_root();
    ensure_registry_directory(&registry_root);
    let lock_path = registry_root.join("lock");
    let lock_file = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .custom_flags(libc::O_NOFOLLOW)
        .open(&lock_path)
        .unwrap_or_else(|err| {
            panic!(
                "failed to open synthetic bubblewrap mount registry lock {}: {err}",
                lock_path.display()
            )
        });
    if unsafe { libc::flock(lock_file.as_raw_fd(), libc::LOCK_EX) } < 0 {
        let err = std::io::Error::last_os_error();
        panic!(
            "failed to lock synthetic bubblewrap mount registry {}: {err}",
            lock_path.display()
        );
    }
    let result = f();
    if unsafe { libc::flock(lock_file.as_raw_fd(), libc::LOCK_UN) } < 0 {
        let err = std::io::Error::last_os_error();
        panic!(
            "failed to unlock synthetic bubblewrap mount registry {}: {err}",
            lock_path.display()
        );
    }
    result
}

fn ensure_registry_directory(path: &Path) {
    match fs::symlink_metadata(path) {
        Ok(metadata) if metadata.file_type().is_symlink() || !metadata.is_dir() => {
            panic!(
                "synthetic bubblewrap mount registry must be a real directory: {}",
                path.display()
            )
        }
        Ok(_) => {}
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => {
            fs::create_dir(path).unwrap_or_else(|create_err| {
                panic!(
                    "failed to create synthetic bubblewrap mount registry {}: {create_err}",
                    path.display()
                )
            });
        }
        Err(err) => panic!(
            "failed to inspect synthetic bubblewrap mount registry {}: {err}",
            path.display()
        ),
    }
}

fn synthetic_mount_marker_dir(path: &Path) -> PathBuf {
    synthetic_mount_registry_root().join(format!("{:016x}", hash_path(path)))
}

pub(crate) fn synthetic_mount_registry_root() -> PathBuf {
    static REGISTRY_ROOT: OnceLock<PathBuf> = OnceLock::new();

    REGISTRY_ROOT
        .get_or_init(|| {
            let effective_uid = unsafe { libc::geteuid() };
            let temp_dir = std::env::temp_dir();
            let temp_dir = temp_dir.canonicalize().unwrap_or_else(|err| {
                panic!(
                    "failed to resolve synthetic mount registry temp directory {}: {err}",
                    temp_dir.display()
                )
            });
            let registry_root = temp_dir.join(format!(
                "codex-bwrap-synthetic-mount-targets-{effective_uid}"
            ));
            assert!(
                registry_root.starts_with(&temp_dir),
                "synthetic mount registry escaped its canonical temp directory"
            );
            // A registry symlink can redirect bookkeeping into a writable root
            // that does not overlap TMPDIR, bypassing its read-only mount.
            assert!(
                !registry_root.is_symlink(),
                "synthetic mount registry must not be a symlink: {}",
                registry_root.display()
            );
            registry_root
        })
        .clone()
}

fn hash_path(path: &Path) -> u64 {
    let mut hash = 0xcbf29ce484222325u64;
    for byte in path.as_os_str().as_bytes() {
        hash ^= u64::from(*byte);
        hash = hash.wrapping_mul(0x100000001b3);
    }
    hash
}

fn exit_with_wait_status(status: libc::c_int) -> ! {
    if libc::WIFEXITED(status) {
        std::process::exit(libc::WEXITSTATUS(status));
    }

    if libc::WIFSIGNALED(status) {
        let signal = libc::WTERMSIG(status);
        unsafe {
            libc::signal(signal, libc::SIG_DFL);
            libc::kill(libc::getpid(), signal);
        }
        std::process::exit(128 + signal);
    }

    std::process::exit(1);
}

fn exit_with_wait_status_or_policy_violation(
    status: libc::c_int,
    protected_create_violation: bool,
) -> ! {
    if protected_create_violation && libc::WIFEXITED(status) && libc::WEXITSTATUS(status) == 0 {
        std::process::exit(1);
    }

    exit_with_wait_status(status);
}

/// Run a short-lived bubblewrap preflight in a child process and capture output.
///
/// Strategy:
/// - This is used by the `/proc` and network-namespace preflights, which run
///   `/bin/true` under bubblewrap with a minimal filesystem view.
/// - The goal is to detect environments where mounting `/proc` or configuring
///   loopback fails, so the real run can use the corresponding fallback.
/// - We capture both stderr and stdout from that preflight to match known
///   failure text across bubblewrap variants.
/// - We do not stream output because this is a one-shot probe with a trivial
///   command, and reads are bounded to a fixed max size.
fn run_bwrap_in_child_capture_output(bwrap_args: crate::bwrap::BwrapArgs) -> String {
    const MAX_PREFLIGHT_OUTPUT_BYTES: u64 = 64 * 1024;
    let crate::bwrap::BwrapArgs {
        args,
        preserved_files,
        synthetic_mount_targets,
        protected_create_targets,
        process_lifetime: _,
    } = bwrap_args;
    let setup_signal_mask = ForwardedSignalMask::block();
    let synthetic_mount_registrations = register_synthetic_mount_targets(&synthetic_mount_targets);
    let protected_create_registrations =
        register_protected_create_targets(&protected_create_targets);

    let mut pipe_fds = [0; 2];
    let pipe_res = unsafe { libc::pipe2(pipe_fds.as_mut_ptr(), libc::O_CLOEXEC) };
    if pipe_res < 0 {
        let err = std::io::Error::last_os_error();
        panic!("failed to create stderr pipe for bubblewrap: {err}");
    }
    let read_fd = pipe_fds[0];
    let write_fd = pipe_fds[1];

    let pid = unsafe { libc::fork() };
    if pid < 0 {
        let err = std::io::Error::last_os_error();
        panic!("failed to fork for bubblewrap: {err}");
    }

    if pid == 0 {
        reset_forwarded_signal_handlers_to_default();
        setup_signal_mask.restore();
        // Child: redirect stderr to the pipe, then run bubblewrap.
        unsafe {
            close_fd_or_panic(read_fd, "close read end in bubblewrap child");
            if libc::dup2(write_fd, libc::STDOUT_FILENO) < 0 {
                let err = std::io::Error::last_os_error();
                panic!("failed to redirect stdout for bubblewrap: {err}");
            }
            if libc::dup2(write_fd, libc::STDERR_FILENO) < 0 {
                let err = std::io::Error::last_os_error();
                panic!("failed to redirect stderr for bubblewrap: {err}");
            }
            close_fd_or_panic(write_fd, "close write end in bubblewrap child");
        }

        exec_bwrap(args, preserved_files);
    }

    let signal_forwarders = install_bwrap_signal_forwarders(pid);
    setup_signal_mask.restore();
    // Parent: close the write end and read stderr while the child runs.
    close_fd_or_panic(write_fd, "close write end in bubblewrap parent");

    // SAFETY: `read_fd` is a valid owned fd in the parent.
    let mut read_file = unsafe { File::from_raw_fd(read_fd) };
    let mut output_bytes = Vec::new();
    let mut limited_reader = (&mut read_file).take(MAX_PREFLIGHT_OUTPUT_BYTES);
    if let Err(err) = limited_reader.read_to_end(&mut output_bytes) {
        panic!("failed to read bubblewrap output: {err}");
    }

    let status = wait_for_bwrap_child(pid);
    let cleanup_signal_mask = ForwardedSignalMask::block();
    BWRAP_CHILD_PID.store(0, Ordering::SeqCst);
    cleanup_synthetic_mount_targets(&synthetic_mount_registrations);
    cleanup_protected_create_targets(&protected_create_registrations);
    signal_forwarders.restore();
    cleanup_signal_mask.restore();
    if libc::WIFSIGNALED(status) {
        exit_with_wait_status(status);
    }

    String::from_utf8_lossy(&output_bytes).into_owned()
}

/// Close an owned file descriptor and panic with context on failure.
///
/// We use explicit close() checks here (instead of ignoring return codes)
/// because this code runs in low-level sandbox setup paths where fd leaks or
/// close errors can mask the root cause of later failures.
fn close_fd_or_panic(fd: libc::c_int, context: &str) {
    let close_res = unsafe { libc::close(fd) };
    if close_res < 0 {
        let err = std::io::Error::last_os_error();
        panic!("{context}: {err}");
    }
}

fn is_proc_mount_failure(stderr: &str) -> bool {
    stderr.contains("Can't mount proc")
        && stderr.contains("/newroot/proc")
        && (stderr.contains("Invalid argument")
            || stderr.contains("Operation not permitted")
            || stderr.contains("Permission denied"))
}

fn is_loopback_setup_failure(stderr: &str) -> bool {
    stderr.contains("loopback")
        && stderr.contains("RTM_NEWADDR")
        && (stderr.contains("Operation not permitted") || stderr.contains("Permission denied"))
}

struct InnerSeccompCommandArgs<'a> {
    sandbox_policy_cwd: &'a Path,
    command_cwd: Option<&'a Path>,
    permission_profile: &'a PermissionProfile,
    allow_network_for_proxy: bool,
    allow_detached_children: bool,
    proxy_route_spec: Option<String>,
    command: Vec<String>,
}

/// Build the inner command that applies seccomp after bubblewrap.
fn build_inner_seccomp_command(args: InnerSeccompCommandArgs<'_>) -> Vec<String> {
    let InnerSeccompCommandArgs {
        sandbox_policy_cwd,
        command_cwd,
        permission_profile,
        allow_network_for_proxy,
        allow_detached_children,
        proxy_route_spec,
        command,
    } = args;
    let current_exe = match std::env::current_exe() {
        Ok(path) => path,
        Err(err) => panic!("failed to resolve current executable path: {err}"),
    };
    let permission_profile_json = match serde_json::to_string(permission_profile) {
        Ok(json) => json,
        Err(err) => panic!("failed to serialize permission profile: {err}"),
    };

    let mut inner = vec![
        current_exe.to_string_lossy().to_string(),
        "--sandbox-policy-cwd".to_string(),
        sandbox_policy_cwd.to_string_lossy().to_string(),
    ];
    if let Some(command_cwd) = command_cwd {
        inner.push("--command-cwd".to_string());
        inner.push(command_cwd.to_string_lossy().to_string());
    }
    inner.extend([
        "--permission-profile".to_string(),
        permission_profile_json,
        "--apply-seccomp-then-exec".to_string(),
    ]);
    if allow_network_for_proxy {
        inner.push("--allow-network-for-proxy".to_string());
        let proxy_route_spec = proxy_route_spec
            .unwrap_or_else(|| panic!("managed proxy mode requires a proxy route spec"));
        inner.push("--proxy-route-spec".to_string());
        inner.push(proxy_route_spec);
    }
    if allow_detached_children {
        inner.push("--allow-detached-children".to_string());
    }
    inner.push("--".to_string());
    inner.extend(command);
    inner
}

/// Exec the provided argv, panicking with context if it fails.
fn exec_or_panic(command: Vec<String>) -> ! {
    #[expect(clippy::expect_used)]
    let c_command =
        CString::new(command[0].as_str()).expect("Failed to convert command to CString");
    #[expect(clippy::expect_used)]
    let c_args: Vec<CString> = command
        .iter()
        .map(|arg| CString::new(arg.as_str()).expect("Failed to convert arg to CString"))
        .collect();

    let mut c_args_ptrs: Vec<*const libc::c_char> = c_args.iter().map(|arg| arg.as_ptr()).collect();
    c_args_ptrs.push(std::ptr::null());

    unsafe {
        libc::execvp(c_command.as_ptr(), c_args_ptrs.as_ptr());
    }

    // If execvp returns, there was an error.
    let err = std::io::Error::last_os_error();
    panic!("Failed to execvp {}: {err}", command[0].as_str());
}

#[cfg(test)]
#[path = "linux_run_main_tests.rs"]
mod tests;
