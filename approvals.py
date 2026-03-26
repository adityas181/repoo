"""Approval API router backed by database tables.

This exposes approval requests for approvers (department admins) and lets them
approve/reject pending PROD publish requests.
"""

from datetime import datetime, timezone
from uuid import UUID

import httpx
from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from agentcore.api.utils import CurrentActiveUser, DbSession
from agentcore.services.database.models.agent.model import Agent, LifecycleStatusEnum
from agentcore.services.database.models.agent_deployment_prod.model import (
    AgentDeploymentProd,
    DeploymentPRODStatusEnum,
    ProdDeploymentLifecycleEnum,
)
from agentcore.services.database.models.agent_registry.model import RegistryDeploymentEnvEnum
from agentcore.services.database.models.approval_request.model import (
    ApprovalDecisionEnum,
    ApprovalRequest,
)
from agentcore.services.database.models.approval_notification.model import ApprovalNotification
from agentcore.services.database.models.department.model import Department
from agentcore.services.database.models.folder.model import Folder
from agentcore.services.database.models.mcp_registry.model import McpRegistry, McpRegistryRead, McpRegistryUpdate
from agentcore.services.database.models.mcp_approval_request.model import McpApprovalRequest
from agentcore.services.database.models.model_approval_request.model import (
    ModelApprovalRequest,
    ModelApprovalRequestType,
)
from agentcore.services.database.models.model_audit_log.model import ModelAuditLog
from agentcore.services.database.models.model_registry.model import (
    ModelApprovalStatus,
    ModelEnvironment,
    ModelRegistry,
    ModelVisibilityScope,
)
from agentcore.services.database.models.role.model import Role
from agentcore.services.database.models.user_department_membership.model import UserDepartmentMembership
from agentcore.services.database.models.user_organization_membership.model import UserOrganizationMembership
from agentcore.services.mcp_service_client import update_mcp_server_via_service
from agentcore.services.database.models.user.model import User
from agentcore.services.database.models.agent_api_key.model import AgentApiKey
from agentcore.services.database.registry_service import sync_agent_registry
from agentcore.services.approval_notifications import upsert_approval_notification
from agentcore.services.auth.utils import generate_agent_api_key
from agentcore.services.database.models.agent_bundle.model import DeploymentEnvEnum


class SubmittedBy(BaseModel):
    name: str
    avatar: str | None = None
    email: str | None = None


class ApproverInfo(BaseModel):
    id: str | None = None
    name: str
    email: str | None = None
    role: str | None = None


class ApprovalAgent(BaseModel):
    id: str
    entityType: str = "agent"  # agent | mcp | model
    title: str
    status: str  # pending, approved, rejected
    description: str
    submittedBy: SubmittedBy
    approver: ApproverInfo | None = None
    project: str = ""
    visibility: str | None = None
    submitted: str
    version: str
    recentChanges: str
    adminComments: str | None = None
    adminAttachments: list[dict] | None = None


class ApprovalPreviewResponse(BaseModel):
    id: str
    title: str
    version: str
    snapshot: dict


class ApprovalResponse(BaseModel):
    success: bool
    message: str
    agentId: str
    newStatus: str
    timestamp: str
    approvedBy: str | None = None
    api_key: str | None = None


class ApprovalNotificationRead(BaseModel):
    id: str
    title: str
    link: str | None = None
    created_at: str


class GuardrailPromotionResult(BaseModel):
    uat_guardrail_id: str
    prod_guardrail_id: str | None = None
    in_sync: bool = False
    ready: bool = False
    error: str | None = None


class ProdPromotionHandoffResponse(BaseModel):
    id: UUID
    agent_id: UUID
    promoted_from_uat_id: UUID | None = None
    version_number: int
    guardrails_ready: bool = False
    guardrail_promotions: list[GuardrailPromotionResult] = []


router = APIRouter(prefix="/approvals", tags=["approvals"])


@router.get("/notifications", response_model=list[ApprovalNotificationRead])
async def list_approval_notifications(
    session: DbSession,
    current_user: CurrentActiveUser,
):
    rows = (
        await session.exec(
            select(ApprovalNotification)
            .where(
                ApprovalNotification.recipient_user_id == current_user.id,
                ApprovalNotification.is_read == False,  # noqa: E712
            )
            .order_by(ApprovalNotification.created_at.desc())
        )
    ).all()
    return [
        ApprovalNotificationRead(
            id=str(row.id),
            title=row.title,
            link=row.link,
            created_at=row.created_at.isoformat(),
        )
        for row in rows
    ]


@router.post("/notifications/{notification_id}/read", status_code=204)
async def mark_approval_notification_read(
    notification_id: UUID,
    session: DbSession,
    current_user: CurrentActiveUser,
):
    row = await session.get(ApprovalNotification, notification_id)
    if row is None or row.recipient_user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Notification not found")
    row.is_read = True
    row.read_at = datetime.now(timezone.utc)
    session.add(row)
    await session.commit()
    return None


@router.post("/notifications/read-all", status_code=204)
async def mark_all_approval_notifications_read(
    session: DbSession,
    current_user: CurrentActiveUser,
):
    rows = (
        await session.exec(
            select(ApprovalNotification).where(
                ApprovalNotification.recipient_user_id == current_user.id,
                ApprovalNotification.is_read == False,  # noqa: E712
            )
        )
    ).all()
    if rows:
        now = datetime.now(timezone.utc)
        for row in rows:
            row.is_read = True
            row.read_at = now
            session.add(row)
        await session.commit()
    return None


def _build_approver_info(user: User | None) -> ApproverInfo | None:
    if not user:
        return None
    name = user.display_name or user.username or "Unknown"
    email = user.email or (user.username if user.username and "@" in user.username else None)
    return ApproverInfo(
        id=str(user.id),
        name=name,
        email=email,
        role=getattr(user, "role", None),
    )


def _format_model_visibility_label(value: str | None) -> str:
    normalized = str(value or ModelVisibilityScope.PRIVATE.value).strip().lower()
    if normalized == ModelVisibilityScope.ORGANIZATION.value:
        return "Organization"
    if normalized == ModelVisibilityScope.DEPARTMENT.value:
        return "Department"
    return "Private"


def _format_mcp_visibility_label(value: str | None, public_scope: str | None) -> str:
    normalized_visibility = str(value or "private").strip().lower()
    normalized_scope = str(public_scope or "").strip().lower()
    if normalized_visibility == "public" and normalized_scope == "organization":
        return "Organization"
    if normalized_visibility == "public" and normalized_scope == "department":
        return "Department"
    return "Private"

async def _promote_guardrails_for_deployment(
    snapshot: dict,
    promoted_by: UUID,
) -> list[GuardrailPromotionResult]:
    """Extract NemoGuardrails nodes from the agent snapshot and promote each to prod.

    Returns a list of promotion results (one per guardrail node found).
    """
    from agentcore.services.guardrail_service_client import promote_guardrail_via_service

    results: list[GuardrailPromotionResult] = []
    for node in snapshot.get("nodes", []):
        node_data = node.get("data", {})
        if node_data.get("type") != "NemoGuardrails":
            continue
        template = node_data.get("node", {}).get("template", {})
        field = template.get("guardrail_id")
        if not field:
            continue
        value = field.get("value") if isinstance(field, dict) else field
        guardrail_id: str | None = None
        if isinstance(value, str) and "|" in value:
            parts = [p.strip() for p in value.split("|")]
            if len(parts) >= 2:
                guardrail_id = parts[1]
        elif isinstance(value, str) and value.strip():
            guardrail_id = value.strip()
        if not guardrail_id:
            continue

        result = GuardrailPromotionResult(uat_guardrail_id=guardrail_id)
        try:
            promo = await promote_guardrail_via_service(
                guardrail_id=guardrail_id,
                promoted_by=str(promoted_by),
            )
            result.prod_guardrail_id = promo.get("prod_guardrail_id")
            result.in_sync = promo.get("in_sync", False)
            result.ready = True
            logger.info(
                "[GUARDRAIL_PROMOTION] Guardrail promoted on approval: "
                f"uat_id={guardrail_id}, prod_id={result.prod_guardrail_id}, "
                f"in_sync={result.in_sync}"
            )
        except Exception as exc:
            result.error = str(exc)
            logger.warning(
                f"[GUARDRAIL_PROMOTION] Failed to promote guardrail {guardrail_id}: {exc}",
                exc_info=True,
            )
        results.append(result)
    return results


def _build_prod_promotion_handoff_payload(
    deployment: AgentDeploymentProd,
    guardrail_promotions: list[GuardrailPromotionResult] | None = None,
) -> ProdPromotionHandoffResponse:
    promo_list = guardrail_promotions or []
    all_ready = all(g.ready for g in promo_list) if promo_list else True
    return ProdPromotionHandoffResponse(
        id=deployment.id,
        agent_id=deployment.agent_id,
        promoted_from_uat_id=deployment.promoted_from_uat_id,
        version_number=deployment.version_number,
        guardrails_ready=all_ready,
        guardrail_promotions=promo_list,
    )


async def _migrate_pinecone_for_prod(
    deployment: AgentDeploymentProd,
    session: DbSession,
) -> None:
    """Copy Pinecone namespaces from UAT to PROD for all Pinecone nodes in the agent snapshot.

    For each Pinecone vectorstore node found in the snapshot:
      1. Read the UAT index_name and namespace
      2. Derive a PROD namespace: {original}_prod_v{version}
      3. Call pinecone-service to copy vectors from UAT → PROD namespace
      4. Update the snapshot with the new PROD namespace

    Safety guarantees:
      - Uses async HTTP client so the event loop is never blocked.
      - Collects all namespace updates first, only applies to snapshot if ALL succeed (atomic).
      - On any copy failure, raises so the caller can block the broken deployment.
      - Catalogue tracking failure is logged but does not block the migration.
    """
    from agentcore.services.pinecone_service_client import (
        async_copy_namespace_via_service,
        is_service_configured,
    )

    if not is_service_configured():
        logger.warning("[PINECONE_MIGRATION] Pinecone service not configured, skipping migration")
        return

    snapshot = deployment.agent_snapshot
    nodes = snapshot.get("nodes", [])

    # Phase 1: Collect all Pinecone nodes that need migration
    migration_plan: list[dict] = []

    for node in nodes:
        node_data = node.get("data", {})
        node_type = node_data.get("type", "")

        if node_type != "Pinecone":
            continue

        template = node_data.get("node", {}).get("template", {})
        index_name_field = template.get("index_name", {})
        namespace_field = template.get("namespace", {})

        index_name = index_name_field.get("value", "") if isinstance(index_name_field, dict) else str(index_name_field)
        uat_namespace = namespace_field.get("value", "") if isinstance(namespace_field, dict) else str(namespace_field)

        if not index_name:
            logger.warning("[PINECONE_MIGRATION] Pinecone node found but index_name is empty, skipping")
            continue

        prod_namespace = f"{uat_namespace}_prod_v{deployment.version_number}" if uat_namespace else f"prod_v{deployment.version_number}"

        migration_plan.append({
            "template": template,
            "namespace_field": namespace_field,
            "index_name": index_name,
            "uat_namespace": uat_namespace,
            "prod_namespace": prod_namespace,
        })

    if not migration_plan:
        logger.info("[PINECONE_MIGRATION] No Pinecone nodes found in snapshot, nothing to migrate")
        return

    # Phase 2: Execute all copies — if ANY fails, the entire migration fails (atomic)
    copy_results: list[dict] = []

    for plan in migration_plan:
        index_name = plan["index_name"]
        uat_namespace = plan["uat_namespace"]
        prod_namespace = plan["prod_namespace"]

        logger.info(
            f"[PINECONE_MIGRATION] Copying index={index_name} "
            f"src_ns='{uat_namespace}' → dst_ns='{prod_namespace}'"
        )

        try:
            result = await async_copy_namespace_via_service(
                index_name=index_name,
                source_namespace=uat_namespace,
                target_namespace=prod_namespace,
            )
            copied = result.get("copied_vectors", 0)
            logger.info(
                f"[PINECONE_MIGRATION] Done: {copied} vectors copied to '{prod_namespace}'"
            )
            copy_results.append({"plan": plan, "copied": copied})
        except httpx.HTTPStatusError as http_err:
            # 400 with "empty or does not exist" means the UAT namespace has no
            # vectors yet (agent configured Pinecone but hasn't ingested data).
            # Treat as a no-op: create the PROD namespace reference in the
            # snapshot so it's ready once data is ingested later.
            detail = str(http_err)
            if http_err.response.status_code == 400 and "empty or does not exist" in detail:
                logger.warning(
                    f"[PINECONE_MIGRATION] Source namespace '{uat_namespace}' is empty/missing "
                    f"in index '{index_name}' — skipping copy (0 vectors). "
                    f"PROD namespace '{prod_namespace}' will be used once data is available."
                )
                copy_results.append({"plan": plan, "copied": 0})
            else:
                logger.error(f"[PINECONE_MIGRATION] Failed to copy namespace: {http_err}")
                raise
        except Exception as copy_err:
            logger.error(f"[PINECONE_MIGRATION] Failed to copy namespace: {copy_err}")
            # Do NOT update snapshot — raise so caller knows migration failed.
            # Rollback of partially-copied data is handled by copy_namespace() in pinecone_service.
            raise

    # Phase 3: ALL copies succeeded — now update the snapshot atomically
    for entry in copy_results:
        plan = entry["plan"]
        namespace_field = plan["namespace_field"]
        template = plan["template"]
        prod_namespace = plan["prod_namespace"]

        if isinstance(namespace_field, dict):
            namespace_field["value"] = prod_namespace
        else:
            template["namespace"] = {"value": prod_namespace}

    deployment.agent_snapshot = snapshot
    flag_modified(deployment, "agent_snapshot")
    session.add(deployment)
    await session.flush()
    logger.info(f"[PINECONE_MIGRATION] Snapshot updated for deployment {deployment.id}")

    # Phase 4: Track in vector_db_catalogue (non-blocking, failure is logged only)
    for entry in copy_results:
        plan = entry["plan"]
        try:
            await _track_pinecone_migration(
                session=session,
                deployment=deployment,
                index_name=plan["index_name"],
                source_namespace=plan["uat_namespace"],
                target_namespace=plan["prod_namespace"],
                vectors_copied=entry["copied"],
            )
        except Exception as track_err:
            logger.warning(f"[PINECONE_MIGRATION] Catalogue tracking failed: {track_err}")


