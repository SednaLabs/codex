#[cfg(test)]
use super::*;
#[cfg(test)]
use crate::linux_run_main::install_bwrap_signal_forwarders;
#[cfg(test)]
use crate::linux_run_main::wait_for_bwrap_child;
#[cfg(test)]
use codex_protocol::models::PermissionProfile;
#[cfg(test)]
use codex_protocol::protocol::FileSystemSandboxPolicy;
#[cfg(test)]
use codex_protocol::protocol::NetworkSandboxPolicy;
#[cfg(test)]
use codex_utils_absolute_path::AbsolutePathBuf;
#[cfg(test)]
use pretty_assertions::assert_eq;

fn read_only_permission_profile() -> PermissionProfile {
    PermissionProfile::read_only()
}

fn read_only_file_system_policy() -> FileSystemSandboxPolicy {
    read_only_permission_profile().file_system_sandbox_policy()
}

#[test]
fn detects_proc_mount_invalid_argument_failure() {
    let stderr = "bwrap: Can't mount proc on /newroot/proc: Invalid argument";
    assert!(is_proc_mount_failure(stderr));
}

#[test]
fn detects_proc_mount_operation_not_permitted_failure() {
    let stderr = "bwrap: Can't mount proc on /newroot/proc: Operation not permitted";
    assert!(is_proc_mount_failure(stderr));
}

#[test]
fn detects_proc_mount_permission_denied_failure() {
    let stderr = "bwrap: Can't mount proc on /newroot/proc: Permission denied";
    assert!(is_proc_mount_failure(stderr));
}

#[test]
fn ignores_non_proc_mount_errors() {
    let stderr = "bwrap: Can't bind mount /dev/null: Operation not permitted";
    assert!(!is_proc_mount_failure(stderr));
}

#[test]
fn inserts_bwrap_argv0_before_command_separator() {
    let file_system_sandbox_policy = read_only_file_system_policy();
    let mut argv = build_bwrap_argv(
        vec!["/bin/true".to_string()],
        &file_system_sandbox_policy,
        Path::new("/"),
        Path::new("/"),
        BwrapOptions {
            mount_proc: true,
            network_mode: BwrapNetworkMode::FullAccess,
            ..Default::default()
        },
    )
    .expect("build bwrap argv")
    .args;
    apply_inner_command_argv0_for_launcher(
        &mut argv,
        /*supports_argv0*/ true,
        "/tmp/codex-arg0-session/codex-linux-sandbox".to_string(),
    );
    assert_eq!(
        argv,
        vec![
            "bwrap".to_string(),
            "--new-session".to_string(),
            "--die-with-parent".to_string(),
            "--ro-bind".to_string(),
            "/".to_string(),
            "/".to_string(),
            "--dev".to_string(),
            "/dev".to_string(),
            "--unshare-user".to_string(),
            "--unshare-pid".to_string(),
            "--proc".to_string(),
            "/proc".to_string(),
            "--argv0".to_string(),
            "codex-linux-sandbox".to_string(),
            "--".to_string(),
            "/bin/true".to_string(),
        ]
    );
}

#[test]
fn rewrites_inner_command_path_when_bwrap_lacks_argv0() {
    let file_system_sandbox_policy = read_only_file_system_policy();
    let mut argv = build_bwrap_argv(
        vec!["/bin/true".to_string()],
        &file_system_sandbox_policy,
        Path::new("/"),
        Path::new("/"),
        BwrapOptions {
            mount_proc: true,
            network_mode: BwrapNetworkMode::FullAccess,
            ..Default::default()
        },
    )
    .expect("build bwrap argv")
    .args;
    apply_inner_command_argv0_for_launcher(
        &mut argv,
        /*supports_argv0*/ false,
        "/tmp/codex-arg0-session/codex-linux-sandbox".to_string(),
    );

    assert!(!argv.iter().any(|arg| arg == "--argv0"));
    assert!(
        argv.windows(2)
            .any(|window| { window == ["--", "/tmp/codex-arg0-session/codex-linux-sandbox"] })
    );
}

