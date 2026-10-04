"""Model catalogs: what each account's provider can actually serve.

Fetched from the provider (with the account's own credentials) when an
account is added and on demand from the UI, then kept on the account as
``catalog``. The UI fills its model menus from it, and the Codex adapter
uses the per-model reasoning levels to reject an effort the model lacks.

Catalog entry: ``{"id", "name", "efforts": [...], "default_effort",
"description", "context_window"}``; ``efforts`` is empty for models without
adjustable reasoning, ``description``/``context_window`` are None when the
provider doesn't say. Order is the provider's own (OpenAI ranks by priority).
"""

import re
import time

import aiohttp

from . import oauth, providers
from .common import Unsupported

CODEX_CLIENT_VERSION = "0.160.0"
# Claude Code's /effort levels, weakest first. Provider levels are matched
# against this order when a requested level isn't supported.
EFFORT_ORDER = ["minimal", "low", "medium", "high", "xhigh", "max", "ultra"]
OPENAI_CHAT_EFFORTS = ["minimal", "low", "medium", "high"]


class CatalogError(Exception):
    pass


async def fetch(http: aiohttp.ClientSession, account: dict, creds: dict) -> list[dict]:
    kind = account["kind"]
    base = account["base_url"].rstrip("/")
    if kind == "openai_codex":
        return await _codex(http, base, creds)
    if kind == "anthropic_oauth":
        headers = {"Authorization": f"Bearer {creds['access_token']}", "anthropic-beta": oauth.OAUTH_BETA,
                   "anthropic-version": "2023-06-01"}
        return _anthropic(await _get(http, f"{base}/v1/models?limit=1000", headers))
    if kind == "anthropic_compat":
        headers = {"x-api-key": creds["api_key"], "Authorization": f"Bearer {creds['api_key']}",
                   "anthropic-version": "2023-06-01"}
        if providers.option(account, "models_listing") == "openai_root":
            # DeepSeek lists models only at the root of its OpenAI-style API.
            root = base.removesuffix("/anthropic")
            return _openai(await _get(http, f"{root}/models", headers), account)
        return _anthropic(await _get(http, f"{base}/v1/models?limit=1000", headers))
    if kind == "openai_chat":
        return _openai(await _get(http, f"{base}/models", {"Authorization": f"Bearer {creds['api_key']}"}), account)
    raise CatalogError(f"no model listing for {kind}")


async def _get(http, url, headers) -> dict:
    async with http.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as r:
        if r.status != 200:
            raise CatalogError(f"{url} returned {r.status}: {(await r.text())[:200]}")
        return await r.json(content_type=None)


async def _codex(http, base, creds) -> list[dict]:
    headers = {"Authorization": f"Bearer {creds['access_token']}",
               "chatgpt-account-id": creds.get("account_id") or "", "originator": "codex_cli_rs"}
    data = await _get(http, f"{base}/models?client_version={CODEX_CLIENT_VERSION}", headers)
    models = sorted((m for m in data.get("models", []) if m.get("visibility", "list") == "list"),
                    key=lambda m: m.get("priority", 999))
    out = []
    for m in models:
        levels = [lv["effort"] if isinstance(lv, dict) else lv for lv in m.get("supported_reasoning_levels") or []]
        out.append({"id": m["slug"], "name": m.get("display_name") or m["slug"],
                    "efforts": levels, "default_effort": m.get("default_reasoning_level"),
                    "description": m.get("description"), "context_window": m.get("context_window")})
    return out


def _anthropic(data: dict) -> list[dict]:
    return [{"id": m["id"], "name": m.get("display_name") or m["id"], "efforts": [], "default_effort": None,
             "description": None, "context_window": m.get("max_input_tokens")}
            for m in data.get("data", [])]


def _openai(data: dict, account: dict) -> list[dict]:
    out = []
    for m in data.get("data", []):
        caps = m.get("capabilities")
        if caps and not (caps.get("completion_chat") and caps.get("function_calling", True)):
            continue  # Mistral lists embedding/OCR models too; Claude Code needs chat + tools
        mid = m["id"]
        reasoning = account["provider"] == "openai_api" and mid.startswith(("gpt-5", "o1", "o3", "o4"))
        out.append({"id": mid, "name": m.get("name") or mid,
                    "efforts": OPENAI_CHAT_EFFORTS if reasoning else [], "default_effort": "medium" if reasoning else None,
                    "description": m.get("description") or None,
                    "context_window": m.get("max_context_length") or m.get("context_length")})
    out.sort(key=lambda m: m["id"])
    return out


_SMALL = re.compile(r"\b(flash|mini|nano|small|lite|luna|haiku|fast|affordable|efficient)\b", re.I)
_OLD = re.compile(r"\b(legacy|older|previous|deprecated)\b", re.I)


def suggest(catalog_models: list[dict]) -> dict:
    """Main/Background picks from the provider's own list: its order (OpenAI
    ranks by priority) and wording ("fast and affordable", "-flash", "legacy").
    Background is "" when the provider lists no clearly smaller model."""
    text = lambda m: f"{m['id']} {m['name']} {m.get('description') or ''}"
    current = [m for m in catalog_models if not _OLD.search(m.get("description") or "")] or catalog_models
    main = next((m for m in current if not _SMALL.search(text(m))), current[0] if current else None)
    small = next((m for m in current if _SMALL.search(text(m)) and m is not main), None)
    return {"big": main["id"] if main else "", "small": small["id"] if small else ""}


def catalog_entry(account: dict, model: str) -> dict | None:
    for m in (account.get("catalog") or {}).get("models", []):
        if m["id"] == model:
            return m
    return None


def resolve_effort(account: dict, model: str, requested: str | None) -> str | None:
    """The reasoning effort to send upstream, or None to send none.

    A fixed per-account setting wins over Claude Code's /effort. A level the
    model does not support is an error, never rounded to a nearby one. With
    no level at all, none is sent and the provider applies its own default.
    """
    fixed = (account.get("options") or {}).get("reasoning_effort")
    want = fixed if fixed and fixed != "auto" else requested
    if not want:
        return None
    entry = catalog_entry(account, model)
    supported = entry["efforts"] if entry else None
    if supported == []:
        if fixed and fixed != "auto":
            raise Unsupported(f"{model} has no reasoning effort setting, but {account['label']} "
                              f"is fixed to {fixed!r}; set Effort to 'Follow Claude Code'")
        return None  # Claude Code always sends a level; this model simply has no knob
    if supported and want not in supported:
        source = "the account's fixed Effort" if want == fixed else "Claude Code (/effort)"
        raise Unsupported(f"{model} does not support effort {want!r} requested by {source}; "
                          f"supported: {', '.join(supported)}")
    return want


def stamp(models: list[dict] | None, error: str | None = None) -> dict:
    return {"fetched_at": time.time(), "models": models or [], "error": error,
            "suggested": suggest(models or [])}
