import asyncio
import time
import uuid
from collections.abc import AsyncIterator

from fastapi import BackgroundTasks, HTTPException, Response
from loguru import logger
from sqlmodel import select


from agentcore.api.disconnect import DisconnectHandlerStreamingResponse
from agentcore.api.utils import (
    CurrentActiveUser,
    EventDeliveryType,
    build_graph_from_data,
    build_graph_from_db,
    get_top_level_vertices,
)
from agentcore.api.v1_schemas import (
    AgentDataRequest,
    InputValueRequest,
)
from agentcore.events.event_manager import EventManager
from agentcore.graph_langgraph import LangGraphAdapter
from agentcore.schema.message import ErrorMessage
from agentcore.services.database.models.agent.model import Agent
from agentcore.services.deps import get_chat_service, get_settings_service, get_telemetry_service, session_scope
from agentcore.services.job_queue.redis_build_events import RedisBuildEventStore, get_redis_build_event_store
from agentcore.services.job_queue.service import JobQueueNotFoundError, JobQueueService
from agentcore.services.telemetry.schema import PlaygroundPayload


async def _ensure_hitl_record(
    *,
    thread_id: str,
    agent_id: str,
    session_id: str,
    user_id: str,
    interrupt_data: dict,
) -> None:
    """Ensure an HITLRequest record exists for this interrupted thread.

    If _persist_hitl_request() in nodes.py already created the record,
    this is a no-op.  Otherwise it creates a new PENDING record so the
    HITL Approvals page can find it.
    """
    try:
        from agentcore.services.database.models.hitl_request.model import (
            HITLRequest,
            HITLStatus,
        )
        from agentcore.services.database.models.user_department_membership.model import UserDepartmentMembership
        from agentcore.services.database.models.user_organization_membership.model import UserOrganizationMembership
        from sqlmodel import col

        async with session_scope() as db:
            # Check if a PENDING record already exists for this thread
            existing = (
                await db.exec(
                    select(HITLRequest)
                    .where(HITLRequest.thread_id == thread_id)
                    .where(HITLRequest.status == HITLStatus.PENDING)
                    .order_by(col(HITLRequest.requested_at).desc())
                    .limit(1)
                )
            ).first()

            if existing:
                logger.debug(
                    f"[HITL] HITLRequest already exists for thread_id={thread_id!r} "
                    f"(id={existing.id})"
                )
                return

            dept_id_val = None
            org_id_val = None
            if user_id:
                try:
                    udm = (
                        await db.exec(
                            select(UserDepartmentMembership)
                            .where(
                                UserDepartmentMembership.user_id == uuid.UUID(user_id),
                                UserDepartmentMembership.status == "active",
                            )
                            .order_by(col(UserDepartmentMembership.updated_at).desc())
                            .limit(1)
                        )
                    ).first()
                    if udm:
                        dept_id_val = udm.department_id
                        org_id_val = udm.org_id
                    else:
                        uom = (
                            await db.exec(
                                select(UserOrganizationMembership.org_id)
                                .where(
                                    UserOrganizationMembership.user_id == uuid.UUID(user_id),
                                    UserOrganizationMembership.status == "active",
                                )
                                .limit(1)
                            )
                        ).first()
                        if uom:
                            org_id_val = uom if isinstance(uom, uuid.UUID) else uom[0]
                except Exception as mem_err:
                    logger.warning(f"[HITL] Could not resolve org/dept from user membership: {mem_err}")

            # Create a new PENDING record
            hitl_req = HITLRequest(
                thread_id=thread_id,
                agent_id=uuid.UUID(agent_id),
                session_id=session_id,
                user_id=uuid.UUID(user_id) if user_id else None,
                interrupt_data=interrupt_data,
                status=HITLStatus.PENDING,
                dept_id=dept_id_val,
                org_id=org_id_val,
            )
            db.add(hitl_req)
            if user_id:
                from agentcore.services.approval_notifications import upsert_approval_notification

                await upsert_approval_notification(
                    db,
                    recipient_user_id=uuid.UUID(user_id),
                    entity_type="hitl_assignment",
                    entity_id=str(hitl_req.id),
                    title="A HITL approval task is awaiting your review.",
                    link="/hitl-approvals",
                )
            await db.commit()
            logger.info(
                f"[HITL] Fallback: created HITLRequest for thread_id={thread_id!r} "
                f"(id={hitl_req.id})"
            )
    except Exception as err:
        logger.error(f"[HITL] Could not ensure HITLRequest record: {err}")