#[test]
fn rewrites_bwrap_helper_command_not_nested_user_command_when_current_exe_appears_later() {
    let nested_current_exe = std::env::current_exe()
        .expect("current exe")
        .to_string_lossy()
        .into_owned();
    let mut argv = vec![
        "bwrap".to_string(),
        "--".to_string(),
        "/tmp/helper-symlink".to_string(),
        "--sandbox-policy-cwd".to_string(),
        "/tmp/cwd".to_string(),
        "--".to_string(),
        nested_current_exe.clone(),
        "--codex-run-as-apply-patch".to_string(),
        "patch".to_string(),
    ];

    apply_inner_command_argv0_for_launcher(
        &mut argv,
        /*supports_argv0*/ false,
        "/tmp/argv0-fallback-helper".to_string(),
    );

    assert_eq!(
        argv,
        vec![
            "bwrap".to_string(),
            "--".to_string(),
            "/tmp/argv0-fallback-helper".to_string(),
            "--sandbox-policy-cwd".to_string(),
            "/tmp/cwd".to_string(),
            "--".to_string(),
            nested_current_exe,
            "--codex-run-as-apply-patch".to_string(),
            "patch".to_string(),
        ]
    );
}

#[test]
fn inserts_unshare_net_when_network_isolation_requested() {
    let file_system_sandbox_policy = read_only_file_system_policy();
    let argv = build_bwrap_argv(
        vec!["/bin/true".to_string()],
        &file_system_sandbox_policy,
        Path::new("/"),
        Path::new("/"),
        BwrapOptions {
            mount_proc: true,
            network_mode: BwrapNetworkMode::Isolated,
            ..Default::default()
        },
    )
    .expect("build bwrap argv")
    .args;
    assert!(argv.contains(&"--unshare-net".to_string()));
}

#[test]
fn inserts_unshare_net_when_proxy_only_network_mode_requested() {
    let file_system_sandbox_policy = read_only_file_system_policy();
    let argv = build_bwrap_argv(
        vec!["/bin/true".to_string()],
        &file_system_sandbox_policy,
        Path::new("/"),
        Path::new("/"),
        BwrapOptions {
            mount_proc: true,
            network_mode: BwrapNetworkMode::ProxyOnly,
            ..Default::default()
        },
    )
    .expect("build bwrap argv")
    .args;
    assert!(argv.contains(&"--unshare-net".to_string()));
}

#[test]
fn proxy_only_mode_takes_precedence_over_full_network_policy() {
    let mode = bwrap_network_mode(
        NetworkSandboxPolicy::Enabled,
        /*allow_network_for_proxy*/ true,
    );
    assert_eq!(mode, BwrapNetworkMode::ProxyOnly);
}

#[test]
fn split_only_filesystem_policy_requires_direct_runtime_enforcement() {
    let temp_dir = tempfile::TempDir::new().expect("tempdir");
    let docs = temp_dir.path().join("docs");
    std::fs::create_dir_all(&docs).expect("create docs");
    let docs = AbsolutePathBuf::from_absolute_path(&docs).expect("absolute docs");
    let policy = FileSystemSandboxPolicy::restricted(vec![
        codex_protocol::permissions::FileSystemSandboxEntry {
            path: codex_protocol::permissions::FileSystemPath::Special {
                value: codex_protocol::permissions::FileSystemSpecialPath::project_roots(
                    /*subpath*/ None,
                ),
            },
            access: codex_protocol::permissions::FileSystemAccessMode::Write,
            missing_path_behavior: None,
        },
        codex_protocol::permissions::FileSystemSandboxEntry {
            path: codex_protocol::permissions::FileSystemPath::Path { path: docs },
            access: codex_protocol::permissions::FileSystemAccessMode::Read,
            missing_path_behavior: None,
        },
    ]);

    assert!(
        policy.needs_direct_runtime_enforcement(NetworkSandboxPolicy::Restricted, temp_dir.path(),)
    );
}

#[test]
fn root_write_read_only_carveout_requires_direct_runtime_enforcement() {
    let temp_dir = tempfile::TempDir::new().expect("tempdir");
    let docs = temp_dir.path().join("docs");
    std::fs::create_dir_all(&docs).expect("create docs");
    let docs = AbsolutePathBuf::from_absolute_path(&docs).expect("absolute docs");
    let policy = FileSystemSandboxPolicy::restricted(vec![
        codex_protocol::permissions::FileSystemSandboxEntry {
            path: codex_protocol::permissions::FileSystemPath::Special {
                value: codex_protocol::permissions::FileSystemSpecialPath::Root,
            },
            access: codex_protocol::permissions::FileSystemAccessMode::Write,
            missing_path_behavior: None,
        },
        codex_protocol::permissions::FileSystemSandboxEntry {
            path: codex_protocol::permissions::FileSystemPath::Path { path: docs },
            access: codex_protocol::permissions::FileSystemAccessMode::Read,
            missing_path_behavior: None,
        },
    ]);

    assert!(
        policy.needs_direct_runtime_enforcement(NetworkSandboxPolicy::Restricted, temp_dir.path(),)
    );
}