async def _track_pinecone_migration(
    session: DbSession,
    deployment: AgentDeploymentProd,
    index_name: str,
    source_namespace: str,
    target_namespace: str,
    vectors_copied: int,
) -> None:
    """Create a PROD entry in vector_db_catalogue to track the migration."""
    from datetime import datetime, timezone

    from agentcore.services.database.models.vector_db_catalogue.model import VectorDBCatalogue

    now = datetime.now(timezone.utc)

    # Try to find the matching UAT entry
    from sqlmodel import select

    uat_entry = (
        await session.exec(
            select(VectorDBCatalogue).where(
                VectorDBCatalogue.index_name == index_name,
                VectorDBCatalogue.namespace == source_namespace,
                VectorDBCatalogue.environment == "uat",
            ).limit(1)
        )
    ).first()

    agent_name = getattr(deployment, "agent_name", "") or ""

    prod_row = VectorDBCatalogue(
        name=f"{index_name}/{target_namespace}",
        description=f"PROD copy from UAT namespace '{source_namespace}'",
        provider="Pinecone",
        deployment=uat_entry.deployment if uat_entry else "SaaS",
        dimensions=uat_entry.dimensions if uat_entry else "",
        index_type=uat_entry.index_type if uat_entry else "serverless",
        status="connected",
        vector_count=str(vectors_copied),
        is_custom=False,
        environment="prod",
        index_name=index_name,
        namespace=target_namespace,
        agent_id=deployment.agent_id,
        agent_name=agent_name,
        source_entry_id=uat_entry.id if uat_entry else None,
        migration_status="completed",
        migrated_at=now,
        vectors_copied=vectors_copied,
        org_id=uat_entry.org_id if uat_entry else None,
        dept_id=uat_entry.dept_id if uat_entry else None,
        created_at=now,
        updated_at=now,
    )
    session.add(prod_row)
    await session.flush()
    logger.info(
        f"[PINECONE_MIGRATION] Tracked PROD entry: index={index_name} ns={target_namespace} vectors={vectors_copied}"
    )


async def _track_pinecone_migration_failure(
    deployment: AgentDeploymentProd,
    session: DbSession,
    error_msg: str,
) -> None:
    """Create a FAILED entry in vector_db_catalogue so the UI can show migration status."""
    from datetime import datetime, timezone

    from agentcore.services.database.models.vector_db_catalogue.model import VectorDBCatalogue

    now = datetime.now(timezone.utc)
    snapshot = deployment.agent_snapshot or {}
    nodes = snapshot.get("nodes", [])

    for node in nodes:
        node_data = node.get("data", {})
        if node_data.get("type", "") != "Pinecone":
            continue

        template = node_data.get("node", {}).get("template", {})
        index_name_field = template.get("index_name", {})
        namespace_field = template.get("namespace", {})

        index_name = index_name_field.get("value", "") if isinstance(index_name_field, dict) else str(index_name_field)
        uat_namespace = namespace_field.get("value", "") if isinstance(namespace_field, dict) else str(namespace_field)

        if not index_name:
            continue

        prod_namespace = f"{uat_namespace}_prod_v{deployment.version_number}" if uat_namespace else f"prod_v{deployment.version_number}"
        agent_name = getattr(deployment, "agent_name", "") or ""

        fail_row = VectorDBCatalogue(
            name=f"{index_name}/{prod_namespace} (FAILED)",
            description=f"Migration FAILED: {error_msg[:500]}",
            provider="Pinecone",
            deployment="SaaS",
            dimensions="",
            index_type="serverless",
            status="error",
            vector_count="0",
            is_custom=False,
            environment="prod",
            index_name=index_name,
            namespace=prod_namespace,
            agent_id=deployment.agent_id,
            agent_name=agent_name,
            migration_status="failed",
            migrated_at=now,
            vectors_copied=0,
            created_at=now,
            updated_at=now,
        )
        session.add(fail_row)

    await session.flush()
    logger.info(f"[PINECONE_MIGRATION] Tracked FAILED migration for deployment {deployment.id}")


async def _migrate_neo4j_for_prod(
    deployment: AgentDeploymentProd,
    session: DbSession,
) -> None:
    """Copy Neo4j graph data from UAT graph_kb_id to PROD graph_kb_id.

    For each Neo4jGraphStore node found in the snapshot:
      1. Read the UAT graph_kb_id
      2. Derive a PROD graph_kb_id: {original}_prod_v{version}
      3. Call graph-rag-service to copy entities, relationships, communities
      4. Update the snapshot with the new PROD graph_kb_id
    """
    from agentcore.services.graph_rag_service_client import (
        copy_graph_kb_via_service,
        is_service_configured,
    )

    if not is_service_configured():
        logger.warning("[NEO4J_MIGRATION] Graph RAG service not configured, skipping migration")
        return

    snapshot = deployment.agent_snapshot
    nodes = snapshot.get("nodes", [])
    updated = False

    for node in nodes:
        node_data = node.get("data", {})
        node_type = node_data.get("type", "")

        if node_type != "Neo4jGraphStore":
            continue

        template = node_data.get("node", {}).get("template", {})
        graph_kb_id_field = template.get("graph_kb_id", {})

        uat_graph_kb_id = (
            graph_kb_id_field.get("value", "default")
            if isinstance(graph_kb_id_field, dict)
            else str(graph_kb_id_field or "default")
        )

        prod_graph_kb_id = f"{uat_graph_kb_id}_prod_v{deployment.version_number}"

        logger.info(
            f"[NEO4J_MIGRATION] Copying graph_kb_id='{uat_graph_kb_id}' → '{prod_graph_kb_id}'"
        )

        try:
            result = copy_graph_kb_via_service(
                source_graph_kb_id=uat_graph_kb_id,
                target_graph_kb_id=prod_graph_kb_id,
            )
            entities = result.get("entities_copied", 0)
            rels = result.get("relationships_copied", 0)
            comms = result.get("communities_copied", 0)
            logger.info(
                f"[NEO4J_MIGRATION] Done: {entities} entities, {rels} rels, "
                f"{comms} communities copied to '{prod_graph_kb_id}'"
            )
        except Exception as copy_err:
            logger.error(f"[NEO4J_MIGRATION] Failed to copy graph_kb: {copy_err}")
            raise

        # Update snapshot with PROD graph_kb_id
        if isinstance(graph_kb_id_field, dict):
            graph_kb_id_field["value"] = prod_graph_kb_id
        else:
            template["graph_kb_id"] = {"value": prod_graph_kb_id}
        updated = True

    if updated:
        deployment.agent_snapshot = snapshot
        flag_modified(deployment, "agent_snapshot")
        session.add(deployment)
        await session.flush()
        logger.info(f"[NEO4J_MIGRATION] Snapshot updated for deployment {deployment.id}")


def _normalize_mcp_mode(value: str) -> str:
    normalized = str(value).strip().lower()
    if normalized not in {"sse", "stdio"}:
        raise HTTPException(status_code=400, detail=f"Unsupported mode '{value}'")
    return normalized


def _normalize_mcp_deployment_env(value: str) -> str:
    normalized = str(value).strip().upper()
    if normalized in {"TEST", "DEV"}:
        normalized = "UAT"
    if normalized not in {"UAT", "PROD"}:
        raise HTTPException(status_code=400, detail=f"Unsupported deployment_env '{value}'")
    return normalized


def _normalize_mcp_environment_list(values: list[str] | None, fallback: str | None = None) -> list[str]:
    normalized = [_normalize_mcp_deployment_env(v).lower() for v in (values or []) if v is not None]
    if not normalized and fallback is not None:
        normalized = [_normalize_mcp_deployment_env(fallback).lower()]
    ordered: list[str] = []
    for env in ("uat", "prod"):
        if env in normalized and env not in ordered:
            ordered.append(env)
    for env in normalized:
        if env not in ordered:
            ordered.append(env)
    return ordered


def _format_mcp_env_label(req: McpApprovalRequest) -> str:
    requested_envs = [str(v).lower() for v in (getattr(req, "requested_environments", None) or []) if v]
    if requested_envs:
        normalized = _normalize_mcp_environment_list(requested_envs)
    else:
        normalized = _normalize_mcp_environment_list([], req.deployment_env or "UAT")
    if not normalized:
        return "UAT"
    if "uat" in normalized and "prod" in normalized:
        return "UAT + PROD"
    return normalized[0].upper()


def _to_status_label(decision: ApprovalDecisionEnum | None) -> str:
    if decision is None:
        return "pending"
    if decision == ApprovalDecisionEnum.APPROVED:
        return "approved"
    return "rejected"


def _to_status_label_any(value: ApprovalDecisionEnum | str | None) -> str:
    if value is None:
        return "pending"
    normalized = str(value).strip().upper()
    if normalized == ApprovalDecisionEnum.APPROVED.value:
        return "approved"
    if normalized == ApprovalDecisionEnum.REJECTED.value:
        return "rejected"
    return "pending"


def _humanize_age(ts: datetime) -> str:
    now = datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    delta = now - ts
    total_sec = max(int(delta.total_seconds()), 0)
    if total_sec < 60:
        return f"{total_sec}s ago"
    if total_sec < 3600:
        return f"{total_sec // 60}m ago"
    if total_sec < 86400:
        return f"{total_sec // 3600}h ago"
    return f"{total_sec // 86400}d ago"


