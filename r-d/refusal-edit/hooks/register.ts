import type { EngineInterface, Register, SessionMessage } from 'claude-code'

type Recovery = 'rewind' | 'drop' | 'keep'

// Set while this mod's own compaction runs: the prompt to cut the conversation at.
let cutAt: string | null = null

// Shown in place of the engine's compaction line, if the engine draws the stored text.
const DROP_NOTE = 'Refused message removed. Nothing was summarized.'

// The index of the user message that opened the refused turn: the last typed
// prompt (not a tool result) holding the text, else the last typed prompt.
export function promptIndex(messages: readonly SessionMessage[], text: string): number {
  const isPrompt = (m: SessionMessage) => m.role === 'user' && !m.toolResults?.length
  const i = messages.findLastIndex(m => isPrompt(m) && m.text.includes(text))
  return i >= 0 ? i : messages.findLastIndex(isPrompt)
}

export function recoveryOf(value: unknown): Recovery {
  return value === 'drop' || value === 'keep' ? value : 'rewind'
}

// Puts the prompt back in the box, below anything already typed there.
async function fillBox($: EngineInterface, text: string): Promise<void> {
  const box = await $.prompt.read()
  await $.prompt.fill(
    box.text.trim() === '' ? { text, mode: 'replace' } : { text: `\n${text}`, mode: 'append' },
  )
}

// Removes the refused exchange from the conversation without a summary. The
// engine compacts only between turns, so a call made while the turn that ran
// this is still closing is retried until the session is idle.
async function dropTurn($: EngineInterface, text: string): Promise<void> {
  cutAt = text
  try {
    for (let attempt = 0; ; attempt++) {
      try {
        await $.session.compact()
        return
      } catch (error) {
        if (!String(error).includes('in flight') || attempt >= 40) throw error
        await $.clock.sleep(250)
      }
    }
  } catch (error) {
    $.ui.log(`could not drop the refused turn: ${String(error)}`)
  } finally {
    cutAt = null
  }
}

async function refill($: EngineInterface, text: string, mode: Recovery): Promise<void> {
  if (mode === 'rewind') {
    // The built-in rewind: truncates the conversation and refills the box itself.
    try {
      await $.command.run({ command: 'rewind' })
      return
    } catch (error) {
      $.ui.log(`could not open the rewind menu, dropping instead: ${String(error)}`)
      mode = 'drop'
    }
  }
  if (mode === 'drop') {
    await dropTurn($, text)
  }
  await fillBox($, text)
}

// What follows a refusal: say why, then recover once the turn has fully ended.
// The reason goes in the transcript too: the rewind menu covers the toast.
function recover($: EngineInterface, text: string, why: string, mode: Recovery): void {
  const next = mode === 'rewind'
    ? 'Press ↑ to pick your message, then Enter twice to restore and edit it.'
    : "It's back in the box to edit."
  const message = `Prompt refused: ${why}. ${next}`
  $.ui.log(message)
  $.ui.toast(message, { timeoutMs: 12000 })
  $.clock.after(0, () => {
    refill($, text, mode).catch(error => $.ui.log(String(error)))
  })
}

export const register: Register = (on, options) => {
  const prompts = new Map<string, string>()
  const configured = recoveryOf(options.recovery)

  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'refusal-test',
      description: 'Act as if your last prompt was refused (tests refusal-edit)',
      argumentHint: '[rewind|drop|keep]',
    })
    return next(e)
  })

  on('turn.start', ($, e, next) => {
    prompts.set(e.turnId, e.text)
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    const result = await next(e)
    const text = prompts.get(e.turnId)
    prompts.delete(e.turnId)

    if (e.agentId === undefined && e.reason === 'refusal' && text) {
      const why = e.refusal.explanation ?? e.refusal.category ?? 'no reason given'
      recover($, text, why, configured)
    }
    return result
  })

  // Runs the same recovery on the last typed prompt, with no real refusal.
  on('command.run', { command: 'refusal-test' }, async ($, e) => {
    const messages = await $.session.messages()
    const last = messages[promptIndex(messages, '')]
    if (!last?.text) {
      return { text: 'refusal-test: send a prompt first, then run this.' }
    }
    const mode = e.args.trim() === '' ? configured : recoveryOf(e.args.trim())
    recover($, last.text, `simulated refusal (/refusal-test, ${mode})`, mode)
    return { text: `refusal-test: simulating a refusal of your last prompt (${mode}).` }
  })

  on('session.compact', { trigger: 'plugin' }, ($, e, next) => {
    if (cutAt === null || e.agentId !== undefined) {
      return next(e)
    }

    const i = promptIndex(e.messages, cutAt)
    if (i < 0) {
      return { skip: 'refusal-edit: the refused prompt was not found' }
    }

    // Messages kept with their handles stand as the engine's own; no summary is made.
    return { messages: e.messages.slice(0, i) }
  })

  // Rewords the notice our own compaction leaves, where the engine draws its text.
  on('session.append', ($, e, next) => {
    if (cutAt === null || (e.door !== 'compaction' && e.door !== 'notice')) {
      return next(e)
    }
    const content = e.message.content.map(block =>
      block.type === 'text' ? { ...block, text: DROP_NOTE } : block,
    )
    return next({ ...e, message: { ...e.message, content } })
  })
}