#[test]
fn managed_proxy_preflight_argv_unshares_network() {
    let mode = bwrap_network_mode(
        NetworkSandboxPolicy::Enabled,
        /*allow_network_for_proxy*/ true,
    );
    let argv = build_preflight_bwrap_argv(mode, /*mount_proc*/ true)
        .expect("build preflight argv")
        .args;
    assert!(argv.iter().any(|arg| arg == "--"));
    assert!(argv.iter().any(|arg| arg == "--unshare-net"));
}

#[test]
fn proc_mount_preflight_does_not_bind_the_full_filesystem() {
    let argv = build_preflight_bwrap_argv(BwrapNetworkMode::FullAccess, /*mount_proc*/ true)
        .expect("build preflight argv")
        .args;

    assert!(argv.windows(2).any(|window| window == ["--tmpfs", "/"]));
    assert!(argv.windows(2).any(|window| window == ["--proc", "/proc"]));
    assert!(
        !argv
            .windows(3)
            .any(|window| window == ["--ro-bind", "/", "/"])
    );
    assert!(!argv.windows(3).any(|window| window == ["--bind", "/", "/"]));
}

#[test]
fn network_preflight_preserves_proc_mount_fallback() {
    let argv = build_preflight_bwrap_argv(BwrapNetworkMode::Isolated, /*mount_proc*/ false)
        .expect("build preflight argv")
        .args;

    assert!(argv.windows(2).any(|window| window == ["--tmpfs", "/"]));
    assert!(!argv.iter().any(|arg| arg == "--proc"));
    assert!(argv.iter().any(|arg| arg == "--unshare-net"));
}

#[test]
fn cleanup_synthetic_mount_targets_removes_only_empty_mount_targets() {
    let temp_dir = tempfile::TempDir::new().expect("tempdir");
    let empty_file = temp_dir.path().join(".git");
    let empty_dir = temp_dir.path().join(".agents");
    let non_empty_file = temp_dir.path().join("non-empty");
    let missing_file = temp_dir.path().join(".missing");
    std::fs::write(&empty_file, "").expect("write empty file");
    std::fs::create_dir(&empty_dir).expect("create empty dir");
    std::fs::write(&non_empty_file, "keep").expect("write nonempty file");

    let registrations = register_synthetic_mount_targets(&[
        crate::bwrap::SyntheticMountTarget::missing(&empty_file),
        crate::bwrap::SyntheticMountTarget::missing_empty_directory(&empty_dir),
        crate::bwrap::SyntheticMountTarget::missing(&non_empty_file),
        crate::bwrap::SyntheticMountTarget::missing(&missing_file),
    ]);
    cleanup_synthetic_mount_targets(&registrations);

    assert!(!empty_file.exists());
    assert!(!empty_dir.exists());
    assert_eq!(
        std::fs::read_to_string(&non_empty_file).expect("read nonempty file"),
        "keep"
    );
    assert!(!missing_file.exists());
}

#[test]
fn synthetic_mount_registry_root_is_unique_to_effective_user() {
    let effective_uid = unsafe { libc::geteuid() };
    assert_eq!(
        synthetic_mount_registry_root(),
        std::env::temp_dir().join(format!(
            "codex-bwrap-synthetic-mount-targets-{effective_uid}"
        ))
    );
}

#[test]
fn cleanup_synthetic_mount_targets_waits_for_other_active_registrations() {
    let temp_dir = tempfile::TempDir::new().expect("tempdir");
    let empty_file = temp_dir.path().join(".git");
    std::fs::write(&empty_file, "").expect("write empty file");
    let target = crate::bwrap::SyntheticMountTarget::missing(&empty_file);

    let registrations = register_synthetic_mount_targets(std::slice::from_ref(&target));
    let active_marker = registrations[0].marker_dir.join("1");
    std::fs::write(&active_marker, "").expect("write active marker");

    cleanup_synthetic_mount_targets(&registrations);
    assert!(empty_file.exists());

    std::fs::remove_file(active_marker).expect("remove active marker");
    let registrations = register_synthetic_mount_targets(std::slice::from_ref(&target));
    cleanup_synthetic_mount_targets(&registrations);

    assert!(!empty_file.exists());
}