def _is_global_approver(user: CurrentActiveUser) -> bool:
    return str(getattr(user, "role", "")).lower() in {"root", "super_admin"}


async def _current_user_org_ids(session: DbSession, user_id: UUID) -> set[UUID]:
    rows = (
        await session.exec(
            select(UserOrganizationMembership.org_id).where(
                UserOrganizationMembership.user_id == user_id,
                UserOrganizationMembership.status == "active",
            )
        )
    ).all()
    return {r if isinstance(r, UUID) else r[0] for r in rows}


def _is_org_scoped_super_admin(current_user: CurrentActiveUser) -> bool:
    return str(getattr(current_user, "role", "")).lower() == "super_admin"


async def _designated_super_admin_org_ids(
    session: DbSession,
    current_user: CurrentActiveUser,
) -> set[UUID]:
    if not _is_org_scoped_super_admin(current_user):
        return set()
    org_ids = await _current_user_org_ids(session, current_user.id)
    if not org_ids:
        return set()
    allowed: set[UUID] = set()
    for org_id in org_ids:
        try:
            super_admin_id = await _resolve_super_admin_user_id(
                session=session,
                org_id=org_id,
            )
        except HTTPException:
            continue
        if super_admin_id == current_user.id:
            allowed.add(org_id)
    return allowed


async def _get_approval_for_action(
    *,
    session: DbSession,
    approval_or_agent_id: str,
    current_user: CurrentActiveUser,
) -> ApprovalRequest:
    req: ApprovalRequest | None = None
    target_uuid: UUID | None = None
    try:
        target_uuid = UUID(approval_or_agent_id)
    except Exception:
        target_uuid = None

    if target_uuid:
        req = (await session.exec(select(ApprovalRequest).where(ApprovalRequest.id == target_uuid))).first()

    if not req and target_uuid:
        stmt = (
            select(ApprovalRequest)
            .where(ApprovalRequest.agent_id == target_uuid)
            .where(ApprovalRequest.decision == None)  # noqa: E711
            .order_by(ApprovalRequest.requested_at.desc())
        )
        stmt = stmt.where(ApprovalRequest.request_to == current_user.id)
        req = (await session.exec(stmt)).first()

    if not req:
        raise HTTPException(status_code=404, detail="Approval request not found")

    if req.request_to != current_user.id:
        if _is_org_scoped_super_admin(current_user):
            org_ids = await _designated_super_admin_org_ids(session, current_user)
            if req.org_id and req.org_id in org_ids:
                return req
        raise HTTPException(status_code=403, detail="Not allowed to act on this approval")

    return req


async def _get_approval_for_view(
    *,
    session: DbSession,
    approval_or_agent_id: str,
    current_user: CurrentActiveUser,
) -> ApprovalRequest:
    req: ApprovalRequest | None = None
    target_uuid: UUID | None = None
    try:
        target_uuid = UUID(approval_or_agent_id)
    except Exception:
        target_uuid = None

    if not target_uuid:
        raise HTTPException(status_code=404, detail="Approval request not found")

    is_super = _is_org_scoped_super_admin(current_user)
    super_org_ids: set[UUID] | None = None
    if is_super:
        super_org_ids = await _designated_super_admin_org_ids(session, current_user)

    # Direct match by approval request id.
    req = (await session.exec(select(ApprovalRequest).where(ApprovalRequest.id == target_uuid))).first()
    if req:
        if req.request_to == current_user.id or req.requested_by == current_user.id:
            return req
        if is_super and super_org_ids and req.org_id in super_org_ids:
            return req
        raise HTTPException(status_code=403, detail="Not allowed to view this approval")

    # Fallback by agent id: latest request visible to user.
    stmt = select(ApprovalRequest).where(ApprovalRequest.agent_id == target_uuid).order_by(ApprovalRequest.requested_at.desc())
    if is_super and super_org_ids:
        stmt = stmt.where(
            (ApprovalRequest.request_to == current_user.id)
            | (ApprovalRequest.requested_by == current_user.id)
            | (ApprovalRequest.org_id.in_(list(super_org_ids)))
        )
    else:
        stmt = stmt.where(
            (ApprovalRequest.request_to == current_user.id) | (ApprovalRequest.requested_by == current_user.id)
        )
    req = (await session.exec(stmt)).first()
    if not req:
        raise HTTPException(status_code=404, detail="Approval request not found")
    return req


async def _get_mcp_approval_for_action(
    *,
    session: DbSession,
    approval_or_mcp_id: str,
    current_user: CurrentActiveUser,
) -> McpApprovalRequest:
    target_uuid: UUID | None = None
    try:
        target_uuid = UUID(approval_or_mcp_id)
    except Exception:
        target_uuid = None
    if not target_uuid:
        raise HTTPException(status_code=404, detail="MCP approval request not found")

    req = await session.get(McpApprovalRequest, target_uuid)
    if not req:
        stmt = (
            select(McpApprovalRequest)
            .where(McpApprovalRequest.mcp_id == target_uuid, McpApprovalRequest.decision == None)  # noqa: E711
            .order_by(McpApprovalRequest.requested_at.desc())
        )
        stmt = stmt.where(McpApprovalRequest.request_to == current_user.id)
        req = (await session.exec(stmt)).first()
    if not req:
        raise HTTPException(status_code=404, detail="MCP approval request not found")
    if req.request_to != current_user.id:
        raise HTTPException(status_code=403, detail="Not allowed to act on this approval")
    if req.requested_by == current_user.id:
        raise HTTPException(status_code=400, detail="No user can approve their own request")
    return req


async def _get_mcp_approval_for_view(
    *,
    session: DbSession,
    approval_or_mcp_id: str,
    current_user: CurrentActiveUser,
) -> McpApprovalRequest:
    target_uuid: UUID | None = None
    try:
        target_uuid = UUID(approval_or_mcp_id)
    except Exception:
        target_uuid = None
    if not target_uuid:
        raise HTTPException(status_code=404, detail="MCP approval request not found")

    is_super = _is_org_scoped_super_admin(current_user)
    super_org_ids: set[UUID] | None = None
    if is_super:
        super_org_ids = await _designated_super_admin_org_ids(session, current_user)

    req = await session.get(McpApprovalRequest, target_uuid)
    if not req:
        stmt = select(McpApprovalRequest).where(McpApprovalRequest.mcp_id == target_uuid).order_by(
            McpApprovalRequest.requested_at.desc()
        )
        if is_super and super_org_ids:
            stmt = stmt.where(
                (McpApprovalRequest.request_to == current_user.id)
                | (McpApprovalRequest.requested_by == current_user.id)
                | (McpApprovalRequest.org_id.in_(list(super_org_ids)))
            )
        else:
            stmt = stmt.where(
                (McpApprovalRequest.request_to == current_user.id) | (McpApprovalRequest.requested_by == current_user.id)
            )
        req = (await session.exec(stmt)).first()
    if not req:
        raise HTTPException(status_code=404, detail="MCP approval request not found")
    if req.request_to == current_user.id or req.requested_by == current_user.id:
        return req
    if is_super and super_org_ids and req.org_id in super_org_ids:
        return req
    raise HTTPException(status_code=403, detail="Not allowed to view this approval")


async def _get_model_approval_for_action(
    *,
    session: DbSession,
    approval_or_model_id: str,
    current_user: CurrentActiveUser,
) -> ModelApprovalRequest:
    target_uuid: UUID | None = None
    try:
        target_uuid = UUID(approval_or_model_id)
    except Exception:
        target_uuid = None
    if not target_uuid:
        raise HTTPException(status_code=404, detail="Model approval request not found")

    req = await session.get(ModelApprovalRequest, target_uuid)
    if not req:
        stmt = (
            select(ModelApprovalRequest)
            .where(ModelApprovalRequest.model_id == target_uuid, ModelApprovalRequest.decision == None)  # noqa: E711
            .order_by(ModelApprovalRequest.requested_at.desc())
        )
        # Model approvals are strictly routed â€” only assigned approver can act
        stmt = stmt.where(ModelApprovalRequest.request_to == current_user.id)
        req = (await session.exec(stmt)).first()
    if not req:
        raise HTTPException(status_code=404, detail="Model approval request not found")
    if req.request_to != current_user.id:
        raise HTTPException(status_code=403, detail="Not allowed to act on this approval")
    if req.requested_by == current_user.id:
        raise HTTPException(status_code=400, detail="No user can approve their own request")
    return req


async def _get_model_approval_for_view(
    *,
    session: DbSession,
    approval_or_model_id: str,
    current_user: CurrentActiveUser,
) -> ModelApprovalRequest:
    target_uuid: UUID | None = None
    try:
        target_uuid = UUID(approval_or_model_id)
    except Exception:
        target_uuid = None
    if not target_uuid:
        raise HTTPException(status_code=404, detail="Model approval request not found")

    is_super = _is_org_scoped_super_admin(current_user)
    super_org_ids: set[UUID] | None = None
    if is_super:
        super_org_ids = await _designated_super_admin_org_ids(session, current_user)

    req = await session.get(ModelApprovalRequest, target_uuid)
    if not req:
        stmt = select(ModelApprovalRequest).where(ModelApprovalRequest.model_id == target_uuid).order_by(
            ModelApprovalRequest.requested_at.desc()
        )
        if is_super and super_org_ids:
            stmt = stmt.where(
                (ModelApprovalRequest.request_to == current_user.id)
                | (ModelApprovalRequest.requested_by == current_user.id)
                | (ModelApprovalRequest.org_id.in_(list(super_org_ids)))
            )
        else:
            stmt = stmt.where(
                (ModelApprovalRequest.request_to == current_user.id)
                | (ModelApprovalRequest.requested_by == current_user.id)
            )
        req = (await session.exec(stmt)).first()
    if not req:
        raise HTTPException(status_code=404, detail="Model approval request not found")
    if req.request_to == current_user.id or req.requested_by == current_user.id:
        return req
    if is_super and super_org_ids and req.org_id in super_org_ids:
        return req
    raise HTTPException(status_code=403, detail="Not allowed to view this approval")


def _normalize_model_env_list(values: list[str] | None, fallback: str | None = None) -> list[str]:
    normalized = [str(v).strip().lower() for v in (values or []) if v]
    if not normalized and fallback:
        normalized = [str(fallback).strip().lower()]
    ordered: list[str] = []
    for env in (ModelEnvironment.UAT.value, ModelEnvironment.PROD.value):
        if env in normalized and env not in ordered:
            ordered.append(env)
    for env in normalized:
        if env not in ordered:
            ordered.append(env)
    return ordered


def _resolve_model_environments(row: ModelRegistry) -> list[str]:
    envs = [str(v).strip().lower() for v in (getattr(row, "environments", None) or []) if v]
    if envs:
        return _normalize_model_env_list(envs)
    return _normalize_model_env_list([getattr(row, "environment", ModelEnvironment.UAT.value)])


def _next_model_environment(envs: list[str]) -> str | None:
    normalized = _normalize_model_env_list(envs)
    if ModelEnvironment.UAT.value in normalized and ModelEnvironment.PROD.value not in normalized:
        return ModelEnvironment.PROD.value
    return None


def _format_model_env_label(row: ModelRegistry) -> str:
    envs = _resolve_model_environments(row)
    if not envs:
        return ModelEnvironment.UAT.value.upper()
    if len(envs) > 1:
        return "UAT + PROD"
    return envs[0].upper()


def _format_model_env_label_for_request(row: ModelRegistry, req: ModelApprovalRequest) -> str:
    requested_envs = [str(v).lower() for v in (getattr(req, "requested_environments", None) or []) if v]
    if ModelEnvironment.UAT.value in requested_envs and ModelEnvironment.PROD.value in requested_envs:
        return "UAT + PROD"
    if req.request_type == ModelApprovalRequestType.CREATE:
        envs = _resolve_model_environments(row)
        if ModelEnvironment.UAT.value in envs and ModelEnvironment.PROD.value in envs:
            return "UAT + PROD"
    if str(req.target_environment or "").lower() == ModelEnvironment.PROD.value:
        envs = _resolve_model_environments(row)
        if ModelEnvironment.UAT.value in envs:
            return "UAT + PROD"
        return ModelEnvironment.PROD.value.upper()
    return _format_model_env_label(row)