def _get_build_event_store() -> RedisBuildEventStore | None:
    try:
        settings_service = get_settings_service()
        return get_redis_build_event_store(settings_service)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"Redis build-event store unavailable: {exc}")
        return None


async def _create_redis_events_response(
    *,
    job_id: str,
    event_store: RedisBuildEventStore,
) -> DisconnectHandlerStreamingResponse:
    async def consume_and_yield() -> AsyncIterator[str]:
        cursor = 0
        while True:
            try:
                events = await event_store.get_events_from(job_id, cursor)
                for payload in events:
                    yield payload
                cursor += len(events)

                status = await event_store.get_status(job_id)
                if status in RedisBuildEventStore.TERMINAL_STATUSES:
                    total = await event_store.get_events_count(job_id)
                    if cursor >= total:
                        break
                elif not events and status is None and not await event_store.job_exists(job_id):
                    break

                await asyncio.sleep(0.05)
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"Error consuming Redis build events for job {job_id}: {exc}")
                break

    return DisconnectHandlerStreamingResponse(
        consume_and_yield(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
        on_disconnect=lambda: None,
    )


async def _get_redis_polling_response(
    *,
    job_id: str,
    event_store: RedisBuildEventStore,
) -> Response:
    events = await event_store.claim_poll_events(job_id)
    content = "\n".join(event.strip() for event in events if event is not None)
    return Response(content=content, media_type="application/x-ndjson")


async def start_agent_build(
    *,
    agent_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    inputs: InputValueRequest | None,
    data: AgentDataRequest | None,
    files: list[str] | None,
    stop_component_id: str | None,
    start_component_id: str | None,
    log_builds: bool,
    current_user: CurrentActiveUser,
    queue_service: JobQueueService,
    agent_name: str | None = None,
) -> str:
    """Start the agent build process by setting up the queue and starting the build task.

    When RabbitMQ is enabled, the job is published to a durable queue and a
    consumer (same process) picks it up. This gives rate-limiting, retry,
    and visibility via the RabbitMQ management UI.

    When RabbitMQ is disabled (default), the job runs directly via
    asyncio.create_task as before.

    Returns:
        the job_id.
    """
    job_id = str(uuid.uuid4())
    redis_event_store = _get_build_event_store()
    from agentcore.services.deps import get_rabbitmq_service
    rabbitmq_service = get_rabbitmq_service()

    try:
        # Distributed RabbitMQ path: use Redis as the cross-pod job/event registry.
        # No local in-memory queue registration is required.
        if rabbitmq_service.is_enabled() and redis_event_store is not None:
            await redis_event_store.init_job(job_id)
            job_data = {
                "job_id": job_id,
                "agent_id": str(agent_id),
                "inputs": inputs.model_dump() if inputs else None,
                "data": data.model_dump() if data else None,
                "files": files,
                "stop_component_id": stop_component_id,
                "start_component_id": start_component_id,
                "log_builds": log_builds,
                "user_id": str(current_user.id),
                "agent_name": agent_name,
            }
            await rabbitmq_service.publish_build_job(job_data)
            logger.info(f"Build job {job_id} published to RabbitMQ (Redis backplane)")
            return job_id

        # Legacy in-memory path (direct or RabbitMQ without Redis).
        _, event_manager = queue_service.create_queue(job_id)
        # Gate that lets generate_agent_events() wait until the SSE consumer
        # (GET /events) is connected before firing astream().
        event_manager._consumer_ready = asyncio.Event()

        if redis_event_store is not None:
            try:
                await redis_event_store.init_job(job_id)
                event_manager.configure_redis_mirror(redis_store=redis_event_store, job_id=job_id)
            except Exception as redis_exc:  # noqa: BLE001
                logger.warning(f"Failed to initialize Redis build-event mirror for {job_id}: {redis_exc}")

        if rabbitmq_service.is_enabled():
            job_data = {
                "job_id": job_id,
                "agent_id": str(agent_id),
                "inputs": inputs.model_dump() if inputs else None,
                "data": data.model_dump() if data else None,
                "files": files,
                "stop_component_id": stop_component_id,
                "start_component_id": start_component_id,
                "log_builds": log_builds,
                "user_id": str(current_user.id),
                "agent_name": agent_name,
            }
            # Create a placeholder task so GET /events doesn't 404 while waiting
            # for the RabbitMQ consumer to replace it with the real build task.
            placeholder_event = asyncio.Event()
            event_manager._job_ready = placeholder_event

            async def _wait_for_consumer():
                await placeholder_event.wait()

            queue_service.start_job(job_id, _wait_for_consumer())
            await rabbitmq_service.publish_build_job(job_data)
            logger.info(f"Build job {job_id} published to RabbitMQ (in-memory path)")
        else:
            task_coro = generate_agent_events(
                agent_id=agent_id,
                background_tasks=background_tasks,
                event_manager=event_manager,
                inputs=inputs,
                data=data,
                files=files,
                stop_component_id=stop_component_id,
                start_component_id=start_component_id,
                log_builds=log_builds,
                current_user=current_user,
                agent_name=agent_name,
            )
            queue_service.start_job(job_id, task_coro)
    except Exception as e:
        if redis_event_store is not None:
            try:
                await redis_event_store.mark_status(job_id, status="failed", error=str(e))
            except Exception as redis_exc:  # noqa: BLE001
                logger.debug(f"Could not mark Redis build job as failed for {job_id}: {redis_exc}")
        logger.exception("Failed to create queue and start task")
        raise HTTPException(status_code=500, detail=str(e)) from e

    return job_id


async def get_agent_events_response(
    *,
    job_id: str,
    queue_service: JobQueueService,
    event_delivery: EventDeliveryType,
):
    """Get events for a specific build job, either as a stream or single event."""
    # In RabbitMQ mode with Redis enabled, always serve build events from Redis.
    # This avoids cross-pod in-memory queue ownership races.
    from agentcore.services.deps import get_rabbitmq_service
    rabbitmq_service = get_rabbitmq_service()
    event_store = _get_build_event_store()
    if rabbitmq_service.is_enabled() and event_store is not None:
        try:
            if await event_store.job_exists(job_id):
                if event_delivery in (EventDeliveryType.STREAMING, EventDeliveryType.DIRECT):
                    return await _create_redis_events_response(job_id=job_id, event_store=event_store)
                return await _get_redis_polling_response(job_id=job_id, event_store=event_store)
        except Exception as redis_exc:  # noqa: BLE001
            logger.warning(f"Redis-first build events lookup failed for job {job_id}: {redis_exc}")

    try:
        main_queue, event_manager, event_task, _ = queue_service.get_queue_data(job_id)

        # Signal that a consumer has connected so generate_agent_events() can
        # proceed past its wait gate and start astream().
        _consumer_ready_ev: asyncio.Event | None = event_manager.__dict__.get("_consumer_ready")

        if event_delivery in (EventDeliveryType.STREAMING, EventDeliveryType.DIRECT):
            if event_task is None:
                logger.error(f"No event task found for job {job_id}")
                raise HTTPException(status_code=404, detail="No event task found for job")
            # Pass _consumer_ready to the response so it fires inside
            # consume_and_yield — when the reader is truly pulling events.
            return await create_agent_response(
                queue=main_queue,
                event_manager=event_manager,
                event_task=event_task,
                consumer_ready=_consumer_ready_ev,
            )

        # Polling mode — signal immediately since there is no streaming reader.
        if _consumer_ready_ev is not None:
            _consumer_ready_ev.set()

        # Polling mode - get all available events
        try:
            events: list = []
            # Get all available events from the queue without blocking
            while not main_queue.empty():
                _, value, _ = await main_queue.get()
                if value is None:
                    # End of stream, trigger end event
                    if event_task is not None:
                        event_task.cancel()
                    event_manager.on_end(data={})
                    # Include the end event
                    events.append(None)
                    break
                events.append(value.decode("utf-8"))

            # If no events were available, wait for one (with timeout)
            if not events:
                _, value, _ = await main_queue.get()
                if value is None:
                    # End of stream, trigger end event
                    if event_task is not None:
                        event_task.cancel()
                    event_manager.on_end(data={})
                else:
                    events.append(value.decode("utf-8"))

            # Return as NDJSON format - each line is a complete JSON object
            content = "\n".join([event for event in events if event is not None])
            return Response(content=content, media_type="application/x-ndjson")
        except asyncio.CancelledError as exc:
            logger.info(f"Event polling was cancelled for job {job_id}")
            raise HTTPException(status_code=499, detail="Event polling was cancelled") from exc
        except asyncio.TimeoutError:
            logger.warning(f"Timeout while waiting for events for job {job_id}")
            return Response(content="", media_type="application/x-ndjson")  # Return empty response instead of error

    except JobQueueNotFoundError as exc:
        # Fallback for multi-pod deployments: stream from Redis mirror if present.
        event_store = _get_build_event_store()
        if event_store is not None:
            try:
                if await event_store.job_exists(job_id):
                    if event_delivery in (EventDeliveryType.STREAMING, EventDeliveryType.DIRECT):
                        return await _create_redis_events_response(job_id=job_id, event_store=event_store)
                    return await _get_redis_polling_response(job_id=job_id, event_store=event_store)
            except Exception as redis_exc:  # noqa: BLE001
                logger.warning(f"Redis fallback failed for job {job_id}: {redis_exc}")

        logger.error(f"Job not found: {job_id}. Error: {exc!s}")
        raise HTTPException(status_code=404, detail=f"Job not found: {exc!s}") from exc
    except Exception as exc:
        if isinstance(exc, HTTPException):
            raise
        logger.exception(f"Unexpected error processing agent events for job {job_id}")
        raise HTTPException(status_code=500, detail=f"Unexpected error: {exc!s}") from exc


async def create_agent_response(
    queue: asyncio.Queue,
    event_manager: EventManager,
    event_task: asyncio.Task,
    consumer_ready: asyncio.Event | None = None,
) -> DisconnectHandlerStreamingResponse:
    """Create a streaming response for the agent build process."""

    async def consume_and_yield() -> AsyncIterator[str]:
        # Signal that the consumer is truly reading from the queue.
        if consumer_ready is not None:
            consumer_ready.set()

        while True:
            try:
                event_id, value, put_time = await queue.get()
                if value is None:
                    break
                yield value.decode("utf-8")
                # Small sleep forces the event loop to process pending I/O
                # (TCP socket writes) between chunks.  Without this, the
                # socket write buffer accumulates all events and the client
                # receives everything at once instead of progressively.
                # asyncio.sleep(0) isn't sufficient — the TCP stack needs
                # a real delay to flush the kernel buffer.
                await asyncio.sleep(0.01)
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"Error consuming event: {exc}")
                break

    def on_disconnect() -> None:
        logger.debug("Client disconnected, closing tasks")
        event_task.cancel()
        event_manager.on_end(data={})

    return DisconnectHandlerStreamingResponse(
        consume_and_yield(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
        on_disconnect=on_disconnect,
    )


async def generate_agent_events(
    *,
    agent_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    event_manager: EventManager,
    inputs: InputValueRequest | None,
    data: AgentDataRequest | None,
    files: list[str] | None,
    stop_component_id: str | None,
    start_component_id: str | None,
    log_builds: bool,
    current_user: CurrentActiveUser,
    agent_name: str | None = None,
) -> None:
    """Generate events for agent building process.

    This function handles the core agent building logic and generates appropriate events:
    - Building and validating the graph
    - Processing vertices
    - Handling errors and cleanup
    """
    import time as time_module
    run_id = f"{agent_id}_{int(time_module.time() * 1000) % 100000}"
    _run_start = time.perf_counter()
    chat_service = get_chat_service()

    telemetry_service = get_telemetry_service()
    if not inputs:
        inputs = InputValueRequest(session=str(agent_id))

    # Resolve agent_name early so metrics at lines 620/633 always have a real name
    if not agent_name:
        async with session_scope() as _name_session:
            _name_result = await _name_session.exec(select(Agent.name).where(Agent.id == agent_id))
            agent_name = _name_result.first()

    async def build_graph_and_get_order() -> tuple[list[str], list[str], LangGraphAdapter]:
        start_time = time.perf_counter()
        components_count = 0
        graph = None
        try:
            agent_id_str = str(agent_id)
            # Create a fresh session for database operations
            async with session_scope() as fresh_session:
                graph = await create_graph(fresh_session, agent_id_str, agent_name)

            first_layer = sort_vertices(graph)


            for vertex_id in first_layer:
                graph.run_manager.add_to_vertices_being_run(vertex_id)

            # Now vertices is a list of lists
            # We need to get the id of each vertex
            # and return the same structure but only with the ids
            components_count = len(graph.vertices)
            vertices_to_run = list(graph.vertices_to_run.union(get_top_level_vertices(graph, graph.vertices_to_run)))

            # Graph is already cached inside build_graph_from_db / build_graph_from_data
            await log_telemetry(start_time, components_count, success=True)

        except Exception as exc:
            await log_telemetry(start_time, components_count, success=False, error_message=str(exc))

            if "stream or streaming set to True" in str(exc):
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            logger.exception("Error checking build status")
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return first_layer, vertices_to_run, graph

    async def log_telemetry(
        start_time: float, components_count: int, *, success: bool, error_message: str | None = None
    ):
        background_tasks.add_task(
            telemetry_service.log_package_playground,
            PlaygroundPayload(
                playground_seconds=int(time.perf_counter() - start_time),
                playground_component_count=components_count,
                playground_success=success,
                playground_error_message=str(error_message) if error_message else "",
            ),
        )

    async def create_graph(fresh_session, agent_id_str: str, agent_name: str | None) -> LangGraphAdapter:
        if inputs is not None and getattr(inputs, "session", None) is not None:
            effective_session_id = inputs.session
        else:
            effective_session_id = agent_id_str

        if not data:
            return await build_graph_from_db(
                agent_id=agent_id,
                session=fresh_session,
                chat_service=chat_service,
                user_id=str(current_user.id),
                session_id=effective_session_id,
            )

        if not agent_name:
            result = await fresh_session.exec(select(Agent.name).where(Agent.id == agent_id))
            agent_name = result.first()

        # Build graph using LangGraph
        return await build_graph_from_data(
            agent_id=agent_id_str,
            payload=data.model_dump(),
            user_id=str(current_user.id),
            agent_name=agent_name,
            session_id=effective_session_id,
            chat_service=chat_service,
        )

    def sort_vertices(graph: LangGraphAdapter) -> list[str]:
        try:
            if isinstance(graph, LangGraphAdapter):
                # Call sort_vertices with stop/start component IDs for filtering
                # This enables "Run Till Specific Component" functionality
                first_layer = graph.sort_vertices(
                    stop_component_id=stop_component_id,
                    start_component_id=start_component_id,
                )
                return first_layer
            else:
                return graph.sort_vertices(stop_component_id, start_component_id)
        except Exception:  # noqa: BLE001
            logger.exception("Error sorting vertices")
            if isinstance(graph, LangGraphAdapter):
                return list(graph.vertex_map.keys())[:1]
            else:
                return graph.sort_vertices()

    # ── Wait for the SSE consumer to connect BEFORE building the graph ──
    # generate_agent_events() runs as a background task the moment POST /run
    # returns.  The frontend then makes a SECOND request: GET /events.  On the
    # first message of a new session, the graph must be built from scratch
    # (DB fetch + LangGraph compilation) which can take several seconds.
    # If we wait for the consumer AFTER the build, the build finishes and
    # astream() fires all events as a burst before the consumer is ready.
    # By waiting here, the consumer connects while the graph builds, so
    # when astream() starts the consumer is already listening.
    _consumer_ready_ev: asyncio.Event | None = event_manager.__dict__.get("_consumer_ready")
    if _consumer_ready_ev is not None:
        try:
            logger.info(f"[{run_id}] Waiting for SSE consumer to connect...")
            await asyncio.wait_for(_consumer_ready_ev.wait(), timeout=5.0)
            logger.info(f"[{run_id}] SSE consumer connected, proceeding with build")
            # Brief yield so consume_and_yield() can start iterating the queue
            # before any events fire.
            await asyncio.sleep(0.05)
        except asyncio.TimeoutError:
            logger.warning(f"[{run_id}] SSE consumer did not connect within 5 s, proceeding anyway")

    try:
        ids, vertices_to_run, graph = await build_graph_and_get_order()
    except Exception as e:
        error_message = ErrorMessage(
            agent_id=agent_id,
            exception=e,
        )
        event_manager.on_error(data=error_message.data)
        await event_manager.finalize_redis_mirror(status="failed", error=str(e))
        raise

    event_manager.on_vertices_sorted(data={"ids": ids, "to_run": vertices_to_run})

    # ── LangGraph compiled execution via astream() ──
    # All graphs (including cyclic ones) execute through the compiled graph.
    # Events (end_vertex) are emitted from inside create_node_function()
    # via the event_manager, so the frontend receives the exact same
    # NDJSON event stream. Zero frontend changes.
    if not isinstance(graph, LangGraphAdapter) or graph.compiled_app is None:
        msg = "LangGraph workflow not compiled. Check graph structure for errors."
        logger.error(msg)
        error_message = ErrorMessage(agent_id=agent_id, exception=ValueError(msg))
        event_manager.on_error(data=error_message.data)
        await event_manager.finalize_redis_mirror(status="failed", error=msg)
        raise ValueError(msg)

    logger.info("Executing graph via compiled astream")
    from agentcore.observability.metrics_registry import adjust_active_sessions, record_session_duration
    adjust_active_sessions(1)
    _session_start = time.perf_counter()
    try:
        from agentcore.schema.schema import INPUT_FIELD_NAME

        run_inputs = inputs.model_dump() if inputs else {}

        # Store event_manager on adapter so node_function can access it
        # via vertex.graph._event_manager (must NOT be in state — not serializable)
        graph._event_manager = event_manager

        # Enable streaming on all components that support it.
        # The run endpoint applies this via process_tweaks(stream=True), but the
        # Playground build path bypasses that, so we must set it here directly.
        for vertex in graph.vertices:
            if hasattr(vertex, "params"):
                vertex.params["stream"] = True
        logger.info("Applied stream=True to all vertices for Playground streaming")

        initial_state = {
            "vertices_results": {},
            "artifacts": {},
            "outputs_logs": {},
            "current_vertex": "",
            "completed_vertices": [],
            "events": [],
            "agent_id": str(agent_id),
            "agent_name": agent_name or "",
            "session_id": getattr(inputs, "session", str(agent_id)) if inputs else str(agent_id),
            "user_id": str(current_user.id),
            "input_data": run_inputs,
            "files": files,
            "fallback_to_env_vars": False,
            "stop_component_id": stop_component_id,
            "start_component_id": start_component_id,
            "predecessor_map": dict(graph.predecessor_map),
            "successor_map": dict(graph.successor_map),
            "in_degree_map": dict(graph.in_degree_map),
            "cycle_vertices": list(graph.cycle_vertices),
            "is_cyclic": graph.is_cyclic,
            "current_layer": 0,
            "vertices_layers": graph.vertices_layers if hasattr(graph, "vertices_layers") else [],
            "input_vertex_ids": list(graph._is_input_vertices),
        }

        _thread_id = initial_state.get("session_id") or str(agent_id)
        _lg_config = {"configurable": {"thread_id": _thread_id}}
        async for _state_update in graph.compiled_app.astream(initial_state, config=_lg_config):
            pass  # end_vertex events already emitted by node_function

        # After astream() returns normally: check if the graph was interrupted.
        # Only applicable when a checkpointer is attached (HITL graphs).
        # Non-HITL graphs (no checkpointer) skip this block entirely — calling
        # aget_state() without a checkpointer raises "No checkpointer set".
        try:
            if getattr(graph.compiled_app, "checkpointer", None) is not None:
                _graph_state = await graph.compiled_app.aget_state(_lg_config)
            else:
                _graph_state = None
            if _graph_state is not None and _graph_state.next:
                # Extract interrupt data from the graph state
                _interrupt_data = {}
                if _graph_state.tasks and _graph_state.tasks[0].interrupts:
                    _interrupt_data = _graph_state.tasks[0].interrupts[0].value or {}

                # Fallback: ensure HITLRequest record exists in DB.
                # _persist_hitl_request() in nodes.py may have failed silently
                # (e.g., DB session issue during LangGraph execution).  This
                # fallback guarantees the HITL Approvals page can find the record.
                await _ensure_hitl_record(
                    thread_id=_thread_id,
                    agent_id=str(agent_id),
                    session_id=_thread_id,
                    user_id=str(current_user.id),
                    interrupt_data=_interrupt_data,
                )

                from agentcore.graph_langgraph.nodes import save_hitl_checkpoint_after_interrupt
                await save_hitl_checkpoint_after_interrupt(_thread_id)
        except Exception as _chk_err:
            logger.warning(f"[HITL] Could not save checkpoint after interrupt: {_chk_err}")

    except asyncio.CancelledError:
        adjust_active_sessions(-1)
        record_session_duration((time.perf_counter() - _session_start) * 1000)
        background_tasks.add_task(graph.end_all_traces_in_context)
        await event_manager.finalize_redis_mirror(status="cancelled")
        raise
    # NOTE: GraphInterrupt is NOT caught here.
    # When interrupt() is called inside a LangGraph node, LangGraph catches the
    # GraphInterrupt internally (saves checkpoint, marks graph interrupted) and
    # astream() terminates *normally*.  All HITL work (frontend events + DB
    # persistence) is done inside the except GraphInterrupt block in nodes.py,
    # which is the only reliable execution point for interrupt handling.
    except Exception as e:
        adjust_active_sessions(-1)
        record_session_duration((time.perf_counter() - _session_start) * 1000)
        from agentcore.observability.metrics_registry import record_agent_run
        record_agent_run(agent_name or "unknown", "error", (time.perf_counter() - _run_start) * 1000)
        logger.error(f"Error in LangGraph execution: {e}")
        await graph.end_all_traces(error=e)
        error_message = ErrorMessage(
            agent_id=agent_id,
            exception=e,
            session_id=graph.session_id if hasattr(graph, "session_id") else None,
        )
        event_manager.on_error(data=error_message.data)
        await event_manager.finalize_redis_mirror(status="failed", error=str(e))
        raise

    adjust_active_sessions(-1)
    record_session_duration((time.perf_counter() - _session_start) * 1000)
    from agentcore.observability.metrics_registry import record_agent_run
    record_agent_run(agent_name or "unknown", "success", (time.perf_counter() - _run_start) * 1000)
    event_manager.on_end(data={})
    await graph.end_all_traces()
    await event_manager.queue.put((None, None, time.time()))
    await event_manager.finalize_redis_mirror(status="completed")


async def cancel_agent_build(
    *,
    job_id: str,
    queue_service: JobQueueService,
) -> bool:
    """Cancel an ongoing agent build job.

    Args:
        job_id: The unique identifier of the job to cancel
        queue_service: The service managing job queues

    Returns:
        True if the job was successfully canceled or doesn't need cancellation
        False if the cancellation failed

    Raises:
        ValueError: If the job doesn't exist
        asyncio.CancelledError: If the task cancellation failed
    """
    async def _mark_cancelled_in_redis() -> None:
        event_store = _get_build_event_store()
        if event_store is None:
            return
        try:
            await event_store.mark_status(job_id, status="cancelled")
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"Could not mark Redis build job as cancelled for {job_id}: {exc}")

    # Get the event task and event manager for the job
    _, _, event_task, _ = queue_service.get_queue_data(job_id)

    if event_task is None:
        logger.warning(f"No event task found for job_id {job_id}")
        return True  # Nothing to cancel is still a success

    if event_task.done():
        logger.info(f"Task for job_id {job_id} is already completed")
        return True  # Nothing to cancel is still a success

    # Store the task reference to check status after cleanup
    task_before_cleanup = event_task

    try:
        # Perform cleanup using the queue service
        await queue_service.cleanup_job(job_id)
    except asyncio.CancelledError:
        # Check if the task was actually cancelled
        if task_before_cleanup.cancelled():
            logger.info(f"Successfully cancelled agent build for job_id {job_id} (CancelledError caught)")
            await _mark_cancelled_in_redis()
            return True
        # If the task wasn't cancelled, re-raise the exception
        logger.error(f"CancelledError caught but task for job_id {job_id} was not cancelled")
        raise

    # If no exception was raised, verify that the task was actually cancelled
    # The task should be done (cancelled) after cleanup
    if task_before_cleanup.cancelled():
        logger.info(f"Successfully cancelled agent build for job_id {job_id}")
        await _mark_cancelled_in_redis()
        return True

    # If we get here, the task wasn't cancelled properly
    logger.error(f"Failed to cancel agent build for job_id {job_id}, task is still running")
    return False
