#[cfg(any(not(debug_assertions), test))]
use codex_install_context::InstallContext;
#[cfg(any(not(debug_assertions), test))]
use codex_install_context::InstallMethod;
#[cfg(any(not(debug_assertions), test))]
use codex_install_context::StandalonePlatform;
use codex_utils_version::SednaReleaseChannel;

/// Update action the CLI should perform after the TUI exits.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum UpdateAction {
    /// Update via the fork-owned standalone release installer.
    StandaloneUnix(SednaReleaseChannel),
}

impl UpdateAction {
    #[cfg(not(debug_assertions))]
    pub(crate) fn from_install_context(
        context: &InstallContext,
        release_channel: SednaReleaseChannel,
    ) -> Option<Self> {
        Self::from_install_context_for_sedna_release_with_channel(
            context,
            crate::version::is_sedna_release_channel(),
            crate::version::CODEX_CLI_VERSION,
            release_channel,
        )
    }

    #[cfg(any(not(debug_assertions), test))]
    fn from_install_context_for_sedna_release(
        context: &InstallContext,
        has_sedna_identity: bool,
        running_release_version: &str,
    ) -> Option<Self> {
        Self::from_install_context_for_sedna_release_with_channel(
            context,
            has_sedna_identity,
            running_release_version,
            SednaReleaseChannel::Stable,
        )
    }

    #[cfg(any(not(debug_assertions), test))]
    fn from_install_context_for_sedna_release_with_channel(
        context: &InstallContext,
        has_sedna_identity: bool,
        running_release_version: &str,
        release_channel: SednaReleaseChannel,
    ) -> Option<Self> {
        Self::from_install_context_for_sedna_release_on_target_with_channel(
            context,
            has_sedna_identity,
            running_release_version,
            std::env::consts::OS,
            std::env::consts::ARCH,
            release_channel,
        )
    }

    #[cfg(any(not(debug_assertions), test))]
    fn from_install_context_for_sedna_release_on_target(
        context: &InstallContext,
        has_sedna_identity: bool,
        running_release_version: &str,
        target_os: &str,
        target_arch: &str,
    ) -> Option<Self> {
        Self::from_install_context_for_sedna_release_on_target_with_channel(
            context,
            has_sedna_identity,
            running_release_version,
            target_os,
            target_arch,
            SednaReleaseChannel::Stable,
        )
    }

    #[cfg(any(not(debug_assertions), test))]
    fn from_install_context_for_sedna_release_on_target_with_channel(
        context: &InstallContext,
        has_sedna_identity: bool,
        running_release_version: &str,
        target_os: &str,
        target_arch: &str,
        release_channel: SednaReleaseChannel,
    ) -> Option<Self> {
        if !has_sedna_identity
            || !codex_utils_version::is_sedna_automatic_update_eligible_for_channel(
                running_release_version,
                target_os,
                target_arch,
                release_channel,
            )
        {
            return None;
        }
        match &context.method {
            InstallMethod::Npm | InstallMethod::Bun | InstallMethod::Pnpm | InstallMethod::Brew => {
                None
            }
            InstallMethod::Standalone { platform, .. } => Some(match platform {
                StandalonePlatform::Unix => UpdateAction::StandaloneUnix(release_channel),
                StandalonePlatform::Windows => return None,
            }),
            InstallMethod::Other => None,
        }
    }

    /// Returns the list of command-line arguments for invoking the update.
    pub fn command_args(self) -> (&'static str, Vec<String>) {
        Self::sedna_standalone_unix_command_args(match self {
            Self::StandaloneUnix(channel) => channel,
        })
    }

    fn sedna_standalone_unix_command_args(
        release_channel: SednaReleaseChannel,
    ) -> (&'static str, Vec<String>) {
        let allow_prerelease = if release_channel == SednaReleaseChannel::Prerelease {
            " --allow-prerelease"
        } else {
            ""
        };
        (
            "bash",
            vec![
                "-c".to_string(),
                format!(
                    "curl -fsSL https://raw.githubusercontent.com/sednalabs/codex/main/scripts/install_sedna_release_asset | CODEX_NON_INTERACTIVE=1 bash -s -- --repository sednalabs/codex --release-tag latest --require-newer-than {}{}",
                    crate::version::CODEX_CLI_VERSION,
                    allow_prerelease,
                ),
            ],
        )
    }

