//! Browser native computer-use provider shared by interactive and exec
//! frontends.

use codex_app_server_protocol::ComputerUseCallOutputContentItem;
use codex_app_server_protocol::ComputerUseCallParams;
use codex_app_server_protocol::ComputerUseCallResponse;
use codex_app_server_protocol::DynamicToolSpec;
use codex_protocol::dynamic_tools::DynamicToolCapability;
use codex_tools::BROWSER_OBSERVE_TOOL_NAME;
use codex_tools::BROWSER_STEP_TOOL_NAME;
use codex_tools::COMPUTER_USE_ADAPTER_BROWSER;
use codex_tools::native_computer_use_provider_for_call;
use serde::Deserialize;
use serde::Serialize;
use serde_json::Value;
use serde_json::json;
use std::path::Path;
use std::path::PathBuf;
use std::process::Stdio;
use std::time::Duration;
use tokio::io::AsyncWriteExt as _;
use tokio::process::Command;
use tokio::time::timeout;

const DEFAULT_REQUEST_TIMEOUT: Duration = Duration::from_secs(120);
const ENV_PROVIDER: &str = "CODEX_BROWSER_COMPUTER_USE_PROVIDER";
const ENV_COMMAND: &str = "CODEX_BROWSER_COMPUTER_USE_COMMAND";
const ENV_NODE: &str = "CODEX_BROWSER_COMPUTER_USE_NODE";
const ENV_TIMEOUT_SECS: &str = "CODEX_BROWSER_COMPUTER_USE_TIMEOUT_SECS";
const ENV_PLAYWRIGHT_STATE_DIR: &str = "CODEX_BROWSER_PLAYWRIGHT_STATE_DIR";
const ENV_PLAYWRIGHT_HEADLESS: &str = "CODEX_BROWSER_PLAYWRIGHT_HEADLESS";
const ENV_PLAYWRIGHT_NODE_PATH: &str = "CODEX_BROWSER_PLAYWRIGHT_NODE_PATH";
const ENV_PLAYWRIGHT_EXECUTABLE_PATH: &str = "CODEX_BROWSER_PLAYWRIGHT_EXECUTABLE_PATH";
const ENV_PLAYWRIGHT_CHANNEL: &str = "CODEX_BROWSER_PLAYWRIGHT_CHANNEL";
const ENV_PLAYWRIGHT_DISPLAY: &str = "CODEX_BROWSER_PLAYWRIGHT_DISPLAY";
const ENV_PLAYWRIGHT_CAPTURE_MODE: &str = "CODEX_BROWSER_PLAYWRIGHT_CAPTURE_MODE";
const ENV_PLAYWRIGHT_ISOLATION: &str = "CODEX_BROWSER_PLAYWRIGHT_ISOLATION";
const ENV_PLAYWRIGHT_VIEWPORT_WIDTH: &str = "CODEX_BROWSER_PLAYWRIGHT_VIEWPORT_WIDTH";
const ENV_PLAYWRIGHT_VIEWPORT_HEIGHT: &str = "CODEX_BROWSER_PLAYWRIGHT_VIEWPORT_HEIGHT";
const ENV_PLAYWRIGHT_ARTIFACT_DIR: &str = "CODEX_BROWSER_PLAYWRIGHT_ARTIFACT_DIR";
const ENV_PLAYWRIGHT_ARTIFACT_POLICY: &str = "CODEX_BROWSER_PLAYWRIGHT_ARTIFACT_POLICY";
const ENV_PLAYWRIGHT_ALLOW_CALL_HEADERS: &str = "CODEX_BROWSER_PLAYWRIGHT_ALLOW_CALL_HEADERS";
const ENV_PLAYWRIGHT_SERVICE_PROFILES: &str = "CODEX_BROWSER_PLAYWRIGHT_SERVICE_PROFILES_JSON";
const PROVIDER_COMMAND: &str = "command";
const PROVIDER_NONE: &str = "none";
const PROVIDER_PLAYWRIGHT: &str = "playwright";
const BACKEND_AUTO: &str = "auto";
const BACKEND_BROWSER: &str = "browser";
const BACKEND_CHROME: &str = "chrome";
const BACKEND_CHROMIUM: &str = "chromium";
const BACKEND_WILDCARD: &str = "*";

const PLAYWRIGHT_BRIDGE_SCRIPT: &str = include_str!("browser_playwright_provider.mjs");
const PLAYWRIGHT_REVIEW_SCRIPT: &str = include_str!("browser_playwright_review.mjs");
const PLAYWRIGHT_INSPECTION_SCRIPT: &str = include_str!("browser_playwright_inspection.mjs");
const PLAYWRIGHT_SERVICE_HEADERS_SCRIPT: &str =
    include_str!("browser_playwright_service_headers.mjs");

/// Return browser computer-use dynamic tools for the process default Codex home.
pub fn configured_browser_dynamic_tools() -> Vec<DynamicToolSpec> {
    let Some(codex_home) = default_codex_home() else {
        return Vec::new();
    };

    configured_browser_dynamic_tools_for_codex_home(codex_home.as_path())
}

/// Return browser computer-use dynamic tools for a specific Codex home.
pub fn configured_browser_dynamic_tools_for_codex_home(codex_home: &Path) -> Vec<DynamicToolSpec> {
    if BrowserRuntimeConfig::load(codex_home).is_none() {
        return Vec::new();
    }

    vec![
        browser_dynamic_tool(
            BROWSER_OBSERVE_TOOL_NAME,
            "Capture the current browser viewport as a model-visible screenshot.",
            "non_mutating",
        ),
        browser_dynamic_tool(
            BROWSER_STEP_TOOL_NAME,
            "Perform bounded browser actions, then return a fresh browser screenshot.",
            "mutating",
        ),
    ]
}

fn browser_dynamic_tool(name: &str, description: &str, mutation_class: &str) -> DynamicToolSpec {
    DynamicToolSpec {
        namespace: None,
        name: name.to_string(),
        description: description.to_string(),
        input_schema: json!({
            "type": "object",
            "additionalProperties": true
        }),
        defer_loading: false,
        persist_on_resume: false,
        capability: Some(DynamicToolCapability {
            family: Some(COMPUTER_USE_ADAPTER_BROWSER.to_string()),
            capability_scope: Some("session".to_string()),
            mutation_class: Some(mutation_class.to_string()),
            lease_mode: None,
        }),
    }
}

/// Result of trying to route a browser computer-use request to a configured
/// local provider.
pub enum BrowserComputerUseOutcome {
    /// The request was claimed by the browser provider path and converted into
    /// a model-facing response.
    Handled(ComputerUseCallResponse),
    /// The request is for another adapter/tool or browser use is not
    /// configured for the active Codex home.
    Unavailable,
}

/// Handle a browser computer-use request using the process default Codex home.
pub async fn handle_browser_computer_use(
    params: &ComputerUseCallParams,
) -> BrowserComputerUseOutcome {
    let Some(codex_home) = default_codex_home() else {
        return BrowserComputerUseOutcome::Unavailable;
    };

    handle_browser_computer_use_for_codex_home(params, codex_home.as_path()).await
}

