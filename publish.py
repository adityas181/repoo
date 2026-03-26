"""AgentCore Publishing API.

Handles deploying agents to UAT and PROD environments with version management,
snapshot freezing, shadow deployment support, and agent cloning.

Architecture:
    - agent_deployment_uat table: INSERT-based versioning for UAT (direct, no approval)
    - agent_deployment_prod table: INSERT-based versioning for PROD (approval flow for developers)
    - Each deployment creates a new row with a version number (v1, v2, v3...)
    - is_active flag controls which versions are serving traffic
    - Shadow deployment: multiple versions can be is_active=True simultaneously
    - Rollback: toggle is_active flags without creating new rows

Endpoints:
    POST   /publish/{agent_id}                   — **Unified publish** (UAT or PROD via env field)
    GET    /publish/{agent_id}/status             — Get deploy status across both envs
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from enum import Enum as PyEnum

from fastapi import APIRouter, HTTPException, Query, status
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from agentcore.api.utils import CurrentActiveUser, DbSession
from agentcore.services.database.models.user_department_membership.model import UserDepartmentMembership
from agentcore.services.database.models.user_organization_membership.model import UserOrganizationMembership
from agentcore.services.database.models.agent.model import Agent, LifecycleStatusEnum
from agentcore.services.database.models.department.model import Department
from agentcore.services.database.models.approval_request.model import (
    ApprovalRequest,
)
from agentcore.services.database.models.folder.model import Folder
from agentcore.services.database.models.agent_deployment_prod.model import (
    AgentDeploymentProd,
    DeploymentPRODStatusEnum,
    ProdDeploymentLifecycleEnum,
    ProdDeploymentVisibilityEnum,
)
from agentcore.services.database.models.agent_deployment_uat.model import (
    AgentDeploymentUAT,
    DeploymentLifecycleEnum,
    DeploymentUATStatusEnum,
    DeploymentVisibilityEnum,
)
from agentcore.services.database.models.user.model import User
from agentcore.services.database.models.agent_api_key.model import AgentApiKey
from agentcore.services.database.models.agent_publish_recipient.model import AgentPublishRecipient
from agentcore.services.auth.utils import generate_agent_api_key
from agentcore.services.database.models.agent_registry.model import RegistryDeploymentEnvEnum
from agentcore.services.database.registry_service import sync_agent_registry
from agentcore.services.database.models.agent_bundle.model import (
    AgentBundle,
    AgentBundleRead,
    BundleTypeEnum,
    DeploymentEnvEnum,
)
from agentcore.services.approval_notifications import upsert_approval_notification

router = APIRouter(prefix="/publish", tags=["Publish"])


# ═══════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════

# Roles that can publish directly to PROD (others go through approval flow)
ADMIN_ROLES = {"admin", "super_admin", "root", "department_admin"}


async def _validate_resources_for_prod(snapshot: dict, session) -> None:
    """Block PROD publish if any model or MCP server is not registered for PROD."""
    nodes = snapshot.get("nodes", [])

    for node in nodes:
        node_type = node.get("data", {}).get("type", "")
        template = node.get("data", {}).get("node", {}).get("template", {})

        # ── Check LLM Models ──
        if node_type in ("RegistryModelComponent", "RegistryEmbeddingsComponent"):
            rm = template.get("registry_model", {})
            registry_value = rm.get("value", "") if isinstance(rm, dict) else ""
            if not registry_value or "|" not in registry_value:
                continue
            parts = [p.strip() for p in registry_value.split("|")]
            if len(parts) < 3:
                continue
            model_id, model_display = parts[2], parts[0]

            from agentcore.services.database.models.model_registry.model import ModelRegistry

            model = await session.get(ModelRegistry, model_id)
            if model is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Cannot publish to PROD: model '{model_display}' (ID: {model_id}) not found in registry.",
                )
            envs = [e.lower() for e in (model.environments or [model.environment])]
            if "prod" not in envs:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"Cannot publish to PROD: model '{model.display_name}' "
                        f"is registered for {envs} only. "
                        f"Register it for PROD or use UAT+PROD when registering."
                    ),
                )
            if not model.is_active:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Cannot publish to PROD: model '{model.display_name}' is inactive.",
                )

        # ── Check MCP Servers ──
        if node_type == "MCPTools":
            from agentcore.services.database.models.mcp_registry.model import McpRegistry

            mcp_value = template.get("mcp_server", {})
            server_name = ""
            if isinstance(mcp_value, dict):
                server_name = mcp_value.get("value", "") or mcp_value.get("name", "")
            elif mcp_value:
                server_name = str(mcp_value)
            server_name = server_name.strip()
            if not server_name:
                continue

            mcp = (
                await session.exec(
                    select(McpRegistry).where(McpRegistry.server_name == server_name)
                )
            ).first()
            if mcp is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Cannot publish to PROD: MCP server '{server_name}' not found in registry.",
                )
            envs = [e.lower() for e in (mcp.environments or [mcp.deployment_env or "uat"])]
            if "prod" not in envs:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"Cannot publish to PROD: MCP server '{mcp.server_name}' "
                        f"is registered for {envs} only. "
                        f"Register it for PROD before publishing."
                    ),
                )
            if not mcp.is_active:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Cannot publish to PROD: MCP server '{mcp.server_name}' is inactive.",
                )


# ═══════════════════════════════════════════════════════════════════════════
# Request / Response Schemas
# ═══════════════════════════════════════════════════════════════════════════


class PublishEnvironment(str, PyEnum):
    """Target environment for publishing."""
    uat = "uat"
    prod = "prod"


class DeployActionRequest(BaseModel):
    """Request body for updating a deployment record.

    Send the fields you want to change as key-value pairs.
    The backend validates the state transition and applies the update.

    Examples:
        Unpublish:  {"status": "UNPUBLISHED", "is_active": false}
        Activate:   {"status": "PUBLISHED",   "is_active": true}
        Deactivate: {"is_active": false}
        Republish:  {"status": "PUBLISHED",   "is_active": true}
    """
    status: str | None = Field(
        default=None,
        description="New status value: 'PUBLISHED' or 'UNPUBLISHED'. Omit to keep current status.",
    )
    is_active: bool | None = Field(
        default=None,
        description="Set the is_active flag. true = serving traffic, false = offline. Omit to keep current value.",
    )


class PublishRequest(BaseModel):
    """Unified request body for deploying an agent to UAT or PROD.

    The frontend sends all required context in a single payload.
    The backend decides whether to deploy directly or go through
    the approval flow based on (environment + user role).
    """

    department_id: UUID | None = Field(
        default=None,
        description="Department the agent belongs to. Optional for org-wide admin private UAT publishes.",
    )
    department_admin_id: UUID | None = Field(
        default=None,
        description=(
            "Optional: department admin user ID. If omitted, backend resolves it from "
            "user_department_membership -> department.admin_user_id."
        ),
    )
    visibility: str = Field(
        default="PRIVATE",
        description="'PUBLIC' = discoverable by all in tenant; 'PRIVATE' = creator + admins only",
    )
    environment: PublishEnvironment = Field(
        description="Target environment: 'uat' or 'prod'",
    )
    publish_description: str | None = Field(
        default=None,
        description="Release notes / description for this deployment action",
    )
    published_agent_name: str | None = Field(
        default=None,
        description=(
            "Stable published display name. Required on first publish and reused "
            "for all later versions."
        ),
    )
    promoted_from_uat_id: UUID | None = Field(
        default=None,
        description=(
            "Optional: UAT deployment ID to promote from. When set, the PROD "
            "snapshot is copied from this UAT record instead of agent.data. "
            "Only valid when environment='prod'."
        ),
    )
    recipient_emails: list[str] | None = Field(
        default=None,
        description="Optional recipient emails for this agent publish scope.",
    )


class CloneFromPublishRequest(BaseModel):
    """Request body for cloning an agent from a deployed snapshot."""

    project_id: UUID = Field(
        description="Target project (folder) to place the cloned agent into",
    )
    new_name: str | None = Field(
        default=None,
        description="Name for the cloned agent. If omitted, uses '<original_name> (Copy)'",
    )


class PublishRecordSummary(BaseModel):
    """Deployment record summary without snapshot (used in list & status endpoints).

    Deliberately excludes agent_snapshot for performance — use the
    /publish/{deploy_id}/snapshot endpoint to get the full frozen flow JSON.
    """

    id: UUID
    agent_id: UUID
    version_number: str
    agent_name: str
    agent_description: str | None = None
    publish_description: str | None = None
    published_by: UUID
    published_at: datetime
    is_active: bool
    is_enabled: bool
    status: str
    visibility: str
    error_message: str | None = None
    environment: str  # "uat" or "prod"
    promoted_from_uat_id: UUID | None = None

    class Config:
        from_attributes = True


class AgentPublishStatusResponse(BaseModel):
    """Combined deployment status across both environments for a single agent.

    Used by the UI to show deploy badges on agent cards:
        🟢 UAT (live)  |  🔵 PROD (live)  |  🟡 PROD (pending)
    """

    agent_id: UUID
    uat: PublishRecordSummary | None = None
    prod: PublishRecordSummary | None = None
    has_pending_approval: bool = False
    pending_requested_by: UUID | None = None
    latest_prod_status: str | None = None
    latest_review_decision: str | None = None
    latest_prod_published_by: UUID | None = None


class PublishSnapshotResponse(BaseModel):
    """Full deployment record including the frozen agent snapshot.

    Used for:
        - Testing/previewing in playground (works for chat AND autonomous agents)
        - Reviewing agent before approval
        - Inspecting a specific version's flow definition

    The agent_snapshot contains the complete flow JSON (nodes + edges) that the
    runtime can execute. This is identical to agent.data at the moment of deployment.
    """

    id: UUID
    agent_id: UUID
    environment: str  # "uat" or "prod"
    version_number: str
    agent_name: str
    agent_description: str | None = None
    agent_snapshot: dict
    publish_description: str | None = None
    published_by: UUID
    published_at: datetime
    status: str
    is_active: bool
    visibility: str


class CloneResponse(BaseModel):
    """Response after cloning a deployed agent into a new agent."""

    agent_id: UUID
    agent_name: str
    project_id: UUID
    cloned_from_publish_id: UUID
    environment_source: str  # "uat" or "prod"


class PublishActionResponse(BaseModel):
    """Generic response for deployment actions (deploy, unpublish, activate, deactivate)."""

    success: bool
    message: str
    publish_id: UUID
    environment: str
    status: str
    is_active: bool
    version_number: str
    promoted_from_uat_id: UUID | None = None
    api_key: str | None = None


class PublishNotifyRequest(BaseModel):
    """Payload for publish notification events."""

    agent_id: UUID
    environment: str
    version_number: str
    deployment_id: UUID


class PublishNotifyResponse(BaseModel):
    """Response for publish notification events."""

    agent_id: UUID
    environment: str
    version_number: str
    deployment_id: UUID


class PublishNotifyVerifiedResponse(BaseModel):
    """DB-verified response for publish notification events.

    Returns full deployment details after verifying the record exists
    in the database — used for downstream deployment orchestration.
    """

    agent_id: UUID
    agent_name: str
    agent_description: str | None = None
    environment: str
    version_number: str
    deployment_id: UUID
    status: str
    is_active: bool
    deployed_by: UUID
    deployed_at: datetime | None = None
    org_id: UUID
    dept_id: UUID | None = None
    verified: bool = True


class ValidatePublishEmailResponse(BaseModel):
    """Validation response for publish recipient emails."""

    agent_id: UUID
    email: str
    department_id: UUID | None
    exists_in_department: bool
    message: str


class PublishContextResponse(BaseModel):
    """Resolved publish context for current user and agent tenant scope."""

    agent_id: UUID
    org_id: UUID
    department_id: UUID | None
    department_admin_id: UUID | None


class PublishEmailSuggestion(BaseModel):
    email: str
    display_name: str | None = None


async def _notify_publish_event(
    session,
    *,
    agent_id: UUID,
    agent_name: str,
    environment: str,
    version_number: int,
    publish_id: UUID,
    published_by: UUID,
    published_at: datetime,
) -> PublishNotifyResponse | None:
    """Fire notification after a successful publish — with DB verification.

    Re-queries the deployment record to confirm status=PUBLISHED before emitting.
    """
    try:
        # ── DB double-confirmation ──
        if environment == "uat":
            record = await session.get(AgentDeploymentUAT, publish_id)
            if not record or record.status != DeploymentUATStatusEnum.PUBLISHED:
                logger.warning(f"[PublishNotify] SKIPPED — UAT record {publish_id} not in PUBLISHED state")
                return None
        else:
            record = await session.get(AgentDeploymentProd, publish_id)
            if not record or record.status != DeploymentPRODStatusEnum.PUBLISHED:
                logger.warning(f"[PublishNotify] SKIPPED — PROD record {publish_id} not in PUBLISHED state")
                return None

        logger.info(
            f"[PublishNotify] agent={agent_id} env={environment} "
            f"version=v{version_number} deployment_id={publish_id}"
        )
        return PublishNotifyResponse(
            agent_id=agent_id,
            environment=environment,
            version_number=f"v{version_number}",
            deployment_id=publish_id,
        )
    except Exception as e:
        logger.warning(f"Publish notification failed: {e}")
        return None


async def _current_user_department_ids(session: DbSession, user_id: UUID) -> set[UUID]:
    rows = (
        await session.exec(
            select(UserDepartmentMembership.department_id).where(
                UserDepartmentMembership.user_id == user_id,
                UserDepartmentMembership.status == "active",
            )
        )
    ).all()
    return set(rows)


async def _current_user_org_ids(session: DbSession, user_id: UUID) -> set[UUID]:
    rows = (
        await session.exec(
            select(UserOrganizationMembership.org_id).where(
                UserOrganizationMembership.user_id == user_id,
                UserOrganizationMembership.status.in_(["accepted", "active"]),
            )
        )
    ).all()
    return set(rows)


async def _resolve_publish_scope(
    session: DbSession,
    *,
    current_user: CurrentActiveUser,
    agent: Agent,
    requested_department_id: UUID | None = None,
    requested_department_admin_id: UUID | None = None,
    allow_departmentless_private_publish: bool = False,
) -> tuple[UUID | None, UUID | None]:
    """Resolve and validate publish department/admin in the agent's org tenant."""
    current_role = str(getattr(current_user, "role", "")).lower()
    is_org_wide_admin = current_role in {"root", "super_admin", "admin"}

    # Super/root admins may not have user_department_membership rows.
    # For these roles, resolve scope by requested department (or agent.dept_id),
    # while still enforcing tenant consistency.
    if is_org_wide_admin:
        resolved_org_id = agent.org_id
        if not resolved_org_id:
            current_user_org_ids = await _current_user_org_ids(session, current_user.id)
            if current_user_org_ids:
                resolved_org_id = sorted(current_user_org_ids, key=str)[0]
                agent.org_id = resolved_org_id
                session.add(agent)

        resolved_department_id = requested_department_id or agent.dept_id
        if not resolved_department_id and resolved_org_id:
            # For org-wide admins without department memberships, use a deterministic
            # fallback department from the agent's organization.
            fallback_department = (
                await session.exec(
                    select(Department)
                    .where(Department.org_id == resolved_org_id)
                    .order_by(col(Department.id))
                )
            ).first()
            if fallback_department:
                resolved_department_id = fallback_department.id

        if not resolved_department_id:
            if allow_departmentless_private_publish and resolved_org_id:
                if not agent.org_id:
                    agent.org_id = resolved_org_id
                    session.add(agent)
                return None, None
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "department_id is required for publish scope resolution when "
                    "no department could be inferred for this agent."
                ),
            )

        department = (
            await session.exec(
                select(Department).where(Department.id == resolved_department_id)
            )
        ).first()
        if not department:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Department {resolved_department_id} not found.",
            )

        # Keep publish within the agent's tenant if agent is already stitched.
        if agent.org_id and department.org_id != agent.org_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Department {resolved_department_id} is not part of organization {agent.org_id}."
                ),
            )

        # Stitch missing tenant fields from resolved department.
        if not agent.org_id:
            agent.org_id = department.org_id
        if not agent.dept_id:
            agent.dept_id = department.id
        session.add(agent)

        resolved_department_admin_id = department.admin_user_id
        if (
            requested_department_admin_id
            and requested_department_admin_id != resolved_department_admin_id
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"department_admin_id {requested_department_admin_id} does not match "
                    f"department admin {resolved_department_admin_id} for department {resolved_department_id}."
                ),
            )

        admin_user = (
            await session.exec(select(User).where(User.id == resolved_department_admin_id))
        ).first()
        if not admin_user:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Department admin user {resolved_department_admin_id} not found.",
            )

        return resolved_department_id, resolved_department_admin_id

    base_memberships = (
        await session.exec(
            select(UserDepartmentMembership).where(
                UserDepartmentMembership.user_id == current_user.id,
                UserDepartmentMembership.status == "active",
            )
        )
    ).all()
    if not base_memberships:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Current user has no active department membership. "
                "Please map the user in user_department_membership first."
            ),
        )

    # If org isn't stitched on agent yet, derive it from publisher membership.
    if not agent.org_id:
        if requested_department_id:
            scoped = [m for m in base_memberships if m.department_id == requested_department_id]
            if not scoped:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"department_id {requested_department_id} is not mapped to publishing user "
                        f"{current_user.id}."
                    ),
                )
            selected = scoped[0]
        else:
            selected = sorted(base_memberships, key=lambda m: (str(m.org_id), str(m.department_id)))[0]

        agent.org_id = selected.org_id
        if not agent.dept_id:
            agent.dept_id = selected.department_id
        session.add(agent)

    memberships = [m for m in base_memberships if m.org_id == agent.org_id]
    if not memberships:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current user has no active department mapping in the agent organization.",
        )

    allowed_dept_ids = {m.department_id for m in memberships}

    if requested_department_id is not None:
        if requested_department_id not in allowed_dept_ids:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"department_id {requested_department_id} is not mapped to current user "
                    f"in organization {agent.org_id}."
                ),
            )
        resolved_department_id = requested_department_id
    elif agent.dept_id and agent.dept_id in allowed_dept_ids:
        resolved_department_id = agent.dept_id
    else:
        # Deterministic fallback when user has multiple departments.
        resolved_department_id = sorted(allowed_dept_ids, key=str)[0]

    department = (
        await session.exec(
            select(Department).where(
                Department.id == resolved_department_id,
                Department.org_id == agent.org_id,
            )
        )
    ).first()
    if not department:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Department {resolved_department_id} is not part of organization {agent.org_id}."
            ),
        )

    resolved_department_admin_id = department.admin_user_id
    if requested_department_admin_id and requested_department_admin_id != resolved_department_admin_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"department_admin_id {requested_department_admin_id} does not match "
                f"department admin {resolved_department_admin_id} for department {resolved_department_id}."
            ),
        )

    admin_user = (
        await session.exec(select(User).where(User.id == resolved_department_admin_id))
    ).first()
    if not admin_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Department admin user {resolved_department_admin_id} not found.",
        )

    return resolved_department_id, resolved_department_admin_id

