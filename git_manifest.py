"""Push agents.yaml to a Git repository (GitHub or Azure DevOps) via PAT token."""

from __future__ import annotations

import base64
from urllib.parse import urlparse

import requests
import yaml
from loguru import logger


# ── URL parsing ──────────────────────────────────────────────────────────────

def _parse_repo_url(url: str) -> dict:
    """Return provider info dict from a GitHub or ADO repo URL."""
    parsed = urlparse(url.rstrip("/"))
    host = parsed.hostname or ""
    parts = [p for p in parsed.path.split("/") if p]

    if "github.com" in host:
        if len(parts) < 2:
            raise ValueError(f"GitHub URL must be https://github.com/owner/repo — got: {url}")
        return {"provider": "github", "owner": parts[0], "repo": parts[1]}

    if "dev.azure.com" in host:
        if len(parts) < 4 or parts[2] != "_git":
            raise ValueError(
                f"ADO URL must be https://dev.azure.com/org/project/_git/repo — got: {url}"
            )
        return {"provider": "ado", "org": parts[0], "project": parts[1], "repo": parts[3]}

    raise ValueError(f"Unsupported repo URL: {url}. Use github.com or dev.azure.com")


# ── GitHub helpers ────────────────────────────────────────────────────────────

def _gh_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}


def _gh_base(info: dict) -> str:
    return f"https://api.github.com/repos/{info['owner']}/{info['repo']}"


def _gh_get(token: str, info: dict, branch: str, file_path: str) -> tuple[dict, str | None]:
    resp = requests.get(
        f"{_gh_base(info)}/contents/{file_path}",
        headers=_gh_headers(token),
        params={"ref": branch},
        timeout=15,
    )
    if resp.status_code == 404:
        return {}, None
    resp.raise_for_status()
    body = resp.json()
    content = base64.b64decode(body["content"]).decode()
    return yaml.safe_load(content) or {}, body["sha"]