/// Handle a browser computer-use request using a specific Codex home.
pub async fn handle_browser_computer_use_for_codex_home(
    params: &ComputerUseCallParams,
    codex_home: &Path,
) -> BrowserComputerUseOutcome {
    if params.adapter != COMPUTER_USE_ADAPTER_BROWSER
        || native_computer_use_provider_for_call(COMPUTER_USE_ADAPTER_BROWSER, &params.tool)
            .is_none()
    {
        return BrowserComputerUseOutcome::Unavailable;
    }

    let Some(config) = BrowserRuntimeConfig::load(codex_home) else {
        return BrowserComputerUseOutcome::Unavailable;
    };

    let requested_backend = requested_backend(&params.arguments);
    let Some(provider) = config.provider_for_backend(requested_backend).cloned() else {
        return BrowserComputerUseOutcome::Handled(with_browser_metadata(
            failed_response(format!(
                "Browser backend `{requested_backend}` is not available from configured browser computer-use providers. Configure `{ENV_COMMAND}` or add a matching provider to ~/.codex/browser-computer-use.json."
            )),
            "none",
            requested_backend,
            "unavailable",
        ));
    };

    let request_timeout = provider.timeout;
    let provider_id = provider.id.clone();
    let provider_kind = provider.kind();
    let profile = provider.profile_label().to_string();
    let response = match timeout(request_timeout, handle_with_provider(params, provider)).await {
        Ok(Ok(response)) => response,
        Ok(Err(err)) => failed_response(err),
        Err(_) => failed_response(format!(
            "Browser computer-use provider timed out after {} seconds.",
            request_timeout.as_secs()
        )),
    };
    BrowserComputerUseOutcome::Handled(with_browser_metadata(
        response,
        &provider_id,
        requested_backend,
        &format!("{provider_kind}:{profile}"),
    ))
}

fn default_codex_home() -> Option<PathBuf> {
    codex_utils_home_dir::find_codex_home()
        .ok()
        .map(PathBuf::from)
}

async fn handle_with_provider(
    params: &ComputerUseCallParams,
    provider: ConfiguredBrowserProvider,
) -> Result<ComputerUseCallResponse, String> {
    let mut response = match provider.provider {
        BrowserProvider::Command(command) => run_command_provider(params, &command).await,
        BrowserProvider::Playwright(playwright) => {
            run_playwright_provider(params, playwright.as_ref()).await
        }
    }?;

    require_native_image_for_visual_response(
        &mut response,
        "Browser observation missing native image output.",
    );
    Ok(response)
}

async fn run_command_provider(
    params: &ComputerUseCallParams,
    command: &CommandProviderConfig,
) -> Result<ComputerUseCallResponse, String> {
    let output = run_provider_process(&command.argv, params, &[]).await?;
    parse_provider_response(&output)
}

async fn run_playwright_provider(
    params: &ComputerUseCallParams,
    config: &PlaywrightProviderConfig,
) -> Result<ComputerUseCallResponse, String> {
    let script_dir = tempfile::Builder::new()
        .prefix("codex-browser-provider-")
        .tempdir()
        .map_err(|err| format!("failed to create browser provider script directory: {err}"))?;
    let script_path = script_dir.path().join("browser_playwright_provider.mjs");
    let review_script_path = script_dir.path().join("browser_playwright_review.mjs");
    let inspection_script_path = script_dir.path().join("browser_playwright_inspection.mjs");
    let service_headers_script_path = script_dir
        .path()
        .join("browser_playwright_service_headers.mjs");
    std::fs::write(&script_path, PLAYWRIGHT_BRIDGE_SCRIPT)
        .map_err(|err| format!("failed to write browser provider script: {err}"))?;
    std::fs::write(&review_script_path, PLAYWRIGHT_REVIEW_SCRIPT)
        .map_err(|err| format!("failed to write browser review script: {err}"))?;
    std::fs::write(&inspection_script_path, PLAYWRIGHT_INSPECTION_SCRIPT)
        .map_err(|err| format!("failed to write browser inspection script: {err}"))?;
    std::fs::write(
        &service_headers_script_path,
        PLAYWRIGHT_SERVICE_HEADERS_SCRIPT,
    )
    .map_err(|err| format!("failed to write browser service header script: {err}"))?;

    let script_path = script_path.to_string_lossy().to_string();
    let envs = playwright_provider_envs(config);

    let output = run_provider_process(&[config.node.clone(), script_path], params, &envs).await?;
    parse_provider_response(&output)
}

fn playwright_provider_envs(config: &PlaywrightProviderConfig) -> Vec<(String, String)> {
    let mut envs = Vec::new();
    push_env(
        &mut envs,
        ENV_PLAYWRIGHT_NODE_PATH,
        config.node_path.clone(),
    );
    if let Some(node_path) = &config.node_path {
        push_env(&mut envs, "NODE_PATH", Some(node_path.clone()));
    }
    push_env(
        &mut envs,
        ENV_PLAYWRIGHT_STATE_DIR,
        config.state_dir.clone(),
    );
    push_env(
        &mut envs,
        ENV_PLAYWRIGHT_EXECUTABLE_PATH,
        config.executable_path.clone(),
    );
    push_env(&mut envs, ENV_PLAYWRIGHT_CHANNEL, config.channel.clone());
    push_env(&mut envs, ENV_PLAYWRIGHT_DISPLAY, config.display.clone());
    if let Some(display) = &config.display {
        push_env(&mut envs, "DISPLAY", Some(display.clone()));
    }
    push_env(
        &mut envs,
        ENV_PLAYWRIGHT_CAPTURE_MODE,
        config.capture_mode.clone(),
    );
    push_env(
        &mut envs,
        ENV_PLAYWRIGHT_ISOLATION,
        config.isolation.clone(),
    );
    push_env(
        &mut envs,
        ENV_PLAYWRIGHT_VIEWPORT_WIDTH,
        config.viewport_width.map(|width| width.to_string()),
    );
    push_env(
        &mut envs,
        ENV_PLAYWRIGHT_VIEWPORT_HEIGHT,
        config.viewport_height.map(|height| height.to_string()),
    );
    push_env(
        &mut envs,
        ENV_PLAYWRIGHT_ARTIFACT_DIR,
        config.artifact_dir.clone(),
    );
    push_env(
        &mut envs,
        ENV_PLAYWRIGHT_ARTIFACT_POLICY,
        config.artifact_policy.clone(),
    );
    if config.allow_call_extra_http_headers.unwrap_or(false) {
        push_env(
            &mut envs,
            ENV_PLAYWRIGHT_ALLOW_CALL_HEADERS,
            Some("1".to_string()),
        );
    }
    if !config.service_profiles.is_empty()
        && let Ok(value) = serde_json::to_string(&config.service_profiles)
    {
        push_env(&mut envs, ENV_PLAYWRIGHT_SERVICE_PROFILES, Some(value));
    }
    if let Some(headless) = config.headless {
        push_env(
            &mut envs,
            ENV_PLAYWRIGHT_HEADLESS,
            Some(if headless { "1" } else { "0" }.to_string()),
        );
    }
    envs
}

fn push_env(envs: &mut Vec<(String, String)>, key: &str, value: Option<String>) {
    if let Some(value) = value
        && !value.trim().is_empty()
    {
        envs.push((key.to_string(), value));
    }
}

