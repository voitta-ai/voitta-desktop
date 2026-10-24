import { atom, read, update } from 'claude-code'
import type { Hook, Register } from 'claude-code'

import type { LlmOption, LlmView, StoredConversation } from '../types'

// /llm: which account this Claude Code window's requests go to.
//
// The window talks to Voitta Desktop's LLM proxy through ANTHROPIC_BASE_URL, and
// every request carries Claude Code's session id. This mod only tells Voitta
// "session X -> account Y"; it never touches the requests themselves. Models
// per account are set in Voitta Desktop (Settings → LLMs).
//
// /voitta-store: Voitta Desktop copies this window's whole conversation (Claude
// Code's transcript, subagents, which account and model answered each request)
// into its own folder. A one-second toast, nothing left on screen.
//
// /mcp (Claude Code's own menu): when Voitta applies the saved tool selection
// silently to Claude Code, typing /mcp first shows Voitta's tool popup for this
// window only; the answer applies to this window right away, in memory, and
// /mcp opens once it is given. Anything else (no Voitta, an older Voitta, the
// popup not in use, this window's MCP connection not identifiable) lets /mcp
// open untouched.

type Engine = Parameters<Hook<'command.run'>>[0]

const PANE = 'voitta-llm'
const view = atom({ plugin: 'voitta-llm', key: 'view' } as const, null)
const error = atom({ plugin: 'voitta-llm', key: 'error' } as const, null)

async function gatewayUrl($: Engine): Promise<string> {
  const base = await $.env.get('ANTHROPIC_BASE_URL')
  if (!base) {
    throw new Error('ANTHROPIC_BASE_URL is not set, so this window does not go through Voitta Desktop.')
  }
  return base.replace(/\/+$/, '')
}