async def _resolve_super_admin_user_id(
    *,
    session: DbSession,
    org_id: UUID | None,
    exclude_user_id: UUID | None = None,
) -> UUID:
    if not org_id:
        raise HTTPException(status_code=400, detail="Organization id is required for super admin resolution")
    stmt = (
        select(User)
        .join(UserOrganizationMembership, UserOrganizationMembership.user_id == User.id)
        .join(Role, Role.id == UserOrganizationMembership.role_id)
        .where(
            UserOrganizationMembership.org_id == org_id,
            UserOrganizationMembership.status == "active",
            func.lower(Role.name) == "super_admin",
        )
        .order_by(User.create_at.asc())
    )
    rows = (await session.exec(stmt)).all()
    for row in rows:
        if exclude_user_id and row.id == exclude_user_id:
            continue
        return row.id
    raise HTTPException(status_code=400, detail="No Super Admin approver available")


async def _resolve_user_primary_dept(
    *,
    session: DbSession,
    user_id: UUID | None,
) -> UUID | None:
    if not user_id:
        return None
    dept_rows = (
        await session.exec(
            select(UserDepartmentMembership.department_id).where(
                UserDepartmentMembership.user_id == user_id,
                UserDepartmentMembership.status == "active",
            )
        )
    ).all()
    if not dept_rows:
        return None
    dept_ids = [r if isinstance(r, UUID) else r[0] for r in dept_rows]
    return sorted(dept_ids, key=lambda x: str(x))[0]


async def _resolve_department_admin_user_id(
    *,
    session: DbSession,
    dept_id: UUID | None,
    requested_by: UUID | None,
) -> UUID:
    resolved_dept_id = dept_id or await _resolve_user_primary_dept(session=session, user_id=requested_by)
    if not resolved_dept_id:
        raise HTTPException(status_code=400, detail="Department id is required for department admin resolution")
    dept = await session.get(Department, resolved_dept_id)
    if not dept or not dept.admin_user_id:
        raise HTTPException(status_code=400, detail="No department admin configured for requester department")
    return dept.admin_user_id


async def _resolve_model_visibility_approver(
    *,
    session: DbSession,
    org_id: UUID | None,
    dept_id: UUID | None,
    visibility_scope: str,
    requested_by: UUID | None,
) -> UUID:
    normalized_visibility = str(visibility_scope or "").strip().lower()
    if normalized_visibility == ModelVisibilityScope.ORGANIZATION.value:
        return await _resolve_super_admin_user_id(session=session, org_id=org_id, exclude_user_id=requested_by)
    return await _resolve_department_admin_user_id(
        session=session,
        dept_id=dept_id,
        requested_by=requested_by,
    )


async def _append_model_audit(
    *,
    session: DbSession,
    model_id: UUID,
    actor_id: UUID | None,
    action: str,
    from_environment: str | None = None,
    to_environment: str | None = None,
    from_visibility: str | None = None,
    to_visibility: str | None = None,
    message: str | None = None,
    details: dict | None = None,
    org_id: UUID | None = None,
    dept_id: UUID | None = None,
) -> None:
    session.add(
        ModelAuditLog(
            model_id=model_id,
            actor_id=actor_id,
            action=action,
            from_environment=from_environment,
            to_environment=to_environment,
            from_visibility=from_visibility,
            to_visibility=to_visibility,
            message=message,
            details=details,
            org_id=org_id,
            dept_id=dept_id,
        )
    )


async def _collect_attachment_metadata(
    *,
    files: list[UploadFile] | None,
    now: datetime,
) -> list[dict]:
    uploaded_files: list[dict] = []
    for file in files or []:
        contents = await file.read()
        uploaded_files.append(
            {
                "filename": file.filename,
                "size": len(contents),
                "uploadedAt": now.isoformat(),
            }
        )
    return uploaded_files


@router.get(
    "/prod-deployments/{deployment_id}/handoff",
    response_model=ProdPromotionHandoffResponse,
    status_code=200,
)
async def get_prod_promotion_handoff(
    deployment_id: UUID,
    *,
    session: DbSession,
    current_user: CurrentActiveUser,
) -> ProdPromotionHandoffResponse:
    """Return PROD deployment handoff payload for downstream backend processing."""
    record = await session.get(AgentDeploymentProd, deployment_id)
    if not record:
        raise HTTPException(status_code=404, detail="PROD deployment not found")

    return _build_prod_promotion_handoff_payload(record)


@router.get("", response_model=list[ApprovalAgent])
async def get_approvals(
    *,
    session: DbSession,
    current_user: CurrentActiveUser,
) -> list[ApprovalAgent]:
    """Fetch approvals for the current approver."""
    try:
        stmt = select(ApprovalRequest).order_by(ApprovalRequest.requested_at.desc())
        if _is_org_scoped_super_admin(current_user):
            org_ids = await _designated_super_admin_org_ids(session, current_user)
            stmt = stmt.where(ApprovalRequest.org_id.in_(list(org_ids)) if org_ids else False)
        else:
            stmt = stmt.where(ApprovalRequest.request_to == current_user.id)
        rows = (await session.exec(stmt)).all()

        payload: list[ApprovalAgent] = []
        for req in rows:
            deployment = await session.get(AgentDeploymentProd, req.deployment_id)
            if not deployment:
                continue

            requester = await session.get(User, req.requested_by)
            approver_id = req.reviewed_by or req.request_to
            approver = await session.get(User, approver_id) if approver_id else None
            agent = await session.get(Agent, req.agent_id)
            project_name = ""
            if agent and agent.project_id:
                folder = await session.get(Folder, agent.project_id)
                if folder:
                    project_name = folder.name

            title = deployment.agent_name or (agent.name if agent else "Untitled Agent")
            description = deployment.agent_description or req.publish_description or ""
            submitter_name = (
                requester.display_name
                if requester and requester.display_name
                else (requester.username if requester else "Unknown")
            )
            submitter_email = (
                requester.email
                if requester and requester.email
                else (
                    requester.username
                    if requester and requester.username and "@" in requester.username
                    else None
                )
            )

            payload.append(
                ApprovalAgent(
                    id=str(req.id),
                    entityType="agent",
                    title=title,
                    status=_to_status_label(req.decision),
                    description=description,
                    submittedBy=SubmittedBy(name=submitter_name, avatar=None, email=submitter_email),
                    approver=_build_approver_info(approver),
                    project=project_name,
                    submitted=(
                        req.updated_at.replace(tzinfo=timezone.utc).isoformat()
                        if req.updated_at.tzinfo is None
                        else req.updated_at.isoformat()
                    ),
                    version=f"v{deployment.version_number}",
                    recentChanges="",  # intentionally blank for now
                )
            )

        mcp_stmt = select(McpApprovalRequest).order_by(McpApprovalRequest.requested_at.desc())
        mcp_stmt = mcp_stmt.where(McpApprovalRequest.request_to == current_user.id)
        mcp_rows = (await session.exec(mcp_stmt)).all()
        for req in mcp_rows:
            row = await session.get(McpRegistry, req.mcp_id)
            if not row:
                continue
            requester = await session.get(User, req.requested_by)
            approver_id = req.reviewed_by or req.request_to
            approver = await session.get(User, approver_id) if approver_id else None
            dept_name = ""
            if req.dept_id:
                dept = await session.get(Department, req.dept_id)
                if dept:
                    dept_name = getattr(dept, "name", "") or ""
            submitter_name = (
                requester.display_name
                if requester and requester.display_name
                else (requester.username if requester else "Unknown")
            )
            submitter_email = (
                requester.email
                if requester and requester.email
                else (
                    requester.username
                    if requester and requester.username and "@" in requester.username
                    else None
                )
            )
            submitted_at = req.requested_at
            deployment_env = _format_mcp_env_label(req)
            payload.append(
                ApprovalAgent(
                    id=str(req.id),
                    entityType="mcp",
                    title=row.server_name,
                    status=_to_status_label_any(req.decision),
                    description=row.description or "",
                    submittedBy=SubmittedBy(name=submitter_name, avatar=None, email=submitter_email),
                    approver=_build_approver_info(approver),
                    project="",
                    visibility=_format_mcp_visibility_label(
                        req.requested_visibility or row.visibility,
                        req.requested_public_scope or row.public_scope,
                    ),
                    submitted=(
                        submitted_at.replace(tzinfo=timezone.utc).isoformat()
                        if submitted_at.tzinfo is None
                        else submitted_at.isoformat()
                    ),
                    version=f"{deployment_env} / {(row.mode or 'mcp').upper()}",
                    recentChanges="",
                )
            )
        model_stmt = select(ModelApprovalRequest).order_by(ModelApprovalRequest.requested_at.desc())
        # Model approvals are strictly routed — only assigned approver sees them.
        model_stmt = model_stmt.where(ModelApprovalRequest.request_to == current_user.id)
        model_rows = (await session.exec(model_stmt)).all()
        for req in model_rows:
            row = await session.get(ModelRegistry, req.model_id)
            if not row:
                continue
            requester = await session.get(User, req.requested_by)
            approver_id = req.reviewed_by or req.request_to
            approver = await session.get(User, approver_id) if approver_id else None
            dept_name = ""
            if req.dept_id:
                dept = await session.get(Department, req.dept_id)
                if dept:
                    dept_name = getattr(dept, "name", "") or ""
            submitter_name = (
                requester.display_name
                if requester and requester.display_name
                else (requester.username if requester else "Unknown")
            )
            submitter_email = (
                requester.email
                if requester and requester.email
                else (
                    requester.username
                    if requester and requester.username and "@" in requester.username
                    else None
                )
            )
            submitted_at = req.requested_at
            # Extract project name from provider_config.request_meta
            provider_cfg = row.provider_config if isinstance(row.provider_config, dict) else {}
            request_meta = provider_cfg.get("request_meta", {})
            model_project_name = request_meta.get("project_name", "") or ""
            payload.append(
                ApprovalAgent(
                    id=str(req.id),
                    entityType="model",
                    title=row.display_name,
                    status=_to_status_label_any(req.decision),
                    description=row.description or "",
                    submittedBy=SubmittedBy(name=submitter_name, avatar=None, email=submitter_email),
                    approver=_build_approver_info(approver),
                    project=model_project_name,
                    visibility=_format_model_visibility_label(req.visibility_requested or row.visibility_scope),
                    submitted=(
                        submitted_at.replace(tzinfo=timezone.utc).isoformat()
                        if submitted_at.tzinfo is None
                        else submitted_at.isoformat()
                    ),
                    version=f"{row.model_name} ({_format_model_env_label_for_request(row, req)})",
                    recentChanges=row.description or "",
                )
            )
        payload.sort(key=lambda item: item.submitted, reverse=True)
        return payload
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching approvals: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch approvals",
        ) from e


