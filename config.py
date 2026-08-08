"""Voitta Desktop — Persistent configuration (apps.json)."""

import json
import os
import uuid
from pathlib import Path

from paths import CONFIG_PATH, ROOT as CONFIG_DIR  # noqa: F401  (re-exported)

# Whether Voitta rewrites ~/.claude/settings.json to point at our proxy.
#
# Default False: arming edits a file outside our own tree, so it is opt-in.
# This constant exists because the default was previously written out at
# three call sites and two of them disagreed with the third, which meant the
# effective value depended on which code path happened to load the config.
CLAUDE_LINK_ARMED_DEFAULT = False


def claude_link_armed(cfg: dict) -> bool:
    """Read the claude_link.armed intent out of a config dict."""
    link_cfg = cfg.get("claude_link") or {}
    return bool(link_cfg.get("armed", CLAUDE_LINK_ARMED_DEFAULT))


# ── MCP servers (the new editable list) ──────────────────────────────────────
#
# Each entry:
#   id           uuid
#   name         human-visible label (e.g. "Enterprise Voitta MCP")
#   prefix       tool-name namespace ("vim", "voitta_rag", …) — fully editable
#   description  surfaced to the LLM via the unified instructions block
#   kind         "http" | "subprocess"
#   url          (kind=http) HTTP endpoint
#   subprocess   (kind=subprocess) {template, …}
#                  template ∈ {"google_mcp", "jira_mcp"} — HTTP subprocess, fixed command shape
#                    fields: cwd, env_path, port
#                  template ∈ {"npx"} — stdio via NpxStdioTransport; fastmcp owns the process
#                    fields: package (e.g. "chrome-devtools-mcp@latest"), args (list, optional)
#                    no port/cwd/env_path needed
#                  template ∈ {"command"} — stdio via StdioTransport; fastmcp owns the process
#                    fields: command (list, e.g. ["node", "/path/to/server.js"])
#                    no port/cwd/env_path needed
#   auth         {type, …}
#                  type ∈ {none, bearer, api_key, basic, custom_headers,
#                          oauth_app, voitta_rag_legacy}
#                  (stdio servers typically use none)


def _default_mcp_servers() -> list[dict]:
    """Single factory-default MCP server for fresh installs — the user
    can wire everything else from Settings → MCPs."""
    return [{
        "id": str(uuid.uuid4()),
        "name": "Enterprise Voitta MCP",
        "prefix": "vim",
        "description": "Enterprise Voitta MCP — image RAG, search, retrieval",
        "kind": "http",
        "url": "https://enterprise.voitta.ai/mcp",
        "auth": {"type": "bearer", "token": ""},
    }]


