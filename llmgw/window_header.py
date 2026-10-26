"""Which Claude Code window an MCP connection belongs to.

The voitta-llm mod's /mcp hook gives one Claude Code window its own tool list.
For that, Voitta's MCP server must know which window each MCP connection comes
from, and MCP traffic carries no Claude Code session id. What both sides can
see is the Claude Code process itself:

* Claude Code runs the MCP entry's ``headersHelper`` on every connection, as
  its own child. This script prints that parent as a header.
* The mod runs the same shell lines through ``$.process.run``, also a child of
  the window's Claude Code process.

Both print ``<pid>@<process start time>``; the start time keeps a recycled pid
from inheriting an old window's choice. A shell between Claude Code and the
script (a ``sh -c`` wrapper) is stepped over once.

``ensure_on_voitta_entry`` installs the script on the user's ``voitta`` MCP
entry with Claude Code's own CLI (``claude mcp remove`` + ``add-json``), never
by writing ``~/.claude.json`` directly: Claude Code rewrites that file all the
time and a direct write could lose its changes.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger("voitta-desktop.llm")

HEADER = "X-Voitta-Claude-Window"

# Sets p (the window's Claude Code pid) and s (its start time). The mod
# (hooks/register.tsx, WINDOW_ID_SH) runs these same lines, then prints
# "$p@$s"; tests/test_mcp_window_tools.py keeps the two identical. No command
# substitution around a `case`: some shells end $( ) at the pattern's ")".
WINDOW_ID_SH = """p=$PPID
case "$(ps -o comm= -p "$p" 2>/dev/null)" in
  sh|*/sh|bash|*/bash|zsh|*/zsh|dash|*/dash|-*) p=$(ps -o ppid= -p "$p" 2>/dev/null | tr -d ' ') ;;
esac
s=$(ps -o lstart= -p "$p" 2>/dev/null)
s=$(echo $s | tr ' ' '_')"""

SCRIPT = f"""#!/bin/sh
# Voitta Desktop: names the Claude Code window this MCP connection comes from,
# so /mcp (voitta-llm mod) can give that one window its own tool list.
# Installed on the "voitta" MCP entry as its headersHelper.
{WINDOW_ID_SH}
printf '{{"{HEADER}": "%s@%s"}}\\n' "$p" "$s"
"""


def script_path(voitta_home: Path) -> Path:
    return voitta_home / "bin" / "claude-window-header"


def ensure_script(path: Path) -> Path:
    """Write the script if it is missing or different; it must be executable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_text() != SCRIPT:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(SCRIPT)
        os.chmod(tmp, 0o755)
        os.replace(tmp, path)
    elif not os.access(path, os.X_OK):
        os.chmod(path, 0o755)
    return path


def claude_user_config() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base).expanduser() / ".claude.json") if base else Path.home() / ".claude.json"


def voitta_entry(config_path: Path | None = None) -> dict | None:
    """The user-scope ``voitta`` MCP entry, read-only; None if absent."""
    try:
        data = json.loads((config_path or claude_user_config()).read_text())
    except (OSError, ValueError):
        return None
    entry = (data.get("mcpServers") or {}).get("voitta") if isinstance(data, dict) else None
    return entry if isinstance(entry, dict) else None


def status(script: Path, config_path: Path | None = None) -> str:
    """set | missing | stale | no_entry | other_helper | not_http.

    ``stale``: a Voitta header script, but at another path (an older Voitta
    home, or a copy that is gone) — replaced like ``missing``. A helper of any
    other name is the user's own and is left alone (``other_helper``).
    """
    entry = voitta_entry(config_path)
    if entry is None:
        return "no_entry"
    if entry.get("type") not in ("http", "sse") or not entry.get("url"):
        return "not_http"
    helper = entry.get("headersHelper")
    if helper == str(script):
        return "set"
    if not helper:
        return "missing"
    return "stale" if Path(helper).name == script.name else "other_helper"


async def ensure_on_voitta_entry(script: Path, run_claude, config_path: Path | None = None) -> tuple[str, list[dict]]:
    """Put the script on the user's voitta entry, keeping every other field.

    ``run_claude(*args) -> (exit code, output)`` runs ``claude <args>``.
    Returns (status, steps). Never replaces a headersHelper the user set
    themselves, and puts the original entry back if the new one is refused.
    """
    ensure_script(script)
    current = status(script, config_path)
    if current not in ("missing", "stale"):
        return current, []
    entry = voitta_entry(config_path)
    wanted = {**entry, "headersHelper": str(script)}
    steps: list[dict] = []

    async def run(*args):
        code, out = await run_claude(*args)
        shown = list(args)
        steps.append({"command": "claude " + " ".join(shown[:4] + (["…"] if len(shown) > 4 else [])),
                      "exit": code, "output": out[-600:]})
        return code

    if await run("mcp", "remove", "voitta", "-s", "user") != 0:
        return "missing", steps
    if await run("mcp", "add-json", "-s", "user", "voitta", json.dumps(wanted)) == 0:
        log.info("voitta MCP entry: headersHelper set to %s", script)
        return status(script, config_path), steps
    log.error("voitta MCP entry: the entry with headersHelper was refused; putting the original back")
    await run("mcp", "add-json", "-s", "user", "voitta", json.dumps(entry))
    return status(script, config_path), steps