@router.post("/{agent_id}/approve", response_model=ApprovalResponse)
async def approve_agent(
    agent_id: str,
    *,
    session: DbSession,
    current_user: CurrentActiveUser,
    comments: str = Form(default=""),
    attachments: list[UploadFile] | None = File(default=None),
) -> ApprovalResponse:
    """Approve a pending deployment request."""
    now = datetime.now(timezone.utc)
    attachment_count = len(attachments or [])
    logger.info(
        f"[APPROVE_REQUEST] target={agent_id} approver={getattr(current_user, 'id', None)} "
        f"comments_len={len((comments or '').strip())} attachments={attachment_count}",
    )
    req: ApprovalRequest | None = None
    mcp_req: McpApprovalRequest | None = None
    model_req: ModelApprovalRequest | None = None
    try:
        req = await _get_approval_for_action(
            session=session,
            approval_or_agent_id=agent_id,
            current_user=current_user,
        )
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        try:
            mcp_req = await _get_mcp_approval_for_action(
                session=session,
                approval_or_mcp_id=agent_id,
                current_user=current_user,
            )
        except HTTPException as mcp_exc:
            if mcp_exc.status_code != 404:
                raise
            model_req = await _get_model_approval_for_action(
                session=session,
                approval_or_model_id=agent_id,
                current_user=current_user,
            )

    if mcp_req is not None:
        if mcp_req.decision is not None:
            raise HTTPException(status_code=400, detail="MCP approval request already finalized")
        mcp_row = await session.get(McpRegistry, mcp_req.mcp_id)
        if not mcp_row:
            raise HTTPException(status_code=404, detail="Linked MCP server not found")
        uploaded_files = await _collect_attachment_metadata(files=attachments, now=now)
        if not comments.strip() and not uploaded_files:
            raise HTTPException(
                status_code=400,
                detail="Either comments or attachments are required for approval",
            )
        mcp_req.decision = ApprovalDecisionEnum.APPROVED
        mcp_req.justification = comments.strip() if comments else None
        mcp_req.reviewed_by = current_user.id
        existing = mcp_req.file_path if isinstance(mcp_req.file_path, dict) else {}
        existing_files = existing.get("files", [])
        if uploaded_files:
            existing["files"] = [*existing_files, *uploaded_files]
            mcp_req.file_path = existing
        mcp_req.reviewed_at = now
        mcp_req.updated_at = now

        mcp_row.approval_status = "approved"
        mcp_row.review_comments = mcp_req.justification
        mcp_row.review_attachments = mcp_req.file_path
        mcp_row.reviewed_at = now
        mcp_row.reviewed_by = current_user.id
        mcp_row.is_active = True
        mcp_row.status = "connected"
        mcp_row.updated_at = now

        # Apply any pending visibility changes from the request.
        if mcp_req.requested_visibility:
            mcp_row.visibility = mcp_req.requested_visibility
            mcp_row.public_scope = mcp_req.requested_public_scope
            mcp_row.org_id = mcp_req.requested_org_id
            mcp_row.dept_id = mcp_req.requested_dept_id
            mcp_row.public_dept_ids = (
                [str(v) for v in (mcp_req.requested_public_dept_ids or [])] or None
            )
            try:
                await update_mcp_server_via_service(
                    str(mcp_row.id),
                    {
                        "visibility": mcp_row.visibility,
                        "public_scope": mcp_row.public_scope,
                        "org_id": str(mcp_row.org_id) if mcp_row.org_id else None,
                        "dept_id": str(mcp_row.dept_id) if mcp_row.dept_id else None,
                        "public_dept_ids": mcp_row.public_dept_ids or [],
                    },
                )
            except Exception as reg_err:
                logger.warning("MCP registry sync failed after approval %s: %s", mcp_req.id, reg_err)
        session.add(mcp_req)
        session.add(mcp_row)
        if mcp_req.requested_by and mcp_req.requested_by != current_user.id:
            await upsert_approval_notification(
                session,
                recipient_user_id=mcp_req.requested_by,
                entity_type="mcp_request_result",
                entity_id=str(mcp_req.id),
                title=f'MCP server "{mcp_row.server_name}" was approved.',
                link="/approval",
            )
        await session.commit()
        approver_name = getattr(current_user, "username", None)
        response_payload = ApprovalResponse(
            success=True,
            message="MCP request approved successfully",
            agentId=str(mcp_req.id),
            newStatus="approved",
            timestamp=now.isoformat(),
            approvedBy=approver_name,
        )
        logger.info(f"[APPROVE_RESPONSE] {response_payload.model_dump()}")
        return response_payload

    if model_req is not None:
        if model_req.decision is not None:
            raise HTTPException(status_code=400, detail="Model approval request already finalized")
        model_row = await session.get(ModelRegistry, model_req.model_id)
        if not model_row:
            raise HTTPException(status_code=404, detail="Linked model not found")
        uploaded_files = await _collect_attachment_metadata(files=attachments, now=now)
        if not comments.strip() and not uploaded_files:
            raise HTTPException(
                status_code=400,
                detail="Either comments or attachments are required for approval",
            )
        model_req.decision = ApprovalDecisionEnum.APPROVED
        model_req.justification = comments.strip() if comments else None
        model_req.reviewed_by = current_user.id
        existing = model_req.file_path if isinstance(model_req.file_path, dict) else {}
        existing_files = existing.get("files", [])
        if uploaded_files:
            existing["files"] = [*existing_files, *uploaded_files]
            model_req.file_path = existing
        model_req.reviewed_at = now
        model_req.updated_at = now

        current_envs = _resolve_model_environments(model_row)
        current_env = current_envs[0] if current_envs else ModelEnvironment.UAT.value
        current_visibility = str(model_row.visibility_scope or ModelVisibilityScope.PRIVATE.value).lower()
        model_row.review_comments = model_req.justification
        model_row.review_attachments = model_req.file_path
        model_row.reviewed_at = now
        model_row.reviewed_by = current_user.id
        model_row.updated_at = now

        if model_req.request_type == ModelApprovalRequestType.PROMOTE:
            expected_next = _next_model_environment(current_envs)
            if not expected_next or str(model_req.target_environment).lower() != expected_next:
                raise HTTPException(
                    status_code=400,
                    detail="Invalid promotion path. Backend enforces UAT->PROD only.",
                )
            target_env = str(model_req.target_environment).lower()
            model_row.environments = _normalize_model_env_list([*current_envs, target_env])
            model_row.environment = model_row.environments[0] if model_row.environments else target_env
            model_row.approval_status = ModelApprovalStatus.APPROVED.value
            model_row.is_active = True
            model_row.request_to = None
            model_row.requested_at = None
            model_row.requested_by = model_req.requested_by
            await _append_model_audit(
                session=session,
                model_id=model_row.id,
                actor_id=current_user.id,
                action="model.promotion.approved",
                from_environment=",".join(current_envs),
                to_environment=",".join(model_row.environments or []),
                message="Model promotion approved",
                org_id=model_row.org_id,
                dept_id=model_row.dept_id,
            )

            if model_req.request_type == ModelApprovalRequestType.VISIBILITY:
                model_row.visibility_scope = str(model_req.visibility_requested).lower()
                if model_req.visibility_requested == ModelVisibilityScope.DEPARTMENT.value:
                    if model_req.org_id:
                        model_row.org_id = model_req.org_id
                    if model_req.dept_id:
                        model_row.dept_id = model_req.dept_id
                    if getattr(model_req, "public_dept_ids", None):
                        model_row.public_dept_ids = list(model_req.public_dept_ids or [])
                elif model_req.visibility_requested == ModelVisibilityScope.ORGANIZATION.value:
                    if model_req.org_id:
                        model_row.org_id = model_req.org_id
                    model_row.dept_id = None
                    model_row.public_dept_ids = None
                else:
                    if model_req.org_id:
                        model_row.org_id = model_req.org_id
                    if model_req.dept_id:
                        model_row.dept_id = model_req.dept_id
                    model_row.public_dept_ids = None
            model_row.approval_status = ModelApprovalStatus.APPROVED.value
            model_row.is_active = True
            model_row.request_to = None
            model_row.requested_at = None
            await _append_model_audit(
                session=session,
                model_id=model_row.id,
                actor_id=current_user.id,
                action="model.visibility.approved",
                from_visibility=current_visibility,
                to_visibility=model_row.visibility_scope,
                message="Model visibility change approved",
                org_id=model_row.org_id,
                dept_id=model_row.dept_id,
            )
        else:
            # CREATE type: apply both visibility and environment changes
            model_row.visibility_scope = str(model_req.visibility_requested or current_visibility).lower()
            target_env = str(model_req.target_environment or current_env).lower()
            model_row.environments = _normalize_model_env_list(
                getattr(model_row, "environments", None) or [target_env], fallback=target_env
            )
            model_row.environment = model_row.environments[0] if model_row.environments else (target_env or current_env)

            model_row.approval_status = ModelApprovalStatus.APPROVED.value
            model_row.is_active = True
            model_row.request_to = None
            model_row.requested_at = None

            await _append_model_audit(
                session=session,
                model_id=model_row.id,
                actor_id=current_user.id,
                action="model.create.approved",
                from_environment=",".join(current_envs),
                to_environment=",".join(model_row.environments or []),
                from_visibility=current_visibility,
                to_visibility=model_row.visibility_scope,
                message="Model onboarding request approved",
                org_id=model_row.org_id,
                dept_id=model_row.dept_id,
            )

            # CREATE requests are completed in a single approval step.
        session.add(model_req)
        session.add(model_row)
        if model_req.requested_by and model_req.requested_by != current_user.id:
            model_label = model_row.display_name or model_row.model_name or "Model request"
            await upsert_approval_notification(
                session,
                recipient_user_id=model_req.requested_by,
                entity_type="model_request_result",
                entity_id=str(model_req.id),
                title=f'Model "{model_label}" was approved.',
                link="/approval",
            )
        await session.commit()
        approver_name = getattr(current_user, "username", None)
        response_payload = ApprovalResponse(
            success=True,
            message="Model request approved successfully",
            agentId=str(model_req.id),
            newStatus="approved",
            timestamp=now.isoformat(),
            approvedBy=approver_name,
        )
        logger.info(f"[APPROVE_RESPONSE] {response_payload.model_dump()}")
        return response_payload

    assert req is not None
    if req.decision is not None:
        raise HTTPException(status_code=400, detail="Approval request already finalized")

    deployment = await session.get(AgentDeploymentProd, req.deployment_id)
    if not deployment:
        raise HTTPException(status_code=404, detail="Linked deployment not found")

    uploaded_files = await _collect_attachment_metadata(files=attachments, now=now)
    if not comments.strip() and not uploaded_files:
        raise HTTPException(
            status_code=400,
            detail="Either comments or attachments are required for approval",
        )

    req.decision = ApprovalDecisionEnum.APPROVED
    req.justification = comments.strip() if comments else None
    req.reviewed_by = current_user.id
    existing = req.file_path if isinstance(req.file_path, dict) else {}
    existing_files = existing.get("files", [])
    if uploaded_files:
        existing["files"] = [*existing_files, *uploaded_files]
        req.file_path = existing
    req.reviewed_at = now
    req.updated_at = now
    session.add(req)

    deployment.status = DeploymentPRODStatusEnum.PUBLISHED
    deployment.lifecycle_step = ProdDeploymentLifecycleEnum.PUBLISHED
    deployment.is_active = True
    deployment.approval_id = req.id
    deployment.updated_at = now
    session.add(deployment)

    # Update agent lifecycle_status to PUBLISHED
    agent = await session.get(Agent, deployment.agent_id)
    if agent:
        agent.lifecycle_status = LifecycleStatusEnum.PUBLISHED
        session.add(agent)

    # Shadow deployment: keep previous versions active so
    # multiple versions can run side-by-side.

    if req.requested_by and req.requested_by != current_user.id:
        await upsert_approval_notification(
            session,
            recipient_user_id=req.requested_by,
            entity_type="agent_publish_result",
            entity_id=str(req.id),
            title=f'Agent "{deployment.agent_name}" was approved.',
            link="/approval",
        )

    await session.commit()

    # ─── Create agent bundle rows from the frozen snapshot ──
    try:
        from agentcore.api.publish import _extract_and_create_bundles
        await session.refresh(deployment)
        if deployment.agent_snapshot:
            bundles = await _extract_and_create_bundles(
                session,
                snapshot=deployment.agent_snapshot,
                agent_id=deployment.agent_id,
                org_id=deployment.org_id,
                dept_id=deployment.dept_id,
                deployment_id=deployment.id,
                deployment_env=DeploymentEnvEnum.PROD,
                created_by=current_user.id,
            )
            if bundles:
                await session.commit()
                logger.info(f"Created {len(bundles)} bundle(s) for approved PROD deploy {deployment.id}")
            else:
                logger.info(f"No bundles extracted from snapshot for approved PROD deploy {deployment.id}")
    except Exception as bundle_err:
        logger.warning(f"Bundle extraction failed for approved PROD deploy {deployment.id}: {bundle_err}", exc_info=True)

    # ─── Auto-generate API key for this approved PROD deployment ──
    generated_api_key: str | None = None
    try:
        plaintext_key, key_hash, key_prefix = generate_agent_api_key()
        api_key_record = AgentApiKey(
            agent_id=deployment.agent_id,
            deployment_id=deployment.id,
            version=f"v{deployment.version_number}",
            environment="prod",
            key_hash=key_hash,
            key_prefix=key_prefix,
            is_active=True,
            created_by=current_user.id,
            created_at=datetime.now(timezone.utc),
        )
        session.add(api_key_record)
        await session.commit()
        generated_api_key = plaintext_key
        logger.info(f"Generated API key (prefix={key_prefix}) for approved PROD deploy {deployment.id}")
    except Exception as key_err:
        logger.warning(f"API key generation failed after approval {req.id}: {key_err}")

    try:
        await sync_agent_registry(
            session,
            agent_id=deployment.agent_id,
            org_id=deployment.org_id,
            acted_by=current_user.id,
            deployment_env=RegistryDeploymentEnvEnum.PROD,
        )
        await session.commit()
    except Exception as reg_err:
        logger.warning(f"Registry sync failed after approval {req.id}: {reg_err}")

    # Sync FileTrigger nodes â†’ auto-create trigger_config entries
    if deployment.agent_snapshot:
        try:
            from agentcore.services.deps import get_trigger_service
            trigger_svc = get_trigger_service()
            await trigger_svc.sync_folder_monitors_for_agent(
                session=session,
                agent_id=deployment.agent_id,
                environment="prod",
                version=f"v{deployment.version_number}",
                deployment_id=deployment.id,
                flow_data=deployment.agent_snapshot,
                created_by=req.requested_by,
            )
        except Exception as fm_err:
            logger.warning(f"FileTrigger sync failed after approval {req.id}: {fm_err}")

    # â”€â”€ Promote guardrails used by this agent to PROD â”€â”€
    guardrail_promotions: list[GuardrailPromotionResult] = []
    if deployment.agent_snapshot:
        try:
            guardrail_promotions = await _promote_guardrails_for_deployment(
                snapshot=deployment.agent_snapshot,
                promoted_by=current_user.id,
            )
        except Exception as guardrail_err:
            logger.warning(
                f"[GUARDRAIL_PROMOTION] Failed to promote guardrails for PROD deploy "
                f"{deployment.id}: {guardrail_err}",
                exc_info=True,
            )

    # â”€â”€â”€ Publish notification (DB-verified) â”€â”€
    try:
        from agentcore.api.publish import _notify_publish_event
        await _notify_publish_event(
            session,
            agent_id=deployment.agent_id,
            agent_name=deployment.agent_name,
            environment="prod",
            version_number=deployment.version_number,
            publish_id=deployment.id,
            published_by=deployment.deployed_by,
            published_at=deployment.deployed_at,
        )
    except Exception as notify_err:
        logger.warning(f"Publish notification failed after approval {req.id}: {notify_err}")

    # ─── Data migration (Pinecone + Neo4j, UAT → PROD) ──
    # Separate flags so the user knows exactly which migration failed.
    pinecone_migration_failed = False
    pinecone_error_msg = ""
    neo4j_migration_failed = False
    neo4j_error_msg = ""

    logger.info(
        f"[DATA_MIGRATION] deployment={deployment.id} "
        f"has_snapshot={bool(deployment.agent_snapshot)} "
        f"promoted_from_uat_id={deployment.promoted_from_uat_id}"
    )

    if deployment.agent_snapshot:
        # --- Pinecone migration ---
        try:
            await _migrate_pinecone_for_prod(deployment=deployment, session=session)
        except Exception as pc_err:
            logger.error(f"[DATA_MIGRATION] Pinecone migration failed: {pc_err}")
            pinecone_migration_failed = True
            pinecone_error_msg = str(pc_err)
            # Track the failure in VDB catalogue
            try:
                await _track_pinecone_migration_failure(
                    deployment=deployment, session=session, error_msg=pinecone_error_msg,
                )
            except Exception as track_err:
                logger.warning(f"[DATA_MIGRATION] Failed to track Pinecone failure: {track_err}")

        # --- Neo4j migration ---
        try:
            await _migrate_neo4j_for_prod(deployment=deployment, session=session)
        except Exception as neo_err:
            logger.error(f"[DATA_MIGRATION] Neo4j migration failed: {neo_err}")
            neo4j_migration_failed = True
            neo4j_error_msg = str(neo_err)

        # Commit or rollback based on results
        if pinecone_migration_failed or neo4j_migration_failed:
            await session.rollback()
            deployment.status = DeploymentPRODStatusEnum.ERROR
            deployment.updated_at = datetime.now(timezone.utc)
            session.add(deployment)
            await session.commit()
            logger.error(
                f"[DATA_MIGRATION] Deployment {deployment.id} marked as ERROR "
                f"due to migration failure. Agent will NOT be active in PROD."
            )
        else:
            await session.commit()

    # Build detailed error message showing which migrations failed
    migration_errors = []
    if pinecone_migration_failed:
        migration_errors.append(f"Pinecone VDB migration failed: {pinecone_error_msg}")
    if neo4j_migration_failed:
        migration_errors.append(f"Neo4j graph migration failed: {neo4j_error_msg}")

    if migration_errors:
        raise HTTPException(
            status_code=500,
            detail=(
                f"Agent approved but data migration failed. "
                f"{' | '.join(migration_errors)}. "
                f"Deployment {deployment.id} has been marked as ERROR and will not serve in PROD. "
                f"Please retry the approval or contact support."
            ),
        )
    # ─── HTTP notify (only if guardrail promotion succeeded) ──
    guardrails_ready = all(g.ready for g in guardrail_promotions) if guardrail_promotions else True
    rag_ready = not pinecone_migration_failed and not neo4j_migration_failed
    if guardrails_ready and rag_ready:
        try:
            import httpx
            from agentcore.services.deps import get_settings_service
            settings = get_settings_service().settings
            base_url = f"http://{settings.host}:{settings.port}"
            payload = {
                "agent_id": str(deployment.agent_id),
                "environment": "prod",
                "version_number": str(deployment.version_number),
                "deployment_id": str(deployment.id),
            }
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(f"{base_url}/api/publish/notify", json=payload)
                resp.raise_for_status()
                verified = resp.json()
            logger.info(
                f"[APPROVAL_NOTIFY] API triggered: agent={verified.get('agent_name')} "
                f"deployment_id={verified.get('deployment_id')} "
                f"version={verified.get('version_number')} "
                f"status={verified.get('status')} is_active={verified.get('is_active')}",
            )
        except Exception as notify_err:
            logger.warning(f"Post-approval notify API failed for approval {req.id}: {notify_err}")
    else:
        error_reasons = []
        if not guardrails_ready:
            failed = [g.uat_guardrail_id for g in guardrail_promotions if not g.ready]
            error_reasons.append(f"Guardrail promotion not ready: {failed}")
        if not rag_ready:
            if pinecone_migration_failed:
                error_reasons.append(f"Pinecone VDB migration failed: {pinecone_error_msg}")
            if neo4j_migration_failed:
                error_reasons.append(f"Neo4j graph migration failed: {neo4j_error_msg}")
        logger.error(
            f"[APPROVAL_NOTIFY] Failed for approval {req.id}. "
            f"Reasons: {' | '.join(error_reasons)}"
        )
        raise HTTPException(
            status_code=500,
            detail=(
                f"Agent approved but promotion checks failed. "
                f"{' | '.join(error_reasons)}. "
                f"Deployment {deployment.id} will not be notified for PROD."
            ),
        )

    # Trigger handoff payload only for approved AGENT promotions (never on reject).
    try:
        handoff_payload = _build_prod_promotion_handoff_payload(
            deployment, guardrail_promotions=guardrail_promotions,
        )
        logger.info(
            f"[PROD_PROMOTION_HANDOFF_TRIGGER] {handoff_payload.model_dump()}",
        )
    except Exception as handoff_err:
        logger.warning(f"Handoff payload trigger failed after approval {req.id}: {handoff_err}")

    approver_name = getattr(current_user, "username", None)
    response_payload = ApprovalResponse(
        success=True,
        message="Agent approved successfully",
        agentId=str(req.agent_id),
        newStatus="approved",
        timestamp=now.isoformat(),
        approvedBy=approver_name,
        api_key=generated_api_key,
    )
    logger.info(
        f"[APPROVE_RESPONSE] {response_payload.model_dump()} "
        f"handoff={_build_prod_promotion_handoff_payload(deployment, guardrail_promotions).model_dump()}",
    )
    return response_payload