#[test]
fn cleanup_synthetic_mount_targets_removes_transient_file_after_concurrent_owner_exits() {
    let temp_dir = tempfile::TempDir::new().expect("tempdir");
    let empty_file = temp_dir.path().join(".git");
    let first_target = crate::bwrap::SyntheticMountTarget::missing(&empty_file);

    let first_registrations = register_synthetic_mount_targets(&[first_target]);
    std::fs::write(&empty_file, "").expect("write transient empty file");
    let active_marker = first_registrations[0].marker_dir.join("1");
    std::fs::write(&active_marker, SYNTHETIC_MOUNT_MARKER_SYNTHETIC).expect("write active marker");
    let metadata = std::fs::symlink_metadata(&empty_file).expect("stat empty file");
    let second_target =
        crate::bwrap::SyntheticMountTarget::existing_empty_file(&empty_file, &metadata);
    let second_registrations = register_synthetic_mount_targets(&[second_target]);

    cleanup_synthetic_mount_targets(&first_registrations);
    assert!(empty_file.exists());

    std::fs::remove_file(active_marker).expect("remove active marker");
    cleanup_synthetic_mount_targets(&second_registrations);

    assert!(!empty_file.exists());
}

#[test]
fn cleanup_synthetic_mount_targets_preserves_real_pre_existing_empty_file() {
    let temp_dir = tempfile::TempDir::new().expect("tempdir");
    let empty_file = temp_dir.path().join(".git");
    std::fs::write(&empty_file, "").expect("write pre-existing empty file");
    let metadata = std::fs::symlink_metadata(&empty_file).expect("stat empty file");
    let first_target =
        crate::bwrap::SyntheticMountTarget::existing_empty_file(&empty_file, &metadata);
    let second_target =
        crate::bwrap::SyntheticMountTarget::existing_empty_file(&empty_file, &metadata);

    let first_registrations = register_synthetic_mount_targets(&[first_target]);
    let second_registrations = register_synthetic_mount_targets(&[second_target]);

    cleanup_synthetic_mount_targets(&first_registrations);
    cleanup_synthetic_mount_targets(&second_registrations);

    assert!(empty_file.exists());
}

#[test]
fn cleanup_protected_create_targets_removes_created_path_and_reports_violation() {
    let temp_dir = tempfile::TempDir::new().expect("tempdir");
    let dot_git = temp_dir.path().join(".git");
    let target = crate::bwrap::ProtectedCreateTarget::missing(&dot_git);

    let registrations = register_protected_create_targets(&[target]);
    std::fs::create_dir(&dot_git).expect("create protected path");
    let violation = cleanup_protected_create_targets(&registrations);

    assert!(violation);
    assert!(!dot_git.exists());
}

#[test]
fn cleanup_protected_create_targets_removes_nested_inaccessible_tree() {
    use std::os::unix::fs::PermissionsExt;

    let temp_dir = tempfile::TempDir::new().expect("tempdir");
    let dot_git = temp_dir.path().join(".git");
    let nested = dot_git.join("objects").join("pack");
    let target = crate::bwrap::ProtectedCreateTarget::missing(&dot_git);
    let registrations = register_protected_create_targets(&[target]);
    std::fs::create_dir_all(&nested).expect("create protected tree");
    std::fs::write(nested.join("object"), "blocked").expect("write protected child");
    std::fs::set_permissions(&nested, std::fs::Permissions::from_mode(0o000))
        .expect("make nested protected directory inaccessible");

    let violation = cleanup_protected_create_targets(&registrations);

    assert!(violation);
    assert!(!dot_git.exists());
    assert!(!registrations[0].marker_dir.exists());
}

#[test]
fn cleanup_protected_create_targets_waits_for_other_active_registrations() {
    let temp_dir = tempfile::TempDir::new().expect("tempdir");
    let dot_git = temp_dir.path().join(".git");
    let target = crate::bwrap::ProtectedCreateTarget::missing(&dot_git);

    let registrations = register_protected_create_targets(std::slice::from_ref(&target));
    let active_marker = registrations[0].marker_dir.join("1");
    std::fs::write(&active_marker, PROTECTED_CREATE_MARKER).expect("write active marker");
    std::fs::write(&dot_git, "").expect("create protected path");

    let violation = cleanup_protected_create_targets(&registrations);
    assert!(violation);
    assert!(dot_git.exists());

    std::fs::remove_file(active_marker).expect("remove active marker");
    let registrations = register_protected_create_targets(std::slice::from_ref(&target));
    let violation = cleanup_protected_create_targets(&registrations);

    assert!(violation);
    assert!(!dot_git.exists());
}

