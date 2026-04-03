"""Azure Key Vault helpers for backend runtime secrets."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from cachetools import TTLCache

from pydantic import BaseModel


class KeyVaultConfig(BaseModel):
    vault_url: str | None = None
    secret_prefix: str = "agentcore"


@dataclass(slots=True)
class KeyVaultSecretStore:
    """Thin wrapper around Azure Key Vault SecretClient."""

    _client: object
    _cache: TTLCache = field(default_factory=lambda: TTLCache(maxsize=512, ttl=600))

    @classmethod
    def from_config(cls, config: KeyVaultConfig) -> "KeyVaultSecretStore | None":
        if not config.vault_url:
            return None

        from azure.identity import DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient

        credential = DefaultAzureCredential(
            exclude_environment_credential=True,
            exclude_interactive_browser_credential=True,
        )

        client = SecretClient(
            vault_url=config.vault_url,
            credential=credential,
            retry_total=5,
            retry_connect=3,
            retry_read=3,
            retry_backoff_factor=0.8,
        )
        return cls(_client=client)

    def get_secret(self, name: str) -> str | None:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            cached = self._cache.get(name)
            if cached is not None:
                return cached
            value = self._client.get_secret(name).value
            if value is not None:
                self._cache[name] = value
            return value
        except ResourceNotFoundError:
            return None

    def set_secret(self, name: str, value: str) -> None:
        if value is None:
            return
        self._client.set_secret(name=name, value=value)
        self._cache[name] = value


def resolve_backend_secrets_from_key_vault() -> None:
    """Load required backend secrets from Azure Key Vault into environment."""
    vault_url = os.getenv("AGENTCORE_KEY_VAULT_URL", "").strip()
    if not vault_url:
        return

    kv_store = KeyVaultSecretStore.from_config(
        KeyVaultConfig(
            vault_url=vault_url,
            secret_prefix=os.getenv("AGENTCORE_KEY_VAULT_SECRET_PREFIX", "agentcore").strip() or "agentcore",
        )
    )
    if kv_store is None:
        msg = "Azure Key Vault client is not initialized. Check AGENTCORE_KEY_VAULT_URL."
        raise RuntimeError(msg)

    mappings = {
        "DATABASE_URL": "AGENTCORE_KEY_VAULT_DATABASE_URL_SECRET_NAME",
        "AGENTCORE_SECRET_KEY": "AGENTCORE_KEY_VAULT_SECRET_KEY_SECRET_NAME",
        "AZURE_CLIENT_SECRET": "AGENTCORE_KEY_VAULT_AZURE_CLIENT_SECRET_SECRET_NAME",
        "AZURE_STORAGE_CONNECTION_STRING": "AGENTCORE_KEY_VAULT_AZURE_STORAGE_CONNECTION_STRING_SECRET_NAME",
        "MODEL_SERVICE_API_KEY": "AGENTCORE_KEY_VAULT_MODEL_SERVICE_API_KEY_SECRET_NAME",
        "MCP_SERVICE_API_KEY": "AGENTCORE_KEY_VAULT_MCP_SERVICE_API_KEY_SECRET_NAME",
        "GUARDRAILS_SERVICE_API_KEY": "AGENTCORE_KEY_VAULT_GUARDRAILS_SERVICE_API_KEY_SECRET_NAME",
        "PINECONE_SERVICE_API_KEY": "AGENTCORE_KEY_VAULT_PINECONE_SERVICE_API_KEY_SECRET_NAME",
        "GRAPH_RAG_SERVICE_API_KEY": "AGENTCORE_KEY_VAULT_GRAPH_RAG_SERVICE_API_KEY_SECRET_NAME",
        "GITHUB_TOKEN": "AGENTCORE_KEY_VAULT_GITHUB_TOKEN_SECRET_NAME",
        "ADO_TOKEN": "AGENTCORE_KEY_VAULT_ADO_TOKEN_SECRET_NAME",
        "NEO4J_PASSWORD": "AGENTCORE_KEY_VAULT_NEO4J_PASSWORD_SECRET_NAME",
        "LTM_EMBEDDING_API_KEY": "AGENTCORE_KEY_VAULT_LTM_EMBEDDING_API_KEY_SECRET_NAME",
        "LTM_PINECONE_API_KEY":"AGENTCORE_KEY_VAULT_LTM_PINECONE_API_KEY_SECRET_NAME",
        "AGENTCORE_INTERNAL_SECRET": "AGENTCORE_KEY_VAULT_INTERNAL_SECRET_SECRET_NAME",
    }

    for env_name, secret_name_env in mappings.items():
        secret_name = (os.getenv(secret_name_env) or "").strip()
        if not secret_name:
            msg = f"{secret_name_env} is required."
            raise RuntimeError(msg)
        secret_value = kv_store.get_secret(secret_name)
        if not secret_value:
            msg = f"Key Vault secret '{secret_name}' for {env_name} was not found or is empty."
            raise RuntimeError(msg)
        os.environ[env_name] = secret_value

    # Optional secrets — only loaded if the env var pointing to the secret name is set.
    # These don't break startup if missing (e.g. BACKEND_SERVICE_API_KEY is only needed
    # on deployments that accept cross-region gateway calls).
    optional_mappings = {
        "BACKEND_SERVICE_API_KEY": "AGENTCORE_KEY_VAULT_BACKEND_SERVICE_API_KEY_SECRET_NAME",
        "AZURE_AI_SEARCH_ENDPOINT": "AGENTCORE_KEY_VAULT_AZURE_AI_SEARCH_ENDPOINT_SECRET_NAME",
        "AZURE_AI_SEARCH_API_KEY": "AGENTCORE_KEY_VAULT_AZURE_AI_SEARCH_API_KEY_SECRET_NAME",
        "GRAFANA_API_KEY": "AGENTCORE_KEY_VAULT_GRAFANA_API_KEY_SECRET_NAME",
        "AZURE_PROMETHEUS_CLIENT_SECRET": "AGENTCORE_KEY_VAULT_PROMETHEUS_CLIENT_SECRET_SECRET_NAME",
        "RABBITMQ_URL": "AGENTCORE_KEY_VAULT_RABBITMQ_URL_SECRET_NAME",
    }

    for env_name, secret_name_env in optional_mappings.items():
        secret_name = (os.getenv(secret_name_env) or "").strip()
        if not secret_name:
            continue
        secret_value = kv_store.get_secret(secret_name)
        if secret_value:
            os.environ[env_name] = secret_value