# ═══════════════════════════════════════════════════════════════════════════

EMAIL_REGEX = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _normalize_recipient_emails(raw_emails: list[str] | None) -> list[str]:
    if not raw_emails:
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_email in raw_emails:
        email = str(raw_email).strip().lower()
        if not email or email in seen:
            continue
        seen.add(email)
        normalized.append(email)
    return normalized


async def _validate_and_store_publish_recipients(
    *,
    session: DbSession,
    agent: Agent,
    department_id: UUID,
    current_user: CurrentActiveUser,
    recipient_emails: list[str],
) -> None:
    if not recipient_emails:
        return

    invalid_format = [email for email in recipient_emails if not EMAIL_REGEX.match(email)]
    if invalid_format:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid email format: {', '.join(invalid_format)}",
        )

    user_rows = (
        await session.exec(
            select(User).where(
                or_(
                    func.lower(User.username).in_(recipient_emails),
                    and_(User.email.is_not(None), func.lower(User.email).in_(recipient_emails)),
                )
            )
        )
    ).all()
    matched_users_by_email: dict[str, User] = {}
    for user in user_rows:
        if user.username:
            matched_users_by_email[str(user.username).strip().lower()] = user
        if user.email:
            matched_users_by_email[str(user.email).strip().lower()] = user

    missing_users = [email for email in recipient_emails if email not in matched_users_by_email]
    if missing_users:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Email not found in users table: {', '.join(missing_users)}",
        )

    recipient_user_ids = {matched_users_by_email[email].id for email in recipient_emails}
    memberships = (
        await session.exec(
            select(UserDepartmentMembership).where(
                UserDepartmentMembership.user_id.in_(list(recipient_user_ids)),
                UserDepartmentMembership.department_id == department_id,
                UserDepartmentMembership.status == "active",
            )
        )
    ).all()
    allowed_user_ids = {membership.user_id for membership in memberships}

    not_in_department = sorted(
        {
            email
            for email in recipient_emails
            if matched_users_by_email[email].id not in allowed_user_ids
        }
    )
    if not_in_department:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Some emails are not active members of this department: "
                f"{', '.join(not_in_department)}"
            ),
        )

    now = datetime.now(timezone.utc)
    for email in recipient_emails:
        user = matched_users_by_email[email]
        existing = (
            await session.exec(
                select(AgentPublishRecipient).where(
                    AgentPublishRecipient.agent_id == agent.id,
                    AgentPublishRecipient.dept_id == department_id,
                    AgentPublishRecipient.recipient_email == email,
                )
            )
        ).first()

        if existing:
            existing.recipient_user_id = user.id
            existing.updated_at = now
            if agent.org_id:
                existing.org_id = agent.org_id
            session.add(existing)
            continue

        session.add(
            AgentPublishRecipient(
                agent_id=agent.id,
                org_id=agent.org_id,
                dept_id=department_id,
                recipient_user_id=user.id,
                recipient_email=email,
                created_by=current_user.id,
                created_at=now,
                updated_at=now,
            )
        )
# Helper Functions
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/validate-email", response_model=ValidatePublishEmailResponse)
@router.post("/validate-email", response_model=ValidatePublishEmailResponse)
async def validate_publish_email(
    *,
    agent_id: UUID = Query(..., description="Agent ID"),
    email: str = Query(..., description="Recipient email to validate"),
    session: DbSession,
    current_user: CurrentActiveUser,
) -> ValidatePublishEmailResponse:
    """Validate that an email exists in the same department(s) as the current user."""
    normalized_email = str(email).strip().lower()
    if "@" not in normalized_email:
        raise HTTPException(status_code=400, detail="Invalid email format.")

    agent = await session.get(Agent, agent_id)
    if not agent or agent.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Agent not found.")

    current_user_dept_ids = await _current_user_department_ids(session, current_user.id)
    if not current_user_dept_ids:
        return ValidatePublishEmailResponse(
            agent_id=agent_id,
            email=normalized_email,
            department_id=None,
            exists_in_department=False,
            message="Current user has no active department mapping.",
        )

    user = (
        await session.exec(
            select(User).where(
                (User.username.ilike(normalized_email)) | (User.email.ilike(normalized_email)),
            )
        )
    ).first()

    if not user:
        return ValidatePublishEmailResponse(
            agent_id=agent_id,
            email=normalized_email,
            department_id=next(iter(current_user_dept_ids)),
            exists_in_department=False,
            message="Email not found in user table for this department.",
        )

    memberships = (
        await session.exec(
            select(UserDepartmentMembership).where(
                UserDepartmentMembership.user_id == user.id,
                UserDepartmentMembership.department_id.in_(list(current_user_dept_ids)),
                UserDepartmentMembership.status == "active",
            )
        )
    ).all()

    exists_in_department = len(memberships) > 0
    resolved_department_id = memberships[0].department_id if memberships else next(iter(current_user_dept_ids))
    return ValidatePublishEmailResponse(
        agent_id=agent_id,
        email=normalized_email,
        department_id=resolved_department_id,
        exists_in_department=exists_in_department,
        message=(
            "Email found in this department."
            if exists_in_department
            else "Email exists, but not in this department."
        ),
    )


