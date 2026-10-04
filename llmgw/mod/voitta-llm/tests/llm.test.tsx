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
  mock.env(on, { ANTHROPIC_BASE_URL: `${GW}/` })
  on('session.id', () => ({ value: opts.session ? opts.session() : 'win-A' }))
  on('ui.status', (_$, e) => {
    status.push(e.text)
    return { value: undefined }
  })
  on('ui.toast', () => ({ value: undefined }))
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
  return { picks, puts, status, closed }
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
