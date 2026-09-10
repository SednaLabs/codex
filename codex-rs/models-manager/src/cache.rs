use chrono::DateTime;
use chrono::Utc;
use codex_protocol::openai_models::ModelInfo;
use codex_protocol::openai_models::deserialize_model_infos_with_legacy_base;
use serde::Deserialize;
use serde::Serialize;
use std::io;
use std::io::ErrorKind;
use std::path::PathBuf;
use std::time::Duration;
use tokio::fs;
use tracing::error;
use tracing::info;

/// Manages loading and saving of models cache to disk.
#[derive(Debug)]
pub(crate) struct ModelsCacheManager {
    cache_path: PathBuf,
    cache_ttl: Duration,
}

impl ModelsCacheManager {
    /// Create a new cache manager with the given path and TTL.
    pub(crate) fn new(cache_path: PathBuf, cache_ttl: Duration) -> Self {
        Self {
            cache_path,
            cache_ttl,
        }
    }

    /// Attempt to load a fresh cache entry. Returns `None` if the cache doesn't exist or is stale.
    pub(crate) async fn load_fresh(&self, expected_version: &str) -> Option<ModelsCache> {
        info!(
            cache_path = %self.cache_path.display(),
            expected_version,
            "models cache: attempting load_fresh"
        );
        let cache = match self.load().await {
            Ok(cache) => cache?,
            Err(err) => {
                error!("failed to load models cache: {err}");
                return None;
            }
        };
        info!(
            cache_path = %self.cache_path.display(),
            cached_version = ?cache.client_version,
            fetched_at = %cache.fetched_at,
            "models cache: loaded cache file"
        );
        if cache.client_version.as_deref() != Some(expected_version) {
            info!(
                cache_path = %self.cache_path.display(),
                expected_version,
                cached_version = ?cache.client_version,
                "models cache: cache version mismatch"
            );
            return None;
        }
        if !cache.is_fresh(self.cache_ttl) {
            info!(
                cache_path = %self.cache_path.display(),
                cache_ttl_secs = self.cache_ttl.as_secs(),
                fetched_at = %cache.fetched_at,
                "models cache: cache is stale"
            );
            return None;
        }
        info!(
            cache_path = %self.cache_path.display(),
            cache_ttl_secs = self.cache_ttl.as_secs(),
            "models cache: cache hit"
        );
        Some(cache)
    }

    /// Persist the cache to disk, creating parent directories as needed.
    pub(crate) async fn persist_cache(
        &self,
        models: &[ModelInfo],
        etag: Option<String>,
        client_version: String,
    ) {
        let cache = ModelsCache {
            fetched_at: Utc::now(),
            etag,
            client_version: Some(client_version),
            models: models.to_vec(),
        };
        if let Err(err) = self.save_internal(&cache).await {
            error!("failed to write models cache: {err}");
        }
    }

    /// Renew the cache TTL by updating the fetched_at timestamp to now.
    pub(crate) async fn renew_cache_ttl(&self) -> io::Result<()> {
        let mut cache = match self.load().await? {
            Some(cache) => cache,
            None => return Err(io::Error::new(ErrorKind::NotFound, "cache not found")),
        };
        cache.fetched_at = Utc::now();
        self.save_internal(&cache).await
    }

    async fn load(&self) -> io::Result<Option<ModelsCache>> {
        match fs::read(&self.cache_path).await {
            Ok(contents) => {
                let cache = serde_json::from_slice(&contents)
                    .map_err(|err| io::Error::new(ErrorKind::InvalidData, err.to_string()))?;
                Ok(Some(cache))
            }
            Err(err) if err.kind() == ErrorKind::NotFound => Ok(None),
            Err(err) => Err(err),
        }
    }

    async fn save_internal(&self, cache: &ModelsCache) -> io::Result<()> {
        if let Some(parent) = self.cache_path.parent() {
            fs::create_dir_all(parent).await?;
        }
        let json = serde_json::to_vec_pretty(cache)
            .map_err(|err| io::Error::new(ErrorKind::InvalidData, err.to_string()))?;
        fs::write(&self.cache_path, json).await
    }