@router.get("/{agent_id}/email-suggestions", response_model=list[PublishEmailSuggestion], status_code=200)
async def get_publish_email_suggestions(
    *,
    session: DbSession,
    agent_id: UUID,
    q: str = Query(default="", description="Email prefix or substring for suggestions"),
    limit: int = Query(default=8, ge=1, le=25),
    current_user: CurrentActiveUser,
) -> list[PublishEmailSuggestion]:
    """Return recipient suggestions scoped to current user's departments.

    Results are ranked with previously selected recipients first, then
    department directory matches from the user table.
    """
    query_text = str(q).strip().lower()
    if not query_text:
        return []

    agent = await _get_agent_or_404(session, agent_id, current_user.id)
    current_user_dept_ids = await _current_user_department_ids(session, current_user.id)
    if not current_user_dept_ids:
        return []

    recent_stmt = (
        select(AgentPublishRecipient, User)
        .join(User, User.id == AgentPublishRecipient.recipient_user_id)
        .where(
            AgentPublishRecipient.agent_id == agent.id,
            AgentPublishRecipient.dept_id.in_(list(current_user_dept_ids)),
            func.lower(AgentPublishRecipient.recipient_email).like(f"%{query_text}%"),
        )
        .order_by(col(AgentPublishRecipient.updated_at).desc())
        .limit(limit)
    )
    recent_rows = (await session.exec(recent_stmt)).all()
    suggestions: list[PublishEmailSuggestion] = []
    seen: set[str] = set()
    for recipient_row, user in recent_rows:
        email = str(recipient_row.recipient_email).strip().lower()
        if not email or email in seen:
            continue
        seen.add(email)
        suggestions.append(
            PublishEmailSuggestion(
                email=email,
                display_name=user.display_name or user.username,
            )
        )

    if len(suggestions) >= limit:
        return suggestions[:limit]

    directory_limit = max(limit * 2, 16)
    directory_stmt = (
        select(User)
        .join(UserDepartmentMembership, UserDepartmentMembership.user_id == User.id)
        .where(
            UserDepartmentMembership.department_id.in_(list(current_user_dept_ids)),
            UserDepartmentMembership.status == "active",
            or_(
                func.lower(User.username).like(f"%{query_text}%"),
                and_(User.email.is_not(None), func.lower(User.email).like(f"%{query_text}%")),
            ),
        )
        .order_by(col(User.display_name), col(User.username))
        .limit(directory_limit)
    )
    directory_users = (await session.exec(directory_stmt)).all()
    for user in directory_users:
        email_candidate = str(user.email or user.username or "").strip().lower()
        if not email_candidate or "@" not in email_candidate or email_candidate in seen:
            continue
        seen.add(email_candidate)
        suggestions.append(
            PublishEmailSuggestion(
                email=email_candidate,
                display_name=user.display_name or user.username,
            )
        )
        if len(suggestions) >= limit:
            break

    return suggestions


@router.get("/{agent_id}/context", response_model=PublishContextResponse, status_code=200)
async def get_publish_context(
    *,
    session: DbSession,
    agent_id: UUID,
    current_user: CurrentActiveUser,
) -> PublishContextResponse:
    """Return resolved tenant-safe publish context for the current user."""
    agent = await _get_agent_or_404(session, agent_id, current_user.id)
    if not agent.org_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Agent must belong to an organization before publishing.",
        )
    department_id, department_admin_id = await _resolve_publish_scope(
        session,
        current_user=current_user,
        agent=agent,
    )
    return PublishContextResponse(
        agent_id=agent.id,
        org_id=agent.org_id,
        department_id=department_id,
        department_admin_id=department_admin_id,
    )

async def _get_next_version_number(
    session: AsyncSession,
    agent_id: UUID,
    table_class: type[AgentDeploymentUAT] | type[AgentDeploymentProd],
) -> int:
    """Calculate the next version number for an agent in the given table.

    Finds the highest existing version number and returns max + 1.
    If no previous versions exist, returns 1.

    Args:
        session: Database session.
        agent_id: The agent to get next version for.
        table_class: AgentDeploymentUAT or AgentDeploymentProd model class.

    Returns:
        Next version number as int (1, 2, 3, ...).
    """
    stmt = select(table_class.version_number).where(table_class.agent_id == agent_id)
    results = (await session.exec(stmt)).all()

    if not results:
        return 1

    return max(results) + 1


async def _resolve_published_agent_name(
    session: AsyncSession,
    *,
    agent_id: UUID,
    requested_name: str | None,
) -> str:
    """Return the stable published name for this agent.

    The first publish locks the name. Later publishes always reuse that same
    deployment name, even if the editable draft agent name changes.
    """

    normalized_requested_name = (requested_name or "").strip()

    existing_uat = (
        await session.exec(
            select(AgentDeploymentUAT)
            .where(
                AgentDeploymentUAT.agent_id == agent_id,
                AgentDeploymentUAT.agent_name.is_not(None),
            )
            .order_by(
                col(AgentDeploymentUAT.deployed_at).asc(),
                col(AgentDeploymentUAT.created_at).asc(),
                col(AgentDeploymentUAT.id).asc(),
            )
        )
    ).first()
    existing_prod = (
        await session.exec(
            select(AgentDeploymentProd)
            .where(
                AgentDeploymentProd.agent_id == agent_id,
                AgentDeploymentProd.agent_name.is_not(None),
            )
            .order_by(
                col(AgentDeploymentProd.deployed_at).asc(),
                col(AgentDeploymentProd.created_at).asc(),
                col(AgentDeploymentProd.id).asc(),
            )
        )
    ).first()

    candidates = [
        record
        for record in (existing_uat, existing_prod)
        if record is not None and str(record.agent_name or "").strip()
    ]
    if candidates:
        first_record = min(
            candidates,
            key=lambda record: (
                getattr(record, "deployed_at", None) or datetime.min.replace(tzinfo=timezone.utc),
                getattr(record, "created_at", None) or datetime.min.replace(tzinfo=timezone.utc),
                str(record.id),
            ),
        )
        stable_name = str(first_record.agent_name).strip()
        if normalized_requested_name and normalized_requested_name != stable_name:
            logger.info(
                "Ignoring publish name override for agent {}. Reusing locked published name '{}'.",
                agent_id,
                stable_name,
            )
        return stable_name

    if not normalized_requested_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="published_agent_name is required for the first publish of this agent.",
        )

    return normalized_requested_name


# ═══════════════════════════════════════════════════════════════════════════
# Bundle extraction — creates AgentBundle rows from a frozen snapshot
# ═══════════════════════════════════════════════════════════════════════════

# Node type → (BundleTypeEnum, template field that holds the resource value)
_NODE_TYPE_TO_BUNDLE: dict[str, tuple[BundleTypeEnum, str]] = {
    "RegistryModelComponent": (BundleTypeEnum.MODEL, "registry_model"),
    "RegistryEmbeddingsComponent": (BundleTypeEnum.MODEL, "registry_model"),
    "MCPTools": (BundleTypeEnum.MCP_SERVER, "mcp_server"),
    "NemoGuardrails": (BundleTypeEnum.GUARDRAIL, "guardrail_id"),
    "DatabaseConnector": (BundleTypeEnum.CONNECTOR, "connector"),
    "Chroma": (BundleTypeEnum.VECTOR_DB, "collection_name"),
    "Pinecone": (BundleTypeEnum.VECTOR_DB, "index_name"),
    "CustomComponent": (BundleTypeEnum.CUSTOM_COMPONENT, "code"),
}

# Node types that are considered built-in tools
_TOOL_NODE_TYPES: set[str] = {
    "APIRequest",
    "CalculatorTool",
    "WebSearchNoAPI",
    "DataVisualizer",
    "DataVisualizerTool",
    "File",
    "Directory",
    "DocumentOCRExtractor",
    "Memory",
    "FileTrigger",
    "FolderMonitor",
    "SmartRouter",
    "NLtoSQL",
    "TalkToDataTool",
    "HumanApproval",
}


def _extract_field_value(template: dict, field_name: str) -> str | None:
    """Safely pull the display value from a node template field."""
    field = template.get(field_name)
    if field is None:
        return None
    if isinstance(field, dict):
        val = field.get("value", "")
    else:
        val = field
    if isinstance(val, dict):
        return val.get("name") or val.get("display_name") or str(val)
    return str(val).strip() if val else None


def _extract_resource_config(template: dict, field_name: str, node_type: str) -> dict | None:
    """Build a frozen config dict from the node template for the bundled resource."""
    field = template.get(field_name)
    if not field:
        return None
    value = field.get("value") if isinstance(field, dict) else field

    if node_type in ("RegistryModelComponent", "RegistryEmbeddingsComponent"):
        # value is "display_name | model_name | uuid"
        config: dict = {"raw_value": value}
        if isinstance(value, str) and "|" in value:
            parts = [p.strip() for p in value.split("|")]
            if len(parts) >= 3:
                config = {"display_name": parts[0], "model_name": parts[1], "model_id": parts[2]}
        # Capture temperature / max_tokens if present
        for extra in ("temperature", "max_tokens", "provider"):
            extra_field = template.get(extra)
            if extra_field and isinstance(extra_field, dict) and extra_field.get("value") is not None:
                config[extra] = extra_field["value"]
        return config

    if node_type == "NemoGuardrails":
        config = {"raw_value": value}
        if isinstance(value, str) and "|" in value:
            parts = [p.strip() for p in value.split("|")]
            if len(parts) >= 2:
                config = {"guardrail_name": parts[0], "guardrail_id": parts[1]}
        for extra in ("enabled", "fail_open"):
            extra_field = template.get(extra)
            if extra_field and isinstance(extra_field, dict) and extra_field.get("value") is not None:
                config[extra] = extra_field["value"]
        return config

    if node_type == "MCPTools":
        if isinstance(value, dict):
            return {"server_name": value.get("name", ""), **{k: v for k, v in value.items() if k != "name"}}
        return {"server_name": str(value)} if value else None

    if node_type == "DatabaseConnector":
        config = {"raw_value": value}
        if isinstance(value, str) and "|" in value:
            parts = [p.strip() for p in value.split("|")]
            if len(parts) >= 4:
                config = {"connector_name": parts[0], "provider": parts[1], "host": parts[2], "connector_id": parts[3]}
        return config

    if node_type in ("Chroma", "Pinecone"):
        config = {"name": value}
        for extra in ("persist_directory", "chroma_server_host", "namespace", "cloud_provider", "cloud_region"):
            extra_field = template.get(extra)
            if extra_field and isinstance(extra_field, dict) and extra_field.get("value"):
                config[extra] = extra_field["value"]
        return config

    return {"value": value} if value else None


def _derive_resource_name(value: str | None, node_type: str) -> str:
    """Derive a human-readable resource_name from the field value."""
    if not value:
        return node_type
    # For "display_name | model_name | uuid" format, use display_name
    if "|" in value:
        return value.split("|")[0].strip()
    return value