def _migrate_mcp_servers(cfg: dict) -> bool:
    """Build mcp_servers list from the legacy hardcoded shape if absent.

    Reads URLs / API keys from cfg.mcp_proxy and subprocess parameters
    from cfg.mcp_subprocess, then writes a unified list. Idempotent:
    returns False (and does nothing) if mcp_servers already exists.

    The legacy fields are left in place after migration so a rollback to
    an older Voitta Desktop build is still functional — they're just no
    longer read by the new code path.
    """
    existing = cfg.get("mcp_servers")
    if isinstance(existing, list) and existing:
        return False

    mp = cfg.get("mcp_proxy", {})
    sub = cfg.get("mcp_subprocess", {})
    servers: list[dict] = []

    # Voitta RAG — keep the legacy multi-app X-Auth-Token-* scheme.
    rag_url = mp.get("rag_url", "").strip()
    if rag_url:
        servers.append({
            "id": str(uuid.uuid4()),
            "name": "Voitta RAG",
            "prefix": "voitta_rag",
            "description": "RAG search, memory, file retrieval",
            "kind": "http",
            "url": f"{rag_url.rstrip('/')}/mcp/mcp",
            "auth": {"type": "voitta_rag_legacy"},
        })

    # Enterprise Voitta MCP (was "Image RAG").
    image_rag_url = mp.get("image_rag_url", "").strip() or "https://enterprise.voitta.ai/mcp"
    servers.append({
        "id": str(uuid.uuid4()),
        "name": "Enterprise Voitta MCP",
        "prefix": "vim",
        "description": "Enterprise Voitta MCP — image RAG, search, retrieval",
        "kind": "http",
        "url": image_rag_url,
        "auth": {"type": "bearer", "token": mp.get("image_rag_key", "")},
    })

    # Paperclip.
    paperclip_url = mp.get("paperclip_url", "").strip()
    if paperclip_url:
        servers.append({
            "id": str(uuid.uuid4()),
            "name": "Paperclip",
            "prefix": "paperclip",
            "description": "Paperclip biomedical paper search (8M+ papers)",
            "kind": "http",
            "url": paperclip_url,
            "auth": {
                "type": "api_key",
                "header": "X-API-Key",
                "value": mp.get("paperclip_key", ""),
            },
        })

    # Ex-YAML simple backends (PPTX, PubMed, FreeCAD).
    servers.append({
        "id": str(uuid.uuid4()),
        "name": "PPTX",
        "prefix": "voitta_pptx",
        "description": "PowerPoint slide rendering",
        "kind": "http",
        "url": "http://192.168.88.212:8001/mcp",
        "auth": {"type": "none"},
    })
    servers.append({
        "id": str(uuid.uuid4()),
        "name": "PubMed",
        "prefix": "pubmed",
        "description": "PubMed literature search & memory",
        "kind": "http",
        "url": "http://192.168.88.210:8000/mcp/mcp",
        "auth": {
            "type": "custom_headers",
            "headers": [{"name": "X-User-Name", "value": "roman"}],
        },
    })
    servers.append({
        "id": str(uuid.uuid4()),
        "name": "FreeCAD",
        "prefix": "freecad",
        "description": "FreeCAD CAD manipulation",
        "kind": "http",
        "url": mp.get("freecad_url", "").strip() or "http://127.0.0.1:50005/mcp",
        "auth": {"type": "none"},
    })

    # Subprocess-launched servers — Google Workspace + Jira.
    servers.append({
        "id": str(uuid.uuid4()),
        "name": "Google Workspace",
        "prefix": "google_workspace",
        "description": "Google Workspace (Gmail, Drive, Sheets, Docs, Calendar)",
        "kind": "subprocess",
        "subprocess": {
            "template": "google_mcp",
            "cwd": sub.get("google_mcp_dir", "~/DEVEL/google_workspace_mcp"),
            "env_path": sub.get("google_mcp_env_path", "~/DEVEL/google_workspace_mcp/.env"),
            "port": int(sub.get("google_mcp_port", 18766)),
        },
        "auth": {"type": "oauth_app", "backend": "google_workspace", "app_type": "google"},
    })
    servers.append({
        "id": str(uuid.uuid4()),
        "name": "Jira",
        "prefix": "jira",
        "description": "Jira issues, sprints, boards",
        "kind": "subprocess",
        "subprocess": {
            "template": "jira_mcp",
            "cwd": sub.get("jira_mcp_dir", "~/DEVEL/mcp-atlassian"),
            "env_path": sub.get("jira_mcp_env_path", str(CONFIG_DIR / "jira.env")),
            "port": int(sub.get("jira_mcp_port", 18767)),
        },
        "auth": {"type": "none"},
    })

    cfg["mcp_servers"] = servers
    return True


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
            # The per-backend URLs/keys that used to live here are now in
            # mcp_servers (see _default_mcp_servers). Only the local proxy
            # port + the Google Workspace edit-proxy URL remain — the latter
            # is still used by the auth flow to talk to the local Google MCP.
            "port": int(os.environ.get("MCP_PROXY_PORT", "18765")),
            "edit_proxy_url": os.environ.get("EDIT_PROXY_URL", f"http://localhost:{os.environ.get('GOOGLE_MCP_PORT', '18766')}"),
        },
        "llm_proxy": {
            "port": int(os.environ.get("LLM_PROXY_PORT", "18900")),
            "upstream_url": os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
        },
        "oauth": {
            "redirect_port": int(os.environ.get("OAUTH_REDIRECT_PORT", "53214")),
        },
        "optimizer": {
            "enabled": True,
            "haiku_only": False,
        },
        "bash": {
            "strip_ansi": True,
            "trim_whitespace": True,
            "strip_progress": False,
            "smart_commands": False,
            # Past keep_turns, replace tool-call arguments whose serialized size
            # is >= this many chars with a get_vt_object reference (0 = off).
            # 500 is the diminishing-returns knee from the cache-study sim.
            "tool_use_ref_min_chars": 500,
        },
        "time": {
            # Per-optimizer time horizon: how many recent turns to leave
            # untouched. Optimization applies only to messages older than
            # this. BashCompressor doesn't appear here — it operates on
            # all turns by design.
            "tool_result_keep_turns": 5,
            "image_keep_turns": 5,
            "thinking_keep_turns": 5,
        },
        "mcp_subprocess": {
            "google_mcp_port": int(os.environ.get("GOOGLE_MCP_PORT", "18766")),
            "google_mcp_dir": os.environ.get("GOOGLE_MCP_DIR", "~/DEVEL/google_workspace_mcp"),
            "google_mcp_env_path": os.environ.get("GOOGLE_MCP_ENV_PATH", "~/DEVEL/google_workspace_mcp/.env"),
            "jira_mcp_port": int(os.environ.get("JIRA_MCP_PORT", "18767")),
            "jira_mcp_dir": os.environ.get("JIRA_MCP_DIR", "~/DEVEL/mcp-atlassian"),
            "jira_mcp_env_path": os.environ.get("JIRA_MCP_ENV_PATH", str(CONFIG_DIR / "jira.env")),
        },
        "disabled_tools": [],
        "tools": {
            # Codex signs its MCP handshake; we don't need to confirm tool
            # exposure interactively every time. When True, the tool-gate
            # popup is skipped for Codex clients and the disabled_tools list
            # is applied silently. Other clients (Claude Code, etc.) still
            # see the popup. Default True per user preference.
            "suppress_codex_popup": True,
        },
        "claude_link": {
            # Tracks user intent for the Connect Claude button. Set True the
            # first time the user clicks Connect; cleared when they click
            # Disconnect. On quit we disarm ~/.claude/settings.json; on next
            # launch we re-arm if this is True. Lets the user keep Voitta in
            # the loop only while Voitta is running.
            "armed": False,
        },
        "mcp_servers": _default_mcp_servers(),
    }


