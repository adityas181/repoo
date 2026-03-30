from __future__ import annotations

import time
from typing import TYPE_CHECKING

from loguru import logger

from agentcore.services.cache.redis_client import get_redis_client

if TYPE_CHECKING:
    from redis.asyncio import StrictRedis

    from agentcore.services.settings.service import SettingsService


class RedisBuildEventStore:
    """Distributed build-event storage for cross-pod /events delivery."""

    TERMINAL_STATUSES = {"completed", "failed", "cancelled"}

    def __init__(
        self,
        redis_client: StrictRedis,
        *,
        ttl_seconds: int = 900,
        namespace: str = "build_events",
    ) -> None:
        self.redis = redis_client
        self.ttl_seconds = max(int(ttl_seconds or 900), 60)
        self.prefix = f"agentcore:{namespace}"

    def _events_key(self, job_id: str) -> str:
        return f"{self.prefix}:{job_id}:events"

    def _meta_key(self, job_id: str) -> str:
        return f"{self.prefix}:{job_id}:meta"

    def _poll_cursor_key(self, job_id: str) -> str:
        return f"{self.prefix}:{job_id}:poll_cursor"

    async def _touch(self, job_id: str) -> None:
        await self.redis.expire(self._events_key(job_id), self.ttl_seconds)
        await self.redis.expire(self._meta_key(job_id), self.ttl_seconds)
        await self.redis.expire(self._poll_cursor_key(job_id), self.ttl_seconds)

    async def init_job(self, job_id: str) -> None:
        now = str(time.time())
        await self.redis.hset(
            self._meta_key(job_id),
            mapping={
                "status": "running",
                "started_at": now,
                "updated_at": now,
            },
        )
        await self._touch(job_id)

    async def append_event(self, job_id: str, payload: str) -> None:
        await self.redis.rpush(self._events_key(job_id), payload)
        await self.redis.hset(self._meta_key(job_id), mapping={"updated_at": str(time.time())})
        await self._touch(job_id)

    async def mark_status(self, job_id: str, *, status: str, error: str | None = None) -> None:
        mapping = {
            "status": status,
            "updated_at": str(time.time()),
        }
        if error:
            mapping["error"] = error[:1000]
        await self.redis.hset(self._meta_key(job_id), mapping=mapping)
        await self._touch(job_id)

    async def get_status(self, job_id: str) -> str | None:
        status = await self.redis.hget(self._meta_key(job_id), "status")
        return str(status) if status else None

    async def get_events_from(self, job_id: str, start_index: int) -> list[str]:
        if start_index < 0:
            start_index = 0
        events = await self.redis.lrange(self._events_key(job_id), start_index, -1)
        return list(events or [])

    async def get_events_count(self, job_id: str) -> int:
        return int(await self.redis.llen(self._events_key(job_id)))

    async def claim_poll_events(self, job_id: str) -> list[str]:
        cursor_raw = await self.redis.get(self._poll_cursor_key(job_id))
        start_index = int(cursor_raw or 0)
        total = await self.get_events_count(job_id)
        if start_index >= total:
            await self._touch(job_id)
            return []

        events = await self.redis.lrange(self._events_key(job_id), start_index, total - 1)
        await self.redis.set(self._poll_cursor_key(job_id), str(total), ex=self.ttl_seconds)
        await self._touch(job_id)
        return list(events or [])

    async def job_exists(self, job_id: str) -> bool:
        return bool(await self.redis.exists(self._meta_key(job_id), self._events_key(job_id)))


_stores: dict[tuple, RedisBuildEventStore] = {}


def get_redis_job_event_store(
    settings_service: SettingsService,
    *,
    namespace: str = "build_events",
) -> RedisBuildEventStore | None:
    """Return a singleton RedisBuildEventStore for the given namespace."""

    settings = settings_service.settings
    if settings.cache_type != "redis":
        return None

    signature = (
        namespace,
        settings.redis_host,
        settings.redis_port,
        settings.redis_db,
        settings.redis_password,
        settings.redis_ssl,
        settings.redis_cache_expire,
    )

    if signature in _stores:
        return _stores[signature]

    try:
        redis_client = get_redis_client(settings_service)
        store = RedisBuildEventStore(
            redis_client,
            ttl_seconds=settings.redis_cache_expire or 900,
            namespace=namespace,
        )
        _stores[signature] = store
        return store
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Redis job-event store unavailable for namespace={namespace!r}: {exc}")
        _stores.pop(signature, None)
        return None


def get_redis_build_event_store(settings_service: SettingsService) -> RedisBuildEventStore | None:
    """Return a singleton RedisBuildEventStore for build events."""
    return get_redis_job_event_store(settings_service, namespace="build_events")