@router.post("/{agent_id}/reject", response_model=ApprovalResponse)
async def reject_agent(
    agent_id: str,
    *,
    session: DbSession,
    current_user: CurrentActiveUser,
    comments: str = Form(default=""),
    reason: str | None = Form(default=None),
    attachments: list[UploadFile] | None = File(default=None),
) -> ApprovalResponse:
    """Reject a pending deployment request."""
    now = datetime.now(timezone.utc)
    req: ApprovalRequest | None = None
    mcp_req: McpApprovalRequest | None = None
    model_req: ModelApprovalRequest | None = None
    try:
        req = await _get_approval_for_action(
            session=session,
            approval_or_agent_id=agent_id,
            current_user=current_user,
        )
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        try:
            mcp_req = await _get_mcp_approval_for_action(
                session=session,
                approval_or_mcp_id=agent_id,
                current_user=current_user,
            )
        except HTTPException as mcp_exc:
            if mcp_exc.status_code != 404:
                raise
            model_req = await _get_model_approval_for_action(
                session=session,
                approval_or_model_id=agent_id,
                current_user=current_user,
            )

    if mcp_req is not None:
        if mcp_req.decision is not None:
            raise HTTPException(status_code=400, detail="MCP approval request already finalized")
        mcp_row = await session.get(McpRegistry, mcp_req.mcp_id)
        if not mcp_row:
            raise HTTPException(status_code=404, detail="Linked MCP server not found")
        uploaded_files = await _collect_attachment_metadata(files=attachments, now=now)
        if not comments.strip() and not uploaded_files:
            raise HTTPException(
                status_code=400,
                detail="Either comments or attachments are required for rejection",
            )
        rejection_reason = reason or "Not approved"
        justification = comments.strip()
        mcp_req.decision = ApprovalDecisionEnum.REJECTED
        mcp_req.justification = f"{rejection_reason}: {justification}" if justification else rejection_reason
        mcp_req.reviewed_by = current_user.id
        existing = mcp_req.file_path if isinstance(mcp_req.file_path, dict) else {}
        existing_files = existing.get("files", [])
        if uploaded_files:
            existing["files"] = [*existing_files, *uploaded_files]
            mcp_req.file_path = existing
        mcp_req.reviewed_at = now
        mcp_req.updated_at = now

        mcp_row.approval_status = "rejected"
        mcp_row.review_comments = mcp_req.justification
        mcp_row.review_attachments = mcp_req.file_path
        mcp_row.reviewed_at = now
        mcp_row.reviewed_by = current_user.id
        mcp_row.is_active = False
        mcp_row.status = "rejected"
        mcp_row.updated_at = now
        session.add(mcp_req)
        session.add(mcp_row)
        if mcp_req.requested_by and mcp_req.requested_by != current_user.id:
            await upsert_approval_notification(
                session,
                recipient_user_id=mcp_req.requested_by,
                entity_type="mcp_request_result",
                entity_id=str(mcp_req.id),
                title=f'MCP server "{mcp_row.server_name}" was rejected.',
                link="/approval",
            )
        await session.commit()
        approver_name = getattr(current_user, "username", None)
        return ApprovalResponse(
            success=True,
            message="MCP request rejected",
            agentId=str(mcp_req.id),
            newStatus="rejected",
            timestamp=now.isoformat(),
            approvedBy=approver_name,
        )

    if model_req is not None:
        if model_req.decision is not None:
            raise HTTPException(status_code=400, detail="Model approval request already finalized")
        model_row = await session.get(ModelRegistry, model_req.model_id)
        if not model_row:
            raise HTTPException(status_code=404, detail="Linked model not found")
        uploaded_files = await _collect_attachment_metadata(files=attachments, now=now)
        if not comments.strip() and not uploaded_files:
            raise HTTPException(
                status_code=400,
                detail="Either comments or attachments are required for rejection",
            )
        rejection_reason = reason or "Not approved"
        justification = comments.strip()
        model_req.decision = ApprovalDecisionEnum.REJECTED
        model_req.justification = f"{rejection_reason}: {justification}" if justification else rejection_reason
        model_req.reviewed_by = current_user.id
        existing = model_req.file_path if isinstance(model_req.file_path, dict) else {}
        existing_files = existing.get("files", [])
        if uploaded_files:
            existing["files"] = [*existing_files, *uploaded_files]
            model_req.file_path = existing
        model_req.reviewed_at = now
        model_req.updated_at = now

        model_row.approval_status = ModelApprovalStatus.REJECTED.value
        model_row.review_comments = model_req.justification
        model_row.review_attachments = model_req.file_path
        model_row.reviewed_at = now
        model_row.reviewed_by = current_user.id
        model_row.is_active = False
        model_row.updated_at = now
        session.add(model_req)
        session.add(model_row)
        await _append_model_audit(
            session=session,
            model_id=model_row.id,
            actor_id=current_user.id,
            action="model.request.rejected",
            from_environment=model_row.environment,
            to_environment=model_row.environment,
            from_visibility=model_row.visibility_scope,
            to_visibility=model_row.visibility_scope,
            message="Model approval request rejected",
            org_id=model_row.org_id,
            dept_id=model_row.dept_id,
            details={"request_type": str(model_req.request_type)},
        )
        if model_req.requested_by and model_req.requested_by != current_user.id:
            model_label = model_row.display_name or model_row.model_name or "Model request"
            await upsert_approval_notification(
                session,
                recipient_user_id=model_req.requested_by,
                entity_type="model_request_result",
                entity_id=str(model_req.id),
                title=f'Model "{model_label}" was rejected.',
                link="/approval",
            )
        await session.commit()
        approver_name = getattr(current_user, "username", None)
        return ApprovalResponse(
            success=True,
            message="Model request rejected",
            agentId=str(model_req.id),
            newStatus="rejected",
            timestamp=now.isoformat(),
            approvedBy=approver_name,
        )

    assert req is not None
    if req.decision is not None:
        raise HTTPException(status_code=400, detail="Approval request already finalized")

    deployment = await session.get(AgentDeploymentProd, req.deployment_id)
    if not deployment:
        raise HTTPException(status_code=404, detail="Linked deployment not found")

    uploaded_files = await _collect_attachment_metadata(files=attachments, now=now)
    if not comments.strip() and not uploaded_files:
        raise HTTPException(
            status_code=400,
            detail="Either comments or attachments are required for rejection",
        )

    rejection_reason = reason or "Not approved"
    justification = comments.strip()
    req.decision = ApprovalDecisionEnum.REJECTED
    req.justification = f"{rejection_reason}: {justification}" if justification else rejection_reason
    req.reviewed_by = current_user.id
    existing = req.file_path if isinstance(req.file_path, dict) else {}
    existing_files = existing.get("files", [])
    if uploaded_files:
        existing["files"] = [*existing_files, *uploaded_files]
        req.file_path = existing
    req.reviewed_at = now
    req.updated_at = now
    session.add(req)

    deployment.status = DeploymentPRODStatusEnum.UNPUBLISHED
    deployment.is_active = False
    deployment.updated_at = now
    session.add(deployment)

    # Reset agent lifecycle_status back to DRAFT on rejection
    agent = await session.get(Agent, req.agent_id)
    if agent:
        agent.lifecycle_status = LifecycleStatusEnum.DRAFT
        session.add(agent)

    if req.requested_by and req.requested_by != current_user.id:
        await upsert_approval_notification(
            session,
            recipient_user_id=req.requested_by,
            entity_type="agent_publish_result",
            entity_id=str(req.id),
            title=f'Agent "{deployment.agent_name}" was rejected.',
            link="/approval",
        )

    await session.commit()

    approver_name = getattr(current_user, "username", None)
    return ApprovalResponse(
        success=True,
        message="Agent rejected",
        agentId=str(req.agent_id),
        newStatus="rejected",
        timestamp=now.isoformat(),
        approvedBy=approver_name,
    )