#[test]
fn bwrap_signal_forwarder_terminates_child_and_keeps_parent_alive() {
    let supervisor_pid = unsafe { libc::fork() };
    assert!(supervisor_pid >= 0, "failed to fork supervisor");

    if supervisor_pid == 0 {
        run_bwrap_signal_forwarder_test_supervisor();
    }

    let status = wait_for_bwrap_child(supervisor_pid);
    assert!(libc::WIFEXITED(status), "supervisor status: {status}");
    assert_eq!(libc::WEXITSTATUS(status), 0);
}

#[test]
fn reparented_namespace_pid_one_validation_uses_nspid_not_ppid() {
    // The outer launcher may already have exited when the detached namespace
    // reaper is observed. The accepted identity is its namespace PID, not a
    // host-parent relationship that has legitimately changed under reaping.
    let before_reparent = "Name:\tbwrap\nPPid:\t4312\nNSpid:\t4312 1\n";
    let after_reparent = "Name:\tbwrap\nPPid:\t1\nNSpid:\t4312 1\n";
    let non_reaper = "Name:\tbwrap\nPPid:\t1\nNSpid:\t4312 2\n";

    assert!(namespace_status_is_pid_one(before_reparent));
    assert!(namespace_status_is_pid_one(after_reparent));
    assert!(!namespace_status_is_pid_one(non_reaper));
}

#[test]
fn parses_bwrap_child_pid_from_strict_json() {
    assert_eq!(
        parse_bwrap_child_pid(r#"{"child-pid":123,"other":true}"#),
        Some(123)
    );
    assert_eq!(
        parse_bwrap_child_pid(
            r#"{
                "other": true,
                "child-pid": 456
            }"#
        ),
        Some(456)
    );
}

#[test]
fn rejects_invalid_bwrap_child_pid_json() {
    for info in [
        "",
        "not-json",
        r#"{"child-pid":0}"#,
        r#"{"child-pid":-1}"#,
        r#"{"child-pid":"123"}"#,
        r#"{"other":123}"#,
        r#"[{"child-pid":123}]"#,
    ] {
        assert_eq!(parse_bwrap_child_pid(info), None, "input: {info:?}");
    }
}

#[test]
fn zombie_marker_owner_is_not_treated_as_live_custody() {
    let child = unsafe { libc::fork() };
    assert!(child >= 0, "fork zombie owner");
    if child == 0 {
        unsafe { libc::_exit(0) };
    }
    loop {
        if process_is_zombie(child) {
            break;
        }
        std::thread::yield_now();
    }
    assert!(!process_is_active(child), "zombie must not retain a marker");
    let mut status = 0;
    assert_eq!(unsafe { libc::waitpid(child, &mut status, 0) }, child);
}

#[test]
fn detached_status_writer_is_close_on_exec_before_command_runs() {
    let pipe = create_detached_command_status_pipe();
    mark_fd_close_on_exec(pipe[1]);
    let flags = unsafe { libc::fcntl(pipe[1], libc::F_GETFD) };
    assert!(flags & libc::FD_CLOEXEC != 0);
    close_fd_if_open(pipe[0]);
    close_fd_if_open(pipe[1]);
}

#[test]
fn execed_command_cannot_forge_detached_status_writer() {
    let pipe = create_detached_command_status_pipe();
    let child = unsafe { libc::fork() };
    assert!(child >= 0, "fork forged-status command");
    if child == 0 {
        close_fd_if_open(pipe[0]);
        mark_fd_close_on_exec(pipe[1]);
        let fd = pipe[1].to_string();
        let shell = std::ffi::CString::new("/bin/sh").expect("shell cstring");
        let arg0 = std::ffi::CString::new("sh").expect("arg0 cstring");
        let dash_c = std::ffi::CString::new("-c").expect("-c cstring");
        let script = std::ffi::CString::new("printf '\\377\\377\\377\\377' >&\"$1\"")
            .expect("script cstring");
        let argument = std::ffi::CString::new(fd).expect("fd cstring");
        unsafe {
            libc::execl(
                shell.as_ptr(),
                arg0.as_ptr(),
                dash_c.as_ptr(),
                script.as_ptr(),
                arg0.as_ptr(),
                argument.as_ptr(),
                std::ptr::null::<libc::c_char>(),
            );
            libc::_exit(127);
        }
    }
    close_fd_if_open(pipe[1]);
    let mut byte = [0_u8; 1];
    assert_eq!(
        unsafe { libc::read(pipe[0], byte.as_mut_ptr().cast(), 1) },
        0
    );
    close_fd_if_open(pipe[0]);
    let status = wait_for_bwrap_child(child);
    assert!(libc::WIFEXITED(status));
    assert_ne!(libc::WEXITSTATUS(status), 0, "forgery command must fail");
}