    /// Returns string representation of the command-line arguments for invoking the update.
    pub fn command_str(self) -> String {
        let (command, args) = self.command_args();
        shlex::try_join(std::iter::once(command).chain(args.iter().map(String::as_str)))
            .unwrap_or_else(|_| format!("{command} {}", args.join(" ")))
    }
}

#[cfg(not(debug_assertions))]
pub fn get_update_action(release_channel: SednaReleaseChannel) -> Option<UpdateAction> {
    UpdateAction::from_install_context(InstallContext::current(), release_channel)
}

#[cfg(test)]
mod tests {
    use super::*;
    use codex_utils_absolute_path::AbsolutePathBuf;
    use pretty_assertions::assert_eq;

    #[test]
    fn maps_install_context_to_update_action_when_identity_is_present() {
        let native_release_dir =
            AbsolutePathBuf::from_absolute_path(std::env::temp_dir().join("native-release"))
                .expect("temp dir path should be absolute");

        assert_eq!(
            UpdateAction::from_install_context_for_sedna_release(
                &InstallContext {
                    method: InstallMethod::Other,
                    package_layout: None,
                },
                /*has_sedna_identity*/ true,
                "1.2.3-sedna.1"
            ),
            None
        );
        assert_eq!(
            UpdateAction::from_install_context_for_sedna_release(
                &InstallContext {
                    method: InstallMethod::Npm,
                    package_layout: None,
                },
                /*has_sedna_identity*/ true,
                "1.2.3-sedna.1"
            ),
            None
        );
        assert_eq!(
            UpdateAction::from_install_context_for_sedna_release(
                &InstallContext {
                    method: InstallMethod::Bun,
                    package_layout: None,
                },
                /*has_sedna_identity*/ true,
                "1.2.3-sedna.1"
            ),
            None
        );
        assert_eq!(
            UpdateAction::from_install_context_for_sedna_release(
                &InstallContext {
                    method: InstallMethod::Pnpm,
                    package_layout: None,
                },
                /*has_sedna_identity*/ true,
                "1.2.3-sedna.1"
            ),
            None
        );
        assert_eq!(
            UpdateAction::from_install_context_for_sedna_release(
                &InstallContext {
                    method: InstallMethod::Brew,
                    package_layout: None,
                },
                /*has_sedna_identity*/ true,
                "1.2.3-sedna.1"
            ),
            None
        );
        assert_eq!(
            UpdateAction::from_install_context_for_sedna_release_on_target(
                &InstallContext {
                    method: InstallMethod::Standalone {
                        platform: StandalonePlatform::Unix,
                        release_dir: native_release_dir.clone(),
                        resources_dir: Some(native_release_dir.join("codex-resources")),
                    },
                    package_layout: None,
                },
                /*has_sedna_identity*/ true,
                "1.2.3-sedna.1",
                "linux",
                "x86_64",
            ),
            Some(UpdateAction::StandaloneUnix(SednaReleaseChannel::Stable))
        );
        assert_eq!(
            UpdateAction::from_install_context_for_sedna_release(
                &InstallContext {
                    method: InstallMethod::Standalone {
                        platform: StandalonePlatform::Windows,
                        release_dir: native_release_dir.clone(),
                        resources_dir: Some(native_release_dir.join("codex-resources")),
                    },
                    package_layout: None,
                },
                /*has_sedna_identity*/ true,
                "1.2.3-sedna.1"
            ),
            None
        );
    }

