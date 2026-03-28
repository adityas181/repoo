
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from langgraph.errors import GraphInterrupt
from loguru import logger

from agentcore.graph_langgraph.state import AgentCoreState

if TYPE_CHECKING:
    from agentcore.graph_langgraph.vertex_wrapper import LangGraphVertex


def create_node_function(vertex: LangGraphVertex, *, is_cycle_router: bool = False):
    """Convert an AgentCore Vertex to a LangGraph node function.

    The returned async callable is used by ``StateGraph.add_node()`` so that
    LangGraph can execute the vertex as part of compiled graph execution
    (via ``compiled_app.ainvoke()`` / ``compiled_app.astream()``).

    The function handles the complete vertex lifecycle:
    1. Cycle router reset (re-activate successors for re-entry)
    2. Routing guard (``is_active()`` check)
    3. Frozen vertex cache restore
    4. Input parameter filtering for input vertices
    5. Dependency resolution from state
    6. Vertex build (component execution)
    7. Transaction logging to all applicable tables
    8. Frozen vertex cache save
    9. ``end_vertex`` event emission for Playground streaming

    Args:
        vertex: The vertex to wrap as a node function.
        is_cycle_router: If True, this vertex is a routing cycle vertex
            (e.g. SmartRouter, Loop) whose successors must be reset to
            ACTIVE before each re-execution so the routing function can
            route correctly after the build.
    """

    async def node_function(state: AgentCoreState) -> dict[str, Any]:
        """Execute this vertex and return only the updated state fields.

        Returns a partial dict (not the full state) so that parallel nodes
        in the same LangGraph superstep don't conflict on channels they
        didn't modify.  Reducer-annotated channels (``_merge_dicts``,
        ``add``) merge the partial updates automatically.
        """
        graph = vertex.graph  # LangGraphAdapter instance

        # ------------------------------------------------------------------
        # 0. CYCLE ROUTER RESET — for routing cycle vertices, reset successor
        #    states to ACTIVE before each execution.  This ensures:
        #    a) The component's build() sees fresh state on re-entry
        #    b) After build(), the routing function can check is_active()
        #       to determine where to route (the component's stop()/start()
        #       calls during build will mark the correct branches).
        # ------------------------------------------------------------------
        if is_cycle_router:
            for sid in graph.successor_map.get(vertex.id, []):
                successor = graph.get_vertex(sid)
                if successor is not None:
                    successor.set_state("ACTIVE")
            # Reset own built state so the component re-executes on cycle re-entry
            vertex.built = False
            vertex.built_object = None
            vertex.built_result = None

        # ------------------------------------------------------------------
        # 1. ROUTING GUARD — skip vertices marked INACTIVE by upstream routers.
        #    Works because LangGraph executes in topological order: routers
        #    run before their downstream vertices, so by the time we reach
        #    here the vertex state has already been set by mark_branch().
        # ------------------------------------------------------------------
        if not vertex.is_active():
            logger.debug(f"Skipping INACTIVE vertex: {vertex.id} ({vertex.display_name})")
            # Return empty update — nothing changed
            return {}

        # ------------------------------------------------------------------
        # 1b. PREDECESSOR BARRIER — LangGraph schedules a node as soon as
        #     ANY incoming edge fires.  For fan-in nodes (multiple predecessors
        #     at different depths) this means the node can run before all
        #     upstream results are available.  Return {} to skip this
        #     premature invocation; LangGraph will invoke the node again
        #     once the remaining predecessors complete and fire their edges.
        #
        #     SKIP for cycle vertices — back-edge predecessors haven't run
        #     yet on the first iteration, so the barrier would deadlock the
        #     cycle.  Cycle execution order is managed by the routing
        #     function (add_conditional_edges) instead.
        # ------------------------------------------------------------------
        cycle_verts = state.get("cycle_vertices", [])
        if vertex.id not in cycle_verts:
            predecessors = state.get("predecessor_map", {}).get(vertex.id, [])
            if predecessors:
                vertices_results = state.get("vertices_results", {})
                missing = [p for p in predecessors if p not in vertices_results]
                if missing:
                    logger.debug(
                        f"Vertex {vertex.id} ({vertex.display_name}) waiting for "
                        f"predecessors: {missing} — skipping this invocation"
                    )
                    return {}

        logger.debug(f"Executing node for vertex: {vertex.id} ({vertex.display_name})")
        start_time = time.time()

        # ------------------------------------------------------------------
        # 2. FROZEN VERTEX CACHE CHECK — restore from cache if available.
        # ------------------------------------------------------------------
        should_build = True
        if vertex.frozen:
            try:
                from agentcore.services.cache.utils import CacheMiss
                from agentcore.services.chat.service import ChatService
                from agentcore.services.deps import get_chat_service

                chat_service = get_chat_service()
                cached_result = await chat_service.get_cache(key=vertex.id)

                if not isinstance(cached_result, CacheMiss):
                    cached_vertex_dict = cached_result["result"]
                    vertex.built = cached_vertex_dict["built"]
                    vertex.artifacts = cached_vertex_dict["artifacts"]
                    vertex.built_object = cached_vertex_dict["built_object"]
                    vertex.built_result = cached_vertex_dict["built_result"]
                    vertex.results = cached_vertex_dict.get("results", {})

                    try:
                        vertex.finalize_build()
                        if vertex.result is not None:
                            vertex.result.used_frozen_result = True
                    except Exception:
                        logger.opt(exception=True).debug("Error finalizing cached build")
                        should_build = True
                    else:
                        should_build = False
            except Exception:
                logger.opt(exception=True).debug(f"Error checking frozen cache for {vertex.id}")
                should_build = True

        # ------------------------------------------------------------------
        # 3. INPUT VERTEX PARAMETER FILTERING
        # ------------------------------------------------------------------
        inputs_dict = state.get("input_data", {})
        input_vertex_ids = state.get("input_vertex_ids", [])
        if vertex.id in input_vertex_ids and inputs_dict:
            from agentcore.schema.schema import INPUT_FIELD_NAME

            input_components = inputs_dict.get("components", [])
            should_update = True
            if input_components:
                if vertex.id not in input_components and vertex.display_name not in input_components:
                    should_update = False

            if should_update and INPUT_FIELD_NAME in inputs_dict:
                new_val = inputs_dict[INPUT_FIELD_NAME]
                # Only overwrite if the new value is non-empty.
                # This preserves TextInput's configured value when the
                # Playground sends an empty chat message.
                if new_val:
                    vertex.update_raw_params({INPUT_FIELD_NAME: new_val}, overwrite=True)

            # Pass uploaded files to input vertices (e.g. ChatInput) so that
            # images/documents are included in the Message sent to the LLM.
            files_from_state = state.get("files")
            if should_update and files_from_state:
                vertex.update_raw_params({"files": files_from_state}, overwrite=True)

        try:
            if should_build:
                # ----------------------------------------------------------
                # 4. DEPENDENCY RESOLUTION from state
                # ----------------------------------------------------------
                resolved_params = _resolve_vertex_dependencies(vertex, state)
                if resolved_params:
                    vertex.update_raw_params(resolved_params, overwrite=True)

                # ----------------------------------------------------------
                # 5. BUILD THE VERTEX (execute the component)
                # ----------------------------------------------------------
                # Store current LangGraph state so SupervisorAgent can
                # resolve worker vertex dependencies during its internal loop.
                vertex.graph._current_lg_state = state

                await vertex.build(
                    user_id=state.get("user_id"),
                    inputs=inputs_dict,
                    files=state.get("files"),
                    event_manager=getattr(vertex.graph, "_event_manager", None),
                    fallback_to_env_vars=state.get("fallback_to_env_vars", False),
                )

            elapsed_time = time.time() - start_time

            # Build the success event
            vertex_event = {
                "vertex_id": vertex.id,
                "display_name": vertex.display_name,
                "result": vertex.result,
                "timestamp": time.time(),
                "elapsed_time": elapsed_time,
                "status": "success",
            }

            logger.debug(f"Vertex {vertex.id} completed in {elapsed_time:.2f}s")

            # ----------------------------------------------------------
            # 6. TRANSACTION LOGGING — all 4 tables + vertex build record
            # ----------------------------------------------------------
            from agentcore.graph_langgraph.transaction_logging import (
                log_all_transactions,
                log_vertex_build_record,
            )

            await log_all_transactions(vertex=vertex, graph=graph, status="success")

            data_dict = {}
            if vertex.built_result is not None:
                data_dict = {"result": str(vertex.built_result)}
            await log_vertex_build_record(vertex=vertex, graph=graph, valid=True, data_dict=data_dict)

            # ----------------------------------------------------------
            # 7. FROZEN VERTEX CACHE SAVE
            # ----------------------------------------------------------
            if vertex.frozen and should_build:
                try:
                    from agentcore.services.deps import get_chat_service

                    chat_service = get_chat_service()
                    vertex_dict = {
                        "built": vertex.built,
                        "results": vertex.results,
                        "artifacts": vertex.artifacts,
                        "built_object": vertex.built_object,
                        "built_result": vertex.built_result,
                    }
                    await chat_service.set_cache(key=vertex.id, value={"result": vertex_dict})
                except Exception:
                    logger.opt(exception=True).debug(f"Error saving frozen cache for {vertex.id}")

            # ----------------------------------------------------------
            # 8. EMIT end_vertex EVENT for Playground streaming
            # ----------------------------------------------------------
            event_manager = getattr(vertex.graph, "_event_manager", None)
            if event_manager is not None:
                _emit_end_vertex_event(
                    vertex=vertex,
                    graph=graph,
                    event_manager=event_manager,
                    elapsed_time=elapsed_time,
                )

            # ----------------------------------------------------------
            # 9. RETURN ONLY UPDATED FIELDS (partial state update)
            #    Reducers handle merging: _merge_dicts for dicts, add for lists.
            #    Static fields (agent_id, session_id, etc.) are NOT returned
            #    so parallel nodes don't conflict on them.
            # ----------------------------------------------------------
            updates: dict[str, Any] = {
                "vertices_results": {vertex.id: vertex.built_result},
                "artifacts": {vertex.id: vertex.artifacts},
                "current_vertex": vertex.id,
                "completed_vertices": [vertex.id],
                "events": [vertex_event],
            }
            if vertex.outputs_logs:
                updates["outputs_logs"] = {vertex.id: vertex.outputs_logs}

            logger.debug(
                f"[node_function] vertex={vertex.id} ({vertex.display_name}), "
                f"built_result type={type(vertex.built_result).__name__}, "
                f"built_result is None={vertex.built_result is None}, "
                f"built_object type={type(vertex.built_object).__name__}"
            )
            return updates

        except GraphInterrupt as gi:
            # Expected HITL pause — LangGraph handles interrupt() internally:
            # astream() terminates NORMALLY (no exception propagates to build.py).
            # So THIS is the only place where we can do HITL work:
            #   1. Emit frontend events (pause message + end_vertex)
            #   2. Persist HITLRequest to DB
            elapsed_time = time.time() - start_time

            # Extract interrupt payload from gi.args[0] (tuple of Interrupt objects)
            _interrupts = gi.args[0] if gi.args else ()
            _first = _interrupts[0] if _interrupts else None
            interrupt_value = getattr(_first, "value", {}) if _first is not None else {}

            # 1. Frontend events
            event_manager = getattr(vertex.graph, "_event_manager", None)
            if event_manager is not None:
                _emit_hitl_pause_event(
                    vertex=vertex,
                    graph=graph,
                    event_manager=event_manager,
                    elapsed_time=elapsed_time,
                    interrupt_value=interrupt_value,
                )

            # 2. Persist HITLRequest to DB
            # NOTE: build.py's except GraphInterrupt block is NEVER reached because
            # LangGraph catches the re-raised exception internally and astream()
            # returns normally.  This is the only reliable insertion point.
            await _persist_hitl_request(
                graph=graph,
                state=state,
                interrupt_value=interrupt_value,
            )

            raise

        except Exception as e:
            logger.exception(f"Error building vertex {vertex.id}: {e}")

            elapsed_time = time.time() - start_time
            error_event = {
                "vertex_id": vertex.id,
                "display_name": vertex.display_name,
                "timestamp": time.time(),
                "elapsed_time": elapsed_time,
                "status": "error",
                "error": str(e),
            }

            # Error transaction logging
            from agentcore.graph_langgraph.transaction_logging import (
                log_all_transactions,
                log_vertex_build_record,
            )

            await log_all_transactions(vertex=vertex, graph=graph, status="error", error=str(e))
            await log_vertex_build_record(
                vertex=vertex, graph=graph, valid=False, data_dict={"error": str(e)}
            )

            raise

    # Set function name for debugging
    node_function.__name__ = f"node_{vertex.id}"
    return node_function


