"""Manifest YAML helpers -- add / remove deployment entries."""

from __future__ import annotations

from pathlib import Path

import yaml
from dotenv import find_dotenv
from loguru import logger

from agentcore.services.deps import get_settings_service


def _resolve_manifest_path() -> Path:
    """Return the absolute path to agents.yaml, resolved from settings."""
    configured = get_settings_service().settings.manifest_file_path
    manifest_path = Path(configured) if configured else Path("agents.yaml")
    if not manifest_path.is_absolute():
        env_dir = Path(find_dotenv()).resolve().parent
        manifest_path = (env_dir / manifest_path).resolve()
    return manifest_path


def _load_manifest(path: Path) -> dict:
    if path.exists():
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {}


def _save_manifest(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def add_manifest_entry(
    *,
    agent_id: str,
    agent_name: str,
    version_number: str,
    environment: str,
    deployment_id: str,
) -> None:
    """Add a deployment entry. Idempotent -- skips if deployment_id already present."""
    try:
        path = _resolve_manifest_path()
        data = _load_manifest(path)
        deployments: list = data.get("agents", [])

        if any(d.get("deployment_id") == deployment_id for d in deployments):
            logger.debug(f"[MANIFEST] Entry for {deployment_id} already exists, skipping add.")
            return

        deployments.append({
            "agent_id": agent_id,
            "agent_name": agent_name,
            "version_number": version_number,
            "environment": environment,
            "deployment_id": deployment_id,
        })
        updated = {"agents": deployments}
        _save_manifest(path, updated)
        logger.info(f"[MANIFEST] Added entry for deployment_id={deployment_id} (total: {len(deployments)})")

        try:
            from agentcore.services.git_manifest import push_manifest_to_git
            push_manifest_to_git(updated, f"manifest: add deployment {deployment_id}")
        except Exception as git_err:
            logger.warning(f"[MANIFEST] Git sync failed (add): {git_err}")
    except Exception as err:
        logger.warning(f"[MANIFEST] Failed to add entry: {err}")


def remove_manifest_entry(*, deployment_id: str) -> None:
    """Remove the entry matching deployment_id. No-op if not found."""
    try:
        path = _resolve_manifest_path()
        data = _load_manifest(path)
        deployments: list = data.get("agents", [])

        original_len = len(deployments)
        deployments = [d for d in deployments if d.get("deployment_id") != deployment_id]

        if len(deployments) == original_len:
            logger.debug(f"[MANIFEST] No entry found for {deployment_id}, nothing to remove.")
            return

        updated = {"agents": deployments}
        _save_manifest(path, updated)
        logger.info(f"[MANIFEST] Removed entry for deployment_id={deployment_id} (remaining: {len(deployments)})")

        try:
            from agentcore.services.git_manifest import push_manifest_to_git
            push_manifest_to_git(updated, f"manifest: remove deployment {deployment_id}")
        except Exception as git_err:
            logger.warning(f"[MANIFEST] Git sync failed (remove): {git_err}")
    except Exception as err:
        logger.warning(f"[MANIFEST] Failed to remove entry: {err}")
