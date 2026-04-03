from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
import os
from dotenv import load_dotenv, find_dotenv
load_dotenv(find_dotenv())
from loguru import logger
from sqlalchemy import delete
from sqlalchemy import exc as sqlalchemy_exc
from sqlmodel import col, select

from agentcore.services.cache.base import ExternalAsyncBaseCacheService
from agentcore.services.cache.factory import CacheServiceFactory
from agentcore.services.database.models.transactions.model import TransactionTable
from agentcore.services.database.models.vertex_builds.model import VertexBuildTable
from agentcore.services.database.utils import initialize_database
from agentcore.services.schema import ServiceType
from agentcore.services.auth import permissions
from agentcore.services.auth.permissions import PermissionCacheService
from agentcore.services.cache.user_cache import UserCacheService
from .deps import get_db_service, get_service, get_settings_service

if TYPE_CHECKING:
    from sqlmodel.ext.asyncio.session import AsyncSession

    from agentcore.services.settings.manager import SettingsService


async def teardown_services() -> None:
    """Teardown all the services."""
    from agentcore.services.manager import service_manager

    await service_manager.teardown()


def initialize_settings_service() -> None:
    """Initialize the settings manager."""
    from agentcore.services.settings import factory as settings_factory

    get_service(ServiceType.SETTINGS_SERVICE, settings_factory.SettingsServiceFactory())


def initialize_session_service() -> None:
    """Initialize the session manager."""
    from agentcore.services.cache import factory as cache_factory
    from agentcore.services.session import factory as session_service_factory

    initialize_settings_service()

    get_service(
        ServiceType.CACHE_SERVICE,
        cache_factory.CacheServiceFactory(),
    )

    get_service(
        ServiceType.SESSION_SERVICE,
        session_service_factory.SessionServiceFactory(),
    )


async def clean_transactions(settings_service: SettingsService, session: AsyncSession) -> None:
    """Clean up old transactions from the database.

    This function deletes transactions that exceed the maximum number to keep (configured in settings).
    It orders transactions by timestamp descending and removes the oldest ones beyond the limit.

    Args:
        settings_service: The settings service containing configuration like max_transactions_to_keep
        session: The database session to use for the deletion
    """
    try:
        # Delete transactions using bulk delete
        delete_stmt = delete(TransactionTable).where(
            col(TransactionTable.id).in_(
                select(TransactionTable.id)
                .order_by(col(TransactionTable.timestamp).desc())
                .offset(settings_service.settings.max_transactions_to_keep)
            )
        )

        await session.exec(delete_stmt)
        await session.commit()
        logger.debug("Successfully cleaned up old transactions")
    except (sqlalchemy_exc.SQLAlchemyError, asyncio.TimeoutError) as exc:
        logger.error(f"Error cleaning up transactions: {exc!s}")
        await session.rollback()
        # Don't re-raise since this is a cleanup task


async def clean_vertex_builds(settings_service: SettingsService, session: AsyncSession) -> None:
    """Clean up old vertex builds from the database.

    This function deletes vertex builds that exceed the maximum number to keep (configured in settings).
    It orders vertex builds by timestamp descending and removes the oldest ones beyond the limit.

    Args:
        settings_service: The settings service containing configuration like max_vertex_builds_to_keep
        session: The database session to use for the deletion
    """
    try:
        # Delete vertex builds using bulk delete
        delete_stmt = delete(VertexBuildTable).where(
            col(VertexBuildTable.id).in_(
                select(VertexBuildTable.id)
                .order_by(col(VertexBuildTable.timestamp).desc())
                .offset(settings_service.settings.max_vertex_builds_to_keep)
            )
        )

        await session.exec(delete_stmt)
        await session.commit()
        logger.debug("Successfully cleaned up old vertex builds")
    except (sqlalchemy_exc.SQLAlchemyError, asyncio.TimeoutError) as exc:
        logger.error(f"Error cleaning up vertex builds: {exc!s}")
        await session.rollback()
        # Don't re-raise since this is a cleanup task


async def initialize_services(*, fix_migration: bool = False) -> None:
    """Initialize all the services needed."""
    settings_service = get_service(ServiceType.SETTINGS_SERVICE)
    cache_service = get_service(ServiceType.CACHE_SERVICE, default=CacheServiceFactory())
    # Test external cache connection and gracefully fall back for local/dev runs.
    if isinstance(cache_service, ExternalAsyncBaseCacheService) and not (await cache_service.is_connected()):
        strict_cache_startup = os.getenv("CACHE_STRICT_STARTUP", "true").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if strict_cache_startup:
            msg = "Cache service failed to connect to external database"
            raise ConnectionError(msg)

        from agentcore.services.cache.service import AsyncInMemoryCache
        from agentcore.services.manager import service_manager

        fallback_ttl = settings_service.settings.cache_expire or settings_service.settings.redis_cache_expire or 3600
        fallback_cache_service = AsyncInMemoryCache(expiration_time=fallback_ttl)
        fallback_cache_service.set_ready()
        service_manager.services[ServiceType.CACHE_SERVICE] = fallback_cache_service
        settings_service.settings.cache_type = "async"
        cache_service = fallback_cache_service
        logger.warning(
            "Cache service failed to connect to external database. "
            "Falling back to in-memory async cache. "
            "Set CACHE_STRICT_STARTUP=false to allow fallback."
        )

    # Initialize database
    await initialize_database(fix_migration=fix_migration)
    db_service = get_db_service()
    await db_service.initialize_alembic_log_file()
    async with db_service.with_session() as session:
        # SSO is enabled - users are managed via Azure AD, no superuser setup needed
        await clean_transactions(settings_service, session)
        await clean_vertex_builds(settings_service, session)
    try:
        permissions.permission_cache = PermissionCacheService(settings_service)
        _user_cache_service = UserCacheService(settings_service)  # Can store globally if needed
    except Exception as e:
        logger.warning(f"Failed to init auth cache: {e}")
        permissions.permission_cache = None
    logger.info("Auth cache services initialized")
