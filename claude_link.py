"""Wire / unwire Claude Code → Voitta Desktop via ~/.claude/settings.json.

The Connect/Disconnect feature on the Settings → Proxies tab uses this module
to compute and apply the diff. Two side-effect surfaces:

  • ~/.claude/settings.json — the file Claude Code reads on startup. We
    rewrite the `env` block to point ANTHROPIC_BASE_URL at our local proxy.

  • Claude Code's user-scope MCP servers (~/.claude.json) — Connect
    registers our MCP proxy as `voitta` by running `claude mcp add`, so
    that sessions can resolve the get_vt_object references the optimizer
    leaves behind. Claude Code rewrites ~/.claude.json constantly, so we go
    through its own CLI rather than editing the file.

  • voitta-desktop's apps.json (llm_proxy.upstream_url) — when we connect
    over the top of an existing ANTHROPIC_BASE_URL (e.g. corporate gateway),
    we INHERIT that URL as our own upstream, so the chain becomes:

        Claude Code → Voitta proxy → corporate gateway → Anthropic

    The original is also preserved under env.VOITTA_ANTHROPIC_BASE_URL so
    Disconnect can put settings.json back exactly the way it was.

Disconnect leaves voitta-desktop's apps.json alone (per the user's "leave
voitta desktop as is" rule). It restores ~/.claude/settings.json and removes
the MCP registration.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

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
    # The user-scope `voitta` MCP server (URL), applied via `claude mcp`.
    mcp_change: Change | None = None

    @property
    def is_noop(self) -> bool:
        return (not self.claude_changes and self.voitta_upstream_change is None
                and self.mcp_change is None)


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


def is_mcp_wired(mcp_port: int) -> bool:
    """True iff Claude Code's user-scope MCP config points at our MCP proxy.

    Only ~/.claude.json counts. Claude Code ignores ``mcpServers`` in a
    settings file (checked with 2.1.296: a server passed that way never
    reached the request).
    """
    return _registered_mcp_url() == _our_mcp_url(mcp_port)


def _registered_mcp_url() -> str | None:
    """URL of the user-scope `voitta` MCP server, or None if absent."""
    servers = _load_claude_user_config().get("mcpServers")
    voitta = servers.get("voitta") if isinstance(servers, dict) else None
    return voitta.get("url") if isinstance(voitta, dict) else None


def _has_mcp_registration() -> bool:
    servers = _load_claude_user_config().get("mcpServers")
    return isinstance(servers, dict) and "voitta" in servers


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


# What Disconnect writes when there is no saved original to restore. Deleting
# ANTHROPIC_BASE_URL instead would not disconnect anything that is running:
# Claude Code applies changed or added `env` keys to live sessions within
# seconds, but a removed key stays in their process environment. Those
# sessions would keep calling our port after Quit (or a crash) and fail.
ANTHROPIC_DEFAULT_URL = "https://api.anthropic.com"


def _our_url(our_port: int) -> str:
    return f"http://127.0.0.1:{our_port}"


def _is_one_of_our_urls(url: str | None) -> bool:
    """True if ``url`` has the shape this module arms with, on ANY port.

    Comparing against the *current* port is not enough. Our port is not
    stable — ``_resolve_port`` falls back to an OS-assigned one whenever the
    configured port is busy (a stale instance, a second copy during an
    upgrade), so the app can arm as :18900 today and :18901 tomorrow.

    Without this check, the next arm sees an ANTHROPIC_BASE_URL that isn't
    the current URL, concludes it must be the user's own upstream, and files
    it under VOITTA_ANTHROPIC_BASE_URL as the value to restore. Disconnect
    then faithfully "restores" a Voitta URL for a port nothing is listening
    on, so Claude Code keeps failing after the app is gone — the exact
    opposite of what disarming is for. This module cannot ever hand back one
    of its own URLs as the user's original.

    Tradeoff: someone whose real upstream is a bare loopback HTTP endpoint
    (a local LiteLLM, say) has it dropped on connect rather than preserved.
    That is visible and one setting to redo, whereas the alternative leaves
    Claude Code silently pointed at a dead port with no clue why.
    """
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return (
        parsed.scheme == "http"
        and parsed.hostname in ("127.0.0.1", "localhost")
        and not parsed.path.strip("/")
        and not parsed.query
    )


def _our_mcp_url(mcp_port: int) -> str:
    return f"http://127.0.0.1:{mcp_port}/mcp"


def _is_one_of_our_mcp_urls(url: str | None) -> bool:
    """True if ``url`` has the shape of our MCP proxy URL, on any port.

    Same reasoning as ``_is_one_of_our_urls``: the port can move. Anything
    else registered as `voitta` (a remote Voitta server, say) is the user's
    and is left alone.
    """
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return (
        parsed.scheme == "http"
        and parsed.hostname in ("127.0.0.1", "localhost")
        and parsed.path.rstrip("/") == "/mcp"
        and not parsed.query
    )


def _plan_mcp_connect(mcp_port: int) -> Change | None:
    current = _registered_mcp_url()
    ours = _our_mcp_url(mcp_port)
    if current == ours:
        return None
    if current is None and not _has_mcp_registration():
        return Change("mcpServers.voitta", None, ours)
    if _is_one_of_our_mcp_urls(current):
        return Change("mcpServers.voitta", current, ours)
    return None  # someone else's server called `voitta`


def _plan_mcp_disconnect() -> Change | None:
    current = _registered_mcp_url()
    if _is_one_of_our_mcp_urls(current):
        return Change("mcpServers.voitta", current, None)
    return None

# ── Plan builders ────────────────────────────────────────────────────────────


def plan_connect(cfg: dict, our_port: int, voitta_upstream: str,
                 mcp_port: int | None = None) -> Plan:
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
        - With ``mcp_port``, register our MCP proxy as the user-scope
          `voitta` server, unless a `voitta` server that isn't ours exists.
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
    # A previously-saved value is only trustworthy if it isn't one of ours —
    # an earlier run on a different port could have stored its own URL there.
    if existing_voitta_saved is not None and not _is_one_of_our_urls(existing_voitta_saved):
        original = existing_voitta_saved
    elif existing_base is not None and not _is_one_of_our_urls(existing_base):
        original = existing_base
        changes.append(Change("env.VOITTA_ANTHROPIC_BASE_URL", None, original))
    elif existing_voitta_saved is not None:
        # Poisoned by an earlier port change: drop it rather than carry a
        # dead URL forward as if it were the user's own.
        original = None
        changes.append(Change("env.VOITTA_ANTHROPIC_BASE_URL", existing_voitta_saved, None))
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
        mcp_change=_plan_mcp_connect(mcp_port) if mcp_port is not None else None,
    )


def plan_disconnect(cfg: dict, our_port: int, mcp: bool = False) -> Plan:
    """Compute the Disconnect plan.

    Restores ~/.claude/settings.json as closely as possible to its
    pre-connect state. voitta-desktop's apps.json is left alone.

    Behaviour:
        - If VOITTA_ANTHROPIC_BASE_URL exists → restore its value as
          ANTHROPIC_BASE_URL and remove the VOITTA_ key.
        - Otherwise → set ANTHROPIC_BASE_URL to ANTHROPIC_DEFAULT_URL.
          Not removed: running sessions ignore a removed key (see
          ANTHROPIC_DEFAULT_URL).
        - ENABLE_TOOL_SEARCH is left alone (we don't track whether we
          added it, and "true" is harmless when not connected).
        - Caller (apply_changes) drops the "env" key entirely if it
          becomes empty.
        - With ``mcp``, remove the user-scope `voitta` MCP server if it
          points at our MCP proxy (any port).
    """
    env = _env_block(cfg)
    changes: list[Change] = []

    voitta_saved = env.get("VOITTA_ANTHROPIC_BASE_URL")
    current_base = env.get("ANTHROPIC_BASE_URL")

    if voitta_saved is not None and not _is_one_of_our_urls(voitta_saved):
        if current_base != voitta_saved:
            changes.append(Change("env.ANTHROPIC_BASE_URL", current_base, voitta_saved))
        changes.append(Change("env.VOITTA_ANTHROPIC_BASE_URL", voitta_saved, None))
    else:
        # Either nothing was saved, or what was saved is one of our own URLs
        # from an earlier port. Restoring that would point Claude Code at a
        # dead port; the default is what actually disconnects.
        if voitta_saved is not None:
            changes.append(Change("env.VOITTA_ANTHROPIC_BASE_URL", voitta_saved, None))
        if current_base != ANTHROPIC_DEFAULT_URL:
            changes.append(Change("env.ANTHROPIC_BASE_URL", current_base, ANTHROPIC_DEFAULT_URL))

    return Plan(
        target="disconnect",
        claude_changes=changes,
        mcp_change=_plan_mcp_disconnect() if mcp else None,
    )


# ── Apply ────────────────────────────────────────────────────────────────────


def apply_changes(plan: Plan) -> None:
    """Write plan.claude_changes to ~/.claude/settings.json, then apply
    plan.mcp_change through `claude mcp`.

    The file is rewritten in full (not patched line-by-line) — we read it,
    mutate the env block, write it back. All other top-level keys
    (mcpServers, hooks, permissions, …) are preserved verbatim.

    voitta_upstream_change is NOT applied here — the caller handles that
    by writing apps.json, since it owns the in-memory config object that
    needs to stay in sync.
    """
    if plan.claude_changes:
        _write_env_changes(plan.claude_changes)
    if plan.mcp_change is not None:
        apply_mcp_change(plan.mcp_change)


def _write_env_changes(changes: list[Change]) -> None:
    cfg = load_claude_settings()
    env = _env_block(cfg)
    # Work on a copy so we don't mutate cfg["env"] if it was a non-dict.
    env = dict(env)

    for change in changes:
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


# ── `claude mcp` ─────────────────────────────────────────────────────────────

# Where the installers put `claude`. An app started from Finder or launchd
# does not get the login shell's PATH, so shutil.which alone misses these.
_CLAUDE_CANDIDATES = (
    Path.home() / ".local" / "bin" / "claude",
    Path.home() / ".claude" / "local" / "claude",
    Path("/opt/homebrew/bin/claude"),
    Path("/usr/local/bin/claude"),
)


class McpCliError(RuntimeError):
    """`claude mcp` is missing or failed. settings.json is already written
    by the time this is raised."""


def find_claude_cli() -> str | None:
    found = shutil.which("claude")
    if found:
        return found
    for path in _CLAUDE_CANDIDATES:
        if os.access(path, os.X_OK):
            return str(path)
    return None


def mcp_add_command(url: str) -> list[str]:
    return ["claude", "mcp", "add", "--transport", "http", "--scope", "user", "voitta", url]


def _run_claude(args: list[str]) -> None:
    cli = find_claude_cli()
    if cli is None:
        raise McpCliError("claude CLI not found")
    env = dict(os.environ)
    # An npm-installed `claude` is a node script; give it a PATH that finds
    # node even when we were started without the login shell's PATH.
    env["PATH"] = os.pathsep.join(
        [str(Path(cli).parent), "/opt/homebrew/bin", "/usr/local/bin", env.get("PATH", "")]
    )
    try:
        result = subprocess.run(
            [cli, *args], cwd=Path.home(), env=env,
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise McpCliError(f"claude {' '.join(args[:2])} failed: {e}") from e
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise McpCliError(f"claude {' '.join(args[:2])} failed: {detail}")


def apply_mcp_change(change: Change) -> None:
    """Apply a ``mcpServers.voitta`` change through `claude mcp`.

    Raises McpCliError if the CLI is missing or fails.
    """
    if change.old is not None:
        _run_claude(["mcp", "remove", "--scope", "user", "voitta"])
    if change.new is not None:
        _run_claude(mcp_add_command(change.new)[1:])


# ── Convenience wrappers ─────────────────────────────────────────────────────
#
# The load → check → plan → apply sequence was open-coded in three places
# (the TUI's toggle, the menu app's startup re-arm, and its quit handler),
# which is how the TUI ended up importing two functions that did not exist.
# One definition, three callers.

def arm_claude_link(our_port: int, voitta_upstream: str,
                    mcp_port: int | None = None) -> bool:
    """Point ~/.claude/settings.json at our proxy, and with ``mcp_port``
    register our MCP proxy. Returns True if anything changed.

    Idempotent: already-connected settings are left alone.
    """
    cfg = load_claude_settings()
    plan = plan_connect(cfg, our_port, voitta_upstream, mcp_port)
    if is_voitta_connected(cfg, our_port):
        plan = Plan(target="connect", mcp_change=plan.mcp_change)
    if plan.is_noop:
        return False
    apply_changes(plan)
    return True


def disarm_claude_link(our_port: int, mcp: bool = False) -> bool:
    """Strip our proxy from ~/.claude/settings.json, and with ``mcp`` remove
    our MCP registration. Returns True if anything changed.

    Idempotent: settings that aren't wired to us are left alone.
    """
    cfg = load_claude_settings()
    plan = plan_disconnect(cfg, our_port, mcp)
    if not is_voitta_connected(cfg, our_port):
        plan = Plan(target="disconnect", mcp_change=plan.mcp_change)
    if plan.is_noop:
        return False
    apply_changes(plan)
    return True