async function call<T = LlmView>($: Engine, method: string, path: string, body?: unknown): Promise<T> {
  const base = await gatewayUrl($)
  let r
  try {
    r = await $.http.fetch(base + path, {
      method,
      headers: { 'content-type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    })
  } catch (err) {
    throw new Error(`Voitta Desktop at ${base} is not reachable (${String(err)}).`)
  }
  if (r.status === 404) {
    throw new Error(`${base} answers, but it is not a Voitta Desktop with /llm support (update Voitta Desktop).`)
  }
  if (!r.ok) {
    throw new Error(`Voitta Desktop refused: ${r.status} ${r.text.slice(0, 200)}`)
  }
  return JSON.parse(r.text) as T
}

function optionLine(o: LlmOption): string {
  const model = o.main ? ` · ${o.main}` : ''
  const warn = o.status === 'ok' ? '' : ` ⚠ ${o.status.replace('_', ' ')}`
  return `${o.label} (${o.provider_label}${model})${warn}`
}

function statusText(v: LlmView): string {
  const picked = v.pick ? v.options.find(o => o.id === v.pick) : undefined
  if (picked) {
    return `LLM: ${picked.label}${picked.main ? ` · ${picked.main}` : ''}`
  }
  if (v.pick) {
    return `LLM: ${v.pick} (gone, run /llm)`
  }
  const d = v.default
  return `LLM: default → ${d ? `${d.label}${d.main ? ` · ${d.main}` : ''}` : 'none'}`
}

// The window's pick, kept here so a /clear (new session id, same window)
// can carry it over. Voitta Desktop is the record; this is only the carry.
let windowPick: string | null = null
// Where Claude Code keeps this window's transcript, as its hooks report it.
// Only a hint: Voitta Desktop checks it, and finds the file by session id without it.
let transcriptPath: string | null = null

async function storeConversation($: Engine): Promise<StoredConversation> {
  const [session, model, cwd] = await Promise.all([$.session.id(), $.session.model(), $.session.cwd()])
  return call<StoredConversation>($, 'POST', '/_voitta/llm/api/conversations', {
    session, model, cwd, transcript_path: transcriptPath,
  })
}

// This window's id as Voitta's MCP server knows it: "<Claude Code pid>@<its
// start time>". Voitta's headersHelper for the voitta MCP entry prints the same
// (llmgw/window_header.py, same shell lines; a test keeps them identical).
const WINDOW_ID_SH = "p=$PPID\ncase \"$(ps -o comm= -p \"$p\" 2>/dev/null)\" in\n  sh|*/sh|bash|*/bash|zsh|*/zsh|dash|*/dash|-*) p=$(ps -o ppid= -p \"$p\" 2>/dev/null | tr -d ' ') ;;\nesac\ns=$(ps -o lstart= -p \"$p\" 2>/dev/null)\ns=$(echo $s | tr ' ' '_')\nprintf '%s@%s' \"$p\" \"$s\""

async function windowId($: Engine): Promise<string | null> {
  try {
    const r = await $.process.run(['/bin/sh', '-c', WINDOW_ID_SH], { timeoutMs: 5000 })
    const id = r.stdout.trim()
    return r.exitCode === 0 && /^\d{1,9}@[A-Za-z0-9_:]{1,64}$/.test(id) ? id : null
  } catch {
    return null
  }
}

type PopupState = { status: string; id?: string; detail?: string }

async function popupCall($: Engine, base: string, path: string, body?: unknown): Promise<PopupState | null> {
  try {
    const r = await $.http.fetch(base + path, body === undefined ? undefined : {
      method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body),
    })
    return r.ok ? (JSON.parse(r.text) as PopupState) : null
  } catch {
    return null
  }
}

// Shows Voitta's tool popup for this window and waits for the answer. Never
// throws: every failure just lets /mcp open as it would have.
// True when the popup was answered with OK and this window's tools are set:
// /mcp then ends there, without Claude Code's menu.
async function windowTools($: Engine): Promise<boolean> {
  let base: string
  try {
    base = await gatewayUrl($)
  } catch {
    return false
  }
  const window = await windowId($)
  if (!window) return false
  const start = await popupCall($, base, '/_voitta/llm/api/tool-popup', { window })
  if (!start) return false
  if (start.status === 'busy') {
    $.ui.toast('Voitta: another tool popup is open', { timeoutMs: 3000 })
    return false
  }
  if (start.status === 'not_linked') {
    $.ui.toast("Voitta can't tell this window's MCP connection; Reinstall /llm in Settings → LLMs, then open a new window",
      { timeoutMs: 5000 })
    return false
  }
  if (start.status !== 'waiting' || !start.id) return false
  // Long-polls of 5 s (one request is cut off at 30 s); about ten minutes in all.
  for (let i = 0; i < 120; i++) {
    const state = await popupCall($, base, `/_voitta/llm/api/tool-popup/${start.id}?wait=5`)
    if (!state) return false
    if (state.status === 'waiting') continue
    return state.status === 'applied'
  }
  return false
}

async function show($: Engine, v: LlmView) {
  windowPick = v.pick
  await update($, view, () => v)
  await update($, error, () => null)
  $.ui.status(statusText(v))
}

async function refresh($: Engine): Promise<LlmView> {
  const v = await call($, 'GET', `/_voitta/llm/api/llm/options?session=${encodeURIComponent(await $.session.id())}`)
  await show($, v)
  return v
}

async function pick($: Engine, account: string | null): Promise<LlmView> {
  const session = await $.session.id()
  const v = await call($, 'PUT', `/_voitta/llm/api/llm/sessions/${encodeURIComponent(session)}`, {
    account: account ?? 'default',
  })
  await show($, v)
  return v
}

async function fail($: Engine, err: unknown): Promise<string> {
  const message = err instanceof Error ? err.message : String(err)
  await update($, error, () => message)
  $.ui.status('LLM: Voitta Desktop unavailable')
  return message
}

// At startup a window may simply not go through Voitta Desktop (the mod is
// installed for every window): say nothing until the person runs /llm.
async function quiet($: Engine, err: unknown) {
  const message = err instanceof Error ? err.message : String(err)
  await update($, error, () => message)
  $.ui.status(undefined)
}

function picked(v: LlmView): string {
  const o = v.pick ? v.options.find(x => x.id === v.pick) : undefined
  const now = o ? optionLine(o) : `the default, ${v.default ? optionLine(v.default) : 'none set'}`
  const warn = o && o.status !== 'ok' ? `\nIts requests will fail until this is fixed on ${v.ui}.` : ''
  return `This window now uses ${now}.${warn}`
}

function menu(v: LlmView): string {
  const lines = v.options.map((o, i) => `  ${i + 1}. ${optionLine(o)}${o.id === v.pick ? '  ← this window' : ''}`)
  const current = v.pick ? '' : '  ← this window'
  return [
    `  0. Default: ${v.default ? optionLine(v.default) : 'none set'}${current}`,
    ...lines,
    '',
    'Type /llm <number or name> to switch, /llm default to follow the default.',
    `Models per account are set on ${v.ui}.`,
  ].join('\n')
}

// The picker is for picking; the status line is the lasting display. So
// the panel goes away on a pick, on Esc, or as soon as the person sends
// anything, never lingering above the prompt.
let paneOpen = false

async function closePane($: Engine) {
  if (paneOpen) {
    paneOpen = false
    await $.ui.close({ id: PANE })
  }
}

export const register: Register = on => {
  on('prompt.submit', async ($, e, next) => {
    await closePane($)
    return next(e)
  })

  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'llm',
      description: "Pick which LLM account this window uses (Voitta Desktop)",
      argumentHint: '[number | name | default]',
    })
    await $.command.register({
      name: 'voitta-store',
      description: "Store this window's whole conversation in Voitta Desktop",
    })
    refresh($).catch(err => quiet($, err))
    return next(e)
  })

  // A /clear moves the window to a new session id without a session.start;
  // the new id starts with no pick, so hand it the window's.
  on('classic.SessionStart', async ($, e, next) => {
    transcriptPath = e.transcript_path ?? transcriptPath
    const result = await next(e)
    if (e.source === 'clear' && windowPick) {
      await pick($, windowPick).catch(err => fail($, err))
    } else if (e.source !== 'startup') {
      await refresh($).catch(err => quiet($, err))
    }
    return result
  })

  on('command.run', { command: 'llm' }, async ($, e) => {
    const arg = e.args.trim()
    try {
      if (!arg) {
        const v = await refresh($)
        const opened = await $.ui.open({ id: PANE, title: 'LLM for this window', focus: true })
        if (opened.isPlaced) {
          paneOpen = true
          return { text: 'Pick an account in the panel (Esc closes it).' }
        }
        await $.ui.close({ id: PANE })
        return { text: `${statusText(v)}\n\n${menu(v)}` }
      }
      if (arg === 'default' || arg === '0') {
        return { text: picked(await pick($, null)) }
      }
      const v = await refresh($)
      const n = Number(arg)
      let match: LlmOption | undefined
      if (Number.isInteger(n) && n >= 1 && n <= v.options.length) {
        match = v.options[n - 1]
      } else {
        const want = arg.toLowerCase()
        const hits = v.options.filter(o =>
          o.id.toLowerCase() === want ||
          [o.label, o.provider, o.provider_label].some(s => s.toLowerCase().includes(want)),
        )
        if (hits.length > 1) {
          return { text: `"${arg}" matches several accounts:\n${hits.map(o => `  • ${optionLine(o)}`).join('\n')}\nBe more specific, or use the number from /llm.` }
        }
        match = hits[0]
      }
      if (!match) {
        return { text: `No account matches "${arg}".\n\n${menu(v)}` }
      }
      return { text: picked(await pick($, match.id)) }
    } catch (err) {
      return { text: `/llm: ${await fail($, err)}` }
    }
  })

  // Nothing in the transcript: a one-second toast says it worked; a failure
  // stays a little longer so it can be read.
  // OK in the popup ends /mcp there, with one line: Claude Code's menu has
  // nothing left to do. Cancel, or no popup at all, opens the menu as usual.
  on('command.run', { command: 'mcp' }, async ($, e, next) => {
    if (await windowTools($)) {
      return { text: "Voitta: this window's tools updated" }
    }
    return next(e)
  })

  on('command.run', { command: 'voitta-store' }, async $ => {
    try {
      await storeConversation($)
      $.ui.toast('Conversation stored', { timeoutMs: 1000 })
    } catch (err) {
      $.ui.toast(`Not stored: ${err instanceof Error ? err.message : String(err)}`, { timeoutMs: 5000 })
    }
    return {}
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    if (e.surface === 'mobile') {
      // The mobile app draws no picker yet.
      const { Text } = $.ui.resolve(e)
      return <Text>Type /llm to see the accounts, then /llm followed by a number or name.</Text>
    }
    const { Box, Select, Text } = $.ui.resolve(e)
    const v = await read($, view)
    const problem = await read($, error)
    if (problem) {
      return <Text color="red">{problem}</Text>
    }
    if (!v) {
      return <Text dimColor>Asking Voitta Desktop…</Text>
    }
    const options = [
      { value: 'default', label: `Default: ${v.default ? optionLine(v.default) : 'none set'}` },
      ...v.options.map(o => ({ value: o.id, label: optionLine(o) })),
    ]
    return (
      <Box flexDirection="column">
        <Select
          key="account"
          label="Account"
          options={options}
          value={v.pick ?? 'default'}
          autoFocus
          onSelect={async (value: string) => {
            try {
              const next = await pick($, value === 'default' ? null : value)
              $.ui.toast(picked(next).split('\n')[0] ?? '')
              await closePane($)
            } catch (err) {
              await fail($, err)
            }
          }}
        />
        <Text dimColor>Models per account are set on {v.ui}</Text>
      </Box>
    )
  })
}
