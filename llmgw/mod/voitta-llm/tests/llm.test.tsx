import { expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'

import type { LlmOption } from '../types'

const GW = 'http://gw.test'

const OPTIONS: LlmOption[] = [
  { id: 'as-is', label: 'As is', provider: 'as_is', provider_label: "Claude Code's own login",
    status: 'ok', status_detail: '', main: null, background: null },
  { id: 'deepseek:k', label: 'DeepSeek API', provider: 'deepseek', provider_label: 'DeepSeek (API key)',
    status: 'ok', status_detail: '', main: 'deepseek-v4-pro', background: 'deepseek-flash' },
  { id: 'openai:a', label: 'me@x.com · prolite', provider: 'openai', provider_label: 'ChatGPT (subscription)',
    status: 'ok', status_detail: '', main: 'gpt-6.1-sol', background: 'gpt-6-luna' },
  { id: 'openai:b', label: 'me@x.com · team', provider: 'openai', provider_label: 'ChatGPT (subscription)',
    status: 'needs_login', status_detail: '', main: 'gpt-5.5', background: 'gpt-5.5' },
]

/** A fake gateway answering the mod's two calls, and a record of what it was asked. */
function gateway(on: On, opts: { session?: () => string; down?: boolean } = {}) {
  const picks: Record<string, string> = {}
  const puts: { session: string; account: string }[] = []
  const status: (string | undefined)[] = []
  const closed: string[] = []
  const toasts: { text: string; timeoutMs?: number }[] = []
  const stores: Record<string, unknown>[] = []
  mock.env(on, { ANTHROPIC_BASE_URL: `${GW}/` })
  on('session.id', () => ({ value: opts.session ? opts.session() : 'win-A' }))
  on('ui.status', (_$, e) => {
    status.push(e.text)
    return { value: undefined }
  })
  on('ui.toast', (_$, e) => {
    toasts.push({ text: e.text, timeoutMs: e.timeoutMs })
    return { value: undefined }
  })
  on('session.model', () => ({ value: 'claude-opus-5-5' }))
  on('session.cwd', () => ({ value: '/work/proj' }))
  on('ui.open', () => ({ value: { isPlaced: true as const } }))
  on('ui.close', (_$, e) => {
    closed.push(e.id)
    return { value: undefined }
  })
  on('classic.SessionStart', () => ({}))
  on('http.fetch', (_$, e) => {
    if (opts.down) {
      return { deny: 'connect ECONNREFUSED' }
    }
    const url = new URL(e.url)
    const view = (session: string) => ({
      session, pick: picks[session] ?? null, default: OPTIONS[0], options: OPTIONS, ui: 'http://localhost:18910',
    })
    if (e.init?.method === 'POST' && url.pathname === '/_voitta/llm/api/conversations') {
      stores.push(JSON.parse(e.init.body ?? '{}') as Record<string, unknown>)
      return { value: { status: 200, ok: true, headers: {}, text: JSON.stringify({
        path: '/home/.voitta-desktop/conversations/win-A.json', bytes: 1234, records: 10, user: 3,
        assistant: 4, subagents: 0, routed_requests: 2, unreadable_lines: 0 }) } }
    }
    if (e.init?.method === 'PUT') {
      const session = decodeURIComponent(url.pathname.split('/').pop() ?? '')
      const account = (JSON.parse(e.init.body ?? '{}') as { account: string }).account
      puts.push({ session, account })
      if (account === 'default') delete picks[session]
      else picks[session] = account
      return { value: { status: 200, ok: true, headers: {}, text: JSON.stringify(view(session)) } }
    }
    expect(url.origin + url.pathname).toBe(`${GW}/_voitta/llm/api/llm/options`)
    return { value: { status: 200, ok: true, headers: {}, text: JSON.stringify(view(url.searchParams.get('session') ?? '')) } }
  })
  return { picks, puts, status, closed, toasts, stores }
}

test('/llm <name> picks that account for this window', async ($, on) => {
  const gw = gateway(on)
  const r = await $.command.run({ command: 'llm', args: 'deepseek' })
  expect(gw.puts).toEqual([{ session: 'win-A', account: 'deepseek:k' }])
  expect(r.text).toContain('This window now uses DeepSeek API')
  expect(gw.status.at(-1)).toBe('LLM: DeepSeek API · deepseek-v4-pro')
})

test('/llm <number> and /llm default', async ($, on) => {
  const gw = gateway(on)
  await $.command.run({ command: 'llm', args: '3' })
  expect(gw.puts.at(-1)).toEqual({ session: 'win-A', account: 'openai:a' })
  const r = await $.command.run({ command: 'llm', args: 'default' })
  expect(gw.puts.at(-1)).toEqual({ session: 'win-A', account: 'default' })
  expect(r.text).toContain('the default, As is')
  expect(gw.status.at(-1)).toBe('LLM: default → As is')
})

test('an ambiguous or unknown name picks nothing', async ($, on) => {
  const gw = gateway(on)
  const several = await $.command.run({ command: 'llm', args: 'chatgpt' })
  expect(several.text).toContain('matches several accounts')
  const none = await $.command.run({ command: 'llm', args: 'mistral' })
  expect(none.text).toContain('No account matches "mistral"')
  expect(gw.puts).toEqual([])
})

test('picking an account that needs a login says so', async ($, on) => {
  gateway(on)
  const r = await $.command.run({ command: 'llm', args: 'team' })
  expect(r.text).toContain('needs login')
  expect(r.text).toContain('will fail until this is fixed')
})

test('gateway down: an error, nothing applied', async ($, on) => {
  const gw = gateway(on, { down: true })
  const r = await $.command.run({ command: 'llm', args: 'deepseek' })
  expect(r.text).toContain('is not reachable')
  expect(gw.status.at(-1)).toBe('LLM: Voitta Desktop unavailable')
})

test('a /clear carries the window\'s pick to its new session id', async ($, on) => {
  let session = 'win-A'
  const gw = gateway(on, { session: () => session })
  await $.command.run({ command: 'llm', args: 'deepseek' })
  session = 'win-A2'
  await $.classic.SessionStart({ source: 'clear', session_id: 'win-A2' })
  expect(gw.puts).toEqual([
    { session: 'win-A', account: 'deepseek:k' },
    { session: 'win-A2', account: 'deepseek:k' },
  ])
})

for (const surface of ['terminal', 'desktop'] as const) {
  test(`the picker pane picks on ${surface}`, async ($, on) => {
    const gw = gateway(on)
    await $.command.run({ command: 'llm', args: '' })
    const pane = await $.ui.mount({
      plugin: 'voitta-llm', surface, component: 'Pane', requestId: 'voitta-llm',
      props: { title: 'LLM for this window', isFocused: true, bodyColumns: 80, placement: 'dock' },
      viewport: { columns: 100, rows: 30 },
    })
    expect(await pane.find({ type: 'Text', text: /Models per account/ })).toBeTruthy()
    await $.ui.select({ plugin: 'voitta-llm', key: 'account', value: 'openai:a' })
    expect(gw.puts.at(-1)).toEqual({ session: 'win-A', account: 'openai:a' })
  })
}

test('the picker closes when the person sends a message, leaving only the status line', async ($, on) => {
  const gw = gateway(on)
  // stands in for the engine sending the prompt on
  on('prompt.submit', (_$, e) => ({ text: e.text }))
  const r = await $.command.run({ command: 'llm', args: '' })
  expect(r.text).toBe('Pick an account in the panel (Esc closes it).')
  await $.prompt.submit({ text: 'hello' })
  expect(gw.closed).toEqual(['voitta-llm'])
  // nothing more to close the second time
  await $.prompt.submit({ text: 'again' })
  expect(gw.closed).toEqual(['voitta-llm'])
})

test('/voitta-store stores this window and shows a one-second toast, nothing else', async ($, on) => {
  const gw = gateway(on)
  const r = await $.command.run({ command: 'voitta-store', args: '' })
  expect(gw.stores).toEqual([{ session: 'win-A', model: 'claude-opus-5-5', cwd: '/work/proj', transcript_path: null }])
  expect(gw.toasts).toEqual([{ text: 'Conversation stored', timeoutMs: 1000 }])
  expect(r.text).toBeUndefined()
  expect(gw.closed).toEqual([])
})

test('/voitta-store failing says so briefly and stores nothing', async ($, on) => {
  const gw = gateway(on, { down: true })
  const r = await $.command.run({ command: 'voitta-store', args: '' })
  expect(gw.stores).toEqual([])
  expect(gw.toasts.length).toBe(1)
  expect(gw.toasts[0]?.text).toContain('Not stored')
  expect(gw.toasts[0]?.timeoutMs).toBe(5000)
  expect(r.text).toBeUndefined()
})

// ---- /mcp: this window's own tools ---------------------------------------------

const WINDOW = '4242@Fri_Oct_23_22:26:07_2026'

/** A fake Voitta for /mcp: the popup's start answer, then the status answers in turn. */
function voittaPopup(on: On, opts: {
  start?: Record<string, unknown> | 404; statuses?: string[]; down?: boolean; noBase?: boolean; noWindow?: boolean
} = {}) {
  const seen: string[] = []   // in order: 'start', 'status', 'mcp opened'
  const toasts: string[] = []
  const bodies: unknown[] = []
  const statuses = [...(opts.statuses ?? [])]
  mock.env(on, opts.noBase ? {} : { ANTHROPIC_BASE_URL: GW })
  on('process.run', () => (opts.noWindow
    ? { deny: 'cannot start /bin/sh' }
    : { value: { exitCode: 0, stdout: `${WINDOW}\n`, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }))
  on('ui.toast', (_$, e) => {
    toasts.push(e.text)
    return { value: undefined }
  })
  on('http.fetch', (_$, e) => {
    if (opts.down) return { deny: 'connect ECONNREFUSED' }
    const url = new URL(e.url)
    if (url.pathname === '/_voitta/llm/api/tool-popup' && e.init?.method === 'POST') {
      seen.push('start')
      bodies.push(JSON.parse(e.init.body ?? '{}'))
      if (opts.start === 404) return { value: { status: 404, ok: false, headers: {}, text: 'Not Found' } }
      return { value: { status: 200, ok: true, headers: {}, text: JSON.stringify(opts.start ?? { status: 'waiting', id: 'p1' }) } }
    }
    expect(url.pathname).toBe('/_voitta/llm/api/tool-popup/p1')
    expect(url.searchParams.get('wait')).toBe('5')
    seen.push('status')
    return { value: { status: 200, ok: true, headers: {}, text: JSON.stringify({ status: statuses.shift() ?? 'waiting' }) } }
  })
  // Claude Code's own /mcp, beneath the mod: it opens only when the hook lets it.
  on('command.run', (_$, e) => {
    seen.push(`${e.command} opened`)
    return { text: '(the /mcp menu)' }
  })
  return { seen, toasts, bodies }
}

test('/mcp waits for the tool popup, then opens; the answer applied shows briefly', async ($, on) => {
  const v = voittaPopup(on, { statuses: ['waiting', 'waiting', 'applied'] })
  await $.command.run({ command: 'mcp', args: '' })
  expect(v.bodies).toEqual([{ window: WINDOW }])
  expect(v.seen).toEqual(['start', 'status', 'status', 'status', 'mcp opened'])
  expect(v.toasts).toEqual(["Voitta: this window's tools updated"])
})

test('/mcp: popup cancelled → /mcp opens, nothing said', async ($, on) => {
  const v = voittaPopup(on, { statuses: ['unchanged'] })
  await $.command.run({ command: 'mcp', args: '' })
  expect(v.seen).toEqual(['start', 'status', 'mcp opened'])
  expect(v.toasts).toEqual([])
})

for (const [why, opts, toasts] of [
  ['the popup is not in use (Claude Code popups not silenced)', { start: { status: 'not_needed' } }, []],
  ['an older Voitta without the popup', { start: 404 as const }, []],
  ['Voitta not running', { down: true }, []],
  ['another popup already open', { start: { status: 'busy' } }, ['Voitta: another tool popup is open']],
  ["this window's connection is not identifiable", { start: { status: 'not_linked' } },
    ["Voitta can't tell this window's MCP connection; Reinstall /llm in Settings → LLMs, then open a new window"]],
] as const) {
  test(`/mcp opens at once when ${why}`, async ($, on) => {
    const v = voittaPopup(on, opts as Parameters<typeof voittaPopup>[1])
    await $.command.run({ command: 'mcp', args: '' })
    expect(v.seen.filter(s => s === 'status')).toEqual([])
    expect(v.seen.at(-1)).toBe('mcp opened')
    expect(v.toasts).toEqual([...toasts])
  })
}

test('/mcp in a window that does not go through Voitta: untouched, nothing asked', async ($, on) => {
  const v = voittaPopup(on, { noBase: true })
  await $.command.run({ command: 'mcp', args: '' })
  expect(v.seen).toEqual(['mcp opened'])
})

test('/mcp when the window id cannot be worked out: untouched, nothing asked', async ($, on) => {
  const v = voittaPopup(on, { noWindow: true })
  await $.command.run({ command: 'mcp', args: '' })
  expect(v.seen).toEqual(['mcp opened'])
})
