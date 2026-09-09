use chrono::DateTime;
use chrono::Utc;
use serde::Deserialize;
use serde::Serialize;
use sha2::Digest;
use sha2::Sha256;
use std::collections::HashMap;
use std::fmt::Debug;
use std::fs::File;
use std::fs::OpenOptions;
use std::io::Read;
use std::io::Write;
#[cfg(unix)]
use std::os::unix::fs::OpenOptionsExt;
use std::path::Path;
use std::path::PathBuf;
use std::sync::Arc;
use std::sync::Mutex;
use std::sync::MutexGuard;
use tracing::warn;

use super::BedrockApiKeyAuth;
use crate::token_data::TokenData;
use codex_agent_identity::AgentIdentityJwtClaims;
use codex_agent_identity::decode_agent_identity_jwt;
use codex_config::types::AuthCredentialsStoreMode;
pub use codex_config::types::AuthKeyringBackendKind;
use codex_keyring_store::DefaultKeyringStore;
use codex_keyring_store::KeyringStore;
use codex_protocol::account::PlanType as AccountPlanType;
use codex_protocol::auth::AuthMode;
use codex_secrets::LocalSecretsNamespace;
use codex_secrets::SecretName;
use codex_secrets::SecretScope;
use codex_secrets::SecretsBackendKind;
use codex_secrets::SecretsManager;
use once_cell::sync::Lazy;

/// Expected structure for $CODEX_HOME/auth.json.
#[derive(Deserialize, Serialize, Clone, Debug, PartialEq)]
pub struct AuthDotJson {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub auth_mode: Option<AuthMode>,

    #[serde(rename = "OPENAI_API_KEY")]
    pub openai_api_key: Option<String>,

    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tokens: Option<TokenData>,

    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_refresh: Option<DateTime<Utc>>,

    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub agent_identity: Option<AgentIdentityStorage>,

    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub personal_access_token: Option<String>,

    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub bedrock_api_key: Option<BedrockApiKeyAuth>,
}

#[derive(Deserialize, Serialize, Clone, Debug, PartialEq, Eq)]
#[serde(untagged)]
pub enum AgentIdentityStorage {
    Jwt(String),
    Record(AgentIdentityAuthRecord),
}

impl AgentIdentityStorage {
    pub fn has_auth_material(&self) -> bool {
        match self {
            Self::Jwt(jwt) => !jwt.trim().is_empty(),
            Self::Record(record) => {
                !record.agent_runtime_id.trim().is_empty()
                    && !record.agent_private_key.trim().is_empty()
            }
        }
    }

    pub(crate) fn as_record(&self) -> Option<&AgentIdentityAuthRecord> {
        match self {
            Self::Jwt(_) => None,
            Self::Record(record) => Some(record),
        }
    }

    pub(crate) fn matches_record(&self, record: &AgentIdentityAuthRecord) -> bool {
        match self {
            Self::Jwt(jwt) => AgentIdentityAuthRecord::from_agent_identity_jwt(jwt)
                .ok()
                .is_some_and(|stored| stored.same_credential(record)),
            Self::Record(stored) => stored.same_credential(record),
        }
    }
}

#[derive(Deserialize, Serialize, Clone, Debug, PartialEq, Eq)]
pub struct AgentIdentityAuthRecord {
    pub agent_runtime_id: String,
    pub agent_private_key: String,
    pub account_id: String,
    pub chatgpt_user_id: String,
    #[serde(
        default,
        deserialize_with = "deserialize_optional_non_empty_string",
        serialize_with = "serialize_optional_string_as_empty"
    )]
    pub email: Option<String>,
    pub plan_type: AccountPlanType,
    pub chatgpt_account_is_fedramp: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub task_id: Option<String>,
}

fn deserialize_optional_non_empty_string<'de, D>(
    deserializer: D,
) -> Result<Option<String>, D::Error>
where
    D: serde::Deserializer<'de>,
{
    Option::<String>::deserialize(deserializer).map(|value| value.filter(|value| !value.is_empty()))
}