    #[test]
    fn missing_identity_or_invalid_release_disables_standalone_update_action() {
        let native_release_dir =
            AbsolutePathBuf::from_absolute_path(std::env::temp_dir().join("native-release"))
                .expect("temp dir path should be absolute");
        assert_eq!(
            UpdateAction::from_install_context_for_sedna_release(
                &InstallContext {
                    method: InstallMethod::Standalone {
                        platform: StandalonePlatform::Unix,
                        release_dir: native_release_dir.clone(),
                        resources_dir: None,
                    },
                    package_layout: None,
                },
                /*has_sedna_identity*/ false,
                "1.2.3-sedna.1",
            ),
            None
        );
        assert_eq!(
            UpdateAction::from_install_context_for_sedna_release(
                &InstallContext {
                    method: InstallMethod::Standalone {
                        platform: StandalonePlatform::Unix,
                        release_dir: native_release_dir,
                        resources_dir: None,
                    },
                    package_layout: None,
                },
                /*has_sedna_identity*/ true,
                "1.2.3",
            ),
            None
        );
    }

    #[test]
    fn standalone_update_action_requires_a_supported_sedna_installer_target() {
        let native_release_dir =
            AbsolutePathBuf::from_absolute_path(std::env::temp_dir().join("native-release"))
                .expect("temp dir path should be absolute");
        let context = InstallContext {
            method: InstallMethod::Standalone {
                platform: StandalonePlatform::Unix,
                release_dir: native_release_dir,
                resources_dir: None,
            },
            package_layout: None,
        };

        assert_eq!(
            UpdateAction::from_install_context_for_sedna_release_on_target(
                &context,
                /*has_sedna_identity*/ true,
                "1.2.3-sedna.1",
                "linux",
                "x86_64",
            ),
            Some(UpdateAction::StandaloneUnix(SednaReleaseChannel::Stable))
        );
        for target in [
            ("macos", "x86_64"),
            ("macos", "aarch64"),
            ("freebsd", "x86_64"),
        ] {
            assert_eq!(
                UpdateAction::from_install_context_for_sedna_release_on_target(
                    &context,
                    /*has_sedna_identity*/ true,
                    "1.2.3-sedna.1",
                    target.0,
                    target.1,
                ),
                None,
                "accepted unsupported target {}-{}",
                target.0,
                target.1
            );
        }
        assert!(
            codex_utils_version::is_sedna_automatic_update_target_supported("linux", "aarch64")
        );
        assert!(
            !codex_utils_version::is_sedna_automatic_update_target_supported("macos", "x86_64")
        );
        assert_eq!(
            UpdateAction::from_install_context_for_sedna_release_on_target(
                &context,
                /*has_sedna_identity*/ true,
                "1.2.3-alpha.1-sedna.1",
                "linux",
                "x86_64",
            ),
            Some(UpdateAction::StandaloneUnix(SednaReleaseChannel::Stable))
        );
    }

    #[test]
    fn standalone_unix_update_uses_the_fork_installer() {
        assert_eq!(
            UpdateAction::StandaloneUnix(SednaReleaseChannel::Stable).command_args(),
            (
                "bash",
                vec![
                    "-c".to_string(),
                    format!(
                        "curl -fsSL https://raw.githubusercontent.com/sednalabs/codex/main/scripts/install_sedna_release_asset | CODEX_NON_INTERACTIVE=1 bash -s -- --repository sednalabs/codex --release-tag latest --require-newer-than {}",
                        crate::version::CODEX_CLI_VERSION,
                    ),
                ],
            )
        );
    }

    #[test]
    fn prerelease_channel_selects_prerelease_eligibility_and_installer_argument() {
        let native_release_dir =
            AbsolutePathBuf::from_absolute_path(std::env::temp_dir().join("native-release"))
                .expect("temp dir path should be absolute");
        let context = InstallContext {
            method: InstallMethod::Standalone {
                platform: StandalonePlatform::Unix,
                release_dir: native_release_dir,
                resources_dir: None,
            },
            package_layout: None,
        };

        let action = UpdateAction::from_install_context_for_sedna_release_on_target_with_channel(
            &context,
            /*has_sedna_identity*/ true,
            "1.2.3-alpha.1-sedna.1",
            "linux",
            "x86_64",
            SednaReleaseChannel::Prerelease,
        );
        assert_eq!(
            action,
            Some(UpdateAction::StandaloneUnix(
                SednaReleaseChannel::Prerelease
            ))
        );
        assert!(
            action
                .expect("prerelease action")
                .command_args()
                .1
                .join(" ")
                .contains("--allow-prerelease")
        );
    }
}
