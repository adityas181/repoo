from typing import Optional

import redis.asyncio as redis
from redis.asyncio.retry import Retry
from redis.backoff import ExponentialBackoff
from redis.credentials import CredentialProvider
from redis.exceptions import ConnectionError, TimeoutError

from agentcore.services.cache.entra_credential_provider import AzureEntraRedisCredentialProvider
from agentcore.services.settings.service import SettingsService

_redis_client: Optional[redis.StrictRedis] = None
_redis_signature: Optional[tuple] = None
_redis_credential_provider: Optional[CredentialProvider] = None
_redis_credential_provider_signature: Optional[tuple] = None

def _get_redis_entra_scope(settings_service: SettingsService) -> str:
    redis_entra_scope = (settings_service.settings.redis_entra_scope or "").strip()
    if not redis_entra_scope:
        msg = "REDIS_ENTRA_SCOPE must be set."
        raise ValueError(msg)
    return redis_entra_scope


def get_redis_credential_provider(settings_service: SettingsService) -> CredentialProvider:
    global _redis_credential_provider, _redis_credential_provider_signature

    redis_entra_scope = _get_redis_entra_scope(settings_service)
    redis_entra_object_id = (settings_service.settings.redis_entra_object_id or "").strip()
    redis_entra_refresh_margin_seconds = settings_service.settings.redis_entra_refresh_margin_seconds
    signature = (
        redis_entra_scope,
        redis_entra_object_id,
        redis_entra_refresh_margin_seconds,
    )

    if _redis_credential_provider is None or _redis_credential_provider_signature != signature:
        if _redis_credential_provider is not None:
            close_provider = getattr(_redis_credential_provider, "close", None)
            if callable(close_provider):
                close_provider()

        _redis_credential_provider = AzureEntraRedisCredentialProvider(
            scope=redis_entra_scope,
            object_id=redis_entra_object_id or None,
            refresh_margin_seconds=redis_entra_refresh_margin_seconds,
        )
        _redis_credential_provider_signature = signature

    return _redis_credential_provider


def get_redis_client(settings_service: SettingsService) -> redis.StrictRedis:
    global _redis_client, _redis_signature
    signature = (
        settings_service.settings.redis_host,
        settings_service.settings.redis_port,
        settings_service.settings.redis_db,
        settings_service.settings.redis_ssl,
        _get_redis_entra_scope(settings_service),
        (settings_service.settings.redis_entra_object_id or "").strip(),
        settings_service.settings.redis_entra_refresh_margin_seconds,
    )
    if _redis_client is None or _redis_signature != signature:
        redis_credential_provider = get_redis_credential_provider(settings_service)
        _redis_client = redis.StrictRedis(
            host=settings_service.settings.redis_host,
            port=settings_service.settings.redis_port,
            db=settings_service.settings.redis_db,
            ssl=settings_service.settings.redis_ssl,
            credential_provider=redis_credential_provider,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
            # Auto-detect stale connections before use
            health_check_interval=15,
            # Retry on transient connection drops (Azure idle timeout, etc.)
            retry=Retry(ExponentialBackoff(cap=2, base=0.1), retries=3),
            retry_on_error=[ConnectionError, TimeoutError, OSError],
        )
        _redis_signature = signature
    return _redis_client