fn serialize_optional_string_as_empty<S>(
    value: &Option<String>,
    serializer: S,
) -> Result<S::Ok, S::Error>
where
    S: serde::Serializer,
{
    value.as_deref().unwrap_or_default().serialize(serializer)
}

impl AgentIdentityAuthRecord {
    pub(crate) fn from_agent_identity_jwt(jwt: &str) -> std::io::Result<Self> {
        let claims =
            decode_agent_identity_jwt(jwt, /*jwks*/ None).map_err(std::io::Error::other)?;

        Ok(claims.into())
    }

    pub(crate) fn same_credential(&self, other: &Self) -> bool {
        self.agent_runtime_id == other.agent_runtime_id
            && self.agent_private_key == other.agent_private_key
            && self.account_id == other.account_id
            && self.chatgpt_user_id == other.chatgpt_user_id
    }
}

impl From<AgentIdentityJwtClaims> for AgentIdentityAuthRecord {
    fn from(claims: AgentIdentityJwtClaims) -> Self {
        Self {
            agent_runtime_id: claims.agent_runtime_id,
            agent_private_key: claims.agent_private_key,
            account_id: claims.account_id,
            chatgpt_user_id: claims.chatgpt_user_id,
            email: claims.email,
            plan_type: claims.plan_type.into(),
            chatgpt_account_is_fedramp: claims.chatgpt_account_is_fedramp,
            task_id: None,
        }
    }
}

pub(super) fn get_auth_file(codex_home: &Path) -> PathBuf {
    codex_home.join("auth.json")
}

pub(super) fn delete_file_if_exists(codex_home: &Path) -> std::io::Result<bool> {
    let auth_file = get_auth_file(codex_home);
    match std::fs::remove_file(&auth_file) {
        Ok(()) => Ok(true),
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => Ok(false),
        Err(err) => Err(err),
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum AuthStorageSource {
    File,
    DirectKeyring,
    SecretsKeyring,
    Ephemeral,
}

#[derive(Clone, Debug, PartialEq)]
pub(super) struct AuthStorageSnapshot {
    pub source: AuthStorageSource,
    pub auth: Option<AuthDotJson>,
}

pub(super) trait AuthStorageBackend: Debug + Send + Sync {
    fn codex_home(&self) -> &Path;
    fn source(&self) -> AuthStorageSource;
    fn load_unlocked(&self) -> std::io::Result<Option<AuthDotJson>>;
    fn save_unlocked(&self, auth: &AuthDotJson) -> std::io::Result<()>;
    fn delete_unlocked(&self) -> std::io::Result<bool>;

    fn load(&self) -> std::io::Result<Option<AuthDotJson>> {
        Ok(self.begin_transaction()?.snapshot().auth.clone())
    }

    fn save(&self, auth: &AuthDotJson) -> std::io::Result<()> {
        let mut transaction = self.begin_transaction()?;
        // A direct save is a new Auto policy decision: prefer the keyring and
        // fall back to the file when that backend is unavailable. Operations
        // that already resolved an authority use `transaction.save` below so
        // they remain pinned to their transaction snapshot.
        transaction.save_preferred(auth)
    }

    fn delete(&self) -> std::io::Result<bool> {
        let mut transaction = self.begin_transaction()?;
        transaction.delete()
    }

    fn resolve_snapshot_unlocked(&self) -> std::io::Result<AuthStorageSnapshot> {
        Ok(AuthStorageSnapshot {
            source: self.source(),
            auth: self.load_unlocked()?,
        })
    }

    fn save_to_source_unlocked(
        &self,
        source: AuthStorageSource,
        auth: &AuthDotJson,
    ) -> std::io::Result<()> {
        if source != self.source() {
            return Err(std::io::Error::other("auth storage source changed"));
        }
        self.save_unlocked(auth)
    }

    fn delete_from_source_unlocked(&self, source: AuthStorageSource) -> std::io::Result<bool> {
        if source != self.source() {
            return Err(std::io::Error::other("auth storage source changed"));
        }
        self.delete_unlocked()
    }

    fn begin_transaction(&self) -> std::io::Result<AuthStorageTransaction<'_>> {
        let process_guard = AUTH_STORAGE_TRANSACTION_LOCK
            .lock()
            .map_err(|_| std::io::Error::other("failed to lock auth storage"))?;
        let file_guard = lock_auth_storage(self.codex_home())?;
        let snapshot = self.resolve_snapshot_unlocked()?;
        Ok(AuthStorageTransaction {
            _process_guard: process_guard,
            _file_guard: file_guard,
            snapshot,
            save_to_source: Box::new(move |source, auth| {
                self.save_to_source_unlocked(source, auth)
            }),
            save_preferred: Box::new(move |auth| self.save_unlocked(auth)),
            delete_from_source: Box::new(move |source| self.delete_from_source_unlocked(source)),
        })
    }
}

pub(super) struct AuthStorageTransaction<'a> {
    _process_guard: MutexGuard<'static, ()>,
    _file_guard: File,
    snapshot: AuthStorageSnapshot,
    save_to_source: Box<dyn Fn(AuthStorageSource, &AuthDotJson) -> std::io::Result<()> + 'a>,
    save_preferred: Box<dyn Fn(&AuthDotJson) -> std::io::Result<()> + 'a>,
    delete_from_source: Box<dyn Fn(AuthStorageSource) -> std::io::Result<bool> + 'a>,
}