def _emit_end_vertex_event(
    *,
    vertex: LangGraphVertex,
    graph: Any,
    event_manager: Any,
    elapsed_time: float,
) -> None:
    """Emit an ``end_vertex`` event in the exact format the Playground frontend expects.

    This emits end_vertex events in the exact format the Playground frontend
    expects, so that the frontend receives identical NDJSON events via the
    LangGraph compiled execution path.
    """
    import json

    from agentcore.api.schemas import ResultDataResponse, VertexBuildResponse
    from agentcore.api.utils import format_elapsed_time

    try:
        # Build ResultDataResponse from vertex result
        if vertex.result is not None:
            result_data_response = ResultDataResponse.model_validate(vertex.result, from_attributes=True)
        else:
            result_data_response = ResultDataResponse()

        result_data_response.message = vertex.artifacts
        result_data_response.duration = format_elapsed_time(elapsed_time)
        result_data_response.timedelta = elapsed_time

        # Compute next_vertices_ids — informational for frontend UI animations.
        # LangGraph controls actual execution order.
        next_vertices_ids = [
            sid
            for sid in graph.successor_map.get(vertex.id, [])
            if graph.get_vertex(sid) and graph.get_vertex(sid).is_active()
        ]
        inactivated_vertices = list(graph.inactivated_vertices)
        top_level_vertices = graph.get_top_level_vertices(next_vertices_ids)

        # Handle stop_vertex filtering
        if graph.stop_vertex and graph.stop_vertex in next_vertices_ids:
            next_vertices_ids = [graph.stop_vertex]

        build_response = VertexBuildResponse(
            inactivated_vertices=list(set(inactivated_vertices)),
            next_vertices_ids=list(set(next_vertices_ids)),
            top_level_vertices=list(set(top_level_vertices)),
            valid=vertex.built,
            params=str(vertex.built_object_repr()),
            id=vertex.id,
            data=result_data_response,
        )

        build_data = json.loads(build_response.model_dump_json())
        event_manager.on_end_vertex(data={"build_data": build_data})

        # Reset per-vertex tracking (same as Playground's _build_vertex)
        graph.reset_inactivated_vertices()
        graph.reset_activated_vertices()
    except Exception:
        logger.opt(exception=True).warning(f"Error emitting end_vertex event for {vertex.id}")