async def _extract_and_create_bundles(
    session: AsyncSession,
    *,
    snapshot: dict,
    agent_id: UUID,
    org_id: UUID | None,
    dept_id: UUID | None,
    deployment_id: UUID,
    deployment_env: DeploymentEnvEnum,
    created_by: UUID,
) -> list[AgentBundle]:
    """Parse the frozen snapshot and create AgentBundle rows for every external resource.

    Extracts all 8 resource types:
        MODEL, MCP_SERVER, GUARDRAIL, KNOWLEDGE_BASE, VECTOR_DB,
        CONNECTOR, TOOL, CUSTOM_COMPONENT

    For PROD deployments, guardrails are automatically promoted — a frozen
    production copy is created (or reused) via the guardrails microservice.
    """
    bundles: list[AgentBundle] = []
    seen: set[tuple[str, str]] = set()  # (bundle_type, resource_name) dedup

    for node in snapshot.get("nodes", []):
        node_data = node.get("data", {})
        node_type = node_data.get("type", "")
        template = node_data.get("node", {}).get("template", {})

        if not node_type or not template:
            continue

        # ── Known component types (Model, MCP, Guardrail, Connector, VectorDB, CustomComponent) ──
        if node_type in _NODE_TYPE_TO_BUNDLE:
            bundle_type, field_name = _NODE_TYPE_TO_BUNDLE[node_type]
            raw_value = _extract_field_value(template, field_name)
            resource_name = _derive_resource_name(raw_value, node_type)
            dedup_key = (bundle_type.value, resource_name)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)

            resource_config = _extract_resource_config(template, field_name, node_type) if raw_value else None

            # NOTE: Guardrail promotion is deferred until admin approval.
            # See approvals.py approve_agent() for the actual promotion logic.

            bundles.append(AgentBundle(
                agent_id=agent_id,
                org_id=org_id,
                dept_id=dept_id,
                deployment_id=deployment_id,
                deployment_env=deployment_env,
                bundle_type=bundle_type,
                resource_name=resource_name,
                resource_config=resource_config,
                created_by=created_by,
            ))

        # ── Built-in tools ──
        elif node_type in _TOOL_NODE_TYPES:
            dedup_key = (BundleTypeEnum.TOOL.value, node_type)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)

            display = node_data.get("node", {}).get("display_name", node_type)
            bundles.append(AgentBundle(
                agent_id=agent_id,
                org_id=org_id,
                dept_id=dept_id,
                deployment_id=deployment_id,
                deployment_env=deployment_env,
                bundle_type=BundleTypeEnum.TOOL,
                resource_name=display,
                resource_config={"component_type": node_type},
                created_by=created_by,
            ))

        # ── Knowledge Base (any node referencing a KB field) ──
        kb_field = template.get("knowledge_base") or template.get("knowledge_base_id")
        if kb_field:
            kb_value = _extract_field_value(template, "knowledge_base") or _extract_field_value(template, "knowledge_base_id")
            if kb_value:
                dedup_key = (BundleTypeEnum.KNOWLEDGE_BASE.value, kb_value)
                if dedup_key not in seen:
                    seen.add(dedup_key)
                    bundles.append(AgentBundle(
                        agent_id=agent_id,
                        org_id=org_id,
                        dept_id=dept_id,
                        deployment_id=deployment_id,
                        deployment_env=deployment_env,
                        bundle_type=BundleTypeEnum.KNOWLEDGE_BASE,
                        resource_name=_derive_resource_name(kb_value, "KnowledgeBase"),
                        resource_config={"raw_value": kb_value},
                        created_by=created_by,
                    ))

    # Persist all bundles
    for bundle in bundles:
        session.add(bundle)

    return bundles


async def _get_agent_or_404(
    session: AsyncSession,
    agent_id: UUID,
    user_id: UUID | None = None,
) -> Agent:
    """Fetch an agent by ID, optionally verifying ownership.

    Args:
        session: Database session.
        agent_id: The agent UUID.
        user_id: If provided, also verifies the agent belongs to this user.

    Returns:
        The Agent instance.

    Raises:
        HTTPException 404: If agent not found or does not belong to user.
    """
    stmt = select(Agent).where(Agent.id == agent_id)
    if user_id is not None:
        stmt = stmt.where(Agent.user_id == user_id)

    agent = (await session.exec(stmt)).first()
    if not agent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent {agent_id} not found" + (" or not owned by you" if user_id else ""),
        )
    return agent


async def _find_deploy_record(
    session: AsyncSession,
    deploy_id: UUID,
) -> tuple[AgentDeploymentUAT | AgentDeploymentProd, str]:
    """Find a deployment record in either the UAT or PROD table.

    UUIDs are globally unique, so there is no ambiguity searching both tables.

    Args:
        session: Database session.
        deploy_id: The deployment record UUID.

    Returns:
        Tuple of (record, environment_str) where environment_str is "uat" or "prod".

    Raises:
        HTTPException 404: If the deployment record is not found in either table.
    """
    uat_record = (await session.exec(
        select(AgentDeploymentUAT).where(AgentDeploymentUAT.id == deploy_id)
    )).first()
    if uat_record:
        return uat_record, "uat"

    prod_record = (await session.exec(
        select(AgentDeploymentProd).where(AgentDeploymentProd.id == deploy_id)
    )).first()
    if prod_record:
        return prod_record, "prod"

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Deployment record {deploy_id} not found in UAT or PROD",
    )


def _record_to_summary(record: AgentDeploymentUAT | AgentDeploymentProd, environment: str) -> PublishRecordSummary:
    """Convert a UAT or PROD deployment record to a lightweight summary (no snapshot)."""
    return PublishRecordSummary(
        id=record.id,
        agent_id=record.agent_id,
        version_number=f"v{record.version_number}",
        agent_name=record.agent_name,
        agent_description=record.agent_description,
        publish_description=record.publish_description,
        published_by=record.deployed_by,
        published_at=record.deployed_at,
        is_active=record.is_active,
        is_enabled=record.is_enabled,
        status=record.status.value if hasattr(record.status, "value") else str(record.status),
        visibility=record.visibility.value if hasattr(record.visibility, "value") else str(record.visibility),
        error_message=record.error_message,
        environment=environment,
        promoted_from_uat_id=getattr(record, "promoted_from_uat_id", None),
    )




# ═══════════════════════════════════════════════════════════════════════════
# STATIC LIST ROUTES
# (defined FIRST so FastAPI matches them before parametric /{uuid} routes)
# ═══════════════════════════════════════════════════════════════════════════



async def _notify_publish_event(
    session: DbSession,
    *,
    agent_id: UUID,
    agent_name: str,
    environment: str,
    version_number: int | str,
    publish_id: UUID,
    published_by: UUID,
    published_at: datetime | None,
) -> None:
    """Internal publish notifier with DB verification.

    This helper verifies the deployment row and logs the publish event.
    It intentionally never raises so successful publishes are not blocked.
    """
    try:
        record, record_env = await _find_deploy_record(session, publish_id)
        if record_env != str(environment).lower():
            logger.warning(
                f"[PUBLISH_NOTIFY] env mismatch for {publish_id}: payload={environment}, db={record_env}",
            )
        if record.agent_id != agent_id:
            logger.warning(
                f"[PUBLISH_NOTIFY] agent mismatch for {publish_id}: payload={agent_id}, db={record.agent_id}",
            )

        payload_version = str(version_number).strip()
        if payload_version.lower().startswith("v"):
            payload_version = payload_version[1:]
        if str(record.version_number) != payload_version:
            logger.warning(
                f"[PUBLISH_NOTIFY] version mismatch for {publish_id}: payload=v{version_number}, db=v{record.version_number}",
            )

        logger.info(
            f"[PUBLISH_NOTIFY] agent='{agent_name}' agent_id={agent_id} publish_id={publish_id} "
            f"env={record_env} version=v{record.version_number} by={published_by} "
            f"at={published_at or record.deployed_at}",
        )
    except Exception as notify_err:
        logger.warning(f"[PUBLISH_NOTIFY] failed for publish_id={publish_id}: {notify_err}")

@router.post("/notify", response_model=PublishNotifyVerifiedResponse, status_code=200)
async def publish_notify_verify(
    *,
    body: PublishNotifyRequest,
    session: DbSession,
):
    """DB-verified publish notification endpoint for UAT and PROD deployments.

    Triggered after agent publish (UAT) or approval (PROD). Looks up the
    deployment record in the appropriate table, verifies that the agent_id
    and version match, updates agents.yaml, and returns the full deployment
    details for downstream deployment orchestration.

    Raises:
        404: Deployment record not found.
        409: Mismatch between request payload and DB record.
    """
    env = (body.environment or "prod").lower()

    # 1. Find the deployment record in the appropriate table
    if env == "uat":
        record = (await session.exec(
            select(AgentDeploymentUAT).where(AgentDeploymentUAT.id == body.deployment_id)
        )).first()
    else:
        record = (await session.exec(
            select(AgentDeploymentProd).where(AgentDeploymentProd.id == body.deployment_id)
        )).first()

    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{env.upper()} deployment record {body.deployment_id} not found",
        )

    # 2. Verify agent_id matches
    if record.agent_id != body.agent_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Agent ID mismatch: request says '{body.agent_id}' "
                f"but deployment record has '{record.agent_id}'"
            ),
        )

    # 3. Verify version matches
    payload_version = str(body.version_number).strip()
    if payload_version.lower().startswith("v"):
        payload_version = payload_version[1:]
    if str(record.version_number) != payload_version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Version mismatch: request says 'v{body.version_number}' "
                f"but deployment record has 'v{record.version_number}'"
            ),
        )

    logger.info(
        f"[PUBLISH_NOTIFY] Verified: agent='{record.agent_name}' "
        f"agent_id={record.agent_id} deployment_id={record.id} "
        f"env={env} version=v{record.version_number} "
        f"status={record.status} is_active={record.is_active}",
    )

    # Append core deployment details to agents.yaml.
    # Failure here is non-fatal — the API response is always returned regardless.
    from agentcore.services.manifest import add_manifest_entry

    add_manifest_entry(
        agent_id=str(record.agent_id),
        agent_name=record.agent_name,
        version_number=f"v{record.version_number}",
        environment=env,
        deployment_id=str(record.id),
    )

    return PublishNotifyVerifiedResponse(
        agent_id=record.agent_id,
        agent_name=record.agent_name,
        agent_description=record.agent_description,
        environment=env,
        version_number=f"v{record.version_number}",
        deployment_id=record.id,
        status=record.status.value if hasattr(record.status, "value") else str(record.status),
        is_active=record.is_active,
        deployed_by=record.deployed_by,
        deployed_at=record.deployed_at,
        org_id=record.org_id,
        dept_id=record.dept_id,
        verified=True,
    )


@router.get("/uat", response_model=list[PublishRecordSummary], status_code=200)
async def list_uat_published_agents(
    *,
    session: DbSession,
    current_user: CurrentActiveUser,
    active_only: bool | None = Query(None, description="true = only is_active=True; false = only is_active=False; omit = all"),
    status_filter: DeploymentUATStatusEnum | None = Query(None, alias="status", description="Filter by status"),
):
    """List all UAT-deployed agents.

    Used by the Control Panel to display UAT-deployed agent cards.

    Args:
        session: Async database session.
        current_user: The authenticated user.
        active_only: If true, only return currently active versions.
        status_filter: Optional filter for deployment status.

    Returns:
        List of deployment record summaries (without full snapshot for performance).
    """
    try:
        stmt = select(AgentDeploymentUAT)

        if active_only is not None:
            stmt = stmt.where(AgentDeploymentUAT.is_active == active_only)  # noqa: E712

        if status_filter:
            stmt = stmt.where(AgentDeploymentUAT.status == status_filter)

        stmt = stmt.order_by(col(AgentDeploymentUAT.deployed_at).desc())
        records = (await session.exec(stmt)).all()

        return [_record_to_summary(r, "uat") for r in records]

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing UAT deployed agents: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/prod", response_model=list[PublishRecordSummary], status_code=200)
async def list_prod_published_agents(
    *,
    session: DbSession,
    current_user: CurrentActiveUser,
    active_only: bool | None = Query(None, description="true = only is_active=True; false = only is_active=False; omit = all"),
    status_filter: DeploymentPRODStatusEnum | None = Query(None, alias="status", description="Filter by status"),
):
    """List all PROD-deployed agents.

    Used by the Control Panel and Agent Registry to display PROD-deployed agents.

    Args:
        session: Async database session.
        current_user: The authenticated user.
        active_only: If true, only return currently active versions.
        status_filter: Optional filter for deployment status.

    Returns:
        List of deployment record summaries (without full snapshot for performance).
    """
    try:
        stmt = select(AgentDeploymentProd)

        if active_only is not None:
            stmt = stmt.where(AgentDeploymentProd.is_active == active_only)  # noqa: E712

        if status_filter:
            stmt = stmt.where(AgentDeploymentProd.status == status_filter)

        stmt = stmt.order_by(col(AgentDeploymentProd.deployed_at).desc())
        records = (await session.exec(stmt)).all()

        return [_record_to_summary(r, "prod") for r in records]

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing PROD deployed agents: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


# ═══════════════════════════════════════════════════════════════════════════
# UAT DEPLOYMENT ACTIONS (unified)
# ═══════════════════════════════════════════════════════════════════════════


