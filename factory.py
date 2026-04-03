from __future__ import annotations

from typing import TYPE_CHECKING

from typing_extensions import override

from agentcore.logging.logger import logger
from agentcore.services.cache.redis_client import get_redis_credential_provider
from agentcore.services.cache.service import AsyncInMemoryCache, CacheService, RedisCache, ThreadingInMemoryCache
from agentcore.services.factory import ServiceFactory

if TYPE_CHECKING:
    from agentcore.services.settings.service import SettingsService


class CacheServiceFactory(ServiceFactory):
    def __init__(self) -> None:
        super().__init__(CacheService)

    @override
    def create(self, settings_service: SettingsService):
        # Here you would have logic to create and configure a CacheService
        # based on the settings_service

        if settings_service.settings.cache_type == "redis":
            logger.debug("Creating Redis cache")
            return RedisCache(
                host=settings_service.settings.redis_host,
                port=settings_service.settings.redis_port,
                db=settings_service.settings.redis_db,
                credential_provider=get_redis_credential_provider(settings_service),
                ssl=settings_service.settings.redis_ssl,
                expiration_time=settings_service.settings.redis_cache_expire,
            )

        if settings_service.settings.cache_type == "memory":
            return ThreadingInMemoryCache(expiration_time=settings_service.settings.cache_expire)
        if settings_service.settings.cache_type == "async":
            return AsyncInMemoryCache(expiration_time=settings_service.settings.cache_expire)
        return None
