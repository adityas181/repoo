from __future__ import annotations

import asyncio
import inspect
import json
import time
import uuid
from functools import partial
from typing import TYPE_CHECKING

from fastapi.encoders import jsonable_encoder
from loguru import logger
from typing_extensions import Protocol

from agentcore.schema.playground_events import create_event_by_type

if TYPE_CHECKING:
    from agentcore.schema.log import LoggableType


class EventCallback(Protocol):
    def __call__(self, *, manager: EventManager, event_type: str, data: LoggableType): ...


class PartialEventCallback(Protocol):
    def __call__(self, *, data: LoggableType): ...


class EventManager:
    def __init__(self, queue: asyncio.Queue):
        self.queue = queue
        self.events: dict[str, PartialEventCallback] = {}

    @staticmethod
    def _validate_callback(callback: EventCallback) -> None:
        if not callable(callback):
            msg = "Callback must be callable"
            raise TypeError(msg)
        # Check if it has `self, event_type and data`
        sig = inspect.signature(callback)
        parameters = ["manager", "event_type", "data"]
        if len(sig.parameters) != len(parameters):
            msg = "Callback must have exactly 3 parameters"
            raise ValueError(msg)
        if not all(param.name in parameters for param in sig.parameters.values()):
            msg = "Callback must have exactly 3 parameters: manager, event_type, and data"
            raise ValueError(msg)

    def register_event(
        self,
        name: str,
        event_type: str,
        callback: EventCallback | None = None,
    ) -> None:
        if not name:
            msg = "Event name cannot be empty"
            raise ValueError(msg)
        if not name.startswith("on_"):
            msg = "Event name must start with 'on_'"
            raise ValueError(msg)
        if callback is None:
            callback_ = partial(self.send_event, event_type=event_type)
        else:
            callback_ = partial(callback, manager=self, event_type=event_type)
        self.events[name] = callback_

    async def _drain_redis_mirror_queue(self) -> None:
        mirror_queue: asyncio.Queue | None = self.__dict__.get("_redis_mirror_queue")
        redis_store = self.__dict__.get("_redis_event_store")
        job_id: str | None = self.__dict__.get("_redis_job_id")
        if mirror_queue is None or redis_store is None or not job_id:
            return

        while True:
            item = await mirror_queue.get()
            if item is None:
                break
            try:
                await redis_store.append_event(job_id, item)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Failed to mirror build event to Redis for job {job_id}: {exc}")
                # Disable Redis mirroring for this job to avoid noisy logs.
                self.__dict__.pop("_redis_event_store", None)
                self.__dict__.pop("_redis_job_id", None)
                break

    def configure_redis_mirror(self, *, redis_store, job_id: str) -> None:
        """Enable ordered Redis mirroring for emitted events."""
        if redis_store is None or not job_id:
            return

        self.__dict__["_redis_event_store"] = redis_store
        self.__dict__["_redis_job_id"] = job_id

        mirror_queue: asyncio.Queue | None = self.__dict__.get("_redis_mirror_queue")
        mirror_task: asyncio.Task | None = self.__dict__.get("_redis_mirror_task")
        if mirror_queue is None:
            mirror_queue = asyncio.Queue()
            self.__dict__["_redis_mirror_queue"] = mirror_queue
        if mirror_task is None or mirror_task.done():
            self.__dict__["_redis_mirror_task"] = asyncio.create_task(self._drain_redis_mirror_queue())

    async def finalize_redis_mirror(self, *, status: str | None = None, error: str | None = None) -> None:
        """Flush mirrored events and optionally update final job status in Redis."""
        mirror_queue: asyncio.Queue | None = self.__dict__.get("_redis_mirror_queue")
        mirror_task: asyncio.Task | None = self.__dict__.get("_redis_mirror_task")
        redis_store = self.__dict__.get("_redis_event_store")
        job_id: str | None = self.__dict__.get("_redis_job_id")

        if mirror_queue is not None and mirror_task is not None and not mirror_task.done():
            mirror_queue.put_nowait(None)
            try:
                await asyncio.wait_for(mirror_task, timeout=3)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"Error finalizing Redis mirror task for job {job_id}: {exc}")

        if redis_store is not None and job_id and status:
            try:
                await redis_store.mark_status(job_id, status=status, error=error)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"Failed to update Redis build status for job {job_id}: {exc}")

    def send_event(self, *, event_type: str, data: LoggableType):
        try:
            if isinstance(data, dict) and event_type in {"message", "error", "warning", "info", "token"}:
                data = create_event_by_type(event_type, **data)
        except TypeError as e:
            logger.debug(f"Error creating playground event: {e}")
        except Exception:
            raise
        jsonable_data = jsonable_encoder(data)
        json_data = {"event": event_type, "data": jsonable_data}
        event_id = f"{event_type}-{uuid.uuid4()}"
        str_data = json.dumps(json_data) + "\n\n"
        self.queue.put_nowait((event_id, str_data.encode("utf-8"), time.time()))

        mirror_queue: asyncio.Queue | None = self.__dict__.get("_redis_mirror_queue")
        if mirror_queue is not None:
            mirror_queue.put_nowait(str_data)

    def noop(self, *, data: LoggableType) -> None:
        pass

    def __getattr__(self, name: str) -> PartialEventCallback:
        return self.events.get(name, self.noop)


def create_default_event_manager(queue):
    manager = EventManager(queue)
    manager.register_event("on_token", "token")
    manager.register_event("on_vertices_sorted", "vertices_sorted")
    manager.register_event("on_error", "error")
    manager.register_event("on_end", "end")
    manager.register_event("on_message", "add_message")
    manager.register_event("on_remove_message", "remove_message")
    manager.register_event("on_end_vertex", "end_vertex")
    manager.register_event("on_build_start", "build_start")
    manager.register_event("on_build_end", "build_end")
    return manager


def create_stream_tokens_event_manager(queue):
    manager = EventManager(queue)
    manager.register_event("on_message", "add_message")
    manager.register_event("on_token", "token")
    manager.register_event("on_end", "end")
    return manager