impl AuthStorageTransaction<'_> {
    pub(super) fn snapshot(&self) -> &AuthStorageSnapshot {
        &self.snapshot
    }

    pub(super) fn save(&mut self, auth: &AuthDotJson) -> std::io::Result<()> {
        (self.save_to_source)(self.snapshot.source, auth)?;
        self.snapshot.auth = Some(auth.clone());
        Ok(())
    }

    pub(super) fn save_preferred(&mut self, auth: &AuthDotJson) -> std::io::Result<()> {
        (self.save_preferred)(auth)?;
        self.snapshot.auth = Some(auth.clone());
        Ok(())
    }

    pub(super) fn delete(&mut self) -> std::io::Result<bool> {
        let removed = (self.delete_from_source)(self.snapshot.source)?;
        if removed {
            self.snapshot.auth = None;
        }
        Ok(removed)
    }

    pub(super) fn compare_delete(
        &mut self,
        should_delete: impl FnOnce(&AuthDotJson) -> bool,
    ) -> std::io::Result<bool> {
        let Some(auth) = self.snapshot.auth.as_ref() else {
            return Ok(false);
        };
        if !should_delete(auth) {
            return Ok(false);
        }
        self.delete()
    }
}

// Serialize transactions in-process as well as across cooperating Codex
// processes. The lock is deliberately separate from CODEX_HOME so acquiring it
// does not create or modify the durable auth directory.
static AUTH_STORAGE_TRANSACTION_LOCK: Lazy<Mutex<()>> = Lazy::new(|| Mutex::new(()));

fn lock_auth_storage(codex_home: &Path) -> std::io::Result<File> {
    let absolute_codex_home = std::path::absolute(codex_home)?;
    let mut hasher = Sha256::new();
    hasher.update(absolute_codex_home.to_string_lossy().as_bytes());
    let lock_key = digest_hex(hasher.finalize());
    let lock_dir = std::env::temp_dir().join("codex-auth-locks");
    std::fs::create_dir_all(&lock_dir)?;
    let lock_path = lock_dir.join(lock_key);
    let mut options = OpenOptions::new();
    options.read(true).write(true).create(true);
    #[cfg(unix)]
    options.mode(0o600);
    let lock_file = options.open(lock_path)?;
    lock_file.lock()?;
    Ok(lock_file)
}

#[derive(Clone, Debug)]
pub(super) struct FileAuthStorage {
    codex_home: PathBuf,
}

impl FileAuthStorage {
    pub(super) fn new(codex_home: PathBuf) -> Self {
        Self { codex_home }
    }

    /// Attempt to read and parse the `auth.json` file in the given `CODEX_HOME` directory.
    /// Returns the full AuthDotJson structure.
    pub(super) fn try_read_auth_json(&self, auth_file: &Path) -> std::io::Result<AuthDotJson> {
        let mut file = File::open(auth_file)?;
        let mut contents = String::new();
        file.read_to_string(&mut contents)?;
        let auth_dot_json: AuthDotJson = serde_json::from_str(&contents)?;

        Ok(auth_dot_json)
    }
}

