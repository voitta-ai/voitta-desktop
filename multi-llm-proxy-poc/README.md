# Multi-LLM gateway (POC)

A standalone proxy that lets an unmodified **Claude Code** client run on any of
several upstream accounts. You can switch between them from a local web page
without restarting Claude Code.

| Provider | Login | How requests are sent |
|---|---|---|
| Claude (Pro/Max subscription) | OAuth in the browser, same flow as `claude /login` | passed through; only the auth header is swapped |
| ChatGPT (Plus/Pro subscription) | OAuth in the browser, same flow as `codex login` | translated from Messages to the Codex Responses API |
| DeepSeek | API key | passed through to its Anthropic-compatible endpoint; model renamed |
| Mistral | API key | translated from Messages to Chat Completions |
| OpenAI API, or another Anthropic/OpenAI-compatible service | API key + base URL | same as above |

Once this is debugged, it is meant to replace the pass-through LLM proxy in Voitta Desktop.

## Run

```bash
cd multi-llm-proxy-poc
../.venv/bin/python run.py          # needs only aiohttp
open http://localhost:18910
```

Add accounts on that page and choose the default. Then start Claude Code with the command the page shows:

```bash
claude --settings '{"env": {"ANTHROPIC_BASE_URL": "http://localhost:18910"}}'
```

Use `--settings` rather than plain environment variables. An `ANTHROPIC_BASE_URL`
in `~/.claude/settings.json` overrides the process environment, and Voitta Desktop
writes one there. Claude Code keeps its own login: the **As is** option forwards it
untouched, and every other account replaces it with that account's credentials.

## Per-window accounts: `/llm`

`mod/` is a local plugin marketplace holding one Claude Code mod, `voitta-llm`
(needs Claude Code 2.1.287+). The page's **Install /llm** button adds it with
`claude plugin marketplace add mod/` and `claude plugin install voitta-llm@voitta`.

- `/llm` opens a picker of this gateway's accounts (a numbered list where no panel can
  be drawn, e.g. VS Code); `/llm deepseek`, `/llm 2` and `/llm default` pick directly.
- The pick applies to **that window only**. Claude Code sends `X-Claude-Code-Session-Id`
  with every request (main loop, background calls and subagents, checked in the e2e
  test), and the gateway routes by it: the window's pick, else the default.
- The mod never touches requests; it only tells the gateway "session → account"
  (`GET /_gw/api/llm/options`, `PUT /_gw/api/llm/sessions/<id>`). Picks are stored in
  `$LLMGW_HOME/sessions.json` and survive gateway restarts.
- `--resume` keeps the session id, so the pick stays. `/clear` starts a new id; the mod
  hands the window's pick to it.
- A picked account that is later removed or needs a new login fails that window's
  requests with an error. It never falls back to the default.
- The mod finds the gateway through `ANTHROPIC_BASE_URL`. In a window that doesn't go
  through a gateway it stays silent until you type `/llm`.

Mod checks: `claude plugin validate mod/voitta-llm` and `claude plugin test mod/voitta-llm`.

Environment variables: `LLMGW_PORT` (default 18910) and `LLMGW_HOME` (default
`~/.voitta-llm-poc`). Accounts are stored in `$LLMGW_HOME/accounts.json`, mode 0600.

## Behaviour

- **Logging in to an account that's already listed** replaces that entry's tokens and
  keeps its settings. Accounts are identified by the provider's account and organization ids
  (Claude) or user and workspace ids (ChatGPT), so logging in with a different account
  adds a new row.
- **Claude: link + code** adds someone else's account, or one signed in from a browser on another
  machine. *Create link* copies a ready-to-send message. The other person signs in, approves, and
  sends back the code Claude shows them, which you paste in. Open invites last 7 days and survive
  restarts (`$LLMGW_HOME/invites.json`). Claude's code itself is short-lived, so paste it soon. If
  the exchange fails, the invite stays open and they can reopen the same link for a new code.