    #[cfg(test)]
    /// Set the cache TTL.
    pub(crate) fn set_ttl(&mut self, ttl: Duration) {
        self.cache_ttl = ttl;
    }

    #[cfg(test)]
    /// Manipulate cache file for testing. Allows setting a custom fetched_at timestamp.
    pub(crate) async fn manipulate_cache_for_test<F>(&self, f: F) -> io::Result<()>
    where
        F: FnOnce(&mut DateTime<Utc>),
    {
        let mut cache = match self.load().await? {
            Some(cache) => cache,
            None => return Err(io::Error::new(ErrorKind::NotFound, "cache not found")),
        };
        f(&mut cache.fetched_at);
        self.save_internal(&cache).await
    }

    #[cfg(test)]
    /// Mutate the full cache contents for testing.
    pub(crate) async fn mutate_cache_for_test<F>(&self, f: F) -> io::Result<()>
    where
        F: FnOnce(&mut ModelsCache),
    {
        let mut cache = match self.load().await? {
            Some(cache) => cache,
            None => return Err(io::Error::new(ErrorKind::NotFound, "cache not found")),
        };
        f(&mut cache);
        self.save_internal(&cache).await
    }
}

/// Serialized snapshot of models and metadata cached on disk.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub(crate) struct ModelsCache {
    pub(crate) fetched_at: DateTime<Utc>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) etag: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) client_version: Option<String>,
    #[serde(deserialize_with = "deserialize_model_infos_with_legacy_base")]
    pub(crate) models: Vec<ModelInfo>,
}

impl ModelsCache {
    /// Returns `true` when the cache entry has not exceeded the configured TTL.
    fn is_fresh(&self, ttl: Duration) -> bool {
        if ttl.is_zero() {
            return false;
        }
        let Ok(ttl_duration) = chrono::Duration::from_std(ttl) else {
            return false;
        };
        let age = Utc::now().signed_duration_since(self.fetched_at);
        age <= ttl_duration
    }
}

#[cfg(test)]
mod tests {
    use super::ModelsCache;
    use serde_json::json;

    #[test]
    fn cache_deserializer_accepts_catalog_without_legacy_base() {
        let mut payload = json!({
            "fetched_at": "2026-09-07T00:00:00Z",
            "models": [{
                "slug": "astra",
                "display_name": "Astra",
                "description": null,
                "supported_reasoning_levels": [],
                "shell_type": "shell_command",
                "visibility": "list",
                "supported_in_api": true,
                "priority": 1,
                "availability_nux": null,
                "upgrade": null,
                "model_messages": {
                    "instructions_template": "template",
                    "instructions_variables": null
                },
                "support_verbosity": false,
                "default_verbosity": null,
                "apply_patch_tool_type": null,
                "truncation_policy": {"mode": "bytes", "limit": 10000},
                "supports_parallel_tool_calls": false,
                "experimental_supported_tools": []
            }]
        });
        let cache: ModelsCache =
            serde_json::from_value(payload.clone()).expect("cache catalog should deserialize");

        assert_eq!(cache.models[0].base_instructions, "template");

        payload["models"][0]
            .as_object_mut()
            .unwrap()
            .remove("model_messages");
        let error = serde_json::from_value::<ModelsCache>(payload)
            .unwrap_err()
            .to_string();
        assert!(error.contains("missing both"));

        let mut whitespace = json!({
            "fetched_at": "2026-09-07T00:00:00Z",
            "models": [{
                "slug": "astra",
                "display_name": "Astra",
                "description": null,
                "supported_reasoning_levels": [],
                "shell_type": "shell_command",
                "visibility": "list",
                "supported_in_api": true,
                "priority": 1,
                "availability_nux": null,
                "upgrade": null,
                "model_messages": {
                    "instructions_template": " \n\t",
                    "instructions_variables": null
                },
                "support_verbosity": false,
                "default_verbosity": null,
                "apply_patch_tool_type": null,
                "truncation_policy": {"mode": "bytes", "limit": 10000},
                "supports_parallel_tool_calls": false,
                "experimental_supported_tools": []
            }]
        });
        let error = serde_json::from_value::<ModelsCache>(whitespace.take())
            .unwrap_err()
            .to_string();
        assert!(error.contains("missing both"));
    }
}