impl AuthStorageBackend for FileAuthStorage {
    fn codex_home(&self) -> &Path {
        &self.codex_home
    }

    fn source(&self) -> AuthStorageSource {
        AuthStorageSource::File
    }

    fn load_unlocked(&self) -> std::io::Result<Option<AuthDotJson>> {
        let auth_file = get_auth_file(&self.codex_home);
        let auth_dot_json = match self.try_read_auth_json(&auth_file) {
            Ok(auth) => auth,
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(err) => return Err(err),
        };
        Ok(Some(auth_dot_json))
    }

    fn save_unlocked(&self, auth_dot_json: &AuthDotJson) -> std::io::Result<()> {
        let auth_file = get_auth_file(&self.codex_home);

        if let Some(parent) = auth_file.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let json_data = serde_json::to_string_pretty(auth_dot_json)?;
        let mut options = OpenOptions::new();
        options.truncate(true).write(true).create(true);
        #[cfg(unix)]
        {
            options.mode(0o600);
        }
        let mut file = options.open(auth_file)?;
        file.write_all(json_data.as_bytes())?;
        file.flush()?;
        Ok(())
    }

    fn delete_unlocked(&self) -> std::io::Result<bool> {
        delete_file_if_exists(&self.codex_home)
    }
}

static CODEX_AUTH_SECRET_NAME: Lazy<SecretName> =
    Lazy::new(|| match SecretName::new("CODEX_AUTH") {
        Ok(name) => name,
        Err(err) => unreachable!("CODEX_AUTH should be a valid secret name: {err}"),
    });
const KEYRING_SERVICE: &str = "Codex Auth";

// turns codex_home path into a stable, short key string
fn compute_store_key(codex_home: &Path) -> std::io::Result<String> {
    let canonical = codex_home
        .canonicalize()
        .unwrap_or_else(|_| codex_home.to_path_buf());
    let path_str = canonical.to_string_lossy();
    let mut hasher = Sha256::new();
    hasher.update(path_str.as_bytes());
    let digest = hasher.finalize();
    let hex = digest_hex(digest);
    let truncated = hex.get(..16).unwrap_or(&hex);
    Ok(format!("cli|{truncated}"))
}

fn digest_hex(digest: impl AsRef<[u8]>) -> String {
    use std::fmt::Write as _;

    let digest = digest.as_ref();
    let mut hex = String::with_capacity(digest.len() * 2);
    for &byte in digest {
        let _ = write!(&mut hex, "{byte:02x}");
    }
    hex
}

#[derive(Clone, Debug)]
struct DirectKeyringAuthStorage {
    codex_home: PathBuf,
    keyring_store: Arc<dyn KeyringStore>,
}

impl DirectKeyringAuthStorage {
    fn new(codex_home: PathBuf, keyring_store: Arc<dyn KeyringStore>) -> Self {
        Self {
            codex_home,
            keyring_store,
        }
    }

    fn load_from_keyring(&self, key: &str) -> std::io::Result<Option<AuthDotJson>> {
        match self.keyring_store.load(KEYRING_SERVICE, key) {
            Ok(Some(serialized)) => serde_json::from_str(&serialized).map(Some).map_err(|err| {
                std::io::Error::other(format!(
                    "failed to deserialize CLI auth from keyring: {err}"
                ))
            }),
            Ok(None) => Ok(None),
            Err(error) => Err(std::io::Error::other(format!(
                "failed to load CLI auth from keyring: {}",
                error.message()
            ))),
        }
    }

    fn save_to_keyring(&self, key: &str, value: &str) -> std::io::Result<()> {
        match self.keyring_store.save(KEYRING_SERVICE, key, value) {
            Ok(()) => Ok(()),
            Err(error) => {
                let message = format!(
                    "failed to write OAuth tokens to keyring: {}",
                    error.message()
                );
                warn!("{message}");
                Err(std::io::Error::other(message))
            }
        }
    }
}

impl AuthStorageBackend for DirectKeyringAuthStorage {
    fn codex_home(&self) -> &Path {
        &self.codex_home
    }

