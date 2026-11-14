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

Settings → **Actions** → **Configure Claude**. It sets up Claude Code for your
user (every project and window) in the three places Claude Code reads, and
shows what it will change before you click:

| What | Where | Value |
|---|---|---|
| LLM proxy | `~/.claude/settings.json` → `env.ANTHROPIC_BASE_URL` | `http://127.0.0.1:<LLM proxy port>` |
| MCP server | Claude Code's user MCP servers, `~/.claude.json` → `voitta` | `http://127.0.0.1:<MCP proxy port>/mcp`, with Voitta's header script |
| `/llm` mod | `claude plugin install voitta-llm@voitta` | `/llm`, `/voitta-store`, the `/mcp` tool popup |

Everything else in those files is kept. The MCP entry and the mod are changed
only through Claude Code's own CLI (`claude mcp add-json -s user`,
`claude plugin`). A previous `ANTHROPIC_BASE_URL` (a company gateway, say) is
kept behind Voitta as its upstream and restored by **Disconnect LLM proxy**.
Running it again repairs whatever has drifted (a changed port, a missing header
script). Claude Code windows that are already open switch to the LLM proxy
within seconds, mid-conversation; MCP tools and the mod reach them when they
are restarted. If `CLAUDE_CONFIG_DIR` is set, both files are in that folder
instead.

This is user-wide: while it is set, every Claude Code session in every project
goes through Voitta, and what those sessions send is kept under
`~/.voitta-desktop/` (request logs and removed tool results) until Voitta
Desktop next starts.

Claude Code's own credentials (a subscription login, a gateway token or an API
key) travel with each request and are passed upstream unchanged, so there is
no API key to enter in Voitta Desktop.

The MCP server gives Claude access to all mounted backends through one
endpoint. A fresh install has one backend, Enterprise Voitta MCP (`vim_*`),
which needs its bearer token in Settings → MCPs; until then the only tool is
`get_vt_object`. Add the others in Settings → MCPs:

| Tool prefix | Backend | What it does |
|---|---|---|
| `voitta_rag_*` | voitta.ai RAG | Search, memory, file retrieval across your indexed docs |
| `google_workspace_*` | Google Workspace | Gmail, Drive, Sheets, Docs, Calendar, Slides |
| `jira_*` | Jira Cloud | Issues, sprints, boards, comments |

By hand instead: `claude mcp add --transport http -s user voitta
http://127.0.0.1:18765/mcp`, and `"env": {"ANTHROPIC_BASE_URL":
"http://127.0.0.1:18900"}` in `~/.claude/settings.json`.

The **Actions** tab also holds the other one-off actions: install/update the
mod on its own, refresh MCP tools, reset the tool gate, show the log, stored
conversations.

### 3. Authenticate providers

After launching Voitta Desktop:

1. Click the dog icon in the menu bar
2. Click a provider (e.g. "Google") under the Auth section
3. Browser opens, sign in, authorize
4. Provider shows as connected (filled dot)

Tokens refresh automatically in the background. Click a connected provider again to sign out.

### 6. LLM accounts (optional)

By default Claude Code's requests go to Anthropic with Claude Code's own
login (**As is**). Settings → **LLMs** adds other accounts — Claude and
ChatGPT subscriptions (browser login, or a link + code for someone else's
account) and API-key providers (DeepSeek, Mistral, OpenAI, any
Anthropic- or OpenAI-compatible endpoint) — and sets the **default**. The
menu bar's **LLM ▸** submenu switches the default too.

Each Claude Code window can pick its own account with `/llm`, a Claude Code
mod (Claude Code 2.1.287+) installed with Settings → Actions → Configure Claude.
Requests still pass every optimizer first; non-Anthropic providers are
translated to and from the Anthropic Messages API. There is no fallback: a
failing account returns its error to Claude Code. Accounts live in
`~/.voitta-desktop/llm/` (mode 0600). See `llmgw/__init__.py`.

The same mod adds `/voitta-store`: Voitta Desktop copies the window's whole
conversation (Claude Code's transcript including compacted turns, its
subagents, and which account and model answered each request) to
`~/.voitta-desktop/conversations/<session id>/` (the transcript files byte for
byte, plus `meta.json` and the routing record). A one-second toast confirms
it; storing again replaces that window's copy with the newer one.

With **Suppress popup for Claude Code** on, the mod's `/mcp` hook first shows
Voitta's tool popup for **that window only**; the answer applies to the window
at once (no reconnect) and lasts until it closes or Voitta restarts, while other
windows keep the saved selection. It needs Voitta's header script
(`~/.voitta-desktop/bin/claude-window-header`) on the user-level `voitta` MCP
entry, which Settings → Actions → **Configure Claude** adds with `claude mcp`;
it applies to windows opened after that. Windows without the mod are
unchanged.

**Stored conversations…** in the menu bar (with the count) opens a window to
browse them: search titles, projects and models; sort; filter by project. A
conversation shows its whole history, compacted turns included, with each
answer's model and, where Voitta's routing record identifies it, which account
and model actually answered. The Models & routing tab lists what was asked
for, what answered and every routed request. Export as Markdown, one JSON file
with everything, or Claude Code's own `.jsonl` (several at once → a `.zip`);
Reveal in Finder; Delete removes Voitta's copy only, never Claude Code's files.

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
- `llm/` — LLM accounts and credentials, `/llm` window picks, open invites (mode 0600)
- `conversations/` — conversations stored with `/voitta-store`, one folder each (mode 0600; never purged)
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
