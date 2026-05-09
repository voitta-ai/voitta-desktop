"""Wire / unwire Claude Code → Voitta Desktop via ~/.claude/settings.json.

The Connect/Disconnect feature on the Settings → Proxies tab uses this module
to compute and apply the diff. Two side-effect surfaces:

  • ~/.claude/settings.json — the file Claude Code reads on startup. We
    rewrite the `env` block to point ANTHROPIC_BASE_URL at our local proxy.

  • voitta-desktop's apps.json (llm_proxy.upstream_url) — when we connect
    over the top of an existing ANTHROPIC_BASE_URL (e.g. corporate gateway),
    we INHERIT that URL as our own upstream, so the chain becomes:

        Claude Code → Voitta proxy → corporate gateway → Anthropic

    The original is also preserved under env.VOITTA_ANTHROPIC_BASE_URL so
    Disconnect can put settings.json back exactly the way it was.

Disconnect leaves voitta-desktop's apps.json alone (per the user's "leave
voitta desktop as is" rule). Only ~/.claude/settings.json is restored.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

CLAUDE_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
# `claude mcp add` writes here, not to settings.json. Claude Code merges both.
CLAUDE_USER_CONFIG_PATH = Path.home() / ".claude.json"
CODEX_CONFIG_PATH = Path.home() / ".codex" / "config.toml"


# ── Plan + change types ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class Change:
    """One setting transition. ``old=None`` means the key was absent;
    ``new=None`` means the key will be removed."""
    label: str
    old: str | None
    new: str | None

    @property
    def kind(self) -> str:
        if self.old is None and self.new is not None:
            return "add"
        if self.old is not None and self.new is None:
            return "remove"
        return "change"


@dataclass(frozen=True)
class Plan:
    target: str  # "connect" or "disconnect"
    claude_changes: list[Change] = field(default_factory=list)
    voitta_upstream_change: Change | None = None

    @property
    def is_noop(self) -> bool:
        return not self.claude_changes and self.voitta_upstream_change is None


# ── settings.json I/O ────────────────────────────────────────────────────────


def load_claude_settings() -> dict:
    """Return the parsed ~/.claude/settings.json, or {} if missing/malformed.

    A malformed file is treated as missing — the user can fix it by hand;
    we won't overwrite the entire structure trying to recover.
    """
    if not CLAUDE_SETTINGS_PATH.exists():
        return {}
    try:
        return json.loads(CLAUDE_SETTINGS_PATH.read_text())
    except Exception:
        return {}


def settings_file_is_malformed() -> bool:
    """True iff the file exists but isn't parseable JSON."""
    if not CLAUDE_SETTINGS_PATH.exists():
        return False
    try:
        json.loads(CLAUDE_SETTINGS_PATH.read_text())
        return False
    except Exception:
        return True


def _env_block(cfg: dict) -> dict:
    """Return cfg["env"] coerced to a plain dict (treat null/non-object as
    empty so we never crash on hand-edited files)."""
    env = cfg.get("env")
    return env if isinstance(env, dict) else {}


def is_voitta_connected(cfg: dict, our_port: int) -> bool:
    """True iff settings.json's ANTHROPIC_BASE_URL points at our proxy."""
    env = _env_block(cfg)
    return env.get("ANTHROPIC_BASE_URL") == _our_url(our_port)


def is_mcp_wired(cfg: dict, mcp_port: int) -> bool:
    """True iff Claude Code's MCP config points at our MCP proxy.

    Claude Code merges mcpServers from two locations:
      • ~/.claude/settings.json — passed in as ``cfg``
      • ~/.claude.json          — where ``claude mcp add`` writes (HTTP entries)
    Either being correct is enough — that's the same merge Claude Code does
    at startup.
    """
    target = _our_mcp_url(mcp_port)
    sources: list[dict] = []
    if isinstance(cfg, dict):
        sources.append(cfg)
    sources.append(_load_claude_user_config())
    for source in sources:
        servers = source.get("mcpServers")
        if not isinstance(servers, dict):
            continue
        voitta = servers.get("voitta")
        if isinstance(voitta, dict) and voitta.get("url") == target:
            return True
    return False


def is_codex_mcp_wired(mcp_port: int) -> bool:
    """True iff Codex's ~/.codex/config.toml has [mcp_servers.voitta] pointing
    at our MCP proxy."""
    if not CODEX_CONFIG_PATH.exists():
        return False
    try:
        try:
            import tomllib  # py 3.11+
        except ImportError:  # pragma: no cover — fallback for older Python
            import tomli as tomllib  # type: ignore[no-redef]
        data = tomllib.loads(CODEX_CONFIG_PATH.read_text())
    except Exception:
        return False
    servers = data.get("mcp_servers")
    if not isinstance(servers, dict):
        return False
    voitta = servers.get("voitta")
    if not isinstance(voitta, dict):
        return False
    return voitta.get("url") == _our_mcp_url(mcp_port)


def _load_claude_user_config() -> dict:
    """Return parsed ~/.claude.json, or {} if missing/malformed."""
    if not CLAUDE_USER_CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CLAUDE_USER_CONFIG_PATH.read_text())
    except Exception:
        return {}


def _our_url(our_port: int) -> str:
    return f"http://127.0.0.1:{our_port}"