def _emit_hitl_pause_event(
    *,
    vertex: Any,
    graph: Any,
    event_manager: Any,
    elapsed_time: float,
    interrupt_value: dict,
) -> None:
    """Emit events that signal a HITL pause to the frontend.

    Emits two events:
    1. ``end_vertex`` — marks the HumanApproval node as "completed" (green checkmark
       in the flow canvas) with the review question shown as the vertex output.
    2. ``add_message`` with ``sender="Machine"`` — adds the HITL question to the
       chat history AND clears the "agent running..." spinner (the frontend only
       clears the spinner when a Machine message or error arrives).
    """
    import json
    from uuid import uuid4

    from agentcore.api.schemas import ResultDataResponse, VertexBuildResponse
    from agentcore.api.utils import format_elapsed_time

    try:
        # Build a human-readable summary of the pending review request.
        question = (
            interrupt_value.get("question", "Awaiting human review")
            if isinstance(interrupt_value, dict)
            else str(interrupt_value)
        )
        actions = (
            interrupt_value.get("actions", [])
            if isinstance(interrupt_value, dict)
            else []
        )
        actions_str = ", ".join(actions) if actions else "—"

        # ── 1. end_vertex event ───────────────────────────────────────────────
        result_data = ResultDataResponse(
            results={"text": f"⏸ Waiting for human review\n\n{question}\n\nActions: {actions_str}"},
            message=None,
            duration=format_elapsed_time(elapsed_time),
            timedelta=elapsed_time,
        )

        build_response = VertexBuildResponse(
            inactivated_vertices=[],
            next_vertices_ids=[],
            top_level_vertices=[],
            valid=True,
            params=f"HITL pause — actions: {actions_str}",
            id=vertex.id,
            data=result_data,
        )

        build_data = json.loads(build_response.model_dump_json())
        event_manager.on_end_vertex(data={"build_data": build_data})

        graph.reset_inactivated_vertices()
        graph.reset_activated_vertices()

        # ── 2. add_message event (Machine) — clears "agent running..." spinner ──
        # The frontend's messagesStore calls setDisplayLoadingMessage(false) only
        # when it receives a message with sender="Machine" or category="error".
        # Without this, the spinner stays permanently after a HITL pause.
        #
        # IMPORTANT: timestamp must be set to a current UTC time string in the
        # format "YYYY-MM-DD HH:MM:SS UTC".  Without it parseTimestampAsUTC()
        # returns 0 (1970 epoch), which sorts the message before the User's
        # message and hides it above the StickToBottom scroll position.
        from datetime import datetime, timezone as _tz

        session_id = getattr(graph, "_session_id", None) or getattr(graph, "session_id", None)
        agent_id = str(graph.agent_id) if getattr(graph, "agent_id", None) else None
        now_utc = datetime.now(_tz.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        actions_display = "\n".join(f"• {a}" for a in actions) if actions else "—"
        message_text = (
            f"⏸ **Waiting for human review**\n\n"
            f"{question}\n\n"
            f"**Available actions:**\n{actions_display}"
        )

        msg_id = str(uuid4())

        # Flag deployed runs so the frontend can show "pending admin approval"
        # instead of inline action buttons (only dept admin can approve).
        _is_deployed = bool(
            getattr(graph, "orch_deployment_id", None)
            or getattr(graph, "prod_deployment_id", None)
            or getattr(graph, "uat_deployment_id", None)
        )

        hitl_properties = {
            "hitl": True,
            "thread_id": session_id,
            "actions": actions,
            "is_deployed_run": _is_deployed,
        }

        event_manager.on_message(data={
            "sender": "Machine",
            "sender_name": "Agent",
            "text": message_text,
            "category": "message",
            "session_id": session_id,
            "agent_id": agent_id,
            "id": msg_id,
            "timestamp": now_utc,
            "files": [],
            "edit": False,
            "background_color": "",
            "text_color": "",
            # HITL metadata — chat-message.tsx reads these to render action buttons.
            "properties": hitl_properties,
        })

        # Persist the HITL pause message to the conversation table so the
        # buttons survive page refreshes / message refetches.
        # Skip for orchestrator runs — orchestrator.py already persists to
        # orch_conversation with the correct deployment_id / user_id.
        is_orch = bool(getattr(graph, "orch_deployment_id", None))

        if not is_orch:
            import asyncio

            async def _persist_hitl_message():
                try:
                    from agentcore.schema.message import Message as SchemaMessage
                    from agentcore.schema.properties import Properties

                    hitl_msg = await SchemaMessage.create(
                        text=message_text,
                        sender="Machine",
                        sender_name="Agent",
                        session_id=session_id,
                        agent_id=agent_id,
                        files=[],
                        properties=Properties(**hitl_properties),
                    )
                    hitl_msg.data["id"] = msg_id
                    from agentcore.memory import astore_message
                    await astore_message(hitl_msg, agent_id=agent_id)
                except Exception as store_err:
                    logger.warning(f"[HITL] Could not persist pause message to DB: {store_err}")

            try:
                loop = asyncio.get_running_loop()
                loop.create_task(_persist_hitl_message())
            except RuntimeError:
                logger.warning("[HITL] No running event loop — skipping DB persistence of pause message")
        else:
            logger.info("[HITL] Orchestrator run — skipping conversation table persistence (orch handles its own)")

        logger.info(
            f"[HITL] Emitted pause events for {vertex.id} ({vertex.display_name}). "
            f"Question: {question!r}"
        )
    except Exception:
        logger.opt(exception=True).warning(
            f"[HITL] Error emitting pause events for {vertex.id}"
        )


async def _persist_hitl_request(
    *,
    graph: Any,
    state: Any,
    interrupt_value: dict,
) -> None:
    """Persist a HITLRequest row to the database.

    Called from the ``except GraphInterrupt`` block in ``node_function``.
    build.py's ``except GraphInterrupt`` is never reached because LangGraph
    handles the re-raised exception internally (astream terminates normally).

    NOTE: checkpoint_data is NOT set here because the LangGraph checkpoint has
    not been saved to MemorySaver yet at this point — Pregel saves the checkpoint
    AFTER catching the re-raised GraphInterrupt (i.e., after this handler runs).
    The checkpoint is serialized in build.py's post-astream hook via
    ``save_hitl_checkpoint_after_interrupt()``.
    """
    import uuid

    try:
        from agentcore.services.database.models.hitl_request.model import (
            HITLRequest,
            HITLStatus,
        )
        from agentcore.services.deps import session_scope as _session_scope
        from agentcore.services.database.models.user_department_membership.model import UserDepartmentMembership
        from agentcore.services.database.models.user_organization_membership.model import UserOrganizationMembership
        from sqlmodel import col, select

        thread_id = getattr(graph, "_session_id", None) or ""
        agent_id_raw = getattr(graph, "agent_id", None)
        user_id_raw = state.get("user_id") if state else None
        if not user_id_raw:
            user_id_raw = getattr(graph, "orch_user_id", None)

        # Tag orchestrator runs so _store_hitl_confirmation writes to orch_conversation
        orch_deployment_id = getattr(graph, "orch_deployment_id", None)
        if orch_deployment_id:
            interrupt_value["_orch_meta"] = {
                "deployment_id": str(orch_deployment_id),
                "user_id": str(user_id_raw) if user_id_raw else None,
                "session_id": getattr(graph, "orch_session_id", None),
            }
        deploy_meta: dict[str, str] = {}
        prod_deployment_id = getattr(graph, "prod_deployment_id", None)
        uat_deployment_id = getattr(graph, "uat_deployment_id", None)
        if prod_deployment_id:
            deploy_meta["env"] = "2"
            deploy_meta["deployment_id"] = str(prod_deployment_id)
            prod_ver = getattr(graph, "prod_version_number", None)
            if prod_ver is not None:
                deploy_meta["version"] = f"v{prod_ver}"
        elif uat_deployment_id:
            deploy_meta["env"] = "1"
            deploy_meta["deployment_id"] = str(uat_deployment_id)
            uat_ver = getattr(graph, "uat_version_number", None)
            if uat_ver is not None:
                deploy_meta["version"] = f"v{uat_ver}"
        elif not (
            orch_deployment_id
            or getattr(graph, "prod_deployment_id", None)
            or getattr(graph, "uat_deployment_id", None)
        ):
            deploy_meta["env"] = "0"
            deploy_meta["version"] = "v1"
        if deploy_meta:
            interrupt_value["_deploy_meta"] = deploy_meta

        # ── Determine if this is a published/deployed run ──
        # Any of the three deployment context fields being set means the agent
        # was invoked from a published deployment (orch, direct run, webhook,
        # trigger, etc.) rather than the playground.
        is_deployed = bool(
            orch_deployment_id
            or getattr(graph, "prod_deployment_id", None)
            or getattr(graph, "uat_deployment_id", None)
        )

        assigned_to: uuid.UUID | None = None
        dept_id_val: uuid.UUID | None = None
        org_id_val: uuid.UUID | None = None

        if is_deployed:
            # Extract dept_id / org_id from the deployment context already
            # available on the graph — no extra DB query needed for these.
            raw_dept = (
                getattr(graph, "orch_dept_id", None)
                or getattr(graph, "prod_dept_id", None)
                or getattr(graph, "uat_dept_id", None)
            )
            raw_org = (
                getattr(graph, "orch_org_id", None)
                or getattr(graph, "prod_org_id", None)
                or getattr(graph, "uat_org_id", None)
            )
            logger.info(
                f"[HITL] Deployment context — "
                f"orch_dept_id={getattr(graph, 'orch_dept_id', None)}, "
                f"prod_dept_id={getattr(graph, 'prod_dept_id', None)}, "
                f"uat_dept_id={getattr(graph, 'uat_dept_id', None)}, "
                f"raw_dept={raw_dept}, raw_org={raw_org}"
            )
            dept_id_val = uuid.UUID(str(raw_dept)) if raw_dept else None
            org_id_val = uuid.UUID(str(raw_org)) if raw_org else None

            # Fallback: if dept_id not on graph, look it up from the agent record
            if not dept_id_val and agent_id_raw:
                try:
                    from sqlmodel import select as _sel
                    from agentcore.services.database.models.agent.model import Agent

                    async with _session_scope() as _agent_db:
                        _agent_row = (
                            await _agent_db.exec(
                                _sel(Agent.dept_id).where(
                                    Agent.id == uuid.UUID(str(agent_id_raw))
                                )
                            )
                        ).first()
                        if _agent_row:
                            dept_id_val = _agent_row
                            logger.info(f"[HITL] Resolved dept_id={dept_id_val} from agent record (fallback)")
                except Exception as _agent_err:
                    logger.warning(f"[HITL] Could not resolve dept_id from agent: {_agent_err}")

            # Resolve the department admin to route the HIL request to them.
            if dept_id_val:
                try:
                    from agentcore.services.database.models.department.model import Department

                    async with _session_scope() as _dept_db:
                        dept_row = (
                            await _dept_db.exec(
                                select(Department).where(Department.id == dept_id_val)
                            )
                        ).first()
                        if dept_row and dept_row.admin_user_id:
                            assigned_to = dept_row.admin_user_id
                            logger.info(
                                f"[HITL] Routed deployed-run HIL to dept admin "
                                f"{assigned_to} (dept={dept_id_val})"
                            )
                except Exception as _dept_err:
                    logger.warning(f"[HITL] Could not resolve dept admin: {_dept_err}")
        else:
            # Playground / non-deployed runs: stamp org/dept from user membership
            if user_id_raw:
                try:
                    async with _session_scope() as _mem_db:
                        udm = (
                            await _mem_db.exec(
                                select(UserDepartmentMembership)
                                .where(
                                    UserDepartmentMembership.user_id == uuid.UUID(str(user_id_raw)),
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
                                await _mem_db.exec(
                                    select(UserOrganizationMembership.org_id)
                                    .where(
                                        UserOrganizationMembership.user_id == uuid.UUID(str(user_id_raw)),
                                        UserOrganizationMembership.status == "active",
                                    )
                                    .limit(1)
                                )
                            ).first()
                            if uom:
                                org_id_val = uom if isinstance(uom, uuid.UUID) else uom[0]
                except Exception as _mem_err:
                    logger.warning(f"[HITL] Could not resolve org/dept from user membership: {_mem_err}")

        async with _session_scope() as _db:
            _hitl = HITLRequest(
                thread_id=thread_id,
                agent_id=uuid.UUID(str(agent_id_raw)) if agent_id_raw else uuid.uuid4(),
                session_id=thread_id,
                user_id=uuid.UUID(str(user_id_raw)) if user_id_raw else None,
                interrupt_data=interrupt_value,
                status=HITLStatus.PENDING,
                checkpoint_data=None,  # filled in by save_hitl_checkpoint_after_interrupt()
                assigned_to=assigned_to,
                dept_id=dept_id_val,
                org_id=org_id_val,
                is_deployed_run=is_deployed,
            )
            _db.add(_hitl)
            if assigned_to:
                from agentcore.services.approval_notifications import upsert_approval_notification

                await upsert_approval_notification(
                    _db,
                    recipient_user_id=assigned_to,
                    entity_type="hitl_assignment",
                    entity_id=str(_hitl.id),
                    title="A HITL approval task is awaiting your review.",
                    link="/hitl-approvals",
                )
            await _db.commit()
            logger.info(f"[HITL] Persisted HITLRequest for thread_id={thread_id!r} (deployed={is_deployed})")
    except Exception as _err:
        logger.warning(f"[HITL] Could not persist HITLRequest: {_err}")


async def save_hitl_checkpoint_after_interrupt(thread_id: str) -> None:
    """Serialize the MemorySaver checkpoint and store it in the HITLRequest DB row.

    Must be called AFTER ``compiled_app.astream()`` / ``ainvoke()`` returns when
    the graph was interrupted.  At that point Pregel has already saved the
    checkpoint to MemorySaver, so ``storage`` and ``blobs`` are populated.

    Updates the most-recent PENDING HITLRequest row for ``thread_id`` with the
    serialized checkpoint so it survives server restarts.
    """
    import base64
    import pickle

    try:
        from agentcore.graph_langgraph.checkpointer import get_checkpointer
        from agentcore.services.database.models.hitl_request.model import (
            HITLRequest,
            HITLStatus,
        )
        from agentcore.services.deps import session_scope as _session_scope
        from sqlmodel import col, select

        checkpointer = get_checkpointer()
        # storage: defaultdict(thread_id -> defaultdict(checkpoint_ns -> dict[...]))
        # blobs:   defaultdict((thread_id, ns, channel, version) -> (type, bytes))
        storage_for_thread = dict(checkpointer.storage.get(thread_id, {}))
        blobs_for_thread = {
            key: value
            for key, value in checkpointer.blobs.items()
            if key[0] == thread_id
        }
        if not storage_for_thread and not blobs_for_thread:
            logger.warning(
                f"[HITL] No checkpoint in MemorySaver for thread_id={thread_id!r} "
                "— checkpoint_data will remain NULL"
            )
            return

        payload = {"storage": storage_for_thread, "blobs": blobs_for_thread}
        checkpoint_b64 = base64.b64encode(pickle.dumps(payload)).decode()

        # Update the most recent PENDING HITLRequest for this thread_id
        async with _session_scope() as _db:
            stmt = (
                select(HITLRequest)
                .where(HITLRequest.thread_id == thread_id)
                .where(HITLRequest.status == HITLStatus.PENDING)
                .where(HITLRequest.checkpoint_data.is_(None))
                .order_by(col(HITLRequest.requested_at).desc())
                .limit(1)
            )
            result = await _db.exec(stmt)
            hitl_row = result.first()
            if hitl_row:
                hitl_row.checkpoint_data = checkpoint_b64
                _db.add(hitl_row)
                await _db.commit()
                logger.info(
                    f"[HITL] Checkpoint saved for thread_id={thread_id!r} "
                    f"({len(checkpoint_b64)} chars)"
                )
            else:
                logger.warning(
                    f"[HITL] No PENDING HITLRequest found for thread_id={thread_id!r} "
                    "to attach checkpoint"
                )
    except Exception as _err:
        logger.warning(f"[HITL] Could not save checkpoint data: {_err}")


def create_routing_function(
    *,
    vertex_id: str,
    successor_ids: list[str],
    graph_adapter: Any,
):
    """Create a routing function for ``add_conditional_edges()``.

    The returned callable runs **after** the cycle router node's
    ``node_function`` has completed.  It inspects the ``is_active()``
    state of each successor (which was set by the component's
    ``stop()`` / ``start()`` calls during build) and returns the ID
    of the first active successor, or ``END`` if none is active
    (which terminates the cycle).

    Args:
        vertex_id: ID of the routing cycle vertex.
        successor_ids: Ordered list of successor vertex IDs.
        graph_adapter: The ``LangGraphAdapter`` instance (for vertex lookup).

    Returns:
        A callable ``route(state) -> str`` suitable for
        ``StateGraph.add_conditional_edges()``.
    """
    from langgraph.graph import END

    def route(state: AgentCoreState) -> str:
        for sid in successor_ids:
            v = graph_adapter.get_vertex(sid)
            if v is not None and v.is_active():
                logger.debug(f"Routing from {vertex_id} -> {sid}")
                return sid
        logger.debug(f"Routing from {vertex_id} -> END (no active successors)")
        return END

    route.__name__ = f"route_{vertex_id}"
    return route


def _get_source_output_name(vertex: LangGraphVertex, source_vertex_id: str, target_param: str) -> str | None:
    """Return the sourceHandle name for the edge connecting source_vertex_id → vertex[target_param].

    Multi-output nodes (SmartRouter, HumanApproval, …) store their results as a
    dict keyed by output name.  When resolving parameters from state we need to
    know *which* output was connected to this parameter so we can extract the
    right value instead of passing the whole dict downstream.
    """
    graph = getattr(vertex, "graph", None)
    if graph is None:
        return None
    for edge in getattr(graph, "edges", []):
        if edge.get("source") != source_vertex_id:
            continue
        if edge.get("target") != vertex.id:
            continue
        # Check that this edge targets the right parameter
        target_handle = edge.get("data", {}).get("targetHandle", {})
        field_name = target_handle.get("fieldName") if isinstance(target_handle, dict) else None
        if field_name and field_name != target_param:
            continue
        source_handle = edge.get("data", {}).get("sourceHandle", {})
        return source_handle.get("name") if isinstance(source_handle, dict) else None
    return None


def _extract_from_result(result: Any, source_output: str | None) -> Any:
    """Extract a specific output value from a multi-output built_result dict.

    When a node produces multiple outputs (e.g. HumanApproval with Approve/Reject),
    its vertices_results entry is a dict like ``{"Approve": Message(...), "Reject": Message(...)}``.
    This helper picks the right value using the connected output name.
    """
    if not isinstance(result, dict):
        return result
    if source_output and source_output in result:
        return result[source_output]
    # Single-value dict — unwrap automatically
    if len(result) == 1:
        return next(iter(result.values()))
    # Multiple values but no matching key — return the whole dict (caller must handle)
    return result


def _resolve_vertex_dependencies(vertex: LangGraphVertex, state: AgentCoreState) -> dict[str, Any]:
    """Resolve vertex parameter dependencies from state.

    Vertices can have parameters that reference other vertices by ID. This function
    resolves those references by looking up the results in the state.

    When the upstream vertex produced multiple outputs (built_result is a dict),
    we use the edge's sourceHandle.name to extract only the connected output value.
    """
    resolved_params = {}

    for key, value in vertex.raw_params.items():
        # Case 1: Value is a vertex ID (string matching pattern)
        if isinstance(value, str) and value in state["vertices_results"]:
            result = state["vertices_results"][value]
            if isinstance(result, dict):
                source_output = _get_source_output_name(vertex, value, key)
                result = _extract_from_result(result, source_output)
            resolved_params[key] = result

        # Case 2: Value is a list that might contain vertex IDs
        elif isinstance(value, list):
            resolved_list = []
            for item in value:
                if isinstance(item, str) and item in state["vertices_results"]:
                    item_result = state["vertices_results"][item]
                    if isinstance(item_result, dict):
                        source_output = _get_source_output_name(vertex, item, key)
                        item_result = _extract_from_result(item_result, source_output)
                    if isinstance(item_result, list):
                        resolved_list.extend(item_result)
                    else:
                        resolved_list.append(item_result)
                else:
                    resolved_list.append(item)
            if resolved_list != value:
                resolved_params[key] = resolved_list

        # Case 3: Value is a dict with vertex ID values
        elif isinstance(value, dict):
            resolved_dict = {}
            has_vertices = False
            for sub_key, sub_value in value.items():
                if isinstance(sub_value, str) and sub_value in state["vertices_results"]:
                    has_vertices = True
                    item_result = state["vertices_results"][sub_value]
                    if isinstance(item_result, dict):
                        source_output = _get_source_output_name(vertex, sub_value, key)
                        item_result = _extract_from_result(item_result, source_output)
                    resolved_dict[sub_key] = item_result
                else:
                    resolved_dict[sub_key] = sub_value

            if has_vertices:
                resolved_params[key] = resolved_dict

    return resolved_params