#[test]
fn post_ready_monitor_death_kills_namespace_and_cleans_protected_marker() {
    let temp_dir = tempfile::TempDir::new().expect("tempdir");
    let protected_path = temp_dir.path().join(".git");
    let target = crate::bwrap::ProtectedCreateTarget::missing(&protected_path);
    let mut registrations = register_protected_create_targets(std::slice::from_ref(&target));
    assert!(
        !protected_path.exists(),
        "the protected target must be absent before monitor readiness"
    );

    let namespace_pid = unsafe { libc::fork() };
    assert!(namespace_pid >= 0, "fork namespace stand-in");
    if namespace_pid == 0 {
        loop {
            unsafe { libc::pause() };
        }
    }
    let raw_pidfd = unsafe { libc::syscall(libc::SYS_pidfd_open, namespace_pid, 0) };
    assert!(raw_pidfd >= 0, "open pidfd for namespace stand-in");
    let namespace_init = BwrapNamespaceInit {
        pid: namespace_pid,
        pidfd: unsafe { OwnedFd::from_raw_fd(raw_pidfd as libc::c_int) },
    };
    transfer_protected_create_registrations(&mut registrations, namespace_pid);

    let mut ready_pipe = [-1, -1];
    assert_eq!(unsafe { libc::pipe(ready_pipe.as_mut_ptr()) }, 0);
    FORCE_DETACHED_MONITOR_EXIT_AFTER_READY.store(true, Ordering::SeqCst);
    let supervisor_pid = unsafe { libc::fork() };
    assert!(supervisor_pid >= 0, "fork supervisor");
    if supervisor_pid == 0 {
        close_fd_if_open(ready_pipe[0]);
        supervise_protected_create_monitor(
            namespace_init,
            vec![target],
            registrations,
            ready_pipe[1],
        );
    }
    close_fd_if_open(ready_pipe[1]);
    let mut ready = [0_u8; 1];
    assert_eq!(
        unsafe { libc::read(ready_pipe[0], ready.as_mut_ptr().cast(), 1) },
        1
    );
    assert_eq!(ready, [1]);
    close_fd_if_open(ready_pipe[0]);

    let status = wait_for_bwrap_child(supervisor_pid);
    FORCE_DETACHED_MONITOR_EXIT_AFTER_READY.store(false, Ordering::SeqCst);
    assert!(libc::WIFEXITED(status));
    assert_ne!(libc::WEXITSTATUS(status), 0, "supervisor must fail closed");
    assert!(
        !process_is_active(namespace_pid),
        "supervisor must kill namespace"
    );
    assert!(!protected_path.exists(), "protected target must be removed");
    assert!(
        !synthetic_mount_marker_dir(&protected_path).exists(),
        "marker must be removed"
    );
}

#[cfg(test)]
fn run_bwrap_signal_forwarder_test_supervisor() -> ! {
    let child_pid = unsafe { libc::fork() };
    if child_pid < 0 {
        unsafe {
            libc::_exit(2);
        }
    }

    if child_pid == 0 {
        loop {
            unsafe {
                libc::pause();
            }
        }
    }

    install_bwrap_signal_forwarders(child_pid);
    unsafe {
        libc::raise(libc::SIGTERM);
    }

    let status = wait_for_bwrap_child(child_pid);
    let child_terminated_by_sigterm =
        libc::WIFSIGNALED(status) && libc::WTERMSIG(status) == libc::SIGTERM;
    unsafe {
        libc::_exit(if child_terminated_by_sigterm { 0 } else { 1 });
    }
}

#[test]
fn managed_proxy_inner_command_includes_route_spec() {
    let permission_profile = read_only_permission_profile();
    let args = build_inner_seccomp_command(InnerSeccompCommandArgs {
        sandbox_policy_cwd: Path::new("/tmp"),
        command_cwd: Some(Path::new("/tmp/link")),
        permission_profile: &permission_profile,
        allow_network_for_proxy: true,
        allow_detached_children: false,
        proxy_route_spec: Some("{\"routes\":[]}".to_string()),
        command: vec!["/bin/true".to_string()],
    });

    assert!(args.iter().any(|arg| arg == "--proxy-route-spec"));
    assert!(args.iter().any(|arg| arg == "{\"routes\":[]}"));
}