@router.post("/uat/{deploy_id}/action", response_model=PublishActionResponse, status_code=200)
async def uat_deploy_action(
    *,
    session: DbSession,
    deploy_id: UUID,
    body: DeployActionRequest,
    current_user: CurrentActiveUser,
):
    """Update a UAT deployment record.

    Send the fields you want to change as key-value pairs:

    | Goal        | Payload                                          |
    |-------------|--------------------------------------------------|
    | Unpublish   | `{"status": "UNPUBLISHED", "is_active": false}`   |
    | Activate    | `{"is_active": true}`                             |
    | Deactivate  | `{"is_active": false}`                            |
    | Republish   | `{"status": "PUBLISHED", "is_active": true}`      |

    Permission: deployer (owner) or admin/manager role.
    """
    try:
        record = (await session.exec(
            select(AgentDeploymentUAT).where(AgentDeploymentUAT.id == deploy_id)
        )).first()
        if not record:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"UAT deployment record {deploy_id} not found",
            )

        new_status = body.status.upper() if body.status else None
        new_is_active = body.is_active
        current_status_val = record.status.value if hasattr(record.status, "value") else str(record.status)
        changes: list[str] = []

        # ── Validate & apply status change ──
        if new_status is not None and new_status != current_status_val:
            if new_status == "UNPUBLISHED":
                record.status = DeploymentUATStatusEnum.UNPUBLISHED
                # Force is_active=False when unpublishing (unless explicitly overridden)
                if new_is_active is None:
                    new_is_active = False
                changes.append("status → UNPUBLISHED")

            elif new_status == "PUBLISHED":
                if current_status_val != "UNPUBLISHED":
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=f"Cannot set status to PUBLISHED from '{current_status_val}'. "
                               f"Only UNPUBLISHED records can be republished.",
                    )
                # Deactivate other active versions for this agent (republish semantics)
                existing_active = (await session.exec(
                    select(AgentDeploymentUAT).where(
                        AgentDeploymentUAT.agent_id == record.agent_id,
                        AgentDeploymentUAT.id != record.id,
                        AgentDeploymentUAT.is_active == True,  # noqa: E712
                    )
                )).all()
                for rec in existing_active:
                    rec.is_active = False
                    session.add(rec)

                record.status = DeploymentUATStatusEnum.PUBLISHED
                if new_is_active is None:
                    new_is_active = True
                changes.append("status → PUBLISHED")

            else:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Invalid status '{new_status}'. Allowed: PUBLISHED, UNPUBLISHED.",
                )

        # ── Validate & apply is_active change ──
        if new_is_active is not None and new_is_active != record.is_active:
            effective_status = (
                record.status.value if hasattr(record.status, "value") else str(record.status)
            )
            if new_is_active is True and effective_status != "PUBLISHED":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Cannot activate a version with status '{effective_status}'. "
                           f"Only PUBLISHED versions can be activated.",
                )
            record.is_active = new_is_active
            changes.append(f"is_active → {new_is_active}")

        if not changes:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="No changes requested. Send at least one of: status, is_active.",
            )

        session.add(record)
        await session.commit()
        await session.refresh(record)

        # ─── Sync agent registry after any UAT change ──
        try:
            agent = (await session.exec(select(Agent).where(Agent.id == record.agent_id))).first()
            org_id = agent.org_id if agent else None
            await sync_agent_registry(
                session,
                agent_id=record.agent_id,
                org_id=org_id,
                acted_by=current_user.id,
                deployment_env=RegistryDeploymentEnvEnum.UAT,
            )
            await session.commit()
        except Exception as reg_err:
            logger.warning(f"Registry sync failed after update of UAT agent {record.agent_id}: {reg_err}")

        msg = f"UAT v{record.version_number} updated: {', '.join(changes)}"
        logger.info(f"{msg} | deploy_id={deploy_id} user={current_user.id}")

        # ─── Sync agents.yaml on publish/unpublish/activate/deactivate ──
        from agentcore.services.manifest import add_manifest_entry, remove_manifest_entry
        effective_status = record.status.value if hasattr(record.status, "value") else str(record.status)
        if record.is_active and effective_status == "PUBLISHED":
            add_manifest_entry(
                agent_id=str(record.agent_id),
                agent_name=record.agent_name,
                version_number=f"v{record.version_number}",
                environment="uat",
                deployment_id=str(record.id),
            )
        else:
            remove_manifest_entry(deployment_id=str(record.id))

        return PublishActionResponse(
            success=True,
            message=msg,
            publish_id=record.id,
            environment="uat",
            status=record.status.value if hasattr(record.status, "value") else str(record.status),
            is_active=record.is_active,
            version_number=f"v{record.version_number}",
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating UAT record {deploy_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


# ═══════════════════════════════════════════════════════════════════════════
# PROD DEPLOYMENT ACTIONS (unified)
# ═══════════════════════════════════════════════════════════════════════════


@router.post("/prod/{deploy_id}/action", response_model=PublishActionResponse, status_code=200)
async def prod_deploy_action(
    *,
    session: DbSession,
    deploy_id: UUID,
    body: DeployActionRequest,
    current_user: CurrentActiveUser,
):
    """Update a PROD deployment record.

    Send the fields you want to change as key-value pairs:

    | Goal        | Payload                                          |
    |-------------|--------------------------------------------------|
    | Unpublish   | `{"status": "UNPUBLISHED", "is_active": false}`   |
    | Activate    | `{"is_active": true}`                             |
    | Deactivate  | `{"is_active": false}`                            |
    | Republish   | `{"status": "PUBLISHED", "is_active": true}`      |

    Permission: any authenticated user.
    """
    try:
        record = (await session.exec(
            select(AgentDeploymentProd).where(AgentDeploymentProd.id == deploy_id)
        )).first()
        if not record:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"PROD deployment record {deploy_id} not found",
            )

        new_status = body.status.upper() if body.status else None
        new_is_active = body.is_active
        current_status_val = record.status.value if hasattr(record.status, "value") else str(record.status)
        changes: list[str] = []

        # ── Validate & apply status change ──
        if new_status is not None and new_status != current_status_val:
            if new_status == "UNPUBLISHED":
                record.status = DeploymentPRODStatusEnum.UNPUBLISHED
                if new_is_active is None:
                    new_is_active = False
                changes.append("status → UNPUBLISHED")

            elif new_status == "PUBLISHED":
                if current_status_val != "UNPUBLISHED":
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=f"Cannot set status to PUBLISHED from '{current_status_val}'. "
                               f"Only UNPUBLISHED records can be republished.",
                    )
                # Shadow deployment: keep other active versions running.

                record.status = DeploymentPRODStatusEnum.PUBLISHED
                if new_is_active is None:
                    new_is_active = True
                changes.append("status → PUBLISHED")

            else:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Invalid status '{new_status}'. Allowed: PUBLISHED, UNPUBLISHED.",
                )

        # ── Validate & apply is_active change ──
        if new_is_active is not None and new_is_active != record.is_active:
            effective_status = (
                record.status.value if hasattr(record.status, "value") else str(record.status)
            )
            if new_is_active is True and effective_status != "PUBLISHED":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Cannot activate a version with status '{effective_status}'. "
                           f"Only PUBLISHED versions can be activated.",
                )
            record.is_active = new_is_active
            changes.append(f"is_active → {new_is_active}")

        if not changes:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="No changes requested. Send at least one of: status, is_active.",
            )

        session.add(record)
        await session.commit()
        await session.refresh(record)

        # ─── Sync agent registry after any PROD change ──
        try:
            agent = (await session.exec(select(Agent).where(Agent.id == record.agent_id))).first()
            org_id = agent.org_id if agent else None
            await sync_agent_registry(
                session,
                agent_id=record.agent_id,
                org_id=org_id,
                acted_by=current_user.id,
                deployment_env=RegistryDeploymentEnvEnum.PROD,
            )
            await session.commit()
        except Exception as reg_err:
            logger.warning(f"Registry sync failed after update of agent {record.agent_id}: {reg_err}")

        msg = f"PROD v{record.version_number} updated: {', '.join(changes)}"
        logger.info(f"{msg} | deploy_id={deploy_id} user={current_user.id}")

        return PublishActionResponse(
            success=True,
            message=msg,
            publish_id=record.id,
            environment="prod",
            status=record.status.value if hasattr(record.status, "value") else str(record.status),
            is_active=record.is_active,
            version_number=f"v{record.version_number}",
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating PROD record {deploy_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


# ═══════════════════════════════════════════════════════════════════════════
# UAT/PROD DELETE (archive) ENDPOINTS


@router.delete("/uat/{deploy_id}", response_model=PublishActionResponse, status_code=200)
async def delete_uat_deployment(
    *,
    session: DbSession,
    deploy_id: UUID,
    current_user: CurrentActiveUser,
):
    """Archive (soft-delete) a UAT deployment version.

    Requires the deployment to be disabled in Control Panel and stopped.
    """
    record = (
        await session.exec(
            select(AgentDeploymentUAT).where(AgentDeploymentUAT.id == deploy_id)
        )
    ).first()
    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"UAT deployment record {deploy_id} not found",
        )

    dept_id = record.dept_id
    if dept_id is None:
        agent = await session.get(Agent, record.agent_id)
        dept_id = agent.dept_id if agent else None
    await _require_undeploy_permission(
        session,
        current_user=current_user,
        department_id=dept_id,
    )

    status_val = record.status.value if hasattr(record.status, "value") else str(record.status)
    if record.is_enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Disable the agent in Control Panel before deleting this version. "
                f"Current: status={status_val}, is_active={record.is_active}, is_enabled={record.is_enabled}."
            ),
        )
    if record.lifecycle_step != DeploymentLifecycleEnum.ARCHIVED:
        record.lifecycle_step = DeploymentLifecycleEnum.ARCHIVED
    if record.status != DeploymentUATStatusEnum.UNPUBLISHED:
        record.status = DeploymentUATStatusEnum.UNPUBLISHED
    record.is_enabled = False
    record.is_active = False
    record.updated_at = datetime.now(timezone.utc)
    session.add(record)
    await session.commit()
    await session.refresh(record)

    return PublishActionResponse(
        success=True,
        message=f"UAT v{record.version_number} archived",
        publish_id=record.id,
        environment="uat",
        status=record.status.value if hasattr(record.status, "value") else str(record.status),
        is_active=record.is_active,
        version_number=f"v{record.version_number}",
    )


@router.delete("/prod/{deploy_id}", response_model=PublishActionResponse, status_code=200)
async def delete_prod_deployment(
    *,
    session: DbSession,
    deploy_id: UUID,
    current_user: CurrentActiveUser,
):
    """Archive (soft-delete) a PROD deployment version.

    Requires the deployment to be disabled in Control Panel and stopped.
    """
    record = (
        await session.exec(
            select(AgentDeploymentProd).where(AgentDeploymentProd.id == deploy_id)
        )
    ).first()
    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"PROD deployment record {deploy_id} not found",
        )

    dept_id = record.dept_id
    if dept_id is None:
        agent = await session.get(Agent, record.agent_id)
        dept_id = agent.dept_id if agent else None
    await _require_undeploy_permission(
        session,
        current_user=current_user,
        department_id=dept_id,
    )

    status_val = record.status.value if hasattr(record.status, "value") else str(record.status)
    if record.is_enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Disable the agent in Control Panel before deleting this version. "
                f"Current: status={status_val}, is_active={record.is_active}, is_enabled={record.is_enabled}."
            ),
        )
    if record.lifecycle_step != ProdDeploymentLifecycleEnum.ARCHIVED:
        record.lifecycle_step = ProdDeploymentLifecycleEnum.ARCHIVED
    if record.status != DeploymentPRODStatusEnum.UNPUBLISHED:
        record.status = DeploymentPRODStatusEnum.UNPUBLISHED
    record.is_enabled = False
    record.is_active = False
    record.updated_at = datetime.now(timezone.utc)
    session.add(record)
    await session.commit()
    await session.refresh(record)

    return PublishActionResponse(
        success=True,
        message=f"PROD v{record.version_number} archived",
        publish_id=record.id,
        environment="prod",
        status=record.status.value if hasattr(record.status, "value") else str(record.status),
        is_active=record.is_active,
        version_number=f"v{record.version_number}",
    )