async fn run_provider_process(
    argv: &[String],
    params: &ComputerUseCallParams,
    envs: &[(String, String)],
) -> Result<Vec<u8>, String> {
    let (program, args) = argv
        .split_first()
        .ok_or_else(|| "Browser provider command is empty.".to_string())?;
    let mut child = Command::new(program)
        .args(args)
        .envs(envs.iter().map(|(key, value)| (key, value)))
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true)
        .spawn()
        .map_err(|err| format!("failed to start browser provider `{program}`: {err}"))?;

    let mut stdin = child
        .stdin
        .take()
        .ok_or_else(|| "failed to open browser provider stdin".to_string())?;
    let body = serde_json::to_vec(params)
        .map_err(|err| format!("failed to serialize browser provider request: {err}"))?;
    stdin
        .write_all(&body)
        .await
        .map_err(|err| format!("failed to write browser provider request: {err}"))?;
    drop(stdin);

    let output = child
        .wait_with_output()
        .await
        .map_err(|err| format!("failed to wait for browser provider: {err}"))?;
    if output.status.success() {
        Ok(output.stdout)
    } else {
        let stderr = String::from_utf8_lossy(&output.stderr);
        Err(format!(
            "Browser provider exited with status {}: {}",
            output.status,
            compact_process_output(&stderr)
        ))
    }
}