- **ChatGPT: link + device code** does the same for ChatGPT, using OpenAI's device-code login
  (what `codex login --device-auth` uses). You send them `https://auth.openai.com/codex/device`
  and an 8-character code; they sign in and enter it. The gateway polls OpenAI and adds the account
  as soon as they approve, so nothing comes back to paste. OpenAI's codes expire after about
  15 minutes and are held in memory only, so a restart cancels waiting codes. This also avoids
  port 1455.
- **Tokens refresh themselves**: before any request whose token expires within 5 minutes,
  in a background sweep every 10 minutes, and once more on an upstream 401. If the
  provider rejects the refresh token, the account shows *log in again*.
- **Routing**: a window goes to the account it picked with `/llm`, otherwise to the default
  (an account, or **As is**). Removing the default resets it to As is, never to some
  other account. Errors, including 429s and usage limits, are returned to Claude Code
  as Anthropic-shaped errors. The gateway never fails over to another account.
- **Models**: each non-Claude account maps Claude Code's model tiers onto its own models.
  `*haiku*` requests (Claude Code's background tasks) use *background*, and every other
  `claude-*` model uses *main*. The menus list the provider's real models, fetched with the
  account's own credentials when the account is added (*↻ Models* reloads them). A model name
  that doesn't start with `claude` (e.g. `/model deepseek-v4-pro`) is sent unchanged.
- **Effort**: Claude Code sends its `/effort` level on every request. ChatGPT accounts (and
  OpenAI API reasoning models) use it, clamped to the levels the chosen model supports. An
  account can instead fix its effort on the page. DeepSeek and Mistral have no effort setting;
  how much they reason depends on which model you pick.
- **Privacy**: Anthropic-compatible third parties receive only standard Messages fields.
  Claude Code's `safeguards` (local paths, user name), `context_management`, `output_config`
  and server tools are removed before sending.
- **Thinking**: ChatGPT reasoning summaries are shown to Claude Code as thinking blocks.
  The encrypted reasoning state is carried in the block's signature (`voitta-oai:` prefix),
  so later turns can hand it back. The gateway removes these blocks before a request goes
  to Anthropic, so you can switch accounts in the middle of a conversation.
- **Not translated**: Anthropic server tools (web search, web fetch), which are dropped for
  non-Anthropic providers; `cache_control` markers; and PDFs for Chat Completions providers.
  `count_tokens` is an estimate (about 4 characters per token) for translated providers.
- `X-LLMGW-Account: <id>` on a request pins it to one account. The UI's *Test* button uses this.

## Layout

```
llmgw/
  server.py               routes, login callbacks, account API, UI
  upstream.py             per-request dispatch, credential refresh, passthrough, request log
  oauth.py                Claude + ChatGPT PKCE logins and token refresh
  store.py                accounts.json; replaces existing accounts on re-login
  providers.py            presets and model mapping
  translate_chat.py       Messages ⇄ Chat Completions
  translate_responses.py  Messages ⇄ Responses (Codex backend)
  common.py, sse.py       shared stream and event helpers
  static/index.html       the account page
tests/
  test_*.py               unit and app tests against a fake upstream (pytest)
  e2e_claude_code.py      runs the real `claude` CLI through the gateway: tool loop, subagents,
                          --resume, and the /llm mod picking per window
mod/
  .claude-plugin/marketplace.json   local marketplace "voitta"
  voitta-llm/                       the /llm mod (hooks/register.tsx, tests/)
```

```bash
../.venv/bin/python -m pytest -q
../.venv/bin/python tests/e2e_claude_code.py
```

## Caveats

- The OAuth client ids are Claude Code's and the Codex CLI's own. The ChatGPT login
  needs port 1455, which the Codex client is registered for. The gateway listens there
  only while a login is in progress, so a `codex login` running at the same moment will
  conflict.
- Using subscription credentials outside each vendor's own client may conflict with
  their terms. This POC is for use with your own accounts.
