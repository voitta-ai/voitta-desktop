"""Voitta Desktop — Persistent configuration (apps.json)."""

import json
import os
import uuid
from pathlib import Path

CONFIG_DIR = Path.home() / ".voitta_desktop"
CONFIG_PATH = CONFIG_DIR / "apps.json"

def _default_config() -> dict:
    """Build defaults from environment variables — no hardcoded ports."""
    return {
        "apps": [],
        "jira": {
            "server_url": os.environ.get("JIRA_URL", ""),
            "email": os.environ.get("JIRA_EMAIL", ""),
            "api_token": os.environ.get("JIRA_API_TOKEN", ""),
            "project": os.environ.get("JIRA_PROJECT", ""),
        },
        "mcp_proxy": {
            "port": int(os.environ.get("MCP_PROXY_PORT", "18765")),
            "edit_proxy_url": os.environ.get("EDIT_PROXY_URL", f"http://localhost:{os.environ.get('GOOGLE_MCP_PORT', '18766')}"),
            "rag_url": os.environ.get("VOITTA_RAG_URL", "https://rag.voitta.ai"),
            "image_rag_url": os.environ.get("VOITTA_IMAGE_RAG_URL", "https://rag-img.voitta.ai/mcp"),
            "image_rag_key": "",
            "paperclip_url": os.environ.get("PAPERCLIP_URL", "https://paperclip.gxl.ai/mcp"),
            "paperclip_key": "",
        },
        "llm_proxy": {
            "port": int(os.environ.get("LLM_PROXY_PORT", "18900")),
            "upstream_url": os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
        },
        "disabled_tools": [],
    }


def load_config() -> dict:
    """Load config from disk, returning defaults if missing or corrupt."""
    defaults = _default_config()
    if not CONFIG_PATH.exists():
        return defaults
    try:
        data = json.loads(CONFIG_PATH.read_text())
        for key in defaults:
            if key not in data:
                data[key] = defaults[key]
        # Migrate: rename "proxy" -> "mcp_proxy" if old config
        if "proxy" in data and "mcp_proxy" not in data:
            data["mcp_proxy"] = data.pop("proxy")
        # Env vars override saved ports (so .env is always authoritative)
        data.setdefault("mcp_proxy", {})["port"] = defaults["mcp_proxy"]["port"]
        data.setdefault("llm_proxy", {})["port"] = defaults["llm_proxy"]["port"]
        # Backfill new fields onto pre-existing configs without clobbering saved values
        data["llm_proxy"].setdefault("upstream_url", defaults["llm_proxy"]["upstream_url"])
        return data
    except Exception:
        return defaults


def save_config(config: dict) -> None:
    """Write config to disk."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, indent=2))


def migrate_from_legacy(settings: dict, env_defaults: dict | None = None) -> dict:
    """Build apps.json config from legacy ~/.voitta_auth_settings.json values."""
    config = json.loads(json.dumps(_default_config()))

    # Microsoft (RAG)
    ms_tenant = settings.get("ms_tenant_id") or os.environ.get("AZURE_TENANT_ID", "")
    ms_client = settings.get("ms_client_id") or os.environ.get("AZURE_CLIENT_ID", "")
    if ms_tenant or ms_client:
        config["apps"].append({
            "id": str(uuid.uuid4()),
            "name": "Microsoft",
            "type": "microsoft",
            "tenant_id": ms_tenant,
            "client_id": ms_client,
            "use_for": ["rag"],
        })

    # Google (RAG)
    g_client = settings.get("google_client_id") or os.environ.get("GOOGLE_CLIENT_ID", "")
    g_secret = settings.get("google_client_secret") or os.environ.get("GOOGLE_CLIENT_SECRET", "")
    if g_client or g_secret:
        config["apps"].append({
            "id": str(uuid.uuid4()),
            "name": "Google",
            "type": "google",
            "client_id": g_client,
            "client_secret": g_secret,
            "use_for": ["rag"],
        })

    # Google Workspace
    ge_client = settings.get("google_edit_client_id") or g_client
    ge_secret = settings.get("google_edit_client_secret") or g_secret
    if ge_client or ge_secret:
        existing = next(
            (a for a in config["apps"]
             if a["type"] == "google" and a["client_id"] == ge_client and a.get("client_secret") == ge_secret),
            None,
        )
        if existing:
            if "google_workspace" not in existing["use_for"]:
                existing["use_for"].append("google_workspace")
        else:
            config["apps"].append({
                "id": str(uuid.uuid4()),
                "name": "Google Edit",
                "type": "google",
                "client_id": ge_client,
                "client_secret": ge_secret,
                "use_for": ["google_workspace"],
            })

    # Microsoft Edit
    me_tenant = settings.get("ms_edit_tenant_id") or ms_tenant
    me_client = settings.get("ms_edit_client_id") or ms_client
    if me_tenant or me_client:
        existing = next(
            (a for a in config["apps"]
             if a["type"] == "microsoft" and a["client_id"] == me_client and a.get("tenant_id") == me_tenant),
            None,
        )
        if existing:
            if "google_workspace" not in existing["use_for"]:
                existing["use_for"].append("google_workspace")
        else:
            config["apps"].append({
                "id": str(uuid.uuid4()),
                "name": "Microsoft Edit",
                "type": "microsoft",
                "tenant_id": me_tenant,
                "client_id": me_client,
                "use_for": ["google_workspace"],
            })

    # Jira
    jira_url = settings.get("jira_url", "") or os.environ.get("JIRA_URL", "")
    config["jira"] = {
        "server_url": settings.get("jira_server_url", jira_url),
        "email": settings.get("jira_email", "") or os.environ.get("JIRA_EMAIL", ""),
        "api_token": settings.get("jira_api_token", "") or os.environ.get("JIRA_API_TOKEN", ""),
        "project": settings.get("jira_project", "") or os.environ.get("JIRA_PROJECT", ""),
    }

    # MCP Proxy
    config["mcp_proxy"] = {
        "port": int(settings.get("proxy_port", os.environ.get("MCP_PROXY_PORT", "18765"))),
        "edit_proxy_url": settings.get("edit_proxy_url", os.environ.get("EDIT_PROXY_URL", "http://localhost:18766")),
        "rag_url": settings.get("voitta_rag_url", os.environ.get("VOITTA_RAG_URL", "https://rag.voitta.ai")),
        "image_rag_url": settings.get("voitta_image_rag_url", os.environ.get("VOITTA_IMAGE_RAG_URL", "https://rag-img.voitta.ai/mcp")),
        "image_rag_key": settings.get("voitta_image_rag_key", ""),
        "paperclip_url": settings.get("paperclip_url", os.environ.get("PAPERCLIP_URL", "https://paperclip.gxl.ai/mcp")),
        "paperclip_key": settings.get("paperclip_key", ""),
    }

    # LLM Proxy
    config["llm_proxy"] = {
        "port": int(settings.get("llm_proxy_port", os.environ.get("LLM_PROXY_PORT", "18900"))),
        "upstream_url": settings.get("llm_upstream_url", os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")),
    }

    return config


def apps_for_backend(config: dict, backend: str) -> list[dict]:
    """Return apps assigned to a given backend, in order."""
    return [a for a in config.get("apps", []) if backend in a.get("use_for", [])]
