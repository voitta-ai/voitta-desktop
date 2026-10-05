import { expect, mock, test } from 'claude-code/testing'
import type { On, SessionMessage } from 'claude-code'

import { promptIndex } from '../hooks/register'

const refusal = { category: 'cyber', explanation: 'flagged by the classifier' }

const msg = (role: 'user' | 'assistant', text: string, toolResults?: SessionMessage['toolResults']): SessionMessage =>
  ({ role, text, toolUses: [], ...(toolResults ? { toolResults } : {}) })

// Stands in for the engine beneath the plugin; records toasts, fills and compactions.
function engine(on: On, messages: SessionMessage[] = [], opts: { failCompaction?: boolean } = {}) {
  const seen = { filled: [] as string[], toasts: [] as string[], compactions: 0, commands: [] as string[], logs: [] as string[] }
  on('turn.start', (_, e) => ({ turnId: e.turnId }))
  on('turn.complete', (_, e) => ({ text: e.answer }))
  on('command.run', (_, e) => {
    seen.commands.push(e.command)
    return {}
  })
  on('session.messages', () => ({ value: messages }))
  // A real engine never leaves an empty conversation; `failCompaction` makes
  // it refuse instead.
  on('session.compact', (_, e) => {
    if (opts.failCompaction) return { skip: 'refused by the test engine' }
    seen.compactions += 1
    return { messages: e.messages?.length ? e.messages : [msg('user', '(conversation start)')] }
  })
  on('ui.toast', (_, e) => {
    seen.toasts.push(e.text)
    return { value: undefined }
  })
  on('ui.log', (_, e) => {
    seen.logs.push(e.text)
    return { value: undefined }
  })
  on('prompt.read', () => ({ value: { text: '', cursor: 0 } }))
  on('prompt.fill', (_, e) => {
    seen.filled.push(e.text)
    return { isFilled: true, text: e.text, cursor: e.text.length }
  })
  return seen
}

for (const recovery of ['drop', 'keep'] as const) {
  test(`a refused prompt goes back in the box (${recovery})`, { options: { recovery } }, async ($, on) => {
    const clock = mock.clock(on)
    const seen = engine(on)

    await $.turn.start({ turnId: 't1', text: 'the flagged prompt' })
    await $.turn.complete({
      turnId: 't1', reason: 'refusal', refusal, answer: '', durationMs: 10, isAborted: false,
    })
    await clock.advance(0)

    expect(seen.toasts).toEqual(["Prompt refused: flagged by the classifier. It's back in the box to edit."])
    expect(seen.filled).toEqual(['the flagged prompt'])
    expect(seen.compactions).toBe(recovery === 'drop' ? 1 : 0)
    expect(seen.commands).toEqual([])
    expect(seen.logs).toEqual(seen.toasts)
  })
}

// The retry while the refused turn is still closing ("a turn is in flight")
// can't be simulated here: the current test kit skips a hook that throws
// instead of passing the error up. See ../README.md.
test('drop mode still refills the box when the compaction is refused', { options: { recovery: 'drop' } }, async ($, on) => {
  const clock = mock.clock(on)
  const seen = engine(on, [], { failCompaction: true })

  await $.turn.start({ turnId: 't1', text: 'the flagged prompt' })
  await $.turn.complete({
    turnId: 't1', reason: 'refusal', refusal, answer: '', durationMs: 10, isAborted: false,
  })
  await clock.advance(0)

  expect(seen.compactions).toBe(0)
  expect(seen.filled).toEqual(['the flagged prompt'])
})

test('rewind mode opens the built-in rewind menu and does not compact', async ($, on) => {
  const clock = mock.clock(on)
  const seen = engine(on)

  await $.turn.start({ turnId: 't1', text: 'the flagged prompt' })
  await $.turn.complete({
    turnId: 't1', reason: 'refusal', refusal, answer: '', durationMs: 10, isAborted: false,
  })
  await clock.advance(0)

  expect(seen.toasts).toEqual([
    'Prompt refused: flagged by the classifier. Press ↑ to pick your message, then Enter twice to restore and edit it.',
  ])
  expect(seen.commands).toEqual(['rewind'])
  expect(seen.compactions).toBe(0)
  expect(seen.filled).toEqual([])
})

test('an answered turn leaves the box alone', async ($, on) => {
  const clock = mock.clock(on)
  const seen = engine(on)

  await $.turn.start({ turnId: 't2', text: 'hello' })
  await $.turn.complete({ turnId: 't2', reason: 'answer', answer: 'hi', durationMs: 10, isAborted: false })
  await clock.advance(0)

  expect(seen.toasts).toEqual([])
  expect(seen.filled).toEqual([])
})

test('/refusal-test simulates a refusal of the last prompt', async ($, on) => {
  const clock = mock.clock(on)
  const seen = engine(on, [msg('user', 'first prompt'), msg('assistant', 'ok'), msg('user', 'my last prompt'), msg('assistant', 'sure')])

  await $.command.run({
    command: 'refusal-test', args: 'keep', origin: { kind: 'composer' }, presentation: { isFullscreen: false, columns: 120 },
  })
  await clock.advance(0)

  expect(seen.toasts).toEqual(["Prompt refused: simulated refusal (/refusal-test, keep). It's back in the box to edit."])
  expect(seen.filled).toEqual(['my last prompt'])
})

test('the cut lands on the refused prompt, not a later tool result', () => {
  const messages = [
    msg('user', 'first prompt'),
    msg('assistant', 'ok'),
    msg('user', 'the flagged prompt'),
    msg('assistant', ''),
    msg('user', '', [{ tool_use_id: 'x', text: 'out', isError: false, result: undefined } as never]),
  ]
  expect(promptIndex(messages, 'the flagged prompt')).toBe(2)
  expect(promptIndex(messages, 'not there')).toBe(2)
  expect(promptIndex([], 'x')).toBe(-1)
})
