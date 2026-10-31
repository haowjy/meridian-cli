import type { EngineInterface, Register } from 'claude-code'

// Delivers Meridian's starting prompt as the first user turn of an interactive
// launch. Meridian names the file in `_MERIDIAN_CLAUDE_FIRST_TURN_PATH` only when
// it keeps the prompt off argv (so `pkill -f <word>` elsewhere cannot match it
// and `ps` cannot show it).
//
// `session.start` fires again after a reload or worker respawn, also after
// `/clear`, so a receipt written next to the prompt file marks the launch's
// prompt as delivered; the turn count cannot, because `/clear` empties it.

const RECEIPT = 'first-turn-delivered'

// The submit failed: leave the task in the composer so one Enter sends it.
async function strand($: EngineInterface, text: string, path: string, why: string): Promise<void> {
  await $.prompt.fill({ text })
  $.ui.toast(`Meridian could not submit the starting prompt (${why}); it is in the prompt box. Source: ${path}`, { timeoutMs: 30000 })
}

async function submitFirstTurn($: EngineInterface): Promise<void> {
  const path = await $.env.get('_MERIDIAN_CLAUDE_FIRST_TURN_PATH')
  if (path === undefined || path === '') return
  const receipt = path.replace(/[^/]*$/, RECEIPT)
  if (await $.fs.exists(receipt)) return
  const text = await $.fs.read(path)
  if (text.trim() === '') return
  let why: string
  try {
    const result = await $.prompt.submit({ text, asUser: true })
    if (result.drop === undefined) {
      await $.fs.write(receipt, '')
      return
    }
    why = `dropped: ${result.drop}`
  } catch (err) {
    why = String(err)
  }
  await strand($, text, path, why)
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    if (e.isInteractive === false || e.surface === null) return next(e)
    // Not awaited: the submit lands after the session has started (probe p10381).
    void submitFirstTurn($).catch(err => {
      $.ui.toast(`Meridian first-turn mod failed: ${String(err)}. See starting-prompt.md in the launch directory.`, { timeoutMs: 30000 })
    })
    return next(e)
  })
}