@router.post("/{agent_id}/attachments")
async def upload_attachments(
    agent_id: str,
    *,
    session: DbSession,
    current_user: CurrentActiveUser,
    attachments: list[UploadFile] = File(...),
):
    """Attach metadata of uploaded files to approval request."""
    req: ApprovalRequest | None = None
    mcp_req: McpApprovalRequest | None = None
    model_req: ModelApprovalRequest | None = None
    try:
        req = await _get_approval_for_action(
            session=session,
            approval_or_agent_id=agent_id,
            current_user=current_user,
        )
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        try:
            mcp_req = await _get_mcp_approval_for_action(
                session=session,
                approval_or_mcp_id=agent_id,
                current_user=current_user,
            )
        except HTTPException as mcp_exc:
            if mcp_exc.status_code != 404:
                raise
            model_req = await _get_model_approval_for_action(
                session=session,
                approval_or_model_id=agent_id,
                current_user=current_user,
            )
    uploaded_files: list[dict] = []
    now = datetime.now(timezone.utc)
    for file in attachments:
        contents = await file.read()
        uploaded_files.append(
            {
                "filename": file.filename,
                "size": len(contents),
                "uploadedAt": now.isoformat(),
            }
        )

    if mcp_req is not None:
        mcp_row = await session.get(McpRegistry, mcp_req.mcp_id)
        if not mcp_row:
            raise HTTPException(status_code=404, detail="Linked MCP server not found")
        existing = mcp_req.file_path if isinstance(mcp_req.file_path, dict) else {}
        existing_files = existing.get("files", [])
        existing["files"] = [*existing_files, *uploaded_files]
        mcp_req.file_path = existing
        mcp_req.updated_at = now
        mcp_row.review_attachments = existing
        mcp_row.updated_at = now
        session.add(mcp_req)
        session.add(mcp_row)
    elif model_req is not None:
        model_row = await session.get(ModelRegistry, model_req.model_id)
        if not model_row:
            raise HTTPException(status_code=404, detail="Linked model not found")
        existing = model_req.file_path if isinstance(model_req.file_path, dict) else {}
        existing_files = existing.get("files", [])
        existing["files"] = [*existing_files, *uploaded_files]
        model_req.file_path = existing
        model_req.updated_at = now
        model_row.review_attachments = existing
        model_row.updated_at = now
        session.add(model_req)
        session.add(model_row)
    else:
        assert req is not None
        existing = req.file_path if isinstance(req.file_path, dict) else {}
        existing_files = existing.get("files", [])
        existing["files"] = [*existing_files, *uploaded_files]
        req.file_path = existing
        req.updated_at = now
        session.add(req)
    await session.commit()

    return {
        "success": True,
        "message": "Attachments uploaded successfully",
        "agentId": str(
            mcp_req.id if mcp_req is not None else (model_req.id if model_req is not None else req.agent_id)
        ),
        "uploadedFiles": uploaded_files,
    }


@router.get("/{agent_id}", response_model=ApprovalAgent)
async def get_agent_details(
    agent_id: str,
    *,
    session: DbSession,
    current_user: CurrentActiveUser,
) -> ApprovalAgent:
    req: ApprovalRequest | None = None
    mcp_req: McpApprovalRequest | None = None
    model_req: ModelApprovalRequest | None = None
    try:
        req = await _get_approval_for_view(
            session=session,
            approval_or_agent_id=agent_id,
            current_user=current_user,
        )
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        try:
            mcp_req = await _get_mcp_approval_for_view(
                session=session,
                approval_or_mcp_id=agent_id,
                current_user=current_user,
            )
        except HTTPException as mcp_exc:
            if mcp_exc.status_code != 404:
                raise
            model_req = await _get_model_approval_for_view(
                session=session,
                approval_or_model_id=agent_id,
                current_user=current_user,
            )
    if mcp_req is not None:
        row = await session.get(McpRegistry, mcp_req.mcp_id)
        if not row:
            raise HTTPException(status_code=404, detail="Linked MCP server not found")
        requester = await session.get(User, mcp_req.requested_by)
        approver_id = mcp_req.reviewed_by or mcp_req.request_to
        approver = await session.get(User, approver_id) if approver_id else None
        submitted_at = mcp_req.requested_at
        return ApprovalAgent(
            id=str(mcp_req.id),
            entityType="mcp",
            title=row.server_name,
            status=_to_status_label_any(mcp_req.decision),
            description=row.description or "",
            submittedBy=SubmittedBy(
                name=(
                    requester.display_name
                    if requester and requester.display_name
                    else (requester.username if requester else "Unknown")
                ),
                avatar=None,
                email=(
                    requester.email
                    if requester and requester.email
                    else (
                        requester.username
                        if requester and requester.username and "@" in requester.username
                        else None
                    )
                ),
            ),
            approver=_build_approver_info(approver),
            project="",
            submitted=(
                submitted_at.replace(tzinfo=timezone.utc).isoformat()
                if submitted_at.tzinfo is None
                else submitted_at.isoformat()
            ),
            version=f"{_format_mcp_env_label(mcp_req)} / {(row.mode or 'mcp').upper()}",
            recentChanges="New MCP server request",
            adminComments=mcp_req.justification,
            adminAttachments=(mcp_req.file_path.get("files", []) if isinstance(mcp_req.file_path, dict) else []),
        )
    if model_req is not None:
        row = await session.get(ModelRegistry, model_req.model_id)
        if not row:
            raise HTTPException(status_code=404, detail="Linked model not found")
        requester = await session.get(User, model_req.requested_by)
        approver_id = model_req.reviewed_by or model_req.request_to
        approver = await session.get(User, approver_id) if approver_id else None
        submitted_at = model_req.requested_at
        provider_cfg = row.provider_config if isinstance(row.provider_config, dict) else {}
        request_meta = provider_cfg.get("request_meta", {})
        model_project_name = request_meta.get("project_name", "")
        return ApprovalAgent(
            id=str(model_req.id),
            entityType="model",
            title=row.display_name,
            status=_to_status_label_any(model_req.decision),
            description=row.description or "",
            submittedBy=SubmittedBy(
                name=(
                    requester.display_name
                    if requester and requester.display_name
                    else (requester.username if requester else "Unknown")
                ),
                avatar=None,
                email=(
                    requester.email
                    if requester and requester.email
                    else (
                        requester.username
                        if requester and requester.username and "@" in requester.username
                        else None
                    )
                ),
            ),
            approver=_build_approver_info(approver),
            project=model_project_name,
            submitted=(
                submitted_at.replace(tzinfo=timezone.utc).isoformat()
                if submitted_at.tzinfo is None
                else submitted_at.isoformat()
            ),
            version=f"{row.model_name} ({_format_model_env_label_for_request(row, model_req)})",
            recentChanges=row.description or "",
            adminComments=model_req.justification,
            adminAttachments=(model_req.file_path.get("files", []) if isinstance(model_req.file_path, dict) else []),
        )
    assert req is not None
    deployment = await session.get(AgentDeploymentProd, req.deployment_id)
    if not deployment:
        raise HTTPException(status_code=404, detail="Linked deployment not found")
    requester = await session.get(User, req.requested_by)
    approver_id = req.reviewed_by or req.request_to
    approver = await session.get(User, approver_id) if approver_id else None
    agent = await session.get(Agent, req.agent_id)
    project_name = ""
    if agent and agent.project_id:
        folder = await session.get(Folder, agent.project_id)
        if folder:
            project_name = folder.name

    return ApprovalAgent(
        id=str(req.id),
        entityType="agent",
        title=deployment.agent_name or (agent.name if agent else "Untitled Agent"),
        status=_to_status_label(req.decision),
        description=deployment.agent_description or req.publish_description or "",
        submittedBy=SubmittedBy(
            name=(requester.display_name if requester and requester.display_name else (requester.username if requester else "Unknown")),
            avatar=None,
            email=(
                requester.email
                if requester and requester.email
                else (
                    requester.username
                    if requester and requester.username and "@" in requester.username
                    else None
                )
            ),
        ),
        approver=_build_approver_info(approver),
        project=project_name,
        submitted=(
            req.updated_at.replace(tzinfo=timezone.utc).isoformat()
            if req.updated_at.tzinfo is None
            else req.updated_at.isoformat()
        ),
        version=f"v{deployment.version_number}",
        recentChanges="",  # intentionally blank for now
        adminComments=req.justification,
        adminAttachments=(req.file_path.get("files", []) if isinstance(req.file_path, dict) else []),
    )


