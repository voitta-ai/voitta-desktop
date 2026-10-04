import { atom, read, update } from 'claude-code'
import type { Hook, Register } from 'claude-code'

import type { LlmOption, LlmView } from '../types'

// /llm: which account this Claude Code window's requests go to.
//
// The window talks to Voitta Desktop's LLM proxy through ANTHROPIC_BASE_URL, and
// every request carries Claude Code's session id. This mod only tells Voitta
// "session X -> account Y"; it never touches the requests themselves. Models
// per account are set in Voitta Desktop (Settings → LLMs).

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

async function call($: Engine, method: string, path: string, body?: unknown): Promise<LlmView> {
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
  return JSON.parse(r.text) as LlmView
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
    refresh($).catch(err => quiet($, err))
    return next(e)
  })

  // A /clear moves the window to a new session id without a session.start;
  // the new id starts with no pick, so hand it the window's.
  on('classic.SessionStart', async ($, e, next) => {
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