def _deep_backfill(target: dict, defaults: dict) -> None:
    """Recursively populate missing keys in `target` from `defaults` without
    overwriting saved values."""
    for k, v in defaults.items():
        if k not in target:
            target[k] = v
        elif isinstance(v, dict) and isinstance(target.get(k), dict):
            _deep_backfill(target[k], v)


def load_config() -> dict:
    """Load config from disk, returning defaults if missing or corrupt."""
    defaults = _default_config()
    if not CONFIG_PATH.exists():
        return defaults
    try:
        data = json.loads(CONFIG_PATH.read_text())
        # Migrate: rename "proxy" -> "mcp_proxy" if old config
        if "proxy" in data and "mcp_proxy" not in data:
            data["mcp_proxy"] = data.pop("proxy")
        # If the loaded config predates the unified mcp_servers list, build
        # it from the legacy mcp_proxy / mcp_subprocess shape BEFORE backfill
        # plants the single-entry factory default. Idempotent — no-op once
        # the list is populated.
        migrated = _migrate_mcp_servers(data)
        # Backfill missing top-level + nested keys from env-derived defaults.
        # Saved values win — env only seeds new/missing fields. The Settings
        # window is the single source of truth; .env merely provides initial
        # values on first run.
        _deep_backfill(data, defaults)
        if migrated:
            save_config(data)
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
        "freecad_url": settings.get("freecad_url", os.environ.get("FREECAD_URL", "http://127.0.0.1:50005/mcp")),
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