    fn source(&self) -> AuthStorageSource {
        AuthStorageSource::DirectKeyring
    }

    fn load_unlocked(&self) -> std::io::Result<Option<AuthDotJson>> {
        let key = compute_store_key(&self.codex_home)?;
        self.load_from_keyring(&key)
    }

    fn save_unlocked(&self, auth: &AuthDotJson) -> std::io::Result<()> {
        let key = compute_store_key(&self.codex_home)?;
        // Simpler error mapping per style: prefer method reference over closure
        let serialized = serde_json::to_string(auth).map_err(std::io::Error::other)?;
        self.save_to_keyring(&key, &serialized)?;
        if let Err(err) = delete_file_if_exists(&self.codex_home) {
            warn!("failed to remove CLI auth fallback file: {err}");
        }
        Ok(())
    }

    fn delete_unlocked(&self) -> std::io::Result<bool> {
        let key = compute_store_key(&self.codex_home)?;
        let keyring_removed = self
            .keyring_store
            .delete(KEYRING_SERVICE, &key)
            .map_err(|err| {
                std::io::Error::other(format!("failed to delete auth from keyring: {err}"))
            })?;
        let file_removed = delete_file_if_exists(&self.codex_home)?;
        Ok(keyring_removed || file_removed)
    }
}

#[derive(Clone)]
struct SecretsKeyringAuthStorage {
    codex_home: PathBuf,
    direct_storage: DirectKeyringAuthStorage,
    secrets_manager: SecretsManager,
}

impl Debug for SecretsKeyringAuthStorage {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("SecretsKeyringAuthStorage")
            .field("codex_home", &self.codex_home)
            .finish_non_exhaustive()
    }
}

impl SecretsKeyringAuthStorage {
    fn new(codex_home: PathBuf, keyring_store: Arc<dyn KeyringStore>) -> Self {
        let direct_storage =
            DirectKeyringAuthStorage::new(codex_home.clone(), Arc::clone(&keyring_store));
        let secrets_manager = SecretsManager::new_with_keyring_store_and_namespace(
            codex_home.clone(),
            SecretsBackendKind::Local,
            keyring_store,
            LocalSecretsNamespace::CodexAuth,
        );
        Self {
            codex_home,
            direct_storage,
            secrets_manager,
        }
    }
}

impl AuthStorageBackend for SecretsKeyringAuthStorage {
    fn codex_home(&self) -> &Path {
        &self.codex_home
    }

    fn source(&self) -> AuthStorageSource {
        AuthStorageSource::SecretsKeyring
    }

    fn load_unlocked(&self) -> std::io::Result<Option<AuthDotJson>> {
        match self
            .secrets_manager
            .get(&SecretScope::Global, &CODEX_AUTH_SECRET_NAME)
            .map_err(|err| {
                std::io::Error::other(format!(
                    "failed to load CLI auth from encrypted auth storage: {err}"
                ))
            })? {
            Some(serialized) => serde_json::from_str(&serialized).map(Some).map_err(|err| {
                std::io::Error::other(format!(
                    "failed to deserialize CLI auth from encrypted auth storage: {err}"
                ))
            }),
            None => Ok(None),
        }
    }

    fn save_unlocked(&self, auth: &AuthDotJson) -> std::io::Result<()> {
        let serialized = serde_json::to_string(auth).map_err(std::io::Error::other)?;
        self.secrets_manager
            .set(&SecretScope::Global, &CODEX_AUTH_SECRET_NAME, &serialized)
            .map_err(|err| {
                let message =
                    format!("failed to write OAuth tokens to encrypted auth storage: {err}");
                warn!("{message}");
                std::io::Error::other(message)
            })?;
        if let Err(err) = delete_file_if_exists(&self.codex_home) {
            warn!("failed to remove CLI auth fallback file: {err}");
        }
        Ok(())
    }