#[test]
fn inner_command_includes_permission_profile_flag() {
    let permission_profile = read_only_permission_profile();
    let args = build_inner_seccomp_command(InnerSeccompCommandArgs {
        sandbox_policy_cwd: Path::new("/tmp"),
        command_cwd: Some(Path::new("/tmp/link")),
        permission_profile: &permission_profile,
        allow_network_for_proxy: false,
        allow_detached_children: false,
        proxy_route_spec: None,
        command: vec!["/bin/true".to_string()],
    });

    assert!(args.iter().any(|arg| arg == "--permission-profile"));
    assert!(
        args.windows(2)
            .any(|window| { window == ["--command-cwd", "/tmp/link"] })
    );
}

#[test]
fn non_managed_inner_command_omits_route_spec() {
    let permission_profile = read_only_permission_profile();
    let args = build_inner_seccomp_command(InnerSeccompCommandArgs {
        sandbox_policy_cwd: Path::new("/tmp"),
        command_cwd: Some(Path::new("/tmp/link")),
        permission_profile: &permission_profile,
        allow_network_for_proxy: false,
        allow_detached_children: false,
        proxy_route_spec: None,
        command: vec!["/bin/true".to_string()],
    });

    assert!(!args.iter().any(|arg| arg == "--proxy-route-spec"));
}

#[test]
fn bwrap_bootstrap_policy_leaves_full_read_policy_unchanged() {
    let policy = read_only_file_system_policy();

    assert_eq!(
        file_system_policy_with_bwrap_bootstrap_roots(&policy),
        policy
    );
}

#[test]
fn bwrap_bootstrap_policy_adds_helper_and_minimal_runtime_roots() {
    let temp_dir = tempfile::TempDir::new().expect("tempdir");
    let workspace = temp_dir.path().join("workspace");
    std::fs::create_dir_all(&workspace).expect("create workspace");
    let workspace = AbsolutePathBuf::from_absolute_path(&workspace).expect("absolute workspace");
    let requested = FileSystemSandboxPolicy::restricted(vec![
        codex_protocol::permissions::FileSystemSandboxEntry::new(
            codex_protocol::permissions::FileSystemPath::Path { path: workspace },
            codex_protocol::permissions::FileSystemAccessMode::Write,
        ),
    ]);
    let current_exe =
        AbsolutePathBuf::from_absolute_path(std::env::current_exe().expect("current exe"))
            .expect("absolute current exe");
    let current_exe_parent = current_exe
        .parent()
        .expect("current executable should have a parent");

    assert!(!requested.include_platform_defaults());
    assert!(
        !requested.can_read_path_with_cwd(current_exe_parent.as_path(), temp_dir.path()),
        "requested policy should not already expose the test helper dir"
    );

    let bootstrap = file_system_policy_with_bwrap_bootstrap_roots(&requested);

    assert!(bootstrap.include_platform_defaults());
    assert!(bootstrap.entries.iter().any(|entry| {
        matches!(
            &entry.path,
            codex_protocol::permissions::FileSystemPath::Special {
                value: codex_protocol::permissions::FileSystemSpecialPath::Minimal,
            }
        ) && entry.access == codex_protocol::permissions::FileSystemAccessMode::Read
            && entry.missing_path_behavior.is_none()
    }));
    assert!(
        bootstrap.can_read_path_with_cwd(current_exe_parent.as_path(), temp_dir.path()),
        "bwrap must be able to exec the inner sandbox helper stage"
    );
}

#[test]
fn managed_proxy_inner_command_requires_route_spec() {
    let result = std::panic::catch_unwind(|| {
        let permission_profile = read_only_permission_profile();
        build_inner_seccomp_command(InnerSeccompCommandArgs {
            sandbox_policy_cwd: Path::new("/tmp"),
            command_cwd: Some(Path::new("/tmp/link")),
            permission_profile: &permission_profile,
            allow_network_for_proxy: true,
            allow_detached_children: false,
            proxy_route_spec: None,
            command: vec!["/bin/true".to_string()],
        })
    });
    assert!(result.is_err());
}