@router.get("/{agent_id}/mcp-config", response_model=McpRegistryRead)
async def get_mcp_config_for_approval(
    agent_id: str,
    *,
    session: DbSession,
    current_user: CurrentActiveUser,
) -> McpRegistryRead:
    """Return editable MCP configuration linked to an approval request."""
    mcp_req = await _get_mcp_approval_for_view(
        session=session,
        approval_or_mcp_id=agent_id,
        current_user=current_user,
    )
    row = await session.get(McpRegistry, mcp_req.mcp_id)
    if not row:
        raise HTTPException(status_code=404, detail="Linked MCP server not found")
    return McpRegistryRead.from_orm_model(row)


@router.put("/{agent_id}/mcp-config", response_model=McpRegistryRead)
async def update_mcp_config_for_approval(
    agent_id: str,
    payload: McpRegistryUpdate,
    *,
    session: DbSession,
    current_user: CurrentActiveUser,
) -> McpRegistryRead:
    """Update MCP config during review; only approver assigned to pending request can edit."""
    mcp_req = await _get_mcp_approval_for_action(
        session=session,
        approval_or_mcp_id=agent_id,
        current_user=current_user,
    )
    if mcp_req.decision is not None:
        raise HTTPException(status_code=400, detail="MCP approval request already finalized")

    row = await session.get(McpRegistry, mcp_req.mcp_id)
    if not row:
        raise HTTPException(status_code=404, detail="Linked MCP server not found")

    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        return McpRegistryRead.from_orm_model(row)

    if "server_name" in updates and isinstance(updates["server_name"], str):
        candidate_name = updates["server_name"].strip().lower()
        existing = (
            await session.exec(
                select(McpRegistry.id).where(
                    func.lower(McpRegistry.server_name) == candidate_name,
                    McpRegistry.id != row.id,
                )
            )
        ).first()
        if existing is not None:
            raise HTTPException(status_code=409, detail="MCP server name already exists")
        updates["server_name"] = updates["server_name"].strip()

    if "mode" in updates and updates["mode"] is not None:
        updates["mode"] = _normalize_mcp_mode(updates["mode"])
    effective_mode = updates.get("mode", row.mode)

    if "environments" in updates:
        raise HTTPException(status_code=400, detail="Direct environment change is blocked during approval")

    if "deployment_env" in updates and updates["deployment_env"] is not None:
        normalized_env = _normalize_mcp_deployment_env(updates["deployment_env"])
        if normalized_env != _normalize_mcp_deployment_env(row.deployment_env or "UAT"):
            raise HTTPException(status_code=400, detail="Direct environment change is blocked during approval")
        updates["deployment_env"] = normalized_env

    if any(
        key in updates
        for key in ("visibility", "public_scope", "public_dept_ids", "org_id", "dept_id")
    ):
        raise HTTPException(status_code=400, detail="Tenancy changes are blocked during approval")

    # Keep transport fields coherent whenever mode changes.
    if effective_mode == "sse":
        updates["command"] = None
        updates["args"] = None
    elif effective_mode == "stdio":
        updates["url"] = None

    allowed_fields = {
        "server_name",
        "description",
        "mode",
        "deployment_env",
        "url",
        "command",
        "args",
    }
    for field_name, value in updates.items():
        if field_name in allowed_fields:
            setattr(row, field_name, value)

    row.updated_at = datetime.now(timezone.utc)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return McpRegistryRead.from_orm_model(row)


@router.get("/{agent_id}/preview", response_model=ApprovalPreviewResponse)
async def get_agent_preview(
    agent_id: str,
    *,
    session: DbSession,
    current_user: CurrentActiveUser,
) -> ApprovalPreviewResponse:
    req: ApprovalRequest | None = None
    mcp_req: McpApprovalRequest | None = None
    model_req: ModelApprovalRequest | None = None
    try:
        req = await _get_approval_for_view(
            session=session,
            approval_or_agent_id=agent_id,
            current_user=current_user,
        )
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        try:
            mcp_req = await _get_mcp_approval_for_view(
                session=session,
                approval_or_mcp_id=agent_id,
                current_user=current_user,
            )
        except HTTPException as mcp_exc:
            if mcp_exc.status_code != 404:
                raise
            model_req = await _get_model_approval_for_view(
                session=session,
                approval_or_model_id=agent_id,
                current_user=current_user,
            )
    if mcp_req is not None:
        row = await session.get(McpRegistry, mcp_req.mcp_id)
        if not row:
            raise HTTPException(status_code=404, detail="Linked MCP server not found")
        return ApprovalPreviewResponse(
            id=str(mcp_req.id),
            title=row.server_name,
            version=f"{_format_mcp_env_label(mcp_req)} / {(row.mode or 'mcp').upper()}",
            snapshot={
                "server_name": row.server_name,
                "description": row.description,
                "mode": row.mode,
                "deployment_env": _format_mcp_env_label(mcp_req),
                "requested_environments": [str(v).lower() for v in (getattr(mcp_req, "requested_environments", None) or []) if v] or None,
                "url": row.url,
                "command": row.command,
                "args": row.args,
                "visibility": row.visibility,
                "public_scope": row.public_scope,
                "org_id": str(row.org_id) if row.org_id else None,
                "dept_id": str(row.dept_id) if row.dept_id else None,
                "approval_status": row.approval_status,
            },
        )
    if model_req is not None:
        row = await session.get(ModelRegistry, model_req.model_id)
        if not row:
            raise HTTPException(status_code=404, detail="Linked model not found")
        return ApprovalPreviewResponse(
            id=str(model_req.id),
            title=row.display_name,
            version=f"{_format_model_env_label(row)} / {str(row.model_type).upper()}",
            snapshot={
                "model_id": str(row.id),
                "display_name": row.display_name,
                "description": row.description,
                "provider": row.provider,
                "model_name": row.model_name,
                "model_type": row.model_type,
                "environment": row.environment,
                "environments": row.environments,
                "requested_environments": model_req.requested_environments,
                "requested_type": str(model_req.request_type),
                "source_environment": model_req.source_environment,
                "target_environment": model_req.target_environment,
                "visibility_requested": model_req.visibility_requested,
                "visibility_scope": row.visibility_scope,
                "org_id": str(row.org_id) if row.org_id else None,
                "dept_id": str(row.dept_id) if row.dept_id else None,
                "approval_status": row.approval_status,
                "provider_config": row.provider_config,
                "capabilities": row.capabilities,
                "default_params": row.default_params,
            },
        )
    assert req is not None
    deployment = await session.get(AgentDeploymentProd, req.deployment_id)
    if not deployment:
        raise HTTPException(status_code=404, detail="Linked deployment not found")

    if not deployment.agent_snapshot:
        raise HTTPException(
            status_code=404,
            detail="No deployment snapshot found for preview",
        )

    return ApprovalPreviewResponse(
        id=str(req.id),
        title=deployment.agent_name or "Review Details",
        version=f"v{deployment.version_number}",
        snapshot=deployment.agent_snapshot,
    )


@router.post("/{agent_id}/reset-status")
async def reset_agent_status(
    agent_id: str,
    *,
    session: DbSession,
    current_user: CurrentActiveUser,
):
    """Reset status back to pending (kept for testing/demo utility)."""
    req: ApprovalRequest | None = None
    mcp_req: McpApprovalRequest | None = None
    model_req: ModelApprovalRequest | None = None
    try:
        req = await _get_approval_for_action(
            session=session,
            approval_or_agent_id=agent_id,
            current_user=current_user,
        )
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        try:
            mcp_req = await _get_mcp_approval_for_action(
                session=session,
                approval_or_mcp_id=agent_id,
                current_user=current_user,
            )
        except HTTPException as mcp_exc:
            if mcp_exc.status_code != 404:
                raise
            model_req = await _get_model_approval_for_action(
                session=session,
                approval_or_model_id=agent_id,
                current_user=current_user,
            )
    if mcp_req is not None:
        now = datetime.now(timezone.utc)
        mcp_row = await session.get(McpRegistry, mcp_req.mcp_id)
        if not mcp_row:
            raise HTTPException(status_code=404, detail="Linked MCP server not found")
        mcp_req.decision = None
        mcp_req.reviewed_at = None
        mcp_req.updated_at = now
        mcp_row.approval_status = "pending"
        mcp_row.reviewed_at = None
        mcp_row.reviewed_by = None
        mcp_row.is_active = False
        mcp_row.status = "pending_approval"
        mcp_row.updated_at = now
        session.add(mcp_req)
        session.add(mcp_row)
        await session.commit()
        return {
            "success": True,
            "message": "MCP status reset to pending",
            "agentId": str(mcp_req.id),
            "newStatus": "pending",
        }
    if model_req is not None:
        now = datetime.now(timezone.utc)
        model_row = await session.get(ModelRegistry, model_req.model_id)
        if not model_row:
            raise HTTPException(status_code=404, detail="Linked model not found")
        model_req.decision = None
        model_req.reviewed_at = None
        model_req.updated_at = now
        model_row.approval_status = ModelApprovalStatus.PENDING.value
        model_row.reviewed_at = None
        model_row.reviewed_by = None
        model_row.is_active = False
        model_row.updated_at = now
        session.add(model_req)
        session.add(model_row)
        await _append_model_audit(
            session=session,
            model_id=model_row.id,
            actor_id=current_user.id,
            action="model.request.reset_pending",
            message="Model approval reset to pending",
            org_id=model_row.org_id,
            dept_id=model_row.dept_id,
            details={"request_type": str(model_req.request_type)},
        )
        await session.commit()
        return {
            "success": True,
            "message": "Model status reset to pending",
            "agentId": str(model_req.id),
            "newStatus": "pending",
        }
    assert req is not None
    deployment = await session.get(AgentDeploymentProd, req.deployment_id)
    if not deployment:
        raise HTTPException(status_code=404, detail="Linked deployment not found")

    now = datetime.now(timezone.utc)
    req.decision = None
    req.reviewed_at = None
    req.updated_at = now
    session.add(req)

    deployment.status = DeploymentPRODStatusEnum.PENDING_APPROVAL
    deployment.is_active = False
    deployment.updated_at = now
    session.add(deployment)

    await session.commit()
    return {
        "success": True,
        "message": "Agent status reset to pending",
        "agentId": str(req.agent_id),
        "newStatus": "pending",
    }
