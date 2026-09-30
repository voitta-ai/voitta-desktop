# Voitta Desktop

macOS menu bar app that fuses two proxies into a single application:

- **LLM Proxy** (port 18900) — intercepts Claude API calls, tracks conversations, optimizes context to save tokens
- **MCP Auth Proxy** (port 18765) — unified FastMCP endpoint that aggregates RAG, Google Workspace, and Jira backends with automatic OAuth2 token injection

The dog in your menu bar shows token savings and active conversation count. Click it for auth status on top, live conversation details below.

## Setup

### 1. Install

Download `Voitta.Desktop-<version>.dmg` from the
[latest release](https://github.com/voitta-ai/voitta-desktop/releases/latest),
open it and drag **Voitta Desktop** to **Applications**. The build is signed
and notarized, and runs on Apple silicon with macOS 14 or later.

Launch it. A dog icon appears in your menu bar, both proxies start, and on
first launch the Settings window opens, where credentials and backends are
configured. Later, open Settings from the dog's menu. macOS hides menu bar
icons that don't fit (on a notched MacBook with a busy menu bar, the dog may
never be drawn). Launching Voitta Desktop again while it runs, from
Spotlight or Applications, also opens Settings.

<details>
<summary>Run from source instead</summary>

```bash
cd voitta-desktop
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.sample .env   # optional, see below
python app.py
```

On first start the app seeds `~/.voitta-desktop/apps.json` from the
environment, including a `.env` in the working directory. After that
`apps.json` wins; change settings in the Settings window. Key fields:

| Variable | What it is |
|---|---|
| `AZURE_TENANT_ID` | Microsoft Entra ID (Azure AD) tenant |
| `AZURE_CLIENT_ID` | Microsoft app registration client ID |
| `GOOGLE_CLIENT_ID` | Google OAuth2 client ID |
| `GOOGLE_CLIENT_SECRET` | Google OAuth2 client secret |
| `JIRA_URL` | e.g. `https://yourorg.atlassian.net` |
| `JIRA_EMAIL` | Your Jira account email |
| `JIRA_API_TOKEN` | Jira API token (create at https://id.atlassian.com/manage-profile/security/api-tokens) |

</details>

### 2. Configure Claude Code

Claude Code needs two things pointed at Voitta Desktop:

#### A. LLM Proxy — set the base URL

Add to your shell profile (`~/.zshrc` or `~/.bashrc`):

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:18900
```

This routes all Claude API traffic through the local proxy, enabling conversation tracking, token usage visualization, and context optimization (image stripping, stale file read removal).

Your `ANTHROPIC_API_KEY` stays the same — the proxy forwards it to `api.anthropic.com` transparently.

#### B. MCP Auth Proxy — register the MCP server

Add to your project's `.mcp.json` (or `~/.claude/.mcp.json` for global):

```json
{
  "mcpServers": {
    "voitta": {
      "url": "http://127.0.0.1:18765/mcp"
    }
  }
}
```

This gives Claude access to all mounted backends through a single endpoint:

| Tool prefix | Backend | What it does |
|---|---|---|
| `voitta_rag_*` | voitta.ai RAG | Search, memory, file retrieval across your indexed docs |
| `google_workspace_*` | Google Workspace | Gmail, Drive, Sheets, Docs, Calendar, Slides |
| `jira_*` | Jira Cloud | Issues, sprints, boards, comments |

#### Full example — `.mcp.json` in your project root:

```json
{
  "mcpServers": {
    "voitta": {
      "url": "http://127.0.0.1:18765/mcp"
    }
  }
}
```

#### Full example — `~/.zshrc` addition:

```bash
# Route Claude Code through Voitta Desktop proxy
export ANTHROPIC_BASE_URL=http://127.0.0.1:18900
```

### 3. Authenticate providers

After launching Voitta Desktop:

1. Click the dog icon in the menu bar
2. Click a provider (e.g. "Google") under the Auth section
3. Browser opens, sign in, authorize
4. Provider shows as connected (filled dot)

Tokens refresh automatically in the background. Click a connected provider again to sign out.

## Ports

| Port | Purpose | Configured via |
|---|---|---|
| 18900 | LLM reverse proxy (Anthropic API) | `LLM_PROXY_PORT` |
| 18765 | MCP auth proxy (FastMCP) | `MCP_PROXY_PORT` |
| 18766 | Google Workspace MCP subprocess | `GOOGLE_MCP_PORT` |
| 18767 | Jira MCP (mcp-atlassian) subprocess | `JIRA_MCP_PORT` |
| 53214 | OAuth2 redirect callback | `OAUTH_REDIRECT_PORT` |

Saved in `~/.voitta-desktop/apps.json` and editable in Settings. The
variables only seed a fresh config.

## What you see in the menu bar

```
$2.15 3 🐕
```

- Green dollar amount = cumulative token savings from context optimization
- Number = active conversations
- Dog = it's running

Click the dog to expand:

```
── Auth ─────────────────────────────
MCP  http://127.0.0.1:18765/mcp
RAG (voitta.ai)
●  Google                       you@gmail.com
○  Microsoft                    Not connected
Jira
●  Jira Cloud                   PROJ (you@company.com)

── Conversations ────────────────────
LLM Proxy  http://127.0.0.1:18900
"Fix the login bug"  [1.2M cache:42%] ×12
  ▶ user message...
  ◆ thinking...
  ⚙ Read(/src/app.py)
  ◀ assistant response...
"Add unit tests"  [800k cache:31%] ×5

Settings
Help
Quit
```

Click a conversation to see a token usage chart. Click "Open conversation details..." for the interactive stacked-bar visualization.

## Config storage

Everything lives under `~/.voitta-desktop/` (set `VOITTA_DESKTOP_HOME` to
move it):

- `apps.json` — OAuth apps, MCP servers, Jira credentials, proxy ports
- `logs/` — `desktop.log`, request JSONL, per-conversation dumps, MCP subprocess logs
- `state/objects.db` — Removed tool results, images and calls, retrievable via `get_vt_object`
- `cache/tools/` — Cached MCP tool listings (resilience)
- `jira.env` — Auto-generated for the mcp-atlassian subprocess

Every restart deletes the previous runs' conversations: the request JSONL,
the per-conversation and rejected-request dumps, and the object store.
Claude Code's own transcripts are untouched.

On first run, Voitta Desktop picks up config from the older
`~/.voitta_desktop/`, `~/.voitta_desktop_cache/` and `~/.voitta_auth/`
locations if they exist.

## MCP subprocess dependencies

The MCP auth proxy launches two optional subprocesses. These are only needed if you use the corresponding backends:

- **Google Workspace**: expects `google_workspace_mcp` at `~/DEVEL/google_workspace_mcp` (configurable via `GOOGLE_MCP_DIR`)
- **Jira**: uses `mcp-atlassian` via `uvx` (install with `uv tool install mcp-atlassian`)

If a subprocess directory doesn't exist, that backend is silently skipped.

## License

GNU AGPLv3