    fn delete_unlocked(&self) -> std::io::Result<bool> {
        let keyring_removed = self
            .secrets_manager
            .delete(&SecretScope::Global, &CODEX_AUTH_SECRET_NAME)
            .map_err(|err| {
                std::io::Error::other(format!(
                    "failed to delete auth from encrypted auth storage: {err}"
                ))
            })?;
        let file_removed = delete_file_if_exists(&self.codex_home)?;
        // The outer auth transaction already owns the process and file locks.
        // Re-entering `delete` here would try to acquire the non-reentrant
        // process lock a second time and deadlock the transaction.
        let direct_removed = self.direct_storage.delete_unlocked()?;
        Ok(keyring_removed || file_removed || direct_removed)
    }
}

#[derive(Clone, Debug)]
struct AutoAuthStorage {
    keyring_storage: Arc<dyn AuthStorageBackend>,
    file_storage: Arc<FileAuthStorage>,
}

impl AutoAuthStorage {
    fn new(
        codex_home: PathBuf,
        keyring_store: Arc<dyn KeyringStore>,
        keyring_backend_kind: AuthKeyringBackendKind,
    ) -> Self {
        Self {
            keyring_storage: create_keyring_auth_storage(
                codex_home.clone(),
                keyring_store,
                keyring_backend_kind,
            ),
            file_storage: Arc::new(FileAuthStorage::new(codex_home)),
        }
    }
}

impl AuthStorageBackend for AutoAuthStorage {
    fn codex_home(&self) -> &Path {
        self.file_storage.codex_home()
    }

    // Auto resolves its concrete source as part of the transaction snapshot.
    // This value is only a placeholder for callers that do not use a snapshot.
    fn source(&self) -> AuthStorageSource {
        AuthStorageSource::File
    }

    fn resolve_snapshot_unlocked(&self) -> std::io::Result<AuthStorageSnapshot> {
        match self.keyring_storage.load_unlocked() {
            Ok(Some(auth)) => Ok(AuthStorageSnapshot {
                source: self.keyring_storage.source(),
                auth: Some(auth),
            }),
            Ok(None) => {
                let file_auth = self.file_storage.load_unlocked()?;
                Ok(AuthStorageSnapshot {
                    source: if file_auth.is_some() {
                        AuthStorageSource::File
                    } else {
                        self.keyring_storage.source()
                    },
                    auth: file_auth,
                })
            }
            Err(err) => {
                warn!("failed to load CLI auth from keyring, falling back to file storage: {err}");
                Ok(AuthStorageSnapshot {
                    source: AuthStorageSource::File,
                    auth: self.file_storage.load_unlocked()?,
                })
            }
        }
    }

    fn load_unlocked(&self) -> std::io::Result<Option<AuthDotJson>> {
        Ok(self.resolve_snapshot_unlocked()?.auth)
    }

    fn save_unlocked(&self, auth: &AuthDotJson) -> std::io::Result<()> {
        match self.keyring_storage.save_unlocked(auth) {
            Ok(()) => Ok(()),
            Err(err) => {
                warn!("failed to save auth to keyring, falling back to file storage: {err}");
                self.file_storage.save_unlocked(auth)
            }
        }
    }

    fn save_to_source_unlocked(
        &self,
        source: AuthStorageSource,
        auth: &AuthDotJson,
    ) -> std::io::Result<()> {
        match source {
            AuthStorageSource::File => self.file_storage.save_unlocked(auth),
            source if source == self.keyring_storage.source() => {
                self.keyring_storage.save_unlocked(auth)
            }
            _ => Err(std::io::Error::other("auth storage source changed")),
        }
    }

    fn delete_unlocked(&self) -> std::io::Result<bool> {
        // Preserve the historical Auto logout behavior: remove both the keyring
        // entry and any fallback file.
        match self.keyring_storage.delete_unlocked() {
            Ok(keyring_removed) => {
                let file_removed = self.file_storage.delete_unlocked()?;
                Ok(keyring_removed || file_removed)
            }
            Err(err) => {
                warn!(
                    "failed to delete CLI auth from keyring, falling back to file storage: {err}"
                );
                self.file_storage.delete_unlocked()
            }
        }
    }

    fn delete_from_source_unlocked(&self, source: AuthStorageSource) -> std::io::Result<bool> {
        match source {
            AuthStorageSource::File => self.file_storage.delete_unlocked(),
            source if source == self.keyring_storage.source() => {
                self.keyring_storage.delete_unlocked()
            }
            _ => Err(std::io::Error::other("auth storage source changed")),
        }
    }