fn parse_provider_response(bytes: &[u8]) -> Result<ComputerUseCallResponse, String> {
    serde_json::from_slice(bytes).map_err(|err| {
        let snippet = compact_process_output(&String::from_utf8_lossy(bytes));
        format!("failed to parse browser provider response: {err}; stdout: {snippet}")
    })
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct BrowserRuntimeConfig {
    providers: Vec<ConfiguredBrowserProvider>,
}

impl BrowserRuntimeConfig {
    fn load(codex_home: &Path) -> Option<Self> {
        Self::from_sources(
            BrowserRuntimeConfigFile::load(codex_home),
            BrowserRuntimeEnv::read(),
        )
    }

    fn from_sources(
        file: Option<BrowserRuntimeConfigFile>,
        env: BrowserRuntimeEnv,
    ) -> Option<Self> {
        let provider_name = env
            .provider
            .clone()
            .or_else(|| file.as_ref().and_then(|config| config.provider.clone()));
        if provider_name.as_deref() == Some(PROVIDER_NONE) {
            return None;
        }

        let timeout = env
            .timeout_secs
            .or_else(|| file.as_ref().and_then(|config| config.timeout_secs))
            .map(Duration::from_secs)
            .unwrap_or(DEFAULT_REQUEST_TIMEOUT);

        if let Some(command) = env
            .command
            .clone()
            .and_then(|command| command_spec_to_argv(CommandSpec::String(command)))
            .or_else(|| {
                file.as_ref()
                    .and_then(|config| config.command.clone())
                    .and_then(command_spec_to_argv)
            })
        {
            return Some(Self {
                providers: vec![ConfiguredBrowserProvider::command(
                    "env-command",
                    command,
                    wildcard_backends(),
                    timeout,
                )],
            });
        }

        if provider_name.as_deref() == Some(PROVIDER_PLAYWRIGHT) {
            return Some(Self {
                providers: vec![ConfiguredBrowserProvider::playwright(
                    "playwright",
                    PlaywrightProviderConfig {
                        node: env
                            .node
                            .clone()
                            .or_else(|| file.as_ref().and_then(|config| config.node.clone()))
                            .unwrap_or_else(|| "node".to_string()),
                        node_path: env
                            .node_path
                            .clone()
                            .or_else(|| file.as_ref().and_then(|config| config.node_path.clone())),
                        state_dir: env
                            .state_dir
                            .clone()
                            .or_else(|| file.as_ref().and_then(|config| config.state_dir.clone())),
                        headless: env
                            .headless
                            .or_else(|| file.as_ref().and_then(|config| config.headless)),
                        executable_path: env.executable_path.clone().or_else(|| {
                            file.as_ref()
                                .and_then(|config| config.executable_path.clone())
                        }),
                        channel: env
                            .channel
                            .clone()
                            .or_else(|| file.as_ref().and_then(|config| config.channel.clone())),
                        display: env
                            .display
                            .clone()
                            .or_else(|| file.as_ref().and_then(|config| config.display.clone())),
                        capture_mode: env.capture_mode.clone().or_else(|| {
                            file.as_ref().and_then(|config| config.capture_mode.clone())
                        }),
                        isolation: env
                            .isolation
                            .clone()
                            .or_else(|| file.as_ref().and_then(|config| config.isolation.clone())),
                        viewport_width: env
                            .viewport_width
                            .or_else(|| file.as_ref().and_then(|config| config.viewport_width)),
                        viewport_height: env
                            .viewport_height
                            .or_else(|| file.as_ref().and_then(|config| config.viewport_height)),
                        artifact_dir: env.artifact_dir.clone().or_else(|| {
                            file.as_ref().and_then(|config| config.artifact_dir.clone())
                        }),
                        artifact_policy: env.artifact_policy.clone().or_else(|| {
                            file.as_ref()
                                .and_then(|config| config.artifact_policy.clone())
                        }),
                        allow_call_extra_http_headers: env.allow_call_extra_http_headers.or_else(
                            || {
                                file.as_ref()
                                    .and_then(|config| config.allow_call_extra_http_headers)
                            },
                        ),
                        service_profiles: merge_service_profiles(
                            file.as_ref()
                                .and_then(|config| config.service_profiles.clone()),
                            env.service_profiles,
                        ),
                    },
                    default_playwright_backends(),
                    timeout,
                )],
            });
        }

        if provider_name.as_deref() == Some(PROVIDER_COMMAND) {
            return None;
        }

        let file = file?;
        let providers = file.configured_providers(timeout, &env);
        (!providers.is_empty()).then_some(Self { providers })
    }

    fn provider_for_backend(&self, backend: &str) -> Option<&ConfiguredBrowserProvider> {
        self.providers
            .iter()
            .find(|provider| provider.supports_backend(backend))
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct ConfiguredBrowserProvider {
    id: String,
    provider: BrowserProvider,
    backends: Vec<String>,
    timeout: Duration,
}

impl ConfiguredBrowserProvider {
    fn command(
        id: impl Into<String>,
        argv: Vec<String>,
        backends: Vec<String>,
        timeout: Duration,
    ) -> Self {
        Self {
            id: id.into(),
            provider: BrowserProvider::Command(CommandProviderConfig { argv }),
            backends,
            timeout,
        }
    }

    fn playwright(
        id: impl Into<String>,
        config: PlaywrightProviderConfig,
        backends: Vec<String>,
        timeout: Duration,
    ) -> Self {
        Self {
            id: id.into(),
            provider: BrowserProvider::Playwright(Box::new(config)),
            backends,
            timeout,
        }
    }

    fn supports_backend(&self, backend: &str) -> bool {
        if matches!(&self.provider, BrowserProvider::Playwright(_)) {
            return self
                .backends
                .iter()
                .any(|configured| configured.eq_ignore_ascii_case(backend));
        }

        self.backends.is_empty()
            || self.backends.iter().any(|configured| {
                configured == BACKEND_WILDCARD || configured.eq_ignore_ascii_case(backend)
            })
    }

    fn kind(&self) -> &'static str {
        match &self.provider {
            BrowserProvider::Command(_) => PROVIDER_COMMAND,
            BrowserProvider::Playwright(_) => PROVIDER_PLAYWRIGHT,
        }
    }

    fn profile_label(&self) -> &str {
        match &self.provider {
            BrowserProvider::Command(_) => "external",
            BrowserProvider::Playwright(config) => match config.isolation.as_deref() {
                Some(iso @ ("thread" | "shared" | "environment" | "call")) => iso,
                Some(_) => "configured",
                None => "thread",
            },
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum BrowserProvider {
    Command(CommandProviderConfig),
    Playwright(Box<PlaywrightProviderConfig>),
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct CommandProviderConfig {
    argv: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct PlaywrightProviderConfig {
    node: String,
    node_path: Option<String>,
    state_dir: Option<String>,
    headless: Option<bool>,
    executable_path: Option<String>,
    channel: Option<String>,
    display: Option<String>,
    capture_mode: Option<String>,
    isolation: Option<String>,
    viewport_width: Option<u64>,
    viewport_height: Option<u64>,
    artifact_dir: Option<String>,
    artifact_policy: Option<String>,
    allow_call_extra_http_headers: Option<bool>,
    service_profiles: Vec<ServiceProfileConfigFile>,
}

#[derive(Deserialize)]
struct BrowserRuntimeConfigFile {
    provider: Option<String>,
    command: Option<CommandSpec>,
    node: Option<String>,
    node_path: Option<String>,
    timeout_secs: Option<u64>,
    state_dir: Option<String>,
    headless: Option<bool>,
    executable_path: Option<String>,
    channel: Option<String>,
    display: Option<String>,
    capture_mode: Option<String>,
    isolation: Option<String>,
    viewport_width: Option<u64>,
    viewport_height: Option<u64>,
    artifact_dir: Option<String>,
    artifact_policy: Option<String>,
    allow_call_extra_http_headers: Option<bool>,
    service_profiles: Option<Vec<ServiceProfileConfigFile>>,
    providers: Option<Vec<BrowserProviderConfigFile>>,
    routing: Option<BrowserRoutingConfigFile>,
}

impl BrowserRuntimeConfigFile {
    fn load(codex_home: &Path) -> Option<Self> {
        for path in [
            codex_home.join("browser-computer-use.json"),
            codex_home.join("browser-dynamic-tools.json"),
        ] {
            if let Ok(contents) = std::fs::read_to_string(path)
                && let Ok(config) = serde_json::from_str(&contents)
            {
                return Some(config);
            }
        }
        None
    }

    fn configured_providers(
        &self,
        default_timeout: Duration,
        env: &BrowserRuntimeEnv,
    ) -> Vec<ConfiguredBrowserProvider> {
        let mut providers = self
            .providers
            .as_ref()
            .map(|providers| {
                providers
                    .iter()
                    .filter(|provider| provider.matches_current_platform())
                    .filter_map(|provider| provider.to_configured(default_timeout, env))
                    .collect::<Vec<_>>()
            })
            .unwrap_or_default();

        if let Some(order) = self
            .routing
            .as_ref()
            .and_then(|routing| routing.fallback_order.as_ref())
        {
            providers = order_providers(providers, order);
        }

        providers
    }
}

#[derive(Deserialize)]
struct BrowserRoutingConfigFile {
    fallback_order: Option<Vec<String>>,
}

#[derive(Deserialize)]
struct BrowserProviderConfigFile {
    id: Option<String>,
    provider: Option<String>,
    command: Option<CommandSpec>,
    node: Option<String>,
    node_path: Option<String>,
    timeout_secs: Option<u64>,
    state_dir: Option<String>,
    headless: Option<bool>,
    executable_path: Option<String>,
    channel: Option<String>,
    display: Option<String>,
    capture_mode: Option<String>,
    isolation: Option<String>,
    viewport_width: Option<u64>,
    viewport_height: Option<u64>,
    artifact_dir: Option<String>,
    artifact_policy: Option<String>,
    allow_call_extra_http_headers: Option<bool>,
    service_profiles: Option<Vec<ServiceProfileConfigFile>>,
    backends: Option<Vec<String>>,
    platforms: Option<Vec<String>>,
}

impl BrowserProviderConfigFile {
    fn matches_current_platform(&self) -> bool {
        let Some(platforms) = &self.platforms else {
            return true;
        };
        platforms
            .iter()
            .any(|platform| platform_matches_current(platform))
    }

    fn to_configured(
        &self,
        default_timeout: Duration,
        env: &BrowserRuntimeEnv,
    ) -> Option<ConfiguredBrowserProvider> {
        let provider_name = self.provider.as_deref().unwrap_or(PROVIDER_COMMAND);
        if provider_name == PROVIDER_NONE {
            return None;
        }

        let timeout = self
            .timeout_secs
            .map(Duration::from_secs)
            .unwrap_or(default_timeout);
        let backends = self.backends.clone().unwrap_or_else(|| {
            if provider_name == PROVIDER_PLAYWRIGHT {
                default_playwright_backends()
            } else {
                wildcard_backends()
            }
        });
        let backends = if provider_name == PROVIDER_PLAYWRIGHT {
            normalize_playwright_backends(backends)
        } else {
            backends
        };
        let id = self.id.clone().unwrap_or_else(|| provider_name.to_string());

        match provider_name {
            PROVIDER_PLAYWRIGHT => Some(ConfiguredBrowserProvider::playwright(
                id,
                PlaywrightProviderConfig {
                    node: self
                        .node
                        .clone()
                        .or_else(|| env.node.clone())
                        .unwrap_or_else(|| "node".to_string()),
                    node_path: self.node_path.clone().or_else(|| env.node_path.clone()),
                    state_dir: self.state_dir.clone().or_else(|| env.state_dir.clone()),
                    headless: self.headless.or(env.headless),
                    executable_path: self
                        .executable_path
                        .clone()
                        .or_else(|| env.executable_path.clone()),
                    channel: self.channel.clone().or_else(|| env.channel.clone()),
                    display: self.display.clone().or_else(|| env.display.clone()),
                    capture_mode: self
                        .capture_mode
                        .clone()
                        .or_else(|| env.capture_mode.clone()),
                    isolation: self.isolation.clone().or_else(|| env.isolation.clone()),
                    viewport_width: self.viewport_width.or(env.viewport_width),
                    viewport_height: self.viewport_height.or(env.viewport_height),
                    artifact_dir: self
                        .artifact_dir
                        .clone()
                        .or_else(|| env.artifact_dir.clone()),
                    artifact_policy: self
                        .artifact_policy
                        .clone()
                        .or_else(|| env.artifact_policy.clone()),
                    allow_call_extra_http_headers: self
                        .allow_call_extra_http_headers
                        .or(env.allow_call_extra_http_headers),
                    service_profiles: merge_service_profiles(
                        self.service_profiles.clone(),
                        env.service_profiles.clone(),
                    ),
                },
                backends,
                timeout,
            )),
            PROVIDER_COMMAND => self
                .command
                .clone()
                .and_then(command_spec_to_argv)
                .map(|argv| ConfiguredBrowserProvider::command(id, argv, backends, timeout)),
            _ => None,
        }
    }
}

#[derive(Default)]
struct BrowserRuntimeEnv {
    provider: Option<String>,
    command: Option<String>,
    node: Option<String>,
    node_path: Option<String>,
    timeout_secs: Option<u64>,
    state_dir: Option<String>,
    headless: Option<bool>,
    executable_path: Option<String>,
    channel: Option<String>,
    display: Option<String>,
    capture_mode: Option<String>,
    isolation: Option<String>,
    viewport_width: Option<u64>,
    viewport_height: Option<u64>,
    artifact_dir: Option<String>,
    artifact_policy: Option<String>,
    allow_call_extra_http_headers: Option<bool>,
    service_profiles: Vec<ServiceProfileConfigFile>,
}

impl BrowserRuntimeEnv {
    fn read() -> Self {
        Self {
            provider: first_env(&[ENV_PROVIDER]),
            command: first_env(&[ENV_COMMAND]),
            node: first_env(&[ENV_NODE]),
            node_path: first_env(&[ENV_PLAYWRIGHT_NODE_PATH]),
            timeout_secs: first_env(&[ENV_TIMEOUT_SECS]).and_then(|value| value.parse().ok()),
            state_dir: first_env(&[ENV_PLAYWRIGHT_STATE_DIR]),
            headless: first_env(&[ENV_PLAYWRIGHT_HEADLESS]).and_then(|value| parse_bool(&value)),
            executable_path: first_env(&[ENV_PLAYWRIGHT_EXECUTABLE_PATH]),
            channel: first_env(&[ENV_PLAYWRIGHT_CHANNEL]),
            display: first_env(&[ENV_PLAYWRIGHT_DISPLAY]),
            capture_mode: first_env(&[ENV_PLAYWRIGHT_CAPTURE_MODE]),
            isolation: first_env(&[ENV_PLAYWRIGHT_ISOLATION]),
            viewport_width: first_env(&[ENV_PLAYWRIGHT_VIEWPORT_WIDTH])
                .and_then(|value| value.parse().ok()),
            viewport_height: first_env(&[ENV_PLAYWRIGHT_VIEWPORT_HEIGHT])
                .and_then(|value| value.parse().ok()),
            artifact_dir: first_env(&[ENV_PLAYWRIGHT_ARTIFACT_DIR]),
            artifact_policy: first_env(&[ENV_PLAYWRIGHT_ARTIFACT_POLICY]),
            allow_call_extra_http_headers: first_env(&[ENV_PLAYWRIGHT_ALLOW_CALL_HEADERS])
                .and_then(|value| parse_bool(&value)),
            service_profiles: first_env(&[ENV_PLAYWRIGHT_SERVICE_PROFILES])
                .and_then(|value| serde_json::from_str(&value).ok())
                .unwrap_or_default(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
struct ServiceProfileConfigFile {
    id: String,
    actor: Option<String>,
    allowed_hosts: Vec<String>,
    headers: Option<std::collections::BTreeMap<String, String>>,
    env_headers: Option<std::collections::BTreeMap<String, String>>,
}

#[derive(Clone, Deserialize)]
#[serde(untagged)]
enum CommandSpec {
    String(String),
    Array(Vec<String>),
}

fn command_spec_to_argv(command: CommandSpec) -> Option<Vec<String>> {
    let argv = match command {
        CommandSpec::String(command) => shlex::split(&command)?,
        CommandSpec::Array(argv) => argv,
    };
    (!argv.is_empty()).then_some(argv)
}

fn order_providers(
    providers: Vec<ConfiguredBrowserProvider>,
    order: &[String],
) -> Vec<ConfiguredBrowserProvider> {
    let mut remaining = providers;
    let mut ordered = Vec::new();
    for id in order {
        if let Some(index) = remaining.iter().position(|provider| provider.id == *id) {
            ordered.push(remaining.remove(index));
        }
    }
    ordered.extend(remaining);
    ordered
}

fn merge_service_profiles(
    file_profiles: Option<Vec<ServiceProfileConfigFile>>,
    env_profiles: Vec<ServiceProfileConfigFile>,
) -> Vec<ServiceProfileConfigFile> {
    let mut profiles = file_profiles.unwrap_or_default();
    profiles.extend(env_profiles);
    profiles
}

fn wildcard_backends() -> Vec<String> {
    vec![BACKEND_WILDCARD.to_string()]
}

fn default_playwright_backends() -> Vec<String> {
    vec![
        BACKEND_AUTO.to_string(),
        BACKEND_BROWSER.to_string(),
        BACKEND_CHROMIUM.to_string(),
    ]
}

fn normalize_playwright_backends(backends: Vec<String>) -> Vec<String> {
    let mut normalized = Vec::new();
    for backend in backends {
        if backend == BACKEND_WILDCARD {
            for default_backend in default_playwright_backends() {
                if !normalized.contains(&default_backend) {
                    normalized.push(default_backend);
                }
            }
        } else if !backend.eq_ignore_ascii_case(BACKEND_CHROME) && !normalized.contains(&backend) {
            normalized.push(backend);
        }
    }
    normalized
}

fn with_browser_metadata(
    mut response: ComputerUseCallResponse,
    provider: &str,
    backend: &str,
    profile: &str,
) -> ComputerUseCallResponse {
    let provider = safe_metadata_value(provider);
    let backend = safe_metadata_value(backend);
    let profile = safe_metadata_value(profile);
    append_text(
        &mut response.content_items,
        &format!("\n\nbrowser_metadata: provider={provider} backend={backend} profile={profile}"),
    );
    response
}

fn safe_metadata_value(value: &str) -> String {
    let mut sanitized = value
        .chars()
        .filter(|character| {
            character.is_ascii_alphanumeric() || matches!(character, '-' | '_' | '.')
        })
        .take(64)
        .collect::<String>();
    if sanitized.is_empty() {
        sanitized.push_str("unknown");
    }
    sanitized
}

fn platform_matches_current(platform: &str) -> bool {
    match platform.to_ascii_lowercase().as_str() {
        "all" | "*" => true,
        "linux" => cfg!(target_os = "linux"),
        "mac" | "macos" | "darwin" => cfg!(target_os = "macos"),
        "windows" | "win32" => cfg!(target_os = "windows"),
        "unix" => cfg!(unix),
        other => other == std::env::consts::OS,
    }
}

fn requested_backend(arguments: &Value) -> &str {
    arguments
        .get("backend")
        .and_then(Value::as_str)
        .unwrap_or(BACKEND_AUTO)
}

fn first_env(keys: &[&str]) -> Option<String> {
    keys.iter()
        .filter_map(|key| std::env::var(key).ok())
        .find(|value| !value.trim().is_empty())
}

fn parse_bool(value: &str) -> Option<bool> {
    match value.to_ascii_lowercase().as_str() {
        "1" | "true" | "yes" | "on" => Some(true),
        "0" | "false" | "no" | "off" => Some(false),
        _ => None,
    }
}

fn response_includes_native_image(response: &ComputerUseCallResponse) -> bool {
    response
        .content_items
        .iter()
        .any(|item| matches!(item, ComputerUseCallOutputContentItem::InputImage { .. }))
}

fn require_native_image_for_visual_response(
    response: &mut ComputerUseCallResponse,
    missing_image_message: &str,
) {
    if !response.success || response_includes_native_image(response) {
        return;
    }

    append_text(
        &mut response.content_items,
        &format!(
            "\n\n{missing_image_message} The browser provider must return screenshots as native image content items rather than text-only summaries or artifact paths."
        ),
    );
    response.success = false;
    response.error = Some(match response.error.take() {
        Some(existing_error) if !existing_error.trim().is_empty() => {
            format!("{missing_image_message} Previous provider error: {existing_error}")
        }
        _ => missing_image_message.to_string(),
    });
}

fn append_text(items: &mut Vec<ComputerUseCallOutputContentItem>, extra: &str) {
    if let Some(ComputerUseCallOutputContentItem::InputText { text }) = items.first_mut() {
        text.push_str(extra);
    } else {
        items.insert(
            0,
            ComputerUseCallOutputContentItem::InputText {
                text: extra.trim().to_string(),
            },
        );
    }
}

fn failed_response(error: String) -> ComputerUseCallResponse {
    ComputerUseCallResponse {
        content_items: vec![ComputerUseCallOutputContentItem::InputText {
            text: error.clone(),
        }],
        success: false,
        error: Some(error),
    }
}

fn compact_process_output(output: &str) -> String {
    const LIMIT: usize = 500;
    let compact = output.trim();
    if compact.chars().count() <= LIMIT {
        return compact.to_string();
    }
    let mut truncated = compact.chars().take(LIMIT - 3).collect::<String>();
    truncated.push_str("...");
    truncated
}

#[cfg(test)]
mod tests {
    use super::*;
    use pretty_assertions::assert_eq;
    use serde_json::json;
    use std::io::Write as _;

    #[test]
    fn command_spec_accepts_shell_like_string_and_array() {
        assert_eq!(
            command_spec_to_argv(CommandSpec::String("node provider.mjs".to_string())),
            Some(vec!["node".to_string(), "provider.mjs".to_string()])
        );
        assert_eq!(
            command_spec_to_argv(CommandSpec::Array(vec![
                "node".to_string(),
                "provider.mjs".to_string()
            ])),
            Some(vec!["node".to_string(), "provider.mjs".to_string()])
        );
    }

    #[test]
    fn requested_backend_defaults_to_auto() {
        assert_eq!(requested_backend(&json!({})), BACKEND_AUTO);
        assert_eq!(requested_backend(&json!({"backend": "iab"})), "iab");
        assert_eq!(requested_backend(&json!({"backend": "chrome"})), "chrome");
    }

    #[test]
    fn providers_array_routes_by_requested_backend() {
        let config = BrowserRuntimeConfig::from_sources(
            Some(BrowserRuntimeConfigFile {
                provider: None,
                command: None,
                node: None,
                node_path: None,
                timeout_secs: None,
                state_dir: None,
                headless: None,
                executable_path: None,
                channel: None,
                display: None,
                capture_mode: None,
                isolation: None,
                viewport_width: None,
                viewport_height: None,
                artifact_dir: None,
                artifact_policy: None,
                allow_call_extra_http_headers: None,
                service_profiles: None,
                providers: Some(vec![
                    BrowserProviderConfigFile {
                        id: Some("chrome-provider".to_string()),
                        provider: Some(PROVIDER_COMMAND.to_string()),
                        command: Some(CommandSpec::Array(vec![
                            "node".to_string(),
                            "chrome-provider.mjs".to_string(),
                        ])),
                        node: None,
                        node_path: None,
                        timeout_secs: None,
                        state_dir: None,
                        headless: None,
                        executable_path: None,
                        channel: None,
                        display: None,
                        capture_mode: None,
                        isolation: None,
                        viewport_width: None,
                        viewport_height: None,
                        artifact_dir: None,
                        artifact_policy: None,
                        allow_call_extra_http_headers: None,
                        service_profiles: None,
                        backends: Some(vec!["chrome".to_string()]),
                        platforms: None,
                    },
                    BrowserProviderConfigFile {
                        id: Some("playwright".to_string()),
                        provider: Some(PROVIDER_PLAYWRIGHT.to_string()),
                        command: None,
                        node: Some("node".to_string()),
                        node_path: None,
                        timeout_secs: None,
                        state_dir: None,
                        headless: None,
                        executable_path: None,
                        channel: None,
                        display: None,
                        capture_mode: None,
                        isolation: None,
                        viewport_width: None,
                        viewport_height: None,
                        artifact_dir: None,
                        artifact_policy: None,
                        allow_call_extra_http_headers: None,
                        service_profiles: None,
                        backends: Some(vec![BACKEND_AUTO.to_string()]),
                        platforms: None,
                    },
                ]),
                routing: None,
            }),
            BrowserRuntimeEnv::default(),
        )
        .expect("configured providers");

        assert_eq!(
            config.provider_for_backend("chrome").map(|p| &p.id),
            Some(&"chrome-provider".to_string())
        );
        assert_eq!(
            config.provider_for_backend(BACKEND_AUTO).map(|p| &p.id),
            Some(&"playwright".to_string())
        );
        assert_eq!(config.provider_for_backend("iab"), None);
    }

    #[test]
    fn routing_fallback_order_controls_wildcard_provider_preference() {
        let config = BrowserRuntimeConfig::from_sources(
            Some(BrowserRuntimeConfigFile {
                provider: None,
                command: None,
                node: None,
                node_path: None,
                timeout_secs: None,
                state_dir: None,
                headless: None,
                executable_path: None,
                channel: None,
                display: None,
                capture_mode: None,
                isolation: None,
                viewport_width: None,
                viewport_height: None,
                artifact_dir: None,
                artifact_policy: None,
                allow_call_extra_http_headers: None,
                service_profiles: None,
                providers: Some(vec![
                    BrowserProviderConfigFile {
                        id: Some("first".to_string()),
                        provider: Some(PROVIDER_COMMAND.to_string()),
                        command: Some(CommandSpec::Array(vec!["first".to_string()])),
                        node: None,
                        node_path: None,
                        timeout_secs: None,
                        state_dir: None,
                        headless: None,
                        executable_path: None,
                        channel: None,
                        display: None,
                        capture_mode: None,
                        isolation: None,
                        viewport_width: None,
                        viewport_height: None,
                        artifact_dir: None,
                        artifact_policy: None,
                        allow_call_extra_http_headers: None,
                        service_profiles: None,
                        backends: None,
                        platforms: None,
                    },
                    BrowserProviderConfigFile {
                        id: Some("second".to_string()),
                        provider: Some(PROVIDER_COMMAND.to_string()),
                        command: Some(CommandSpec::Array(vec!["second".to_string()])),
                        node: None,
                        node_path: None,
                        timeout_secs: None,
                        state_dir: None,
                        headless: None,
                        executable_path: None,
                        channel: None,
                        display: None,
                        capture_mode: None,
                        isolation: None,
                        viewport_width: None,
                        viewport_height: None,
                        artifact_dir: None,
                        artifact_policy: None,
                        allow_call_extra_http_headers: None,
                        service_profiles: None,
                        backends: None,
                        platforms: None,
                    },
                ]),
                routing: Some(BrowserRoutingConfigFile {
                    fallback_order: Some(vec!["second".to_string(), "first".to_string()]),
                }),
            }),
            BrowserRuntimeEnv::default(),
        )
        .expect("configured providers");

        assert_eq!(
            config.provider_for_backend(BACKEND_AUTO).map(|p| &p.id),
            Some(&"second".to_string())
        );
    }

    #[test]
    fn legacy_playwright_provider_does_not_claim_chrome_backend() {
        let config = BrowserRuntimeConfig::from_sources(
            Some(BrowserRuntimeConfigFile {
                provider: Some(PROVIDER_PLAYWRIGHT.to_string()),
                command: None,
                node: Some("node".to_string()),
                node_path: None,
                timeout_secs: None,
                state_dir: None,
                headless: None,
                executable_path: None,
                channel: None,
                display: None,
                capture_mode: None,
                isolation: None,
                viewport_width: None,
                viewport_height: None,
                artifact_dir: None,
                artifact_policy: None,
                allow_call_extra_http_headers: None,
                service_profiles: None,
                providers: None,
                routing: None,
            }),
            BrowserRuntimeEnv::default(),
        )
        .expect("playwright provider");

        assert!(config.provider_for_backend(BACKEND_AUTO).is_some());
        assert!(config.provider_for_backend(BACKEND_BROWSER).is_some());
        assert!(config.provider_for_backend(BACKEND_CHROME).is_none());
        assert!(config.provider_for_backend(BACKEND_CHROMIUM).is_some());
        assert!(config.provider_for_backend("iab").is_none());
    }

    #[test]
    fn explicit_playwright_chrome_backend_is_rejected() {
        let config = BrowserRuntimeConfig::from_sources(
            Some(BrowserRuntimeConfigFile {
                provider: None,
                command: None,
                node: None,
                node_path: None,
                timeout_secs: None,
                state_dir: None,
                headless: None,
                executable_path: None,
                channel: None,
                display: None,
                capture_mode: None,
                isolation: None,
                viewport_width: None,
                viewport_height: None,
                artifact_dir: None,
                artifact_policy: None,
                allow_call_extra_http_headers: None,
                service_profiles: None,
                providers: Some(vec![BrowserProviderConfigFile {
                    id: Some("playwright".to_string()),
                    provider: Some(PROVIDER_PLAYWRIGHT.to_string()),
                    command: None,
                    node: None,
                    node_path: None,
                    timeout_secs: None,
                    state_dir: None,
                    headless: None,
                    executable_path: None,
                    channel: None,
                    display: None,
                    capture_mode: None,
                    isolation: None,
                    viewport_width: None,
                    viewport_height: None,
                    artifact_dir: None,
                    artifact_policy: None,
                    allow_call_extra_http_headers: None,
                    service_profiles: None,
                    backends: Some(vec![
                        BACKEND_CHROME.to_string(),
                        BACKEND_WILDCARD.to_string(),
                    ]),
                    platforms: None,
                }]),
                routing: None,
            }),
            BrowserRuntimeEnv::default(),
        )
        .expect("configured providers");

        assert!(config.provider_for_backend(BACKEND_CHROME).is_none());
        assert!(config.provider_for_backend(BACKEND_AUTO).is_some());
        assert!(config.provider_for_backend(BACKEND_BROWSER).is_some());
        assert!(config.provider_for_backend(BACKEND_CHROMIUM).is_some());
    }

    #[test]
    fn playwright_provider_carries_artifact_and_service_profile_config() {
        let config = BrowserRuntimeConfig::from_sources(
            Some(BrowserRuntimeConfigFile {
                provider: Some(PROVIDER_PLAYWRIGHT.to_string()),
                command: None,
                node: Some("node".to_string()),
                node_path: None,
                timeout_secs: None,
                state_dir: None,
                headless: None,
                executable_path: None,
                channel: None,
                display: None,
                capture_mode: None,
                isolation: None,
                viewport_width: None,
                viewport_height: None,
                artifact_dir: Some("/tmp/artifacts".to_string()),
                artifact_policy: Some("failure".to_string()),
                allow_call_extra_http_headers: Some(true),
                service_profiles: Some(vec![ServiceProfileConfigFile {
                    id: "cf-access".to_string(),
                    actor: Some("service account".to_string()),
                    allowed_hosts: vec!["example.test".to_string()],
                    headers: Some(std::collections::BTreeMap::from([(
                        "CF-Access-Client-Id".to_string(),
                        "client-id".to_string(),
                    )])),
                    env_headers: Some(std::collections::BTreeMap::from([(
                        "CF-Access-Client-Secret".to_string(),
                        "CF_SECRET".to_string(),
                    )])),
                }]),
                providers: None,
                routing: None,
            }),
            BrowserRuntimeEnv::default(),
        )
        .expect("playwright provider");

        let provider = config
            .provider_for_backend(BACKEND_AUTO)
            .expect("provider for auto");
        let BrowserProvider::Playwright(playwright) = &provider.provider else {
            panic!("expected playwright provider");
        };
        assert_eq!(playwright.artifact_dir.as_deref(), Some("/tmp/artifacts"));
        assert_eq!(playwright.artifact_policy.as_deref(), Some("failure"));
        assert_eq!(playwright.allow_call_extra_http_headers, Some(true));
        assert_eq!(
            playwright.service_profiles,
            vec![ServiceProfileConfigFile {
                id: "cf-access".to_string(),
                actor: Some("service account".to_string()),
                allowed_hosts: vec!["example.test".to_string()],
                headers: Some(std::collections::BTreeMap::from([(
                    "CF-Access-Client-Id".to_string(),
                    "client-id".to_string(),
                )])),
                env_headers: Some(std::collections::BTreeMap::from([(
                    "CF-Access-Client-Secret".to_string(),
                    "CF_SECRET".to_string(),
                )])),
            }]
        );
    }

    #[test]
    fn playwright_provider_exports_real_browser_environment() {
        let config = PlaywrightProviderConfig {
            node: "node".to_string(),
            node_path: Some("/opt/node_modules".to_string()),
            state_dir: Some("/tmp/codex-browser".to_string()),
            headless: Some(false),
            executable_path: Some("/usr/bin/google-chrome".to_string()),
            channel: None,
            display: Some(":99".to_string()),
            capture_mode: Some("viewport".to_string()),
            isolation: Some("thread".to_string()),
            viewport_width: Some(1440),
            viewport_height: Some(1000),
            artifact_dir: Some("/tmp/codex-browser-artifacts".to_string()),
            artifact_policy: Some("failure".to_string()),
            allow_call_extra_http_headers: Some(true),
            service_profiles: vec![ServiceProfileConfigFile {
                id: "cf-access".to_string(),
                actor: Some("service account".to_string()),
                allowed_hosts: vec!["example.test".to_string()],
                headers: None,
                env_headers: None,
            }],
        };

        assert_eq!(
            playwright_provider_envs(&config),
            vec![
                (
                    ENV_PLAYWRIGHT_NODE_PATH.to_string(),
                    "/opt/node_modules".to_string()
                ),
                ("NODE_PATH".to_string(), "/opt/node_modules".to_string()),
                (
                    ENV_PLAYWRIGHT_STATE_DIR.to_string(),
                    "/tmp/codex-browser".to_string()
                ),
                (
                    ENV_PLAYWRIGHT_EXECUTABLE_PATH.to_string(),
                    "/usr/bin/google-chrome".to_string()
                ),
                (ENV_PLAYWRIGHT_DISPLAY.to_string(), ":99".to_string()),
                ("DISPLAY".to_string(), ":99".to_string()),
                (
                    ENV_PLAYWRIGHT_CAPTURE_MODE.to_string(),
                    "viewport".to_string()
                ),
                (ENV_PLAYWRIGHT_ISOLATION.to_string(), "thread".to_string()),
                (
                    ENV_PLAYWRIGHT_VIEWPORT_WIDTH.to_string(),
                    "1440".to_string()
                ),
                (
                    ENV_PLAYWRIGHT_VIEWPORT_HEIGHT.to_string(),
                    "1000".to_string()
                ),
                (
                    ENV_PLAYWRIGHT_ARTIFACT_DIR.to_string(),
                    "/tmp/codex-browser-artifacts".to_string()
                ),
                (
                    ENV_PLAYWRIGHT_ARTIFACT_POLICY.to_string(),
                    "failure".to_string()
                ),
                (
                    ENV_PLAYWRIGHT_ALLOW_CALL_HEADERS.to_string(),
                    "1".to_string()
                ),
                (
                    ENV_PLAYWRIGHT_SERVICE_PROFILES.to_string(),
                    r#"[{"id":"cf-access","actor":"service account","allowed_hosts":["example.test"],"headers":null,"env_headers":null}]"#
                        .to_string()
                ),
                (ENV_PLAYWRIGHT_HEADLESS.to_string(), "0".to_string()),
            ]
        );
    }

    #[test]
    fn configured_browser_tools_are_session_scoped_native_tools() {
        let tools = [
            browser_dynamic_tool(BROWSER_OBSERVE_TOOL_NAME, "observe", "non_mutating"),
            browser_dynamic_tool(BROWSER_STEP_TOOL_NAME, "step", "mutating"),
        ];

        assert_eq!(tools[0].name, BROWSER_OBSERVE_TOOL_NAME);
        assert_eq!(tools[1].name, BROWSER_STEP_TOOL_NAME);
        assert!(tools.iter().all(|tool| tool.namespace.is_none()));
        assert!(tools.iter().all(|tool| !tool.defer_loading));
        assert!(tools.iter().all(|tool| !tool.persist_on_resume));
        assert!(tools.iter().all(|tool| {
            tool.capability
                .as_ref()
                .and_then(|capability| capability.family.as_deref())
                == Some(COMPUTER_USE_ADAPTER_BROWSER)
        }));
    }

    #[test]
    fn configured_browser_tools_load_from_explicit_codex_home() {
        let codex_home = tempfile::tempdir().expect("temp codex home");
        std::fs::write(
            codex_home.path().join("browser-computer-use.json"),
            r#"{"provider":"playwright"}"#,
        )
        .expect("write browser provider config");

        let tools = configured_browser_dynamic_tools_for_codex_home(codex_home.path());

        assert_eq!(
            tools
                .iter()
                .map(|tool| tool.name.as_str())
                .collect::<Vec<_>>(),
            vec![BROWSER_OBSERVE_TOOL_NAME, BROWSER_STEP_TOOL_NAME]
        );
    }

    #[test]
    fn browser_provider_response_preserves_native_image() {
        let mut response = ComputerUseCallResponse {
            content_items: vec![
                ComputerUseCallOutputContentItem::InputText {
                    text: "Browser observation".to_string(),
                },
                ComputerUseCallOutputContentItem::InputImage {
                    image_url: "data:image/png;base64,AAAA".to_string(),
                    detail: Some("high".to_string()),
                },
            ],
            success: true,
            error: None,
        };

        require_native_image_for_visual_response(
            &mut response,
            "Browser observation missing native image output.",
        );

        assert!(response.success);
        assert_eq!(response.error, None);
    }

    #[test]
    fn browser_provider_response_without_native_image_fails_loudly() {
        let mut response = ComputerUseCallResponse {
            content_items: vec![ComputerUseCallOutputContentItem::InputText {
                text: "Browser observation\nurl: https://example.test".to_string(),
            }],
            success: true,
            error: None,
        };

        require_native_image_for_visual_response(
            &mut response,
            "Browser observation missing native image output.",
        );

        assert!(!response.success);
        assert_eq!(
            response.error.as_deref(),
            Some("Browser observation missing native image output.")
        );
        let ComputerUseCallOutputContentItem::InputText { text } = &response.content_items[0]
        else {
            panic!("expected text summary");
        };
        assert!(text.contains("url: https://example.test"));
        assert!(text.contains("must return screenshots as native image content items"));
    }

    #[test]
    fn missing_native_image_diagnostic_is_visible_for_empty_response() {
        let mut response = ComputerUseCallResponse {
            content_items: vec![],
            success: true,
            error: None,
        };

        require_native_image_for_visual_response(
            &mut response,
            "Browser observation missing native image output.",
        );

        assert!(!response.success);
        assert_eq!(
            response.error.as_deref(),
            Some("Browser observation missing native image output.")
        );
        assert_eq!(response.content_items.len(), 1);
        let ComputerUseCallOutputContentItem::InputText { text } = &response.content_items[0]
        else {
            panic!("expected text diagnostic");
        };
        assert!(text.contains("must return screenshots as native image content items"));
    }

    #[test]
    fn parse_provider_response_accepts_computer_use_content_items() {
        let response = parse_provider_response(
            br#"{
              "contentItems": [
                {"type":"inputText","text":"Browser observation"},
                {"type":"inputImage","imageUrl":"data:image/png;base64,AAAA","detail":"high"}
              ],
              "success": true
            }"#,
        )
        .expect("valid provider response");

        assert_eq!(
            response.content_items,
            vec![
                ComputerUseCallOutputContentItem::InputText {
                    text: "Browser observation".to_string(),
                },
                ComputerUseCallOutputContentItem::InputImage {
                    image_url: "data:image/png;base64,AAAA".to_string(),
                    detail: Some("high".to_string()),
                },
            ]
        );
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn command_provider_bridge_returns_native_image_response() {
        let mut provider = tempfile::NamedTempFile::new().expect("temp provider");
        provider
            .write_all(
                br#"#!/bin/sh
cat >/dev/null
cat <<'JSON'
{"contentItems":[{"type":"inputText","text":"Browser observation from fake provider"},{"type":"inputImage","imageUrl":"data:image/png;base64,AAAA","detail":"high"}],"success":true}
JSON
"#,
            )
            .expect("write provider");
        let params = ComputerUseCallParams {
            thread_id: "thread-1".to_string(),
            turn_id: "turn-1".to_string(),
            call_id: "call-1".to_string(),
            environment_id: Some("env-1".to_string()),
            adapter: COMPUTER_USE_ADAPTER_BROWSER.to_string(),
            tool: BROWSER_OBSERVE_TOOL_NAME.to_string(),
            arguments: json!({}),
        };

        let response = run_command_provider(
            &params,
            &CommandProviderConfig {
                argv: vec![
                    "sh".to_string(),
                    provider.path().to_string_lossy().to_string(),
                ],
            },
        )
        .await
        .expect("provider response");

        assert!(response.success);
        assert!(response_includes_native_image(&response));
    }
}