#[test]
fn resolve_permission_profile_derives_runtime_policies() {
    let permission_profile = read_only_permission_profile();
    let resolved = resolve_permission_profile(Some(permission_profile.clone()))
        .expect("profile should resolve");

    assert_eq!(resolved.permission_profile, permission_profile);
    assert_eq!(
        resolved.file_system_sandbox_policy,
        read_only_file_system_policy()
    );
    assert_eq!(
        resolved.network_sandbox_policy,
        NetworkSandboxPolicy::Restricted
    );
}

#[test]
fn resolve_permission_profile_preserves_direct_runtime_profile() {
    let temp_dir = tempfile::TempDir::new().expect("tempdir");
    let docs = temp_dir.path().join("docs");
    std::fs::create_dir_all(&docs).expect("create docs");
    let docs = AbsolutePathBuf::from_absolute_path(&docs).expect("absolute docs");
    let file_system_sandbox_policy = FileSystemSandboxPolicy::restricted(vec![
        codex_protocol::permissions::FileSystemSandboxEntry {
            path: codex_protocol::permissions::FileSystemPath::Special {
                value: codex_protocol::permissions::FileSystemSpecialPath::Root,
            },
            access: codex_protocol::permissions::FileSystemAccessMode::Read,
            missing_path_behavior: None,
        },
        codex_protocol::permissions::FileSystemSandboxEntry {
            path: codex_protocol::permissions::FileSystemPath::Path { path: docs },
            access: codex_protocol::permissions::FileSystemAccessMode::Write,
            missing_path_behavior: None,
        },
    ]);
    let permission_profile = PermissionProfile::from_runtime_permissions(
        &file_system_sandbox_policy,
        NetworkSandboxPolicy::Restricted,
    );
    let resolved = resolve_permission_profile(Some(permission_profile.clone()))
        .expect("profile should resolve");

    assert_eq!(resolved.permission_profile, permission_profile);
    assert_eq!(
        resolved.file_system_sandbox_policy,
        file_system_sandbox_policy
    );
    assert_eq!(
        resolved.network_sandbox_policy,
        NetworkSandboxPolicy::Restricted
    );
}

#[test]
fn resolve_permission_profile_rejects_missing_configuration() {
    let err = resolve_permission_profile(/*permission_profile*/ None)
        .expect_err("missing profile should fail");

    assert_eq!(err, ResolvePermissionProfileError::MissingConfiguration);
}

#[test]
fn apply_seccomp_then_exec_with_legacy_landlock_panics() {
    let result = std::panic::catch_unwind(|| {
        ensure_inner_stage_mode_is_valid(
            /*apply_seccomp_then_exec*/ true, /*use_legacy_landlock*/ true,
        )
    });
    assert!(result.is_err());
}

#[test]
fn legacy_landlock_rejects_split_only_filesystem_policies() {
    let temp_dir = tempfile::TempDir::new().expect("tempdir");
    let docs = temp_dir.path().join("docs");
    std::fs::create_dir_all(&docs).expect("create docs");
    let docs = AbsolutePathBuf::from_absolute_path(&docs).expect("absolute docs");
    let policy = FileSystemSandboxPolicy::restricted(vec![
        codex_protocol::permissions::FileSystemSandboxEntry {
            path: codex_protocol::permissions::FileSystemPath::Special {
                value: codex_protocol::permissions::FileSystemSpecialPath::Root,
            },
            access: codex_protocol::permissions::FileSystemAccessMode::Read,
            missing_path_behavior: None,
        },
        codex_protocol::permissions::FileSystemSandboxEntry {
            path: codex_protocol::permissions::FileSystemPath::Path { path: docs },
            access: codex_protocol::permissions::FileSystemAccessMode::Write,
            missing_path_behavior: None,
        },
    ]);

    let result = std::panic::catch_unwind(|| {
        ensure_legacy_landlock_mode_supports_policy(
            /*use_legacy_landlock*/ true,
            &policy,
            NetworkSandboxPolicy::Restricted,
            temp_dir.path(),
        );
    });

    assert!(result.is_err());
}

#[test]
fn valid_inner_stage_modes_do_not_panic() {
    ensure_inner_stage_mode_is_valid(
        /*apply_seccomp_then_exec*/ false, /*use_legacy_landlock*/ false,
    );
    ensure_inner_stage_mode_is_valid(
        /*apply_seccomp_then_exec*/ false, /*use_legacy_landlock*/ true,
    );
    ensure_inner_stage_mode_is_valid(
        /*apply_seccomp_then_exec*/ true, /*use_legacy_landlock*/ false,
    );
}