    fn delete(&self) -> std::io::Result<bool> {
        let _process_guard = AUTH_STORAGE_TRANSACTION_LOCK
            .lock()
            .map_err(|_| std::io::Error::other("failed to lock auth storage"))?;
        let _file_guard = lock_auth_storage(self.codex_home())?;
        self.delete_unlocked()
    }
}

// A global in-memory store for mapping codex_home -> AuthDotJson.
static EPHEMERAL_AUTH_STORE: Lazy<Mutex<HashMap<String, AuthDotJson>>> =
    Lazy::new(|| Mutex::new(HashMap::new()));

#[derive(Clone, Debug)]
struct EphemeralAuthStorage {
    codex_home: PathBuf,
}

impl EphemeralAuthStorage {
    fn new(codex_home: PathBuf) -> Self {
        Self { codex_home }
    }

    fn with_store<F, T>(&self, action: F) -> std::io::Result<T>
    where
        F: FnOnce(&mut HashMap<String, AuthDotJson>, String) -> std::io::Result<T>,
    {
        let key = compute_store_key(&self.codex_home)?;
        let mut store = EPHEMERAL_AUTH_STORE
            .lock()
            .map_err(|_| std::io::Error::other("failed to lock ephemeral auth storage"))?;
        action(&mut store, key)
    }
}

impl AuthStorageBackend for EphemeralAuthStorage {
    fn codex_home(&self) -> &Path {
        &self.codex_home
    }

    fn source(&self) -> AuthStorageSource {
        AuthStorageSource::Ephemeral
    }

    fn load_unlocked(&self) -> std::io::Result<Option<AuthDotJson>> {
        self.with_store(|store, key| Ok(store.get(&key).cloned()))
    }

    fn save_unlocked(&self, auth: &AuthDotJson) -> std::io::Result<()> {
        self.with_store(|store, key| {
            store.insert(key, auth.clone());
            Ok(())
        })
    }

    fn delete_unlocked(&self) -> std::io::Result<bool> {
        self.with_store(|store, key| Ok(store.remove(&key).is_some()))
    }
}

pub(super) fn create_auth_storage(
    codex_home: PathBuf,
    mode: AuthCredentialsStoreMode,
    keyring_backend_kind: AuthKeyringBackendKind,
) -> Arc<dyn AuthStorageBackend> {
    let keyring_store: Arc<dyn KeyringStore> = Arc::new(DefaultKeyringStore);
    create_auth_storage_with_store(codex_home, mode, keyring_store, keyring_backend_kind)
}

fn create_auth_storage_with_store(
    codex_home: PathBuf,
    mode: AuthCredentialsStoreMode,
    keyring_store: Arc<dyn KeyringStore>,
    keyring_backend_kind: AuthKeyringBackendKind,
) -> Arc<dyn AuthStorageBackend> {
    match mode {
        AuthCredentialsStoreMode::File => Arc::new(FileAuthStorage::new(codex_home)),
        AuthCredentialsStoreMode::Keyring => {
            create_keyring_auth_storage(codex_home, keyring_store, keyring_backend_kind)
        }
        AuthCredentialsStoreMode::Auto => Arc::new(AutoAuthStorage::new(
            codex_home,
            keyring_store,
            keyring_backend_kind,
        )),
        AuthCredentialsStoreMode::Ephemeral => Arc::new(EphemeralAuthStorage::new(codex_home)),
    }
}

fn create_keyring_auth_storage(
    codex_home: PathBuf,
    keyring_store: Arc<dyn KeyringStore>,
    keyring_backend_kind: AuthKeyringBackendKind,
) -> Arc<dyn AuthStorageBackend> {
    match keyring_backend_kind {
        AuthKeyringBackendKind::Direct => {
            Arc::new(DirectKeyringAuthStorage::new(codex_home, keyring_store))
        }
        AuthKeyringBackendKind::Secrets => {
            Arc::new(SecretsKeyringAuthStorage::new(codex_home, keyring_store))
        }
    }
}

#[cfg(test)]
#[path = "storage_tests.rs"]
mod tests;