async def _track_pinecone_for_uat(
    session,
    snapshot: dict,
    agent_id: UUID,
    agent_name: str,
    org_id: UUID | None,
    dept_id: UUID | None,
) -> None:
    """Scan snapshot for Pinecone nodes and create/update VectorDBCatalogue UAT entries.

    Called once when an agent is published to UAT. Records the index/namespace
    in the catalogue so it's visible in the Vector Store Observatory.
    """
    from sqlmodel import select
    from agentcore.services.database.models.vector_db_catalogue.model import VectorDBCatalogue

    nodes = snapshot.get("nodes", [])
    now = datetime.now(timezone.utc)

    for node in nodes:
        node_data = node.get("data", {})
        if node_data.get("type", "") != "Pinecone":
            continue

        template = node_data.get("node", {}).get("template", {})
        index_name_field = template.get("index_name", {})
        namespace_field = template.get("namespace", {})

        index_name = index_name_field.get("value", "") if isinstance(index_name_field, dict) else str(index_name_field)
        namespace = namespace_field.get("value", "") if isinstance(namespace_field, dict) else str(namespace_field)

        if not index_name:
            continue

        # Fetch live vector count from Pinecone
        live_vector_count = "0"
        live_dimensions = ""
        try:
            from agentcore.services.pinecone_service_client import (
                async_namespace_stats_via_service,
                is_service_configured,
            )
            if is_service_configured():
                stats = await async_namespace_stats_via_service(index_name, namespace)
                live_vector_count = str(stats.get("vector_count", 0))
                live_dimensions = str(stats.get("dimension", ""))
        except Exception as stats_err:
            logger.warning(
                "[VDB_UAT_TRACK] Failed to fetch live stats for index=%s ns=%s: %s",
                index_name, namespace, stats_err,
            )

        # Check if a UAT catalogue entry already exists for this index/namespace
        existing = (
            await session.exec(
                select(VectorDBCatalogue).where(
                    VectorDBCatalogue.index_name == index_name,
                    VectorDBCatalogue.namespace == namespace,
                    VectorDBCatalogue.environment == "uat",
                ).limit(1)
            )
        ).first()

        if existing:
            existing.agent_id = agent_id
            existing.agent_name = agent_name
            existing.org_id = org_id
            existing.dept_id = dept_id
            existing.vector_count = live_vector_count
            if live_dimensions:
                existing.dimensions = live_dimensions
            existing.updated_at = now
            session.add(existing)
            logger.info("[VDB_UAT_TRACK] Updated UAT entry: index=%s ns=%s vectors=%s", index_name, namespace, live_vector_count)
        else:
            entry = VectorDBCatalogue(
                name=f"{index_name}/{namespace}" if namespace else index_name,
                description=f"UAT namespace for agent '{agent_name}'",
                provider="Pinecone",
                deployment="SaaS",
                dimensions=live_dimensions,
                index_type="serverless",
                status="connected",
                vector_count=live_vector_count,
                is_custom=False,
                environment="uat",
                index_name=index_name,
                namespace=namespace,
                agent_id=agent_id,
                agent_name=agent_name,
                org_id=org_id,
                dept_id=dept_id,
                created_at=now,
                updated_at=now,
            )
            session.add(entry)
            logger.info("[VDB_UAT_TRACK] Created UAT entry: index=%s ns=%s vectors=%s", index_name, namespace, live_vector_count)

    await session.flush()


# UNIFIED PUBLISH ENDPOINT
# ═══════════════════════════════════════════════════════════════════════════


