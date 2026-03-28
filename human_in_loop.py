"""Human-in-the-Loop (HITL) API endpoints.

These endpoints allow the frontend (or any client) to:
  - List paused graph runs awaiting human input
  - Inspect the interrupt context for a paused run
  - Resume a paused run with a human decision
  - Cancel a paused run

A run is paused when a HumanApproval node calls LangGraph's interrupt().  The graph state is frozen in the MemorySaver
checkpointer, identified by thread_id (== session_id used in arun()).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, status
from langgraph.types import Command
from loguru import logger
from sqlmodel import col, select

from agentcore.api.utils import (
    CurrentActiveUser,
    DbSession,
    build_graph_from_data,
    build_graph_from_db_no_cache,
)
from agentcore.graph_langgraph.checkpointer import get_checkpointer
from agentcore.services.database.models.agent.model import Agent
from agentcore.services.database.models.hitl_request.model import (
    HITLDelegateRequest,
    HITLRequest,
    HITLRequestRead,
    HITLResumeRequest,
    HITLStatus,
)
from agentcore.services.database.models.user_department_membership.model import UserDepartmentMembership
from agentcore.services.database.models.approval_notification.model import ApprovalNotification
from agentcore.services.database.models.user.model import User
from agentcore.services.approval_notifications import upsert_approval_notification
from agentcore.services.auth.permissions import get_permissions_for_role

router = APIRouter(prefix="/v1/hitl", tags=["Human-in-the-Loop"])


# ── Helpers ──────────────────────────────────────────────────────────────────

async def _restore_checkpoint_to_memory(thread_id: str, checkpoint_data: str | None) -> None:
    """Restore a serialized checkpoint from the DB back into the MemorySaver.

    If ``checkpoint_data`` is None the function is a no-op; the MemorySaver may
    still have the checkpoint from the current process run (no restart since the
    original run).  If it is set, we deserialize and inject the entries so the
    compiled graph can find the checkpoint even after a server restart.
    """
    if not checkpoint_data:
        logger.debug(
            f"[HITL] No checkpoint_data stored for thread_id={thread_id!r} "
            "— relying on in-process MemorySaver"
        )
        return

    try:
        import base64
        import pickle

        payload = pickle.loads(base64.b64decode(checkpoint_data))
        storage_data: dict = payload.get("storage", {})
        blobs_data: dict = payload.get("blobs", {})

        checkpointer = get_checkpointer()

        # Restore storage entries for this thread
        for checkpoint_ns, entries in storage_data.items():
            checkpointer.storage[thread_id][checkpoint_ns].update(entries)

        # Restore blob entries for this thread
        for key, value in blobs_data.items():
            checkpointer.blobs[key] = value

        logger.info(
            f"[HITL] Restored checkpoint for thread_id={thread_id!r} from DB "
            f"({len(checkpoint_data)} chars)"
        )
    except Exception as _err:
        logger.warning(f"[HITL] Could not restore checkpoint: {_err}")


async def _get_pending_request(thread_id: str, session) -> HITLRequest:
    """Fetch the most-recent PENDING HITLRequest or raise 404.

    Filters to PENDING status so that historical resolved rows for the same
    thread_id (e.g. from previous test runs) are never returned by mistake.
    """
    stmt = (
        select(HITLRequest)
        .where(HITLRequest.thread_id == thread_id)
        .where(HITLRequest.status == HITLStatus.PENDING)
        .order_by(col(HITLRequest.requested_at).desc())
        .limit(1)
    )
    result = await session.exec(stmt)
    req = result.first()
    if req is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No pending HITL request found for thread_id={thread_id!r}",
        )
    return req


async def _enrich_with_agent_names(rows: list[HITLRequest], session) -> list[dict]:
    """Batch-load agent names and assignee names, merge into HITLRequestRead dicts."""
    agent_ids = {r.agent_id for r in rows}
    agent_map: dict[UUID, str] = {}
    if agent_ids:
        agent_rows = (
            await session.exec(select(Agent).where(Agent.id.in_(agent_ids)))
        ).all()
        agent_map = {a.id: a.name for a in agent_rows}

    # Batch-load assigned-to user names for display in the HITL table.
    assigned_user_ids = {r.assigned_to for r in rows if r.assigned_to}
    user_map: dict[UUID, str] = {}
    if assigned_user_ids:
        user_rows = (
            await session.exec(select(User).where(User.id.in_(assigned_user_ids)))
        ).all()
        user_map = {
            u.id: getattr(u, "display_name", None) or getattr(u, "username", str(u.id))
            for u in user_rows
        }

    return [
        {
            **HITLRequestRead.model_validate(r, from_attributes=True).model_dump(),
            "agent_name": agent_map.get(r.agent_id),
            "assigned_to_name": user_map.get(r.assigned_to) if r.assigned_to else None,
        }
        for r in rows
    ]


async def _mark_hitl_notification_read(
    *,
    session: DbSession,
    hitl_request_id: str,
    recipient_user_id: UUID | None,
) -> None:
    if not recipient_user_id:
        return

    notification = (
        await session.exec(
            select(ApprovalNotification).where(
                ApprovalNotification.recipient_user_id == recipient_user_id,
                ApprovalNotification.entity_type == "hitl_assignment",
                ApprovalNotification.entity_id == hitl_request_id,
                ApprovalNotification.is_read == False,  # noqa: E712
            )
        )
    ).first()
    if notification:
        notification.is_read = True
        notification.read_at = datetime.now(timezone.utc)
        session.add(notification)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/pending", response_model=list[HITLRequestRead])
async def list_pending_hitl(
    current_user: CurrentActiveUser,
    session: DbSession,
    status_filter: str | None = Query(default=None, alias="status"),
) -> list[dict]:
    """Return HITL requests, optionally filtered by status.

    Query params:
        status: "pending" (default) — only PENDING requests
                "all"              — all requests regardless of status

    Returns only requests explicitly assigned to or created by the current user.
    No user (including superusers) sees all requests by default — they must be
    assigned or delegated to see deployed-run HIL requests.
    """
    stmt = select(HITLRequest).order_by(col(HITLRequest.requested_at).desc())

    if status_filter != "all":
        stmt = stmt.where(HITLRequest.status == HITLStatus.PENDING)

    from sqlalchemy import and_, or_
    stmt = stmt.where(
        or_(
            # Published/deployed runs: only the assigned approver sees them
            and_(
                HITLRequest.is_deployed_run == True,  # noqa: E712
                HITLRequest.assigned_to == current_user.id,
            ),
            # Playground runs: creator sees their own (current behavior)
            and_(
                HITLRequest.is_deployed_run == False,  # noqa: E712
                or_(
                    HITLRequest.user_id == current_user.id,
                    HITLRequest.user_id.is_(None),
                ),
            ),
        )
    )

    result = await session.exec(stmt)
    rows = result.all()
    return await _enrich_with_agent_names(rows, session)


@router.get("/{thread_id}/state", response_model=HITLRequestRead)
async def get_hitl_state(
    thread_id: str,
    current_user: CurrentActiveUser,
    session: DbSession,
) -> dict:
    """Return the interrupt context for a paused run.

    The ``interrupt_data`` field contains the question, context, and available
    actions that should be shown to the human reviewer.
    """
    req = await _get_pending_request(thread_id, session)
    _check_hitl_authorization(req, current_user)
    enriched = await _enrich_with_agent_names([req], session)
    return enriched[0]


@router.post("/{thread_id}/resume")
async def resume_hitl(
    thread_id: str,
    body: HITLResumeRequest,
    current_user: CurrentActiveUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    """Resume a paused graph run with the human's decision.

    On the backend pod (no AGENTCORE_IS_POD):
    - playground/non-deployed runs execute locally on backend
    - deployed/published runs are forwarded via ORCHESTRATOR_BASE_URL
      so execution stays on routed agent pod context.
    On the agent pod (AGENTCORE_IS_POD=true), or when called internally, this
    executes the graph locally.

    Body:
        action: The action the human chose (must match one of the actions in interrupt_data)
        feedback: Optional free-text feedback from the reviewer
        edited_value: Optional edited value (for "Edit" action paths)
    """
    hitl_req = await _get_pending_request(thread_id, session)
    _check_hitl_authorization(hitl_req, current_user)
    if not _is_playground_owner(hitl_req, current_user):
        await _require_hitl_permission(
            current_user, _permission_for_hitl_action(body.action)
        )
    else:
        logger.info(
            f"[HITL] Playground owner bypass for thread_id={thread_id!r}, "
            f"user_id={current_user.id}, action={body.action!r}"
        )

    if hitl_req.status != HITLStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Run is not pending — current status: {hitl_req.status.value}",
        )

    # ── Forward to agent pod if we are the backend pod ──────────────────
    _is_agent_pod = bool(os.environ.get("AGENTCORE_IS_POD"))
    _internal_secret = os.environ.get("AGENTCORE_INTERNAL_SECRET", "")
    _is_internal = bool(
        _internal_secret
        and request.headers.get("X-Internal-Secret") == _internal_secret
    )

    if not _is_agent_pod and not _is_internal:
        # Playground/non-deployed runs should resume on backend pod.
        # Only deployed/published runs are forwarded through orchestrator routing.
        if not hitl_req.is_deployed_run:
            logger.info(
                f"[HITL] Playground run for thread_id={thread_id!r} — "
                "executing resume locally on backend pod"
            )
            return await _execute_resume_locally(
                thread_id=thread_id,
                body=body,
                hitl_req=hitl_req,
                current_user=current_user,
                session=session,
            )

        base_url = os.environ.get("ORCHESTRATOR_BASE_URL", "")
        if base_url:
            hitl_resume_flag_raw = str(os.environ.get("HITL_RESUME_VIA_RUN_API", "false"))
            use_run_resume = hitl_resume_flag_raw.lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
            logger.info(
                f"[HITL] Resume routing config — HITL_RESUME_VIA_RUN_API={hitl_resume_flag_raw!r}, "
                f"parsed_use_run_resume={use_run_resume}, "
                f"has_orchestrator_base_url={bool(base_url)}"
            )
            if use_run_resume:
                return await _forward_resume_via_run_api(
                    base_url=base_url,
                    secret=_internal_secret,
                    thread_id=thread_id,
                    body=body,
                    hitl_req=hitl_req,
                    current_user=current_user,
                    session=session,
                    request=request,
                )
            return await _forward_resume_to_agent_pod(
                base_url=base_url,
                secret=_internal_secret,
                thread_id=thread_id,
                body=body,
                hitl_req=hitl_req,
                current_user=current_user,
                session=session,
                request=request,
            )
        logger.warning(
            "[HITL] ORCHESTRATOR_BASE_URL not set — running resume locally on backend pod"
        )

    # ── Execute locally (agent pod, or fallback) ────────────────────────
    return await _execute_resume_locally(
        thread_id=thread_id,
        body=body,
        hitl_req=hitl_req,
        current_user=current_user,
        session=session,
    )


async def _forward_resume_to_agent_pod(
    *,
    base_url: str,
    secret: str,
    thread_id: str,
    body: HITLResumeRequest,
    hitl_req: HITLRequest,
    current_user: Any,
    session: Any,
    request: Request | None = None,
) -> dict[str, Any]:
    """Forward the HITL resume request to the agent pod via HTTP."""
    url = f"{base_url}/api/v1/hitl/{thread_id}/resume"
    headers = {
        "X-Internal-Secret": secret,
        "Content-Type": "application/json",
    }
    if request:
        auth_header = request.headers.get("Authorization")
        if auth_header:
            headers["Authorization"] = auth_header
    payload = body.model_dump()
    logger.info(f"[HITL] Forwarding resume to agent pod: {url}")

    try:
        async with httpx.AsyncClient(timeout=300, verify=False) as client:
            resp = await client.post(url, json=payload, headers=headers)

        if resp.status_code != 200:
            logger.error(
                f"[HITL] Agent pod resume failed: status={resp.status_code}, "
                f"body={resp.text[:500]}"
            )
            raise HTTPException(
                status_code=resp.status_code,
                detail=f"Agent pod resume failed: {resp.text[:500]}",
            )

        result = resp.json()
        logger.info(
            f"[HITL] Agent pod resume completed for thread_id={thread_id!r}: "
            f"status={result.get('status')}"
        )

        # The agent pod handles graph execution, checkpoint, and orch_conversation
        # storage.  But we still need to update the HITLRequest status and
        # notification on the backend pod (since we own the DB session).
        agent_status = result.get("status")
        decision = {
            "action": body.action,
            "feedback": body.feedback or "",
            "edited_value": body.edited_value or "",
        }

        if agent_status == "interrupted":
            # Agent pod hit another HITL interrupt — it already created
            # a new HITLRequest row.  Just mark the current one resolved.
            _resolve_request(hitl_req, decision, body.action, current_user.id)
            session.add(hitl_req)
            await _mark_hitl_notification_read(
                session=session,
                hitl_request_id=str(hitl_req.id),
                recipient_user_id=hitl_req.assigned_to or current_user.id,
            )
            await session.commit()
        elif agent_status == "completed":
            # Already resolved by the agent pod's local _execute_resume_locally.
            # The agent pod updated the HITLRequest and stored orch_conversation.
            # Just refresh to get latest state.
            await session.refresh(hitl_req)

        return result

    except httpx.HTTPError as exc:
        logger.exception(f"[HITL] Failed to forward resume to agent pod: {exc}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to reach agent pod for HITL resume: {exc}",
        ) from exc


async def _resolve_hitl_run_target(
    *,
    hitl_req: HITLRequest,
    session: Any,
) -> tuple[str, str] | None:
    """Resolve env/version for HITL resume via /api/run routing."""
    def _log_target(source: str, env_code: str, version: str) -> None:
        env_name = {"0": "dev", "1": "uat", "2": "prod"}.get(env_code, f"unknown({env_code})")
        logger.info(
            f"[HITL] Resolved resume target from {source}: env={env_name} ({env_code}), version={version}"
        )

    interrupt_data = hitl_req.interrupt_data or {}
    deploy_meta = interrupt_data.get("_deploy_meta") or {}

    env_code = str(deploy_meta.get("env") or "").strip()
    version = str(deploy_meta.get("version") or "").strip()
    if env_code in {"0", "1", "2"} and version:
        if not version.startswith("v"):
            version = f"v{version}"
        _log_target("interrupt_data._deploy_meta", env_code, version)
        return env_code, version

    deployment_id = (
        deploy_meta.get("deployment_id")
        or (interrupt_data.get("_orch_meta") or {}).get("deployment_id")
    )
    if deployment_id:
        try:
            dep_uuid = UUID(str(deployment_id))
            from agentcore.services.database.models.agent_deployment_uat.model import (
                AgentDeploymentUAT,
            )
            from agentcore.services.database.models.agent_deployment_prod.model import (
                AgentDeploymentProd,
            )

            uat_dep = await session.get(AgentDeploymentUAT, dep_uuid)
            if uat_dep:
                resolved = ("1", f"v{uat_dep.version_number}")
                _log_target("interrupt_data deployment_id -> UAT table", resolved[0], resolved[1])
                return resolved

            prod_dep = await session.get(AgentDeploymentProd, dep_uuid)
            if prod_dep:
                resolved = ("2", f"v{prod_dep.version_number}")
                _log_target("interrupt_data deployment_id -> PROD table", resolved[0], resolved[1])
                return resolved
        except Exception as exc:
            logger.warning(f"[HITL] Could not resolve deployment target for resume: {exc}")

    # Fallback for older rows where interrupt_data missed deployment metadata:
    # infer deployment_id from orchestrator conversation entries for this
    # session/agent and then resolve env/version from deployment tables.
    if hitl_req.session_id:
        try:
            from sqlmodel import col, select
            from agentcore.services.database.models.orch_conversation.model import (
                OrchConversationTable,
            )
            from agentcore.services.database.models.agent_deployment_uat.model import (
                AgentDeploymentUAT,
            )
            from agentcore.services.database.models.agent_deployment_prod.model import (
                AgentDeploymentProd,
            )

            dep_stmt = (
                select(OrchConversationTable.deployment_id)
                .where(OrchConversationTable.session_id == hitl_req.session_id)
                .where(OrchConversationTable.agent_id == hitl_req.agent_id)
                .where(OrchConversationTable.deployment_id.is_not(None))
                .order_by(col(OrchConversationTable.timestamp).desc())
                .limit(1)
            )
            dep_row = (await session.exec(dep_stmt)).first()
            dep_from_conversation = None
            if dep_row is not None:
                dep_from_conversation = dep_row[0] if isinstance(dep_row, tuple) else dep_row

            if dep_from_conversation:
                dep_uuid = UUID(str(dep_from_conversation))
                uat_dep = await session.get(AgentDeploymentUAT, dep_uuid)
                if uat_dep:
                    resolved = ("1", f"v{uat_dep.version_number}")
                    _log_target("orch_conversation deployment_id -> UAT table", resolved[0], resolved[1])
                    return resolved

                prod_dep = await session.get(AgentDeploymentProd, dep_uuid)
                if prod_dep:
                    resolved = ("2", f"v{prod_dep.version_number}")
                    _log_target("orch_conversation deployment_id -> PROD table", resolved[0], resolved[1])
                    return resolved
        except Exception as exc:
            logger.warning(f"[HITL] Could not infer deployment target from orch conversation: {exc}")

    if not hitl_req.is_deployed_run:
        _log_target("non-deployed fallback", "0", "v1")
        return "0", "v1"

    return None


async def _forward_resume_via_run_api(
    *,
    base_url: str,
    secret: str,
    thread_id: str,
    body: HITLResumeRequest,
    hitl_req: HITLRequest,
    current_user: Any,
    session: Any,
    request: Request,
) -> dict[str, Any]:
    """Forward HITL resume through /api/run to reuse existing pod routing."""
    target = await _resolve_hitl_run_target(hitl_req=hitl_req, session=session)
    if not target:
        logger.warning(
            f"[HITL] Could not resolve env/version for thread_id={thread_id!r}; "
            "falling back to legacy /api/v1/hitl forward path"
        )
        return await _forward_resume_to_agent_pod(
            base_url=base_url,
            secret=secret,
            thread_id=thread_id,
            body=body,
            hitl_req=hitl_req,
            current_user=current_user,
            session=session,
            request=request,
        )

    env_code, version = target
    url = (
        f"{base_url}/api/run/{hitl_req.agent_id}"
        f"?env={env_code}&version={version}&stream=false"
        f"&hitl_resume_thread_id={thread_id}"
    )
    headers = {
        "X-Internal-Secret": secret,
        "Content-Type": "application/json",
        "X-Orch-User-Id": str(current_user.id),
        # Some ingress/proxies may drop unknown query params.
        # Send thread id via header as a resilient fallback.
        "X-HITL-Resume-Thread-Id": thread_id,
    }
    payload = {
        "session_id": hitl_req.session_id or thread_id,
        "hitl_action": body.action,
        "hitl_feedback": body.feedback or "",
        "hitl_edited_value": body.edited_value or "",
    }
    logger.info(f"[HITL] Forwarding resume via /api/run: {url}")

    try:
        async with httpx.AsyncClient(timeout=300, verify=False) as client:
            resp = await client.post(url, json=payload, headers=headers)

        if resp.status_code != 200:
            logger.error(
                f"[HITL] /api/run resume failed: status={resp.status_code}, "
                f"body={resp.text[:500]}"
            )
            raise HTTPException(
                status_code=resp.status_code,
                detail=f"/api/run resume failed: {resp.text[:500]}",
            )

        result = resp.json()
        if not isinstance(result, dict) or result.get("status") not in {"completed", "interrupted"}:
            logger.warning(
                f"[HITL] /api/run returned non-resume payload for thread_id={thread_id!r}; "
                "falling back to legacy /api/v1/hitl forward path"
            )
            return await _forward_resume_to_agent_pod(
                base_url=base_url,
                secret=secret,
                thread_id=thread_id,
                body=body,
                hitl_req=hitl_req,
                current_user=current_user,
                session=session,
                request=request,
            )
        logger.info(
            f"[HITL] /api/run resume completed for thread_id={thread_id!r}: "
            f"status={result.get('status')}"
        )
        await session.refresh(hitl_req)
        return result
    except httpx.HTTPError as exc:
        logger.exception(f"[HITL] Failed to forward /api/run resume: {exc}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to reach agent pod for HITL resume: {exc}",
        ) from exc


async def _execute_resume_locally(
    *,
    thread_id: str,
    body: HITLResumeRequest,
    hitl_req: HITLRequest,
    current_user: Any,
    session: Any,
    agent_payload: dict | None = None,
    agent_name: str | None = None,
) -> dict[str, Any]:
    """Execute the HITL resume locally (on the agent pod or fallback)."""
    decision = {
        "action": body.action,
        "feedback": body.feedback or "",
        "edited_value": body.edited_value or "",
    }

    try:
        # Rebuild the LangGraph compiled app for this agent.
        # When agent_payload is provided (e.g. /api/run env/version snapshot),
        # prefer it so resume stays on the exact deployed version.
        if agent_payload is not None:
            graph = await build_graph_from_data(
                agent_id=str(hitl_req.agent_id),
                payload=agent_payload,
                agent_name=agent_name or "Agent",
                user_id=str(current_user.id),
                session_id=hitl_req.session_id or thread_id,
            )
        else:
            graph = await build_graph_from_db_no_cache(
                agent_id=hitl_req.agent_id,
                session=session,
                user_id=str(current_user.id),
                session_id=hitl_req.session_id or thread_id,
            )

        lg_config = {"configurable": {"thread_id": thread_id}}

        # Restore checkpoint from DB into MemorySaver so that the compiled graph
        # can find it even after a server restart (MemorySaver is in-process only).
        await _restore_checkpoint_to_memory(thread_id, hitl_req.checkpoint_data)

        # Verify the checkpoint is present before attempting resume.
        _pre_state = await graph.compiled_app.aget_state(lg_config)
        if not _pre_state.next:
            logger.warning(
                f"[HITL] No interrupted checkpoint found for thread_id={thread_id!r} "
                "after restore attempt — resume may run from scratch"
            )
        else:
            logger.info(
                f"[HITL] Checkpoint verified for thread_id={thread_id!r}: "
                f"next={_pre_state.next}"
            )

        # Hydrate upstream vertex objects from checkpoint state so that
        # _resolve_params() in vertex_wrapper.py can resolve edge-connected
        # inputs.  On resume, the graph is rebuilt from scratch (new vertex
        # objects with built=False), but LangGraph only re-executes from the
        # interrupted node onward — upstream vertices never run.  Without
        # hydration, _resolve_params() skips them (built=False) and downstream
        # components receive None for their inputs.
        #
        # IMPORTANT: Only hydrate vertices that are UPSTREAM (predecessors) of
        # the interrupted node.  The interrupted node itself and all downstream
        # nodes must NOT be hydrated — they need to re-execute fresh.
        # We use the predecessor_map to compute the full set of transitive
        # predecessors via BFS.
        try:
            checkpoint_values = _pre_state.values or {}
            vertices_results = checkpoint_values.get("vertices_results", {})
            predecessor_map = checkpoint_values.get("predecessor_map", {})
            next_nodes = set(_pre_state.next or ())

            # BFS: collect all transitive predecessors of the interrupted node(s)
            upstream: set[str] = set()
            queue = list(next_nodes)
            while queue:
                nid = queue.pop(0)
                for pred in predecessor_map.get(nid, []):
                    if pred not in upstream and pred not in next_nodes:
                        upstream.add(pred)
                        queue.append(pred)

            if vertices_results and upstream:
                hydrated = []
                for vid in upstream:
                    result = vertices_results.get(vid)
                    v = graph.get_vertex(vid)
                    if v and not v.built and result is not None:
                        v.built = True
                        v.built_result = result
                        v.built_object = result
                        hydrated.append(vid)
                        # Debug: log the type and structure of the hydrated result
                        if isinstance(result, dict):
                            detail = {k: type(val).__name__ for k, val in result.items()}
                        else:
                            detail = type(result).__name__
                        logger.info(
                            f"[HITL] Hydrated vertex {vid}: result_structure={detail}"
                        )
                if hydrated:
                    logger.info(
                        f"[HITL] Hydrated {len(hydrated)} upstream vertices "
                        f"from checkpoint: {hydrated}"
                    )
                else:
                    logger.debug(
                        f"[HITL] No upstream vertices to hydrate "
                        f"(next={next_nodes}, upstream={upstream})"
                    )
        except Exception as _hydrate_err:
            logger.warning(f"[HITL] Could not hydrate vertices from checkpoint: {_hydrate_err}")

        # Recover the original input content from the stored interrupt data
        # as a fallback — in case hydration didn't cover the input.
        try:
            if _pre_state.tasks and _pre_state.tasks[0].interrupts:
                interrupt_data = _pre_state.tasks[0].interrupts[0].value or {}
                decision["original_content"] = interrupt_data.get("context", "")
        except (IndexError, AttributeError):
            pass

        # Resume: pass Command(resume=decision) instead of initial_state.
        final_state = await graph.compiled_app.ainvoke(
            Command(resume=decision),
            config=lg_config,
        )

        # Check if the graph hit another interrupt() downstream.
        graph_state = await graph.compiled_app.aget_state(lg_config)
        if graph_state.next:
            next_interrupt_data: dict = {}
            if graph_state.tasks and graph_state.tasks[0].interrupts:
                next_interrupt_data = graph_state.tasks[0].interrupts[0].value or {}

            # Serialize the new checkpoint for the second interrupt so it too
            # can be restored after a server restart.
            from agentcore.graph_langgraph.nodes import save_hitl_checkpoint_after_interrupt
            await save_hitl_checkpoint_after_interrupt(thread_id)

            # Update this record as resolved, create a new pending record.
            _resolve_request(hitl_req, decision, body.action, current_user.id)
            session.add(hitl_req)
            await _mark_hitl_notification_read(
                session=session,
                hitl_request_id=str(hitl_req.id),
                recipient_user_id=hitl_req.assigned_to or current_user.id,
            )

            new_req = HITLRequest(
                thread_id=thread_id,
                agent_id=hitl_req.agent_id,
                session_id=hitl_req.session_id,
                user_id=hitl_req.user_id,
                interrupt_data=next_interrupt_data,
                status=HITLStatus.PENDING,
                # Carry forward routing fields so re-interrupts stay with
                # the same approver / department context.
                assigned_to=hitl_req.assigned_to,
                dept_id=hitl_req.dept_id,
                org_id=hitl_req.org_id,
                is_deployed_run=hitl_req.is_deployed_run,
            )
            session.add(new_req)
            await session.commit()

            logger.info(f"[HITL] Graph interrupted again at: {graph_state.next}")
            return {
                "status": "interrupted",
                "thread_id": thread_id,
                "interrupt_data": next_interrupt_data,
                "hitl_request_id": str(new_req.id),
            }

        # Run completed normally.
        _resolve_request(hitl_req, decision, body.action, current_user.id)
        session.add(hitl_req)
        await _mark_hitl_notification_read(
            session=session,
            hitl_request_id=str(hitl_req.id),
            recipient_user_id=hitl_req.assigned_to or current_user.id,
        )
        await session.commit()

        # Extract the output text from the output vertex and check if
        # ChatOutput already stored its message to the conversation table.
        output_stored_by_component = False
        output_text: str | None = None
        for oid in getattr(graph, "_is_output_vertices", []):
            vertex = graph.get_vertex(oid)
            if not vertex or not vertex.built:
                continue
            comp = getattr(vertex, "custom_component", None)
            if comp and getattr(comp, "_stored_message_id", None):
                output_stored_by_component = True
            # Always extract the text — we need it for orch_conversation
            # even when ChatOutput already stored to the conversation table.
            if vertex.built_result is not None:
                result = vertex.built_result
                if isinstance(result, dict):
                    for v in result.values():
                        if v is not None and hasattr(v, "text"):
                            output_text = v.text
                            break
                elif hasattr(result, "text"):
                    output_text = result.text
            break  # Only check the first output vertex

        # Determine if this was an orchestrator run (orch metadata attached by nodes.py)
        orch_meta = (hitl_req.interrupt_data or {}).get("_orch_meta")
        if orch_meta and not orch_meta.get("user_id"):
            fallback_user_id = hitl_req.user_id or current_user.id
            if fallback_user_id:
                orch_meta = dict(orch_meta)
                orch_meta["user_id"] = str(fallback_user_id)
                logger.warning(
                    f"[HITL] orch_meta.user_id missing; using fallback user_id={orch_meta['user_id']} "
                    f"for thread_id={thread_id!r}"
                )
        logger.info(
            f"[HITL] Resume result — output_text={output_text!r:.200}, "
            f"orch_meta={orch_meta}, "
            f"output_stored_by_component={output_stored_by_component}, "
            f"interrupt_data_keys={list((hitl_req.interrupt_data or {}).keys())}"
        )

        # For orchestrator runs, store the LLM response as a separate
        # orch_conversation message.  ChatOutput stores to the conversation
        # table (Playground), but the Orch page reads from orch_conversation.
        if orch_meta and output_text:
            # Clean up stale one-word action echoes (e.g. "Approve") that may
            # have been persisted by older interrupted-run parsing.
            await _cleanup_stale_orch_action_echo(
                thread_id=thread_id,
                action=body.action,
                orch_meta=orch_meta,
            )
            await _store_orch_agent_response(
                agent_id=str(hitl_req.agent_id),
                output_text=output_text,
                orch_meta=orch_meta,
            )
        # For orchestrator runs, keep chat output focused on the AI response.
        # The HITL table is the source of truth for approval status.
        if not orch_meta:
            await _store_hitl_confirmation(
                thread_id=thread_id,
                agent_id=str(hitl_req.agent_id),
                action=body.action,
                output_text=output_text if not output_stored_by_component else None,
                orch_meta=orch_meta,
            )

        logger.info(f"[HITL] Run {thread_id!r} resumed and completed successfully.")
        return {
            "status": "completed",
            "thread_id": thread_id,
            "action": body.action,
            "output_text": output_text,
        }

    except Exception as exc:
        logger.exception(f"[HITL] Error resuming thread {thread_id!r}: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to resume run: {exc}",
        ) from exc


@router.post("/{thread_id}/cancel")
async def cancel_hitl(
    thread_id: str,
    current_user: CurrentActiveUser,
    session: DbSession,
) -> dict[str, str]:
    """Cancel a pending HITL request without resuming the graph.

    The frozen graph state remains in the checkpointer but the DB record is
    marked as cancelled.  The run cannot be resumed after cancellation.
    """
    hitl_req = await _get_pending_request(thread_id, session)
    _check_hitl_authorization(hitl_req, current_user)
    if not _is_playground_owner(hitl_req, current_user):
        await _require_hitl_permission(current_user, "hitl_reject")
    else:
        logger.info(
            f"[HITL] Playground owner cancel bypass for thread_id={thread_id!r}, "
            f"user_id={current_user.id}"
        )

    if hitl_req.status != HITLStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Run is not pending — current status: {hitl_req.status.value}",
        )

    hitl_req.status = HITLStatus.CANCELLED
    hitl_req.decided_at = datetime.now(timezone.utc)
    hitl_req.decided_by_user_id = current_user.id
    session.add(hitl_req)
    await _mark_hitl_notification_read(
        session=session,
        hitl_request_id=str(hitl_req.id),
        recipient_user_id=hitl_req.assigned_to or current_user.id,
    )
    await session.commit()

    logger.info(f"[HITL] Run {thread_id!r} cancelled by user {current_user.id}.")
    return {"status": "cancelled", "thread_id": thread_id}


@router.post("/{thread_id}/delegate")
async def delegate_hitl(
    thread_id: str,
    body: HITLDelegateRequest,
    current_user: CurrentActiveUser,
    session: DbSession,
) -> dict[str, Any]:
    """Delegate a pending HITL request to another user.

    Only the current assignee (or a superuser) can delegate.  The request
    is reassigned so it disappears from the delegator's list and appears
    for the new assignee.
    """
    hitl_req = await _get_pending_request(thread_id, session)
    _check_hitl_authorization(hitl_req, current_user)

    if not hitl_req.dept_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This HITL request cannot be delegated without a department scope.",
        )

    # Validate target user exists, is active, and belongs to the same department.
    target_user = (
        await session.exec(select(User).where(User.id == body.delegate_to_user_id))
    ).first()
    if not target_user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Target user not found",
        )
    if not getattr(target_user, "is_active", True):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Target user is not active",
        )
    target_membership = (
        await session.exec(
            select(UserDepartmentMembership).where(
                UserDepartmentMembership.user_id == body.delegate_to_user_id,
                UserDepartmentMembership.department_id == hitl_req.dept_id,
                UserDepartmentMembership.status == "active",
            )
        )
    ).first()
    if not target_membership:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Target user must be an active member of the same department.",
        )

    previous_assignee = hitl_req.assigned_to
    hitl_req.assigned_to = body.delegate_to_user_id
    hitl_req.delegated_by = current_user.id
    hitl_req.delegated_at = datetime.now(timezone.utc)
    session.add(hitl_req)

    if previous_assignee and previous_assignee != body.delegate_to_user_id:
        await _mark_hitl_notification_read(
            session=session,
            hitl_request_id=str(hitl_req.id),
            recipient_user_id=previous_assignee,
        )

    if body.delegate_to_user_id != current_user.id:
        await upsert_approval_notification(
            session,
            recipient_user_id=body.delegate_to_user_id,
            entity_type="hitl_assignment",
            entity_id=str(hitl_req.id),
            title="A HITL task was delegated to you.",
            link="/hitl-approvals",
        )

    await session.commit()

    logger.info(
        f"[HITL] Request {thread_id!r} delegated from {current_user.id} "
        f"to {body.delegate_to_user_id}"
    )
    return {
        "status": "delegated",
        "thread_id": thread_id,
        "delegated_to": str(body.delegate_to_user_id),
    }


@router.get("/delegatable-users")
async def get_delegatable_users(
    current_user: CurrentActiveUser,
    session: DbSession,
    dept_id: UUID = Query(..., description="Department ID to list users from"),
) -> list[dict]:
    """Return active users in the given department who can receive delegated HITL requests."""
    from agentcore.services.database.models.user_department_membership.model import (
        UserDepartmentMembership,
    )

    stmt = (
        select(User)
        .join(UserDepartmentMembership, UserDepartmentMembership.user_id == User.id)
        .where(
            UserDepartmentMembership.department_id == dept_id,
            UserDepartmentMembership.status == "active",
            User.id != current_user.id,
        )
    )
    users = (await session.exec(stmt)).all()
    return [
        {
            "id": str(u.id),
            "display_name": getattr(u, "display_name", None) or getattr(u, "username", str(u.id)),
            "email": getattr(u, "email", None),
        }
        for u in users
    ]


# ── Internal helpers ──────────────────────────────────────────────────────────

def _check_hitl_authorization(hitl_req: HITLRequest, current_user) -> None:
    """Verify the current user is allowed to act on this HITL request.

    For deployed runs with an assigned approver, only that approver may
    resume / cancel / delegate.  No superuser bypass — superusers must be
    explicitly assigned or delegated by the department admin.
    Playground requests (no assigned_to) remain accessible to the original
    creator.
    """
    if hitl_req.assigned_to:
        if hitl_req.assigned_to != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You are not assigned to this approval request",
            )
        return

    # Playground runs (no assignee): only creator can act.
    if (
        not hitl_req.is_deployed_run
        and hitl_req.user_id is not None
        and hitl_req.user_id != current_user.id
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the flow owner can act on this playground HITL request",
        )


def _is_playground_owner(hitl_req: HITLRequest, current_user) -> bool:
    return (
        not hitl_req.is_deployed_run
        and hitl_req.user_id is not None
        and hitl_req.user_id == current_user.id
    )


async def _require_hitl_permission(current_user, permission_key: str) -> None:
    permissions = await get_permissions_for_role(str(current_user.role))
    if permission_key not in permissions:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing required permission: {permission_key}",
        )


def _permission_for_hitl_action(action: str) -> str:
    return "hitl_reject" if "reject" in action.lower() else "hitl_approve"

async def _store_orch_agent_response(
    agent_id: str,
    output_text: str,
    orch_meta: dict,
) -> None:
    """Store the LLM/ChatOutput response to orch_conversation after HITL resume.

    During normal orchestrator runs, the orch endpoint extracts the response text
    and stores it to orch_conversation.  During HITL resume, ChatOutput stores to
    the conversation table (Playground) but NOT orch_conversation.  This function
    fills that gap so the Orchestration page shows the full AI response.
    """
    try:
        from uuid import UUID as _UUID
        from agentcore.services.database.models.orch_conversation.model import OrchConversationTable
        from agentcore.services.database.models.orch_conversation.crud import orch_add_message
        from agentcore.services.deps import session_scope

        async with session_scope() as db:
            orch_msg = OrchConversationTable(
                sender="agent",
                sender_name="AI",
                session_id=orch_meta.get("session_id"),
                text=output_text,
                agent_id=_UUID(agent_id) if agent_id else None,
                user_id=_UUID(orch_meta["user_id"]) if orch_meta.get("user_id") else None,
                deployment_id=_UUID(orch_meta["deployment_id"]) if orch_meta.get("deployment_id") else None,
                files=[],
                properties={},
                category="message",
                content_blocks=[],
            )
            await orch_add_message(orch_msg, db)
        logger.info(
            f"[HITL] Stored orch agent response ({len(output_text)} chars) "
            f"session_id={orch_meta.get('session_id')}, "
            f"deployment_id={orch_meta.get('deployment_id')}, "
            f"user_id={orch_meta.get('user_id')}"
        )
    except Exception as _err:
        logger.exception(f"[HITL] Could not store orch agent response: {_err}")


async def _cleanup_stale_orch_action_echo(
    *,
    thread_id: str,
    action: str,
    orch_meta: dict,
) -> None:
    """Remove stale action-echo messages (plain 'Approve'/'Reject') in orch chat."""
    try:
        from sqlalchemy import func
        from uuid import UUID as _UUID
        from agentcore.services.database.models.orch_conversation.model import OrchConversationTable
        from agentcore.services.deps import session_scope

        session_id = orch_meta.get("session_id") or thread_id
        if not session_id or not action:
            return

        async with session_scope() as db:
            stmt = (
                select(OrchConversationTable)
                .where(OrchConversationTable.session_id == session_id)
                .where(OrchConversationTable.sender == "agent")
                .where(func.lower(OrchConversationTable.text) == action.strip().lower())
                .order_by(col(OrchConversationTable.timestamp).desc())
                .limit(1)
            )

            if orch_meta.get("user_id"):
                stmt = stmt.where(OrchConversationTable.user_id == _UUID(orch_meta["user_id"]))
            if orch_meta.get("deployment_id"):
                stmt = stmt.where(OrchConversationTable.deployment_id == _UUID(orch_meta["deployment_id"]))

            row = (await db.exec(stmt)).first()
            if row:
                await db.delete(row)
                await db.commit()
                logger.info(
                    f"[HITL] Removed stale orch action-echo message id={row.id} "
                    f"session_id={session_id}, action={action!r}"
                )
    except Exception as _err:
        logger.debug(f"[HITL] Could not cleanup stale orch action echo: {_err}")


async def _store_hitl_confirmation(
    thread_id: str,
    agent_id: str,
    action: str,
    output_text: str | None = None,
    orch_meta: dict | None = None,
) -> None:
    """Store a confirmation chat message after HITL resume.

    Writes to the correct table depending on context:
    - Playground runs  → ``conversation`` table (via ``astore_message``)
    - Orchestrator runs → ``orch_conversation`` table (via ``orch_add_message``)

    ``orch_meta`` is set by ``_persist_hitl_request`` in nodes.py when the graph
    was launched from the orchestrator.  It contains ``deployment_id``, ``user_id``,
    and ``session_id`` needed for the orch_conversation row.
    """
    try:
        is_reject = "reject" in action.lower()
        icon = "✗" if is_reject else "✓"
        text = f"{icon} **{action}** — Human review completed"
        if output_text:
            text += f"\n\n> {output_text}"

        if orch_meta:
            # Orchestrator run → write to orch_conversation
            from uuid import UUID as _UUID
            from agentcore.services.database.models.orch_conversation.model import OrchConversationTable
            from agentcore.services.database.models.orch_conversation.crud import orch_add_message
            from agentcore.services.deps import session_scope

            async with session_scope() as db:
                orch_msg = OrchConversationTable(
                    sender="agent",
                    sender_name="Agent",
                    session_id=orch_meta.get("session_id") or thread_id,
                    text=text,
                    agent_id=_UUID(agent_id) if agent_id else None,
                    user_id=_UUID(orch_meta["user_id"]) if orch_meta.get("user_id") else None,
                    deployment_id=_UUID(orch_meta["deployment_id"]) if orch_meta.get("deployment_id") else None,
                    files=[],
                    properties={},
                    category="message",
                    content_blocks=[],
                )
                await orch_add_message(orch_msg, db)
            logger.info(f"[HITL] Stored orch confirmation for thread_id={thread_id!r}, action={action!r}")
        else:
            # Playground run → write to conversation
            from agentcore.memory import astore_message
            from agentcore.schema.message import Message

            msg = Message(
                text=text,
                sender="Machine",
                sender_name="Agent",
                session_id=thread_id,
                agent_id=agent_id,
            )
            await astore_message(msg, agent_id=agent_id)
            logger.info(f"[HITL] Stored playground confirmation for thread_id={thread_id!r}, action={action!r}")
    except Exception as _err:
        logger.warning(f"[HITL] Could not store confirmation message: {_err}")




def _resolve_request(
    req: HITLRequest,
    decision: dict,
    action: str,
    decided_by: UUID,
) -> None:
    """Update a HITLRequest record with the human's decision."""
    action_lower = action.lower()
    if "reject" in action_lower:
        req.status = HITLStatus.REJECTED
    elif "edit" in action_lower:
        req.status = HITLStatus.EDITED
    else:
        req.status = HITLStatus.APPROVED

    req.decision = decision
    req.decided_by_user_id = decided_by
    req.decided_at = datetime.now(timezone.utc)