def _our_mcp_url(mcp_port: int) -> str:
    return f"http://127.0.0.1:{mcp_port}/mcp"


# ── Plan builders ────────────────────────────────────────────────────────────


def plan_connect(cfg: dict, our_port: int, voitta_upstream: str) -> Plan:
    """Compute the Connect plan.

    Inputs:
        cfg              — current contents of ~/.claude/settings.json
        our_port         — voitta-desktop's LLM proxy port (saved value)
        voitta_upstream  — voitta-desktop's current llm_proxy.upstream_url

    Behaviour:
        - ANTHROPIC_BASE_URL is set to http://127.0.0.1:<our_port>
        - If Claude already had a non-Voitta ANTHROPIC_BASE_URL, save it
          under VOITTA_ANTHROPIC_BASE_URL (preserved original) AND set
          voitta-desktop's upstream_url to that value (inheritance).
        - If VOITTA_ANTHROPIC_BASE_URL already exists from a prior install,
          trust it as the canonical original (recovery from dirty state).
        - ENABLE_TOOL_SEARCH: only added if not already present. Existing
          values are left alone.
    """
    env = _env_block(cfg)
    our_url = _our_url(our_port)
    changes: list[Change] = []

    existing_base = env.get("ANTHROPIC_BASE_URL")
    existing_voitta_saved = env.get("VOITTA_ANTHROPIC_BASE_URL")

    # ANTHROPIC_BASE_URL — always set to our URL on connect.
    if existing_base != our_url:
        changes.append(Change("env.ANTHROPIC_BASE_URL", existing_base, our_url))

    # Determine what (if anything) to preserve as the "original" for restore.
    # Trust an existing VOITTA_ value if present (dirty recovery); otherwise
    # use the current ANTHROPIC_BASE_URL if it's not already pointing at us.
    if existing_voitta_saved is not None:
        original = existing_voitta_saved
    elif existing_base is not None and existing_base != our_url:
        original = existing_base
        changes.append(Change("env.VOITTA_ANTHROPIC_BASE_URL", None, original))
    else:
        original = None  # nothing to preserve, clean install

    # voitta-desktop inherits the original upstream when there is one and it
    # differs from our current upstream_url.
    voitta_upstream_change: Change | None = None
    if original is not None and original != voitta_upstream:
        voitta_upstream_change = Change(
            "Voitta upstream URL", voitta_upstream, original
        )

    # ENABLE_TOOL_SEARCH — only add if not present at all (any existing
    # value is honoured, even "false" or "0").
    if "ENABLE_TOOL_SEARCH" not in env:
        changes.append(Change("env.ENABLE_TOOL_SEARCH", None, "true"))

    return Plan(
        target="connect",
        claude_changes=changes,
        voitta_upstream_change=voitta_upstream_change,
    )


def plan_disconnect(cfg: dict, our_port: int) -> Plan:
    """Compute the Disconnect plan.

    Restores ~/.claude/settings.json as closely as possible to its
    pre-connect state. voitta-desktop's apps.json is left alone.

    Behaviour:
        - If VOITTA_ANTHROPIC_BASE_URL exists → restore its value as
          ANTHROPIC_BASE_URL and remove the VOITTA_ key.
        - Otherwise → remove ANTHROPIC_BASE_URL entirely.
        - ENABLE_TOOL_SEARCH is left alone (we don't track whether we
          added it, and "true" is harmless when not connected).
        - Caller (apply_changes) drops the "env" key entirely if it
          becomes empty.
    """
    env = _env_block(cfg)
    changes: list[Change] = []

    voitta_saved = env.get("VOITTA_ANTHROPIC_BASE_URL")
    current_base = env.get("ANTHROPIC_BASE_URL")

    if voitta_saved is not None:
        if current_base != voitta_saved:
            changes.append(Change("env.ANTHROPIC_BASE_URL", current_base, voitta_saved))
        changes.append(Change("env.VOITTA_ANTHROPIC_BASE_URL", voitta_saved, None))
    elif current_base is not None:
        changes.append(Change("env.ANTHROPIC_BASE_URL", current_base, None))

    return Plan(target="disconnect", claude_changes=changes)


# ── Apply ────────────────────────────────────────────────────────────────────


def apply_changes(plan: Plan) -> None:
    """Write plan.claude_changes to ~/.claude/settings.json.

    The file is rewritten in full (not patched line-by-line) — we read it,
    mutate the env block, write it back. All other top-level keys
    (mcpServers, hooks, permissions, …) are preserved verbatim.

    voitta_upstream_change is NOT applied here — the caller handles that
    by writing apps.json, since it owns the in-memory config object that
    needs to stay in sync.
    """
    cfg = load_claude_settings()
    env = _env_block(cfg)
    # Work on a copy so we don't mutate cfg["env"] if it was a non-dict.
    env = dict(env)

    for change in plan.claude_changes:
        if not change.label.startswith("env."):
            continue
        key = change.label[len("env."):]
        if change.new is None:
            env.pop(key, None)
        else:
            env[key] = change.new

    if env:
        cfg["env"] = env
    else:
        cfg.pop("env", None)

    CLAUDE_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Trailing newline for git-friendliness; 2-space indent matches the
    # convention Claude Code uses in its own examples.
    CLAUDE_SETTINGS_PATH.write_text(json.dumps(cfg, indent=2) + "\n")