def _gh_put(
    token: str,
    info: dict,
    branch: str,
    file_path: str,
    data: dict,
    sha: str | None,
    msg: str,
) -> str:
    payload = {
        "message": msg,
        "content": base64.b64encode(
            yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False).encode()
        ).decode(),
        "branch": branch,
    }
    if sha is not None:
        payload["sha"] = sha
    resp = requests.put(
        f"{_gh_base(info)}/contents/{file_path}",
        headers=_gh_headers(token),
        json=payload,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("commit", {}).get("sha", "unknown")


# ── Azure DevOps helpers ──────────────────────────────────────────────────────

def _ado_headers(token: str) -> dict:
    encoded = base64.b64encode(f":{token}".encode()).decode()
    return {
        "Authorization": f"Basic {encoded}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _ado_base(info: dict) -> str:
    return (
        f"https://dev.azure.com/{info['org']}/{info['project']}"
        f"/_apis/git/repositories/{info['repo']}"
    )


def _ado_get(token: str, info: dict, branch: str, file_path: str) -> tuple[dict, str | None]:
    # Step 1: metadata-only call to check existence and get objectId (returns JSON)
    meta_resp = requests.get(
        f"{_ado_base(info)}/items",
        headers=_ado_headers(token),
        params={
            "path": file_path,
            "versionDescriptor.version": branch,
            "api-version": "7.1",
        },
        timeout=15,
    )
    if meta_resp.status_code == 404:
        return {}, None
    if not meta_resp.ok:
        raise ValueError(f"ADO GET items failed: HTTP {meta_resp.status_code} — {meta_resp.text[:300]}")
    try:
        object_id = meta_resp.json().get("objectId")
    except Exception:
        raise ValueError(f"ADO metadata returned non-JSON (HTTP {meta_resp.status_code}): {meta_resp.text[:300]}")

    # Step 2: content call — use plain Accept header so ADO returns raw file text
    encoded = base64.b64encode(f":{token}".encode()).decode()
    content_headers = {"Authorization": f"Basic {encoded}", "Accept": "text/plain"}
    content_resp = requests.get(
        f"{_ado_base(info)}/items",
        headers=content_headers,
        params={
            "path": file_path,
            "versionDescriptor.version": branch,
            "includeContent": "true",
            "api-version": "7.1",
        },
        timeout=15,
    )
    if not content_resp.ok:
        raise ValueError(f"ADO GET content failed: HTTP {content_resp.status_code} — {content_resp.text[:300]}")
    return yaml.safe_load(content_resp.text) or {}, object_id


def _ado_latest_commit(token: str, info: dict, branch: str) -> str:
    resp = requests.get(
        f"{_ado_base(info)}/refs",
        headers=_ado_headers(token),
        params={"filter": f"heads/{branch}", "api-version": "7.1"},
        timeout=15,
    )
    if not resp.ok:
        raise ValueError(f"ADO GET refs failed: HTTP {resp.status_code} — {resp.text[:300]}")
    try:
        refs = resp.json().get("value", [])
    except Exception:
        raise ValueError(f"ADO GET refs returned non-JSON (HTTP {resp.status_code}): {resp.text[:300]}")
    if not refs:
        raise ValueError(f"Branch '{branch}' not found in ADO repo")
    return refs[0]["objectId"]


def _ado_put(
    token: str,
    info: dict,
    branch: str,
    file_path: str,
    data: dict,
    version_id: str | None,
    msg: str,
) -> str:
    content = yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)
    old_oid = _ado_latest_commit(token, info, branch)
    payload = {
        "refUpdates": [{"name": f"refs/heads/{branch}", "oldObjectId": old_oid}],
        "commits": [{
            "comment": msg,
            "changes": [{
                "changeType": "edit" if version_id else "add",
                "item": {"path": f"/{file_path}"},
                "newContent": {"content": content, "contentType": "rawtext"},
            }],
        }],
    }
    resp = requests.post(
        f"{_ado_base(info)}/pushes?api-version=7.1",
        headers=_ado_headers(token),
        json=payload,
        timeout=15,
    )
    resp.raise_for_status()
    commits = resp.json().get("commits", [])
    return commits[0]["commitId"] if commits else "unknown"


# ── Public API ────────────────────────────────────────────────────────────────

def read_manifest_from_git() -> dict:
    """Read current manifest YAML from the configured Git repo.

    Returns the parsed dict (e.g. {"agents": [...]}).
    Returns {} if git sync is disabled or the file doesn't exist yet.
    """
    from agentcore.services.deps import get_settings_service

    s = get_settings_service().settings
    provider = (s.git_provider or "").strip().lower()
    if not provider:
        return {}

    branch = s.git_branch or "main"
    file_path = s.git_manifest_file or "agents.yaml"

    if provider in ("ado", "both"):
        if s.ado_repo_url:
            info = _parse_repo_url(s.ado_repo_url)
            data, _ = _ado_get(s.ado_token, info, branch, file_path)
            return data

    if provider in ("github", "both"):
        if s.github_repo_url:
            info = _parse_repo_url(s.github_repo_url)
            data, _ = _gh_get(s.github_token, info, branch, file_path)
            return data

    return {}


def push_manifest_to_git(data: dict, commit_message: str) -> None:
    """Push *data* as manifest YAML to the configured Git repo(s).

    Controlled by GIT_PROVIDER:
      'github' — push to GitHub only
      'ado'    — push to Azure DevOps only
      'both'   — push to both providers
      ''       — disabled, no-op

    Raises on network/API errors — callers should catch and log.
    """
    from agentcore.services.deps import get_settings_service

    s = get_settings_service().settings
    provider = (s.git_provider or "").strip().lower()
    if not provider:
        return  # git sync disabled

    branch = s.git_branch or "main"
    file_path = s.git_manifest_file or "agents.yaml"

    if provider in ("github", "both"):
        if not s.github_repo_url:
            logger.warning("[GIT_MANIFEST] git_provider includes 'github' but GITHUB_REPO_URL is not set — skipping")
        else:
            info = _parse_repo_url(s.github_repo_url)
            _, sha = _gh_get(s.github_token, info, branch, file_path)
            commit = _gh_put(s.github_token, info, branch, file_path, data, sha, commit_message)
            logger.info(f"[GIT_MANIFEST] Pushed to GitHub repo, commit={commit}")

    if provider in ("ado", "both"):
        if not s.ado_repo_url:
            logger.warning("[GIT_MANIFEST] git_provider includes 'ado' but ADO_REPO_URL is not set — skipping")
        else:
            info = _parse_repo_url(s.ado_repo_url)
            _, version_id = _ado_get(s.ado_token, info, branch, file_path)
            commit = _ado_put(s.ado_token, info, branch, file_path, data, version_id, commit_message)
            logger.info(f"[GIT_MANIFEST] Pushed to ADO repo, commit={commit}")

    if provider not in ("github", "ado", "both"):
        logger.warning(f"[GIT_MANIFEST] Unknown GIT_PROVIDER='{provider}' — expected github, ado, or both")
