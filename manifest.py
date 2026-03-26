"""Manifest YAML helpers -- add / remove agent entries directly in Git repo."""

from __future__ import annotations

from loguru import logger


_ENV_TO_NUMERIC = {"dev": 0, "uat": 1, "prod": 2}


def _normalize_env(environment: str) -> int:
    """Convert env name to numeric code (dev→0, uat→1, prod→2). Pass-through if already numeric."""
    val = environment.lower()
    if val in _ENV_TO_NUMERIC:
        return _ENV_TO_NUMERIC[val]
    return int(val)


def add_manifest_entry(
    *,
    agent_id: str,
    agent_name: str,
    version_number: str,
    environment: str,
    deployment_id: str,
) -> None:
    """Add an agent entry directly in the Git repo. Idempotent -- skips if deployment_id already present."""
    try:
        from agentcore.services.git_manifest import read_manifest_from_git, push_manifest_to_git

        data = read_manifest_from_git()
        agents: list = data.get("agents", [])

        if any(d.get("deployment_id") == deployment_id for d in agents):
            logger.debug(f"[MANIFEST] Entry for {deployment_id} already exists, skipping add.")
            return

        env_code = _normalize_env(environment)
        agents.append({
            "agent_id": agent_id,
            "agent_name": agent_name,
            "version_number": version_number,
            "environment": env_code,
            "deployment_id": deployment_id,
        })
        updated = {"agents": agents}
        push_manifest_to_git(updated, f"manifest: add agent {agent_name} ({deployment_id})")
        logger.info(f"[MANIFEST] Added entry for deployment_id={deployment_id} (total: {len(agents)})")
    except Exception as err:
        logger.warning(f"[MANIFEST] Failed to add entry: {err}")


def remove_manifest_entry(*, deployment_id: str) -> None:
    """Remove the entry matching deployment_id directly from the Git repo. No-op if not found."""
    try:
        from agentcore.services.git_manifest import read_manifest_from_git, push_manifest_to_git

        data = read_manifest_from_git()
        agents: list = data.get("agents", [])

        original_len = len(agents)
        agents = [d for d in agents if d.get("deployment_id") != deployment_id]

        if len(agents) == original_len:
            logger.debug(f"[MANIFEST] No entry found for {deployment_id}, nothing to remove.")
            return

        updated = {"agents": agents}
        push_manifest_to_git(updated, f"manifest: remove deployment {deployment_id}")
        logger.info(f"[MANIFEST] Removed entry for deployment_id={deployment_id} (remaining: {len(agents)})")
    except Exception as err:
        logger.warning(f"[MANIFEST] Failed to remove entry: {err}")