@router.post("/{agent_id}", response_model=PublishActionResponse, status_code=201)
async def publish_agent(
    *,
    session: DbSession,
    agent_id: UUID,
    body: PublishRequest,
    current_user: CurrentActiveUser,
):
    """Publish an agent to UAT or PROD (single unified endpoint).

    The frontend sends a single payload with all required context:
        - agent_id (path): which agent to publish
        - department_id: the department the agent belongs to
        - department_admin_id: the department admin to whom the request is directed
        - visibility: PUBLIC or PRIVATE
        - environment: "uat" or "prod"
        - publish_description: optional release notes

    Behaviour:
        **UAT** — always direct deploy for any role with publish permission.
        **PROD + admin/manager** — direct deploy, no approval needed.
        **PROD + developer** — creates a PENDING_APPROVAL record and an
        approval_request targeting the supplied department_admin_id.

    Returns:
        PublishActionResponse with the deployment record details.
    """
    try:
        agent = await _get_agent_or_404(session, agent_id, current_user.id)
        env = body.environment.value  # "uat" or "prod"

        if not agent.data:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot deploy agent with no flow data. Build the agent first.",
            )

        allow_departmentless_private_publish = (
            env == "uat"
            and str(body.visibility).strip().upper() == "PRIVATE"
            and str(getattr(current_user, "role", "")).lower() in {"root", "super_admin", "admin"}
        )

        resolved_department_id, resolved_department_admin_id = await _resolve_publish_scope(
            session,
            current_user=current_user,
            agent=agent,
            requested_department_id=body.department_id,
            requested_department_admin_id=body.department_admin_id,
            allow_departmentless_private_publish=allow_departmentless_private_publish,
        )
        recipient_emails = _normalize_recipient_emails(body.recipient_emails)
        if resolved_department_id is None and recipient_emails:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "Recipient emails require a department-scoped publish. "
                    "Publish privately without recipients or publish into a department."
                ),
            )
        await _validate_and_store_publish_recipients(
            session=session,
            agent=agent,
            department_id=resolved_department_id,
            current_user=current_user,
            recipient_emails=recipient_emails,
        )

        # Freeze snapshot — immutable copy of the current agent flow
        snapshot = agent.data.copy()
        promoted_from_uat_id = body.promoted_from_uat_id
        promoted_uat_version_number: int | None = None
        published_agent_name = await _resolve_published_agent_name(
            session,
            agent_id=agent_id,
            requested_name=body.published_agent_name,
        )

        # ── Validate & resolve UAT promotion ──
        if promoted_from_uat_id is not None:
            if env != "prod":
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="promoted_from_uat_id is only valid when environment='prod'.",
                )
            uat_record = (await session.exec(
                select(AgentDeploymentUAT).where(AgentDeploymentUAT.id == promoted_from_uat_id)
            )).first()
            if not uat_record:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"UAT deployment {promoted_from_uat_id} not found.",
                )
            if uat_record.agent_id != agent_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"UAT deployment {promoted_from_uat_id} belongs to agent {uat_record.agent_id}, "
                        f"not {agent_id}."
                    ),
                )
            # Use the UAT-tested snapshot instead of the current draft
            snapshot = uat_record.agent_snapshot.copy()
            promoted_uat_version_number = uat_record.version_number
            logger.info(
                f"Promoting from UAT v{uat_record.version_number} ({promoted_from_uat_id}) "
                f"to PROD for agent {agent_id}"
            )
            uat_record.moved_to_prod = True
            uat_record.updated_at = datetime.now(timezone.utc)
            session.add(uat_record)

        # ── Derive agent input type from snapshot nodes ──────────
        _node_types = {n.get("data", {}).get("type") for n in snapshot.get("nodes", [])}
        if "ChatInput" in _node_types:
            snapshot["_input_type"] = "chat"
        elif _node_types & {"FolderMonitor", "FileTrigger"}:
            snapshot["_input_type"] = "file_processing"
        else:
            snapshot["_input_type"] = "autonomous"

        # Capture org_id before any commits to avoid session expiry issues
        agent_org_id = agent.org_id

        if env == "uat":
            # ─── UAT: always direct deploy ───────────────────────
            next_version = await _get_next_version_number(session, agent_id, AgentDeploymentUAT)

            visibility_enum = DeploymentVisibilityEnum(body.visibility.upper())
            if agent_org_id is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Organization is required for publishing.",
                )

            new_record = AgentDeploymentUAT(
                agent_id=agent_id,
                org_id=agent.org_id,
                dept_id=resolved_department_id,
                version_number=next_version,
                agent_snapshot=snapshot,
                agent_name=published_agent_name,
                agent_description=agent.description,
                publish_description=body.publish_description,
                deployed_by=current_user.id,
                deployed_at=datetime.now(timezone.utc),
                is_active=True,
                status=DeploymentUATStatusEnum.PUBLISHED,
                visibility=visibility_enum,
            )
            session.add(new_record)

            # Deactivate all previous versions for this agent
            existing_records = (await session.exec(
                select(AgentDeploymentUAT).where(
                    AgentDeploymentUAT.agent_id == agent_id,
                    AgentDeploymentUAT.id != new_record.id,
                    AgentDeploymentUAT.is_active == True,  # noqa: E712
                )
            )).all()
            for rec in existing_records:
                rec.is_active = False
                session.add(rec)

            await session.commit()
            await session.refresh(new_record)

            # ─── Auto-generate API key for this UAT deployment ──
            plaintext_key, key_hash, key_prefix = generate_agent_api_key()
            api_key_record = AgentApiKey(
                agent_id=agent_id,
                deployment_id=new_record.id,
                version=f"v{next_version}",
                environment="uat",
                key_hash=key_hash,
                key_prefix=key_prefix,
                is_active=True,
                created_by=current_user.id,
                created_at=datetime.now(timezone.utc),
            )
            session.add(api_key_record)
            await session.commit()
            logger.info(f"Generated API key (prefix={key_prefix}) for UAT deploy {new_record.id} v{next_version}")

            logger.info(
                f"Deployed agent '{published_agent_name}' ({agent_id}) to UAT as v{next_version} "
                f"by user {current_user.id} [dept={resolved_department_id}]"
            )

            # ─── Create agent bundle rows from snapshot ──
            try:
                bundles = await _extract_and_create_bundles(
                    session,
                    snapshot=snapshot,
                    agent_id=agent_id,
                    org_id=agent_org_id,
                    dept_id=resolved_department_id,
                    deployment_id=new_record.id,
                    deployment_env=DeploymentEnvEnum.UAT,
                    created_by=current_user.id,
                )
                if bundles:
                    await session.commit()
                    logger.info(f"Created {len(bundles)} bundle(s) for UAT deploy {new_record.id}")
            except Exception as bundle_err:
                logger.warning(f"Bundle extraction failed for UAT deploy of {agent_id}: {bundle_err}")


            # ─── Track Pinecone indexes in vector catalogue ──
            try:
                await _track_pinecone_for_uat(
                    session=session,
                    snapshot=snapshot,
                    agent_id=agent_id,
                    agent_name=published_agent_name,
                    org_id=agent.org_id,
                    dept_id=resolved_department_id,
                )
                await session.commit()
            except Exception as vdb_err:
                logger.warning(f"Vector catalogue tracking failed for UAT deploy of {agent_id}: {vdb_err}")

            # Sync FileTrigger nodes → auto-create trigger_config entries
            try:
                from agentcore.services.deps import get_trigger_service
                trigger_svc = get_trigger_service()
                await trigger_svc.sync_folder_monitors_for_agent(
                    session=session,
                    agent_id=agent_id,
                    environment="uat",
                    version=f"v{next_version}",
                    deployment_id=new_record.id,
                    flow_data=snapshot,
                    created_by=current_user.id,
                )
            except Exception as sched_err:
                logger.warning(f"FileTrigger sync failed for UAT deploy of {agent_id}: {sched_err}")

            # ─── Sync agent registry after UAT publish ──
            try:
                await sync_agent_registry(
                    session,
                    agent_id=agent_id,
                    org_id=agent.org_id,
                    acted_by=current_user.id,
                    deployment_env=RegistryDeploymentEnvEnum.UAT,
                )
                await session.commit()
            except Exception as reg_err:
                logger.warning(f"Registry sync failed after UAT publish of {agent_id}: {reg_err}")

            # ─── Publish notification (DB-verified) ──
            await _notify_publish_event(
                session,
                agent_id=agent_id,
                agent_name=published_agent_name,
                environment="uat",
                version_number=next_version,
                publish_id=new_record.id,
                published_by=current_user.id,
                published_at=new_record.deployed_at,
            )

            # ─── HTTP notify (manifest update + downstream deployment orchestration) ──
            try:
                import httpx
                from agentcore.services.deps import get_settings_service
                settings = get_settings_service().settings
                base_url = f"http://{settings.host}:{settings.port}"
                payload = {
                    "agent_id": str(agent_id),
                    "environment": "uat",
                    "version_number": str(next_version),
                    "deployment_id": str(new_record.id),
                }
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.post(f"{base_url}/api/publish/notify", json=payload)
                    resp.raise_for_status()
                    verified = resp.json()
                logger.info(
                    f"[UAT_NOTIFY] API triggered: agent={verified.get('agent_name')} "
                    f"deployment_id={verified.get('deployment_id')} "
                    f"version={verified.get('version_number')} "
                    f"status={verified.get('status')} is_active={verified.get('is_active')}",
                )
            except Exception as notify_err:
                logger.warning(f"Post-deploy notify API failed for UAT deploy of {agent_id}: {notify_err}")

            return PublishActionResponse(
                success=True,
                message=f"Agent '{published_agent_name}' deployed to UAT as v{next_version}",
                publish_id=new_record.id,
                environment="uat",
                status=new_record.status.value,
                is_active=True,
                version_number=f"v{next_version}",
                api_key=plaintext_key,
            )

        else:
            # ─── PROD ────────────────────────────────────────────
            next_version = (
                promoted_uat_version_number
                if promoted_uat_version_number is not None
                else await _get_next_version_number(session, agent_id, AgentDeploymentProd)
            )
            role = str(getattr(current_user, "role", "")).lower()
            is_admin = role in ADMIN_ROLES

            visibility_enum = ProdDeploymentVisibilityEnum(body.visibility.upper())

            if promoted_uat_version_number is not None:
                existing_prod_version = (
                    await session.exec(
                        select(AgentDeploymentProd).where(
                            AgentDeploymentProd.agent_id == agent_id,
                            AgentDeploymentProd.version_number == next_version,
                        )
                    )
                ).first()
                if existing_prod_version is not None:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=(
                            f"PROD version v{next_version} already exists for agent {agent_id}. "
                            "This UAT version has already been promoted or the version number is in use."
                        ),
                    )

            if is_admin:
                # Validate all models and MCP servers are available for PROD
                await _validate_resources_for_prod(snapshot, session)
                # Admin/manager: direct deploy
                new_record = AgentDeploymentProd(
                    agent_id=agent_id,
                    org_id=agent.org_id,
                    dept_id=resolved_department_id,
                    promoted_from_uat_id=promoted_from_uat_id,
                    version_number=next_version,
                    agent_snapshot=snapshot,
                    agent_name=published_agent_name,
                    agent_description=agent.description,
                    publish_description=body.publish_description,
                    deployed_by=current_user.id,
                    deployed_at=datetime.now(timezone.utc),
                    is_active=True,
                    status=DeploymentPRODStatusEnum.PUBLISHED,
                    lifecycle_step=ProdDeploymentLifecycleEnum.PUBLISHED,
                    visibility=visibility_enum,
                )
                session.add(new_record)

                # Update agent lifecycle_status to PUBLISHED
                agent.lifecycle_status = LifecycleStatusEnum.PUBLISHED
                session.add(agent)

                # Shadow deployment: keep previous versions active so
                # multiple versions can run side-by-side.

                await session.commit()
                await session.refresh(new_record)

                # ─── Auto-generate API key for this PROD deployment ──
                plaintext_key, key_hash, key_prefix = generate_agent_api_key()
                api_key_record = AgentApiKey(
                    agent_id=agent_id,
                    deployment_id=new_record.id,
                    version=f"v{next_version}",
                    environment="prod",
                    key_hash=key_hash,
                    key_prefix=key_prefix,
                    is_active=True,
                    created_by=current_user.id,
                    created_at=datetime.now(timezone.utc),
                )
                session.add(api_key_record)
                await session.commit()
                logger.info(f"Generated API key (prefix={key_prefix}) for PROD deploy {new_record.id} v{next_version}")

                logger.info(
                    f"Admin direct-deployed agent '{published_agent_name}' ({agent_id}) to PROD "
                    f"as v{next_version} by {current_user.id} [dept={resolved_department_id}]"
                )

                # ─── Create agent bundle rows from snapshot ──
                try:
                    _snapshot_node_types = [
                        n.get("data", {}).get("type", "?") for n in snapshot.get("nodes", [])
                    ]
                    logger.info(
                        f"[BUNDLE_DEBUG] PROD admin deploy {new_record.id}: "
                        f"snapshot has {len(snapshot.get('nodes', []))} nodes, "
                        f"types={_snapshot_node_types}"
                    )
                    bundles = await _extract_and_create_bundles(
                        session,
                        snapshot=snapshot,
                        agent_id=agent_id,
                        org_id=agent_org_id,
                        dept_id=resolved_department_id,
                        deployment_id=new_record.id,
                        deployment_env=DeploymentEnvEnum.PROD,
                        created_by=current_user.id,
                    )
                    logger.info(f"[BUNDLE_DEBUG] PROD admin: _extract_and_create_bundles returned {len(bundles)} bundle(s)")
                    if bundles:
                        await session.commit()
                        logger.info(f"Created {len(bundles)} bundle(s) for PROD deploy {new_record.id}")
                    else:
                        logger.info(f"No bundles extracted from snapshot for PROD deploy {new_record.id}")
                except Exception as bundle_err:
                    logger.error(f"[BUNDLE_DEBUG] Bundle extraction FAILED for PROD deploy of {agent_id}: {bundle_err}", exc_info=True)

                # ─── Sync agent registry after PROD admin publish ──
                try:
                    await sync_agent_registry(
                        session,
                        agent_id=agent_id,
                        org_id=agent.org_id,
                        acted_by=current_user.id,
                        deployment_env=RegistryDeploymentEnvEnum.PROD,
                    )
                    await session.commit()
                except Exception as reg_err:
                    logger.warning(f"Registry sync failed after PROD publish of {agent_id}: {reg_err}")

                # Sync FileTrigger nodes → auto-create trigger_config entries
                try:
                    from agentcore.services.deps import get_trigger_service
                    trigger_svc = get_trigger_service()
                    await trigger_svc.sync_folder_monitors_for_agent(
                        session=session,
                        agent_id=agent_id,
                        environment="prod",
                        version=f"v{next_version}",
                        deployment_id=new_record.id,
                        flow_data=snapshot,
                        created_by=current_user.id,
                    )
                except Exception as fm_err:
                    logger.warning(f"FileTrigger sync failed for PROD deploy of {agent_id}: {fm_err}")

                # ─── Publish notification (DB-verified) ──
                await _notify_publish_event(
                    session,
                    agent_id=agent_id,
                    agent_name=published_agent_name,
                    environment="prod",
                    version_number=next_version,
                    publish_id=new_record.id,
                    published_by=current_user.id,
                    published_at=new_record.deployed_at,
                )

                # ─── HTTP notify (for downstream deployment orchestration) ──
                try:
                    import httpx
                    from agentcore.services.deps import get_settings_service
                    settings = get_settings_service().settings
                    base_url = f"http://{settings.host}:{settings.port}"
                    payload = {
                        "agent_id": str(agent_id),
                        "environment": "prod",
                        "version_number": str(next_version),
                        "deployment_id": str(new_record.id),
                    }
                    async with httpx.AsyncClient(timeout=10) as client:
                        resp = await client.post(f"{base_url}/api/publish/notify", json=payload)
                        resp.raise_for_status()
                        verified = resp.json()
                    logger.info(
                        f"[PROD_ADMIN_NOTIFY] API triggered: agent={verified.get('agent_name')} "
                        f"deployment_id={verified.get('deployment_id')} "
                        f"version={verified.get('version_number')} "
                        f"status={verified.get('status')} is_active={verified.get('is_active')}",
                    )
                except Exception as notify_err:
                    logger.warning(f"Post-deploy notify API failed for PROD deploy of {agent_id}: {notify_err}")

                return PublishActionResponse(
                    success=True,
                    message=f"Agent '{published_agent_name}' deployed to PROD as v{next_version}",
                    publish_id=new_record.id,
                    environment="prod",
                    status=DeploymentPRODStatusEnum.PUBLISHED.value,
                    is_active=True,
                    version_number=f"v{next_version}",
                    promoted_from_uat_id=promoted_from_uat_id,
                    api_key=plaintext_key,
                )

            else:
                # Validate all models and MCP servers are available for PROD
                await _validate_resources_for_prod(snapshot, session)
                # Developer: create PENDING_APPROVAL + approval_request
                new_record = AgentDeploymentProd(
                    agent_id=agent_id,
                    org_id=agent.org_id,
                    dept_id=resolved_department_id,
                    promoted_from_uat_id=promoted_from_uat_id,
                    version_number=next_version,
                    agent_snapshot=snapshot,
                    agent_name=published_agent_name,
                    agent_description=agent.description,
                    publish_description=body.publish_description,
                    deployed_by=current_user.id,
                    deployed_at=datetime.now(timezone.utc),
                    is_active=False,
                    status=DeploymentPRODStatusEnum.PENDING_APPROVAL,
                    visibility=visibility_enum,
                )
                session.add(new_record)

                # Update agent lifecycle_status to PENDING_APPROVAL
                agent.lifecycle_status = LifecycleStatusEnum.PENDING_APPROVAL
                session.add(agent)

                await session.flush()  # get new_record.id

                # Create approval_request targeting the supplied department admin
                approval = ApprovalRequest(
                    agent_id=agent_id,
                    deployment_id=new_record.id,
                    org_id=agent.org_id,
                    dept_id=resolved_department_id,
                    requested_by=current_user.id,
                    request_to=resolved_department_admin_id,
                    requested_at=datetime.now(timezone.utc),
                    visibility_requested=visibility_enum,
                    publish_description=body.publish_description,
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
                session.add(approval)
                await session.flush()
                await upsert_approval_notification(
                    session,
                    recipient_user_id=resolved_department_admin_id,
                    entity_type="agent_publish_request",
                    entity_id=str(approval.id),
                    title=f'Agent "{published_agent_name}" awaiting your approval.',
                    link="/approval",
                )

                # Link approval back to deployment record
                new_record.approval_id = approval.id
                session.add(new_record)

                await session.commit()
                await session.refresh(new_record)

                logger.info(
                    f"Developer {current_user.id} submitted agent '{published_agent_name}' ({agent_id}) "
                    f"for PROD approval as v{next_version}. "
                    f"Approval sent to dept admin {resolved_department_admin_id} [dept={resolved_department_id}]"
                )

                # ─── Create agent bundle rows from snapshot ──
                try:
                    bundles = await _extract_and_create_bundles(
                        session,
                        snapshot=snapshot,
                        agent_id=agent_id,
                        org_id=agent_org_id,
                        dept_id=resolved_department_id,
                        deployment_id=new_record.id,
                        deployment_env=DeploymentEnvEnum.PROD,
                        created_by=current_user.id,
                    )
                    if bundles:
                        await session.commit()
                        logger.info(f"Created {len(bundles)} bundle(s) for PROD pending-approval deploy {new_record.id}")
                    else:
                        logger.info(f"No bundles extracted from snapshot for PROD pending-approval deploy {new_record.id}")
                except Exception as bundle_err:
                    logger.warning(f"Bundle extraction failed for PROD deploy of {agent_id}: {bundle_err}", exc_info=True)

                return PublishActionResponse(
                    success=True,
                    message=f"Agent '{published_agent_name}' submitted for PROD approval as v{next_version}. "
                            f"Awaiting department admin review.",
                    publish_id=new_record.id,
                    environment="prod",
                    status=DeploymentPRODStatusEnum.PENDING_APPROVAL.value,
                    is_active=False,
                    version_number=f"v{next_version}",
                    promoted_from_uat_id=promoted_from_uat_id,
                )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deploying agent {agent_id} to {body.environment.value}: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/{agent_id}/status", response_model=AgentPublishStatusResponse, status_code=200)
async def get_agent_publish_status(
    *,
    session: DbSession,
    agent_id: UUID,
    current_user: CurrentActiveUser,
):
    """Get deployment status for an agent across both environments.

    Returns the latest active deployment record (if any) for UAT and PROD,
    plus whether there's a pending PROD approval.

    Used by the UI to show deploy badges on agent cards:
        🟢 UAT (live)  |  🔵 PROD (live)  |  🟡 PROD (pending)

    Args:
        session: Async database session.
        agent_id: UUID of the agent.
        current_user: The authenticated user.

    Returns:
        AgentPublishStatusResponse with UAT and PROD status.
    """
    try:
        # Get active UAT record (most recent)
        uat_record = (await session.exec(
            select(AgentDeploymentUAT).where(
                AgentDeploymentUAT.agent_id == agent_id,
                AgentDeploymentUAT.is_active == True,  # noqa: E712
                AgentDeploymentUAT.is_enabled == True,  # noqa: E712
                AgentDeploymentUAT.lifecycle_step != DeploymentLifecycleEnum.ARCHIVED,
            ).order_by(col(AgentDeploymentUAT.deployed_at).desc())
        )).first()

        # Get active PROD record (most recent)
        prod_record = (await session.exec(
            select(AgentDeploymentProd).where(
                AgentDeploymentProd.agent_id == agent_id,
                AgentDeploymentProd.is_active == True,  # noqa: E712
                AgentDeploymentProd.is_enabled == True,  # noqa: E712
                AgentDeploymentProd.lifecycle_step != ProdDeploymentLifecycleEnum.ARCHIVED,
            ).order_by(col(AgentDeploymentProd.deployed_at).desc())
        )).first()

        # Check for any pending PROD approval
        pending = (await session.exec(
            select(AgentDeploymentProd).where(
                AgentDeploymentProd.agent_id == agent_id,
                AgentDeploymentProd.status == DeploymentPRODStatusEnum.PENDING_APPROVAL,
            )
            .order_by(col(AgentDeploymentProd.deployed_at).desc())
        )).first()

        latest_prod_any = (await session.exec(
            select(AgentDeploymentProd).where(
                AgentDeploymentProd.agent_id == agent_id,
            ).order_by(col(AgentDeploymentProd.deployed_at).desc())
        )).first()

        latest_decision: str | None = None
        if latest_prod_any and latest_prod_any.approval_id:
            latest_approval = await session.get(ApprovalRequest, latest_prod_any.approval_id)
            if latest_approval and latest_approval.decision is not None:
                latest_decision = (
                    latest_approval.decision.value
                    if hasattr(latest_approval.decision, "value")
                    else str(latest_approval.decision)
                )

        return AgentPublishStatusResponse(
            agent_id=agent_id,
            uat=_record_to_summary(uat_record, "uat") if uat_record else None,
            prod=_record_to_summary(prod_record, "prod") if prod_record else None,
            has_pending_approval=pending is not None,
            pending_requested_by=pending.deployed_by if pending else None,
            latest_prod_status=(
                latest_prod_any.status.value
                if latest_prod_any and hasattr(latest_prod_any.status, "value")
                else (str(latest_prod_any.status) if latest_prod_any else None)
            ),
            latest_review_decision=latest_decision,
            latest_prod_published_by=latest_prod_any.deployed_by if latest_prod_any else None,
        )

    except Exception as e:
        logger.error(f"Error getting deploy status for agent {agent_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/{agent_id}/versions/{env}", response_model=list[PublishRecordSummary], status_code=200)
async def get_version_history(
    *,
    session: DbSession,
    agent_id: UUID,
    env: str,
    include_archived: bool = False,
    current_user: CurrentActiveUser,
):
    """Get version history for an agent in a specific environment.

    Returns all deployment records (all versions) for the given agent and environment,
    ordered by deployed_at descending (newest first).

    Args:
        session: Async database session.
        agent_id: UUID of the agent.
        env: Environment — must be 'uat' or 'prod'.
        current_user: The authenticated user.

    Returns:
        List of PublishRecordSummary objects representing the version timeline.

    Raises:
        400: Invalid environment value.
    """
    try:
        if env not in ("uat", "prod"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid environment '{env}'. Must be 'uat' or 'prod'.",
            )

        table_class = AgentDeploymentUAT if env == "uat" else AgentDeploymentProd
        stmt = (
            select(table_class)
            .where(table_class.agent_id == agent_id)
            .order_by(col(table_class.deployed_at).desc())
        )
        if not include_archived:
            archived_status = (
                DeploymentLifecycleEnum.ARCHIVED
                if env == "uat"
                else ProdDeploymentLifecycleEnum.ARCHIVED
            )
            stmt = stmt.where(table_class.lifecycle_step != archived_status)
        records = (await session.exec(stmt)).all()

        return [_record_to_summary(r, env) for r in records]

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting version history for agent {agent_id} in {env}: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/{deploy_id}/snapshot", response_model=PublishSnapshotResponse, status_code=200)
async def get_publish_snapshot(
    *,
    session: DbSession,
    deploy_id: UUID,
    current_user: CurrentActiveUser,
):
    """Get the full frozen agent snapshot for a deployment record.

    Returns the complete flow JSON (nodes + edges) that was frozen at deploy time.
    This endpoint is used for:

    1. **Testing in playground** — Load the snapshot and run it as a temporary agent.
       Works for both regular **chat agents** AND **autonomous agents** because the
       snapshot contains the full flow definition that the runtime can execute.
       The frontend sends this snapshot to the existing chat/build API.

    2. **Reviewing before approval** — Dept Admin views the exact flow that will
       go live.

    3. **Inspecting a specific version** — Compare different versions' flow definitions.

    The endpoint searches both agent_deployment_uat and agent_deployment_prod tables
    (UUIDs are globally unique so there is no ambiguity).

    Args:
        session: Async database session.
        deploy_id: UUID of the deployment record (in either UAT or PROD table).
        current_user: The authenticated user.

    Returns:
        PublishSnapshotResponse with the full agent_snapshot dict.

    Raises:
        404: Deployment record not found in either table.
    """
    try:
        record, env = await _find_deploy_record(session, deploy_id)

        return PublishSnapshotResponse(
            id=record.id,
            agent_id=record.agent_id,
            environment=env,
            version_number=f"v{record.version_number}",
            agent_name=record.agent_name,
            agent_description=record.agent_description,
            agent_snapshot=record.agent_snapshot,
            publish_description=record.publish_description,
            published_by=record.deployed_by,
            published_at=record.deployed_at,
            status=record.status.value if hasattr(record.status, "value") else str(record.status),
            is_active=record.is_active,
            visibility=record.visibility.value if hasattr(record.visibility, "value") else str(record.visibility),
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting snapshot for deployment {deploy_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/{deploy_id}/clone", response_model=CloneResponse, status_code=201)
async def clone_from_publish(
    *,
    session: DbSession,
    deploy_id: UUID,
    body: CloneFromPublishRequest,
    current_user: CurrentActiveUser,
):
    """Clone a deployed agent into a new agent (Copy on Edit pattern).

    Creates a brand-new agent in the agent table using the frozen snapshot from
    a deployment record. This is the "Copy on Edit" flow from the design.

    Sequence:
        1. Developer sees deployed agent in the Agent Registry
        2. Clicks "Copy" / "Edit" → selects target project (folder)
        3. System reads agent_snapshot from the deployment record
        4. INSERT new agent with:
           - user_id = current_user (NOT the original author)
           - project_id = selected project
           - data = frozen snapshot (the flow JSON)
           - name = original name + " (Copy)" or custom name
           - cloned_from_deployment_id = deployment record UUID (lineage tracking)
        5. User can now edit THEIR copy independently
        6. Original author's agent is UNTOUCHED

    Works for both chat agents and autonomous agents since the snapshot contains
    the complete flow definition.

    Args:
        session: Async database session.
        deploy_id: UUID of the deployment record to clone from.
        body: Clone configuration (project_id, optional new_name).
        current_user: The authenticated user who will own the clone.

    Returns:
        CloneResponse with the new agent's details.

    Raises:
        404: Deployment record not found or target folder not found.
        400: Snapshot has no usable flow data.
    """
    try:
        record, env = await _find_deploy_record(session, deploy_id)

        if not record.agent_snapshot:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Deployment record has no snapshot data to clone from",
            )

        # Verify target folder exists and belongs to user
        folder = (await session.exec(
            select(Folder).where(
                Folder.id == body.project_id,
                Folder.user_id == current_user.id,
            )
        )).first()
        if not folder:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Folder {body.project_id} not found or not owned by you",
            )

        # Determine agent name with uniqueness handling
        base_name = body.new_name or f"{record.agent_name} (Copy)"

        existing = (await session.exec(
            select(Agent).where(Agent.name == base_name, Agent.user_id == current_user.id)
        )).first()

        if existing:
            like_pattern = f"{base_name} (%"
            copies = (await session.exec(
                select(Agent).where(
                    Agent.name.like(like_pattern),  # type: ignore[union-attr]
                    Agent.user_id == current_user.id,
                )
            )).all()
            if copies:
                extract_number = re.compile(rf"^{re.escape(base_name)} \((\d+)\)$")
                numbers = []
                for c in copies:
                    match = extract_number.search(c.name)
                    if match:
                        numbers.append(int(match.groups()[0]))
                if numbers:
                    base_name = f"{base_name} ({max(numbers) + 1})"
                else:
                    base_name = f"{base_name} (1)"
            else:
                base_name = f"{base_name} (1)"

        # Create the new agent from the snapshot
        new_agent = Agent(
            name=base_name,
            description=record.agent_description,
            data=record.agent_snapshot,
            user_id=current_user.id,
            project_id=body.project_id,
            cloned_from_deployment_id=deploy_id,
            updated_at=datetime.now(timezone.utc),
        )
        session.add(new_agent)
        await session.commit()
        await session.refresh(new_agent)

        logger.info(
            f"User {current_user.id} cloned agent from {env} deployment {deploy_id} → "
            f"new agent '{base_name}' ({new_agent.id})"
        )

        return CloneResponse(
            agent_id=new_agent.id,
            agent_name=new_agent.name,
            project_id=new_agent.project_id,
            cloned_from_publish_id=deploy_id,
            environment_source=env,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error cloning from deployment {deploy_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


# ═══════════════════════════════════════════════════════════════════════════
# Bundle endpoints
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/{deploy_id}/bundles", response_model=list[AgentBundleRead], status_code=200)
async def get_deployment_bundles(
    *,
    session: DbSession,
    deploy_id: UUID,
    current_user: CurrentActiveUser,
):
    """Get all bundled resources for a specific deployment version.

    Returns the frozen snapshot of every external resource (model, MCP server,
    guardrail, knowledge base, vector DB, connector, tool, custom component)
    that was captured when this deployment was published.

    Args:
        session: Async database session.
        deploy_id: UUID of the deployment record (UAT or PROD).
        current_user: The authenticated user.

    Returns:
        List of AgentBundleRead objects for the deployment.
    """
    try:
        bundles = (await session.exec(
            select(AgentBundle)
            .where(AgentBundle.deployment_id == deploy_id)
            .order_by(AgentBundle.bundle_type, AgentBundle.resource_name)
        )).all()

        return [AgentBundleRead.model_validate(b) for b in bundles]

    except Exception as e:
        logger.error(f"Error fetching bundles for deployment {deploy_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/{agent_id}/bundles/latest", response_model=list[AgentBundleRead], status_code=200)
async def get_latest_bundles(
    *,
    session: DbSession,
    agent_id: UUID,
    env: str = Query(default="uat", description="Environment: 'uat' or 'prod'"),
    current_user: CurrentActiveUser,
):
    """Get bundles for the latest active deployment of an agent.

    Finds the most recent active deployment in the given environment and returns
    its bundled resources. Useful for the frontend sidebar to show what the
    currently running version of an agent uses.

    Args:
        session: Async database session.
        agent_id: UUID of the agent.
        env: Environment — 'uat' or 'prod'.
        current_user: The authenticated user.

    Returns:
        List of AgentBundleRead objects for the latest active deployment.
    """
    try:
        if env not in ("uat", "prod"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid environment '{env}'. Must be 'uat' or 'prod'.",
            )

        table_class = AgentDeploymentUAT if env == "uat" else AgentDeploymentProd
        latest = (await session.exec(
            select(table_class)
            .where(table_class.agent_id == agent_id, table_class.is_active == True)  # noqa: E712
            .order_by(col(table_class.deployed_at).desc())
            .limit(1)
        )).first()

        if not latest:
            return []

        bundles = (await session.exec(
            select(AgentBundle)
            .where(AgentBundle.deployment_id == latest.id)
            .order_by(AgentBundle.bundle_type, AgentBundle.resource_name)
        )).all()

        return [AgentBundleRead.model_validate(b) for b in bundles]

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching latest bundles for agent {agent_id} in {env}: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e
